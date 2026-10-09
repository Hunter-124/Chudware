#pragma once
// kit_form.h — ui::kit form vocabulary: tooltips, number/unit formatting, and ONE field family.
// Included at the end of kit.h; include "kit.h" (not this file) from panels.
//
// ID RULE (non-negotiable)
//   Every interactive primitive here takes an explicit `const char* id` and scopes itself with
//   PushID(id), so two fields with the same visible label ("Name", "Length") can never collide.
//   The id must be stable: a literal ("bore", "netname") or, in a loop, wrap the call in
//   ImGui::PushID(index) / a stable record id. Never pass visible, unit-bearing or data-derived
//   text as the id. Labels are display-only and may change freely.
//
// FIELD LAYOUT
//   Label left / value right, or label stacked above a full-width value:
//
//     inline:   Label text *   [ value ................ ] unit
//               (issue line / disabled reason, full row width from the row's left edge)
//     stacked:  Label text *
//               [ value ................................ ] unit
//               (issue line / disabled reason)
//
//   VALUE COLUMN (one per pane): the value column starts at the same screen x for EVERY field of
//   the pane — every card, every group — at clamp(48% of the pane width, pmLabelCol(),
//   1.6 * pmLabelCol()) from the pane's left content edge. The pane is the nearest window that is
//   not a kit card (panel body page, dialog, popup). fieldValueColumnX() returns it, so bespoke
//   rows can align with fields.
//
//   STACKING RULE (deterministic, decided once per group):
//     inline iff the group's widest label fits left of the value column AND the value keeps
//            max(4 frame heights, the widest value NEED of the group) right of it;
//     otherwise EVERY field of the group stacks.
//   A field's need is what its content requires unclipped: a combo's text + chevron (fieldEnum:
//   the widest item; fieldCombo: the shown text), three vec3 cells of kVecCellSample, two range
//   cells, a slider's readout cell + minimal track. The group measures labels and needs as it
//   draws and decides from LAST frame's measurement; `widestLabelHint` only stands in until the
//   group has measured once (first frame), so an over-long hint never stacks a card whose real
//   labels fit. Wrap each card's fields in fieldGroupBegin/End (or the RAII FieldGroup) — a card
//   never mixes stacked and inline rows. A field outside any group decides alone.
//
//   UNITS: a dim suffix in the pane's UNIT LANE right of the value. The lane is as wide as the
//   widest unit drawn in the pane (floored at "kg/m³"). Every built-in value widget (number,
//   int, vec3, range, slider, enum, combo, text, path) ends at the lane, with or without a unit,
//   so ALL value boxes of a pane share one right edge across cards. Readouts and custom
//   fieldBegin rows reserve it only when they carry a unit. A pane that draws no unit at all has
//   no lane. Never bake a unit into a printf format or a label. Spell units with unitText().
//
//   NUMBERS at rest are right-aligned against the unit with thousands separators ("12,000");
//   while editing they show the plain, parse-safe draft left-aligned.
//
//   COMMIT: every typed field returns true only when the edit is COMMITTED — on deactivation
//   after an edit (Enter, Tab, click-away) for text-mode fields; immediately for checkbox, enum
//   and combo; on release for the slider track (whose *v follows the drag live) and on
//   deactivation for its readout cell. Escape reverts. Numeric fields edit as text; the draft
//   lives in a kit-owned store keyed by the field's ImGuiID, so the buffer is never reseeded
//   mid-edit and callers keep no editingKey/editText state. A focus round trip that leaves the
//   text unchanged never commits (the stored full precision survives the rounded display).
//   Unparseable input shows an Err issue line ('"abc" is not a number.') until the next
//   successful commit.
//
//   ISSUES: FieldOpts::issue draws an inlineStatus under the field, spanning the full row width
//   from the row's left edge (it wraps there instead of squeezing into the value column), and
//   tints the value frame border with issueTone (Err or Warn). Internal parse/clamp notes use the
//   same line. Write issues without the field/card name ("Needs a temperature above absolute
//   zero.") — the line sits directly under the field it describes.
//
//   DISABLED: FieldOpts::disabled dims the row, draws the value frame hollow (outline, no fill)
//   and shows disabledReason as a dim one-line caption under the field (elided, full text on
//   hover) plus a tooltip anywhere on the row — the reason is never tooltip-only.
//
// GLYPHS: unit spellings use only ui::glyphs allow-listed codepoints (° ² ³ · µ are Latin-1 and
// carried by the body font). U+2248 (approx-equal) and U+2212 (minus) are NOT carried, so
// "approximately zero" renders as "~0" and minus as ASCII '-'.
//
// GL/GUI thread only.

#include <cstddef>
#include <string>
#include <vector>

#include "kit.h"

namespace ui {
namespace kit {

// ---- tooltips --------------------------------------------------------------------------------
// The one styled tooltip: glass popup bg, wrapped at a readable measure. No-op on null/empty, so
// an optional reason can be passed straight through. Replaces raw ImGui::SetTooltip.
//   if (ImGui::IsItemHovered()) kit::tooltip("Bore diameter of the barrel.");
void tooltip(const char* text);
// tooltip(text) when the LAST item is hovered (including when it is disabled).
//   kit::secondaryButton("Export"); kit::itemTooltip("Write STL next to the document.");
void itemTooltip(const char* text);

// ---- number formatting -------------------------------------------------------------------------
// fmtValue: readouts. Significant digits (default 4), trailing zeros stripped, thousands
// separators at |v| >= 1e4, engineering prefix for |v| < 1e-4 ("14.37 µ"), "~0" below 1e-15,
// em dash for NaN, "inf" for infinities. Never "0.349999" / "1.43747e-05" / "23.6491".
//   kit::fmtValue(23.6491)   -> "23.65"      kit::fmtValue(123456.7) -> "123,500"
//   kit::fmtValue(0.349999)  -> "0.35"       kit::fmtValue(1.43747e-5) -> "14.37 µ"
std::string fmtValue(double v, int sigFigs = 4);
// fmtPlain: parse-safe and compact (what number fields display, what plot ticks use).
// Significant digits, no separators, no prefixes; e-notation outside [1e-4, 1e15) ("1.437e-5").
std::string fmtPlain(double v, int sigFigs = 4);
// fmtQuantity: value + unit. A tiny value fuses the prefix into a prefixable base unit
// ("14.37 µm", "3.2 µs"); otherwise e-notation ("1.437e-5 MPa"). Unit "°" attaches without a space.
//   kit::fmtQuantity(206.8, kit::unitText(kit::Unit::MPa)) -> "206.8 MPa"
std::string fmtQuantity(double v, const char* unit, int sigFigs = 4);
// fmtQuantityScaled: fmtQuantity after stepping `unit` up its x1000 ladder while the value
// would print >= 10,000: Pa -> kPa -> MPa -> GPa, psi -> ksi -> Msi, N -> kN -> MN, lbf -> kip,
// N·m -> kN·m -> MN·m, W -> kW -> MW -> GW, J -> kJ -> MJ -> GJ, Hz -> kHz -> MHz -> GHz,
// kg -> t, µm -> mm -> m -> km. Other units (and values already < 10,000) format exactly like
// fmtQuantity. Accepts any unitText() key spelling. Opt-in: use it for readouts, never for an
// editable field's unit (that must stay the unit the field converts with).
//   kit::fmtQuantityScaled(10010000, "psi") -> "10.01 Msi"
//   kit::fmtQuantityScaled(40030, "psi")    -> "40.03 ksi"
//   kit::fmtQuantityScaled(206800, "MPa")   -> "206.8 GPa"
//   kit::fmtQuantityScaled(2500, "psi")     -> "2500 psi"
std::string fmtQuantityScaled(double v, const char* unit, int sigFigs = 4);
// fmtDelta: signed change with percent and judgement tone.
//   Tone t; kit::fmtDelta(100, 103.4, "MPa", false, &t) -> "+3.4 MPa (+3.4%)", t = Warn
// Improved (direction matches higherIsBetter) -> Ok, worse -> Warn, negligible -> "±0 unit" Neutral.
// No percent when `from` is 0. NaN/inf in either -> em dash, Neutral.
std::string fmtDelta(double from, double to, const char* unit, bool higherIsBetter,
                     Tone* outTone = nullptr, int sigFigs = 3);
// Lenient parse of user/display text: trims, ignores ',' '_' and spaces; whole string must be a
// finite number. Returns false otherwise.
bool parseNumber(const char* text, double* out);

// ---- units -------------------------------------------------------------------------------------
// ONE spelling per unit. unitText(Unit) is the canonical display string; unitText(key) maps the
// ASCII spellings found in the codebase ("degC", "W/m2", "kg/m3", "mm^2", "W/(m K)", "N m",
// "lpm", ...) to the canonical one and returns `key` unchanged when unknown (null -> "").
enum class Unit {
    None,
    // temperature
    DegC, DegF, K,
    // pressure / stress
    Pa, kPa, MPa, GPa, bar, psi, ksi, Msi,
    // length / area / volume
    um, mm, cm, m, in, ft, mm2, cm2, m2, in2, mm3, cm3, m3, L,
    // flow / velocity / acceleration
    m3_s, m3_h, L_min, L_s, gpm, m_s, mm_s, m_s2, gAccel,
    // mass / density / inertia
    g, kg, lb, g_cm3, kg_m3, lb_in3, kg_m2,
    // thermal
    W_mK, W_m2, W_m2K, J_kgK, perK,
    // fluid
    Pa_s, mPa_s, m2_s, cSt,
    // force / torque / power / energy
    N, kN, lbf, kip, N_m, N_mm, kN_m, lbf_in, W, kW, hp, J, kJ,
    // rotation / frequency / time / angle
    rpm, rad_s, Hz, kHz, MHz, GHz, us, ms, s, minute, hour, deg, rad,
    // electrical
    V, A, Ohm, F,
    // dimensionless
    Percent,
};
const char* unitText(Unit u);
const char* unitText(const char* key);

// ---- field options ----------------------------------------------------------------------------
// One options struct for the whole family; each field reads the members it needs. C++17 has no
// designated initializers, so chain the with*() setters on a temporary:
//   kit::fieldNumber("bore", "Bore", &bore,
//                    kit::FieldOpts().withUnit("mm").withTip("Barrel bore.").withMin(0));
enum class EnumStyle { Auto, Segmented, Combo };

struct FieldOpts {
    // shared
    const char* tip            = nullptr;  // tooltip on the label AND the value
    const char* unit           = nullptr;  // suffix in the unit slot (use unitText())
    const char* hint           = nullptr;  // placeholder when empty (the default/library value)
    bool        required       = false;    // accent '*' after the label
    const char* issue          = nullptr;  // issue line under the field, full row width ("" / null = none)
    Tone        issueTone      = Tone::Err;// Err or Warn: issue line + value border tint
    bool        disabled       = false;
    const char* disabledReason = nullptr;  // dim caption under the field + row tooltip

    // optional value: when non-null and *present == false the field shows empty (hint visible);
    // committing empty text sets *present = false (value untouched) and returns true; committing
    // a value sets *present = true. Number, int, vec3. Without it, empty input is an error.
    bool* present = nullptr;

    // numeric display <-> stored conversion: display = stored * scale + offset, unless the
    // function pair is given (both or neither). min/max clamp is in STORED units (a clamped
    // commit shows a Warn note). sigFigs drives the displayed precision.
    double scale    = 1.0;
    double offset   = 0.0;
    double (*toDisplay)(double stored)  = nullptr;
    double (*toStored)(double display)  = nullptr;
    double minV     = -1.0e300;
    double maxV     = 1.0e300;
    int    sigFigs  = 4;

    // vec3 / range
    bool               commitAsGroup = false;    // vec3: commit once focus leaves all three cells
    const char* const* cellHints     = nullptr;  // empty-cell placeholders: vec3 (3), range {"min","max"}

    // enum / combo
    EnumStyle          enumStyle    = EnumStyle::Auto;
    const bool*        itemDisabled = nullptr;   // per item; disabled items force the combo style
    const char* const* itemReasons  = nullptr;   // per item tooltip shown while disabled
    const char* const* itemTips     = nullptr;   // per item tooltip shown while enabled
    const char*        preview      = nullptr;   // combo closed-state text override

    // text / path
    int         lines       = 1;        // > 1: multiline text box of that many lines
    const char* browseLabel = nullptr;  // path: button text (default "Browse…")

    FieldOpts& withTip(const char* s)               { tip = s; return *this; }
    FieldOpts& withUnit(const char* s)              { unit = s; return *this; }
    FieldOpts& withUnit(Unit u)                     { unit = unitText(u); return *this; }
    FieldOpts& withHint(const char* s)              { hint = s; return *this; }
    FieldOpts& withRequired(bool r = true)          { required = r; return *this; }
    FieldOpts& withIssue(const char* s, Tone t = Tone::Err) { issue = s; issueTone = t; return *this; }
    FieldOpts& withDisabled(bool d, const char* why = nullptr) {
        disabled = d; disabledReason = why; return *this;
    }
    FieldOpts& withPresent(bool* p)                 { present = p; return *this; }
    FieldOpts& withScale(double s, double off = 0.0){ scale = s; offset = off; return *this; }
    FieldOpts& withConversion(double (*toDisp)(double), double (*toStor)(double)) {
        toDisplay = toDisp; toStored = toStor; return *this;
    }
    FieldOpts& withMin(double v)                    { minV = v; return *this; }
    FieldOpts& withMax(double v)                    { maxV = v; return *this; }
    FieldOpts& withRange(double lo, double hi)      { minV = lo; maxV = hi; return *this; }
    FieldOpts& withSigFigs(int n)                   { sigFigs = n; return *this; }
    FieldOpts& withGroupCommit(bool g = true)       { commitAsGroup = g; return *this; }
    FieldOpts& withCellHints(const char* const* h)  { cellHints = h; return *this; }
    FieldOpts& withEnumStyle(EnumStyle s)           { enumStyle = s; return *this; }
    FieldOpts& withItemDisabled(const bool* d, const char* const* reasons) {
        itemDisabled = d; itemReasons = reasons; return *this;
    }
    FieldOpts& withItemTips(const char* const* t)   { itemTips = t; return *this; }
    FieldOpts& withPreview(const char* s)           { preview = s; return *this; }
    FieldOpts& withLines(int n)                     { lines = n; return *this; }
    FieldOpts& withBrowseLabel(const char* s)       { browseLabel = s; return *this; }
};

// ---- field groups -----------------------------------------------------------------------------
// One group per card/section. Pushes `id` as an ID scope (so fields only need card-unique ids)
// and makes the stacked/inline decision once for every field inside (rule in the file header).
//   kit::fieldGroupBegin("cyl", "Working pressure");
//   kit::fieldNumber("bore", "Bore", &bore, kit::FieldOpts().withUnit(kit::Unit::mm));
//   kit::fieldNumber("p", "Working pressure", &p, kit::FieldOpts().withUnit(kit::Unit::bar));
//   kit::fieldGroupEnd();
void fieldGroupBegin(const char* id, const char* widestLabelHint = nullptr);
void fieldGroupEnd();
// Screen x of the value column fields use here (the open group's, else the pane's). For bespoke
// rows (key/value readouts, inline buttons) that must line up with the fields of the pane.
float fieldValueColumnX();
// RAII form:  { kit::FieldGroup g("cyl", "Working pressure"); ...fields... }
struct FieldGroup {
    FieldGroup(const char* id, const char* widestLabelHint = nullptr) {
        fieldGroupBegin(id, widestLabelHint);
    }
    ~FieldGroup() { fieldGroupEnd(); }
    FieldGroup(const FieldGroup&)            = delete;
    FieldGroup& operator=(const FieldGroup&) = delete;
};

// ---- custom value (the layout engine itself) ---------------------------------------------------
// Draws the label (+ required marker, tooltip), positions the cursor at the value column and
// returns the value width (the unit lane is excluded only when o.unit is set; otherwise the row
// spans the full value column). Draw ONE line of bespoke widgets within that width (segmented
// with tips, toggle chips, a face well), then call fieldEnd(), which paints the unit, the issue
// line / disabled caption and pops the scopes. Applies disabled + issue border to the widgets.
//   const float w = kit::fieldBegin("axes", "Symmetry", kit::FieldOpts().withTip("..."));
//   kit::toggleChip("X", &x); ImGui::SameLine(); kit::toggleChip("Y", &y);
//   kit::fieldEnd();
float fieldBegin(const char* id, const char* label, const FieldOpts& o = FieldOpts());
void  fieldEnd();

// Read-only value in the field layout (aligned with editable fields of the same group). The value
// elides with a full-text tooltip; `o.unit` goes in the unit slot.
//   kit::fieldReadout("saved", "Saved as", path.c_str());
void fieldReadout(const char* id, const char* label, const char* value,
                  const FieldOpts& o = FieldOpts(), Tone valueTone = Tone::Neutral);

// ---- typed fields (true = committed) ----------------------------------------------------------
// Number: *v is STORED units (SI); shown through scale/offset or the conversion pair.
//   if (kit::fieldNumber("t_amb", "Ambient", &tK,
//           kit::FieldOpts().withUnit(kit::Unit::DegC).withScale(1.0, -273.15)))
//       persist();
bool fieldNumber(const char* id, const char* label, double* v, const FieldOpts& o = FieldOpts());
bool fieldNumber(const char* id, const char* label, float* v, const FieldOpts& o = FieldOpts());
// Integer: minV/maxV clamp; scale/offset/sigFigs ignored. Rejects fractional input.
//   kit::fieldInt("iters", "Max iterations", &n, kit::FieldOpts().withMin(1));
bool fieldInt(const char* id, const char* label, int* v, const FieldOpts& o = FieldOpts());
// Text: commits on deactivation when the text changed. The char[] overload truncates to n-1.
//   kit::fieldText("name", "Name", nameBuf, sizeof(nameBuf), kit::FieldOpts().withHint("Pump 1"));
bool fieldText(const char* id, const char* label, char* buf, std::size_t n,
               const FieldOpts& o = FieldOpts());
bool fieldText(const char* id, const char* label, std::string* v,
               const FieldOpts& o = FieldOpts());
// Path: text + secondary Browse button. Returns true when the TEXT committed; *browse is set
// true (and only ever set) when Browse was clicked — open your dialog, then assign *v.
//   bool browse = false;
//   kit::fieldPath("stl", "Domain STL", &path, &browse);
//   if (browse) openFileDialog(...);
bool fieldPath(const char* id, const char* label, std::string* v, bool* browse,
               const FieldOpts& o = FieldOpts());
bool fieldPath(const char* id, const char* label, char* buf, std::size_t n, bool* browse,
               const FieldOpts& o = FieldOpts());

// Kit-styled checkbox (promoted InterwebzCheckbox): rounded box, eased accent fill, vector check;
// the box AND the label are one hit target. Returns true when toggled. Inline use (DOF X/Y/Z):
//   kit::checkbox("fx", "X", &fx); ImGui::SameLine(); kit::checkbox("fy", "Y", &fy);
bool checkbox(const char* id, const char* label, bool* v, const char* tip = nullptr,
              bool enabled = true, const char* disabledReason = nullptr);
// Form row form: the checkbox spans the row from its left edge ([x] Label), issue line below.
//   kit::fieldCheckbox("voids", "No enclosed voids", &noVoids, kit::FieldOpts().withTip("..."));
bool fieldCheckbox(const char* id, const char* label, bool* v, const FieldOpts& o = FieldOpts());

// Enum over a fixed label array: Auto = segmented when n <= 4, no item is disabled and every
// label fits its segment; otherwise a combo (disabled items greyed, reason on hover). *sel < 0
// shows o.hint in the combo. Returns true when the selection changed.
//   static const char* const kOrder[] = {"Linear", "Quadratic"};
//   kit::fieldEnum("order", "Element order", &order, kOrder, 2);
bool fieldEnum(const char* id, const char* label, int* sel, const char* const* items, int n,
               const FieldOpts& o = FieldOpts());
// Combo over arbitrary (data-derived, possibly duplicate) strings; every item is PushID(i)'d.
// o.preview overrides the closed text (e.g. a short name while the list shows descriptions).
//   kit::fieldCombo("comp", "Component", &idx, names, kit::FieldOpts().withPreview(shortName));
bool fieldCombo(const char* id, const char* label, int* sel, const std::vector<std::string>& items,
                const FieldOpts& o = FieldOpts());
// Popup geometry for the NEXT ImGui::BeginCombo whose frame starts at screen x `frameX` and is
// `frameW` wide: at least frame-wide, never wider than the room from the frame's LEFT edge to the
// viewport work area's right edge, at most `maxItems` rows tall. With the width bounded that way
// ImGui's combo placement always hangs the popup from the frame's left edge instead of flipping it
// to "below, toward left" across the neighbouring pane. Kit combos (fieldEnum/fieldCombo) already
// call it; a raw BeginCombo calls it immediately before BeginCombo. Popup content must size from
// the popup itself (Selectable with zero width, GetContentRegionAvail) — never from
// GetWindowWidth(), which feeds back into the auto-sized popup and widens it every frame.
//   kit::constrainNextComboPopup(ImGui::GetCursorScreenPos().x, w);
//   if (ImGui::BeginCombo("##mode", preview)) { ... ImGui::EndCombo(); }
void constrainNextComboPopup(float frameX, float frameW, int maxItems = 20);

// X/Y/Z cells sharing one unit; each cell shows its axis letter (axis-tinted) inside the frame,
// so cellHints are only placeholders for empty cells. Each cell keeps room for "-0,000.0"; when
// the inline column cannot hold three, the group stacks (label above a full-width row of three).
// Per-cell commit, or whole-vector commit with o.commitAsGroup (a direction you normalise).
//   kit::fieldVec3("force", "Force", f, kit::FieldOpts().withUnit(kit::Unit::N));
bool fieldVec3(const char* id, const char* label, double v[3], const FieldOpts& o = FieldOpts());
bool fieldVec3(const char* id, const char* label, float v[3], const FieldOpts& o = FieldOpts());
// Min/max pair; a commit that would make lo > hi is rejected with an issue line.
//   kit::fieldRange("band", "Temperature band", &lo, &hi, kit::FieldOpts().withUnit("°C"));
bool fieldRange(const char* id, const char* label, double* lo, double* hi,
                const FieldOpts& o = FieldOpts());
bool fieldRange(const char* id, const char* label, float* lo, float* hi,
                const FieldOpts& o = FieldOpts());
// Slider for BOUNDED RATIOS only (%, 0-1, fineness): a thin track + accent fill + round thumb,
// then an editable readout cell (right-aligned value) before the unit — the value is never
// under the thumb. lo/hi are stored units; dragging/clicking the track moves *v live and returns
// true once on release; typing a value in the cell commits on deactivation (clamped to lo..hi).
//   kit::fieldSlider("vf", "Volume fraction", &vf, 0.05, 0.95,
//                    kit::FieldOpts().withScale(100).withUnit(kit::Unit::Percent));
bool fieldSlider(const char* id, const char* label, double* v, double lo, double hi,
                 const FieldOpts& o = FieldOpts());
bool fieldSlider(const char* id, const char* label, float* v, float lo, float hi,
                 const FieldOpts& o = FieldOpts());

}  // namespace kit
}  // namespace ui
