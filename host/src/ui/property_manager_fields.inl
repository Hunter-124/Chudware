// Field parsing, validation and serialization shared by the PM and its headless regression.
// Include in exactly one translation unit per executable; no worker, renderer or UI lifetime code.
#include "property_manager.h"
#include "imgui.h"  // editing timestamp only

#include <algorithm>
#include <array>
#include <cctype>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <iterator>
#include <string_view>
#include <vector>

namespace ui {
// Return the param-value string currently selected by a Combo field with the given key.
static std::string_view comboValueView(const std::vector<PMField>& fields, const char* key) {
    for (const auto& f : fields) {
        if (f.def->kind != PMFieldKind::Combo || std::strcmp(f.def->key, key) != 0) continue;
        const std::string_view opts = f.def->comboOptions ? f.def->comboOptions : "";
        int cur = 0; size_t start = 0;
        while (start <= opts.size()) {
            size_t bar = opts.find('|', start);
            const auto opt = opts.substr(start, bar - start);
            if (cur == f.comboIndex) return opt;
            ++cur;
            if (bar == std::string::npos) break;
            start = bar + 1;
        }
    }
    return {};
}

static std::string comboValue(const std::vector<PMField>& fields, const char* key) {
    return std::string(comboValueView(fields, key));
}

// Resolve the entire chain of controllers. A hidden/disabled controller cannot expose a child,
// and malformed cyclic form definitions fail closed rather than recursing indefinitely.
static bool fieldEnabledImpl(const PMFieldDef& def, const std::vector<PMField>& fields,
                             size_t depth) {
    if (def.comingSoon || depth > fields.size()) return false;
    if (!def.enableKey) return true;
    for (const auto& source : fields) {
        if (!source.def || std::strcmp(source.def->key, def.enableKey) != 0 ||
            (source.def->kind != PMFieldKind::Combo && source.def->kind != PMFieldKind::Check))
            continue;
        if (!fieldEnabledImpl(*source.def, fields, depth + 1)) return false;
        const std::string_view cur = source.def->kind == PMFieldKind::Check
            ? (source.checkValue ? "true" : "false")
            : comboValueView(fields, def.enableKey);
        const std::string_view allowed = def.enableVals ? def.enableVals : "";
        if (allowed.empty()) return true;
        size_t start = 0;
        while (start <= allowed.size()) {
            const size_t bar = allowed.find('|', start);
            if (allowed.substr(start, bar - start) == cur) return true;
            if (bar == std::string_view::npos) break;
            start = bar + 1;
        }
        return false;
    }
    return false;
}

static bool fieldEnabled(const PMFieldDef& def, const std::vector<PMField>& fields) {
    return fieldEnabledImpl(def, fields, 0);
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

static bool parseDimImpl(const std::string& raw, double& outMm, std::string& err, bool angle,
                         double bareLengthScale, bool unitless) {
    err.clear();
    std::string s;
    for (char c : raw) if (!std::isspace((unsigned char)c)) s += c;
    if (s.empty()) { err = "empty"; return false; }
    for (auto& c : s) c = (char)std::tolower((unsigned char)c);

    // Trailing-unit-applies-to-all: exactly one unit in the input AND it is the final token
    // ("1/2in", "(3+5)/2 in"). Strip it, evaluate bare, scale the result. Inputs with units on
    // interior literals ("100mm+2in/0.5m") fall through to per-literal conversion.
    double wholeScale = 1.0;
    bool wholeUnit = false;
    {
        std::vector<DimTok> probe;
        int unitCount = 0; bool endsWithUnit = false; std::string perr;
        if (dimTokenize(s, angle, probe, unitCount, endsWithUnit, perr)) {
            if (unitless && unitCount != 0) { err = "This value is unitless"; return false; }
            if (unitCount == 1 && endsWithUnit) {
                for (const auto& u : kDimUnits) {
                    size_t n = std::strlen(u.name);
                    if (u.angle == angle && s.size() > n &&
                        s.compare(s.size() - n, n, u.name) == 0) {
                        wholeScale = u.factor;
                        wholeUnit = true;
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

    // An explicit mm unit also governs the whole expression even though its factor is 1.
    const double bareScale = (wholeUnit || wholeScale != 1.0) ? 1.0 : bareLengthScale;

    std::vector<DimTok> toks;
    int unitCount = 0; bool endsWithUnit = false;
    if (!dimTokenize(s, angle, toks, unitCount, endsWithUnit, err, bareScale)) return false;
    if (toks.size() <= 1) { err = "empty"; return false; }

    DimEval ev{toks};
    double v = ev.expr();
    if (!ev.err.empty()) { err = ev.err; return false; }
    if (ev.t[ev.i].kind != DimTok::End) { err = "trailing input"; return false; }
    const double result = v * wholeScale;
    if (!std::isfinite(result)) { err = "not a finite number"; return false; }
    outMm = result;
    return true;
}

bool pmParseDim(const std::string& raw, double& outMm, std::string& err, bool angle,
                double bareLengthScale) {
    return parseDimImpl(raw, outMm, err, angle, bareLengthScale, false);
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

// Manual and AI text take the same unit and documented-constraint path. Keep the last
// accepted value intact on invalid input; readiness will block until the text is corrected.
static bool parseFieldText(PMField& f, bool inputIps) {
    std::string txt = f.dimBuf.data();
    while (!txt.empty() && std::isspace((unsigned char)txt.front())) txt.erase(txt.begin());
    while (!txt.empty() && std::isspace((unsigned char)txt.back())) txt.pop_back();
    if (!txt.empty() && txt.front() == '=') {
        const std::string expr = txt.substr(1);
        if (expr.find_first_not_of(" \t\r\n") == std::string::npos) {
            f.dimError = "Enter an equation after '='";
            return false;
        }
        if (f.dimExpr != expr) {
            f.dimExprValue = f.dimValue;
            f.dimExprRawString = false;
        }
        f.dimExpr = expr;
    } else {
        double value = 0.0;
        std::string err;
        const bool angle = pmFieldIsAngle(f.def);
        const bool unitless = pmFieldUnitless(f.def);
        const double scale = inputIps && !angle && !unitless ? 25.4 : 1.0;
        if (!parseDimImpl(txt, value, err, angle, scale, unitless)) {
            f.dimError = err;
            return false;
        }
        if (std::strcmp(f.def->key, "k_factor") == 0 && (value < 0.0 || value > 1.0)) {
            f.dimError = "K-factor must be between 0 and 1";
            return false;
        }
        f.dimValue = value;
        f.dimExpr.clear();
        f.dimExprRawString = false;
        f.dimValueKnown = true;
    }
    f.dimError.clear();
    f.dimTextDirty = false;
    f.dimSyncPending = false;
    return true;
}

// A preserved bare length keeps its original unit while focused. Once accepted and blurred,
// replace that text with an explicit value in the current unit before another edit can reinterpret it.
static void syncAcceptedDimText(PMField& f, bool ips) {
    if (f.dimExpr.empty() && !pmFieldIsAngle(f.def) && !pmFieldUnitless(f.def) &&
        f.dimInputIps != ips) {
        if (f.dimTextEditing) {
            f.dimSyncPending = true;
            return;
        }
        const std::string display = pmFieldValueText(f.def, f.dimValue, ips);
        std::snprintf(f.dimBuf.data(), f.dimBuf.size(), "%s", display.c_str());
    }
    f.dimInputIps = ips;
}

static bool applyAiDimPatch(PMField& f, const nlohmann::json& value, bool ips) {
    std::string text;
    if (value.is_number()) text = value.dump(); // preserve numeric precision
    else if (value.is_string()) text = value.get<std::string>();
    else if (value.is_object() && value.contains("expr") && value["expr"].is_string())
        text = "=" + value["expr"].get<std::string>();
    else return false;
    if (text.empty()) return false;
    if (f.dimBuf.size() <= text.size()) f.dimBuf.resize(text.size() + 1, 0);
    std::snprintf(f.dimBuf.data(), f.dimBuf.size(), "%s", text.c_str());
    f.dimInputIps = ips;
    f.dimTextDirty = true;
    const bool valid = parseFieldText(f, ips);
    f.dimTextEntry = !valid || !f.dimExpr.empty();
    if (valid && f.dimExpr.empty()) {
        const std::string display = pmFieldValueText(f.def, f.dimValue, ips);
        std::snprintf(f.dimBuf.data(), f.dimBuf.size(), "%s", display.c_str());
    }
    return valid;
}

// A dim value object {value, expr} for an op.
static nlohmann::json dimJson(const PMField& f) {
    nlohmann::json d;
    if (!f.dimExpr.empty()) {
        if (f.dimExprRawString) return f.dimExpr;
        d["value"] = f.dimExprValue;
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
    f.comboLabels.clear();
    if (d.kind == PMFieldKind::Combo || d.kind == PMFieldKind::DatumRef) {
        const std::string_view source = d.comboLabels ? d.comboLabels
                                                   : (d.comboOptions ? d.comboOptions : "");
        size_t start = 0;
        while (start <= source.size()) {
            const size_t bar = source.find('|', start);
            f.comboLabels.emplace_back(source.substr(start, bar - start));
            if (bar == std::string_view::npos) break;
            start = bar + 1;
        }
    }
    switch (d.kind) {
        case PMFieldKind::Dim: {
            f.dimValue = d.dimDefault;
            f.dimExprValue = d.dimDefault;
            f.dimExprRawString = false;
            f.dimValueKnown = true;
            f.dimExpr.clear();
            f.dimTextEntry = false;
            f.dimInputIps = ips;
            f.dimTextDirty = f.dimTextEditing = false;
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

void PropertyManager::loadFromTree(const nlohmann::json& feature) {
    nlohmann::json params = feature.value("params", nlohmann::json::object());
    nlohmann::json refs   = feature.value("refs",   nlohmann::json::object());
    legacyFillet_ = form_ && std::strcmp(form_->type, "fillet") == 0 &&
        (!params.contains("type") || params["type"].is_null() ||
         (params["type"].is_string() && params["type"].get<std::string>().empty()));
    const bool legacyEndpoint = legacyFillet_ && params.contains("radius2") &&
                                !params["radius2"].is_null();
    legacyFilletModeKnown_ = legacyFillet_ && !legacyEndpoint;
    // Controllers can appear after their wells (e.g. chamfer vertices before Type). Populate
    // them first so duplicate/conditional wells load only the reference for the actual mode.
    for (auto& f : fields_) {
        const char* key = f.def->key;
        if (f.def->kind == PMFieldKind::Combo && params.contains(key) && params[key].is_string() &&
            !(legacyFillet_ && std::strcmp(key, "type") == 0)) {
            const std::string value = params[key].get<std::string>();
            f.comboIndex = pmComboIndexOf(*f.def, value);
            f.comboUnsupported = f.comboIndex < 0 ? value : std::string();
        } else if (f.def->kind == PMFieldKind::Check && params.contains(key) &&
                   params[key].is_boolean()) {
            f.checkValue = params[key].get<bool>();
        }
    }
    // With no current compiler evidence, expose a stored endpoint as editable but describe the
    // mode as automatic; its legacy zero/equal/unequal semantics remain owned by the compiler.
    if (legacyEndpoint)
        for (auto& f : fields_)
            if (f.def->kind == PMFieldKind::Combo && std::strcmp(f.def->key, "type") == 0)
                f.comboIndex = pmComboIndexOf(*f.def, "variable");
    for (auto& f : fields_) {
        const char* key = f.def->key;
        switch (f.def->kind) {
            case PMFieldKind::Dim: {
                if (auto it = params.find(key); it != params.end()) {
                    const auto& d = *it;
                    if (d.is_number()) {
                        f.dimValue = d.get<double>();
                    } else if (d.is_string()) {
                        f.dimExpr = d.get<std::string>();
                        f.dimExprRawString = !f.dimExpr.empty();
                        f.dimValueKnown = false;
                    } else if (d.is_object()) {
                        if (auto expr = d.find("expr"); expr != d.end() && expr->is_string())
                            f.dimExpr = expr->get<std::string>();
                        if (auto value = d.find("value"); value != d.end() && value->is_number())
                            f.dimValue = value->get<double>();
                    }
                    f.dimExprValue = f.dimValue;
                    std::string disp = f.dimExpr.empty()
                        ? pmFieldValueText(f.def, f.dimValue, ips_) : "=" + f.dimExpr;
                    std::strncpy(f.dimBuf.data(), disp.c_str(), f.dimBuf.size() - 1);
                }
                break;
            }
            case PMFieldKind::Combo:
            case PMFieldKind::Check:
                break; // loaded in the controller pass above
            case PMFieldKind::Int: {
                if (params.contains(key) && params[key].is_number())
                    f.intValue = (int)(params[key].get<double>() + 0.5);
                else if (params.contains(key) && params[key].is_object() &&
                         params[key]["value"].is_number())
                    f.intValue = (int)(params[key]["value"].get<double>() + 0.5);
                break;
            }
            case PMFieldKind::Text: {
                if (params.contains(key) && params[key].is_string()) {
                    const std::string value = params[key].get<std::string>();
                    f.textBuf.assign(std::max<size_t>(128, value.size() + 1), 0);
                    std::memcpy(f.textBuf.data(), value.data(), value.size());
                }
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
                // Duplicate method-specific wells share one stored key; only the active well
                // owns that reference. A datum dropdown already represents its matching string.
                if (!fieldVisible(*f.def, fields_)) break;
                if (refs.contains(key) && refs[key].is_string()) {
                    const std::string value = refs[key].get<std::string>();
                    bool representedByDatum = false;
                    for (const auto& other : fields_)
                        if (other.def->kind == PMFieldKind::DatumRef &&
                            std::strcmp(other.def->key, key) == 0 &&
                            pmComboIndexOf(*other.def, value) >= 0) {
                            representedByDatum = true;
                            break;
                        }
                    if (representedByDatum) break;
                }
                if (refs.contains(key)) {
                    const auto& rv = refs[key];
                    auto add = [&](const nlohmann::json& selObj) {
                        if (!selObj.is_object() && !selObj.is_string()) return;
                        PMSelEntry e; e.selector = selObj;
                        std::string k = selObj.is_string()
                            ? selObj.get<std::string>() : selObj.value("kind", std::string("ref"));
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
    // Thread's two checkboxes describe presence/absence of real compiler parameters. Features
    // authored outside the PM need not contain the UI-only flags, and older flags may be stale.
    if (form_ && std::strcmp(form_->type, "thread") == 0) {
        for (auto& f : fields_) {
            if (f.def->kind != PMFieldKind::Check) continue;
            if (std::strcmp(f.def->key, "custom_size") == 0)
                f.checkValue = !params.contains("size") &&
                    (params.contains("major_dia") || params.contains("pitch"));
            else if (std::strcmp(f.def->key, "full_length") == 0)
                f.checkValue = !params.contains("length");
        }
    }
    if (form_ && std::strcmp(form_->type, "fillet") == 0) {
        auto data = feature.find("data");
        if (data != feature.end() && data->is_object()) {
            auto effective = data->find("fillet_effective");
            if (effective != data->end()) hydrateFilletEvidence(*effective);
        }
    }
}

void PropertyManager::hydrateFilletEvidence(const nlohmann::json& evidence) {
    if (!form_ || std::strcmp(form_->type, "fillet") != 0 || !evidence.is_object()) return;
    const auto effective = evidence.find("effective");
    if (effective == evidence.end() || !effective->is_object()) return;
    if (legacyFillet_) {
        auto end = effective->find("radius2");
        if (end != effective->end() && (end->is_null() || end->is_number())) {
            for (auto& f : fields_)
                if (f.def->kind == PMFieldKind::Combo && std::strcmp(f.def->key, "type") == 0) {
                    f.comboIndex = pmComboIndexOf(*f.def, end->is_number() ? "variable" : "constant");
                    legacyFilletModeKnown_ = true;
                    break;
                }
        }
    }
    for (auto& f : fields_) {
        if (f.def->kind != PMFieldKind::Dim || f.dimExpr.empty() ||
            (std::strcmp(f.def->key, "radius") != 0 && std::strcmp(f.def->key, "radius2") != 0))
            continue;
        const auto value = effective->find(f.def->key);
        if (value != effective->end() && value->is_number()) {
            const double radius = value->get<double>();
            if (std::isfinite(radius) && radius > 0.0) {
                f.dimValue = radius;
                f.dimValueKnown = true;
            }
        }
    }
}

// ---- blend edge wells: the edges a stored fillet/chamfer names -----------------------------
//
// A fillet/chamfer is shown LIVE and APPLIED while it is edited, so the displayed body no longer
// holds the edges it rounds. Its stored refs (picks, or agent-authored geometry queries) are
// therefore resolved by the engine on the feature's INPUT body (doc_feature_edges) and kept per
// pick as PMSelEntry::shapes: that is what the viewport draws and what the user deselects.

static bool pmBlendForm(const PMFormDef* form) {
    return form && form->type &&
           (std::strcmp(form->type, "fillet") == 0 || std::strcmp(form->type, "chamfer") == 0);
}

PMField* PropertyManager::blendEdgeWell() {
    if (!pmBlendForm(form_)) return nullptr;
    for (auto& f : fields_)
        if (f.def->kind == PMFieldKind::SelectorWell && f.def->selKinds.edge &&
            std::strcmp(f.def->key, "edges") == 0 && fieldVisible(*f.def, fields_)) return &f;
    return nullptr;
}

const PMField* PropertyManager::blendEdgeWell() const {
    return const_cast<PropertyManager*>(this)->blendEdgeWell();
}

// True while some pick of a blend edges well has no engine answer yet (neither drawable edges nor
// a reason it has none): a reopened ref, or an edge picked in this session.
static bool pmBlendEdgesUnanswered(const PMField& well) {
    return std::any_of(well.picks.begin(), well.picks.end(), [](const PMSelEntry& pick) {
        return pick.shapes.empty() && pick.unresolved.empty();
    });
}

static const nlohmann::json* pmJsonField(const nlohmann::json& object, const char* key) {
    if (!object.is_object()) return nullptr;
    const auto it = object.find(key);
    return it == object.end() ? nullptr : &*it;
}

static bool pmVec3(const nlohmann::json& v, std::array<float, 3>& out) {
    if (!v.is_array() || v.size() != 3) return false;
    for (size_t i = 0; i < 3; ++i) {
        if (!v[i].is_number()) return false;
        out[i] = v[i].get<float>();
    }
    return true;
}

// A pick's world polyline rebuilt from its own selector: a line from centroid ± ½·len·axis, a
// circle as a loop of radius len/2π in the plane normal to its axis, anything else a single
// centroid point (drawn as a ring). False when the selector carries no centroid.
static bool pmSelectorPolyline(const nlohmann::json& sel, std::vector<std::array<float, 3>>& pts,
                               bool& closed) {
    const nlohmann::json* centroid = pmJsonField(sel, "centroid");
    std::array<float, 3> c;
    if (!centroid || !pmVec3(*centroid, c)) return false;
    const nlohmann::json* surfJson = pmJsonField(sel, "surf");
    const std::string surf = surfJson && surfJson->is_string() ? surfJson->get<std::string>() : "";
    const nlohmann::json* axisJson = pmJsonField(sel, "axis");
    std::array<float, 3> axis;
    const bool hasAxis = axisJson && pmVec3(*axisJson, axis);
    const nlohmann::json* lenJson = pmJsonField(sel, "area_or_len");
    const bool hasLen = lenJson && lenJson->is_number();
    closed = false;
    if (surf == "line" && hasAxis && hasLen) {
        const float half = 0.5f * lenJson->get<float>();
        pts.push_back({c[0] - axis[0] * half, c[1] - axis[1] * half, c[2] - axis[2] * half});
        pts.push_back({c[0] + axis[0] * half, c[1] + axis[1] * half, c[2] + axis[2] * half});
        return true;
    }
    if (surf == "circle" && hasAxis && hasLen) {
        float nx = axis[0], ny = axis[1], nz = axis[2];
        const float nl = std::sqrt(nx * nx + ny * ny + nz * nz);
        const float r = lenJson->get<float>() / (2.0f * 3.14159265358979f);
        if (nl > 1e-6f && r > 1e-6f) {
            nx /= nl; ny /= nl; nz /= nl;
            // Two basis vectors spanning the circle plane (u ⟂ n, v = n × u).
            const float rx = (std::fabs(nx) < 0.9f) ? 1.0f : 0.0f;
            const float ry = (std::fabs(nx) < 0.9f) ? 0.0f : 1.0f;
            float ux = ny * 0.0f - nz * ry, uy = nz * rx - nx * 0.0f, uz = nx * ry - ny * rx;
            const float ul = std::sqrt(ux * ux + uy * uy + uz * uz);
            if (ul > 1e-6f) {
                ux /= ul; uy /= ul; uz /= ul;
                const float vx = ny * uz - nz * uy, vy = nz * ux - nx * uz, vz = nx * uy - ny * ux;
                const int N = 48;
                pts.reserve(N);
                for (int k = 0; k < N; ++k) {
                    const float t = (float)k / (float)N * 2.0f * 3.14159265358979f;
                    const float ct = std::cos(t) * r, st = std::sin(t) * r;
                    pts.push_back({c[0] + ux * ct + vx * st, c[1] + uy * ct + vy * st,
                                   c[2] + uz * ct + vz * st});
                }
                closed = true;
                return true;
            }
        }
    }
    pts.push_back(c);
    return true;
}

void PropertyManager::collectActiveWellEdgeSegments(std::vector<EdgeSeg>& out) const {
    const PMField* well = blendEdgeWell();
    if (!active_ || !well || !well->wellActive) return;
    for (int i = 0; i < (int)well->picks.size(); ++i) {
        const PMSelEntry& pick = well->picks[i];
        if (!pick.shapes.empty()) {
            for (int j = 0; j < (int)pick.shapes.size(); ++j) {
                EdgeSeg seg;
                seg.pts = pick.shapes[j].pts;
                seg.closed = pick.shapes[j].closed;
                seg.pickIndex = i;
                seg.shapeIndex = j;
                out.push_back(std::move(seg));
            }
            continue;
        }
        EdgeSeg seg;
        if (!pmSelectorPolyline(pick.selector, seg.pts, seg.closed)) continue;
        seg.pickIndex = i;
        out.push_back(std::move(seg));
    }
}

bool PropertyManager::blendHandle(BlendHandle& out) const {
    const PMField* well = blendEdgeWell();
    if (!active_ || !well) return false;
    for (const PMSelEntry& pick : well->picks)
        for (const PMEdgeShape& shape : pick.shapes) {
            if (!shape.hasFrame) continue;
            out.anchor = shape.mid;
            out.dir = shape.grow;
            return true;
        }
    return false;
}

// "Line, 20 mm" from an edge selector's own fields ("" when it carries neither).
static std::string pmEdgeDetail(const nlohmann::json& sel, bool ips) {
    const nlohmann::json* surf = pmJsonField(sel, "surf");
    const nlohmann::json* len = pmJsonField(sel, "area_or_len");
    std::string out;
    if (surf && surf->is_string()) {
        const std::string s = surf->get<std::string>();
        out = s == "line" ? "Line" : s == "circle" ? "Circle" : "Curve";
    }
    if (len && len->is_number()) {
        if (!out.empty()) out += ", ";
        out += pmFormatDim(len->get<double>(), ips);
    }
    return out;
}

static std::vector<PropertyManager::EdgeRow> pmBlendEdgeRows(const PMField& well, bool ips) {
    std::vector<PropertyManager::EdgeRow> rows;
    auto add = [&](int pick, int shape, const nlohmann::json& sel, const std::string& note) {
        PropertyManager::EdgeRow row;
        row.pickIndex = pick;
        row.shapeIndex = shape;
        const std::string detail = note.empty() ? pmEdgeDetail(sel, ips) : note;
        row.label = "Edge " + std::to_string(rows.size() + 1) +
                    (detail.empty() ? std::string() : " (" + detail + ")");
        rows.push_back(std::move(row));
    };
    for (int i = 0; i < (int)well.picks.size(); ++i) {
        const PMSelEntry& pick = well.picks[i];
        if (pick.shapes.empty()) {
            add(i, -1, pick.selector, pick.unresolved.empty() ? std::string() : "not found");
            continue;
        }
        for (int j = 0; j < (int)pick.shapes.size(); ++j)
            add(i, j, pick.shapes[j].selector, std::string());
    }
    return rows;
}

std::vector<PropertyManager::EdgeRow> PropertyManager::activeWellEdgeRows() const {
    const PMField* well = blendEdgeWell();
    if (!active_ || !well || !well->wellActive) return {};
    return pmBlendEdgeRows(*well, ips_);
}

// Take one edge out of a blend well. A ref that names several edges (a stored geometry query)
// is replaced by one canonical pick per REMAINING edge, so its stored form changes only when the
// user actually removes one of them.
static bool pmEraseBlendEdge(PMField& well, int pickIndex, int shapeIndex) {
    if (pickIndex < 0 || pickIndex >= (int)well.picks.size()) return false;
    const PMSelEntry& pick = well.picks[pickIndex];
    std::vector<PMSelEntry> kept;
    if (shapeIndex >= 0 && pick.shapes.size() > 1) {
        if (shapeIndex >= (int)pick.shapes.size()) return false;
        for (int j = 0; j < (int)pick.shapes.size(); ++j) {
            if (j == shapeIndex) continue;
            PMSelEntry edge;
            edge.selector = pick.shapes[j].selector;
            edge.label = "Edge";
            edge.shapes.push_back(pick.shapes[j]);
            kept.push_back(std::move(edge));
        }
    }
    well.picks.erase(well.picks.begin() + pickIndex);
    well.picks.insert(well.picks.begin() + pickIndex, std::make_move_iterator(kept.begin()),
                      std::make_move_iterator(kept.end()));
    return true;
}

bool PropertyManager::removeActiveWellPick(int pickIndex, int shapeIndex) {
    PMField* well = blendEdgeWell();
    if (!active_ || !well || !well->wellActive || !pmEraseBlendEdge(*well, pickIndex, shapeIndex))
        return false;
    dirty_ = true;
    lastEditTime_ = ImGui::GetTime();
    return true;
}

// A click on an edge the well already holds as a stored ref takes it out instead of adding it a
// second time. The displayed body only still has that edge when the feature's own round failed
// (the history then builds on the body before it), but then this is the click that must toggle.
static bool pmToggleStoredEdge(PMField& well, const nlohmann::json& selector) {
    const nlohmann::json* centroid = pmJsonField(selector, "centroid");
    const nlohmann::json* surf = pmJsonField(selector, "surf");
    std::array<float, 3> c;
    if (!centroid || !pmVec3(*centroid, c)) return false;
    for (int i = 0; i < (int)well.picks.size(); ++i)
        for (int j = 0; j < (int)well.picks[i].shapes.size(); ++j) {
            const nlohmann::json& held = well.picks[i].shapes[j].selector;
            const nlohmann::json* heldCentroid = pmJsonField(held, "centroid");
            const nlohmann::json* heldSurf = pmJsonField(held, "surf");
            std::array<float, 3> h;
            if (!heldCentroid || !pmVec3(*heldCentroid, h)) continue;
            if ((surf ? *surf : nlohmann::json()) != (heldSurf ? *heldSurf : nlohmann::json()))
                continue;
            if (std::fabs(h[0] - c[0]) < 1e-3f && std::fabs(h[1] - c[1]) < 1e-3f &&
                std::fabs(h[2] - c[2]) < 1e-3f)
                return pmEraseBlendEdge(well, i, j);
        }
    return false;
}

bool PropertyManager::applyEdgeRefGeometry(const nlohmann::json& reply,
                                           const std::vector<nlohmann::json>& requested) {
    PMField* well = blendEdgeWell();
    const nlohmann::json* rows = pmJsonField(reply, "refs");
    if (!well || !rows || !rows->is_array() || rows->size() != well->picks.size() ||
        requested.size() != well->picks.size())
        return false;
    // The user changed the selection while the engine worked: the answer is for another list.
    for (size_t i = 0; i < requested.size(); ++i)
        if (well->picks[i].selector != requested[i]) return false;
    for (size_t i = 0; i < rows->size(); ++i) {
        const nlohmann::json& row = (*rows)[i];
        PMSelEntry& pick = well->picks[i];
        pick.shapes.clear();
        pick.unresolved.clear();
        const nlohmann::json* error = pmJsonField(row, "error");
        if (error && error->is_string()) pick.unresolved = error->get<std::string>();
        const nlohmann::json* edges = pmJsonField(row, "edges");
        if (edges && edges->is_array()) {
            for (const auto& edge : *edges) {
                const nlohmann::json* selector = pmJsonField(edge, "selector");
                const nlohmann::json* points = pmJsonField(edge, "points");
                const nlohmann::json* closed = pmJsonField(edge, "closed");
                if (!selector || !selector->is_object() || !points || !points->is_array()) continue;
                PMEdgeShape shape;
                shape.selector = *selector;
                shape.closed = closed && closed->is_boolean() && closed->get<bool>();
                for (const auto& p : *points) {
                    std::array<float, 3> pt;
                    if (pmVec3(p, pt)) shape.pts.push_back(pt);
                }
                const nlohmann::json* mid = pmJsonField(edge, "mid");
                const nlohmann::json* grow = pmJsonField(edge, "grow");
                if (mid && grow && pmVec3(*mid, shape.mid) && pmVec3(*grow, shape.grow)) {
                    const float len = std::sqrt(shape.grow[0] * shape.grow[0] +
                                                shape.grow[1] * shape.grow[1] +
                                                shape.grow[2] * shape.grow[2]);
                    if (len > 1e-6f) {
                        for (float& g : shape.grow) g /= len;
                        shape.hasFrame = true;
                    }
                }
                if (shape.pts.size() >= 2) pick.shapes.push_back(std::move(shape));
            }
        }
        // An answer that drew nothing is still an answer: without a reason the pick would look
        // as if the engine had not been asked yet and be asked again after every preview.
        if (pick.shapes.empty() && pick.unresolved.empty())
            pick.unresolved = "the engine returned no drawable edge";
    }
    return true;
}

// Preserve active or invalid edits, including their original bare-unit interpretation.
void PropertyManager::reformatDimDisplays() {
    for (auto& f : fields_) {
        if (!f.def || f.def->kind != PMFieldKind::Dim) continue;
        if (f.dimTextDirty || f.dimTextEditing || !f.dimError.empty()) continue;
        if (!f.dimExpr.empty()) continue;          // expr binding shown as "=expr"; leave it
        if (f.def->unitLabel && f.def->unitLabel[0] == '\0') continue;  // unit-less field
        if (f.def->unitLabel && std::strstr(f.def->unitLabel, "deg")) continue;  // angle: degrees both
        std::string disp = pmFormatDim(f.dimValue, ips_);
        std::strncpy(f.dimBuf.data(), disp.c_str(), f.dimBuf.size() - 1);
        f.dimInputIps = ips_;
    }
}

bool PropertyManager::flushDimText() {
    bool valid = true;
    for (auto& f : fields_) {
        if (f.def->kind != PMFieldKind::Dim || !fieldVisible(*f.def, fields_) ||
            !fieldEnabled(*f.def, fields_) || (!f.dimTextDirty && f.dimError.empty())) continue;
        const double oldValue = f.dimValue;
        const std::string oldExpr = f.dimExpr;
        if (!parseFieldText(f, f.dimInputIps)) { valid = false; continue; }
        if (f.dimValue != oldValue || f.dimExpr != oldExpr) {
            dirty_ = true;
            lastEditTime_ = ImGui::GetTime();
        }
        syncAcceptedDimText(f, ips_);
    }
    return valid;
}

// ---- op assembly ----------------------------------------------------------

// Display name for a NEW feature: the form title plus the next free number ("Fillet1", then
// "Fillet2", ...). Numbering continues after the highest existing "<title><digits>" name so a
// deleted middle feature never makes a later one reuse a live name.
static std::string pmUniqueFeatureName(const nlohmann::json& tree, const std::string& title) {
    long highest = 0;
    const auto features = tree.is_object() ? tree.find("features") : tree.end();
    if (tree.is_object() && features != tree.end() && features->is_array()) {
        for (const auto& f : *features) {
            if (!f.is_object()) continue;
            const auto name = f.find("name");
            if (name == f.end() || !name->is_string()) continue;
            const std::string& s = name->get_ref<const std::string&>();
            if (s.size() <= title.size() || s.compare(0, title.size(), title) != 0) continue;
            const std::string digits = s.substr(title.size());
            if (digits.size() > 9 ||
                digits.find_first_not_of("0123456789") != std::string::npos) continue;
            highest = std::max(highest, std::stol(digits));
        }
    }
    return title + std::to_string(highest + 1);
}

void PropertyManager::buildOpFeature(nlohmann::json& feature, const nlohmann::json& tree) const {
    feature["type"] = form_->type;
    feature["name"] = targetId_.empty() ? pmUniqueFeatureName(tree, title_) : std::string(title_);
    nlohmann::json params = nlohmann::json::object();
    nlohmann::json refs   = nlohmann::json::object();
    // edit_feature merges patches, so absent/hidden form-owned keys must explicitly be removed.
    // Initialize once per key, then let every visible, populated row override the deletion.
    // This also preserves a DatumRef when its same-key override well is empty.
    if (!targetId_.empty()) {
        for (const auto& f : fields_) {
            if (f.def->comingSoon) continue;
            switch (f.def->kind) {
                case PMFieldKind::SelectorWell:
                case PMFieldKind::FeatureWell:
                case PMFieldKind::DatumRef: refs[f.def->key] = nullptr; break;
                default: params[f.def->key] = nullptr; break;
            }
        }
    }
    for (const auto& f : fields_) {
        // Hidden and unavailable controls do not contribute values; their initialized null patch
        // remains only if no other visible row owns the same key.
        if (f.def->comingSoon || !fieldVisible(*f.def, fields_) ||
            !fieldEnabled(*f.def, fields_)) continue;
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
                // Empty text removes an existing optional value and is omitted on add.
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
    if (legacyFillet_) {
        // Untyped blends have compiler-defined automatic endpoint semantics. Preserve that
        // authored mode and any hidden endpoint until the user explicitly chooses a subtype.
        params.erase("type");
        for (const auto& f : fields_)
            if (f.def->kind == PMFieldKind::Dim && std::strcmp(f.def->key, "radius2") == 0 &&
                !fieldVisible(*f.def, fields_))
                params.erase("radius2");
    }
    // Interactive Thread uses a cheap turned-band proxy. The final edit must DELETE the flag;
    // omitting it from an edit patch would leave the preview geometry in the saved feature.
    if (form_ && std::string(form_->type) == "thread") {
        if (!committing_) params["preview"] = true;
        else if (!targetId_.empty()) params["preview"] = nullptr;
    }
    feature["params"] = params;
    if (!refs.empty()) feature["refs"] = refs;
}

// Validate every active editor before checking references. Geometry constraints belong to the
// compiler, not a generic positive-dimension heuristic (datum offsets may be signed or zero).
static bool readyForPreview(const PMFormDef* form, const std::vector<PMField>& fields) {
    if (!form) return false;
    for (const auto& f : fields) {
        if (!fieldVisible(*f.def, fields) || !fieldEnabled(*f.def, fields)) continue;
        if (f.def->kind == PMFieldKind::Dim &&
            (!f.dimError.empty() || f.dimTextDirty || !std::isfinite(f.dimValue))) return false;
        if (f.def->kind == PMFieldKind::Combo && !f.comboUnsupported.empty()) return false;
    }
    std::string t = form->type;
    auto picksFor = [&](const char* key) -> size_t {
        for (const auto& f : fields)
            if (f.def->kind == PMFieldKind::SelectorWell && fieldVisible(*f.def, fields) &&
                std::strcmp(f.def->key, key) == 0) return f.picks.size();
        return 0;
    };
    if (t == "plane") {
        const auto method = comboValueView(fields, "method");
        if (method == "three_point") return picksFor("points") >= 3;
        if (method == "angle" || method == "midplane")
            return picksFor("base") > 0 && picksFor("second") > 0;
        return picksFor("base") > 0;
    }
    if (t == "csys") return true; // origin and axes have legitimate engine defaults

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
    if (t == "loft" || t == "surf_loft") {
        for (const auto& f : fields)
            if ((f.def->kind == PMFieldKind::SelectorWell) && std::strcmp(f.def->key, "sections") == 0)
                return f.picks.size() >= 2;
        return false;
    }

    // Only a currently visible blend well is relevant; hidden picks are retained for switching.
    if (t == "fillet" || t == "chamfer") {
        static const char* anyWell[] = {"edges", "middle_face", "face_set_a", "vertices"};
        for (const auto& f : fields) {
            if (f.def->kind != PMFieldKind::SelectorWell || f.picks.empty() ||
                !fieldVisible(*f.def, fields)) continue;
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
    else if (t == "thread") needWell = "target_face";
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

    // Other forms can use defaults, including zero or signed dimensions. Rebuild errors remain
    // actionable engine diagnostics rather than duplicating feature-specific geometric limits.
    return true;
}


}  // namespace ui
