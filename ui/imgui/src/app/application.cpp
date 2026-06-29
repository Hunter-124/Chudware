#include "app/application.h"
#include "app/app_state.h"
#include "ui/panels.h"
#include "theme/theme.h"

#include "imgui.h"
#include "imgui_internal.h"   // DockBuilder
#include <GLFW/glfw3.h>

namespace app {

static AppState g_state;
AppState& Application::state() { return g_state; }

bool Application::init(GLFWwindow* window, const std::string& resourcesDir, float scale) {
    g_state.window = window;
    scene_.init();               // compile shaders + FBO (GL context is current here)
    g_state.scene = &scene_;
    g_state.seedDemo();
    (void)resourcesDir; (void)scale;
    return true;
}

void Application::shutdown() { scene_.shutdown(); g_state.scene = nullptr; }

void Application::buildDockLayout(ImGuiID dockId) {
    ImGui::DockBuilderRemoveNode(dockId);
    ImGui::DockBuilderAddNode(dockId, ImGuiDockNodeFlags_DockSpace);
    ImGui::DockBuilderSetNodeSize(dockId, ImGui::GetMainViewport()->WorkSize);

    ImGuiID center = dockId, left, right;
    left  = ImGui::DockBuilderSplitNode(center, ImGuiDir_Left,  0.20f, nullptr, &center);
    right = ImGui::DockBuilderSplitNode(center, ImGuiDir_Right, 0.30f, nullptr, &center);

    ImGui::DockBuilderDockWindow("Browser###left", left);
    ImGui::DockBuilderDockWindow("Viewport###viewport", center);
    ImGui::DockBuilderDockWindow("Assistant###agent", right);
    ImGui::DockBuilderFinish(dockId);
    layoutBuilt_ = true;
}

void Application::frame() {
    AppState& s = g_state;
    ImGuiIO& io = ImGui::GetIO();
    s.tickDemoAgent(io.DeltaTime);

    // keyboard: ⌘K / Ctrl+K palette
    if ((io.KeyCtrl || io.KeySuper) && ImGui::IsKeyPressed(ImGuiKey_K, false)) {
        s.paletteOpen = !s.paletteOpen;
        if (s.paletteOpen) { s.paletteQuery[0] = '\0'; s.paletteSel = 0; }
    }

    // undo / redo (engine), unless typing in a field or mid-tool / mid-agent
    if ((io.KeyCtrl || io.KeySuper) && !io.WantTextInput && s.engineReady && s.engine.hasDoc()
        && !s.agent.running && s.activeTool.empty()) {
        bool redo = (ImGui::IsKeyPressed(ImGuiKey_Y, false)) ||
                    (io.KeyShift && ImGui::IsKeyPressed(ImGuiKey_Z, false));
        if (redo) { if (s.engine.redo().ok) { s.buildTreeFromDoc(); s.status = "Redo"; } }
        else if (ImGui::IsKeyPressed(ImGuiKey_Z, false)) { if (s.engine.undo().ok) { s.buildTreeFromDoc(); s.status = "Undo"; } }
    }
    // file shortcuts
    if ((io.KeyCtrl || io.KeySuper) && !io.WantTextInput) {
        if (ImGui::IsKeyPressed(ImGuiKey_S, false)) s.doSave(io.KeyShift);
        else if (ImGui::IsKeyPressed(ImGuiKey_O, false)) s.doOpen();
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

    // --- top bars (stacked, fixed height) ---
    ui::drawTitleBar(s, titleH);
    ui::drawWorkspaceBar(s, wsH);
    ui::drawContextToolbar(s, tbH);

    // --- middle: [rail | dockspace], reserve status at the bottom ---
    float midH = ImGui::GetContentRegionAvail().y - statusH;
    if (midH < 0) midH = 0;

    ImGui::PushStyleColor(ImGuiCol_ChildBg, TVEC(bg0));
    ImGui::BeginChild("##rail", ImVec2(railW, midH), false,
                      ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoScrollWithMouse);
    ui::drawActivityRail(s, railW);
    ImGui::EndChild();
    ImGui::PopStyleColor();

    ImGui::SameLine(0, 0);

    ImGuiID dockId = ImGui::GetID("##dockspace");
    ImGui::DockSpace(dockId, ImVec2(0, midH), ImGuiDockNodeFlags_PassthruCentralNode);
    if (!layoutBuilt_) buildDockLayout(dockId);

    ui::drawStatusBar(s, statusH);

    ImGui::End();   // ##root

    // --- docked panels ---
    ImGui::PushStyleColor(ImGuiCol_WindowBg, TVEC(bg1));
    ImGuiWindowFlags pflags = ImGuiWindowFlags_NoCollapse;
    if (ImGui::Begin("Browser###left", nullptr, pflags)) {
        if (!s.activeTool.empty()) ui::drawInspector(s);
        ui::drawLeftDock(s);
    }
    ImGui::End();

    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(0, 0));
    if (ImGui::Begin("Viewport###viewport", nullptr, pflags | ImGuiWindowFlags_NoScrollbar |
                     ImGuiWindowFlags_NoScrollWithMouse))
        ui::drawViewport(s);
    ImGui::End();
    ImGui::PopStyleVar();

    if (ImGui::Begin("Assistant###agent", nullptr, pflags))
        ui::drawAgentPanel(s);
    ImGui::End();
    ImGui::PopStyleColor();

    // --- overlays ---
    if (s.paletteOpen) ui::drawCommandPalette(s);
}

} // namespace app
