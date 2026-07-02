// T-U3 CAM workspace panel for the ui/imgui shell.
#include <string> Hosts the job/setup/op tree, tool table
// editor, sim-result strip (machined fraction, gouges, engagement warnings from T-M2/M4), and
// post-processor / setup-sheet actions. Built on panel_scaffold.h + theme:: tokens.
// See docs/UI_PATTERN_LIBRARY.md, docs/GLASS_DESIGN_SPEC.md, and ui/abstract/cam_view.h.
//
// Body-only (no Begin/End) — application.cpp owns the window Begin/End and passes p_open.
// The panel drives the C++ core's cam_* RPCs through app::AppState::worker (WorkerClient).
// Headless-verifiable: AI_CAD_SHOT=cam_workspace exercises the populated job view.
#pragma once

namespace app { struct AppState; }

namespace ui {

// Draw the CAM workspace panel body.  Call inside an open ImGui::Begin/End block.
void drawCamWorkspacePanel(app::AppState& s);

// Open the panel and reveal it (called by ribbon command handler or scenario).
void openCamWorkspacePanel();

// Close flag — application.cpp passes this to ImGui::Begin as p_open.
bool* camWorkspacePanelOpen();

// Load an existing CAM job doc (built by the copilot fast-path or by cam_program) into the
// panel. Pulls cam_tree + cam_generate (cached toolpaths) and reveals the panel.
void camWorkspaceLoadJob(app::AppState& s, const std::string& docId);

}  // namespace ui
