#pragma once
// Shared canvas renderer for net_canvas_view.h — used by piping, hydraulic, and
// structural viewer panels (T-U7). Draws nodes as labelled boxes, edges as lines,
// and a results side-list. Pure ImDrawList rendering; no external image assets.
// ISO 1219 hydraulic symbols are drawn inline with ~12 ImDrawList primitives.

#include <string>

namespace aicad::ui { struct NetCanvasModel; }
namespace app { struct AppState; }

namespace ui {

// Draw the shared network canvas + results side-list inside the current ImGui window.
// Call after ImGui::Begin / before ImGui::End.
// model is read-only; selectedNode may be updated on click.
void drawNetCanvas(aicad::ui::NetCanvasModel& model);

}  // namespace ui
