// T-U8 Model & telemetry transparency panel — public API.
// Built on ui/imgui/src/ui/panel_scaffold.h (T-U1 kit); view-model lives in
// ui/abstract/transparency_view.h (no ImGui includes there).
// Screenshot-verifiable via AI_CAD_SHOT=transparency (worker one-shot).
//
// Erik-ratified rules that bind this panel:
//   - Consent default OFF; changing tier requires explicit user action (radio buttons).
//   - Canary opt-in default OFF; the toggle adds/removes local machine ID from opt_in
//     list via serving_set_canary — it never auto-promotes a canary to stable.
//   - Which-brain is display-only; offline or no-routes → "not serving" state shown.
//   - serving_promote / serving_evaluate are never wired to UI actions here.
#pragma once

#include <string>

namespace app { struct AppState; }

namespace ui {

// Body only (no ImGui::Begin/End) — call inside a docked/closable window, same
// convention as drawBomPanel() / drawRequirementsPanel().
void drawTransparencyPanel(app::AppState& s);

// Open the (closable) transparency pane. Costs nothing while closed.
// Also refreshes the consent index and triggers a serving_status RPC if idle.
void openTransparencyPanel();

// The pane's open flag — application.cpp passes it to ImGui::Begin as p_open.
bool* transparencyPanelOpen();

// Seed the panel model from a JSON fixture (scenario/shot use only — populates
// gTransparencyModel so the headless screenshot shows real rows without a live
// async RPC round-trip). json is a serving_status result object.
void seedTransparencyPanelForShot(const std::string& serving_status_json);

} // namespace ui
