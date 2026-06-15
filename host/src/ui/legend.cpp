// Result legend overlay (C19) — see legend.h.
#include "legend.h"

#include <cstdio>
#include <string>

#include "imgui.h"

#include "metrics.h"
#include "../core/app_state.h"
#include "../ui_theme.h"

namespace ui {

namespace {
// Same control stops as Renderer::buildLut (blue->cyan->green->yellow->red), in sRGB 0..1.
struct Stop { float t, r, g, b; };
const Stop kStops[] = {
    {0.00f, 0.00f, 0.10f, 0.55f},
    {0.25f, 0.00f, 0.70f, 0.90f},
    {0.50f, 0.10f, 0.80f, 0.20f},
    {0.75f, 0.95f, 0.85f, 0.00f},
    {1.00f, 0.85f, 0.10f, 0.10f},
};
const int kNS = (int)(sizeof(kStops) / sizeof(kStops[0]));

ImU32 rampColor(float t) {
    if (t < 0) t = 0; if (t > 1) t = 1;
    int k = 0;
    while (k < kNS - 2 && t > kStops[k + 1].t) ++k;
    float span = kStops[k + 1].t - kStops[k].t;
    if (span < 1e-6f) span = 1e-6f;
    float f = (t - kStops[k].t) / span;
    float r = kStops[k].r + (kStops[k + 1].r - kStops[k].r) * f;
    float g = kStops[k].g + (kStops[k + 1].g - kStops[k].g) * f;
    float b = kStops[k].b + (kStops[k + 1].b - kStops[k].b) * f;
    return ImGui::ColorConvertFloat4ToU32(ImVec4(r, g, b, 1.0f));
}

std::string fmtNum(float v) {
    char buf[32];
    float a = v < 0 ? -v : v;
    if (a != 0.0f && (a < 0.01f || a >= 100000.0f))
        std::snprintf(buf, sizeof(buf), "%.2e", v);
    else
        std::snprintf(buf, sizeof(buf), "%.3g", v);
    return buf;
}
}  // namespace

void drawLegend(AppState& st, ImDrawList* dl, const ImVec2& r0, const ImVec2& r1) {
    if (!st.legendOn) return;

    const auto& pal = ui::palette;
    const ImU32 bg = ui::Palette::toU32(pal.overlayBg);
    const ImU32 bord = ui::Palette::toU32(pal.overlayBorder);
    const ImU32 txt = ui::Palette::toU32(pal.overlayText);

    // Geometry: a tall thin bar near the right edge, vertically centred-ish in the lower half.
    const float barW   = ui::metrics::px(18);
    const float barH   = ui::metrics::px(200);
    const float pad    = ui::metrics::px(8);
    const float labelW = ui::metrics::px(64);

    float boxW = barW + labelW + pad * 3.0f;
    float boxH = barH + pad * 3.0f + ui::metrics::px(18);  // + title row
    ImVec2 b1(r1.x - ui::metrics::px(12), r1.y - ui::metrics::px(12));
    ImVec2 b0(b1.x - boxW, b1.y - boxH);

    dl->AddRectFilled(b0, b1, bg, ui::metrics::chipRound());
    dl->AddRect(b0, b1, bord, ui::metrics::chipRound());

    // Title row.
    std::string title = st.legendTitle.empty() ? std::string("Result") : st.legendTitle;
    if (!st.legendUnits.empty()) title += "  (" + st.legendUnits + ")";
    dl->AddText(ImVec2(b0.x + pad, b0.y + pad), txt, title.c_str());

    // Colour bar (top = max, bottom = min).
    ImVec2 barTL(b0.x + pad, b0.y + pad + 18.0f);
    ImVec2 barBR(barTL.x + barW, barTL.y + barH);
    const int segs = 64;
    for (int i = 0; i < segs; ++i) {
        float t1 = 1.0f - (float)i / segs;        // top is max
        float t2 = 1.0f - (float)(i + 1) / segs;
        float y0 = barTL.y + barH * ((float)i / segs);
        float y1 = barTL.y + barH * ((float)(i + 1) / segs);
        dl->AddRectFilledMultiColor(ImVec2(barTL.x, y0), ImVec2(barBR.x, y1),
                                    rampColor(t1), rampColor(t1), rampColor(t2), rampColor(t2));
    }
    dl->AddRect(barTL, barBR, bord);

    // Tick labels: max, mid, min.
    float vmin = st.legendMin, vmax = st.legendMax;
    auto label = [&](float frac, float value) {
        std::string s = fmtNum(value);
        float y = barTL.y + barH * (1.0f - frac);
        dl->AddLine(ImVec2(barBR.x, y), ImVec2(barBR.x + ui::metrics::px(4), y), txt);
        dl->AddText(ImVec2(barBR.x + ui::metrics::px(7), y - ui::metrics::px(7)), txt, s.c_str());
    };
    label(1.0f, vmax);
    label(0.5f, (vmin + vmax) * 0.5f);
    label(0.0f, vmin);
}

}  // namespace ui
