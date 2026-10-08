// result_ramp.h — THE result colormap ramp (SolidWorks-style "jet": blue -> cyan -> green ->
// yellow -> red). Single source for Renderer::buildLut (the 256x1 LUT texture the result shader
// samples with the normalized scalar t in [0,1]) and the viewport legend bar (ui/legend.cpp), so
// the legend can never drift from the shaded body. Header-only, GL-free.
#pragma once

namespace result_ramp {

// Control stops (t, r, g, b) in sRGB 0..1, ascending t, first at 0 and last at 1.
struct Stop {
    float t, r, g, b;
};
inline constexpr Stop kStops[] = {
    {0.00f, 0.00f, 0.10f, 0.55f},  // deep blue
    {0.25f, 0.00f, 0.70f, 0.90f},  // cyan
    {0.50f, 0.10f, 0.80f, 0.20f},  // green
    {0.75f, 0.95f, 0.85f, 0.00f},  // yellow
    {1.00f, 0.85f, 0.10f, 0.10f},  // red
};
inline constexpr int kStopCount = (int)(sizeof(kStops) / sizeof(kStops[0]));

// Piecewise-linear sample of the ramp at t (clamped to [0,1]) into rgb[3] (sRGB 0..1).
inline void sample(float t, float rgb[3]) {
    if (!(t > 0.0f)) t = 0.0f;
    if (t > 1.0f) t = 1.0f;
    int k = 0;
    while (k < kStopCount - 2 && t > kStops[k + 1].t) ++k;
    float span = kStops[k + 1].t - kStops[k].t;
    if (span < 1e-6f) span = 1e-6f;
    const float f = (t - kStops[k].t) / span;
    rgb[0] = kStops[k].r + (kStops[k + 1].r - kStops[k].r) * f;
    rgb[1] = kStops[k].g + (kStops[k + 1].g - kStops[k].g) * f;
    rgb[2] = kStops[k].b + (kStops[k + 1].b - kStops[k].b) * f;
}

}  // namespace result_ramp
