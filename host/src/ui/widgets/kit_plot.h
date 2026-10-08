#pragma once
// kit_plot.h — ui::kit charts, chip flow, result cards and read-only tables.
//
// ONE implementation each (kit_plot.cpp). Simulation panels (sim/*, sim_topology*, piping,
// hydraulics, evaluate dialogs) consume these instead of ImGui::PlotLines/PlotHistogram,
// hand-rolled draw-list charts, raw BeginTable and stacked chip calls.
//
// ID RULE: every interactive primitive takes an explicit `id` and scopes itself with PushID(id),
// and every per-item widget inside it is keyed by its INDEX, never its visible text. Two charts,
// cards or tables may therefore show identical labels without an ImGui ID conflict, as long as
// their `id`s differ within the enclosing scope (loops: PushID(index) around the call).
//
// Colors: series ramp from seriesColor() (ui::palette roles, adjusted for the surface); semantic
// lines/bands/markers by kit::Tone. Sizes derive from the font size and ui::metrics. No literals.
//
// CHART COVERAGE (which option replaces which hand-rolled chart):
//   hydraulics plots (pressure/position/speed)  linePlot, shared x = time, N series, legend
//   EM S11/S21, port voltages, electronics,      linePlot, x = frequency/time, 1-2 series
//     vibration response (drawEmLinePlot)
//   topology convergence history                 linePlot + PlotRef::h(target) dashed
//   fatigue Haigh diagram                        linePlot, one series per theory (dim the
//                                                non-selected), PlotBand::between (safe region),
//                                                Langer line as a dashed toned series,
//                                                PlotMarker operating point
//   fatigue S-N curve                            linePlot, xAxis.log, PlotBand::between
//                                                (reliability band), PlotRef::h(Se), PlotMarker
//   fatigue cycle sketch                         linePlot Sparkline, x = phase, PlotRef::h(mean)
//   fatigue life estimate bars                   barPlot horizontal, value.log, PlotBar.lo..value
//                                                range + mark (median), note "inf"
//   mesh aspect histogram                        barPlot vertical (columns)
//   topology live objective/volume/change        linePlot Sparkline (caption + last value)
//   flow drag trace                              linePlot Sparkline
//   motion position trace                        linePlot (or Sparkline) + PlotRef::v(playhead)
//   piping pressure profile                      linePlot, x = station index or distance
//
// GL/GUI thread only.

#include <limits>
#include <string>
#include <vector>

#include "imgui.h"

#include "../../ui_theme.h"   // ui::AdaptiveSurface
#include "kit.h"              // Tone (kit.h includes this header after its own declarations)

namespace ui {
namespace kit {

// =================================================================================================
// series palette
// =================================================================================================
// Categorical chart ramp from ui::palette roles (accent, accent2, axis/CAM hues), cycling past its
// length, retuned for legibility on `surf` like toneOn(). Status roles (Ok/Warn/Err) are NOT in the
// ramp, so a Tone-colored reference line or band never reads as "series 3".
//   ImU32 c = kit::seriesColorU32(i, surf);
ImVec4 seriesColor(int i, const AdaptiveSurface& surf);
ImU32  seriesColorU32(int i, const AdaptiveSurface& surf, float alphaMul = 1.0f);

// =================================================================================================
// line plot
// =================================================================================================
// Non-owning view of a float or double array (implicitly built from std::vector<float/double>).
// The array must outlive the plot call; never pass a temporary vector.
struct PlotData {
    const float*  f = nullptr;
    const double* d = nullptr;
    int           n = 0;
    PlotData() = default;
    PlotData(const std::vector<float>& v) : f(v.data()), n((int)v.size()) {}
    PlotData(const std::vector<double>& v) : d(v.data()), n((int)v.size()) {}
    PlotData(const float* p, int count) : f(p), n(count) {}
    PlotData(const double* p, int count) : d(p), n(count) {}
    int    size() const { return n; }
    bool   empty() const { return n <= 0; }
    double operator[](int i) const { return f ? (double)f[i] : d ? d[i] : 0.0; }
};

// One curve. Aggregate-friendly: `{"Pressure", ys}`. Non-finite samples break the line.
struct PlotSeries {
    const char* label  = nullptr;  // legend + hover readout; null = unlabelled (no legend entry)
    PlotData    y;
    PlotData    x;                 // optional per-series x; empty = PlotSpec::x (or sample index)
    int         ramp   = -1;       // seriesColor index; -1 = this series' position in the list
    bool        toned  = false;    // true = color by `tone` instead of the ramp
    Tone        tone   = Tone::Neutral;
    bool        dashed = false;
    bool        dim    = false;    // de-emphasised (thin, translucent): e.g. non-selected theories
    bool        points = false;    // dot at every sample (with line=false: a scatter)
    bool        line   = true;
};

// Horizontal (value on y) or vertical (value on x) reference line. Chart: the label sits at a plot
// edge (right or left end, above or below the line — whichever side no trace crosses) on a plate
// so a trace never strikes through it. Sparkline: the label goes to the caption row, right side.
//   PlotRef::h(0.30, "Volume target")            dashed Warn target
//   PlotRef::v(t, nullptr, Tone::Accent, false)  solid playhead
struct PlotRef {
    double      value    = 0.0;
    const char* label    = nullptr;
    Tone        tone     = Tone::Warn;
    bool        vertical = false;
    bool        dashed   = true;
    static PlotRef h(double y, const char* label = nullptr, Tone t = Tone::Warn, bool dashed = true) {
        return PlotRef{y, label, t, false, dashed};
    }
    static PlotRef v(double x, const char* label = nullptr, Tone t = Tone::Warn, bool dashed = true) {
        return PlotRef{x, label, t, true, dashed};
    }
};

// Shaded region drawn under the series.
//   ySpan(lo, hi)            full-width band between two y values (tolerance window)
//   xSpan(lo, hi)            full-height band between two x values (time window)
//   between(x, yLo, yHi)     region between two curves sampled on `x` (S-N reliability band);
//                            empty yLo = down to y = 0 (Haigh safe region under a theory line)
// `ramp` >= 0 colors it like that series instead of by `tone`.
struct PlotBand {
    enum class Kind { YSpan, XSpan, Between };
    Kind        kind  = Kind::YSpan;
    double      lo    = 0.0, hi = 0.0;
    PlotData    x, yLo, yHi;
    const char* label = nullptr;   // legend entry (filled swatch) when set
    Tone        tone  = Tone::Ok;
    int         ramp  = -1;
    static PlotBand ySpan(double lo, double hi, const char* label = nullptr, Tone t = Tone::Ok) {
        PlotBand b; b.kind = Kind::YSpan; b.lo = lo; b.hi = hi; b.label = label; b.tone = t; return b;
    }
    static PlotBand xSpan(double lo, double hi, const char* label = nullptr, Tone t = Tone::Ok) {
        PlotBand b; b.kind = Kind::XSpan; b.lo = lo; b.hi = hi; b.label = label; b.tone = t; return b;
    }
    static PlotBand between(PlotData x, PlotData yLo, PlotData yHi, const char* label = nullptr,
                            Tone t = Tone::Ok) {
        PlotBand b; b.kind = Kind::Between; b.x = x; b.yLo = yLo; b.yHi = yHi; b.label = label;
        b.tone = t; return b;
    }
};

// A highlighted point (operating point, critical sample). Hovering it adds it to the readout.
struct PlotMarker {
    double      x = 0.0, y = 0.0;
    const char* label = nullptr;   // legend + readout
    Tone        tone  = Tone::Err;
};

// Axis description. The title row reads "title (unit)"; the y title sits on its own row ABOVE
// the plot, the x title right-aligned under the tick labels. Ticks are 1-2-5 x 10^n; a range
// that nearly reaches 0 snaps to include it (`zero`). Fixed `min`/`max` (finite) override data.
// `log` = decade ticks labelled 10^n (positive samples only; S-N life axis).
struct PlotAxis {
    const char* title   = nullptr;
    const char* unit    = nullptr;
    bool        log     = false;
    bool        zero    = true;
    bool        integer = false;   // ticks never fall between integers (iterations, counts)
    double      min     = std::numeric_limits<double>::quiet_NaN();
    double      max     = std::numeric_limits<double>::quiet_NaN();
};

enum class PlotMode {
    Chart,       // axes, grid, ticks, titles, legend, hover cursor + readout
    Sparkline,   // compact live trace: no axes/legend; caption + ref labels on a row ABOVE the trace
};

struct PlotSpec {
    PlotMode                mode = PlotMode::Chart;
    PlotData                x;            // shared x; empty = sample index 0..n-1
    std::vector<PlotSeries> series;
    PlotAxis                xAxis, yAxis;
    std::vector<PlotRef>    refs;
    std::vector<PlotBand>   bands;
    std::vector<PlotMarker> markers;
    // Total height in px including title/tick/caption rows (legend is extra, below). 0 = auto:
    // ~10 text lines for Chart; ~2.4 lines of trace for Sparkline, plus its caption row if any.
    float       height    = 0.0f;
    bool        legend    = true;     // shown when it has >= 2 entries (series/bands/markers)
    const char* caption   = nullptr;  // Sparkline caption, on its own row above the trace (left)
    bool        captionLast = true;   // Sparkline: append series 0's last value (+ y unit)
    const char* emptyText = nullptr;  // shown in the reserved frame when nothing is plottable
};

struct PlotHover {
    bool   hovered = false;
    bool   clicked = false;   // left click inside the plot area (e.g. seek the motion playhead)
    double x       = 0.0;     // data x under the cursor
    int    index   = -1;      // nearest sample index (shared x / series 0); bar index for barPlot
};

// Draws a full-width chart at the cursor and advances past it (and its legend).
//   kit::PlotSpec p;
//   p.x = t;  p.xAxis = {"Time", "s"};  p.yAxis = {"Pressure", "bar"};
//   p.series.push_back({"Pump outlet", pOut});
//   p.series.push_back({"Cylinder A", pA});
//   p.refs.push_back(kit::PlotRef::h(relief, "Relief setting"));
//   kit::linePlot("hyd_pressure", p);
PlotHover linePlot(const char* id, const PlotSpec& spec);

// =================================================================================================
// bar plot
// =================================================================================================
// One bar per entry. A finite `lo` turns the bar into a range lo..value (life band); a finite
// `mark` adds a dot (median / design point); `note` is drawn past the bar end ("inf").
struct PlotBar {
    const char* label   = nullptr;   // category label (vertical: under the column; horizontal: gutter)
    double      value   = 0.0;
    double      lo      = std::numeric_limits<double>::quiet_NaN();
    double      mark    = std::numeric_limits<double>::quiet_NaN();
    const char* note    = nullptr;
    int         ramp    = -1;        // -1 = seriesColor(0) for every bar
    bool        toned   = false;
    Tone        tone    = Tone::Neutral;
    bool        dim     = false;
    const char* tooltip = nullptr;   // appended to the hover readout
};

struct BarSpec {
    std::vector<PlotBar> bars;
    bool                 horizontal = false;  // false: columns (histogram); true: rows (life bands)
    PlotAxis             value;               // value axis (log for cycles)
    const char*          categoryTitle = nullptr;
    std::vector<PlotRef> refs;                // on the value axis (`vertical` ignored)
    float                height    = 0.0f;    // 0 = auto (columns ~6 lines; rows: one per bar)
    const char*          emptyText = nullptr;
};

//   kit::BarSpec b;  b.value = {"Elements", nullptr, false, true, true};  b.categoryTitle = "Aspect ratio";
//   for (int i = 0; i < 10; ++i) b.bars.push_back({labels[i], hist[i]});
//   kit::barPlot("mesh_aspect", b);
PlotHover barPlot(const char* id, const BarSpec& spec);

// =================================================================================================
// chip flow
// =================================================================================================
// Successive chips flow left-to-right on ONE line and wrap at the content edge instead of
// stacking. Each flow item is index-scoped (PushID(i)), so two chips with the same text never
// collide. Use the flow* wrappers for kit chips; any other item (status glyph, face chip + remove
// button group) is announced with chipFlowItem(widthIfKnown) right before it. Widths are measured
// and cached per item, so an unknown width (0) settles on the next frame.
//   kit::chipFlowBegin("outcome");
//   kit::chipFlowItem(ImGui::GetTextLineHeight());  kit::statusGlyphInline(st);
//   kit::flowChip("Converged", kit::Tone::Ok, true);
//   kit::flowValueChip("Peak", "212 bar", kit::Tone::Warn);
//   kit::chipFlowEnd();
void chipFlowBegin(const char* id);
void chipFlowItem(float width = 0.0f);
void chipFlowEnd();
void flowChip(const char* label, Tone t = Tone::Neutral, bool dot = false,
              const char* tooltip = nullptr);
void flowValueChip(const char* label, const char* value, Tone t = Tone::Neutral,
                   const char* tooltip = nullptr);
bool flowToggleChip(const char* label, bool* on, Tone t = Tone::Accent,
                    const char* tooltip = nullptr);

// Removable chip: the label plus an inline x that removes it, drawn and hit-tested as ONE unit at
// frame height (the x zone is a frame-height square). Returns true on the frame its x is clicked.
// `id` scopes both hit areas (stable regardless of the label); `tooltip` is shown on the x (null =
// "Remove"); a label elided to the line shows in full on hover. `hovered` (optional) receives
// whether the chip is under the cursor this frame (e.g. highlight the face in the viewport).
// Inside chipFlowBegin/End it announces its own flow item (do not call chipFlowItem before it);
// outside a flow it is a normal layout item. Label text after "##" is hidden (ImGui convention).
//   kit::chipFlowBegin("faces");
//   for (int i = 0; i < n; ++i) {
//       bool hov = false;
//       if (kit::removableChip("face", names[i].c_str(), "Remove this face", kit::Tone::Neutral, &hov))
//           removeAt = i;
//       if (hov) hoveredFace = i;
//   }
//   kit::chipFlowEnd();
bool removableChip(const char* id, const char* label, const char* tooltip,
                   Tone t = Tone::Neutral, bool* hovered = nullptr);

// =================================================================================================
// key/value results
// =================================================================================================
// keyValueRow with an explicit id scope: the copy button's ID derives from `id`, not the key, so
// repeated keys ("Type", "Name") in one window never collide. Prefer this over keyValueRow
// whenever copyable is true or the key text is data-derived.
void keyValueRowId(const char* id, const char* key, const char* value,
                   Tone valueTone = Tone::Neutral, bool copyable = false);

struct KeyValueItem {
    std::string key;
    std::string value;
    Tone        tone     = Tone::Neutral;   // e.g. Err for FoS < 1
    bool        copyable = false;
};

// A kit card (title + optional icon, collapsible when `open` is non-null) holding one
// index-keyed keyValueRow per item; an empty list shows a dim "No values reported.".
//   kit::keyValueCard("cyl_result", "Cylinder", "cylinder_feature", {
//       {"Bore", kit::fmtQuantity(bore, "mm")},
//       {"Force margin", "-4 %", kit::Tone::Err}});
void keyValueCard(const char* id, const char* title, const char* icon,
                  const std::vector<KeyValueItem>& rows, bool* open = nullptr);

// =================================================================================================
// tables
// =================================================================================================
enum class TableAlign { Left, Right };
struct TableColumn {
    const char* header = "";
    TableAlign  align  = TableAlign::Left;   // Right for numbers
    float       weight = 1.0f;               // stretch weight
    bool        dim    = false;              // secondary column (units)
};

// Themed READ-ONLY table: kit header (dim caption, hairline under it, no ImGui header fill),
// inner horizontal hairlines, ink-derived colors. `height` > 0 makes the body scroll inside a
// fixed-height region with the pill scrollbar. ALWAYS pair with tableEnd(), even when this
// returns false (clipped) — then skip the rows.
//   if (kit::tableBegin("links", {{"Run"}, {"Flow", kit::TableAlign::Right}, {"Unit", {}, 0.5f, true}})) {
//       for (...) { kit::tableRow(); kit::tableCell(name); kit::tableCell(q.c_str()); kit::tableCell("L/min"); }
//   }
//   kit::tableEnd();
bool tableBegin(const char* id, const std::vector<TableColumn>& columns, float height = 0.0f);
void tableRow();
// Next cell of the current row: aligned per its column, elided to the cell (full text + `tooltip`
// on hover), colored by `tone` (Neutral = primary ink, or dim for dim columns).
void tableCell(const char* text, Tone tone = Tone::Neutral, const char* tooltip = nullptr);
void tableEnd();

struct MetricCell {
    std::string text;
    Tone        tone = Tone::Neutral;
};
struct MetricRow {
    std::string             metric;            // elided with tooltip
    std::string             unit;              // folded into the label: "Final mass (g)"
    std::vector<MetricCell> values;            // one per value header, right-aligned
    const char*             tooltip = nullptr; // hover on the metric name
};

// Results table: Metric | values... (1-3 value columns: "Value", or "Baseline", "Final",
// "Change"). Numbers right-aligned, per-cell tone, kit table look. Fits a ~330 px side pane:
//   - never a Unit column: a row's unit folds into its label ("Final mass (g)");
//   - a value column whose cells are all empty / em dash is dropped (no Baseline without one);
//   - when the metric column would drop below ~7 em, the table STACKS: each metric on its own
//     full-width line with its value columns right-aligned across the full width beneath it
//     (the header row then names only the value columns). Cells elide (tooltip) only if even
//     the stacked value columns cannot hold them.
//   kit::metricTable("topo_metrics", {"Baseline", "Final", "Change"}, {
//       {"Compliance", "N·mm", {{fmtValue(c0)}, {fmtValue(c1)}, kit::deltaCell(c0, c1, false)}}});
void metricTable(const char* id, const std::vector<const char*>& valueHeaders,
                 const std::vector<MetricRow>& rows, const char* metricHeader = "Metric");

// Change cell "+12.3 (+4.1%)" toned Ok when the change is an improvement, Warn when worse.
MetricCell deltaCell(double from, double to, bool higherIsBetter, int sigFigs = 3);

}  // namespace kit
}  // namespace ui
