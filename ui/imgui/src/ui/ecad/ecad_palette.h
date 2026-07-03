// EcadPalette — the ECAD canvas/panel's color roles, ported from host's ui::palette (ui_theme.h)
// roles the ECAD scaffold used (sketchGrid/sketchInk/sketchUnder/sketchInfer, axisX/Y/Z, statusOk/
// Warn/Err, accent/accentDim, selectionHighlight/hoverHighlight, text/textDim/textDisabled,
// windowBg/panelBg/viewportBottom). The new tree's theme::Palette (tokens.h) doesn't carry these
// named roles, so — same seam as ui::sketch::SketchPalette (sketch_palette.h) — this is a small,
// self-contained, host-value-seeded struct local to ecad/**. Values are the host DARK palette
// (ui_theme.cpp fillDark); the ECAD canvas, like the sketch canvas, is not yet theme-adaptive.
//
// Ported for H2 (T-G8 wave 4).
#pragma once

#include "imgui.h"

namespace ui::ecad {

struct EcadPalette {
    float scale = 1.f;  // set from theme::S() once per draw (see ecad_panel.cpp)

    float windowBg[4]       = {0.082f, 0.090f, 0.110f, 1.f};
    float panelBg[4]        = {0.094f, 0.106f, 0.129f, 1.f};
    float viewportBottom[4] = {0.933f, 0.941f, 0.953f, 1.f};

    float text[4]           = {0.898f, 0.918f, 0.945f, 1.f};
    float textDim[4]        = {0.620f, 0.660f, 0.720f, 1.f};
    float textDisabled[4]   = {0.471f, 0.502f, 0.549f, 1.f};

    float accent[4]         = {0.176f, 0.831f, 0.749f, 1.f};
    float accentDim[4]      = {0.118f, 0.553f, 0.498f, 1.f};

    float selectionHighlight[4] = {0.176f, 0.831f, 0.749f, 0.22f};
    float hoverHighlight[4]     = {0.176f, 0.831f, 0.749f, 0.40f};

    float axisX[4] = {0.910f, 0.380f, 0.380f, 1.f};
    float axisY[4] = {0.470f, 0.820f, 0.450f, 1.f};
    float axisZ[4] = {0.420f, 0.620f, 0.960f, 1.f};

    float sketchUnder[4] = {0.337f, 0.612f, 0.960f, 1.f};
    float sketchGrid[4]  = {0.520f, 0.560f, 0.620f, 0.18f};
    float sketchInk[4]   = {0.090f, 0.100f, 0.125f, 1.f};
    float sketchInfer[4] = {0.950f, 0.650f, 0.200f, 0.85f};

    float statusOk[4]   = {0.176f, 0.831f, 0.749f, 1.f};
    float statusWarn[4] = {0.953f, 0.761f, 0.420f, 1.f};
    float statusErr[4]  = {0.961f, 0.549f, 0.420f, 1.f};

    ImU32 toU32(const float rgba[4], float alphaMul = 1.0f) const {
        return ImGui::ColorConvertFloat4ToU32(
            ImVec4(rgba[0], rgba[1], rgba[2], rgba[3] * alphaMul));
    }
    ImVec4 toV4(const float rgba[4], float alphaMul = 1.0f) const {
        return ImVec4(rgba[0], rgba[1], rgba[2], rgba[3] * alphaMul);
    }
};

EcadPalette& ecadPalette();

}  // namespace ui::ecad
