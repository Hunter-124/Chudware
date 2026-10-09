// kit_form.cpp — the ONE implementation of the form vocabulary declared in kit_form.h:
// kit::tooltip, the number/unit formatters, and the field family with its layout engine.
//
// It absorbs (and retires) the per-panel field helpers: hydraulic_panel fieldRow/ValueColumn/
// numField/intField/comboField/pickField/textField, piping_panel numField/intField/comboField/
// vec3Field, sim_topology fieldLabel/numberField/boolField/directionField + parseReal/fmtG +
// SettingsView edit plumbing, sim_topology_bc numberField/vectorField/textField (and its unit
// width bug: the unit slot here reserves unitW + ItemSpacing), and InterwebzCheckbox's look.
//
// House rules (kit.cpp): palette roles only, metrics/density sizes only, motion through ui::anim.
// GL/GUI thread only.
#include "kit.h"

#include <cerrno>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <climits>
#include <cstring>
#include <string>
#include <unordered_map>
#include <vector>
#include <utility>

#include "imgui.h"
#include "imgui_internal.h"

#include "../anim.h"
#include "../metrics.h"

namespace ui {
namespace kit {
namespace {

// ---- ratios (never sizes) -----------------------------------------------------------------------
constexpr float kTooltipWrapEm   = 26.0f;  // same measure as kit.cpp's tooltips
constexpr float kPaneLabelFrac   = 0.48f;  // value column starts this far across the pane...
constexpr float kPaneLabelMaxMul = 1.6f;   // ...but never more than 1.6 pmLabelCol() in
constexpr float kMinValueFrames  = 4.0f;   // inline needs >= 4 frame heights of value width
constexpr float kLabelInkA       = 0.85f;  // field label = current ink, slightly secondary
constexpr float kUnitInkA        = 0.62f;  // unit suffix / disabled caption = dim ink
constexpr float kDisabledA       = 0.55f;
constexpr float kDisabledFrameA  = 0.35f;  // disabled value frame: hollow (bg mostly gone)
constexpr float kSettled         = 0.004f;
constexpr float kCheckRound      = 4.0f;   // logical px, through metrics::rounding
constexpr float kThumbEdgeA      = 0.80f;  // slider thumb rim: accentDim at this alpha
constexpr float kCheckHoverEdge  = 0.50f;  // unchecked box edge eases this far to accent on hover
constexpr float kPressedLift     = 0.35f;  // controlFace lift of an open combo / dragged thumb
constexpr float kCheckInsetFrac  = 0.22f;  // check mark inset inside the box
constexpr float kChevronFrac     = 0.16f;  // combo chevron half-width as a fraction of frame h
constexpr float kThumbFrac       = 0.28f;  // slider thumb radius as a fraction of frame h
constexpr float kThumbHaloMul    = 1.6f;   // hover halo radius / thumb radius
constexpr float kTrackU          = 1.0f;   // slider track thickness in grid units
constexpr float kSliderCellMinFr = 1.6f;   // slider readout cell >= this many frame heights
constexpr float kSliderTrackMinFr= 2.0f;   // slider track >= this many frame heights inline
constexpr int   kGcFrames        = 600;    // edit state unseen this long is dropped
constexpr int   kGroupCommitIdle = 2;      // frames with no active cell before a group commits
const char* const kVecCellSample = "-0,000.0";            // a vec3 cell shows this unclipped
const char* const kLaneSample    = "kg/m\xC2\xB3";        // minimum width of the unit lane

// ---- palette plumbing ---------------------------------------------------------------------------
inline ImU32 roleU32(const float c[4], float alphaMul = 1.0f) {
    float t[4];
    Palette::withAlpha(c, t, alphaMul);
    return (ImU32)Palette::toU32(t);
}
inline ImVec4 roleVec(const float c[4], float alphaMul = 1.0f) {
    return ImVec4(c[0], c[1], c[2], c[3] * alphaMul);
}
inline ImVec4 lerpVec4(const ImVec4& a, const ImVec4& b, float t) {
    return ImVec4(a.x + (b.x - a.x) * t, a.y + (b.y - a.y) * t, a.z + (b.z - a.z) * t,
                  a.w + (b.w - a.w) * t);
}
// Current ink at `alphaMul`, honouring style.Alpha (so BeginDisabled dims draw-list text too).
inline ImU32 inkU32(float alphaMul = 1.0f) { return ImGui::GetColorU32(ImGuiCol_Text, alphaMul); }

// ---- string helpers -----------------------------------------------------------------------------
std::string trimmed(const std::string& s) {
    const std::size_t b = s.find_first_not_of(" \t");
    if (b == std::string::npos) return std::string();
    const std::size_t e = s.find_last_not_of(" \t");
    return s.substr(b, e - b + 1);
}

// std::string-backed InputText (the imgui_stdlib resize pattern).
int resizeCallback(ImGuiInputTextCallbackData* d) {
    if (d->EventFlag == ImGuiInputTextFlags_CallbackResize) {
        auto* s = static_cast<std::string*>(d->UserData);
        s->resize((std::size_t)d->BufTextLen);
        d->Buf = s->data();
    }
    return 0;
}
bool inputString(const char* cid, const char* hint, std::string* s, ImGuiInputTextFlags flags) {
    return ImGui::InputTextWithHint(cid, hint ? hint : "", s->data(), s->capacity() + 1,
                                    flags | ImGuiInputTextFlags_CallbackResize, resizeCallback, s);
}
bool inputStringMultiline(const char* cid, std::string* s, const ImVec2& size) {
    return ImGui::InputTextMultiline(cid, s->data(), s->capacity() + 1, size,
                                     ImGuiInputTextFlags_CallbackResize, resizeCallback, s);
}

// ---- number formatting core ---------------------------------------------------------------------
double roundSig(double v, int sig) {
    if (v == 0.0 || !std::isfinite(v) || std::fabs(v) < 1e-290) return v;
    const double e = std::floor(std::log10(std::fabs(v)));
    const double p = std::pow(10.0, (double)sig - 1.0 - e);
    if (!std::isfinite(p) || p == 0.0) return v;
    return std::round(v * p) / p;
}

void stripZeros(char* b) {
    if (!std::strchr(b, '.')) return;
    std::size_t n = std::strlen(b);
    while (n > 0 && b[n - 1] == '0') b[--n] = '\0';
    if (n > 0 && b[n - 1] == '.') b[--n] = '\0';
}

std::string noNegZero(const char* b) {
    if (std::strcmp(b, "-0") == 0) return std::string("0");
    return std::string(b);
}

// Fixed notation with `sig` significant digits; caller guarantees 1e-4 <= |rounded| < 1e15.
std::string fixedSig(double v, int sig) {
    const double r   = roundSig(v, sig);
    const int    e   = (int)std::floor(std::log10(std::fabs(r)));
    const int    dec = ImMax(0, sig - 1 - e);
    char b[64];
    std::snprintf(b, sizeof(b), "%.*f", dec, r);
    stripZeros(b);
    return noNegZero(b);
}

// Compact e-notation: "1.437e-5", "2.5e18".
std::string expSig(double v, int sig) {
    char b[64];
    std::snprintf(b, sizeof(b), "%.*e", ImMax(0, sig - 1), v);
    char* e = std::strchr(b, 'e');
    if (!e) return std::string(b);
    const int exp = std::atoi(e + 1);
    *e = '\0';
    stripZeros(b);
    char out[80];
    std::snprintf(out, sizeof(out), "%se%d", b, exp);
    return std::string(out);
}

int clampSig(int sig) { return ImClamp(sig, 1, 15); }

// SI prefix for an exponent that is a multiple of 3 in [-15, -3].
const char* siPrefix(int e3) {
    switch (e3) {
        case -3:  return "m";
        case -6:  return "\xC2\xB5";   // µ U+00B5 (Latin-1, in the atlas)
        case -9:  return "n";
        case -12: return "p";
        case -15: return "f";
        default:  return nullptr;
    }
}

// Engineering split of a tiny value: mantissa text + prefix. False if out of prefix range.
bool engParts(double v, int sig, std::string* mant, const char** prefix) {
    const double a  = std::fabs(v);
    const int    e  = (int)std::floor(std::log10(a));
    int          e3 = (int)std::floor((double)e / 3.0) * 3;
    double       m  = v / std::pow(10.0, (double)e3);
    if (std::fabs(roundSig(m, sig)) >= 1000.0) {
        e3 += 3;
        m /= 1000.0;
    }
    const char* p = siPrefix(e3);
    if (!p) return false;
    *mant   = fmtPlain(m, sig);
    *prefix = p;
    return true;
}

void insertThousands(std::string* s) {
    std::size_t start = (!s->empty() && ((*s)[0] == '-' || (*s)[0] == '+')) ? 1 : 0;
    std::size_t end   = s->find('.');
    if (end == std::string::npos) end = s->size();
    for (std::size_t i = end; i > start + 3; i -= 3) s->insert(i - 3, 1, ',');
}

bool prefixableUnit(const char* u) {
    static const char* const kBase[] = {"m", "s", "g", "Pa", "N", "W", "J", "Hz", "V", "A", "F",
                                        "L"};
    for (const char* b : kBase)
        if (std::strcmp(u, b) == 0) return true;
    return false;
}

const char* kEmDash = "\xE2\x80\x94";   // U+2014

// ---- units --------------------------------------------------------------------------------------
// text = canonical display spelling; aliases = '|'-separated ASCII spellings seen in the code.
// Every escape is closed by a string split so a following hex-looking letter is never absorbed.
struct UnitRow {
    Unit        u;
    const char* text;
    const char* aliases;
};
const UnitRow kUnits[] = {
    {Unit::None, "", ""},
    {Unit::DegC, "\xC2\xB0" "C", "degC|deg C|C|oC|celsius"},
    {Unit::DegF, "\xC2\xB0" "F", "degF|deg F|oF|fahrenheit"},
    {Unit::K, "K", "kelvin"},
    {Unit::Pa, "Pa", "pa"},
    {Unit::kPa, "kPa", "kpa"},
    {Unit::MPa, "MPa", "mpa|N/mm2|N/mm^2"},
    {Unit::GPa, "GPa", "gpa"},
    {Unit::bar, "bar", "bars"},
    {Unit::psi, "psi", "PSI"},
    {Unit::ksi, "ksi", "KSI|kpsi"},
    {Unit::Msi, "Msi", "MSI|Mpsi"},
    {Unit::um, "\xC2\xB5" "m", "um|micron|microns"},
    {Unit::mm, "mm", ""},
    {Unit::cm, "cm", ""},
    {Unit::m, "m", ""},
    {Unit::in, "in", "inch|inches|in."},
    {Unit::ft, "ft", "feet"},
    {Unit::mm2, "mm\xC2\xB2", "mm2|mm^2|sq mm"},
    {Unit::cm2, "cm\xC2\xB2", "cm2|cm^2"},
    {Unit::m2, "m\xC2\xB2", "m2|m^2"},
    {Unit::in2, "in\xC2\xB2", "in2|in^2|sq in"},
    {Unit::mm3, "mm\xC2\xB3", "mm3|mm^3"},
    {Unit::cm3, "cm\xC2\xB3", "cm3|cm^3|cc"},
    {Unit::m3, "m\xC2\xB3", "m3|m^3"},
    {Unit::L, "L", "l|liter|litre|liters|litres"},
    {Unit::m3_s, "m\xC2\xB3" "/s", "m3/s|m^3/s"},
    {Unit::m3_h, "m\xC2\xB3" "/h", "m3/h|m^3/h|m3/hr"},
    {Unit::L_min, "L/min", "l/min|lpm|LPM|L/m"},
    {Unit::L_s, "L/s", "l/s"},
    {Unit::gpm, "gpm", "GPM"},
    {Unit::m_s, "m/s", ""},
    {Unit::mm_s, "mm/s", ""},
    {Unit::m_s2, "m/s\xC2\xB2", "m/s2|m/s^2"},
    {Unit::gAccel, "g", "g0|gn"},
    {Unit::g, "g", "gram|grams"},
    {Unit::kg, "kg", ""},
    {Unit::lb, "lb", "lbm|lbs"},
    {Unit::g_cm3, "g/cm\xC2\xB3", "g/cm3|g/cm^3|g/cc"},
    {Unit::kg_m3, "kg/m\xC2\xB3", "kg/m3|kg/m^3"},
    {Unit::lb_in3, "lb/in\xC2\xB3", "lb/in3|lb/in^3|lbm/in3"},
    {Unit::kg_m2, "kg\xC2\xB7" "m\xC2\xB2", "kg m2|kg*m2|kg-m2|kgm2|kg m^2|kg*m^2"},
    {Unit::W_mK, "W/(m\xC2\xB7" "K)", "W/(m K)|W/(m*K)|W/(m-K)|W/mK|W/m-K|W/m/K|W/m.K"},
    {Unit::W_m2, "W/m\xC2\xB2", "W/m2|W/m^2"},
    {Unit::W_m2K, "W/(m\xC2\xB2\xC2\xB7" "K)",
     "W/m2K|W/(m2 K)|W/(m2*K)|W/(m2-K)|W/m^2K|W/(m^2 K)|W/m2/K|W/m2-K"},
    {Unit::J_kgK, "J/(kg\xC2\xB7" "K)", "J/kgK|J/(kg K)|J/(kg*K)|J/(kg-K)|J/kg/K|J/kg-K"},
    {Unit::perK, "1/K", "/K|K^-1"},
    {Unit::Pa_s, "Pa\xC2\xB7" "s", "Pa s|Pa*s|Pa-s|Pas|Pa.s"},
    {Unit::mPa_s, "mPa\xC2\xB7" "s", "mPa s|mPa*s|mPa-s|cP|cp"},
    {Unit::m2_s, "m\xC2\xB2" "/s", "m2/s|m^2/s"},
    {Unit::cSt, "cSt", "cst|mm2/s|mm^2/s"},
    {Unit::N, "N", ""},
    {Unit::kN, "kN", "kn"},
    {Unit::lbf, "lbf", ""},
    {Unit::kip, "kip", "kips"},
    {Unit::N_m, "N\xC2\xB7" "m", "N m|Nm|N*m|N-m|N.m"},
    {Unit::N_mm, "N\xC2\xB7" "mm", "N mm|Nmm|N*mm|N-mm"},
    {Unit::kN_m, "kN\xC2\xB7" "m", "kN m|kNm|kN*m|kN-m"},
    {Unit::lbf_in, "lbf\xC2\xB7" "in", "lbf in|lbf-in|lbf*in|in-lbf|in*lbf|in lbf"},
    {Unit::W, "W", ""},
    {Unit::kW, "kW", "kw"},
    {Unit::hp, "hp", "HP"},
    {Unit::J, "J", ""},
    {Unit::kJ, "kJ", "kj"},
    {Unit::rpm, "rpm", "RPM|rev/min"},
    {Unit::rad_s, "rad/s", ""},
    {Unit::Hz, "Hz", "hz"},
    {Unit::kHz, "kHz", "khz"},
    {Unit::MHz, "MHz", "mhz"},
    {Unit::GHz, "GHz", "ghz"},
    {Unit::us, "\xC2\xB5" "s", "us|usec"},
    {Unit::ms, "ms", "msec"},
    {Unit::s, "s", "sec|secs"},
    {Unit::minute, "min", "mins"},
    {Unit::hour, "h", "hr|hrs"},
    {Unit::deg, "\xC2\xB0", "deg|degree|degrees"},
    {Unit::rad, "rad", ""},
    {Unit::V, "V", ""},
    {Unit::A, "A", ""},
    {Unit::Ohm, "Ohm", "ohm|ohms|Ohms"},
    {Unit::F, "F", ""},
    {Unit::Percent, "%", "pct|percent"},
};

bool aliasMatch(const char* aliases, const char* key) {
    const std::size_t kn = std::strlen(key);
    const char*       p  = aliases;
    while (*p) {
        const char*       bar = std::strchr(p, '|');
        const std::size_t n   = bar ? (std::size_t)(bar - p) : std::strlen(p);
        if (n == kn && std::strncmp(p, key, n) == 0) return true;
        if (!bar) break;
        p = bar + 1;
    }
    return false;
}

// ---- edit-state store ---------------------------------------------------------------------------
// One entry per text-mode cell (draft text + the text it was seeded with) and per field (issue
// note, vec3 group draft). Keyed by ImGuiID; entries unseen for kGcFrames are dropped.
struct EditState {
    std::string text;
    std::string seed;
    std::string error;
    Tone        errTone      = Tone::Err;
    double      draft[3]     = {0.0, 0.0, 0.0};
    bool        groupEditing = false;
    bool        groupDirty   = false;
    bool        dragDirty    = false;   // slider: the value moved during the current drag
    int         idleFrames   = 0;
    int         frame        = 0;
};
std::unordered_map<ImGuiID, EditState>& states() {
    static std::unordered_map<ImGuiID, EditState> s;
    return s;
}
EditState& stateFor(ImGuiID id) {
    auto&      m   = states();
    const int  now = ImGui::GetFrameCount();
    static int lastGc = 0;
    if (now - lastGc > kGcFrames) {
        lastGc = now;
        for (auto it = m.begin(); it != m.end();) {
            if (now - it->second.frame > kGcFrames && !it->second.groupEditing) it = m.erase(it);
            else ++it;
        }
    }
    EditState& s = m[id];
    s.frame      = now;
    return s;
}
void setNote(EditState& s, std::string msg, Tone t) {
    s.error   = std::move(msg);
    s.errTone = t;
}

// ---- layout engine ------------------------------------------------------------------------------
// The PANE is the nearest window that is not a kit card body: the panel body page, a dialog, a
// popup. Cards are child windows (kit.cpp cardBegin -> BeginChild("kit_card_body")), so walking
// past them is what makes every card of a pane share one value column and one unit lane.
bool isCardBody(const ImGuiWindow* w) {
    if (!w || !w->ParentWindow || !(w->Flags & ImGuiWindowFlags_ChildWindow) || !w->Name) return false;
    const char* slash = std::strrchr(w->Name, '/');
    static const char kCard[] = "kit_card_body_";
    return slash && std::strncmp(slash + 1, kCard, sizeof(kCard) - 1) == 0;
}
ImGuiWindow* paneWindow() {
    ImGuiWindow* w = ImGui::GetCurrentWindow();
    while (isCardBody(w)) w = w->ParentWindow;
    return w;
}
// Screen x of the pane's value column: a pmLabelCol()-floored fraction of the pane width, so it
// is identical for every card and field of the pane (and only moves when the pane is resized).
float paneValueX(const ImGuiWindow* pane) {
    const float base = ui::metrics::pmLabelCol();
    const float col  = ImClamp(pane->Size.x * kPaneLabelFrac, base, base * kPaneLabelMaxMul);
    return pane->WorkRect.Min.x + col;
}
// Pane-wide unit lane: the widest unit any field of the pane drew LAST frame (floored at
// kLaneSample so ordinary units never move the column), never narrower than `ownUnitW`. Records
// `ownUnitW` for the next frame. 0 when the pane draws no units at all.
float paneLane(ImGuiWindow* pane, float ownUnitW) {
    ImGuiStorage& st     = pane->StateStorage;
    const ImGuiID kFrame = ImHashStr("##kit_form_lane_frame", 0, pane->ID);
    const ImGuiID kCur   = ImHashStr("##kit_form_lane_cur", 0, pane->ID);
    const ImGuiID kLast  = ImHashStr("##kit_form_lane_last", 0, pane->ID);
    const int     now    = ImGui::GetFrameCount();
    if (st.GetInt(kFrame, -1) != now) {
        st.SetFloat(kLast, st.GetFloat(kCur, 0.0f));
        st.SetFloat(kCur, 0.0f);
        st.SetInt(kFrame, now);
    }
    if (ownUnitW > st.GetFloat(kCur, 0.0f)) st.SetFloat(kCur, ownUnitW);
    const float lane = ImMax(st.GetFloat(kLast, 0.0f), ownUnitW);
    return lane > 0.0f ? ImMax(lane, ImGui::CalcTextSize(kLaneSample).x) : 0.0f;
}

struct GroupFrame {
    ImGuiStorage* storage   = nullptr;
    ImGuiID       keyLabel  = 0;
    ImGuiID       keyNeed   = 0;
    ImGuiID       keySeen   = 0;
    float         valueX    = 0.0f;
    bool          stacked   = false;
    float         measLabel = 0.0f;   // widest label this frame
    float         measNeed  = 0.0f;   // widest value need (incl. unit slot) this frame
};
std::vector<GroupFrame> g_groups;

struct FieldFrame {
    ImGuiID     stateId   = 0;
    float       rowLeft   = 0.0f;
    float       rowTop    = 0.0f;
    float       rowRight  = 0.0f;
    float       valueX    = 0.0f;
    float       valueY    = 0.0f;
    float       valueW    = 0.0f;
    float       slotX     = 0.0f;
    bool        stacked   = false;
    const char* unit      = nullptr;
    const char* issue     = nullptr;
    Tone        issueTone = Tone::Err;
    bool        disabled  = false;
    const char* disabledReason = nullptr;
    const char* tip       = nullptr;
    int         vars      = 0;
    int         cols      = 0;
};
std::vector<FieldFrame> g_fields;

// What a field needs from the layout beyond its label.
struct FieldKind {
    bool  lane = false;      // ends at the pane unit lane even without a unit (every built-in
                             // value widget does, so a card's boxes share one right edge)
    float need = 0.0f;       // value width its content needs unclipped (combo text, 3 vec cells)
};

// The stacking rule (documented in kit_form.h): inline iff the label fits left of the pane value
// column and the value keeps max(4 frame heights, its need) right of it.
bool fitsInline(float labelW, float need, float rowLeft, float rowRight, float valueX) {
    const float sp = ImGui::GetStyle().ItemSpacing.x;
    const float minValue = ImMax(ImGui::GetFrameHeight() * kMinValueFrames, need);
    return labelW + sp <= valueX - rowLeft && rowRight - valueX >= minValue;
}

struct Layout {
    float valueX  = 0.0f;    // screen x of the value column (inline rows)
    bool  stacked = false;
};
Layout resolve(float ownLabelW, float ownNeed, float rowLeft, float rowRight, ImGuiWindow* pane) {
    Layout L;
    if (!g_groups.empty()) {
        GroupFrame& g = g_groups.back();
        g.measLabel   = ImMax(g.measLabel, ownLabelW);
        g.measNeed    = ImMax(g.measNeed, ownNeed);
        L.valueX      = g.valueX;
        L.stacked     = g.stacked;
    } else {
        L.valueX  = paneValueX(pane);
        L.stacked = !fitsInline(ownLabelW, ownNeed, rowLeft, rowRight, L.valueX);
    }
    return L;
}

EditState& fieldState() { return stateFor(g_fields.back().stateId); }

// Value tooltip: the field tip while enabled (the disabled reason is a row tooltip in fieldEnd).
void valueTip(const FieldOpts& o) {
    if (!o.disabled) itemTooltip(o.tip);
}

double toDisp(double stored, const FieldOpts& o) {
    if (o.toDisplay) return o.toDisplay(stored);
    return stored * o.scale + o.offset;
}
double toStor(double display, const FieldOpts& o) {
    if (o.toStored) return o.toStored(display);
    return o.scale != 0.0 ? (display - o.offset) / o.scale : display;
}

std::string quoted(const std::string& s) { return "\"" + s + "\""; }

// Clamp a stored candidate into [minV, maxV]; a clamp leaves a Warn note naming the limit.
double clampNoted(double v, const FieldOpts& o, EditState& fs) {
    if (v < o.minV || v > o.maxV) {
        const bool   low = v < o.minV;
        const double lim = low ? o.minV : o.maxV;
        std::string  msg = std::string(low ? "Raised to the minimum, " : "Limited to the maximum, ") +
                          fmtQuantity(toDisp(lim, o), o.unit, o.sigFigs) + ".";
        setNote(fs, std::move(msg), Tone::Warn);
        return lim;
    }
    fs.error.clear();
    return v;
}

// ---- text-mode numeric cell ------------------------------------------------------------------------
enum class Cell { None, Value, Empty, Invalid };

std::string intText(double v) {
    char b[48];
    std::snprintf(b, sizeof(b), "%lld", (long long)std::llround(v));
    return std::string(b);
}

// Resting display of a number cell: thousands-grouped ("12,000") when that stays fixed notation.
// The edit draft stays ungrouped; parseNumber ignores ',' either way.
std::string groupedNumber(double v, bool integer, int sig) {
    std::string s = integer ? intText(v) : fmtPlain(v, sig);
    if (s.find('e') == std::string::npos && std::fabs(v) >= 1e4) insertThousands(&s);
    return s;
}

// Focus ring: while a value frame is being edited its soft edge eases to the accent — the one
// "you are typing here" cue (ImGui's own frame only nudges its fill). Call right after the input.
void focusRing() {
    const float t = anim::toward(ImHashStr("##kit_focus", 0, ImGui::GetItemID()),
                                 ImGui::IsItemActive() ? 1.0f : 0.0f, anim::kFast);
    if (t <= kSettled) return;
    ImGui::GetWindowDrawList()->AddRect(ImGui::GetItemRectMin(), ImGui::GetItemRectMax(),
                                        roleU32(palette.accent, t * ImGui::GetStyle().Alpha),
                                        ImGui::GetStyle().FrameRounding, 0,
                                        ui::metrics::stroke(1.0f));
}

// One InputText bound to a display value. Not active: reseeded from the value every frame and
// painted RIGHT-aligned (grouped) against the unit; active: left-aligned plain draft that
// persists in the store. Deactivated with changed text: parsed and reported. `prefix` (vec3 axis
// letter) sits inside the frame's left padding in `prefixRole`.
Cell numberCell(const char* cid, const char* hint, double disp, bool has, bool integer, int sig,
                float width, double* out, std::string* bad, const char* prefix = nullptr,
                const float* prefixRole = nullptr) {
    const ImGuiID gid    = ImGui::GetID(cid);
    EditState&    s      = stateFor(gid);
    const bool    active = ImGui::GetActiveID() == gid;
    if (!active) {
        if (has && std::isfinite(disp)) s.text = integer ? intText(disp) : fmtPlain(disp, sig);
        else                            s.text.clear();
        s.seed = s.text;
    }
    const ImGuiStyle& st    = ImGui::GetStyle();
    const ImVec2      pad   = st.FramePadding;
    const float       preW  = (prefix && prefix[0])
                                  ? ImGui::CalcTextSize(prefix).x + st.ItemInnerSpacing.x
                                  : 0.0f;
    const bool        paint = !active && !s.text.empty();
    if (preW > 0.0f) ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(pad.x + preW, pad.y));
    if (paint) ImGui::PushStyleColor(ImGuiCol_Text, roleVec(palette.text, 0.0f));
    ImGui::SetNextItemWidth(width);
    const ImGuiInputTextFlags flags =
        (integer ? ImGuiInputTextFlags_CharsDecimal : ImGuiInputTextFlags_CharsScientific) |
        ImGuiInputTextFlags_AutoSelectAll;
    inputString(cid, hint, &s.text, flags);
    if (paint) ImGui::PopStyleColor();
    if (preW > 0.0f) ImGui::PopStyleVar();
    focusRing();

    const ImVec2 a  = ImGui::GetItemRectMin();
    const ImVec2 b  = ImGui::GetItemRectMax();
    ImDrawList*  dl = ImGui::GetWindowDrawList();
    if (preW > 0.0f) {
        dl->AddText(ImVec2(a.x + pad.x, a.y + pad.y),
                    prefixRole ? roleU32(prefixRole, st.Alpha) : inkU32(kUnitInkA), prefix);
    }
    if (paint) {
        const float inL = a.x + pad.x + preW;
        const float inR = b.x - pad.x;
        std::string shown = groupedNumber(disp, integer, sig);
        float       tw    = ImGui::CalcTextSize(shown.c_str()).x;
        if (tw > inR - inL) {   // grouping does not fit: the compact draft text instead
            shown = s.text;
            tw    = ImGui::CalcTextSize(shown.c_str()).x;
        }
        const float x = tw <= inR - inL ? inR - tw : inL;
        dl->PushClipRect(ImVec2(inL, a.y), ImVec2(ImMax(inR, inL), b.y), true);
        dl->AddText(ImVec2(x, a.y + pad.y), inkU32(), shown.c_str());
        dl->PopClipRect();
    }
    if (!ImGui::IsItemDeactivated()) return Cell::None;

    const std::string t = trimmed(s.text);
    if (t == trimmed(s.seed)) return Cell::None;   // focus round trip: keep full precision
    if (t.empty()) return Cell::Empty;
    double v = 0.0;
    if (!parseNumber(t.c_str(), &v) || (integer && v != std::floor(v))) {
        *bad = t;
        return Cell::Invalid;
    }
    *out = v;
    return Cell::Value;
}

const float* axisRole(int axis) {
    return axis == 0 ? palette.axisX : axis == 1 ? palette.axisY : palette.axisZ;
}

// ---- text cell (string draft) ---------------------------------------------------------------------
// Returns true when the text committed; the committed text is the state's draft.
bool textCell(const char* cid, const char* cur, float width, const FieldOpts& o,
              const std::string** committed) {
    const ImGuiID gid = ImGui::GetID(cid);
    EditState&    s   = stateFor(gid);
    if (ImGui::GetActiveID() != gid) s.text.assign(cur ? cur : "");
    if (o.lines > 1) {
        const ImGuiStyle& st = ImGui::GetStyle();
        const float h = ImGui::GetTextLineHeight() * (float)o.lines + st.FramePadding.y * 2.0f;
        inputStringMultiline(cid, &s.text, ImVec2(width, h));
    } else {
        ImGui::SetNextItemWidth(width);
        inputString(cid, o.hint, &s.text, 0);
    }
    focusRing();
    valueTip(o);
    if (!ImGui::IsItemDeactivated()) return false;
    if (std::strcmp(s.text.c_str(), cur ? cur : "") == 0) return false;
    *committed = &s.text;
    return true;
}

void copyToBuf(const std::string& s, char* buf, std::size_t n) {
    if (!buf || n == 0) return;
    const std::size_t k = ImMin(s.size(), n - 1);
    std::memcpy(buf, s.data(), k);
    buf[k] = '\0';
}

// ---- combo core -----------------------------------------------------------------------------------
using ItemGetter = const char* (*)(const void* ctx, int i);

const char* arrayItem(const void* ctx, int i) {
    const char* s = static_cast<const char* const*>(ctx)[i];
    return s ? s : "";
}
const char* vectorItem(const void* ctx, int i) {
    return (*static_cast<const std::vector<std::string>*>(ctx))[(std::size_t)i].c_str();
}

void paintChevronDown(ImDrawList* dl, ImVec2 c, float r, ImU32 col) {
    const ImVec2 pts[3] = {ImVec2(c.x - r, c.y - r * 0.5f), ImVec2(c.x, c.y + r * 0.5f),
                           ImVec2(c.x + r, c.y - r * 0.5f)};
    dl->AddPolyline(pts, 3, col, ImDrawFlags_None, ui::metrics::stroke(1.5f));
}

// Reused splitter for the combo popup's item rows: rounded highlight pills go on channel 0 under
// the Selectable labels on channel 1. Combos never nest their popups, so one instance suffices.
ImDrawListSplitter& comboSplitter() {
    static ImDrawListSplitter s;
    return s;
}

// Kit combo: own preview (elided, placeholder dim) + vector chevron on a raised control face,
// PushID(i) per item, disabled items greyed with their reason on hover. The popup hangs from the
// frame's LEFT edge at least frame-wide (constrainNextComboPopup), with rounded highlight pills.
// Returns true when *sel changed.
bool comboCore(const char* cid, int* sel, int n, ItemGetter get, const void* ctx, float w,
               const FieldOpts& o) {
    ImGuiContext&     g  = *GImGui;
    ImDrawList*       dl = ImGui::GetWindowDrawList();
    const ImGuiStyle& st = ImGui::GetStyle();
    const ImVec2      p0 = ImGui::GetCursorScreenPos();
    const float       h  = ImGui::GetFrameHeight();
    const ImVec2      p1(p0.x + w, p0.y + h);

    const bool  valid = *sel >= 0 && *sel < n;
    const char* shown = (o.preview && o.preview[0]) ? o.preview : (valid ? get(ctx, *sel) : nullptr);
    const bool  placeholder = shown == nullptr;
    if (placeholder) shown = (o.hint && o.hint[0]) ? o.hint : (n == 0 ? "None available" : "Select\xE2\x80\xA6");

    bool changed = false;
    if (n == 0) ImGui::BeginDisabled();
    // The face is the kit's, so read the live frame roles (a caller or the field may have pushed
    // them: issue border, disabled hollow frame, glass-tinted fills) before clearing ImGui's own.
    const ImVec4 fillRest = ImGui::ColorConvertU32ToFloat4(ImGui::GetColorU32(ImGuiCol_FrameBg));
    const ImVec4 fillHov  = ImGui::ColorConvertU32ToFloat4(ImGui::GetColorU32(ImGuiCol_FrameBgHovered));
    const ImVec4 fillAct  = ImGui::ColorConvertU32ToFloat4(ImGui::GetColorU32(ImGuiCol_FrameBgActive));
    const ImU32  edge     = st.FrameBorderSize > 0.0f ? ImGui::GetColorU32(ImGuiCol_Border)
                                                      : ui::controlBorderU32();
    const ImVec4 clear    = roleVec(palette.frameBg, 0.0f);
    ImGui::PushStyleColor(ImGuiCol_FrameBg,        clear);
    ImGui::PushStyleColor(ImGuiCol_FrameBgHovered, clear);
    ImGui::PushStyleColor(ImGuiCol_FrameBgActive,  clear);
    ImGui::PushStyleColor(ImGuiCol_PopupBg,        roleVec(palette.popupBg));
    ImGui::PushStyleColor(ImGuiCol_Border,         roleVec(palette.glassBorder));
    ImGui::PushStyleVar(ImGuiStyleVar_FrameBorderSize, 0.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_PopupRounding,   rowRound());
    ImGui::PushStyleVar(ImGuiStyleVar_PopupBorderSize, ui::metrics::stroke(1.0f));
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,   ImVec2(st.FramePadding.x, ui::metrics::U(1.0f)));
    constrainNextComboPopup(p0.x, w);
    // Resolve the frame id while the PARENT window is current: once BeginCombo opens the popup,
    // GetID() and LastItemData both belong to the popup window.
    const ImGuiID fid = ImGui::GetID(cid);
    ImGui::SetNextItemWidth(w);
    const bool open = ImGui::BeginCombo(cid, "", ImGuiComboFlags_NoArrowButton | ImGuiComboFlags_HeightLarge);
    ImGui::PopStyleVar(4);
    ImGui::PopStyleColor(5);
    const bool hovered = g.HoveredId == fid;
    if (open) {
        ImDrawList*         pdl   = ImGui::GetWindowDrawList();
        ImDrawListSplitter& split = comboSplitter();
        split.Split(pdl, 2);
        split.SetCurrentChannel(pdl, 1);
        const ImVec4 none = roleVec(palette.accent, 0.0f);   // Selectable's square fill off
        ImGui::PushStyleColor(ImGuiCol_Header,        none);
        ImGui::PushStyleColor(ImGuiCol_HeaderHovered, none);
        ImGui::PushStyleColor(ImGuiCol_HeaderActive,  none);
        const float rr    = chipRound();
        const float inset = ui::metrics::stroke(1.0f);
        for (int i = 0; i < n; ++i) {
            ImGui::PushID(i);
            const bool dis = o.itemDisabled && o.itemDisabled[i];
            if (ImGui::Selectable(get(ctx, i), i == *sel,
                                  dis ? ImGuiSelectableFlags_Disabled : ImGuiSelectableFlags_None) &&
                !dis && i != *sel) {
                *sel    = i;
                changed = true;
            }
            const bool  itemHov = ImGui::IsItemHovered();
            const float hv = anim::toward(ImHashStr("##kit_combo_hv", 0, ImGui::GetItemID()),
                                          itemHov ? 1.0f : 0.0f, anim::kFast);
            const bool  isSel = i == *sel;
            if (isSel || hv > kSettled) {
                // Selectable's rect is padded by half the item spacing on every side so rows
                // touch; inset it back so each highlight is its own rounded pill.
                const ImVec2 a = ImGui::GetItemRectMin(), b = ImGui::GetItemRectMax();
                const ImVec4 rest = isSel ? roleVec(palette.accentSoft) : none;
                split.SetCurrentChannel(pdl, 0);
                pdl->AddRectFilled(ImVec2(a.x, a.y + inset), ImVec2(b.x, b.y - inset),
                                   ImGui::GetColorU32(lerpVec4(rest, roleVec(palette.accentMid), hv)),
                                   rr);
                split.SetCurrentChannel(pdl, 1);
            }
            if (ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled)) {
                if (dis) tooltip(o.itemReasons ? o.itemReasons[i] : nullptr);
                else     tooltip(o.itemTips ? o.itemTips[i] : nullptr);
            }
            if (isSel) ImGui::SetItemDefaultFocus();
            ImGui::PopID();
        }
        ImGui::PopStyleColor(3);
        split.Merge(pdl);
        ImGui::EndCombo();
    }
    // Name the combo record for the shot/audit item snapshot (no-op outside test-engine builds).
    IMGUI_TEST_ENGINE_ITEM_INFO(fid, cid, 0);

    // Face + preview + chevron on the parent draw list (captured before BeginCombo opened its
    // popup). Open = pressed-in; hover eases the fill toward the hovered frame role.
    const float  hv    = anim::toward(ImHashStr("##kit_combo_face", 0, fid),
                                      (hovered || open) ? 1.0f : 0.0f, anim::kFast);
    const ImVec4 fill  = open ? fillAct : lerpVec4(fillRest, fillHov, hv);
    const bool   inert = n == 0 || o.disabled;
    ui::controlFace(dl, p0, p1, ImGui::ColorConvertFloat4ToU32(fill), edge, st.FrameRounding,
                    inert ? 0.0f : (open ? kPressedLift : 1.0f));
    const float       chevR = h * kChevronFrac;
    const float       textW = ImMax(w - st.FramePadding.x - h, ui::metrics::stroke(1.0f));
    const std::string txt   = elide(shown, textW);
    const ImVec2      ts    = ImGui::CalcTextSize(txt.c_str());
    dl->AddText(ImVec2(p0.x + st.FramePadding.x, p0.y + (h - ts.y) * 0.5f),
                placeholder ? ImGui::GetColorU32(ImGuiCol_TextDisabled) : inkU32(), txt.c_str());
    paintChevronDown(dl, ImVec2(p0.x + w - h * 0.5f, p0.y + h * 0.5f), chevR, inkU32(kUnitInkA));
    if (n == 0) ImGui::EndDisabled();

    if (!o.disabled && ImGui::IsWindowHovered(ImGuiHoveredFlags_AllowWhenBlockedByActiveItem) &&
        ImGui::IsMouseHoveringRect(p0, p1) &&
        !ImGui::IsPopupOpen("", ImGuiPopupFlags_AnyPopupId)) {
        if (txt != shown && o.tip && o.tip[0]) {
            const std::string both = std::string(shown) + "\n\n" + o.tip;
            tooltip(both.c_str());
        } else if (txt != shown) {
            tooltip(shown);
        } else {
            tooltip(o.tip);
        }
    }
    return changed;
}

bool anyTrue(const bool* a, int n) {
    if (!a) return false;
    for (int i = 0; i < n; ++i)
        if (a[i]) return true;
    return false;
}

}  // namespace

// ==================================================================================================
// tooltips
// ==================================================================================================
void tooltip(const char* text) {
    if (!text || !text[0]) return;
    if (!ImGui::BeginTooltip()) return;
    ImGui::PushTextWrapPos(ImGui::GetFontSize() * kTooltipWrapEm);
    ImGui::TextUnformatted(text);
    ImGui::PopTextWrapPos();
    ImGui::EndTooltip();
}

void itemTooltip(const char* text) {
    if (text && text[0] && ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled)) tooltip(text);
}

// ==================================================================================================
// combo popup geometry
// ==================================================================================================
void constrainNextComboPopup(float frameX, float frameW, int maxItems) {
    const ImGuiStyle&    st    = ImGui::GetStyle();
    const ImGuiViewport* vp    = ImGui::GetWindowViewport();
    const float          right = vp->WorkPos.x + vp->WorkSize.x - st.DisplaySafeAreaPadding.x;
    const float          minW  = ImMax(frameW, ui::metrics::stroke(1.0f));
    const float          maxW  = ImMax(minW, right - frameX);
    // ImGui's own row-count height (CalcMaxPopupHeightFromItemCount), with the padding the popup
    // will actually use: a caller-supplied constraint replaces ImGui's height logic entirely.
    const int   rows = ImMax(maxItems, 1);
    const float maxH = (ImGui::GetFontSize() + st.ItemSpacing.y) * (float)rows - st.ItemSpacing.y +
                       st.WindowPadding.y * 2.0f;
    ImGui::SetNextWindowSizeConstraints(ImVec2(minW, 0.0f), ImVec2(maxW, maxH));
}

// ==================================================================================================
// formatting
// ==================================================================================================
std::string fmtPlain(double v, int sigFigs) {
    const int sig = clampSig(sigFigs);
    if (std::isnan(v)) return std::string("nan");
    if (std::isinf(v)) return std::string(v > 0 ? "inf" : "-inf");
    if (v == 0.0) return std::string("0");
    const double ar = std::fabs(roundSig(v, sig));
    if (ar < 1e-4 || ar >= 1e15) return expSig(v, sig);
    return fixedSig(v, sig);
}

std::string fmtValue(double v, int sigFigs) {
    const int sig = clampSig(sigFigs);
    if (std::isnan(v)) return std::string(kEmDash);
    if (std::isinf(v)) return std::string(v > 0 ? "inf" : "-inf");
    if (v == 0.0) return std::string("0");
    const double a = std::fabs(v);
    if (a < 1e-15) return std::string("~0");   // U+2248 is not in the atlas
    const double ar = std::fabs(roundSig(v, sig));
    if (ar < 1e-4) {
        std::string mant;
        const char* prefix = nullptr;
        if (engParts(v, sig, &mant, &prefix)) return mant + " " + prefix;
        return expSig(v, sig);
    }
    if (ar >= 1e15) return expSig(v, sig);
    std::string s = fixedSig(v, sig);
    if (ar >= 1e4) insertThousands(&s);
    return s;
}

std::string fmtQuantity(double v, const char* unit, int sigFigs) {
    if (!unit || !unit[0]) return fmtValue(v, sigFigs);
    if (std::isnan(v)) return std::string(kEmDash);
    const int    sig = clampSig(sigFigs);
    const double a   = std::fabs(v);
    if (std::isfinite(v) && v != 0.0 && a >= 1e-15 && std::fabs(roundSig(v, sig)) < 1e-4) {
        std::string mant;
        const char* prefix = nullptr;
        if (prefixableUnit(unit) && engParts(v, sig, &mant, &prefix))
            return mant + " " + prefix + unit;
        return expSig(v, sig) + " " + unit;
    }
    const bool  attached = std::strcmp(unit, "\xC2\xB0") == 0;   // bare degree sign: "45°"
    std::string s        = fmtValue(v, sig);
    if (!attached) s += ' ';
    s += unit;
    return s;
}

std::string fmtQuantityScaled(double v, const char* unit, int sigFigs) {
    const char* u = unitText(unit);
    if (!u[0] || !std::isfinite(v)) return fmtQuantity(v, u, sigFigs);
    // Ladders of x1000 steps. Mass starts at kg (not g: "g" is also standard gravity).
    static const char* const kLadders[][5] = {
        {"Pa", "kPa", "MPa", "GPa", nullptr},
        {"psi", "ksi", "Msi", nullptr, nullptr},
        {"N", "kN", "MN", nullptr, nullptr},
        {"lbf", "kip", nullptr, nullptr, nullptr},
        {"N\xC2\xB7" "m", "kN\xC2\xB7" "m", "MN\xC2\xB7" "m", nullptr, nullptr},
        {"W", "kW", "MW", "GW", nullptr},
        {"J", "kJ", "MJ", "GJ", nullptr},
        {"Hz", "kHz", "MHz", "GHz", nullptr},
        {"kg", "t", nullptr, nullptr, nullptr},
        {"\xC2\xB5" "m", "mm", "m", "km", nullptr},
    };
    const int sig = clampSig(sigFigs);
    for (const auto& ladder : kLadders) {
        for (int i = 0; i < 5 && ladder[i]; ++i) {
            if (std::strcmp(ladder[i], u) != 0) continue;
            double x = v;
            int    j = i;
            while (j + 1 < 5 && ladder[j + 1] && std::fabs(roundSig(x, sig)) >= 1e4) {
                x /= 1000.0;
                ++j;
            }
            return fmtQuantity(x, ladder[j], sigFigs);
        }
    }
    return fmtQuantity(v, u, sigFigs);
}

std::string fmtDelta(double from, double to, const char* unit, bool higherIsBetter,
                     Tone* outTone, int sigFigs) {
    if (outTone) *outTone = Tone::Neutral;
    if (!std::isfinite(from) || !std::isfinite(to)) return std::string(kEmDash);
    const double d   = to - from;
    const double ref = ImMax(std::fabs(from), std::fabs(to));
    if (d == 0.0 || std::fabs(d) <= 1e-9 * ref) {
        std::string s("\xC2\xB1" "0");   // ± U+00B1
        if (unit && unit[0]) {
            if (std::strcmp(unit, "\xC2\xB0") != 0) s += ' ';
            s += unit;
        }
        return s;
    }
    std::string s(d > 0.0 ? "+" : "-");
    s += fmtQuantity(std::fabs(d), unit, sigFigs);
    if (from != 0.0) {
        const double pct = d / std::fabs(from) * 100.0;
        s += pct > 0.0 ? " (+" : " (-";
        s += fmtValue(std::fabs(pct), sigFigs);
        s += "%)";
    }
    if (outTone) *outTone = ((d > 0.0) == higherIsBetter) ? Tone::Ok : Tone::Warn;
    return s;
}

bool parseNumber(const char* text, double* out) {
    if (!text || !out) return false;
    char        b[128];
    std::size_t n = 0;
    for (const char* p = text; *p; ++p) {
        if (*p == ',' || *p == '_' || *p == ' ' || *p == '\t') continue;
        if (n + 1 >= sizeof(b)) return false;
        b[n++] = *p;
    }
    if (n == 0) return false;
    b[n] = '\0';
    errno     = 0;
    char*  end = nullptr;
    double v   = std::strtod(b, &end);
    if (errno == ERANGE || end != b + n || !std::isfinite(v)) return false;
    *out = v;
    return true;
}

// ==================================================================================================
// units
// ==================================================================================================
const char* unitText(Unit u) {
    for (const UnitRow& r : kUnits)
        if (r.u == u) return r.text;
    return "";
}

const char* unitText(const char* key) {
    if (!key) return "";
    if (!key[0]) return key;
    for (const UnitRow& r : kUnits)
        if (std::strcmp(r.text, key) == 0) return r.text;
    for (const UnitRow& r : kUnits)
        if (r.aliases[0] && aliasMatch(r.aliases, key)) return r.text;
    return key;
}

// ==================================================================================================
// groups
// ==================================================================================================
void fieldGroupBegin(const char* id, const char* widestLabelHint) {
    ImGui::PushID(id && id[0] ? id : "kit_fieldgroup");
    GroupFrame g;
    g.storage  = ImGui::GetStateStorage();
    g.keyLabel = ImGui::GetID("##kit_fg_label");
    g.keyNeed  = ImGui::GetID("##kit_fg_need");
    g.keySeen  = ImGui::GetID("##kit_fg_seen");
    // The hint only stands in until the group has measured its real labels once: an
    // over-long hint must not stack a card whose drawn labels fit.
    const bool seen   = g.storage->GetInt(g.keySeen, 0) != 0;
    const float widest = seen ? g.storage->GetFloat(g.keyLabel, 0.0f)
                              : (widestLabelHint && widestLabelHint[0]
                                     ? ImGui::CalcTextSize(widestLabelHint).x
                                     : 0.0f);
    const float need     = g.storage->GetFloat(g.keyNeed, 0.0f);
    const float rowLeft  = ImGui::GetCursorScreenPos().x;
    const float rowRight = rowLeft + ImMax(ImGui::GetContentRegionAvail().x, ui::metrics::stroke(1.0f));
    g.valueX  = paneValueX(paneWindow());
    g.stacked = !fitsInline(widest, need, rowLeft, rowRight, g.valueX);
    g_groups.push_back(g);
}

void fieldGroupEnd() {
    if (g_groups.empty()) return;   // unmatched End: refuse rather than corrupt the ID stack
    const GroupFrame g = g_groups.back();
    g_groups.pop_back();
    g.storage->SetFloat(g.keyLabel, g.measLabel);
    g.storage->SetFloat(g.keyNeed, g.measNeed);
    g.storage->SetInt(g.keySeen, 1);
    ImGui::PopID();
}

float fieldValueColumnX() {
    if (!g_groups.empty()) return g_groups.back().valueX;
    return paneValueX(paneWindow());
}

// ==================================================================================================
// layout engine
// ==================================================================================================
namespace {

// The disabled reason as one dim caption line starting at `x` (elided; full text on hover).
void disabledCaption(const char* reason, float x, float right) {
    if (!reason || !reason[0]) return;
    ImGui::SetCursorScreenPos(ImVec2(x, ImGui::GetCursorScreenPos().y));
    const std::string shown = elide(reason, ImMax(right - x, ui::metrics::stroke(1.0f)));
    ImVec4 ink = ImGui::GetStyleColorVec4(ImGuiCol_Text);
    ink.w *= kUnitInkA;
    ImGui::PushStyleColor(ImGuiCol_Text, ink);
    ImGui::TextUnformatted(shown.c_str());
    ImGui::PopStyleColor();
    if (shown != reason && ImGui::IsItemHovered()) tooltip(reason);
}

float beginField(const char* id, const char* label, const FieldOpts& o, const FieldKind& k) {
    ImGui::PushID(id && id[0] ? id : "kit_field");

    const ImGuiStyle& st  = ImGui::GetStyle();
    const float       sp  = st.ItemSpacing.x;
    const char*       lab = label ? label : "";
    const char*       mark = " *";
    const float markerW = o.required ? ImGui::CalcTextSize(mark).x : 0.0f;
    const float labelW  = lab[0] ? ImGui::CalcTextSize(lab).x + markerW : 0.0f;
    const float unitW   = (o.unit && o.unit[0]) ? ImGui::CalcTextSize(o.unit).x : 0.0f;

    // Unit lane: every built-in value widget (number, enum, combo, text, path, readout) ends at
    // the pane lane, so all value boxes of the pane share ONE right edge whether or not they
    // carry a unit. Custom fieldBegin rows reserve it only when they carry a unit.
    ImGuiWindow* pane = paneWindow();
    const float  lane = paneLane(pane, unitW);
    const float  slot = ((k.lane || unitW > 0.0f) && lane > 0.0f) ? lane + sp : 0.0f;

    FieldFrame f;
    f.stateId  = ImGui::GetID("##kit_field");
    const ImVec2 start = ImGui::GetCursorScreenPos();
    const float  avail = ImMax(ImGui::GetContentRegionAvail().x, ui::metrics::stroke(1.0f));
    const float  need  = k.need > 0.0f ? k.need + slot : 0.0f;
    const Layout L     = resolve(labelW, need, start.x, start.x + avail, pane);
    f.rowLeft  = start.x;
    f.rowTop   = start.y;
    f.rowRight = start.x + avail;
    f.stacked  = L.stacked;
    f.unit     = o.unit;
    f.issue    = o.issue;
    f.issueTone = o.issueTone;
    f.disabled = o.disabled;
    f.disabledReason = o.disabledReason;
    f.tip      = o.tip;

    EditState& fs = stateFor(f.stateId);

    if (o.disabled) ImGui::BeginDisabled();

    if (lab[0]) {
        if (!L.stacked) ImGui::AlignTextToFramePadding();
        const float       maxW  = ImMax((L.stacked ? avail : L.valueX - start.x - sp) - markerW,
                                        ui::metrics::stroke(1.0f));
        const std::string shown = elide(lab, maxW);
        ImVec4 ink = ImGui::GetStyleColorVec4(ImGuiCol_Text);
        ink.w *= kLabelInkA;
        ImGui::PushStyleColor(ImGuiCol_Text, ink);
        ImGui::TextUnformatted(shown.c_str());
        ImGui::PopStyleColor();
        if (!o.disabled && ImGui::IsItemHovered()) {
            if (shown != lab) {
                std::string both(lab);
                if (o.tip && o.tip[0]) both.append("\n\n").append(o.tip);
                tooltip(both.c_str());
            } else {
                tooltip(o.tip);
            }
        }
        if (o.required) {
            ImGui::SameLine(0.0f, 0.0f);
            ImGui::PushStyleColor(ImGuiCol_Text, toneVec(Tone::Accent));
            ImGui::TextUnformatted(mark);
            ImGui::PopStyleColor();
        }
    }

    if (!L.stacked && (lab[0] || !g_groups.empty())) {
        // Inline row (or an unlabelled continuation row of an inline group): value column.
        if (lab[0]) ImGui::SameLine(0.0f, 0.0f);
        const ImVec2 c = ImGui::GetCursorScreenPos();
        ImGui::SetCursorScreenPos(ImVec2(ImMax(L.valueX, c.x), c.y));
    }

    const ImVec2 v0 = ImGui::GetCursorScreenPos();
    f.valueX = v0.x;
    f.valueY = v0.y;
    f.valueW = ImMax(f.rowRight - v0.x - slot, ui::metrics::stroke(1.0f));
    f.slotX  = f.valueX + f.valueW + sp;

    ImGui::PushStyleVar(ImGuiStyleVar_FrameRounding, chipRound());
    ImGui::PushStyleVar(ImGuiStyleVar_FrameBorderSize, ui::metrics::stroke(1.0f));
    f.vars += 2;
    const bool ownIssue = o.issue && o.issue[0];
    if (ownIssue || !fs.error.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Border, toneVec(ownIssue ? o.issueTone : fs.errTone));
        ++f.cols;
    } else if (o.disabled) {
        // Clearly disabled: a hollow outlined frame (on top of the disabled alpha), not a filled
        // box that only looks a little faint.
        ImGui::PushStyleColor(ImGuiCol_Border, roleVec(palette.border));
        ImGui::PushStyleColor(ImGuiCol_FrameBg, roleVec(palette.frameBg, kDisabledFrameA));
        ImGui::PushStyleColor(ImGuiCol_FrameBgHovered, roleVec(palette.frameBg, kDisabledFrameA));
        f.cols += 3;
    } else {
        // Every value frame carries the soft 1-px control edge in BOTH themes (dark used to be
        // borderless and light used the hard window outline), matching kit buttons and combos.
        ImGui::PushStyleColor(ImGuiCol_Border, roleVec(palette.controlBorder));
        ++f.cols;
    }
    ImGui::SetNextItemWidth(f.valueW);
    g_fields.push_back(f);
    return f.valueW;
}

}  // namespace

float fieldBegin(const char* id, const char* label, const FieldOpts& o) {
    return beginField(id, label, o, FieldKind{});
}

void fieldEnd() {
    if (g_fields.empty()) return;   // unmatched End: refuse rather than corrupt the ID stack
    const FieldFrame f = g_fields.back();
    g_fields.pop_back();

    ImGui::PopStyleColor(f.cols);
    ImGui::PopStyleVar(f.vars);

    if (f.unit && f.unit[0]) {
        const ImGuiStyle& st = ImGui::GetStyle();
        ImGui::GetWindowDrawList()->AddText(ImVec2(f.slotX, f.valueY + st.FramePadding.y),
                                            inkU32(kUnitInkA), f.unit);
    }

    if (f.disabled) {
        const ImVec2 rowMax(f.rowRight, ImGui::GetCursorScreenPos().y);
        if (ImGui::IsWindowHovered() &&
            ImGui::IsMouseHoveringRect(ImVec2(f.rowLeft, f.rowTop), rowMax)) {
            tooltip(f.disabledReason && f.disabledReason[0] ? f.disabledReason : f.tip);
        }
        ImGui::EndDisabled();
    }

    // Issue / disabled caption: full row width from the row's left edge (an inline row's value
    // column is far too narrow for a sentence), wrapping rather than squeezing.
    const EditState& fs    = stateFor(f.stateId);
    const bool       own   = f.issue && f.issue[0];
    const char*      issue = own ? f.issue : (fs.error.empty() ? nullptr : fs.error.c_str());
    if (issue) {
        ImGui::SetCursorScreenPos(ImVec2(f.rowLeft, ImGui::GetCursorScreenPos().y));
        inlineStatus(issue, own ? f.issueTone : fs.errTone);
    } else if (f.disabled) {
        // The reason is visible inline, not tooltip-only.
        disabledCaption(f.disabledReason, f.rowLeft, f.rowRight);
    }
    ImGui::PopID();
}

void fieldReadout(const char* id, const char* label, const char* value, const FieldOpts& o,
                  Tone valueTone) {
    const float       w = fieldBegin(id, label, o);
    const char*       v = value ? value : "";
    const std::string shown = elide(v, w);
    ImGui::AlignTextToFramePadding();
    ImGui::PushStyleColor(ImGuiCol_Text, valueTone == Tone::Neutral
                                             ? ImGui::GetStyleColorVec4(ImGuiCol_Text)
                                             : toneVec(valueTone));
    ImGui::TextUnformatted(shown.c_str());
    ImGui::PopStyleColor();
    if (!o.disabled && ImGui::IsItemHovered()) tooltip(shown != v ? v : o.tip);
    fieldEnd();
}

// ==================================================================================================
// numeric fields
// ==================================================================================================
bool fieldNumber(const char* id, const char* label, double* v, const FieldOpts& o) {
    if (!v) return false;
    const float  w   = beginField(id, label, o, FieldKind{true, 0.0f});
    EditState&   fs  = fieldState();
    const bool   has = !o.present || *o.present;
    double       nd  = 0.0;
    std::string  bad;
    const Cell   c   = numberCell("##v", o.hint, toDisp(*v, o), has, false, o.sigFigs, w, &nd, &bad);
    valueTip(o);

    bool committed = false;
    switch (c) {
        case Cell::Value: {
            const double sv = toStor(nd, o);
            if (!std::isfinite(sv)) {
                setNote(fs, quoted(bad.empty() ? fmtPlain(nd) : bad) + " is out of range.", Tone::Err);
                break;
            }
            const double cv = clampNoted(sv, o, fs);
            if (cv != *v || (o.present && !*o.present)) {
                *v = cv;
                if (o.present) *o.present = true;
                committed = true;
            }
            break;
        }
        case Cell::Empty:
            if (o.present) {
                fs.error.clear();
                if (*o.present) {
                    *o.present = false;
                    committed  = true;
                }
            } else {
                setNote(fs, "Enter a value.", Tone::Err);
            }
            break;
        case Cell::Invalid: setNote(fs, quoted(bad) + " is not a number.", Tone::Err); break;
        case Cell::None: break;
    }
    fieldEnd();
    return committed;
}

bool fieldNumber(const char* id, const char* label, float* v, const FieldOpts& o) {
    if (!v) return false;
    double     t = (double)*v;
    const bool r = fieldNumber(id, label, &t, o);
    if (r) *v = (float)t;
    return r;
}

bool fieldInt(const char* id, const char* label, int* v, const FieldOpts& o) {
    if (!v) return false;
    const float w   = beginField(id, label, o, FieldKind{true, 0.0f});
    EditState&  fs  = fieldState();
    const bool  has = !o.present || *o.present;
    double      nd  = 0.0;
    std::string bad;
    const Cell  c   = numberCell("##v", o.hint, (double)*v, has, true, o.sigFigs, w, &nd, &bad);
    valueTip(o);

    bool committed = false;
    switch (c) {
        case Cell::Value: {
            const double lo = ImMax(o.minV, (double)INT_MIN);
            const double hi = ImMin(o.maxV, (double)INT_MAX);
            double       cv = nd;
            if (cv < lo || cv > hi) {
                cv = cv < lo ? lo : hi;
                setNote(fs, std::string(nd < lo ? "Raised to the minimum, " : "Limited to the maximum, ") +
                                intText(cv) + ".",
                        Tone::Warn);
            } else {
                fs.error.clear();
            }
            const int iv = (int)cv;
            if (iv != *v || (o.present && !*o.present)) {
                *v = iv;
                if (o.present) *o.present = true;
                committed = true;
            }
            break;
        }
        case Cell::Empty:
            if (o.present) {
                fs.error.clear();
                if (*o.present) {
                    *o.present = false;
                    committed  = true;
                }
            } else {
                setNote(fs, "Enter a value.", Tone::Err);
            }
            break;
        case Cell::Invalid: setNote(fs, quoted(bad) + " is not a whole number.", Tone::Err); break;
        case Cell::None: break;
    }
    fieldEnd();
    return committed;
}

bool fieldVec3(const char* id, const char* label, double v[3], const FieldOpts& o) {
    if (!v) return false;
    static const char* const kXYZ[3] = {"X", "Y", "Z"};

    // Each cell carries its axis letter inside the frame and keeps room for kVecCellSample;
    // when the inline value column cannot hold three such cells the group stacks (label above
    // a full-width row of three cells).
    const ImGuiStyle& st    = ImGui::GetStyle();
    const float       sp    = st.ItemInnerSpacing.x;
    float             letW  = 0.0f;
    for (const char* a : kXYZ) letW = ImMax(letW, ImGui::CalcTextSize(a).x);
    const float minCell = letW + sp + ImGui::CalcTextSize(kVecCellSample).x + st.FramePadding.x * 2.0f;

    const float w   = beginField(id, label, o, FieldKind{true, minCell * 3.0f + sp * 2.0f});
    EditState&  fs  = fieldState();
    const bool  has = !o.present || *o.present;
    const float cw  = ImMax((w - sp * 2.0f) / 3.0f, ui::metrics::stroke(1.0f));
    const bool  grp = o.commitAsGroup && fs.groupEditing;

    bool committed = false;
    bool anyActive = false;
    for (int k = 0; k < 3; ++k) {
        if (k) ImGui::SameLine(0.0f, sp);
        ImGui::PushID(k);
        const double disp = grp ? fs.draft[k] : toDisp(v[k], o);
        double       nd   = 0.0;
        std::string  bad;
        const Cell c = numberCell("##c", o.cellHints ? o.cellHints[k] : nullptr, disp, has || grp,
                                  false, o.sigFigs, cw, &nd, &bad, kXYZ[k], axisRole(k));
        anyActive |= ImGui::IsItemActive();
        valueTip(o);
        if (c == Cell::Invalid) {
            setNote(fs, quoted(bad) + " is not a number.", Tone::Err);
        } else if (c == Cell::Value || c == Cell::Empty) {
            const double d = c == Cell::Empty ? toDisp(0.0, o) : nd;   // an emptied cell is 0
            if (o.commitAsGroup) {
                if (!fs.groupEditing) {
                    for (int j = 0; j < 3; ++j) fs.draft[j] = has ? toDisp(v[j], o) : toDisp(0.0, o);
                    fs.groupEditing = true;
                }
                fs.draft[k]   = d;
                fs.groupDirty = true;
                fs.error.clear();
            } else {
                const double sv = toStor(d, o);
                if (std::isfinite(sv)) {
                    const double cv = clampNoted(sv, o, fs);
                    if (cv != v[k] || (o.present && !*o.present)) {
                        if (o.present && !*o.present)
                            for (int j = 0; j < 3; ++j)
                                if (j != k) v[j] = 0.0;
                        v[k] = cv;
                        if (o.present) *o.present = true;
                        committed = true;
                    }
                }
            }
        }
        ImGui::PopID();
    }

    if (o.commitAsGroup) {
        if (anyActive) {
            if (!fs.groupEditing) {
                for (int j = 0; j < 3; ++j) fs.draft[j] = has ? toDisp(v[j], o) : toDisp(0.0, o);
                fs.groupEditing = true;
                fs.groupDirty   = false;
            }
            fs.idleFrames = 0;
        } else if (fs.groupEditing && ++fs.idleFrames >= kGroupCommitIdle) {
            // Focus has left all three cells (a Tab between cells never idles this long).
            fs.groupEditing = false;
            fs.idleFrames   = 0;
            if (fs.groupDirty) {
                fs.groupDirty = false;
                double nv[3];
                bool   ok = true;
                for (int j = 0; j < 3 && ok; ++j) {
                    const double sv = toStor(fs.draft[j], o);
                    ok              = std::isfinite(sv);
                    nv[j]           = ok ? clampNoted(sv, o, fs) : 0.0;
                }
                if (ok && (nv[0] != v[0] || nv[1] != v[1] || nv[2] != v[2] ||
                           (o.present && !*o.present))) {
                    for (int j = 0; j < 3; ++j) v[j] = nv[j];
                    if (o.present) *o.present = true;
                    committed = true;
                }
            }
        }
    }
    fieldEnd();
    return committed;
}

bool fieldVec3(const char* id, const char* label, float v[3], const FieldOpts& o) {
    if (!v) return false;
    double     t[3] = {(double)v[0], (double)v[1], (double)v[2]};
    const bool r    = fieldVec3(id, label, t, o);
    if (r)
        for (int k = 0; k < 3; ++k) v[k] = (float)t[k];
    return r;
}

bool fieldRange(const char* id, const char* label, double* lo, double* hi, const FieldOpts& o) {
    if (!lo || !hi) return false;
    static const char* const kMinMax[2] = {"min", "max"};
    const char* const*       hints      = o.cellHints ? o.cellHints : kMinMax;
    const char*              dash       = "\xE2\x80\x93";   // en dash U+2013

    const ImGuiStyle& st    = ImGui::GetStyle();
    const float       sp    = st.ItemInnerSpacing.x;
    const float       dashW = ImGui::CalcTextSize(dash).x;
    const float       minCell = ImGui::CalcTextSize(kVecCellSample).x + st.FramePadding.x * 2.0f;
    const float       w     = beginField(id, label, o,
                                         FieldKind{true, minCell * 2.0f + dashW + sp * 2.0f});
    EditState&        fs    = fieldState();
    const float       cw    = ImMax((w - dashW - sp * 2.0f) * 0.5f, ui::metrics::stroke(1.0f));

    bool committed = false;
    for (int k = 0; k < 2; ++k) {
        if (k) {
            ImGui::SameLine(0.0f, sp);
            const ImVec2 p = ImGui::GetCursorScreenPos();
            ImGui::GetWindowDrawList()->AddText(ImVec2(p.x, p.y + st.FramePadding.y),
                                                inkU32(kUnitInkA), dash);
            ImGui::Dummy(ImVec2(dashW, ImGui::GetFrameHeight()));
            ImGui::SameLine(0.0f, sp);
        }
        ImGui::PushID(k);
        double*     tgt = k == 0 ? lo : hi;
        double      nd  = 0.0;
        std::string bad;
        const Cell  c   = numberCell("##c", hints[k], toDisp(*tgt, o), true, false, o.sigFigs, cw,
                                     &nd, &bad);
        valueTip(o);
        if (c == Cell::Invalid) {
            setNote(fs, quoted(bad) + " is not a number.", Tone::Err);
        } else if (c == Cell::Empty) {
            setNote(fs, "Enter both values.", Tone::Err);
        } else if (c == Cell::Value) {
            const double sv = toStor(nd, o);
            if (std::isfinite(sv)) {
                const double cv = clampNoted(sv, o, fs);
                if ((k == 0 && cv > *hi) || (k == 1 && cv < *lo)) {
                    setNote(fs, "The minimum cannot exceed the maximum.", Tone::Err);
                } else if (cv != *tgt) {
                    *tgt      = cv;
                    committed = true;
                }
            }
        }
        ImGui::PopID();
    }
    fieldEnd();
    return committed;
}

bool fieldRange(const char* id, const char* label, float* lo, float* hi, const FieldOpts& o) {
    if (!lo || !hi) return false;
    double     a = (double)*lo, b = (double)*hi;
    const bool r = fieldRange(id, label, &a, &b, o);
    if (r) {
        *lo = (float)a;
        *hi = (float)b;
    }
    return r;
}

// Kit slider: a thin track with an accent fill and a round thumb (never drawn over text), then an
// editable readout cell right-aligned against the unit. Dragging/clicking the track moves *v
// live and commits on release; typing in the cell commits on deactivation (clamped, noted).
bool fieldSlider(const char* id, const char* label, double* v, double lo, double hi,
                 const FieldOpts& o) {
    if (!v) return false;
    const ImGuiStyle& st = ImGui::GetStyle();
    const float       h  = ImGui::GetFrameHeight();
    const float       gap = st.ItemSpacing.x;

    double dlo = toDisp(lo, o), dhi = toDisp(hi, o);
    if (dlo > dhi) std::swap(dlo, dhi);
    const double mag = ImMax(std::fabs(dlo), std::fabs(dhi));
    const int    e   = mag > 0.0 ? (int)std::floor(std::log10(mag)) : 0;
    const int    dec = ImClamp(clampSig(o.sigFigs) - 1 - e, 0, 6);
    const double q   = std::pow(10.0, (double)dec);

    // Readout cell: as wide as the widest value the range can show.
    char loTxt[64], hiTxt[64];
    std::snprintf(loTxt, sizeof(loTxt), "%.*f", dec, dlo);
    std::snprintf(hiTxt, sizeof(hiTxt), "%.*f", dec, dhi);
    const float cellW = ImMax(ImMax(ImGui::CalcTextSize(loTxt).x, ImGui::CalcTextSize(hiTxt).x) +
                                  st.FramePadding.x * 2.0f,
                              h * kSliderCellMinFr);

    const float w = beginField(id, label, o, FieldKind{true, cellW + gap + h * kSliderTrackMinFr});
    EditState&  fs     = fieldState();
    const float trackW = ImMax(w - cellW - gap, ui::metrics::stroke(1.0f));
    double      d      = ImClamp(toDisp(*v, o), dlo, dhi);
    const double sLo   = ImMin(lo, hi), sHi = ImMax(lo, hi);

    // ---- track ----
    const ImVec2 p0 = ImGui::GetCursorScreenPos();
    ImGui::InvisibleButton("##track", ImVec2(trackW, h));
    const bool  hovered = ImGui::IsItemHovered();
    const bool  active  = ImGui::IsItemActive();
    const float r       = h * kThumbFrac;
    const float x0      = p0.x + r;
    const float x1      = ImMax(p0.x + trackW - r, x0);
    if (ImGui::IsItemActivated()) fs.dragDirty = false;
    if (active && x1 > x0 && dhi > dlo) {
        const float t  = ImSaturate((ImGui::GetIO().MousePos.x - x0) / (x1 - x0));
        double      nd = dlo + (dhi - dlo) * (double)t;
        nd             = ImClamp(std::round(nd * q) / q, dlo, dhi);
        if (nd != d) {
            const double sv = toStor(nd, o);
            if (std::isfinite(sv)) {
                *v           = ImClamp(sv, sLo, sHi);
                d            = nd;
                fs.dragDirty = true;
                fs.error.clear();
            }
        }
    }
    bool committed = false;
    if (ImGui::IsItemDeactivated()) {
        committed    = fs.dragDirty;
        fs.dragDirty = false;
    }
    if (hovered || active) ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
    valueTip(o);

    ImDrawList*  dl    = ImGui::GetWindowDrawList();
    const float  alpha = st.Alpha;
    const float  cy    = p0.y + h * 0.5f;
    const float  th    = ui::metrics::U(kTrackU);
    auto         xOf   = [&](double val) {
        return dhi > dlo ? x0 + (x1 - x0) * (float)((val - dlo) / (dhi - dlo)) : x0;
    };
    const float xt = xOf(d);
    const float xa = xOf(dlo < 0.0 && dhi > 0.0 ? 0.0 : dlo);   // fill grows from zero when inside
    // Rounded track: the soft control edge tone for the rail, accent for the filled run.
    dl->AddRectFilled(ImVec2(x0, cy - th * 0.5f), ImVec2(x1, cy + th * 0.5f),
                      roleU32(palette.controlBorder, alpha), th * 0.5f);
    if (xt != xa) {
        dl->AddRectFilled(ImVec2(ImMin(xa, xt), cy - th * 0.5f), ImVec2(ImMax(xa, xt), cy + th * 0.5f),
                          roleU32(palette.accent, alpha), th * 0.5f);
    }
    const float hv = anim::toward(ImGui::GetID("##thumb_hv"), (hovered || active) ? 1.0f : 0.0f,
                                  anim::kFast);
    if (hv > kSettled) {
        dl->AddCircleFilled(ImVec2(xt, cy), r * (1.0f + (kThumbHaloMul - 1.0f) * hv),
                            roleU32(palette.accentSoft, alpha * hv));
    }
    // Thumb: the raised control face as a disc (rounding = radius) — soft lip, lighter top, rim.
    ui::controlFace(dl, ImVec2(xt - r, cy - r), ImVec2(xt + r, cy + r),
                    roleU32(active ? palette.accentDim : palette.accent, alpha),
                    roleU32(palette.accentDim, kThumbEdgeA * alpha), r, active ? kPressedLift : 1.0f);

    // ---- readout cell ----
    ImGui::SameLine(0.0f, gap);
    double      nd = 0.0;
    std::string bad;
    const Cell  c  = numberCell("##n", nullptr, d, true, false, o.sigFigs, cellW, &nd, &bad);
    valueTip(o);
    switch (c) {
        case Cell::Value: {
            const double sv = toStor(nd, o);
            if (!std::isfinite(sv)) {
                setNote(fs, quoted(bad.empty() ? fmtPlain(nd) : bad) + " is out of range.", Tone::Err);
                break;
            }
            FieldOpts lim = o;
            lim.minV      = sLo;
            lim.maxV      = sHi;
            const double cv = clampNoted(sv, lim, fs);
            if (cv != *v) {
                *v        = cv;
                committed = true;
            }
            break;
        }
        case Cell::Empty:   setNote(fs, "Enter a value.", Tone::Err); break;
        case Cell::Invalid: setNote(fs, quoted(bad) + " is not a number.", Tone::Err); break;
        case Cell::None:    break;
    }
    fieldEnd();
    return committed;
}

bool fieldSlider(const char* id, const char* label, float* v, float lo, float hi,
                 const FieldOpts& o) {
    if (!v) return false;
    double     t = (double)*v;
    const bool r = fieldSlider(id, label, &t, (double)lo, (double)hi, o);
    if ((float)t != *v) *v = (float)t;   // live drag tracking
    return r;
}

// ==================================================================================================
// text / path
// ==================================================================================================
bool fieldText(const char* id, const char* label, char* buf, std::size_t n, const FieldOpts& o) {
    if (!buf || n == 0) return false;
    const float        w   = beginField(id, label, o, FieldKind{true, 0.0f});
    const std::string* txt = nullptr;
    const bool         r   = textCell("##t", buf, w, o, &txt);
    if (r) copyToBuf(*txt, buf, n);
    fieldEnd();
    return r;
}

bool fieldText(const char* id, const char* label, std::string* v, const FieldOpts& o) {
    if (!v) return false;
    // Paragraphs and native JSON need a readable line, not the scalar value column.
    const float need = o.lines > 1 ? ImGui::GetFontSize() * 16.0f : 0.0f;
    const float        w   = beginField(id, label, o, FieldKind{true, need});
    const std::string* txt = nullptr;
    const bool         r   = textCell("##t", v->c_str(), w, o, &txt);
    if (r) *v = *txt;
    fieldEnd();
    return r;
}

namespace {
// Shared path body: text cell + Browse button in the value column.
bool pathCore(const char* cur, bool* browse, const FieldOpts& o, float w,
              const std::string** txt) {
    const ImGuiStyle& st     = ImGui::GetStyle();
    const char*       btn    = o.browseLabel && o.browseLabel[0] ? o.browseLabel : "Browse\xE2\x80\xA6";
    const float       btnW   = ImGui::CalcTextSize(btn).x + st.FramePadding.x * 2.0f;
    const float       inputW = ImMax(w - btnW - st.ItemSpacing.x, ui::metrics::stroke(1.0f));
    const bool        r      = textCell("##t", cur, inputW, o, txt);
    ImGui::SameLine(0.0f, st.ItemSpacing.x);
    if (secondaryButton(btn, !o.disabled, nullptr, ImVec2(btnW, 0.0f)) && browse) *browse = true;
    return r;
}
}  // namespace

bool fieldPath(const char* id, const char* label, std::string* v, bool* browse,
               const FieldOpts& o) {
    if (!v) return false;
    const float        w   = beginField(id, label, o, FieldKind{true, 0.0f});
    const std::string* txt = nullptr;
    const bool         r   = pathCore(v->c_str(), browse, o, w, &txt);
    if (r) *v = *txt;
    fieldEnd();
    return r;
}

bool fieldPath(const char* id, const char* label, char* buf, std::size_t n, bool* browse,
               const FieldOpts& o) {
    if (!buf || n == 0) return false;
    const float        w   = beginField(id, label, o, FieldKind{true, 0.0f});
    const std::string* txt = nullptr;
    const bool         r   = pathCore(buf, browse, o, w, &txt);
    if (r) copyToBuf(*txt, buf, n);
    fieldEnd();
    return r;
}

// ==================================================================================================
// checkbox
// ==================================================================================================
bool checkbox(const char* id, const char* label, bool* v, const char* tip, bool enabled,
              const char* disabledReason) {
    ImGui::PushID(id && id[0] ? id : "kit_checkbox");

    const char*  lab = label ? label : "";
    const float  h   = ImGui::GetFrameHeight();
    const float  box = ImGui::GetTextLineHeight();
    const float  gap = ui::metrics::U(2.0f);
    const ImVec2 ts  = ImGui::CalcTextSize(lab);
    const float  w   = box + (lab[0] ? gap + ts.x : 0.0f);
    const ImVec2 p0  = ImGui::GetCursorScreenPos();

    if (!enabled) ImGui::BeginDisabled();
    bool clicked = ImGui::InvisibleButton("##kit_check", ImVec2(w, h));
    if (!enabled) {
        ImGui::EndDisabled();
        clicked = false;
    }
    const bool hovered = ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled);
    const bool held    = ImGui::IsItemActive();
    if (clicked && v) *v = !*v;

    const bool  on   = v && *v;
    const float fill = anim::toward(ImGui::GetID("##fill"), on ? 1.0f : 0.0f, anim::kFast);
    const float hv   = anim::toward(ImGui::GetID("##hv"), (hovered && enabled) ? 1.0f : 0.0f,
                                    anim::kFast);

    ImDrawList*  dl    = ImGui::GetWindowDrawList();
    const float  alpha = (enabled ? 1.0f : kDisabledA) * ImGui::GetStyle().Alpha;
    const ImVec2 bMin(p0.x, p0.y + (h - box) * 0.5f);
    const ImVec2 bMax(bMin.x + box, bMin.y + box);
    const float  rnd = ui::metrics::rounding(kCheckRound);

    // One raised face whose fill eases frame -> accent as it checks; the soft control edge leans
    // toward the accent on hover and settles on the accent rim once checked.
    const ImVec4 base = held ? roleVec(palette.frameBgActive)
                             : lerpVec4(roleVec(palette.frameBg), roleVec(palette.frameBgHovered), hv);
    ImVec4 face = lerpVec4(base, roleVec(palette.accent), fill);
    face.w *= alpha;
    ImVec4 edge = lerpVec4(lerpVec4(roleVec(palette.controlBorder), roleVec(palette.accent),
                                    hv * kCheckHoverEdge),
                           roleVec(palette.accentDim, kThumbEdgeA), fill);
    edge.w *= alpha;
    ui::controlFace(dl, bMin, bMax, ImGui::ColorConvertFloat4ToU32(face),
                    ImGui::ColorConvertFloat4ToU32(edge), rnd, (held || !enabled) ? 0.0f : 1.0f);
    if (fill > kSettled) {
        ImVec4 ck = ui::maxContrastTextOn(roleVec(palette.accent));
        ck.w *= fill * alpha;
        const float in = box * kCheckInsetFrac;
        ImGui::RenderCheckMark(dl, ImVec2(bMin.x + in, bMin.y + in),
                               ImGui::ColorConvertFloat4ToU32(ck), box - in * 2.0f);
    }
    if (lab[0]) {
        dl->AddText(ImVec2(bMax.x + gap, p0.y + (h - ts.y) * 0.5f),
                    enabled ? inkU32() : roleU32(palette.textDisabled, ImGui::GetStyle().Alpha), lab);
    }
    if (hovered) tooltip(enabled ? tip : (disabledReason && disabledReason[0] ? disabledReason : tip));

    ImGui::PopID();
    return clicked;
}

bool fieldCheckbox(const char* id, const char* label, bool* v, const FieldOpts& o) {
    ImGui::PushID(id && id[0] ? id : "kit_fieldcheck");
    // The checkbox spans the row from its left edge: the label IS the hit target, so it never
    // sits in the label column. Disabled state comes from the opts, not BeginDisabled, so the
    // reason stays reachable on hover.
    const float rowLeft  = ImGui::GetCursorScreenPos().x;
    const float rowRight = rowLeft + ImMax(ImGui::GetContentRegionAvail().x, ui::metrics::stroke(1.0f));
    const bool  r        = checkbox("cb", label, v, o.tip, !o.disabled, o.disabledReason);
    if (o.issue && o.issue[0]) {
        inlineStatus(o.issue, o.issueTone);
    } else if (o.disabled) {
        // Caption aligned with the checkbox label (box + the checkbox's label gap).
        disabledCaption(o.disabledReason,
                        rowLeft + ImGui::GetTextLineHeight() + ui::metrics::U(2.0f), rowRight);
    }
    ImGui::PopID();
    return r;
}

// ==================================================================================================
// enum / combo
// ==================================================================================================
namespace {
// Value width a combo needs to show `text` unelided (kit combo: left padding + text + chevron).
float comboNeed(float textW) {
    return textW + ImGui::GetStyle().FramePadding.x + ImGui::GetFrameHeight();
}
}  // namespace

bool fieldEnum(const char* id, const char* label, int* sel, const char* const* items, int n,
               const FieldOpts& o) {
    if (!sel || !items || n < 0) return false;
    float itemsW = 0.0f;
    for (int i = 0; i < n; ++i) itemsW = ImMax(itemsW, ImGui::CalcTextSize(items[i] ? items[i] : "").x);
    const float widest = (o.preview && o.preview[0]) ? ImMax(itemsW, ImGui::CalcTextSize(o.preview).x)
                                                     : itemsW;
    // Need: the combo form shows every item unelided; a forced segmented row needs n segments.
    const float need = o.enumStyle == EnumStyle::Segmented
                           ? (itemsW + padX() * 2.0f) * (float)ImMax(n, 1)
                           : comboNeed(widest);
    const float w           = beginField(id, label, o, FieldKind{true, need});
    const bool  anyDisabled = anyTrue(o.itemDisabled, n);

    bool seg = false;
    if (n > 0 && !anyDisabled && *sel >= 0 && *sel < n) {
        if (o.enumStyle == EnumStyle::Segmented) {
            seg = true;
        } else if (o.enumStyle == EnumStyle::Auto && n <= 4) {
            seg = itemsW + padX() * 2.0f <= w / (float)n;
        }
    }

    bool changed = false;
    if (seg) {
        int s = *sel;
        changed = segmented("##seg", items, n, &s, w, o.itemTips);
        if (changed) *sel = s;
        if (!o.itemTips) valueTip(o);
    } else {
        changed = comboCore("##combo", sel, n, arrayItem, items, w, o);
    }
    fieldEnd();
    return changed;
}

bool fieldCombo(const char* id, const char* label, int* sel, const std::vector<std::string>& items,
                const FieldOpts& o) {
    if (!sel) return false;
    // Data-derived lists can be long: the need is the CLOSED text (preview or selection), so the
    // group stacks when the shown value would elide inline.
    const int   n     = (int)items.size();
    const char* shown = (o.preview && o.preview[0]) ? o.preview
                        : (*sel >= 0 && *sel < n)    ? items[(std::size_t)*sel].c_str()
                                                     : (o.hint ? o.hint : "");
    const float w = beginField(id, label, o, FieldKind{true, comboNeed(ImGui::CalcTextSize(shown).x)});
    const bool  changed = comboCore("##combo", sel, n, vectorItem, &items, w, o);
    fieldEnd();
    return changed;
}

}  // namespace kit
}  // namespace ui
