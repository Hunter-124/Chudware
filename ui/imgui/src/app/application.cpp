#include "app/application.h"
#include "app/app_state.h"
#include "ui/panels.h"
#include "theme/theme.h"

#include "viewport_impl.h"
#include "render/aicm_loader.h"

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

void Application::updateViewportBackdrop(AppState& s) {
    if (!glViewportReady_ || !glViewport_ || !s.scene) return;
    if (!s.engineReady || s.engine.aicmPath().empty()) return;
    const int w = s.scene->width(), h = s.scene->height();
    if (w < 8 || h < 8) return;

    render::DocMesh doc;
    std::string err;
    if (!render::loadAicmFile(s.engine.aicmPath(), doc, err)) return;

    glViewport_->resize(w, h);
    if (!glViewport_->setDoc(doc, err)) return;
    glViewport_->setSceneRevision(s.engine.revision());
    glViewport_->render();

    const auto bd = glViewport_->backdrop();
    theme::ViewportBackdrop tbd;
    tbd.textureId = bd.textureId;
    tbd.uvMin = ImVec2(bd.uvMin[0], bd.uvMin[1]);
    tbd.uvMax = ImVec2(bd.uvMax[0], bd.uvMax[1]);
    theme::setViewportBackdrop(tbd);
}

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

    s.rpc.drain();
    s.tickDemoAgent(io.DeltaTime);
    updateViewportBackdrop(s);

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
    ImGuiID dockId = ImGui::GetID("##dockspace");
    ImGui::DockSpace(dockId, ImVec2(0, midH), ImGuiDockNodeFlags_PassthruCentralNode);
    ImGui::PopStyleColor(2);
    if (!layoutBuilt_) buildDockLayout(dockId);

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
                     ImGuiWindowFlags_NoScrollWithMouse))
        ui::drawViewport(s);
    ImGui::End();
    ImGui::PopStyleVar();

    if (ImGui::Begin("Assistant###agent", nullptr, pflags))
        ui::drawAgentPanel(s);
    ImGui::End();
    ImGui::PopStyleColor();

    if (s.paletteOpen) ui::drawCommandPalette(s);
}

} // namespace app
