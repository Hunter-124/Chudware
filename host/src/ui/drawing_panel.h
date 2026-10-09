// Chudware Drawing panel — 2D sheet renderer for the DRAWINGS module.
//
// When the active document tab is a Drawing (AppState::DocTab kind == Drawing), the central area
// hosts this 2D sheet renderer instead of the 3D viewport. The engine compiles the whole sheet to
// sheet-mm draw primitives (frame + title block, view geometry, view decorations, annotations); the
// panel strokes them with ISO 128 paper-space line weights through a pan + zoom transform, fitted
// into the area the pinned glass columns leave clear. Mouse-wheel zooms to the cursor; MMB or
// Space + LMB pans; a one-shot "fit" frames the sheet on open. Views and annotations are
// selectable, draggable and deletable; the View Layout / Annotation ribbon tabs arm the tools.
#pragma once

#include <string>

struct AppState;

namespace ui {

// Draw the 2D drawing sheet for the active Drawing tab into the current ImGui window region. Called
// from drawViewportPanel after it has Begun the central "Viewport" dock window and confirmed the
// active tab is a Drawing. Reads + mutates only the active DocTab's drawing cache + sheet pan/zoom.
void drawDrawingSheet(AppState& state);

// The Drawing workspace's body when the active tab is NOT a drawing: a kit empty state ("No drawing
// open") with a Make Drawing action (disabled with the reason when no part is open) plus rows that
// switch to any drawing tab already open. Draws into the current window region like
// drawDrawingSheet.
void drawDrawingEmptyState(AppState& state);

// W17: dispatch a "dwg_*" ribbon command (View Layout / Annotation tabs) against the active
// Drawing tab — adds views, arms annotation tools, opens the sheet/title-block editor, re-arranges
// (dwg_arrange), exports (dwg_export_pdf / _svg / _dxf), etc. Returns true if the command was
// recognized + handled (false otherwise, so app.cpp can fall through). No-op + returns true with a
// toast when the active tab is not a Drawing.
bool drawingHandleCommand(AppState& state, const std::string& cmd);

}  // namespace ui
