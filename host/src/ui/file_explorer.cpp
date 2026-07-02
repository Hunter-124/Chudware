// File Explorer panel (Wave 3, GUI rework) — see file_explorer.h.
//
// Workspace file browser for the left task pane's "Explorer" tab. Renders a "Recent" section
// (AppState::recentFiles) plus a recursive, lazy-expand directory tree rooted at the workspace dir
// (AppState::workspaceDir, default <Documents>/AI-CAD), filtered to known CAD/ECAD extensions.
//
// Double-click opens a file (CAD -> featureTreeOpenPath via st.requestOpenPath; ECAD/KiCad files
// route through the same request flag and app.cpp's pump dispatches them to the ECAD open path).
// Right-click yields Open / Rename / Copy / Paste / Delete / New Folder. F2 renames inline,
// Del deletes (with a confirm modal). All filesystem mutations are clamped to the workspace root.
#include <algorithm>
#include <cctype>
#include <cstdlib>
#include <filesystem>
#include <string>
#include <system_error>
#include <vector>

#include "imgui.h"
#include "imgui_internal.h"  // ImRect (ui::adaptivePanel rect argument)

#include "file_explorer.h"
#include "dragdrop.h"       // kFileDragPayload (Explorer drag source -> viewport drop target)
#include "feature_tree.h"   // featureTreeOpenPath / featureTreeActivePath
#include "icons.h"          // getIcon / IconUV
#include "layout.h"         // kLeftPaneExplorer

#include "../core/app_state.h"
#include "../ui_theme.h"    // ui::uiScale, ui::palette

namespace fs = std::filesystem;

namespace ui {

// ---- module state (single Explorer instance) ----
static int s_explorerStripe = 0;
static void explorerRowPill(bool selected = false) {
    ui::glassRowAtCursor(3.5f, (s_explorerStripe++ % 2) == 0, selected, 5.0f);
}

// Liquid glass v2 (host contract §H4): fetch (never paint) the left pane's ONE glass surface —
// layout.cpp's drawLeftPane already painted it this frame under "host_left_glass"; adaptivePanel
// is idempotent per frame (A2), so this is a cheap cached lookup regardless of the rect passed
// (window pos/size here is this child's rect, not the whole pane — irrelevant after the first
// same-frame call).
static const ui::AdaptiveSurface& explorerSurf() {
    ImVec2 wp_ = ImGui::GetWindowPos(); ImVec2 ws_ = ImGui::GetWindowSize();
    ImRect r(wp_, ImVec2(wp_.x + ws_.x, wp_.y + ws_.y));
    return ui::adaptivePanel(ui::panelId("host_left_glass"), r);
}
// Contrast halo for drawIconGlass: surf.tint is already the opposite-tone neutral (see
// ui_theme.cpp adaptiveSurface) at ~0x50 alpha; 0 alpha (no live backdrop) => drawIconGlass
// draws plain (icons.h contract).
static ImU32 explorerHalo(const ui::AdaptiveSurface& surf) {
    if (!ui::viewportBackdrop().valid()) return 0;
    return ImGui::ColorConvertFloat4ToU32(ImVec4(surf.tint.x, surf.tint.y, surf.tint.z, 0x50 / 255.f));
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

// Lower-cased extension of a path, INCLUDING the leading dot. Handles our double-dot ".step+" /
// ".ecad+" (fs::path::extension stops at the last dot, so "x.step+" -> ".step+" is fine; but
// "x.kicad_sch" -> ".kicad_sch" too). Returns "" when there is no extension.
static std::string lowerExt(const fs::path& p) {
    std::string e = p.extension().string();
    std::transform(e.begin(), e.end(), e.begin(),
                   [](unsigned char c) { return (char)std::tolower(c); });
    return e;
}

// True when `child` is inside (or equal to) `root` — the guard for every destructive op so the
// Explorer can never rename/delete/paste outside the workspace. Compares lexically-normal paths.
static bool withinWorkspace(const fs::path& root, const fs::path& child) {
    std::error_code ec;
    fs::path r = fs::weakly_canonical(root, ec);
    if (ec) r = root.lexically_normal();
    fs::path c = fs::weakly_canonical(child, ec);
    if (ec) c = child.lexically_normal();
    auto rs = r.string(), cs = c.string();
    if (cs.size() < rs.size()) return false;
    // case-insensitive prefix (Windows)
    for (size_t i = 0; i < rs.size(); ++i)
        if (std::tolower((unsigned char)cs[i]) != std::tolower((unsigned char)rs[i])) return false;
    return cs.size() == rs.size() || cs[rs.size()] == '\\' || cs[rs.size()] == '/';
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

// Draw a 16px icon inline (or nothing if the atlas lacks it) and advance the cursor. Tint tracks
// the pane's adaptive surface (max contrast over the frosted backdrop); drawIconGlass adds the
// opposite-tone contrast halo (host contract §H4).
static void drawIcon(const char* name) {
    const float sz = 16.0f * ui::uiScale;
    IconUV ic = getIcon(name, 20);
    if (ic.valid) {
        const ui::AdaptiveSurface& surf = explorerSurf();
        ImVec2 p0 = ImGui::GetCursorScreenPos();
        ImGui::Dummy(ImVec2(sz, sz));
        drawIconGlass(ImGui::GetWindowDrawList(), name, ImVec2(p0.x + sz * 0.5f, p0.y + sz * 0.5f),
                     sz, ImGui::ColorConvertFloat4ToU32(surf.textSecondary), explorerHalo(surf));
        ImGui::SameLine(0, 4.0f * ui::uiScale);
    }
}

static bool toolbarIconButton(const char* id, const char* icon, const char* tip, bool enabled = true) {
    const float S = ui::uiScale;
    const float btn = 28.0f * S;
    const float glyph = 18.0f * S;
    ImVec2 p0 = ImGui::GetCursorScreenPos();
    ImVec2 p1(p0.x + btn, p0.y + btn);

    ImGui::PushID(id);
    if (!enabled) ImGui::BeginDisabled();
    bool clicked = ImGui::InvisibleButton("##fx_tool", ImVec2(btn, btn));
    bool hovered = ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled);
    if (!enabled) ImGui::EndDisabled();

    ImDrawList* dl = ImGui::GetWindowDrawList();
    if (hovered && enabled)
        ui::glassRowBackground(dl, p0, p1, 5.0f, false, false);
    IconUV ic = getIcon(icon, 20);
    if (ic.valid) {
        const ui::AdaptiveSurface& surf = explorerSurf();
        ImVec4 tintV = enabled ? surf.textPrimary
                               : ImVec4(surf.textTertiary.x, surf.textTertiary.y,
                                        surf.textTertiary.z, surf.textTertiary.w * 0.35f);
        drawIconGlass(dl, icon, ImVec2(p0.x + btn * 0.5f, p0.y + btn * 0.5f), glyph,
                     ImGui::ColorConvertFloat4ToU32(tintV), explorerHalo(surf));
    }
    if (hovered) ImGui::SetTooltip("%s", tip);
    ImGui::PopID();
    return clicked && enabled;
}

// ---- workspace resolution ------------------------------------------------
void ensureWorkspaceDir(AppState& st) {
    if (st.workspaceDir.empty()) {
        // Prefer the persisted setting, else <USERPROFILE>/Documents/AI-CAD, else appdata.
        if (!st.settings.workspaceDir.empty()) {
            st.workspaceDir = st.settings.workspaceDir;
        } else {
#ifdef _WIN32
            const char* up = std::getenv("USERPROFILE");
            fs::path base = up && *up ? fs::path(up) / "Documents" : fs::path(appdataDir());
#else
            const char* home = std::getenv("HOME");
            fs::path base = home && *home ? fs::path(home) / "Documents" : fs::path(appdataDir());
#endif
            st.workspaceDir = (base / "AI-CAD").string();
        }
    }
    std::error_code ec;
    fs::create_directories(st.workspaceDir, ec);   // idempotent; ignore "already exists"
    if (s_currentFolder.empty()) s_currentFolder = st.workspaceDir;
}

// ---- inline rename commit -------------------------------------------------
static void commitRename(AppState& st, const fs::path& root) {
    if (s_renameId.empty()) return;
    fs::path oldP = s_renameId;
    std::string newName = s_renameBuf;
    // trim
    while (!newName.empty() && (newName.front() == ' ')) newName.erase(newName.begin());
    while (!newName.empty() && (newName.back()  == ' ')) newName.pop_back();
    s_renameId.clear();
    if (newName.empty() || newName == oldP.filename().string()) return;
    if (newName.find_first_of("\\/:*?\"<>|") != std::string::npos) {
        st.toast(ui::ToastLevel::Warn, "Invalid characters in name");
        return;
    }
    fs::path newP = oldP.parent_path() / newName;
    if (!withinWorkspace(root, oldP) || !withinWorkspace(root, newP)) {
        st.toast(ui::ToastLevel::Warn, "Rename blocked outside the workspace");
        return;
    }
    std::error_code ec;
    if (fs::exists(newP, ec)) { st.toast(ui::ToastLevel::Warn, "A file with that name exists"); return; }
    fs::rename(oldP, newP, ec);
    if (ec) st.toast(ui::ToastLevel::Error, "Rename failed: " + ec.message());
    else {
        st.addLog("[explorer] renamed " + oldP.filename().string() + " -> " + newName);
        if (s_selectedPath == oldP.string()) s_selectedPath = newP.string();
    }
}

// ---- copy / paste (filesystem) -------------------------------------------
// Build a collision-free destination by appending " (copy)" (then " (copy 2)", ...) before the
// extension until the name is free.
static fs::path uniqueDest(const fs::path& dir, const fs::path& src) {
    std::string stem = src.stem().string();
    std::string ext  = src.extension().string();
    fs::path cand = dir / src.filename();
    std::error_code ec;
    if (!fs::exists(cand, ec)) return cand;
    for (int n = 1; n < 1000; ++n) {
        std::string suffix = (n == 1) ? " (copy)" : (" (copy " + std::to_string(n) + ")");
        cand = dir / (stem + suffix + ext);
        if (!fs::exists(cand, ec)) return cand;
    }
    return cand;
}

static bool doPaste(AppState& st, const fs::path& root, const fs::path& destDir) {
    if (s_clipboardPath.empty()) { st.toast(ui::ToastLevel::Info, "Nothing copied"); return false; }
    fs::path src = s_clipboardPath;
    std::error_code ec;
    if (!fs::exists(src, ec)) { st.toast(ui::ToastLevel::Warn, "Copied file no longer exists"); return false; }
    if (!withinWorkspace(root, destDir)) { st.toast(ui::ToastLevel::Warn, "Paste blocked outside the workspace"); return false; }
    fs::path dest = uniqueDest(destDir, src);
    if (fs::is_directory(src, ec))
        fs::copy(src, dest, fs::copy_options::recursive, ec);
    else
        fs::copy_file(src, dest, fs::copy_options::none, ec);
    if (ec) { st.toast(ui::ToastLevel::Error, "Paste failed: " + ec.message()); return false; }
    st.addLog("[explorer] pasted -> " + dest.string());
    st.toast(ui::ToastLevel::Info, "Pasted " + dest.filename().string());
    s_selectedPath = dest.string();
    return true;
}

static void createNewFolder(AppState& st, const fs::path& root, const fs::path& dir) {
    if (!withinWorkspace(root, dir)) {
        st.toast(ui::ToastLevel::Warn, "New Folder blocked outside the workspace");
        return;
    }
    std::error_code e2;
    int n = 1;
    fs::path cand = dir / "New Folder";
    while (fs::exists(cand, e2)) cand = dir / ("New Folder " + std::to_string(++n));
    if (fs::create_directory(cand, e2)) {
        s_pendingNewFolder = cand.string();
        s_currentFolder = dir.string();
        st.addLog("[explorer] new folder " + cand.string());
    } else {
        st.toast(ui::ToastLevel::Error, "New Folder failed");
    }
}

// ---- open dispatch --------------------------------------------------------
// Route a double-clicked file to the right open path. CAD + ECAD both go through st.requestOpenPath;
// app.cpp's request pump inspects the extension and dispatches (CAD -> featureTreeOpenPath, ECAD ->
// the ECAD open path). Keeping the actual open on the GL-thread pump avoids re-entrant doc swaps
// mid-tree-walk.
static void requestOpen(AppState& st, const fs::path& p) {
    std::string e = lowerExt(p);
    if (!isKnownExt(e)) return;
    st.requestOpenPath = p.string();
}

// ---- tree node rendering --------------------------------------------------
// A file row: icon + name, selectable, double-click opens, F2/Del/context-menu act on it.
static void drawFileRow(AppState& st, const fs::path& root, const fs::path& p, const std::string& ext) {
    const std::string pathStr = p.string();
    const bool isRenaming = (s_renameId == pathStr);
    const bool selected = (s_selectedPath == pathStr);
    ImGui::PushID(pathStr.c_str());
    explorerRowPill(selected);
    drawIcon(fileIconName(ext));
    if (isRenaming) {
        ImGui::SetNextItemWidth(-1);
        if (s_renameFocus) { ImGui::SetKeyboardFocusHere(); s_renameFocus = false; }
        if (ImGui::InputText("##rn", s_renameBuf, sizeof(s_renameBuf),
                             ImGuiInputTextFlags_EnterReturnsTrue | ImGuiInputTextFlags_AutoSelectAll))
            commitRename(st, root);
        if (ImGui::IsItemDeactivated()) commitRename(st, root);   // click-away / Esc commits-or-cancels
    } else {
        if (ImGui::Selectable(p.filename().string().c_str(), selected,
                              ImGuiSelectableFlags_AllowDoubleClick)) {
            s_selectedPath = pathStr;
            s_currentFolder = p.parent_path().string();
            if (ImGui::IsMouseDoubleClicked(ImGuiMouseButton_Left)) requestOpen(st, p);
        }
        // Drag SOURCE: dragging a known-extension row onto the viewport opens it (the viewport is the
        // matching drop target). The payload is the absolute path INCLUDING its NUL terminator so the
        // target can read it straight back as a C string. Drag preview = the filename.
        if (isKnownExt(ext) && ImGui::BeginDragDropSource(ImGuiDragDropFlags_SourceAllowNullID)) {
            ImGui::SetDragDropPayload(kFileDragPayload, pathStr.c_str(), pathStr.size() + 1);
            ImGui::TextUnformatted(p.filename().string().c_str());
            ImGui::EndDragDropSource();
        }
        // context menu
        if (ImGui::BeginPopupContextItem("##fctx")) {
            s_selectedPath = pathStr;
            s_currentFolder = p.parent_path().string();
            if (ImGui::MenuItem("Open"))   requestOpen(st, p);
            ImGui::Separator();
            if (ImGui::MenuItem("Rename", "F2")) {
                s_renameId = pathStr;
                std::snprintf(s_renameBuf, sizeof(s_renameBuf), "%s", p.filename().string().c_str());
                s_renameFocus = true;
            }
            if (ImGui::MenuItem("Copy", "Ctrl+C")) s_clipboardPath = pathStr;
            if (ImGui::MenuItem("Paste", "Ctrl+V", false, !s_clipboardPath.empty()))
                doPaste(st, root, p.parent_path());
            if (ImGui::MenuItem("Delete", "Del")) s_pendingDelete = pathStr;
            ImGui::EndPopup();
        }
    }
    ImGui::PopID();
}

// Recursively draw a directory: a tree node that lazy-lists its children on open. Folders first
// (sorted), then known files (sorted). Hidden/dot entries and unknown extensions are skipped.
static void drawDirNode(AppState& st, const fs::path& root, const fs::path& dir, bool isRoot) {
    std::error_code ec;
    std::string label = isRoot ? ("Workspace  (" + dir.filename().string() + ")")
                               : dir.filename().string();
    ImGuiTreeNodeFlags flags = ImGuiTreeNodeFlags_OpenOnArrow | ImGuiTreeNodeFlags_SpanAvailWidth;
    if (isRoot) flags |= ImGuiTreeNodeFlags_DefaultOpen;
    if (s_selectedPath == dir.string()) flags |= ImGuiTreeNodeFlags_Selected;

    ImGui::PushID(dir.string().c_str());

    // A non-root folder being inline-renamed shows an InputText in place of its tree node; its
    // children stay collapsed during the rename (kept simple — folder rename is a leaf interaction).
    if (!isRoot && s_renameId == dir.string()) {
        explorerRowPill(s_selectedPath == dir.string());
        drawIcon("body_folder");
        ImGui::SetNextItemWidth(-1);
        if (s_renameFocus) { ImGui::SetKeyboardFocusHere(); s_renameFocus = false; }
        if (ImGui::InputText("##rnd", s_renameBuf, sizeof(s_renameBuf),
                             ImGuiInputTextFlags_EnterReturnsTrue | ImGuiInputTextFlags_AutoSelectAll))
            commitRename(st, root);
        if (ImGui::IsItemDeactivated()) commitRename(st, root);
        ImGui::PopID();
        return;
    }

    explorerRowPill(s_selectedPath == dir.string());
    drawIcon("body_folder");
    bool open = ImGui::TreeNodeEx(label.c_str(), flags);
    if (ImGui::IsItemClicked() && !ImGui::IsItemToggledOpen()) {
        s_selectedPath = dir.string();
        s_currentFolder = dir.string();
    }
    // folder context menu (New Folder / Paste / Rename / Delete — root only allows New Folder/Paste)
    if (ImGui::BeginPopupContextItem("##dctx")) {
        s_selectedPath = dir.string();
        s_currentFolder = dir.string();
        if (ImGui::MenuItem("New Folder")) {
            createNewFolder(st, root, dir);
        }
        if (ImGui::MenuItem("Paste", "Ctrl+V", false, !s_clipboardPath.empty()))
            doPaste(st, root, dir);
        if (!isRoot) {
            ImGui::Separator();
            if (ImGui::MenuItem("Rename", "F2")) {
                s_renameId = dir.string();
                std::snprintf(s_renameBuf, sizeof(s_renameBuf), "%s", dir.filename().string().c_str());
                s_renameFocus = true;
            }
            if (ImGui::MenuItem("Delete", "Del")) s_pendingDelete = dir.string();
        }
        ImGui::EndPopup();
    }

    if (open) {
        // gather + sort children (folders first, then known files)
        std::vector<fs::path> dirs, files;
        for (fs::directory_iterator it(dir, fs::directory_options::skip_permission_denied, ec), end;
             it != end; it.increment(ec)) {
            if (ec) break;
            const fs::path& c = it->path();
            std::string name = c.filename().string();
            if (!name.empty() && name[0] == '.') continue;   // skip dotfiles
            std::error_code e2;
            if (fs::is_directory(c, e2)) dirs.push_back(c);
            else if (isKnownExt(lowerExt(c))) files.push_back(c);
        }
        auto byName = [](const fs::path& a, const fs::path& b) {
            std::string an = a.filename().string(), bn = b.filename().string();
            std::transform(an.begin(), an.end(), an.begin(), ::tolower);
            std::transform(bn.begin(), bn.end(), bn.begin(), ::tolower);
            return an < bn;
        };
        std::sort(dirs.begin(), dirs.end(), byName);
        std::sort(files.begin(), files.end(), byName);
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
    if (ImGui::CollapsingHeader("Recent", ImGuiTreeNodeFlags_DefaultOpen)) {
        int idx = 0;
        for (const std::string& rp : st.recentFiles) {
            ImGui::PushID(idx++);
            fs::path p(rp);
            explorerRowPill(false);
            drawIcon(fileIconName(lowerExt(p)));
            std::error_code ec;
            bool exists = fs::exists(p, ec);
            if (!exists) ImGui::PushStyleColor(ImGuiCol_Text, explorerSurf().textSecondary);
            explorerRowPill();
            if (ImGui::Selectable(p.filename().string().c_str(), false,
                                  ImGuiSelectableFlags_AllowDoubleClick) &&
                ImGui::IsMouseDoubleClicked(ImGuiMouseButton_Left) && exists)
                requestOpen(st, p);
            // Drag a Recent row onto the viewport to open it (same payload as the tree rows).
            if (exists && isKnownExt(lowerExt(p)) &&
                ImGui::BeginDragDropSource(ImGuiDragDropFlags_SourceAllowNullID)) {
                ImGui::SetDragDropPayload(kFileDragPayload, rp.c_str(), rp.size() + 1);
                ImGui::TextUnformatted(p.filename().string().c_str());
                ImGui::EndDragDropSource();
            }
            if (!exists) ImGui::PopStyleColor();
            if (ImGui::IsItemHovered()) ImGui::SetTooltip("%s", rp.c_str());
            ImGui::PopID();
        }
        ImGui::Separator();
    }
}

// ---- delete confirm modal -------------------------------------------------
static void drawDeleteModal(AppState& st, const fs::path& root) {
    if (!s_pendingDelete.empty() && !ImGui::IsPopupOpen("Delete?"))
        ImGui::OpenPopup("Delete?");
    bool open = !s_pendingDelete.empty();
    if (ImGui::BeginPopupModal("Delete?", &open, ImGuiWindowFlags_AlwaysAutoResize)) {
        fs::path p(s_pendingDelete);
        ImGui::Text("Delete \"%s\"?", p.filename().string().c_str());
        std::error_code ec;
        if (fs::is_directory(p, ec)) ImGui::TextDisabled("This folder and its contents.");
        ImGui::Spacing();
        if (ImGui::Button("Delete", ImVec2(110 * ui::uiScale, 0))) {
            if (!withinWorkspace(root, p)) {
                st.toast(ui::ToastLevel::Warn, "Delete blocked outside the workspace");
            } else {
                std::error_code de;
                std::uintmax_t n = fs::remove_all(p, de);
                if (de) st.toast(ui::ToastLevel::Error, "Delete failed: " + de.message());
                else {
                    st.addLog("[explorer] deleted " + p.string() + " (" + std::to_string(n) + " items)");
                    st.toast(ui::ToastLevel::Info, "Deleted " + p.filename().string());
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
    s_explorerStripe = 0;
    ensureWorkspaceDir(st);
    fs::path root(st.workspaceDir);

    // A just-created folder enters inline rename on the next frame (after the tree expands it).
    if (!s_pendingNewFolder.empty()) {
        s_renameId = s_pendingNewFolder;
        std::snprintf(s_renameBuf, sizeof(s_renameBuf), "%s",
                      fs::path(s_pendingNewFolder).filename().string().c_str());
        s_renameFocus = true;
        s_selectedPath = s_pendingNewFolder;
        s_pendingNewFolder.clear();
    }

    fs::path current = s_currentFolder.empty() ? root : fs::path(s_currentFolder);
    std::error_code curEc;
    if (!fs::is_directory(current, curEc)) current = current.parent_path();
    if (current.empty() || !withinWorkspace(root, current)) current = root;

    const bool selectedFile = !s_selectedPath.empty() &&
        fs::is_regular_file(fs::path(s_selectedPath), curEc) &&
        isKnownExt(lowerExt(fs::path(s_selectedPath)));

    ui::glassRowAtCursor(3.5f, false, false, 8.0f);
    if (toolbarIconButton("open", "open", "Open selected file", selectedFile))
        requestOpen(st, fs::path(s_selectedPath));
    ImGui::SameLine(0, 3.0f * ui::uiScale);
    if (toolbarIconButton("new_folder", "folder_new", "New folder"))
        createNewFolder(st, root, current);
    ImGui::SameLine(0, 3.0f * ui::uiScale);
    if (toolbarIconButton("paste", "paste", "Paste into current folder", !s_clipboardPath.empty()))
        doPaste(st, root, current);
    ImGui::SameLine(0, 3.0f * ui::uiScale);
    if (toolbarIconButton("refresh", "refresh", "Refresh"))
        st.toast(ui::ToastLevel::Info, "Explorer refreshed");

    ui::glassRowAtCursor(3.5f, false, false, 5.0f);
    ImGui::SameLine();
    ImGui::PushStyleColor(ImGuiCol_Text, explorerSurf().textSecondary);
    std::string folderName = current.filename().string();
    if (folderName.empty()) folderName = current.string();
    ImGui::TextUnformatted(folderName.c_str());
    ImGui::PopStyleColor();
    if (ImGui::IsItemHovered()) ImGui::SetTooltip("%s", current.string().c_str());
    ImGui::Separator();

    drawRecentSection(st);

    // Keyboard: F2 rename / Del delete on the current selection (only when this pane owns nav and no
    // text field is focused — the inline-rename InputText sets WantTextInput, so this won't fire mid
    // rename).
    if (!s_selectedPath.empty() && !ImGui::GetIO().WantTextInput &&
        ImGui::IsWindowFocused(ImGuiFocusedFlags_RootAndChildWindows)) {
        if (ImGui::IsKeyPressed(ImGuiKey_F2, false)) {
            fs::path sp(s_selectedPath);
            s_renameId = s_selectedPath;
            std::snprintf(s_renameBuf, sizeof(s_renameBuf), "%s", sp.filename().string().c_str());
            s_renameFocus = true;
        } else if (ImGui::IsKeyPressed(ImGuiKey_Delete, false)) {
            s_pendingDelete = s_selectedPath;
        }
    }

    ImGui::BeginChild("##fx_tree", ImVec2(0, 0), ImGuiChildFlags_None);
    std::error_code ec;
    if (fs::exists(root, ec))
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
    st.toast(ui::ToastLevel::Info, "Copied " + fs::path(s_selectedPath).filename().string());
    return true;
}

bool fileExplorerPaste(AppState& st) {
    if (s_clipboardPath.empty()) return false;
    ensureWorkspaceDir(st);
    fs::path root(st.workspaceDir);
    fs::path dest = s_currentFolder.empty() ? root : fs::path(s_currentFolder);
    std::error_code ec;
    if (!fs::is_directory(dest, ec)) dest = dest.parent_path();   // selection was a file -> its folder
    return doPaste(st, root, dest);
}

}  // namespace ui
