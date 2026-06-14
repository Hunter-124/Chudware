#include "left_pane.h"

#include <cstdlib>
#include <algorithm>
#include <filesystem>
#include <string>
#include <vector>

#include "imgui.h"

#include "../core/app_state.h"
#include "config_panel.h"
#include "feature_tree.h"
#include "file_dialog.h"
#include "icons.h"
#include "property_manager.h"
#include "../ui_theme.h"

namespace ui {

// Geometry contract for this pane (all logical px multiplied by uiScale):
// - Docked window label: "FeatureManager##LeftPane" (user-facing FeatureManager, stable internal id).
// - Vertical tab rail: 42*S wide, bordered child. Each tab is a stable 36*S square with 4*S
//   vertical rhythm, 20*S atlas glyph, 5*S corner radius, and a 3*S active accent bar inset 6*S.
// - Body child consumes the remaining width and hosts exactly one embedded manager body.
// - Explorer row icons are 16*S with a 4*S label gap; file list uses a bordered child so long
//   paths scroll instead of resizing the dock. All text sizing comes from pre-scaled ImGui fonts.
// These formulas mirror docs/GUI_STYLE_GUIDE.md §6 and keep the pane DPI-safe.

static inline std::string ljstr(const nlohmann::json& o, const char* k) {
    auto it = o.find(k);
    return (it != o.end() && it->is_string()) ? it->get<std::string>() : std::string();
}

static std::string baseName(const std::string& p) {
    size_t cut = p.find_last_of("\\/");
    return cut == std::string::npos ? p : p.substr(cut + 1);
}

static const char* kOpenFilter =
    "AI-CAD part (*.step+)\0*.step+\0All files (*.*)\0*.*\0";

static void openCadFile(AppState& st, const std::string& path) {
    if (path.empty()) return;
    std::string err;
    if (!st.docMirror.open(st.worker, path, err)) {
        st.addLog("[explorer] open failed: " + err);
        st.toast(ui::ToastLevel::Error, "Open failed: " + err);
        return;
    }

    st.clearViewportForNewDoc();
    const bool isAsm = ljstr(st.docMirror.tree(), "schema") == "aicad.asm/1";
    st.addDocTab(isAsm);
    if (st.activeTab >= 0 && st.activeTab < (int)st.docTabs.size()) {
        st.docTabs[st.activeTab].path = path;
        st.docTabs[st.activeTab].dirty = false;
    }
    st.leftPaneTab = AppState::LP_FeatureManager;
    st.toast(ui::ToastLevel::Info, "Opened " + baseName(path));

    const std::string shownName = baseName(path);
    st.rpc.call("doc_rebuild", {{"doc_id", st.docMirror.docId()}},
        [&st, shownName](const core::RpcResult& r) {
            if (!r.ok || r.result.contains("code")) {
                const std::string msg = !r.ok ? r.err : ljstr(r.result, "message");
                st.toast(ui::ToastLevel::Warn,
                         "Opened " + shownName + " - mesh refresh failed: " + msg);
                return;
            }
            std::string aicm;
            if (r.result.contains("tess") && r.result["tess"].is_object())
                aicm = ljstr(r.result["tess"], "aicm_path");
            if (!aicm.empty()) {
                std::string lerr;
                if (!st.loadDocMesh(aicm, lerr) && !lerr.empty())
                    st.addLog("[explorer] loadDocMesh failed: " + lerr);
            }
            st.docMirror.resync(st.worker);
        }, 60000);
}

static std::filesystem::path defaultExplorerRoot() {
    if (const char* p = std::getenv("AI_CAD_WORKSPACE"))
        if (*p) return std::filesystem::path(p);
    if (const char* up = std::getenv("USERPROFILE"))
        return std::filesystem::path(up) / "Documents" / "AI-CAD";
    return std::filesystem::current_path();
}

static bool isKnownCadFile(const std::filesystem::path& p) {
    const std::string e = p.extension().string();
    return e == ".step+" || e == ".step" || e == ".iges" || e == ".igs" ||
           e == ".stl" || e == ".3mf" || e == ".ecad+" ||
           e == ".kicad_sch" || e == ".kicad_pcb";
}

static void drawExplorer(AppState& st) {
    const auto& pal = ui::palette;
    ImGui::PushFont(st.fonts.medium);
    ImGui::TextUnformatted("Explorer");
    ImGui::PopFont();
    ImGui::SameLine();
    if (ImGui::Button("Open...")) {
        const std::string path = ui::openFileDialog(kOpenFilter);
        openCadFile(st, path);
    }

    ImGui::Separator();
    ImGui::TextColored(ImVec4(pal.textDim[0], pal.textDim[1], pal.textDim[2], 1),
                       "Workspace");

    static std::filesystem::path root = defaultExplorerRoot();
    std::error_code ec;
    if (!std::filesystem::exists(root, ec))
        std::filesystem::create_directories(root, ec);

    const std::string rootText = root.string();
    ImGui::PushTextWrapPos(ImGui::GetCursorPosX() + ImGui::GetContentRegionAvail().x);
    ImGui::TextColored(ImVec4(pal.textDim[0], pal.textDim[1], pal.textDim[2], 1),
                       "%s", rootText.c_str());
    ImGui::PopTextWrapPos();

    if (ImGui::BeginChild("##explorer_files", ImVec2(0, 0), ImGuiChildFlags_Border)) {
        int shown = 0;
        if (std::filesystem::exists(root, ec)) {
            std::vector<std::filesystem::directory_entry> entries;
            for (std::filesystem::directory_iterator it(root, ec), end; !ec && it != end; it.increment(ec))
                entries.push_back(*it);
            std::sort(entries.begin(), entries.end(), [](const auto& a, const auto& b) {
                if (a.is_directory() != b.is_directory()) return a.is_directory() > b.is_directory();
                return a.path().filename().string() < b.path().filename().string();
            });
            for (const auto& e : entries) {
                const bool isDir = e.is_directory(ec);
                if (!isDir && !isKnownCadFile(e.path())) continue;
                IconUV ic = getIcon(isDir ? "body_folder" : "part", 20);
                if (ic.valid) {
                    ImGui::Image(ic.texId, ImVec2(16.0f * ui::uiScale, 16.0f * ui::uiScale),
                                 ic.uv0, ic.uv1);
                    ImGui::SameLine(0, 4.0f * ui::uiScale);
                }
                const std::string label = e.path().filename().string();
                if (ImGui::Selectable(label.c_str(), false, ImGuiSelectableFlags_AllowDoubleClick)) {
                    if (isDir) {
                        if (ImGui::IsMouseDoubleClicked(ImGuiMouseButton_Left)) root = e.path();
                    } else if (ImGui::IsMouseDoubleClicked(ImGuiMouseButton_Left)) {
                        openCadFile(st, e.path().string());
                    }
                }
                if (ImGui::IsItemHovered())
                    ImGui::SetTooltip("%s", e.path().string().c_str());
                ++shown;
            }
        }
        if (shown == 0) {
            ImGui::TextColored(ImVec4(pal.textDim[0], pal.textDim[1], pal.textDim[2], 1),
                               "No CAD files in this folder.");
        }
    }
    ImGui::EndChild();
}

static bool paneTab(AppState& st, int tab, const char* icon, const char* tooltip, bool enabled = true) {
    const float S = ui::uiScale;
    const float w = 36.0f * S;
    const float h = 36.0f * S;
    const bool selected = st.leftPaneTab == tab;
    ImGui::PushID(tab);
    if (!enabled) ImGui::BeginDisabled();
    ImVec2 p = ImGui::GetCursorScreenPos();
    bool clicked = ImGui::InvisibleButton("##tab", ImVec2(w, h));
    const bool hovered = ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled);
    if (!enabled) ImGui::EndDisabled();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const auto& pal = ui::palette;
    ImU32 bg = selected ? ui::Palette::toU32(pal.ribbonTabActive)
                        : (hovered ? IM_COL32(255,255,255,24) : IM_COL32(0,0,0,0));
    dl->AddRectFilled(p, ImVec2(p.x + w, p.y + h), bg, 5.0f * S);
    if (selected)
        dl->AddRectFilled(ImVec2(p.x, p.y + 6.0f * S), ImVec2(p.x + 3.0f * S, p.y + h - 6.0f * S),
                          ui::Palette::toU32(pal.accent), 2.0f * S);
    IconUV ic = getIcon(icon, 20);
    if (ic.valid) {
        const float sz = 20.0f * S;
        ImGui::SetCursorScreenPos(ImVec2(p.x + (w - sz) * 0.5f, p.y + (h - sz) * 0.5f));
        ImGui::Image(ic.texId, ImVec2(sz, sz), ic.uv0, ic.uv1);
    }
    ImGui::SetCursorScreenPos(ImVec2(p.x, p.y + h + 4.0f * S));
    if (hovered) ImGui::SetTooltip("%s", tooltip);
    if (clicked && enabled) st.leftPaneTab = tab;
    ImGui::PopID();
    return clicked;
}

void drawLeftPane(AppState& st, PropertyManager& propertyManager, ConfigPanel& configPanel,
                  const std::function<void(const std::string&)>& editCallback) {
    if (!st.showFeatureTree) return;

    static bool wasPmActive = false;
    if (propertyManager.active()) {
        st.leftPaneTab = AppState::LP_PropertyManager;
    } else if (wasPmActive && st.leftPaneTab == AppState::LP_PropertyManager) {
        st.leftPaneTab = AppState::LP_FeatureManager;
    }
    wasPmActive = propertyManager.active();

    if (!ImGui::Begin("FeatureManager##LeftPane", &st.showFeatureTree)) { ImGui::End(); return; }

    const float tabW = 42.0f * ui::uiScale;
    ImGui::BeginChild("##left_tabs", ImVec2(tabW, 0), ImGuiChildFlags_Border);
    paneTab(st, AppState::LP_Explorer, "open", "Explorer");
    paneTab(st, AppState::LP_FeatureManager, "history", "FeatureManager");
    paneTab(st, AppState::LP_PropertyManager, "settings", "PropertyManager", propertyManager.active());
    paneTab(st, AppState::LP_Configurations, "settings", "ConfigurationManager");
    ImGui::EndChild();

    ImGui::SameLine(0, 0);
    ImGui::BeginChild("##left_body", ImVec2(0, 0), false);
    switch (st.leftPaneTab) {
        case AppState::LP_Explorer:
            drawExplorer(st);
            break;
        case AppState::LP_PropertyManager:
            if (propertyManager.active()) propertyManager.drawEmbedded(st);
            else {
                ImGui::TextColored(ImVec4(ui::palette.textDim[0], ui::palette.textDim[1],
                                          ui::palette.textDim[2], 1),
                                   "Start a command to edit its parameters here.");
            }
            break;
        case AppState::LP_Configurations:
            configPanel.drawEmbedded(st);
            break;
        case AppState::LP_FeatureManager:
        default:
            drawFeatureTreeEmbedded(st, editCallback);
            break;
    }
    ImGui::EndChild();
    ImGui::End();
}

}  // namespace ui
