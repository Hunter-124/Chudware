#include "legend.h"

#include <cstdio>
#include <string>

#include "imgui.h"
#include "app/app_state.h"
#include "theme/theme.h"
#include "ui/viewport.h"

namespace ui {

namespace {

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

void drawLegend(app::AppState& st, ImDrawList* dl, const ImVec2& r0, const ImVec2& r1) {
    (void)st;
    const ViewportState& vs = viewportState();
    if (!vs.legendOn) return;

    const float S = theme::S();
    const ImU32 bord = TCOL(glassBorder);

    const float barW   = 18.f * S;
    const float barH   = 200.f * S;
    const float pad    = 8.f * S;
    const float labelW = 64.f * S;

    float boxW = barW + labelW + pad * 3.0f;
    float boxH = barH + pad * 3.0f + 18.f * S;
    ImVec2 b1(r1.x - 12.f * S, r1.y - 12.f * S);
    ImVec2 b0(b1.x - boxW, b1.y - boxH);

    const theme::AdaptiveSurface& surf =
        theme::adaptivePanel(theme::panelId("legend_glass"), ImRect(b0, b1));
    theme::glassPanel(dl, ImRect(b0, b1), theme::MaterialLevel::UltraThin, 6.f, surf);

    std::string title = vs.legendTitle.empty() ? std::string("Result") : vs.legendTitle;
    if (!vs.legendUnits.empty()) title += "  (" + vs.legendUnits + ")";
    theme::glassText(dl, nullptr, ImGui::GetFontSize(), ImVec2(b0.x + pad, b0.y + pad), surf,
                     title.c_str());

    ImVec2 barTL(b0.x + pad, b0.y + pad + 18.f * S);
    ImVec2 barBR(barTL.x + barW, barTL.y + barH);
    const int segs = 64;
    for (int i = 0; i < segs; ++i) {
        float t1 = 1.0f - (float)i / segs;
        float t2 = 1.0f - (float)(i + 1) / segs;
        float y0 = barTL.y + barH * ((float)i / segs);
        float y1 = barTL.y + barH * ((float)(i + 1) / segs);
        dl->AddRectFilledMultiColor(ImVec2(barTL.x, y0), ImVec2(barBR.x, y1),
                                    rampColor(t1), rampColor(t1), rampColor(t2), rampColor(t2));
    }
    dl->AddRect(barTL, barBR, bord);

    float vmin = vs.legendMin, vmax = vs.legendMax;
    auto label = [&](float frac, float value) {
        std::string s = fmtNum(value);
        float y = barTL.y + barH * (1.0f - frac);
        dl->AddLine(ImVec2(barBR.x, y), ImVec2(barBR.x + 4.f * S, y),
                    ImGui::ColorConvertFloat4ToU32(surf.textSecondary));
        theme::glassText(dl, nullptr, ImGui::GetFontSize(),
                         ImVec2(barBR.x + 7.f * S, y - 7.f * S), surf, s.c_str());
    };
    label(1.0f, vmax);
    label(0.5f, (vmin + vmax) * 0.5f);
    label(0.0f, vmin);
    (void)r0;
}

}  // namespace ui
