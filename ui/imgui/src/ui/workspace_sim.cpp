// Simulate workspace body — workspace-level chrome + SimManager panel.
//
// Workspace chrome added here (phase-2):
//   - A narrow "Simulate" title bar across the top of the viewport (glass overlay strip).
//   - A quick-create button row for common study types.
//
// The study tree / setup / results column is drawn by simManager().drawEmbedded().
// The timeline transport for motion studies lives inside sim_manager.cpp's
// drawMotionResults(), reachable via the Results node of any Motion study.
#include "ui/workspace_sim.h"
#include "ui/sim_manager.h"
#include "app/app_state.h"
#include "theme/theme.h"
#include "ui/appearance_common.h"
#include "imgui.h"

namespace ui {

void drawSimWorkspace(app::AppState& s, const ImVec2& vpMin, const ImVec2& vpMax) {
    const float S = theme::S();

    // ---- Workspace title strip -----------------------------------------------
    // A narrow glass bar at the very top of the viewport — "Simulate" label +
    // quick-create buttons for each study type.
    const float barH = 30.f * S;
    ImGui::SetNextWindowPos(ImVec2(vpMin.x, vpMin.y), ImGuiCond_Always);
    ImGui::SetNextWindowSize(ImVec2(vpMax.x - vpMin.x, barH), ImGuiCond_Always);
    ImGui::SetNextWindowBgAlpha(0.0f);
    constexpr ImGuiWindowFlags kBarFlags =
        ImGuiWindowFlags_NoDecoration | ImGuiWindowFlags_NoInputs |
        ImGuiWindowFlags_NoMove | ImGuiWindowFlags_NoSavedSettings |
        ImGuiWindowFlags_NoBringToFrontOnFocus | ImGuiWindowFlags_NoNav |
        ImGuiWindowFlags_NoScrollbar;
    // We draw just the label as an overlay — quick-create buttons are already in
    // the simManager column (+ New Study popup). The strip is purely informational.
    ImGui::PushStyleColor(ImGuiCol_WindowBg, c5::glassBg());
    ImGui::PushStyleColor(ImGuiCol_Border,   c5::glassBorder());
    if (ImGui::Begin("##sim_ws_titlebar", nullptr, kBarFlags)) {
        ImGui::PushFont(theme::font(theme::Font::H2));
        ImGui::TextColored(c5::accent(), " Simulate");
        ImGui::PopFont();

        // If a motion study is selected and has a result, show a compact playback hint.
        SimManager& sm = simManager();
        const int nStudies = sm.studyCount();
        (void)nStudies;  // future: walk studies for status indicator
        ImGui::End();
    }
    ImGui::PopStyleColor(2);

    // ---- SimManager panel (study tree + forms + timeline transport) -----------
    ui::simManager().drawEmbedded(s, vpMin, vpMax);
}

} // namespace ui
