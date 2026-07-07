// PLM Project Hub — page bodies.
//
// Overview (dashboard + quick checkpoint) and Files (items grid + track/link) are
// implemented here; History/Releases/Planning/Issues/Team/Settings are staged as
// honest placeholders filled in later phases. Every page reads only the cached
// rows on g_plmHub (filled by plm_state.cpp) and mutates via async st.rpc; on a
// successful mutation it bumps the generation so caches refetch.
#include "plm_hub.h"

#include <cstdarg>
#include <cstdio>
#include <cstring>
#include <string>

#include "imgui.h"

#include "core/app_state.h"
#include "core/async_rpc.h"
#include "ui_theme.h"

namespace ui {
namespace {

nlohmann::json pathParam(AppState& st) {
    nlohmann::json p;
    p["path"] = st.plmProjectPath();
    return p;
}

void accentButtonColors() {
    ImGui::PushStyleColor(ImGuiCol_Button,
        ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 0.85f));
    ImGui::PushStyleColor(ImGuiCol_ButtonHovered,
        ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 1.0f));
}

void dimText(const char* fmt, ...) {
    va_list ap; va_start(ap, fmt);
    ImGui::PushStyleColor(ImGuiCol_Text,
        ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], 1.0f));
    ImGui::TextV(fmt, ap);
    ImGui::PopStyleColor();
    va_end(ap);
}

// a small labelled count card
void statCard(const char* label, int n, const float* col) {
    const float S = ui::uiScale;
    ImGui::BeginGroup();
    ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(col[0], col[1], col[2], 1.0f));
    char buf[24]; std::snprintf(buf, sizeof(buf), "%d", n);
    ImGui::TextUnformatted(buf);
    ImGui::PopStyleColor();
    ImGui::PushStyleColor(ImGuiCol_Text,
        ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], 1.0f));
    ImGui::TextUnformatted(label);
    ImGui::PopStyleColor();
    ImGui::EndGroup();
    ImGui::SameLine(0, 26 * S);
}

void plmComingSoon(const char* title, const char* note) {
    plmSectionHeader(title);
    plmEmptyState("[ \xc2\xb7 ]", note,
                  "This page is being built out in the Project Hub.");
}

// ---- Overview -----------------------------------------------------------------

void doCheckpoint(AppState& st) {
    auto& H = g_plmHub;
    if (H.busy) return;
    H.busy = true; H.actionErr.clear(); H.actionOk.clear();
    nlohmann::json p = pathParam(st);
    p["message"] = H.checkpointMsg;  // empty is allowed → assist tier may draft
    st.rpc.call("plm_checkpoint", p, [&st](const core::RpcResult& r) {
        auto& H = g_plmHub;
        H.busy = false;
        if (!r.ok) { H.actionErr = r.err; return; }
        H.checkpointMsg[0] = '\0';
        H.actionOk = "Checkpoint saved.";
        st.toast(ui::ToastLevel::Success, "Checkpoint saved");
        plmInvalidate();
    });
}

}  // namespace

void plmDrawOverviewPage(AppState& st) {
    auto& H = g_plmHub;
    const float S = ui::uiScale;
    plmEnsureOverview(st);

    plmSectionHeader("Overview");
    if (!H.ovErr.empty()) { plmError(H.ovErr); return; }

    const auto& ov = H.overview;
    // stat cards
    statCard("Files",   ov.itemsCount, palette.accent);
    statCard("Unsaved", ov.dirtyCount, ov.dirtyCount ? palette.statusWarn : palette.textDim);
    statCard("Tasks",   ov.openTasks,  palette.textDim);
    statCard("Issues",  ov.openIssues, ov.openIssues ? palette.statusWarn : palette.textDim);
    ImGui::NewLine();
    ImGui::Dummy(ImVec2(0, 6 * S));

    // quick checkpoint — the basic loop, front and centre
    plmSectionHeader("Save a checkpoint");
    dimText("Record a point in this project's history you can return to.");
    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(6 * S, 5 * S));
    ImGui::SetNextItemWidth(-160 * S);
    bool enter = ImGui::InputTextWithHint("##plm_ckpt", "What changed? (optional)",
        H.checkpointMsg, sizeof(H.checkpointMsg), ImGuiInputTextFlags_EnterReturnsTrue);
    ImGui::PopStyleVar();
    ImGui::SameLine();
    accentButtonColors();
    bool save = ImGui::Button("Save checkpoint", ImVec2(150 * S, 0)) || enter;
    ImGui::PopStyleColor(2);
    if (save && !H.busy) doCheckpoint(st);
    if (H.busy) plmSpinner("Saving");
    plmError(H.actionErr);
    if (!H.actionOk.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.statusOk[0], palette.statusOk[1], palette.statusOk[2], 1.0f));
        ImGui::TextUnformatted(H.actionOk.c_str());
        ImGui::PopStyleColor();
    }

    ImGui::Dummy(ImVec2(0, 6 * S));
    ImGui::Columns(2, "##ov_cols", false);

    // recent activity
    plmSectionHeader("Recent activity");
    if (ov.recent.empty()) {
        dimText("No history yet.");
    } else {
        for (const auto& e : ov.recent) {
            ImGui::PushID(e.commit.c_str());
            plmRowPill(false, 2.0f);
            ImGui::BeginGroup();
            std::string headline = !e.message.empty() ? e.message
                                 : (!e.summary.empty() ? e.summary : "(saved)");
            ImGui::TextWrapped("%s", headline.c_str());
            dimText("  %s  %s", e.author.c_str(), e.authoredAt.c_str());
            ImGui::EndGroup();
            ImGui::Spacing();
            ImGui::PopID();
        }
    }
    ImGui::NextColumn();

    // my tasks + dirty docs
    plmSectionHeader("Open tasks");
    if (ov.tasks.empty()) {
        dimText("No open tasks.");
    } else {
        for (const auto& t : ov.tasks) {
            ImGui::PushID(t.id.c_str());
            plmRowPill(false, 1.3f);
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], 1.0f));
            ImGui::Text("[%s]", t.priority.empty() ? "p2" : t.priority.c_str());
            ImGui::PopStyleColor();
            ImGui::SameLine();
            ImGui::TextWrapped("%s", t.title.c_str());
            ImGui::Spacing();
            ImGui::PopID();
        }
    }
    if (ov.dirtyCount > 0) {
        ImGui::Dummy(ImVec2(0, 4 * S));
        plmSectionHeader("Unsaved files");
        for (const auto& it : ov.dirtyItems) {
            plmDocTypeTag(it.docType, it.kind);
            ImGui::SameLine();
            ImGui::TextWrapped("%s", it.path.c_str());
        }
    }
    ImGui::Columns(1);
}

// ---- Files --------------------------------------------------------------------

namespace {

struct KindFilter { const char* label; const char* match; };  // match "" = all
static const KindFilter kFilters[] = {
    {"All", ""}, {"Design", "design"}, {"Documents", "docs"}, {"Code", "code"},
};

bool passesFilter(const PlmItemRow& it, int f) {
    if (f == 0) return true;
    if (f == 1) return it.kind == "part" || it.kind == "assembly" ||
                       it.kind == "drawing" || it.kind == "ecad" || it.kind == "cam";
    if (f == 2) return it.kind == "requirements" ||
                       (it.kind == "deliverable" && it.docType != "software" &&
                        it.docType != "linked_repo");
    if (f == 3) return it.docType == "software" || it.docType == "linked_repo";
    return true;
}

void doTrack(AppState& st) {
    auto& H = g_plmHub;
    if (H.busy || !H.trackPath[0]) return;
    H.busy = true; H.actionErr.clear();
    nlohmann::json p = pathParam(st);
    p["file_path"] = H.trackPath;
    st.rpc.call("plm_item_add", p, [&st](const core::RpcResult& r) {
        auto& H = g_plmHub;
        H.busy = false;
        if (!r.ok) { H.actionErr = r.err; st.toast(ui::ToastLevel::Error, r.err); return; }
        H.trackPath[0] = '\0';
        st.toast(ui::ToastLevel::Success, "Tracking new file");
        plmInvalidate();
    });
}

void doLink(AppState& st) {
    auto& H = g_plmHub;
    if (H.busy || !H.linkUrl[0]) return;
    H.busy = true; H.actionErr.clear();
    nlohmann::json p = pathParam(st);
    p["url"] = H.linkUrl;
    p["name"] = H.linkName[0] ? H.linkName : "Linked code";
    st.rpc.call("plm_link_repo", p, [&st](const core::RpcResult& r) {
        auto& H = g_plmHub;
        H.busy = false;
        if (!r.ok) { H.actionErr = r.err; st.toast(ui::ToastLevel::Error, r.err); return; }
        H.linkUrl[0] = '\0'; H.linkName[0] = '\0';
        st.toast(ui::ToastLevel::Success, "Linked external code");
        plmInvalidate();
    });
}

void doUntrack(AppState& st, const std::string& itemId) {
    auto& H = g_plmHub;
    if (H.busy) return;
    H.busy = true;
    nlohmann::json p = pathParam(st);
    p["item_id"] = itemId;
    st.rpc.call("plm_item_untrack", p, [&st](const core::RpcResult& r) {
        auto& H = g_plmHub;
        H.busy = false;
        if (!r.ok) { st.toast(ui::ToastLevel::Error, r.err); return; }
        st.toast(ui::ToastLevel::Info, "Stopped tracking");
        plmInvalidate();
    });
}

}  // namespace

void plmDrawFilesPage(AppState& st) {
    auto& H = g_plmHub;
    const float S = ui::uiScale;
    plmEnsureItems(st);

    plmSectionHeader("Files");

    // track-a-file + link-code row
    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(6 * S, 4 * S));
    ImGui::SetNextItemWidth(240 * S);
    ImGui::InputTextWithHint("##track", "file or folder (relative to project)",
                             H.trackPath, sizeof(H.trackPath));
    ImGui::PopStyleVar();
    ImGui::SameLine();
    accentButtonColors();
    if (ImGui::Button("Track", ImVec2(70 * S, 0))) doTrack(st);
    ImGui::PopStyleColor(2);
    ImGui::SameLine(0, 18 * S);
    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(6 * S, 4 * S));
    ImGui::SetNextItemWidth(180 * S);
    ImGui::InputTextWithHint("##linkurl", "external code URL",
                             H.linkUrl, sizeof(H.linkUrl));
    ImGui::SameLine();
    ImGui::SetNextItemWidth(110 * S);
    ImGui::InputTextWithHint("##linkname", "name", H.linkName, sizeof(H.linkName));
    ImGui::PopStyleVar();
    ImGui::SameLine();
    if (ImGui::Button("Link code", ImVec2(90 * S, 0))) doLink(st);
    plmError(H.actionErr);

    // filter pills
    ImGui::Dummy(ImVec2(0, 4 * S));
    for (int i = 0; i < (int)(sizeof(kFilters) / sizeof(kFilters[0])); ++i) {
        if (i) ImGui::SameLine();
        bool sel = (H.kindFilter == i);
        ImGui::PushStyleColor(ImGuiCol_Button, sel
            ? ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 0.30f)
            : ImVec4(palette.button[0], palette.button[1], palette.button[2], 0.5f));
        if (ImGui::SmallButton(kFilters[i].label)) H.kindFilter = i;
        ImGui::PopStyleColor();
    }
    ImGui::Separator();
    ImGui::Spacing();

    if (H.itemsLoading && H.items.empty()) { plmSpinner("Loading"); return; }
    if (!H.itemsErr.empty()) { plmError(H.itemsErr); return; }

    int shown = 0;
    for (const auto& it : H.items) {
        if (!passesFilter(it, H.kindFilter)) continue;
        ++shown;
        ImGui::PushID(it.itemId.c_str());
        bool sel = (H.selectedItem == it.itemId);
        plmRowPill(sel, 1.4f);
        plmDocTypeTag(it.docType, it.kind);
        ImGui::SameLine();
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], 1.0f));
        ImGui::Text(" %-12.12s", it.partNumber.c_str());
        ImGui::PopStyleColor();
        ImGui::SameLine();
        ImGui::Text("%s", it.path.c_str());
        ImGui::SameLine(ImGui::GetContentRegionAvail().x - 140 * S);
        plmLifecycleBadge(it.lifecycle, !it.rev.empty() ? it.rev : it.workingRev);
        if (it.dirty) {
            ImGui::SameLine();
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(palette.statusWarn[0], palette.statusWarn[1], palette.statusWarn[2], 1.0f));
            ImGui::TextUnformatted("\xe2\x97\x8f");  // ● unsaved dot
            ImGui::PopStyleColor();
        }
        if (ImGui::IsItemClicked()) H.selectedItem = sel ? "" : it.itemId;

        if (sel) {
            ImGui::Indent(16 * S);
            dimText("%s  \xc2\xb7  %s", it.kind.c_str(),
                    it.docType.empty() ? "design document" : it.docType.c_str());
            if (ImGui::SmallButton("Where used")) {
                nlohmann::json p = pathParam(st);
                p["item_id"] = it.itemId;
                st.rpc.call("plm_where_used", p, [&st](const core::RpcResult& r) {
                    if (!r.ok) { st.toast(ui::ToastLevel::Error, r.err); return; }
                    int n = 0;
                    if (auto c = r.result.find("consumers");
                        c != r.result.end() && c->is_array()) n = (int)c->size();
                    st.toast(ui::ToastLevel::Info,
                             n ? (std::to_string(n) + " place(s) use this")
                               : "Not used elsewhere");
                });
            }
            ImGui::SameLine();
            ImGui::PushStyleColor(ImGuiCol_Button,
                ImVec4(palette.statusErr[0], palette.statusErr[1], palette.statusErr[2], 0.55f));
            if (ImGui::SmallButton("Stop tracking")) doUntrack(st, it.itemId);
            ImGui::PopStyleColor();
            ImGui::Unindent(16 * S);
        }
        ImGui::Spacing();
        ImGui::PopID();
    }
    if (shown == 0)
        plmEmptyState("(#)", "No files here yet",
                      "Save a design, or Track a document/folder above.");
}

// ---- History ------------------------------------------------------------------

void plmDrawHistoryPage(AppState& st) {
    auto& H = g_plmHub;
    const float S = ui::uiScale;
    plmEnsureHistory(st);

    plmSectionHeader("History");

    // scope toggle
    static const char* kScope[] = {"Design", "Planning", "All"};
    for (int i = 0; i < 3; ++i) {
        if (i) ImGui::SameLine();
        bool sel = (H.histScope == i);
        ImGui::PushStyleColor(ImGuiCol_Button, sel
            ? ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 0.30f)
            : ImVec4(palette.button[0], palette.button[1], palette.button[2], 0.5f));
        if (ImGui::SmallButton(kScope[i]) && H.histScope != i) {
            H.histScope = i;
            H.histGen = -1;  // force refetch for the new scope
        }
        ImGui::PopStyleColor();
    }
    ImGui::Separator();
    ImGui::Spacing();

    if (!H.histErr.empty()) { plmError(H.histErr); return; }
    if (H.histLoading && H.history.empty()) { plmSpinner("Loading"); return; }
    if (H.history.empty()) {
        plmEmptyState("[~]", "No history yet", "Save a checkpoint to start tracking.");
        return;
    }

    ImDrawList* dl = ImGui::GetWindowDrawList();
    float lineX = ImGui::GetCursorScreenPos().x + 10 * S;
    ImU32 lineCol = ImGui::ColorConvertFloat4ToU32(
        ImVec4(palette.border[0], palette.border[1], palette.border[2], palette.border[3]));
    float prevY = ImGui::GetCursorScreenPos().y;
    int idx = 0;
    for (const auto& e : H.history) {
        ImGui::PushID(e.commit.c_str());
        bool sel = (H.selectedEntry == e.commit);
        bool rel = (e.kind == "release");
        float rowY = ImGui::GetCursorScreenPos().y;
        float midY = rowY + ImGui::GetFrameHeight() * 0.5f;
        if (idx > 0) dl->AddLine(ImVec2(lineX, prevY), ImVec2(lineX, midY), lineCol, 2 * S);
        ImU32 dotCol = ImGui::ColorConvertFloat4ToU32(rel
            ? ImVec4(palette.statusOk[0], palette.statusOk[1], palette.statusOk[2], 1.0f)
            : ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 0.85f));
        dl->AddCircleFilled(ImVec2(lineX, midY), 5 * S, dotCol);

        ImGui::Indent(26 * S);
        plmRowPill(sel, 2.0f);
        ImGui::BeginGroup();
        if (rel) {
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(palette.statusOk[0], palette.statusOk[1], palette.statusOk[2], 1.0f));
            ImGui::TextUnformatted("[Release]");
            ImGui::PopStyleColor();
            ImGui::SameLine();
        }
        std::string headline = !e.message.empty() ? e.message
                             : (!e.summary.empty() ? e.summary : "(saved)");
        ImGui::TextWrapped("%s", headline.c_str());
        dimText("  %s  %s", e.author.c_str(), e.authoredAt.c_str());
        ImGui::EndGroup();
        if (ImGui::IsItemClicked()) H.selectedEntry = sel ? "" : e.commit;

        if (sel) {
            ImGui::PushStyleColor(ImGuiCol_Button,
                ImVec4(palette.button[0], palette.button[1], palette.button[2], 0.8f));
            if (!H.busy && ImGui::SmallButton("Restore this version")) {
                H.busy = true;
                nlohmann::json p = pathParam(st);
                p["commit"] = e.commit;
                st.rpc.call("plm_restore", p, [&st](const core::RpcResult& r) {
                    g_plmHub.busy = false;
                    if (!r.ok) { st.toast(ui::ToastLevel::Error, r.err); return; }
                    st.toast(ui::ToastLevel::Success, "Restored — saved as the latest version");
                    plmInvalidate();
                });
            }
            ImGui::PopStyleColor();
        }
        ImGui::Unindent(26 * S);
        ImGui::Spacing();
        prevY = midY;
        ++idx;
        ImGui::PopID();
    }
}

// ---- Releases -----------------------------------------------------------------

namespace {
static std::string s_releaseItem;   // item whose release form is open
static char        s_releaseNote[256] = {};

void doRelease(AppState& st, const std::string& itemId, bool confirm) {
    auto& H = g_plmHub;
    if (H.busy) return;
    H.busy = true; H.actionErr.clear();
    nlohmann::json p = pathParam(st);
    p["item_id"] = itemId;
    p["message"] = s_releaseNote;
    p["confirm"] = confirm;
    st.rpc.call("plm_release", p, [&st](const core::RpcResult& r) {
        auto& H = g_plmHub;
        H.busy = false;
        if (!r.ok) { H.actionErr = r.err; return; }
        std::string rev = r.result.value("rev", "");
        st.toast(ui::ToastLevel::Success, "Released revision " + rev);
        s_releaseItem.clear(); s_releaseNote[0] = '\0';
        plmInvalidate();
    });
}
}  // namespace

void plmDrawReleasesPage(AppState& st) {
    auto& H = g_plmHub;
    const float S = ui::uiScale;
    plmEnsureItems(st);

    plmSectionHeader("Releases");
    dimText("Releasing stamps the next revision, freezes the design, and — for "
            "assemblies — pins the bill of materials.");
    ImGui::Spacing();

    if (!H.itemsErr.empty()) { plmError(H.itemsErr); return; }
    if (H.items.empty()) {
        plmEmptyState("(#)", "Nothing to release yet",
                      "Add a design or document, then release it here.");
        return;
    }

    for (const auto& it : H.items) {
        ImGui::PushID(it.itemId.c_str());
        plmRowPill(false, 1.4f);
        plmDocTypeTag(it.docType, it.kind);
        ImGui::SameLine();
        ImGui::Text("%s", it.path.c_str());
        ImGui::SameLine(ImGui::GetContentRegionAvail().x - 200 * S);
        plmLifecycleBadge(it.lifecycle, !it.rev.empty() ? it.rev : it.workingRev);
        ImGui::SameLine(ImGui::GetContentRegionAvail().x - 90 * S);
        bool open = (s_releaseItem == it.itemId);
        if (it.lifecycle == "Released") {
            dimText("released");
        } else if (ImGui::SmallButton(open ? "Cancel" : "Release\xe2\x80\xa6")) {
            s_releaseItem = open ? "" : it.itemId;
            s_releaseNote[0] = '\0';
            H.actionErr.clear();
        }
        if (open) {
            ImGui::Indent(16 * S);
            ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(6 * S, 4 * S));
            ImGui::SetNextItemWidth(-1);
            ImGui::InputTextWithHint("##relnote", "release note (what this revision is)",
                                     s_releaseNote, sizeof(s_releaseNote));
            ImGui::PopStyleVar();
            accentButtonColors();
            if (!H.busy && ImGui::Button("Release", ImVec2(120 * S, 0)))
                doRelease(st, it.itemId, false);
            ImGui::PopStyleColor(2);
            if (!H.actionErr.empty()) {
                plmError(H.actionErr);
                ImGui::PushStyleColor(ImGuiCol_Button,
                    ImVec4(palette.statusWarn[0], palette.statusWarn[1], palette.statusWarn[2], 0.6f));
                if (!H.busy && ImGui::Button("Release anyway", ImVec2(140 * S, 0)))
                    doRelease(st, it.itemId, true);
                ImGui::PopStyleColor();
            }
            if (H.busy) plmSpinner("Releasing");
            ImGui::Unindent(16 * S);
        }
        ImGui::Spacing();
        ImGui::PopID();
    }
}

// ---- Team & Sync --------------------------------------------------------------

void plmDrawTeamPage(AppState& st) {
    (void)st;
    plmSectionHeader("Team & Sync");
    dimText("Share a project through a shared folder or your own server. Everyone "
            "keeps working; Sync brings changes together without ever merging or "
            "losing work — if two people change the same design, both versions are "
            "kept.");
    ImGui::Spacing();
    plmSectionHeader("Shared locations");
    dimText("No shared location connected yet.");
    ImGui::Spacing();
    ImGui::BeginDisabled(true);
    ImGui::Button("Connect a shared folder\xe2\x80\xa6");
    ImGui::SameLine();
    ImGui::Button("Sync now");
    ImGui::EndDisabled();
    ImGui::Spacing();
    dimText("Sharing and Sync arrive in a later update — the project is fully "
            "usable on this machine today.");
}

// ---- Settings -----------------------------------------------------------------

void plmDrawSettingsPage(AppState& st) {
    auto& H = g_plmHub;
    const float S = ui::uiScale;
    plmEnsureSettings(st);

    plmSectionHeader("Settings");
    if (!H.setErr.empty()) { plmError(H.setErr); return; }

    // part-number scheme
    plmSectionHeader("Part-number scheme");
    dimText("New items are numbered <prefix>-0001, <prefix>-0002, … You can always "
            "rename a number later.");
    if (H.prefixBuf[0] == '\0' && !H.partPrefix.empty())
        std::snprintf(H.prefixBuf, sizeof(H.prefixBuf), "%s", H.partPrefix.c_str());
    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(6 * S, 4 * S));
    ImGui::SetNextItemWidth(160 * S);
    ImGui::InputTextWithHint("##prefix", "PRJ", H.prefixBuf, sizeof(H.prefixBuf));
    ImGui::PopStyleVar();
    ImGui::SameLine();
    accentButtonColors();
    if (!H.busy && ImGui::Button("Save", ImVec2(80 * S, 0)) && H.prefixBuf[0]) {
        H.busy = true;
        nlohmann::json p = pathParam(st);
        p["prefix"] = H.prefixBuf;
        st.rpc.call("plm_scheme_set", p, [&st](const core::RpcResult& r) {
            g_plmHub.busy = false;
            if (!r.ok) { st.toast(ui::ToastLevel::Error, r.err); return; }
            st.toast(ui::ToastLevel::Success, "Part-number scheme updated");
            plmInvalidate();
        });
    }
    ImGui::PopStyleColor(2);

    ImGui::Dummy(ImVec2(0, 8 * S));
    plmSectionHeader("AI assistance");
    dimText("How much the assistant helps with history. Manual controls are always "
            "available.");
    struct Mode { const char* id; const char* label; };
    static const Mode kModes[] = {
        {"manual", "Manual"}, {"assisted", "Assisted"}, {"managed", "Managed"},
    };
    for (int i = 0; i < 3; ++i) {
        if (i) ImGui::SameLine();
        bool sel = (H.aiMode == kModes[i].id);
        ImGui::PushStyleColor(ImGuiCol_Button, sel
            ? ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 0.30f)
            : ImVec4(palette.button[0], palette.button[1], palette.button[2], 0.5f));
        if (ImGui::Button(kModes[i].label) && !H.busy && !sel) {
            H.busy = true;
            nlohmann::json p = pathParam(st);
            p["mode"] = kModes[i].id;
            st.rpc.call("plm_set_ai_mode", p, [&st](const core::RpcResult& r) {
                g_plmHub.busy = false;
                if (!r.ok) { st.toast(ui::ToastLevel::Error, r.err); return; }
                st.toast(ui::ToastLevel::Info, "Assistance level updated");
                plmInvalidate();
            });
        }
        ImGui::PopStyleColor();
    }
}

// ---- staged placeholders (Phase 5) --------------------------------------------

void plmDrawPlanningPage(AppState&) { plmComingSoon("Planning", "Task board and milestones."); }
void plmDrawIssuesPage(AppState&)   { plmComingSoon("Issues",   "Issue log linked to items and revisions."); }

}  // namespace ui
