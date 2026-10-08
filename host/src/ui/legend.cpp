// Result legend overlay (C19) — see legend.h.
#include "legend.h"

#include <algorithm>
#include <cctype>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

#include "imgui.h"

#include "metrics.h"
#include "viewport_panel.h"   // ui::viewportClearRect — the legend anchors in the un-occluded corner
#include "widgets/kit.h"   // kit::fmtValue / kit::unitText / kit::elide / kit::tooltip
#include "../core/app_state.h"
#include "../result_ramp.h"   // the renderer's LUT stops (single source)
#include "../ui_theme.h"

namespace ui {

namespace {

// Width the legend occupied at the clear rect's right edge (box + both margins) and the frame it
// was drawn on; legendReserveWidth() hands it to the viewport framing so the model is fitted
// beside the legend rather than under it.
float s_reserveW = 0.0f;
int   s_reserveFrame = -1;

ImU32 rampColor(float t) {
    float rgb[3];
    result_ramp::sample(t, rgb);
    return ImGui::ColorConvertFloat4ToU32(ImVec4(rgb[0], rgb[1], rgb[2], 1.0f));
}

// Field label in sentence case with proper nouns kept ("von_mises" / "Von mises stress" ->
// "von Mises stress", "tresca shear" -> "Tresca shear"). The unit goes on its own line.
std::string legendLabel(const std::string& label) {
    std::string t = label.empty() ? std::string("Result") : label;
    std::replace(t.begin(), t.end(), '_', ' ');
    std::string low = t;
    for (char& c : low) c = (char)std::tolower((unsigned char)c);
    struct Proper { const char* lower; const char* shown; };
    static const Proper kProper[] = {
        {"von mises", "von Mises"}, {"tresca", "Tresca"}, {"reynolds", "Reynolds"}};
    for (const Proper& p : kProper) {
        const size_t n = std::strlen(p.lower);
        for (size_t at = low.find(p.lower); at != std::string::npos; at = low.find(p.lower, at + n))
            t.replace(at, n, p.shown);
    }
    if (t.compare(0, 9, "von Mises") != 0 && std::islower((unsigned char)t[0]))
        t[0] = (char)std::toupper((unsigned char)t[0]);
    return t;
}

// "10^-3" in superscript digits ("10⁻³"); every glyph is in glyph_ranges.h kBody.
std::string powerOfTen(int e) {
    static const char* const kSup[10] = {
        "\xe2\x81\xb0", "\xc2\xb9", "\xc2\xb2", "\xc2\xb3", "\xe2\x81\xb4",
        "\xe2\x81\xb5", "\xe2\x81\xb6", "\xe2\x81\xb7", "\xe2\x81\xb8", "\xe2\x81\xb9"};
    std::string out = "10";
    if (e < 0) out += "\xe2\x81\xbb";   // superscript minus
    const std::string digits = std::to_string(e < 0 ? -e : e);
    for (char c : digits) out += kSup[c - '0'];
    return out;
}

// Display scale for the tick labels: values outside [1e-2, 1e6) move to a prefixed unit on the
// unit's own ladder (Pa -> MPa, N -> kN, mm -> µm, ...), else to a "×10ⁿ unit" multiplier, so a
// tick never reads "250,000,000" or "0.000012". `unit` is the canonical kit::unitText spelling.
struct LegendScale {
    double      div = 1.0;   // displayed = value / div
    std::string unit;        // unit line under the title ("" = dimensionless, no multiplier)
};

LegendScale legendScale(double vmin, double vmax, const std::string& unit) {
    LegendScale sc;
    sc.unit = unit;
    const double m = std::max(std::fabs(vmin), std::fabs(vmax));
    if (!(m > 0.0) || !std::isfinite(m) || (m >= 1e-2 && m < 1e6)) return sc;

    struct Rung { const char* unit; double factor; };   // factor relative to the ladder base
    using kit::Unit;
    const Rung pressure[] = {{kit::unitText(Unit::Pa), 1.0}, {kit::unitText(Unit::kPa), 1e3},
                             {kit::unitText(Unit::MPa), 1e6}, {kit::unitText(Unit::GPa), 1e9}};
    const Rung imperialStress[] = {{kit::unitText(Unit::psi), 1.0}, {"ksi", 1e3}};
    const Rung length[] = {{kit::unitText(Unit::um), 1e-3}, {kit::unitText(Unit::mm), 1.0},
                           {kit::unitText(Unit::m), 1e3}};
    const Rung force[] = {{kit::unitText(Unit::N), 1.0}, {kit::unitText(Unit::kN), 1e3}};
    const Rung power[] = {{kit::unitText(Unit::W), 1.0}, {kit::unitText(Unit::kW), 1e3}};
    const Rung freq[] = {{kit::unitText(Unit::Hz), 1.0}, {kit::unitText(Unit::kHz), 1e3},
                         {kit::unitText(Unit::MHz), 1e6}, {kit::unitText(Unit::GHz), 1e9}};
    struct Ladder { const Rung* rungs; int n; };
    const Ladder ladders[] = {{pressure, 4}, {imperialStress, 2}, {length, 3},
                              {force, 2},    {power, 2},          {freq, 4}};

    if (!unit.empty()) {
        for (const Ladder& l : ladders) {
            int at = -1;
            for (int i = 0; i < l.n; ++i)
                if (unit == l.rungs[i].unit) at = i;
            if (at < 0) continue;
            // Largest rung that still shows the extreme as >= 1 (else the smallest rung).
            const double base = m * l.rungs[at].factor;
            int pick = 0;
            for (int i = 0; i < l.n; ++i)
                if (base / l.rungs[i].factor >= 1.0) pick = i;
            const double shown = base / l.rungs[pick].factor;
            if (shown >= 1e-2 && shown < 1e6) {
                sc.div = l.rungs[pick].factor / l.rungs[at].factor;
                sc.unit = l.rungs[pick].unit;
                return sc;
            }
            break;
        }
    }
    // No ladder (or it ran out): an engineering power-of-ten multiplier on the unit line.
    const int e = 3 * (int)std::floor(std::log10(m) / 3.0);
    if (e == 0) return sc;
    sc.div = std::pow(10.0, (double)e);
    sc.unit = "\xc3\x97" + powerOfTen(e) + (unit.empty() ? std::string() : " " + unit);
    return sc;
}

// 1-2-5 x 10^n step closest above `raw`.
double niceStep(double raw) {
    if (!(raw > 0.0) || !std::isfinite(raw)) return 0.0;
    const double mag = std::pow(10.0, std::floor(std::log10(raw)));
    const double f   = raw / mag;
    const double n   = f <= 1.0 ? 1.0 : f <= 2.0 ? 2.0 : f <= 5.0 ? 5.0 : 10.0;
    return n * mag;
}

struct Tick {
    float       frac;    // 0 = min (bottom), 1 = max (top)
    double      value;   // the value the text shows (after rounding)
    std::string text;
};

// Fixed-decimal tick text with trailing zeros stripped; never "-0".
std::string tickText(double v, int dec) {
    char b[64];
    std::snprintf(b, sizeof b, "%.*f", dec, v);
    std::string s = b;
    if (s.find('.') != std::string::npos) {
        while (!s.empty() && s.back() == '0') s.pop_back();
        if (!s.empty() && s.back() == '.') s.pop_back();
    }
    if (s == "-0") s = "0";
    return s;
}

// "17100" -> "17,100" (integer part only).
void groupThousands(std::string& s) {
    const size_t start = (!s.empty() && s[0] == '-') ? 1 : 0;
    size_t end = s.find('.');
    if (end == std::string::npos) end = s.size();
    for (size_t i = end; i > start + 3; i -= 3) s.insert(i - 3, ",");
}

// Exact-resolution min/max at the ends plus interior 1-2-5 ticks, as many as fit `barH` at one
// label per `minGapPx`. Interior ticks crowding an end label are dropped so the extremes always
// read. ONE formatting rule per tick set: interior ticks print at the step's resolution, the ends
// one decade finer (but at least 3 significant digits of the largest magnitude), so "29.3 / 20 /
// 10 / 0.1" never mixes "0.102" precision with rounded steps; thousands separators go on every
// label or on none ("17,100 / 10,000 / 5,000", never "10,000 / 5000"). Values are display-scaled.
std::vector<Tick> buildTicks(double vmin, double vmax, float barH, float minGapPx) {
    std::vector<Tick> ticks;
    const double range = vmax - vmin;
    if (!(range > 0.0) || !std::isfinite(range)) {   // flat field: one label
        ticks.push_back({1.0f, vmax, kit::fmtValue(vmax, 3)});
        return ticks;
    }
    const double mag = std::max(std::fabs(vmin), std::fabs(vmax));
    const int slots = std::max(1, (int)std::floor(barH / std::max(minGapPx, 1.0f)));
    const double step = niceStep(range / (double)slots);
    const int stepExp = step > 0.0 ? (int)std::floor(std::log10(step)) : 0;
    const int magExp = mag > 0.0 ? (int)std::floor(std::log10(mag)) : 0;
    const int endExp = std::min(stepExp - 1, magExp - 2);
    const int decInterior = std::clamp(-stepExp, 0, 6);
    const int decEnd = std::clamp(-endExp, 0, 6);
    auto endValue = [&](double v) {
        if (endExp > 0) {
            const double q = std::pow(10.0, (double)endExp);
            v = std::round(v / q) * q;
        }
        return v;
    };
    const double hi = endValue(vmax), lo = endValue(vmin);
    ticks.push_back({1.0f, hi, tickText(hi, decEnd)});
    ticks.push_back({0.0f, lo, tickText(lo, decEnd)});
    if (step > 0.0) {
        const float edgeFrac = minGapPx / std::max(barH, 1.0f);
        for (double v = std::ceil(vmin / step) * step; v <= vmax; v += step) {
            const float frac = (float)((v - vmin) / range);
            if (frac < edgeFrac || frac > 1.0f - edgeFrac) continue;
            const double shown = std::fabs(v) < step * 1e-6 ? 0.0 : v;   // no "-1e-17"
            ticks.push_back({frac, shown, tickText(shown, decInterior)});
        }
    }
    double shownMag = 0.0;
    for (const Tick& t : ticks) shownMag = std::max(shownMag, std::fabs(t.value));
    if (shownMag >= 1e4)
        for (Tick& t : ticks) groupThousands(t.text);
    return ticks;
}

}  // namespace

float legendReserveWidth() {
    return s_reserveFrame >= ImGui::GetFrameCount() - 1 ? s_reserveW : 0.0f;
}

void drawLegend(AppState& st, ImDrawList* dl, const ImVec2& r0, const ImVec2& r1) {
    if (!st.legendOn) return;

    const float lineH  = ImGui::GetTextLineHeight() + ui::metrics::px(2);
    const float barW   = ui::metrics::px(18);
    const float barPref = ui::metrics::px(200);       // preferred bar height (tall viewports)
    const float pad    = ui::metrics::px(8);
    const float margin = ui::metrics::px(12);        // inset from the clear rect edges
    const float tickL  = ui::metrics::px(4);         // tick mark length
    const float tickGap = ui::metrics::px(7);        // bar edge -> label
    const float titleGap = ui::metrics::px(4);       // title -> bar

    // Anchor in the bottom-right corner of the viewport's CLEAR rect (between the pinned
    // FeatureManager / Simulation and Copilot columns, below the doc-tab band) — never under a
    // glass pane. The viewport frames the model beside it (legendReserveWidth).
    ImVec2 c0 = r0, c1 = r1;
    {
        ImVec2 a, b;
        if (ui::viewportClearRect(a, b)) {
            c0 = ImVec2(std::max(c0.x, a.x), std::max(c0.y, a.y));
            c1 = ImVec2(std::min(c1.x, b.x), std::min(c1.y, b.y));
        }
    }
    const float rightLimit = c1.x - margin;
    const float availW = rightLimit - (c0.x + margin);
    const float availH = (c1.y - margin) - (c0.y + margin);
    if (availW <= 0.0f || availH <= 0.0f) return;

    // Title: the field label on ONE line (elided with a hover tooltip when it cannot fit) and the
    // display unit on its own line below — the same two-row shape for every field, never a
    // word-wrap that splits "Von Mises / stress (psi)".
    const std::string label = legendLabel(st.legendTitle);
    const std::string unitCanon = kit::unitText(st.legendUnits.c_str());
    const double rawMin = st.legendMin, rawMax = st.legendMax;
    const LegendScale scale = legendScale(rawMin, rawMax, unitCanon);
    const double vmin = rawMin / scale.div, vmax = rawMax / scale.div;

    // Bar height: preferred, clamped to the viewport rect once the title is known. Size the ticks
    // at the preferred height first (labels only widen with more ticks).
    const float minGap = lineH * 1.6f;
    std::vector<Tick> ticks = buildTicks(vmin, vmax, barPref, minGap);
    float labelW = 0.0f;
    for (const Tick& t : ticks) labelW = std::max(labelW, ImGui::CalcTextSize(t.text.c_str()).x);

    // Shrink-wrap the pill to the bar+labels; the title may widen it (up to 2x the body) so
    // ordinary field names ("Peak pressure", "Von Mises") stay whole, and the unit line always
    // fits. Never wider than the rect.
    const float bodyW = pad + barW + tickGap + labelW + pad;
    const float labelTextW = ImGui::CalcTextSize(label.c_str()).x;
    const float unitTextW = scale.unit.empty() ? 0.0f : ImGui::CalcTextSize(scale.unit.c_str()).x;
    float boxW = std::max({bodyW, std::min(labelTextW + pad * 2.0f, bodyW * 2.0f),
                           unitTextW + pad * 2.0f});
    boxW = std::min(boxW, availW);
    const float innerW = boxW - pad * 2.0f;
    const bool labelElided = labelTextW > innerW;
    const std::string labelShown = labelElided ? kit::elide(label.c_str(), innerW) : label;
    const std::string unitShown =
        unitTextW > innerW ? kit::elide(scale.unit.c_str(), innerW) : scale.unit;
    const int titleRows = unitShown.empty() ? 1 : 2;
    const float titleH = lineH * (float)titleRows;

    // Clamp the bar to what the viewport rect leaves after the title and padding. Too short to
    // carry even the two end labels: draw nothing rather than spill over the rect.
    const float chromeH = pad * 2.0f + titleH + titleGap + lineH;   // + half a label above/below
    const float barH = std::min(barPref, availH - chromeH);
    if (barH < lineH * 2.0f) return;
    if (barH < barPref) ticks = buildTicks(vmin, vmax, barH, minGap);
    const float boxH = chromeH + barH;

    const ImVec2 b1(rightLimit, c1.y - margin);
    const ImVec2 b0(b1.x - boxW, b1.y - boxH);
    s_reserveW = (c1.x - b0.x) + margin;
    s_reserveFrame = ImGui::GetFrameCount();

    // Liquid-glass v2 (contrast audit P2): the legend floats over the live RESULT colormap, so the
    // legacy fixed overlayBg pill let a saturated stress field bleed through at 0.88 alpha and
    // soften the text. adaptivePanel samples the field under the pill; glassText carries the halo.
    // Regular (not UltraThin) glass: the legend needs a solid enough backing chip to stay readable
    // over saturated result colors at native scale.
    const ui::AdaptiveSurface& surf =
        ui::adaptivePanel(ui::panelId("vp_legend"), ImRect(b0, b1), ui::GlassGroup::Hud);
    ui::glassPanel(dl, ImRect(b0, b1), ui::GlassLevel::Regular, 6.0f, surf);
    dl->PushClipRect(b0, b1, true);

    // Title rows: label, then the display unit.
    const ImVec2 titleTL(b0.x + pad, b0.y + pad);
    ui::glassText(dl, nullptr, ImGui::GetFontSize(), titleTL, surf, labelShown.c_str());
    if (!unitShown.empty())
        ui::glassText(dl, nullptr, ImGui::GetFontSize(), ImVec2(titleTL.x, titleTL.y + lineH), surf,
                      unitShown.c_str());

    // Colour bar (top = max, bottom = min), sampled from the renderer's LUT stops.
    const ImVec2 barTL(b0.x + pad, b0.y + pad + titleH + titleGap + lineH * 0.5f);
    const ImVec2 barBR(barTL.x + barW, barTL.y + barH);
    const int segs = 64;
    for (int i = 0; i < segs; ++i) {
        const float t1 = 1.0f - (float)i / segs;   // top is max
        const float t2 = 1.0f - (float)(i + 1) / segs;
        const float y0 = barTL.y + barH * ((float)i / segs);
        const float y1 = barTL.y + barH * ((float)(i + 1) / segs);
        dl->AddRectFilledMultiColor(ImVec2(barTL.x, y0), ImVec2(barBR.x, y1),
                                    rampColor(t1), rampColor(t1), rampColor(t2), rampColor(t2));
    }
    dl->AddRect(barTL, barBR, ImGui::ColorConvertFloat4ToU32(surf.textTertiary));

    // Ticks: exact extremes + 1-2-5 interior values. Marks from the surface's secondary tone;
    // labels via glassText so they hold up when the pill sits over the extremes of the ramp.
    const ImU32 tick = ImGui::ColorConvertFloat4ToU32(surf.textSecondary);
    const float textH = ImGui::GetTextLineHeight();
    for (const Tick& t : ticks) {
        const float y = barTL.y + barH * (1.0f - t.frac);
        dl->AddLine(ImVec2(barBR.x, y), ImVec2(barBR.x + tickL, y), tick,
                    ui::metrics::stroke(1.0f));
        ui::glassText(dl, nullptr, ImGui::GetFontSize(),
                      ImVec2(barBR.x + tickGap, y - textH * 0.5f), surf, t.text.c_str());
    }
    dl->PopClipRect();

    // Full title on hover when the label had to elide (only while the viewport itself is hovered,
    // so a pane floating over the legend never triggers it).
    if (labelElided && ImGui::IsWindowHovered() &&
        ImGui::IsMouseHoveringRect(titleTL, ImVec2(b1.x - pad, titleTL.y + titleH))) {
        const std::string full = scale.unit.empty() ? label : label + " (" + scale.unit + ")";
        kit::tooltip(full.c_str());
    }
}

}  // namespace ui
