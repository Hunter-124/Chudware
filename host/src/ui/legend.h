// Result legend (C19): a vertical colour bar with min/max/units + a title, drawn as a viewport
// overlay (bottom-right) whenever a result colormap is active. The ramp mirrors the renderer's
// SolidWorks-style jet LUT (blue->green->red) so the bar matches the shaded body.
#pragma once

struct AppState;
struct ImDrawList;
struct ImVec2;

namespace ui {

// Draw the legend into the given draw list, anchored within the viewport rect [r0,r1].
// No-op when st.legendOn is false. Reads st.legendMin/Max/Title/Units.
void drawLegend(AppState& st, ImDrawList* dl, const ImVec2& r0, const ImVec2& r1);

}  // namespace ui
