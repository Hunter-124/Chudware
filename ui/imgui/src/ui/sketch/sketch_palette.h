#pragma once
#include "imgui.h"

namespace ui::sketch {

// Host ui_theme sketch/overlay roles (float RGBA) — kept local so sketch code needs no host/ includes.
struct SketchPalette {
    float scale = 1.f;

    float overlayBg[4]     = {0.039f, 0.055f, 0.075f, 0.843f};
    float overlayBorder[4] = {0.176f, 0.831f, 0.749f, 0.275f};
    float overlayText[4]   = {0.886f, 0.906f, 0.941f, 0.941f};
    float accent[4]        = {0.176f, 0.831f, 0.749f, 1.f};
    float sketchUnder[4]   = {0.337f, 0.612f, 0.960f, 1.f};
    float sketchFull[4]    = {0.090f, 0.100f, 0.125f, 1.f};
    float sketchOver[4]    = {0.961f, 0.549f, 0.420f, 1.f};
    float sketchGrid[4]    = {0.520f, 0.560f, 0.620f, 0.18f};
    float sketchInk[4]     = {0.090f, 0.100f, 0.125f, 1.f};
    float sketchDimLine[4] = {0.165f, 0.184f, 0.220f, 1.f};
    float sketchRelBg[4]     = {0.550f, 0.780f, 0.550f, 0.90f};
    float sketchRelBorder[4] = {0.300f, 0.520f, 0.300f, 1.f};
    float sketchInfer[4]     = {0.950f, 0.650f, 0.200f, 0.85f};
    float statusErr[4]  = {0.961f, 0.549f, 0.420f, 1.f};
    float statusWarn[4] = {0.957f, 0.761f, 0.420f, 1.f};
    float statusBg[4]   = {0.075f, 0.086f, 0.108f, 1.f};
    float statusOk[4]   = {0.176f, 0.831f, 0.749f, 1.f};
    float hoverHighlight[4] = {0.176f, 0.831f, 0.749f, 0.40f};
    float consoleDim[4] = {0.620f, 0.660f, 0.720f, 1.f};
    float windowBg[4]   = {0.082f, 0.094f, 0.118f, 1.f};

    ImU32 toU32(const float rgba[4], float alphaMul = 1.f) const {
        float c[4] = {rgba[0], rgba[1], rgba[2], rgba[3] * alphaMul};
        return ImGui::ColorConvertFloat4ToU32(ImVec4(c[0], c[1], c[2], c[3]));
    }
};

SketchPalette& sketchPalette();

}  // namespace ui::sketch
