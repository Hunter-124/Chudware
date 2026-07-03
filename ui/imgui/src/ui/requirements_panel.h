// T-U5 Requirements & verification panel — public API.
// Built on ui/imgui/src/ui/panel_scaffold.h (T-U1 kit); view-model lives in
// ui/abstract/req_view.h (no ImGui includes there).  Screenshot-verifiable via
// AI_CAD_SHOT=requirements (worker one-shot shot, same timing group as bomShotDone).
//
// Waive contract: the UI may only call req_status with who="user" for the
// user_confirmed and waived transitions; the waive_reason must be non-empty before
// the Waive button is enabled (engine_oracle/requirements/requirements.py:set_status).
#pragma once

#include <string>

namespace app { struct AppState; }

namespace ui {

// Body only (no ImGui::Begin/End) — call inside a docked/closable window, same
// convention as drawBomPanel() / drawPartsPanel().
void drawRequirementsPanel(app::AppState& s);

// Open the (closable) requirements pane.  Costs nothing while closed.
void openRequirementsPanel();

// The pane's open flag — application.cpp passes it to ImGui::Begin as p_open.
bool* requirementsPanelOpen();

// Seed the panel model from a req_report JSON result (scenario/shot use only —
// populates gReqModel so the headless screenshot shows real rows without a live
// async RPC round-trip).  json_result is the raw result dict returned by req_report.
void seedRequirementsPanelFromJson(const std::string& json_result);

} // namespace ui
