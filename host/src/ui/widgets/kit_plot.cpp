// kit_plot.cpp — the ONE implementation of the kit charts, chip flow, result cards and tables
// declared in kit_plot.h.
//
// The line chart absorbs hydraulic_panel.cpp's drawPlot (1-2-5 ticks with 2 % snapping, clipped
// series, hover cursor with a dot per series, wrapping legend) and fixes its y-unit placement:
// the unit lives on the y title row above the plot, never beside the first x tick. Reference
// lines (sim_topology drawSeriesPlot's dashed target), bands, markers and log axes (fatigue
// Haigh / S-N / life charts) are options of the same painter rather than new copies.
//
// House rules: colors are ui::palette roles, kit::Tone or the caller's ink (ImGuiCol_Text);
// sizes derive from the font size, ui::metrics::U/px/stroke/rounding and named ratios below.
//
// GL/GUI thread only.
#include "kit_plot.h"

#include <algorithm>
#include <cfloat>
#include <cmath>
#include <cstdio>
#include <string>
#include <vector>

#include "imgui.h"
#include "imgui_internal.h"

#include "../anim.h"
#include "../metrics.h"

namespace ui {
namespace kit {
namespace detail {
// kit.cpp: keyValueRow with an explicit key column (labelW > 0 fixes it; longer keys elide).
void keyValueRowCol(const char* key, const char* value, Tone valueTone, bool copyable, float labelW);
}  // namespace detail
namespace {

namespace m = ui::metrics;

// ---- ratios (alphas of a role / fractions of a length; never colors or pixels) -----------------
constexpr float kFrameA         = 0.60f;  // plot frame fill (frameBg role)
constexpr float kGridA          = 0.12f;  // grid lines (ink)
constexpr float kDimInkA        = 0.62f;  // tick labels, titles, captions (ink)
constexpr float kCursorA        = 0.70f;  // hover cursor line (ink)
constexpr float kHoverWashA     = 0.06f;  // hovered bar slot (ink)
constexpr float kBandA          = 0.16f;  // shaded band fill
constexpr float kDimSeriesA     = 0.42f;  // de-emphasised series / bars
constexpr float kBarA           = 0.85f;  // bar fill
constexpr float kTableBorderA   = 0.14f;  // table hairlines (ink)
constexpr float kTableStripeA   = 0.03f;  // alternate row wash (ink)
constexpr float kZeroSnap       = 0.25f;  // a range whose near end is < 25 % of its far end includes 0
constexpr float kRangeSnap      = 0.02f;  // extremes within 2 % of a step past a tick snap onto it
constexpr float kSparkMargin    = 0.08f;  // sparkline vertical breathing room (fraction of span)
constexpr float kAutoChartLines = 10.0f;  // auto chart height, in text lines
constexpr float kAutoSparkLines = 2.4f;   // auto sparkline height, in text lines
constexpr float kAutoColLines   = 7.0f;   // auto column-chart height, in text lines
constexpr float kMinChartLines  = 6.0f;
constexpr float kMinSparkLines  = 1.5f;
constexpr float kTickGapYLines  = 2.8f;   // ~one y tick per this many text lines
constexpr float kTickGapXEm     = 6.5f;   // ~one x tick per this many font sizes
constexpr float kBarFill        = 0.70f;  // column width / slot width
constexpr float kBarRowLines    = 1.40f;  // horizontal bar row pitch, in text lines
constexpr float kBarThick       = 0.40f;  // horizontal bar thickness / row pitch
constexpr float kMetricMinEm    = 7.0f;   // metricTable: inline only if the metric keeps >= 7 em
constexpr float kMetricStackU   = 0.5f;   // stacked metricTable: gap between name and values (U)
constexpr float kHitRadiusU     = 1.5f;   // marker hover radius, in metrics::U
constexpr float kTooltipWrapEm = 26.0f;  // readout tooltip wrap width, in font sizes
constexpr float kPlateA        = 0.92f;  // plate behind a reference-line label (frameBg role)
constexpr float kRmChipFillA   = 0.16f;  // removable chip fill / border: kit.cpp chip() ratios
constexpr float kRmChipBorderA = 0.55f;
constexpr float kRmHoverFillA  = 0.10f;  // extra fill while the chip is hovered
constexpr float kRmWashA       = 0.18f;  // wash behind the x while it is hovered (Err role)
constexpr float kRmGlyphEm     = 0.24f;  // half-size of the x glyph, in font sizes

const char* const kEmDash = "\xe2\x80\x94";

// ---- palette plumbing ----------------------------------------------------------------------------
ImU32 roleU32(const float c[4], float alphaMul = 1.0f) {
    float t[4];
    Palette::withAlpha(c, t, alphaMul);
    return (ImU32)Palette::toU32(t);
}
ImU32 vecU32(ImVec4 c, float alphaMul = 1.0f) {
    c.w *= alphaMul;
    return ImGui::ColorConvertFloat4ToU32(c);
}
ImVec4 inkVec(float alphaMul = 1.0f) {
    ImVec4 c = ImGui::GetStyleColorVec4(ImGuiCol_Text);
    c.w *= alphaMul;
    return c;
}
ImU32 inkU32(float alphaMul = 1.0f) { return ImGui::ColorConvertFloat4ToU32(inkVec(alphaMul)); }

// The surface the plot paints on, derived from the caller's ink (dark ink = bright surface) —
// the same inference kit.cpp makes, so series retune exactly like toneOn() does for its caller.
AdaptiveSurface inkSurface() {
    AdaptiveSurface s;
    const ImVec4 ink = inkVec();
    s.darkText      = 0.2126f * ink.x + 0.7152f * ink.y + 0.0722f * ink.z < 0.5f;
    s.textPrimary   = ink;
    s.textSecondary = inkVec(kDimInkA);
    s.textTertiary  = inkVec(kDimInkA);
    return s;
}

// Categorical ramp: brand accents first, then the theme-aware axis / CAM hues. No status roles.
const float* rampRole(int i) {
    const float* roles[] = {palette.accent2, palette.accent,    palette.axisY, palette.camArc,
                            palette.flowInlet, palette.camTool, palette.axisX, palette.axisZ};
    constexpr int n = (int)(sizeof(roles) / sizeof(roles[0]));
    return roles[((i % n) + n) % n];
}

// Same legibility correction toneOn() applies to tone roles, for a ramp role.
ImVec4 roleOn(const float* role, const AdaptiveSurface& surf) {
    ImVec4      c(role[0], role[1], role[2], 1.0f);
    const float lum = 0.2126f * c.x + 0.7152f * c.y + 0.0722f * c.z;
    if (surf.darkText && lum > 0.45f) {
        c.x *= 0.52f;
        c.y *= 0.52f;
        c.z *= 0.52f;
    } else if (!surf.darkText && lum < 0.35f) {
        c.x += (1.0f - c.x) * 0.55f;
        c.y += (1.0f - c.y) * 0.55f;
        c.z += (1.0f - c.z) * 0.55f;
    }
    return c;
}

ImU32 toneCol(Tone t, const AdaptiveSurface& surf, float alphaMul = 1.0f) {
    return vecU32(toneOn(t, surf), alphaMul);
}

// ---- numbers --------------------------------------------------------------------------------------
bool finite(double v) { return std::isfinite(v); }

// The smallest 1, 2 or 5 x 10^n not below `raw`.
double niceStep(double raw) {
    if (!(raw > 0.0) || !std::isfinite(raw)) return 1.0;
    const double p = std::pow(10.0, std::floor(std::log10(raw)));
    const double f = raw / p;
    return (f <= 1.0 + 1e-9 ? 1.0 : f <= 2.0 + 1e-9 ? 2.0 : f <= 5.0 + 1e-9 ? 5.0 : 10.0) * p;
}

// Thousands separators on the integer part of a plain decimal ("12500.5" -> "12,500.5").
std::string grouped(std::string s) {
    const size_t start = !s.empty() && s[0] == '-' ? 1 : 0;
    size_t       dot   = s.find('.');
    if (dot == std::string::npos) dot = s.size();
    for (size_t i = dot; i > start + 3; i -= 3) s.insert(i - 3, ",");
    return s;
}

// Tick labels with just enough precision to tell ticks `step` apart. Large or tiny magnitudes go
// through the kit's significant-figure display formatter (thousands separators, SI prefix for
// tiny values); an axis reaching 10^4 groups its digits like every other displayed quantity; a
// tick at |v| ~ 0 reads "0", never "-0".
std::vector<std::string> linearLabels(const std::vector<double>& ticks, double step) {
    double maxAbs = 0.0;
    for (double t : ticks) maxAbs = std::max(maxAbs, std::fabs(t));
    std::vector<std::string> out;
    out.reserve(ticks.size());
    const bool compact = maxAbs >= 1e5 || (step < 1e-4 && maxAbs > 0.0);
    const int  sig = compact ? std::clamp((int)std::floor(std::log10(std::max(maxAbs, step))) -
                                              (int)std::floor(std::log10(step)) + 1,
                                          1, 6)
                             : 0;
    const int dec = std::clamp((int)std::ceil(-std::log10(step) - 1e-9), 0, 6);
    for (double t : ticks) {
        if (std::fabs(t) < 1e-9 * step) t = 0.0;
        if (compact) {
            out.push_back(fmtValue(t, sig));
        } else {
            char b[48];
            std::snprintf(b, sizeof b, "%.*f", dec, t);
            out.push_back(maxAbs >= 1e4 ? grouped(b) : std::string(b));
        }
    }
    return out;
}

// "10" with a superscript exponent: 1, 10, 10², 10³, 10⁻¹ (all inside the font's allow-list).
std::string decadeLabel(int e) {
    if (e == 0) return "1";
    if (e == 1) return "10";
    static const char* const kSup[10] = {"\xe2\x81\xb0", "\xc2\xb9",     "\xc2\xb2",
                                         "\xc2\xb3",     "\xe2\x81\xb4", "\xe2\x81\xb5",
                                         "\xe2\x81\xb6", "\xe2\x81\xb7", "\xe2\x81\xb8",
                                         "\xe2\x81\xb9"};
    std::string s = "10";
    if (e < 0) s += "\xe2\x81\xbb";
    const std::string digits = std::to_string(std::abs(e));
    for (char c : digits) s += kSup[c - '0'];
    return s;
}

std::string axisTitle(const PlotAxis& ax) {
    const bool t = ax.title && ax.title[0], u = ax.unit && ax.unit[0];
    if (t && u) return std::string(ax.title) + " (" + ax.unit + ")";
    if (t) return ax.title;
    if (u) return ax.unit;
    return std::string();
}

std::string withUnit(double v, const char* unit) {
    return unit && unit[0] ? fmtQuantity(v, unit) : fmtValue(v);
}

// The visible part of a label: ImGui's "##" convention hides an id suffix ("Pressure##p1"), so a
// caller's disambiguator never reaches the legend, readout or chip text.
std::string visibleLabel(const char* s) {
    return s && s[0] ? std::string(s, ImGui::FindRenderedTextEnd(s)) : std::string();
}

// Whether segment pq touches rect r: bounding boxes overlap and the rect's corners are not all on
// one side of the line (separating-axis test). A degenerate segment is a point-in-rect test.
bool segHitsRect(ImVec2 p, ImVec2 q, const ImRect& r) {
    if (std::max(p.x, q.x) < r.Min.x || std::min(p.x, q.x) > r.Max.x ||
        std::max(p.y, q.y) < r.Min.y || std::min(p.y, q.y) > r.Max.y)
        return false;
    const float dx = q.x - p.x, dy = q.y - p.y;
    auto side = [&](float x, float y) { return dx * (y - p.y) - dy * (x - p.x); };
    const float s0 = side(r.Min.x, r.Min.y), s1 = side(r.Max.x, r.Min.y);
    const float s2 = side(r.Min.x, r.Max.y), s3 = side(r.Max.x, r.Max.y);
    return !((s0 > 0.0f && s1 > 0.0f && s2 > 0.0f && s3 > 0.0f) ||
             (s0 < 0.0f && s1 < 0.0f && s2 < 0.0f && s3 < 0.0f));
}

// ---- ranges / scales -------------------------------------------------------------------------------
struct Range {
    bool   log = false;
    double lo = DBL_MAX, hi = -DBL_MAX;
    void   add(double v) {
        if (!finite(v) || (log && !(v > 0.0))) return;
        lo = std::min(lo, v);
        hi = std::max(hi, v);
    }
    bool ok() const { return lo <= hi; }
};

struct Scale {
    bool                     log = false;
    double                   lo = 0.0, hi = 1.0;   // data space; exponents when log
    float                    p0 = 0.0f, p1 = 1.0f; // pixel of lo / hi
    std::vector<double>      ticks;                // data values
    std::vector<std::string> labels;
    float map(double v) const {
        double t;
        if (log) {
            if (!(v > 0.0)) return NAN;
            t = (std::log10(v) - lo) / (hi - lo);
        } else {
            t = (v - lo) / (hi - lo);
        }
        return p0 + (p1 - p0) * (float)t;
    }
    double unmap(float px) const {
        const double t = (px - p0) / (p1 - p0 != 0.0f ? p1 - p0 : 1.0f);
        const double v = lo + t * (hi - lo);
        return log ? std::pow(10.0, v) : v;
    }
};

// Linear axis. `pad` extends the range out to whole ticks (value axes); otherwise the range is
// the data's (x axes: a time series starts and ends where its samples do).
Scale buildLinear(const Range& r, const PlotAxis& ax, float lengthPx, float tickGapPx, bool pad) {
    Scale        s;
    const bool   fixLo = finite(ax.min), fixHi = finite(ax.max);
    double       vlo = r.ok() ? r.lo : 0.0, vhi = r.ok() ? r.hi : 1.0;
    if (fixLo) vlo = ax.min;
    if (fixHi) vhi = ax.max;
    if (ax.zero) {
        if (!fixLo && vlo > 0.0 && vlo < kZeroSnap * vhi) vlo = 0.0;
        if (!fixHi && vhi < 0.0 && vhi > kZeroSnap * vlo) vhi = 0.0;
    }
    if (vhi - vlo < 1e-12 * std::max(1.0, std::fabs(vhi))) {
        // Flat data: open a symmetric window around it (on the free side when one end is fixed).
        double d = std::max(0.5, 0.1 * std::fabs(vhi));
        if (ax.integer) d = std::max(d, 1.0);
        if (fixLo)      vhi = vlo + 2.0 * d;   // fixed floor (or both fixed): open upward
        else if (fixHi)     vlo = vhi - 2.0 * d;
        else {
            vlo -= d;
            vhi += d;
        }
    }
    const int n = std::clamp((int)(lengthPx / std::max(tickGapPx, 1.0f)), 2, 8);
    double    step = 0.0, lo = vlo, hi = vhi;
    if (pad) {
        // Of the nice steps giving at most ~1 interval more than wanted, the one whose axis
        // wastes the least span wins (hydraulic_panel's search, kept verbatim in spirit).
        for (int k = n; k <= 2 * n; ++k) {
            double st = niceStep((vhi - vlo) / k);
            if (ax.integer) st = std::max(st, 1.0);
            const double l = fixLo ? vlo : std::floor(vlo / st + kRangeSnap) * st;
            double       u = fixHi ? vhi : std::ceil(vhi / st - kRangeSnap) * st;
            if (u - l < 0.5 * st) u = l + st;
            if (k > n && (u - l) / st > n + 1.5) continue;
            if (step == 0.0 || u - l < hi - lo - 1e-9 * st) {
                step = st;
                lo   = l;
                hi   = u;
            }
        }
    } else {
        step = niceStep((vhi - vlo) / n);
        if (ax.integer) step = std::max(step, 1.0);
        // Prefer the coarsest step (up to ~2x the wanted tick count) whose ticks land exactly on the
        // data end, so the axis is labelled where the series stops (0..3 s -> 0 1 2 3, not 0 2).
        for (int k = n; k <= 2 * n; ++k) {
            double st = niceStep((vhi - vlo) / k);
            if (ax.integer) st = std::max(st, 1.0);
            if (st > step) continue;
            const double e = vhi / st;
            if (std::fabs(e - std::round(e)) < 1e-6 * std::max(1.0, std::fabs(e))) {
                step = st;
                break;
            }
        }
    }
    s.lo = lo;
    s.hi = hi > lo ? hi : lo + 1.0;
    const long long k0 = (long long)std::ceil(s.lo / step - 1e-6);
    const long long k1 = (long long)std::floor(s.hi / step + 1e-6);
    for (long long k = k0; k <= k1 && s.ticks.size() < 64; ++k) s.ticks.push_back((double)k * step);
    s.labels = linearLabels(s.ticks, step);
    if (!pad && !s.ticks.empty()) {
        // A data axis whose ends fall between ticks still labels both ends (one extra decimal so
        // the label does not misstate where the samples start or stop).
        if (s.ticks.front() - s.lo > 1e-6 * step) {
            s.ticks.insert(s.ticks.begin(), s.lo);
            s.labels.insert(s.labels.begin(), linearLabels({s.lo}, step * 0.1).front());
        }
        if (s.hi - s.ticks.back() > 1e-6 * step) {
            s.ticks.push_back(s.hi);
            s.labels.push_back(linearLabels({s.hi}, step * 0.1).front());
        }
    }
    return s;
}

// Log axis: whole decades covering the data, thinned to every k-th decade when crowded.
Scale buildLog(const Range& r, const PlotAxis& ax, float lengthPx, float tickGapPx) {
    Scale s;
    s.log = true;
    double lo = r.ok() ? std::floor(std::log10(r.lo)) : 0.0;
    double hi = r.ok() ? std::ceil(std::log10(r.hi)) : 1.0;
    if (finite(ax.min) && ax.min > 0.0) lo = std::log10(ax.min);
    if (finite(ax.max) && ax.max > 0.0) hi = std::log10(ax.max);
    if (hi <= lo) hi = lo + 1.0;
    s.lo = lo;
    s.hi = hi;
    const int maxTicks = std::max(1, (int)(lengthPx / std::max(tickGapPx, 1.0f)));
    const int every    = std::max(1, (int)std::ceil((hi - lo) / maxTicks));
    for (int e = (int)std::ceil(lo - 1e-9); e <= (int)std::floor(hi + 1e-9); e += every) {
        s.ticks.push_back(std::pow(10.0, e));
        s.labels.push_back(decadeLabel(e));
    }
    return s;
}

Scale buildScale(const Range& r, const PlotAxis& ax, float lengthPx, float tickGapPx, bool pad) {
    return ax.log ? buildLog(r, ax, lengthPx, tickGapPx) : buildLinear(r, ax, lengthPx, tickGapPx, pad);
}

float maxLabelW(const Scale& s) {
    float w = 0.0f;
    for (const auto& l : s.labels) w = std::max(w, ImGui::CalcTextSize(l.c_str()).x);
    return w;
}

// ---- painters ------------------------------------------------------------------------------------
void dashedPolyline(ImDrawList* dl, const ImVec2* p, int n, ImU32 col, float th) {
    const float dash = m::U(1.5f), gap = m::U(1.0f);
    bool        on     = true;
    float       remain = dash;
    for (int i = 0; i + 1 < n; ++i) {
        const ImVec2 a = p[i], b = p[i + 1];
        const float  L = std::sqrt((b.x - a.x) * (b.x - a.x) + (b.y - a.y) * (b.y - a.y));
        if (!(L > 0.0f)) continue;
        const ImVec2 d((b.x - a.x) / L, (b.y - a.y) / L);
        float        t = 0.0f;
        while (t < L) {
            const float st = std::min(remain, L - t);
            if (on)
                dl->AddLine(ImVec2(a.x + d.x * t, a.y + d.y * t),
                            ImVec2(a.x + d.x * (t + st), a.y + d.y * (t + st)), col, th);
            t += st;
            remain -= st;
            if (remain <= 0.0f) {
                on     = !on;
                remain = on ? dash : gap;
            }
        }
    }
}

void strokeLine(ImDrawList* dl, ImVec2 a, ImVec2 b, ImU32 col, float th, bool dashed) {
    if (dashed) {
        const ImVec2 p[2] = {a, b};
        dashedPolyline(dl, p, 2, col, th);
    } else {
        dl->AddLine(a, b, col, th);
    }
}

// Framed placeholder at the plot's own size, so a plot that has no data yet keeps its slot
// instead of collapsing the layout around it.
void emptyFrame(float W, float H, const char* text) {
    const ImVec2 p0 = ImGui::GetCursorScreenPos();
    ImGui::Dummy(ImVec2(W, H));
    ImDrawList* dl = ImGui::GetWindowDrawList();
    dl->AddRectFilled(p0, ImVec2(p0.x + W, p0.y + H), roleU32(palette.frameBg, kFrameA),
                      m::rounding(3.0f));
    const std::string shown = elide(text && text[0] ? text : "No data to plot yet.", W - m::U(2.0f));
    const ImVec2      ts    = ImGui::CalcTextSize(shown.c_str());
    dl->AddText(ImVec2(p0.x + (W - ts.x) * 0.5f, p0.y + (H - ts.y) * 0.5f), inkU32(kDimInkA),
                shown.c_str());
}

// ---- legend ---------------------------------------------------------------------------------------
enum class Swatch { Line, Dashed, Dot, Fill };
struct LegendEntry {
    const char* label;
    Swatch      kind;
    ImU32       col;
};

float legendItemW(const LegendEntry& e) {
    return m::U(3.5f) + m::U(1.5f) + ImGui::CalcTextSize(e.label, nullptr, true).x + m::U(4.0f);
}

void drawSwatch(ImDrawList* dl, ImVec2 p, float lineH, const LegendEntry& e) {
    const float  sw = m::U(3.5f);
    const float  cy = p.y + 0.5f * lineH;
    switch (e.kind) {
        case Swatch::Line:
            dl->AddLine(ImVec2(p.x, cy), ImVec2(p.x + sw, cy), e.col, ui::metrics::stroke(2.5f));
            break;
        case Swatch::Dashed:
            strokeLine(dl, ImVec2(p.x, cy), ImVec2(p.x + sw, cy), e.col, m::stroke(2.0f), true);
            break;
        case Swatch::Dot:
            dl->AddCircleFilled(ImVec2(p.x + sw * 0.5f, cy), m::U(0.9f), e.col);
            break;
        case Swatch::Fill:
            dl->AddRectFilled(ImVec2(p.x, cy - m::U(1.0f)), ImVec2(p.x + sw, cy + m::U(1.0f)), e.col,
                              m::rounding(2.0f));
            break;
    }
}

// Wrapping legend below the plot, advancing the cursor by its height.
void drawLegend(const std::vector<LegendEntry>& entries, float W) {
    const float  lineH = ImGui::GetTextLineHeight();
    const float  rowH  = ImGui::GetTextLineHeightWithSpacing();
    const ImVec2 l0    = ImGui::GetCursorScreenPos();
    ImDrawList*  dl    = ImGui::GetWindowDrawList();
    float        x = 0.0f, y = 0.0f;
    for (const auto& e : entries) {
        const float w = legendItemW(e);
        if (x > 0.0f && x + w > W) {
            x = 0.0f;
            y += rowH;
        }
        drawSwatch(dl, ImVec2(l0.x + x, l0.y + y), lineH, e);
        const float  tx    = l0.x + x + m::U(3.5f) + m::U(1.5f);
        const std::string s = elide(visibleLabel(e.label).c_str(), std::max(W - (tx - l0.x), m::U(1.0f)));
        dl->AddText(ImVec2(tx, l0.y + y), inkU32(), s.c_str());
        x += w;
    }
    ImGui::Dummy(ImVec2(W, y + rowH));
}

// One readout line in a tooltip: swatch + text.
void readoutLine(const LegendEntry* sw, const std::string& text) {
    if (sw) {
        const ImVec2 p = ImGui::GetCursorScreenPos();
        ImGui::Dummy(ImVec2(m::U(3.5f), ImGui::GetTextLineHeight()));
        drawSwatch(ImGui::GetWindowDrawList(), p, ImGui::GetTextLineHeight(), *sw);
        ImGui::SameLine(0.0f, m::U(1.5f));
    }
    ImGui::TextUnformatted(text.c_str());
}

// ---- series access ---------------------------------------------------------------------------------
const PlotData& seriesX(const PlotSeries& s, const PlotSpec& spec) {
    return !s.x.empty() ? s.x : spec.x;
}
int seriesN(const PlotSeries& s, const PlotData& xs) {
    return xs.empty() ? s.y.size() : std::min(s.y.size(), xs.size());
}
double xAt(const PlotData& xs, int i) { return xs.empty() ? (double)i : xs[i]; }

ImVec4 seriesVec(const PlotSeries& s, int idx, const AdaptiveSurface& surf) {
    return s.toned ? toneOn(s.tone, surf) : seriesColor(s.ramp >= 0 ? s.ramp : idx, surf);
}

}  // namespace

// ==================================================================================================
// series palette
// ==================================================================================================
ImVec4 seriesColor(int i, const AdaptiveSurface& surf) { return roleOn(rampRole(i), surf); }

ImU32 seriesColorU32(int i, const AdaptiveSurface& surf, float alphaMul) {
    return vecU32(seriesColor(i, surf), alphaMul);
}

// ==================================================================================================
// line plot
// ==================================================================================================
PlotHover linePlot(const char* id, const PlotSpec& spec) {
    PlotHover out;
    ImGui::PushID(id && id[0] ? id : "kit_plot");

    const AdaptiveSurface surf  = inkSurface();
    const bool            spark = spec.mode == PlotMode::Sparkline;
    const float           lineH = ImGui::GetTextLineHeight();
    const float           pad   = m::U(1.0f);
    const float           W     = std::max(ImGui::GetContentRegionAvail().x, lineH * 4.0f);
    // Labelled reference lines: a Chart places them at a plot edge; a Sparkline moves them (and its
    // caption) to a row ABOVE the trace so no text is ever drawn over the curve.
    const bool refLabels = std::any_of(spec.refs.begin(), spec.refs.end(),
                                       [](const PlotRef& r) { return r.label && r.label[0]; });
    const bool  capRow = spark && ((spec.caption && spec.caption[0]) || refLabels);
    const float capH   = capRow ? lineH + pad * 0.5f : 0.0f;
    float H = spec.height > 0.0f ? spec.height - capH
                                 : lineH * (spark ? kAutoSparkLines : kAutoChartLines);
    H       = std::max(H, lineH * (spark ? kMinSparkLines : kMinChartLines));

    // ---- data ranges (series, then everything that must stay in view) ----
    Range rx, ry;
    rx.log  = spec.xAxis.log;
    ry.log  = spec.yAxis.log;
    bool any = false;
    for (const auto& s : spec.series) {
        const PlotData& xs  = seriesX(s, spec);
        const int       n   = seriesN(s, xs);
        int             cnt = 0;
        for (int i = 0; i < n; ++i) {
            const double xv = xAt(xs, i), yv = s.y[i];
            if (!finite(xv) || !finite(yv)) continue;
            if ((rx.log && !(xv > 0.0)) || (ry.log && !(yv > 0.0))) continue;
            rx.add(xv);
            ry.add(yv);
            ++cnt;
        }
        if (cnt >= ((s.line && !s.points) ? 2 : 1)) any = true;
    }
    if (!any) {
        emptyFrame(W, capH + H, spec.emptyText);
        ImGui::PopID();
        return out;
    }
    for (const auto& r : spec.refs) (r.vertical ? rx : ry).add(r.value);
    for (const auto& mk : spec.markers) {
        rx.add(mk.x);
        ry.add(mk.y);
    }
    for (const auto& b : spec.bands) {
        if (b.kind == PlotBand::Kind::YSpan) {
            ry.add(b.lo);
            ry.add(b.hi);
        } else if (b.kind == PlotBand::Kind::XSpan) {
            rx.add(b.lo);
            rx.add(b.hi);
        } else {
            const int n = b.yLo.empty() ? std::min(b.x.size(), b.yHi.size())
                                        : std::min({b.x.size(), b.yLo.size(), b.yHi.size()});
            for (int i = 0; i < n; ++i) {
                ry.add(b.yLo.empty() ? 0.0 : b.yLo[i]);
                ry.add(b.yHi[i]);
            }
        }
    }

    // ---- layout ----
    const std::string yTitle = spark ? std::string() : axisTitle(spec.yAxis);
    const std::string xTitle = spark ? std::string() : axisTitle(spec.xAxis);
    const ImVec2      p0     = ImGui::GetCursorScreenPos();
    Scale             xs, ys;
    ImVec2            a, b;
    float             Htot = capH + H;
    if (spark) {
        a = ImVec2(p0.x + pad, p0.y + capH + pad);
        b = ImVec2(p0.x + W - pad, p0.y + capH + H - pad);
        PlotAxis yAx = spec.yAxis;
        yAx.zero     = false;
        ys           = buildScale(ry, yAx, b.y - a.y, b.y - a.y, false);
        if (!ys.log) {
            // Data extent plus breathing room; a flat trace sits mid-height.
            double lo = ry.ok() ? ry.lo : 0.0, hi = ry.ok() ? ry.hi : 1.0;
            if (hi - lo < 1e-12 * std::max(1.0, std::fabs(hi))) {
                const double d = std::max(0.5, 0.1 * std::fabs(hi));
                lo -= d;
                hi += d;
            }
            const double mg = kSparkMargin * (hi - lo);
            ys.lo = finite(spec.yAxis.min) ? spec.yAxis.min : lo - mg;
            ys.hi = finite(spec.yAxis.max) ? spec.yAxis.max : hi + mg;
            if (ys.hi <= ys.lo) ys.hi = ys.lo + 1.0;
        }
        PlotAxis xAx = spec.xAxis;
        xAx.zero     = false;
        xs           = buildScale(rx, xAx, b.x - a.x, b.x - a.x, false);
        if (!xs.log && rx.ok()) {
            xs.lo = finite(spec.xAxis.min) ? spec.xAxis.min : rx.lo;
            xs.hi = finite(spec.xAxis.max) ? spec.xAxis.max : rx.hi;
            if (xs.hi <= xs.lo) xs.hi = xs.lo + 1.0;
        }
    } else {
        const float titleH  = yTitle.empty() ? 0.0f : lineH + pad;
        const float topM    = titleH + lineH * 0.5f;
        const float tickH   = lineH + pad;
        const float xTitleH = xTitle.empty() ? 0.0f : lineH;
        const float areaH   = std::max(H - topM - tickH - xTitleH, lineH * 2.0f);
        ys = buildScale(ry, spec.yAxis, areaH, lineH * kTickGapYLines, true);
        const float gutter =
            std::max(maxLabelW(ys), ImGui::CalcTextSize("000").x) + 2.0f * pad;
        a  = ImVec2(p0.x + gutter, p0.y + topM);
        b  = ImVec2(p0.x + W - pad, a.y + areaH);
        xs = buildScale(rx, spec.xAxis, b.x - a.x, ImGui::GetFontSize() * kTickGapXEm, false);
        Htot = topM + areaH + tickH + xTitleH;
    }
    ys.p0 = b.y;
    ys.p1 = a.y;
    xs.p0 = a.x;
    xs.p1 = b.x;

    ImGui::InvisibleButton("##plot", ImVec2(W, Htot));
    const ImVec2 mouse   = ImGui::GetIO().MousePos;
    const bool   inArea  = mouse.x >= a.x && mouse.x <= b.x && mouse.y >= p0.y && mouse.y <= p0.y + Htot;
    out.hovered          = ImGui::IsItemHovered() && inArea;
    out.clicked          = out.hovered && ImGui::IsItemClicked(ImGuiMouseButton_Left);
    if (out.hovered) out.x = xs.unmap(mouse.x);

    ImDrawList* dl      = ImGui::GetWindowDrawList();
    const ImU32 dimInk  = inkU32(kDimInkA);
    const ImU32 gridCol = inkU32(kGridA);
    const ImU32 frame   = roleU32(palette.frameBg, kFrameA);
    const float rnd     = m::rounding(3.0f);
    dl->PushClipRect(p0, ImVec2(p0.x + W, p0.y + Htot), true);

    // ---- frame, grid, ticks, titles ----
    if (spark) {
        dl->AddRectFilled(ImVec2(p0.x, p0.y + capH), ImVec2(p0.x + W, p0.y + capH + H), frame, rnd);
    } else {
        if (!yTitle.empty()) {
            const std::string t = elide(yTitle.c_str(), W);
            dl->AddText(p0, dimInk, t.c_str());
        }
        dl->AddRectFilled(a, b, frame, rnd);
        for (size_t g = 0; g < ys.ticks.size(); ++g) {
            const float y = ys.map(ys.ticks[g]);
            if (!(y >= a.y - 0.5f && y <= b.y + 0.5f)) continue;
            dl->AddLine(ImVec2(a.x, y), ImVec2(b.x, y), gridCol, m::stroke(1.0f));
            const ImVec2 ts = ImGui::CalcTextSize(ys.labels[g].c_str());
            dl->AddText(ImVec2(a.x - ts.x - pad, y - ts.y * 0.5f), dimInk, ys.labels[g].c_str());
        }
        // x tick labels: the first and last always show (where the samples start and stop); an
        // interior label is dropped when it would overlap a kept neighbour or the last label.
        struct XLabel {
            float  x, w;
            size_t g;
        };
        std::vector<XLabel> xl;
        for (size_t g = 0; g < xs.ticks.size(); ++g) {
            const float x = xs.map(xs.ticks[g]);
            if (!(x >= a.x - 0.5f && x <= b.x + 0.5f)) continue;
            if (x > a.x + 1.0f && x < b.x - 1.0f)
                dl->AddLine(ImVec2(x, a.y), ImVec2(x, b.y), gridCol, m::stroke(1.0f));
            const float tw = ImGui::CalcTextSize(xs.labels[g].c_str()).x;
            xl.push_back({std::clamp(x - 0.5f * tw, a.x, std::max(a.x, b.x - tw)), tw, g});
        }
        float lastRight = -FLT_MAX;
        for (size_t i = 0; i < xl.size(); ++i) {
            const XLabel& l      = xl[i];
            const bool    isLast = i + 1 == xl.size();
            if (!isLast && (l.x < lastRight + pad || l.x + l.w + pad > xl.back().x)) continue;
            dl->AddText(ImVec2(l.x, b.y + pad), dimInk, xs.labels[l.g].c_str());
            lastRight = l.x + l.w;
        }
        if (!xTitle.empty()) {
            const std::string t  = elide(xTitle.c_str(), W);
            const float       tw = ImGui::CalcTextSize(t.c_str()).x;
            dl->AddText(ImVec2(p0.x + W - tw, b.y + lineH + pad), dimInk, t.c_str());
        }
    }

    // ---- data (clipped to the plot area) ----
    dl->PushClipRect(a, b, true);
    std::vector<LegendEntry> legend;
    for (size_t bi = 0; bi < spec.bands.size(); ++bi) {
        const PlotBand& bd  = spec.bands[bi];
        const ImU32     col = bd.ramp >= 0 ? seriesColorU32(bd.ramp, surf, kBandA)
                                           : toneCol(bd.tone, surf, kBandA);
        if (bd.kind == PlotBand::Kind::YSpan) {
            const float y0 = ys.map(bd.lo), y1 = ys.map(bd.hi);
            if (finite(y0) && finite(y1))
                dl->AddRectFilled(ImVec2(a.x, std::min(y0, y1)), ImVec2(b.x, std::max(y0, y1)), col);
        } else if (bd.kind == PlotBand::Kind::XSpan) {
            const float x0 = xs.map(bd.lo), x1 = xs.map(bd.hi);
            if (finite(x0) && finite(x1))
                dl->AddRectFilled(ImVec2(std::min(x0, x1), a.y), ImVec2(std::max(x0, x1), b.y), col);
        } else {
            const int n = bd.yLo.empty() ? std::min(bd.x.size(), bd.yHi.size())
                                         : std::min({bd.x.size(), bd.yLo.size(), bd.yHi.size()});
            for (int i = 0; i + 1 < n; ++i) {
                const float x0 = xs.map(bd.x[i]), x1 = xs.map(bd.x[i + 1]);
                const float l0 = ys.map(bd.yLo.empty() ? 0.0 : bd.yLo[i]);
                const float l1 = ys.map(bd.yLo.empty() ? 0.0 : bd.yLo[i + 1]);
                const float h0 = ys.map(bd.yHi[i]), h1 = ys.map(bd.yHi[i + 1]);
                if (!finite(x0) || !finite(x1) || !finite(l0) || !finite(l1) || !finite(h0) ||
                    !finite(h1))
                    continue;
                dl->PathLineTo(ImVec2(x0, l0));
                dl->PathLineTo(ImVec2(x1, l1));
                dl->PathLineTo(ImVec2(x1, h1));
                dl->PathLineTo(ImVec2(x0, h0));
                dl->PathFillConvex(col);
            }
        }
        if (bd.label && bd.label[0]) legend.push_back({bd.label, Swatch::Fill, col});
    }
    for (const auto& r : spec.refs) {
        const ImU32 col = toneCol(r.tone, surf);
        if (r.vertical) {
            const float x = xs.map(r.value);
            if (finite(x)) strokeLine(dl, ImVec2(x, a.y), ImVec2(x, b.y), col, m::stroke(1.25f), r.dashed);
        } else {
            const float y = ys.map(r.value);
            if (finite(y)) strokeLine(dl, ImVec2(a.x, y), ImVec2(b.x, y), col, m::stroke(1.25f), r.dashed);
        }
    }
    // Trace segments in pixels, collected only when a Chart has ref labels to keep clear of them.
    const bool                            placeRefLabels = !spark && refLabels;
    std::vector<std::pair<ImVec2, ImVec2>> segs;
    std::vector<ImVec2>                    pts;
    for (size_t si = 0; si < spec.series.size(); ++si) {
        const PlotSeries& s   = spec.series[si];
        const PlotData&   sx  = seriesX(s, spec);
        const int         n   = seriesN(s, sx);
        const ImU32       col = vecU32(seriesVec(s, (int)si, surf), s.dim ? kDimSeriesA : 1.0f);
        const float       th  = s.dim ? m::stroke(1.0f) : m::stroke(1.75f);
        auto flush = [&]() {
            if (s.line && pts.size() >= 2) {
                if (s.dashed) dashedPolyline(dl, pts.data(), (int)pts.size(), col, th);
                else          dl->AddPolyline(pts.data(), (int)pts.size(), col, 0, th);
            }
            if (placeRefLabels) {
                if (s.line && pts.size() >= 2)
                    for (size_t k = 1; k < pts.size(); ++k) segs.emplace_back(pts[k - 1], pts[k]);
                else
                    for (const ImVec2& q : pts) segs.emplace_back(q, q);
            }
            pts.clear();
        };
        for (int i = 0; i < n; ++i) {
            const float x = xs.map(xAt(sx, i)), y = ys.map(s.y[i]);
            if (!finite(x) || !finite(y) || !finite(s.y[i])) {
                flush();
                continue;
            }
            pts.emplace_back(x, y);
            if (s.points) dl->AddCircleFilled(ImVec2(x, y), m::U(0.6f), col);
        }
        flush();
        if (s.label && s.label[0])
            legend.push_back({s.label, s.line ? (s.dashed ? Swatch::Dashed : Swatch::Line) : Swatch::Dot, col});
    }
    for (const auto& mk : spec.markers) {
        const float x = xs.map(mk.x), y = ys.map(mk.y);
        const ImU32 col = toneCol(mk.tone, surf);
        if (finite(x) && finite(y)) {
            dl->AddCircleFilled(ImVec2(x, y), m::U(1.0f), col);
            if (placeRefLabels) segs.emplace_back(ImVec2(x, y), ImVec2(x, y));
            dl->AddCircle(ImVec2(x, y), m::U(1.0f), inkU32(), 0, m::stroke(1.0f));
        }
        if (mk.label && mk.label[0]) legend.push_back({mk.label, Swatch::Dot, col});
    }
    // Reference labels (Chart): at a plot edge on a plate, on whichever side of the line no trace
    // crosses. Horizontal: right end then left end, above then below the line. Vertical: top then
    // bottom, right of the line then left. Ties go to the earliest (preferred) slot; labels never
    // stack on each other. Sparkline labels live on the caption row instead (below).
    if (placeRefLabels) {
        const float         plateX = m::U(0.5f);
        const float         gap    = pad * 0.5f;
        std::vector<ImRect> placed;
        for (const auto& r : spec.refs) {
            if (!r.label || !r.label[0]) continue;
            const std::string txt = visibleLabel(r.label);
            const ImVec2      ts  = ImGui::CalcTextSize(txt.c_str());
            const float       w   = std::min(ts.x + 2.0f * plateX, b.x - a.x);
            const float       h   = ts.y;
            ImVec2            cand[4];
            if (r.vertical) {
                const float x = xs.map(r.value);
                if (!finite(x)) continue;
                const float xr = x + gap, xl = x - gap - w, yt = a.y + gap, yb = b.y - gap - h;
                cand[0] = ImVec2(xr, yt);
                cand[1] = ImVec2(xl, yt);
                cand[2] = ImVec2(xr, yb);
                cand[3] = ImVec2(xl, yb);
            } else {
                const float y = ys.map(r.value);
                if (!finite(y)) continue;
                const float xr = b.x - pad - w, xl = a.x + pad, ya = y - gap - h, yb = y + gap;
                cand[0] = ImVec2(xr, ya);
                cand[1] = ImVec2(xr, yb);
                cand[2] = ImVec2(xl, ya);
                cand[3] = ImVec2(xl, yb);
            }
            int best = -1, bestScore = std::numeric_limits<int>::max();
            for (int c = 0; c < 4 && bestScore > 0; ++c) {
                const ImRect rc(cand[c], ImVec2(cand[c].x + w, cand[c].y + h));
                if (rc.Min.x < a.x - 0.5f || rc.Max.x > b.x + 0.5f || rc.Min.y < a.y - 0.5f ||
                    rc.Max.y > b.y + 0.5f)
                    continue;
                int score = 0;
                for (const ImRect& o : placed)
                    if (o.Overlaps(rc)) score += (int)segs.size() + 1;   // worse than any trace hit
                for (const auto& sg : segs)
                    if (segHitsRect(sg.first, sg.second, rc)) ++score;
                if (score < bestScore) {
                    bestScore = score;
                    best      = c;
                }
            }
            ImVec2 o = best >= 0 ? cand[best] : cand[0];
            if (best < 0) {   // a plot too short for any slot: clamp the preferred one inside
                o.x = std::clamp(o.x, a.x, std::max(a.x, b.x - w));
                o.y = std::clamp(o.y, a.y, std::max(a.y, b.y - h));
            }
            const ImRect rc(o, ImVec2(o.x + w, o.y + h));
            placed.push_back(rc);
            dl->AddRectFilled(rc.Min, rc.Max, roleU32(palette.frameBg, kPlateA), ui::metrics::rounding(2.0f));
            const std::string shown = elide(txt.c_str(), w - 2.0f * plateX);
            dl->AddText(ImVec2(o.x + plateX, o.y), toneCol(r.tone, surf), shown.c_str());
        }
    }

    // ---- hover cursor + readout ----
    std::vector<int> nearest(spec.series.size(), -1);
    if (out.hovered) {
        float cursorX = NAN;
        for (size_t si = 0; si < spec.series.size(); ++si) {
            const PlotSeries& s  = spec.series[si];
            const PlotData&   sx = seriesX(s, spec);
            const int         n  = seriesN(s, sx);
            float             best = FLT_MAX;
            for (int i = 0; i < n; ++i) {
                const float x = xs.map(xAt(sx, i));
                if (!finite(x) || !finite(s.y[i])) continue;
                const float d = std::fabs(x - mouse.x);
                if (d < best) {
                    best        = d;
                    nearest[si] = i;
                }
            }
            if (nearest[si] >= 0 && out.index < 0) {
                out.index = nearest[si];
                cursorX   = xs.map(xAt(sx, nearest[si]));
            }
        }
        if (finite(cursorX))
            dl->AddLine(ImVec2(cursorX, a.y), ImVec2(cursorX, b.y), inkU32(kCursorA), m::stroke(1.0f));
        for (size_t si = 0; si < spec.series.size(); ++si) {
            if (nearest[si] < 0) continue;
            const PlotSeries& s  = spec.series[si];
            const float       x  = xs.map(xAt(seriesX(s, spec), nearest[si]));
            const float       y  = ys.map(s.y[nearest[si]]);
            if (finite(x) && finite(y))
                dl->AddCircleFilled(ImVec2(x, y), m::U(0.75f), vecU32(seriesVec(s, (int)si, surf)));
        }
    }
    dl->PopClipRect();

    // Sparkline caption row, above the trace: caption (+ last value) on the left, labelled
    // reference lines (swatch + label, in the line's tone) on the right.
    std::string capFull, capShown;
    if (capRow) {
        struct RefTag {
            std::string text;
            LegendEntry sw;
            float       w;
        };
        const float         swW = m::U(3.5f), swGap = m::U(1.0f), tagGap = m::U(2.0f);
        std::vector<RefTag> tags;
        float               tagsW = 0.0f;
        for (const auto& r : spec.refs) {
            if (!r.label || !r.label[0]) continue;
            RefTag t{visibleLabel(r.label),
                     {nullptr, r.dashed ? Swatch::Dashed : Swatch::Line, toneCol(r.tone, surf)},
                     0.0f};
            t.w = swW + swGap + ImGui::CalcTextSize(t.text.c_str()).x;
            tagsW += (tags.empty() ? 0.0f : tagGap) + t.w;
            tags.push_back(std::move(t));
        }
        if (spec.caption && spec.caption[0]) {
            capFull = spec.caption;
            if (spec.captionLast && !spec.series.empty()) {
                const PlotSeries& s = spec.series[0];
                for (int i = s.y.size() - 1; i >= 0; --i)
                    if (finite(s.y[i])) {
                        capFull += "  " + withUnit(s.y[i], spec.yAxis.unit);
                        break;
                    }
            }
        }
        // The caption keeps at least ~6 em; reference tags that cannot fit beside it are dropped
        // from the row (the dashed line itself still shows in the trace).
        const float capMinW =
            capFull.empty() ? 0.0f
                            : std::min(ImGui::CalcTextSize(capFull.c_str()).x, ImGui::GetFontSize() * 6.0f);
        const bool tagsFit = !tags.empty() && tagsW + (capFull.empty() ? 0.0f : capMinW + 2.0f * pad) <= W;
        if (tagsFit) {
            float x = p0.x + W - tagsW;
            for (const auto& t : tags) {
                drawSwatch(dl, ImVec2(x, p0.y), lineH, t.sw);
                dl->AddText(ImVec2(x + swW + swGap, p0.y), t.sw.col, t.text.c_str());
                x += t.w + tagGap;
            }
        }
        if (!capFull.empty()) {
            capShown = elide(capFull.c_str(), W - (tagsFit ? tagsW + 2.0f * pad : 0.0f));
            dl->AddText(p0, dimInk, capShown.c_str());
        }
    }
    dl->PopClipRect();

    if (out.hovered && ImGui::BeginTooltip()) {
        if (spark && capShown != capFull) readoutLine(nullptr, capFull);   // elided caption in full
        if (out.index >= 0) {
            double xv = out.x;
            for (size_t si = 0; si < spec.series.size(); ++si)
                if (nearest[si] >= 0) {
                    xv = xAt(seriesX(spec.series[si], spec), nearest[si]);
                    break;
                }
            const std::string xv_s = withUnit(xv, spec.xAxis.unit);
            readoutLine(nullptr, spec.xAxis.title && spec.xAxis.title[0]
                                     ? std::string(spec.xAxis.title) + ": " + xv_s
                                     : xv_s);
        }
        for (size_t si = 0; si < spec.series.size(); ++si) {
            if (nearest[si] < 0) continue;
            const PlotSeries& s  = spec.series[si];
            const LegendEntry sw = {nullptr, s.dashed ? Swatch::Dashed : Swatch::Line,
                                    vecU32(seriesVec(s, (int)si, surf))};
            std::string name = visibleLabel(s.label);
            if (name.empty())
                name = spec.yAxis.title && spec.yAxis.title[0] ? spec.yAxis.title : "Value";
            readoutLine(&sw, name + ": " + withUnit(s.y[nearest[si]], spec.yAxis.unit));
        }
        const float hit = m::U(kHitRadiusU);
        for (const auto& mk : spec.markers) {
            const float x = xs.map(mk.x), y = ys.map(mk.y);
            if (!finite(x) || !finite(y) || std::fabs(x - mouse.x) > hit || std::fabs(y - mouse.y) > hit)
                continue;
            const LegendEntry sw  = {nullptr, Swatch::Dot, toneCol(mk.tone, surf)};
            const std::string mkl = visibleLabel(mk.label);
            readoutLine(&sw, (mkl.empty() ? std::string("Point") : mkl) + ": " +
                                 withUnit(mk.x, spec.xAxis.unit) + ", " +
                                 withUnit(mk.y, spec.yAxis.unit));
        }
        ImGui::EndTooltip();
    }

    if (!spark && spec.legend && legend.size() >= 2) drawLegend(legend, W);
    ImGui::PopID();
    return out;
}

// ==================================================================================================
// bar plot
// ==================================================================================================
PlotHover barPlot(const char* id, const BarSpec& spec) {
    PlotHover out;
    ImGui::PushID(id && id[0] ? id : "kit_bars");

    const AdaptiveSurface surf  = inkSurface();
    const float           lineH = ImGui::GetTextLineHeight();
    const float           pad   = m::U(1.0f);
    const float           W     = std::max(ImGui::GetContentRegionAvail().x, lineH * 4.0f);
    const int             n     = (int)spec.bars.size();
    const bool            horiz = spec.horizontal;
    const float           rowH  = lineH * kBarRowLines;

    Range rv;
    rv.log   = spec.value.log;
    bool any = false;
    for (const auto& br : spec.bars) {
        if (finite(br.value) && (!rv.log || br.value > 0.0)) any = true;
        rv.add(br.value);
        rv.add(br.lo);
        rv.add(br.mark);
    }
    const std::string valTitle = axisTitle(spec.value);
    const std::string catTitle = spec.categoryTitle ? spec.categoryTitle : "";
    const float       titleH   = valTitle.empty() ? 0.0f : lineH + pad;
    const float       tickH    = lineH + pad;
    const float       catTH    = catTitle.empty() ? 0.0f : lineH;
    float H = spec.height > 0.0f ? spec.height
                                 : horiz ? lineH * 0.5f + std::max(n, 1) * rowH + tickH + titleH
                                         : lineH * kAutoColLines;
    if (!any) {
        emptyFrame(W, std::max(H, lineH * 3.0f), spec.emptyText);
        ImGui::PopID();
        return out;
    }
    for (const auto& r : spec.refs) rv.add(r.value);
    if (!rv.log) rv.add(0.0);   // bars grow from zero

    const ImVec2 p0 = ImGui::GetCursorScreenPos();
    Scale        vs;
    ImVec2       a, b;
    float        Htot;
    if (horiz) {
        float gutter = 0.0f;
        for (const auto& br : spec.bars)
            if (br.label) gutter = std::max(gutter, ImGui::CalcTextSize(br.label).x);
        gutter    = std::min(gutter, W * 0.4f) + 2.0f * pad;
        const float bodyH = std::max(H - lineH * 0.5f - tickH - titleH, (float)n * lineH);
        a  = ImVec2(p0.x + gutter, p0.y + lineH * 0.5f);
        b  = ImVec2(p0.x + W - pad, a.y + bodyH);
        vs = buildScale(rv, spec.value, b.x - a.x, ImGui::GetFontSize() * kTickGapXEm, true);
        vs.p0 = a.x;
        vs.p1 = b.x;
        Htot  = lineH * 0.5f + bodyH + tickH + titleH;
    } else {
        const float topM  = titleH + lineH * 0.5f;
        const float areaH = std::max(H - topM - tickH - catTH, lineH * 2.0f);
        vs = buildScale(rv, spec.value, areaH, lineH * kTickGapYLines, true);
        const float gutter = std::max(maxLabelW(vs), ImGui::CalcTextSize("000").x) + 2.0f * pad;
        a     = ImVec2(p0.x + gutter, p0.y + topM);
        b     = ImVec2(p0.x + W - pad, a.y + areaH);
        vs.p0 = b.y;
        vs.p1 = a.y;
        Htot  = topM + areaH + tickH + catTH;
    }
    const float slot = horiz ? (b.y - a.y) / std::max(n, 1) : (b.x - a.x) / std::max(n, 1);

    ImGui::InvisibleButton("##bars", ImVec2(W, Htot));
    const ImVec2 mouse = ImGui::GetIO().MousePos;
    const bool   inside = horiz ? (mouse.y >= a.y && mouse.y < b.y) : (mouse.x >= a.x && mouse.x < b.x);
    out.hovered = ImGui::IsItemHovered() && inside && n > 0;
    if (out.hovered) {
        out.index = std::clamp((int)((horiz ? mouse.y - a.y : mouse.x - a.x) / slot), 0, n - 1);
        out.x     = vs.unmap(horiz ? mouse.x : mouse.y);
    }
    out.clicked = out.hovered && ImGui::IsItemClicked(ImGuiMouseButton_Left);

    ImDrawList* dl      = ImGui::GetWindowDrawList();
    const ImU32 dimInk  = inkU32(kDimInkA);
    const ImU32 gridCol = inkU32(kGridA);
    dl->PushClipRect(p0, ImVec2(p0.x + W, p0.y + Htot), true);
    dl->AddRectFilled(a, b, roleU32(palette.frameBg, kFrameA), ui::metrics::rounding(3.0f));

    // value-axis grid + labels
    float lastRight = -FLT_MAX;
    for (size_t g = 0; g < vs.ticks.size(); ++g) {
        const float  v  = vs.map(vs.ticks[g]);
        const ImVec2 ts = ImGui::CalcTextSize(vs.labels[g].c_str());
        if (horiz) {
            if (!(v >= a.x - 0.5f && v <= b.x + 0.5f)) continue;
            dl->AddLine(ImVec2(v, a.y), ImVec2(v, b.y), gridCol, m::stroke(1.0f));
            const float tx = std::clamp(v - 0.5f * ts.x, a.x, std::max(a.x, b.x - ts.x));
            if (tx < lastRight + pad) continue;
            dl->AddText(ImVec2(tx, b.y + pad), dimInk, vs.labels[g].c_str());
            lastRight = tx + ts.x;
        } else {
            if (!(v >= a.y - 0.5f && v <= b.y + 0.5f)) continue;
            dl->AddLine(ImVec2(a.x, v), ImVec2(b.x, v), gridCol, m::stroke(1.0f));
            dl->AddText(ImVec2(a.x - ts.x - pad, v - ts.y * 0.5f), dimInk, vs.labels[g].c_str());
        }
    }
    if (!valTitle.empty()) {
        const std::string t = elide(valTitle.c_str(), W);
        if (horiz) {
            const float tw = ImGui::CalcTextSize(t.c_str()).x;
            dl->AddText(ImVec2(p0.x + W - tw, b.y + tickH), dimInk, t.c_str());
        } else {
            dl->AddText(p0, dimInk, t.c_str());
        }
    }
    if (!horiz && !catTitle.empty()) {
        const std::string t  = elide(catTitle.c_str(), W);
        const float       tw = ImGui::CalcTextSize(t.c_str()).x;
        dl->AddText(ImVec2(p0.x + W - tw, b.y + tickH), dimInk, t.c_str());
    }

    // base of a bar without `lo`: zero on a linear axis, the axis floor on a log one
    const float basePx = vs.log ? vs.p0 : vs.map(std::clamp(0.0, vs.lo, vs.hi));
    // vertical category labels: every k-th when they would collide
    float maxCat = 0.0f;
    for (const auto& br : spec.bars)
        if (br.label) maxCat = std::max(maxCat, ImGui::CalcTextSize(br.label).x);
    const int every = horiz ? 1 : std::max(1, (int)std::ceil((maxCat + pad) / std::max(slot, 1.0f)));

    for (int i = 0; i < n; ++i) {
        const PlotBar& br  = spec.bars[i];
        const ImVec4   cv  = br.toned ? toneOn(br.tone, surf) : seriesColor(br.ramp >= 0 ? br.ramp : 0, surf);
        const ImU32    col = vecU32(cv, br.dim ? kDimSeriesA : kBarA);
        const float    v1  = vs.map(br.value);
        const float    v0  = finite(br.lo) ? vs.map(br.lo) : basePx;
        if (i == out.index && out.hovered) {
            if (horiz)
                dl->AddRectFilled(ImVec2(a.x, a.y + slot * i), ImVec2(b.x, a.y + slot * (i + 1)),
                                  inkU32(kHoverWashA));
            else
                dl->AddRectFilled(ImVec2(a.x + slot * i, a.y), ImVec2(a.x + slot * (i + 1), b.y),
                                  inkU32(kHoverWashA));
        }
        if (horiz) {
            const float cy = a.y + slot * (i + 0.5f), th = slot * kBarThick;
            if (br.label) {
                const std::string l  = elide(br.label, a.x - p0.x - 2.0f * pad);
                const ImVec2      ts = ImGui::CalcTextSize(l.c_str());
                dl->AddText(ImVec2(a.x - pad - ts.x, cy - ts.y * 0.5f), br.dim ? dimInk : inkU32(), l.c_str());
            }
            dl->PushClipRect(a, b, true);
            if (finite(v1) && finite(v0))
                dl->AddRectFilled(ImVec2(std::min(v0, v1), cy - th * 0.5f),
                                  ImVec2(std::max(v0, v1), cy + th * 0.5f), col, m::rounding(2.0f));
            const float mk = finite(br.mark) ? vs.map(br.mark) : NAN;
            if (finite(mk)) dl->AddCircleFilled(ImVec2(mk, cy), th * 0.55f, inkU32());
            if (br.note && br.note[0] && finite(v1)) {
                const ImVec2 ts = ImGui::CalcTextSize(br.note);
                const float  nx = v1 + pad + ts.x <= b.x ? v1 + pad : std::max(a.x, v1 - pad - ts.x);
                dl->AddText(ImVec2(nx, cy - ts.y * 0.5f), dimInk, br.note);
            }
            dl->PopClipRect();
        } else {
            const float bw = slot * kBarFill, x0 = a.x + slot * i + (slot - bw) * 0.5f;
            dl->PushClipRect(a, b, true);
            if (finite(v1) && finite(v0))
                dl->AddRectFilled(ImVec2(x0, std::min(v0, v1)), ImVec2(x0 + bw, std::max(v0, v1)), col,
                                  m::rounding(2.0f));
            const float mk = finite(br.mark) ? vs.map(br.mark) : NAN;
            if (finite(mk)) dl->AddCircleFilled(ImVec2(x0 + bw * 0.5f, mk), bw * 0.25f, inkU32());
            if (br.note && br.note[0] && finite(v1)) {
                const ImVec2 ts = ImGui::CalcTextSize(br.note);
                dl->AddText(ImVec2(x0 + (bw - ts.x) * 0.5f, std::max(a.y, std::min(v0, v1) - ts.y)), dimInk,
                            br.note);
            }
            dl->PopClipRect();
            if (br.label && i % every == 0) {
                const ImVec2 ts = ImGui::CalcTextSize(br.label);
                const float  cx = x0 + bw * 0.5f;
                dl->AddText(ImVec2(std::clamp(cx - ts.x * 0.5f, a.x, std::max(a.x, b.x - ts.x)), b.y + pad),
                            br.dim ? dimInk : inkU32(), br.label);
            }
        }
    }
    // references on the value axis
    dl->PushClipRect(a, b, true);
    for (const auto& r : spec.refs) {
        const ImU32 col = toneCol(r.tone, surf);
        const float v   = vs.map(r.value);
        if (!finite(v)) continue;
        if (horiz) strokeLine(dl, ImVec2(v, a.y), ImVec2(v, b.y), col, m::stroke(1.25f), r.dashed);
        else       strokeLine(dl, ImVec2(a.x, v), ImVec2(b.x, v), col, m::stroke(1.25f), r.dashed);
        if (r.label && r.label[0]) {
            // Same plated edge label as linePlot, so a bar never strikes through it.
            const std::string txt = visibleLabel(r.label);
            const ImVec2      ts  = ImGui::CalcTextSize(txt.c_str());
            const float       px  = m::U(0.5f);
            const ImVec2      o   = horiz ? ImVec2(v + pad + ts.x + 2.0f * px <= b.x ? v + pad
                                                                              : v - pad - ts.x - 2.0f * px,
                                                   a.y)
                                          : ImVec2(b.x - ts.x - 2.0f * px - pad,
                                                   std::max(a.y, v - ts.y - pad * 0.5f));
            dl->AddRectFilled(o, ImVec2(o.x + ts.x + 2.0f * px, o.y + ts.y),
                              roleU32(palette.frameBg, kPlateA), m::rounding(2.0f));
            dl->AddText(ImVec2(o.x + px, o.y), col, txt.c_str());
        }
    }
    dl->PopClipRect();
    dl->PopClipRect();

    if (out.hovered && ImGui::BeginTooltip()) {
        const PlotBar&    br = spec.bars[out.index];
        const LegendEntry sw = {br.label, Swatch::Fill,
                                vecU32(br.toned ? toneOn(br.tone, surf)
                                                : seriesColor(br.ramp >= 0 ? br.ramp : 0, surf))};
        std::string txt = br.label && br.label[0] ? std::string(br.label) + ": " : std::string();
        if (finite(br.lo)) txt += fmtValue(br.lo) + " " + kEmDash + " ";
        txt += withUnit(br.value, spec.value.unit);
        readoutLine(&sw, txt);
        if (finite(br.mark)) readoutLine(nullptr, "Mark: " + withUnit(br.mark, spec.value.unit));
        if (br.note && br.note[0]) readoutLine(nullptr, br.note);
        if (br.tooltip && br.tooltip[0]) {
            ImGui::PushTextWrapPos(ImGui::GetFontSize() * kTooltipWrapEm);
            ImGui::TextUnformatted(br.tooltip);
            ImGui::PopTextWrapPos();
        }
        ImGui::EndTooltip();
    }
    ImGui::PopID();
    return out;
}

// ==================================================================================================
// chip flow
// ==================================================================================================
namespace {
struct FlowFrame {
    float startX   = 0.0f;
    float rightX   = 0.0f;
    float prevMaxX = 0.0f;
    int   index    = 0;
    bool  open     = false;
};
std::vector<FlowFrame> g_flows;

float flowGap() { return m::U(1.0f); }

// Ends the open item group, caching its measured width for the next frame's wrap decision.
void closeFlowItem(FlowFrame& f) {
    if (!f.open) return;
    ImGui::PopID();
    ImGui::EndGroup();
    f.open                = false;
    f.prevMaxX            = ImGui::GetItemRectMax().x;
    const float   w       = ImGui::GetItemRectSize().x;
    ImGuiStorage* store   = ImGui::GetStateStorage();
    const ImGuiID key     = ImGui::GetID(f.index - 1);
    if (std::fabs(store->GetFloat(key, -1.0f) - w) > 0.5f) {
        store->SetFloat(key, w);
        anim::requestFrames();   // a newly measured width re-flows on the next frame
    }
}

float chipEstimate(float textW, bool dot) {
    return m::U(1.5f) * 2.0f + textW + (dot ? m::U(0.75f) * 2.0f + m::U(1.0f) : 0.0f);
}
}  // namespace

void chipFlowBegin(const char* id) {
    ImGui::PushID(id && id[0] ? id : "kit_flow");
    FlowFrame f;
    f.startX = ImGui::GetCursorScreenPos().x;
    f.rightX = f.startX + std::max(ImGui::GetContentRegionAvail().x, m::U(1.0f));
    g_flows.push_back(f);
}

void chipFlowItem(float width) {
    if (g_flows.empty()) return;
    FlowFrame& f = g_flows.back();
    closeFlowItem(f);
    const float w = width > 0.0f ? width : ImGui::GetStateStorage()->GetFloat(ImGui::GetID(f.index), 0.0f);
    if (f.index > 0) {
        if (f.prevMaxX + flowGap() + w <= f.rightX)
            ImGui::SameLine(0.0f, flowGap());
        else
            ImGui::SetCursorScreenPos(ImVec2(f.startX, ImGui::GetCursorScreenPos().y));
    }
    ImGui::BeginGroup();
    ImGui::PushID(f.index);
    f.open = true;
    ++f.index;
}

void chipFlowEnd() {
    if (g_flows.empty()) return;   // unmatched End: refuse rather than corrupt the ImGui stacks
    closeFlowItem(g_flows.back());
    g_flows.pop_back();
    ImGui::PopID();
}

void flowChip(const char* label, Tone t, bool dot, const char* tooltip) {
    chipFlowItem(chipEstimate(ImGui::CalcTextSize(label ? label : "", nullptr, true).x, dot));
    chip(label, t, dot, tooltip);
}

void flowValueChip(const char* label, const char* value, Tone t, const char* tooltip) {
    chipFlowItem(m::U(1.5f) * 2.0f + ImGui::CalcTextSize(label ? label : "").x + m::U(1.0f) +
                 ImGui::CalcTextSize(value ? value : "").x);
    ui::kit::valueChip(label, value, t, tooltip);
}

bool flowToggleChip(const char* label, bool* on, Tone t, const char* tooltip) {
    chipFlowItem(chipEstimate(ImGui::CalcTextSize(label ? label : "", nullptr, true).x, false));
    return ui::kit::toggleChip(label, on, t, tooltip);
}

bool removableChip(const char* id, const char* label, const char* tip, Tone t, bool* hovered) {
    const std::string lab  = visibleLabel(label);
    const float       h    = ImGui::GetFrameHeight();
    const float       padL = m::U(1.5f);
    const float       xW   = h;   // the x zone: a frame-height square at the right end
    // Width to the label, capped to the line it sits on so a long face name elides, not overflows.
    float lineW = ImGui::GetContentRegionAvail().x;
    if (!g_flows.empty()) lineW = g_flows.back().rightX - g_flows.back().startX;
    const float w = std::max(std::min(padL + ImGui::CalcTextSize(lab.c_str()).x + xW, lineW), padL + xW);
    if (!g_flows.empty()) chipFlowItem(w);

    ImGui::PushID(id && id[0] ? id : "kit_rmchip");
    ImGui::BeginGroup();
    const ImVec2 p0    = ImGui::GetCursorScreenPos();
    const float  bodyW = w - xW;
    ImGui::InvisibleButton("##body", ImVec2(bodyW, h));
    const bool bodyHov = ImGui::IsItemHovered();
    ImGui::SameLine(0.0f, 0.0f);
    const bool removed = ImGui::InvisibleButton("##remove", ImVec2(xW, h));
    const bool xHov    = ImGui::IsItemHovered();
    ImGui::EndGroup();

    const bool  anyHov = bodyHov || xHov;
    const float hv     = anim::toward(ImGui::GetID("##hv"), anyHov ? 1.0f : 0.0f, anim::kFast);
    const float xv     = anim::toward(ImGui::GetID("##xhv"), xHov ? 1.0f : 0.0f, anim::kFast);

    const AdaptiveSurface surf = inkSurface();
    ImDrawList*           dl   = ImGui::GetWindowDrawList();
    const ImVec2          p1(p0.x + w, p0.y + h);
    const float           rnd = chipRound();
    dl->AddRectFilled(p0, p1, toneU32(t, kRmChipFillA + kRmHoverFillA * hv), rnd);
    dl->AddRect(p0, p1, toneU32(t, kRmChipBorderA + (1.0f - kRmChipBorderA) * hv), rnd, 0,
                m::stroke(1.0f));

    // Label: primary ink for a neutral chip (a face name is content, not a caption), else the tone.
    const std::string shown = elide(lab.c_str(), std::max(bodyW - padL, m::U(1.0f)));
    const ImU32       ink   = t == Tone::Neutral ? inkU32() : toneCol(t, surf);
    dl->AddText(ImVec2(p0.x + padL, p0.y + (h - ImGui::GetTextLineHeight()) * 0.5f), ink, shown.c_str());

    // The x: a vector cross (no glyph dependency), dim at rest, Err-toned with a wash on hover.
    const ImVec2 c(p1.x - xW * 0.5f, p0.y + h * 0.5f);
    if (xv > 0.004f) dl->AddCircleFilled(c, h * 0.36f, toneCol(Tone::Err, surf, kRmWashA * xv));
    const float  r     = ImGui::GetFontSize() * kRmGlyphEm;
    const ImVec4 restC = inkVec(kDimInkA), hotC = toneOn(Tone::Err, surf);
    const ImU32  xc    = ImGui::ColorConvertFloat4ToU32(
        ImVec4(restC.x + (hotC.x - restC.x) * xv, restC.y + (hotC.y - restC.y) * xv,
               restC.z + (hotC.z - restC.z) * xv, restC.w + (hotC.w - restC.w) * xv));
    dl->AddLine(ImVec2(c.x - r, c.y - r), ImVec2(c.x + r, c.y + r), xc, m::stroke(1.5f));
    dl->AddLine(ImVec2(c.x - r, c.y + r), ImVec2(c.x + r, c.y - r), xc, m::stroke(1.5f));

    if (xHov) tooltip(tip && tip[0] ? tip : "Remove");
    else if (bodyHov && shown != lab) tooltip(lab.c_str());
    if (hovered) *hovered = anyHov;
    ImGui::PopID();
    return removed;
}

// ==================================================================================================
// key/value results
// ==================================================================================================
void keyValueRowId(const char* id, const char* key, const char* value, Tone valueTone, bool copyable) {
    ImGui::PushID(id && id[0] ? id : "kit_kv");
    keyValueRow(key, value, valueTone, copyable);
    ImGui::PopID();
}

void keyValueCard(const char* id, const char* title, const char* icon,
                  const std::vector<KeyValueItem>& rows, bool* open) {
    if (cardBegin(id, title, icon, open)) {
        if (rows.empty()) dimText("No values reported.");
        // One value column for the whole card: the pane-wide field value column (so readouts line
        // up with the pane's fields), widened only when a key is longer than that.
        float keyW = 0.0f;
        for (const auto& r : rows) keyW = std::max(keyW, ImGui::CalcTextSize(r.key.c_str()).x);
        const float gapX    = ImGui::GetStyle().ItemSpacing.x;
        const float paneCol = fieldValueColumnX() - ImGui::GetCursorScreenPos().x;
        const float col     = std::max(paneCol > gapX ? paneCol : m::pmLabelCol(), keyW + gapX);
        for (size_t i = 0; i < rows.size(); ++i) {
            ImGui::PushID((int)i);
            detail::keyValueRowCol(rows[i].key.c_str(), rows[i].value.c_str(), rows[i].tone,
                                   rows[i].copyable, col);
            ImGui::PopID();
        }
    }
    cardEnd();
}

// ==================================================================================================
// tables
// ==================================================================================================
namespace {
struct TableFrame {
    std::vector<TableColumn> cols;
    int                      col    = -1;
    bool                     table  = false;
    bool                     scroll = false;
    int                      colors = 0;
};
std::vector<TableFrame> g_tables;

// Draw `text` in the current cell, aligned and elided; returns whether it was elided.
bool cellText(const char* text, TableAlign align, ImU32 col) {
    const float       w     = std::max(ImGui::GetContentRegionAvail().x, m::U(1.0f));
    const std::string shown = elide(text, w);
    const float       tw    = ImGui::CalcTextSize(shown.c_str()).x;
    if (align == TableAlign::Right) ImGui::SetCursorPosX(ImGui::GetCursorPosX() + std::max(0.0f, w - tw));
    ImGui::PushStyleColor(ImGuiCol_Text, col);
    ImGui::TextUnformatted(shown.c_str());
    ImGui::PopStyleColor();
    return shown != text;
}
}  // namespace

bool tableBegin(const char* id, const std::vector<TableColumn>& columns, float height) {
    ImGui::PushID(id && id[0] ? id : "kit_table");
    TableFrame f;
    f.cols   = columns;
    f.scroll = height > 0.0f;
    if (f.scroll)
        ImGui::BeginChild("##kit_table_scroll", ImVec2(0.0f, height), ImGuiChildFlags_None,
                          ImGuiWindowFlags_NoScrollbar);
    // Ink-derived chrome: adapts to whatever surface the table sits on, and never ImGui's own
    // blue header fill (the header is a dim caption row, not a TableHeadersRow).
    ImGui::PushStyleColor(ImGuiCol_TableBorderLight, inkVec(kTableBorderA));
    ImGui::PushStyleColor(ImGuiCol_TableBorderStrong, inkVec(kTableBorderA));
    ImGui::PushStyleColor(ImGuiCol_TableRowBg, inkVec(0.0f));
    ImGui::PushStyleColor(ImGuiCol_TableRowBgAlt, inkVec(kTableStripeA));
    f.colors = 4;
    const int n = std::max(1, (int)columns.size());
    f.table = ImGui::BeginTable("##kit_table", n,
                                ImGuiTableFlags_SizingStretchProp | ImGuiTableFlags_BordersInnerH |
                                    ImGuiTableFlags_RowBg | ImGuiTableFlags_NoSavedSettings);
    if (f.table) {
        for (int i = 0; i < n; ++i) {
            const TableColumn c = i < (int)columns.size() ? columns[i] : TableColumn{};
            ImGui::TableSetupColumn(c.header ? c.header : "", ImGuiTableColumnFlags_WidthStretch,
                                    c.weight > 0.0f ? c.weight : 1.0f);
        }
        ImGui::TableNextRow();
        for (int i = 0; i < (int)columns.size(); ++i) {
            ImGui::TableSetColumnIndex(i);
            const char* h = columns[i].header ? columns[i].header : "";
            if (cellText(h, columns[i].align, inkU32(kDimInkA)) && ImGui::IsItemHovered()) tooltip(h);
        }
    }
    g_tables.push_back(std::move(f));
    return g_tables.back().table;
}

void tableRow() {
    if (g_tables.empty() || !g_tables.back().table) return;
    ImGui::TableNextRow();
    g_tables.back().col = -1;
}

void tableCell(const char* text, Tone tone, const char* tip) {
    if (g_tables.empty() || !g_tables.back().table) return;
    TableFrame& f = g_tables.back();
    ++f.col;
    if (f.col >= (int)f.cols.size() || !ImGui::TableSetColumnIndex(f.col)) return;
    const TableColumn& c   = f.cols[f.col];
    const char*        t   = text ? text : "";
    const ImU32        col = tone != Tone::Neutral ? toneU32(tone) : inkU32(c.dim ? kDimInkA : 1.0f);
    const bool         cut = cellText(t, c.align, col);
    if (ImGui::IsItemHovered() && (cut || (tip && tip[0]))) {
        std::string full = cut ? std::string(t) : std::string();
        if (tip && tip[0]) full += (full.empty() ? "" : "\n") + std::string(tip);
        tooltip(full.c_str());
    }
}

void tableEnd() {
    if (g_tables.empty()) return;   // unmatched End: refuse rather than corrupt the ImGui stacks
    TableFrame f = std::move(g_tables.back());
    g_tables.pop_back();
    if (f.table) ImGui::EndTable();
    ImGui::PopStyleColor(f.colors);
    if (f.scroll) {
        ui::drawWindowPillScrollbarV(inkSurface());
        ImGui::EndChild();
    }
    ImGui::PopID();
}

// Adaptive results table (see kit_plot.h). Painted directly (kit table look: dim header, ink
// hairlines between rows, alternate-row wash) because the narrow form needs a row that spans
// every column, which ImGui tables cannot do.
void metricTable(const char* id, const std::vector<const char*>& valueHeaders,
                 const std::vector<MetricRow>& rows, const char* metricHeader) {
    ImGui::PushID(id && id[0] ? id : "kit_metric_table");
    const int nvIn = std::clamp((int)valueHeaders.size(), 1, 3);
    auto header = [&](int i) -> const char* {
        return i < (int)valueHeaders.size() && valueHeaders[i] ? valueHeaders[i] : "Value";
    };
    auto value = [&](const MetricRow& r, int i) -> const char* {
        return i < (int)r.values.size() && !r.values[(size_t)i].text.empty() ? r.values[(size_t)i].text.c_str()
                                                                             : kEmDash;
    };
    auto tone = [&](const MetricRow& r, int i) {
        return i < (int)r.values.size() ? r.values[(size_t)i].tone : Tone::Neutral;
    };

    // Value columns that carry at least one real value (no all-dash "Baseline" without a baseline).
    std::vector<int> cols;
    for (int i = 0; i < nvIn; ++i) {
        bool any = rows.empty();
        for (const auto& r : rows) any = any || std::string(value(r, i)) != kEmDash;
        if (any) cols.push_back(i);
    }
    if (cols.empty()) cols.push_back(0);
    const int nc = (int)cols.size();

    // The unit folds into the metric label: never a separate (narrow, truncating) Unit column.
    std::vector<std::string> labels;
    labels.reserve(rows.size());
    for (const auto& r : rows) labels.push_back(r.unit.empty() ? r.metric : r.metric + " (" + r.unit + ")");
    const char* mHead = metricHeader ? metricHeader : "Metric";

    const ImGuiStyle& st    = ImGui::GetStyle();
    const float       cpX   = st.CellPadding.x;
    const float       cpY   = st.CellPadding.y;
    const float       lineH = ImGui::GetTextLineHeight();
    const float       rowH  = lineH + cpY * 2.0f;
    const float       W     = std::max(ImGui::GetContentRegionAvail().x, m::U(1.0f));

    std::vector<float> colW((size_t)nc);
    float valuesW = 0.0f;
    for (int c = 0; c < nc; ++c) {
        float w = ImGui::CalcTextSize(header(cols[(size_t)c])).x;
        for (const auto& r : rows) w = std::max(w, ImGui::CalcTextSize(value(r, cols[(size_t)c])).x);
        colW[(size_t)c] = w + cpX * 2.0f;
        valuesW += colW[(size_t)c];
    }
    float metricNat = ImGui::CalcTextSize(mHead).x;
    for (const auto& l : labels) metricNat = std::max(metricNat, ImGui::CalcTextSize(l.c_str()).x);
    metricNat += cpX * 2.0f;
    const float metricMin = std::min(metricNat, ImGui::GetFontSize() * kMetricMinEm + cpX * 2.0f);

    // Inline: Metric | values (values at natural width, the metric takes the rest). When that
    // would squeeze the metric below ~7 em, STACK: the metric on its own full-width line, the
    // value columns across the full width beneath it (the header then names only the values).
    const bool stacked = !rows.empty() && metricMin + valuesW > W;
    const float metricW = stacked ? 0.0f : std::max(W - valuesW, metricMin);
    {
        const float room  = W - metricW;
        const float extra = room - valuesW;
        for (auto& w : colW) {
            if (stacked && extra >= 0.0f) w += extra / (float)nc;          // share the slack
            else if (extra < 0.0f)        w *= room / std::max(valuesW, 1.0f);   // cells elide
        }
    }

    ImDrawList*  dl       = ImGui::GetWindowDrawList();
    const ImVec2 p0       = ImGui::GetCursorScreenPos();
    const bool   hoverable = ImGui::IsWindowHovered();
    const ImU32  hairline = inkU32(kTableBorderA);
    const ImU32  stripe   = inkU32(kTableStripeA);
    const ImU32  dimInk   = inkU32(kDimInkA);
    const ImU32  ink      = inkU32();
    const float  t        = m::stroke(1.0f);

    // One text cell in [x0, x1]: aligned, elided, full text (+ tip) on hover.
    auto cell = [&](const char* text, float x0, float x1, float ty, bool right, ImU32 col,
                    const char* tip) {
        const std::string shown = elide(text, std::max(x1 - x0 - cpX * 2.0f, m::U(1.0f)));
        const float       tw    = ImGui::CalcTextSize(shown.c_str()).x;
        dl->AddText(ImVec2(right ? x1 - cpX - tw : x0 + cpX, ty), col, shown.c_str());
        const bool cut = shown != text;
        if ((cut || (tip && tip[0])) && hoverable &&
            ImGui::IsMouseHoveringRect(ImVec2(x0, ty), ImVec2(x1, ty + lineH))) {
            std::string full = cut ? std::string(text) : std::string();
            if (tip && tip[0]) full += (full.empty() ? "" : "\n") + std::string(tip);
            tooltip(full.c_str());
        }
    };
    auto valuesRow = [&](float x, float ty, const MetricRow* r) {
        for (int c = 0; c < nc; ++c) {
            const int   i  = cols[(size_t)c];
            const float x1 = x + colW[(size_t)c];
            if (r) {
                const Tone tn = tone(*r, i);
                cell(value(*r, i), x, x1, ty, true, tn != Tone::Neutral ? toneU32(tn) : ink, nullptr);
            } else {
                cell(header(i), x, x1, ty, true, dimInk, nullptr);
            }
            x = x1;
        }
    };

    float y = p0.y;
    if (!stacked) cell(mHead, p0.x, p0.x + metricW, y + cpY, false, dimInk, nullptr);
    valuesRow(p0.x + metricW, y + cpY, nullptr);
    y += rowH;

    if (rows.empty()) {
        dl->AddLine(ImVec2(p0.x, y), ImVec2(p0.x + W, y), hairline, t);
        cell("No metrics reported.", p0.x, p0.x + W, y + cpY, false, dimInk, nullptr);
        y += rowH;
    }
    const float stackGap = m::U(kMetricStackU);
    for (size_t k = 0; k < rows.size(); ++k) {
        const MetricRow& r      = rows[k];
        const float      blockH = stacked ? cpY * 2.0f + lineH * 2.0f + stackGap : rowH;
        if (k % 2 == 1) dl->AddRectFilled(ImVec2(p0.x, y), ImVec2(p0.x + W, y + blockH), stripe);
        dl->AddLine(ImVec2(p0.x, y), ImVec2(p0.x + W, y), hairline, t);
        if (stacked) {
            cell(labels[k].c_str(), p0.x, p0.x + W, y + cpY, false, ink, r.tooltip);
            valuesRow(p0.x, y + cpY + lineH + stackGap, &r);
        } else {
            cell(labels[k].c_str(), p0.x, p0.x + metricW, y + cpY, false, ink, r.tooltip);
            valuesRow(p0.x + metricW, y + cpY, &r);
        }
        y += blockH;
    }

    ImGui::Dummy(ImVec2(W, y - p0.y));
    ImGui::PopID();
}

MetricCell deltaCell(double from, double to, bool higherIsBetter, int sigFigs) {
    MetricCell c;
    Tone       t = Tone::Neutral;
    c.text       = fmtDelta(from, to, nullptr, higherIsBetter, &t, sigFigs);
    c.tone       = t;
    return c;
}

}  // namespace kit
}  // namespace ui
