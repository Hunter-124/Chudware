#include <string>
// T-U6 Simulation Studies panel for the ui/imgui shell.
// Study list (FEA static/modal, CFD flow), result summaries with honest solver caveats
// (convergence notes, Cd calibration status per SIM_AUDIT.md), narration from
// describe_results, and per-study re-run action.
//
// Built on panel_scaffold.h + theme:: tokens (docs/UI_PATTERN_LIBRARY.md §1).
// Body-only (no Begin/End) — application.cpp owns the window Begin/End and passes p_open.
// Headless-verifiable: AI_CAD_SHOT=sim_studies exercises the populated study list.
#pragma once

namespace app { struct AppState; }

namespace ui {

// Draw the Simulation Studies panel body. Call inside an open ImGui::Begin/End block.
void drawSimStudiesPanel(app::AppState& s);

// Open the panel and reveal it (called by ribbon command or scenario).
void openSimStudiesPanel();

// Close flag — application.cpp passes this to ImGui::Begin as p_open.
bool* simStudiesPanelOpen();

// Seed the panel directly from a serialised sim_study_list JSON response (for headless
// screenshot scenarios when the worker isn't ready or no doc is loaded).
void seedSimStudiesPanelFromJson(const std::string& json);

}  // namespace ui
