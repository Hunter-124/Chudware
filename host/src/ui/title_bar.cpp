// AI-CAD title bar — menu bar + window caption buttons + chrome::HitLayout publishing.
#define GLFW_INCLUDE_NONE
#include <GLFW/glfw3.h>

#include "imgui.h"
#include "imgui_internal.h"

#include "title_bar.h"

#include "../core/app_state.h"
#include "../renderer.h"
#include "../ui_theme.h"   // ui::uiScale
#include "file_dialog.h"
#include "property_manager.h"

// ---------------- caption buttons (shared small ui helper) ----------------
bool windowButton(const char* id, WinBtn kind, float h, bool maximized) {
    // C25-W3G: button width tracks uiScale so buttons stay clickable at 1.5x/2.0x.
    const float btnW = h * (1.35f + 0.15f * (ui::uiScale - 1.0f));
    const ImVec2 size(btnW, h);
    const ImVec2 p0 = ImGui::GetCursorScreenPos();
    bool clicked = ImGui::InvisibleButton(id, size);
    bool hovered = ImGui::IsItemHovered();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    if (hovered) {
        ImU32 bg = kind == BTN_CLOSE ? IM_COL32(232, 17, 35, 255) : IM_COL32(255, 255, 255, 26);
        dl->AddRectFilled(p0, ImVec2(p0.x + size.x, p0.y + size.y), bg);
    }
    // Caption glyph color from palette so it adapts to light/dark themes.
    const unsigned char fgv = (unsigned char)(ui::palette.captionFg * 255.0f + 0.5f);
    const ImU32 fg = (hovered && kind == BTN_CLOSE) ? IM_COL32(255, 255, 255, 255)
                                                    : IM_COL32(fgv, fgv, fgv, 235);
    const ImVec2 c(p0.x + size.x * 0.5f, p0.y + size.y * 0.5f);
    const float s = h * 0.16f;  // glyph half-size
    const float th = 1.4f;
    if (kind == BTN_MIN) {
        dl->AddLine(ImVec2(c.x - s, c.y), ImVec2(c.x + s, c.y), fg, th);
    } else if (kind == BTN_MAX) {
        if (maximized) {  // restore: two stacked squares
            dl->AddRect(ImVec2(c.x - s + 2, c.y - s), ImVec2(c.x + s, c.y + s - 2), fg, 0, 0, th);
            dl->AddRect(ImVec2(c.x - s, c.y - s + 2), ImVec2(c.x + s - 2, c.y + s), fg, 0, 0, th);
        } else {
            dl->AddRect(ImVec2(c.x - s, c.y - s), ImVec2(c.x + s, c.y + s), fg, 0, 0, th);
        }
    } else {  // BTN_CLOSE: an X
        dl->AddLine(ImVec2(c.x - s, c.y - s), ImVec2(c.x + s, c.y + s), fg, th);
        dl->AddLine(ImVec2(c.x - s, c.y + s), ImVec2(c.x + s, c.y - s), fg, th);
    }
    return clicked;
}

// C24: SolidWorks 2025 default arrangement.
//   LEFT   = LeftPane, full height ribbon->status (~300px logical at 1x): Explorer /
//            FeatureManager / PropertyManager / ConfigurationManager as vertical icon tabs.
//   RIGHT  = Task Pane (~340px): Design (AI panel) + Agent Activity as tabs, like SW's task pane.
//            The floating command dialogs (Insert/Mate/Exploded/Interference/BOM, Mass Properties
//            & friends, Simulation) dock as tabs into the bottom of this pane on first open so they
//            never hide behind the central viewport node (the C20b NoBringToFrontOnFocus debt).
//   BOTTOM = Console, a thin strip below the viewport (collapsed-feeling; SW has no console but we
//            keep ours unobtrusive).
//   CENTER = Viewport, as large as possible.
// "Reset layout" rebuilds exactly this.
void buildDefaultLayout(ImGuiID root, const ImVec2& size) {
    ImGui::DockBuilderRemoveNode(root);
    ImGui::DockBuilderAddNode(root, ImGuiDockNodeFlags_DockSpace);
    ImGui::DockBuilderSetNodeSize(root, size);

    // Target pixel widths translated to fractions of the dockspace width.
    const float w = size.x > 1.0f ? size.x : 1440.0f;
    // W4-K / GUI audit #2.6: clamp the dock widths in PIXELS, not in fixed fraction bounds. The
    // target px scale by uiScale so the docks track the pre-scaled fonts; clamping the *fraction*
    // with a fixed ceiling (the old 0.34/0.36) silently ate that DPI growth at >=1.5x and clipped
    // tree labels — the very thing W4-K was meant to fix. See docs/GUI_STYLE_GUIDE.md §6.
    const float sc = ui::uiScale;
    auto clampf = [](float v, float lo, float hi) { return v < lo ? lo : (v > hi ? hi : v); };
    float leftPx  = clampf(258.0f * sc, 180.0f * sc, 0.34f * w);  float leftFrac  = leftPx  / w;
    float rightPx = clampf(300.0f * sc, 200.0f * sc, 0.36f * w);  float rightFrac = rightPx / w;
    ImGuiID main = root, left, right, rightBottom;
    // Left column = SolidWorks-style manager pane (full height).
    left  = ImGui::DockBuilderSplitNode(main, ImGuiDir_Left,  leftFrac,  nullptr, &main);
    // Right column = Task Pane (the consolidated AI panel), full height like SolidWorks.
    right = ImGui::DockBuilderSplitNode(main, ImGuiDir_Right, rightFrac / (1.0f - leftFrac),
                                        nullptr, &main);
    // Bottom slice of the right pane holds the command dialogs as docked tabs.
    rightBottom = ImGui::DockBuilderSplitNode(right, ImGuiDir_Down, 0.42f, nullptr, &right);

    ImGui::DockBuilderDockWindow("FeatureManager##LeftPane", left);
    // One task-pane window: Design / Activity / Console live as internal tabs.
    ImGui::DockBuilderDockWindow("AI", right);
    // Wave N/O: ECAD shares the central node with the Viewport (tabbed). Dock ECAD FIRST so the
    // Viewport is the default-active tab (3D is the primary view); the ECAD tab pops forward on its
    // own (SetNextWindowFocus) when an ECAD document is loaded.
    ImGui::DockBuilderDockWindow("ECAD", main);
    ImGui::DockBuilderDockWindow("Viewport", main);
    // Command dialogs dock into the lower-right slot so they surface as tabs instead of floating
    // behind the central node. They share one node (only one is typically open at a time).
    const char* dialogs[] = {
        "Simulation", "Insert Component", "Mate##asm", "Exploded View",
        "Interference Detection", "Bill of Materials",
        // Evaluate dialogs (titles set in evaluate_dialogs.cpp) — pre-registered so they land in
        // the task pane the first time they open (DockBuilderDockWindow matches by name).
        "Mass Properties", "Measure", "Section Properties", "Geometry Check", "Symmetry Check",
        "Zebra Stripes", "Draft angle", "Wall thickness", "Mean curvature",
        // Design Table — same task-pane slot so the editable config grid surfaces as a tab.
        "Design Table",
        // Weldment Cut List — same task-pane slot so it surfaces as a tab when toggled on.
        "Cut List",
    };
    for (const char* d : dialogs) ImGui::DockBuilderDockWindow(d, rightBottom);

    ImGui::DockBuilderFinish(root);
}

namespace ui {

void drawTitleBar(AppState& st, bool maximized, float& barHOut) {
    GLFWwindow* win = st.win;
    chrome::HitLayout& hitLayout = *st.hitLayout;

    // C25-W3G: scale the frame padding so the title bar grows with DPI.
    float s = ui::uiScale;
    // ---------- custom title bar (our own; the OS title bar is stripped) ----------
    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(14.0f * s, 12.0f * s));
    const float barH = ImGui::GetFrameHeight();
    hitLayout.titleBarHeight = (int)barH;
    ImGui::PushStyleColor(ImGuiCol_MenuBarBg,
        ImVec4(ui::palette.headerBg[0], ui::palette.headerBg[1],
               ui::palette.headerBg[2], ui::palette.headerBg[3]));
    if (ImGui::BeginMainMenuBar()) {
        // C25-W3G: compute caption button width early so we can clamp the brand/menu region.
        const float bw = barH * (1.35f + 0.15f * (s - 1.0f));
        const float captionZoneW = bw * 3.0f;
        const float winW = ImGui::GetWindowWidth();
        // Available horizontal space for brand + menus (leave caption buttons on the right).
        const float menuZoneR = winW - captionZoneW - 4.0f;

        // brand mark on the left
        const float iconH = barH * 0.52f;
        if (st.logoIcon.id) {
            ImGui::SetCursorPosX(12.0f * s);
            ImGui::SetCursorPosY((barH - iconH) * 0.5f);
            ImGui::Image((ImTextureID)(intptr_t)st.logoIcon.id, ImVec2(iconH, iconH));
            ImGui::SameLine(0, 9.0f * s);
        } else {
            ImGui::SetCursorPosX(14.0f * s);
        }
        const Tex& wm = (ui::activeTheme == ui::Theme::Light && st.wordmarkDark.id)
                            ? st.wordmarkDark : st.wordmark;
        if (wm.id) {
            const float wmH = barH * 0.40f;
            float wmW = wmH * (float)wm.w / wm.h;
            // C25-W3G: only show wordmark if it fits in the available zone.
            if (ImGui::GetCursorPosX() + wmW + 18.0f * s < menuZoneR - 120.0f) {
                ImGui::SetCursorPosY((barH - wmH) * 0.5f);
                ImGui::Image((ImTextureID)(intptr_t)wm.id, ImVec2(wmW, wmH));
                ImGui::SameLine(0, 18.0f * s);
            }
        }

        // C25-W3G: collapse to single "Menu" dropdown below a width threshold.
        const float collapseThreshold = 700.0f * s;
        hitLayout.menuLeft = (int)ImGui::GetCursorScreenPos().x;

        auto cmd = [&](const char* id) {
            if (st.ribbonDispatch) st.ribbonDispatch(id);
        };
        auto treeString = [&](const char* key) {
            const nlohmann::json& tree = st.docMirror.tree();
            auto it = tree.find(key);
            return (it != tree.end() && it->is_string()) ? it->get<std::string>() : std::string();
        };
        auto fileBaseName = [](const std::string& p) {
            size_t cut = p.find_last_of("\\/");
            return cut == std::string::npos ? p : p.substr(cut + 1);
        };
        auto newCadDoc = [&](bool assembly) {
            if (!st.workerStarted || st.job.running.load()) {
                st.toast(ui::ToastLevel::Warn, "Worker is busy");
                return;
            }
            std::string err;
            st.suppressTabDirty = true;
            bool ok = assembly ? st.docMirror.newAssembly(st.worker, err)
                               : st.docMirror.newPart(st.worker, err);
            st.suppressTabDirty = false;
            if (!ok) {
                st.toast(ui::ToastLevel::Error, assembly ? "New assembly failed" : "New part failed");
                st.addLog(std::string("[doc] new failed: ") + err);
                return;
            }
            st.clearViewportForNewDoc();
            st.addDocTab(assembly);
            st.showFeatureTree = true;
            st.leftPaneTab = AppState::LP_FeatureManager;
            if (assembly) {
                st.showRibbon = true;
                st.ribbonTab = AppState::RT_Assembly;
            }
        };
        auto saveCadDoc = [&](bool forceDialog) {
            if (!st.docMirror.hasDoc()) return;
            std::string path;
            if (st.activeTab >= 0 && st.activeTab < (int)st.docTabs.size())
                path = st.docTabs[st.activeTab].path;
            if (forceDialog || path.empty()) {
                std::string suggest = path.empty() ? treeString("name") : fileBaseName(path);
                if (suggest.empty()) suggest = "Part1";
                if (suggest.find('.') == std::string::npos) suggest += ".step+";
                path = ui::saveFileDialog(
                    "AI-CAD part (*.step+)\0*.step+\0All files (*.*)\0*.*\0",
                    "step+", suggest.c_str());
                if (path.empty()) return;
            }
            std::string err;
            if (st.docMirror.save(st.worker, path, err)) {
                st.clearActiveTabDirty();
                if (st.activeTab >= 0 && st.activeTab < (int)st.docTabs.size()) {
                    st.docTabs[st.activeTab].path = path;
                    st.docTabs[st.activeTab].dirty = false;
                }
                st.toast(ui::ToastLevel::Info, "Saved " + fileBaseName(path));
            } else {
                st.toast(ui::ToastLevel::Error, "Save failed: " + err);
                st.addLog("[doc] save failed: " + err);
            }
        };
        auto drawFileMenu = [&]() {
            if (ImGui::BeginMenu("New")) {
                if (ImGui::MenuItem("Part")) newCadDoc(false);
                if (ImGui::MenuItem("Assembly")) newCadDoc(true);
                if (ImGui::MenuItem("Drawing")) cmd("make_drawing");
                ImGui::Separator();
                if (ImGui::MenuItem("Schematic")) cmd("ecad_new_sch");
                if (ImGui::MenuItem("PCB")) cmd("ecad_new_pcb");
                ImGui::EndMenu();
            }
            if (ImGui::MenuItem("Open...", "Ctrl+O")) {
                st.showFeatureTree = true;
                st.leftPaneTab = AppState::LP_Explorer;
            }
            ImGui::Separator();
            if (ImGui::MenuItem("Save", "Ctrl+S", false, st.docMirror.hasDoc()))
                saveCadDoc(false);
            if (ImGui::MenuItem("Save As...", "Ctrl+Shift+S", false, st.docMirror.hasDoc()))
                saveCadDoc(true);
            if (ImGui::BeginMenu("Export", st.docMirror.hasDoc())) {
                ImGui::MenuItem("STEP...", nullptr, false, false);
                ImGui::MenuItem("STL...", nullptr, false, false);
                ImGui::MenuItem("3MF...", nullptr, false, false);
                ImGui::MenuItem("DXF...", nullptr, false, false);
                ImGui::EndMenu();
            }
            ImGui::Separator();
            if (ImGui::MenuItem("Settings...")) st.openSettings = true;
            if (ImGui::MenuItem("Repair / install runtime", nullptr, false,
                                !st.setupRunning.load() && !st.job.running.load()))
                st.startSetup(true);
            ImGui::Separator();
            if (ImGui::MenuItem("Quit", "Alt+F4")) glfwSetWindowShouldClose(win, 1);
        };
        auto drawEditMenu = [&]() {
            if (ImGui::MenuItem("Undo", "Ctrl+Z", false, st.docMirror.hasDoc())) st.docUndo();
            if (ImGui::MenuItem("Redo", "Ctrl+Y", false, st.docMirror.hasDoc())) st.docRedo();
            ImGui::Separator();
            ImGui::MenuItem("Cut", "Ctrl+X", false, false);
            ImGui::MenuItem("Copy", "Ctrl+C", false, false);
            ImGui::MenuItem("Paste", "Ctrl+V", false, false);
            ImGui::MenuItem("Delete", "Del", false, false);
            ImGui::MenuItem("Select All", "Ctrl+A", false, false);
        };
        auto drawViewMenu = [&]() {
            ImGui::MenuItem("Ribbon", nullptr, &st.showRibbon);
            if (ImGui::MenuItem("FeatureManager", nullptr,
                                st.showFeatureTree && st.leftPaneTab == AppState::LP_FeatureManager)) {
                st.showFeatureTree = true;
                st.leftPaneTab = AppState::LP_FeatureManager;
            }
            if (ImGui::MenuItem("PropertyManager", nullptr,
                                st.showFeatureTree && st.leftPaneTab == AppState::LP_PropertyManager,
                                st.propertyManager && st.propertyManager->active())) {
                st.showFeatureTree = true;
                st.leftPaneTab = AppState::LP_PropertyManager;
            }
            if (ImGui::MenuItem("ConfigurationManager", nullptr,
                                st.showFeatureTree && st.leftPaneTab == AppState::LP_Configurations)) {
                st.showFeatureTree = true;
                st.leftPaneTab = AppState::LP_Configurations;
            }
            if (ImGui::MenuItem("Explorer", nullptr,
                                st.showFeatureTree && st.leftPaneTab == AppState::LP_Explorer)) {
                st.showFeatureTree = true;
                st.leftPaneTab = AppState::LP_Explorer;
            }
            ImGui::MenuItem("Design Table", nullptr, &st.showDesignTablePanel);
            ImGui::MenuItem("Cut List", nullptr, &st.showCutListPanel);
            ImGui::MenuItem("AI Panel", nullptr, &st.showAIPanel);
            ImGui::MenuItem("Viewport", nullptr, &st.showViewport);
            ImGui::Separator();
            bool isLight = (ui::activeTheme == ui::Theme::Light);
            if (ImGui::MenuItem("Light theme (SolidWorks-style)", nullptr, isLight)) {
                ui::Theme next = isLight ? ui::Theme::Dark : ui::Theme::Light;
                ui::applyTheme(next, st.scale);
                st.settings.theme = (next == ui::Theme::Light) ? "light" : "dark";
                saveSettings(st.settings);
                if (st.renderer)
                    st.renderer->setBackground(
                        ui::palette.viewportTop[0], ui::palette.viewportTop[1],
                        ui::palette.viewportTop[2], ui::palette.viewportBottom[0],
                        ui::palette.viewportBottom[1], ui::palette.viewportBottom[2]);
            }
            ImGui::Separator();
            if (ImGui::MenuItem("Reset layout")) st.resetLayout = true;
        };
        auto drawInsertMenu = [&]() {
            if (ImGui::MenuItem("Sketch")) cmd("sketch_new");
            if (ImGui::BeginMenu("Features")) {
                if (ImGui::MenuItem("Extrude Boss/Base")) cmd("extrude_boss");
                if (ImGui::MenuItem("Extrude Cut")) cmd("extrude_cut");
                if (ImGui::MenuItem("Revolve Boss/Base")) cmd("revolve_boss");
                if (ImGui::MenuItem("Revolve Cut")) cmd("revolve_cut");
                if (ImGui::MenuItem("Sweep")) cmd("sweep_boss");
                if (ImGui::MenuItem("Loft")) cmd("loft_boss");
                ImGui::EndMenu();
            }
            if (ImGui::BeginMenu("Reference Geometry")) {
                if (ImGui::MenuItem("Plane")) cmd("ref_plane");
                if (ImGui::MenuItem("Axis")) cmd("ref_axis");
                if (ImGui::MenuItem("Point")) cmd("ref_point");
                ImGui::EndMenu();
            }
            if (ImGui::MenuItem("Hole Wizard")) cmd("hole_wizard");
            if (ImGui::MenuItem("Pattern / Mirror")) cmd("linear_pattern");
        };
        auto drawToolsMenu = [&]() {
            if (ImGui::BeginMenu("Evaluate")) {
                if (ImGui::MenuItem("Measure")) cmd("eval_measure");
                if (ImGui::MenuItem("Mass Properties")) cmd("eval_mass_props");
                if (ImGui::MenuItem("Section Properties")) cmd("eval_section_props");
                if (ImGui::MenuItem("Geometry Check")) cmd("eval_geometry");
                ImGui::EndMenu();
            }
            if (ImGui::BeginMenu("Simulation")) {
                if (ImGui::MenuItem("Static Study")) cmd("sim_new_static");
                if (ImGui::MenuItem("Modal Study")) cmd("sim_new_modal");
                if (ImGui::MenuItem("Flow Study")) cmd("sim_new_flow");
                ImGui::EndMenu();
            }
            if (ImGui::BeginMenu("Sheet Metal")) {
                if (ImGui::MenuItem("Base Flange")) cmd("sm_base_flange");
                if (ImGui::MenuItem("Flatten")) cmd("sm_flat_pattern");
                if (ImGui::MenuItem("Export Flat DXF")) cmd("sm_export_flat_dxf");
                ImGui::EndMenu();
            }
            if (ImGui::BeginMenu("ECAD")) {
                if (ImGui::MenuItem("New Schematic")) cmd("ecad_new_sch");
                if (ImGui::MenuItem("New PCB")) cmd("ecad_new_pcb");
                if (ImGui::MenuItem("ERC")) cmd("ecad_run_erc");
                if (ImGui::MenuItem("DRC")) cmd("ecad_run_drc");
                if (ImGui::MenuItem("Board to 3D")) cmd("ecad_to_3d");
                ImGui::EndMenu();
            }
        };
        auto drawWindowMenu = [&]() {
            if (ImGui::MenuItem("Reset Layout")) st.resetLayout = true;
            ImGui::MenuItem("Tile Horizontal", nullptr, false, false);
            ImGui::MenuItem("Tile Vertical", nullptr, false, false);
            ImGui::MenuItem("Cascade", nullptr, false, false);
            ImGui::Separator();
            ImGui::MenuItem("Close All", nullptr, false, false);
        };
        auto drawHelpMenu = [&]() {
            if (ImGui::MenuItem("About AI-CAD")) st.showAbout = true;
            ImGui::MenuItem("Documentation", nullptr, false, false);
            ImGui::MenuItem("Keyboard Shortcuts", nullptr, false, false);
        };

        if (winW < collapseThreshold) {
            // Collapsed: single "Menu" top-level item with all submenus
            if (ImGui::BeginMenu("Menu")) {
                if (ImGui::BeginMenu("File"))   { drawFileMenu();   ImGui::EndMenu(); }
                if (ImGui::BeginMenu("Edit"))   { drawEditMenu();   ImGui::EndMenu(); }
                if (ImGui::BeginMenu("View"))   { drawViewMenu();   ImGui::EndMenu(); }
                if (ImGui::BeginMenu("Insert")) { drawInsertMenu(); ImGui::EndMenu(); }
                if (ImGui::BeginMenu("Tools"))  { drawToolsMenu();  ImGui::EndMenu(); }
                if (ImGui::BeginMenu("Window")) { drawWindowMenu(); ImGui::EndMenu(); }
                if (ImGui::BeginMenu("Help"))   { drawHelpMenu();   ImGui::EndMenu(); }
                ImGui::EndMenu();
            }
        } else {
            if (ImGui::BeginMenu("File"))   { drawFileMenu();   ImGui::EndMenu(); }
            if (ImGui::BeginMenu("Edit"))   { drawEditMenu();   ImGui::EndMenu(); }
            if (ImGui::BeginMenu("View"))   { drawViewMenu();   ImGui::EndMenu(); }
            if (ImGui::BeginMenu("Insert")) { drawInsertMenu(); ImGui::EndMenu(); }
            if (ImGui::BeginMenu("Tools"))  { drawToolsMenu();  ImGui::EndMenu(); }
            if (ImGui::BeginMenu("Window")) { drawWindowMenu(); ImGui::EndMenu(); }
            if (ImGui::BeginMenu("Help"))   { drawHelpMenu();   ImGui::EndMenu(); }
        }
        hitLayout.menuRight = (int)ImGui::GetItemRectMax().x;

        // window controls, right-aligned.
        const float bx = winW - captionZoneW;
        hitLayout.controlsLeft = (int)bx;
        hitLayout.controlsRight = (int)(bx + captionZoneW);
        ImGui::SetCursorPos(ImVec2(bx, 0.0f));
        if (windowButton("##min", BTN_MIN, barH, false)) glfwIconifyWindow(win);
        ImGui::SetCursorPos(ImVec2(bx + bw, 0.0f));
        if (windowButton("##max", BTN_MAX, barH, maximized)) {
            if (maximized) glfwRestoreWindow(win);
            else glfwMaximizeWindow(win);
        }
        ImGui::SetCursorPos(ImVec2(bx + bw * 2.0f, 0.0f));
        if (windowButton("##close", BTN_CLOSE, barH, false)) glfwSetWindowShouldClose(win, 1);
        ImGui::EndMainMenuBar();
    }
    ImGui::PopStyleColor();
    ImGui::PopStyleVar();  // FramePadding

    barHOut = barH;
}

}  // namespace ui
