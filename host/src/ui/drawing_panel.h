// Chudware Drawing panel — 2D sheet renderer for the DRAWINGS module.
//
// When the active document tab is a Drawing (AppState::DocTab kind == Drawing), the central area
// hosts this 2D sheet renderer instead of the 3D viewport. It draws the sheet border (from the
// engine's size_mm), then each view's `visible` (solid) and `hidden` (dashed) polylines, all
// transformed from sheet-mm to screen with a pan + zoom transform. Mouse-wheel zooms to the cursor;
// MMB or Space + LMB pans; a one-shot "fit" frames the sheet on first show. Per-view labels show
// orientation + scale; a view with error/empty geometry shows a small "view failed" note.
//
// The renderer is read-only against the cached drawing_get JSON (DocTab::drawingDoc). View move /
// scale / annotation editing is a later wave — this pass is create + render + pan/zoom only.
#pragma once

#include <string>

struct AppState;

namespace ui {

// Draw the 2D drawing sheet for the active Drawing tab into the current ImGui window region. Called
// from drawViewportPanel after it has Begun the central "Viewport" dock window and confirmed the
// active tab is a Drawing. Reads + mutates only the active DocTab's drawing cache + sheet pan/zoom.
void drawDrawingSheet(AppState& state);

// W17: dispatch a "dwg_*" ribbon command (View Layout / Annotation tabs) against the active
// Drawing tab — adds views, arms annotation tools, opens the title-block editor, exports, etc.
// Returns true if the command was recognized + handled (false otherwise, so app.cpp can fall
// through). No-op + returns true with a toast when the active tab is not a Drawing.
bool drawingHandleCommand(AppState& state, const std::string& cmd);

}  // namespace ui
