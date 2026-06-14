// PropertyManager implementation (C17).
//
// Draws the active feature form in the FeatureManager dock slot, routes viewport picks into the
// active selector well, debounces live previews through doc_apply_ops (add or edit_feature), and
// commits (OK) / rolls back (Cancel via worker undo of each preview batch).
#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

#include "imgui.h"

#include "property_manager.h"
#include "icons.h"
#include "sketch_mode.h"   // C38 (P1f): SketchMode::active() — sketch owns Esc while editing

#include "../core/app_state.h"
#include "../core/doc_mirror.h"
#include "../render/id_pass.h"

namespace ui {

// C37-F: Debounce window for live preview (was 0.40 s; reduced for snappier feel).
static const double kDebounceSec = 0.25;

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
static bool fieldEnabled(const PMFieldDef& def, const std::vector<PMField>& fields) {
    if (def.comingSoon) return false;
    if (!def.enableKey)  return true;
    std::string cur = comboValue(fields, def.enableKey);
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
        if (dimTokenize(s, angle, probe, unitCount, endsWithUnit, perr) &&
            unitCount == 1 && endsWithUnit) {
            for (const auto& u : kDimUnits) {
                size_t n = std::strlen(u.name);
                if (u.angle == angle && s.size() > n &&
                    s.compare(s.size() - n, n, u.name) == 0) {
                    wholeScale = u.factor;
                    s.erase(s.size() - n);
                    break;
                }
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

std::string pmFormatDim(double mm, bool ips) {
    char buf[48];
    if (ips) std::snprintf(buf, sizeof(buf), "%.4gin", mm / 25.4);
    else     std::snprintf(buf, sizeof(buf), "%.4gmm", mm);
    return buf;
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

static void initField(PMField& f, const PMFieldDef& d, bool ips) {
    f.def = &d;
    switch (d.kind) {
        case PMFieldKind::Dim: {
            f.dimValue = d.dimDefault;
            f.dimExpr.clear();
            std::string disp = pmFormatDim(d.dimDefault, ips);
            std::strncpy(f.dimBuf.data(), disp.c_str(), f.dimBuf.size() - 1);
            break;
        }
        case PMFieldKind::Combo:   f.comboIndex = 0; break;
        case PMFieldKind::DatumRef: f.comboIndex = 0; break;
        case PMFieldKind::Check:   f.checkValue = d.checkDefault; break;
        case PMFieldKind::Int:     f.intValue = (int)(d.dimDefault + 0.5); break;
        case PMFieldKind::SelectorWell:
        case PMFieldKind::FeatureWell: f.picks.clear(); f.wellActive = false; break;
    }
}

bool PropertyManager::openForCommand(AppState& st, const std::string& cmdId) {
    std::string mode;
    const PMFormDef* form = pmFormForCommand(cmdId, mode);
    if (!form) return false;
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
    lastEditTime_ = ImGui::GetTime();
    previewError_.clear();
    focusedOnOpen_ = false;
    wantFocusFirstField_ = true;  // C37-F: focus first field on next draw

    // C37-F: Take a checkpoint BEFORE any preview, so Cancel can revert with a single call.
    checkpointId_ = st.docMirror.hasDoc() ? st.docMirror.checkpoint(st.worker) : std::string();
    if (checkpointId_.empty())
        st.addLog("[pm] checkpoint failed — Cancel will use undo loop fallback");

    ips_ = st.ips();   // document-unit display follows the active unit system (MMGS / IPS)
    fields_.clear();
    fields_.resize(form->fields.size());
    for (size_t i = 0; i < form->fields.size(); ++i)
        initField(fields_[i], form->fields[i], ips_);

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

    st.addLog("[pm] open " + std::string(form_->type) +
              (mode_.empty() ? "" : " (" + mode_ + ")"));
    return true;
}

bool PropertyManager::openForEdit(AppState& st, const std::string& featureId) {
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
        st.addLog("[pm] checkpoint failed — Cancel will use undo loop fallback");

    ips_ = st.ips();   // document-unit display follows the active unit system (MMGS / IPS)
    fields_.clear();
    fields_.resize(form->fields.size());
    for (size_t i = 0; i < form->fields.size(); ++i)
        initField(fields_[i], form->fields[i], ips_);

    loadFromTree(st, *target);
    st.addLog("[pm] edit " + featureId + " (" + type + ")");
    return true;
}

void PropertyManager::loadFromTree(AppState& st, const nlohmann::json& feature) {
    (void)st;
    nlohmann::json params = feature.value("params", nlohmann::json::object());
    nlohmann::json refs   = feature.value("refs",   nlohmann::json::object());
    for (auto& f : fields_) {
        const char* key = f.def->key;
        switch (f.def->kind) {
            case PMFieldKind::Dim: {
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
                        std::string disp = pmFormatDim(f.dimValue, ips_);
                        std::strncpy(f.dimBuf.data(), disp.c_str(), f.dimBuf.size() - 1);
                    }
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
        if (!fieldEnabled(*f.def, fields_)) continue;   // skip greyed / coming-soon dims
        PMCalloutItem it;
        it.label = f.def->label;
        it.value = f.dimExpr.empty() ? pmFormatDim(f.dimValue, ips_)
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
}

// ---- selector wells -------------------------------------------------------

PMField* PropertyManager::activeWell() {
    for (auto& f : fields_)
        if (f.def->kind == PMFieldKind::SelectorWell && f.wellActive) return &f;
    return nullptr;
}

bool PropertyManager::routePick(const nlohmann::json& selector, int pickKind) {
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
    well->picks.push_back(e);
    well->wellDangling = false;
    dirty_ = true; lastEditTime_ = ImGui::GetTime();
    return true;
}

bool PropertyManager::activeWellWantsBody() const {
    for (const auto& f : fields_)
        if (f.def->kind == PMFieldKind::SelectorWell && f.wellActive)
            return f.def->selKinds.body;
    return false;
}

std::string PropertyManager::activeWellKindHint() const {
    for (const auto& f : fields_) {
        if (f.def->kind != PMFieldKind::SelectorWell || !f.wellActive) continue;
        const PMSelKinds& k = f.def->selKinds;
        std::vector<const char*> parts;
        if (k.face)   parts.push_back("faces");
        if (k.edge)   parts.push_back("edges");
        if (k.vertex) parts.push_back("vertices");
        if (k.body)   parts.push_back("bodies");
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
    dirty_ = true; lastEditTime_ = ImGui::GetTime();
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
                    nlohmann::json arr = nlohmann::json::array();
                    for (const auto& p : f.picks) arr.push_back(p.selector);
                    refs[key] = arr;
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
    feature["params"] = params;
    if (!refs.empty()) feature["refs"] = refs;
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

    // Loft needs at least TWO section profiles before a preview is meaningful.
    if (t == "loft") {
        for (const auto& f : fields)
            if ((f.def->kind == PMFieldKind::SelectorWell) && std::strcmp(f.def->key, "sections") == 0)
                return f.picks.size() >= 2;
        return false;
    }

    // Selector-gated forms: a primary well (SelectorWell or FeatureWell) must have at least one
    // pick. Patterns gate on their FeatureWell; sweep on profile (path errors surface inline);
    // hole on the face; draft on the faces to taper.
    const char* needWell = nullptr;
    if (t == "fillet" || t == "chamfer") needWell = "edges";
    else if (t == "extrude" || t == "revolve" || t == "sweep") needWell = "profile";
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
    if (needWell) {
        for (const auto& f : fields)
            if ((f.def->kind == PMFieldKind::SelectorWell || f.def->kind == PMFieldKind::FeatureWell)
                && std::strcmp(f.def->key, needWell) == 0)
                return !f.picks.empty();
        return false;  // well not found
    }

    // Primitive forms (box/cylinder): at least one enabled Dim must be >0 and error-free.
    // shell: any non-zero thickness is enough. Revolve with no wells handled above.
    for (const auto& f : fields) {
        if (f.def->kind != PMFieldKind::Dim) continue;
        if (f.def->comingSoon) continue;
        if (!f.dimError.empty()) return false;   // invalid input — don't fire
        if (f.dimValue > 0.0) return true;
    }
    return false;
}

// ---- preview / commit / cancel --------------------------------------------

bool PropertyManager::applyPreview(AppState& st) {
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

    nlohmann::json ops = nlohmann::json::array();
    if (!firstPreviewDone_ && targetId_.empty()) {
        // First preview: add the feature.
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

    auto resp = st.docMirror.applyOps(st.worker, ops, previewUndoGroupStarted_);
    if (!resp.ok) {
        // C38 (P4f): the inline previewError_ block in the PM body is the surface for a failed
        // debounced preview — do NOT toast here. Previews fire per-keystroke, so toasting each
        // failure spams the user with transient parse/validation errors mid-edit. Toasts stay on the
        // commit/cancel paths (terminal, user-initiated). Still logged for observability.
        previewError_ = resp.error_message;
        st.addLog("[pm] preview failed: " + resp.error_message);
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
        st.loadDocMesh(resp.aicm_path, lerr);
        if (!lerr.empty()) st.addLog("[pm] mesh: " + lerr);
    }
    // Reflect any dangling ref back into the wells (red tint).
    if (!targetId_.empty() && resp.feature_status.is_array()) {
        for (const auto& s : resp.feature_status) {
            if (!s.is_object() || s.value("id", std::string()) != targetId_) continue;
            auto dr = s.find("dangling_refs");
            std::vector<std::string> dangling;
            if (dr != s.end() && dr->is_array())
                for (const auto& d : *dr) if (d.is_string()) dangling.push_back(d.get<std::string>());
            for (auto& f : fields_) {
                if (f.def->kind != PMFieldKind::SelectorWell) continue;
                f.wellDangling = false;
                for (const auto& dref : dangling)
                    if (dref.rfind(f.def->key, 0) == 0) f.wellDangling = true;
            }
        }
    }
    dirty_ = false;
    return true;
}

void PropertyManager::tick(AppState& st) {
    if (!active_ || !dirty_) return;
    double now = ImGui::GetTime();
    if (now - lastEditTime_ < kDebounceSec) return;
    applyPreview(st);
    // applyPreview may have called close() if the feature vanished; check active_ again.
    (void)active_;
}

void PropertyManager::commit(AppState& st) {
    // Make sure the latest field state is reflected before committing.
    if (!readyForPreview(form_, fields_)) {
        previewError_ = "Select the required references and enter valid dimensions before accepting.";
        st.addLog("[pm] OK blocked: form is not ready");
        return;
    }
    if (dirty_ || !firstPreviewDone_) {
        if (!applyPreview(st)) {
            st.addLog("[pm] OK blocked: preview/apply failed");
            return;
        }
    }
    if (!previewError_.empty()) {
        st.addLog("[pm] OK blocked: " + previewError_);
        return;
    }
    st.addLog("[pm] OK -> committed " + (targetId_.empty() ? std::string("(no geometry)") : targetId_));
    // The feature stays in the document; the tree returns on close.
    close();
}

bool PropertyManager::cancelEdits(AppState& st) {
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

static void pushTextColor(const float c[4]) {
    ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(c[0], c[1], c[2], c[3]));
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

void PropertyManager::draw(AppState& st) {
    if (!active_) return;

    // Live unit-system toggle: if the document units changed while this form is open (e.g. the
    // user clicked the status-bar MMGS/IPS cluster), reformat every open Dim field's DISPLAY from
    // its canonical mm value. The stored value is unchanged — purely a display reformat.
    if (st.ips() != ips_) { ips_ = st.ips(); reformatDimDisplays(); }

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
        if (!ImGui::Begin("##PMFloating", nullptr,
                          ImGuiWindowFlags_NoCollapse | ImGuiWindowFlags_NoNav)) {
            ImGui::End(); return;
        }
    }

    drawBody(st);
    ImGui::End();
}

void PropertyManager::drawEmbedded(AppState& st) {
    if (!active_) return;
    if (st.ips() != ips_) { ips_ = st.ips(); reformatDimDisplays(); }
    drawBody(st);
}

void PropertyManager::drawBody(AppState& st) {
    const auto& pal = ui::palette;

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

    // ---- header: feature icon + title + description -----------------------
    {
        IconUV ic = getIcon(form_->icon, 20);
        if (ic.valid) {
            ImGui::Image(ic.texId, ImVec2(18.0f * ui::uiScale, 18.0f * ui::uiScale),
                         ic.uv0, ic.uv1);
            ImGui::SameLine(0, 6);
        }
        pushTextColor(pal.text);
        ImGui::TextUnformatted(title_.c_str());
        ImGui::PopStyleColor();

        // C37-F: One-line command description beneath the title.
        const char* desc = formDescription(form_->type);
        if (desc && desc[0]) {
            pushTextColor(pal.textDim);
            float wrapW = ImGui::GetContentRegionAvail().x;
            ImGui::PushTextWrapPos(ImGui::GetCursorPosX() + wrapW);
            ImGui::TextUnformatted(desc);
            ImGui::PopTextWrapPos();
            ImGui::PopStyleColor();
        }

        // C37-F: OK / Cancel buttons scaled by uiScale, with tooltips and green/red iconography.
        float btnW = 28.0f * ui::uiScale;
        float pairW = btnW * 2 + 4 * ui::uiScale;
        float rightX = ImGui::GetWindowContentRegionMax().x - pairW;
        float afterTitleX = ImGui::GetCursorPosX();
        if (rightX > afterTitleX + 6) {
            ImGui::SameLine(0, 0);
            ImGui::SetCursorPosX(rightX);
        } else {
            ImGui::SetCursorPosX(rightX < 0 ? 0 : rightX);
        }

        // OK button — green accent.
        ImGui::PushStyleColor(ImGuiCol_Button,
            ImVec4(pal.accentDim[0], pal.accentDim[1], pal.accentDim[2], 1));
        ImGui::PushStyleColor(ImGuiCol_ButtonHovered,
            ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 1));
        {
            // Atlas glyph is "ok_check" / "cancel_x" (NOT "ok"/"cancel"); the text fallback uses
            // ASCII because the loaded fonts don't cover U+2713/U+2717 (✓/✗ render as a notdef box).
            IconUV okIc = getIcon("ok_check", 20);
            bool btnOk;
            if (okIc.valid) {
                btnOk = ImGui::ImageButton("##pm_ok",
                    okIc.texId, ImVec2(btnW - 4, btnW - 4), okIc.uv0, okIc.uv1);
            } else {
                btnOk = ImGui::Button("OK##pm", ImVec2(btnW, 0));
            }
            ok = btnOk;
        }
        if (ImGui::IsItemHovered()) ImGui::SetTooltip("OK (Enter)");
        ImGui::PopStyleColor(2);

        ImGui::SameLine(0, 4 * ui::uiScale);

        // Cancel button — red/muted.
        ImGui::PushStyleColor(ImGuiCol_Button,
            ImVec4(pal.button[0], pal.button[1], pal.button[2], 1));
        ImGui::PushStyleColor(ImGuiCol_ButtonHovered,
            ImVec4(pal.statusErr[0], pal.statusErr[1], pal.statusErr[2], 0.7f));
        {
            IconUV cancelIc = getIcon("cancel_x", 20);
            bool btnCancel;
            if (cancelIc.valid) {
                btnCancel = ImGui::ImageButton("##pm_cancel",
                    cancelIc.texId, ImVec2(btnW - 4, btnW - 4), cancelIc.uv0, cancelIc.uv1);
            } else {
                btnCancel = ImGui::Button("X##pm", ImVec2(btnW, 0));
            }
            cancel = cancel || btnCancel;
        }
        if (ImGui::IsItemHovered()) ImGui::SetTooltip("Cancel (Esc)");
        ImGui::PopStyleColor(2);

        // C37-F: Enter commits ONLY when the PM window is focused/hovered or a PM widget was
        // active this frame. This prevents Enter-in-chat from accidentally committing the form.
        if (ImGui::IsKeyPressed(ImGuiKey_Enter, false) &&
            (pmWindowFocused || pmWindowHovered || pmWidgetWasActive)) {
            ok = true;
        }

        ImGui::Separator();

        if (ok)     { commit(st);      return; }
        if (cancel) { cancelEdits(st); return; }
    }

    // ---- field rows grouped by `group`, collapsible ------------------------
    const char* curGroup = nullptr;
    bool groupOpen = true;
    const bool useIps = ips_;   // document-unit display (set at open; re-synced above on toggle)

    // C37-F: track which Dim field is first/editable for SetKeyboardFocusHere.
    bool firstEditableDimSeen = false;

    for (size_t i = 0; i < fields_.size(); ++i) {
        PMField& f = fields_[i];
        // New group header?
        if (!curGroup || std::strcmp(curGroup, f.def->group) != 0) {
            curGroup = f.def->group;
            groupOpen = ImGui::CollapsingHeader(curGroup, ImGuiTreeNodeFlags_DefaultOpen);
        }
        if (!groupOpen) continue;

        // Conditional enable: grey-out when the sibling Combo condition isn't met or comingSoon.
        bool enabled = fieldEnabled(*f.def, fields_);
        if (!enabled) ImGui::BeginDisabled();

        ImGui::PushID((int)i);
        switch (f.def->kind) {
            case PMFieldKind::Dim: {
                pushTextColor(pal.textDim);
                // C37-F: unit suffix in label ("Radius [mm]"). A field may override it (angle =
                // "[deg]"; an empty override = no suffix).
                const char* unitSuffix = f.def->unitLabel ? f.def->unitLabel
                                                          : (useIps ? " [in]" : " [mm]");
                ImGui::Text("%s%s", f.def->label, unitSuffix);
                ImGui::PopStyleColor();
                if (f.def->comingSoon && ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled))
                    ImGui::SetTooltip("Not yet supported by engine");
                // Reserve room for the - / + step buttons so the InputText doesn't shove them past
                // the pane's right edge (was SetNextItemWidth(-1), which let the steppers overflow
                // / clip — the most visible "misplaced element" on the Extrude form).
                {
                    const ImGuiStyle& dimStl = ImGui::GetStyle();
                    float stepBtnW = ImGui::CalcTextSize("+").x + dimStl.FramePadding.x * 2.0f;
                    ImGui::SetNextItemWidth(-(stepBtnW * 2.0f + 9.0f * ui::uiScale));
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
                ImGui::SameLine(0, 3 * ui::uiScale);
                ImGui::PushStyleColor(ImGuiCol_Button,
                    ImVec4(pal.button[0], pal.button[1], pal.button[2], 1));
                if (ImGui::SmallButton("-##step")) {
                    if (enabled && !f.def->comingSoon) {
                        f.dimValue = std::max(0.0, f.dimValue - 1.0);
                        std::string disp = pmFormatDim(f.dimValue, useIps);
                        std::strncpy(f.dimBuf.data(), disp.c_str(), f.dimBuf.size() - 1);
                        f.dimError.clear();
                        dirty_ = true;
                        lastEditTime_ = ImGui::GetTime() - kDebounceSec;  // fire immediately
                    }
                }
                ImGui::SameLine(0, 2 * ui::uiScale);
                if (ImGui::SmallButton("+##step")) {
                    if (enabled && !f.def->comingSoon) {
                        f.dimValue += 1.0;
                        std::string disp = pmFormatDim(f.dimValue, useIps);
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
                        std::string disp = pmFormatDim(f.dimValue, useIps);
                        std::strncpy(f.dimBuf.data(), disp.c_str(), f.dimBuf.size() - 1);
                        f.dimError.clear();
                        dirty_ = true;
                        lastEditTime_ = ImGui::GetTime() - kDebounceSec;  // fire immediately
                    }
                }

                if (enabled && (changed || deact)) {
                    std::string txt = f.dimBuf.data();
                    // trim
                    while (!txt.empty() && std::isspace((unsigned char)txt.front())) txt.erase(txt.begin());
                    while (!txt.empty() && std::isspace((unsigned char)txt.back())) txt.pop_back();
                    if (!txt.empty() && txt[0] == '=') {
                        f.dimExpr = txt.substr(1);
                        f.dimError.clear();
                        dirty_ = true; lastEditTime_ = ImGui::GetTime();
                    } else {
                        double mm;
                        std::string err;
                        // A bare (unit-less) number follows the document length unit: mm in MMGS,
                        // inch in IPS. Angle fields ([deg]) stay degrees in both systems, so they
                        // keep the unit-less default scale. (angle stays false as before so an
                        // explicit length suffix on any field still parses — existing behavior.)
                        const bool isAngle = f.def->unitLabel && std::strstr(f.def->unitLabel, "deg");
                        const double bareScale = (useIps && !isAngle) ? 25.4 : 1.0;
                        if (pmParseDim(txt, mm, err, /*angle=*/false, bareScale)) {
                            f.dimValue = mm; f.dimExpr.clear(); f.dimError.clear();
                            dirty_ = true; lastEditTime_ = ImGui::GetTime();
                        } else {
                            f.dimError = err;
                        }
                    }
                }
                // expr resolved-value + tooltip
                if (enabled && !f.dimExpr.empty()) {
                    pushTextColor(pal.accent);
                    ImGui::Text(u8"= %s  (%s)", f.dimExpr.c_str(),
                                pmFormatDim(f.dimValue, useIps).c_str());
                    ImGui::PopStyleColor();
                    if (ImGui::IsItemHovered())
                        ImGui::SetTooltip("Bound to equation '%s'", f.dimExpr.c_str());
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
                // Label + activate toggle.
                pushTextColor(f.wellActive ? pal.accent : pal.textDim);
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

                ImVec4 border = f.wellDangling
                    ? ImVec4(pal.statusErr[0], pal.statusErr[1], pal.statusErr[2], 1)
                    : (f.wellActive
                        ? ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 1)
                        : ImVec4(pal.border[0], pal.border[1], pal.border[2], pal.border[3]));
                // C37-F: thicker border on active wells for clearer active-state visual.
                ImGui::PushStyleVar(ImGuiStyleVar_FrameBorderSize, f.wellActive ? 2.0f : 1.0f);
                ImGui::PushStyleColor(ImGuiCol_Border, border);
                ImGui::PushStyleColor(ImGuiCol_Button,
                    f.wellActive ? ImVec4(pal.accentSoft[0], pal.accentSoft[1],
                                          pal.accentSoft[2], pal.accentSoft[3])
                                 : ImVec4(pal.frameBg[0], pal.frameBg[1], pal.frameBg[2], 1));
                if (ImGui::Button((chip + "##well").c_str(), ImVec2(-1, 0))) {
                    // Activate this well; deactivate others.
                    for (auto& o : fields_)
                        if (o.def->kind == PMFieldKind::SelectorWell) o.wellActive = false;
                    f.wellActive = true;
                }
                ImGui::PopStyleColor(2);
                ImGui::PopStyleVar();

                // C37-F: tooltip on the well button.
                if (ImGui::IsItemHovered()) {
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
                    ImGui::PushStyleColor(ImGuiCol_Text,
                        ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 0.75f));
                    ImGui::TextUnformatted("Pick faces/edges in the viewport");
                    ImGui::PopStyleColor();
                }
                if (f.wellDangling) {
                    pushTextColor(pal.statusErr);
                    ImGui::TextUnformatted("! dangling reference");
                    ImGui::PopStyleColor();
                }
                break;
            }
            case PMFieldKind::Combo: {
                pushTextColor(pal.textDim);
                ImGui::TextUnformatted(f.def->label);
                ImGui::PopStyleColor();
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
                            if (enabled) { f.comboIndex = n; dirty_ = true; lastEditTime_ = ImGui::GetTime(); }
                        }
                        if (sel) ImGui::SetItemDefaultFocus();
                    }
                    ImGui::EndCombo();
                }
                break;
            }
            case PMFieldKind::Check: {
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
            case PMFieldKind::Int: {
                pushTextColor(pal.textDim);
                ImGui::TextUnformatted(f.def->label);
                ImGui::PopStyleColor();
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
            case PMFieldKind::DatumRef: {
                pushTextColor(pal.textDim);
                ImGui::TextUnformatted(f.def->label);
                ImGui::PopStyleColor();
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
            case PMFieldKind::FeatureWell: {
                pushTextColor(pal.textDim);
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
        pushTextColor(pal.statusErr);
        float wrapW = ImGui::GetContentRegionAvail().x;
        ImGui::PushTextWrapPos(ImGui::GetCursorPosX() + wrapW);
        ImGui::Text(u8"! Preview error: %s", previewError_.c_str());
        ImGui::PopTextWrapPos();
        ImGui::PopStyleColor();
    }

    // ---- live-preview status hint -----------------------------------------
    ImGui::Separator();
    pushTextColor(pal.textDim);
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

}  // namespace ui
