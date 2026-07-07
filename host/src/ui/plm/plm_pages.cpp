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

// ---- staged placeholders (Phases 4-5) -----------------------------------------

void plmDrawHistoryPage(AppState&)  { plmComingSoon("History",  "Timeline, compare and restore."); }
void plmDrawReleasesPage(AppState&) { plmComingSoon("Releases", "Release board, pre-flight and BOM freeze."); }
void plmDrawPlanningPage(AppState&) { plmComingSoon("Planning", "Task board and milestones."); }
void plmDrawIssuesPage(AppState&)   { plmComingSoon("Issues",   "Issue log linked to items and revisions."); }
void plmDrawTeamPage(AppState&)     { plmComingSoon("Team & Sync", "Shared folders, locks and one-button Sync."); }
void plmDrawSettingsPage(AppState&) { plmComingSoon("Settings", "Part-number scheme, AI assist and customization."); }

}  // namespace ui
