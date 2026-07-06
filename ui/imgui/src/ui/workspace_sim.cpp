// Simulate workspace body — delegates to SimManager + adds workspace chrome.
//
// Backend: simManager().drawEmbedded() (sim_manager.h) draws the study-tree /
// setup / results column as a pinned glass column over the 3D viewport. This
// file wraps that call so phase-2 agents can add workspace-level chrome (toolbar,
// study browser, results summary) without touching sim_manager.cpp or viewport.cpp.
//
// Phase-2 agents: add code BEFORE the drawEmbedded call (toolbar strip) or AFTER
// (status summary). The viewport 3D scene + HUD already rendered beneath this.
#include "ui/workspace_sim.h"
#include "ui/sim_manager.h"
#include "app/app_state.h"
#include "theme/theme.h"
#include "imgui.h"

namespace ui {

void drawSimWorkspace(app::AppState& s, const ImVec2& vpMin, const ImVec2& vpMax) {
    // TODO(phase-2 sim): add a workspace header bar here — "Simulate" title strip +
    // study type selector (Static / Modal / Flow / Thermal / Fatigue). See
    // sim_manager.h SimManager for the study API surface.

    // Delegate to the existing SimManager panel (live backend — do not replace).
    ui::simManager().drawEmbedded(s, vpMin, vpMax);
}

} // namespace ui
