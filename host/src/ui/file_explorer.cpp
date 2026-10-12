// Explorer: working-project PLM changes, workspace files and separate recents.
// Vault status/checkpoints are async; filesystem browsing uses FsBackend
// snapshots. Explicit revision-named sibling files group only in the view:
// every original path remains available for open, rename, copy and delete.
#include <algorithm>
#include <cctype>
#include <cstdio>
#include <cstdlib>
#include <cstdint>
#include <map>
#include <memory>
#include <string>
#include <unordered_map>
#include <vector>

#include "imgui.h"
#include "imgui_internal.h"  // ImRect (ui::adaptivePanel rect argument)
#include "widgets/kit.h"
#include "plm/plm_hub.h"
#include "plm/plm_workspace.h"

#include "file_explorer.h"
#include "dragdrop.h"       // kFileDragPayload (Explorer drag source -> viewport drop target)
#include "feature_tree.h"   // featureTreeOpenPath / featureTreeActivePath
#include "icons.h"          // getIcon / IconUV
#include "layout.h"         // kLeftPaneExplorer
#include "metrics.h"

#include "../core/app_state.h"
#include "../core/fs_backend.h"
#include "../ui_theme.h"    // ui::uiScale, ui::palette


namespace ui {

// ---- path string helpers (no FS I/O) ----------------------------------------
static std::string pathFilename(const std::string& p) {
    size_t cut = p.find_last_of("/\\");
    return cut == std::string::npos ? p : p.substr(cut + 1);
}
static std::string pathParent(const std::string& p) {
    size_t cut = p.find_last_of("/\\");
    if (cut == std::string::npos) return {};
    if (cut == 0) return p.substr(0, 1);
    return p.substr(0, cut);
}
static std::string pathJoin(const std::string& a, const std::string& b) {
    if (a.empty()) return b;
    if (b.empty()) return a;
    char last = a.back();
    if (last == '/' || last == '\\') return a + b;
#ifdef _WIN32
    return a + "\\" + b;
#else
    return a + "/" + b;
#endif
}
static std::string pathStem(const std::string& name) {
    size_t dot = name.find_last_of('.');
    if (dot == std::string::npos || dot == 0) return name;
    return name.substr(0, dot);
}
static std::string pathExtension(const std::string& name) {
    size_t dot = name.find_last_of('.');
    if (dot == std::string::npos || dot == 0) return {};
    return name.substr(dot);
}

// ---- module state (single Explorer instance) ----
// Row styling comes from feature_tree.h's shared FM helpers (fmRowPill / fmRowPillEnd /
// fmTreeIcon* / fmSectionDivider / fmToolbarIconBtn) so Explorer rows are pixel-identical to the
// FeatureManager reference: same pill height + FramePadding, same soft-blue selection card, same
// accent-outline hover, same 16px crisp icons and centered hairline section rules.

// Liquid glass v2 (host contract §H4): fetch (never paint) the left pane's ONE glass surface —
// layout.cpp's drawLeftPane already painted it this frame under "host_left_glass"; adaptivePanel
// is idempotent per frame (A2), so this is a cheap cached lookup regardless of the rect passed
// (window pos/size here is this child's rect, not the whole pane — irrelevant after the first
// same-frame call).
static const ui::AdaptiveSurface& explorerSurf() {
    ImVec2 wp_ = ImGui::GetWindowPos(); ImVec2 ws_ = ImGui::GetWindowSize();
    ImRect r(wp_, ImVec2(wp_.x + ws_.x, wp_.y + ws_.y));
    // Glass v2.1: the left pane is the FeatureManager group (per-group user overrides). All
    // "host_left_glass" call sites (layout.cpp, feature_tree.cpp) must pass the same group.
    return ui::adaptivePanel(ui::panelId("host_left_glass"), r, ui::GlassGroup::FeatureManager);
}
static std::string s_selectedPath;     // the currently-highlighted tree node (file or folder)
static std::string s_currentFolder;    // folder that New Folder / Paste act inside (a dir, or a
                                       // file's parent); defaults to the workspace root
static std::string s_renameId;         // path being inline-renamed ("" = none)
static char        s_renameBuf[260] = {0};
static bool        s_renameFocus = false;
static std::string s_pendingDelete;    // path queued for the delete-confirm modal ("" = none)
static std::string s_clipboardPath;    // Explorer copy/paste stash (a file or folder path)
static std::string s_pendingNewFolder; // reveal intent, retained until the inline rename is visible
// UI caches hold filtered/sorted rows; backend snapshots own refresh scheduling.
// Shared ownership keeps a tree walk valid when a child mutation invalidates the
// cache while its parent's rows are still being iterated.
struct LegacyFileGroup {
    std::string title;
    std::vector<size_t> members;
    size_t head = 0;
};
struct DirListCache {
    uint64_t revision = UINT64_MAX;
    chudware::FsReadState state = chudware::FsReadState::Loading;
    std::string error;
    std::vector<std::string> dirs;   // full paths, name-sorted (case-insensitive)
    std::vector<std::string> files;  // full paths, name-sorted (case-insensitive)
    std::vector<int> fileGroup;  // -1 for ungrouped; otherwise index into legacyGroups
    std::vector<LegacyFileGroup> legacyGroups;
};
static bool withinWorkspace(const std::string& root, const std::string& child);
// The hub's item rows do not carry vault change codes. Keep the Explorer's
// read-only status snapshot separately, fenced by project and request serial.
struct ExplorerPlmItem {
    std::string path, itemId, rev, lifecycle, workingRev, change;
};
struct ExplorerPlmState {
    std::string project, branch, error, checkpointError, checkpointOk;
    std::vector<ExplorerPlmItem> items, untracked;
    std::unordered_map<std::string, const ExplorerPlmItem*> byPath;
    int hubGeneration = -1;
    unsigned long long itemsRevision = UINT64_MAX;
    unsigned serial = 0;
    bool loading = false, ready = false, checkpointing = false;
    // The branch answer is independent of the (much slower) item scan: its own in-flight flag,
    // and `branchKnown` once any answer landed, so the chip keeps the last value while a
    // refresh is pending instead of flipping back to a loading label.
    bool branchLoading = false, branchKnown = false;
    bool changesOpen = true, filesOpen = true;
    double fetchedAt = -1.0e9;
    char message[256] = {};
};
static ExplorerPlmState s_plm;
// Last branch seen per project this session: switching back to a project shows it at once.
static std::unordered_map<std::string, std::string> s_branchByProject;
static constexpr double kPlmRefreshSeconds = 15.0;

static std::string plmField(const nlohmann::json& row, const char* key) {
    const auto it = row.find(key);
    return it != row.end() && it->is_string() ? it->get<std::string>() : std::string();
}

static void invalidateExplorerPlm() {
    s_plm.fetchedAt = -1.0e9;
    plmInvalidate();
}

static void openPlm(AppState& st, PlmPage page, const std::string& itemId = {}) {
    g_plmHub.page = page;
    if (page == PlmPage::Files) plmSelectFilesPage();
    if (page == PlmPage::Releases) plmSelectReleasesPage();
    if (!itemId.empty()) {
        g_plmHub.selectedItem = itemId;
        g_plmHub.histItemFilter = itemId;
        g_plmHub.histGen = -1;
    }
    st.activeWorkspace = AppState::Workspace::PLM;
}

static void pollExplorerPlm(AppState& st, const std::string& project, bool force = false) {
    auto& P = s_plm;
    if (project != P.project) {
        ++P.serial;
        P.project = project;
        P.error.clear();
        P.checkpointError.clear();
        P.checkpointOk.clear();
        P.byPath.clear();
        P.items.clear();
        P.untracked.clear();
        P.ready = false;
        P.loading = false;
        P.branchLoading = false;
        P.checkpointing = false;
        P.fetchedAt = -1.0e9;
        P.hubGeneration = -1;
        P.itemsRevision = UINT64_MAX;
        const auto known = s_branchByProject.find(project);
        P.branchKnown = known != s_branchByProject.end();
        P.branch = P.branchKnown ? known->second : std::string();
    }
    if (!st.workerStarted) {
        if (P.loading || P.ready || P.checkpointing || P.branchLoading) ++P.serial;
        if (P.checkpointing)
            P.checkpointError = "Connection lost during checkpoint; refresh to confirm its result.";
        P.checkpointing = false;
        P.loading = false;
        P.branchLoading = false;
        P.ready = false;
        P.fetchedAt = -1.0e9;
        return;
    }
    if (project.empty() || P.checkpointing) return;
    auto& H = g_plmHub;
    const double now = ImGui::GetTime();
    const bool refresh = force || P.hubGeneration != H.generation ||
        now - P.fetchedAt >= kPlmRefreshSeconds;
    if (refresh) {
        // Explorer and PLM consume one coalesced scan, not two copies on the shared FIFO.
        // Adopt a warm/in-flight scan on first entry instead of invalidating its first answer.
        if (!H.itemsLoading && (force ||
            (P.fetchedAt > -1.0e8 && now - P.fetchedAt >= kPlmRefreshSeconds)))
            H.itemsGen = -1;
        P.hubGeneration = H.generation;
        P.fetchedAt = now;
    }
    if (!P.branchLoading && (refresh || !P.branchKnown)) {
        P.branchLoading = true;
        const unsigned serial = ++P.serial;
        // The cheap branch read precedes the shared working-file scan.
        st.rpc.call("plm_branch_list", {{"path", project}}, [project, serial, &st](const core::RpcResult& r) {
            auto& state = s_plm;
            if (state.serial != serial || state.project != project) return;
            state.branchLoading = false;
            if (st.plmProjectPath() != project) return;
            state.branchKnown = true;
            std::string err;
            state.branch = plmOk(r, err) ? plmField(r.result, "active") : std::string();
            if (!state.branch.empty()) s_branchByProject[project] = state.branch;
        });
    }
    plmEnsureItems(st);
    P.loading = H.itemsLoading;
    if (H.itemsProjectDir == project) {
        P.error = H.itemsErr;
        if (H.itemsErr.empty() && P.itemsRevision != H.itemsRevision) {
            P.itemsRevision = H.itemsRevision;
            P.ready = true;
            P.byPath.clear();
            P.items.clear();
            P.untracked.clear();
            auto copy = [](const PlmItemRow& row) {
                return ExplorerPlmItem{row.path, row.itemId, row.rev, row.lifecycle,
                                       row.workingRev, row.change};
            };
            for (const auto& row : H.items) P.items.push_back(copy(row));
            for (const auto& row : H.untracked) P.untracked.push_back(copy(row));
            auto index = [&P](const std::vector<ExplorerPlmItem>& rows) {
                for (const auto& item : rows) {
                    std::string key = item.path;
                    for (char& c : key) if (c == '\\') c = '/';
                    P.byPath.emplace(std::move(key), &item);
                }
            };
            index(P.items);
            index(P.untracked);
        }
    }
}

static const ExplorerPlmItem* statusForPath(const std::string& path) {
    const std::string& project = s_plm.project;
    if (!s_plm.ready || project.empty() || path.size() <= project.size() ||
        !withinWorkspace(project, path)) return nullptr;
    std::string rel = path.substr(project.size());
    while (!rel.empty() && (rel.front() == '/' || rel.front() == '\\')) rel.erase(rel.begin());
    for (char& c : rel) if (c == '\\') c = '/';
    const auto found = s_plm.byPath.find(rel);
    return found == s_plm.byPath.end() ? nullptr : found->second;
}
static std::string statusFileLabel(const std::string& name, const ExplorerPlmItem* status) {
    if (!status) return name;
    std::string label = name;
    if (!status->change.empty()) label = "[" + status->change + "] " + label;
    const std::string& rev = status->workingRev.empty() ? status->rev : status->workingRev;
    if (!rev.empty()) label += " · rev " + rev;
    if (!status->lifecycle.empty()) label += " · " + status->lifecycle;
    return label;
}
static std::map<std::string, std::shared_ptr<DirListCache>> s_dirListCache;
static std::string s_workspaceError;

static void invalidateDirListCache(bool workspaceChanged = false) {
    s_dirListCache.clear();
    chudware::fsBackend().invalidateBrowseCache(workspaceChanged);
}

// Hover tooltip: full file/dir name, plus absolute path on a second line when it adds context.
static void itemPathTooltip(const std::string& name, const std::string& fullPath) {
    if (!ImGui::IsItemHovered()) return;
    if (fullPath.empty() || fullPath == name)
        ImGui::SetTooltip("%s", name.c_str());
    else
        ImGui::SetTooltip("%s\n%s", name.c_str(), fullPath.c_str());
}

// TreeNodeEx label for a row about to be submitted at the cursor: the name elided with "…" to
// the width left after the arrow/icon slot, so a long filename never runs under the pane edge
// (itemPathTooltip carries the full name). A fixed "###row" id excludes the elided label from
// the node identity as the pane resizes; callers PushID the path/index first.
static std::string rowLabel(const std::string& name) {
    const float w = ImGui::GetContentRegionAvail().x - ImGui::GetTreeNodeToLabelSpacing() -
                    ImGui::GetStyle().FramePadding.x;
    return ui::kit::elide(name.c_str(), w) + "###row";
}


// ---- known extensions ----------------------------------------------------
// .step+ is our native part container and .draw+ is a standalone editable drawing;
// the rest are import/interop formats (AutoCAD .dxf/.dwg and ACIS .sat/.sab included).
// ECAD/KiCad files open through the ECAD path. Lower-cased compare.
static bool isCadExt(const std::string& e) {
    return e == ".draw+" || e == ".step+" || e == ".step" || e == ".stp" ||
           e == ".iges" || e == ".igs" || e == ".stl" || e == ".3mf" ||
           e == ".dxf" || e == ".dwg" || e == ".sat" || e == ".sab";
}
static bool isEcadExt(const std::string& e) {
    return e == ".ecad+" || e == ".ecad" || e == ".kicad_sch" || e == ".kicad_pcb";
}
static bool isKnownExt(const std::string& e) { return isCadExt(e) || isEcadExt(e); }

// Lower-cased extension of a path, INCLUDING the leading dot.
static std::string lowerExt(const std::string& p) {
    std::string e = pathExtension(pathFilename(p));
    std::transform(e.begin(), e.end(), e.begin(),
                   [](unsigned char c) { return (char)std::tolower(c); });
    return e;
}

// True when `child` is inside (or equal to) `root` — the guard for every destructive op so the
// Explorer can never rename/delete/paste outside the workspace. Compares lexically-normal paths.
static bool withinWorkspace(const std::string& root, const std::string& child) {
    auto norm = [](std::string s) {
        for (char& c : s)
            if (c == '\\') c = '/';
        // strip trailing slash (except root "/")
        while (s.size() > 1 && s.back() == '/') s.pop_back();
        return s;
    };
    std::string rs = norm(root), cs = norm(child);
    if (cs.size() < rs.size()) return false;
    for (size_t i = 0; i < rs.size(); ++i)
        if (std::tolower((unsigned char)cs[i]) != std::tolower((unsigned char)rs[i])) return false;
    return cs.size() == rs.size() || cs[rs.size()] == '/';
}

// Pick a per-extension atlas icon name (verified against host/assets/icons/atlas.json). Folders use
// "body_folder"; CAD parts use "part"; everything else falls back to a generic doc-ish glyph.
static const char* fileIconName(const std::string& ext) {
    if (ext == ".step+" || ext == ".step" || ext == ".stp") return "part";
    if (ext == ".iges" || ext == ".igs" || ext == ".stl" || ext == ".3mf") return "part";
    if (ext == ".ecad+" || ext == ".ecad") return "ai_panel";   // generic-but-present glyph
    if (ext == ".kicad_sch") return "ai_panel";
    if (ext == ".kicad_pcb") return "ai_panel";
    return "new_doc";
}

// Text fallback glyph for a row whose atlas icon is missing (fmTreeIcon* contract).
static const char* fileIconFallback(const std::string& ext) {
    return isEcadExt(ext) ? "[E]" : "[F]";
}

// ---- workspace resolution ------------------------------------------------
void ensureWorkspaceDir(AppState& st) {
    auto& fsb = chudware::fsBackend();
    if (st.workspaceDir.empty()) st.workspaceDir = st.settings.workspaceDir;
#ifdef __EMSCRIPTEN__
    // The gateway creates its authorized root. Never mkdir from a draw call.
    // Do not publish "/" as a temporary daemon path while discovery is pending.
    if (st.workspaceDir.empty() || st.workspaceDir == "/" || st.workspaceDir == "/workspace")
        st.workspaceDir = fsb.pollDefaultRoot(s_workspaceError);
#else
    if (st.workspaceDir.empty()) st.workspaceDir = fsb.defaultRoot();
#endif
    static std::string s_lastRoot;
    if (st.workspaceDir != s_lastRoot) {
        invalidateDirListCache(/*workspaceChanged=*/!s_lastRoot.empty());
        s_lastRoot = st.workspaceDir;
        s_currentFolder = st.workspaceDir;
        s_selectedPath.clear();
        s_renameId.clear();
        s_renameFocus = false;
        s_pendingDelete.clear();
        s_pendingNewFolder.clear();
        s_clipboardPath.clear();
#ifndef __EMSCRIPTEN__
        // Desktop setup is idempotent once per workspace, not once per frame.
        fsb.mkdir(st.workspaceDir, s_workspaceError);
#endif
    }
}

// ---- inline rename commit -------------------------------------------------
static void commitRename(AppState& st, const std::string& root) {
    if (s_renameId.empty()) return;
    auto& fsb = chudware::fsBackend();
    std::string oldP = s_renameId;
    std::string newName = s_renameBuf;
    // trim
    while (!newName.empty() && (newName.front() == ' ')) newName.erase(newName.begin());
    while (!newName.empty() && (newName.back()  == ' ')) newName.pop_back();
    s_renameId.clear();
    s_renameFocus = false;
    if (s_pendingNewFolder == oldP) s_pendingNewFolder.clear();
    if (newName.empty() || newName == pathFilename(oldP)) return;
    if (newName.find_first_of("\\/:*?\"<>|") != std::string::npos) {
        st.toast(ui::ToastLevel::Warn, "Invalid characters in name");
        return;
    }
    std::string newP = pathJoin(pathParent(oldP), newName);
    if (!withinWorkspace(root, oldP) || !withinWorkspace(root, newP)) {
        st.toast(ui::ToastLevel::Warn, "Rename blocked outside the workspace");
        return;
    }
    std::string err;
    const auto destination = fsb.stat(newP, err);
    if (!err.empty()) { st.toast(ui::ToastLevel::Error, "Rename failed: " + err); return; }
    if (destination.exists) { st.toast(ui::ToastLevel::Warn, "A file with that name exists"); return; }
    if (!fsb.rename(oldP, newP, err)) {
        st.toast(ui::ToastLevel::Error, "Rename failed: " + err);
    } else {
        invalidateDirListCache();
        invalidateExplorerPlm();
        st.addLog("[explorer] renamed " + pathFilename(oldP) + " -> " + newName);
        auto remap = [&](std::string& path) {
            if (!path.empty() && withinWorkspace(oldP, path))
                path.replace(0, oldP.size(), newP);
        };
        remap(s_selectedPath);
        remap(s_currentFolder);
        remap(s_clipboardPath);
        remap(s_pendingNewFolder);
        remap(s_pendingDelete);
    }
}

// ---- copy / paste (filesystem) -------------------------------------------
// Build a collision-free destination by appending " (copy)" (then " (copy 2)", ...) before a
// file's extension, or after a folder's complete name, until the name is free.
static std::string uniqueDest(const std::string& dir, const std::string& src, bool isDir,
                              std::string& err) {
    auto& fsb = chudware::fsBackend();
    std::string fname = pathFilename(src);
    std::string stem = isDir ? fname : pathStem(fname);
    std::string ext  = isDir ? std::string() : pathExtension(fname);
    std::string cand = pathJoin(dir, fname);
    if (!fsb.stat(cand, err).exists) return err.empty() ? cand : std::string();
    for (int n = 1; n < 1000; ++n) {
        std::string suffix = (n == 1) ? " (copy)" : (" (copy " + std::to_string(n) + ")");
        cand = pathJoin(dir, stem + suffix + ext);
        if (!fsb.stat(cand, err).exists) return err.empty() ? cand : std::string();
    }
    err = "No unused copy name available";
    return {};
}

static bool doPaste(AppState& st, const std::string& root, const std::string& destDir) {
    auto& fsb = chudware::fsBackend();
    if (s_clipboardPath.empty()) { st.toast(ui::ToastLevel::Info, "Nothing copied"); return false; }
    std::string src = s_clipboardPath;
    std::string err;
    const auto source = fsb.stat(src, err);
    if (!err.empty()) { st.toast(ui::ToastLevel::Error, "Paste failed: " + err); return false; }
    if (!source.exists) { st.toast(ui::ToastLevel::Warn, "Copied file no longer exists"); return false; }
    if (!withinWorkspace(root, destDir)) { st.toast(ui::ToastLevel::Warn, "Paste blocked outside the workspace"); return false; }
    if (source.isDir && withinWorkspace(src, destDir)) {
        st.toast(ui::ToastLevel::Warn, "Cannot paste a folder into itself or its descendants");
        return false;
    }
    std::string dest = uniqueDest(destDir, src, source.isDir, err);
    if (!err.empty()) { st.toast(ui::ToastLevel::Error, "Paste failed: " + err); return false; }
    if (!fsb.copy(src, dest, /*recursive=*/source.isDir, err)) {
        st.toast(ui::ToastLevel::Error, "Paste failed: " + err);
        return false;
    }
    st.addLog("[explorer] pasted -> " + dest);
    invalidateDirListCache();
    invalidateExplorerPlm();

    st.toast(ui::ToastLevel::Info, "Pasted " + pathFilename(dest));
    s_selectedPath = dest;
    return true;
}

static void createNewFolder(AppState& st, const std::string& root, const std::string& dir) {
    auto& fsb = chudware::fsBackend();
    if (!withinWorkspace(root, dir)) {
        st.toast(ui::ToastLevel::Warn, "New Folder blocked outside the workspace");
        return;
    }
    int n = 1;
    std::string cand = pathJoin(dir, "New Folder");
    std::string err;
    while (fsb.stat(cand, err).exists && err.empty())
        cand = pathJoin(dir, "New Folder " + std::to_string(++n));
    if (!err.empty()) { st.toast(ui::ToastLevel::Error, "New Folder failed: " + err); return; }
    if (fsb.mkdir(cand, err)) {
        s_renameId.clear();
        s_renameFocus = false;
        s_pendingNewFolder = cand;
        s_currentFolder = dir;
        st.addLog("[explorer] new folder " + cand);
        invalidateDirListCache();
        invalidateExplorerPlm();

    } else {
        st.toast(ui::ToastLevel::Error, "New Folder failed" + (err.empty() ? "" : (": " + err)));
    }
}

// ---- open dispatch --------------------------------------------------------
// Route a double-clicked file to the right open path. CAD + ECAD both go through st.requestOpenPath;
// app.cpp's request pump inspects the extension and dispatches (CAD -> featureTreeOpenPath, ECAD ->
// the ECAD open path). Keeping the actual open on the GL-thread pump avoids re-entrant doc swaps
// mid-tree-walk.
static void requestOpen(AppState& st, const std::string& p) {
    std::string e = lowerExt(p);
    if (!isKnownExt(e)) return;
    st.requestOpenPath = p;
}

static kit::Tone changeTone(const std::string& code) {
    if (code == "D") return kit::Tone::Err;
    if (code == "M") return kit::Tone::Warn;
    if (code == "A" || code == "U") return kit::Tone::Accent;
    return kit::Tone::Neutral;
}

static const char* changeName(const std::string& code) {
    if (code == "M") return "Modified";
    if (code == "A") return "Added";
    if (code == "D") return "Deleted";
    if (code == "U") return "Untracked";
    return "";
}

static void drawChanges(AppState& st, const std::string& project) {
    auto& P = s_plm;
    int count = 0;
    if (P.ready) {
        for (const auto& row : P.items) if (!row.change.empty()) ++count;
        for (const auto& row : P.untracked) if (!row.change.empty()) ++count;
    }
    const std::string title = P.ready ? "Changes (" + std::to_string(count) + ")" : "Changes";
    const bool open = kit::cardBegin("##explorer_changes", title.c_str(), "history", &P.changesOpen);
    if (open) {
        if (kit::iconButton("##changes_refresh", "refresh", "Refresh vault changes",
                            !P.loading && !P.checkpointing && st.workerStarted)) {
            invalidateExplorerPlm();
            pollExplorerPlm(st, project, true);
        }
        ImGui::SameLine();
        kit::dimText("Project-wide checkpoint");
        if (P.loading && P.ready) kit::spinner("Refreshing project changes");
        if (!st.workerStarted)
            kit::blockedState("Vault offline", "The Chudware service is unavailable.",
                              "Files can still be browsed; reconnect to see changes.");
        else if (!P.error.empty())
            kit::blockedState("Vault status unavailable", P.error.c_str(), "Refresh to retry.");
        else if (P.loading && !P.ready)
            kit::spinner("Checking project changes");
        else if (!P.ready)
            kit::emptyState("history", "Checking vault", "Your files are still available below.");
        else if (!count)
            kit::emptyState("ok_badge", "No changes", "The project matches its last checkpoint.");
        else {
            ImGui::BeginChild("##changes_rows", ImVec2(0, std::min(160.0f * uiScale,
                       ImGui::GetTextLineHeightWithSpacing() * (count + 1))),
                       ImGuiChildFlags_None, ImGuiWindowFlags_NoScrollbar);
            auto drawRow = [&](const ExplorerPlmItem& item) {
                if (item.change.empty()) return;
                ImGui::PushID(item.path.c_str());
                kit::RowStyle style;
                style.icon = "new_doc";
                style.iconTone = changeTone(item.change);
                style.meta = item.change.c_str();
                const std::string tooltip = item.path + "\n" + changeName(item.change);
                style.tooltip = tooltip.c_str();
                if (kit::row("##change", item.path.c_str(), style)) {
                    const std::string absolute = pathJoin(project, item.path);
                    if (item.change != "D" && isKnownExt(lowerExt(absolute)))
                        requestOpen(st, absolute);
                    else if (!item.itemId.empty())
                        openPlm(st, PlmPage::Files, item.itemId);
                }
                if (!item.itemId.empty()) {
                    ui::pushContextMenuStyle();
                    if (ImGui::BeginPopupContextItem("##change_menu")) {
                        if (ImGui::MenuItem("History"))
                            openPlm(st, PlmPage::Files, item.itemId);
                        if (ImGui::MenuItem("Release in PLM"))
                            openPlm(st, PlmPage::Releases, item.itemId);
                        ImGui::EndPopup();
                    }
                    ui::popContextMenuStyle();
                }
                ImGui::PopID();
            };
            for (const auto& item : P.items) drawRow(item);
            for (const auto& item : P.untracked) drawRow(item);
            ui::drawWindowPillScrollbarV(explorerSurf());
            ImGui::EndChild();
        }
        kit::rule();
        ImGui::SetNextItemWidth(-1);
        ImGui::InputTextWithHint("##checkpoint_msg", "Checkpoint message",
                                 P.message, sizeof(P.message));
        if (kit::primaryButton("Checkpoint", P.ready && !P.loading && !P.checkpointing &&
                                               count > 0 && st.workerStarted,
                               "Requires available vault changes")) {
            P.checkpointing = true;
            P.checkpointError.clear();
            P.checkpointOk.clear();
            const std::string message = P.message;
            const unsigned serial = P.serial;
            st.rpc.call("plm_checkpoint", {{"path", project}, {"message", message}},
                        [project, serial, message, &st](const core::RpcResult& r) {
                auto& state = s_plm;
                if (state.project != project || state.serial != serial ||
                    st.plmProjectPath() != project) return;
                state.checkpointing = false;
                if (!plmOk(r, state.checkpointError)) return;
                state.checkpointOk = "Checkpoint saved";
                state.checkpointError.clear();
                if (message == state.message) state.message[0] = '\0';
                invalidateExplorerPlm();
            });
        }
        ImGui::SameLine();
        if (kit::secondaryButton("PLM", st.workerStarted, "Service unavailable"))
            openPlm(st, PlmPage::Files);
        if (P.checkpointing) kit::spinner("Saving checkpoint");
        kit::inlineStatus(P.checkpointError.c_str(), kit::Tone::Err);
        kit::inlineStatus(P.checkpointOk.c_str(), kit::Tone::Ok);
    }
    kit::cardEnd();
}

// ---- tree node rendering --------------------------------------------------
// A file row: icon + name, selectable, double-click opens, F2/Del/context-menu act on it.
static void drawFileRow(AppState& st, const std::string& root, const std::string& p,
                        const std::string& ext) {
    const std::string& pathStr = p;
    const bool isRenaming = (s_renameId == pathStr);
    const bool selected = (s_selectedPath == pathStr);
    const std::string fname = pathFilename(p);
    const ExplorerPlmItem* status = statusForPath(p);
    const std::string annotated = statusFileLabel(fname, status);
    ImGui::PushID(pathStr.c_str());
    if (isRenaming) {
        // Inline rename: pill + icon + InputText (same frame height as a normal row).
        ui::FmRowRect rr = ui::fmRowPill(selected);
        ui::fmTreeIcon(fileIconName(ext), fileIconFallback(ext));
        ImGui::SameLine(0, 4.0f * ui::uiScale);
        ImGui::SetNextItemWidth(-1);
        if (s_renameFocus) { ImGui::SetKeyboardFocusHere(); s_renameFocus = false; }
        if (ImGui::InputText("##rn", s_renameBuf, sizeof(s_renameBuf),
                             ImGuiInputTextFlags_EnterReturnsTrue | ImGuiInputTextFlags_AutoSelectAll))
            commitRename(st, root);
        if (ImGui::IsItemDeactivated()) commitRename(st, root);   // click-away / Esc commits-or-cancels
        ui::fmRowPillEnd(rr, selected);
    } else {
        // FM leaf-row pattern (identical to the FeatureManager plane rows): pill FIRST at the
        // row's true left edge, then a leaf TreeNodeEx, then the type icon painted into the
        // unused leaf-arrow slot so icon + label share the FM's exact metrics and centering.
        ui::FmRowRect rr = ui::fmRowPill(selected);
        ImVec2 rowPos = ImGui::GetCursorScreenPos();
        ImGui::TreeNodeEx(rowLabel(annotated).c_str(),
                          ImGuiTreeNodeFlags_Leaf | ImGuiTreeNodeFlags_NoTreePushOnOpen |
                          ImGuiTreeNodeFlags_SpanFullWidth | ImGuiTreeNodeFlags_FramePadding);
        ui::fmTreeIconInArrowSlot(rowPos, fileIconName(ext), fileIconFallback(ext));
        ui::fmRowPillEnd(rr, selected);
        if (ImGui::IsItemClicked()) {
            s_selectedPath = pathStr;
            s_currentFolder = pathParent(p);
            if (ImGui::IsMouseDoubleClicked(ImGuiMouseButton_Left)) requestOpen(st, p);
        }
        // Drag model files to the viewport, or JSON scenarios to simulation setup.
        // The absolute path includes its NUL terminator; each target validates
        // the formats it accepts. Drag preview = the filename.
        if ((isKnownExt(ext) || ext == ".json") &&
            ImGui::BeginDragDropSource(ImGuiDragDropFlags_SourceAllowNullID)) {
            ImGui::SetDragDropPayload(kFileDragPayload, pathStr.c_str(), pathStr.size() + 1);
            ImGui::TextUnformatted(fname.c_str());
            ImGui::EndDragDropSource();
        }
        // context menu
        ui::pushContextMenuStyle();
        if (ImGui::BeginPopupContextItem("##fctx")) {
            s_selectedPath = pathStr;
            s_currentFolder = pathParent(p);
            if (ImGui::MenuItem("Open", nullptr, false, isKnownExt(ext))) requestOpen(st, p);
            if (status && !status->itemId.empty()) {
                if (ImGui::MenuItem("History"))
                    openPlm(st, PlmPage::Files, status->itemId);
                if (ImGui::MenuItem("Release in PLM"))
                    openPlm(st, PlmPage::Releases, status->itemId);
            }
            ImGui::Separator();
            if (ImGui::MenuItem("Rename", "F2")) {
                s_pendingNewFolder.clear();
                s_renameId = pathStr;
                std::snprintf(s_renameBuf, sizeof(s_renameBuf), "%s", fname.c_str());
                s_renameFocus = true;
            }
            if (ImGui::MenuItem("Copy", "Ctrl+C")) s_clipboardPath = pathStr;
            if (ImGui::MenuItem("Paste", "Ctrl+V", false, !s_clipboardPath.empty()))
                doPaste(st, root, pathParent(p));
            if (ImGui::MenuItem("Delete", "Del")) s_pendingDelete = pathStr;
            ImGui::EndPopup();
        }
        ui::popContextMenuStyle();
        itemPathTooltip(fname, pathStr);
        if (ImGui::IsItemHovered() && status) {
            const std::string rev = status->workingRev.empty() ? status->rev : status->workingRev;
            std::string detail = pathStr;
            if (!status->change.empty()) detail += "\n" + std::string(changeName(status->change));
            if (!rev.empty()) detail += "\nRevision " + rev;
            if (!status->lifecycle.empty()) detail += "\n" + status->lifecycle;
            ImGui::SetTooltip("%s", detail.c_str());
        }
    }
    ImGui::PopID();
}

// Only explicit suffixes count as legacy copies: generic trailing numbers often
// identify distinct model sizes, not revisions. This groups siblings visually,
// without renaming, deleting, or treating them as revisions of one vault item.
static std::string legacySeriesName(const std::string& path) {
    const std::string name = pathFilename(path), ext = pathExtension(name);
    if (!isKnownExt(lowerExt(path))) return {};
    const std::string stem = pathStem(name);
    std::string low = stem;
    std::transform(low.begin(), low.end(), low.begin(),
                   [](unsigned char c) { return (char)std::tolower(c); });
    for (size_t at = low.find_last_of("_-"); at != std::string::npos && at > 0;
         at = low.find_last_of("_-", at - 1)) {
        const std::string suffix = low.substr(at + 1);
        bool version = false;
        if (suffix.size() >= 2 && suffix[0] == 'v') {
            version = std::all_of(suffix.begin() + 1, suffix.end(),
                                  [](unsigned char c) { return std::isdigit(c); });
        } else if (suffix.size() >= 4 && suffix.compare(0, 3, "rev") == 0) {
            const size_t end = suffix.find("_final", 3);
            const std::string rev = suffix.substr(3, end == std::string::npos ? end : end - 3);
            version = !rev.empty() && std::all_of(rev.begin(), rev.end(),
                           [](unsigned char c) { return std::isalnum(c); });
            if (end != std::string::npos) {
                const std::string final = suffix.substr(end + 6);
                version = version && !final.empty() &&
                          std::all_of(final.begin(), final.end(),
                                      [](unsigned char c) { return std::isdigit(c); });
            }
        }
        if (version) {
            std::string key = low.substr(0, at) + ext;
            std::transform(key.begin(), key.end(), key.begin(),
                           [](unsigned char c) { return (char)std::tolower(c); });
            return key;
        }
    }
    return {};
}

static void groupLegacyFiles(DirListCache& rows) {
    const auto& files = rows.files;
    rows.fileGroup.assign(files.size(), -1);
    std::map<std::string, std::vector<size_t>> groups;
    for (size_t i = 0; i < files.size(); ++i) {
        const std::string key = legacySeriesName(files[i]);
        if (!key.empty()) groups[key].push_back(i);
    }
    for (size_t i = 0; i < files.size(); ++i) {
        std::string key = pathFilename(files[i]);
        std::transform(key.begin(), key.end(), key.begin(),
                       [](unsigned char c) { return (char)std::tolower(c); });
        const auto found = groups.find(key);
        if (found != groups.end()) found->second.insert(found->second.begin(), i);
    }
    for (auto& [key, members] : groups) {
        if (members.size() < 2) continue;
        LegacyFileGroup group;
        group.members = std::move(members);
        group.head = *std::min_element(group.members.begin(), group.members.end());
        const std::string firstName = pathFilename(files[group.members.front()]);
        const std::string ext = pathExtension(firstName);
        const std::string name = legacySeriesName(files[group.members.front()]).empty()
            ? firstName : pathStem(firstName).substr(0, key.size() - ext.size()) + ext;
        group.title = name + " · " + std::to_string(group.members.size()) + " files";
        for (const size_t index : group.members)
            rows.fileGroup[index] = static_cast<int>(rows.legacyGroups.size());
        rows.legacyGroups.push_back(std::move(group));
    }
}

static void drawDirectoryFiles(AppState& st, const std::string& root,
                               const DirListCache& rows) {
    for (size_t i = 0; i < rows.files.size(); ++i) {
        const int groupIndex = rows.fileGroup[i];
        if (groupIndex < 0) {
            drawFileRow(st, root, rows.files[i], lowerExt(rows.files[i]));
            continue;
        }
        const LegacyFileGroup& group = rows.legacyGroups[static_cast<size_t>(groupIndex)];
        if (i != group.head) continue;
        ImGui::PushID(rows.files[group.head].c_str());
        const bool open = ImGui::TreeNodeEx(rowLabel(group.title).c_str(),
                         ImGuiTreeNodeFlags_OpenOnArrow |
                         ImGuiTreeNodeFlags_SpanFullWidth | ImGuiTreeNodeFlags_FramePadding);
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("These are separate files and may be separate PLM items. "
                              "No files have been merged or removed.");
        if (open) {
            kit::inlineStatus("Revision-named copies are separate files, not merged.",
                              kit::Tone::Warn);
            for (const size_t index : group.members)
                drawFileRow(st, root, rows.files[index], lowerExt(rows.files[index]));
            ImGui::TreePop();
        }
        ImGui::PopID();
    }
}

// Recursively draw a directory: lazy-list children on open, folders before files.
// Hide dot entries, not non-CAD files. Backend snapshots own refresh scheduling.
static void drawDirNode(AppState& st, const std::string& root, const std::string& dir, bool isRoot) {
    auto& fsb = chudware::fsBackend();
    // The "Workspace" section rule above the tree already names the section; the root row just
    // shows the workspace folder's own name (FM-style: divider carries the section label).
    std::string label = pathFilename(dir);
    if (label.empty() || label == "/" || (label.size() == 2 && label[1] == ':'))
        label = dir;  // root-ish
    ImGuiTreeNodeFlags flags = ImGuiTreeNodeFlags_OpenOnArrow | ImGuiTreeNodeFlags_SpanFullWidth |
                               ImGuiTreeNodeFlags_FramePadding;   // pill-height node
    if (isRoot) flags |= ImGuiTreeNodeFlags_DefaultOpen;
    if (!isRoot && s_pendingNewFolder == dir && s_renameId != dir) {
        s_renameId = dir;
        std::snprintf(s_renameBuf, sizeof(s_renameBuf), "%s", pathFilename(dir).c_str());
        s_renameFocus = true;
        s_selectedPath = dir;
    }
    const bool dirSelected = (s_selectedPath == dir);

    ImGui::PushID(dir.c_str());

    // A non-root folder being inline-renamed shows an InputText in place of its tree node; its
    // children stay collapsed during the rename (kept simple — folder rename is a leaf interaction).
    if (!isRoot && s_renameId == dir) {
        ui::FmRowRect rr = ui::fmRowPill(dirSelected);
        ui::fmTreeIcon("body_folder", "[D]");
        ImGui::SameLine(0, 4.0f * ui::uiScale);
        ImGui::SetNextItemWidth(-1);
        const bool revealing = (s_pendingNewFolder == dir);
        if (s_renameFocus) ImGui::SetKeyboardFocusHere();
        if (ImGui::InputText("##rnd", s_renameBuf, sizeof(s_renameBuf),
                             ImGuiInputTextFlags_EnterReturnsTrue | ImGuiInputTextFlags_AutoSelectAll))
            commitRename(st, root);
        if (ImGui::IsItemDeactivated()) commitRename(st, root);
        if (revealing) {
            ImGui::SetScrollHereY(0.5f);
            // A clipped row needs another frame after scrolling before reveal is consumed.
            if (ImGui::IsItemVisible()) {
                s_pendingNewFolder.clear();
                s_renameFocus = false;
            }
        } else {
            s_renameFocus = false;
        }
        ui::fmRowPillEnd(rr, dirSelected);
        ImGui::PopID();
        return;
    }

    // FM expandable-row pattern (identical to the FeatureManager Origin row): pill, 16px icon as
    // its own item, then the chevron node. Pill ends right after the node, BEFORE children.
    ui::FmRowRect rr = ui::fmRowPill(dirSelected);
    ui::fmTreeIcon("body_folder", "[D]");
    ImGui::SameLine(0, 4.0f * ui::uiScale);
    if (!s_pendingNewFolder.empty() && withinWorkspace(dir, s_pendingNewFolder))
        ImGui::SetNextItemOpen(true, ImGuiCond_Always);
    bool open = ImGui::TreeNodeEx(rowLabel(label).c_str(), flags);
    ui::fmRowPillEnd(rr, dirSelected);
    if (ImGui::IsItemClicked() && !ImGui::IsItemToggledOpen()) {
        s_selectedPath = dir;
        s_currentFolder = dir;
    }
    itemPathTooltip(label, dir);
    // folder context menu (New Folder / Paste / Rename / Delete — root only allows New Folder/Paste)
    ui::pushContextMenuStyle();
    if (ImGui::BeginPopupContextItem("##dctx")) {
        s_selectedPath = dir;
        s_currentFolder = dir;
        if (ImGui::MenuItem("New Folder")) {
            createNewFolder(st, root, dir);
        }
        if (ImGui::MenuItem("Set as working folder")) setActiveProjectDir(st, dir);
        if (ImGui::MenuItem("Paste", "Ctrl+V", false, !s_clipboardPath.empty()))
            doPaste(st, root, dir);
        if (!isRoot) {
            ImGui::Separator();
            if (ImGui::MenuItem("Rename", "F2")) {
                s_pendingNewFolder.clear();
                s_renameId = dir;
                std::snprintf(s_renameBuf, sizeof(s_renameBuf), "%s", pathFilename(dir).c_str());
                s_renameFocus = true;
            }
            if (ImGui::MenuItem("Delete", "Del")) s_pendingDelete = dir;
        }
        ImGui::EndPopup();
    }
    ui::popContextMenuStyle();

    if (open) {
        const auto& snapshot = fsb.pollDirectory(dir);
        auto& cached = s_dirListCache[dir];
        if (!cached || cached->revision != snapshot.revision || cached->state != snapshot.state) {
            auto fresh = std::make_shared<DirListCache>();
            fresh->revision = snapshot.revision;
            fresh->state = snapshot.state;
            fresh->error = snapshot.error;
            const auto& entries = snapshot.entries;
            fresh->files.reserve(entries.size());
            for (const auto& e : entries) {
                if (e.name.empty() || e.name[0] == '.') continue;
                std::string full = pathJoin(dir, e.name);
                if (e.isDir) {
                    fresh->dirs.push_back(std::move(full));
                } else {
                    fresh->files.push_back(std::move(full));
                }
            }
            auto byName = [](const std::string& a, const std::string& b) {
                std::string an = pathFilename(a), bn = pathFilename(b);
                std::transform(an.begin(), an.end(), an.begin(), ::tolower);
                std::transform(bn.begin(), bn.end(), bn.begin(), ::tolower);
                return an < bn;
            };
            std::sort(fresh->dirs.begin(), fresh->dirs.end(), byName);
            std::sort(fresh->files.begin(), fresh->files.end(), byName);
            groupLegacyFiles(*fresh);
            cached = std::move(fresh);
        }
        const auto rows = cached;
        for (const auto& d : rows->dirs) drawDirNode(st, root, d, /*isRoot=*/false);
        drawDirectoryFiles(st, root, *rows);
        // Loading is not absence: web mutations invalidate listings before their async refresh.
        // Stop forcing ancestors only when a completed listing cannot contain the reveal target.
        if (!s_pendingNewFolder.empty() && withinWorkspace(dir, s_pendingNewFolder) &&
            rows->state != chudware::FsReadState::Loading &&
            (rows->state != chudware::FsReadState::Ready ||
             std::none_of(rows->dirs.begin(), rows->dirs.end(), [](const std::string& child) {
                 return withinWorkspace(child, s_pendingNewFolder);
             }))) {
            if (s_renameId == s_pendingNewFolder) s_renameId.clear();
            s_pendingNewFolder.clear();
            s_renameFocus = false;
        }
        if (rows->state == chudware::FsReadState::Error)
            ImGui::TextWrapped("Unable to read folder: %s", rows->error.c_str());
        else if (rows->state == chudware::FsReadState::Missing)
            ImGui::TextDisabled("  Folder missing");
        else if (rows->state == chudware::FsReadState::Loading)
            ImGui::TextDisabled("  Loading...");
        else if (rows->dirs.empty() && rows->files.empty())
            ImGui::TextDisabled("  (empty)");
        ImGui::TreePop();
    }
    ImGui::PopID();
}

// ---- Recent section -------------------------------------------------------
static bool s_recentOpen = false;
static void drawRecentSection(AppState& st) {
    if (st.recentFiles.empty()) return;
    auto& fsb = chudware::fsBackend();
    if (!kit::cardBegin("##explorer_recent", "Recent", "history", &s_recentOpen)) {
        kit::cardEnd();
        return;
    }
#ifndef __EMSCRIPTEN__
    static std::map<std::string, chudware::FsStatSnapshot> recentStats;
    static double checkedAt = -1e9;
    if (ImGui::GetTime() - checkedAt > 2.0) {
        recentStats.clear();
        checkedAt = ImGui::GetTime();
    }
#endif
    int idx = 0;
    for (const std::string& rp : st.recentFiles) {
        ImGui::PushID(idx++);
#ifdef __EMSCRIPTEN__
        const auto probe = fsb.pollStat(rp);
#else
        auto found = recentStats.find(rp);
        if (found == recentStats.end())
            found = recentStats.emplace(rp, fsb.pollStat(rp)).first;
        const auto& probe = found->second;
#endif
        // Unknown/network errors are not evidence of deletion. Keep opening
        // available; doc_open reports authoritative failures without a preflight.
        const bool exists = probe.state != chudware::FsReadState::Ready || probe.value.exists;
        const std::string ext = lowerExt(rp);
        const std::string fname = pathFilename(rp);
        // Same FM leaf-row pattern as the tree's file rows; a missing file keeps a readable dim
        // state (dim text + faded icon), like the FM's hidden planes.
        ui::FmRowRect rr = ui::fmRowPill(false);
        ImVec2 rowPos = ImGui::GetCursorScreenPos();
        if (!exists) ImGui::PushStyleColor(ImGuiCol_Text, explorerSurf().textSecondary);
        ImGui::TreeNodeEx(rowLabel(statusFileLabel(fname, statusForPath(rp))).c_str(),
                          ImGuiTreeNodeFlags_Leaf | ImGuiTreeNodeFlags_NoTreePushOnOpen |
                          ImGuiTreeNodeFlags_SpanFullWidth | ImGuiTreeNodeFlags_FramePadding);
        if (!exists) ImGui::PopStyleColor();
        ui::fmTreeIconInArrowSlot(rowPos, fileIconName(ext), fileIconFallback(ext),
                                  exists ? 1.0f : 0.45f);
        ui::fmRowPillEnd(rr, false);
        if (ImGui::IsItemClicked() && ImGui::IsMouseDoubleClicked(ImGuiMouseButton_Left) && exists)
            requestOpen(st, rp);
        // Recent files use the same model/scenario payload as directory rows.
        if (exists && (isKnownExt(ext) || ext == ".json") &&
            ImGui::BeginDragDropSource(ImGuiDragDropFlags_SourceAllowNullID)) {
            ImGui::SetDragDropPayload(kFileDragPayload, rp.c_str(), rp.size() + 1);
            ImGui::TextUnformatted(fname.c_str());
            ImGui::EndDragDropSource();
        }
        itemPathTooltip(fname, rp);
        ImGui::PopID();
    }
    kit::cardEnd();
}

// ---- delete confirm modal -------------------------------------------------
static void drawDeleteModal(AppState& st, const std::string& root) {
    auto& fsb = chudware::fsBackend();
    if (!s_pendingDelete.empty() && !ImGui::IsPopupOpen("Delete?"))
        ImGui::OpenPopup("Delete?");
    bool open = !s_pendingDelete.empty();
    if (ImGui::BeginPopupModal("Delete?", &open, ImGuiWindowFlags_AlwaysAutoResize)) {
        std::string p = s_pendingDelete;
        ImGui::Text("Delete \"%s\"?", pathFilename(p).c_str());
        const auto probe = fsb.pollStat(p);
        if (probe.state == chudware::FsReadState::Ready && probe.value.isDir)
            ImGui::TextDisabled("This folder and its contents.");
        ImGui::Spacing();
        if (ImGui::Button("Delete", ImVec2(110 * ui::uiScale, 0))) {
            if (!withinWorkspace(root, p)) {
                st.toast(ui::ToastLevel::Warn, "Delete blocked outside the workspace");
            } else {
                std::string err;
                if (!fsb.remove(p, err)) {
                    st.toast(ui::ToastLevel::Error, "Delete failed: " + err);
                } else {
                    invalidateDirListCache();
                    invalidateExplorerPlm();
                    st.addLog("[explorer] deleted " + p);
                    st.toast(ui::ToastLevel::Info, "Deleted " + pathFilename(p));
                    if (s_selectedPath == s_pendingDelete) s_selectedPath.clear();
                }
            }
            s_pendingDelete.clear();
            ImGui::CloseCurrentPopup();
        }
        ImGui::SameLine();
        if (ImGui::Button("Cancel", ImVec2(110 * ui::uiScale, 0))) {
            s_pendingDelete.clear();
            ImGui::CloseCurrentPopup();
        }
        ImGui::EndPopup();
    }
    if (!open) s_pendingDelete.clear();   // window-X closed it
}

// ---- body -----------------------------------------------------------------
void drawFileExplorerBody(AppState& st) {
    auto& fsb = chudware::fsBackend();
    ui::fmRowStripeReset();   // restart the FM stripe phase + refresh the shared FM palette

    // Compact vertical rhythm (the FeatureManager convention): tighten ItemSpacing.y only so
    // the breadcrumb/rows don't read "double spaced". RAII so it unwinds on any early return.
    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing,
                        ImVec2(ImGui::GetStyle().ItemSpacing.x, 4.0f * ui::uiScale));
    struct SpacingPop { ~SpacingPop() { ImGui::PopStyleVar(); } } spacingPop;

    ensureWorkspaceDir(st);
    const std::string root = st.workspaceDir;
    if (root.empty()) {
        ImGui::TextWrapped("%s", s_workspaceError.empty() ? "Loading workspace..." : s_workspaceError.c_str());
        return;
    }
    kit::pushDensity(kit::Density::Compact);
    struct DensityPop { ~DensityPop() { kit::popDensity(); } } densityPop;
    const std::string project = st.plmProjectPath();
    pollExplorerPlm(st, project);
    // Files already names the root in its tree row; avoid repeating it in the header too.
    kit::sectionHeader("Working project", "body_folder", nullptr, -1, true);
    if (!project.empty()) {
        // Last known branch while a refresh is pending; no chip at all until the first answer
        // for this project arrives (a single ref read, issued ahead of the item scan).
        const char* branch = !st.workerStarted ? "Vault offline"
            : !s_plm.branch.empty() ? s_plm.branch.c_str()
            : s_plm.branchKnown ? "Branch unavailable" : nullptr;
        if (branch) {
            kit::chip(branch, kit::Tone::Neutral, false, "Active vault branch");
            if (ImGui::IsItemHovered()) ImGui::SetTooltip("%s", project.c_str());
        }
        drawChanges(st, project);
    } else {
        kit::emptyState("body_folder", "No working project",
                        "Choose a working folder to see its PLM changes.");
    }

    if (ImGui::IsWindowFocused(ImGuiFocusedFlags_RootAndChildWindows) &&
        ImGui::IsKeyPressed(ImGuiKey_Escape, false)) {
        s_pendingNewFolder.clear();
        s_renameId.clear();
        s_renameFocus = false;
    }

    // Capture the OUTER pane's bottom before the auto-sized Files card changes the window.
    const float explorerBottom = ImGui::GetCursorScreenPos().y + ImGui::GetContentRegionAvail().y;
    const bool filesOpen = kit::cardBegin("##explorer_files", "Files", "body_folder",
                                          &s_plm.filesOpen);
    if (!filesOpen) {
        kit::cardEnd();
        drawRecentSection(st);
        drawDeleteModal(st, root);
        return;
    }

    // Reveal remains pending through Loading snapshots; drawDirNode consumes it at the input row.

    std::string current = s_currentFolder.empty() ? root : s_currentFolder;
    const auto currentStat = fsb.pollStat(current);
    if (currentStat.state == chudware::FsReadState::Ready && !currentStat.value.isDir)
        current = pathParent(current);
    if (current.empty() || !withinWorkspace(root, current)) current = root;

    const auto selectedStat = s_selectedPath.empty() ? chudware::FsStatSnapshot{} : fsb.pollStat(s_selectedPath);
    const bool selectedFile = !s_selectedPath.empty() && isKnownExt(lowerExt(s_selectedPath)) &&
        selectedStat.state == chudware::FsReadState::Ready && selectedStat.value.isFile;

    // Toolbar: the exact FeatureManager top-strip layout — a centered row of 24px glass icon
    // buttons (4dp gaps), the row reserved with a Dummy (glass/icons are draw-list only), the
    // layout cursor restored after the absolute-positioned buttons, then Spacing before the body.
    {
        const float S = ui::uiScale;
        const float bw = 24.0f * S, gap = 4.0f * S, nBtns = 4.0f;
        const float toolW = nBtns * bw + (nBtns - 1) * gap;
        ImVec2 toolRowMin = ImGui::GetCursorScreenPos();
        const float availW = ImGui::GetContentRegionAvail().x;
        ImGui::Dummy(ImVec2(availW, bw));
        float bx = toolRowMin.x + std::max(0.0f, (availW - toolW) * 0.5f);
        const float by = toolRowMin.y;
        auto toolBtn = [&](const char* id, const char* icon, const char* tip, bool en) -> bool {
            ImGui::SetCursorScreenPos(ImVec2(bx, by));
            bool hit = ui::fmToolbarIconBtn(id, icon, tip, en);
            bx += bw + gap;
            return hit;
        };
        if (toolBtn("fx_open", "open", "Open selected file", selectedFile) && selectedFile)
            requestOpen(st, s_selectedPath);
        if (toolBtn("fx_new_folder", "folder_new", "New folder", true))
            createNewFolder(st, root, current);
        if (toolBtn("fx_paste", "paste", "Paste into current folder",
                    !s_clipboardPath.empty()) && !s_clipboardPath.empty())
            doPaste(st, root, current);
        if (toolBtn("fx_refresh", "refresh", "Refresh", true)) {
            invalidateDirListCache();
            invalidateExplorerPlm();
            st.toast(ui::ToastLevel::Info, "Explorer refreshed");
        }
        ImGui::SetCursorScreenPos(ImVec2(toolRowMin.x, toolRowMin.y + bw));
        ImGui::Dummy(ImVec2(0, 0));
        ImGui::Spacing();
    }

    // A breadcrumb is useful only below the root; the root tree row already names the project.
    if (current != root) {
        const std::string folderName = pathFilename(current);
        kit::dimText(folderName.c_str());
        if (ImGui::IsItemHovered()) ImGui::SetTooltip("%s", current.c_str());
    }

    // Keyboard: F2 rename / Del delete on the current selection (only when this pane owns nav and no
    // text field is focused — the inline-rename InputText sets WantTextInput, so this won't fire mid
    // rename).
    if (!s_selectedPath.empty() && !ImGui::GetIO().WantTextInput &&
        ImGui::IsWindowFocused(ImGuiFocusedFlags_RootAndChildWindows)) {
        if (ImGui::IsKeyPressed(ImGuiKey_F2, false)) {
            s_pendingNewFolder.clear();
            s_renameId = s_selectedPath;
            std::snprintf(s_renameBuf, sizeof(s_renameBuf), "%s",
                          pathFilename(s_selectedPath).c_str());
            s_renameFocus = true;
        } else if (ImGui::IsKeyPressed(ImGuiKey_Delete, false)) {
            s_pendingDelete = s_selectedPath;
        }
    }

    // Keep the separate Recent header visible; expanded recents use the outer scrollbar.
    const float recentHeaderHeight = st.recentFiles.empty() ? 0.0f :
        kit::padY() * 2.0f +
        std::max(ImGui::GetTextLineHeight(), metrics::iconHeader()) +
        ImGui::GetStyle().ItemSpacing.y;
    const float treeHeight = std::max(96.0f * ui::uiScale,
        explorerBottom - ImGui::GetCursorScreenPos().y -
        ImGui::GetStyle().WindowPadding.y - ImGui::GetStyle().ItemSpacing.y -
        recentHeaderHeight);
    ImGui::BeginChild("##fx_tree", ImVec2(0, treeHeight), ImGuiChildFlags_None,
                      ImGuiWindowFlags_NoScrollbar);
    drawDirNode(st, root, root, /*isRoot=*/true);
    ui::drawWindowPillScrollbarV(explorerSurf());
    ImGui::EndChild();
    kit::cardEnd();
    drawRecentSection(st);

    drawDeleteModal(st, root);
}

// ---- Wave 3: Explorer-tab clipboard hooks (app.cpp's global Ctrl+C/V router) ----
bool fileExplorerActive(const AppState& st) {
    return st.leftPaneTab == kLeftPaneExplorer;
}

bool fileExplorerCopySelection(AppState& st) {
    if (s_selectedPath.empty()) return false;
    s_clipboardPath = s_selectedPath;
    st.toast(ui::ToastLevel::Info, "Copied " + pathFilename(s_selectedPath));
    return true;
}

bool fileExplorerPaste(AppState& st) {
    if (s_clipboardPath.empty()) return false;
    auto& fsb = chudware::fsBackend();
    ensureWorkspaceDir(st);
    std::string root = st.workspaceDir;
    std::string dest = s_currentFolder.empty() ? root : s_currentFolder;
    if (!fsb.isDirectory(dest)) dest = pathParent(dest);   // selection was a file -> its folder
    return doPaste(st, root, dest);
}

}  // namespace ui
