// Explorer: the working folder's file tree first, with filter/search, a compact toolbar, and
// collapsible Recent / Changes strips pinned at the bottom of the pane.
// Vault status/checkpoints are async; filesystem browsing and search use FsBackend snapshots.
// Explicit revision-named sibling files group only in the view: every original path remains
// available for open, rename, copy and delete.
#include <array>
#include <algorithm>
#include <cctype>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstdint>
#include <cstring>
#include <ctime>
#include <deque>
#include <map>
#include <memory>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>

#include "imgui.h"
#include "imgui_internal.h"  // ImRect, RenderArrow, ScrollToItem
#include "widgets/kit.h"
#include "plm/plm_hub.h"
#include "plm/plm_workspace.h"

#include "file_explorer.h"
#include "anim.h"           // ui::anim::requestFrames (busy glyph in a strip header)
#include "dragdrop.h"       // kFileDragPayload (Explorer drag source -> viewport drop target)
#include "feature_tree.h"   // fmRowPill / fmRowPillEnd / fmTreeIconInArrowSlot
#include "file_dialog.h"    // revealInFileManager / haveOsFileManager
#include "file_thumbs.h"    // fileThumb / fileThumbsPump (native document previews)
#include "icons.h"          // getIcon / IconUV
#include "layout.h"         // kLeftPaneExplorer
#include "metrics.h"
#include "workspace_home.h" // workspaceHomeActive (tiles by default on the empty workspace)

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
static std::string lowerCopy(std::string s) {
    std::transform(s.begin(), s.end(), s.begin(),
                   [](unsigned char c) { return (char)std::tolower(c); });
    return s;
}
// Comparison key for one path: forward slashes, no trailing slash, case-folded on Windows. Used
// for expansion state, selection, open-tab and reveal matching, so the same file spelled with a
// different separator (doc tab vs. directory listing) still lines up.
static std::string normKey(std::string s) {
    for (char& c : s)
        if (c == '\\') c = '/';
    while (s.size() > 1 && s.back() == '/') s.pop_back();
#ifdef _WIN32
    s = lowerCopy(std::move(s));
#endif
    return s;
}
// `full` relative to `root` ("" when equal or outside), forward slashes.
static std::string relativeTo(const std::string& root, const std::string& full) {
    if (full.size() <= root.size()) return {};
    std::string rel = full.substr(root.size());
    while (!rel.empty() && (rel.front() == '/' || rel.front() == '\\')) rel.erase(rel.begin());
    for (char& c : rel)
        if (c == '\\') c = '/';
    return rel;
}

// Natural, case-insensitive order: digit runs compare by value, so stack_2 < stack_10.
static bool naturalLess(const std::string& a, const std::string& b) {
    size_t i = 0, j = 0;
    while (i < a.size() && j < b.size()) {
        const unsigned char ca = (unsigned char)a[i], cb = (unsigned char)b[j];
        if (std::isdigit(ca) && std::isdigit(cb)) {
            size_t i0 = i, j0 = j;
            while (i0 < a.size() && a[i0] == '0') ++i0;
            while (j0 < b.size() && b[j0] == '0') ++j0;
            size_t i1 = i0, j1 = j0;
            while (i1 < a.size() && std::isdigit((unsigned char)a[i1])) ++i1;
            while (j1 < b.size() && std::isdigit((unsigned char)b[j1])) ++j1;
            const size_t la = i1 - i0, lb = j1 - j0;
            if (la != lb) return la < lb;
            const int c = a.compare(i0, la, b, j0, lb);
            if (c != 0) return c < 0;
            if (i1 - i != j1 - j) return i1 - i < j1 - j;   // equal value: fewer leading zeros first
            i = i1;
            j = j1;
            continue;
        }
        const int la = std::tolower(ca), lb = std::tolower(cb);
        if (la != lb) return la < lb;
        ++i;
        ++j;
    }
    if (a.size() - i != b.size() - j) return a.size() - i < b.size() - j;
    return a < b;   // deterministic tie-break on case
}

// ---- module state (single Explorer instance) ----
// Row styling comes from feature_tree.h's shared FM helpers (fmRowPill / fmRowPillEnd /
// fmTreeIconInArrowSlot) so Explorer rows share the FeatureManager pill, selection card and
// hover outline; the Explorer draws its own disclosure arrow, icon, label and metadata on top.

// Liquid glass v2 (host contract §H4): fetch (never paint) the left pane's ONE glass surface —
// layout.cpp's drawLeftPane already painted it this frame under "host_left_glass"; adaptivePanel
// is idempotent per frame (A2), so this is a cheap cached lookup regardless of the rect passed.
static const ui::AdaptiveSurface& explorerSurf() {
    ImVec2 wp_ = ImGui::GetWindowPos(); ImVec2 ws_ = ImGui::GetWindowSize();
    ImRect r(wp_, ImVec2(wp_.x + ws_.x, wp_.y + ws_.y));
    // Glass v2.1: the left pane is the FeatureManager group (per-group user overrides). All
    // "host_left_glass" call sites (layout.cpp, feature_tree.cpp) must pass the same group.
    return ui::adaptivePanel(ui::panelId("host_left_glass"), r, ui::GlassGroup::FeatureManager);
}
static std::string s_selectedPath;     // the currently-highlighted row (file or folder)
static std::string s_currentFolder;    // folder that New Folder / Paste act inside (a dir, or a
                                       // file's parent); defaults to the workspace root
static std::string s_renameId;         // path being inline-renamed ("" = none)
static char        s_renameBuf[260] = {0};
static bool        s_renameFocus = false;
static std::string s_pendingDelete;    // path queued for the delete-confirm modal ("" = none)
static std::string s_clipboardPath;    // Explorer copy/paste stash (a file or folder path)
static std::string s_pendingNewFolder; // reveal intent, retained until the inline rename is visible
// Expanded folders (normKey) and expanded revision-name groups ("\x01" + normKey(head)). The
// Explorer owns this set instead of ImGui tree storage so Collapse all / Reveal can edit it.
static std::unordered_set<std::string> s_expanded;
static std::string s_scrollTarget;     // normKey of a row to bring into view once it is drawn
static bool        s_scrollCenter = false;
static char        s_query[160] = {0}; // filter field; non-empty switches the tree to results
static bool        s_focusSearch = false;
static bool        s_recentOpen = false;
// Rows drawn in the tree/results this frame, in order; last frame's list drives Up/Down.
struct VisRow { std::string path; bool isDir = false; };
static std::vector<VisRow> s_rowsThisFrame, s_rowsLastFrame;
static std::string s_selKey;           // normKey(s_selectedPath), refreshed before drawing rows
static std::unordered_set<std::string> s_openKeys;   // normKey of every doc tab's saved path
static std::string s_activeKey;        // normKey of the active doc tab's saved path
static uint64_t    s_fsEpoch = 0;      // bumps on every listing invalidation (restarts search)
// Tiles view: browses ONE folder at a time (breadcrumb in the header line). s_viewPref is the
// session's explicit choice (-1 = automatic: tiles on the empty workspace home, tree otherwise).
static std::string s_tileDir;
static int         s_viewPref = -1;
static int         s_tileCols = 1;     // last frame's column count (Up/Down move by a row)
static bool        s_tileScrollTop = false;
// A CAD file opened from the Explorer: once its tab is active, the pane shows its FeatureManager.
static std::string s_pendingFmSwitch;

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
    std::vector<std::string> dirs;             // full paths, natural order
    std::vector<chudware::FsEntry> dirMeta;    // parallel to dirs
    std::vector<std::string> files;            // full paths, natural order
    std::vector<chudware::FsEntry> fileMeta;   // parallel to files
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
    const auto found = s_plm.byPath.find(relativeTo(project, path));
    return found == s_plm.byPath.end() ? nullptr : found->second;
}
static std::string statusRev(const ExplorerPlmItem* status) {
    if (!status) return {};
    return status->workingRev.empty() ? status->rev : status->workingRev;
}
static int changedCount() {
    if (!s_plm.ready) return 0;
    int count = 0;
    for (const auto& row : s_plm.items) if (!row.change.empty()) ++count;
    for (const auto& row : s_plm.untracked) if (!row.change.empty()) ++count;
    return count;
}

static std::map<std::string, std::shared_ptr<DirListCache>> s_dirListCache;
static std::unordered_set<std::string> s_visitedDirs;
static std::string s_workspaceError;

static void invalidateDirListCache(bool workspaceChanged = false) {
    s_dirListCache.clear();
    ++s_fsEpoch;
    chudware::fsBackend().invalidateBrowseCache(workspaceChanged);
}

// ---- known extensions ----------------------------------------------------
// .step+ is our native part container and .draw+ is a standalone editable drawing;
// the rest are import/interop formats (AutoCAD .dxf/.dwg and ACIS .sat/.sab included).
// ECAD/KiCad files open through the ECAD path. Lower-cased compare.
static bool isCadExt(const std::string& e) {
    return e == ".scad" || e == ".draw+" || e == ".step+" || e == ".step" || e == ".stp" ||
           e == ".iges" || e == ".igs" || e == ".stl" || e == ".3mf" ||
           e == ".dxf" || e == ".dwg" || e == ".sat" || e == ".sab";
}
static bool isEcadExt(const std::string& e) {
    return e == ".ecad+" || e == ".ecad" || e == ".kicad_sch" || e == ".kicad_pcb" ||
           e == ".kicad_pro";
}
static bool isRevengExt(const std::string& e) {
    return e == ".ply" || e == ".splat" || e == ".spz" || e == ".ksplat" ||
           e == ".mp4" || e == ".mov" || e == ".webm" || e == ".mkv" || e == ".avi";
}
static bool isKnownExt(const std::string& e) {
    return isCadExt(e) || isEcadExt(e) || isRevengExt(e);
}

// Lower-cased extension of a path, INCLUDING the leading dot.
static std::string lowerExt(const std::string& p) {
    return lowerCopy(pathExtension(pathFilename(p)));
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

// Per-extension atlas icon (contracts/icons.md inventory: part, file_* kinds, ws_drawing,
// ecad_pcb). Folders use "body_folder"; everything unknown is the deliberately plain page.
static const char* fileIconName(const std::string& ext) {
    if (ext == ".step+") return "part";
    if (ext == ".step" || ext == ".stp" || ext == ".iges" || ext == ".igs" ||
        ext == ".sat" || ext == ".sab") return "file_step";
    if (ext == ".stl" || ext == ".3mf" || ext == ".obj" || ext == ".ply") return "file_mesh";
    if (ext == ".splat" || ext == ".spz" || ext == ".ksplat") return "file_mesh";
    if (ext == ".draw+" || ext == ".dxf" || ext == ".dwg") return "ws_drawing";
    if (isEcadExt(ext)) return "ecad_pcb";
    if (ext == ".png" || ext == ".jpg" || ext == ".jpeg" || ext == ".webp" || ext == ".bmp" ||
        ext == ".gif" || ext == ".svg") return "file_image";
    if (ext == ".json" || ext == ".csv" || ext == ".xml" || ext == ".yaml" || ext == ".yml" ||
        ext == ".toml") return "file_data";
    if (ext == ".pdf" || ext == ".md" || ext == ".txt" || ext == ".rtf") return "file_doc";
    if (ext == ".py" || ext == ".cpp" || ext == ".hpp" || ext == ".c" || ext == ".h" ||
        ext == ".js" || ext == ".ts" || ext == ".rs" || ext == ".sh" || ext == ".lua")
        return "file_code";
    if (ext == ".zip" || ext == ".tar" || ext == ".gz" || ext == ".7z" || ext == ".xz")
        return "file_archive";
    return "file_generic";
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
    // Retire obsolete persisted host roots without stripping/reinterpreting them.
    // The current authenticated workspace has only a logical relative root.
    std::string relative, pathError;
    if (!chudware::workspaceRelativePath(st.workspaceDir, relative, pathError)) {
        st.workspaceDir = ".";
        st.settings.workspaceDir = ".";
    } else {
        st.workspaceDir = relative.empty() ? "." : relative;
    }
    s_workspaceError.clear();
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
        s_expanded.clear();
        s_scrollTarget.clear();
        s_query[0] = '\0';
        s_rowsLastFrame.clear();
        s_tileDir = st.workspaceDir;
        s_pendingFmSwitch.clear();
#ifndef __EMSCRIPTEN__
        // Desktop setup is idempotent once per workspace, not once per frame.
        fsb.mkdir(st.workspaceDir, s_workspaceError);
#endif
    }
}

// ---- expansion / reveal -----------------------------------------------------
static bool isExpanded(const std::string& dir) { return s_expanded.count(normKey(dir)) != 0; }
static void setExpanded(const std::string& dir, bool open) {
    if (open) s_expanded.insert(normKey(dir));
    else s_expanded.erase(normKey(dir));
}
// Expand every folder between the workspace root (exclusive: its children are top level) and
// `path` (inclusive when includeSelf).
static void expandAncestors(const std::string& root, const std::string& path, bool includeSelf) {
    const std::string rootKey = normKey(root);
    std::string p = includeSelf ? path : pathParent(path);
    while (!p.empty() && withinWorkspace(root, p)) {
        const std::string key = normKey(p);
        if (key == rootKey) break;
        s_expanded.insert(key);
        const std::string up = pathParent(p);
        if (up == p) break;
        p = up;
    }
}
// Leave search, open the folders above `path`, select it and scroll it into view.
static void revealPath(const std::string& root, const std::string& path, bool expandSelf) {
    s_query[0] = '\0';
    expandAncestors(root, path, expandSelf);
    s_selectedPath = path;
    s_currentFolder = expandSelf ? path : pathParent(path);
    s_tileDir = s_currentFolder;   // the tiles view shows the folder holding the target
    s_tileScrollTop = true;
    s_scrollTarget = normKey(path);
    s_scrollCenter = true;
}
static void selectPath(const std::string& path, bool isDir) {
    s_selectedPath = path;
    s_currentFolder = isDir ? path : pathParent(path);
    s_scrollTarget.clear();
}
static void beginRename(const std::string& root, const std::string& path) {
    s_pendingNewFolder.clear();
    revealPath(root, path, false);   // the inline editor lives in the tree, not the results
    s_renameId = path;
    std::snprintf(s_renameBuf, sizeof(s_renameBuf), "%s", pathFilename(path).c_str());
    s_renameFocus = true;
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
        // Keep a renamed folder (and anything open beneath it) expanded.
        const std::string oldKey = normKey(oldP), newKey = normKey(newP);
        std::vector<std::string> moved;
        for (auto it = s_expanded.begin(); it != s_expanded.end();) {
            if (withinWorkspace(oldKey, *it)) {
                moved.push_back(newKey + it->substr(oldKey.size()));
                it = s_expanded.erase(it);
            } else {
                ++it;
            }
        }
        for (auto& key : moved) s_expanded.insert(std::move(key));
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
    revealPath(root, dest, false);
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
        s_query[0] = '\0';                       // the inline name editor lives in the tree
        expandAncestors(root, cand, /*includeSelf=*/false);
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
// mid-tree-walk. A part/assembly opened here hands the pane to its FeatureManager once its tab is
// active (applyPendingFmSwitch); drawings and ECAD documents open in their own workspaces.
static void requestOpen(AppState& st, const std::string& p) {
    std::string e = lowerExt(p);
    if (!isKnownExt(e)) return;
    st.requestOpenPath = p;
    if (isCadExt(e) && e != ".draw+") s_pendingFmSwitch = normKey(p);
}

static void applyPendingFmSwitch(AppState& st) {
    if (s_pendingFmSwitch.empty() || st.activeTab < 0 || st.activeTab >= (int)st.docTabs.size())
        return;
    const AppState::DocTab& tab = st.docTabs[st.activeTab];
    if (tab.path.empty() || normKey(tab.path) != s_pendingFmSwitch) return;
    s_pendingFmSwitch.clear();
    using Kind = AppState::DocTab::TabKind;
    if (st.activeWorkspace == AppState::Workspace::Model &&
        (tab.kind == Kind::Part || tab.kind == Kind::Assembly))
        st.leftPaneTab = kLeftPaneFeatureManager;
}

static kit::Tone changeTone(const std::string& code) {
    if (code == "D") return kit::Tone::Err;
    if (code == "M") return kit::Tone::Warn;
    if (code == "A") return kit::Tone::Accent;
    return kit::Tone::Neutral;
}

static const char* changeName(const std::string& code) {
    if (code == "M") return "Modified";
    if (code == "A") return "Added";
    if (code == "D") return "Deleted";
    if (code == "U") return "Untracked";
    return "";
}
// Tooltip wording: what the change mark means, relative to the vault.
static const char* changeDetail(const std::string& code) {
    if (code == "M") return "Modified since the last checkpoint";
    if (code == "A") return "Added since the last checkpoint";
    if (code == "D") return "Deleted since the last checkpoint";
    if (code == "U") return "Not in the vault yet";
    return "";
}

// ---- row painter -------------------------------------------------------------
// One Explorer row: FM pill + full-width hit target, then (left to right) indent, disclosure
// arrow (folders/groups) or open-in-tab dot (files), type icon, elided name, an optional dim
// suffix (search results' folder), and right-aligned vault metadata (rev + change code).
// Native documents draw as taller CARD rows instead: a rendered preview (file_thumbs.h) in place
// of the icon, the name without its extension, and a dim second line (kind · folder · age).
// The row's InvisibleButton stays the last item so callers attach drag/context/tooltip to it.
struct RowGeom { float arrowX = 0, iconX = 0, labelX = 0; };
static RowGeom rowGeom(float x0, int depth) {
    const float S = ui::uiScale, fs = ImGui::GetFontSize();
    RowGeom g;
    g.arrowX = x0 + 6.0f * S + (float)depth * fs;
    g.iconX = g.arrowX + fs * 0.85f;
    g.labelX = g.iconX + std::min(16.0f * S, fs) + 5.0f * S;
    return g;
}
struct RowView {
    int depth = 0;
    bool arrow = false, expanded = false;
    const char* icon = "file_generic";
    const char* fallback = "[F]";
    float iconAlpha = 1.0f;
    const char* label = "";
    const char* suffix = nullptr;      // compact rows: dim text after the name
    bool dim = false, accent = false, openDot = false, selected = false;
    const float* tint = nullptr;
    const ExplorerPlmItem* status = nullptr;
    const FileThumb* thumb = nullptr;  // non-null: a document card row
    const char* subtitle = nullptr;    // card rows: the dim second line
};
struct RowHit { bool clicked = false, doubleClicked = false; };

static float compactRowH() { return ImGui::GetFontSize() + 8.0f * ui::uiScale; }   // fmRowPill frame
static float cardRowH() {
    return std::max(ImGui::GetFontSize() * 2.0f + 14.0f * ui::uiScale, 40.0f * ui::uiScale);
}

// Preserve layout, keyboard navigation and stripe parity without building offscreen
// widgets. Editors, selection/context menus, reveal targets and drags stay live.
static bool skipClippedRow(const std::string& path, bool isDir, bool navigable = true,
                           float height = 0.0f) {
    if (height <= 0.0f) height = compactRowH();
    if (ImGui::IsRectVisible(ImVec2(ImGui::GetContentRegionAvail().x, height))) return false;
    const std::string key = normKey(path);
    if (path == s_renameId || path == s_pendingNewFolder || key == s_selKey ||
        key == s_scrollTarget || ImGui::GetCurrentContext()->DragDropActive) return false;
    ImGui::Dummy(ImVec2(0.0f, height));
    ui::fmRowStripeSkip(1);
    if (navigable) s_rowsThisFrame.push_back({path, isDir});
    return true;
}

static ImU32 inkWithAlpha(const ImVec4& c, float a) {
    return ImGui::ColorConvertFloat4ToU32(ImVec4(c.x, c.y, c.z, c.w * a));
}

// A document preview well: the rendered thumbnail when ready, else the kind icon on a soft tile.
static void drawPreviewBox(ImDrawList* dl, ImVec2 p0, ImVec2 p1, const FileThumb* thumb,
                           const char* icon, float iconAlpha, float roundingPx) {
    const ui::AdaptiveSurface& surf = explorerSurf();
    const float S = ui::uiScale;
    const float stroke = std::max(1.0f, 1.0f * S);
    if (thumb && thumb->tex) {
        dl->AddImageRounded((ImTextureID)(intptr_t)thumb->tex, p0, p1, ImVec2(0, 0), ImVec2(1, 1),
                            IM_COL32(255, 255, 255, (int)(255.0f * iconAlpha)), roundingPx);
        dl->AddRect(p0, p1, inkWithAlpha(surf.textPrimary, 0.16f), roundingPx, 0, stroke);
        return;
    }
    dl->AddRectFilled(p0, p1, inkWithAlpha(surf.textPrimary, 0.07f), roundingPx);
    dl->AddRect(p0, p1, inkWithAlpha(surf.textPrimary, 0.12f), roundingPx, 0, stroke);
    const float side = std::min(p1.x - p0.x, p1.y - p0.y);
    const float px = std::min(side * 0.5f, 40.0f * S);
    const ImVec2 c((p0.x + p1.x) * 0.5f, (p0.y + p1.y) * 0.5f);
    const IconUV iv = getIcon(icon, px > 20.0f ? 32 : 20);
    const bool pending = thumb && thumb->state == FileThumb::State::Pending;
    const float a = iconAlpha * (pending ? 0.45f : 0.85f);
    if (iv.valid)
        dl->AddImage(iv.texId, ImVec2(c.x - px * 0.5f, c.y - px * 0.5f),
                     ImVec2(c.x + px * 0.5f, c.y + px * 0.5f), iv.uv0, iv.uv1,
                     IM_COL32(255, 255, 255, (int)(a * 255.0f)));
}

// Vault metadata at the right edge of a text line ending at `right`: a small tone dot for an
// uncheckpointed change (outermost; the tooltip and the Changes tab spell it out), the revision
// to its left. Returns the new right edge.
static float drawVaultMeta(ImDrawList* dl, const ExplorerPlmItem* status, float right, float textY,
                           float minX) {
    if (!status) return right;
    const ui::AdaptiveSurface& surf = explorerSurf();
    const float S = ui::uiScale, fs = ImGui::GetFontSize(), gap = 5.0f * S;
    const std::string& code = status->change;
    if (!code.empty()) {
        const float r = std::max(2.5f * S, fs * 0.2f);
        dl->AddCircleFilled(ImVec2(right - r, textY + fs * 0.5f), r,
                            ImGui::ColorConvertFloat4ToU32(kit::toneOn(changeTone(code), surf)));
        right -= r * 2.0f + gap;
    }
    const std::string rev = statusRev(status);
    if (!rev.empty()) {
        const std::string revText = "rev " + rev;
        const float w = ImGui::CalcTextSize(revText.c_str()).x;
        if (right - w > minX + fs * 3.0f) {
            dl->AddText(ImVec2(right - w, textY), ImGui::ColorConvertFloat4ToU32(surf.textSecondary),
                        revText.c_str());
            right -= w + gap;
        }
    }
    return right;
}

static RowHit drawRow(const char* id, const RowView& v) {
    RowHit hit;
    const ui::AdaptiveSurface& surf = explorerSurf();
    const float S = ui::uiScale;
    const bool card = v.thumb != nullptr;
    ui::FmRowRect rr = ui::fmRowPill(v.selected, v.tint, card ? cardRowH() : 0.0f);
    const float rowH = rr.mx.y - rr.mn.y;
    ImGui::InvisibleButton(id, ImVec2(std::max(rr.mx.x - rr.mn.x, ui::metrics::stroke(1.0f)), rowH));
    hit.clicked = ImGui::IsItemClicked(ImGuiMouseButton_Left);
    hit.doubleClicked = hit.clicked && ImGui::IsMouseDoubleClicked(ImGuiMouseButton_Left);
    RowGeom g = rowGeom(rr.mn.x, v.depth);
    ImDrawList* dl = ImGui::GetWindowDrawList();
    if (card) {
        const float inset = 4.0f * S, side = rowH - inset * 2.0f;
        const ImVec2 t0(g.iconX - 2.0f * S, rr.mn.y + inset);
        drawPreviewBox(dl, t0, ImVec2(t0.x + side, t0.y + side), v.thumb, v.icon, v.iconAlpha,
                       4.0f * S);
        g.labelX = t0.x + side + 8.0f * S;
    } else {
        ui::fmTreeIconInArrowSlot(ImVec2(g.iconX - ImGui::GetStyle().FramePadding.x, rr.mn.y),
                                  v.icon, v.fallback, v.iconAlpha);
    }
    ui::fmRowPillEnd(rr, v.selected);

    const float fs = ImGui::GetFontSize();
    const float midY = (rr.mn.y + rr.mx.y) * 0.5f;
    // Card rows: name above centre, subtitle below; compact rows: one centred line.
    const float textY = card ? midY - fs - 1.0f * S : midY - fs * 0.5f;
    const ImU32 inkPrimary = ImGui::ColorConvertFloat4ToU32(surf.textPrimary);
    const ImU32 inkDim = ImGui::ColorConvertFloat4ToU32(surf.textSecondary);
    const ImU32 inkAccent = ImGui::ColorConvertFloat4ToU32(kit::toneOn(kit::Tone::Accent, surf));
    const float slotCx = (g.arrowX + g.iconX) * 0.5f;
    if (v.arrow) {
        const float sc = 0.7f;
        ImGui::RenderArrow(dl, ImVec2(slotCx - fs * 0.5f, midY - fs * 0.5f * sc), inkDim,
                           v.expanded ? ImGuiDir_Down : ImGuiDir_Right, sc);
    } else if (v.openDot) {
        dl->AddCircleFilled(ImVec2(slotCx, midY), fs * 0.17f, inkAccent);
    }

    const float gap = 4.0f * S;
    const float right = drawVaultMeta(dl, v.status, rr.mx.x - 6.0f * S, textY, g.labelX);
    const std::string name = kit::elide(v.label, std::max(right - g.labelX, fs));
    dl->AddText(ImVec2(g.labelX, textY), v.accent ? inkAccent : (v.dim ? inkDim : inkPrimary),
                name.c_str());
    if (card) {
        if (v.subtitle && v.subtitle[0]) {
            const float maxW = rr.mx.x - 6.0f * S - g.labelX;
            dl->AddText(ImVec2(g.labelX, midY + 1.0f * S), inkDim,
                        kit::elide(v.subtitle, std::max(maxW, fs)).c_str());
        }
    } else if (v.suffix && v.suffix[0]) {
        const float sx = g.labelX + ImGui::CalcTextSize(name.c_str()).x + gap * 1.5f;
        if (right - sx > fs * 2.0f)
            dl->AddText(ImVec2(sx, textY), inkDim, kit::elide(v.suffix, right - sx).c_str());
    }
    return hit;
}

// Inline rename editor occupying a row (same pill and icon slot as the row it replaces).
static void drawRenameRow(AppState& st, const std::string& root, const std::string& path,
                          int depth, const char* icon, const char* fallback) {
    const bool selected = normKey(path) == s_selKey;
    ui::FmRowRect rr = ui::fmRowPill(selected);
    const RowGeom g = rowGeom(rr.mn.x, depth);
    ImGui::SetCursorScreenPos(ImVec2(g.labelX - ImGui::GetStyle().FramePadding.x, rr.mn.y));
    ImGui::SetNextItemWidth(std::max(rr.mx.x - ImGui::GetCursorScreenPos().x - 4.0f * ui::uiScale,
                                     ImGui::GetFontSize() * 4.0f));
    const bool revealing = (s_pendingNewFolder == path);
    if (s_renameFocus) ImGui::SetKeyboardFocusHere();
    if (ImGui::InputText("##rename", s_renameBuf, sizeof(s_renameBuf),
                         ImGuiInputTextFlags_EnterReturnsTrue | ImGuiInputTextFlags_AutoSelectAll))
        commitRename(st, root);
    if (ImGui::IsItemDeactivated()) commitRename(st, root);   // click-away commits; Esc cancels above
    ui::fmTreeIconInArrowSlot(ImVec2(g.iconX - ImGui::GetStyle().FramePadding.x, rr.mn.y),
                              icon, fallback);
    if (revealing) {
        ImGui::ScrollToItem(ImGuiScrollFlags_KeepVisibleCenterY);
        // A clipped row needs another frame after scrolling before reveal is consumed.
        if (ImGui::IsItemVisible()) {
            s_pendingNewFolder.clear();
            s_renameFocus = false;
        }
    } else {
        s_renameFocus = false;
    }
    ui::fmRowPillEnd(rr, selected);
}

// Selected rows keyboard-moved off screen, and reveal targets, scroll into view when drawn.
static void scrollIfTarget(const std::string& path, const std::string& key) {
    if (s_scrollTarget.empty() || key != s_scrollTarget) return;
    s_selectedPath = path;
    s_selKey = key;
    ImGui::ScrollToItem(s_scrollCenter ? ImGuiScrollFlags_KeepVisibleCenterY
                                       : ImGuiScrollFlags_KeepVisibleEdgeY);
    s_scrollTarget.clear();
    s_scrollCenter = false;
}

// ---- tooltips -----------------------------------------------------------------
static std::string fmtWhen(int64_t unixSeconds) {
    if (unixSeconds <= 0) return {};
    const std::time_t t = (std::time_t)unixSeconds;
    char buf[32] = {0};
    if (const std::tm* lt = std::localtime(&t)) std::strftime(buf, sizeof(buf), "%Y-%m-%d %H:%M", lt);
    return kit::fmtAgo((double)unixSeconds) + (buf[0] ? std::string(" (") + buf + ")" : std::string());
}
// `preview` (a ready document thumbnail) heads the tooltip with a large picture.
static void rowTooltip(const std::string& path, bool isDir, const chudware::FsEntry* meta,
                       const ExplorerPlmItem* status, const char* note = nullptr,
                       const FileThumb* preview = nullptr) {
    if (!ImGui::IsItemHovered(ImGuiHoveredFlags_ForTooltip)) return;
    std::string tip = pathFilename(path) + "\n" + path;
    if (meta) {
        std::string line;
        if (!isDir) line = kit::fmtBytes(meta->size);
        const std::string when = fmtWhen(meta->mtime);
        if (!when.empty()) line += (line.empty() ? "Modified " : " \xC2\xB7 Modified ") + when;
        if (!line.empty()) tip += "\n" + line;
    }
    if (status) {
        std::string line;
        if (!status->change.empty()) line = changeDetail(status->change);
        const std::string rev = statusRev(status);
        if (!rev.empty()) line += (line.empty() ? "" : " \xC2\xB7 ") + std::string("Revision ") + rev;
        if (!status->lifecycle.empty()) line += (line.empty() ? "" : " \xC2\xB7 ") + status->lifecycle;
        if (!line.empty()) tip += "\n" + line;
    }
    if (!isDir) {
        const std::string key = normKey(path);
        if (key == s_activeKey) tip += "\nActive document";
        else if (s_openKeys.count(key)) tip += "\nOpen in a tab";
        if (!isKnownExt(lowerExt(path))) tip += "\nNot a CAD/ECAD document";
    }
    if (note && note[0]) tip += std::string("\n") + note;
    if (preview && preview->tex) {
        ImGui::BeginTooltip();
        const float side = 168.0f * ui::uiScale;
        const ImVec2 p0 = ImGui::GetCursorScreenPos();
        ImGui::Dummy(ImVec2(side, side));
        ImGui::GetWindowDrawList()->AddImageRounded((ImTextureID)(intptr_t)preview->tex, p0,
                                                    ImVec2(p0.x + side, p0.y + side), ImVec2(0, 0),
                                                    ImVec2(1, 1), IM_COL32_WHITE, 6.0f * ui::uiScale);
        ImGui::TextUnformatted(tip.c_str());
        ImGui::EndTooltip();
        return;
    }
    ImGui::SetTooltip("%s", tip.c_str());
}

// ---- context menus ------------------------------------------------------------
static void pathMenuItems(const std::string& path) {
    if (ImGui::MenuItem("Copy path")) ImGui::SetClipboardText(path.c_str());
    // Desktop only: the browser build has no OS file manager. The helper backgrounds the
    // launcher, so the GL thread never waits on it.
    if (haveOsFileManager() && ImGui::MenuItem("Show in file manager"))
        revealInFileManager(path);
}
static void setWorkingFolder(AppState& st, const std::string& dir) {
    // An explicit working-folder choice; keep the user in the workspace they are browsing from.
    const AppState::Workspace keep = st.activeWorkspace;
    setActiveProjectDir(st, dir);
    st.activeWorkspace = keep;
}
static void rootMenuItems(AppState& st, const std::string& root) {
    s_currentFolder = root;
    if (ImGui::MenuItem("New Folder")) createNewFolder(st, root, root);
    if (ImGui::MenuItem("Paste", "Ctrl+V", false, !s_clipboardPath.empty())) doPaste(st, root, root);
    if (ImGui::MenuItem("Refresh")) {
        invalidateDirListCache();
        invalidateExplorerPlm();
    }
    ImGui::Separator();
    pathMenuItems(root);
}
static void fileContextMenu(AppState& st, const std::string& root, const std::string& p,
                            const std::string& ext, const ExplorerPlmItem* status) {
    ui::pushContextMenuStyle();
    if (ImGui::BeginPopupContextItem("##fctx")) {
        selectPath(p, false);
        if (ImGui::MenuItem("Open", "Enter", false, isKnownExt(ext))) requestOpen(st, p);
        if (status && !status->itemId.empty()) {
            if (ImGui::MenuItem("History"))
                openPlm(st, PlmPage::Files, status->itemId);
            if (ImGui::MenuItem("Release in PLM"))
                openPlm(st, PlmPage::Releases, status->itemId);
        }
        ImGui::Separator();
        pathMenuItems(p);
        ImGui::Separator();
        if (ImGui::MenuItem("Rename", "F2")) beginRename(root, p);
        if (ImGui::MenuItem("Copy", "Ctrl+C")) s_clipboardPath = p;
        if (ImGui::MenuItem("Paste", "Ctrl+V", false, !s_clipboardPath.empty()))
            doPaste(st, root, pathParent(p));
        if (ImGui::MenuItem("Delete", "Del")) s_pendingDelete = p;
        ImGui::EndPopup();
    }
    ui::popContextMenuStyle();
}
static void folderContextMenu(AppState& st, const std::string& root, const std::string& dir) {
    ui::pushContextMenuStyle();
    if (ImGui::BeginPopupContextItem("##dctx")) {
        selectPath(dir, true);
        if (ImGui::MenuItem("New Folder")) createNewFolder(st, root, dir);
        if (ImGui::MenuItem("Set as working folder")) setWorkingFolder(st, dir);
        ImGui::Separator();
        pathMenuItems(dir);
        ImGui::Separator();
        if (ImGui::MenuItem("Rename", "F2")) beginRename(root, dir);
        if (ImGui::MenuItem("Copy", "Ctrl+C")) s_clipboardPath = dir;
        if (ImGui::MenuItem("Paste", "Ctrl+V", false, !s_clipboardPath.empty()))
            doPaste(st, root, dir);
        if (ImGui::MenuItem("Delete", "Del")) s_pendingDelete = dir;
        ImGui::EndPopup();
    }
    ui::popContextMenuStyle();
}

// ---- file / folder rows ---------------------------------------------------------
static const char* nativeKindLabel(NativeDocKind kind) {
    if (kind == NativeDocKind::Assembly) return "Assembly";
    if (kind == NativeDocKind::Part) return "Part";
    return nullptr;
}
// Tile-width age: "now", "12m", "3h", "5d", "6w", "4mo", "2y".
static std::string compactAge(int64_t mtime) {
    const double s = std::difftime(std::time(nullptr), (std::time_t)mtime);
    if (s < 60.0) return "now";
    const struct { double unit; const char* suffix; } steps[] = {
        {365.0 * 86400.0, "y"}, {30.0 * 86400.0, "mo"}, {7.0 * 86400.0, "w"},
        {86400.0, "d"}, {3600.0, "h"}, {60.0, "m"}};
    for (const auto& step : steps)
        if (s >= step.unit) return std::to_string((long long)(s / step.unit)) + step.suffix;
    return "now";
}
// "Assembly · parts · 3 h ago": kind (once known), the search hit's folder, the file's age
// (`compact`: tiles use the short age).
static std::string documentSubtitle(const FileThumb& thumb, const char* folder, int64_t mtime,
                                    bool compact = false) {
    std::string out;
    auto add = [&out](const std::string& part) {
        if (part.empty()) return;
        if (!out.empty()) out += " \xC2\xB7 ";
        out += part;
    };
    if (const char* kind = nativeKindLabel(thumb.kind)) add(kind);
    if (folder) add(folder);
    if (mtime > 0) add(compact ? compactAge(mtime) : kit::fmtAgo((double)mtime));
    return out;
}
// Native documents are listed by name: their kind is on the subtitle, so ".step+" is noise.
static std::string documentName(const std::string& fname) { return pathStem(fname); }

// A file row: single click selects, double click / Enter opens, drag drops into the viewport,
// right-click for the menu. `suffix` is the dim folder shown beside search results. Native
// documents (with listing metadata) draw as preview cards.
static void drawFileRow(AppState& st, const std::string& root, const std::string& p,
                        const chudware::FsEntry* meta, int depth, const char* suffix = nullptr) {
    const std::string ext = lowerExt(p);
    const bool native = meta && fileThumbSupported(ext);
    if (skipClippedRow(p, false, true, native ? cardRowH() : 0.0f)) return;
    const std::string key = normKey(p);
    const std::string fname = pathFilename(p);
    ImGui::PushID(p.c_str());
    if (s_renameId == p) {
        drawRenameRow(st, root, p, depth, fileIconName(ext), fileIconFallback(ext));
        s_rowsThisFrame.push_back({p, false});
        ImGui::PopID();
        return;
    }
    const ExplorerPlmItem* status = statusForPath(p);
    const bool active = !s_activeKey.empty() && key == s_activeKey;
    FileThumb thumb;
    std::string label = fname, subtitle;
    if (native) {
        thumb = fileThumb(p, meta->mtime, meta->size);
        label = documentName(fname);
        subtitle = documentSubtitle(thumb, suffix, meta->mtime);
    }
    RowView v;
    v.depth = depth;
    v.icon = native && thumb.kind == NativeDocKind::Assembly ? "assembly" : fileIconName(ext);
    v.fallback = fileIconFallback(ext);
    v.label = label.c_str();
    v.suffix = suffix;
    v.dim = !isKnownExt(ext);
    v.iconAlpha = v.dim ? 0.6f : 1.0f;
    v.accent = active;
    v.openDot = active || s_openKeys.count(key) != 0;
    v.tint = active ? ui::palette.accent : nullptr;
    v.selected = key == s_selKey;
    v.status = status;
    if (native) {
        v.thumb = &thumb;
        v.subtitle = subtitle.c_str();
    }
    const RowHit hit = drawRow("##file", v);
    s_rowsThisFrame.push_back({p, false});
    scrollIfTarget(p, key);
    if (hit.clicked) selectPath(p, false);
    if (hit.doubleClicked) requestOpen(st, p);
    // Drag model files to the viewport, or JSON scenarios to simulation setup.
    // The absolute path includes its NUL terminator; each target validates
    // the formats it accepts. Drag preview = the filename.
    if ((isKnownExt(ext) || ext == ".json") &&
        ImGui::BeginDragDropSource(ImGuiDragDropFlags_SourceAllowNullID)) {
        ImGui::SetDragDropPayload(kFileDragPayload, p.c_str(), p.size() + 1);
        ImGui::TextUnformatted(fname.c_str());
        ImGui::EndDragDropSource();
    }
    fileContextMenu(st, root, p, ext, status);
    rowTooltip(p, false, meta, status, nullptr, native ? &thumb : nullptr);
    ImGui::PopID();
}

// Only explicit suffixes count as legacy copies: generic trailing numbers often
// identify distinct model sizes, not revisions. This groups siblings visually,
// without renaming, deleting, or treating them as revisions of one vault item.
static std::string legacySeriesName(const std::string& path) {
    const std::string name = pathFilename(path), ext = pathExtension(name);
    if (!isKnownExt(lowerExt(path))) return {};
    const std::string stem = pathStem(name);
    const std::string low = lowerCopy(stem);
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
        if (version) return lowerCopy(low.substr(0, at) + ext);
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
        const auto found = groups.find(lowerCopy(pathFilename(files[i])));
        if (found != groups.end()) found->second.insert(found->second.begin(), i);
    }
    for (auto& [key, members] : groups) {
        if (members.size() < 2) continue;
        LegacyFileGroup group;
        group.members = std::move(members);
        group.head = *std::min_element(group.members.begin(), group.members.end());
        const std::string firstName = pathFilename(files[group.members.front()]);
        const std::string ext = pathExtension(firstName);
        group.title = legacySeriesName(files[group.members.front()]).empty()
            ? firstName : pathStem(firstName).substr(0, key.size() - ext.size()) + ext;
        for (const size_t index : group.members)
            rows.fileGroup[index] = static_cast<int>(rows.legacyGroups.size());
        rows.legacyGroups.push_back(std::move(group));
    }
}

// Files of one listing; explicit revision-named siblings collapse under one compact group row
// ("name  · N revision copies") whose members stay individually openable rows.
static void drawDirectoryFiles(AppState& st, const std::string& root, const DirListCache& rows,
                               int depth) {
    for (size_t i = 0; i < rows.files.size(); ++i) {
        const int groupIndex = rows.fileGroup[i];
        if (groupIndex < 0) {
            drawFileRow(st, root, rows.files[i], &rows.fileMeta[i], depth);
            continue;
        }
        const LegacyFileGroup& group = rows.legacyGroups[static_cast<size_t>(groupIndex)];
        if (i != group.head) continue;
        const std::string& headPath = rows.files[group.head];
        const std::string groupKey = "\x01" + normKey(headPath);
        if (!s_scrollTarget.empty())   // revealing a member opens its group
            for (const size_t index : group.members)
                if (normKey(rows.files[index]) == s_scrollTarget) s_expanded.insert(groupKey);
        bool open = s_expanded.count(groupKey) != 0;
        if (!skipClippedRow(headPath, false, false)) {
            const std::string ext = lowerExt(headPath);
            const std::string count =
                "\xC2\xB7 " + std::to_string(group.members.size()) + " revision copies";
            ImGui::PushID(groupKey.c_str());
            RowView v;
            v.depth = depth;
            v.arrow = true;
            v.expanded = open;
            v.icon = fileIconName(ext);
            v.fallback = fileIconFallback(ext);
            v.label = group.title.c_str();
            v.suffix = count.c_str();
            const RowHit hit = drawRow("##group", v);
            if (hit.clicked && !hit.doubleClicked) {
                open = !open;
                if (open) s_expanded.insert(groupKey);
                else s_expanded.erase(groupKey);
            }
            if (ImGui::IsItemHovered(ImGuiHoveredFlags_ForTooltip))
                ImGui::SetTooltip("Revision-named copies: separate files that may be separate PLM "
                                  "items.\nNothing has been merged or removed.");
            ImGui::PopID();
        }
        if (open)
            for (const size_t index : group.members)
                drawFileRow(st, root, rows.files[index], &rows.fileMeta[index], depth + 1);
    }
}

// Cached, filtered, naturally-sorted rows for one directory (folders first, then files).
// Dot entries are hidden; every other file is listed (non-documents render dimmed).
static std::shared_ptr<DirListCache> dirRows(const std::string& dir) {
    s_visitedDirs.insert(dir);
    const auto& snapshot = chudware::fsBackend().pollDirectory(dir);
    auto& cached = s_dirListCache[dir];
    if (cached && cached->revision == snapshot.revision && cached->state == snapshot.state &&
        cached->error == snapshot.error)
        return cached;
    auto fresh = std::make_shared<DirListCache>();
    fresh->revision = snapshot.revision;
    fresh->state = snapshot.state;
    fresh->error = snapshot.error;
    std::vector<const chudware::FsEntry*> dirs, files;
    for (const auto& e : snapshot.entries) {
        if (e.name.empty() || e.name[0] == '.') continue;
        (e.isDir ? dirs : files).push_back(&e);
    }
    auto byName = [](const chudware::FsEntry* a, const chudware::FsEntry* b) {
        return naturalLess(a->name, b->name);
    };
    std::sort(dirs.begin(), dirs.end(), byName);
    std::sort(files.begin(), files.end(), byName);
    for (const auto* e : dirs) {
        fresh->dirs.push_back(pathJoin(dir, e->name));
        fresh->dirMeta.push_back(*e);
    }
    for (const auto* e : files) {
        fresh->files.push_back(pathJoin(dir, e->name));
        fresh->fileMeta.push_back(*e);
    }
    groupLegacyFiles(*fresh);
    cached = std::move(fresh);
    return cached;
}

static void dimLine(int depth, const char* text) {
    const float off = rowGeom(0.0f, depth).labelX;
    ImGui::Indent(off);
    kit::dimText(text);
    ImGui::Unindent(off);
}

static void drawDirChildren(AppState& st, const std::string& root, const std::string& dir,
                            int depth);

// A folder row: clicking anywhere on it selects and toggles it.
static void drawFolderRow(AppState& st, const std::string& root, const std::string& dir,
                          const chudware::FsEntry* meta, int depth) {
    if (s_pendingNewFolder == dir && s_renameId != dir) {
        s_renameId = dir;
        std::snprintf(s_renameBuf, sizeof(s_renameBuf), "%s", pathFilename(dir).c_str());
        s_renameFocus = true;
        s_selectedPath = dir;
        s_selKey = normKey(dir);
    }
    if (skipClippedRow(dir, true)) {
        if (isExpanded(dir)) drawDirChildren(st, root, dir, depth + 1);
        return;
    }
    ImGui::PushID(dir.c_str());
    if (s_renameId == dir) {
        // Children stay hidden during a folder rename (it is a leaf interaction).
        drawRenameRow(st, root, dir, depth, "body_folder", "[D]");
        s_rowsThisFrame.push_back({dir, true});
        ImGui::PopID();
        return;
    }
    const std::string key = normKey(dir);
    bool open = s_expanded.count(key) != 0;
    RowView v;
    v.depth = depth;
    v.arrow = true;
    v.expanded = open;
    v.icon = "body_folder";
    v.fallback = "[D]";
    const std::string name = pathFilename(dir);
    v.label = name.c_str();
    v.selected = key == s_selKey;
    const RowHit hit = drawRow("##dir", v);
    s_rowsThisFrame.push_back({dir, true});
    scrollIfTarget(dir, key);
    if (hit.clicked) {
        selectPath(dir, true);
        if (!hit.doubleClicked) {   // the second click of a double-click must not undo the first
            open = !open;
            setExpanded(dir, open);
        }
    }
    folderContextMenu(st, root, dir);
    rowTooltip(dir, true, meta, nullptr);
    ImGui::PopID();
    if (open) drawDirChildren(st, root, dir, depth + 1);
}

static void drawDirChildren(AppState& st, const std::string& root, const std::string& dir,
                            int depth) {
    const auto rows = dirRows(dir);   // shared copy: a mutation mid-walk cannot free these rows
    for (size_t i = 0; i < rows->dirs.size(); ++i)
        drawFolderRow(st, root, rows->dirs[i], &rows->dirMeta[i], depth);
    drawDirectoryFiles(st, root, *rows, depth);
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
    const bool empty = rows->dirs.empty() && rows->files.empty();
    if (rows->state == chudware::FsReadState::Error) {
        const std::string msg = "Unable to read folder: " + rows->error;
        if (depth == 0) kit::blockedState("Folder unreadable", rows->error.c_str(), "Refresh to retry.");
        else dimLine(depth, msg.c_str());
    } else if (rows->state == chudware::FsReadState::Missing) {
        if (depth == 0) kit::blockedState("Folder missing", dir.c_str(), "Choose another working folder.");
        else dimLine(depth, "Folder missing");
    } else if (rows->state == chudware::FsReadState::Loading && empty) {
        if (depth == 0) kit::spinner("Loading\xE2\x80\xA6");
        else dimLine(depth, "Loading\xE2\x80\xA6");
    } else if (empty) {
        if (depth == 0)
            kit::emptyState("body_folder", "Empty folder",
                            "Save a part here, or right-click to create a folder.");
        else dimLine(depth, "Empty");
    }
}

// ---- search -------------------------------------------------------------------
// Incremental BFS over async directory snapshots, with at most ~2 ms of work per
// frame (including entry matching). Pending folders rotate to the queue's back.
// Dot folders are skipped; the walk stops after kSearchMaxEntries entries or kSearchMaxDepth.
struct SearchHit {
    std::string path, folder;   // folder: workspace-relative parent ("" at the top level)
    bool isDir = false;
    chudware::FsEntry meta;
};
struct SearchState {
    std::string query, root;
    uint64_t epoch = 0;
    std::deque<std::pair<std::string, int>> queue;
    std::vector<SearchHit> hits;
    std::shared_ptr<DirListCache> activeRows;
    std::string activeDir;
    size_t activeIndex = 0;
    int activeDepth = 0;
    size_t scanned = 0, extraHits = 0;
    bool capped = false, done = true;
};
static SearchState s_search;
static constexpr size_t kSearchMaxEntries = 4000;
static constexpr int kSearchMaxDepth = 12;
static constexpr size_t kSearchMaxHits = 500;

static std::string trimmedQuery() {
    std::string q = s_query;
    while (!q.empty() && q.front() == ' ') q.erase(q.begin());
    while (!q.empty() && q.back() == ' ') q.pop_back();
    return lowerCopy(std::move(q));
}
// Every space-separated token must occur in the name (or in the relative path, for a token
// containing '/').
static bool searchMatches(const std::string& query, const std::string& name, const std::string& rel) {
    const std::string lname = lowerCopy(name), lrel = lowerCopy(rel);
    size_t at = 0;
    while (at < query.size()) {
        size_t end = query.find(' ', at);
        if (end == std::string::npos) end = query.size();
        if (end > at) {
            const std::string token = query.substr(at, end - at);
            const std::string& hay = token.find('/') != std::string::npos ? lrel : lname;
            if (hay.find(token) == std::string::npos) return false;
        }
        at = end + 1;
    }
    return true;
}
static void stepSearch(const std::string& root, const std::string& query) {
    auto& S = s_search;
    if (query != S.query || root != S.root || S.epoch != s_fsEpoch) {
        S = SearchState{};
        S.query = query;
        S.root = root;
        S.epoch = s_fsEpoch;
        S.done = query.empty();
        if (!S.done) S.queue.emplace_back(root, 0);
    }
    if (S.done) return;
    auto& fsb = chudware::fsBackend();
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(2);
    size_t rotated = 0;
    while (std::chrono::steady_clock::now() < deadline) {
        if (!S.activeRows) {
            if (S.queue.empty()) break;
            auto [dir, depth] = std::move(S.queue.front());
            S.queue.pop_front();
            const auto& snap = fsb.pollDirectory(dir);
            if (snap.state == chudware::FsReadState::Loading) {
                S.queue.emplace_back(std::move(dir), depth);
                if (++rotated >= S.queue.size()) break;
                continue;
            }
            if (snap.state != chudware::FsReadState::Ready) continue;
            S.activeRows = dirRows(dir);
            S.activeDir = std::move(dir);
            S.activeDepth = depth;
            S.activeIndex = 0;
            rotated = 0;
        }
        s_visitedDirs.insert(S.activeDir);
        const auto& rows = *S.activeRows;
        if (S.activeIndex == rows.dirs.size() + rows.files.size()) {
            S.activeRows.reset();
            continue;
        }
        if (++S.scanned > kSearchMaxEntries) {
            S.capped = true;
            S.queue.clear();
            S.activeRows.reset();
            break;
        }
        const bool isDir = S.activeIndex < rows.dirs.size();
        const size_t index = isDir ? S.activeIndex : S.activeIndex - rows.dirs.size();
        const auto& full = isDir ? rows.dirs[index] : rows.files[index];
        const auto& entry = isDir ? rows.dirMeta[index] : rows.fileMeta[index];
        ++S.activeIndex;
        const std::string folder = relativeTo(root, S.activeDir);
        const std::string rel = folder.empty() ? entry.name : folder + "/" + entry.name;
        if (searchMatches(query, entry.name, rel)) {
            if (S.hits.size() < kSearchMaxHits) S.hits.push_back({full, folder, isDir, entry});
            else ++S.extraHits;
        }
        if (isDir && S.activeDepth + 1 < kSearchMaxDepth)
            S.queue.emplace_back(full, S.activeDepth + 1);
    }
    S.done = S.queue.empty() && !S.activeRows;
    if (!S.done) ui::anim::requestFramesAt(30);
}

static void drawSearchResults(AppState& st, const std::string& root) {
    const auto& S = s_search;
    if (!S.done) kit::spinner("Searching\xE2\x80\xA6");
    for (const SearchHit& h : S.hits) {
        if (!h.isDir) {
            drawFileRow(st, root, h.path, &h.meta, 0, h.folder.c_str());
            continue;
        }
        if (skipClippedRow(h.path, true)) continue;
        const std::string key = normKey(h.path);
        ImGui::PushID(h.path.c_str());
        RowView v;
        v.icon = "body_folder";
        v.fallback = "[D]";
        const std::string name = pathFilename(h.path);
        v.label = name.c_str();
        v.suffix = h.folder.c_str();
        v.selected = key == s_selKey;
        const RowHit hit = drawRow("##hitdir", v);
        s_rowsThisFrame.push_back({h.path, true});
        scrollIfTarget(h.path, key);
        if (hit.clicked) selectPath(h.path, true);
        if (hit.doubleClicked) revealPath(root, h.path, true);
        folderContextMenu(st, root, h.path);
        rowTooltip(h.path, true, &h.meta, nullptr, "Double-click to show it in the tree");
        ImGui::PopID();
    }
    if (S.done && S.hits.empty()) kit::dimText("No matches");
    if (S.extraHits > 0) {
        const std::string more = "+" + std::to_string(S.extraHits) + " more matches \xE2\x80\x94 refine the filter";
        kit::dimText(more.c_str());
    }
    if (S.capped) kit::dimText("Search stopped after 4000 entries \xE2\x80\x94 refine the filter.");
}

// ---- tiles view -----------------------------------------------------------------
// One folder (or the search hits) as a grid of cards: folders, document previews, file icons.
// Same selection, open, drag, context menus, tooltips and inline rename as the tree rows;
// double-clicking a folder enters it (the header line becomes its breadcrumb).
struct TileItem {
    std::string path;
    bool isDir = false;
    const chudware::FsEntry* meta = nullptr;
    const char* folder = nullptr;   // search hits: workspace-relative parent
};

static float tileTextH() { return ImGui::GetFontSize() * 2.0f + 12.0f * ui::uiScale; }

static void enterTileDir(const std::string& dir) {
    s_tileDir = dir;
    s_currentFolder = dir;
    s_selectedPath.clear();
    s_query[0] = '\0';
    s_tileScrollTop = true;
}
// Back one folder (header Back button, Backspace, mouse Back), landing on the folder just left.
static void tileGoUp() {
    const std::string from = s_tileDir;
    enterTileDir(pathParent(s_tileDir));
    s_selectedPath = from;
    s_scrollTarget = normKey(from);
}

static void drawTileRename(AppState& st, const std::string& root, const std::string& path,
                           ImVec2 at, float width) {
    ImGui::SetCursorScreenPos(at);
    ImGui::SetNextItemWidth(width);
    if (s_renameFocus) ImGui::SetKeyboardFocusHere();
    if (ImGui::InputText("##tile_rename", s_renameBuf, sizeof(s_renameBuf),
                         ImGuiInputTextFlags_EnterReturnsTrue | ImGuiInputTextFlags_AutoSelectAll))
        commitRename(st, root);
    if (ImGui::IsItemDeactivated()) commitRename(st, root);
    if (s_pendingNewFolder == path) {
        ImGui::ScrollToItem(ImGuiScrollFlags_KeepVisibleCenterY);
        if (ImGui::IsItemVisible()) {
            s_pendingNewFolder.clear();
            s_renameFocus = false;
        }
    } else {
        s_renameFocus = false;
    }
}

// ---- folder tile glyph ----------------------------------------------------------
// A folder drawn from vector shapes (crisp at any scale): tabbed back panel, up to three
// document previews standing in the pocket, translucent front panel. The previews are the
// folder's own native documents (nested folders are searched two levels deep), rendered by the
// same file_thumbs pipeline as the document tiles.
struct PeekDoc {
    std::string path;
    int64_t mtime = 0;
    uint64_t size = 0;
};
static constexpr size_t kFolderPeekMax = 3;

static void collectPeekDocs(const std::string& dir, int depth, size_t want, int& dirBudget,
                            std::vector<PeekDoc>& out) {
    if (dirBudget <= 0) return;
    --dirBudget;
    const auto rows = dirRows(dir);
    for (size_t i = 0; i < rows->files.size() && out.size() < want; ++i)
        if (fileThumbSupported(lowerExt(rows->files[i])))
            out.push_back({rows->files[i], rows->fileMeta[i].mtime, rows->fileMeta[i].size});
    if (depth <= 0) return;
    for (size_t i = 0; i < rows->dirs.size() && out.size() < want; ++i)
        collectPeekDocs(rows->dirs[i], depth - 1, want, dirBudget, out);
}

static void drawFolderGlyph(ImDrawList* dl, ImVec2 a, ImVec2 b, const std::vector<PeekDoc>& docs,
                            bool lift) {
    const float S = ui::uiScale;
    const float W = std::min(std::min(b.x - a.x, (b.y - a.y) / 0.82f), 150.0f * S);
    const float H = W * 0.82f;
    const float fx0 = (a.x + b.x) * 0.5f - W * 0.5f, fx1 = fx0 + W;
    const float fy0 = (a.y + b.y) * 0.5f - H * 0.5f, fy1 = fy0 + H;
    const float r = std::max(3.0f * S, W * 0.06f);
    const float tabH = H * 0.10f, tabW = W * 0.38f;
    const float pocketTop = fy0 + H * 0.54f;
    const float stroke = std::max(1.0f, 1.0f * S);

    const ImU32 backCol   = IM_COL32(205, 138, 44, 255);
    const ImU32 frontCol  = IM_COL32(246, 187, 84, 238);
    const ImU32 edgeCol   = IM_COL32(150, 92, 22, 150);
    const ImU32 glossCol  = IM_COL32(255, 244, 214, 150);

    // Soft ground shadow, then the back panel with its tab.
    dl->AddRectFilled(ImVec2(fx0 + 2.0f * S, fy0 + tabH + 3.0f * S), ImVec2(fx1 - 2.0f * S, fy1 + 3.0f * S),
                      IM_COL32(0, 0, 0, 38), r + 2.0f * S);
    dl->AddRectFilled(ImVec2(fx0, fy0), ImVec2(fx0 + tabW, fy0 + tabH + r * 2.0f), backCol, r,
                      ImDrawFlags_RoundCornersTop);
    dl->AddRectFilled(ImVec2(fx0, fy0 + tabH), ImVec2(fx1, fy1), backCol, r);
    dl->AddRect(ImVec2(fx0, fy0 + tabH), ImVec2(fx1, fy1), edgeCol, r, 0, stroke);

    // Previews stand in the pocket; the front panel hides their lower part.
    const size_t n = std::min(docs.size(), kFolderPeekMax);
    if (n > 0) {
        const float s = W * (n == 1 ? 0.52f : n == 2 ? 0.45f : 0.42f);
        const float margin = W * 0.07f;
        const float baseTop = fy0 + H * 0.10f - (lift ? H * 0.03f : 0.0f);
        const float low = H * 0.05f;
        struct Slot { float x, y; };
        Slot slots[kFolderPeekMax];
        const float cx = (fx0 + fx1) * 0.5f - s * 0.5f;
        if (n == 1) {
            slots[0] = {cx, baseTop};
        } else if (n == 2) {
            slots[0] = {fx0 + margin, baseTop + low};
            slots[1] = {fx1 - margin - s, baseTop};
        } else {
            slots[0] = {fx0 + margin, baseTop + low};
            slots[1] = {fx1 - margin - s, baseTop + low};
            slots[2] = {cx, baseTop};   // centre card on top
        }
        for (size_t i = 0; i < n; ++i) {
            const ImVec2 c0(slots[i].x, slots[i].y), c1(c0.x + s, c0.y + s);
            const FileThumb thumb = fileThumb(docs[i].path, docs[i].mtime, docs[i].size);
            const char* icon = thumb.kind == NativeDocKind::Assembly ? "assembly" : fileIconName(".step+");
            dl->AddRectFilled(ImVec2(c0.x + 1.0f * S, c0.y + 2.0f * S), ImVec2(c1.x + 1.0f * S, c1.y + 2.0f * S),
                              IM_COL32(0, 0, 0, 55), 4.0f * S);
            dl->AddRectFilled(c0, c1, IM_COL32(246, 244, 238, 255), 4.0f * S);
            drawPreviewBox(dl, c0, c1, &thumb, icon, 1.0f, 4.0f * S);
        }
    }

    // Front panel: pocket with a gloss line along its top edge.
    dl->AddRectFilled(ImVec2(fx0, pocketTop), ImVec2(fx1, fy1), frontCol, r);
    dl->AddRectFilled(ImVec2(fx0 + r * 0.5f, pocketTop + stroke), ImVec2(fx1 - r * 0.5f, pocketTop + stroke * 2.0f),
                      glossCol, 0.0f);
    dl->AddRect(ImVec2(fx0, pocketTop), ImVec2(fx1, fy1), edgeCol, r, 0, stroke);
}

static void drawTile(AppState& st, const std::string& root, const TileItem& it, float w, float h) {
    const float S = ui::uiScale, fs = ImGui::GetFontSize();
    const std::string ext = it.isDir ? std::string() : lowerExt(it.path);
    const std::string key = normKey(it.path);
    const std::string fname = pathFilename(it.path);
    const bool native = !it.isDir && it.meta && fileThumbSupported(ext);
    const bool known = it.isDir || isKnownExt(ext);
    const bool active = !it.isDir && !s_activeKey.empty() && key == s_activeKey;
    const bool open = !it.isDir && (active || s_openKeys.count(key) != 0);
    if (it.isDir && s_pendingNewFolder == it.path && s_renameId != it.path) {
        s_renameId = it.path;
        std::snprintf(s_renameBuf, sizeof(s_renameBuf), "%s", fname.c_str());
        s_renameFocus = true;
        s_selectedPath = it.path;
        s_selKey = key;
    }
    const bool selected = key == s_selKey;
    const ExplorerPlmItem* status = it.isDir ? nullptr : statusForPath(it.path);
    FileThumb thumb;
    if (native) thumb = fileThumb(it.path, it.meta->mtime, it.meta->size);

    ImGui::PushID(it.path.c_str());
    const ImVec2 p0 = ImGui::GetCursorScreenPos(), p1(p0.x + w, p0.y + h);
    ImGui::InvisibleButton("##tile", ImVec2(w, h));
    const bool hovered = ImGui::IsItemHovered();
    const bool clicked = ImGui::IsItemClicked(ImGuiMouseButton_Left);
    const bool doubleClicked = clicked && ImGui::IsMouseDoubleClicked(ImGuiMouseButton_Left);
    ui::fmCardPaint(p0, p1, selected, hovered, 8.0f);

    ImDrawList* dl = ImGui::GetWindowDrawList();
    const ui::AdaptiveSurface& surf = explorerSurf();
    const float pad = 7.0f * S;
    const ImVec2 a(p0.x + pad, p0.y + pad), b(p1.x - pad, p1.y - tileTextH());
    if (it.isDir) {
        std::vector<PeekDoc> peeks;
        int dirBudget = 8;   // listings one tile may probe per frame (cached by the backend)
        collectPeekDocs(it.path, 2, kFolderPeekMax, dirBudget, peeks);
        drawFolderGlyph(dl, a, b, peeks, hovered || selected);
    } else {
        const char* icon = native && thumb.kind == NativeDocKind::Assembly ? "assembly"
                                                                         : fileIconName(ext);
        drawPreviewBox(dl, a, b, native ? &thumb : nullptr, icon, known ? 1.0f : 0.55f, 6.0f * S);
    }
    if (open)
        dl->AddCircleFilled(ImVec2(a.x + 8.0f * S, a.y + 8.0f * S), fs * 0.2f,
                            ImGui::ColorConvertFloat4ToU32(kit::toneOn(kit::Tone::Accent, surf)));
    if (status && !status->change.empty()) {
        ExplorerPlmItem codeOnly;
        codeOnly.change = status->change;
        drawVaultMeta(dl, &codeOnly, b.x - 4.0f * S, a.y + 4.0f * S, a.x);
    }

    // Two centred lines under the picture: name, then kind/age (or size, or the hit's folder).
    const float textW = w - pad * 2.0f;
    const std::string name = kit::elide((native ? documentName(fname) : fname).c_str(), textW);
    std::string sub;
    if (native) sub = documentSubtitle(thumb, it.folder, it.meta->mtime, /*compact=*/true);
    else if (it.folder && it.folder[0]) sub = it.folder;
    else if (it.isDir) sub = "Folder";
    else if (it.meta) sub = kit::fmtBytes(it.meta->size);
    sub = kit::elide(sub.c_str(), textW);
    const float line1 = b.y + 5.0f * S;
    const ImU32 nameInk = ImGui::ColorConvertFloat4ToU32(
        active ? kit::toneOn(kit::Tone::Accent, surf) : known ? surf.textPrimary : surf.textSecondary);
    const float nameW = ImGui::CalcTextSize(name.c_str()).x;
    if (s_renameId != it.path)
        dl->AddText(ImVec2(p0.x + (w - nameW) * 0.5f, line1), nameInk, name.c_str());
    const float subW = ImGui::CalcTextSize(sub.c_str()).x;
    dl->AddText(ImVec2(p0.x + (w - subW) * 0.5f, line1 + fs + 2.0f * S),
                ImGui::ColorConvertFloat4ToU32(surf.textSecondary), sub.c_str());

    s_rowsThisFrame.push_back({it.path, it.isDir});
    scrollIfTarget(it.path, key);
    if (clicked) {
        selectPath(it.path, it.isDir);
        s_currentFolder = s_tileDir;   // New Folder / Paste act in the folder on screen
    }
    if (doubleClicked) {
        if (it.isDir) enterTileDir(it.path);
        else requestOpen(st, it.path);
    }
    if (!it.isDir && (isKnownExt(ext) || ext == ".json") &&
        ImGui::BeginDragDropSource(ImGuiDragDropFlags_SourceAllowNullID)) {
        ImGui::SetDragDropPayload(kFileDragPayload, it.path.c_str(), it.path.size() + 1);
        ImGui::TextUnformatted(fname.c_str());
        ImGui::EndDragDropSource();
    }
    if (it.isDir) folderContextMenu(st, root, it.path);
    else fileContextMenu(st, root, it.path, ext, status);
    rowTooltip(it.path, it.isDir, it.meta, status, it.isDir ? "Double-click to open" : nullptr);
    if (s_renameId == it.path)
        drawTileRename(st, root, it.path, ImVec2(p0.x + pad, line1 - 3.0f * S), textW);
    ImGui::PopID();
}

static void drawTilesView(AppState& st, const std::string& root, const std::string& query) {
    const float S = ui::uiScale;
    const float gap = 8.0f * S;
    const float avail = ImGui::GetContentRegionAvail().x;
    const float minW = 118.0f * S;
    const int cols = std::max(1, (int)((avail + gap) / (minW + gap)));
    const float w = std::floor((avail - gap * (float)(cols - 1)) / (float)cols);
    const float h = std::floor(w - 14.0f * S) + tileTextH() + 7.0f * S;   // picture ~ square
    s_tileCols = cols;
    if (s_tileScrollTop) {
        ImGui::SetScrollY(0.0f);
        s_tileScrollTop = false;
    }

    std::vector<TileItem> items;
    std::shared_ptr<DirListCache> rows;   // keeps the listing's metadata alive for the walk
    if (!query.empty()) {
        if (!s_search.done) kit::spinner("Searching\xE2\x80\xA6");
        for (const SearchHit& hit : s_search.hits)
            items.push_back({hit.path, hit.isDir, &hit.meta, hit.folder.c_str()});
    } else {
        rows = dirRows(s_tileDir);
        for (size_t i = 0; i < rows->dirs.size(); ++i)
            items.push_back({rows->dirs[i], true, &rows->dirMeta[i], nullptr});
        for (size_t i = 0; i < rows->files.size(); ++i)
            items.push_back({rows->files[i], false, &rows->fileMeta[i], nullptr});
    }

    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing, ImVec2(gap, gap));
    for (size_t i = 0; i < items.size(); i += (size_t)cols) {
        const size_t end = std::min(items.size(), i + (size_t)cols);
        bool live = ImGui::IsRectVisible(ImVec2(avail, h)) ||
                    ImGui::GetCurrentContext()->DragDropActive;
        for (size_t j = i; j < end && !live; ++j) {
            const std::string key = normKey(items[j].path);
            live = key == s_selKey || key == s_scrollTarget || items[j].path == s_renameId ||
                   items[j].path == s_pendingNewFolder;
        }
        if (!live) {   // offscreen row: keep layout + keyboard order, build nothing
            ImGui::Dummy(ImVec2(avail, h));
            for (size_t j = i; j < end; ++j) s_rowsThisFrame.push_back({items[j].path, items[j].isDir});
            continue;
        }
        for (size_t j = i; j < end; ++j) {
            if (j > i) ImGui::SameLine(0.0f, gap);
            drawTile(st, root, items[j], w, h);
        }
    }
    ImGui::PopStyleVar();

    if (!query.empty()) {
        if (s_search.done && s_search.hits.empty()) kit::dimText("No matches");
        if (s_search.extraHits > 0) {
            const std::string more = "+" + std::to_string(s_search.extraHits) +
                                     " more matches \xE2\x80\x94 refine the filter";
            kit::dimText(more.c_str());
        }
        return;
    }
    // A folder created elsewhere (a folder's context menu) is revealed where it lives; one this
    // completed listing cannot contain is dropped (the tree view applies the same rule).
    if (!s_pendingNewFolder.empty()) {
        if (normKey(pathParent(s_pendingNewFolder)) != normKey(s_tileDir)) {
            enterTileDir(pathParent(s_pendingNewFolder));
        } else if (rows->state != chudware::FsReadState::Loading &&
                   std::none_of(rows->dirs.begin(), rows->dirs.end(), [](const std::string& d) {
                       return d == s_pendingNewFolder;
                   })) {
            if (s_renameId == s_pendingNewFolder) s_renameId.clear();
            s_pendingNewFolder.clear();
            s_renameFocus = false;
        }
    }
    if (rows->state == chudware::FsReadState::Error)
        kit::blockedState("Folder unreadable", rows->error.c_str(), "Refresh to retry.");
    else if (rows->state == chudware::FsReadState::Missing)
        kit::blockedState("Folder missing", s_tileDir.c_str(), "Choose another working folder.");
    else if (rows->state == chudware::FsReadState::Loading && items.empty())
        kit::spinner("Loading\xE2\x80\xA6");
    else if (items.empty())
        kit::emptyState("body_folder", "Empty folder",
                        "Save a part here, or right-click to create a folder.");
}

// ---- Recent strip ---------------------------------------------------------------
// One-line collapsible strip header: disclosure arrow + label, then a right-aligned count chip.
// Returns true when clicked (toggle).
static bool stripHeader(const char* id, const char* label, bool open, const char* badge,
                        const char* tooltip) {
    const ui::AdaptiveSurface& surf = explorerSurf();
    const float fs = ImGui::GetFontSize();
    const float h = ImGui::GetFrameHeight();
    const bool clicked = kit::rowBegin(id, h, false);
    const bool hovered = ImGui::IsItemHovered();
    const ImVec2 p0 = ImGui::GetItemRectMin(), p1 = ImGui::GetItemRectMax();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const float midY = (p0.y + p1.y) * 0.5f, textY = midY - fs * 0.5f;
    const float padX = kit::padX();
    const ImU32 inkDim = ImGui::ColorConvertFloat4ToU32(surf.textSecondary);
    const float sc = 0.7f;
    ImGui::RenderArrow(dl, ImVec2(p0.x + padX, midY - fs * 0.5f * sc), inkDim,
                       open ? ImGuiDir_Down : ImGuiDir_Right, sc);
    dl->AddText(ImVec2(p0.x + padX + fs * 1.1f, textY), ImGui::GetColorU32(ImGuiCol_Text), label);
    if (badge && badge[0]) {
        const float chipW = ui::metrics::U(1.5f) * 2.0f + ImGui::CalcTextSize(badge).x;
        const float chipH = ImGui::GetTextLineHeight() + ui::metrics::U(0.5f) * 2.0f;
        ImGui::SetCursorScreenPos(ImVec2(p1.x - padX - chipW, midY - chipH * 0.5f));
        kit::chip(badge, kit::Tone::Neutral);
    }
    if (hovered && tooltip && tooltip[0]) ImGui::SetTooltip("%s", tooltip);
    kit::rowEnd();
    return clicked;
}

static float pillRowH() { return ImGui::GetFontSize() + 8.0f * ui::uiScale; }   // fmRowPill frame
static constexpr int kRecentRowsMax = 4;
// Strip toggles apply at the start of the NEXT frame, before the tree height is reserved, so a
// strip never expands past the pane for a frame.
static bool s_toggleRecent = false;

// Native documents are preview cards here too; the height follows the extension alone so the
// strip never jumps while a stat or a thumbnail is still pending.
static float recentRowH(const std::string& path) {
    return fileThumbSupported(lowerExt(path)) ? cardRowH() : pillRowH();
}
static float recentListHeight(const AppState& st) {
    const float sp = ImGui::GetStyle().ItemSpacing.y;
    float h = 0.0f;
    const size_t n = std::min<size_t>(st.recentFiles.size(), kRecentRowsMax);
    for (size_t i = 0; i < n; ++i) h += recentRowH(st.recentFiles[i]) + sp;
    return h;
}
static float recentStripHeight(const AppState& st) {
    if (st.recentFiles.empty()) return 0.0f;
    const float sp = ImGui::GetStyle().ItemSpacing.y;
    float h = ImGui::GetFrameHeight() + sp;
    if (s_recentOpen) h += recentListHeight(st) + sp;
    return h;
}

static void drawRecentStrip(AppState& st) {
    if (st.recentFiles.empty()) return;
    auto& fsb = chudware::fsBackend();
    const std::string count = std::to_string(st.recentFiles.size());
    if (stripHeader("##fx_recent", "Recent", s_recentOpen, count.c_str(),
                    "Recently opened files (may live outside the working folder)"))
        s_toggleRecent = true;
    if (!s_recentOpen) return;
    ImGui::BeginChild("##fx_recent_rows", ImVec2(0, recentListHeight(st)), ImGuiChildFlags_None,
                      ImGuiWindowFlags_NoScrollbar);
    int idx = 0;
    for (const std::string& rp : st.recentFiles) {
        const std::string ext = lowerExt(rp);
        const bool card = fileThumbSupported(ext);
        if (skipClippedRow(rp, false, false, recentRowH(rp))) { ++idx; continue; }
        ImGui::PushID(idx++);
        const auto probe = fsb.pollStat(rp);
        const bool statReady = probe.state == chudware::FsReadState::Ready;
        // Unknown/network errors are not evidence of deletion. Keep opening
        // available; doc_open reports authoritative failures without a preflight.
        const bool exists = !statReady || probe.value.exists;
        const std::string fname = pathFilename(rp);
        const std::string key = normKey(rp);
        const ExplorerPlmItem* status = statusForPath(rp);
        chudware::FsEntry meta;
        meta.size = probe.value.size;
        meta.mtime = probe.value.mtime;
        FileThumb thumb;
        if (card && statReady && exists) thumb = fileThumb(rp, meta.mtime, meta.size);
        else if (card && !statReady) thumb.state = FileThumb::State::Pending;
        std::string label = fname, subtitle;
        if (card) {
            label = documentName(fname);
            const std::string folder = pathFilename(pathParent(rp));
            subtitle = exists ? documentSubtitle(thumb, folder.c_str(), statReady ? meta.mtime : 0)
                              : "Missing on disk";
        }
        RowView v;
        v.icon = card && thumb.kind == NativeDocKind::Assembly ? "assembly" : fileIconName(ext);
        v.fallback = fileIconFallback(ext);
        v.iconAlpha = exists ? 1.0f : 0.45f;
        v.label = label.c_str();
        v.dim = !exists;
        v.openDot = s_openKeys.count(key) != 0;
        v.status = status;
        if (card) {
            v.thumb = &thumb;
            v.subtitle = subtitle.c_str();
        }
        const RowHit hit = drawRow("##recent", v);
        if (hit.doubleClicked && exists) requestOpen(st, rp);
        // Recent files use the same model/scenario payload as directory rows.
        if (exists && (isKnownExt(ext) || ext == ".json") &&
            ImGui::BeginDragDropSource(ImGuiDragDropFlags_SourceAllowNullID)) {
            ImGui::SetDragDropPayload(kFileDragPayload, rp.c_str(), rp.size() + 1);
            ImGui::TextUnformatted(fname.c_str());
            ImGui::EndDragDropSource();
        }
        ui::pushContextMenuStyle();
        if (ImGui::BeginPopupContextItem("##rctx")) {
            if (ImGui::MenuItem("Open", nullptr, false, exists && isKnownExt(ext))) requestOpen(st, rp);
            pathMenuItems(rp);
            ImGui::EndPopup();
        }
        ui::popContextMenuStyle();
        rowTooltip(rp, false, statReady && exists ? &meta : nullptr, status,
                   exists ? nullptr : "Missing on disk", card ? &thumb : nullptr);
        ImGui::PopID();
    }
    ui::drawWindowPillScrollbarV(explorerSurf());
    ImGui::EndChild();
}

static void runCheckpoint(AppState& st, const std::string& project) {
    auto& P = s_plm;
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

// ---- Changes pane ------------------------------------------------------------------
// The left column's Changes tab: the working folder's uncheckpointed vault changes grouped by
// kind (Modified / Added / Deleted / Untracked), native documents as preview cards like the
// Explorer's, with the checkpoint composer pinned under the list.
static constexpr const char* kChangeCodes[] = {"M", "A", "D", "U"};
static bool s_changeGroupOpen[4] = {true, true, true, true};
static std::string s_changeSel;   // normKey of the selected change row

// Every tab's saved path feeds the open-in-tab dots; returns the active document's path.
static std::string refreshOpenMarkers(const AppState& st) {
    std::string activePath;
    s_openKeys.clear();
    s_activeKey.clear();
    for (int i = 0; i < (int)st.docTabs.size(); ++i) {
        const std::string& p = st.docTabs[i].path;
        if (p.empty()) continue;
        s_openKeys.insert(normKey(p));
        if (i == st.activeTab) {
            activePath = p;
            s_activeKey = normKey(p);
        }
    }
    return activePath;
}

static void refreshChanges(AppState& st, const std::string& project) {
    invalidateExplorerPlm();
    pollExplorerPlm(st, project, true);
}

static void drawChangeRow(AppState& st, const std::string& project, const ExplorerPlmItem& item) {
    const std::string absolute = pathJoin(project, item.path);
    const std::string ext = lowerExt(absolute);
    const bool deleted = item.change == "D";
    const bool card = !deleted && fileThumbSupported(ext);
    if (skipClippedRow(absolute, false, false, card ? cardRowH() : 0.0f)) return;
    ImGui::PushID(item.path.c_str());
    const std::string key = normKey(absolute);
    const std::string fname = pathFilename(absolute);
    const size_t slash = item.path.find_last_of("/\\");
    const std::string folder = slash == std::string::npos ? std::string() : item.path.substr(0, slash);
    chudware::FsEntry meta;
    bool haveMeta = false;
    if (!deleted) {
        const auto probe = chudware::fsBackend().pollStat(absolute);
        if (probe.state == chudware::FsReadState::Ready && probe.value.exists) {
            meta.size = probe.value.size;
            meta.mtime = probe.value.mtime;
            haveMeta = true;
        }
    }
    FileThumb thumb;
    if (card && haveMeta) thumb = fileThumb(absolute, meta.mtime, meta.size);
    else if (card) thumb.state = FileThumb::State::Pending;
    const std::string label = card ? documentName(fname) : fname;
    const std::string subtitle =
        card ? documentSubtitle(thumb, folder.empty() ? nullptr : folder.c_str(), haveMeta ? meta.mtime : 0)
             : std::string();
    RowView v;
    v.icon = card && thumb.kind == NativeDocKind::Assembly ? "assembly" : fileIconName(ext);
    v.fallback = fileIconFallback(ext);
    v.label = label.c_str();
    v.suffix = card || folder.empty() ? nullptr : folder.c_str();
    v.dim = deleted;
    v.iconAlpha = deleted ? 0.5f : 1.0f;
    v.accent = !s_activeKey.empty() && key == s_activeKey;
    v.openDot = s_openKeys.count(key) != 0;
    v.selected = key == s_changeSel;
    v.status = &item;
    if (card) {
        v.thumb = &thumb;
        v.subtitle = subtitle.c_str();
    }
    const RowHit hit = drawRow("##change", v);
    const bool openable = !deleted && isKnownExt(ext);
    if (hit.clicked) s_changeSel = key;
    if (hit.doubleClicked) {
        if (openable) requestOpen(st, absolute);
        else if (!item.itemId.empty()) openPlm(st, PlmPage::Files, item.itemId);
    }
    if (!deleted && (isKnownExt(ext) || ext == ".json") &&
        ImGui::BeginDragDropSource(ImGuiDragDropFlags_SourceAllowNullID)) {
        ImGui::SetDragDropPayload(kFileDragPayload, absolute.c_str(), absolute.size() + 1);
        ImGui::TextUnformatted(fname.c_str());
        ImGui::EndDragDropSource();
    }
    ui::pushContextMenuStyle();
    if (ImGui::BeginPopupContextItem("##change_menu")) {
        s_changeSel = key;
        if (ImGui::MenuItem("Open", nullptr, false, openable)) requestOpen(st, absolute);
        const bool inWorkspace = !st.workspaceDir.empty() && withinWorkspace(st.workspaceDir, absolute);
        if (ImGui::MenuItem("Show in Explorer", nullptr, false, !deleted && inWorkspace)) {
            st.leftPaneTab = kLeftPaneExplorer;
            revealPath(st.workspaceDir, absolute, false);
        }
        if (!item.itemId.empty()) {
            if (ImGui::MenuItem("History")) openPlm(st, PlmPage::Files, item.itemId);
            if (ImGui::MenuItem("Release in PLM")) openPlm(st, PlmPage::Releases, item.itemId);
        }
        ImGui::Separator();
        pathMenuItems(absolute);
        ImGui::EndPopup();
    }
    ui::popContextMenuStyle();
    rowTooltip(absolute, false, haveMeta ? &meta : nullptr, &item,
               deleted ? (item.itemId.empty() ? nullptr : "Double-click for its history in PLM")
                       : "Double-click to open",
               card ? &thumb : nullptr);
    ImGui::PopID();
}

// Header: branch (or vault state) on the left, the change count and Refresh on the right.
static void drawChangesHeader(AppState& st, const std::string& project, int count) {
    const auto& P = s_plm;
    const float S = ui::uiScale, fs = ImGui::GetFontSize();
    const float h = ImGui::GetFrameHeight(), gap = 4.0f * S;
    const ui::AdaptiveSurface& surf = explorerSurf();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const ImVec2 p0 = ImGui::GetCursorScreenPos();
    const float avail = ImGui::GetContentRegionAvail().x;
    std::string summary;
    kit::Tone tone = kit::Tone::Neutral;
    if (P.checkpointing) summary = "Saving\xE2\x80\xA6";
    else if (P.ready && count > 0) {
        summary = std::to_string(count) + (count == 1 ? " change" : " changes");
        tone = kit::Tone::Warn;
    } else if (P.ready) summary = "Up to date";
    const float chipW = summary.empty() ? 0.0f
        : ui::metrics::U(1.5f) * 2.0f + ImGui::CalcTextSize(summary.c_str()).x;
    const float right = p0.x + avail - h - (summary.empty() ? 0.0f : chipW + gap);

    ImGui::Dummy(ImVec2(std::min(16.0f * S, fs) + 6.0f * S, h));
    ui::fmTreeIconInArrowSlot(ImVec2(p0.x - ImGui::GetStyle().FramePadding.x, p0.y), "git", "[G]");
    const float textX = p0.x + std::min(16.0f * S, fs) + 6.0f * S;
    std::string title = !P.branch.empty() ? P.branch : pathFilename(project);
    if (title.empty()) title = "Vault";
    title = kit::elide(title.c_str(), std::max(right - gap - textX, fs));
    dl->AddText(ImVec2(textX, p0.y + (h - fs) * 0.5f), ImGui::ColorConvertFloat4ToU32(surf.textPrimary),
                title.c_str());
    if (ImGui::IsMouseHoveringRect(ImVec2(textX, p0.y), ImVec2(right, p0.y + h)) &&
        ImGui::IsWindowHovered())
        ImGui::SetTooltip(P.branch.empty() ? "Vault: %s" : "Active vault branch\n%s",
                          P.branch.empty() ? project.c_str() : P.branch.c_str());
    if (!summary.empty()) {
        const float chipH = ImGui::GetTextLineHeight() + ui::metrics::U(0.5f) * 2.0f;
        ImGui::SetCursorScreenPos(ImVec2(right, p0.y + (h - chipH) * 0.5f));
        kit::chip(summary.c_str(), tone, false, "Uncheckpointed vault changes in the working folder");
    }
    ImGui::SetCursorScreenPos(ImVec2(p0.x + avail - h, p0.y));
    const bool canRefresh = st.workerStarted && !P.loading && !P.checkpointing;
    if (kit::iconButton("##chg_refresh", "refresh",
                        canRefresh ? "Refresh vault status" : "Refresh vault status (scan in progress)",
                        canRefresh, kit::Tone::Neutral, h))
        refreshChanges(st, project);
    ImGui::SetCursorScreenPos(ImVec2(p0.x, p0.y + h + ImGui::GetStyle().ItemSpacing.y));
    ImGui::Dummy(ImVec2(0, 0));
}

// Checkpoint composer under the list: message, then Checkpoint + Open PLM, then any status.
static float checkpointComposerHeight() {
    const float sp = ImGui::GetStyle().ItemSpacing.y;
    float h = sp * 2.0f + ImGui::GetTextLineHeight() * 2.0f + ImGui::GetStyle().FramePadding.y * 2.0f +
              sp + ImGui::GetFrameHeight();
    if (s_plm.checkpointing || !s_plm.checkpointError.empty()) h += sp + ImGui::GetTextLineHeightWithSpacing();
    return h;
}
static void drawCheckpointComposer(AppState& st, const std::string& project, int count) {
    auto& P = s_plm;
    const float S = ui::uiScale;
    const ImGuiStyle& style = ImGui::GetStyle();
    const ui::AdaptiveSurface& surf = explorerSurf();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const ImVec2 at = ImGui::GetCursorScreenPos();
    const float avail = ImGui::GetContentRegionAvail().x;
    dl->AddLine(ImVec2(at.x, at.y + style.ItemSpacing.y * 0.5f),
                ImVec2(at.x + avail, at.y + style.ItemSpacing.y * 0.5f),
                inkWithAlpha(surf.textPrimary, 0.12f), std::max(1.0f, 1.0f * S));
    ImGui::Dummy(ImVec2(0, style.ItemSpacing.y));
    const float inputH = ImGui::GetTextLineHeight() * 2.0f + style.FramePadding.y * 2.0f;
    ImGui::InputTextMultiline("##checkpoint_msg", P.message, sizeof(P.message), ImVec2(-1.0f, inputH));
    if (P.message[0] == '\0' && !ImGui::IsItemActive()) {
        const ImVec2 mn = ImGui::GetItemRectMin();
        dl->AddText(ImVec2(mn.x + style.FramePadding.x, mn.y + style.FramePadding.y),
                    ImGui::ColorConvertFloat4ToU32(surf.textSecondary), "Describe this checkpoint (optional)");
    }
    const std::string label = "Checkpoint " + std::to_string(count) + (count == 1 ? " change" : " changes");
    const char* plmLabel = "Open PLM";
    const float plmW = ImGui::CalcTextSize(plmLabel).x + style.FramePadding.x * 4.0f;
    const float gap = 4.0f * S;
    if (kit::primaryButton(label.c_str(), !P.loading && !P.checkpointing,
                           P.checkpointing ? "Saving the checkpoint" : "Wait for the vault scan to finish",
                           ImVec2(avail - plmW - gap, 0)))
        runCheckpoint(st, project);
    ImGui::SameLine(0, gap);
    if (kit::secondaryButton(plmLabel, st.workerStarted, "Service unavailable", ImVec2(plmW, 0)))
        openPlm(st, PlmPage::Files);
    if (P.checkpointing) kit::spinner("Saving checkpoint");
    kit::inlineStatus(P.checkpointError.c_str(), kit::Tone::Err);
}

void drawChangesPaneBody(AppState& st) {
    ui::fmRowStripeReset();
    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing,
                        ImVec2(ImGui::GetStyle().ItemSpacing.x, 4.0f * ui::uiScale));
    struct SpacingPop { ~SpacingPop() { ImGui::PopStyleVar(); } } spacingPop;
    kit::pushDensity(kit::Density::Compact);
    struct DensityPop { ~DensityPop() { kit::popDensity(); } } densityPop;

    ensureWorkspaceDir(st);
    const std::string project = st.plmProjectPath();
    if (project.empty()) {
        kit::emptyState("git", "No working folder", "Choose a working folder to track its changes.");
        return;
    }
    pollExplorerPlm(st, project);
    fileThumbsPump();
    refreshOpenMarkers(st);
    auto& P = s_plm;
    const int count = changedCount();
    drawChangesHeader(st, project, count);

    if (!st.workerStarted) {
        kit::blockedState("Vault offline", "The services worker is not connected.",
                          "Changes appear once it reconnects; the Explorer stays browsable.");
        return;
    }
    if (!P.error.empty()) {
        if (kit::blockedState("Status unavailable", P.error.c_str(), nullptr, "Retry"))
            refreshChanges(st, project);
        return;
    }
    if (!P.ready) {
        kit::spinner("Checking for changes\xE2\x80\xA6");
        return;
    }
    if (count == 0) {
        const char* body = P.checkpointOk.empty() ? "Everything matches the last checkpoint."
                                                  : "Checkpoint saved. Everything matches it.";
        if (kit::emptyState("git", "No changes", body, "Open PLM", st.workerStarted))
            openPlm(st, PlmPage::Files);
        return;
    }

    ImGui::BeginChild("##changes_list", ImVec2(0, -checkpointComposerHeight()), ImGuiChildFlags_None,
                      ImGuiWindowFlags_NoScrollbar);
    ImGuiWindow* list = ImGui::GetCurrentWindow();
    const float workRight = list->WorkRect.Max.x, contentRight = list->ContentRegionRect.Max.x;
    list->WorkRect.Max.x -= ui::metrics::px(14.0f);   // the overlay scrollbar's hit lane
    list->ContentRegionRect.Max.x -= ui::metrics::px(14.0f);
    for (int g = 0; g < 4; ++g) {
        const std::string code = kChangeCodes[g];
        int n = 0;
        for (const auto& item : P.items) n += item.change == code;
        for (const auto& item : P.untracked) n += item.change == code;
        if (n == 0) continue;
        ImGui::PushID(g);
        const std::string title = std::string(changeName(code)) + " (" + std::to_string(n) + ")";
        ui::fmSectionDividerCollapse(title.c_str(), &s_changeGroupOpen[g]);
        if (s_changeGroupOpen[g]) {
            for (const auto& item : P.items)
                if (item.change == code) drawChangeRow(st, project, item);
            for (const auto& item : P.untracked)
                if (item.change == code) drawChangeRow(st, project, item);
        }
        ImGui::PopID();
    }
    ui::pushContextMenuStyle();
    if (ImGui::BeginPopupContextWindow("##changes_ctx",
                                       ImGuiPopupFlags_MouseButtonRight | ImGuiPopupFlags_NoOpenOverItems)) {
        if (ImGui::MenuItem("Refresh vault status", nullptr, false, !P.loading && !P.checkpointing))
            refreshChanges(st, project);
        if (ImGui::MenuItem("Open PLM")) openPlm(st, PlmPage::Files);
        ImGui::EndPopup();
    }
    ui::popContextMenuStyle();
    list->WorkRect.Max.x = workRight;
    list->ContentRegionRect.Max.x = contentRight;
    ui::drawWindowPillScrollbarV(explorerSurf(), ui::PillScrollbarStyle{5.0f, false});
    ImGui::EndChild();
    drawCheckpointComposer(st, project, count);
}

int fileChangesCount(const AppState& st) {
    return s_plm.project == st.plmProjectPath() ? changedCount() : 0;
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

// ---- keyboard -------------------------------------------------------------------
// Pane-focused keys: Ctrl+F filter, Esc cancel/clear, F2 rename, Del delete, Enter open/toggle,
// Up/Down move through last frame's visible rows, Right/Left expand/collapse (Left on a closed
// or file row selects its parent folder). Tiles: arrows move in the grid, Enter opens a file or
// enters a folder, Backspace goes up one folder.
static void handleExplorerKeys(AppState& st, const std::string& root, bool tiles) {
    if (!ImGui::IsWindowFocused(ImGuiFocusedFlags_RootAndChildWindows)) return;
    ImGuiIO& io = ImGui::GetIO();
    if (ImGui::IsKeyChordPressed(ImGuiMod_Ctrl | ImGuiKey_F)) {
        s_focusSearch = true;
        return;
    }
    if (ImGui::IsKeyPressed(ImGuiKey_Escape, false)) {
        s_pendingNewFolder.clear();
        s_renameId.clear();
        s_renameFocus = false;
        // An active filter field clears itself (EscapeClearsAll); otherwise Esc leaves search.
        if (!io.WantTextInput) s_query[0] = '\0';
    }
    if (io.WantTextInput || io.KeyCtrl || io.KeyAlt || io.KeySuper) return;
    if (!s_selectedPath.empty()) {
        if (ImGui::IsKeyPressed(ImGuiKey_F2, false)) { beginRename(root, s_selectedPath); return; }
        if (ImGui::IsKeyPressed(ImGuiKey_Delete, false)) { s_pendingDelete = s_selectedPath; return; }
    }
    if (tiles && s_query[0] == '\0' && ImGui::IsKeyPressed(ImGuiKey_Backspace, false) &&
        normKey(s_tileDir) != normKey(root)) {
        tileGoUp();
        return;
    }
    const auto& rows = s_rowsLastFrame;
    if (rows.empty()) return;
    int at = -1;
    const std::string selKey = normKey(s_selectedPath);
    for (int i = 0; i < (int)rows.size(); ++i)
        if (normKey(rows[i].path) == selKey) { at = i; break; }
    auto moveTo = [&](int i) {
        i = std::max(0, std::min(i, (int)rows.size() - 1));
        selectPath(rows[i].path, rows[i].isDir);
        s_scrollTarget = normKey(rows[i].path);
        s_scrollCenter = false;
    };
    const int step = tiles ? std::max(1, s_tileCols) : 1;
    if (ImGui::IsKeyPressed(ImGuiKey_DownArrow)) { moveTo(at < 0 ? 0 : at + step); return; }
    if (ImGui::IsKeyPressed(ImGuiKey_UpArrow)) { moveTo(at < 0 ? (int)rows.size() - 1 : at - step); return; }
    if (tiles && ImGui::IsKeyPressed(ImGuiKey_RightArrow)) { moveTo(at < 0 ? 0 : at + 1); return; }
    if (tiles && ImGui::IsKeyPressed(ImGuiKey_LeftArrow)) { moveTo(at < 0 ? 0 : at - 1); return; }
    if (at < 0) return;
    const VisRow& row = rows[at];
    const bool searching = s_query[0] != '\0';
    if (ImGui::IsKeyPressed(ImGuiKey_Enter, false) || ImGui::IsKeyPressed(ImGuiKey_KeypadEnter, false)) {
        if (!row.isDir) requestOpen(st, row.path);
        else if (tiles) enterTileDir(row.path);
        else if (searching) revealPath(root, row.path, true);
        else setExpanded(row.path, !isExpanded(row.path));
        return;
    }
    if (searching || tiles) return;
    if (ImGui::IsKeyPressed(ImGuiKey_RightArrow) && row.isDir) {
        setExpanded(row.path, true);
    } else if (ImGui::IsKeyPressed(ImGuiKey_LeftArrow)) {
        if (row.isDir && isExpanded(row.path)) {
            setExpanded(row.path, false);
        } else {
            const std::string up = pathParent(row.path);
            if (!up.empty() && normKey(up) != normKey(root) && withinWorkspace(root, up)) {
                selectPath(up, true);
                s_scrollTarget = normKey(up);
                s_scrollCenter = false;
            }
        }
    }
}

// ---- headless capture hooks --------------------------------------------------------
// CHUDWARE_SHOT=explorer (app.cpp forces the Explorer tab) reads these once, so the capture
// shows a deterministic state of the redesigned pane:
//   CHUDWARE_EXPLORER_SHOT_EXPAND=parts,assemblies/sub   expand workspace-relative folders
//   CHUDWARE_EXPLORER_SHOT_SELECT=parts/bracket.step+    select (and scroll to) a path
//   CHUDWARE_EXPLORER_SHOT_QUERY=stack                   prefill the filter (results view)
//   CHUDWARE_EXPLORER_SHOT_STRIPS=changes,recent         expand the bottom strips
//   CHUDWARE_EXPLORER_SHOT_VIEW=tiles|tree               force the view (default: automatic)
//   CHUDWARE_EXPLORER_SHOT_TILEDIR=parts                 folder the tiles view shows
//   CHUDWARE_EXPLORER_SHOT_OPEN=asm/tbot_full.step+      open it as a double-click would, once the
//                                                        engine is up; the capture waits for it
static std::string s_shotOpen;          // absolute; cleared once requested
static std::string s_shotOpenTarget;    // absolute; the capture waits for this tab
static void applyShotStateOnce(AppState& st, const std::string& root) {
    static bool applied = false;
    if (applied || root.empty()) return;
    applied = true;
    const char* shot = std::getenv("CHUDWARE_SHOT");
    if (!shot || std::strcmp(shot, "explorer") != 0) return;
    auto list = [](const char* env) {
        std::vector<std::string> out;
        const char* v = std::getenv(env);
        if (!v) return out;
        std::string s = v;
        size_t at = 0;
        while (at <= s.size()) {
            size_t end = s.find(',', at);
            if (end == std::string::npos) end = s.size();
            if (end > at) out.push_back(s.substr(at, end - at));
            at = end + 1;
        }
        return out;
    };
    for (const auto& rel : list("CHUDWARE_EXPLORER_SHOT_EXPAND"))
        expandAncestors(root, pathJoin(root, rel), /*includeSelf=*/true);
    if (const char* dir = std::getenv("CHUDWARE_EXPLORER_SHOT_TILEDIR")) s_tileDir = pathJoin(root, dir);
    for (const auto& rel : list("CHUDWARE_EXPLORER_SHOT_SELECT"))
        revealPath(root, pathJoin(root, rel), false);
    if (const char* q = std::getenv("CHUDWARE_EXPLORER_SHOT_QUERY"))
        std::snprintf(s_query, sizeof(s_query), "%s", q);
    for (const auto& strip : list("CHUDWARE_EXPLORER_SHOT_STRIPS"))
        if (strip == "recent") s_recentOpen = true;
    if (const char* pane = std::getenv("CHUDWARE_EXPLORER_SHOT_PANE"))
        if (std::strcmp(pane, "changes") == 0) st.leftPaneTab = kLeftPaneChanges;
    if (const char* view = std::getenv("CHUDWARE_EXPLORER_SHOT_VIEW"))
        s_viewPref = std::strcmp(view, "tiles") == 0 ? 1 : std::strcmp(view, "tree") == 0 ? 0 : -1;
    if (const char* open = std::getenv("CHUDWARE_EXPLORER_SHOT_OPEN"))
        s_shotOpen = s_shotOpenTarget = pathJoin(root, open);
}

bool fileExplorerShotSettled(const AppState& st) {
    if (fileThumbsBusy() || !s_shotOpen.empty()) return false;
    // CHUDWARE_EXPLORER_SHOT_PANE=changes: the list is the subject, so wait for a vault answer.
    if (st.leftPaneTab == kLeftPaneChanges && !s_plm.ready) return false;
    if (s_shotOpenTarget.empty()) return true;
    if (st.activeTab < 0 || st.activeTab >= (int)st.docTabs.size()) return false;
    return normKey(st.docTabs[st.activeTab].path) == normKey(s_shotOpenTarget) && st.hasDocMesh &&
           !st.rpc.busy() && st.requestOpenPath.empty();
}

// Ghost toolbar button with a drawn glyph, for actions the icon atlas has no picture for.
enum class ToolGlyph { Tiles, List, CollapseAll, Back };
static bool glyphButton(const char* id, ToolGlyph glyph, const char* tooltip, bool enabled, float size) {
    const float S = ui::uiScale;
    const ImVec2 p0 = ImGui::GetCursorScreenPos(), p1(p0.x + size, p0.y + size);
    const bool clicked = ImGui::InvisibleButton(id, ImVec2(size, size)) && enabled;
    const bool hov = ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled);
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const ui::AdaptiveSurface& surf = explorerSurf();
    if (hov && enabled) ui::glassRowBackground(dl, p0, p1, 5.0f, false, false, 0.40f);
    const ImU32 ink = inkWithAlpha(surf.textPrimary, enabled ? 0.92f : 0.35f);
    const float th = std::max(1.0f, 1.4f * S);
    const ImVec2 c((p0.x + p1.x) * 0.5f, (p0.y + p1.y) * 0.5f);
    const float r = size * 0.24f;   // glyph half-extent
    switch (glyph) {
    case ToolGlyph::Tiles: {
        const float g = 1.6f * S, s = r - g * 0.5f;
        for (int i = 0; i < 4; ++i) {
            const float x = (i % 2) ? c.x + g * 0.5f : c.x - g * 0.5f - s;
            const float y = (i / 2) ? c.y + g * 0.5f : c.y - g * 0.5f - s;
            dl->AddRect(ImVec2(x, y), ImVec2(x + s, y + s), ink, 1.5f * S, 0, th);
        }
        break;
    }
    case ToolGlyph::List:
        for (int i = -1; i <= 1; ++i) {
            const float y = c.y + (float)i * r * 0.75f;
            dl->AddCircleFilled(ImVec2(c.x - r, y), th, ink);
            dl->AddLine(ImVec2(c.x - r * 0.45f, y), ImVec2(c.x + r, y), ink, th);
        }
        break;
    case ToolGlyph::CollapseAll:   // two chevrons closing on a centre line
        dl->AddPolyline(std::array<ImVec2, 3>{ImVec2(c.x - r * 0.8f, c.y - r), ImVec2(c.x, c.y - r * 0.35f),
                                              ImVec2(c.x + r * 0.8f, c.y - r)}.data(),
                        3, ink, ImDrawFlags_None, th);
        dl->AddPolyline(std::array<ImVec2, 3>{ImVec2(c.x - r * 0.8f, c.y + r), ImVec2(c.x, c.y + r * 0.35f),
                                              ImVec2(c.x + r * 0.8f, c.y + r)}.data(),
                        3, ink, ImDrawFlags_None, th);
        break;
    case ToolGlyph::Back:   // a left arrow: shaft + open head
        dl->AddLine(ImVec2(c.x - r, c.y), ImVec2(c.x + r, c.y), ink, th);
        dl->AddPolyline(std::array<ImVec2, 3>{ImVec2(c.x - r * 0.15f, c.y - r * 0.85f), ImVec2(c.x - r, c.y),
                                              ImVec2(c.x - r * 0.15f, c.y + r * 0.85f)}.data(),
                        3, ink, ImDrawFlags_None, th);
        break;
    }
    if (hov && tooltip) ImGui::SetTooltip("%s", tooltip);
    return clicked;
}

// ---- header + toolbar ---------------------------------------------------------------
// One line: folder icon + working-folder name (tooltip: full path; right-click: folder actions)
// with the vault branch chip inline at the right. In the tiles view the name becomes the
// breadcrumb of the folder on screen: every segment navigates, the last is the current folder,
// and a Back button right after it goes up one folder.
static void drawHeaderLine(AppState& st, const std::string& root, bool tiles) {
    const float S = ui::uiScale, fs = ImGui::GetFontSize();
    const float h = ImGui::GetFrameHeight();
    const float avail = ImGui::GetContentRegionAvail().x;
    const char* branch = !st.workerStarted ? nullptr
        : !s_plm.branch.empty() ? s_plm.branch.c_str()
        : s_plm.branchKnown ? "No branch" : nullptr;
    const float gap = 4.0f * S;
    const float chipW = branch ? ui::metrics::U(1.5f) * 2.0f + ImGui::CalcTextSize(branch).x : 0.0f;
    const bool canBack = tiles && withinWorkspace(root, s_tileDir) && normKey(s_tileDir) != normKey(root);
    const float backW = canBack ? h + gap : 0.0f;
    const float nameW = std::max(avail - (branch ? chipW + gap : 0.0f) - backW, fs * 2.0f);
    const ImVec2 p0 = ImGui::GetCursorScreenPos();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const ui::AdaptiveSurface& surf = explorerSurf();
    std::string name = pathFilename(root);
    if (name.empty()) name = root;
    const float textX = p0.x + std::min(16.0f * S, fs) + 6.0f * S;
    const float textY = p0.y + (h - fs) * 0.5f;

    // Segments: (label, folder). Only the root outside the tiles view.
    std::vector<std::pair<std::string, std::string>> segs{{name, root}};
    if (tiles && withinWorkspace(root, s_tileDir)) {
        std::string at = root;
        const std::string rel = relativeTo(root, s_tileDir);
        size_t from = 0;
        while (from < rel.size()) {
            size_t end = rel.find('/', from);
            if (end == std::string::npos) end = rel.size();
            if (end > from) {
                at = pathJoin(at, rel.substr(from, end - from));
                segs.emplace_back(rel.substr(from, end - from), at);
            }
            from = end + 1;
        }
    }
    const std::string sep = " / ";
    const float sepW = ImGui::CalcTextSize(sep.c_str()).x;
    // Narrow pane: keep the root and the deepest segments, dropping the middle behind "…".
    size_t firstTail = 1;
    auto widthFrom = [&](size_t tail) {
        float w = ImGui::CalcTextSize(segs[0].first.c_str()).x;
        if (tail > 1) w += sepW + ImGui::CalcTextSize("\xE2\x80\xA6").x;
        for (size_t i = tail; i < segs.size(); ++i) w += sepW + ImGui::CalcTextSize(segs[i].first.c_str()).x;
        return w;
    };
    while (firstTail + 1 < segs.size() && widthFrom(firstTail) > p0.x + nameW - textX) ++firstTail;

    ImGui::Dummy(ImVec2(textX - p0.x, h));   // icon slot; the icon centres on this item's height
    ui::fmTreeIconInArrowSlot(ImVec2(p0.x - ImGui::GetStyle().FramePadding.x, p0.y),
                              "body_folder", "[D]");
    float x = textX;
    const float maxX = p0.x + nameW;
    auto segment = [&](size_t i, bool last) {
        const std::string shown = kit::elide(segs[i].first.c_str(), std::max(maxX - x, fs));
        const float w = ImGui::CalcTextSize(shown.c_str()).x;
        ImGui::SameLine(0, 0);
        ImGui::SetCursorScreenPos(ImVec2(x, p0.y));
        ImGui::PushID((int)i);
        const bool clicked = ImGui::InvisibleButton("##crumb", ImVec2(std::max(w, 1.0f), h));
        const bool hov = ImGui::IsItemHovered();
        const bool nav = tiles && !last;
        const ImU32 ink = ImGui::ColorConvertFloat4ToU32(
            (last || !tiles) ? surf.textPrimary : (hov ? surf.textPrimary : surf.textSecondary));
        dl->AddText(ImVec2(x, textY), ink, shown.c_str());
        if (nav && hov)
            dl->AddLine(ImVec2(x, textY + fs + 1.0f * S), ImVec2(x + w, textY + fs + 1.0f * S), ink,
                        std::max(1.0f, 1.0f * S));
        if (clicked && nav) enterTileDir(segs[i].second);
        if (ImGui::IsItemHovered(ImGuiHoveredFlags_ForTooltip))
            ImGui::SetTooltip(i == 0 ? "Working folder\n%s\nRight-click for folder actions" : "%s",
                              segs[i].second.c_str());
        if (i == 0) {
            ui::pushContextMenuStyle();
            if (ImGui::BeginPopupContextItem("##fx_head_ctx")) {
                rootMenuItems(st, root);
                ImGui::EndPopup();
            }
            ui::popContextMenuStyle();
        }
        ImGui::PopID();
        x += w;
    };
    auto separator = [&](const char* text) {
        dl->AddText(ImVec2(x, textY), ImGui::ColorConvertFloat4ToU32(surf.textSecondary), text);
        x += ImGui::CalcTextSize(text).x;
    };
    segment(0, segs.size() == 1);
    if (firstTail > 1) {
        separator(sep.c_str());
        separator("\xE2\x80\xA6");
    }
    for (size_t i = firstTail; i < segs.size() && x < maxX - fs; ++i) {
        separator(sep.c_str());
        segment(i, i + 1 == segs.size());
    }
    if (canBack) {
        ImGui::SameLine(0, 0);
        ImGui::SetCursorScreenPos(ImVec2(x + gap, p0.y));
        std::string up = pathFilename(pathParent(s_tileDir));
        if (up.empty() || normKey(pathParent(s_tileDir)) == normKey(root)) up = name;
        const std::string tip = "Back to " + up + " (Backspace)";
        if (glyphButton("##fx_back", ToolGlyph::Back, tip.c_str(), true, h)) tileGoUp();
    }
    ImGui::SetCursorScreenPos(ImVec2(p0.x + nameW + backW, p0.y));
    ImGui::Dummy(ImVec2(0, h));
    if (branch) {
        ImGui::SameLine(0, gap);
        const float chipH = ImGui::GetTextLineHeight() + ui::metrics::U(0.5f) * 2.0f;
        ImGui::SetCursorScreenPos(ImVec2(ImGui::GetCursorScreenPos().x, p0.y + (h - chipH) * 0.5f));
        kit::chip(branch, kit::Tone::Neutral, false, "Active vault branch");
    }
}

// Filter field with New folder / Refresh / Collapse all (tree) / Reveal active document / view
// toggle beside it (wrapping under it when the pane is too narrow).
static void drawSearchToolbar(AppState& st, const std::string& root, const std::string& current,
                              const std::string& activePath, bool tiles) {
    const float S = ui::uiScale;
    const float bs = ImGui::GetFrameHeight(), gap = 2.0f * S;
    const int nButtons = tiles ? 4 : 5;
    const float toolW = (float)nButtons * (bs + gap);
    const float avail = ImGui::GetContentRegionAvail().x;
    const bool inline_ = avail - toolW >= ImGui::GetFontSize() * 8.0f;
    ImGui::SetNextItemWidth(inline_ ? avail - toolW : -1.0f);
    if (s_focusSearch) {
        ImGui::SetKeyboardFocusHere();
        s_focusSearch = false;
    }
    ImGui::InputTextWithHint("##fx_search", "Filter files\xE2\x80\xA6 (Ctrl+F)", s_query,
                             sizeof(s_query), ImGuiInputTextFlags_EscapeClearsAll);
    if (inline_) ImGui::SameLine(0, gap);
    const std::string folderName = pathFilename(current).empty() ? current : pathFilename(current);
    const std::string newTip = "New folder in " + folderName;
    if (kit::iconButton("##fx_new_folder", "folder_new", newTip.c_str(), true, kit::Tone::Neutral, bs))
        createNewFolder(st, root, current);
    ImGui::SameLine(0, gap);
    if (kit::iconButton("##fx_refresh", "refresh", "Refresh files and vault status", true,
                        kit::Tone::Neutral, bs)) {
        invalidateDirListCache();
        invalidateExplorerPlm();
    }
    if (!tiles) {   // the tiles view shows one folder: nothing to collapse
        ImGui::SameLine(0, gap);
        if (glyphButton("##fx_collapse", ToolGlyph::CollapseAll, "Collapse all folders",
                        !s_expanded.empty(), bs))
            s_expanded.clear();
    }
    ImGui::SameLine(0, gap);
    const bool canReveal = !activePath.empty() && withinWorkspace(root, activePath) &&
                           normKey(activePath) != normKey(root);
    const char* revealTip = canReveal ? "Reveal active document"
        : activePath.empty() ? "Reveal active document (it has not been saved)"
                             : "Reveal active document (it is outside the working folder)";
    if (kit::iconButton("##fx_reveal", "isolate", revealTip, canReveal, kit::Tone::Neutral, bs))
        revealPath(root, activePath, false);
    ImGui::SameLine(0, gap);
    if (glyphButton("##fx_view", tiles ? ToolGlyph::List : ToolGlyph::Tiles,
                    tiles ? "Show as a tree" : "Show as tiles", true, bs)) {
        s_viewPref = tiles ? 0 : 1;
        if (!tiles) s_tileDir = current;   // tiles open on the folder in context
    }
}

// ---- body -----------------------------------------------------------------
void drawFileExplorerBody(AppState& st) {
    auto& fsb = chudware::fsBackend();
    s_visitedDirs.clear();
    ui::fmRowStripeReset();   // restart the FM stripe phase + refresh the shared FM palette

    // Compact vertical rhythm (the FeatureManager convention): tighten ItemSpacing.y only so
    // rows don't read "double spaced". RAII so it unwinds on any early return.
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
    applyShotStateOnce(st, root);
    if (!s_shotOpen.empty() && st.workerStarted && !st.rpc.busy() && ImGui::GetFrameCount() > 10) {
        requestOpen(st, s_shotOpen);
        s_shotOpen.clear();
    }
    applyPendingFmSwitch(st);
    fileThumbsPump();
    if (s_tileDir.empty() || !withinWorkspace(root, s_tileDir)) s_tileDir = root;
    const bool tiles = s_viewPref == 1 || (s_viewPref == -1 && workspaceHomeActive(st));
    pollExplorerPlm(st, st.plmProjectPath());   // rows' change dots + revisions
    if (s_toggleRecent) s_recentOpen = !s_recentOpen;
    s_toggleRecent = false;
    const std::string activePath = refreshOpenMarkers(st);

    // Capture the pane's bottom before anything is laid out: the tree takes all the height the
    // header, filter row and bottom strips leave.
    const float paneBottom = ImGui::GetCursorScreenPos().y + ImGui::GetContentRegionAvail().y;

    handleExplorerKeys(st, root, tiles);
    // The mouse's Back button (X1) over the pane goes up a folder, like the header's Back.
    if (tiles && s_query[0] == '\0' && normKey(s_tileDir) != normKey(root) &&
        ImGui::IsWindowHovered(ImGuiHoveredFlags_ChildWindows) && ImGui::IsMouseClicked(3))
        tileGoUp();

    std::string current = s_currentFolder.empty() ? root : s_currentFolder;
    const auto currentStat = fsb.pollStat(current);
    if (currentStat.state == chudware::FsReadState::Ready && !currentStat.value.isDir)
        current = pathParent(current);
    if (current.empty() || !withinWorkspace(root, current)) current = root;
    if (tiles) current = s_tileDir;   // New Folder / Paste act on the folder on screen

    drawHeaderLine(st, root, tiles);
    drawSearchToolbar(st, root, current, activePath, tiles);

    const std::string query = trimmedQuery();
    stepSearch(root, query);
    s_selKey = normKey(s_selectedPath);

    const float sp = ImGui::GetStyle().ItemSpacing.y;
    const float reserve = recentStripHeight(st);
    const float treeH = std::max(pillRowH() * 3.0f,
                                 paneBottom - ImGui::GetCursorScreenPos().y - reserve - sp);
    s_rowsThisFrame.clear();
    ImGui::BeginChild("##fx_tree", ImVec2(0, treeH), ImGuiChildFlags_None,
                      ImGuiWindowFlags_NoScrollbar);
    // The scrollbar is an overlay inside this child. Reserve its hit lane so
    // rows, selection outlines and revision labels never sit underneath it.
    ImGuiWindow* treeWindow = ImGui::GetCurrentWindow();
    const float workRight = treeWindow->WorkRect.Max.x;
    const float contentRight = treeWindow->ContentRegionRect.Max.x;
    treeWindow->WorkRect.Max.x -= ui::metrics::px(14.0f);
    treeWindow->ContentRegionRect.Max.x -= ui::metrics::px(14.0f);
    if (tiles) drawTilesView(st, root, query);
    else if (!query.empty()) drawSearchResults(st, root);
    else drawDirChildren(st, root, root, 0);
    // Right-click on empty space acts on the working folder itself.
    ui::pushContextMenuStyle();
    if (ImGui::BeginPopupContextWindow("##fx_tree_ctx",
                                       ImGuiPopupFlags_MouseButtonRight | ImGuiPopupFlags_NoOpenOverItems)) {
        rootMenuItems(st, root);
        ImGui::EndPopup();
    }
    ui::popContextMenuStyle();
    treeWindow->WorkRect.Max.x = workRight;
    treeWindow->ContentRegionRect.Max.x = contentRight;
    ui::drawWindowPillScrollbarV(explorerSurf(), ui::PillScrollbarStyle{5.0f, false});
    ImGui::EndChild();
    s_rowsLastFrame.swap(s_rowsThisFrame);

    drawRecentStrip(st);

    drawDeleteModal(st, root);
    for (auto it = s_dirListCache.begin(); it != s_dirListCache.end();) {
        if (!s_visitedDirs.count(it->first)) it = s_dirListCache.erase(it);
        else ++it;
    }
    fsb.pruneBrowseCache();
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
