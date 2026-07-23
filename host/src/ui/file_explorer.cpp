// File Explorer panel (Wave 3, GUI rework) — see file_explorer.h.
//
// Workspace file browser for the left task pane's "Explorer" tab. Renders a "Recent" section
// (AppState::recentFiles) plus a recursive, lazy-expand directory tree rooted at the workspace dir
// (AppState::workspaceDir, default <Documents>/Chudware), filtered to known CAD/ECAD extensions.
//
// Double-click opens a file (CAD -> featureTreeOpenPath via st.requestOpenPath; ECAD/KiCad files
// route through the same request flag and app.cpp's pump dispatches them to the ECAD open path).
// Right-click yields Open / Rename / Copy / Paste / Delete / New Folder. F2 renames inline,
// Del deletes (with a confirm modal). All filesystem mutations are clamped to the workspace root.
//
// Filesystem ops go through chudware::FsBackend (desktop = std::filesystem; web = gateway
// /workspace/*) so the WASM client browses the server workspace.
#include <algorithm>
#include <cctype>
#include <cstdio>
#include <cstdlib>
#include <cstdint>
#include <map>
#include <string>
#include <vector>

#include "imgui.h"
#include "imgui_internal.h"  // ImRect (ui::adaptivePanel rect argument)

#include "file_explorer.h"
#include "dragdrop.h"       // kFileDragPayload (Explorer drag source -> viewport drop target)
#include "feature_tree.h"   // featureTreeOpenPath / featureTreeActivePath
#include "icons.h"          // getIcon / IconUV
#include "layout.h"         // kLeftPaneExplorer

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
static std::string s_clipboardPath;    // Explorer copy/paste stash (a file path)
static std::string s_pendingNewFolder; // a just-created folder to auto-enter inline-rename next frame
// Recent-row existence cache: fs exists per entry per frame is real filesystem traffic while the
// Recent list is visible. Refresh at most every ~2s; a path not yet cached (a just-added recent)
// is probed once on first sight, so a list change still paints correctly the same frame.
static std::map<std::string, bool> s_recentExists;
static double s_recentExistsAt = -1e9;
// Directory listing cache: drawDirNode used to call fsb.list() every frame for every open
// folder. Cache the filtered/sorted children for a short TTL (and bust on mutations / Refresh)
// so mtime-sorted files don't imply per-frame FS traffic. mtime comes from FsEntry (filled once
// per list()) — never a separate last_write_time per row.
struct DirListCache {
    double scannedAt = -1e9;
    std::vector<std::string> dirs;   // full paths, name-sorted (case-insensitive)
    std::vector<std::string> files;  // full paths, mtime-desc (unknown mtime = oldest)
};
static std::map<std::string, DirListCache> s_dirListCache;
static constexpr double kDirListCacheTtlSec = 2.0;

static void invalidateDirListCache() { s_dirListCache.clear(); }

// Hover tooltip: full file/dir name, plus absolute path on a second line when it adds context.
static void itemPathTooltip(const std::string& name, const std::string& fullPath) {
    if (!ImGui::IsItemHovered()) return;
    if (fullPath.empty() || fullPath == name)
        ImGui::SetTooltip("%s", name.c_str());
    else
        ImGui::SetTooltip("%s\n%s", name.c_str(), fullPath.c_str());
}


// ---- known extensions ----------------------------------------------------
// .step+ is our native part container; the rest are import/interop formats. ECAD/KiCad files open
// through the ECAD path. Lower-cased compare.
static bool isCadExt(const std::string& e) {
    return e == ".step+" || e == ".step" || e == ".stp" || e == ".iges" || e == ".igs" ||
           e == ".stl"   || e == ".3mf";
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
    if (st.workspaceDir.empty()) {
        // Prefer the persisted setting, else the backend default (desktop Documents/Chudware
        // or Documents/Chudware; web = server workspace abs root).
        if (!st.settings.workspaceDir.empty()) {
            st.workspaceDir = st.settings.workspaceDir;
        } else {
            st.workspaceDir = fsb.defaultRoot();
        }
    }
    std::string err;
    fsb.mkdir(st.workspaceDir, err);   // idempotent; ignore "already exists"
    if (s_currentFolder.empty()) s_currentFolder = st.workspaceDir;
    // Root-change detection: Settings -> General's "Workspace folder" Apply resets st.workspaceDir
    // (possibly to empty = re-resolve default, handled above) while the app is live. Re-root the
    // browser to the new workspace — without this, s_currentFolder keeps showing the old tree.
    static std::string s_lastRoot;
    if (st.workspaceDir != s_lastRoot) {
        s_lastRoot = st.workspaceDir;
        s_currentFolder = st.workspaceDir;
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
    if (fsb.exists(newP)) { st.toast(ui::ToastLevel::Warn, "A file with that name exists"); return; }
    if (!fsb.rename(oldP, newP, err)) {
        st.toast(ui::ToastLevel::Error, "Rename failed: " + err);
    } else {
        invalidateDirListCache();
        st.addLog("[explorer] renamed " + pathFilename(oldP) + " -> " + newName);
        if (s_selectedPath == oldP) s_selectedPath = newP;
    }
}

// ---- copy / paste (filesystem) -------------------------------------------
// Build a collision-free destination by appending " (copy)" (then " (copy 2)", ...) before the
// extension until the name is free.
static std::string uniqueDest(const std::string& dir, const std::string& src) {
    auto& fsb = chudware::fsBackend();
    std::string fname = pathFilename(src);
    std::string stem = pathStem(fname);
    std::string ext  = pathExtension(fname);
    std::string cand = pathJoin(dir, fname);
    if (!fsb.exists(cand)) return cand;
    for (int n = 1; n < 1000; ++n) {
        std::string suffix = (n == 1) ? " (copy)" : (" (copy " + std::to_string(n) + ")");
        cand = pathJoin(dir, stem + suffix + ext);
        if (!fsb.exists(cand)) return cand;
    }
    return cand;
}

static bool doPaste(AppState& st, const std::string& root, const std::string& destDir) {
    auto& fsb = chudware::fsBackend();
    if (s_clipboardPath.empty()) { st.toast(ui::ToastLevel::Info, "Nothing copied"); return false; }
    std::string src = s_clipboardPath;
    if (!fsb.exists(src)) { st.toast(ui::ToastLevel::Warn, "Copied file no longer exists"); return false; }
    if (!withinWorkspace(root, destDir)) { st.toast(ui::ToastLevel::Warn, "Paste blocked outside the workspace"); return false; }
    std::string dest = uniqueDest(destDir, src);
    std::string err;
    const bool isDir = fsb.isDirectory(src);
    if (!fsb.copy(src, dest, /*recursive=*/isDir, err)) {
        st.toast(ui::ToastLevel::Error, "Paste failed: " + err);
        return false;
    }
    st.addLog("[explorer] pasted -> " + dest);
    invalidateDirListCache();

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
    while (fsb.exists(cand)) cand = pathJoin(dir, "New Folder " + std::to_string(++n));
    std::string err;
    if (fsb.mkdir(cand, err)) {
        s_pendingNewFolder = cand;
        s_currentFolder = dir;
        st.addLog("[explorer] new folder " + cand);
        invalidateDirListCache();

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

// ---- tree node rendering --------------------------------------------------
// A file row: icon + name, selectable, double-click opens, F2/Del/context-menu act on it.
static void drawFileRow(AppState& st, const std::string& root, const std::string& p,
                        const std::string& ext) {
    const std::string& pathStr = p;
    const bool isRenaming = (s_renameId == pathStr);
    const bool selected = (s_selectedPath == pathStr);
    const std::string fname = pathFilename(p);
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
        ImGui::TreeNodeEx(fname.c_str(),
                          ImGuiTreeNodeFlags_Leaf | ImGuiTreeNodeFlags_NoTreePushOnOpen |
                          ImGuiTreeNodeFlags_SpanFullWidth | ImGuiTreeNodeFlags_FramePadding);
        ui::fmTreeIconInArrowSlot(rowPos, fileIconName(ext), fileIconFallback(ext));
        ui::fmRowPillEnd(rr, selected);
        if (ImGui::IsItemClicked()) {
            s_selectedPath = pathStr;
            s_currentFolder = pathParent(p);
            if (ImGui::IsMouseDoubleClicked(ImGuiMouseButton_Left)) requestOpen(st, p);
        }
        // Drag SOURCE: dragging a known-extension row onto the viewport opens it (the viewport is the
        // matching drop target). The payload is the absolute path INCLUDING its NUL terminator so the
        // target can read it straight back as a C string. Drag preview = the filename.
        if (isKnownExt(ext) && ImGui::BeginDragDropSource(ImGuiDragDropFlags_SourceAllowNullID)) {
            ImGui::SetDragDropPayload(kFileDragPayload, pathStr.c_str(), pathStr.size() + 1);
            ImGui::TextUnformatted(fname.c_str());
            ImGui::EndDragDropSource();
        }
        // context menu
        if (ImGui::BeginPopupContextItem("##fctx")) {
            s_selectedPath = pathStr;
            s_currentFolder = pathParent(p);
            if (ImGui::MenuItem("Open"))   requestOpen(st, p);
            ImGui::Separator();
            if (ImGui::MenuItem("Rename", "F2")) {
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
        itemPathTooltip(fname, pathStr);
    }
    ImGui::PopID();
}

// Recursively draw a directory: a tree node that lazy-lists its children on open. Folders first
// (name-sorted), then known files (mtime-desc). Hidden/dot entries and unknown extensions are
// skipped. Children come from s_dirListCache (TTL + mutation bust) so open folders don't re-stat
// every frame.
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
    const bool dirSelected = (s_selectedPath == dir);

    ImGui::PushID(dir.c_str());

    // A non-root folder being inline-renamed shows an InputText in place of its tree node; its
    // children stay collapsed during the rename (kept simple — folder rename is a leaf interaction).
    if (!isRoot && s_renameId == dir) {
        ui::FmRowRect rr = ui::fmRowPill(dirSelected);
        ui::fmTreeIcon("body_folder", "[D]");
        ImGui::SameLine(0, 4.0f * ui::uiScale);
        ImGui::SetNextItemWidth(-1);
        if (s_renameFocus) { ImGui::SetKeyboardFocusHere(); s_renameFocus = false; }
        if (ImGui::InputText("##rnd", s_renameBuf, sizeof(s_renameBuf),
                             ImGuiInputTextFlags_EnterReturnsTrue | ImGuiInputTextFlags_AutoSelectAll))
            commitRename(st, root);
        if (ImGui::IsItemDeactivated()) commitRename(st, root);
        ui::fmRowPillEnd(rr, dirSelected);
        ImGui::PopID();
        return;
    }

    // FM expandable-row pattern (identical to the FeatureManager Origin row): pill, 16px icon as
    // its own item, then the chevron node. Pill ends right after the node, BEFORE children.
    ui::FmRowRect rr = ui::fmRowPill(dirSelected);
    ui::fmTreeIcon("body_folder", "[D]");
    ImGui::SameLine(0, 4.0f * ui::uiScale);
    bool open = ImGui::TreeNodeEx(label.c_str(), flags);
    ui::fmRowPillEnd(rr, dirSelected);
    if (ImGui::IsItemClicked() && !ImGui::IsItemToggledOpen()) {
        s_selectedPath = dir;
        s_currentFolder = dir;
    }
    itemPathTooltip(label, dir);
    // folder context menu (New Folder / Paste / Rename / Delete — root only allows New Folder/Paste)
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
                s_renameId = dir;
                std::snprintf(s_renameBuf, sizeof(s_renameBuf), "%s", pathFilename(dir).c_str());
                s_renameFocus = true;
            }
            if (ImGui::MenuItem("Delete", "Del")) s_pendingDelete = dir;
        }
        ImGui::EndPopup();
    }

    if (open) {
        // Cache-aware gather + sort: dirs by name, files by mtime desc (FsEntry.mtime from list()).
        const double now = ImGui::GetTime();
        auto cacheIt = s_dirListCache.find(dir);
        if (cacheIt == s_dirListCache.end() ||
            (now - cacheIt->second.scannedAt) > kDirListCacheTtlSec) {
            DirListCache fresh;
            fresh.scannedAt = now;
            std::string listErr;
            auto entries = fsb.list(dir, listErr);
            std::vector<std::pair<std::string, int64_t>> filePairs;  // path, mtime
            for (const auto& e : entries) {
                if (e.name.empty() || e.name[0] == '.') continue;
                std::string full = pathJoin(dir, e.name);
                if (e.isDir) {
                    fresh.dirs.push_back(std::move(full));
                } else if (isKnownExt(lowerExt(e.name))) {
                    // mtime==0 (unknown/error) sorts as oldest per contract.
                    filePairs.emplace_back(std::move(full), e.mtime);
                }
            }
            auto byName = [](const std::string& a, const std::string& b) {
                std::string an = pathFilename(a), bn = pathFilename(b);
                std::transform(an.begin(), an.end(), an.begin(), ::tolower);
                std::transform(bn.begin(), bn.end(), bn.begin(), ::tolower);
                return an < bn;
            };
            std::sort(fresh.dirs.begin(), fresh.dirs.end(), byName);
            // Newest first; tie-break by name for stable order when mtimes match.
            std::sort(filePairs.begin(), filePairs.end(),
                      [&](const std::pair<std::string, int64_t>& a,
                          const std::pair<std::string, int64_t>& b) {
                          if (a.second != b.second) return a.second > b.second;
                          return byName(a.first, b.first);
                      });
            fresh.files.reserve(filePairs.size());
            for (auto& fp : filePairs) fresh.files.push_back(std::move(fp.first));
            cacheIt = s_dirListCache.insert_or_assign(dir, std::move(fresh)).first;
        }
        const auto& dirs = cacheIt->second.dirs;
        const auto& files = cacheIt->second.files;
        for (const auto& d : dirs) drawDirNode(st, root, d, /*isRoot=*/false);
        for (const auto& f : files) drawFileRow(st, root, f, lowerExt(f));
        if (dirs.empty() && files.empty())
            ImGui::TextDisabled("  (empty)");
        ImGui::TreePop();
    }
    ImGui::PopID();
}

// ---- Recent section -------------------------------------------------------
static void drawRecentSection(AppState& st) {
    if (st.recentFiles.empty()) return;
    auto& fsb = chudware::fsBackend();
    // FM-style section rule (the "Reference"/"Features" treatment) instead of a CollapsingHeader.
    ui::fmSectionDivider("Recent");
    if (ImGui::GetTime() - s_recentExistsAt > 2.0) {
        s_recentExists.clear();
        s_recentExistsAt = ImGui::GetTime();
    }
    int idx = 0;
    for (const std::string& rp : st.recentFiles) {
        ImGui::PushID(idx++);
        auto exIt = s_recentExists.find(rp);
        if (exIt == s_recentExists.end()) {
            exIt = s_recentExists.emplace(rp, fsb.exists(rp)).first;
        }
        const bool exists = exIt->second;
        const std::string ext = lowerExt(rp);
        const std::string fname = pathFilename(rp);
        // Same FM leaf-row pattern as the tree's file rows; a missing file keeps a readable dim
        // state (dim text + faded icon), like the FM's hidden planes.
        ui::FmRowRect rr = ui::fmRowPill(false);
        ImVec2 rowPos = ImGui::GetCursorScreenPos();
        if (!exists) ImGui::PushStyleColor(ImGuiCol_Text, explorerSurf().textSecondary);
        ImGui::TreeNodeEx(fname.c_str(),
                          ImGuiTreeNodeFlags_Leaf | ImGuiTreeNodeFlags_NoTreePushOnOpen |
                          ImGuiTreeNodeFlags_SpanFullWidth | ImGuiTreeNodeFlags_FramePadding);
        if (!exists) ImGui::PopStyleColor();
        ui::fmTreeIconInArrowSlot(rowPos, fileIconName(ext), fileIconFallback(ext),
                                  exists ? 1.0f : 0.45f);
        ui::fmRowPillEnd(rr, false);
        if (ImGui::IsItemClicked() && ImGui::IsMouseDoubleClicked(ImGuiMouseButton_Left) && exists)
            requestOpen(st, rp);
        // Drag a Recent row onto the viewport to open it (same payload as the tree rows).
        if (exists && isKnownExt(ext) &&
            ImGui::BeginDragDropSource(ImGuiDragDropFlags_SourceAllowNullID)) {
            ImGui::SetDragDropPayload(kFileDragPayload, rp.c_str(), rp.size() + 1);
            ImGui::TextUnformatted(fname.c_str());
            ImGui::EndDragDropSource();
        }
        itemPathTooltip(fname, rp);
        ImGui::PopID();
    }
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
        if (fsb.isDirectory(p)) ImGui::TextDisabled("This folder and its contents.");
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

    // A just-created folder enters inline rename on the next frame (after the tree expands it).
    if (!s_pendingNewFolder.empty()) {
        s_renameId = s_pendingNewFolder;
        std::snprintf(s_renameBuf, sizeof(s_renameBuf), "%s",
                      pathFilename(s_pendingNewFolder).c_str());
        s_renameFocus = true;
        s_selectedPath = s_pendingNewFolder;
        s_pendingNewFolder.clear();
    }

    std::string current = s_currentFolder.empty() ? root : s_currentFolder;
    if (!fsb.isDirectory(current)) current = pathParent(current);
    if (current.empty() || !withinWorkspace(root, current)) current = root;

    const bool selectedFile = !s_selectedPath.empty() &&
        fsb.isRegularFile(s_selectedPath) &&
        isKnownExt(lowerExt(s_selectedPath));

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
            st.toast(ui::ToastLevel::Info, "Explorer refreshed");
        }
        ImGui::SetCursorScreenPos(ImVec2(toolRowMin.x, toolRowMin.y + bw));
        ImGui::Dummy(ImVec2(0, 0));
        ImGui::Spacing();
    }

    // Breadcrumb: the current folder, in an FM row pill (secondary ink, frame-padded centering).
    {
        std::string folderName = pathFilename(current);
        if (folderName.empty()) folderName = current;
        ui::FmRowRect bc = ui::fmRowPill(false);
        ImGui::SetCursorScreenPos(ImVec2(bc.mn.x + 6.0f * ui::uiScale, bc.mn.y));
        ImGui::AlignTextToFramePadding();
        ImGui::PushStyleColor(ImGuiCol_Text, explorerSurf().textSecondary);
        ImGui::TextUnformatted(folderName.c_str());
        ImGui::PopStyleColor();
        ui::fmRowPillEnd(bc, false);
        if (ImGui::IsItemHovered()) ImGui::SetTooltip("%s", current.c_str());
    }

    drawRecentSection(st);

    // Keyboard: F2 rename / Del delete on the current selection (only when this pane owns nav and no
    // text field is focused — the inline-rename InputText sets WantTextInput, so this won't fire mid
    // rename).
    if (!s_selectedPath.empty() && !ImGui::GetIO().WantTextInput &&
        ImGui::IsWindowFocused(ImGuiFocusedFlags_RootAndChildWindows)) {
        if (ImGui::IsKeyPressed(ImGuiKey_F2, false)) {
            s_renameId = s_selectedPath;
            std::snprintf(s_renameBuf, sizeof(s_renameBuf), "%s",
                          pathFilename(s_selectedPath).c_str());
            s_renameFocus = true;
        } else if (ImGui::IsKeyPressed(ImGuiKey_Delete, false)) {
            s_pendingDelete = s_selectedPath;
        }
    }

    ui::fmSectionDivider("Workspace");
    ImGui::BeginChild("##fx_tree", ImVec2(0, 0), ImGuiChildFlags_None);
    if (fsb.exists(root))
        drawDirNode(st, root, root, /*isRoot=*/true);
    else
        ImGui::TextDisabled("Workspace folder missing");
    ImGui::EndChild();

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
