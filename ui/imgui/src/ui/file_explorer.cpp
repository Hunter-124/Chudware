// File Explorer panel — see file_explorer.h.
#include "ui/file_explorer.h"

#include <algorithm>
#include <cctype>
#include <cstdio>
#include <filesystem>
#include <string>
#include <system_error>
#include <vector>

#include "imgui.h"

#include "app/app_state.h"
#include "ui/settings_modal.h"
#include "theme/icons.h"
#include "theme/theme.h"
#include <platform/paths.h>

namespace fs = std::filesystem;

namespace ui {
namespace {

inline constexpr const char* kFileDragPayload = "AICAD_FILE";

static int s_explorerStripe = 0;
static void explorerRowPill(bool selected = false) {
    theme::glassRowAtCursor(3.5f, (s_explorerStripe++ % 2) == 0, selected, 5.f);
}

static std::string s_selectedPath;
static std::string s_currentFolder;
static std::string s_renameId;
static char        s_renameBuf[260] = {0};
static bool        s_renameFocus = false;
static std::string s_pendingDelete;
static std::string s_clipboardPath;
static std::string s_pendingNewFolder;
static std::string s_requestOpenPath;

static void notify(app::AppState& st, const char* msg) { st.status = msg; }

static bool isCadExt(const std::string& e) {
    return e == ".step+" || e == ".step" || e == ".stp" || e == ".iges" || e == ".igs" ||
           e == ".stl"   || e == ".3mf";
}
static bool isEcadExt(const std::string& e) {
    return e == ".ecad+" || e == ".ecad" || e == ".kicad_sch" || e == ".kicad_pcb";
}
static bool isKnownExt(const std::string& e) { return isCadExt(e) || isEcadExt(e); }

static std::string lowerExt(const fs::path& p) {
    std::string e = p.extension().string();
    std::transform(e.begin(), e.end(), e.begin(),
                   [](unsigned char c) { return (char)std::tolower(c); });
    return e;
}

static bool withinWorkspace(const fs::path& root, const fs::path& child) {
    std::error_code ec;
    fs::path r = fs::weakly_canonical(root, ec);
    if (ec) r = root.lexically_normal();
    fs::path c = fs::weakly_canonical(child, ec);
    if (ec) c = child.lexically_normal();
    auto rs = r.string(), cs = c.string();
    if (cs.size() < rs.size()) return false;
    for (size_t i = 0; i < rs.size(); ++i)
        if (std::tolower((unsigned char)cs[i]) != std::tolower((unsigned char)rs[i])) return false;
    return cs.size() == rs.size() || cs[rs.size()] == '\\' || cs[rs.size()] == '/';
}

static const char* fileIconName(const std::string& ext) {
    if (ext == ".step+" || ext == ".step" || ext == ".stp") return "file_part";
    if (ext == ".iges" || ext == ".igs" || ext == ".stl" || ext == ".3mf") return "file_part";
    if (ext == ".ecad+" || ext == ".ecad") return "rail_files";
    if (ext == ".kicad_sch") return "rail_files";
    if (ext == ".kicad_pcb") return "rail_files";
    return "new_doc";
}

static void drawIcon(const char* name) {
    const float sz = 16.f * theme::S();
    icons::inlineIcon(name, sz, TCOL(txt1));
    ImGui::SameLine(0, 4.f * theme::S());
}

static bool toolbarIconButton(const char* id, const char* icon, const char* tip, bool enabled = true) {
    const float S = theme::S();
    const float btn = 28.f * S;
    ImVec2 p0 = ImGui::GetCursorScreenPos();
    ImVec2 p1(p0.x + btn, p0.y + btn);

    ImGui::PushID(id);
    if (!enabled) ImGui::BeginDisabled();
    bool clicked = ImGui::InvisibleButton("##fx_tool", ImVec2(btn, btn));
    bool hovered = ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled);
    if (!enabled) ImGui::EndDisabled();

    ImDrawList* dl = ImGui::GetWindowDrawList();
    if (hovered && enabled)
        theme::glassRowBackground(dl, p0, p1, 5.f * S, false, false);
    ImU32 col = enabled ? TCOL(txt0) : theme::roleU32(theme::pal().txt2, 0.35f);
    icons::draw(dl, icon, ImVec2(p0.x + btn * 0.5f, p0.y + btn * 0.5f), btn * 0.32f, col);
    if (hovered) ImGui::SetTooltip("%s", tip);
    ImGui::PopID();
    return clicked && enabled;
}


static void commitRename(app::AppState& st, const fs::path& root) {
    if (s_renameId.empty()) return;
    fs::path oldP = s_renameId;
    std::string newName = s_renameBuf;
    while (!newName.empty() && newName.front() == ' ') newName.erase(newName.begin());
    while (!newName.empty() && newName.back()  == ' ') newName.pop_back();
    s_renameId.clear();
    if (newName.empty() || newName == oldP.filename().string()) return;
    if (newName.find_first_of("\\/:*?\"<>|") != std::string::npos) {
        notify(st, "Invalid characters in name");
        return;
    }
    fs::path newP = oldP.parent_path() / newName;
    if (!withinWorkspace(root, oldP) || !withinWorkspace(root, newP)) {
        notify(st, "Rename blocked outside the workspace");
        return;
    }
    std::error_code ec;
    if (fs::exists(newP, ec)) { notify(st, "A file with that name exists"); return; }
    fs::rename(oldP, newP, ec);
    if (ec) notify(st, ("Rename failed: " + ec.message()).c_str());
    else if (s_selectedPath == oldP.string()) s_selectedPath = newP.string();
}

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

static bool doPaste(app::AppState& st, const fs::path& root, const fs::path& destDir) {
    if (s_clipboardPath.empty()) { notify(st, "Nothing copied"); return false; }
    fs::path src = s_clipboardPath;
    std::error_code ec;
    if (!fs::exists(src, ec)) { notify(st, "Copied file no longer exists"); return false; }
    if (!withinWorkspace(root, destDir)) { notify(st, "Paste blocked outside the workspace"); return false; }
    fs::path dest = uniqueDest(destDir, src);
    if (fs::is_directory(src, ec))
        fs::copy(src, dest, fs::copy_options::recursive, ec);
    else
        fs::copy_file(src, dest, fs::copy_options::none, ec);
    if (ec) { notify(st, ("Paste failed: " + ec.message()).c_str()); return false; }
    notify(st, ("Pasted " + dest.filename().string()).c_str());
    s_selectedPath = dest.string();
    return true;
}

static void createNewFolder(app::AppState& st, const fs::path& root, const fs::path& dir) {
    if (!withinWorkspace(root, dir)) {
        notify(st, "New Folder blocked outside the workspace");
        return;
    }
    std::error_code e2;
    int n = 1;
    fs::path cand = dir / "New Folder";
    while (fs::exists(cand, e2)) cand = dir / ("New Folder " + std::to_string(++n));
    if (fs::create_directory(cand, e2)) {
        s_pendingNewFolder = cand.string();
        s_currentFolder = dir.string();
    } else {
        notify(st, "New Folder failed");
    }
}

static void requestOpen(app::AppState& st, const fs::path& p) {
    std::string e = lowerExt(p);
    if (!isKnownExt(e)) return;
    s_requestOpenPath = p.string();
    notify(st, ("Open " + p.filename().string()).c_str());
}

static void drawFileRow(app::AppState& st, const fs::path& root, const fs::path& p, const std::string& ext);
static void drawDirNode(app::AppState& st, const fs::path& root, const fs::path& dir, bool isRoot);

static void drawFileRow(app::AppState& st, const fs::path& root, const fs::path& p, const std::string& ext) {
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
        if (ImGui::IsItemDeactivated()) commitRename(st, root);
    } else {
        if (ImGui::Selectable(p.filename().string().c_str(), selected,
                              ImGuiSelectableFlags_AllowDoubleClick)) {
            s_selectedPath = pathStr;
            s_currentFolder = p.parent_path().string();
            if (ImGui::IsMouseDoubleClicked(ImGuiMouseButton_Left)) requestOpen(st, p);
        }
        if (isKnownExt(ext) && ImGui::BeginDragDropSource(ImGuiDragDropFlags_SourceAllowNullID)) {
            ImGui::SetDragDropPayload(kFileDragPayload, pathStr.c_str(), pathStr.size() + 1);
            ImGui::TextUnformatted(p.filename().string().c_str());
            ImGui::EndDragDropSource();
        }
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

static void drawDirNode(app::AppState& st, const fs::path& root, const fs::path& dir, bool isRoot) {
    std::error_code ec;
    std::string label = isRoot ? ("Workspace  (" + dir.filename().string() + ")")
                               : dir.filename().string();
    ImGuiTreeNodeFlags flags = ImGuiTreeNodeFlags_OpenOnArrow | ImGuiTreeNodeFlags_SpanAvailWidth;
    if (isRoot) flags |= ImGuiTreeNodeFlags_DefaultOpen;
    if (s_selectedPath == dir.string()) flags |= ImGuiTreeNodeFlags_Selected;

    ImGui::PushID(dir.string().c_str());

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
    if (ImGui::BeginPopupContextItem("##dctx")) {
        s_selectedPath = dir.string();
        s_currentFolder = dir.string();
        if (ImGui::MenuItem("New Folder")) createNewFolder(st, root, dir);
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
        std::vector<fs::path> dirs, files;
        for (fs::directory_iterator it(dir, fs::directory_options::skip_permission_denied, ec), end;
             it != end; it.increment(ec)) {
            if (ec) break;
            const fs::path& c = it->path();
            std::string name = c.filename().string();
            if (!name.empty() && name[0] == '.') continue;
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
        for (const auto& d : dirs) drawDirNode(st, root, d, false);
        for (const auto& f : files) drawFileRow(st, root, f, lowerExt(f));
        if (dirs.empty() && files.empty())
            ImGui::TextDisabled("  (empty)");
        ImGui::TreePop();
    }
    ImGui::PopID();
}

static void drawRecentSection(app::AppState& st) {
    auto& recent = appSettings().recentFiles;
    if (recent.empty()) return;
    if (ImGui::CollapsingHeader("Recent", ImGuiTreeNodeFlags_DefaultOpen)) {
        int idx = 0;
        for (const std::string& rp : recent) {
            ImGui::PushID(idx++);
            fs::path p(rp);
            explorerRowPill(false);
            drawIcon(fileIconName(lowerExt(p)));
            std::error_code ec;
            bool exists = fs::exists(p, ec);
            if (!exists) ImGui::PushStyleColor(ImGuiCol_Text, TVEC(txt2));
            if (ImGui::Selectable(p.filename().string().c_str(), false,
                                  ImGuiSelectableFlags_AllowDoubleClick) &&
                ImGui::IsMouseDoubleClicked(ImGuiMouseButton_Left) && exists)
                requestOpen(st, p);
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

static void drawDeleteModal(app::AppState& st, const fs::path& root) {
    if (!s_pendingDelete.empty() && !ImGui::IsPopupOpen("Delete?"))
        ImGui::OpenPopup("Delete?");
    bool open = !s_pendingDelete.empty();
    const float S = theme::S();
    if (ImGui::BeginPopupModal("Delete?", &open, ImGuiWindowFlags_AlwaysAutoResize)) {
        fs::path p(s_pendingDelete);
        ImGui::Text("Delete \"%s\"?", p.filename().string().c_str());
        std::error_code ec;
        if (fs::is_directory(p, ec)) ImGui::TextDisabled("This folder and its contents.");
        ImGui::Spacing();
        if (ImGui::Button("Delete", ImVec2(110 * S, 0))) {
            if (!withinWorkspace(root, p)) {
                notify(st, "Delete blocked outside the workspace");
            } else {
                std::error_code de;
                fs::remove_all(p, de);
                if (de) notify(st, ("Delete failed: " + de.message()).c_str());
                else {
                    notify(st, ("Deleted " + p.filename().string()).c_str());
                    if (s_selectedPath == s_pendingDelete) s_selectedPath.clear();
                }
            }
            s_pendingDelete.clear();
            ImGui::CloseCurrentPopup();
        }
        ImGui::SameLine();
        if (ImGui::Button("Cancel", ImVec2(110 * S, 0))) {
            s_pendingDelete.clear();
            ImGui::CloseCurrentPopup();
        }
        ImGui::EndPopup();
    }
    if (!open) s_pendingDelete.clear();
}

}  // namespace

void ensureWorkspaceDir(app::AppState& st) {
    AppSettings& cfg = appSettings();
    if (cfg.workspaceDir.empty()) {
        fs::path base = platform::documentsDir();
        if (base.empty()) base = platform::appdataRoot();
        cfg.workspaceDir = (base / "AI-CAD").string();
    }
    std::error_code ec;
    fs::create_directories(cfg.workspaceDir, ec);
    if (s_currentFolder.empty()) s_currentFolder = cfg.workspaceDir;
    (void)st;
}

void drawFileExplorerBody(app::AppState& st) {
    s_explorerStripe = 0;
    ensureWorkspaceDir(st);
    fs::path root(appSettings().workspaceDir);

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

    theme::glassRowAtCursor(3.5f, false, false, 8.f);
    if (toolbarIconButton("open", "open", "Open selected file", selectedFile))
        requestOpen(st, fs::path(s_selectedPath));
    ImGui::SameLine(0, 3.f * theme::S());
    if (toolbarIconButton("new_folder", "folder_new", "New folder"))
        createNewFolder(st, root, current);
    ImGui::SameLine(0, 3.f * theme::S());
    if (toolbarIconButton("paste", "paste", "Paste into current folder", !s_clipboardPath.empty()))
        doPaste(st, root, current);
    ImGui::SameLine(0, 3.f * theme::S());
    if (toolbarIconButton("refresh", "refresh", "Refresh"))
        notify(st, "Explorer refreshed");

    theme::glassRowAtCursor(3.5f, false, false, 5.f);
    ImGui::SameLine();
    ImGui::PushStyleColor(ImGuiCol_Text, TVEC(txt2));
    std::string folderName = current.filename().string();
    if (folderName.empty()) folderName = current.string();
    ImGui::TextUnformatted(folderName.c_str());
    ImGui::PopStyleColor();
    if (ImGui::IsItemHovered()) ImGui::SetTooltip("%s", current.string().c_str());
    ImGui::Separator();

    drawRecentSection(st);

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
        drawDirNode(st, root, root, true);
    else
        ImGui::TextDisabled("Workspace folder missing");
    ImGui::EndChild();

    drawDeleteModal(st, root);
}

bool fileExplorerActive(const app::AppState& st) { return st.railTab == kFilesRailTab; }

bool fileExplorerCopySelection(app::AppState& st) {
    if (s_selectedPath.empty()) return false;
    s_clipboardPath = s_selectedPath;
    notify(st, ("Copied " + fs::path(s_selectedPath).filename().string()).c_str());
    return true;
}

bool fileExplorerPaste(app::AppState& st) {
    if (s_clipboardPath.empty()) return false;
    ensureWorkspaceDir(st);
    fs::path root(appSettings().workspaceDir);
    fs::path dest = s_currentFolder.empty() ? root : fs::path(s_currentFolder);
    std::error_code ec;
    if (!fs::is_directory(dest, ec)) dest = dest.parent_path();
    return doPaste(st, root, dest);
}

std::string takeExplorerOpenRequest() {
    std::string out = s_requestOpenPath;
    s_requestOpenPath.clear();
    return out;
}

}  // namespace ui
