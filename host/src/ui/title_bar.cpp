// AI-CAD title bar — menu bar + window caption buttons + chrome::HitLayout publishing.
#define GLFW_INCLUDE_NONE
#include <GLFW/glfw3.h>

#include "imgui.h"

#include "title_bar.h"

#include "../core/app_state.h"
#include "../renderer.h"
#include "../ui_theme.h"   // ui::uiScale
#include "doc_tabs.h"      // ui::openNewDocChooser (File > New)
#include "feature_tree.h"  // Wave 3: ui::featureTreeActivePath (Save vs. Save As gating)
#include "property_manager.h"  // View > PropertyManager checkmark queries PropertyManager::active()

#include <filesystem>      // Wave 3: Recent-files menu labels

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
            dl->AddRect(ImVec2(c.x - s + 2 * ui::uiScale, c.y - s), ImVec2(c.x + s, c.y + s - 2 * ui::uiScale), fg, 0, 0, th);
            dl->AddRect(ImVec2(c.x - s, c.y - s + 2 * ui::uiScale), ImVec2(c.x + s - 2 * ui::uiScale, c.y + s), fg, 0, 0, th);
        } else {
            dl->AddRect(ImVec2(c.x - s, c.y - s), ImVec2(c.x + s, c.y + s), fg, 0, 0, th);
        }
    } else {  // BTN_CLOSE: an X
        dl->AddLine(ImVec2(c.x - s, c.y - s), ImVec2(c.x + s, c.y + s), fg, th);
        dl->AddLine(ImVec2(c.x - s, c.y + s), ImVec2(c.x + s, c.y - s), fg, th);
    }
    return clicked;
}

// Wave 1C: buildDefaultLayout() moved to ui/layout.cpp — the default dock arrangement (slot names +
// split ratios that "Reset layout" rebuilds) was never a title-bar concern. The "Reset layout" menu
// items below just set st.resetLayout; app.cpp calls buildDefaultLayout() (global, declared in
// app_state.h) on the next frame.

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

        // Wave 5A: SolidWorks-standard 7-menu bar (File / Edit / View / Insert / Tools /
        // Window / Help). Items wire to existing AppState flags, AppState methods, or the unified
        // ribbon dispatcher (st.ribbonDispatch — any string id is compile-safe; unknown ids just
        // log a stub). Items with no reachable backing from this TU are drawn disabled (greyed) —
        // see the agent report for the exact list. A document-open guard gates doc-dependent items.
        const bool hasDoc = (st.activeTab >= 0);

        auto drawFileMenu = [&]() {
            if (ImGui::MenuItem("New...", "Ctrl+N")) ui::openNewDocChooser();
            // Wave 3: Open / Save / Save As fire one-shot AppState request flags consumed by app.cpp's
            // GL-thread pump, which reuses feature_tree's doOpenDoc/doSaveDoc (native dialog + RPC +
            // mesh refresh) and records Recent. Doing the actual open/save on the pump (not here, mid
            // menu draw) avoids a re-entrant doc swap during ImGui's menu walk.
            const bool docSaved = !ui::featureTreeActivePath().empty();
            if (ImGui::MenuItem("Open...", "Ctrl+O")) st.requestOpenDialog = true;
            // F4: Import... — opens any importable format (STEP/IGES/BREP/STL/OBJ/PLY/3MF/glTF/GLB)
            // via the SAME GL-thread pump as Open. The Open dialog's filter is already the broad
            // importable-file filter (feature_tree's kImportFilter), and the engine's doc_open RPC
            // branches by extension, so Import-as-new-doc needs no separate path today. (Future:
            // merge-into-current-doc via the engine import_file `into` param — a doc_import RPC.)
            if (ImGui::MenuItem("Import...")) st.requestOpenDialog = true;
            ImGui::Separator();
            if (ImGui::MenuItem("Save", "Ctrl+S", false, hasDoc)) st.requestSave = true;
            if (ImGui::MenuItem("Save As...", "Ctrl+Shift+S", false, hasDoc)) st.requestSaveAs = true;
            // Wave-5-fill: Save All iterates docTabs on the GL-thread pump (saves each saveable
            // part/assembly; never-saved docs prompt a Save-As one at a time). Enabled when any
            // tab is open.
            if (ImGui::MenuItem("Save All", nullptr, false, hasDoc)) st.requestSaveAll = true;
            (void)docSaved;
            ImGui::Separator();
            // Wave-5-fill: 3D export to the formats engine.doc.formats.export_document accepts. Only
            // a part/assembly tab (a DocMirror-backed doc) can be exported — a Drawing/ECAD tab leaves
            // the mirror pointed at the hidden part, so we gate on it explicitly. Each item sets a
            // one-shot requestExportFormat drained on the GL thread (native Save-As + exportFile), so
            // the dialog never re-enters this menu walk. (Drawing export stays on the 2D panel's own
            // DXF/PDF button — exportDrawing needs the drawing registry id, not this part doc.)
            const bool canExport3d = hasDoc && !st.activeTabIsDrawing() && !st.activeTabIsEcad();
            if (ImGui::BeginMenu("Export", canExport3d)) {
                if (ImGui::MenuItem("STEP (.step)...")) st.requestExportFormat = "step";
                if (ImGui::MenuItem("IGES (.iges)...")) st.requestExportFormat = "iges";
                if (ImGui::MenuItem("BREP (.brep)...")) st.requestExportFormat = "brep";
                ImGui::Separator();
                // F4: mesh-lane exports (engine export_file accepts these format strings too).
                if (ImGui::MenuItem("STL (.stl)..."))   st.requestExportFormat = "stl";
                if (ImGui::MenuItem("OBJ (.obj)..."))   st.requestExportFormat = "obj";
                if (ImGui::MenuItem("PLY (.ply)..."))   st.requestExportFormat = "ply";
                if (ImGui::MenuItem("3MF (.3mf)..."))   st.requestExportFormat = "3mf";
                if (ImGui::MenuItem("glTF (.gltf)...")) st.requestExportFormat = "gltf";
                if (ImGui::MenuItem("GLB (.glb)..."))   st.requestExportFormat = "glb";
                ImGui::EndMenu();
            }
            ImGui::Separator();
            // Wave 3: Recent Files — backed by AppState::recentFiles (persisted to config.json).
            // Choosing one queues requestOpenPath; app.cpp's pump opens it (CAD or ECAD by extension).
            if (ImGui::BeginMenu("Recent Files", !st.recentFiles.empty())) {
                for (const std::string& rp : st.recentFiles) {
                    std::string name = std::filesystem::path(rp).filename().string();
                    if (ImGui::MenuItem(name.c_str())) st.requestOpenPath = rp;
                    if (ImGui::IsItemHovered()) ImGui::SetTooltip("%s", rp.c_str());
                }
                ImGui::Separator();
                if (ImGui::MenuItem("Clear Recent Files")) {
                    st.recentFiles.clear();
                    st.settings.recentFiles.clear();
                    saveSettings(st.settings);
                }
                ImGui::EndMenu();
            }
            ImGui::Separator();
            ImGui::MenuItem("Close", "Ctrl+W", false, false);  // per-tab close not reachable here
            if (ImGui::MenuItem("Quit", "Alt+F4")) glfwSetWindowShouldClose(win, 1);
        };
        auto drawEditMenu = [&]() {
            if (ImGui::MenuItem("Undo", "Ctrl+Z", false, hasDoc)) st.docUndo();
            if (ImGui::MenuItem("Redo", "Ctrl+Y", false, hasDoc)) st.docRedo();
            ImGui::Separator();
            // edit_cut/edit_copy/edit_paste dispatch ids are being registered by a parallel agent
            // this round; dispatching by string is compile-safe regardless (no-op + log if absent).
            if (ImGui::MenuItem("Cut", "Ctrl+X", false, hasDoc))   st.ribbonDispatch("edit_cut");
            if (ImGui::MenuItem("Copy", "Ctrl+C", false, hasDoc))  st.ribbonDispatch("edit_copy");
            if (ImGui::MenuItem("Paste", "Ctrl+V", false, hasDoc)) st.ribbonDispatch("edit_paste");
            // Wave-5-fill: Delete / Select All act on the FeatureManager selection via the unified
            // dispatcher (edit_delete / edit_select_all). Delete removes the selected feature(s);
            // Select All selects every feature in the active part's tree.
            if (ImGui::MenuItem("Delete", "Del", false, hasDoc))       st.ribbonDispatch("edit_delete");
            if (ImGui::MenuItem("Select All", "Ctrl+A", false, hasDoc)) st.ribbonDispatch("edit_select_all");
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
            ImGui::MenuItem("Equations", nullptr, &st.showEquations);
            ImGui::MenuItem("AI Panel", nullptr, &st.showAIPanel);
            ImGui::MenuItem("Viewport", nullptr, &st.showViewport);
            ImGui::Separator();
            if (ImGui::MenuItem("Simulation tools", nullptr, st.showSimulationTools)) {
                st.showSimulationTools = !st.showSimulationTools;
                if (st.showSimulationTools) { st.showRibbon = true; st.ribbonTab = AppState::RT_Simulation; }
                else if (st.ribbonTab == AppState::RT_Simulation) st.ribbonTab = AppState::RT_Features;
            }
            if (ImGui::MenuItem("PCB / electronics tools", nullptr, st.showPcbTools)) {
                st.showPcbTools = !st.showPcbTools;
                if (st.showPcbTools) { st.showRibbon = true; st.ribbonTab = AppState::RT_PCB; }
                else if (st.ribbonTab == AppState::RT_PCB) st.ribbonTab = AppState::RT_Features;
            }
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
            if (ImGui::BeginMenu("Feature", hasDoc)) {
                if (ImGui::MenuItem("Extrude Boss/Base")) st.ribbonDispatch("extrude_boss");
                if (ImGui::MenuItem("Revolve Boss/Base")) st.ribbonDispatch("revolve_boss");
                if (ImGui::MenuItem("Sweep"))             st.ribbonDispatch("sweep_boss");
                if (ImGui::MenuItem("Loft"))              st.ribbonDispatch("loft_boss");
                ImGui::Separator();
                if (ImGui::MenuItem("Fillet"))            st.ribbonDispatch("fillet");
                if (ImGui::MenuItem("Chamfer"))           st.ribbonDispatch("chamfer");
                if (ImGui::MenuItem("Shell"))             st.ribbonDispatch("shell");
                if (ImGui::MenuItem("Draft"))             st.ribbonDispatch("draft");
                if (ImGui::MenuItem("Rib"))               st.ribbonDispatch("rib");
                ImGui::EndMenu();
            }
            if (ImGui::MenuItem("Sketch")) st.ribbonDispatch("sketch_new");
            ImGui::Separator();
            if (ImGui::BeginMenu("Reference Geometry", hasDoc)) {
                if (ImGui::MenuItem("Plane"))             st.ribbonDispatch("ref_plane");
                if (ImGui::MenuItem("Axis"))             st.ribbonDispatch("ref_axis");
                if (ImGui::MenuItem("Point"))            st.ribbonDispatch("ref_point");
                if (ImGui::MenuItem("Coordinate System")) st.ribbonDispatch("ref_csys");
                ImGui::EndMenu();
            }
            if (ImGui::BeginMenu("Pattern/Mirror", hasDoc)) {
                if (ImGui::MenuItem("Linear Pattern"))   st.ribbonDispatch("linear_pattern");
                if (ImGui::MenuItem("Circular Pattern")) st.ribbonDispatch("circular_pattern");
                if (ImGui::MenuItem("Mirror"))           st.ribbonDispatch("mirror_feature");
                ImGui::EndMenu();
            }
            if (ImGui::MenuItem("Hole Wizard", nullptr, false, hasDoc)) st.ribbonDispatch("hole_wizard");
            ImGui::Separator();
            if (ImGui::BeginMenu("Assembly")) {
                if (ImGui::MenuItem("Insert Component")) st.ribbonDispatch("asm_insert");
                if (ImGui::MenuItem("Mate"))             st.ribbonDispatch("asm_mate");
                ImGui::EndMenu();
            }
        };

        auto drawToolsMenu = [&]() {
            if (ImGui::BeginMenu("Evaluate", hasDoc)) {
                if (ImGui::MenuItem("Measure"))         st.ribbonDispatch("eval_measure");
                if (ImGui::MenuItem("Mass Properties")) st.ribbonDispatch("eval_mass_props");
                if (ImGui::MenuItem("Section Properties")) st.ribbonDispatch("eval_section_props");
                ImGui::EndMenu();
            }
            if (ImGui::MenuItem("Simulation")) st.ribbonDispatch("sim_new_static");
            ImGui::Separator();
            if (ImGui::MenuItem("Sheet Metal")) st.ribbonDispatch("sm_base_flange");
            if (ImGui::MenuItem("Weldments"))   st.ribbonDispatch("weld_member");
            if (ImGui::MenuItem("ECAD / PCB"))  st.ribbonDispatch("ecad_new_sch");
            ImGui::Separator();
            // Equations / Global Variables editor (grafted from codex): opens the modeless dialog.
            if (ImGui::MenuItem("Equations...")) st.showEquations = true;
            ImGui::Separator();
            if (ImGui::MenuItem("Options...")) st.openSettings = true;
            if (ImGui::MenuItem("Repair / install runtime", nullptr, false,
                                !st.setupRunning.load() && !st.job.running.load()))
                st.startSetup(true);
        };

        auto drawWindowMenu = [&]() {
            // Multi-window tiling has no backing (single OS window + dockspace). Greyed placeholders.
            ImGui::MenuItem("Tile Horizontally", nullptr, false, false);
            ImGui::MenuItem("Tile Vertically", nullptr, false, false);
            ImGui::MenuItem("Cascade", nullptr, false, false);
            ImGui::Separator();
            ImGui::MenuItem("Close All", nullptr, false, false);
        };
        auto drawHelpMenu = [&]() {
            if (ImGui::MenuItem("AI Help")) {
                st.showAIPanel = true;
                st.focusHelpTab = true;
            }
            if (ImGui::MenuItem("About AI-CAD")) st.showAbout = true;
            // Documentation / Keyboard Shortcuts have no backing yet — greyed placeholders.
            ImGui::MenuItem("Documentation", nullptr, false, false);
            ImGui::MenuItem("Keyboard Shortcuts", nullptr, false, false);
        };

        if (winW < collapseThreshold) {
            // Collapsed: single "Menu" top-level item with all submenus (narrow-window rollup).
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
