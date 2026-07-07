// Result legend overlay (C19) — see legend.h.
#include "legend.h"

#include <algorithm>
#include <cstdio>
#include <string>
#include <vector>

#include "imgui.h"

#include "layout.h"   // ui::overlayColumnEdge — the legend dodges the pinned Copilot column
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

    // Geometry: a tall thin bar near the right edge, vertically centred-ish in the lower half.
    const float barW   = ui::metrics::px(18);
    const float barH   = ui::metrics::px(200);
    const float pad    = ui::metrics::px(8);
    const float labelW = ui::metrics::px(64);

    // Shrink-wrap the pill to the bar+labels; a long result caption ("von Mises (distortion
    // energy) (MPa)") WORD-WRAPS across the top instead of driving the pill width — sizing the
    // whole card to the one-line title left a large dead glass region right of the bar over the
    // model (visual-review fix). The title may still widen the pill a little (up to ~1.5x the
    // body) so short captions stay on one line.
    std::string title = st.legendTitle.empty() ? std::string("Result") : st.legendTitle;
    if (!st.legendUnits.empty()) title += "  (" + st.legendUnits + ")";
    const float bodyW = barW + labelW + pad * 3.0f;
    float boxW = std::max(bodyW, std::min(ImGui::CalcTextSize(title.c_str()).x + pad * 2.0f,
                                          bodyW * 1.5f));
    // Greedy word-wrap of the title to the pill's inner width (glassText draws one line per call).
    std::vector<std::string> titleLines;
    {
        const float wrapW = boxW - pad * 2.0f;
        std::string line;
        size_t i = 0;
        while (i < title.size()) {
            size_t j = title.find(' ', i);
            if (j == std::string::npos) j = title.size();
            std::string word = title.substr(i, j - i);
            std::string cand = line.empty() ? word : line + " " + word;
            if (!line.empty() && ImGui::CalcTextSize(cand.c_str()).x > wrapW) {
                titleLines.push_back(line);
                line = word;
            } else {
                line = cand;
            }
            i = j + 1;
        }
        if (!line.empty()) titleLines.push_back(line);
        if (titleLines.empty()) titleLines.push_back(title);
    }
    const float lineH  = ImGui::GetTextLineHeight() + ui::metrics::px(2);
    const float titleH = lineH * (float)titleLines.size();
    float boxH = barH + pad * 3.0f + titleH + ui::metrics::px(4);  // +4: title->bar gap
    // Anchor LEFT of the pinned Copilot column — the raw viewport right edge put the legend (the
    // payload of a results screen) underneath the floating glass column (visual-review fix).
    float rightLimit = r1.x;
    {
        const float e = ui::overlayColumnEdge(1);
        if (e > r0.x + boxW + ui::metrics::px(24) && e < r1.x) rightLimit = e;
    }
    ImVec2 b1(rightLimit - ui::metrics::px(12), r1.y - ui::metrics::px(12));
    ImVec2 b0(b1.x - boxW, b1.y - boxH);

    // Liquid-glass v2 (contrast audit P2): the legend floats over the live RESULT colormap, so the
    // legacy fixed overlayBg pill let a saturated stress field bleed through at 0.88 alpha and
    // soften the text. adaptivePanel samples the field under the pill; glassText carries the halo.
    // Regular (not UltraThin) glass: the legend needs a solid enough backing chip to stay readable
    // over saturated result colors at native scale.
    const ui::AdaptiveSurface& surf =
        ui::adaptivePanel(ui::panelId("vp_legend"), ImRect(b0, b1), ui::GlassGroup::Hud);
    ui::glassPanel(dl, ImRect(b0, b1), ui::GlassLevel::Regular, 6.0f, surf);

    // Title rows (wrapped).
    for (size_t li = 0; li < titleLines.size(); ++li)
        ui::glassText(dl, nullptr, ImGui::GetFontSize(),
                      ImVec2(b0.x + pad, b0.y + pad + lineH * (float)li), surf,
                      titleLines[li].c_str());

    // Colour bar (top = max, bottom = min).
    ImVec2 barTL(b0.x + pad, b0.y + pad + titleH + ui::metrics::px(4));
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
    dl->AddRect(barTL, barBR, ImGui::ColorConvertFloat4ToU32(surf.textTertiary));

    // Tick labels: max, mid, min. Tick marks from the surface's secondary tone; labels via
    // glassText so they hold up when the pill sits over the extremes of the ramp.
    float vmin = st.legendMin, vmax = st.legendMax;
    const ImU32 tick = ImGui::ColorConvertFloat4ToU32(surf.textSecondary);
    auto label = [&](float frac, float value) {
        std::string s = fmtNum(value);
        float y = barTL.y + barH * (1.0f - frac);
        dl->AddLine(ImVec2(barBR.x, y), ImVec2(barBR.x + ui::metrics::px(4), y), tick);
        ui::glassText(dl, nullptr, ImGui::GetFontSize(),
                      ImVec2(barBR.x + ui::metrics::px(7), y - ui::metrics::px(7)), surf, s.c_str());
    };
    label(1.0f, vmax);
    label(0.5f, (vmin + vmax) * 0.5f);
    label(0.0f, vmin);
}

}  // namespace ui
