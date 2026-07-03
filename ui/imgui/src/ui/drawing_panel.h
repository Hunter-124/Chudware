// AI-CAD Drawing panel — 2D sheet renderer for the DRAWINGS module.
//
// Ported from host/src/ui/drawing_panel.{h,cpp} (H3). When the app is in the Drafting workspace
// (cmd::Workspace::Drafting — entered via the "make_drawing" command), the central Viewport
// window hosts this 2D sheet renderer instead of the 3D viewport — mirroring the host, which
// renders the drawing sheet INSIDE the central viewport panel rather than as a separate dock
// window (unlike CAM, which docks alongside Viewport in the shared central node). It draws the
// sheet border (from the engine's size_mm), then each view's `visible` (solid) and `hidden`
// (dashed) polylines, all transformed from sheet-mm to screen with a pan + zoom transform.
// Mouse-wheel zooms to the cursor; MMB or Space + LMB pans; a one-shot "fit" frames the sheet on
// first show.
//
// Unlike host (which keeps drawing state per-DocTab), this panel owns its state in a file-static
// singleton (no AppState fields) — the same convention cam_panel.h / preview_pane.h use — keyed
// off the active source part's doc id. The panel self-creates the drawing (drawing_new + the
// four standard views + drawing_get — byte-identical RPCs to host's AppState::makeDrawing()) the
// first time it is asked to render for a given source document, since the new tree's
// "make_drawing" command dispatch (app/app_state.cpp) only flips the workspace/doc-type flags and
// does not itself talk to the engine.
#pragma once

#include <string>

#include <nlohmann/json.hpp>

namespace app { struct AppState; }

namespace ui {

// Draw the 2D drawing sheet into the current ImGui window region. Called from
// drawViewport (viewport.cpp) once it has determined the app is in the Drafting workspace,
// in place of the 3D render for that frame. Self-creates the drawing (from the active part)
// on first call; afterwards reads + mutates only the panel's own drawing cache + pan/zoom.
void drawDrawingSheet(app::AppState& state);

// W17: dispatch a "dwg_*" ribbon command (View Layout / Annotation tabs) against the active
// drawing — adds views, arms annotation tools, opens the title-block editor, exports, etc.
// Returns true if the command was recognized + handled (false otherwise). No-op + returns true
// with a toast when no drawing has been created yet. Not yet reachable from the ribbon (ribbon
// command wiring is out of this wave's write-set — see task report), ported for completeness /
// future wiring, mirroring cam_panel's unwired camHandleCommand().
bool drawingHandleCommand(app::AppState& state, const std::string& cmd);

// T-G9 scenario/parity hooks: read-only access to the panel's active drawing (registry id +
// cached drawing_get JSON) and a host-refreshDrawingTab-equivalent refresh, so the AI_CAD_SHOT
// drawing scenario can drive the same detailing RPC sequence host's scenario drives.
std::string drawingActiveDocId();
nlohmann::json drawingActiveDoc();
void drawingRefreshActive(app::AppState& state);

}  // namespace ui
