// Manufacture workspace body — delegates to CAM manager + adds workspace chrome.
//
// Backend: drawCamManager(s) (cam_panel.h) draws the CAM job tree, toolpath list,
// setup panels, and simulation controls. This file wraps that call so phase-2
// agents can add workspace-level chrome (toolbar, material-removal stats, post
// controls) without editing cam_panel.cpp or viewport.cpp.
//
// Phase-2 agents: add a compact toolbar strip BEFORE the drawCamManager call —
// the strip should show the same New Job / Add Setup / Add Tool / Generate / Post
// commands from cmd::Workspace::Manufacture (cmd::groupsFor(W::Manufacture)) but
// rendered as a mini context bar inside the content area (not the glass ribbon).
#include "ui/workspace_manufacture.h"
#include "ui/cam_panel.h"
#include "app/app_state.h"
#include "theme/theme.h"
#include "imgui.h"

namespace ui {

void drawManufactureWorkspace(app::AppState& s) {
    // TODO(phase-2 cam): add workspace header bar here — "Manufacture" title strip +
    // breadcrumb (Job / Setup / Operation) and Generate / Post shortcut buttons.
    // See cam_panel.h drawCamWorkspacePanel for the CAM panel API.

    // Delegate to the existing CAM manager panel (live backend — do not replace).
    ui::drawCamManager(s);
}

} // namespace ui
