#pragma once
// Framework-agnostic view-model adapter for the structural frame viewer panel (T-U7).
// Wraps net_canvas_view.h with structural-specific JSON parsing.
// No ImGui/GL includes.

#include "net_canvas_view.h"
#include <string>

namespace aicad::ui {

// Populate a NetCanvasModel from an aicad.structure/1 doc JSON + optional analysis results.
// doc_json     : structure doc with "elements" array (each has id, element_type,
//                section_name, material, length_mm, transform[16]).
// results_json : optional dict keyed by element id:
//                {"elem_id":{"dc_ratio":0.82,"deflection_max_mm":3.1,"ok":true},...} or "".
void structuralDocToCanvas(const std::string& doc_json,
                           const std::string& results_json,
                           NetCanvasModel& out);

}  // namespace aicad::ui
