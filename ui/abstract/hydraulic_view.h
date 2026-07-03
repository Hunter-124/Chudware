#pragma once
// Framework-agnostic view-model adapter for the hydraulic viewer panel (T-U7).
// Wraps net_canvas_view.h with hydraulic-specific JSON parsing.
// No ImGui/GL includes.

#include "net_canvas_view.h"
#include <string>

namespace aicad::ui {

// Populate a NetCanvasModel from an aicad.hydraulic/1 doc JSON + optional sizing results.
// doc_json      : hydraulic doc with "components" and "hoses" arrays.
// sizing_json   : optional dict keyed by component id:
//                 {"c1":{"extend_force_kn":37.3,"extend_flow_lpm":2.4},...} or "".
void hydraulicDocToCanvas(const std::string& doc_json,
                          const std::string& sizing_json,
                          NetCanvasModel& out);

}  // namespace aicad::ui
