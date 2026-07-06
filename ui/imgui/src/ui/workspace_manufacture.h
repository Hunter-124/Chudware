// Manufacture workspace body — CAM / machining workspace (OverlayHosting kind).
//
// Phase-2 hook: fill drawManufactureWorkspace() with CAM-specific chrome. The
// CAM manager backend is drawCamManager() / drawCamWorkspacePanel() (cam/cam_panel.h).
// Reference-geometry commands (ref_plane / ref_axis / ref_point) are already
// registered for this workspace in the command table — dispatch is unchanged.
//
// Wiring: viewport.cpp calls drawManufactureWorkspace() when workspace == Manufacture
// (or camActive is true) instead of the inline drawCamManager() call that was there.
// The 3D scene still renders beneath this (OverlayHosting kind — not a full takeover).
#pragma once

namespace app { struct AppState; }

namespace ui {

// Manufacture workspace body. Called from viewport.cpp content-swap path when the
// active workspace is Manufacture (camActive or workspace==Manufacture).
// Phase-2: add a workspace toolbar strip (New Job / Setup / Generate / Post) above
// the CAM manager panel, then call drawCamManager(s) for the job tree + toolpaths.
void drawManufactureWorkspace(app::AppState& s);

} // namespace ui
