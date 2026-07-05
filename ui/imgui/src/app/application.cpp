#include "app/application.h"
#include "app/app_state.h"
#include "ui/layout.h"
#include "ui/panels.h"
#include "ui/viewport.h"
#include "ui/scenarios.h"
#include "ui/cursors.h"
#include "ui/cam/cam_panel.h"
#include "ui/ecad/ecad_panel.h"
#include "ui/command_search.h"
#include "ui/file_explorer.h"
#include "ui/settings_modal.h"
#include "ui/preview_pane.h"
#include "ui/scaffold_demo_panel.h"
#include "ui/parts_panel.h"
#include "ui/cam_panel.h"          // T-U3 CAM workspace
#include "ui/first_run.h"
#include "ui/bom_panel.h"
#include "ui/requirements_panel.h"  // T-U5 requirements & verification panel
#include "ui/transparency_panel.h"  // T-U8 model & telemetry transparency panel
#include "ui/sim_panel.h"                       // T-U6 simulation studies
#include "ui/piping_panel.h"       // T-U7 piping viewer
#include "ui/hydraulic_panel.h"    // T-U7 hydraulic viewer
#include "ui/structural_panel.h"   // T-U7 structural viewer
#include "ui/gallery_panel.h"       // T-U9 sample gallery
#include "ui/mission_control_panel.h"  // T-F17 mission-control (DAG run steering) panel
#include "ui/agent_inspector_panel.h"  // T-W17 agent inspector (advisor observability)
#include "ui/watch_grid_panel.h"       // T-W17 multi-viewport watch grid
#include "ui/agent_dag_panel.h"        // T-DP17 interactive agent DAG widget
#include "theme/theme.h"
#include "theme/texture_cache.h"

#include "viewport_impl.h"

#include "imgui.h"
#include "imgui_internal.h"
#include <GLFW/glfw3.h>

namespace app {

static AppState g_state;
static aicad::viewport::GlViewport g_glViewport;

AppState& Application::state() { return g_state; }

bool Application::init(GLFWwindow* window, const std::string& resourcesDir, float scale) {
    g_state.window = window;
    glViewport_ = &g_glViewport;
    scene_.init();
    g_state.scene = &scene_;
    int dw = 1280, dh = 800;
    if (window) glfwGetFramebufferSize(window, &dw, &dh);
    glViewportReady_ = glViewport_->init(dw > 0 ? dw : 1280, dh > 0 ? dh : 800);
    g_state.seedDemo();
    ui::initCursors(window);
    if (glViewportReady_)
        ui::setViewportPipeline(glViewport_);
    (void)resourcesDir; (void)scale;
    return true;
}

void Application::shutdown() {
    g_state.rpc.shutdown();
    g_state.simClient.stop();
    g_state.worker.stop();
    ui::clearPreviewTextures();
    scene_.shutdown();
    g_state.scene = nullptr;
    glViewportReady_ = false;
    glViewport_ = nullptr;
}

void Application::frame() {
    AppState& s = g_state;
    ImGuiIO& io = ImGui::GetIO();

    s.rpc.drain();
    ui::scenarioTick(s);
    s.tickDemoAgent(io.DeltaTime);

    if ((io.KeyCtrl || io.KeySuper) && ImGui::IsKeyPressed(ImGuiKey_K, false)) {
        s.paletteOpen = !s.paletteOpen;
        if (s.paletteOpen) { s.paletteQuery[0] = '\0'; s.paletteSel = 0; }
    }

    if ((io.KeyCtrl || io.KeySuper) && !io.WantTextInput && s.engineReady && s.engine.hasDoc()
        && !s.agent.running && s.activeTool.empty()) {
        bool redo = (ImGui::IsKeyPressed(ImGuiKey_Y, false)) ||
                    (io.KeyShift && ImGui::IsKeyPressed(ImGuiKey_Z, false));
        if (redo) {
            aicad::ui::Action a; a.cmdId = "edit_redo"; a.source = aicad::ui::ActionSource::Shortcut;
            if (s.actionBus.submit(a).ok) { s.onDocMutation(); s.status = "Redo"; }
        } else if (ImGui::IsKeyPressed(ImGuiKey_Z, false)) {
            aicad::ui::Action a; a.cmdId = "edit_undo"; a.source = aicad::ui::ActionSource::Shortcut;
            if (s.actionBus.submit(a).ok) { s.onDocMutation(); s.status = "Undo"; }
        }
    }
    if ((io.KeyCtrl || io.KeySuper) && !io.WantTextInput) {
        if (ImGui::IsKeyPressed(ImGuiKey_S, false)) s.doSave(io.KeyShift);
        else if (ImGui::IsKeyPressed(ImGuiKey_O, false)) s.doOpen();
        else if (!io.KeyShift && ImGui::IsKeyPressed(ImGuiKey_N, false)) ui::openNewDocChooser();
    }

    {
        int settingsTab = -1;
        if (ui::takeOpenSettingsRequest(&settingsTab)) {
            ui::settingsUi().openSettings = true;
            if (settingsTab >= 0) ui::settingsUi().openSettingsTab = settingsTab;
        }
        std::string openPath = ui::takeExplorerOpenRequest();
        if (!openPath.empty() && s.engineReady) {
            if (s.engine.open(openPath)) {
                s.onDocMutation();
                s.status = "Opened " + openPath;
            }
        }
    }

    const float titleH = theme::ctx().m.titleBarH  * theme::S();
    const float wsH    = theme::ctx().m.workspaceH  * theme::S();
    const float tbH    = theme::ctx().m.toolbarH    * theme::S();
    const float statusH= theme::ctx().m.statusH     * theme::S();
    const float railW  = theme::ctx().m.railW        * theme::S();

    ImGuiViewport* vp = ImGui::GetMainViewport();
    ImGui::SetNextWindowPos(vp->Pos);
    ImGui::SetNextWindowSize(vp->Size);
    ImGui::SetNextWindowViewport(vp->ID);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowRounding, 0.f);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowBorderSize, 0.f);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(0, 0));
    ImGuiWindowFlags rootFlags =
        ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_NoCollapse | ImGuiWindowFlags_NoResize |
        ImGuiWindowFlags_NoMove | ImGuiWindowFlags_NoBringToFrontOnFocus | ImGuiWindowFlags_NoNavFocus |
        ImGuiWindowFlags_NoDocking;
    ImGui::Begin("##root", nullptr, rootFlags);
    ImGui::PopStyleVar(3);

    ui::drawTitleBar(s, titleH);
    ui::drawWorkspaceBar(s, wsH);
    ui::drawContextToolbar(s, tbH);
    (void)railW;   // T-G23: the standalone left rail is gone — its tabs live inside the glass column.

    float midH = ImGui::GetContentRegionAvail().y - statusH;
    if (midH < 0) midH = 0;

    // T-G23 overlay layout: the dockspace hosts ONLY the central document window (Viewport/ECAD/CAM),
    // full-width edge-to-edge. The FeatureManager + Copilot are pinned glass columns drawn OVER it
    // (drawLeftPane / drawRightPane below), not docked task panes. Publish the overlay band (ribbon
    // bottom -> status-bar top) so those columns anchor their geometry; the old ##rail child and the
    // left/right dock splits are GONE (buildDefaultLayout is now a single splitless central node).
    const ImVec2 bandMin = ImGui::GetCursorScreenPos();
    const float dockW = ImGui::GetContentRegionAvail().x;
    ui::setOverlayBand(bandMin.x, bandMin.y, bandMin.x + dockW, bandMin.y + midH);

    ImGui::PushStyleColor(ImGuiCol_WindowBg, ImVec4(0, 0, 0, 0));
    ImGui::PushStyleColor(ImGuiCol_DockingEmptyBg, ImVec4(0, 0, 0, 0));
    ImGui::PushStyleColor(ImGuiCol_Separator, ImVec4(0, 0, 0, 0));
    ImGui::PushStyleColor(ImGuiCol_SeparatorHovered, ImVec4(0, 0, 0, 0));
    ImGui::PushStyleColor(ImGuiCol_SeparatorActive, ImVec4(0, 0, 0, 0));
    ImGuiID dockId = ImGui::GetID("##dockspace");
    ImGui::DockSpace(dockId, ImVec2(0, midH), ImGuiDockNodeFlags_PassthruCentralNode);
    if (!layoutBuilt_) {
        buildDefaultLayout(dockId, ImVec2(dockW > 1.f ? dockW : 1440.f, midH));
        layoutBuilt_ = true;
    }
    ImGui::PopStyleColor(5);

    ui::drawStatusBar(s, statusH);

    ImGui::End();

    ImGui::PushStyleColor(ImGuiCol_WindowBg, ImVec4(0,0,0,0));
    ImGuiWindowFlags pflags = ImGuiWindowFlags_NoCollapse | ImGuiWindowFlags_NoBackground;

    // Pinned glass FeatureManager column over the viewport's left edge (replaces "Browser###left").
    // Hosts the rail tabs + tree/bodies/files/configs + the PropertyManager form (drawInspector).
    ui::drawLeftPane(s);

    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(0, 0));
    if (ImGui::Begin("Viewport###viewport", nullptr, pflags | ImGuiWindowFlags_NoScrollbar |
                     ImGuiWindowFlags_NoScrollWithMouse)) {
        ui::setViewportBackdropActive(true);
        ui::drawViewport(s);
    } else {
        ui::setViewportBackdropActive(false);
    }
    ImGui::End();
    ImGui::PopStyleVar();

    // H1: CAM dock window — kept as a registered ImGui window for docking-layout consistency, but
    // drawCamManager() is now called INSIDE drawViewport() as a content-swap (st.camActive gate)
    // rather than here. Drawing from two sites per frame would double-submit the same ImGui IDs.
    // camTakeFocusRequest()/SetNextWindowFocus() removed: the content-swap in viewport.cpp surfaces
    // CAM without needing to bring a separate dock tab forward (that approach was proven not to work
    // in headless mode — see work/tasks/T-G8.md "TRIED + FAILED").
    ImGui::Begin("CAM", nullptr, pflags);
    ImGui::End();

    // H2: ECAD dock window — same rationale as CAM above. drawEcadEditor() now lives inside
    // drawViewport() gated on workspace==Schematic||Board. The separate Begin("ECAD") block here
    // is a no-draw stub kept only for dock-layout consistency.
    ImGui::Begin("ECAD", nullptr, pflags);
    ImGui::End();

    // Pinned glass Copilot column over the viewport's right edge (replaces "Assistant###agent").
    ui::drawRightPane(s);

    // T-F16 preview pane: closable. T-G23 removed the docked rightBottom slot (overlay layout), so it
    // now floats — anchored top-right inside the overlay band, left of the Copilot column, on first
    // open. Focus is requested BEFORE Begin (a hidden window's Begin returns false, so an in-window
    // SetWindowFocus would never run). Zero footprint while closed.
    if (*ui::previewPaneOpen()) {
        if (ui::previewTakeFocusRequest()) ImGui::SetNextWindowFocus();
        ui::setNextCommandDialogPlacement();
        if (ImGui::Begin("Preview###preview", ui::previewPaneOpen(), pflags))
            ui::drawPreviewPane(s);
        ImGui::End();
    }

    // T-U3 CAM workspace panel: closable, reveals when a cam_* ribbon command fires or
    // camWorkspaceLoadJob() is called (e.g. from the copilot fast-path). Floats centered on
    // first open (ImGuiCond_Appearing) so it surfaces over the viewport; zero footprint when closed.
    // AI_CAD_SHOT=cam_workspace exercises the populated job / sim / post state.
    if (*ui::camWorkspacePanelOpen()) {
        const ImGuiViewport* camVp = ImGui::GetMainViewport();
        ImGui::SetNextWindowPos(ImVec2(camVp->WorkPos.x + camVp->WorkSize.x * 0.5f,
                                      camVp->WorkPos.y + camVp->WorkSize.y * 0.5f),
                                ImGuiCond_Appearing, ImVec2(0.5f, 0.5f));
        ImGui::SetNextWindowSize(ImVec2(820.f * theme::S(), 560.f * theme::S()),
                                 ImGuiCond_Appearing);
        if (ImGui::Begin("CAM Workspace###cam_workspace", ui::camWorkspacePanelOpen(), pflags))
            ui::drawCamWorkspacePanel(s);
        ImGui::End();
    }

    // T-U1 kit demo: closable, proves panel_scaffold.h compiles/renders/screenshots
    // (AI_CAD_SHOT=scaffold_demo). Zero footprint while closed. Not dock-registered (it is a
    // kit demo, not a shipped feature) — floats centered so it doesn't overlap ribbon/tree chrome
    // (equations_dialog.cpp's ImGuiCond_Appearing pattern).
    if (*ui::scaffoldDemoPanelOpen()) {
        const ImGuiViewport* vp = ImGui::GetMainViewport();
        ImGui::SetNextWindowPos(ImVec2(vp->WorkPos.x + vp->WorkSize.x * 0.5f,
                                        vp->WorkPos.y + vp->WorkSize.y * 0.5f),
                                 ImGuiCond_Appearing, ImVec2(0.5f, 0.5f));
        ImGui::SetNextWindowSize(ImVec2(420.f * theme::S(), 420.f * theme::S()), ImGuiCond_Appearing);
        if (ImGui::Begin("Scaffold Demo###scaffold_demo", ui::scaffoldDemoPanelOpen(), pflags))
            ui::drawScaffoldDemoPanel(s);
        ImGui::End();
    }

    // T-U2 Parts Browser: closable panel built on panel_scaffold.h (T-U1 kit).
    // Federated search across all part domains; detail card + insert via part_place (Pillar 17).
    // Screenshot-verifiable via AI_CAD_SHOT=parts_browser (worker one-shot).
    // Zero footprint while closed — same convention as drawScaffoldDemoPanel / drawPreviewPane.
    if (*ui::partsPanelOpen()) {
        const ImGuiViewport* vp = ImGui::GetMainViewport();
        ImGui::SetNextWindowPos(ImVec2(vp->WorkPos.x + vp->WorkSize.x * 0.5f,
                                        vp->WorkPos.y + vp->WorkSize.y * 0.5f),
                                 ImGuiCond_Appearing, ImVec2(0.5f, 0.5f));
        ImGui::SetNextWindowSize(ImVec2(520.f * theme::S(), 680.f * theme::S()), ImGuiCond_Appearing);
        if (ImGui::Begin("Parts Browser###parts_browser", ui::partsPanelOpen(), pflags))
            ui::drawPartsPanel(s);
        ImGui::End();
    }
    // T-U4 BOM & sourcing panel — closable, screenshot-verifiable via AI_CAD_SHOT=bom.
    // Floats centered (same positioning as scaffold_demo) so it doesn't overlap ribbon/tree chrome.
    // Zero footprint while closed.
    if (*ui::bomPanelOpen()) {
        const ImGuiViewport* vp2 = ImGui::GetMainViewport();
        ImGui::SetNextWindowPos(ImVec2(vp2->WorkPos.x + vp2->WorkSize.x * 0.5f,
                                       vp2->WorkPos.y + vp2->WorkSize.y * 0.5f),
                                ImGuiCond_Appearing, ImVec2(0.5f, 0.5f));
        ImGui::SetNextWindowSize(ImVec2(680.f * theme::S(), 480.f * theme::S()),
                                 ImGuiCond_Appearing);
        if (ImGui::Begin("BOM & Sourcing###bom", ui::bomPanelOpen(), pflags))
            ui::drawBomPanel(s);
        ImGui::End();
    }
    // T-U5 Requirements & verification panel — closable, screenshot-verifiable via
    // AI_CAD_SHOT=requirements.  Floats centered; zero footprint while closed.
    if (*ui::requirementsPanelOpen()) {
        const ImGuiViewport* vp3 = ImGui::GetMainViewport();
        ImGui::SetNextWindowPos(ImVec2(vp3->WorkPos.x + vp3->WorkSize.x * 0.5f,
                                       vp3->WorkPos.y + vp3->WorkSize.y * 0.5f),
                                ImGuiCond_Appearing, ImVec2(0.5f, 0.5f));
        ImGui::SetNextWindowSize(ImVec2(600.f * theme::S(), 520.f * theme::S()),
                                 ImGuiCond_Appearing);
        if (ImGui::Begin("Requirements###requirements", ui::requirementsPanelOpen(), pflags))
            ui::drawRequirementsPanel(s);
        ImGui::End();
    }
    // T-U8 Model & telemetry transparency panel — closable, screenshot-verifiable via
    // AI_CAD_SHOT=transparency. Floats centered; zero footprint while closed.
    // Exposes consent tier, which-brain, canary opt-in, and sidecar readout.
    if (*ui::transparencyPanelOpen()) {
        const ImGuiViewport* vp4 = ImGui::GetMainViewport();
        ImGui::SetNextWindowPos(ImVec2(vp4->WorkPos.x + vp4->WorkSize.x * 0.5f,
                                       vp4->WorkPos.y + vp4->WorkSize.y * 0.5f),
                                ImGuiCond_Appearing, ImVec2(0.5f, 0.5f));
        ImGui::SetNextWindowSize(ImVec2(560.f * theme::S(), 540.f * theme::S()),
                                 ImGuiCond_Appearing);
        if (ImGui::Begin("Model & Telemetry###transparency", ui::transparencyPanelOpen(), pflags))
            ui::drawTransparencyPanel(s);
        ImGui::End();
    }

    // T-U6 Simulation Studies panel — closable, screenshot-verifiable via AI_CAD_SHOT=sim_studies.
    // Floats centered on first open. Shows FEA/CFD study list with honest solver caveats
    // (Cd uncalibrated for LBM per SIM_AUDIT.md; convergence note for FEA).
    // Zero footprint while closed.
    if (*ui::simStudiesPanelOpen()) {
        const ImGuiViewport* vp3 = ImGui::GetMainViewport();
        ImGui::SetNextWindowPos(ImVec2(vp3->WorkPos.x + vp3->WorkSize.x * 0.5f,
                                       vp3->WorkPos.y + vp3->WorkSize.y * 0.5f),
                                ImGuiCond_Appearing, ImVec2(0.5f, 0.5f));
        ImGui::SetNextWindowSize(ImVec2(560.f * theme::S(), 520.f * theme::S()),
                                 ImGuiCond_Appearing);
        if (ImGui::Begin("Simulation Studies###sim_studies", ui::simStudiesPanelOpen(), pflags))
            ui::drawSimStudiesPanel(s);
        ImGui::End();
    }

    // T-U7 Piping network viewer — read-only v1, screenshot-verifiable via AI_CAD_SHOT=piping_view.
    if (*ui::pipingPanelOpen()) {
        const ImGuiViewport* vp7a = ImGui::GetMainViewport();
        ImGui::SetNextWindowPos(ImVec2(vp7a->WorkPos.x + vp7a->WorkSize.x * 0.5f,
                                       vp7a->WorkPos.y + vp7a->WorkSize.y * 0.5f),
                                ImGuiCond_Appearing, ImVec2(0.5f, 0.5f));
        ImGui::SetNextWindowSize(ImVec2(760.f * theme::S(), 520.f * theme::S()),
                                 ImGuiCond_Appearing);
        if (ImGui::Begin("Piping Network###piping_view", ui::pipingPanelOpen(), pflags))
            ui::drawPipingPanel(s);
        ImGui::End();
    }

    // T-U7 Hydraulic schematic viewer — read-only v1, AI_CAD_SHOT=hydraulic_view.
    if (*ui::hydraulicPanelOpen()) {
        const ImGuiViewport* vp7b = ImGui::GetMainViewport();
        ImGui::SetNextWindowPos(ImVec2(vp7b->WorkPos.x + vp7b->WorkSize.x * 0.5f,
                                       vp7b->WorkPos.y + vp7b->WorkSize.y * 0.5f),
                                ImGuiCond_Appearing, ImVec2(0.5f, 0.5f));
        ImGui::SetNextWindowSize(ImVec2(760.f * theme::S(), 520.f * theme::S()),
                                 ImGuiCond_Appearing);
        if (ImGui::Begin("Hydraulic Schematic###hydraulic_view", ui::hydraulicPanelOpen(), pflags))
            ui::drawHydraulicPanel(s);
        ImGui::End();
    }

    // T-U7 Structural frame viewer — read-only v1, AI_CAD_SHOT=structural_view.
    if (*ui::structuralPanelOpen()) {
        const ImGuiViewport* vp7c = ImGui::GetMainViewport();
        ImGui::SetNextWindowPos(ImVec2(vp7c->WorkPos.x + vp7c->WorkSize.x * 0.5f,
                                       vp7c->WorkPos.y + vp7c->WorkSize.y * 0.5f),
                                ImGuiCond_Appearing, ImVec2(0.5f, 0.5f));
        ImGui::SetNextWindowSize(ImVec2(760.f * theme::S(), 520.f * theme::S()),
                                 ImGuiCond_Appearing);
        if (ImGui::Begin("Structural Frame###structural_view", ui::structuralPanelOpen(), pflags))
            ui::drawStructuralPanel(s);
        ImGui::End();
    }

    // T-U9 Sample Gallery panel — closable, screenshot-verifiable via AI_CAD_SHOT=gallery.
    // Floats centered; zero footprint while closed.
    if (*ui::galleryPanelOpen()) {
        const ImGuiViewport* vp4 = ImGui::GetMainViewport();
        ImGui::SetNextWindowPos(ImVec2(vp4->WorkPos.x + vp4->WorkSize.x * 0.5f,
                                       vp4->WorkPos.y + vp4->WorkSize.y * 0.5f),
                                ImGuiCond_Appearing, ImVec2(0.5f, 0.5f));
        ImGui::SetNextWindowSize(ImVec2(620.f * theme::S(), 680.f * theme::S()),
                                 ImGuiCond_Appearing);
        if (ImGui::Begin("Sample Gallery###gallery", ui::galleryPanelOpen(), pflags))
            ui::drawGalleryPanel(s);
        ImGui::End();
    }

    // Live plan/action stream (run tree + event trail) + gate steering (approve/reject).
    if (*ui::missionControlPanelOpen()) {
        const ImGuiViewport* vp5 = ImGui::GetMainViewport();
        ImGui::SetNextWindowPos(ImVec2(vp5->WorkPos.x + vp5->WorkSize.x * 0.5f,
                                       vp5->WorkPos.y + vp5->WorkSize.y * 0.5f),
                                ImGuiCond_Appearing, ImVec2(0.5f, 0.5f));
        ImGui::SetNextWindowSize(ImVec2(640.f * theme::S(), 780.f * theme::S()),
                                 ImGuiCond_Appearing);
        if (ImGui::Begin("Mission Control###mission_control", ui::missionControlPanelOpen(), pflags))
            ui::drawMissionControlPanel(s);
        ImGui::End();
    }

    // T-W17 Agent Inspector — advisor/agent observability, replayable from a run
    // dir. Screenshot-verifiable via AI_CAD_SHOT=agent_inspector[_empty].
    if (*ui::agentInspectorPanelOpen()) {
        const ImGuiViewport* vpi = ImGui::GetMainViewport();
        ImGui::SetNextWindowPos(ImVec2(vpi->WorkPos.x + vpi->WorkSize.x * 0.5f,
                                       vpi->WorkPos.y + vpi->WorkSize.y * 0.5f),
                                ImGuiCond_Appearing, ImVec2(0.5f, 0.5f));
        ImGui::SetNextWindowSize(ImVec2(680.f * theme::S(), 860.f * theme::S()), ImGuiCond_Appearing);
        if (ImGui::Begin("Agent Inspector###agent_inspector", ui::agentInspectorPanelOpen(), pflags))
            ui::drawAgentInspectorPanel(s);
        ImGui::End();
    }
    // T-W17 Agent Watch grid — live part previews per agent doc.
    // Screenshot-verifiable via AI_CAD_SHOT=watch_grid.
    if (*ui::watchGridPanelOpen()) {
        const ImGuiViewport* vpw = ImGui::GetMainViewport();
        ImGui::SetNextWindowPos(ImVec2(vpw->WorkPos.x + vpw->WorkSize.x * 0.5f,
                                       vpw->WorkPos.y + vpw->WorkSize.y * 0.5f),
                                ImGuiCond_Appearing, ImVec2(0.5f, 0.5f));
        ImGui::SetNextWindowSize(ImVec2(720.f * theme::S(), 560.f * theme::S()), ImGuiCond_Appearing);
        if (ImGui::Begin("Agent Watch###watch_grid", ui::watchGridPanelOpen(), pflags))
            ui::drawWatchGridPanel(s);
        ImGui::End();
    }
    // T-W17 Agent Settings now renders inline in the Settings modal's AI tab
    // (settings consolidation: the popup is the only settings surface).

    // T-DP17 Agent DAG — interactive node-graph of the running workflow.
    // Compact mode embedded in overlay; Full mode here as a standalone window.
    if (*ui::agentDagPanelOpen()) {
        const ImGuiViewport* vpd = ImGui::GetMainViewport();
        ImGui::SetNextWindowPos(ImVec2(vpd->WorkPos.x + vpd->WorkSize.x * 0.5f,
                                       vpd->WorkPos.y + vpd->WorkSize.y * 0.5f),
                                ImGuiCond_Appearing, ImVec2(0.5f, 0.5f));
        ImGui::SetNextWindowSize(ImVec2(820.f * theme::S(), 640.f * theme::S()),
                                 ImGuiCond_Appearing);
        if (ImGui::Begin("Agent DAG###agent_dag", ui::agentDagPanelOpen(), pflags))
            ui::drawAgentDagPanel(s, aicad::ui::DagWidgetMode::Full);
        ImGui::End();
    }

    ImGui::PopStyleColor();

    if (s.paletteOpen) ui::drawCommandPalette(s);

    ui::maybeOpenFirstRun(s);
    ui::drawFirstRunDialog(s);
    ui::drawSettingsModal(s, false);
    ui::drawProfilerWindow(s);
    ui::drawCommandSearch(s);
    ui::drawToasts(s);
    ui::applyCursorRequest(s.window);
}

} // namespace app
