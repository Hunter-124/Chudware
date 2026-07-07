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
#include <ctime>
#include <string>

#include "imgui.h"

#include "core/app_state.h"
#include "core/async_rpc.h"
#include "ui/file_dialog.h"
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

// A self-bounding "card": an auto-height bordered child whose rounded background
// exactly wraps its content with even internal padding. Used by the kanban task
// cards and single-line PLM rows so controls sit padded and vertically centred
// inside their box instead of clipping out the top edge — the failure mode of the
// old fixed-height plmRowPill, which guessed a height and drew content hugging its
// top. AutoResizeY makes the box grow to fit wrapped titles and expanded controls.
void plmCardBegin(const char* id, bool sel, float padX = 8.0f, float padY = 6.0f) {
    const float S = ui::uiScale;
    ImGui::PushStyleColor(ImGuiCol_ChildBg, sel
        ? ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 0.20f)
        : ImVec4(palette.text[0], palette.text[1], palette.text[2], 0.055f));
    ImGui::PushStyleColor(ImGuiCol_Border, sel
        ? ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 0.55f)
        : ImVec4(palette.text[0], palette.text[1], palette.text[2], 0.14f));
    ImGui::PushStyleVar(ImGuiStyleVar_ChildRounding, 6.0f * S);
    ImGui::PushStyleVar(ImGuiStyleVar_ChildBorderSize, 1.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(padX * S, padY * S));
    ImGui::BeginChild(id, ImVec2(0, 0),
                      ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_Borders);
}

// Close a card opened with plmCardBegin. Returns true if the card body was clicked
// outside any inner widget (for row/card selection). Reads hover state while the
// child is still current, so call it before doing anything else.
bool plmCardEnd(float gap = 4.0f) {
    const float S = ui::uiScale;
    bool clicked = ImGui::IsWindowHovered() &&
                   ImGui::IsMouseClicked(ImGuiMouseButton_Left) &&
                   !ImGui::IsAnyItemHovered();
    ImGui::EndChild();
    ImGui::PopStyleVar(3);
    ImGui::PopStyleColor(2);
    ImGui::Dummy(ImVec2(0, gap * S));
    return clicked;
}

// Dim helper text. Wraps to the content-region width so long paragraphs never
// run off the right edge (short inline uses after SameLine wrap harmlessly).
void dimText(const char* fmt, ...) {
    va_list ap; va_start(ap, fmt);
    ImGui::PushStyleColor(ImGuiCol_Text,
        ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], 1.0f));
    ImGui::TextWrappedV(fmt, ap);
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
        if (!plmOk(r, H.actionErr)) return;
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
    statCard("Files",      ov.itemsCount,     palette.accent);
    statCard("Unsaved",    ov.dirtyCount,     ov.dirtyCount ? palette.statusWarn : palette.textDim);
    statCard("Tasks",      ov.openTasks,      palette.textDim);
    statCard("Issues",     ov.openIssues,     ov.openIssues ? palette.statusWarn : palette.textDim);
    statCard("Milestones", ov.openMilestones, palette.textDim);
    ImGui::NewLine();
    if (!ov.nextMilestone.empty())
        dimText("Next milestone: %s", ov.nextMilestone.c_str());
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
            plmCardBegin("##act", false);
            std::string headline = !e.message.empty() ? e.message
                                 : (!e.summary.empty() ? e.summary : "(saved)");
            ImGui::TextWrapped("%s", headline.c_str());
            dimText("%s  \xc2\xb7  %s", e.author.c_str(), e.authoredAt.c_str());
            plmCardEnd();
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
            plmCardBegin("##ovt", false);
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], 1.0f));
            ImGui::AlignTextToFramePadding();
            ImGui::Text("[%s]", t.priority.empty() ? "p2" : t.priority.c_str());
            ImGui::PopStyleColor();
            ImGui::SameLine();
            ImGui::AlignTextToFramePadding();
            ImGui::TextWrapped("%s", t.title.c_str());
            plmCardEnd();
            ImGui::PopID();
        }
    }
    if (ov.dirtyCount > 0) {
        ImGui::Dummy(ImVec2(0, 4 * S));
        plmSectionHeader("Unsaved files");
        for (const auto& it : ov.dirtyItems) {
            plmDocTypeTag(it.docType, it.kind);
            ImGui::SameLine();
            ImGui::AlignTextToFramePadding();
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
        if (!plmOk(r, H.actionErr)) { st.toast(ui::ToastLevel::Error, H.actionErr); return; }
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
    p["name"] = H.linkName[0] ? H.linkName : "Linked repo";
    if (H.linkCheckout[0]) p["checkout_path"] = H.linkCheckout;  // local clone for pinning
    st.rpc.call("plm_link_repo", p, [&st](const core::RpcResult& r) {
        auto& H = g_plmHub;
        H.busy = false;
        if (!plmOk(r, H.actionErr)) { st.toast(ui::ToastLevel::Error, H.actionErr); return; }
        H.linkUrl[0] = '\0'; H.linkName[0] = '\0'; H.linkCheckout[0] = '\0';
        st.toast(ui::ToastLevel::Success, "Linked GitHub repo");
        plmInvalidate();
    });
}

// Take an absolute path and store it in `dst`, made relative to the project folder
// (plm_item_add expects a project-relative path). A pick outside the project is
// rejected with a toast. Empty `abs` (dialog cancelled) is a silent no-op.
void storeProjectRelative(AppState& st, std::string abs, char* dst, size_t dstSz) {
    if (abs.empty()) return;
    std::string root = st.plmProjectPath();
    for (char& c : abs)  if (c == '\\') c = '/';
    for (char& c : root) if (c == '\\') c = '/';
    if (!root.empty() && root.back() == '/') root.pop_back();
    if (abs.size() > root.size() + 1 &&
        abs.compare(0, root.size(), root) == 0 && abs[root.size()] == '/') {
        std::snprintf(dst, dstSz, "%s", abs.c_str() + root.size() + 1);
    } else {
        st.toast(ui::ToastLevel::Error, "Pick a file or folder inside the project");
    }
}

void browseTrackFile(AppState& st) {
    storeProjectRelative(st, ui::openFileDialog(
        "All files (*.*)\0*.*\0Documents (*.pdf *.docx *.xlsx)\0*.pdf;*.docx;*.xlsx\0"),
        g_plmHub.trackPath, sizeof(g_plmHub.trackPath));
}

void browseTrackFolder(AppState& st) {
    storeProjectRelative(st, ui::openFolderDialog(),
                         g_plmHub.trackPath, sizeof(g_plmHub.trackPath));
}

// The linked-repo checkout is an arbitrary local folder (not project-relative) —
// store it verbatim.
void browseCheckoutFolder(AppState&) {
    std::string abs = ui::openFolderDialog();
    if (!abs.empty())
        std::snprintf(g_plmHub.linkCheckout, sizeof(g_plmHub.linkCheckout), "%s", abs.c_str());
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
        std::string h; if (!plmOk(r, h)) { st.toast(ui::ToastLevel::Error, h); return; }
        st.toast(ui::ToastLevel::Info, "Stopped tracking");
        plmInvalidate();
    });
}

}  // namespace

void drawFilesTab(AppState& st) {
    auto& H = g_plmHub;
    const float S = ui::uiScale;
    plmEnsureItems(st);

    // track-a-file row: pick a file/folder via the native picker (no typing).
    dimText("Add a document or folder from this project to track its history.");
    ImGui::Spacing();
    if (ImGui::Button("Browse file\xe2\x80\xa6")) browseTrackFile(st);
    ImGui::SameLine();
    if (ImGui::Button("Browse folder\xe2\x80\xa6")) browseTrackFolder(st);
    if (H.trackPath[0]) {
        ImGui::SameLine();
        accentButtonColors();
        if (ImGui::Button("Track", ImVec2(70 * S, 0))) doTrack(st);
        ImGui::PopStyleColor(2);
        ImGui::SameLine();
        if (ImGui::Button("Clear")) H.trackPath[0] = '\0';
        dimText("Selected: %s", H.trackPath);
    }

    // link a GitHub / Git repo (kept under version control externally; its issues
    // are tracked here in the project). Optional local checkout enables pinning.
    ImGui::Dummy(ImVec2(0, 6 * S));
    dimText("Link a GitHub / Git repo (version-controlled outside AI-CAD).");
    ImGui::Spacing();
    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(6 * S, 4 * S));
    ImGui::SetNextItemWidth(-1);
    ImGui::InputTextWithHint("##linkurl", "https://github.com/owner/repo",
                             H.linkUrl, sizeof(H.linkUrl));
    ImGui::SetNextItemWidth(200 * S);
    ImGui::InputTextWithHint("##linkname", "display name (optional)",
                             H.linkName, sizeof(H.linkName));
    ImGui::PopStyleVar();
    ImGui::SameLine();
    if (ImGui::Button("Checkout folder\xe2\x80\xa6")) browseCheckoutFolder(st);
    if (H.linkCheckout[0]) { ImGui::SameLine(); dimText("%s", H.linkCheckout); }
    ImGui::Spacing();
    accentButtonColors();
    if (ImGui::Button("Link repo", ImVec2(110 * S, 0))) doLink(st);
    ImGui::PopStyleColor(2);
    plmError(H.actionErr);

    // filter pills
    ImGui::Dummy(ImVec2(0, 6 * S));
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
        plmCardBegin("##fitem", sel);

        // one padded row: [TAG] path .......... rev · lifecycle  ●
        plmDocTypeTag(it.docType, it.kind);
        ImGui::SameLine();
        ImGui::AlignTextToFramePadding();
        ImGui::TextUnformatted(it.path.c_str());

        // right-pinned badge, clamped so it never crosses the text on narrow cards
        float badgeW = 130 * S + (it.dirty ? 16 * S : 0);
        float pinX = ImGui::GetContentRegionMax().x - badgeW;
        if (pinX > ImGui::GetCursorPosX() + 8 * S) ImGui::SameLine(pinX);
        else ImGui::SameLine(0, 12 * S);
        plmLifecycleBadge(it.lifecycle, !it.rev.empty() ? it.rev : it.workingRev);
        if (it.dirty) {
            ImGui::SameLine();
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(palette.statusWarn[0], palette.statusWarn[1], palette.statusWarn[2], 1.0f));
            ImGui::TextUnformatted("\xe2\x97\x8f");  // ● unsaved dot
            ImGui::PopStyleColor();
        }

        if (sel) {
            ImGui::Spacing();
            dimText("%s  \xc2\xb7  %s%s", it.kind.c_str(),
                    it.docType.empty() ? "design document" : it.docType.c_str(),
                    it.partNumber.empty() ? "" : ("  \xc2\xb7  " + it.partNumber).c_str());
            if (ImGui::SmallButton("Where used")) {
                nlohmann::json p = pathParam(st);
                p["item_id"] = it.itemId;
                st.rpc.call("plm_where_used", p, [&st](const core::RpcResult& r) {
                    std::string h; if (!plmOk(r, h)) { st.toast(ui::ToastLevel::Error, h); return; }
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
        }
        if (plmCardEnd()) H.selectedItem = sel ? "" : it.itemId;
        ImGui::PopID();
    }
    if (shown == 0)
        plmEmptyState("(#)", "No files here yet",
                      "Save a design, or Track a document/folder above.");
}

// The Files destination folds in History + Releases as sub-tabs, so the whole
// "what's in the project / how it changed / what's released" story lives together.
void plmDrawFilesPage(AppState& st) {
    const float S = ui::uiScale;
    const float gap = 10.0f * S;

    // A bordered, padded panel so its content sits inset from the edges instead of
    // clipping flush-left. AutoResize is off (fixed-size split panes).
    auto panelBegin = [&](const char* id, ImVec2 sz) {
        ImGui::PushStyleColor(ImGuiCol_ChildBg,
            ImVec4(palette.text[0], palette.text[1], palette.text[2], 0.028f));
        ImGui::PushStyleColor(ImGuiCol_Border,
            ImVec4(palette.text[0], palette.text[1], palette.text[2], 0.12f));
        ImGui::PushStyleVar(ImGuiStyleVar_ChildRounding, 6.0f * S);
        ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(12 * S, 10 * S));
        ImGui::BeginChild(id, sz, ImGuiChildFlags_Borders);
    };
    auto panelEnd = [&]() {
        ImGui::EndChild();
        ImGui::PopStyleVar(2);
        ImGui::PopStyleColor(2);
    };

    // Files span the full left vertical side; Releases (top) + History (bottom)
    // share the right side. No tab bar — everything visible at once.
    const float leftW = ImGui::GetContentRegionAvail().x * 0.50f;

    panelBegin("##files_left", ImVec2(leftW, 0));
    plmSectionHeader("Files");
    ImGui::Spacing();
    drawFilesTab(st);
    panelEnd();

    ImGui::SameLine(0.0f, gap);

    ImGui::BeginChild("##files_right", ImVec2(0, 0), ImGuiChildFlags_None);
    const float rightH = ImGui::GetContentRegionAvail().y;
    const float topH = rightH * 0.5f - gap * 0.5f;

    panelBegin("##files_releases", ImVec2(0, topH));
    plmDrawReleasesPage(st);
    panelEnd();

    ImGui::Dummy(ImVec2(0, gap));

    panelBegin("##files_history", ImVec2(0, 0));
    plmDrawHistoryPage(st);
    panelEnd();
    ImGui::EndChild();
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
    const float lineX = ImGui::GetCursorScreenPos().x + 9 * S;
    ImU32 lineCol = ImGui::ColorConvertFloat4ToU32(
        ImVec4(palette.border[0], palette.border[1], palette.border[2], palette.border[3]));
    float prevY = -1.0f;
    for (const auto& e : H.history) {
        ImGui::PushID(e.commit.c_str());
        bool sel = (H.selectedEntry == e.commit);
        bool rel = (e.kind == "release");

        ImGui::Indent(26 * S);
        float cardTop = ImGui::GetCursorScreenPos().y;
        plmCardBegin("##hrow", sel);
        if (rel) {
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(palette.statusOk[0], palette.statusOk[1], palette.statusOk[2], 1.0f));
            ImGui::AlignTextToFramePadding();
            ImGui::TextUnformatted("[Release]");
            ImGui::PopStyleColor();
            ImGui::SameLine();
        }
        std::string headline = !e.message.empty() ? e.message
                             : (!e.summary.empty() ? e.summary : "(saved)");
        ImGui::TextWrapped("%s", headline.c_str());
        dimText("%s  \xc2\xb7  %s", e.author.c_str(), e.authoredAt.c_str());
        if (sel) {
            ImGui::PushStyleColor(ImGuiCol_Button,
                ImVec4(palette.button[0], palette.button[1], palette.button[2], 0.8f));
            if (!H.busy && ImGui::SmallButton("Restore this version")) {
                H.busy = true;
                nlohmann::json p = pathParam(st);
                p["commit"] = e.commit;
                st.rpc.call("plm_restore", p, [&st](const core::RpcResult& r) {
                    g_plmHub.busy = false;
                    std::string h; if (!plmOk(r, h)) { st.toast(ui::ToastLevel::Error, h); return; }
                    st.toast(ui::ToastLevel::Success, "Restored — saved as the latest version");
                    plmInvalidate();
                });
            }
            ImGui::PopStyleColor();
        }
        bool clicked = plmCardEnd();
        ImGui::Unindent(26 * S);

        // timeline node + connector, centred on the card we just drew. plmCardEnd
        // appends a 4px gap Dummy, so the card bottom is the cursor minus that gap.
        float cardBot = ImGui::GetCursorScreenPos().y - 4 * S;
        float midY = (cardTop + cardBot) * 0.5f;
        if (prevY >= 0) dl->AddLine(ImVec2(lineX, prevY), ImVec2(lineX, midY), lineCol, 2 * S);
        ImU32 dotCol = ImGui::ColorConvertFloat4ToU32(rel
            ? ImVec4(palette.statusOk[0], palette.statusOk[1], palette.statusOk[2], 1.0f)
            : ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 0.85f));
        dl->AddCircleFilled(ImVec2(lineX, midY), 5 * S, dotCol);
        prevY = midY;

        if (clicked) H.selectedEntry = sel ? "" : e.commit;
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
        if (!plmOk(r, H.actionErr)) return;
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
    dimText("Stamp the next revision and freeze the design.");
    ImGui::Spacing();

    if (!H.itemsErr.empty()) { plmError(H.itemsErr); return; }
    if (H.items.empty()) {
        plmEmptyState("(#)", "Nothing to release yet",
                      "Add a design or document, then release it here.");
        return;
    }

    for (const auto& it : H.items) {
        ImGui::PushID(it.itemId.c_str());
        bool open = (s_releaseItem == it.itemId);
        plmCardBegin("##rel", open);
        plmDocTypeTag(it.docType, it.kind);
        ImGui::SameLine();
        ImGui::AlignTextToFramePadding();
        ImGui::TextUnformatted(it.path.c_str());

        // right cluster: [rev · lifecycle]  [Release… | released] — clamp so the
        // controls never overrun the path text in a narrow embedded card.
        const float ctrlW = 200 * S;
        float pinX = ImGui::GetContentRegionMax().x - ctrlW;
        bool tight = pinX <= ImGui::GetCursorPosX() + 8 * S;
        if (tight) { ImGui::NewLine(); } else { ImGui::SameLine(pinX); }
        plmLifecycleBadge(it.lifecycle, !it.rev.empty() ? it.rev : it.workingRev);
        ImGui::SameLine(0, 12 * S);
        if (it.lifecycle == "Released") {
            ImGui::AlignTextToFramePadding();
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
        plmCardEnd();
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
            std::string h; if (!plmOk(r, h)) { st.toast(ui::ToastLevel::Error, h); return; }
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
                std::string h; if (!plmOk(r, h)) { st.toast(ui::ToastLevel::Error, h); return; }
                st.toast(ui::ToastLevel::Info, "Assistance level updated");
                plmInvalidate();
            });
        }
        ImGui::PopStyleColor();
    }
}

// ---- Planning (kanban + milestones) -------------------------------------------

namespace {

void planCall(AppState& st, const char* op, const char* entity,
              const nlohmann::json& data, const std::string& id = "") {
    auto& H = g_plmHub;
    if (H.busy) return;
    H.busy = true;
    nlohmann::json p = pathParam(st);
    p["op"] = op;
    p["entity"] = entity;
    if (!data.is_null()) p["data"] = data;
    if (!id.empty()) p["id"] = id;
    st.rpc.call("plm_plan", p, [&st](const core::RpcResult& r) {
        g_plmHub.busy = false;
        std::string h; if (!plmOk(r, h)) { st.toast(ui::ToastLevel::Error, h); return; }
        plmInvalidate();
    });
}

// A compact, interactive calendar popup bound to a "YYYY-MM-DD" text buffer.
// The trigger button shows the current value (or a hint); the popup lets the
// user page months and click a day. Returns true when the value changed.
bool plmDatePicker(const char* id, char* buf, size_t n) {
    const float S = ui::uiScale;
    bool changed = false;

    // parse the buffer (fall back to today for the initial view)
    int y = 0, mo = 0, d = 0;
    bool haveVal = (std::sscanf(buf, "%d-%d-%d", &y, &mo, &d) == 3 &&
                    mo >= 1 && mo <= 12 && d >= 1 && d <= 31);
    std::time_t tt = std::time(nullptr);
    std::tm now{};
#if defined(_WIN32)
    localtime_s(&now, &tt);
#else
    localtime_r(&tt, &now);
#endif
    if (!haveVal) { y = now.tm_year + 1900; mo = now.tm_mon + 1; d = 0; }

    ImGui::PushID(id);
    // view state (month being browsed) lives in ImGui storage, keyed per widget
    ImGuiStorage* store = ImGui::GetStateStorage();
    ImGuiID kY = ImGui::GetID("vy"), kM = ImGui::GetID("vm");
    char label[64];
    if (haveVal) std::snprintf(label, sizeof(label), "%04d-%02d-%02d", y, mo, d);
    else         std::snprintf(label, sizeof(label), "Pick a date\xe2\x80\xa6");
    if (ImGui::Button(label, ImVec2(150 * S, 0))) {
        store->SetInt(kY, y);
        store->SetInt(kM, mo);
        ImGui::OpenPopup("##cal");
    }
    ImGui::SameLine();
    if (haveVal && ImGui::SmallButton("Clear")) { buf[0] = '\0'; changed = true; }

    if (ImGui::BeginPopup("##cal")) {
        // Fixed cell geometry + a tight, uniform column pitch so the weekday header,
        // the leading blanks and the day buttons all sit on the SAME grid. (The default
        // 8px ItemSpacing plus variable-width header text made the columns drift, which
        // read as the day numbers being off-centre under their headers.)
        const float kCell = 26 * S, kCellH = 22 * S, kGap = 2 * S;
        const float kGridW = kCell * 7 + kGap * 6;  // full week row width
        ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing, ImVec2(kGap, kGap));
        ImGui::PushStyleVar(ImGuiStyleVar_ButtonTextAlign, ImVec2(0.5f, 0.5f));

        int vy = store->GetInt(kY, y), vm = store->GetInt(kM, mo);
        // header: < Month Year >  (arrows pinned to the grid edges, title centred between)
        if (ImGui::ArrowButton("##pm", ImGuiDir_Left)) {
            if (--vm < 1) { vm = 12; --vy; }
            store->SetInt(kY, vy); store->SetInt(kM, vm);
        }
        static const char* kMon[] = {"Jan","Feb","Mar","Apr","May","Jun",
                                     "Jul","Aug","Sep","Oct","Nov","Dec"};
        char hdr[32]; std::snprintf(hdr, sizeof(hdr), "%s %d", kMon[vm - 1], vy);
        float hw = ImGui::CalcTextSize(hdr).x;
        float rowX0 = ImGui::GetCursorPosX();  // left edge of the arrow just drawn's row
        ImGui::SameLine();
        ImGui::SetCursorPosX(rowX0 + (kGridW - hw) * 0.5f);
        ImGui::AlignTextToFramePadding();
        ImGui::TextUnformatted(hdr);
        ImGui::SameLine(rowX0 + kGridW - ImGui::GetFrameHeight());
        if (ImGui::ArrowButton("##nm", ImGuiDir_Right)) {
            if (++vm > 12) { vm = 1; ++vy; }
            store->SetInt(kY, vy); store->SetInt(kM, vm);
        }

        ImDrawList* dl = ImGui::GetWindowDrawList();
        // weekday header — each label centred inside its 26px column
        static const char* kDow[] = {"Su","Mo","Tu","We","Th","Fr","Sa"};
        ImU32 dimCol = ImGui::ColorConvertFloat4ToU32(
            ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], 1.0f));
        for (int i = 0; i < 7; ++i) {
            if (i) ImGui::SameLine();
            ImVec2 p = ImGui::GetCursorScreenPos();
            ImVec2 ts = ImGui::CalcTextSize(kDow[i]);
            dl->AddText(ImVec2(p.x + (kCell - ts.x) * 0.5f, p.y), dimCol, kDow[i]);
            ImGui::Dummy(ImVec2(kCell, ts.y));
        }

        // day grid — first weekday + day count via std::tm normalisation
        std::tm first{}; first.tm_year = vy - 1900; first.tm_mon = vm - 1;
        first.tm_mday = 1; std::mktime(&first);
        int lead = first.tm_wday;  // 0=Sun
        static const int mdays[] = {31,28,31,30,31,30,31,31,30,31,30,31};
        int dim = mdays[vm - 1];
        if (vm == 2 && ((vy % 4 == 0 && vy % 100 != 0) || vy % 400 == 0)) dim = 29;

        int cell = 0;
        for (int i = 0; i < lead; ++i, ++cell) {
            if (cell % 7) ImGui::SameLine();
            ImGui::Dummy(ImVec2(kCell, kCellH));
        }
        for (int day = 1; day <= dim; ++day, ++cell) {
            if (cell % 7) ImGui::SameLine();
            ImGui::PushID(day);
            bool isSel = (haveVal && vy == y && vm == mo && day == d);
            bool isToday = (vy == now.tm_year + 1900 && vm == now.tm_mon + 1 &&
                            day == now.tm_mday);
            if (isSel)
                ImGui::PushStyleColor(ImGuiCol_Button,
                    ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 0.85f));
            else if (isToday)
                ImGui::PushStyleColor(ImGuiCol_Button,
                    ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 0.25f));
            char db[4]; std::snprintf(db, sizeof(db), "%d", day);
            if (ImGui::Button(db, ImVec2(kCell, kCellH))) {
                std::snprintf(buf, n, "%04d-%02d-%02d", vy, vm, day);
                changed = true;
                ImGui::CloseCurrentPopup();
            }
            if (isSel || isToday) ImGui::PopStyleColor();
            ImGui::PopID();
        }
        ImGui::PopStyleVar(2);
        ImGui::EndPopup();
    }
    ImGui::PopID();
    return changed;
}

void priorityTag(const std::string& pr) {
    const float* c = palette.textDim;
    if (pr == "p0" || pr == "p1") c = palette.statusWarn;
    ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(c[0], c[1], c[2], 1.0f));
    ImGui::Text("[%s]", pr.empty() ? "p2" : pr.c_str());
    ImGui::PopStyleColor();
}

static std::string s_selTask;

}  // namespace

// A milestone's inline read / edit row (condensed for the right-hand column).
static void drawMilestoneRow(AppState& st, const PlmMilestoneRow& m) {
    auto& H = g_plmHub;
    const float S = ui::uiScale;
    bool done = (m.status == "done");
    bool editing = (H.msEditId == m.id);
    ImGui::PushID(m.id.c_str());
    plmCardBegin("##ms", editing);
    if (editing) {
        ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(5 * S, 3 * S));
        ImGui::SetNextItemWidth(-1);
        ImGui::InputTextWithHint("##met", "title", H.msEditTitle, sizeof(H.msEditTitle));
        ImGui::PopStyleVar();
        plmDatePicker("##med", H.msEditDue, sizeof(H.msEditDue));
        ImGui::SameLine();
        accentButtonColors();
        if (ImGui::SmallButton("Save") && H.msEditTitle[0]) {
            nlohmann::json d = {{"title", H.msEditTitle}};
            d["due"] = H.msEditDue[0] ? H.msEditDue : "";
            planCall(st, "update", "milestone", d, m.id);
            H.msEditId.clear();
        }
        ImGui::PopStyleColor(2);
        ImGui::SameLine();
        if (ImGui::SmallButton("Cancel")) H.msEditId.clear();
    } else {
        ImGui::PushStyleColor(ImGuiCol_Text, done
            ? ImVec4(palette.statusOk[0], palette.statusOk[1], palette.statusOk[2], 1.0f)
            : ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], 1.0f));
        ImGui::AlignTextToFramePadding();
        ImGui::TextUnformatted(done ? "[x]" : "[ ]");
        ImGui::PopStyleColor();
        ImGui::SameLine();
        ImGui::TextWrapped("%s", m.title.c_str());
        if (!m.due.empty()) dimText("due %s", m.due.c_str());
        // action row
        ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(4 * S, 1 * S));
        ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing, ImVec2(3 * S, 3 * S));
        if (ImGui::SmallButton("Edit")) {
            H.msEditId = m.id;
            std::snprintf(H.msEditTitle, sizeof(H.msEditTitle), "%s", m.title.c_str());
            std::snprintf(H.msEditDue, sizeof(H.msEditDue), "%s", m.due.c_str());
        }
        ImGui::SameLine();
        if (ImGui::SmallButton(done ? "Reopen" : "Complete"))
            planCall(st, "update", "milestone", {{"status", done ? "open" : "done"}}, m.id);
        ImGui::SameLine();
        ImGui::PushStyleColor(ImGuiCol_Button,
            ImVec4(palette.statusErr[0], palette.statusErr[1], palette.statusErr[2], 0.5f));
        if (ImGui::SmallButton("Del")) planCall(st, "delete", "milestone", nullptr, m.id);
        ImGui::PopStyleColor();
        ImGui::PopStyleVar(2);
    }
    plmCardEnd();
    ImGui::PopID();
}

// One issue row on the Planning page: severity + title, with visible actions —
// including "Make plan", which spins the issue into a linked task so you can
// organise fixes right where you see the problems.
static void drawIssueRow(AppState& st, const PlmIssueRow& is) {
    const float S = ui::uiScale;
    bool closed = (is.state == "closed");
    ImGui::PushID(is.id.c_str());
    plmCardBegin("##is", false);
    const float* sc = palette.textDim;
    if (is.severity == "critical" || is.severity == "high") sc = palette.statusErr;
    else if (is.severity == "med") sc = palette.statusWarn;
    ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(sc[0], sc[1], sc[2], 1.0f));
    ImGui::AlignTextToFramePadding();
    ImGui::Text("[%s]", is.severity.empty() ? "med" : is.severity.c_str());
    ImGui::PopStyleColor();
    ImGui::SameLine();
    if (closed) {
        ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(palette.textDisabled[0],
            palette.textDisabled[1], palette.textDisabled[2], 1.0f));
        ImGui::TextWrapped("(closed) %s", is.title.c_str());
        ImGui::PopStyleColor();
    } else {
        ImGui::TextWrapped("%s", is.title.c_str());
    }
    ImGui::Spacing();
    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(4 * S, 1 * S));
    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing, ImVec2(3 * S, 3 * S));
    accentButtonColors();
    if (ImGui::SmallButton("Make plan")) {
        nlohmann::json d = {{"title", is.title},
                            {"links", nlohmann::json::array({
                                {{"kind", "issue"}, {"id", is.id}}})}};
        planCall(st, "create", "task", d);
        st.toast(ui::ToastLevel::Success, "Added a task from this issue");
    }
    ImGui::PopStyleColor(2);
    ImGui::SameLine();
    if (ImGui::SmallButton(closed ? "Reopen" : "Close"))
        planCall(st, "update", "issue", {{"state", closed ? "open" : "closed"}}, is.id);
    ImGui::SameLine();
    ImGui::PushStyleColor(ImGuiCol_Button,
        ImVec4(palette.statusErr[0], palette.statusErr[1], palette.statusErr[2], 0.5f));
    if (ImGui::SmallButton("Del")) planCall(st, "delete", "issue", nullptr, is.id);
    ImGui::PopStyleColor();
    ImGui::PopStyleVar(2);
    plmCardEnd();
    ImGui::PopID();
}

void plmDrawPlanningPage(AppState& st) {
    auto& H = g_plmHub;
    const float S = ui::uiScale;
    plmEnsurePlanning(st);

    plmSectionHeader("Planning");
    if (!H.planErr.empty()) plmError(H.planErr);

    // ---- top band: Team & Sync (left) + Milestones (right, condensed) ----------
    ImGui::Columns(2, "##plan_top", false);
    ImGui::SetColumnWidth(0, 320 * S);

    plmSectionHeader("Team & Sync");
    dimText("Share via a shared folder or your own server. Sync brings changes "
            "together without ever merging or losing work.");
    ImGui::BeginDisabled(true);
    ImGui::Button("Connect a shared folder\xe2\x80\xa6");
    ImGui::SameLine();
    ImGui::Button("Sync now");
    ImGui::EndDisabled();
    dimText("Sharing & Sync arrive in a later update.");

    ImGui::NextColumn();

    plmSectionHeader("Milestones");
    static char s_msTitle[160] = {};
    static char s_msDue[32] = {};
    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(6 * S, 4 * S));
    ImGui::SetNextItemWidth(150 * S);
    ImGui::InputTextWithHint("##msTitle", "milestone", s_msTitle, sizeof(s_msTitle));
    ImGui::PopStyleVar();
    ImGui::SameLine();
    plmDatePicker("##msDue", s_msDue, sizeof(s_msDue));
    ImGui::SameLine();
    if (ImGui::Button("Add") && s_msTitle[0]) {
        nlohmann::json d = {{"title", s_msTitle}};
        if (s_msDue[0]) d["due"] = s_msDue;
        planCall(st, "create", "milestone", d);
        s_msTitle[0] = '\0'; s_msDue[0] = '\0';
    }
    if (H.milestones.empty()) dimText("No milestones yet.");
    for (const auto& m : H.milestones) drawMilestoneRow(st, m);

    ImGui::Columns(1);
    ImGui::Dummy(ImVec2(0, 4 * S));
    ImGui::Separator();

    // ---- Issues (folded in so you can see problems while you plan) --------------
    plmSectionHeader("Issues");
    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(6 * S, 4 * S));
    ImGui::SetNextItemWidth(-130 * S);
    bool ienter = ImGui::InputTextWithHint("##newissue", "describe an issue\xe2\x80\xa6",
        H.newIssueTitle, sizeof(H.newIssueTitle), ImGuiInputTextFlags_EnterReturnsTrue);
    ImGui::PopStyleVar();
    ImGui::SameLine();
    accentButtonColors();
    if ((ImGui::Button("Log issue", ImVec2(120 * S, 0)) || ienter) && H.newIssueTitle[0]) {
        planCall(st, "create", "issue", {{"title", H.newIssueTitle}});
        H.newIssueTitle[0] = '\0';
    }
    ImGui::PopStyleColor(2);
    if (H.issues.empty()) dimText("No issues logged. \"Make plan\" turns one into a task.");
    for (const auto& is : H.issues) drawIssueRow(st, is);

    ImGui::Dummy(ImVec2(0, 4 * S));
    ImGui::Separator();

    // ---- Task board ------------------------------------------------------------
    plmSectionHeader("Board");
    // add a task
    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(6 * S, 4 * S));
    ImGui::SetNextItemWidth(-120 * S);
    bool enter = ImGui::InputTextWithHint("##newtask", "new task title\xe2\x80\xa6",
        H.newTaskTitle, sizeof(H.newTaskTitle), ImGuiInputTextFlags_EnterReturnsTrue);
    ImGui::PopStyleVar();
    ImGui::SameLine();
    accentButtonColors();
    if ((ImGui::Button("Add task", ImVec2(110 * S, 0)) || enter) && H.newTaskTitle[0]) {
        planCall(st, "create", "task", {{"title", H.newTaskTitle}});
        H.newTaskTitle[0] = '\0';
    }
    ImGui::PopStyleColor(2);
    ImGui::Spacing();

    // kanban columns — bordered, scrollable boxes so cards stay contained inside
    // their column instead of spilling past the bottom. Each column scrolls
    // independently when it fills up; the boxes are sized to the remaining page
    // height so they feel like a real board.
    struct Col { const char* id; const char* label; };
    static const Col kCols[] = {
        {"todo", "To do"}, {"doing", "Doing"}, {"done", "Done"}, {"blocked", "Blocked"},
    };
    static const char* kPipe[] = {"todo", "doing", "done"};  // ordered lane; "blocked" is aside
    const float gap  = 8 * S;
    float colW = (ImGui::GetContentRegionAvail().x - gap * 3) / 4.0f;
    float colH = ImGui::GetContentRegionAvail().y - 210 * S;  // reserve room for milestones
    if (colH < 260 * S) colH = 260 * S;

    for (int ci = 0; ci < 4; ++ci) {
        const auto& col = kCols[ci];
        if (ci) ImGui::SameLine(0, gap);
        ImGui::BeginGroup();
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 1.0f));
        ImGui::TextUnformatted(col.label);
        ImGui::PopStyleColor();

        ImGui::PushID(ci);
        ImGui::PushStyleVar(ImGuiStyleVar_ChildRounding, 6 * S);
        ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(6 * S, 6 * S));
        ImGui::PushStyleColor(ImGuiCol_ChildBg,
            ImVec4(palette.text[0], palette.text[1], palette.text[2], 0.03f));
        ImGui::PushStyleColor(ImGuiCol_Border,
            ImVec4(palette.text[0], palette.text[1], palette.text[2], 0.12f));
        ImGui::BeginChild("##col", ImVec2(colW, colH), ImGuiChildFlags_Borders);

        int shown = 0;
        for (const auto& t : H.allTasks) {
            if (t.status != col.id) continue;
            ++shown;
            ImGui::PushID(t.id.c_str());
            bool sel = (s_selTask == t.id);
            plmCardBegin("##task", sel, 8.0f, 5.0f);
            priorityTag(t.priority);
            ImGui::SameLine();
            ImGui::TextWrapped("%s", t.title.c_str());
            if (!t.assignee.empty()) dimText("@%s", t.assignee.c_str());

            // Movement controls — always visible, one click, no select-first step.
            // ‹ › walk the todo→doing→done lane; Block parks the task. A tight
            // frame padding keeps them small so the card stays compact.
            int idx = -1;
            for (int k = 0; k < 3; ++k) if (t.status == kPipe[k]) idx = k;
            bool blocked = (idx < 0);
            ImGui::Spacing();
            ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(4 * S, 1 * S));
            ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing, ImVec2(3 * S, 3 * S));
            // ArrowButton draws a real triangle (font-independent — the ‹ › glyphs
            // were not in the atlas and rendered blank); the tight FramePadding
            // above shrinks its square to a compact size.
            ImGui::BeginDisabled(!blocked && idx == 0);
            if (ImGui::ArrowButton("##prev", ImGuiDir_Left))
                planCall(st, "update", "task",
                         {{"status", blocked ? "todo" : kPipe[idx - 1]}}, t.id);
            ImGui::EndDisabled();
            ImGui::SameLine();
            ImGui::BeginDisabled(!blocked && idx == 2);
            if (ImGui::ArrowButton("##next", ImGuiDir_Right))
                planCall(st, "update", "task",
                         {{"status", blocked ? "doing" : kPipe[idx + 1]}}, t.id);
            ImGui::EndDisabled();
            ImGui::SameLine();
            if (blocked) {
                if (ImGui::SmallButton("Unblock"))
                    planCall(st, "update", "task", {{"status", "todo"}}, t.id);
            } else if (ImGui::SmallButton("Block")) {
                planCall(st, "update", "task", {{"status", "blocked"}}, t.id);
            }
            if (sel) {
                ImGui::SameLine();
                ImGui::PushStyleColor(ImGuiCol_Button,
                    ImVec4(palette.statusErr[0], palette.statusErr[1], palette.statusErr[2], 0.55f));
                if (ImGui::SmallButton("Del"))
                    planCall(st, "delete", "task", nullptr, t.id);
                ImGui::PopStyleColor();
            }
            ImGui::PopStyleVar(2);
            if (plmCardEnd()) s_selTask = sel ? "" : t.id;
            ImGui::PopID();
        }
        if (!shown) dimText("Nothing here yet.");

        ImGui::EndChild();
        ImGui::PopStyleColor(2);
        ImGui::PopStyleVar(2);
        ImGui::PopID();
        ImGui::EndGroup();
    }
}

// ---- Issues -------------------------------------------------------------------

namespace {
static std::string s_selIssue;

void severityBadge(const std::string& sev) {
    const float* c = palette.textDim;
    if (sev == "critical" || sev == "high") c = palette.statusErr;
    else if (sev == "med") c = palette.statusWarn;
    ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(c[0], c[1], c[2], 1.0f));
    ImGui::Text("[%s]", sev.empty() ? "med" : sev.c_str());
    ImGui::PopStyleColor();
}
}  // namespace

void plmDrawIssuesPage(AppState& st) {
    auto& H = g_plmHub;
    const float S = ui::uiScale;
    plmEnsurePlanning(st);

    plmSectionHeader("Issues");
    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(6 * S, 4 * S));
    ImGui::SetNextItemWidth(-120 * S);
    bool enter = ImGui::InputTextWithHint("##newissue", "describe an issue\xe2\x80\xa6",
        H.newIssueTitle, sizeof(H.newIssueTitle), ImGuiInputTextFlags_EnterReturnsTrue);
    ImGui::PopStyleVar();
    ImGui::SameLine();
    accentButtonColors();
    if ((ImGui::Button("Log issue", ImVec2(110 * S, 0)) || enter) && H.newIssueTitle[0]) {
        planCall(st, "create", "issue", {{"title", H.newIssueTitle}});
        H.newIssueTitle[0] = '\0';
    }
    ImGui::PopStyleColor(2);
    if (!H.planErr.empty()) plmError(H.planErr);
    ImGui::Separator();
    ImGui::Spacing();

    if (H.issues.empty()) {
        plmEmptyState("[!]", "No issues logged",
                      "Track bugs and defects here, linked to the items they affect.");
        return;
    }
    for (const auto& is : H.issues) {
        ImGui::PushID(is.id.c_str());
        bool sel = (s_selIssue == is.id);
        bool closed = (is.state == "closed");
        plmRowPill(sel, sel ? 2.6f : 1.4f);
        severityBadge(is.severity);
        ImGui::SameLine();
        if (closed) {
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(palette.textDisabled[0], palette.textDisabled[1],
                       palette.textDisabled[2], 1.0f));
            ImGui::Text("(closed) %s", is.title.c_str());
            ImGui::PopStyleColor();
        } else {
            ImGui::Text("%s", is.title.c_str());
        }
        if (ImGui::IsItemClicked()) s_selIssue = sel ? "" : is.id;
        if (sel) {
            ImGui::Indent(16 * S);
            if (ImGui::SmallButton(closed ? "Reopen" : "Close issue"))
                planCall(st, "update", "issue",
                         {{"state", closed ? "open" : "closed"}}, is.id);
            ImGui::SameLine();
            ImGui::PushStyleColor(ImGuiCol_Button,
                ImVec4(palette.statusErr[0], palette.statusErr[1], palette.statusErr[2], 0.5f));
            if (ImGui::SmallButton("Delete"))
                planCall(st, "delete", "issue", nullptr, is.id);
            ImGui::PopStyleColor();
            ImGui::Unindent(16 * S);
        }
        ImGui::Spacing();
        ImGui::PopID();
    }
}

}  // namespace ui
