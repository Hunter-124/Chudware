// PropertyManager implementation (C17).
//
// Draws the active feature form in the FeatureManager dock slot, routes viewport picks into the
// active selector well, debounces live previews through doc_apply_ops (add or edit_feature), and
// commits (OK) / rolls back (Cancel via worker undo of each preview batch).
#include <algorithm>
#include <cfloat>
#include <cmath>
#include <cstdint>
#include <cstdarg>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

#include "imgui.h"
#include "imgui_internal.h"   // ImRect (ui::adaptivePanel rect argument)

#include "property_manager.h"
#include "icons.h"
#include "widgets/kit_form.h"  // display-only dimension formatting for viewport callouts
#include "sketch_mode.h"   // C38 (P1f): SketchMode::active() — sketch owns Esc while editing
#include "../ui_theme.h"   // liquid glass v2: adaptivePanel / glassRowBackground / AdaptiveSurface

#include "../core/app_state.h"
#include "../core/doc_mirror.h"
#include "engine_backend.h"   // Phase 4: daemon edit_hold/edit_release (services/worker)
#include "ipc_client.h"       // services/ipc EngineClient
#include "../render/id_pass.h"
#include "../renderer.h"     // Renderer::refDatums() for axis.auto geometry lookup
#include "../vec_math.h"     // W2-ref: explicit auto-axis geometry (Vec3 / cross / normalize)

namespace ui {

// C37-F: Debounce window for live preview (was 0.40 s; reduced for snappier feel).
static const double kDebounceSec = 0.25;
// A live drag/keystroke preview is disposable: cap its engine round-trip so a pathological or
// very slow rebuild can't freeze the form — or machine-gun the shared daemon — for the full 120 s
// default. On timeout the preview drops to an inline error and the GUI stays responsive; the
// OK/commit path (committing_) keeps the patient default so a real commit never aborts early.
static const int kPreviewTimeoutMs = 20000;

// ---- Phase 4: daemon edit-hold helpers (no-ops in legacy in-process mode) ----
// Take/refresh the exclusive edit-hold on `docId` so foreign clients (MCP
// agents) get a retriable busy_preview while this form previews. Returns false
// only on a busy rejection (another client holds it).
static bool pmEditHold(const std::string& docId, std::string* holderOut = nullptr) {
    if (!engine::daemonMode() || docId.empty()) return true;
    auto* c = engine::client();
    if (!c) return true;
    nlohmann::json r; std::string e;
    if (c->call("edit_hold", {{"doc_id", docId}}, r, e)) return true;
    if (r.is_object() && r.value("code", std::string()) == "busy_preview") {
        if (holderOut) *holderOut = r.value("holder", std::string("another client"));
        return false;
    }
    return true;  // transport hiccup: don't block the human's edit
}
static void pmEditRelease(const std::string& docId) {
    if (!engine::daemonMode() || docId.empty()) return;
    if (auto* c = engine::client()) {
        nlohmann::json r; std::string e;
        c->call("edit_release", {{"doc_id", docId}}, r, e);
    }
}
// Open-time hold: busy is surfaced as a warning toast but does NOT block the
// form (human steering wins; the CAS overlap guard covers a real race). The
// wording mirrors the doc.lease_change "held" toast (engine_events.cpp) — same
// daemon lease state, so it names the holder AND says the lock is temporary
// rather than reading as a permanent refusal.
static std::string pmTakeOpenHold(AppState& st) {
    if (!engine::daemonMode() || !st.docMirror.hasDoc()) return std::string();
    const std::string docId = st.docMirror.docId();
    std::string holder;
    if (!pmEditHold(docId, &holder)) {
        st.toast(ui::ToastLevel::Warn,
                 "An " + holder + " client is editing this part — your edit is not blocked, "
                 "but its lock is briefly held and previews may conflict");
        return std::string();
    }
    return docId;
}

// ---- field enable helpers -------------------------------------------------

// Return the param-value string currently selected by a Combo field with the given key.
static std::string comboValue(const std::vector<PMField>& fields, const char* key) {
    for (const auto& f : fields) {
        if (f.def->kind != PMFieldKind::Combo || std::strcmp(f.def->key, key) != 0) continue;
        std::string opts = f.def->comboOptions ? f.def->comboOptions : "";
        int cur = 0; size_t start = 0;
        while (start <= opts.size()) {
            size_t bar = opts.find('|', start);
            std::string opt = opts.substr(start, bar - start);
            if (cur == f.comboIndex) return opt;
            ++cur;
            if (bar == std::string::npos) break;
            start = bar + 1;
        }
    }
    return {};
}

// Return true when the field's enableKey/enableVals condition is satisfied (always true when
// enableKey is null). comingSoon fields are never enabled for interaction.
// The gating value of the field named `key`, as a string token: a Combo yields its selected
// option value; a Check yields "true"/"false". Lets a field be gated (enableKey/enableVals) on
// either a sibling combo OR a sibling checkbox (e.g. thin-feature thickness on the "thin" check).
static std::string enableSourceValue(const std::vector<PMField>& fields, const char* key) {
    std::string c = comboValue(fields, key);
    if (!c.empty()) return c;
    for (const auto& f : fields)
        if (f.def->kind == PMFieldKind::Check && std::strcmp(f.def->key, key) == 0)
            return f.checkValue ? "true" : "false";
    return c;
}

static bool fieldEnabled(const PMFieldDef& def, const std::vector<PMField>& fields) {
    if (def.comingSoon) return false;
    if (!def.enableKey)  return true;
    std::string cur = enableSourceValue(fields, def.enableKey);
    std::string allowed = def.enableVals ? def.enableVals : "";
    if (allowed.empty()) return true;
    // Check if cur appears as a token in the pipe-separated allowed list.
    size_t start = 0;
    while (start <= allowed.size()) {
        size_t bar = allowed.find('|', start);
        std::string tok = allowed.substr(start, bar - start);
        if (tok == cur) return true;
        if (bar == std::string::npos) break;
        start = bar + 1;
    }
    return false;
}

static std::vector<std::string> comboOptionsList(const PMFieldDef& def) {
    std::vector<std::string> out;
    std::string opts = def.comboOptions ? def.comboOptions : "";
    size_t start = 0;
    while (start <= opts.size()) {
        size_t bar = opts.find('|', start);
        out.push_back(opts.substr(start, bar - start));
        if (bar == std::string::npos) break;
        start = bar + 1;
    }
    return out;
}

static bool hasString(const std::vector<std::string>& xs, const char* key) {
    if (!key) return false;
    for (const auto& x : xs)
        if (x == key) return true;
    return false;
}

static const char* aiKindName(PMFieldKind k) {
    switch (k) {
        case PMFieldKind::Dim: return "dim";
        case PMFieldKind::SelectorWell: return "selector";
        case PMFieldKind::Combo: return "combo";
        case PMFieldKind::Check: return "check";
        case PMFieldKind::Int: return "int";
        case PMFieldKind::DatumRef: return "datum_ref";
        case PMFieldKind::FeatureWell: return "feature_well";
        case PMFieldKind::Text: return "text";
    }
    return "unknown";
}

// Conditional fields are hidden entirely until their controlling combo makes them relevant.
// This keeps reference wells/offsets out of the user's way and prevents inactive params/refs from
// being serialized into preview ops. comingSoon rows remain visible but disabled when ungated.
static bool fieldVisible(const PMFieldDef& def, const std::vector<PMField>& fields) {
    if (!def.enableKey) return true;
    return fieldEnabled(def, fields);
}

// ---- dim parsing ----------------------------------------------------------
// SolidWorks-style unit math (C50c): a dim field accepts a full arithmetic expression with
// per-literal unit suffixes — "100mm+2in/0.5m", "(3+1/8)in", "25.4/2", "0.5m-3cm". Each unit
// literal converts to canonical units (mm; deg for angle fields) at the literal, with standard
// */ over +- precedence and parentheses. ONE SolidWorks-compatible exception: when the whole
// input carries exactly one unit and it is the trailing token, the unit applies to the entire
// expression ("1/2in" = half an inch, not 1 ÷ 50.8mm). Bare numbers = document units (MMGS).

namespace {

struct DimTok {
    enum Kind { Num, Op, LParen, RParen, End } kind = End;
    double value = 0.0;     // Num: literal already converted to canonical units
    bool   hadUnit = false; // Num: a unit suffix was attached
    char   op = 0;          // Op: + - * /
};

// Longest-match unit table. Factor converts to mm (length) or deg (angle).
struct DimUnit { const char* name; double factor; bool angle; };
static const DimUnit kDimUnits[] = {
    {"thou", 0.0254, false}, {"mil", 0.0254, false},
    {"deg", 1.0, true},      {"rad", 57.29577951308232, true},
    {"\xc2\xb0", 1.0, true},                                      // °
    {"mm", 1.0, false},  {"cm", 10.0, false}, {"um", 0.001, false},
    {"\xc2\xb5m", 0.001, false},                                  // µm
    {"ft", 304.8, false}, {"in", 25.4, false},
    {"m", 1000.0, false}, {"\"", 25.4, false}, {"'", 304.8, false},
};

// Tokenize a de-spaced, lowercased dim string. Returns false with err on a bad token.
// unitCount/lastIsUnit report unit usage for the trailing-unit-applies-to-all rule.
// bareLengthScale: canonical-mm value of one unit-less LENGTH literal (1.0 = MMGS, 25.4 = IPS).
// A bare length literal (angle=false, no suffix) is scaled by it so "1.5" honours the doc unit;
// literals with an explicit suffix and all angle values are unaffected.
static bool dimTokenize(const std::string& s, bool angle, std::vector<DimTok>& out,
                        int& unitCount, bool& endsWithUnit, std::string& err,
                        double bareLengthScale = 1.0) {
    unitCount = 0; endsWithUnit = false;
    size_t i = 0;
    while (i < s.size()) {
        char c = s[i];
        if (c == '+' || c == '-' || c == '*' || c == '/') {
            DimTok t; t.kind = DimTok::Op; t.op = c; out.push_back(t); ++i; continue;
        }
        if (c == '(') { out.push_back({DimTok::LParen}); ++i; continue; }
        if (c == ')') {
            out.push_back({DimTok::RParen}); ++i; endsWithUnit = false;
            // A unit directly after a group scales the group: "(3+1/8)in" -> (3+1/8)*25.4.
            for (const auto& u : kDimUnits) {
                size_t n = std::strlen(u.name);
                if (s.compare(i, n, u.name) != 0) continue;
                if (i + n < s.size() && std::isalpha((unsigned char)s[i + n])) continue;
                if (u.angle != angle) {
                    err = angle ? "length unit in an angle value"
                                : "angle unit in a length value";
                    return false;
                }
                DimTok mul; mul.kind = DimTok::Op; mul.op = '*'; out.push_back(mul);
                DimTok f; f.kind = DimTok::Num; f.value = u.factor; f.hadUnit = true;
                out.push_back(f);
                ++unitCount;
                endsWithUnit = true;
                i += n;
                break;
            }
            continue;
        }
        if (std::isdigit((unsigned char)c) || c == '.') {
            size_t pos = 0;
            double v = 0;
            try { v = std::stod(s.substr(i), &pos); }
            catch (...) { err = "bad number"; return false; }
            i += pos;
            DimTok t; t.kind = DimTok::Num; t.value = v;
            // unit suffix? (longest match wins)
            for (const auto& u : kDimUnits) {
                size_t n = std::strlen(u.name);
                if (s.compare(i, n, u.name) != 0) continue;
                // "m" must not eat the m of a following identifier — units are only ever
                // followed by an operator/paren/end, so a letter after the match means a
                // longer (unknown) suffix; keep scanning the table for the longer names
                // first (table order) and reject leftover letters below.
                if (i + n < s.size() && std::isalpha((unsigned char)s[i + n])) continue;
                if (u.angle != angle) {
                    err = angle ? "length unit in an angle value"
                                : "angle unit in a length value";
                    return false;
                }
                t.value *= u.factor;
                t.hadUnit = true;
                ++unitCount;
                i += n;
                break;
            }
            // Bare (unit-less) LENGTH literal: interpret in the document's active length unit
            // (e.g. IPS -> a bare "1.5" is 1.5 in = 38.1 mm). Angles ignore this (degrees in both).
            if (!t.hadUnit && !angle && bareLengthScale != 1.0) t.value *= bareLengthScale;
            endsWithUnit = t.hadUnit;
            out.push_back(t);
            continue;
        }
        err = std::string("unexpected '") + c + "'";
        return false;
    }
    out.push_back({DimTok::End});
    return true;
}

// Recursive-descent evaluation: expr := term (('+'|'-') term)*, term := unary (('*'|'/') unary)*,
// unary := '-' unary | num | '(' expr ')'.
struct DimEval {
    const std::vector<DimTok>& t;
    size_t i = 0;
    std::string err;
    double expr() {
        double v = term();
        while (err.empty() && t[i].kind == DimTok::Op && (t[i].op == '+' || t[i].op == '-')) {
            char op = t[i].op; ++i;
            double r = term();
            v = (op == '+') ? v + r : v - r;
        }
        return v;
    }
    double term() {
        double v = unary();
        while (err.empty() && t[i].kind == DimTok::Op && (t[i].op == '*' || t[i].op == '/')) {
            char op = t[i].op; ++i;
            double r = unary();
            if (op == '/') {
                if (std::fabs(r) < 1e-30) { err = "division by zero"; return 0; }
                v /= r;
            } else v *= r;
        }
        return v;
    }
    double unary() {
        if (t[i].kind == DimTok::Op && t[i].op == '-') { ++i; return -unary(); }
        if (t[i].kind == DimTok::Op && t[i].op == '+') { ++i; return unary(); }
        if (t[i].kind == DimTok::Num) return t[i++].value;
        if (t[i].kind == DimTok::LParen) {
            ++i;
            double v = expr();
            if (err.empty() && t[i].kind != DimTok::RParen) { err = "missing ')'"; return 0; }
            ++i;
            return v;
        }
        err = "expected a number";
        return 0;
    }
};

}  // namespace

bool pmParseDim(const std::string& raw, double& outMm, std::string& err, bool angle,
                double bareLengthScale) {
    err.clear();
    std::string s;
    for (char c : raw) if (!std::isspace((unsigned char)c)) s += c;
    if (s.empty()) { err = "empty"; return false; }
    for (auto& c : s) c = (char)std::tolower((unsigned char)c);

    // Trailing-unit-applies-to-all: exactly one unit in the input AND it is the final token
    // ("1/2in", "(3+5)/2 in"). Strip it, evaluate bare, scale the result. Inputs with units on
    // interior literals ("100mm+2in/0.5m") fall through to per-literal conversion.
    double wholeScale = 1.0;
    {
        std::vector<DimTok> probe;
        int unitCount = 0; bool endsWithUnit = false; std::string perr;
        if (dimTokenize(s, angle, probe, unitCount, endsWithUnit, perr)) {
            if (unitCount == 1 && endsWithUnit) {
                for (const auto& u : kDimUnits) {
                    size_t n = std::strlen(u.name);
                    if (u.angle == angle && s.size() > n &&
                        s.compare(s.size() - n, n, u.name) == 0) {
                        wholeScale = u.factor;
                        s.erase(s.size() - n);
                        break;
                    }
                }
            } else if (unitCount == 0 && !angle && bareLengthScale != 1.0) {
                // No explicit units anywhere: the WHOLE expression is a length in the document
                // unit, so scale the final RESULT — not each literal. Per-literal scaling only
                // distributes over +/- ; across '*' it double-counts (".75*0.5" -> x25.4^2) and
                // across '/' it cancels (".75/2" -> x1), which is the IPS dim-math bug.
                wholeScale = bareLengthScale;
            }
        }
    }

    // When an explicit trailing unit governs the whole expression (wholeScale != 1.0), the bare
    // literals that remain are scaled by that unit, NOT by the document default — so suppress the
    // document bare-unit interpretation in that path.
    const double bareScale = (wholeScale != 1.0) ? 1.0 : bareLengthScale;

    std::vector<DimTok> toks;
    int unitCount = 0; bool endsWithUnit = false;
    if (!dimTokenize(s, angle, toks, unitCount, endsWithUnit, err, bareScale)) return false;
    if (toks.size() <= 1) { err = "empty"; return false; }

    DimEval ev{toks};
    double v = ev.expr();
    if (!ev.err.empty()) { err = ev.err; return false; }
    if (ev.t[ev.i].kind != DimTok::End) { err = "trailing input"; return false; }
    if (!std::isfinite(v)) { err = "not a number"; return false; }
    outMm = v * wholeScale;
    return true;
}

std::string pmFormatDim(double mm, bool ips, bool angle) {
    char buf[48];
    if (angle)    std::snprintf(buf, sizeof(buf), "%.4gdeg", mm);  // value already in degrees
    else if (ips) std::snprintf(buf, sizeof(buf), "%.4gin", mm / 25.4);
    else          std::snprintf(buf, sizeof(buf), "%.4gmm", mm);
    return buf;
}

std::string pmFormatArea(double mm2, bool ips) {
    char buf[48];
    if (ips) std::snprintf(buf, sizeof(buf), "%.4gin\xc2\xb2", mm2 / 645.16);
    else     std::snprintf(buf, sizeof(buf), "%.4gmm\xc2\xb2", mm2);
    return buf;
}
std::string pmFormatVolume(double mm3, bool ips) {
    char buf[48];
    if (ips) std::snprintf(buf, sizeof(buf), "%.4gin\xc2\xb3", mm3 / 16387.064);
    else     std::snprintf(buf, sizeof(buf), "%.4gmm\xc2\xb3", mm3);
    return buf;
}
std::string pmFormatMass(double g, bool ips) {
    char buf[48];
    if (ips) std::snprintf(buf, sizeof(buf), "%.4glb", g / 453.59237);
    else     std::snprintf(buf, sizeof(buf), "%.4gg", g);
    return buf;
}
std::string pmFormatAreaMoment(double mm4, bool ips) {
    char buf[48];
    // 25.4^4 = 416231.4256
    if (ips) std::snprintf(buf, sizeof(buf), "%.4gin\xe2\x81\xb4", mm4 / 416231.4256);
    else     std::snprintf(buf, sizeof(buf), "%.4gmm\xe2\x81\xb4", mm4);
    return buf;
}
// Mass moment of inertia (canonical g·mm²): IPS -> lb·in², MMGS -> g·mm². 453.59237 g/lb,
// 645.16 mm²/in² -> g·mm² / (453.59237*645.16) = g·mm² / 292639.66 = lb·in².
std::string pmFormatMassMoment(double g_mm2, bool ips) {
    char buf[48];
    if (ips) std::snprintf(buf, sizeof(buf), "%.4glb\xc2\xb7in\xc2\xb2", g_mm2 / 292639.66);
    else     std::snprintf(buf, sizeof(buf), "%.4gg\xc2\xb7mm\xc2\xb2", g_mm2);
    return buf;
}


// An angle-valued Dim field is tagged by a "deg" unit label (Angle/AngleIf helpers). Its value
// is stored in degrees and must NOT carry the mm/inch length suffix.
static inline bool pmFieldIsAngle(const PMFieldDef* d) {
    return d && d->unitLabel && std::strstr(d->unitLabel, "deg") != nullptr;
}
// A unit-less Dim (DimU: ratio/k-factor/twist turns) — empty unit label. Its value is a plain
// number with NO length suffix and is never IPS-scaled.
static inline bool pmFieldUnitless(const PMFieldDef* d) {
    return d && d->unitLabel && d->unitLabel[0] == '\0';
}
// Format a Dim field's canonical value for its edit buffer / display, honouring the field's unit:
// angle -> "<n>deg", unit-less -> bare "<n>", otherwise length via pmFormatDim (mm / inch).
static std::string pmFieldValueText(const PMFieldDef* d, double v, bool ips) {
    if (pmFieldIsAngle(d)) return pmFormatDim(v, ips, /*angle=*/true);
    if (pmFieldUnitless(d)) { char b[48]; std::snprintf(b, sizeof(b), "%.4g", v); return b; }
    return pmFormatDim(v, ips, /*angle=*/false);
}

// A dim value object {value, expr} for an op.
static nlohmann::json dimJson(const PMField& f) {
    nlohmann::json d;
    if (!f.dimExpr.empty()) {
        // Bound to an expression: value carries the last resolved number (or 0); expr is the bind.
        d["value"] = f.dimValue;
        d["expr"]  = f.dimExpr;
    } else {
        d["value"] = f.dimValue;
        d["expr"]  = nullptr;
    }
    return d;
}

// ---- open / close ---------------------------------------------------------

// Index of `value` among a combo def's '|'-separated option values (-1 if absent).
static int pmComboIndexOf(const PMFieldDef& d, const std::string& value) {
    const std::string opts = d.comboOptions ? d.comboOptions : "";
    int cur = 0; size_t start = 0;
    while (start <= opts.size()) {
        size_t bar = opts.find('|', start);
        if (opts.substr(start, bar - start) == value) return cur;
        ++cur;
        if (bar == std::string::npos) break;
        start = bar + 1;
    }
    return -1;
}

// Thread auto-size: standard sizes the thread form's `size` combo offers, with the
// nominal major Ø + coarse pitch (mm) used to match a picked cylinder's diameter.
// Coarse-series only — fine variants share the major Ø, so the coarse default wins
// (SolidWorks behaviour); the user can still switch to fine in the dropdown.
struct PMThreadSize { const char* value; double major; double pitch; bool metric; };
static const PMThreadSize kPmThreadSizes[] = {
    {"M3", 3.0, 0.50, true},   {"M4", 4.0, 0.70, true},   {"M5", 5.0, 0.80, true},
    {"M6", 6.0, 1.00, true},   {"M8", 8.0, 1.25, true},   {"M10", 10.0, 1.50, true},
    {"M12", 12.0, 1.75, true}, {"M16", 16.0, 2.00, true}, {"M20", 20.0, 2.50, true},
    {"M24", 24.0, 3.00, true},
    {"#10-24", 4.826, 1.05833, false},  {"1/4-20", 6.350, 1.27000, false},
    {"5/16-18", 7.938, 1.41111, false}, {"3/8-16", 9.525, 1.58750, false},
    {"1/2-13", 12.700, 1.95385, false},
};

// Unit-system-aware default (canonical mm). Imperial is authored as clean inches, then
// converted to mm once at form open — NEVER metric-default / 25.4 (which yields 0.3937").
static double pmUnitDefaultMm(double metricMm, double imperialIn, bool ips) {
    return ips ? (imperialIn * 25.4) : metricMm;
}

// Clean native imperial defaults (inches) for PM Dim fields, keyed by feature type + param.
// Returns <0 when the field has no IPS-specific default (use the form's metric dimDefault).
// Angles / unit-less / counts are intentionally absent — they are unit-agnostic.
static double pmImperialDefaultInches(const char* type, const char* key) {
    if (!type || !key) return -1.0;
    struct Entry { const char* type; const char* key; double inches; };
    // Keep in sync with the Dim defaults in pm_forms.cpp (metric stays as authored there).
    static const Entry kTab[] = {
        // primitives
        {"box",            "length",             0.75},
        {"box",            "width",              0.75},
        {"box",            "height",             0.75},
        {"cylinder",       "radius",             0.5},
        {"cylinder",       "height",             0.75},
        // extrude / revolve thin
        {"extrude",        "depth",              0.25},
        {"extrude",        "depth2",             0.25},
        {"extrude",        "thin_thickness",     0.125},
        {"revolve",        "thin_thickness",     0.125},
        // dress-up
        {"fillet",         "radius",             0.125},
        {"fillet",         "radius2",            0.125},
        {"chamfer",        "length",             0.0625},
        {"chamfer",        "length2",            0.0625},
        {"shell",          "thickness",          0.125},
        // hole / thread
        {"hole",           "diameter",           0.25},
        {"hole",           "depth",              0.25},
        {"hole",           "dx",                 0.75},
        {"hole",           "dy",                 0.75},
        {"thread",         "major_dia",          0.5},
        {"thread",         "pitch",              0.05},
        {"thread",         "length",             0.5},
        // patterns
        {"linear_pattern", "spacing1",           0.75},
        {"linear_pattern", "spacing2",           0.75},
        // helix
        {"helix",          "radius",             0.5},
        {"helix",          "pitch",              0.25},
        {"helix",          "height",             0.75},
        // datum plane
        {"plane",          "offset",             0.25},
        // sheet metal
        {"sm_base_flange", "thickness",          0.0625},
        {"sm_base_flange", "default_bend_radius",0.0625},
        {"sm_edge_flange", "length",             0.75},
        {"sm_edge_flange", "radius",             0.0625},
        {"sm_hem",         "length",             0.25},
        {"sm_hem",         "radius",             0.0625},
        {"sm_jog",         "offset",             0.25},
        {"sm_jog",         "length",             0.75},
        {"sm_jog",         "radius",             0.0625},
        // weldments
        {"weld_member",    "size",               0.75},
        {"weld_member",    "w",                  1.5},
        {"weld_member",    "h",                  0.75},
        {"weld_member",    "od",                 1.0},
        {"weld_member",    "leg",                1.0},
        {"weld_member",    "thickness",          0.125},
        {"weld_gusset",    "thickness",          0.25},
        {"weld_gusset",    "d1",                 1.25},
        {"weld_gusset",    "d2",                 1.25},
        {"weld_end_cap",   "thickness",          0.25},
        // rib
        {"rib",            "thickness",          0.125},
        // surfacing
        {"surf_extrude",   "depth",              0.25},
        {"surf_offset",    "distance",           0.25},
        {"surf_thicken",   "thickness",          0.125},
    };
    for (const auto& e : kTab) {
        if (std::strcmp(e.type, type) == 0 && std::strcmp(e.key, key) == 0)
            return e.inches;
    }
    return -1.0;
}

// After initField seeded metric dimDefault, swap in native imperial defaults when IPS is active.
static void applyIpsNativeDefaults(const PMFormDef* form, std::vector<PMField>& fields) {
    if (!form || !form->type) return;
    for (auto& f : fields) {
        if (!f.def || f.def->kind != PMFieldKind::Dim) continue;
        if (pmFieldIsAngle(f.def) || pmFieldUnitless(f.def)) continue;
        const double inches = pmImperialDefaultInches(form->type, f.def->key);
        if (inches < 0.0) continue;
        f.dimValue = pmUnitDefaultMm(f.def->dimDefault, inches, /*ips=*/true);
        std::string disp = pmFieldValueText(f.def, f.dimValue, /*ips=*/true);
        std::strncpy(f.dimBuf.data(), disp.c_str(), f.dimBuf.size() - 1);
        f.dimBuf[f.dimBuf.size() - 1] = '\0';
    }
}

static void initField(PMField& f, const PMFieldDef& d, bool ips) {
    f.def = &d;
    switch (d.kind) {
        case PMFieldKind::Dim: {
            f.dimValue = d.dimDefault;
            f.dimExpr.clear();
            f.dimTextEntry = false;
            std::string disp = pmFieldValueText(&d, d.dimDefault, ips);
            std::strncpy(f.dimBuf.data(), disp.c_str(), f.dimBuf.size() - 1);
            break;
        }
        case PMFieldKind::Combo:   f.comboIndex = 0; break;
        case PMFieldKind::DatumRef: f.comboIndex = 0; break;
        case PMFieldKind::Check:   f.checkValue = d.checkDefault; break;
        case PMFieldKind::Int:     f.intValue = (int)(d.dimDefault + 0.5); break;
        case PMFieldKind::Text:    f.textBuf.assign(128, 0); break;
        case PMFieldKind::SelectorWell:
        case PMFieldKind::FeatureWell: f.picks.clear(); f.wellActive = false; break;
    }
}

bool PropertyManager::openForCommand(AppState& st, const std::string& cmdId) {
    std::string mode;
    const PMFormDef* form = pmFormForCommand(cmdId, mode);
    if (!form) return false;
    if (st.activeDocLockedByAgent()) {
        st.toast(ui::ToastLevel::Warn, st.agentLockReason(st.docMirror.docId()));
        return true;  // consume the recognized command without opening a form
    }
    if (guestActive_) closeGuest();
    if (active_ && !cancelEdits(st)) return false;   // replacing a PM must roll back previews first

    // C40: SolidWorks mode exclusion. Invoking a feature command while a sketch is being
    // edited EXITS the sketch first (its profile then pre-seeds extrude/revolve below);
    // an armed plane-pick is cancelled — a feature PM and the pick overlay must never
    // coexist (they fight over the viewport mouse).
    std::string justExitedSketch;
    if (st.sketchMode && st.sketchMode->active()) {
        justExitedSketch = st.sketchMode->sketchId();
        st.sketchMode->exitSketch(st, /*keep=*/true);
    }
    st.sketchPlanePickActive = false;

    active_ = true;
    editMode_ = false;
    form_ = form;
    mode_ = mode;
    title_ = form->title;
    targetId_.clear();
    previewBatches_ = 0;
    previewUndoGroupStarted_ = false;
    dirty_ = false;
    firstPreviewDone_ = false;
    previewInFlight_ = false;
    lastEditTime_ = ImGui::GetTime();
    previewError_.clear();
    focusedOnOpen_ = false;
    wantFocusFirstField_ = true;  // C37-F: focus first field on next draw
    aiBusy_ = false;
    aiMessage_.clear();
    aiHighlights_.clear();
    ++aiRequestSeq_;

    // C37-F: Take a checkpoint BEFORE any preview, so Cancel can revert with a single call.
    checkpointId_ = st.docMirror.hasDoc() ? st.docMirror.checkpoint(st.worker) : std::string();
    if (checkpointId_.empty())
        st.addLog("[pm] checkpoint failed — Cancel will use undo loop fallback");
    // Phase 4: exclusive daemon edit-hold while this form previews (released in close()).
    holdDocId_ = pmTakeOpenHold(st);
    sessionDocId_ = st.docMirror.docId();
    lastHoldTime_ = ImGui::GetTime();

    ips_ = st.ips();   // document-unit display follows the active unit system (MMGS / IPS)
    fields_.clear();
    fields_.resize(form->fields.size());
    for (size_t i = 0; i < form->fields.size(); ++i)
        initField(fields_[i], form->fields[i], ips_);
    // IPS: replace metric-seeded Dim defaults with clean native imperial values (0.25 in, not
    // 0.3937 in from 10 mm / 25.4). Metric mode keeps the form table defaults unchanged.
    if (ips_) applyIpsNativeDefaults(form_, fields_);


    // Thread form in an IPS document defaults to an SAE size (1/4-20 UNC) instead of
    // the metric first option; metric docs keep the M-series default (combo index 0).
    if (ips_ && form_->type && std::string(form_->type) == "thread") {
        for (auto& f : fields_) {
            if (f.def->kind != PMFieldKind::Combo || std::strcmp(f.def->key, "size") != 0)
                continue;
            int idx = pmComboIndexOf(*f.def, "1/4-20");
            if (idx >= 0) f.comboIndex = idx;
            break;
        }
    }

    // If a mode/op combo exists, preselect it from the command (boss/cut split button; boolean
    // combine/subtract/intersect). Match mode_ against the combo's option VALUES so any N-way combo
    // works (not just boss/cut), falling back to index 0 (legacy "cut" -> 1).
    if (!mode_.empty()) {
        for (auto& f : fields_) {
            if (f.def->kind != PMFieldKind::Combo) continue;
            if (std::strcmp(f.def->key, "mode") != 0 && std::strcmp(f.def->key, "op") != 0) continue;
            const std::string opts = f.def->comboOptions ? f.def->comboOptions : "";
            int idx = -1, cur = 0; size_t start = 0;
            while (start <= opts.size()) {
                size_t bar = opts.find('|', start);
                if (opts.substr(start, bar - start) == mode_) { idx = cur; break; }
                ++cur;
                if (bar == std::string::npos) break;
                start = bar + 1;
            }
            f.comboIndex = (idx >= 0) ? idx : ((mode_ == "cut") ? 1 : 0);
        }
    }

    // First selector well becomes active so the user can pick immediately.
    for (auto& f : fields_)
        if (f.def->kind == PMFieldKind::SelectorWell) { f.wellActive = true; break; }

    // Seed any already-resolved viewport selection into the first well (SolidWorks pre-select).
    PMField* well = activeWell();
    if (well) {
        for (const auto& it : st.selection.items()) {
            if (it.pending_resolve || it.selector.empty()) continue;
            try {
                nlohmann::json selObj = nlohmann::json::parse(it.selector);
                int k = (int)it.hit.kind;
                routePick(selObj, k);
            } catch (...) {}
        }
    }

    // C40/C51: SolidWorks pre-select for extrude/revolve/sweep — the sketch chosen in the
    // FeatureManager (or, failing that, the most recent non-empty sketch) seeds the Profile well
    // as a WHOLE-SKETCH region ({kind:region, sketch:fid} with no centroid/index). Whole-sketch
    // resolves to all material (even-odd) regions, so a circle-in-a-square previews as a washer and
    // the old fragile centroid match (which produced spurious "dangling reference" errors on
    // concentric loops) is gone. The viewport region picker then lets the user narrow to specific
    // regions. Loft is NOT seeded — its sections are chosen explicitly (needs 2+, ordered).
    if (std::strcmp(form_->type, "extrude") == 0 || std::strcmp(form_->type, "revolve") == 0 ||
        std::strcmp(form_->type, "sweep") == 0) {
        PMField* prof = profileWell();
        const nlohmann::json& tree = st.docMirror.tree();
        if (prof && prof->picks.empty() && tree.is_object() &&
            tree.contains("features") && tree["features"].is_array()) {
            const nlohmann::json* pick = nullptr;   // just-exited/selected sketch wins, else last
            for (const auto& f : tree["features"]) {
                if (!f.is_object() || f.value("type", std::string()) != "sketch") continue;
                const auto& ents = f.contains("params") ? f["params"].value("entities", nlohmann::json::array())
                                                        : nlohmann::json::array();
                if (!ents.is_array() || ents.empty()) continue;   // empty sketch: nothing to extrude
                const std::string fidv = f.value("id", std::string());
                if (fidv == justExitedSketch || fidv == st.selectedFeatureId) { pick = &f; break; }
                pick = &f;
            }
            if (pick) {
                PMSelEntry se;
                se.selector = {{"kind", "region"}, {"sketch", pick->value("id", std::string())}};
                se.label = "Whole sketch";
                prof->picks.push_back(std::move(se));
                dirty_ = true;   // schedule the live preview against the seeded whole-sketch profile
                lastEditTime_ = ImGui::GetTime();
                st.addLog("[pm] profile seeded (whole sketch) from " + pick->value("id", std::string()));
            }
        }
    }

    // Pattern/mirror: seed the "features" FeatureWell from the FeatureManager selection so the
    // command operates immediately on the selected feature (SolidWorks pre-select). A sketch/datum
    // selection is skipped (nothing to replicate); an empty list leaves mirror as a whole-body
    // mirror and the patterns waiting for an "Add selected" pick.
    if (std::strcmp(form_->type, "mirror") == 0 ||
        std::strcmp(form_->type, "linear_pattern") == 0 ||
        std::strcmp(form_->type, "circular_pattern") == 0) {
        const std::string& sfid = st.selectedFeatureId;
        if (!sfid.empty()) {
            const nlohmann::json& tree = st.docMirror.tree();
            std::string nm = sfid, ftype;
            if (tree.is_object() && tree.contains("features") && tree["features"].is_array())
                for (const auto& tf : tree["features"])
                    if (tf.is_object() && tf.value("id", std::string()) == sfid) {
                        nm = tf.value("name", sfid); ftype = tf.value("type", std::string()); break;
                    }
            const bool replicable = !(ftype == "sketch" || ftype == "plane" || ftype == "axis" ||
                                      ftype == "point" || ftype == "csys" || ftype.empty());
            if (replicable)
                for (auto& fld : fields_)
                    if (fld.def->kind == PMFieldKind::FeatureWell) {
                        PMSelEntry e; e.selector = sfid; e.label = nm;
                        fld.picks.push_back(std::move(e));
                        dirty_ = true; lastEditTime_ = ImGui::GetTime();
                        break;
                    }
        }
    }

    // Weldments: seed the single "sketch" FeatureWell from a tree-selected SKETCH so a path picked
    // before the command auto-fills (SolidWorks pre-select). Non-sketch selections are ignored —
    // the user picks the path sketch in the FeatureManager then clicks "Add selected".
    if (std::strcmp(form_->type, "weld_member") == 0) {
        const std::string& sfid = st.selectedFeatureId;
        if (!sfid.empty()) {
            const nlohmann::json& tree = st.docMirror.tree();
            std::string nm = sfid, ftype;
            if (tree.is_object() && tree.contains("features") && tree["features"].is_array())
                for (const auto& tf : tree["features"])
                    if (tf.is_object() && tf.value("id", std::string()) == sfid) {
                        nm = tf.value("name", sfid); ftype = tf.value("type", std::string()); break;
                    }
            if (ftype == "sketch")
                for (auto& fld : fields_)
                    if (fld.def->kind == PMFieldKind::FeatureWell &&
                        std::strcmp(fld.def->key, "sketch") == 0) {
                        PMSelEntry e; e.selector = sfid; e.label = nm;
                        fld.picks.push_back(std::move(e));
                        dirty_ = true; lastEditTime_ = ImGui::GetTime();
                        break;
                    }
        }
    }

    // Plane pre-select (SolidWorks "select a plane, then run the command"): if a plane is selected —
    // a default origin plane ("plane.front/top/right") or a created datum-plane feature — seed the
    // form's first empty plane-accepting well with it (ref-plane base, mirror plane, draft neutral,
    // helix base, …). Routed as a BARE string, matching routeDatumPlane / the engine's datum resolve.
    {
        const std::string& sfid = st.selectedFeatureId;
        std::string label;
        bool isPlane = false;
        if (sfid == "plane.front" || sfid == "plane.top" || sfid == "plane.right") {
            isPlane = true;
            label = sfid.substr(sfid.rfind('.') + 1);
            if (!label.empty()) label[0] = (char)std::toupper((unsigned char)label[0]);
        } else if (!sfid.empty()) {
            const nlohmann::json& tree = st.docMirror.tree();
            if (tree.is_object() && tree.contains("features") && tree["features"].is_array())
                for (const auto& tf : tree["features"])
                    if (tf.is_object() && tf.value("id", std::string()) == sfid &&
                        tf.value("type", std::string()) == "plane") {
                        isPlane = true; label = tf.value("name", sfid); break;
                    }
        }
        if (isPlane)
            for (auto& fld : fields_)
                if (fld.def->kind == PMFieldKind::SelectorWell && fld.def->selKinds.plane &&
                    fld.picks.empty() && fieldVisible(*fld.def, fields_)) {
                    PMSelEntry e; e.selector = sfid; e.label = label;
                    fld.picks.push_back(std::move(e));
                    fld.wellDangling = false;
                    dirty_ = true; lastEditTime_ = ImGui::GetTime();
                    st.addLog("[pm] plane well seeded from preselected " + sfid);
                    break;
                }
    }

    st.addLog("[pm] open " + std::string(form_->type) +
              (mode_.empty() ? "" : " (" + mode_ + ")"));
    return true;
}

bool PropertyManager::openForEdit(AppState& st, const std::string& featureId) {
    if (st.activeDocLockedByAgent()) {
        st.toast(ui::ToastLevel::Warn, st.agentLockReason(st.docMirror.docId()));
        return false;
    }
    const nlohmann::json& tree = st.docMirror.tree();
    if (!tree.is_object()) return false;
    auto feat = tree.find("features");
    if (feat == tree.end() || !feat->is_array()) return false;
    const nlohmann::json* target = nullptr;
    for (const auto& f : *feat) {
        if (f.is_object() && f.value("id", std::string()) == featureId) { target = &f; break; }
    }
    if (!target) return false;
    std::string type = target->value("type", std::string());
    const PMFormDef* form = pmFindForm(type);
    if (!form) { st.addLog("[pm] no form for feature type '" + type + "'"); return false; }
    if (guestActive_) closeGuest();
    if (active_ && !cancelEdits(st)) return false;

    active_ = true;
    editMode_ = true;
    form_ = form;
    mode_.clear();
    title_ = form->title;
    targetId_ = featureId;
    previewBatches_ = 0;
    previewUndoGroupStarted_ = false;
    dirty_ = false;
    firstPreviewDone_ = true;   // the feature already exists; edits go straight to edit_feature
    previewInFlight_ = false;
    lastEditTime_ = ImGui::GetTime();
    previewError_.clear();
    focusedOnOpen_ = false;
    wantFocusFirstField_ = true;  // C37-F: focus first field on next draw
    aiBusy_ = false;
    aiMessage_.clear();
    aiHighlights_.clear();
    ++aiRequestSeq_;

    // C37-F: Take a checkpoint BEFORE any preview edit, so Cancel reverts with one call.
    checkpointId_ = st.docMirror.hasDoc() ? st.docMirror.checkpoint(st.worker) : std::string();
    if (checkpointId_.empty())
        st.addLog("[pm] checkpoint failed — Cancel will use undo loop fallback");
    // Phase 4: exclusive daemon edit-hold while this form previews (released in close()).
    holdDocId_ = pmTakeOpenHold(st);
    sessionDocId_ = st.docMirror.docId();
    lastHoldTime_ = ImGui::GetTime();

    ips_ = st.ips();   // document-unit display follows the active unit system (MMGS / IPS)
    fields_.clear();
    fields_.resize(form->fields.size());
    for (size_t i = 0; i < form->fields.size(); ++i)
        initField(fields_[i], form->fields[i], ips_);

    loadFromTree(st, *target);

    // A fillet/chamfer is shown LIVE and APPLIED while editing — the session already has it applied,
    // so the rounded result is on screen the moment the PM opens. The original edges in the feature
    // are drawn as dashed 3D overlay lines (viewport_panel + collectActiveWellEdgeSegments), built
    // from each selector's geometry, so the user can see and click-to-deselect edges that have been
    // rounded away. No suppressed-base preview is needed.

    st.addLog("[pm] edit " + featureId + " (" + type + ")");
    return true;
}

void PropertyManager::presetFieldForShot(const std::string& key, const std::string& value) {
    if (!active_) return;
    for (auto& f : fields_) {
        if (!f.def->key || key != f.def->key) continue;
        switch (f.def->kind) {
            case PMFieldKind::Combo:
            case PMFieldKind::DatumRef: {
                std::string opts = f.def->comboOptions ? f.def->comboOptions : "";
                int cur = 0; size_t start = 0;
                while (start <= opts.size()) {
                    size_t bar = opts.find('|', start);
                    if (opts.substr(start, bar - start) == value) { f.comboIndex = cur; break; }
                    ++cur;
                    if (bar == std::string::npos) break;
                    start = bar + 1;
                }
                break;
            }
            case PMFieldKind::Check:
                f.checkValue = (value == "true" || value == "1" || value == "yes");
                break;
            case PMFieldKind::Dim: {
                try {
                    f.dimValue = std::stod(value); f.dimExpr.clear(); f.dimError.clear();
                    std::string disp = pmFieldValueText(f.def, f.dimValue, ips_);
                    std::strncpy(f.dimBuf.data(), disp.c_str(), f.dimBuf.size() - 1);
                } catch (...) {}
                break;
            }
            case PMFieldKind::Int:
                try { f.intValue = std::stoi(value); } catch (...) {}
                break;
            default: break;
        }
        return;
    }
}

bool PropertyManager::dragSetDim(const std::string& key, double value) {
    if (!active_) return false;
    for (auto& f : fields_) {
        if (f.def->kind != PMFieldKind::Dim || !f.def->key || key != f.def->key) continue;
        if (!fieldVisible(*f.def, fields_) || !fieldEnabled(*f.def, fields_)) return false;
        f.dimValue = value;
        f.dimExpr.clear();
        f.dimError.clear();
        std::string disp = pmFieldValueText(f.def, f.dimValue, ips_);
        std::strncpy(f.dimBuf.data(), disp.c_str(), f.dimBuf.size() - 1);
        dirty_ = true;
        lastEditTime_ = ImGui::GetTime() - kDebounceSec;  // preview immediately (like a drag release)
        return true;
    }
    return false;
}

bool PropertyManager::dragSetCheck(const std::string& key, bool value) {
    if (!active_) return false;
    for (auto& f : fields_) {
        if (f.def->kind != PMFieldKind::Check || !f.def->key || key != f.def->key) continue;
        if (!fieldVisible(*f.def, fields_) || !fieldEnabled(*f.def, fields_)) return false;
        if (f.checkValue == value) return true;  // already set — still a hit for the caller
        f.checkValue = value;
        dirty_ = true;
        lastEditTime_ = ImGui::GetTime() - kDebounceSec;  // preview immediately (like dragSetDim)
        return true;
    }
    return false;
}

bool PropertyManager::dragGetCheck(const std::string& key, bool& out) const {
    if (!active_) return false;
    for (const auto& f : fields_) {
        if (f.def->kind != PMFieldKind::Check || !f.def->key || key != f.def->key) continue;
        if (!fieldVisible(*f.def, fields_) || !fieldEnabled(*f.def, fields_)) return false;
        out = f.checkValue;
        return true;
    }
    return false;
}

void PropertyManager::loadFromTree(AppState& st, const nlohmann::json& feature) {
    (void)st;
    nlohmann::json params = feature.value("params", nlohmann::json::object());
    nlohmann::json refs   = feature.value("refs",   nlohmann::json::object());
    for (auto& f : fields_) {
        const char* key = f.def->key;
        switch (f.def->kind) {
            case PMFieldKind::Dim: {
                if (auto it = params.find(key); it != params.end()) {
                    const auto& d = *it;
                    if (d.is_number()) {
                        f.dimValue = d.get<double>();
                    } else if (d.is_object()) {
                        if (auto expr = d.find("expr"); expr != d.end() && expr->is_string())
                            f.dimExpr = expr->get<std::string>();
                        if (auto value = d.find("value"); value != d.end() && value->is_number())
                            f.dimValue = value->get<double>();
                    }
                    std::string disp = f.dimExpr.empty()
                        ? pmFieldValueText(f.def, f.dimValue, ips_) : "=" + f.dimExpr;
                    std::strncpy(f.dimBuf.data(), disp.c_str(), f.dimBuf.size() - 1);
                }
                break;
            }
            case PMFieldKind::Combo: {
                if (params.contains(key) && params[key].is_string()) {
                    std::string val = params[key].get<std::string>();
                    // Resolve combo option index.
                    std::string opts = f.def->comboOptions ? f.def->comboOptions : "";
                    int idx = 0, cur = 0; size_t start = 0;
                    while (start <= opts.size()) {
                        size_t bar = opts.find('|', start);
                        std::string opt = opts.substr(start, bar - start);
                        if (opt == val) { idx = cur; break; }
                        ++cur;
                        if (bar == std::string::npos) break;
                        start = bar + 1;
                    }
                    f.comboIndex = idx;
                }
                break;
            }
            case PMFieldKind::Check: {
                if (params.contains(key) && params[key].is_boolean())
                    f.checkValue = params[key].get<bool>();
                break;
            }
            case PMFieldKind::Int: {
                if (params.contains(key) && params[key].is_number())
                    f.intValue = (int)(params[key].get<double>() + 0.5);
                else if (params.contains(key) && params[key].is_object() &&
                         params[key]["value"].is_number())
                    f.intValue = (int)(params[key]["value"].get<double>() + 0.5);
                break;
            }
            case PMFieldKind::DatumRef: {
                // refs[key] is a datum string ("axis.x"); resolve it to the combo option index.
                if (refs.contains(key) && refs[key].is_string()) {
                    std::string val = refs[key].get<std::string>();
                    std::string opts = f.def->comboOptions ? f.def->comboOptions : "";
                    int idx = 0, cur = 0; size_t start = 0;
                    while (start <= opts.size()) {
                        size_t bar = opts.find('|', start);
                        std::string opt = opts.substr(start, bar - start);
                        if (opt == val) { idx = cur; break; }
                        ++cur;
                        if (bar == std::string::npos) break;
                        start = bar + 1;
                    }
                    f.comboIndex = idx;
                }
                break;
            }
            case PMFieldKind::FeatureWell: {
                f.picks.clear();
                // Multi wells (mirror/pattern) store an array of ids; a single-emit well
                // (FeatWell1, e.g. weld_member.sketch) stores a bare id string. Accept both on
                // the edit path so the well re-populates.
                auto addFid = [&](const nlohmann::json& s) {
                    if (!s.is_string()) return;
                    std::string fid = s.get<std::string>();
                    PMSelEntry e; e.selector = fid; e.label = fid;
                    f.picks.push_back(std::move(e));
                };
                if (refs.contains(key)) {
                    if (refs[key].is_array()) for (const auto& s : refs[key]) addFid(s);
                    else addFid(refs[key]);
                }
                break;
            }
            case PMFieldKind::SelectorWell: {
                f.picks.clear();
                if (refs.contains(key)) {
                    const auto& rv = refs[key];
                    auto add = [&](const nlohmann::json& selObj) {
                        if (!selObj.is_object()) return;
                        PMSelEntry e; e.selector = selObj;
                        std::string k = selObj.value("kind", std::string("ref"));
                        e.label = k.empty() ? "ref" : (char)std::toupper((unsigned char)k[0]) + k.substr(1);
                        f.picks.push_back(e);
                    };
                    if (rv.is_array()) for (const auto& s : rv) add(s);
                    else add(rv);
                }
                break;
            }
        }
    }
}

// ---- in-viewport callouts (C27-A) -----------------------------------------

// Pull a world-space anchor from a selector entry: prefer the selector's own "centroid"
// (resolve_pick stamps a world centroid on every face/edge selector); fall back to the
// AppState topo-id centroid caches keyed by (body_id<<32 | topo_id) when absent.
static bool anchorFromSel(AppState& st, const PMSelEntry& e, bool wantEdge, float out[3]) {
    const nlohmann::json& s = e.selector;
    // Datum-plane / feature-id picks store a bare string selector — no centroid/hint to anchor on
    // (and .value() below would throw on a non-object). They carry no in-viewport callout anchor.
    if (!s.is_object()) return false;
    auto c = s.find("centroid");
    if (c != s.end() && c->is_array() && c->size() == 3 &&
        (*c)[0].is_number() && (*c)[1].is_number() && (*c)[2].is_number()) {
        out[0] = (float)(*c)[0].get<double>();
        out[1] = (float)(*c)[1].get<double>();
        out[2] = (float)(*c)[2].get<double>();
        return true;
    }
    // Cache fallback: hint_index is 0-based; cache keys are 1-based AICM topo ids.
    int hint = s.value("hint_index", -1);
    if (hint < 0) return false;
    uint64_t key = (uint64_t)1 << 32 | (uint64_t)(hint + 1);  // body 1 (single-part edit path)
    if (wantEdge) {
        auto it = st.edgeCentroidCache.find(key);
        if (it == st.edgeCentroidCache.end()) return false;
        out[0] = it->second[0]; out[1] = it->second[1]; out[2] = it->second[2];
        return true;
    }
    auto it = st.faceNormalCache.find(key);  // {nx,ny,nz, cx,cy,cz}
    if (it == st.faceNormalCache.end()) return false;
    out[0] = it->second[3]; out[1] = it->second[4]; out[2] = it->second[5];
    return true;
}

std::vector<PMCalloutItem> PropertyManager::calloutItems(AppState& st) const {
    std::vector<PMCalloutItem> out;
    if (!active_ || !form_) return out;

    // Resolve a single shared anchor for the feature: first edge ref's centroid, else first
    // face ref's centroid, else none. Edges win (dress-up features point at an edge).
    float anchor[3] = {0, 0, 0};
    bool hasAnchor = false;
    for (const auto& f : fields_) {
        if (f.def->kind != PMFieldKind::SelectorWell || f.picks.empty()) continue;
        if (!f.def->selKinds.edge) continue;
        if (anchorFromSel(st, f.picks.front(), /*wantEdge=*/true, anchor)) { hasAnchor = true; break; }
    }
    if (!hasAnchor) {
        for (const auto& f : fields_) {
            if (f.def->kind != PMFieldKind::SelectorWell || f.picks.empty()) continue;
            if (!f.def->selKinds.face) continue;
            if (anchorFromSel(st, f.picks.front(), /*wantEdge=*/false, anchor)) { hasAnchor = true; break; }
        }
    }

    for (const auto& f : fields_) {
        if (f.def->kind != PMFieldKind::Dim) continue;
        if (!fieldVisible(*f.def, fields_)) continue;
        if (!fieldEnabled(*f.def, fields_)) continue;   // skip greyed / coming-soon dims
        PMCalloutItem it;
        it.label = f.def->label;
        it.value = f.dimExpr.empty()
            ? (pmFieldUnitless(f.def) ? ui::kit::fmtValue(f.dimValue)
               : pmFieldIsAngle(f.def) ? ui::kit::fmtQuantity(f.dimValue, "deg")
               : ui::kit::fmtQuantity(ips_ ? f.dimValue / 25.4 : f.dimValue, ips_ ? "in" : "mm"))
            : ("=" + f.dimExpr);
        it.hasAnchor = hasAnchor;
        if (hasAnchor) { it.anchor[0] = anchor[0]; it.anchor[1] = anchor[1]; it.anchor[2] = anchor[2]; }
        out.push_back(std::move(it));
    }
    return out;
}

// Live unit-toggle support: rewrite each non-expr Dim field's display buffer from its canonical
// mm dimValue using the current ips_. Expr-bound fields keep their "=expr" text untouched; the
// stored canonical value is never changed, so flipping units is purely cosmetic.
void PropertyManager::reformatDimDisplays() {
    for (auto& f : fields_) {
        if (!f.def || f.def->kind != PMFieldKind::Dim) continue;
        if (!f.dimExpr.empty()) continue;          // expr binding shown as "=expr"; leave it
        if (f.def->unitLabel && f.def->unitLabel[0] == '\0') continue;  // unit-less field
        if (f.def->unitLabel && std::strstr(f.def->unitLabel, "deg")) continue;  // angle: degrees both
        std::string disp = pmFormatDim(f.dimValue, ips_);
        std::strncpy(f.dimBuf.data(), disp.c_str(), f.dimBuf.size() - 1);
        f.dimError.clear();
    }
}

void PropertyManager::close() {
    pmEditRelease(holdDocId_);   // Phase 4: both OK and Cancel funnel through here
    holdDocId_.clear();
    sessionDocId_.clear();
    active_ = false;
    editMode_ = false;
    form_ = nullptr;
    fields_.clear();
    mode_.clear();
    title_.clear();
    targetId_.clear();
    previewBatches_ = 0;
    dirty_ = false;
    firstPreviewDone_ = false;
    // Leave previewInFlight_ alone if a drain callback is still pending — the callback
    // clears it and decrements rebuildsInFlight; active_ is false so it no-ops the rest.
    checkpointId_.clear();    // C37-F
    focusedOnOpen_ = false;   // C37-F
    wantFocusFirstField_ = false; // C37-F
    previewError_.clear();    // C37-F
    previewWarning_.clear();
    committing_ = false;
    aiBusy_ = false;
    aiMessage_.clear();
    aiHighlights_.clear();
    ++aiRequestSeq_;
}

bool PropertyManager::closeForAgentLock(AppState& st) {
    if ((!active_ && (!guestActive_ || guestReadOnly_)) ||
        !st.docLockedByAgent(sessionDocId_)) return false;
    std::string part = "part";
    for (const auto& tab : st.docTabs)
        if (tab.docId == sessionDocId_) { part = tab.title; break; }
    closeIfActive();  // keep applied previews and release edit_hold; never revert
    if (guestActive_) closeGuest();
    st.toast(ui::ToastLevel::Info,
             "Agent started editing " + part + " — your edit was kept and closed");
    return true;
}

// ---- selector wells -------------------------------------------------------

PMField* PropertyManager::activeWell() {
    for (auto& f : fields_)
        if (f.def->kind == PMFieldKind::SelectorWell && f.wellActive &&
            fieldVisible(*f.def, fields_)) return &f;
    return nullptr;
}

void PropertyManager::advanceWellAfterFill(PMField* filled) {
    // Only single-ref wells advance; a multi-pick well stays active to gather more refs.
    if (!filled || filled->def->selMulti || filled->picks.empty()) return;
    // Find the next EMPTY visible selector well after `filled` (in field order); if none follows,
    // wrap to the first empty one (so a 2-well form fills the other well no matter the click order).
    bool seenFilled = false;
    PMField* next = nullptr;
    PMField* firstEmpty = nullptr;
    for (auto& f : fields_) {
        if (f.def->kind != PMFieldKind::SelectorWell || !fieldVisible(*f.def, fields_)) continue;
        const bool empty = f.picks.empty();
        if (&f == filled) { seenFilled = true; continue; }
        if (empty && !firstEmpty) firstEmpty = &f;
        if (empty && seenFilled && !next) next = &f;
    }
    PMField* target = next ? next : firstEmpty;
    if (!target) return;                    // no empty well left — keep focus where it is
    for (auto& o : fields_)
        if (o.def->kind == PMFieldKind::SelectorWell || o.def->kind == PMFieldKind::FeatureWell)
            o.wellActive = false;
    target->wellActive = true;
}

bool PropertyManager::routePick(const nlohmann::json& selector, int pickKind,
                                uint32_t pickBody, uint32_t pickTopo) {
    PMField* well = activeWell();
    if (!well || !selector.is_object()) return false;
    // Filter by allowed kinds.
    render::PickKind k = (render::PickKind)pickKind;
    const PMSelKinds& ak = well->def->selKinds;
    bool allowed = (k == render::PickKind::Face   && ak.face) ||
                   (k == render::PickKind::Edge   && ak.edge) ||
                   (k == render::PickKind::Vertex && ak.vertex);
    // Body wells (boolean): the selection observer resolved a body selector (kind="body") from
    // whatever element was clicked — accept it regardless of the underlying pick kind.
    if (!allowed && ak.body && selector.value("kind", std::string()) == "body") allowed = true;
    if (!allowed) return false;

    // De-dup by hint_index + centroid (same element picked twice toggles off — SolidWorks).
    auto same = [&](const nlohmann::json& a, const nlohmann::json& b) {
        // A topology pick (object) never equals a datum-plane pick (bare string), and .value()
        // would throw on the string — so a mixed well just replaces (single) / appends (multi).
        if (!a.is_object() || !b.is_object()) return false;
        // hint_index uses the SAME absent-sentinel on both sides — with asymmetric defaults
        // (-1 vs -2) two picks that both lack a hint_index never compared equal, so a re-pick of a
        // hint-less selector couldn't toggle off. The centroid term still distinguishes distinct
        // elements that happen to share a missing hint_index.
        return a.value("kind", std::string()) == b.value("kind", std::string()) &&
               a.value("hint_index", -1) == b.value("hint_index", -1) &&
               a.value("centroid", nlohmann::json()) == b.value("centroid", nlohmann::json());
    };
    for (size_t i = 0; i < well->picks.size(); ++i) {
        if (same(well->picks[i].selector, selector)) {
            well->picks.erase(well->picks.begin() + i);
            dirty_ = true; lastEditTime_ = ImGui::GetTime();
            return true;
        }
    }
    if (!well->def->selMulti) well->picks.clear();   // single-ref wells hold one pick
    PMSelEntry e; e.selector = selector;
    std::string kind = selector.value("kind", std::string("ref"));
    e.label = kind.empty() ? "Ref" : std::string(1, (char)std::toupper((unsigned char)kind[0])) + kind.substr(1);
    e.pickBody = pickBody; e.pickTopo = pickTopo; e.pickKind = pickKind;  // for viewport highlight
    well->picks.push_back(e);
    well->wellDangling = false;
    dirty_ = true; lastEditTime_ = ImGui::GetTime();

    // THREAD AUTO-SIZE: picking a cylindrical face into the thread form's target well
    // snaps the Size combo to the nearest standard thread for that diameter (a 5/16
    // shaft → 5/16-18). The pick selector already carries the kernel-computed radius
    // (GSL Stage 0), so no engine round-trip is needed. A shaft is compared against the
    // MAJOR Ø (turned slightly undersize is expected); a bore (concavity=="bore")
    // against the basic minor Ø. Sizes from the doc's unit system are preferred
    // (SAE in IPS, metric in MMGS), falling back to the other family on a clear miss.
    if (form_ && form_->type && std::string(form_->type) == "thread" &&
        std::strcmp(well->def->key, "target_face") == 0 &&
        selector.value("surf", std::string()) == "cylinder" &&
        selector.contains("radius") && selector["radius"].is_number()) {
        bool customSize = false;
        for (const auto& f : fields_)
            if (std::strcmp(f.def->key, "custom_size") == 0) customSize = f.checkValue;
        if (!customSize) {
            const double dia = 2.0 * selector["radius"].get<double>();
            const bool bore = selector.value("concavity", std::string()) == "bore";
            auto bestIn = [&](bool metric, const PMThreadSize*& hit, double& err) {
                for (const auto& t : kPmThreadSizes) {
                    if (t.metric != metric) continue;
                    // Reference Ø this pick should measure near: major for a shaft,
                    // ISO/UN basic minor (major − 1.0825·pitch) for a bore.
                    const double ref = bore ? t.major - 1.0825 * t.pitch : t.major;
                    const double d = std::abs(dia - ref);
                    if (d < err) { err = d; hit = &t; }
                }
            };
            const PMThreadSize* hit = nullptr; double err = 1e300;
            bestIn(!ips_, hit, err);
            // Accept within a band that covers undersize-turned shafts / drilled bores
            // but rejects a clearly off-family diameter; else try the other family.
            auto tol = [](const PMThreadSize& t) { return std::max(0.25, 0.05 * t.major); };
            if (!hit || err > tol(*hit)) { hit = nullptr; err = 1e300; bestIn(ips_, hit, err); }
            if (hit && err <= tol(*hit)) {
                for (auto& f : fields_) {
                    if (f.def->kind != PMFieldKind::Combo ||
                        std::strcmp(f.def->key, "size") != 0) continue;
                    int idx = pmComboIndexOf(*f.def, hit->value);
                    if (idx >= 0) f.comboIndex = idx;
                    break;
                }
            }
        }
    }

    advanceWellAfterFill(well);
    return true;
}

void PropertyManager::collectActiveWellHighlights(std::vector<std::array<uint32_t, 3>>& out) const {
    for (const auto& f : fields_) {
        if (f.def->kind != PMFieldKind::SelectorWell || !f.wellActive ||
            !fieldVisible(*f.def, fields_))
            continue;
        // Only edge/face/vertex topology wells carry highlightable picks (skip body/datum wells).
        if (!(f.def->selKinds.edge || f.def->selKinds.face || f.def->selKinds.vertex)) return;
        for (const auto& p : f.picks)
            if (p.pickKind >= 0)
                out.push_back({p.pickBody, p.pickTopo, (uint32_t)p.pickKind});
        return;  // a single well is active at a time
    }
}

void PropertyManager::collectActiveWellEdgeSegments(std::vector<EdgeSeg>& out) const {
    if (!active_ || !form_) return;
    const std::string ftype = form_->type ? form_->type : "";
    if (ftype != "fillet" && ftype != "chamfer") return;  // dashed old-edge overlay is blend-only
    for (const auto& f : fields_) {
        if (f.def->kind != PMFieldKind::SelectorWell || !f.wellActive ||
            !fieldVisible(*f.def, fields_) || !f.def->selKinds.edge)
            continue;
        for (int i = 0; i < (int)f.picks.size(); ++i) {
            const nlohmann::json& sel = f.picks[i].selector;
            if (!sel.is_object() || !sel.contains("centroid") || !sel["centroid"].is_array() ||
                sel["centroid"].size() < 3)
                continue;
            EdgeSeg seg;
            seg.pickIndex = i;
            const auto& c = sel["centroid"];
            float cx = c[0].get<float>(), cy = c[1].get<float>(), cz = c[2].get<float>();
            const std::string surf = sel.value("surf", std::string());
            const bool hasAxis = sel.contains("axis") && sel["axis"].is_array() &&
                                 sel["axis"].size() == 3;
            const bool hasLen = !sel.value("area_or_len", nlohmann::json()).is_null();
            if (surf == "line" && hasAxis && hasLen) {
                // Straight edge: reconstruct the segment from centroid ± half-length along its axis.
                float ax = sel["axis"][0].get<float>(), ay = sel["axis"][1].get<float>(),
                      az = sel["axis"][2].get<float>();
                float half = 0.5f * sel["area_or_len"].get<float>();
                seg.pts.push_back({cx - ax * half, cy - ay * half, cz - az * half});
                seg.pts.push_back({cx + ax * half, cy + ay * half, cz + az * half});
                seg.closed = false;
            } else if (surf == "circle" && hasAxis && hasLen) {
                // Circular edge (the revolve case): centroid is the center, area_or_len is the
                // circumference (=2πr), axis is the circle normal. Sample a closed loop in the plane
                // perpendicular to the axis so the dashed overlay traces the real edge.
                float nx = sel["axis"][0].get<float>(), ny = sel["axis"][1].get<float>(),
                      nz = sel["axis"][2].get<float>();
                float nl = std::sqrt(nx * nx + ny * ny + nz * nz);
                float r = sel["area_or_len"].get<float>() / (2.0f * 3.14159265358979f);
                if (nl > 1e-6f && r > 1e-6f) {
                    nx /= nl; ny /= nl; nz /= nl;
                    // Two basis vectors spanning the circle plane (u ⟂ n, v = n × u).
                    float rx = (std::fabs(nx) < 0.9f) ? 1.0f : 0.0f;
                    float ry = (std::fabs(nx) < 0.9f) ? 0.0f : 1.0f;
                    float ux = ny * 0.0f - nz * ry, uy = nz * rx - nx * 0.0f, uz = nx * ry - ny * rx;
                    float ul = std::sqrt(ux * ux + uy * uy + uz * uz);
                    if (ul > 1e-6f) {
                        ux /= ul; uy /= ul; uz /= ul;
                        float vx = ny * uz - nz * uy, vy = nz * ux - nx * uz, vz = nx * uy - ny * ux;
                        const int N = 48;
                        seg.pts.reserve(N);
                        for (int k = 0; k < N; ++k) {
                            float t = (float)k / (float)N * 2.0f * 3.14159265358979f;
                            float ct = std::cos(t) * r, stt = std::sin(t) * r;
                            seg.pts.push_back({cx + ux * ct + vx * stt, cy + uy * ct + vy * stt,
                                               cz + uz * ct + vz * stt});
                        }
                        seg.closed = true;
                    }
                }
                if (seg.pts.empty()) { seg.pts.push_back({cx, cy, cz}); seg.closed = false; }
            } else {
                // Arc/spline/ellipse we can't reconstruct from the selector: mark the centroid so the
                // viewport still draws a small ring there as a deselect target.
                seg.pts.push_back({cx, cy, cz});
                seg.closed = false;
            }
            out.push_back(seg);
        }
        return;  // a single well is active at a time
    }
}

bool PropertyManager::removeActiveWellPick(int pickIndex) {
    for (auto& f : fields_) {
        if (f.def->kind != PMFieldKind::SelectorWell || !f.wellActive ||
            !fieldVisible(*f.def, fields_) || !f.def->selKinds.edge)
            continue;
        if (pickIndex < 0 || pickIndex >= (int)f.picks.size()) return false;
        f.picks.erase(f.picks.begin() + pickIndex);
        dirty_ = true; lastEditTime_ = ImGui::GetTime();
        return true;
    }
    return false;
}

bool PropertyManager::activeWellWantsBody() const {
    for (const auto& f : fields_)
        if (f.def->kind == PMFieldKind::SelectorWell && f.wellActive &&
            fieldVisible(*f.def, fields_))
            return f.def->selKinds.body;
    return false;
}

bool PropertyManager::activeWellWantsPlane() const {
    for (const auto& f : fields_)
        if (f.def->kind == PMFieldKind::SelectorWell && f.wellActive &&
            fieldVisible(*f.def, fields_))
            return f.def->selKinds.plane;
    return false;
}

bool PropertyManager::activeWellWantsAxis() const {
    for (const auto& f : fields_)
        if (f.def->kind == PMFieldKind::SelectorWell && f.wellActive &&
            fieldVisible(*f.def, fields_))
            return f.def->selKinds.axis;
    return false;
}

// T-BL37: returns true when the active selector well accepts a point datum.
bool PropertyManager::activeWellWantsPoint() const {
    for (const auto& f : fields_)
        if (f.def->kind == PMFieldKind::SelectorWell && f.wellActive &&
            fieldVisible(*f.def, fields_))
            return f.def->selKinds.point;
    return false;
}

bool PropertyManager::activeWellPrefersEdge() const {
    for (const auto& f : fields_)
        if (f.def->kind == PMFieldKind::SelectorWell && f.wellActive &&
            fieldVisible(*f.def, fields_))
            // Pure edge well (edge, not face) -> snap to nearby edges. A face-or-edge well stays on
            // the exact-pixel pick so face selection isn't hijacked by a nearby edge.
            return f.def->selKinds.edge && !f.def->selKinds.face;
    return false;
}

bool PropertyManager::activeWellExpandsFaceToEdges() const {
    // SolidWorks "fillet a whole face": clicking a FACE while a fillet/chamfer edge well is active
    // adds ALL of that face's edges. Only for blend edge wells (edge-only, not face/body wells).
    if (!form_) return false;
    const std::string t = form_->type ? form_->type : "";
    if (t != "fillet" && t != "chamfer") return false;
    for (const auto& f : fields_)
        if (f.def->kind == PMFieldKind::SelectorWell && f.wellActive &&
            fieldVisible(*f.def, fields_))
            return f.def->selKinds.edge && !f.def->selKinds.face;
    return false;
}

bool PropertyManager::routeDatumAxis(AppState& st, const std::string& fid,
                                     const std::string& label) {
    PMField* well = activeWell();
    if (!well || !well->def->selKinds.axis || fid.empty()) return false;

    auto isSameAxisPick = [&](const nlohmann::json& sel) -> bool {
        if (!sel.is_object()) return false;
        // Reserved / feature datum form, or explicit auto-axis form tagged with feature id.
        const std::string k = sel.value("kind", std::string());
        if ((k == "datum" || k == "axis") && sel.value("feature", std::string()) == fid)
            return true;
        return false;
    };

    // Toggle off if this exact datum axis is already in the well (SolidWorks re-pick).
    for (size_t i = 0; i < well->picks.size(); ++i) {
        if (isSameAxisPick(well->picks[i].selector)) {
            well->picks.erase(well->picks.begin() + i);
            dirty_ = true; lastEditTime_ = ImGui::GetTime();
            return true;
        }
    }
    if (!well->def->selMulti) well->picks.clear();   // single-ref wells hold one pick

    PMSelEntry e;
    e.label = label.empty() ? fid : label;

    // Host-only auto plane-intersection axes (axis.auto:…) are NOT in the engine reserved
    // set — store explicit geometry so document.cpp:axis_from_explicit can resolve them.
    // Geometry comes from the inject cache / renderer (never re-solve plane pairs here).
    if (fid.rfind("axis.auto:", 0) == 0) {
        auto emitExplicit = [&](float ox, float oy, float oz,
                                float dx, float dy, float dz) {
            e.selector = {
                {"kind", "axis"},
                {"feature", fid},
                {"point", {ox, oy, oz}},
                {"axis",  {dx, dy, dz}}
            };
            well->picks.push_back(e);
            well->wellDangling = false;
            dirty_ = true; lastEditTime_ = ImGui::GetTime();
            advanceWellAfterFill(well);
        };
        bool ok = false;
        for (const auto& ent : st.axisAutoCache) {
            if (ent.id != fid) continue;
            emitExplicit(ent.origin[0], ent.origin[1], ent.origin[2],
                         ent.dir[0], ent.dir[1], ent.dir[2]);
            ok = true;
            break;
        }
        if (!ok && st.renderer) {
            for (const auto& d : st.renderer->refDatums()) {
                if (d.kind != Renderer::RefDatum::Kind::Axis || d.id != fid) continue;
                emitExplicit(d.origin.x, d.origin.y, d.origin.z,
                             d.dir.x, d.dir.y, d.dir.z);
                ok = true;
                break;
            }
        }
        if (ok) return true;
        // If geometry is unavailable, fall through to datum form (chip still shows).
    }

    // Reserved axis.x/y/z + user axis features: engine axis_from_datum / resolve_axis_ref.
    e.selector = {{"kind", "datum"}, {"feature", fid}};
    well->picks.push_back(e);
    well->wellDangling = false;
    dirty_ = true; lastEditTime_ = ImGui::GetTime();
    advanceWellAfterFill(well);
    return true;
}

bool PropertyManager::routeDatumPlane(const std::string& datumId, const std::string& labelIn) {
    PMField* well = activeWell();
    if (!well || !well->def->selKinds.plane || datumId.empty()) return false;
    // Toggle off if this exact datum plane is already in the well (SolidWorks re-pick).
    for (size_t i = 0; i < well->picks.size(); ++i) {
        const auto& sel = well->picks[i].selector;
        if (sel.is_string() && sel.get<std::string>() == datumId) {
            well->picks.erase(well->picks.begin() + i);
            dirty_ = true; lastEditTime_ = ImGui::GetTime();
            return true;
        }
    }
    if (!well->def->selMulti) well->picks.clear();   // single-ref wells hold one pick
    // Chip label: the datum's own name when given (created plane "Plane1"); else a short label
    // derived from the reserved id ("plane.front" -> "Front").
    std::string label = labelIn;
    if (label.empty()) {
        label = datumId;
        auto dot = label.rfind('.');
        if (dot != std::string::npos) label = label.substr(dot + 1);
        if (!label.empty()) label[0] = (char)std::toupper((unsigned char)label[0]);
    }
    PMSelEntry e;
    e.selector = datumId;   // bare reserved-datum string; _SEL_OR_DATUM slots accept it
    e.label = label;
    well->picks.push_back(e);
    well->wellDangling = false;
    dirty_ = true; lastEditTime_ = ImGui::GetTime();
    advanceWellAfterFill(well);
    return true;
}

// T-BL37: route a point-datum (world origin or user-created point datum) into the active
// selector well.  Uses the {kind:"datum",feature:fid} selector so resolve_point in
// compile_document() picks it up.  "point.origin" is the canonical world-origin id.
bool PropertyManager::routeDatumPoint(const std::string& fid, const std::string& label) {
    PMField* well = activeWell();
    if (!well || !well->def->selKinds.point || fid.empty()) return false;
    // Toggle off if this exact datum point is already in the well (SolidWorks re-pick).
    for (size_t i = 0; i < well->picks.size(); ++i) {
        const auto& sel = well->picks[i].selector;
        if (sel.is_object() && sel.value("kind", std::string()) == "datum" &&
            sel.value("feature", std::string()) == fid) {
            well->picks.erase(well->picks.begin() + i);
            dirty_ = true; lastEditTime_ = ImGui::GetTime();
            return true;
        }
    }
    if (!well->def->selMulti) well->picks.clear();   // single-ref wells hold one pick
    PMSelEntry e;
    e.selector = {{"kind", "datum"}, {"feature", fid}};  // resolve_point accepts this
    e.label = label.empty() ? fid : label;
    well->picks.push_back(e);
    well->wellDangling = false;
    dirty_ = true; lastEditTime_ = ImGui::GetTime();
    advanceWellAfterFill(well);
    return true;
}

std::string PropertyManager::activeWellKindHint() const {
    for (const auto& f : fields_) {
        if (f.def->kind != PMFieldKind::SelectorWell || !f.wellActive ||
            !fieldVisible(*f.def, fields_)) continue;
        const PMSelKinds& k = f.def->selKinds;
        std::vector<const char*> parts;
        if (k.face)   parts.push_back("faces");
        if (k.edge)   parts.push_back("edges");
        if (k.vertex) parts.push_back("vertices");
        if (k.body)   parts.push_back("bodies");
        if (k.plane)  parts.push_back("planes");
        if (k.axis)   parts.push_back("axes");
        if (k.point)  parts.push_back("points");  // T-BL37
        std::string s;
        for (size_t i = 0; i < parts.size(); ++i) {
            if (i) s += (i + 1 == parts.size()) ? " or " : ", ";
            s += parts[i];
        }
        return s;
    }
    return "";
}

// ---- C51: sketched-feature region picking ---------------------------------

// Short chip label for a region/face selector (region picks read "Whole sketch" / "Region N").
static std::string pmRegionLabel(const nlohmann::json& sel) {
    std::string kind = sel.value("kind", std::string("ref"));
    if (kind == "region") {
        if (sel.contains("index") && sel["index"].is_number())
            return "Region " + std::to_string((int)sel["index"].get<double>() + 1);
        return "Whole sketch";
    }
    if (kind.empty()) return "Ref";
    return std::string(1, (char)std::toupper((unsigned char)kind[0])) + kind.substr(1);
}

PMField* PropertyManager::profileWell() {
    for (auto& f : fields_)
        if (f.def->kind == PMFieldKind::SelectorWell &&
            (std::strcmp(f.def->key, "profile") == 0 || std::strcmp(f.def->key, "sections") == 0))
            return &f;
    return nullptr;
}

const PMField* PropertyManager::profileWell() const {
    for (const auto& f : fields_)
        if (f.def->kind == PMFieldKind::SelectorWell &&
            (std::strcmp(f.def->key, "profile") == 0 || std::strcmp(f.def->key, "sections") == 0))
            return &f;
    return nullptr;
}

bool PropertyManager::activeWellWantsPathFeature() const {
    if (!active_) return false;
    // activeWell() is non-const; inline the same lookup here.
    for (const auto& f : fields_) {
        if (f.def->kind != PMFieldKind::SelectorWell || !f.wellActive ||
            !fieldVisible(*f.def, fields_)) continue;
        return std::strcmp(f.def->key, "path") == 0 ||
               std::strcmp(f.def->key, "centerline") == 0;
    }
    return false;
}

bool PropertyManager::routePathFeature(const std::string& fid, const std::string& name,
                                      bool isCurve) {
    PMField* well = activeWell();
    if (!well || fid.empty()) return false;
    if (std::strcmp(well->def->key, "path") != 0 &&
        std::strcmp(well->def->key, "centerline") != 0) return false;
    // Schema-valid selector: a sketch path is a region (its wire = spine); a curve/helix
    // path is a datum-feature ref. Both resolve to `fid` via selector_owner_id.
    nlohmann::json sel = isCurve ? nlohmann::json{{"kind", "datum"}, {"feature", fid}}
                                 : nlohmann::json{{"kind", "region"}, {"sketch", fid}};
    auto owner = [](const nlohmann::json& s) -> std::string {
        if (!s.is_object()) return std::string();
        if (s.contains("feature") && s["feature"].is_string()) return s["feature"];
        if (s.contains("sketch") && s["sketch"].is_string()) return s["sketch"];
        return std::string();
    };
    // Toggle off if this exact feature already fills the path (SolidWorks re-pick).
    for (size_t i = 0; i < well->picks.size(); ++i) {
        if (owner(well->picks[i].selector) == fid) {
            well->picks.erase(well->picks.begin() + i);
            dirty_ = true; lastEditTime_ = ImGui::GetTime();
            return true;
        }
    }
    well->picks.clear();   // path / centerline are single-ref
    PMSelEntry e;
    e.selector = std::move(sel);
    e.label = name.empty() ? fid : name;
    well->picks.push_back(std::move(e));
    well->wellDangling = false;
    dirty_ = true; lastEditTime_ = ImGui::GetTime();
    advanceWellAfterFill(well);
    return true;
}

std::vector<std::string> PropertyManager::pathHighlightFeatureIds() const {
    std::vector<std::string> out;
    if (!active_) return out;
    for (const auto& f : fields_) {
        if (f.def->kind != PMFieldKind::SelectorWell) continue;
        const bool isPath = std::strcmp(f.def->key, "path") == 0 ||
                            std::strcmp(f.def->key, "centerline") == 0 ||
                            std::strcmp(f.def->key, "guides") == 0;
        if (!isPath) continue;
        for (const auto& p : f.picks) {
            const auto& s = p.selector;
            if (!s.is_object()) continue;
            if (s.contains("feature") && s["feature"].is_string()) out.push_back(s["feature"]);
            else if (s.contains("sketch") && s["sketch"].is_string()) out.push_back(s["sketch"]);
        }
    }
    return out;
}

bool PropertyManager::wantsRegionPicker() const {
    return active_ && profileWell() != nullptr;
}

bool PropertyManager::sectionsAreOrdered() const {
    const PMField* w = profileWell();
    return w && std::strcmp(w->def->key, "sections") == 0;
}

std::vector<nlohmann::json> PropertyManager::profilePicks() const {
    std::vector<nlohmann::json> out;
    if (const PMField* w = profileWell())
        for (const auto& p : w->picks) out.push_back(p.selector);
    return out;
}

void PropertyManager::setProfilePicks(AppState& st, const std::vector<nlohmann::json>& sels) {
    (void)st;
    PMField* w = profileWell();
    if (!w) return;
    w->picks.clear();
    for (const auto& s : sels) {
        PMSelEntry e; e.selector = s; e.label = pmRegionLabel(s);
        w->picks.push_back(std::move(e));
    }
    w->wellDangling = false;
    // A region click re-enters the select session (user is actively picking contours).
    for (auto& o : fields_)
        if (o.def->kind == PMFieldKind::SelectorWell ||
            o.def->kind == PMFieldKind::FeatureWell)
            o.wellActive = false;
    w->wellActive = true;
    suppressWellAutoArm_ = false;
    dirty_ = true; lastEditTime_ = ImGui::GetTime();
}

void PropertyManager::endProfileSelection() {
    if (!active_) return;
    for (auto& f : fields_) {
        if (f.def->kind == PMFieldKind::SelectorWell ||
            f.def->kind == PMFieldKind::FeatureWell)
            f.wellActive = false;
    }
    // Stop the always-on re-home from re-arming next frame (would undo the dismiss).
    suppressWellAutoArm_ = true;
}


bool PropertyManager::hasActiveSelectorWell() const {
    if (!active_) return false;
    for (const auto& f : fields_) {
        if ((f.def->kind == PMFieldKind::SelectorWell ||
             f.def->kind == PMFieldKind::FeatureWell) &&
            f.wellActive && fieldVisible(*f.def, fields_))
            return true;
    }
    return false;
}



std::vector<std::string> PropertyManager::regionCandidateSketches(AppState& st) const {
    std::vector<std::string> out;
    const PMField* w = profileWell();
    if (!w) return out;
    auto pushUnique = [&](const std::string& s) {
        if (s.empty()) return;
        for (const auto& e : out) if (e == s) return;
        out.push_back(s);
    };
    // Sketches already referenced by current picks (preserve order).
    for (const auto& p : w->picks)
        if (p.selector.value("kind", std::string()) == "region")
            pushUnique(p.selector.value("sketch", std::string()));
    const bool sections = std::strcmp(w->def->key, "sections") == 0;
    if (!out.empty() && !sections) return out;   // single-profile well is bound to its sketch
    // Otherwise enumerate non-empty unsuppressed sketches from the tree (loft / unseeded profile).
    const nlohmann::json& tree = st.docMirror.tree();
    if (tree.is_object() && tree.contains("features") && tree["features"].is_array()) {
        for (const auto& f : tree["features"]) {
            if (!f.is_object() || f.value("type", std::string()) != "sketch") continue;
            if (f.value("suppressed", false)) continue;
            const auto& ents = f.contains("params")
                ? f["params"].value("entities", nlohmann::json::array())
                : nlohmann::json::array();
            if (!ents.is_array() || ents.empty()) continue;
            pushUnique(f.value("id", std::string()));
        }
    }
    return out;
}

// ---- op assembly ----------------------------------------------------------

void PropertyManager::buildOpFeature(nlohmann::json& feature) const {
    feature["type"] = form_->type;
    feature["name"] = std::string(title_) + (targetId_.empty() ? "1" : "");  // overwritten by id_map naming
    nlohmann::json params = nlohmann::json::object();
    nlohmann::json refs   = nlohmann::json::object();
    for (const auto& f : fields_) {
        // comingSoon fields are display-only; never emit them to the engine. (Disabled-but-required
        // params like extrude's depth on a through-all are still emitted — the engine ignores the
        // ones that don't apply, and the schema requires their presence.)
        if (f.def->comingSoon) continue;
        if (!fieldVisible(*f.def, fields_)) continue;
        // Conditionally-omitted fields (e.g. hole.diameter when a non-custom standard is chosen):
        // dropping them lets the engine's table/defaults drive the value instead of an override.
        if (f.def->omitWhenDisabled && !fieldEnabled(*f.def, fields_)) continue;
        const char* key = f.def->key;
        switch (f.def->kind) {
            case PMFieldKind::Dim:
                params[key] = dimJson(f);
                break;
            case PMFieldKind::Combo: {
                std::string opts = f.def->comboOptions ? f.def->comboOptions : "";
                int cur = 0; size_t start = 0; std::string chosen;
                while (start <= opts.size()) {
                    size_t bar = opts.find('|', start);
                    std::string opt = opts.substr(start, bar - start);
                    if (cur == f.comboIndex) { chosen = opt; break; }
                    ++cur;
                    if (bar == std::string::npos) break;
                    start = bar + 1;
                }
                if (!chosen.empty()) params[key] = chosen;
                break;
            }
            case PMFieldKind::Check:
                params[key] = f.checkValue;
                break;
            case PMFieldKind::Int:
                params[key] = f.intValue;
                break;
            case PMFieldKind::Text: {
                // Emit a free-form string param only when the user typed something (an empty skip
                // field must NOT emit params.skip — the engine then patterns the full grid).
                std::string txt = f.textBuf.data();
                if (!txt.empty()) params[key] = txt;
                break;
            }
            case PMFieldKind::DatumRef: {
                // The chosen combo OPTION value is a datum string ("axis.x"/"plane.right") emitted
                // straight into refs (a _SEL_OR_DATUM slot accepts reserved datum strings).
                std::string opts = f.def->comboOptions ? f.def->comboOptions : "";
                int cur = 0; size_t start = 0; std::string chosen;
                while (start <= opts.size()) {
                    size_t bar = opts.find('|', start);
                    std::string opt = opts.substr(start, bar - start);
                    if (cur == f.comboIndex) { chosen = opt; break; }
                    ++cur;
                    if (bar == std::string::npos) break;
                    start = bar + 1;
                }
                if (!chosen.empty()) refs[key] = chosen;
                break;
            }
            case PMFieldKind::SelectorWell: {
                if (f.def->selMulti) {
                    // OMIT an empty multi-well — an optional SelList ref (e.g. sweep/loft
                    // guides) validates as "must be a non-empty list" if present-but-[],
                    // so an empty well must drop the ref entirely, not emit [].
                    if (!f.picks.empty()) {
                        nlohmann::json arr = nlohmann::json::array();
                        for (const auto& p : f.picks) arr.push_back(p.selector);
                        refs[key] = arr;
                    }
                } else if (!f.picks.empty()) {
                    refs[key] = f.picks.front().selector;
                }
                break;
            }
            case PMFieldKind::FeatureWell: {
                // Each pick's selector is a bare feature-id string. selMulti (default) emits a
                // _FEAT_LIST array (mirror/pattern). selMulti=false (FeatWell1) is SINGLE-EMIT:
                // refs[key] = the bare id string (weld_member's path sketch wants one id, not an
                // array). Omit the ref entirely when empty (mirror reads an absent list as a
                // whole-body mirror; weld_member then dangles until a sketch is picked).
                if (!f.picks.empty()) {
                    if (f.def->selMulti) {
                        nlohmann::json arr = nlohmann::json::array();
                        for (const auto& p : f.picks) arr.push_back(p.selector);
                        refs[key] = arr;
                    } else {
                        refs[key] = f.picks.front().selector;
                    }
                }
                break;
            }
        }
    }
    // THREAD live-preview DRAFT: a real helical thread cut costs 1–3 s of intrinsic OCCT
    // boolean (profiled floor), which is unusable per drag tick. While the form is still
    // being tweaked we send params.preview=true; the engine's thread compiler then renders
    // a CHEAP turned band (~ms) showing the exact start/length/flip extent instead of the
    // helical geometry. commit() forces a full re-apply with committing_=true (flag
    // suppressed), so real geometry always replaces the draft before the feature is kept.
    if (!committing_ && form_ && std::string(form_->type) == "thread")
        params["preview"] = true;
    feature["params"] = params;
    if (!refs.empty()) feature["refs"] = refs;
}

nlohmann::json PropertyManager::buildAiAssistPayload(AppState& st) const {
    nlohmann::json schema = {
        {"type", form_ ? form_->type : ""},
        {"title", title_},
        {"mode", mode_},
        {"fields", nlohmann::json::array()},
    };
    nlohmann::json current = {{"fields", nlohmann::json::object()}};

    for (const auto& f : fields_) {
        const bool visible = fieldVisible(*f.def, fields_);
        const bool enabled = fieldEnabled(*f.def, fields_);
        nlohmann::json sf = {
            {"key", f.def->key},
            {"label", f.def->label},
            {"group", f.def->group},
            {"kind", aiKindName(f.def->kind)},
            {"visible", visible},
            {"enabled", enabled},
        };
        if (f.def->kind == PMFieldKind::Combo || f.def->kind == PMFieldKind::DatumRef)
            sf["options"] = comboOptionsList(*f.def);
        if (f.def->kind == PMFieldKind::SelectorWell) {
            sf["multi"] = f.def->selMulti;
            sf["accepts"] = {
                {"face", f.def->selKinds.face}, {"edge", f.def->selKinds.edge},
                {"vertex", f.def->selKinds.vertex}, {"body", f.def->selKinds.body},
                {"plane", f.def->selKinds.plane},
            };
        }
        schema["fields"].push_back(sf);

        nlohmann::json cf = {
            {"kind", aiKindName(f.def->kind)},
            {"visible", visible},
            {"enabled", enabled},
        };
        switch (f.def->kind) {
            case PMFieldKind::Dim:
                cf["value"] = f.dimValue;
                cf["text"] = std::string(f.dimBuf.data());
                cf["error"] = f.dimError;
                break;
            case PMFieldKind::Combo:
            case PMFieldKind::DatumRef:
                cf["value"] = comboValue(fields_, f.def->key);
                break;
            case PMFieldKind::Check:
                cf["value"] = f.checkValue;
                break;
            case PMFieldKind::Int:
                cf["value"] = f.intValue;
                break;
            case PMFieldKind::SelectorWell:
            case PMFieldKind::FeatureWell:
                cf["count"] = (int)f.picks.size();
                break;
        }
        current["fields"][f.def->key] = cf;
    }

    nlohmann::json selected = nlohmann::json::array();
    for (const auto& item : st.selection.items()) {
        if (item.selector.empty()) continue;
        try {
            selected.push_back(nlohmann::json::parse(item.selector));
        } catch (...) {
            selected.push_back(item.selector);
        }
    }

    std::string intent = "Fill the active " + title_ +
                         " PropertyManager. Prefer safe visible field patches, and activate a "
                         "selector well when a real reference pick is needed.";
    return {
        {"doc_id", st.docMirror.docId()},
        {"intent", intent},
        {"form_schema", schema},
        {"current", current},
        {"selected_refs", selected},
    };
}

void PropertyManager::requestAiAssist(AppState& st) {
    if (!active_ || aiBusy_) return;
    if (!st.workerStarted) {
        st.toast(ui::ToastLevel::Warn, "Start the runtime before using Select with AI");
        return;
    }
    aiBusy_ = true;
    aiMessage_ = "AI is checking the visible options...";
    aiHighlights_.clear();
    const int seq = ++aiRequestSeq_;
    nlohmann::json params = buildAiAssistPayload(st);
    // Multi-document: this PropertyManager is now owned by its document's DocSession, but its result
    // patches fields via st (the ACTIVE doc). Bind the request to its doc so a tab switch before the
    // result drains can't apply this (backgrounded) form's AI suggestions against the foreground doc.
    const std::string assistDocId = st.docMirror.docId();
    st.rpc.call("agent_pm_assist_v1", params, [this, &st, seq, assistDocId](const core::RpcResult& r) {
        if (!active_ || seq != aiRequestSeq_) return;
        if (st.docMirror.docId() != assistDocId) { aiBusy_ = false; return; }  // active doc changed
        aiBusy_ = false;
        if (!r.ok) {
            aiMessage_ = "AI assist failed: " + r.err;
            st.toast(ui::ToastLevel::Error, aiMessage_);
            return;
        }
        if (r.result.is_object() && r.result.contains("code")) {
            aiMessage_ = "AI assist failed: " + r.result.value("message", std::string("worker error"));
            st.toast(ui::ToastLevel::Error, aiMessage_);
            return;
        }
        applyAiAssistResult(st, r.result, seq);
    }, 120000);
}

void PropertyManager::applyAiAssistResult(AppState& st, const nlohmann::json& result, int requestSeq) {
    if (!active_ || requestSeq != aiRequestSeq_ || !result.is_object()) return;
    int patched = 0;
    auto findVisible = [&](const std::string& key) -> PMField* {
        for (auto& f : fields_)
            if (key == f.def->key && fieldVisible(*f.def, fields_)) return &f;
        return nullptr;
    };

    auto patches = result.find("patches");
    if (patches != result.end() && patches->is_array()) {
        for (const auto& p : *patches) {
            if (!p.is_object()) continue;
            std::string key = p.value("key", std::string());
            PMField* f = findVisible(key);
            if (!f) continue;
            const auto& val = p["value"];
            bool changed = false;
            switch (f->def->kind) {
                case PMFieldKind::Dim: {
                    std::string text;
                    if (val.is_number()) text = std::to_string(val.get<double>());
                    else if (val.is_string()) text = val.get<std::string>();
                    else if (val.is_object() && val.contains("expr") && val["expr"].is_string())
                        text = "=" + val["expr"].get<std::string>();
                    if (!text.empty()) {
                        std::memset(f->dimBuf.data(), 0, f->dimBuf.size());
                        std::strncpy(f->dimBuf.data(), text.c_str(), f->dimBuf.size() - 1);
                        if (!text.empty() && text[0] == '=') {
                            f->dimExpr = text.substr(1);
                            f->dimError.clear();
                            changed = true;
                        } else {
                            double v = 0.0; std::string err;
                            bool angle = f->def->unitLabel && std::strstr(f->def->unitLabel, "deg");
                            if (pmParseDim(text, v, err, angle, ips_ ? 25.4 : 1.0)) {
                                f->dimValue = v;
                                f->dimExpr.clear();
                                f->dimError.clear();
                                changed = true;
                            } else {
                                f->dimError = err;
                            }
                        }
                    }
                    break;
                }
                case PMFieldKind::Combo:
                case PMFieldKind::DatumRef: {
                    if (!val.is_string()) break;
                    std::string want = val.get<std::string>();
                    auto opts = comboOptionsList(*f->def);
                    for (int i = 0; i < (int)opts.size(); ++i) {
                        if (opts[i] == want) {
                            if (f->comboIndex != i) {
                                f->comboIndex = i;
                                changed = true;
                            }
                            break;
                        }
                    }
                    break;
                }
                case PMFieldKind::Check:
                    if (val.is_boolean() && f->checkValue != val.get<bool>()) {
                        f->checkValue = val.get<bool>();
                        changed = true;
                    }
                    break;
                case PMFieldKind::Int:
                    if (val.is_number_integer() && f->intValue != val.get<int>()) {
                        f->intValue = std::min(std::max(val.get<int>(), f->def->intMin),
                                               f->def->intMax);
                        changed = true;
                    }
                    break;
                case PMFieldKind::SelectorWell:
                case PMFieldKind::FeatureWell:
                    break;  // AI never patches refs; it can only activate a well.
            }
            if (changed) ++patched;
        }
    }

    aiHighlights_.clear();
    auto hi = result.find("highlights");
    if (hi != result.end() && hi->is_array()) {
        for (const auto& h : *hi)
            if (h.is_string()) aiHighlights_.push_back(h.get<std::string>());
    }

    bool activated = false;
    std::string activate = result.value("activate_well", std::string());
    if (!activate.empty()) {
        PMField* w = findVisible(activate);
        if (w && (w->def->kind == PMFieldKind::SelectorWell ||
                  w->def->kind == PMFieldKind::FeatureWell)) {
            for (auto& of : fields_)
                if (of.def->kind == PMFieldKind::SelectorWell ||
                    of.def->kind == PMFieldKind::FeatureWell)
                    of.wellActive = false;
            w->wellActive = true;
            activated = true;
            if (!hasString(aiHighlights_, w->def->key)) aiHighlights_.push_back(w->def->key);
        }
    }
    if (!activated && patched > 0) {
        for (auto& w : fields_) {
            if (w.def->kind != PMFieldKind::SelectorWell) continue;
            if (!fieldVisible(*w.def, fields_) || !w.picks.empty()) continue;
            for (auto& of : fields_)
                if (of.def->kind == PMFieldKind::SelectorWell ||
                    of.def->kind == PMFieldKind::FeatureWell)
                    of.wellActive = false;
            w.wellActive = true;
            activated = true;
            if (!hasString(aiHighlights_, w.def->key)) aiHighlights_.push_back(w.def->key);
            break;
        }
    }

    aiMessage_ = result.value("answer", std::string());
    auto qs = result.find("questions");
    if (qs != result.end() && qs->is_array() && !qs->empty()) {
        aiMessage_ += "\n";
        int n = 1;
        for (const auto& q : *qs) {
            if (!q.is_object()) continue;
            aiMessage_ += "\n" + std::to_string(n++) + ". " +
                          q.value("prompt", std::string());
        }
    }
    if (aiMessage_.empty()) {
        if (patched || activated) aiMessage_ = "AI applied safe form guidance.";
        else aiMessage_ = "AI did not find a safe automatic change.";
    }
    if (patched > 0) {
        dirty_ = true;
        lastEditTime_ = ImGui::GetTime();
    }
    st.toast(ui::ToastLevel::Info,
             activated ? "AI selected the next reference well" :
             patched > 0 ? "AI filled safe visible fields" : "AI guidance ready");
}

// C37-F: Minimum conditions for a preview RPC to be worth firing.
// ALL forms now gate on this — box/cylinder need at least one Dim with a valid (>0) value
// rather than firing immediately on open. Selector-based forms need their primary well filled.
static bool readyForPreview(const PMFormDef* form, const std::vector<PMField>& fields) {
    std::string t = form->type;

    // Mirror is ready as soon as it opens — the plane is a DatumRef (always set) and an empty
    // feature list is a valid whole-body mirror.
    if (t == "mirror") return true;

    // Split is ready as soon as it opens — the tool is a DatumRef (always set; an optional face pick
    // overrides it). The engine errors cleanly if the chosen plane misses the part.
    if (t == "split") return true;

    // surf_revolve needs BOTH a profile and an axis: the engine errors "axis required" if OK fires
    // without an axis, so gate on both wells (unlike solid revolve, whose axis can be a sketch
    // centerline resolved engine-side).
    if (t == "surf_revolve") {
        bool haveProfile = false, haveAxis = false;
        for (const auto& f : fields) {
            if (f.def->kind != PMFieldKind::SelectorWell) continue;
            if (std::strcmp(f.def->key, "profile") == 0)   haveProfile = !f.picks.empty();
            else if (std::strcmp(f.def->key, "axis") == 0) haveAxis    = !f.picks.empty();
        }
        return haveProfile && haveAxis;
    }

    // Loft needs at least TWO section profiles before a preview is meaningful.
    if (t == "loft") {
        for (const auto& f : fields)
            if ((f.def->kind == PMFieldKind::SelectorWell) && std::strcmp(f.def->key, "sections") == 0)
                return f.picks.size() >= 2;
        return false;
    }

    // Fillet/chamfer have type-dependent primary wells: a fillet previews on edges
    // (constant/variable) OR middle_face (full round) OR face_set_a (face fillet); a chamfer on
    // edges (distance/angle) OR vertices (vertex chamfer). Ready as soon as any one is filled.
    if (t == "fillet" || t == "chamfer") {
        static const char* anyWell[] = {"edges", "middle_face", "face_set_a", "vertices"};
        for (const auto& f : fields) {
            if (f.def->kind != PMFieldKind::SelectorWell || f.picks.empty()) continue;
            for (const char* k : anyWell)
                if (std::strcmp(f.def->key, k) == 0) return true;
        }
        return false;
    }

    // Selector-gated forms: a primary well (SelectorWell or FeatureWell) must have at least one
    // pick. Patterns gate on their FeatureWell; sweep on profile (path errors surface inline);
    // hole on the face; draft on the faces to taper.
    const char* needWell = nullptr;
    if (t == "extrude" || t == "revolve" || t == "sweep") needWell = "profile";
    else if (t == "hole") needWell = "face";
    else if (t == "draft") needWell = "faces";
    else if (t == "linear_pattern" || t == "circular_pattern") needWell = "features";
    else if (t == "weld_member") needWell = "sketch";  // FeatureWell: path sketch must be picked
    else if (t == "weld_gusset") needWell = "faces";   // two member faces forming the corner
    else if (t == "weld_end_cap") needWell = "face";   // one member end face to close
    else if (t == "boolean") needWell = "targets";   // need at least one target body
    else if (t == "sm_base_flange") needWell = "profile";  // sheet-metal base needs a sketch region
    else if (t == "sm_edge_flange") needWell = "edge";     // sheet-metal edge flange needs a bend edge
    else if (t == "sm_hem")         needWell = "edge";     // hem needs a bend edge
    else if (t == "sm_jog")         needWell = "edge";     // jog needs a bend edge
    else if (t == "axis")    needWell = "a";          // datum axis primary reference
    else if (t == "point")   needWell = "target";     // datum point reference
    else if (t == "rib")     needWell = "profile";    // rib needs an open-contour sketch profile
    // Surfacing — gate each surf feature on its primary (required) ref well, mirroring schema.py.
    else if (t == "surf_extrude" || t == "surf_sweep" ||
             t == "surf_planar")        needWell = "profile";   // profile-driven surfaces
    else if (t == "surf_loft")          needWell = "sections";  // ordered section profiles
    else if (t == "surf_offset")        needWell = "faces";     // faces to offset
    else if (t == "surf_ruled")         needWell = "edge_a";    // first ruling edge
    else if (t == "surf_boundary")      needWell = "dir1";      // direction-1 curves
    else if (t == "surf_fill")          needWell = "edges";     // bounding edge loop
    else if (t == "surf_knit")          needWell = "surfaces";  // surface bodies to knit
    else if (t == "surf_thicken")       needWell = "surface";   // the surface to thicken
    else if (t == "surf_trim")          needWell = "surface";   // the surface being trimmed
    else if (t == "surf_untrim" || t == "surf_delete_face" ||
             t == "surf_replace_face")  needWell = "faces";     // target faces
    if (needWell) {
        for (const auto& f : fields)
            if ((f.def->kind == PMFieldKind::SelectorWell || f.def->kind == PMFieldKind::FeatureWell)
                && fieldVisible(*f.def, fields)
                && std::strcmp(f.def->key, needWell) == 0)
                return !f.picks.empty();
        return false;  // well not found
    }

    // Primitive forms (box/cylinder): at least one enabled Dim must be >0 and error-free.
    // shell: any non-zero thickness is enough. Revolve with no wells handled above.
    for (const auto& f : fields) {
        if (f.def->kind != PMFieldKind::Dim) continue;
        if (f.def->comingSoon) continue;
        if (!fieldVisible(*f.def, fields)) continue;
        if (!f.dimError.empty()) return false;   // invalid input — don't fire
        if (f.dimValue > 0.0) return true;
    }
    return false;
}

// ---- preview / commit / cancel --------------------------------------------

bool PropertyManager::applyPreview(AppState& st) {
    if (closeForAgentLock(st)) return false;
    if (!st.docMirror.hasDoc() || !st.workerStarted) {
        previewError_ = "Document runtime is not ready.";
        return false;
    }
    if (!readyForPreview(form_, fields_)) {
        previewError_ = "Select the required references and enter valid dimensions before accepting.";
        dirty_ = false;
        return false;
    }

    // C37-F: Defensive check — if the target feature has vanished from the mirror tree (agent
    // run / external undo replaced the doc), close the PM rather than firing an edit_feature
    // that will be rejected. Only relevant for the edit-path where targetId_ is known.
    if (firstPreviewDone_ && !targetId_.empty()) {
        const nlohmann::json& tree = st.docMirror.tree();
        bool found = false;
        if (tree.is_object()) {
            auto feat = tree.find("features");
            if (feat != tree.end() && feat->is_array()) {
                for (const auto& f : *feat)
                    if (f.is_object() && f.value("id", std::string()) == targetId_) { found = true; break; }
            }
        }
        if (!found) {
            st.toast(ui::ToastLevel::Warn, "Feature " + targetId_ + " no longer exists — closing PM");
            st.addLog("[pm] tick: feature " + targetId_ + " vanished; closing PM");
            close();
            return false;
        }
    }

    nlohmann::json feature;
    buildOpFeature(feature);

    // Fillet/chamfer preview is LIVE and APPLIED (SolidWorks style): the feature stays un-suppressed
    // so the viewport shows the actual rounded result as the user edits. The original (pre-round)
    // edges are drawn as dashed 3D lines (see viewport_panel + collectActiveWellEdgeSegments) so the
    // user can still see and deselect them even after they're rounded away. Picking a NEW (un-rounded)
    // edge on the live body resolves correctly now that make_pick_selector enumerates edges in the
    // tessellator's unique order (the earlier "couldn't be located" / 3-way-corner-drop bug).
    const std::string ftype = feature.value("type", std::string());
    const bool blend = (ftype == "fillet" || ftype == "chamfer");

    nlohmann::json ops = nlohmann::json::array();
    if (!firstPreviewDone_ && targetId_.empty()) {
        ops.push_back({{"op", "add_feature"}, {"feature", feature}, {"before", nullptr}});
    } else {
        // Subsequent previews / edit-path: edit the existing feature in place.
        if (targetId_.empty()) {
            previewError_ = "Preview target was not created.";
            dirty_ = false;
            return false;
        }
        ops.push_back({{"op", "edit_feature"}, {"id", targetId_},
                       {"params", feature.value("params", nlohmann::json::object())},
                       {"refs",   feature.value("refs",   nlohmann::json::object())}});
    }
    if (blend)
        st.addLog("[pm] blend preview: edges=" + std::to_string(
                      feature.value("refs", nlohmann::json::object())
                          .value("edges", nlohmann::json::array()).size()));

    // T-PERF1: async applyOps so typing stays live during a multi-second rebuild. Serialize
    // in-flight previews so the first add_feature can learn targetId_ before a second batch
    // races a second add. Latest-wins on the mirror is handled by mutationGen inside
    // applyOpsAsync; we just refuse to queue a second while one is open.
    if (previewInFlight_) {
        // Keep dirty_ true so tick re-fires after the in-flight result drains.
        return false;
    }
    dirty_ = false;
    previewInFlight_ = true;
    ++st.rebuildsInFlight;
    const bool coalesce = previewUndoGroupStarted_;
    const bool blendCap = blend;
    // Disposable live preview -> short timeout; a genuine commit -> patient default (never abort).
    const int timeoutMs = committing_ ? 120000 : kPreviewTimeoutMs;
    st.docMirror.applyOpsAsync(st.rpc, st.worker, ops,
        [this, &st, blendCap](const core::OpsResponse& resp, bool applied) {
            previewInFlight_ = false;
            --st.rebuildsInFlight;
            // Form may have closed while the RPC was in flight.
            if (!active_) return;
            if (!applied || !resp.ok) {
                // C38 (P4f): inline previewError_ only — never toast per-keystroke failures.
                if (!applied && resp.ok) {
                    // Superseded by a newer mutation (or doc switch) — silently drop.
                    return;
                }
                previewError_ = resp.error_message.empty() ? resp.error_code : resp.error_message;
                st.addLog("[pm] preview failed: " + previewError_);
                return;
            }
            previewError_.clear();
            if (!previewUndoGroupStarted_) {
                previewUndoGroupStarted_ = true;
                ++previewBatches_;
            }
            if (targetId_.empty() && resp.id_map.is_object()) {
                auto it = resp.id_map.find("$0");
                if (it != resp.id_map.end() && it->is_string())
                    targetId_ = it->get<std::string>();
            }
            firstPreviewDone_ = true;

            if (!resp.aicm_path.empty())
                st.loadDocMeshAsync(resp.aicm_path);  // T-PERF3: parse off GL thread

            previewWarning_.clear();
            if (!targetId_.empty() && resp.feature_status.is_array()) {
                for (const auto& s : resp.feature_status) {
                    if (!s.is_object() || s.value("id", std::string()) != targetId_) continue;
                    auto wt = s.find("warning");
                    if (wt != s.end() && wt->is_string())
                        previewWarning_ = wt->get<std::string>();
                    const bool sok = s.value("ok", true);
                    if (!sok) previewError_ = s.value("error", std::string());
                    if (blendCap)
                        st.addLog("[pm] feature status " + targetId_ +
                                  ": ok=" + std::string(sok ? "1" : "0") +
                                  " warn=\"" + (wt != s.end() && wt->is_string()
                                                    ? wt->get<std::string>() : "") + "\"" +
                                  (sok ? "" : " err=\"" + s.value("error", std::string()) + "\""));
                    auto dr = s.find("dangling_refs");
                    std::vector<std::string> dangling;
                    if (dr != s.end() && dr->is_array())
                        for (const auto& d : *dr)
                            if (d.is_string()) dangling.push_back(d.get<std::string>());
                    for (auto& f : fields_) {
                        if (f.def->kind != PMFieldKind::SelectorWell) continue;
                        f.wellDangling = false;
                        for (const auto& dref : dangling)
                            if (dref.rfind(f.def->key, 0) == 0) f.wellDangling = true;
                    }
                }
            }
        }, coalesce, timeoutMs);
    return true;
}

void PropertyManager::tick(AppState& st) {
    if (closeForAgentLock(st)) return;
    // Phase 4: keep the daemon edit-hold fresh while the form is open. The
    // daemon already refreshes it on every preview op; this covers an open-but-
    // idle form, rate-limited to once/second.
    if (active_ && !holdDocId_.empty() && ImGui::GetTime() - lastHoldTime_ > 1.0) {
        pmEditHold(holdDocId_);
        lastHoldTime_ = ImGui::GetTime();
    }
    if (!active_ || !dirty_ || previewInFlight_) return;
    double now = ImGui::GetTime();
    if (now - lastEditTime_ < kDebounceSec) return;
    applyPreview(st);
    // applyPreview may have called close() if the feature vanished; check active_ again.
    (void)active_;
}

void PropertyManager::commit(AppState& st) {
    if (closeForAgentLock(st)) return;
    // Make sure the latest field state is reflected before committing.
    if (!readyForPreview(form_, fields_)) {
        previewError_ = "Select the required references and enter valid dimensions before accepting.";
        st.addLog("[pm] OK blocked: form is not ready");
        return;
    }
    // The fillet/chamfer is already applied live in the session, so commit just flushes any pending
    // edit and closes — no un-suppress step is needed anymore.
    // Thread previews run at a SHORTENED proxy length for speed (buildOpFeature); the commit MUST
    // rebuild at the true length even when nothing changed since the last preview, so `committing_`
    // both suppresses the proxy shortening AND forces a final full-quality apply for the thread form.
    committing_ = true;
    const bool forceFullRebuild = form_ && std::string(form_->type) == "thread";
    if (dirty_ || !firstPreviewDone_ || forceFullRebuild) {
        if (!applyPreview(st)) {
            committing_ = false;
            st.addLog("[pm] OK blocked: preview/apply failed");
            return;
        }
    }
    committing_ = false;
    if (!previewError_.empty()) {
        st.addLog("[pm] OK blocked: " + previewError_);
        return;
    }
    st.addLog("[pm] OK -> committed " + (targetId_.empty() ? std::string("(no geometry)") : targetId_));
    // The feature stays in the document; the tree returns on close.
    close();
}

bool PropertyManager::cancelEdits(AppState& st) {
    if (closeForAgentLock(st)) return true;
    // C37-F: Single revert call replaces the old N-undo loop, eliminating the UI freeze.
    bool ok = true;
    if (!checkpointId_.empty()) {
        auto r = st.docMirror.revert(st.worker, checkpointId_);
        if (r.ok) {
            if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
            st.addLog("[pm] Cancel -> reverted to checkpoint (was " +
                      std::to_string(previewBatches_) + " preview batch(es))");
        } else {
            st.toast(ui::ToastLevel::Error, "Cancel revert failed: " + r.error_message);
            st.addLog("[pm] Cancel revert failed: " + r.error_message);
            ok = false;
        }
    } else {
        // Fallback: checkpoint wasn't available at open — use the undo loop.
        int n = previewBatches_;
        for (int i = 0; i < n; ++i) {
            auto r = st.docMirror.undo(st.worker);
            if (!r.ok) { st.addLog("[pm] cancel undo failed: " + r.error_message); ok = false; break; }
            if (!r.aicm_path.empty()) { std::string le; st.loadDocMesh(r.aicm_path, le); }
        }
        if (ok)
            st.addLog("[pm] Cancel -> undid " + std::to_string(n) + " preview batch(es) (no checkpoint)");
    }
    if (!ok) return false;
    close();
    return true;
}

// ---- drawing --------------------------------------------------------------

// Liquid glass v2 (host contract §H4): the PropertyManager form renders EMBEDDED inside the pinned
// FeatureManager glass column (layout.cpp drawLeftPane -> drawBody), which already painted ONE glass
// surface for the WHOLE pane via ui::adaptivePanel(panelId("host_left_glass"), …). We must NOT paint
// our own glassPanel (that would double-paint / fight the column background). Instead we fetch that
// SAME surface (same id + group => same cached AdaptiveSurface, idempotent per frame — design doc
// A2) so labels/values read against the LIVE glass tint (surf.textPrimary/textSecondary track the
// backdrop luminance in both themes) instead of the theme-static pal.text/pal.textDim. Mirrors
// feature_tree.cpp's fmSurf(). Semantic roles (accent/status/selection) stay on palette.
static const ui::AdaptiveSurface& pmSurf() {
    ImVec2 wp = ImGui::GetWindowPos();
    ImVec2 ws = ImGui::GetWindowSize();
    ImRect r(wp, ImVec2(wp.x + ws.x, wp.y + ws.y));
    // Group tag MUST match layout.cpp's paint site (adaptivePanel contract: same group every frame
    // for a given id) so the FeatureManager per-group glass knobs ride through unchanged.
    return ui::adaptivePanel(ui::panelId("host_left_glass"), r, ui::GlassGroup::FeatureManager);
}

// Forward decls — used by the exported selection-well / form header below.
static void pushTextColorV(const ImVec4& c);
static void pushTextColor(const float c[4]);
static void pmTooltip(const char* fmt, ...);

// ---- exported selection-well + form header (asm tools + feature forms share these) ------------

PMSelectionWellResult pmDrawSelectionWell(const char* id, const char* label,
                                          const char* chipText, bool active,
                                          bool dangling) {
    PMSelectionWellResult r;
    const auto& pal = ui::palette;
    const ui::AdaptiveSurface& wsurf = pmSurf();
    const float S = ui::uiScale;

    if (active) pushTextColor(pal.accent);
    else        pushTextColorV(wsurf.textSecondary);
    ImGui::TextUnformatted(label ? label : "");
    ImGui::PopStyleColor();
    if (active) {
        const char* pickHint = "Pick in viewport";
        const float hintW = ImGui::CalcTextSize(pickHint).x;
        ImGui::SameLine(0, 0.0f);
        const float remain = ImGui::GetContentRegionAvail().x;
        if (remain > hintW)
            ImGui::SetCursorPosX(ImGui::GetCursorPosX() + remain - hintW);
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 0.75f));
        ImGui::TextUnformatted(pickHint);
        ImGui::PopStyleColor();
    }

    const char* chip = (chipText && chipText[0]) ? chipText : "(pick in viewport)";
    ImVec4 border = dangling
        ? ImVec4(pal.statusErr[0], pal.statusErr[1], pal.statusErr[2], 1)
        : (active
            ? ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 1)
            : ImVec4(pal.border[0], pal.border[1], pal.border[2], pal.border[3]));
    const float availW = ImGui::GetContentRegionAvail().x;
    const float textPadX = 10.0f * S;
    const float textPadY = 7.0f * S;
    const float wrapW = std::max(40.0f * S, availW - textPadX * 2.0f);
    ImVec2 chipSz = ImGui::CalcTextSize(chip, nullptr, false, wrapW);
    float wellH = std::max(ImGui::GetFrameHeight(), chipSz.y + textPadY * 2.0f);

    ImGui::PushID(id ? id : "##well");
    ImVec2 wp0 = ImGui::GetCursorScreenPos();
    ImVec2 wp1(wp0.x + availW, wp0.y + wellH);
    if (ImGui::InvisibleButton("##well", ImVec2(availW, wellH)))
        r.clicked = true;
    r.hovered = ImGui::IsItemHovered();
    if (ImGui::IsItemClicked(ImGuiMouseButton_Right))
        r.rightClicked = true;
    ImDrawList* dl = ImGui::GetWindowDrawList();
    if (active) {
        ui::glassRowBackground(dl, wp0, wp1, 6.0f * S, /*stripe=*/false,
                               /*selected=*/true, 0.42f);
    } else {
        ui::glassRowBackground(dl, wp0, wp1, 6.0f * S, /*stripe=*/false,
                               /*selected=*/false, r.hovered ? 0.42f : 0.28f);
    }
    dl->AddRect(wp0, wp1, ImGui::ColorConvertFloat4ToU32(border), 6.0f * S, 0,
                active ? std::max(1.0f, 2.0f * S) : std::max(1.0f, 1.0f * S));
    ImU32 chipCol = (!chipText || !chipText[0] || std::strcmp(chip, "(pick in viewport)") == 0)
        ? ImGui::ColorConvertFloat4ToU32(wsurf.textSecondary)
        : ImGui::ColorConvertFloat4ToU32(wsurf.textPrimary);
    // Treat empty-looking chips (placeholder) as secondary; callers pass a filled label otherwise.
    if (chipText && chipText[0] && std::strcmp(chipText, "(pick in viewport)") != 0)
        chipCol = ImGui::ColorConvertFloat4ToU32(wsurf.textPrimary);
    else
        chipCol = ImGui::ColorConvertFloat4ToU32(wsurf.textSecondary);
    dl->AddText(nullptr, 0.0f, ImVec2(wp0.x + textPadX, wp0.y + textPadY),
                chipCol, chip, nullptr, wrapW);
    if (r.hovered) {
        if (active) pmTooltip("Active — pick faces/edges in the viewport");
        else if (chipText && chipText[0] && std::strcmp(chipText, "(pick in viewport)") != 0)
            pmTooltip("Click to re-activate; right-click to clear");
        else
            pmTooltip("Click to activate pick target");
    }
    if (dangling) {
        pushTextColor(pal.statusErr);
        ImGui::TextUnformatted("! dangling reference");
        ImGui::PopStyleColor();
    }
    ImGui::PopID();
    return r;
}

void pmDrawFormHeader(const char* icon, const char* title, const char* description) {
    const auto& pal = ui::palette;
    const ui::AdaptiveSurface& surf = pmSurf();
    const float S = ui::uiScale;
    const float glyphSz = 26.0f * S;
    const float hdrPadV = 7.0f * S;
    const float hdrPadH = 10.0f * S;
    const float accentBarW = 4.0f * S;
    const float rowAvail = std::max(1.0f, ImGui::GetContentRegionAvail().x);
    const float hdrH = glyphSz + hdrPadV * 2.0f;

    ImVec2 hdrMin = ImGui::GetCursorScreenPos();
    ImVec2 hdrMax(hdrMin.x + rowAvail, hdrMin.y + hdrH);
    ImDrawList* dl = ImGui::GetWindowDrawList();
    ui::glassBackground(dl, hdrMin, hdrMax, 6.0f);
    {
        const float bi = 4.0f * S;
        dl->AddRectFilled(ImVec2(hdrMin.x,             hdrMin.y + bi),
                          ImVec2(hdrMin.x + accentBarW, hdrMax.y - bi),
                          Palette::toU32(pal.accent), 2.0f * S);
    }
    ImGui::SetCursorScreenPos(ImVec2(hdrMin.x + accentBarW + hdrPadH, hdrMin.y + hdrPadV));
    float afterGlyphX = hdrMin.x + accentBarW + hdrPadH;
    if (icon && icon[0]) {
        IconUV ic = getIcon(icon, 32);
        if (ic.valid) {
            ImGui::Image(ic.texId, ImVec2(glyphSz, glyphSz), ic.uv0, ic.uv1);
            afterGlyphX += glyphSz + 8.0f * S;
        }
    }
    float titleY = hdrMin.y + (hdrH - ImGui::GetTextLineHeight()) * 0.5f;
    ImGui::SetCursorScreenPos(ImVec2(afterGlyphX, titleY));
    pushTextColor(pal.accent);
    ImGui::TextUnformatted(title ? title : "");
    ImGui::PopStyleColor();
    ImGui::SetCursorScreenPos(ImVec2(hdrMin.x, hdrMax.y + 4.0f * S));
    if (description && description[0]) {
        pushTextColorV(surf.textSecondary);
        float wrapW = ImGui::GetContentRegionAvail().x;
        ImGui::PushTextWrapPos(ImGui::GetCursorPosX() + wrapW);
        ImGui::TextUnformatted(description);
        ImGui::PopTextWrapPos();
        ImGui::PopStyleColor();
        ImGui::Dummy(ImVec2(0, 2.0f * S));
    }
}

void PropertyManager::openGuest(AppState& st, const char* title, const char* icon,
                                GuestFn body, GuestFn onOk, GuestFn onCancel, bool readOnly) {
    if (!readOnly && st.activeDocLockedByAgent()) {
        st.toast(ui::ToastLevel::Warn, st.agentLockReason(st.docMirror.docId()));
        return;
    }
    // Guest entry keeps any live feature preview, releasing its edit_hold without a revert.
    closeIfActive();
    sessionDocId_ = st.docMirror.docId();
    guestReadOnly_ = readOnly;
    guestActive_ = true;
    guestTitle_ = title ? title : "";
    guestIcon_ = icon ? icon : "";
    guestBody_ = std::move(body);
    guestOnOk_ = std::move(onOk);
    guestOnCancel_ = std::move(onCancel);
}

void PropertyManager::closeGuest() {
    guestActive_ = false;
    guestReadOnly_ = false;
    sessionDocId_.clear();
    guestTitle_.clear();
    guestIcon_.clear();
    guestBody_ = nullptr;
    guestOnOk_ = nullptr;
    guestOnCancel_ = nullptr;
}

// Push a text color from an adaptive ImVec4 (surf.textPrimary/textSecondary).
static void pushTextColorV(const ImVec4& c) { ImGui::PushStyleColor(ImGuiCol_Text, c); }

static void pushTextColor(const float c[4]) {
    ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(c[0], c[1], c[2], c[3]));
}

// T-G14: drawBody() pushes ImGuiCol_Text = surf.textPrimary (adaptive, DARK over the light
// viewport) as the default so on-panel VALUE text tracks the frosted-glass tint. But a tooltip is
// a SEPARATE popup window whose PopupBg is the THEME-toned glassBg (dark in the dark theme), so it
// must use THEME-toned text (pal.text = light in the dark theme), not the adaptive backdrop tone —
// otherwise the inherited surf.textPrimary paints dark text on the dark popup (black-on-black).
// Mirror the open-combo pattern (which re-pushes pal.text). Use this for every tooltip inside the
// drawBody/label call-graph.
static void pmTooltip(const char* fmt, ...) {
    const auto& t = ui::palette.text;
    ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(t[0], t[1], t[2], t[3]));
    va_list args;
    va_start(args, fmt);
    ImGui::SetTooltipV(fmt, args);
    va_end(args);
    ImGui::PopStyleColor();
}

// C37-F: descriptions for each form type shown under the title.
static const char* formDescription(const char* type) {
    if (!type) return "";
    if (std::strcmp(type, "box")      == 0) return "Create a rectangular solid at the origin.";
    if (std::strcmp(type, "cylinder") == 0) return "Create a cylinder at the origin.";
    if (std::strcmp(type, "extrude")  == 0) return "Extrude a sketch profile along its normal.";
    if (std::strcmp(type, "revolve")  == 0) return "Revolve a profile around an axis.";
    if (std::strcmp(type, "fillet")   == 0) return "Round selected edges with a constant or variable radius.";
    if (std::strcmp(type, "chamfer")  == 0) return "Bevel selected edges by distance.";
    if (std::strcmp(type, "shell")    == 0) return "Hollow the solid to a uniform wall thickness.";
    return "";
}

static bool pmCompactLabel(const char* label, const char* suffix,
                           const Palette& pal, bool enabled) {
    (void)pal;
    const float S = ui::uiScale;
    const float avail = ImGui::GetContentRegionAvail().x;
    const bool inlineRow = avail >= 220.0f * S;
    float labelW = avail * 0.40f;
    labelW = std::max(112.0f * S, std::min(labelW, 138.0f * S));

    // Liquid glass v2: labels read against the LIVE column glass tint (adaptive max-contrast text)
    // instead of theme-static pal.text/pal.textDim. Enabled -> primary, disabled -> dimmer secondary.
    const ui::AdaptiveSurface& surf = pmSurf();
    pushTextColorV(enabled ? surf.textPrimary : surf.textSecondary);
    std::string full = std::string(label) + (suffix ? suffix : "");
    if (inlineRow) {
        ImGui::AlignTextToFramePadding();
        // T-G14: the field column starts at SameLine(labelW). If the label text is WIDER than the
        // column (e.g. "Tangent propagation"), the next widget lands ON TOP of the tail of the text
        // (the PM-tool-menu overlap). Truncate the drawn label to the column budget with an
        // ellipsis (full text stays in the hover tooltip, per this row's stated two-column intent)
        // so the widget at labelW never overlaps.
        const float gap    = ImGui::GetStyle().ItemInnerSpacing.x;
        const float budget = labelW - gap;
        std::string shown = full;
        bool clipped = false;
        if (ImGui::CalcTextSize(shown.c_str()).x > budget && budget > 0.0f) {
            clipped = true;
            const char* ell = "\xE2\x80\xA6";  // U+2026 ellipsis
            const float ellW = ImGui::CalcTextSize(ell).x;
            while (!shown.empty() &&
                   ImGui::CalcTextSize(shown.c_str()).x + ellW > budget)
                shown.pop_back();
            shown += ell;
        }
        ImGui::TextUnformatted(shown.c_str());
        ImGui::PopStyleColor();
        (void)clipped;
        if (ImGui::IsItemHovered()) pmTooltip("%s", full.c_str());
        ImGui::SameLine(labelW);
        return true;
    }

    ImGui::TextUnformatted(full.c_str());
    ImGui::PopStyleColor();
    return false;
}

// Optional / secondary PM groups start collapsed; primary required groups stay open.
// Matched by group title string from pm_forms.cpp (e.g. "Direction 2", "Options").
static bool pmGroupDefaultOpen(const char* label) {
    if (!label || !label[0]) return true;
    // Secondary / optional groups — keep this list in sync with optional sections in pm_forms.
    static const char* kCollapsedDefaults[] = {
        "Direction 2",
        "Thin Feature",
        "Options",
        "Selected Contours",
        "Guide Curves",
        "Advanced",
        "Instances to Skip",
        "Centerline",
        "Tangency",
    };
    for (const char* g : kCollapsedDefaults)
        if (std::strcmp(label, g) == 0) return false;
    return true;
}

// NOT a ui::kit::sectionHeader in disguise: this is the PM group's DISCLOSURE control — an
// ImGui::CollapsingHeader whose open state it returns, with the app's shared glass row pill behind
// it and the adaptive-surface ink T-G14 requires over the live viewport. The kit's sectionHeader is
// a static caption and its collapsible primitive (cardBegin/cardEnd) is a paired container, which
// this call site cannot be: the group card that follows is opened here and closed by the NEXT
// group's header. Nothing below duplicates a kit painter.
static bool pmSectionHeader(const char* label, const Palette& pal) {  // gui-lint: allow (disclosure control, not a caption painter)
    if (!label || !label[0]) return true;
    const float S = ui::uiScale;
    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(9.0f * S, 4.0f * S));
    ImGui::PushStyleVar(ImGuiStyleVar_FrameRounding, 6.0f * S);

    // Liquid glass v2: the section header is a hoverable row, so give it a translucent glass PILL
    // (glassRowBackground) consistent with the rest of the app (FeatureManager rows / the collapse
    // chevron in layout.cpp) instead of the theme-static glassBg Header fill. ImGui paints the
    // Header* fills UNDER our pill draw, so make them transparent and let the pill be the surface.
    // Section captions read against the live glass tint (adaptive secondary text). Active state keeps
    // the semantic accent wash so an open/pressed header still flashes accent (unchanged intent).
    const ui::AdaptiveSurface& surf = pmSurf();
    ImGui::PushStyleColor(ImGuiCol_Header,        ImVec4(0, 0, 0, 0));
    ImGui::PushStyleColor(ImGuiCol_HeaderHovered, ImVec4(0, 0, 0, 0));
    ImGui::PushStyleColor(ImGuiCol_HeaderActive,
        ImVec4(pal.accentSoft[0], pal.accentSoft[1], pal.accentSoft[2], pal.accentSoft[3]));
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);

    // Draw the frosted pill FIRST (behind the header text/arrow) at the row rect. Hover reads last
    // frame's state via IsItemHovered() after submit would paint over the label, so use the mouse
    // rect test up-front for the brighter hover wash. The pill sits on the window draw list before
    // CollapsingHeader submits its glyphs, so text/arrow render on top.
    const ImVec2 rowMin = ImGui::GetCursorScreenPos();
    const float  rowW   = ImGui::GetContentRegionAvail().x;
    const float  rowH   = ImGui::GetFrameHeight();
    const ImVec2 rowMax(rowMin.x + rowW, rowMin.y + rowH);
    const bool hov = ImGui::IsMouseHoveringRect(rowMin, rowMax);
    ui::glassRowBackground(ImGui::GetWindowDrawList(), rowMin, rowMax, 6.0f * S,
                           /*stripe=*/false, /*selected=*/false, hov ? 0.42f : 0.30f);
    // Force default open/closed when the header appears (form open / first show). User toggles
    // stick for the rest of the session while the form stays open (ImGuiCond_Appearing).
    ImGui::SetNextItemOpen(pmGroupDefaultOpen(label), ImGuiCond_Appearing);
    const bool open = ImGui::CollapsingHeader(label);

    ImGui::PopStyleColor(4);
    ImGui::PopStyleVar(2);
    return open;
}

// T-G19: OK / Cancel command buttons, relocated ABOVE the PM into the left-pane top row (between
// the Configurations tab and the collapse chevron). Rendered by layout.cpp from that
// row while a form is active. Wider than the old 26dp squares, with regenerated ok_check / cancel_x
// glyphs. The cluster is right-aligned to END at `rightX`, vertically centered on `centerY`. Clicking
// OK commits (commit() still guards form readiness + toasts on failure); Cancel reverts every preview
// batch. Returns the cluster's pixel width (0 when inactive) so the caller can reserve space.
float PropertyManager::drawChromeButtons(AppState& st, float rightX, float centerY) {
    if (closeForAgentLock(st)) return 0.0f;
    if (!active()) return 0.0f;
    if (active_ && !guestActive_ && !form_) return 0.0f;
    const float S   = ui::uiScale;
    const auto& pal = ui::palette;
    // TASK C: larger, SQUARE, easy-to-hit targets. The old cluster was a 40x26 non-square pair using
    // atlas sprites; these are generous 34dp squares — the biggest square that still fits inside the
    // ~42dp left-pane header band (rowPad 6 + btn 30 + rowPad 6) without spilling into the tabs or the
    // separator below. Square + bigger = an obvious click target, per the brief.
    const float btn      = 34.0f * S;    // square edge
    const float gap      = 8.0f * S;
    const float clusterW = btn * 2.0f + gap;
    const float x0       = rightX - clusterW;
    const float y0       = centerY - btn * 0.5f;

    // Draw at absolute screen positions but leave the caller's layout cursor untouched.
    const ImVec2 savedCur = ImGui::GetCursorScreenPos();
    ImGui::PushID("pm_chrome_okcancel");
    bool ok = false, cancel = false;

    // TASK B: crisp, programmatic glyphs drawn straight onto the button rect via ImDrawList — no more
    // muddy atlas sprite, and no atlas/POST_BUILD staging hazard. A clean two-segment check and a
    // two-stroke X, both centred, thickness scaled with the button so they read at any DPI.
    auto drawCheck = [&](ImDrawList* dl, const ImVec2& c, float half, ImU32 col, float th) {
        // Classic checkmark: short down-stroke from upper-left to a low pivot, long up-stroke to the
        // upper-right. Proportions tuned to look balanced inside a square.
        ImVec2 p0(c.x - half * 0.72f, c.y + half * 0.05f);
        ImVec2 p1(c.x - half * 0.18f, c.y + half * 0.58f);
        ImVec2 p2(c.x + half * 0.74f, c.y - half * 0.55f);
        dl->PathLineTo(p0); dl->PathLineTo(p1); dl->PathLineTo(p2);
        dl->PathStroke(col, ImDrawFlags_None, th);
        // Round the corners/ends so the crook reads clean, not chiselled.
        dl->AddCircleFilled(p0, th * 0.5f, col);
        dl->AddCircleFilled(p1, th * 0.5f, col);
        dl->AddCircleFilled(p2, th * 0.5f, col);
    };
    auto drawX = [&](ImDrawList* dl, const ImVec2& c, float half, ImU32 col, float th) {
        float r = half * 0.62f;
        dl->AddLine(ImVec2(c.x - r, c.y - r), ImVec2(c.x + r, c.y + r), col, th);
        dl->AddLine(ImVec2(c.x + r, c.y - r), ImVec2(c.x - r, c.y + r), col, th);
    };

    ImDrawList* dl = ImGui::GetWindowDrawList();

    // Confirm (OK) + Cancel: neutral rest fill always. Hover/active is a status-tinted glowing
    // edge (layered outer rings + crisp border + faint inner wash) — never a solid status block.
    // Matches the soft-halo language of drawManipArrow / doc_tabs AI pulse, statusOk for check
    // and statusErr for X so the pair reads as a matched set.
    auto drawChromeHoverGlow = [&](const ImVec2& mn, const ImVec2& mx, const float status[4],
                                   bool hovered, bool active) {
        if (!hovered && !active) return;
        const float rnd = ImGui::GetStyle().FrameRounding;
        // Faint status wash inside the face (low alpha so the neutral fill still dominates).
        {
            float wash[4];
            Palette::withAlpha(status, wash, active ? 0.16f : 0.09f);
            dl->AddRectFilled(mn, mx, Palette::toU32(wash), rnd);
        }
        // Soft outer glow: layered AddRect rings stepping outward with decreasing alpha
        // (same layered-halo idea as doc_tabs AI pulse / drawManipArrow under-stroke).
        const float baseA = active ? 0.30f : 0.20f;
        for (int k = 3; k >= 1; --k) {
            const float grow = (float)k * 2.0f * S;
            const float a = baseA * (float)k / 3.0f;
            float g[4]; Palette::withAlpha(status, g, a);
            dl->AddRect(ImVec2(mn.x - grow, mn.y - grow),
                        ImVec2(mx.x + grow, mx.y + grow),
                        Palette::toU32(g), rnd + grow, 0, 2.0f * S);
        }
        // Crisp status-tinted border ring; pressed deepens it.
        {
            float edge[4];
            Palette::withAlpha(status, edge, active ? 1.0f : 0.90f);
            dl->AddRect(mn, mx, Palette::toU32(edge), rnd, 0,
                        (active ? 2.2f : 1.6f) * S);
        }
    };

    // Neutral face for both buttons (rest fill stays put on hover — glow carries the state).
    const ImVec4 faceRest(
        pal.button[0], pal.button[1], pal.button[2], 0.92f);
    const ImVec4 faceHover(
        pal.buttonHovered[0], pal.buttonHovered[1], pal.buttonHovered[2], 0.92f);
    const ImVec4 faceActive(
        pal.buttonActive[0], pal.buttonActive[1], pal.buttonActive[2], 0.95f);
    // Glyph on the neutral face: palette text role is light-on-dark / dark-on-light.
    const ImU32 glyphCol = Palette::toU32(pal.text);

    // Confirm (OK) — statusOk glow on hover/active.
    ImGui::SetCursorScreenPos(ImVec2(x0, y0));
    ImGui::PushStyleColor(ImGuiCol_Button,        faceRest);
    ImGui::PushStyleColor(ImGuiCol_ButtonHovered, faceHover);
    ImGui::PushStyleColor(ImGuiCol_ButtonActive,  faceActive);
    ok = ImGui::Button("##pm_ok", ImVec2(btn, btn));
    if (ImGui::IsItemHovered()) ImGui::SetTooltip("OK (Enter)");
    {
        ImVec2 mn = ImGui::GetItemRectMin(), mx = ImGui::GetItemRectMax();
        ImVec2 c((mn.x + mx.x) * 0.5f, (mn.y + mx.y) * 0.5f);
        drawChromeHoverGlow(mn, mx, pal.statusOk,
                            ImGui::IsItemHovered(), ImGui::IsItemActive());
        drawCheck(dl, c, btn * 0.5f, glyphCol, 3.0f * S);
    }
    ImGui::PopStyleColor(3);

    // Cancel — statusErr glow on hover/active (no solid red face).
    ImGui::SetCursorScreenPos(ImVec2(x0 + btn + gap, y0));
    ImGui::PushStyleColor(ImGuiCol_Button,        faceRest);
    ImGui::PushStyleColor(ImGuiCol_ButtonHovered, faceHover);
    ImGui::PushStyleColor(ImGuiCol_ButtonActive,  faceActive);
    cancel = ImGui::Button("##pm_cancel", ImVec2(btn, btn));
    if (ImGui::IsItemHovered()) ImGui::SetTooltip("Cancel (Esc)");
    {
        ImVec2 mn = ImGui::GetItemRectMin(), mx = ImGui::GetItemRectMax();
        ImVec2 c((mn.x + mx.x) * 0.5f, (mn.y + mx.y) * 0.5f);
        drawChromeHoverGlow(mn, mx, pal.statusErr,
                            ImGui::IsItemHovered(), ImGui::IsItemActive());
        drawX(dl, c, btn * 0.5f, glyphCol, 3.0f * S);
    }
    ImGui::PopStyleColor(3);

    ImGui::PopID();
    ImGui::SetCursorScreenPos(savedCur);   // restore the caller's layout flow

    if (guestActive_) {
        if (ok && guestOnOk_)          guestOnOk_(st);
        else if (cancel && guestOnCancel_) guestOnCancel_(st);
        else if (cancel)              closeGuest();
    } else {
        if (ok)     commit(st);
        else if (cancel) cancelEdits(st);
    }
    return clusterW;
}

// Wave 1D: standalone wrapper — owns the FeatureManager-slot Begin/End plus the C37-F floating
// fallback, then defers the form content to drawBody(). The Wave 1D tabbed left pane calls
// drawBody() directly inside its "LeftPane" container child (no separate window).
void PropertyManager::draw(AppState& st) {
    if (!active_) return;

    // C37-F: Floating fallback — if the FeatureManager dock window isn't visible this frame
    // (dock closed or un-docked and minimised), draw the PM as an independent floating window
    // so it can never be invisibly active and trap keyboard/pick input.
    bool dockVisible = ImGui::Begin("FeatureManager");
    if (!dockVisible) {
        ImGui::End();
        // Open as a floating fallback.
        ImGui::SetNextWindowSize(ImVec2(260.0f * ui::uiScale, 400.0f * ui::uiScale),
                                 ImGuiCond_Appearing);
        ImGui::SetNextWindowPos(ImVec2(80.0f * ui::uiScale, 80.0f * ui::uiScale),
                                ImGuiCond_Appearing);
        // Frosted-glass fill so the floating PropertyManager fallback joins the app-wide glass
        // family (the docked FeatureManager keeps its solid panel chrome). T-G14: use the ADAPTIVE
        // tint for the intended window rect (NOT the theme-static pal.glassBg, which is dark in the
        // dark theme and gave black-on-black over the light viewport). drawBody's own pmSurf()
        // samples the same backdrop for text, so fill + text stay contrasting. Same panelId as the
        // docked column so both resolve to the same cached surface.
        const float uS = ui::uiScale;
        const ImRect floatRect(ImVec2(80.0f * uS, 80.0f * uS),
                               ImVec2((80.0f + 260.0f) * uS, (80.0f + 400.0f) * uS));
        const ui::AdaptiveSurface& fsurf =
            ui::adaptivePanel(ui::panelId("host_left_glass"), floatRect, ui::GlassGroup::FeatureManager);
        ImGui::PushStyleColor(ImGuiCol_WindowBg,
            ImVec4(fsurf.tint.x, fsurf.tint.y, fsurf.tint.z, 0.92f));
        const bool pmFloat = ImGui::Begin("##PMFloating", nullptr,
                          ImGuiWindowFlags_NoCollapse | ImGuiWindowFlags_NoNav);
        ImGui::PopStyleColor();
        if (!pmFloat) {
            ImGui::End(); return;
        }
    }
    drawBody(st);
    ImGui::End();
}

// Wave 1D: the form's inner content (no Begin/End). Caller owns the surrounding window/child.
void PropertyManager::drawBody(AppState& st) {
    if (closeForAgentLock(st)) return;
    // Guest assembly forms (Mate/Insert/…) occupy the same left-pane slot as feature forms.
    if (guestActive_) {
        const ui::AdaptiveSurface& surf = pmSurf();
        ImGui::PushStyleColor(ImGuiCol_FrameBg,
            ImVec4(surf.tint.x, surf.tint.y, surf.tint.z, 0.16f));
        ImGui::PushStyleColor(ImGuiCol_FrameBgHovered,
            ImVec4(surf.tint.x, surf.tint.y, surf.tint.z, 0.26f));
        ImGui::PushStyleColor(ImGuiCol_FrameBgActive,
            ImVec4(surf.tint.x, surf.tint.y, surf.tint.z, 0.30f));
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);

        // Esc cancels the guest form (same policy as feature forms, sketch owns Esc when active).
        const bool sketchActive = st.sketchMode && st.sketchMode->active();
        if (ImGui::IsKeyPressed(ImGuiKey_Escape, false) &&
            !st.sketchPlanePickActive && !sketchActive && !ImGui::GetIO().WantTextInput) {
            if (guestOnCancel_) guestOnCancel_(st);
            else closeGuest();
            ImGui::PopStyleColor(4);
            return;
        }

        pmDrawFormHeader(guestIcon_.c_str(), guestTitle_.c_str(), nullptr);
        ImGui::Separator();
        if (guestBody_) guestBody_(st);

        ImGui::PopStyleColor(4);
        return;
    }

    if (!active_ || !form_) return;

    // Keep a VISIBLE selector well active at all times. A method combo can HIDE the currently-
    // active well (ref_axis/ref_plane use duplicate-key, method-gated wells: switching
    // "Cylindrical Face" -> "Two Planes" hides the face well and reveals two plane wells). The
    // active flag would stay stranded on the now-hidden well, leaving NO visible active well — so
    // the datum-plane/axis picker goes dark and the references don't show until the user clicks the
    // new well. Re-home the active flag onto the first visible selector well (preferring an empty
    // one) so the relevant references are pickable the instant a form opens OR its method changes —
    // SolidWorks selection-first behaviour, for every tool. Only fires when nothing visible is
    // active, so it never steals a well the user is actively filling. (Lives in drawBody, not
    // draw(), because the tabbed left pane calls drawBody() directly — see layout.cpp.)
    {
        // After a deliberate endProfileSelection(), leave wells inactive until the user
        // re-arms (well chip click / region pick). Without this, the re-home undoes dismiss
        // every frame and empty-space click appears to do nothing.
        if (!suppressWellAutoArm_) {
            bool haveVisibleActive = false;
            for (const auto& f : fields_)
                if (f.def->kind == PMFieldKind::SelectorWell && f.wellActive &&
                    fieldVisible(*f.def, fields_)) { haveVisibleActive = true; break; }
            if (!haveVisibleActive) {
                PMField* target = nullptr;
                for (auto& f : fields_) {
                    if (f.def->kind != PMFieldKind::SelectorWell || !fieldVisible(*f.def, fields_))
                        continue;
                    if (!target) target = &f;                       // fallback: first visible well
                    if (f.picks.empty()) { target = &f; break; }    // prefer the first empty well
                }
                if (target) {
                    for (auto& o : fields_)
                        if (o.def->kind == PMFieldKind::SelectorWell ||
                            o.def->kind == PMFieldKind::FeatureWell)
                            o.wellActive = false;
                    target->wellActive = true;
                }
            }
        }
    }

    // Live unit-system toggle: if the document units changed while this form is open (e.g. the
    // user clicked the status-bar MMGS/IPS cluster), reformat every open Dim field's DISPLAY from
    // its canonical mm value. The stored value is unchanged — purely a display reformat.
    if (st.ips() != ips_) { ips_ = st.ips(); reformatDimDisplays(); }

    const auto& pal = ui::palette;
    // Liquid glass v2: the ONE shared column surface (same id layout.cpp painted this frame). Used
    // for adaptive max-contrast SECONDARY/dim text throughout the form so it reads against the live
    // glass tint; semantic roles (accent/status) stay on `pal`. Idempotent per frame (id-cached).
    const ui::AdaptiveSurface& surf = pmSurf();

    // Fix black-on-black: SliderFloat/DragFloat/InputText/BeginCombo draw an OPAQUE dark FrameBg
    // by default. Over the fully-transparent FeatureManager column the adaptive `surf.textPrimary`
    // is dark (contrasts the light viewport), so dark ink on dark frame = invisible text.
    // Push a translucent glass-tinted frame fill so the viewport shows through the field boxes and
    // the adaptive ink stays legible over both light and dark viewport backdrops. Must be popped on
    // EVERY exit path of this function (balanced below at each return and at function end).
    ImGui::PushStyleColor(ImGuiCol_FrameBg,
        ImVec4(surf.tint.x, surf.tint.y, surf.tint.z, 0.16f));
    ImGui::PushStyleColor(ImGuiCol_FrameBgHovered,
        ImVec4(surf.tint.x, surf.tint.y, surf.tint.z, 0.26f));
    ImGui::PushStyleColor(ImGuiCol_FrameBgActive,
        ImVec4(surf.tint.x, surf.tint.y, surf.tint.z, 0.30f));
    // T-G14: value/default text must also track the live glass tint. Field VALUE widgets
    // (SliderFloat/DragFloat/InputText "##dim", Int, Text) render with the DEFAULT ImGuiCol_Text,
    // which is the theme-static pal.text (light in dark theme). Over the light viewport the group
    // card + frame fills are the adaptive LIGHT tint, so theme-static light value text washed out
    // (and dark-on-dark in the palette-fallback regime). Push surf.textPrimary as the default so
    // values contrast the same surface the labels/fills use. In the palette-fallback regime
    // surf.textPrimary == pal.text, so this is a strict no-op there and only corrects the
    // live-backdrop regime. Popped on EVERY exit path (balanced with the 3 FrameBg colors below).
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);

    // ---- C37-F: Keyboard policy -------------------------------------------
    // Gather focus/hover state for this PM window BEFORE processing buttons.
    bool pmWindowFocused = ImGui::IsWindowFocused(ImGuiFocusedFlags_RootAndChildWindows);
    bool pmWindowHovered = ImGui::IsWindowHovered(ImGuiHoveredFlags_RootAndChildWindows |
                                                   ImGuiHoveredFlags_AllowWhenBlockedByActiveItem);
    // C38 (P2f): use last frame's PM-scoped "a PM widget was active" flag (set at the END of this
    // draw, below) instead of a global, prior-frame ImGui::IsAnyItemActive() — the latter is true
    // whenever ANY window has an active item, so Enter in an unrelated panel would commit the PM.
    bool pmWidgetWasActive = pmWidgetActivePrevFrame_;

    const ImGuiIO& io = ImGui::GetIO();

    // C37-F: Esc cancels from anywhere UNLESS sketch-plane-pick mode owns Esc.
    // When a text field is active (WantTextInput), the first Esc deactivates the field (ImGui
    // handles this natively via IsItemDeactivatedAfterEdit / InputText Escape handling).
    // We check WantTextInput BEFORE the key is consumed to avoid double-cancelling the PM.
    bool escPressed = ImGui::IsKeyPressed(ImGuiKey_Escape, false);
    bool cancel = false;
    bool ok     = false;
    // C38 (P1f): sketch mode owns Esc while a sketch is active (its Modify box / tool / cancel all
    // consume it). Without this guard the PM grabs the same Esc the frame the sketch Modify box
    // closes and cancels the PM out from under the user. Skip PM Esc whenever a sketch is active, in
    // addition to the existing plane-pick guard.
    bool sketchActive = st.sketchMode && st.sketchMode->active();
    if (escPressed && !st.sketchPlanePickActive && !sketchActive) {
        if (io.WantTextInput) {
            // A text field is active; Esc will blur it this frame (ImGui's default behaviour).
            // Don't cancel the PM — let the field deactivate first.
        } else {
            cancel = true;
        }
    }

    // ---- header: polished glass command strip ----------------------------------
    {
        const float S         = ui::uiScale;
        const float glyphSz   = 26.0f * S;
        const float hdrPadV   = 7.0f * S;
        const float hdrPadH   = 10.0f * S;
        const float accentBarW = 4.0f * S;
        const float rowAvail  = std::max(1.0f, ImGui::GetContentRegionAvail().x);
        const float hdrH      = glyphSz + hdrPadV * 2.0f;

        // Mode-aware icon: use the cut variant for boss/cut operations when mode_ is "cut".
        const char* hdrIcon = form_->icon;
        {
            const std::string& ft = form_->type;
            if (mode_ == "cut") {
                if      (ft == "extrude") hdrIcon = "extrude_cut";
                else if (ft == "revolve") hdrIcon = "revolve_cut";
                else if (ft == "sweep")   hdrIcon = "sweep_cut";
                else if (ft == "loft")    hdrIcon = "loft_cut";
            }
        }

        // Draw glass background + left accent bar before advancing the cursor.
        ImVec2 hdrMin = ImGui::GetCursorScreenPos();
        ImVec2 hdrMax(hdrMin.x + rowAvail, hdrMin.y + hdrH);
        ImDrawList* dl = ImGui::GetWindowDrawList();
        ui::glassBackground(dl, hdrMin, hdrMax, 6.0f);
        {
            const float bi = 4.0f * S;
            dl->AddRectFilled(ImVec2(hdrMin.x,             hdrMin.y + bi),
                              ImVec2(hdrMin.x + accentBarW, hdrMax.y - bi),
                              Palette::toU32(pal.accent), 2.0f * S);
        }

        // Feature glyph (26 dp; use @32 atlas cell for sharpness).
        ImGui::SetCursorScreenPos(ImVec2(hdrMin.x + accentBarW + hdrPadH, hdrMin.y + hdrPadV));
        float afterGlyphX = hdrMin.x + accentBarW + hdrPadH;
        IconUV ic = getIcon(hdrIcon, 32);
        if (ic.valid) {
            ImGui::Image(ic.texId, ImVec2(glyphSz, glyphSz), ic.uv0, ic.uv1);
            afterGlyphX += glyphSz + 8.0f * S;
        }

        // Title — vertically centred, in accent color.
        float titleY = hdrMin.y + (hdrH - ImGui::GetTextLineHeight()) * 0.5f;
        ImGui::SetCursorScreenPos(ImVec2(afterGlyphX, titleY));
        pushTextColor(pal.accent);
        ImGui::TextUnformatted(title_.c_str());
        ImGui::PopStyleColor();

        // T-G19: OK / Cancel buttons no longer live in this in-PM header strip. They were
        // relocated ABOVE the PM — into the left-pane top row, between the
        // Configurations tab and the collapse chevron — and are rendered by drawChromeButtons(),
        // which layout.cpp calls from that row while a form is active. The keyboard Enter/Esc commit
        // path (below) is unchanged and still drives commit()/cancelEdits() directly.

        // Resume layout below the glass header strip.
        ImGui::SetCursorScreenPos(ImVec2(hdrMin.x, hdrMax.y + 4.0f * S));

        // Optional form description.
        const char* desc = formDescription(form_->type);
        if (desc && desc[0]) {
            pushTextColorV(surf.textSecondary);
            float wrapW = ImGui::GetContentRegionAvail().x;
            ImGui::PushTextWrapPos(ImGui::GetCursorPosX() + wrapW);
            ImGui::TextUnformatted(desc);
            ImGui::PopTextWrapPos();
            ImGui::PopStyleColor();
        }

        // Tight gap under the description / header — Spacing() was too loose above the AI button.
        ImGui::Dummy(ImVec2(0, 2.0f * S));
        if (aiBusy_) ImGui::BeginDisabled();
        ImGui::PushStyleColor(ImGuiCol_Button,
            ImVec4(surf.tint.x, surf.tint.y, surf.tint.z, 0.20f));
        ImGui::PushStyleColor(ImGuiCol_ButtonHovered,
            ImVec4(surf.tint.x, surf.tint.y, surf.tint.z, 0.35f));
        ImGui::PushStyleColor(ImGuiCol_ButtonActive,
            ImVec4(surf.tint.x, surf.tint.y, surf.tint.z, 0.50f));
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
        if (ImGui::Button(aiBusy_ ? "AI is selecting..." : "Select with AI", ImVec2(-1, 0))) {
            requestAiAssist(st);
        }
        ImGui::PopStyleColor(4);
        if (aiBusy_) ImGui::EndDisabled();
        if (ImGui::IsItemHovered())
            pmTooltip("Ask AI to fill safe visible fields or activate the next reference well");
        if (!aiMessage_.empty()) {
            pushTextColor(aiBusy_ ? pal.textDim : pal.accent);
            ImGui::PushTextWrapPos(ImGui::GetCursorPosX() + ImGui::GetContentRegionAvail().x);
            ImGui::TextUnformatted(aiMessage_.c_str());
            ImGui::PopTextWrapPos();
            ImGui::PopStyleColor();
        }

        // C37-F: Enter commits ONLY when the PM window is focused/hovered or a PM widget was
        // active this frame. This prevents Enter-in-chat from accidentally committing the form.
        //
        // BUT: while a text field (a Dim/Int/Text input) is being edited, Enter means "apply the
        // value I just typed to the preview" — NOT "OK the whole feature". io.WantTextInput is true
        // on the frame Enter is pressed (the InputText is still the active item; it deactivates
        // later this same frame), so gating on it lets the field's own EnterReturnsTrue handler take
        // the Enter (parse + schedule preview) instead of the form committing out from under the
        // user. With focus then released, a second Enter (no field active) commits as before.
        if (ImGui::IsKeyPressed(ImGuiKey_Enter, false) && !io.WantTextInput &&
            (pmWindowFocused || pmWindowHovered || pmWidgetWasActive)) {
            ok = true;
        }

        ImGui::Separator();

        // Wave 1D: drawBody() does NOT own the window — early-out is a plain return; the caller
        // (draw() or the LeftPane container) closes its own Begin/child.
        if (ok)     { ImGui::PopStyleColor(4); commit(st);      return; }
        if (cancel) { ImGui::PopStyleColor(4); cancelEdits(st); return; }
    }

    // ---- field rows grouped by `group`, collapsible ------------------------
    const char* curGroup = nullptr;
    bool groupOpen = true;
    bool groupCardOpen = false;
    const bool useIps = ips_;   // document-unit display (set at open; re-synced above on toggle)

    // C37-F: track which Dim field is first/editable for SetKeyboardFocusHere.
    bool firstEditableDimSeen = false;
    // True if the user clicked any SelectorWell chip this frame (re-arm). Exempts that click
    // from the post-loop "click elsewhere in PM ends profile select" dismiss.
    bool wellClickedThisFrame = false;

    // PM group cards are CONTENT-sized groups, not auto-resize child windows. An AutoResizeY child
    // sizes itself from LAST frame's content, so on the frame a form opens (feature edit, command,
    // scenario capture) every card painted as an empty padding-tall shell with its fields clipped
    // away. A group measures this frame's content: fields draw on draw-list channel 1 inside a
    // work rect inset by the card padding, and closeGroupCard paints the wash + border behind them
    // on channel 0 once the height is final, so the form is complete on its very first frame.
    const float cardPadX = 9.0f * ui::uiScale;
    const float cardPadY = 7.0f * ui::uiScale;
    ImVec2 cardMin(0.0f, 0.0f);
    float  cardRight = 0.0f;
    float  backupWorkMaxX = 0.0f, backupRegionMaxX = 0.0f;

    auto openGroupCard = [&]() {
        ImGuiWindow* win = ImGui::GetCurrentWindow();
        ImGui::PushID(curGroup);
        cardMin   = ImGui::GetCursorScreenPos();
        cardRight = cardMin.x + ImGui::GetContentRegionAvail().x;
        // Inset the right edge so full-width rows (wells, combos, "-1" widths, wrapped text) stop
        // at the card padding, exactly like the old child window's content region.
        backupWorkMaxX   = win->WorkRect.Max.x;
        backupRegionMaxX = win->ContentRegionRect.Max.x;
        win->WorkRect.Max.x          = cardRight - cardPadX;
        win->ContentRegionRect.Max.x = cardRight - cardPadX;
        win->DrawList->ChannelsSplit(2);
        win->DrawList->ChannelsSetCurrent(1);
        ImGui::BeginGroup();
        ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing,
                            ImVec2(7.0f * ui::uiScale, 5.0f * ui::uiScale));
        // Group offset stays at the card edge (SameLine(labelW) columns measure from it, as they
        // did from the child's origin); new lines start one padding in.
        ImGui::Indent(cardPadX);
        ImGui::SetCursorScreenPos(ImVec2(cardMin.x + cardPadX, cardMin.y + cardPadY));
        groupCardOpen = true;
    };

    auto closeGroupCard = [&]() {
        if (!groupCardOpen) return;
        ImGuiWindow* win = ImGui::GetCurrentWindow();
        ImGui::Unindent(cardPadX);
        ImGui::PopStyleVar();
        ImGui::EndGroup();
        const float cardBottom = ImGui::GetItemRectMax().y + cardPadY;
        win->WorkRect.Max.x          = backupWorkMaxX;
        win->ContentRegionRect.Max.x = backupRegionMaxX;
        // PM group cards adopt the ADAPTIVE glass tint (surf.tint) — NOT the theme-static
        // pal.glassBg — so the card fill tracks the SAME live-backdrop surface the labels and
        // values read against (surf.textPrimary/textSecondary). T-G14: pal.glassBg is dark in the
        // dark theme, which over the light viewport gave dark card + dark adaptive label text
        // (black-on-black), and light-on-light when the tone flipped. surf.tint contrasts
        // surf.text* by construction in both themes and both regimes.
        const ImGuiStyle& stl = ImGui::GetStyle();
        const ImVec2 cardMax(cardRight, cardBottom);
        win->DrawList->ChannelsSetCurrent(0);
        win->DrawList->AddRectFilled(cardMin, cardMax,
            ImGui::ColorConvertFloat4ToU32(ImVec4(surf.tint.x, surf.tint.y, surf.tint.z, 0.55f)),
            stl.ChildRounding);
        if (stl.ChildBorderSize > 0.0f)
            win->DrawList->AddRect(cardMin, cardMax, Palette::toU32(pal.glassBorder),
                                   stl.ChildRounding, 0, stl.ChildBorderSize);
        win->DrawList->ChannelsMerge();
        // Advance the parent layout past the whole card (bottom padding included).
        ImGui::SetCursorScreenPos(ImVec2(cardMin.x, cardBottom));
        ImGui::Dummy(ImVec2(cardRight - cardMin.x, 0.0f));
        ImGui::PopID();
        ImGui::Dummy(ImVec2(0, 4.0f * ui::uiScale));
        groupCardOpen = false;
    };

    for (size_t i = 0; i < fields_.size(); ++i) {
        PMField& f = fields_[i];
        if (!fieldVisible(*f.def, fields_)) continue;
        // New group header?
        if (!curGroup || std::strcmp(curGroup, f.def->group) != 0) {
            closeGroupCard();
            curGroup = f.def->group;
            groupOpen = pmSectionHeader(curGroup, pal);
            if (groupOpen) openGroupCard();
        }
        if (!groupOpen) continue;

        // Conditional enable: grey-out when the sibling Combo condition isn't met or comingSoon.
        bool enabled = fieldEnabled(*f.def, fields_);
        if (!enabled) ImGui::BeginDisabled();

        ImGui::PushID((int)i);
        if (hasString(aiHighlights_, f.def->key)) {
            pushTextColor(pal.accent);
            ImGui::TextUnformatted("AI suggestion");
            ImGui::PopStyleColor();
        }
        switch (f.def->kind) {
            case PMFieldKind::Dim: {
                const bool isAngle  = pmFieldIsAngle(f.def);
                const bool unitless = pmFieldUnitless(f.def);
                // C37-F: unit suffix in label ("Radius [mm]"). A field may override it (angle =
                // "[deg]"; an empty override = no suffix).
                const char* unitSuffix = f.def->unitLabel ? f.def->unitLabel
                                                          : (useIps ? " [in]" : " [mm]");
                pmCompactLabel(f.def->label, unitSuffix, pal, enabled);
                if (f.def->comingSoon && ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled))
                    pmTooltip("Not yet supported by engine");

                // Widgets audit: bounded dims (angles, draft, k-factor) are sliders; open-ended
                // ones (depth, radii, spacing) are drags — both show the unit in the value text and
                // CTRL+click types an exact number. Unit-math / "=expr" entry lives behind the "="
                // toggle so the SolidWorks expression path is never lost. All conversion happens at
                // the widget edge: dimValue stays canonical mm/deg.
                const double toDisp  = (!isAngle && !unitless && useIps) ? (1.0 / 25.4) : 1.0;
                const bool   bounded = f.def->dimMax > f.def->dimMin;
                // Per-kind step (canonical units): metadata, else 1 mm lengths (1/16 in in IPS),
                // 1 deg angles, 0.05 unit-less ratios. Drives the CTRL+wheel step + drag speed.
                double step = f.def->dimStep;
                if (step <= 0.0)
                    step = isAngle ? 1.0 : (unitless ? 0.05 : (useIps ? 25.4 / 16.0 : 1.0));
                const char* valFmt = isAngle ? "%.4g deg"
                                             : (unitless ? "%.4g" : (useIps ? "%.4g in" : "%.4g mm"));

                // Commit a canonical value coming from the slider/drag/wheel: clamp to the field
                // range, clear any expr/error, and resync the text buffer (the "=" path reads it).
                auto commitDim = [&](double v, bool immediate) {
                    v = std::max(v, f.def->dimMin);
                    if (bounded) v = std::min(v, f.def->dimMax);
                    f.dimValue = v; f.dimExpr.clear(); f.dimError.clear();
                    std::string disp = pmFieldValueText(f.def, f.dimValue, useIps);
                    std::strncpy(f.dimBuf.data(), disp.c_str(), f.dimBuf.size() - 1);
                    dirty_ = true;
                    lastEditTime_ = immediate ? ImGui::GetTime() - kDebounceSec  // fire now
                                              : ImGui::GetTime();               // debounced
                };

                // Reserve room for the "=" toggle so the widget never shoves it past the pane edge.
                const float eqBtnW = ImGui::GetFrameHeight();
                ImGui::SetNextItemWidth(-(eqBtnW + 4.0f * ui::uiScale));

                // C37-F: SetKeyboardFocusHere on the very first editable Dim field when opening
                // (on a drag/slider this opens ImGui's CTRL+click text input, so typing works
                // immediately, same as the old InputText).
                if (wantFocusFirstField_ && enabled && !f.def->comingSoon && !firstEditableDimSeen) {
                    ImGui::SetKeyboardFocusHere(0);
                    firstEditableDimSeen = true;
                    // Don't clear wantFocusFirstField_ yet — clear it after this frame so the
                    // focus request is processed.
                }

                const bool textMode = !f.dimExpr.empty() || f.dimTextEntry;
                if (!textMode) {
                    float v = (float)(f.dimValue * toDisp);
                    // Value-fraction fill bar / drag track inside the numeric field.
                    // 0d717a1a ("PM UX + slider fix") fixed Signed() so open-ended dims correctly
                    // use DragFloat instead of a broken SliderFloat(-1e9..0). That also removed the
                    // native slider grab/track affordance on fields like plane Offset Distance.
                    // Restore a theme-colored fill that tracks the value for BOTH bounded sliders
                    // and open-ended drags, painted under the widget (transparent FrameBg) so
                    // typing (CTRL+click) + drag still work and the "=" equation button stays.
                    {
                        const float Sloc = ui::uiScale;
                        const ImGuiStyle& stl = ImGui::GetStyle();
                        const ImVec2 frameMin = ImGui::GetCursorScreenPos();
                        const float  frameW  = ImGui::CalcItemWidth();
                        const float  frameH  = ImGui::GetFrameHeight();
                        const ImVec2 frameMax(frameMin.x + frameW, frameMin.y + frameH);
                        const float  rnd     = stl.FrameRounding;
                        ImDrawList*  dl      = ImGui::GetWindowDrawList();

                        // Soft fraction in display units. Bounded = true range; positive open-ended
                        // grows a soft max with |value|/default/step; Signed (dimMin << 0) is
                        // zero-centered so negative offsets fill left of mid.
                        const bool signedOpen = !bounded && f.def->dimMin < -1.0e6;
                        float t = 0.0f;          // 0..1 (or -1..1 when signedOpen)
                        float midX = 0.0f, halfW = 0.0f;
                        if (bounded) {
                            const float mn = (float)(f.def->dimMin * toDisp);
                            const float mx = (float)(f.def->dimMax * toDisp);
                            t = (mx > mn) ? (v - mn) / (mx - mn) : 0.0f;
                            t = std::max(0.0f, std::min(1.0f, t));
                        } else if (signedOpen) {
                            const float defAbs = (float)std::fabs(f.def->dimDefault * toDisp);
                            const float soft = std::max(std::max(std::fabs(v), defAbs),
                                                        (float)(step * toDisp) * 16.0f) * 1.5f;
                            t = (soft > 1e-12f) ? (v / soft) : 0.0f;
                            t = std::max(-1.0f, std::min(1.0f, t));
                            midX  = (frameMin.x + frameMax.x) * 0.5f;
                            halfW = (frameMax.x - frameMin.x) * 0.5f;
                        } else {
                            const float mn = (float)(f.def->dimMin * toDisp);
                            const float defAbs = (float)std::fabs(f.def->dimDefault * toDisp);
                            const float softMax = std::max(std::max(std::max(v, mn), defAbs),
                                                           (float)(step * toDisp) * 16.0f) * 1.5f;
                            t = (softMax > mn) ? (v - mn) / (softMax - mn) : 0.0f;
                            t = std::max(0.0f, std::min(1.0f, t));
                        }

                        // Track base (glass tint) — replaces the opaque FrameBg we push transparent.
                        dl->AddRectFilled(frameMin, frameMax,
                            ImGui::ColorConvertFloat4ToU32(
                                ImVec4(surf.tint.x, surf.tint.y, surf.tint.z, 0.18f)), rnd);
                        // Value fill (accentSoft via palette — theme lint ok).
                        if (signedOpen) {
                            const float x0 = (t >= 0.0f) ? midX : (midX + t * halfW);
                            const float x1 = (t >= 0.0f) ? (midX + t * halfW) : midX;
                            if (x1 > x0 + 0.5f) {
                                const ImDrawFlags corners = (t >= 0.0f)
                                    ? ImDrawFlags_RoundCornersRight
                                    : ImDrawFlags_RoundCornersLeft;
                                // Near full-range either side: round both ends of that half.
                                const ImDrawFlags fl = (std::fabs(t) >= 0.999f)
                                    ? ImDrawFlags_RoundCornersAll : corners;
                                dl->AddRectFilled(ImVec2(x0, frameMin.y), ImVec2(x1, frameMax.y),
                                                  Palette::toU32(pal.accentSoft), rnd, fl);
                            }
                            // Zero tick (hairline) so signed direction is readable.
                            dl->AddLine(ImVec2(midX, frameMin.y + 2.0f * Sloc),
                                        ImVec2(midX, frameMax.y - 2.0f * Sloc),
                                        ImGui::ColorConvertFloat4ToU32(
                                            ImVec4(pal.border[0], pal.border[1], pal.border[2],
                                                   0.55f)), 1.0f);
                        } else if (t > 0.001f) {
                            const float x1 = frameMin.x + (frameMax.x - frameMin.x) * t;
                            const ImDrawFlags fl = (t >= 0.999f)
                                ? ImDrawFlags_RoundCornersAll
                                : ImDrawFlags_RoundCornersLeft;
                            dl->AddRectFilled(frameMin, ImVec2(x1, frameMax.y),
                                              Palette::toU32(pal.accentSoft), rnd, fl);
                        }
                        // Value-edge tick (draggable-position cue).
                        {
                            float tickX;
                            if (signedOpen) tickX = midX + t * halfW;
                            else            tickX = frameMin.x + (frameMax.x - frameMin.x) * t;
                            const float tw = 1.5f * Sloc;
                            dl->AddRectFilled(ImVec2(tickX - tw, frameMin.y + 2.0f * Sloc),
                                              ImVec2(tickX + tw, frameMax.y - 2.0f * Sloc),
                                              Palette::toU32(pal.accent), 1.0f * Sloc);
                        }
                        // Subtle track border.
                        dl->AddRect(frameMin, frameMax,
                            ImGui::ColorConvertFloat4ToU32(
                                ImVec4(pal.border[0], pal.border[1], pal.border[2],
                                       pal.border[3] * 0.65f)),
                            rnd, 0, 1.0f);
                    }

                    // Transparent FrameBg so our pre-drawn track/fill is not covered; keep a light
                    // hover/active wash. Push SliderGrab so bounded sliders still show a knob.
                    ImGui::PushStyleColor(ImGuiCol_FrameBg,
                        ImVec4(0, 0, 0, 0));
                    ImGui::PushStyleColor(ImGuiCol_FrameBgHovered,
                        ImVec4(surf.tint.x, surf.tint.y, surf.tint.z, 0.10f));
                    ImGui::PushStyleColor(ImGuiCol_FrameBgActive,
                        ImVec4(surf.tint.x, surf.tint.y, surf.tint.z, 0.14f));
                    ImGui::PushStyleColor(ImGuiCol_SliderGrab,
                        ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 1.0f));
                    ImGui::PushStyleColor(ImGuiCol_SliderGrabActive,
                        ImVec4(pal.accentDim[0], pal.accentDim[1], pal.accentDim[2], 1.0f));

                    bool widgetEdited;
                    if (bounded) {
                        widgetEdited = ImGui::SliderFloat("##dim", &v,
                                           (float)(f.def->dimMin * toDisp),
                                           (float)(f.def->dimMax * toDisp),
                                           valFmt, ImGuiSliderFlags_AlwaysClamp);
                    } else {
                        widgetEdited = ImGui::DragFloat("##dim", &v,
                                           (float)(step * toDisp) * 0.25f,
                                           (float)(f.def->dimMin * toDisp), FLT_MAX,
                                           valFmt, ImGuiSliderFlags_AlwaysClamp);
                    }
                    ImGui::PopStyleColor(5);
                    if (widgetEdited && enabled && !f.def->comingSoon)
                        commitDim((double)v / toDisp, /*immediate=*/false);
                    if (ImGui::IsItemDeactivatedAfterEdit() && dirty_)
                        lastEditTime_ = ImGui::GetTime() - kDebounceSec;  // fire on release/commit
                    // CTRL+wheel (or wheel mid-drag) steps by the field step. A bare hover-wheel
                    // scrolls the pane instead of silently mutating the value (widgets audit).
                    if (enabled && !f.def->comingSoon && io.MouseWheel != 0.0f &&
                        (ImGui::IsItemActive() || (ImGui::IsItemHovered() && io.KeyCtrl)))
                        commitDim(f.dimValue + step * (double)io.MouseWheel, /*immediate=*/true);
                } else {
                    bool changed = ImGui::InputText("##dim", f.dimBuf.data(), f.dimBuf.size(),
                                                    ImGuiInputTextFlags_EnterReturnsTrue |
                                                    ImGuiInputTextFlags_AutoSelectAll);
                    bool deact = ImGui::IsItemDeactivatedAfterEdit();
                    if (enabled && (changed || deact)) {
                        // `changed` is true ONLY when Enter is pressed (EnterReturnsTrue) — an
                        // explicit "apply now" gesture, so we backdate lastEditTime_ past the
                        // debounce window to fire the preview this frame. A plain blur (`deact`
                        // without `changed`) keeps the normal debounce.
                        const double applyTime = changed ? (ImGui::GetTime() - kDebounceSec)
                                                         : ImGui::GetTime();
                        std::string txt = f.dimBuf.data();
                        // trim
                        while (!txt.empty() && std::isspace((unsigned char)txt.front())) txt.erase(txt.begin());
                        while (!txt.empty() && std::isspace((unsigned char)txt.back())) txt.pop_back();
                        if (!txt.empty() && txt[0] == '=') {
                            f.dimExpr = txt.substr(1);
                            f.dimError.clear();
                            dirty_ = true; lastEditTime_ = applyTime;
                        } else {
                            double mm;
                            std::string err;
                            // A bare (unit-less) number follows the document length unit: mm in
                            // MMGS, inch in IPS. Angle fields ([deg]) stay degrees in both systems
                            // (bare = degrees) and parse with angle=true so the "deg" suffix they
                            // display round-trips and a stray length unit is rejected on them.
                            // angle + unit-less (ratio/k-factor/turns) values are never IPS-scaled.
                            const bool unscaled = isAngle || unitless;
                            const double bareScale = (useIps && !unscaled) ? 25.4 : 1.0;
                            if (pmParseDim(txt, mm, err, /*angle=*/isAngle, bareScale)) {
                                f.dimValue = mm; f.dimExpr.clear(); f.dimError.clear();
                                dirty_ = true; lastEditTime_ = applyTime;
                                if (deact) f.dimTextEntry = false;  // plain number -> back to widget
                            } else {
                                f.dimError = err;
                            }
                        }
                    }
                }

                // "=" toggle: switch the row to free text entry (unit math / equations). While an
                // expr is bound the row stays in text mode; the toggle then UNBINDS it (keeping
                // the last resolved value) and returns to the widget.
                ImGui::SameLine(0, 4.0f * ui::uiScale);
                {
                    const bool exprBound = !f.dimExpr.empty();
                    // Liquid glass v2: active "=" (text/expr mode) keeps the semantic accent wash;
                    // idle uses a translucent button fill so the column glass bleeds through (not the
                    // opaque theme button), and its glyph reads via adaptive secondary text.
                    ImGui::PushStyleColor(ImGuiCol_Button, textMode
                        ? ImVec4(pal.accentSoft[0], pal.accentSoft[1], pal.accentSoft[2],
                                 pal.accentSoft[3])
                        : ImVec4(pal.button[0], pal.button[1], pal.button[2], 0.40f));
                    ImGui::PushStyleColor(ImGuiCol_Text, textMode
                        ? ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 1)
                        : surf.textSecondary);
                    if (ImGui::Button("=##dim_txt", ImVec2(eqBtnW, 0)) &&
                        enabled && !f.def->comingSoon) {
                        if (exprBound) {
                            f.dimExpr.clear(); f.dimTextEntry = false;
                            commitDim(f.dimValue, /*immediate=*/true);   // unbind, keep value
                        } else if (textMode) {
                            f.dimTextEntry = false;
                        } else {
                            f.dimTextEntry = true;
                        }
                    }
                    ImGui::PopStyleColor(2);
                    if (ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled))
                        pmTooltip(exprBound
                            ? "Unbind the equation (keep the current value)"
                            : (textMode
                                ? "Back to slider entry"
                                : "Type units or an equation (25mm, 3/8in, =width/2)"));
                }
                // expr resolved-value + tooltip
                if (enabled && !f.dimExpr.empty()) {
                    pushTextColor(pal.accent);
                    ImGui::Text(u8"= %s  (%s)", f.dimExpr.c_str(),
                                pmFieldValueText(f.def, f.dimValue, useIps).c_str());
                    ImGui::PopStyleColor();
                    if (ImGui::IsItemHovered())
                        pmTooltip("Bound to equation '%s'", f.dimExpr.c_str());
                }
                // C37-F: inline red error — shown as a red hint line, not silently ignored.
                if (enabled && !f.dimError.empty()) {
                    pushTextColor(pal.statusErr);
                    ImGui::Text(u8"! %s", f.dimError.c_str());
                    ImGui::PopStyleColor();
                }
                break;
            }
            case PMFieldKind::SelectorWell: {
                // Chip shows count + entity kind for filled wells.
                std::string chip;
                if (f.picks.empty()) {
                    chip = "(pick in viewport)";
                } else if (f.picks.size() == 1) {
                    chip = f.picks.front().label;
                } else {
                    // ASCII "x" — the UI fonts don't carry U+00D7 and a count suffix on a
                    // single pick was pure noise anyway.
                    chip = f.picks.front().label + " x" + std::to_string(f.picks.size());
                }
                auto wr = pmDrawSelectionWell("##well", f.def->label, chip.c_str(),
                                             f.wellActive, f.wellDangling);
                if (wr.clicked) {
                    for (auto& o : fields_)
                        if (o.def->kind == PMFieldKind::SelectorWell) o.wellActive = false;
                    f.wellActive = true;
                    suppressWellAutoArm_ = false;  // user re-armed a well
                    wellClickedThisFrame = true;
                }
                if (wr.rightClicked) {
                    f.picks.clear(); f.wellDangling = false;
                    dirty_ = true; lastEditTime_ = ImGui::GetTime();
                }
                break;
            }
            case PMFieldKind::Combo: {
                pmCompactLabel(f.def->label, "", pal, enabled);
                if (f.def->comingSoon && ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled))
                    pmTooltip("Not yet supported by engine");
                // Build display labels (comboLabels if provided, else fall back to comboOptions).
                std::string lblSrc = f.def->comboLabels ? f.def->comboLabels
                                   : (f.def->comboOptions ? f.def->comboOptions : "");
                std::vector<std::string> labels; { size_t s = 0;
                    while (s <= lblSrc.size()) {
                        size_t b = lblSrc.find('|', s);
                        labels.push_back(lblSrc.substr(s, b - s));
                        if (b == std::string::npos) break; s = b + 1;
                    }
                }
                std::string preview = (f.comboIndex < (int)labels.size()) ? labels[f.comboIndex] : "";
                // Shared select handler for both render styles.
                auto selectOption = [&](int n) {
                    if (!enabled) return;
                    f.comboIndex = n; dirty_ = true; lastEditTime_ = ImGui::GetTime();
                    // SolidWorks: choosing an end/start condition that reveals a
                    // reference selection box auto-activates that box, so the next
                    // viewport click picks the plane/face/vertex (without this the
                    // active well stays on `profile` and the plane picker never arms).
                    // Re-home the active well onto the first now-visible, empty
                    // selector well gated by THIS combo.
                    const char* ckey = f.def->key;
                    PMField* reveal = nullptr;
                    for (auto& w : fields_) {
                        if (w.def->kind != PMFieldKind::SelectorWell) continue;
                        if (!w.def->enableKey ||
                            std::strcmp(w.def->enableKey, ckey) != 0) continue;
                        if (!w.picks.empty() || !fieldVisible(*w.def, fields_)) continue;
                        reveal = &w; break;
                    }
                    if (reveal) {
                        for (auto& o : fields_)
                            if (o.def->kind == PMFieldKind::SelectorWell ||
                                o.def->kind == PMFieldKind::FeatureWell)
                                o.wellActive = false;
                        reveal->wellActive = true;
                        suppressWellAutoArm_ = false;
                    }
                };
                if (f.def->segmented && labels.size() == 2) {
                    // Widgets audit: a combo-of-two (Boss|Cut, Both|One Side) renders as a
                    // segmented pill pair — one click to flip, no dropdown popup.
                    const float S = ui::uiScale;
                    const float gap  = 4.0f * S;
                    const float segW = (ImGui::GetContentRegionAvail().x - gap) * 0.5f;
                    for (int n = 0; n < 2; ++n) {
                        if (n) ImGui::SameLine(0, gap);
                        const bool selSeg = (f.comboIndex == n);
                        ImGui::PushID(n);
                        ImGui::PushStyleVar(ImGuiStyleVar_FrameRounding, 6.0f * S);
                        // Liquid glass v2: selected segment keeps the semantic accent wash; the idle
                        // segment uses a translucent frame fill so the frosted column bleeds through
                        // (not the opaque theme frameBg), with adaptive secondary text on top.
                        ImGui::PushStyleColor(ImGuiCol_Button, selSeg
                            ? ImVec4(pal.accentSoft[0], pal.accentSoft[1], pal.accentSoft[2],
                                     pal.accentSoft[3])
                            : ImVec4(pal.frameBg[0], pal.frameBg[1], pal.frameBg[2], 0.40f));
                        ImGui::PushStyleColor(ImGuiCol_ButtonHovered, selSeg
                            ? ImVec4(pal.accentMid[0], pal.accentMid[1], pal.accentMid[2],
                                     pal.accentMid[3])
                            : ImVec4(pal.frameBgHovered[0], pal.frameBgHovered[1],
                                     pal.frameBgHovered[2], 0.60f));
                        ImGui::PushStyleColor(ImGuiCol_Text, selSeg
                            ? ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 1)
                            : surf.textSecondary);
                        if (ImGui::Button(labels[n].c_str(), ImVec2(segW, 0)) && !selSeg)
                            selectOption(n);
                        ImGui::PopStyleColor(3);
                        ImGui::PopStyleVar();
                        ImGui::PopID();
                    }
                } else {
                    ImGui::SetNextItemWidth(-1);
                    if (ImGui::BeginCombo("##combo", preview.c_str())) {
                        // The dropdown popup has its own opaque dark PopupBg — the adaptive (dark)
                        // ink pushed for the transparent column would be black-on-black here, so use
                        // the theme's normal light text for the popup items.
                        ImGui::PushStyleColor(ImGuiCol_Text,
                            ImVec4(pal.text[0], pal.text[1], pal.text[2], 1.0f));
                        for (int n = 0; n < (int)labels.size(); ++n) {
                            bool sel = (f.comboIndex == n);
                            if (ImGui::Selectable(labels[n].c_str(), sel)) selectOption(n);
                            if (sel) ImGui::SetItemDefaultFocus();
                        }
                        ImGui::PopStyleColor();
                        ImGui::EndCombo();
                    }
                }
                break;
            }
            case PMFieldKind::Check: {
                // Check rows: label uses the full content width minus the checkbox, checkbox is
                // right-aligned at the trailing edge. Unlike Dim/Combo (fixed label column + field),
                // checkboxes are small so the label gets the remaining budget — "Reverse direction"
                // and similar must stay fully readable without the box landing on the text tail.
                const float avail = ImGui::GetContentRegionAvail().x;
                const float gap = ImGui::GetStyle().ItemInnerSpacing.x;
                // Square checkbox frame ≈ GetFrameHeight() (ImGui Checkbox size).
                const float chkW = ImGui::GetFrameHeight();
                const float labelBudget = std::max(0.0f, avail - chkW - gap);

                const ui::AdaptiveSurface& surf = pmSurf();
                pushTextColorV(enabled ? surf.textPrimary : surf.textSecondary);
                const char* full = f.def->label ? f.def->label : "";
                std::string shown = full;
                bool clipped = false;
                if (labelBudget > 0.0f &&
                    ImGui::CalcTextSize(shown.c_str()).x > labelBudget) {
                    clipped = true;
                    const char* ell = "\xE2\x80\xA6";  // U+2026 ellipsis
                    const float ellW = ImGui::CalcTextSize(ell).x;
                    while (!shown.empty() &&
                           ImGui::CalcTextSize(shown.c_str()).x + ellW > labelBudget)
                        shown.pop_back();
                    shown += ell;
                }
                ImGui::AlignTextToFramePadding();
                ImGui::TextUnformatted(shown.c_str());
                ImGui::PopStyleColor();
                if (clipped && ImGui::IsItemHovered())
                    pmTooltip("%s", full);

                // Right-align the checkbox at the trailing edge (same pattern as pick-hint).
                ImGui::SameLine(0.0f, 0.0f);
                {
                    const float remain = ImGui::GetContentRegionAvail().x;
                    if (remain > chkW)
                        ImGui::SetCursorPosX(ImGui::GetCursorPosX() + remain - chkW);
                }
                bool val = f.checkValue;
                if (ImGui::Checkbox("##chk", &val)) {
                    if (enabled) { f.checkValue = val; dirty_ = true; lastEditTime_ = ImGui::GetTime(); }
                }
                if (f.def->comingSoon && ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled))
                    pmTooltip("Not yet supported by engine");
                break;
            }
            case PMFieldKind::Int: {
                pmCompactLabel(f.def->label, "", pal, enabled);
                ImGui::SetNextItemWidth(-1);
                // DragInt bounded to the field's intMin..intMax (AlwaysClamp covers CTRL+click
                // typed entry too) — a stray "100000 instances" can't reach the engine preview.
                int v = f.intValue;
                if (ImGui::DragInt("##int", &v, 0.1f, f.def->intMin, f.def->intMax, "%d",
                                   ImGuiSliderFlags_AlwaysClamp)) {
                    if (enabled) {
                        v = std::min(std::max(v, f.def->intMin), f.def->intMax);
                        if (v != f.intValue) {
                            f.intValue = v; dirty_ = true; lastEditTime_ = ImGui::GetTime();
                        }
                    }
                }
                if (ImGui::IsItemDeactivatedAfterEdit() && dirty_)
                    lastEditTime_ = ImGui::GetTime() - kDebounceSec;  // fire on release/commit
                break;
            }
            case PMFieldKind::Text: {
                pmCompactLabel(f.def->label, "", pal, enabled);
                ImGui::SetNextItemWidth(-1);
                const char* hint = f.def->placeholder ? f.def->placeholder : "";
                if (ImGui::InputTextWithHint("##text", hint, f.textBuf.data(), f.textBuf.size())) {
                    if (enabled) { dirty_ = true; lastEditTime_ = ImGui::GetTime(); }
                }
                break;
            }
            case PMFieldKind::DatumRef: {
                pmCompactLabel(f.def->label, "", pal, enabled);
                std::string lblSrc = f.def->comboLabels ? f.def->comboLabels
                                   : (f.def->comboOptions ? f.def->comboOptions : "");
                std::vector<std::string> labels; { size_t s = 0;
                    while (s <= lblSrc.size()) {
                        size_t b = lblSrc.find('|', s);
                        labels.push_back(lblSrc.substr(s, b - s));
                        if (b == std::string::npos) break; s = b + 1;
                    }
                }
                std::string preview = (f.comboIndex < (int)labels.size()) ? labels[f.comboIndex] : "";
                ImGui::SetNextItemWidth(-1);
                if (ImGui::BeginCombo("##datum", preview.c_str())) {
                    // Light text on the opaque dark popup (see the ##combo note above).
                    ImGui::PushStyleColor(ImGuiCol_Text,
                        ImVec4(pal.text[0], pal.text[1], pal.text[2], 1.0f));
                    for (int n = 0; n < (int)labels.size(); ++n) {
                        bool sel = (f.comboIndex == n);
                        if (ImGui::Selectable(labels[n].c_str(), sel))
                            if (enabled) { f.comboIndex = n; dirty_ = true; lastEditTime_ = ImGui::GetTime(); }
                        if (sel) ImGui::SetItemDefaultFocus();
                    }
                    ImGui::PopStyleColor();
                    ImGui::EndCombo();
                }
                break;
            }
            case PMFieldKind::FeatureWell: {
                pushTextColorV(surf.textSecondary);
                ImGui::TextUnformatted(f.def->label);
                ImGui::PopStyleColor();
                const bool single = !f.def->selMulti;   // FeatWell1: holds exactly one feature id
                std::string chip;
                if (f.picks.empty()) chip = "(select a feature in the tree, then Add)";
                else for (size_t k = 0; k < f.picks.size(); ++k) {
                    if (k) chip += ", ";
                    chip += f.picks[k].label;
                }
                ImGui::PushTextWrapPos(0.0f);
                ImGui::TextUnformatted(chip.c_str());
                ImGui::PopTextWrapPos();
                // Add the currently tree-selected feature to the list. A single-emit well
                // (selMulti=false) replaces its one pick instead of appending.
                if (ImGui::SmallButton("Add selected")) {
                    const std::string& fid = st.selectedFeatureId;
                    if (!fid.empty()) {
                        bool dup = false;
                        for (const auto& p : f.picks)
                            if (p.selector.is_string() && p.selector.get<std::string>() == fid) dup = true;
                        if (!dup) {
                            // Look up the feature's display name from the mirror tree.
                            std::string nm = fid;
                            const nlohmann::json& tree = st.docMirror.tree();
                            if (tree.is_object() && tree.contains("features") && tree["features"].is_array())
                                for (const auto& tf : tree["features"])
                                    if (tf.is_object() && tf.value("id", std::string()) == fid) {
                                        nm = tf.value("name", fid); break;
                                    }
                            if (single) f.picks.clear();   // single well holds one id only
                            PMSelEntry e; e.selector = fid; e.label = nm;
                            f.picks.push_back(std::move(e));
                            dirty_ = true; lastEditTime_ = ImGui::GetTime();
                        }
                    } else {
                        st.toast(ui::ToastLevel::Info, "Select a feature in the FeatureManager first");
                    }
                }
                ImGui::SameLine();
                if (ImGui::SmallButton("Clear##feat")) {
                    f.picks.clear(); dirty_ = true; lastEditTime_ = ImGui::GetTime();
                }
                break;
            }
        }
        ImGui::PopID();

        if (!enabled) ImGui::EndDisabled();
    }
    closeGroupCard();

    // C38 (P3f): After iterating all fields on the first draw after open, mark the focus request
    // handled UNCONDITIONALLY. Previously this only cleared when an editable Dim field was seen, so a
    // SelectorWell-first (or Dim-less) form re-issued SetKeyboardFocusHere every frame and stole
    // focus continuously. We still record focusedOnOpen_ only when a field actually took focus.
    if (wantFocusFirstField_) {
        if (firstEditableDimSeen) focusedOnOpen_ = true;
        wantFocusFirstField_ = false;
    }

    // Click elsewhere in the PM (dim/check/combo/header) while a profile is chosen and the
    // select session is still armed → end select (keep picks). Well-chip clicks re-arm above
    // and are exempted. Empty profile well stays armed (first-pick UX).
    if (!wellClickedThisFrame && hasActiveSelectorWell() && !profilePicks().empty() &&
        ImGui::IsMouseClicked(ImGuiMouseButton_Left) &&
        ImGui::IsWindowHovered(ImGuiHoveredFlags_RootAndChildWindows) &&
        !ImGui::IsAnyItemActive()) {
        // Hovered window is the PM (or its children) but no well chip claimed the click.
        endProfileSelection();
    }

    // C37-F: Inline preview error block (red text, shown when the last preview RPC failed).
    if (!previewError_.empty()) {
        ImGui::Separator();
        pushTextColor(pal.statusErr);
        float wrapW = ImGui::GetContentRegionAvail().x;
        ImGui::PushTextWrapPos(ImGui::GetCursorPosX() + wrapW);
        ImGui::Text(u8"! Preview error: %s", previewError_.c_str());
        ImGui::PopTextWrapPos();
        ImGui::PopStyleColor();
    } else if (!previewWarning_.empty()) {
        // Non-fatal note (amber): the preview succeeded but the engine couldn't fully apply (e.g. a
        // fillet rounded only some of the selected edges because the radius was too large for the
        // rest). The selected edges stay highlighted in the viewport so the user can see which set
        // they picked while they lower the size.
        ImGui::Separator();
        pushTextColor(pal.statusWarn);
        float wrapW = ImGui::GetContentRegionAvail().x;
        ImGui::PushTextWrapPos(ImGui::GetCursorPosX() + wrapW);
        ImGui::Text(u8"! %s", previewWarning_.c_str());
        ImGui::PopTextWrapPos();
        ImGui::PopStyleColor();
    }

    // ---- live-preview status hint -----------------------------------------
    ImGui::Separator();
    pushTextColorV(surf.textSecondary);
    if (dirty_)            ImGui::TextUnformatted("Editing\xe2\x80\xa6 (live preview)");
    else if (firstPreviewDone_ && !targetId_.empty())
        ImGui::TextUnformatted("Preview up to date");
    else                  ImGui::TextUnformatted("Set parameters to preview");
    ImGui::PopStyleColor();

    // C38 (P2f): record whether a PM widget is active at the end of OUR draw, scoped to this PM
    // window (active item AND PM window focus). Read at the top of next frame's draw() to gate
    // Enter-commit. We're still inside the PM window's Begin/End here, so IsAnyItemActive() +
    // window-focus is PM-scoped — an active item in another window won't set this.
    pmWidgetActivePrevFrame_ = ImGui::IsAnyItemActive() &&
        ImGui::IsWindowFocused(ImGuiFocusedFlags_RootAndChildWindows);

    // Balanced pop for the 3 FrameBg colors + 1 adaptive Text color pushed after surf was
    // resolved (above).
    ImGui::PopStyleColor(4);
}

}  // namespace ui
