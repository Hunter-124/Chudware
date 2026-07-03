#pragma once
// Framework-agnostic view-model adapter for the piping viewer panel (T-U7).
// Wraps net_canvas_view.h with piping-specific JSON parsing.
// No ImGui/GL includes.

#include "net_canvas_view.h"
#include <string>

namespace aicad::ui {

// Populate a NetCanvasModel from an aicad.piping/1 doc JSON + optional solve result.
// doc_json   : the raw piping doc (has "runs" and "fittings" arrays).
// solve_json : the solve result dict ({"ok":true,"links":[...],"nodes":[...]}) or "".
//              May be empty; nodes/edges are always drawn, results only when present.
void pipingDocToCanvas(const std::string& doc_json,
                       const std::string& solve_json,
                       NetCanvasModel& out);

}  // namespace aicad::ui
