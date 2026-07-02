#include "app/application.h"
#include "app/app_state.h"
#include "ui/layout.h"
#include "ui/panels.h"
#include "ui/viewport.h"
#include "ui/scenarios.h"
#include "ui/cursors.h"
#include "ui/command_search.h"
#include "ui/file_explorer.h"
#include "ui/settings_modal.h"
#include "theme/theme.h"

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

    float midH = ImGui::GetContentRegionAvail().y - statusH;
    if (midH < 0) midH = 0;

    ImGui::PushStyleColor(ImGuiCol_ChildBg, ImVec4(0, 0, 0, 0));
    ImGui::BeginChild("##rail", ImVec2(railW, midH), false,
                      ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoScrollWithMouse);
    ui::drawActivityRail(s, railW);
    ImGui::EndChild();
    ImGui::PopStyleColor();

    ImGui::SameLine(0, 0);

    ImGui::PushStyleColor(ImGuiCol_WindowBg, ImVec4(0, 0, 0, 0));
    ImGui::PushStyleColor(ImGuiCol_DockingEmptyBg, ImVec4(0, 0, 0, 0));
    ImGui::PushStyleColor(ImGuiCol_Separator, ImVec4(0, 0, 0, 0));
    ImGui::PushStyleColor(ImGuiCol_SeparatorHovered, ImVec4(0, 0, 0, 0));
    ImGui::PushStyleColor(ImGuiCol_SeparatorActive, ImVec4(0, 0, 0, 0));
    const float dockW = ImGui::GetContentRegionAvail().x;
    ImGuiID dockId = ImGui::GetID("##dockspace");
    ImGui::DockSpace(dockId, ImVec2(0, midH), ImGuiDockNodeFlags_PassthruCentralNode);
    if (!layoutBuilt_) {
        buildDefaultLayout(dockId, ImVec2(dockW > 1.f ? dockW : 1440.f, midH));
        layoutBuilt_ = true;
    }
    ui::drawDockSplitters(dockId);
    ImGui::PopStyleColor(5);

    ui::drawStatusBar(s, statusH);

    ImGui::End();

    ImGui::PushStyleColor(ImGuiCol_WindowBg, ImVec4(0,0,0,0));
    ImGuiWindowFlags pflags = ImGuiWindowFlags_NoCollapse | ImGuiWindowFlags_NoBackground;
    if (ImGui::Begin("Browser###left", nullptr, pflags)) {
        if (!s.activeTool.empty()) ui::drawInspector(s);
        ui::drawLeftDock(s);
    }
    ImGui::End();

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

    if (ImGui::Begin("Assistant###agent", nullptr, pflags))
        ui::drawAgentPanel(s);
    ImGui::End();
    ImGui::PopStyleColor();

    if (s.paletteOpen) ui::drawCommandPalette(s);

    ui::drawSettingsModal(s, false);
    ui::drawProfilerWindow(s);
    ui::drawCommandSearch(s);
    ui::drawToasts(s);
    ui::applyCursorRequest(s.window);
}

} // namespace app
