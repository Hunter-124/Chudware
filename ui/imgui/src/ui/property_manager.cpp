// PropertyManager — data-driven inspector (C2 port).
#include "ui/property_manager.h"
#include "app/app_state.h"
#include "theme/theme.h"
#include "theme/domain_tokens.h"
#include "theme/icons.h"
#include "panel_spec.h"
#include "viewport/scene.h"
#include "pick_hit.h"

#include "imgui.h"
#include <algorithm>

namespace ui {

using aicad::ui::FieldKind;
using aicad::ui::PanelField;
using aicad::ui::PanelSpec;
using aicad::ui::SelectorKinds;

static ImVec4 pmPal(unsigned int hex, float alpha = 1.f) {
    ImVec4 v = theme::vec(hex);
    v.w = alpha;
    return v;
}

// C37-F: Debounce window for live preview (was 0.40 s; reduced for snappier feel).
static const double kDebounceSec = 0.25;

// ---- field enable helpers -------------------------------------------------

// Return the param-value string currently selected by a Combo field with the given key.
static std::string comboValue(const std::vector<PMField>& fields, const char* key) {
    for (const auto& f : fields) {
        if (f.def->kind != FieldKind::Combo || std::strcmp(f.def->key, key) != 0) continue;
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
        if (f.def->kind == FieldKind::Check && std::strcmp(f.def->key, key) == 0)
            return f.checkValue ? "true" : "false";
    return c;
}

static bool fieldEnabled(const PanelField& def, const std::vector<PMField>& fields) {
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

static std::vector<std::string> comboOptionsList(const PanelField& def) {
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

// Conditional fields are hidden entirely until their controlling combo makes them relevant.
// This keeps reference wells/offsets out of the user's way and prevents inactive params/refs from
// being serialized into preview ops. comingSoon rows remain visible but disabled when ungated.
static bool fieldVisible(const PanelField& def, const std::vector<PMField>& fields) {
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

// An angle-valued Dim field is tagged by a "deg" unit label (Angle/AngleIf helpers). Its value
// is stored in degrees and must NOT carry the mm/inch length suffix.
static inline bool pmFieldIsAngle(const PanelField* d) {
    return d && d->unitLabel && std::strstr(d->unitLabel, "deg") != nullptr;
}
// A unit-less Dim (DimU: ratio/k-factor/twist turns) — empty unit label. Its value is a plain
// number with NO length suffix and is never IPS-scaled.
static inline bool pmFieldUnitless(const PanelField* d) {
    return d && d->unitLabel && d->unitLabel[0] == '\0';
}
// Format a Dim field's canonical value for its edit buffer / display, honouring the field's unit:
// angle -> "<n>deg", unit-less -> bare "<n>", otherwise length via pmFormatDim (mm / inch).
static std::string pmFieldValueText(const PanelField* d, double v, bool ips) {
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

static void initField(PMField& f, const PanelField& d, bool ips) {
    f.def = &d;
    switch (d.kind) {
        case FieldKind::Dim: {
            f.dimValue = d.dimDefault;
            f.dimExpr.clear();
            std::string disp = pmFieldValueText(&d, d.dimDefault, ips);
            std::strncpy(f.dimBuf.data(), disp.c_str(), f.dimBuf.size() - 1);
            break;
        }
        case FieldKind::Combo:   f.comboIndex = 0; break;
        case FieldKind::DatumRef: f.comboIndex = 0; break;
        case FieldKind::Check:   f.checkValue = d.checkDefault; break;
        case FieldKind::Int:     f.intValue = (int)(d.dimDefault + 0.5); break;
        case FieldKind::Text:    f.textBuf.assign(128, 0); break;
        case FieldKind::SelectorWell:
        case FieldKind::FeatureWell: f.picks.clear(); f.wellActive = false; break;
    }
}

bool PropertyManager::openForCommand(app::AppState& st, const std::string& cmdId) {
    std::string mode;
    const PanelSpec* form = aicad::ui::panelFormForCommand(cmdId, mode);
    if (!form) return false;
    if (active_ && !cancelEdits(st)) return false;   // replacing a PM must roll back previews first

    // C40: SolidWorks mode exclusion. Invoking a feature command while a sketch is being
    // edited EXITS the sketch first (its profile then pre-seeds extrude/revolve below);
    // an armed plane-pick is cancelled — a feature PM and the pick overlay must never
    // coexist (they fight over the viewport mouse).
    std::string justExitedSketch;
    if (st.inSketch) {
    if (st.inSketch) st.inSketch = false;
    }
    // plane pick cleared

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
    lastEditTime_ = ImGui::GetTime();
    previewError_.clear();
    focusedOnOpen_ = false;
    wantFocusFirstField_ = true;  // C37-F: focus first field on next draw

    // C37-F: Take a checkpoint BEFORE any preview, so Cancel can revert with a single call.
    checkpointId_ = st.docMirror.hasDoc() ? st.docMirror.checkpoint(st.worker) : std::string();
    if (checkpointId_.empty())
        

    ips_ = (st.units == "in");   // document-unit display follows the active unit system (MMGS / IPS)
    fields_.clear();
    fields_.resize(form->fields.size());
    for (size_t i = 0; i < form->fields.size(); ++i)
        initField(fields_[i], form->fields[i], ips_);

    // If a mode/op combo exists, preselect it from the command (boss/cut split button; boolean
    // combine/subtract/intersect). Match mode_ against the combo's option VALUES so any N-way combo
    // works (not just boss/cut), falling back to index 0 (legacy "cut" -> 1).
    if (!mode_.empty()) {
        for (auto& f : fields_) {
            if (f.def->kind != FieldKind::Combo) continue;
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
        if (f.def->kind == FieldKind::SelectorWell) { f.wellActive = true; break; }

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
    if (std::strcmp(form_->featureType, "extrude") == 0 || std::strcmp(form_->featureType, "revolve") == 0 ||
        std::strcmp(form_->featureType, "sweep") == 0) {
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
                if (fidv == justExitedSketch || fidv == st.selectedFeature) { pick = &f; break; }
                pick = &f;
            }
            if (pick) {
                PMSelEntry se;
                se.selector = {{"kind", "region"}, {"sketch", pick->value("id", std::string())}};
                se.label = "Whole sketch";
                prof->picks.push_back(std::move(se));
                dirty_ = true;   // schedule the live preview against the seeded whole-sketch profile
                lastEditTime_ = ImGui::GetTime();
                (void)("[pm] profile seeded (whole sketch) from " + pick->value("id", std::string()));
            }
        }
    }

    // Pattern/mirror: seed the "features" FeatureWell from the FeatureManager selection so the
    // command operates immediately on the selected feature (SolidWorks pre-select). A sketch/datum
    // selection is skipped (nothing to replicate); an empty list leaves mirror as a whole-body
    // mirror and the patterns waiting for an "Add selected" pick.
    if (std::strcmp(form_->featureType, "mirror") == 0 ||
        std::strcmp(form_->featureType, "linear_pattern") == 0 ||
        std::strcmp(form_->featureType, "circular_pattern") == 0) {
        const std::string& sfid = st.selectedFeature;
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
                    if (fld.def->kind == FieldKind::FeatureWell) {
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
    if (std::strcmp(form_->featureType, "weld_member") == 0) {
        const std::string& sfid = st.selectedFeature;
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
                    if (fld.def->kind == FieldKind::FeatureWell &&
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
        const std::string& sfid = st.selectedFeature;
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
                if (fld.def->kind == FieldKind::SelectorWell && fld.def->selKinds.plane &&
                    fld.picks.empty() && fieldVisible(*fld.def, fields_)) {
                    PMSelEntry e; e.selector = sfid; e.label = label;
                    fld.picks.push_back(std::move(e));
                    fld.wellDangling = false;
                    dirty_ = true; lastEditTime_ = ImGui::GetTime();
                    (void)("[pm] plane well seeded from preselected " + sfid);
                    break;
                }
    }

    (void)("[pm] open " + std::string(form_->featureType) +
              (mode_.empty() ? "" : " (" + mode_ + ")"));
    return true;
}

bool PropertyManager::openForEdit(app::AppState& st, const std::string& featureId) {
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
    const PanelSpec* form = aicad::ui::findPanelSpec(type.c_str());
    if (!form) { st.status = "[pm] no form for feature type '" + type + "'"; return false; }
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
    lastEditTime_ = ImGui::GetTime();
    previewError_.clear();
    focusedOnOpen_ = false;
    wantFocusFirstField_ = true;  // C37-F: focus first field on next draw

    // C37-F: Take a checkpoint BEFORE any preview edit, so Cancel reverts with one call.
    checkpointId_ = st.docMirror.hasDoc() ? st.docMirror.checkpoint(st.worker) : std::string();
    if (checkpointId_.empty())
        

    ips_ = (st.units == "in");   // document-unit display follows the active unit system (MMGS / IPS)
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

    (void)("[pm] edit " + featureId + " (" + type + ")");
    return true;
}

void PropertyManager::presetFieldForShot(const std::string& key, const std::string& value) {
    if (!active_) return;
    for (auto& f : fields_) {
        if (!f.def->key || key != f.def->key) continue;
        switch (f.def->kind) {
            case FieldKind::Combo:
            case FieldKind::DatumRef: {
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
            case FieldKind::Check:
                f.checkValue = (value == "true" || value == "1" || value == "yes");
                break;
            case FieldKind::Dim: {
                try {
                    f.dimValue = std::stod(value); f.dimExpr.clear(); f.dimError.clear();
                    std::string disp = pmFieldValueText(f.def, f.dimValue, ips_);
                    std::strncpy(f.dimBuf.data(), disp.c_str(), f.dimBuf.size() - 1);
                } catch (...) {}
                break;
            }
            case FieldKind::Int:
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
        if (f.def->kind != FieldKind::Dim || !f.def->key || key != f.def->key) continue;
        if (!fieldVisible(*f.def, fields_) || !fieldEnabled(*f.def, fields_)) return false;
        f.dimValue = value;
        f.dimExpr.clear();
        f.dimError.clear();
        std::string disp = pmFieldValueText(f.def, f.dimValue, ips_);
        std::strncpy(f.dimBuf.data(), disp.c_str(), f.dimBuf.size() - 1);
        dirty_ = true;
        lastEditTime_ = ImGui::GetTime() - kDebounceSec;  // preview immediately (like the steppers)
        return true;
    }
    return false;
}

void PropertyManager::loadFromTree(app::AppState& st, const nlohmann::json& feature) {
    (void)st;
    nlohmann::json params = feature.value("params", nlohmann::json::object());
    nlohmann::json refs   = feature.value("refs",   nlohmann::json::object());
    for (auto& f : fields_) {
        const char* key = f.def->key;
        switch (f.def->kind) {
            case FieldKind::Dim: {
                if (params.contains(key) && params[key].is_object()) {
                    const auto& d = params[key];
                    if (d.contains("expr") && d["expr"].is_string()) {
                        f.dimExpr = d["expr"].get<std::string>();
                        if (d.contains("value") && d["value"].is_number())
                            f.dimValue = d["value"].get<double>();
                        std::string disp = "=" + f.dimExpr;
                        std::strncpy(f.dimBuf.data(), disp.c_str(), f.dimBuf.size() - 1);
                    } else if (d.contains("value") && d["value"].is_number()) {
                        f.dimValue = d["value"].get<double>();
                        std::string disp = pmFieldValueText(f.def, f.dimValue, ips_);
                        std::strncpy(f.dimBuf.data(), disp.c_str(), f.dimBuf.size() - 1);
                    }
                }
                break;
            }
            case FieldKind::Combo: {
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
            case FieldKind::Check: {
                if (params.contains(key) && params[key].is_boolean())
                    f.checkValue = params[key].get<bool>();
                break;
            }
            case FieldKind::Int: {
                if (params.contains(key) && params[key].is_number())
                    f.intValue = (int)(params[key].get<double>() + 0.5);
                else if (params.contains(key) && params[key].is_object() &&
                         params[key]["value"].is_number())
                    f.intValue = (int)(params[key]["value"].get<double>() + 0.5);
                break;
            }
            case FieldKind::DatumRef: {
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
            case FieldKind::FeatureWell: {
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
            case FieldKind::SelectorWell: {
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

void PropertyManager::reformatDimDisplays() {
    for (auto& f : fields_) {
        if (!f.def || f.def->kind != FieldKind::Dim) continue;
        if (!f.dimExpr.empty()) continue;          // expr binding shown as "=expr"; leave it
        if (f.def->unitLabel && f.def->unitLabel[0] == '\0') continue;  // unit-less field
        if (f.def->unitLabel && std::strstr(f.def->unitLabel, "deg")) continue;  // angle: degrees both
        std::string disp = pmFormatDim(f.dimValue, ips_);
        std::strncpy(f.dimBuf.data(), disp.c_str(), f.dimBuf.size() - 1);
        f.dimError.clear();
    }
}

void PropertyManager::close() {
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
    checkpointId_.clear();    // C37-F
    focusedOnOpen_ = false;   // C37-F
    wantFocusFirstField_ = false; // C37-F
    previewError_.clear();    // C37-F
    previewWarning_.clear();
    committing_ = false;
}

// ---- selector wells -------------------------------------------------------

PMField* PropertyManager::activeWell() {
    for (auto& f : fields_)
        if (f.def->kind == FieldKind::SelectorWell && f.wellActive &&
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
        if (f.def->kind != FieldKind::SelectorWell || !fieldVisible(*f.def, fields_)) continue;
        const bool empty = f.picks.empty();
        if (&f == filled) { seenFilled = true; continue; }
        if (empty && !firstEmpty) firstEmpty = &f;
        if (empty && seenFilled && !next) next = &f;
    }
    PMField* target = next ? next : firstEmpty;
    if (!target) return;                    // no empty well left — keep focus where it is
    for (auto& o : fields_)
        if (o.def->kind == FieldKind::SelectorWell || o.def->kind == FieldKind::FeatureWell)
            o.wellActive = false;
    target->wellActive = true;
}

bool PropertyManager::routePick(const nlohmann::json& selector, int pickKind,
                                uint32_t pickBody, uint32_t pickTopo) {
    PMField* well = activeWell();
    if (!well || !selector.is_object()) return false;
    // Filter by allowed kinds.
    worker::PickKind k = static_cast<worker::PickKind>(pickKind);
    const SelectorKinds& ak = well->def->selKinds;
    bool allowed = (k == worker::PickKind::Face   && ak.face) ||
                   (k == worker::PickKind::Edge   && ak.edge) ||
                   (k == worker::PickKind::Vertex && ak.vertex);
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
    advanceWellAfterFill(well);
    return true;
}

void PropertyManager::collectActiveWellHighlights(std::vector<std::array<uint32_t, 3>>& out) const {
    for (const auto& f : fields_) {
        if (f.def->kind != FieldKind::SelectorWell || !f.wellActive ||
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
    const std::string ftype = form_->featureType ? form_->featureType : "";
    if (ftype != "fillet" && ftype != "chamfer") return;  // dashed old-edge overlay is blend-only
    for (const auto& f : fields_) {
        if (f.def->kind != FieldKind::SelectorWell || !f.wellActive ||
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
        if (f.def->kind != FieldKind::SelectorWell || !f.wellActive ||
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
        if (f.def->kind == FieldKind::SelectorWell && f.wellActive &&
            fieldVisible(*f.def, fields_))
            return f.def->selKinds.body;
    return false;
}

bool PropertyManager::activeWellWantsPlane() const {
    for (const auto& f : fields_)
        if (f.def->kind == FieldKind::SelectorWell && f.wellActive &&
            fieldVisible(*f.def, fields_))
            return f.def->selKinds.plane;
    return false;
}

bool PropertyManager::activeWellWantsAxis() const {
    for (const auto& f : fields_)
        if (f.def->kind == FieldKind::SelectorWell && f.wellActive &&
            fieldVisible(*f.def, fields_))
            return f.def->selKinds.axis;
    return false;
}

bool PropertyManager::activeWellPrefersEdge() const {
    for (const auto& f : fields_)
        if (f.def->kind == FieldKind::SelectorWell && f.wellActive &&
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
    const std::string t = form_->featureType ? form_->featureType : "";
    if (t != "fillet" && t != "chamfer") return false;
    for (const auto& f : fields_)
        if (f.def->kind == FieldKind::SelectorWell && f.wellActive &&
            fieldVisible(*f.def, fields_))
            return f.def->selKinds.edge && !f.def->selKinds.face;
    return false;
}

bool PropertyManager::routeDatumAxis(const std::string& fid, const std::string& label) {
    PMField* well = activeWell();
    if (!well || !well->def->selKinds.axis || fid.empty()) return false;
    // Toggle off if this exact datum axis is already in the well (SolidWorks re-pick).
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
    e.selector = {{"kind", "datum"}, {"feature", fid}};  // resolve_axis_ref accepts this
    e.label = label.empty() ? fid : label;
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

std::string PropertyManager::activeWellKindHint() const {
    for (const auto& f : fields_) {
        if (f.def->kind != FieldKind::SelectorWell || !f.wellActive ||
            !fieldVisible(*f.def, fields_)) continue;
        const SelectorKinds& k = f.def->selKinds;
        std::vector<const char*> parts;
        if (k.face)   parts.push_back("faces");
        if (k.edge)   parts.push_back("edges");
        if (k.vertex) parts.push_back("vertices");
        if (k.body)   parts.push_back("bodies");
        if (k.plane)  parts.push_back("planes");
        if (k.axis)   parts.push_back("axes");
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
        if (f.def->kind == FieldKind::SelectorWell &&
            (std::strcmp(f.def->key, "profile") == 0 || std::strcmp(f.def->key, "sections") == 0))
            return &f;
    return nullptr;
}

const PMField* PropertyManager::profileWell() const {
    for (const auto& f : fields_)
        if (f.def->kind == FieldKind::SelectorWell &&
            (std::strcmp(f.def->key, "profile") == 0 || std::strcmp(f.def->key, "sections") == 0))
            return &f;
    return nullptr;
}

bool PropertyManager::activeWellWantsPathFeature() const {
    if (!active_) return false;
    // activeWell() is non-const; inline the same lookup here.
    for (const auto& f : fields_) {
        if (f.def->kind != FieldKind::SelectorWell || !f.wellActive ||
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
        if (f.def->kind != FieldKind::SelectorWell) continue;
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

void PropertyManager::setProfilePicks(app::AppState& st, const std::vector<nlohmann::json>& sels) {
    (void)st;
    PMField* w = profileWell();
    if (!w) return;
    w->picks.clear();
    for (const auto& s : sels) {
        PMSelEntry e; e.selector = s; e.label = pmRegionLabel(s);
        w->picks.push_back(std::move(e));
    }
    w->wellDangling = false;
    dirty_ = true; lastEditTime_ = ImGui::GetTime();
}

std::vector<std::string> PropertyManager::regionCandidateSketches(app::AppState& st) const {
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
    feature["type"] = form_->featureType;
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
            case FieldKind::Dim:
                params[key] = dimJson(f);
                break;
            case FieldKind::Combo: {
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
            case FieldKind::Check:
                params[key] = f.checkValue;
                break;
            case FieldKind::Int:
                params[key] = f.intValue;
                break;
            case FieldKind::Text: {
                // Emit a free-form string param only when the user typed something (an empty skip
                // field must NOT emit params.skip — the engine then patterns the full grid).
                std::string txt = f.textBuf.data();
                if (!txt.empty()) params[key] = txt;
                break;
            }
            case FieldKind::DatumRef: {
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
            case FieldKind::SelectorWell: {
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
            case FieldKind::FeatureWell: {
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
    feature["params"] = params;
    if (!refs.empty()) feature["refs"] = refs;
}

static bool readyForPreview(const PanelSpec* form, const std::vector<PMField>& fields) {
    if (!form) return false;
    std::string t = form->featureType;
    if (t == "mirror" || t == "split") return true;
    if (t == "loft") {
        for (const auto& f : fields)
            if (f.def->kind == FieldKind::SelectorWell && std::strcmp(f.def->key, "sections") == 0)
                return f.picks.size() >= 2;
        return false;
    }
    if (t == "fillet" || t == "chamfer") {
        static const char* anyWell[] = {"edges", "middle_face", "face_set_a", "vertices"};
        for (const auto& f : fields) {
            if (f.def->kind != FieldKind::SelectorWell || f.picks.empty()) continue;
            for (const char* k : anyWell)
                if (std::strcmp(f.def->key, k) == 0) return true;
        }
        return false;
    }
    const char* needWell = nullptr;
    if (t == "extrude" || t == "revolve" || t == "sweep") needWell = "profile";
    else if (t == "hole") needWell = "face";
    else if (t == "draft") needWell = "faces";
    else if (t == "linear_pattern" || t == "circular_pattern") needWell = "features";
    else if (t == "weld_member") needWell = "sketch";
    else if (t == "weld_gusset") needWell = "faces";
    else if (t == "weld_end_cap") needWell = "face";
    else if (t == "boolean") needWell = "targets";
    else if (t == "sm_base_flange") needWell = "profile";
    else if (t == "sm_edge_flange" || t == "sm_hem" || t == "sm_jog") needWell = "edge";
    else if (t == "axis") needWell = "a";
    else if (t == "point") needWell = "target";
    else if (t == "rib") needWell = "profile";
    else if (t == "surf_extrude" || t == "surf_revolve" || t == "surf_sweep" || t == "surf_planar")
        needWell = "profile";
    else if (t == "surf_loft") needWell = "sections";
    else if (t == "surf_offset") needWell = "faces";
    else if (t == "surf_ruled") needWell = "edge_a";
    else if (t == "surf_boundary") needWell = "dir1";
    else if (t == "surf_fill") needWell = "edges";
    else if (t == "surf_knit") needWell = "surfaces";
    else if (t == "surf_thicken" || t == "surf_trim") needWell = "surface";
    else if (t == "surf_untrim" || t == "surf_delete_face" || t == "surf_replace_face") needWell = "faces";
    if (needWell) {
        for (const auto& f : fields)
            if ((f.def->kind == FieldKind::SelectorWell || f.def->kind == FieldKind::FeatureWell)
                && fieldVisible(*f.def, fields) && std::strcmp(f.def->key, needWell) == 0)
                return !f.picks.empty();
        return false;
    }
    for (const auto& f : fields) {
        if (f.def->kind != FieldKind::Dim || f.def->comingSoon) continue;
        if (!fieldVisible(*f.def, fields)) continue;
        if (!f.dimError.empty()) return false;
        if (f.dimValue > 0.0) return true;
    }
    return false;
}

bool PropertyManager::applyPreview(app::AppState& st) {
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
            st.status = "Feature " + targetId_ + " no longer exists — closing PM";
            (void)("[pm] tick: feature " + targetId_ + " vanished; closing PM");
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
        (void)("[pm] blend preview: edges=" + std::to_string(
                      feature.value("refs", nlohmann::json::object())
                          .value("edges", nlohmann::json::array()).size()));

    auto resp = st.docMirror.applyOps(st.worker, ops, previewUndoGroupStarted_);
    if (!resp.ok) {
        // C38 (P4f): the inline previewError_ block in the PM body is the surface for a failed
        // debounced preview — do NOT toast here. Previews fire per-keystroke, so toasting each
        // failure spams the user with transient parse/validation errors mid-edit. Toasts stay on the
        // commit/cancel paths (terminal, user-initiated). Still logged for observability.
        previewError_ = resp.error_message;
        (void)("[pm] preview failed: " + resp.error_message);
        dirty_ = false;
        return false;
    }
    previewError_.clear();  // C37-F: clear stale error on success
    if (!previewUndoGroupStarted_) {
        previewUndoGroupStarted_ = true;
        ++previewBatches_;
    }
    // The add-feature flow learns the new feature id from id_map ($0 -> fX).
    if (targetId_.empty() && resp.id_map.is_object()) {
        auto it = resp.id_map.find("$0");
        if (it != resp.id_map.end() && it->is_string()) targetId_ = it->get<std::string>();
    }
    firstPreviewDone_ = true;

    if (!resp.aicm_path.empty()) {
        std::string lerr;
        if (st.scene && !resp.aicm_path.empty()) st.scene->loadAicm(resp.aicm_path);   // live rounded body -> id-pass + overlays + render
        if (!lerr.empty()) (void)("[pm] mesh: " + lerr);
    }

    // Read the session compile's verdict for this feature (the host's applyOps already ran the real
    // compile — there is no separate stateless compile; the old doc_compile_cpp call was an unknown
    // method in-app and failed silently). For a blend the warning — "rounded N of M edge(s); K
    // couldn't be located" — is the "selection saved but an edge didn't apply" signal; we log it so a
    // dropped edge is visible, surface it in the PM body, and tint any dangling well red.
    previewWarning_.clear();
    if (!targetId_.empty() && resp.feature_status.is_array()) {
        for (const auto& s : resp.feature_status) {
            if (!s.is_object() || s.value("id", std::string()) != targetId_) continue;
            auto wt = s.find("warning");
            if (wt != s.end() && wt->is_string()) previewWarning_ = wt->get<std::string>();
            const bool sok = s.value("ok", true);
            if (!sok) previewError_ = s.value("error", std::string());
            if (blend)
                (void)("[pm] feature status " + targetId_ +
                          ": ok=" + std::string(sok ? "1" : "0") +
                          " warn=\"" + (wt != s.end() && wt->is_string() ? wt->get<std::string>() : "") + "\"" +
                          (sok ? "" : " err=\"" + s.value("error", std::string()) + "\""));
            auto dr = s.find("dangling_refs");
            std::vector<std::string> dangling;
            if (dr != s.end() && dr->is_array())
                for (const auto& d : *dr) if (d.is_string()) dangling.push_back(d.get<std::string>());
            for (auto& f : fields_) {
                if (f.def->kind != FieldKind::SelectorWell) continue;
                f.wellDangling = false;
                for (const auto& dref : dangling)
                    if (dref.rfind(f.def->key, 0) == 0) f.wellDangling = true;
            }
        }
    }
    dirty_ = false;
    return true;
}

void PropertyManager::tick(app::AppState& st) {
    if (!active_ || !dirty_) return;
    double now = ImGui::GetTime();
    if (now - lastEditTime_ < kDebounceSec) return;
    applyPreview(st);
    // applyPreview may have called close() if the feature vanished; check active_ again.
    (void)active_;
}

void PropertyManager::commit(app::AppState& st) {
    // Make sure the latest field state is reflected before committing.
    if (!readyForPreview(form_, fields_)) {
        previewError_ = "Select the required references and enter valid dimensions before accepting.";
        
        return;
    }
    // The fillet/chamfer is already applied live in the session, so commit just flushes any pending
    // edit and closes — no un-suppress step is needed anymore.
    if (dirty_ || !firstPreviewDone_) {
        if (!applyPreview(st)) {
            
            return;
        }
    }
    if (!previewError_.empty()) {
        (void)("[pm] OK blocked: " + previewError_);
        return;
    }
    (void)("[pm] OK -> committed " + (targetId_.empty() ? std::string("(no geometry)") : targetId_));
    st.buildTreeFromDoc();
    st.onDocMutation();
    close();
}

bool PropertyManager::cancelEdits(app::AppState& st) {
    // C37-F: Single revert call replaces the old N-undo loop, eliminating the UI freeze.
    bool ok = true;
    if (!checkpointId_.empty()) {
        auto r = st.docMirror.revert(st.worker, checkpointId_);
        if (r.ok) {
            if (!r.aicm_path.empty()) { std::string le; if (st.scene && !r.aicm_path.empty()) st.scene->loadAicm(r.aicm_path); }
            (void)("[pm] Cancel -> reverted to checkpoint (was " +
                      std::to_string(previewBatches_) + " preview batch(es))");
        } else {
            st.status = "Cancel revert failed: " + r.error_message;
            (void)("[pm] Cancel revert failed: " + r.error_message);
            ok = false;
        }
    } else {
        // Fallback: checkpoint wasn't available at open — use the undo loop.
        int n = previewBatches_;
        for (int i = 0; i < n; ++i) {
            auto r = st.docMirror.undo(st.worker);
            if (!r.ok) { (void)("[pm] cancel undo failed: " + r.error_message); ok = false; break; }
            if (!r.aicm_path.empty()) { std::string le; if (st.scene && !r.aicm_path.empty()) st.scene->loadAicm(r.aicm_path); }
        }
        if (ok)
            (void)("[pm] Cancel -> undid " + std::to_string(n) + " preview batch(es) (no checkpoint)");
    }
    if (!ok) return false;
    close();
    return true;
}

// ---- drawing --------------------------------------------------------------



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
                           const theme::Palette& pal, bool enabled) {
    const float S = theme::S();
    const float avail = ImGui::GetContentRegionAvail().x;
    const bool inlineRow = avail >= 220.0f * S;
    float labelW = avail * 0.40f;
    labelW = std::max(112.0f * S, std::min(labelW, 138.0f * S));

    ImGui::PushStyleColor(ImGuiCol_Text, theme::vec(enabled ? pal.txt0 : pal.txt1));
    if (inlineRow) {
        ImGui::AlignTextToFramePadding();
        ImGui::Text("%s%s", label, suffix ? suffix : "");
        ImGui::PopStyleColor();
        if (ImGui::IsItemHovered()) ImGui::SetTooltip("%s%s", label, suffix ? suffix : "");
        ImGui::SameLine(labelW);
        return true;
    }

    ImGui::Text("%s%s", label, suffix ? suffix : "");
    ImGui::PopStyleColor();
    return false;
}

static bool pmSectionHeader(const char* label, const theme::Palette& pal) {
    if (!label || !label[0]) return true;
    const float S = theme::S();
    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(9.0f * S, 4.0f * S));
    ImGui::PushStyleColor(ImGuiCol_Header, pmPal(pal.bg2));
    ImGui::PushStyleColor(ImGuiCol_HeaderHovered, pmPal(pal.hover));
    ImGui::PushStyleColor(ImGuiCol_HeaderActive, TVEC(accentSoft));
    bool open = ImGui::CollapsingHeader(label, ImGuiTreeNodeFlags_DefaultOpen);
    ImGui::PopStyleColor(3);
    ImGui::PopStyleVar();
    return open;
}

// Wave 1D: standalone wrapper — owns the FeatureManager-slot Begin/End plus the C37-F floating
// fallback, then defers the form content to drawBody(). The Wave 1D tabbed left pane calls
// drawBody() directly inside its "LeftPane" container child (no separate window).
void PropertyManager::drawBody(app::AppState& st) {
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
        bool haveVisibleActive = false;
        for (const auto& f : fields_)
            if (f.def->kind == FieldKind::SelectorWell && f.wellActive &&
                fieldVisible(*f.def, fields_)) { haveVisibleActive = true; break; }
        if (!haveVisibleActive) {
            PMField* target = nullptr;
            for (auto& f : fields_) {
                if (f.def->kind != FieldKind::SelectorWell || !fieldVisible(*f.def, fields_))
                    continue;
                if (!target) target = &f;                       // fallback: first visible well
                if (f.picks.empty()) { target = &f; break; }    // prefer the first empty well
            }
            if (target) {
                for (auto& o : fields_)
                    if (o.def->kind == FieldKind::SelectorWell ||
                        o.def->kind == FieldKind::FeatureWell)
                        o.wellActive = false;
                target->wellActive = true;
            }
        }
    }

    // Live unit-system toggle: if the document units changed while this form is open (e.g. the
    // user clicked the status-bar MMGS/IPS cluster), reformat every open Dim field's DISPLAY from
    // its canonical mm value. The stored value is unchanged — purely a display reformat.
    if ((st.units == "in") != ips_) { ips_ = (st.units == "in"); reformatDimDisplays(); }

    const auto& pal = theme::pal();

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
    bool sketchActive = st.inSketch;
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
        const float S         = theme::S();
        const float glyphSz   = 26.0f * S;
        const float hdrPadV   = 7.0f * S;
        const float hdrPadH   = 10.0f * S;
        const float accentBarW = 4.0f * S;
        const float rowAvail  = std::max(1.0f, ImGui::GetContentRegionAvail().x);
        float btnW            = 26.0f * S;
        const float gap       = 6.0f * S;
        if (btnW * 2.0f + gap > rowAvail - 50.0f * S)
            btnW = std::max(16.0f * S, (rowAvail - 50.0f * S - gap) * 0.5f);
        const float actionW   = btnW * 2.0f + gap;
        const float hdrH      = glyphSz + hdrPadV * 2.0f;

        // Mode-aware icon: use the cut variant for boss/cut operations when mode_ is "cut".
        const char* hdrIcon = form_->icon;
        {
            const std::string& ft = form_->featureType;
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
        theme::glassBackground(dl, hdrMin, hdrMax, 6.0f);
        {
            const float bi = 4.0f * S;
            dl->AddRectFilled(ImVec2(hdrMin.x,             hdrMin.y + bi),
                              ImVec2(hdrMin.x + accentBarW, hdrMax.y - bi),
                              theme::rgba(theme::pal().accent), 2.0f * S);
        }

        // Feature glyph (26 dp; use @32 atlas cell for sharpness).
        ImGui::SetCursorScreenPos(ImVec2(hdrMin.x + accentBarW + hdrPadH, hdrMin.y + hdrPadV));
        float afterGlyphX = hdrMin.x + accentBarW + hdrPadH;
        IconUV ic = ui::getIcon(hdrIcon, 32);
        if (ic.valid) {
            ImGui::Image(ic.texId, ImVec2(glyphSz, glyphSz), ic.uv0, ic.uv1);
            afterGlyphX += glyphSz + 8.0f * S;
        }

        // Title — vertically centred, in accent color.
        float titleY = hdrMin.y + (hdrH - ImGui::GetTextLineHeight()) * 0.5f;
        ImGui::SetCursorScreenPos(ImVec2(afterGlyphX, titleY));
        ImGui::PushStyleColor(ImGuiCol_Text, TVEC(accent));
        ImGui::TextUnformatted(title_.c_str());
        ImGui::PopStyleColor();

        // OK / Cancel — right-aligned within the glass header row.
        float okX = hdrMin.x + rowAvail - actionW - hdrPadH;
        float okY = hdrMin.y + (hdrH - btnW) * 0.5f;
        ImGui::SetCursorScreenPos(ImVec2(okX, okY));

        ImGui::PushStyleColor(ImGuiCol_Button,     theme::pmview::okBtn());
        ImGui::PushStyleColor(ImGuiCol_ButtonHovered, theme::pmview::okBtnHovered());
        ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(4.0f * S, 4.0f * S));
        {
            IconUV okIc = ui::getIcon("ok_badge", 20);
            if (okIc.valid)
                ok = ImGui::ImageButton("##pm_ok", okIc.texId,
                         ImVec2(btnW - 8.0f * S, btnW - 8.0f * S), okIc.uv0, okIc.uv1);
            else
                ok = ImGui::Button("OK##pm", ImVec2(btnW, btnW));
        }
        if (ImGui::IsItemHovered()) ImGui::SetTooltip("OK (Enter)");
        ImGui::PopStyleVar();
        ImGui::PopStyleColor(2);

        ImGui::SameLine(0, gap);

        ImGui::PushStyleColor(ImGuiCol_Button, pmPal(pal.bg3, 0.9f));
        ImGui::PushStyleColor(ImGuiCol_ButtonHovered, pmPal(pal.bad, 0.7f));
        ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(4.0f * S, 4.0f * S));
        {
            IconUV cancelIc = ui::getIcon("error_badge", 20);
            if (cancelIc.valid)
                cancel = cancel || ImGui::ImageButton("##pm_cancel", cancelIc.texId,
                             ImVec2(btnW - 8.0f * S, btnW - 8.0f * S), cancelIc.uv0, cancelIc.uv1);
            else
                cancel = cancel || ImGui::Button("X##pm", ImVec2(btnW, btnW));
        }
        if (ImGui::IsItemHovered()) ImGui::SetTooltip("Cancel (Esc)");
        ImGui::PopStyleVar();
        ImGui::PopStyleColor(2);

        // Resume layout below the glass header strip.
        ImGui::SetCursorScreenPos(ImVec2(hdrMin.x, hdrMax.y + 4.0f * S));

        // Optional form description.
        const char* desc = formDescription(form_->featureType);
        if (desc && desc[0]) {
            ImGui::PushStyleColor(ImGuiCol_Text, TVEC(txt1));
            float wrapW = ImGui::GetContentRegionAvail().x;
            ImGui::PushTextWrapPos(ImGui::GetCursorPosX() + wrapW);
            ImGui::TextUnformatted(desc);
            ImGui::PopTextWrapPos();
            ImGui::PopStyleColor();
        }

        ImGui::Separator();

        // Wave 1D: drawBody() does NOT own the window — early-out is a plain return; the caller
        // (draw() or the LeftPane container) closes its own Begin/child.
        if (ok)     { commit(st);      return; }
        if (cancel) { cancelEdits(st); return; }
    }

    // ---- field rows grouped by `group`, collapsible ------------------------
    const char* curGroup = nullptr;
    bool groupOpen = true;
    bool groupCardOpen = false;
    const bool useIps = ips_;   // document-unit display (set at open; re-synced above on toggle)

    // C37-F: track which Dim field is first/editable for SetKeyboardFocusHere.
    bool firstEditableDimSeen = false;

    auto closeGroupCard = [&]() {
        if (!groupCardOpen) return;
        ImGui::EndChild();
        ImGui::PopStyleColor(2);
        ImGui::PopStyleVar(2);
        ImGui::PopID();
        ImGui::Dummy(ImVec2(0, 6.0f * theme::S()));
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
            if (groupOpen) {
                ImGui::PushID(curGroup);
                ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                                    ImVec2(9.0f * theme::S(), 8.0f * theme::S()));
                ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing,
                                    ImVec2(7.0f * theme::S(), 7.0f * theme::S()));
                // PM group cards adopt the frosted-glass roles so each form section reads as a
                // floating glass card (translucent glassBg lets the panel bleed through subtly).
                ImGui::PushStyleColor(ImGuiCol_ChildBg, TVEC(bg1));
                ImGui::PushStyleColor(ImGuiCol_Border, TVEC(line));
                ImGui::BeginChild("##pm_group_card", ImVec2(0, 0),
                                  ImGuiChildFlags_Border | ImGuiChildFlags_AutoResizeY |
                                  ImGuiChildFlags_AlwaysUseWindowPadding);
                groupCardOpen = true;
            }
        }
        if (!groupOpen) continue;

        // Conditional enable: grey-out when the sibling Combo condition isn't met or comingSoon.
        bool enabled = fieldEnabled(*f.def, fields_);
        if (!enabled) ImGui::BeginDisabled();

        ImGui::PushID((int)i);
        if (false) {
            ImGui::PushStyleColor(ImGuiCol_Text, TVEC(accent));
            ImGui::TextUnformatted("AI suggestion");
            ImGui::PopStyleColor();
        }
        switch (f.def->kind) {
            case FieldKind::Dim: {
                // C37-F: unit suffix in label ("Radius [mm]"). A field may override it (angle =
                // "[deg]"; an empty override = no suffix).
                const char* unitSuffix = f.def->unitLabel ? f.def->unitLabel
                                                          : (useIps ? " [in]" : " [mm]");
                pmCompactLabel(f.def->label, unitSuffix, pal, enabled);
                if (f.def->comingSoon && ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled))
                    ImGui::SetTooltip("Not yet supported by engine");
                // Reserve room for the - / + step buttons so the InputText doesn't shove them past
                // the pane's right edge (was SetNextItemWidth(-1), which let the steppers overflow
                // / clip — the most visible "misplaced element" on the Extrude form).
                {
                    const ImGuiStyle& dimStl = ImGui::GetStyle();
                    float stepBtnW = ImGui::CalcTextSize("+").x + dimStl.FramePadding.x * 2.0f;
                    ImGui::SetNextItemWidth(-(stepBtnW * 2.0f + 9.0f * theme::S()));
                }

                // C37-F: SetKeyboardFocusHere on the very first editable Dim field when opening.
                if (wantFocusFirstField_ && enabled && !f.def->comingSoon && !firstEditableDimSeen) {
                    ImGui::SetKeyboardFocusHere(0);
                    firstEditableDimSeen = true;
                    // Don't clear wantFocusFirstField_ yet — clear it after this frame so the
                    // focus request is processed.
                }

                bool changed = ImGui::InputText("##dim", f.dimBuf.data(), f.dimBuf.size(),
                                                ImGuiInputTextFlags_EnterReturnsTrue |
                                                ImGuiInputTextFlags_AutoSelectAll);
                bool deact = ImGui::IsItemDeactivatedAfterEdit();

                // C37-F: Step buttons (- / +) trigger immediate preview (no debounce).
                ImGui::SameLine(0, 3 * theme::S());
                ImGui::PushStyleColor(ImGuiCol_Button, pmPal(pal.bg3));
                if (ImGui::SmallButton("-##step")) {
                    if (enabled && !f.def->comingSoon) {
                        f.dimValue = std::max(0.0, f.dimValue - 1.0);
                        std::string disp = pmFieldValueText(f.def, f.dimValue, useIps);
                        std::strncpy(f.dimBuf.data(), disp.c_str(), f.dimBuf.size() - 1);
                        f.dimError.clear();
                        dirty_ = true;
                        lastEditTime_ = ImGui::GetTime() - kDebounceSec;  // fire immediately
                    }
                }
                ImGui::SameLine(0, 2 * theme::S());
                if (ImGui::SmallButton("+##step")) {
                    if (enabled && !f.def->comingSoon) {
                        f.dimValue += 1.0;
                        std::string disp = pmFieldValueText(f.def, f.dimValue, useIps);
                        std::strncpy(f.dimBuf.data(), disp.c_str(), f.dimBuf.size() - 1);
                        f.dimError.clear();
                        dirty_ = true;
                        lastEditTime_ = ImGui::GetTime() - kDebounceSec;  // fire immediately
                    }
                }
                ImGui::PopStyleColor();

                // C37-F: mouse-wheel on hovered field triggers immediate preview.
                if (ImGui::IsItemHovered() || ImGui::IsItemActive()) {
                    float wheel = io.MouseWheel;
                    if (wheel != 0.0f && enabled && !f.def->comingSoon) {
                        f.dimValue = std::max(0.0, f.dimValue + (double)wheel);
                        std::string disp = pmFieldValueText(f.def, f.dimValue, useIps);
                        std::strncpy(f.dimBuf.data(), disp.c_str(), f.dimBuf.size() - 1);
                        f.dimError.clear();
                        dirty_ = true;
                        lastEditTime_ = ImGui::GetTime() - kDebounceSec;  // fire immediately
                    }
                }

                if (enabled && (changed || deact)) {
                    // `changed` is true ONLY when Enter is pressed (EnterReturnsTrue) — an explicit
                    // "apply now" gesture, so we backdate lastEditTime_ past the debounce window to
                    // fire the preview this frame. A plain blur (`deact` without `changed`) keeps the
                    // normal debounce.
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
                        // A bare (unit-less) number follows the document length unit: mm in MMGS,
                        // inch in IPS. Angle fields ([deg]) stay degrees in both systems (bare =
                        // degrees) and parse with angle=true so the "deg" suffix they now display
                        // round-trips and a stray length unit is rejected on them.
                        const bool isAngle = pmFieldIsAngle(f.def);
                        // angle + unit-less (ratio/k-factor/turns) values are never IPS-scaled.
                        const bool unscaled = isAngle || pmFieldUnitless(f.def);
                        const double bareScale = (useIps && !unscaled) ? 25.4 : 1.0;
                        if (pmParseDim(txt, mm, err, /*angle=*/isAngle, bareScale)) {
                            f.dimValue = mm; f.dimExpr.clear(); f.dimError.clear();
                            dirty_ = true; lastEditTime_ = applyTime;
                        } else {
                            f.dimError = err;
                        }
                    }
                }
                // expr resolved-value + tooltip
                if (enabled && !f.dimExpr.empty()) {
                    ImGui::PushStyleColor(ImGuiCol_Text, TVEC(accent));
                    ImGui::Text(u8"= %s  (%s)", f.dimExpr.c_str(),
                                pmFieldValueText(f.def, f.dimValue, useIps).c_str());
                    ImGui::PopStyleColor();
                    if (ImGui::IsItemHovered())
                        ImGui::SetTooltip("Bound to equation '%s'", f.dimExpr.c_str());
                }
                // C37-F: inline red error — shown as a red hint line, not silently ignored.
                if (enabled && !f.dimError.empty()) {
                    ImGui::PushStyleColor(ImGuiCol_Text, TVEC(bad));
                    ImGui::Text(u8"! %s", f.dimError.c_str());
                    ImGui::PopStyleColor();
                }
                break;
            }
            case FieldKind::SelectorWell: {
                // Label + activate toggle.
                ImGui::PushStyleColor(ImGuiCol_Text,
                    theme::vec(f.wellActive ? pal.accent : pal.txt1));
                ImGui::TextUnformatted(f.def->label);
                ImGui::PopStyleColor();

                // C37-F: chip shows count + entity kind for filled wells.
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

                ImVec4 border = f.wellDangling ? pmPal(pal.bad)
                    : (f.wellActive ? pmPal(pal.accent) : pmPal(pal.line));
                const float S = theme::S();
                const float availW = ImGui::GetContentRegionAvail().x;
                const float textPadX = 10.0f * S;
                const float textPadY = 7.0f * S;
                const float wrapW = std::max(40.0f * S, availW - textPadX * 2.0f);
                ImVec2 chipText = ImGui::CalcTextSize(chip.c_str(), nullptr, false, wrapW);
                float wellH = std::max(ImGui::GetFrameHeight(), chipText.y + textPadY * 2.0f);

                ImVec2 wp0 = ImGui::GetCursorScreenPos();
                ImVec2 wp1(wp0.x + availW, wp0.y + wellH);
                if (ImGui::InvisibleButton("##well", ImVec2(availW, wellH))) {
                    // Activate this well; deactivate others.
                    for (auto& o : fields_)
                        if (o.def->kind == FieldKind::SelectorWell) o.wellActive = false;
                    f.wellActive = true;
                }
                const bool wellHovered = ImGui::IsItemHovered();
                ImDrawList* dl = ImGui::GetWindowDrawList();
                ImVec4 fill = f.wellActive ? TVEC(accentSoft)
                    : (wellHovered ? pmPal(pal.hover) : pmPal(pal.bg2));
                dl->AddRectFilled(wp0, wp1, ImGui::ColorConvertFloat4ToU32(fill),
                                  6.0f * S);
                dl->AddRect(wp0, wp1, ImGui::ColorConvertFloat4ToU32(border), 6.0f * S, 0,
                            f.wellActive ? std::max(1.0f, 2.0f * S)
                                          : std::max(1.0f, 1.0f * S));
                ImU32 chipCol = f.picks.empty() ? theme::rgba(pal.txt1) : theme::rgba(pal.txt0);
                dl->AddText(nullptr, 0.0f, ImVec2(wp0.x + textPadX, wp0.y + textPadY),
                            chipCol, chip.c_str(), nullptr, wrapW);

                // C37-F: tooltip on the well button.
                if (wellHovered) {
                    if (f.wellActive)
                        ImGui::SetTooltip("Active — pick faces/edges in the viewport");
                    else if (!f.picks.empty())
                        ImGui::SetTooltip("Click to re-activate; right-click to clear");
                    else
                        ImGui::SetTooltip("Click to activate pick target");
                }

                if (ImGui::IsItemClicked(ImGuiMouseButton_Right)) {
                    f.picks.clear(); f.wellDangling = false;
                    dirty_ = true; lastEditTime_ = ImGui::GetTime();
                }
                // C37-F: hint line under active well.
                if (f.wellActive) {
                    // Use accent with reduced alpha as the hint color.
                    ImGui::PushStyleColor(ImGuiCol_Text, pmPal(pal.accent, 0.75f));
                    ImGui::TextUnformatted("Pick in viewport");
                    ImGui::PopStyleColor();
                }
                if (f.wellDangling) {
                    ImGui::PushStyleColor(ImGuiCol_Text, TVEC(bad));
                    ImGui::TextUnformatted("! dangling reference");
                    ImGui::PopStyleColor();
                }
                break;
            }
            case FieldKind::Combo: {
                pmCompactLabel(f.def->label, "", pal, enabled);
                if (f.def->comingSoon && ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled))
                    ImGui::SetTooltip("Not yet supported by engine");
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
                ImGui::SetNextItemWidth(-1);
                if (ImGui::BeginCombo("##combo", preview.c_str())) {
                    for (int n = 0; n < (int)labels.size(); ++n) {
                        bool sel = (f.comboIndex == n);
                        if (ImGui::Selectable(labels[n].c_str(), sel)) {
                            if (enabled) {
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
                                    if (w.def->kind != FieldKind::SelectorWell) continue;
                                    if (!w.def->enableKey ||
                                        std::strcmp(w.def->enableKey, ckey) != 0) continue;
                                    if (!w.picks.empty() || !fieldVisible(*w.def, fields_)) continue;
                                    reveal = &w; break;
                                }
                                if (reveal) {
                                    for (auto& o : fields_)
                                        if (o.def->kind == FieldKind::SelectorWell ||
                                            o.def->kind == FieldKind::FeatureWell)
                                            o.wellActive = false;
                                    reveal->wellActive = true;
                                }
                            }
                        }
                        if (sel) ImGui::SetItemDefaultFocus();
                    }
                    ImGui::EndCombo();
                }
                break;
            }
            case FieldKind::Check: {
                bool val = f.checkValue;
                // Render the box with no inline label, then place the label on the
                // same row with wrapping enabled. ImGui's built-in Checkbox label
                // never wraps and gets clipped on a narrow dock (e.g. the long
                // "Tangent propagation (coming soon)" rows) — wrap it ourselves.
                if (ImGui::Checkbox("##chk", &val)) {
                    if (enabled) { f.checkValue = val; dirty_ = true; lastEditTime_ = ImGui::GetTime(); }
                }
                bool boxHov = ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled);
                ImGui::SameLine(0, ImGui::GetStyle().ItemInnerSpacing.x);
                float wrapX = ImGui::GetCursorPosX() + ImGui::GetContentRegionAvail().x;
                ImGui::PushTextWrapPos(wrapX);
                ImGui::TextUnformatted(f.def->label);
                ImGui::PopTextWrapPos();
                if (f.def->comingSoon &&
                    (boxHov || ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled)))
                    ImGui::SetTooltip("Not yet supported by engine");
                break;
            }
            case FieldKind::Int: {
                pmCompactLabel(f.def->label, "", pal, enabled);
                ImGui::SetNextItemWidth(-1);
                int v = f.intValue;
                if (ImGui::InputInt("##int", &v)) {
                    if (enabled) {
                        if (v < 1) v = 1;
                        f.intValue = v; dirty_ = true; lastEditTime_ = ImGui::GetTime();
                    }
                }
                break;
            }
            case FieldKind::Text: {
                pmCompactLabel(f.def->label, "", pal, enabled);
                ImGui::SetNextItemWidth(-1);
                const char* hint = f.def->placeholder ? f.def->placeholder : "";
                if (ImGui::InputTextWithHint("##text", hint, f.textBuf.data(), f.textBuf.size())) {
                    if (enabled) { dirty_ = true; lastEditTime_ = ImGui::GetTime(); }
                }
                break;
            }
            case FieldKind::DatumRef: {
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
                    for (int n = 0; n < (int)labels.size(); ++n) {
                        bool sel = (f.comboIndex == n);
                        if (ImGui::Selectable(labels[n].c_str(), sel))
                            if (enabled) { f.comboIndex = n; dirty_ = true; lastEditTime_ = ImGui::GetTime(); }
                        if (sel) ImGui::SetItemDefaultFocus();
                    }
                    ImGui::EndCombo();
                }
                break;
            }
            case FieldKind::FeatureWell: {
                ImGui::PushStyleColor(ImGuiCol_Text, TVEC(txt1));
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
                    const std::string& fid = st.selectedFeature;
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
                        st.status = "Select a feature in the FeatureManager first";
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

    // C37-F: Inline preview error block (red text, shown when the last preview RPC failed).
    if (!previewError_.empty()) {
        ImGui::Separator();
        ImGui::PushStyleColor(ImGuiCol_Text, TVEC(bad));
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
        ImGui::PushStyleColor(ImGuiCol_Text, TVEC(txt1));
        float wrapW = ImGui::GetContentRegionAvail().x;
        ImGui::PushTextWrapPos(ImGui::GetCursorPosX() + wrapW);
        ImGui::Text(u8"! %s", previewWarning_.c_str());
        ImGui::PopTextWrapPos();
        ImGui::PopStyleColor();
    }

    // ---- live-preview status hint -----------------------------------------
    ImGui::Separator();
    ImGui::PushStyleColor(ImGuiCol_Text, TVEC(txt1));
    if (dirty_)            ImGui::TextUnformatted("editing\xe2\x80\xa6 (live preview)");
    else if (firstPreviewDone_ && !targetId_.empty())
        ImGui::Text("preview: %s", targetId_.c_str());
    else                  ImGui::TextUnformatted("set parameters to preview");
    ImGui::PopStyleColor();

    // C38 (P2f): record whether a PM widget is active at the end of OUR draw, scoped to this PM
    // window (active item AND PM window focus). Read at the top of next frame's draw() to gate
    // Enter-commit. We're still inside the PM window's Begin/End here, so IsAnyItemActive() +
    // window-focus is PM-scoped — an active item in another window won't set this.
    pmWidgetActivePrevFrame_ = ImGui::IsAnyItemActive() &&
        ImGui::IsWindowFocused(ImGuiFocusedFlags_RootAndChildWindows);
}


std::string PropertyManager::activeFormType() const {
    return (active_ && form_) ? form_->featureType : std::string();
}
std::string PropertyManager::previewFeatureId() const {
    return (active_ && !editMode_) ? targetId_ : std::string();
}
void PropertyManager::closeIfActive() { if (active_) close(); }

PropertyManager& propertyManager() { static PropertyManager pm; return pm; }

void pmApplyShotEnv(app::AppState& st) {
    const char* shot = std::getenv("AI_CAD_SHOT");
    if (!shot || std::strcmp(shot, "pmform") != 0) return;
    PropertyManager& pm = propertyManager();
    if (const char* cmd = std::getenv("AI_CAD_PMCMD")) {
        if (*cmd && !pm.openForCommand(st, cmd))
            std::fprintf(stderr, "pmform: no form for command '%s'\n", cmd);
    }
    if (const char* setEnv = std::getenv("AI_CAD_PMSET")) {
        std::string spec = setEnv; size_t pos = 0;
        while (pos < spec.size()) {
            size_t semi = spec.find(';', pos);
            std::string tok = spec.substr(pos, semi - pos);
            size_t eq = tok.find('=');
            if (eq != std::string::npos)
                pm.presetFieldForShot(tok.substr(0, eq), tok.substr(eq + 1));
            if (semi == std::string::npos) break;
            pos = semi + 1;
        }
    }
}

}  // namespace ui
