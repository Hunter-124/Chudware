// Result legend (C19): a vertical colour bar with a "Field (unit)" title, the min/max and 1-2-5
// interior ticks, drawn as a viewport overlay in the bottom-right corner of the viewport's clear
// rect (ui::viewportClearRect) whenever a result colormap is active. The ramp is result_ramp.h —
// the same stops the renderer's LUT is built from — and the bar height is clamped to the rect.
#pragma once

struct AppState;
struct ImDrawList;
struct ImVec2;

namespace ui {

// Draw the legend into the given draw list, anchored within the viewport rect [r0,r1].
// No-op when st.legendOn is false or the rect is too short for a readable bar.
// Reads st.legendMin/Max/Title/Units.
void drawLegend(AppState& st, ImDrawList* dl, const ImVec2& r0, const ImVec2& r1);

// Width (screen px) the legend takes at the clear rect's right edge, including its margins, when
// it was drawn this or the previous frame; 0 otherwise. The viewport subtracts it from the model
// framing rect so a fitted model sits beside the legend, never under it.
float legendReserveWidth();

}  // namespace ui
