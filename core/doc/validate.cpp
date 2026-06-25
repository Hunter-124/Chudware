// Structural JSON validator for aicad.part/1 — faithful C++ port of the PART path of
// engine/doc/schema.py. See validate.h for the contract. Everything except
// validate_document lives in this anonymous namespace.
//
// NON-EVALUATING expression grammar: validate_expr here is purely structural (mirrors
// equations.validate_expr). "1/0" passes (valid grammar); "foo+1" passes (unknown
// names allowed at grammar time). Only these fail: empty/whitespace, syntax error, an
// operator outside {+,-,*,/,**, unary +/-}, a non-numeric constant, a call to a
// function not in _FUNCS, keyword arguments, or any other construct. '^' -> '**'.
#include "doc/validate.h"

#include <cctype>
#include <map>
#include <regex>
#include <set>
#include <string>
#include <vector>

namespace aicad::doc {
namespace {

using nlohmann::json;
using std::string;
using std::vector;

// --------------------------------------------------------------------------- tables

const std::set<string> RESERVED_DATUMS = {
    "origin", "plane.front", "plane.top", "plane.right",
    "axis.x", "axis.y", "axis.z",
};

const std::set<string> UNIT_SYSTEMS = {"MMGS", "IPS"};

const std::set<string> SELECTOR_KINDS = {"face", "edge", "vertex", "body", "datum", "region"};
const std::set<string> FACE_SURFS = {"plane", "cylinder", "cone", "sphere", "torus", "bspline", "other"};
const std::set<string> EDGE_SURFS = {"line", "circle", "ellipse", "bspline", "other"};

const std::regex HANDLE_RE(R"(^e\d+(\.(p1|p2|mid|p|center|start|end|vertex|apex|p\d+))?$)");
const std::regex FEATURE_ID_RE(R"(^f\d+$)");
const std::regex ENTITY_ID_RE(R"(^e\d+$)");
const std::regex CONSTRAINT_ID_RE(R"(^c\d+$)");
const std::regex P_SUB_RE(R"(^p\d+$)");
const std::regex IDENT_RE(R"(^[A-Za-z_]\w*$)");

// equations._FUNCS keys (the whitelisted document-equation functions).
const std::set<string> EQ_FUNCS = {
    "sin", "cos", "tan", "asin", "acos", "atan",
    "sqrt", "abs", "min", "max", "floor", "ceil",
};
// equations._CONSTS keys.
const std::set<string> EQ_CONSTS = {"pi"};

// schema.EQ_CURVE_FUNCS / EQ_CURVE_CONSTS (the eq-curve grammar).
const std::set<string> EQ_CURVE_FUNCS = {
    "sin", "cos", "tan", "asin", "acos", "atan", "atan2",
    "sqrt", "exp", "log", "abs", "sinh", "cosh", "tanh", "pow",
};
const std::set<string> EQ_CURVE_CONSTS = {"pi", "e"};

// Entity type -> required fields beyond {id, type, construction}.
const std::map<string, vector<string>> ENTITY_FIELDS = {
    {"point", {"p"}},
    {"line", {"p1", "p2"}},
    {"circle", {"center", "radius"}},
    {"arc", {"center", "radius", "start", "end", "ccw"}},
    {"ellipse", {"center", "rx", "ry", "rot"}},
    {"ellipse_arc", {"center", "rx", "ry", "rot", "a0", "a1"}},
    {"parabola", {"vertex", "focus", "t0", "t1"}},
    {"conic", {"p0", "p1", "apex", "rho"}},
    {"spline", {"points"}},
    {"style_spline", {"points"}},
    {"slot", {"p1", "p2", "width"}},
    {"arc_slot", {"center", "radius", "a0", "a1", "width"}},
    {"text", {"p", "text", "height"}},
    {"eq_curve", {"xt", "yt", "t1", "t2"}},
};

// Entity type -> point sub-handles it exposes.
const std::map<string, std::set<string>> ENTITY_SUBS = {
    {"point", {"p"}},
    {"line", {"p1", "p2", "mid"}},
    {"circle", {"center"}},
    {"arc", {"center", "start", "end"}},
    {"ellipse", {"center"}},
    {"ellipse_arc", {"center", "start", "end"}},
    {"parabola", {"vertex", "start", "end"}},
    {"conic", {"start", "end", "apex"}},
    {"slot", {"p1", "p2"}},
    {"arc_slot", {"center", "start", "end"}},
    {"text", {"p"}},
    {"eq_curve", {"start", "end"}},
    // spline / style_spline handled specially.
};

// Constraint type -> (min_refs, max_refs, needs_value).
struct ConstraintSpec { int lo; int hi; bool needs_value; };
const std::map<string, ConstraintSpec> CONSTRAINT_SPECS = {
    {"coincident", {2, 2, false}},
    {"horizontal", {1, 2, false}},
    {"vertical", {1, 2, false}},
    {"parallel", {2, 2, false}},
    {"perpendicular", {2, 2, false}},
    {"tangent", {2, 2, false}},
    {"collinear", {2, 2, false}},
    {"equal", {2, 2, false}},
    {"concentric", {2, 2, false}},
    {"coradial", {2, 2, false}},
    {"midpoint", {2, 2, false}},
    {"symmetric", {3, 3, false}},
    {"fix", {1, 1, false}},
    {"point_on", {2, 2, false}},
    {"distance", {2, 2, true}},
    {"angle", {2, 2, true}},
    {"radius", {1, 1, true}},
    {"diameter", {1, 1, true}},
};

// Ref-slot value forms.
enum class RefForm { Sel, SelOrDatum, SelList, FeatList, CompList, UvList, EntityIdList };

struct RefSlot { string name; RefForm form; bool required; };
struct FeatureSpec { vector<string> params; vector<RefSlot> refs; };

const std::map<string, FeatureSpec> FEATURE_TYPES = {
    {"plane", {{"method"}, {{"base", RefForm::SelOrDatum, false},
                            {"second", RefForm::SelOrDatum, false},
                            {"points", RefForm::SelList, false}}}},
    {"axis", {{"method"}, {{"a", RefForm::SelOrDatum, false},
                           {"b", RefForm::SelOrDatum, false}}}},
    {"point", {{"method"}, {{"target", RefForm::SelOrDatum, false}}}},
    {"csys", {{}, {{"origin", RefForm::SelOrDatum, false},
                   {"x_dir", RefForm::SelOrDatum, false},
                   {"y_dir", RefForm::SelOrDatum, false}}}},
    {"sketch", {{"entities", "constraints"}, {{"plane", RefForm::SelOrDatum, true},
                                              {"offset", RefForm::SelOrDatum, false}}}},
    {"box", {{"length", "width", "height"}, {{"at", RefForm::SelOrDatum, false}}}},
    {"cylinder", {{"radius", "height"}, {{"at", RefForm::SelOrDatum, false}}}},
    {"sphere", {{"radius"}, {{"at", RefForm::SelOrDatum, false}}}},
    {"cone", {{"r1", "r2", "height"}, {{"at", RefForm::SelOrDatum, false}}}},
    {"torus", {{"major_r", "minor_r"}, {{"at", RefForm::SelOrDatum, false}}}},
    {"extrude", {{"mode"}, {{"profile", RefForm::SelList, true},
                            {"up_to", RefForm::SelOrDatum, false},
                            {"up_to2", RefForm::SelOrDatum, false},
                            {"start_ref", RefForm::SelOrDatum, false},
                            {"end_ref", RefForm::SelOrDatum, false},
                            {"end2_ref", RefForm::SelOrDatum, false}}}},
    {"revolve", {{"mode", "angle"}, {{"profile", RefForm::SelList, true},
                                     {"axis", RefForm::SelOrDatum, true}}}},
    {"sweep", {{"mode"}, {{"profile", RefForm::SelList, true},
                          {"path", RefForm::SelOrDatum, true},
                          {"guides", RefForm::SelList, false}}}},
    {"loft", {{"mode"}, {{"sections", RefForm::SelList, true},
                         {"centerline", RefForm::SelOrDatum, false},
                         {"guides", RefForm::SelList, false}}}},
    {"helix", {{"pitch", "height"}, {{"base", RefForm::SelOrDatum, true}}}},
    // thread: cut a real helical ISO-60° thread into a cylindrical face. `length` (the
    // thread run, mm) is always required; the SIZE comes from a `size` ("M8") param OR a
    // bare major_dia + pitch (the compiler enforces that either/or, so neither is listed
    // as a universally-required param). target_face = the cylindrical face to thread.
    {"thread", {{"length"}, {{"target_face", RefForm::Sel, true}}}},
    // text: emboss/engrave a string onto a planar face. `text` (the string) is always
    // required; height/depth/mode/font/align are optional (compiler defaults). target_face
    // = the planar face to mark.
    {"text", {{"text"}, {{"target_face", RefForm::Sel, true}}}},
    {"fillet", {{}, {{"edges", RefForm::SelList, false},
                     {"middle_face", RefForm::SelOrDatum, false},
                     {"face_set_a", RefForm::SelList, false},
                     {"face_set_b", RefForm::SelList, false}}}},
    {"chamfer", {{"length"}, {{"edges", RefForm::SelList, false},
                              {"vertices", RefForm::SelList, false},
                              {"reference_face", RefForm::SelOrDatum, false}}}},
    {"shell", {{"thickness"}, {{"remove", RefForm::SelList, false}}}},
    {"draft", {{"angle"}, {{"neutral", RefForm::SelOrDatum, true},
                           {"pull", RefForm::SelOrDatum, false},
                           {"faces", RefForm::SelList, true},
                           {"parting", RefForm::SelList, false}}}},
    {"hole", {{"style", "end"}, {{"face", RefForm::SelOrDatum, false},
                                 {"positions", RefForm::SelList, false},
                                 {"positions_sketch", RefForm::SelOrDatum, false},
                                 {"points", RefForm::UvList, false}}}},
    {"bolt_circle", {{"diameter", "count", "pcd"}, {{"face", RefForm::SelOrDatum, false}}}},
    {"linear_pattern", {{"count1", "spacing1"}, {{"dir1", RefForm::SelOrDatum, true},
                                                 {"dir2", RefForm::SelOrDatum, false},
                                                 {"features", RefForm::FeatList, true}}}},
    {"circular_pattern", {{"count", "angle"}, {{"axis", RefForm::SelOrDatum, true},
                                               {"features", RefForm::FeatList, true}}}},
    {"mirror", {{}, {{"plane", RefForm::SelOrDatum, true},
                     {"features", RefForm::FeatList, false},
                     {"bodies", RefForm::SelList, false}}}},
    // transform: move/rotate a body. No required params (translate and/or angle+rotate), no ref
    // slots — the optional target `body` is a top-level feature id read by the compiler, not a ref.
    {"transform", {{}, {}}},
    {"boolean", {{"op"}, {{"targets", RefForm::SelList, true},
                          {"tools", RefForm::SelList, false}}}},
    {"sm_base_flange", {{}, {{"profile", RefForm::SelList, true}}}},
    {"sm_edge_flange", {{}, {{"edge", RefForm::SelOrDatum, true}}}},
    {"sm_hem", {{}, {{"edge", RefForm::SelOrDatum, true}}}},
    {"sm_jog", {{}, {{"edge", RefForm::SelOrDatum, true}}}},
    {"sm_flat_pattern", {{}, {}}},
    {"weld_member", {{"profile"}, {{"sketch", RefForm::SelOrDatum, true},
                                   {"segments", RefForm::EntityIdList, false}}}},
    {"weld_gusset", {{}, {{"faces", RefForm::SelList, true}}}},
    {"weld_end_cap", {{}, {{"face", RefForm::SelOrDatum, true}}}},
    {"script", {{"code"}, {}}},
    {"import", {{"source_path", "format"}, {}}},
    {"rib", {{}, {{"profile", RefForm::SelList, true}}}},
    {"split", {{}, {{"tool", RefForm::SelOrDatum, true}}}},
    {"asm_linear_pattern", {{"count1", "spacing1"}, {{"dir1", RefForm::SelOrDatum, true},
                                                     {"components", RefForm::CompList, true}}}},
    {"asm_circular_pattern", {{"count", "angle"}, {{"axis", RefForm::SelOrDatum, true},
                                                   {"components", RefForm::CompList, true}}}},
    {"surf_extrude", {{"end", "depth"}, {{"profile", RefForm::SelList, true}}}},
    {"surf_revolve", {{"angle"}, {{"profile", RefForm::SelList, true},
                                  {"axis", RefForm::SelOrDatum, true}}}},
    {"surf_sweep", {{}, {{"profile", RefForm::SelList, true},
                         {"path", RefForm::SelOrDatum, true},
                         {"guides", RefForm::SelList, false}}}},
    {"surf_loft", {{}, {{"sections", RefForm::SelList, true},
                        {"guides", RefForm::SelList, false},
                        {"centerline", RefForm::SelOrDatum, false}}}},
    {"surf_planar", {{}, {{"profile", RefForm::SelList, true}}}},
    {"surf_offset", {{"distance"}, {{"faces", RefForm::SelList, true}}}},
    {"surf_ruled", {{}, {{"edge_a", RefForm::SelOrDatum, true},
                         {"edge_b", RefForm::SelOrDatum, true}}}},
    {"surf_boundary", {{}, {{"dir1", RefForm::SelList, true},
                            {"dir2", RefForm::SelList, false}}}},
    {"surf_fill", {{}, {{"edges", RefForm::SelList, true}}}},
    {"surf_knit", {{}, {{"surfaces", RefForm::SelList, true}}}},
    {"surf_thicken", {{"thickness"}, {{"surface", RefForm::SelOrDatum, true}}}},
    {"surf_trim", {{"keep"}, {{"surface", RefForm::SelOrDatum, true},
                              {"tool", RefForm::SelOrDatum, true}}}},
    {"surf_untrim", {{}, {{"faces", RefForm::SelList, true}}}},
    {"surf_delete_face", {{}, {{"faces", RefForm::SelList, true}}}},
    {"surf_replace_face", {{}, {{"faces", RefForm::SelList, true},
                                {"surface", RefForm::SelOrDatum, true}}}},
};

// --------------------------------------------------------------------------- helpers

// _is_num: a JSON number that is not a bool. nlohmann is_number() is already false for
// booleans, but exclude booleans explicitly to be safe.
bool is_num(const json& v) { return v.is_number() && !v.is_boolean(); }

// _is_vec(v, n): array of length n where every element is_num.
bool is_vec(const json& v, size_t n) {
    if (!v.is_array() || v.size() != n) return false;
    for (const auto& x : v) {
        if (!is_num(x)) return false;
    }
    return true;
}

// Safe .get() emulation: returns a const ref to the member or a static null.
const json& get(const json& obj, const string& key) {
    static const json kNull;
    if (obj.is_object()) {
        auto it = obj.find(key);
        if (it != obj.end()) return *it;
    }
    return kNull;
}

bool has(const json& obj, const string& key) {
    return obj.is_object() && obj.contains(key);
}

bool is_nonempty_string(const json& v) {
    return v.is_string() && !v.get<string>().empty();
}

// --------------------------------------------------------------------------- expr grammar
// Self-contained, NON-EVALUATING structural parser reproducing equations.validate_expr
// and equations.expr_names. It collects Name identifiers and detects grammar errors,
// but never evaluates (no division-by-zero / unknown-name failures). Unknown FUNCTION
// calls and keyword-args ARE grammar errors. '^' is normalized to '**' first.

struct ExprParseError { string msg; };

class ExprParser {
 public:
    // funcs = whitelisted function names; consts/vars allowed-name handling differs per
    // caller, so this generic parser only enforces: structure + that any *called* name
    // is in `funcs`, and (when check_names) that any bare Name is in `allowed_names`.
    // For the document-equation grammar, bare names are unrestricted (resolved later),
    // so check_names is false and we just collect them. For eq-curve, check_names is
    // true and a non-allowed bare name is an error.
    ExprParser(const string& src, const std::set<string>& funcs,
               bool check_names, const std::set<string>& allowed_names,
               const string& bad_name_msg)
        : s_(src), funcs_(funcs), check_names_(check_names),
          allowed_names_(allowed_names), bad_name_msg_(bad_name_msg) {}

    // Parse and structurally validate. On success, names collected in names_.
    // Throws ExprParseError on any grammar violation.
    void parse() {
        skip_ws();
        if (at_end()) throw ExprParseError{"empty expression"};
        parse_add();
        skip_ws();
        if (!at_end()) throw ExprParseError{"syntax error"};
    }

    const std::set<string>& names() const { return names_; }

 private:
    string s_;  // owned copy — the ctor arg is often a normalize_caret() temporary
    const std::set<string>& funcs_;
    bool check_names_;
    std::set<string> allowed_names_;  // owned — validate_expr passes a {} temporary
    string bad_name_msg_;
    size_t pos_ = 0;
    std::set<string> names_;

    bool at_end() const { return pos_ >= s_.size(); }
    char peek() const { return pos_ < s_.size() ? s_[pos_] : '\0'; }
    void skip_ws() {
        while (pos_ < s_.size() &&
               std::isspace(static_cast<unsigned char>(s_[pos_]))) {
            ++pos_;
        }
    }

    // add : mul (('+'|'-') mul)*
    void parse_add() {
        parse_mul();
        for (;;) {
            skip_ws();
            if (peek() == '+') { ++pos_; parse_mul(); }
            else if (peek() == '-') { ++pos_; parse_mul(); }
            else break;
        }
    }

    // mul : unary (('*'(but not '**') | '/') unary)*
    void parse_mul() {
        parse_unary();
        for (;;) {
            skip_ws();
            if (peek() == '*' && !(pos_ + 1 < s_.size() && s_[pos_ + 1] == '*')) {
                ++pos_; parse_unary();
            } else if (peek() == '/') {
                ++pos_; parse_unary();
            } else {
                break;
            }
        }
    }

    // unary : ('+'|'-') unary | power.  Python allows chained unary (`+ +x`, `--x`).
    void parse_unary() {
        skip_ws();
        if (peek() == '+' || peek() == '-') {
            ++pos_;
            parse_unary();
            return;
        }
        parse_power();
    }

    // power : atom ('**' unary)?   right-assoc; RHS is unary (binds unary tighter on RHS)
    void parse_power() {
        parse_atom();
        skip_ws();
        if (peek() == '*' && pos_ + 1 < s_.size() && s_[pos_ + 1] == '*') {
            pos_ += 2;
            parse_unary();
        }
    }

    // atom : number | name | call | '(' add ')'
    void parse_atom() {
        skip_ws();
        if (at_end()) throw ExprParseError{"syntax error"};
        char c = peek();
        if (c == '(') {
            ++pos_;
            parse_add();
            skip_ws();
            if (peek() != ')') throw ExprParseError{"syntax error"};
            ++pos_;
            return;
        }
        if (std::isdigit(static_cast<unsigned char>(c)) || c == '.') {
            parse_number();
            return;
        }
        if (std::isalpha(static_cast<unsigned char>(c)) || c == '_') {
            parse_name_or_call();
            return;
        }
        // Any other leading character (operator with no operand, stray symbol) is a
        // grammar error — mirrors Python's SyntaxError / "construct not allowed".
        throw ExprParseError{"syntax error"};
    }

    void parse_number() {
        size_t start = pos_;
        bool seen_dot = false, seen_exp = false, seen_digit = false;
        while (pos_ < s_.size()) {
            char c = s_[pos_];
            if (std::isdigit(static_cast<unsigned char>(c))) { seen_digit = true; ++pos_; }
            else if (c == '.' && !seen_dot && !seen_exp) { seen_dot = true; ++pos_; }
            else if ((c == 'e' || c == 'E') && !seen_exp && pos_ > start && seen_digit) {
                seen_exp = true; ++pos_;
                if (pos_ < s_.size() && (s_[pos_] == '+' || s_[pos_] == '-')) ++pos_;
            } else {
                break;
            }
        }
        if (!seen_digit) throw ExprParseError{"syntax error"};
    }

    void parse_name_or_call() {
        size_t start = pos_;
        while (pos_ < s_.size() &&
               (std::isalnum(static_cast<unsigned char>(s_[pos_])) || s_[pos_] == '_')) {
            ++pos_;
        }
        string name = s_.substr(start, pos_ - start);
        skip_ws();
        if (peek() == '(') {
            // Function call: name must be a whitelisted function.
            if (funcs_.find(name) == funcs_.end()) {
                throw ExprParseError{"unknown function"};
            }
            ++pos_;  // consume '('
            skip_ws();
            if (peek() != ')') {
                parse_call_arg();
                for (;;) {
                    skip_ws();
                    if (peek() == ',') { ++pos_; parse_call_arg(); }
                    else break;
                }
            }
            skip_ws();
            if (peek() != ')') throw ExprParseError{"syntax error"};
            ++pos_;
            // Python's expr_names = {n.id for n in ast.walk(tree) if isinstance(n,
            // ast.Name) ...}. ast.walk DOES visit Call.func (an ast.Name), so the
            // function name (e.g. "sin") IS collected. Collect it here too so our
            // names() matches ast.walk exactly (numbers are skipped by the lexer, so
            // scientific-notation literals like 2e5 never leak an 'e' identifier).
            if (name != "pi") names_.insert(name);
            return;
        }
        // Python 3.8+ parses True/False/None as ast.Constant, not ast.Name -> _check
        // rejects them with "only numbers allowed" (bool/None is not int|float).
        if (name == "True" || name == "False" || name == "None") {
            throw ExprParseError{"only numbers allowed"};
        }
        // Bare name (not a call).
        if (check_names_) {
            if (allowed_names_.find(name) == allowed_names_.end()) {
                throw ExprParseError{bad_name_msg_ + name + "'"};
            }
        }
        if (name != "pi") names_.insert(name);  // _CONSTS excluded, matching expr_names
    }

    // A call argument is a full add-expression. Keyword args (name=...) are detected by
    // a '=' following an atom at top level — Python rejects them as keyword arguments.
    void parse_call_arg() {
        parse_add();
        skip_ws();
        if (peek() == '=' && !(pos_ + 1 < s_.size() && s_[pos_ + 1] == '=')) {
            throw ExprParseError{"keyword arguments not allowed"};
        }
    }
};

// Normalize '^' -> '**'.
string normalize_caret(const string& expr) {
    string out;
    out.reserve(expr.size() + 8);
    for (char c : expr) {
        if (c == '^') out += "**";
        else out += c;
    }
    return out;
}

bool is_blank(const string& s) {
    for (char c : s) {
        if (!std::isspace(static_cast<unsigned char>(c))) return false;
    }
    return true;
}

// equations.validate_expr: returns error string (empty == valid). Non-evaluating.
// Document-equation grammar: bare names unrestricted, funcs = EQ_FUNCS.
string validate_expr(const string& expr) {
    if (is_blank(expr)) return "empty expression";
    ExprParser p(normalize_caret(expr), EQ_FUNCS, /*check_names=*/false, {}, "");
    try {
        p.parse();
    } catch (const ExprParseError& e) {
        return e.msg.empty() ? string("syntax error") : e.msg;
    }
    return "";
}

// equations.expr_names: the set of referenced names, excluding _CONSTS = {pi}.
// Mirrors {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id not in
// _CONSTS} — which INCLUDES function-call names (Call.func is an ast.Name). The parser
// collects both bare names and call names (pi excluded), and crucially skips numeric
// literals so scientific notation like 2e5 does not leak an 'e'. Callers only invoke
// this AFTER validate_expr passed, so re-parsing never throws here; if it somehow does,
// return what we have (matching that EquationError would surface as set() in validate_dim).
std::set<string> expr_names(const string& expr) {
    ExprParser p(normalize_caret(expr), EQ_FUNCS, /*check_names=*/false, {}, "");
    try {
        p.parse();
    } catch (const ExprParseError&) {
        return {};
    }
    return p.names();
}

// schema.validate_eq_curve_expr: error string (empty == valid). Allowed names = var,
// EQ_CURVE_CONSTS, EQ_CURVE_FUNCS; funcs = EQ_CURVE_FUNCS; grammar allows % too.
string validate_eq_curve_expr(const string& expr, const string& var) {
    if (is_blank(expr)) return "empty expression";
    std::set<string> allowed = EQ_CURVE_CONSTS;
    allowed.insert(EQ_CURVE_FUNCS.begin(), EQ_CURVE_FUNCS.end());
    allowed.insert(var);
    // eq-curve grammar additionally allows '%'. We extend by pre-checking via a parser
    // variant that treats '%' like a mul-level binop. Reuse ExprParser but it doesn't
    // know '%'. Implement inline below.

    // Inline recursive-descent for the eq-curve grammar (adds '%').
    struct EqParser {
        const string& s; size_t pos = 0;
        const std::set<string>& funcs;
        const std::set<string>& allowed;
        const string& var;
        bool at_end() const { return pos >= s.size(); }
        char peek() const { return pos < s.size() ? s[pos] : '\0'; }
        void ws() { while (pos < s.size() && std::isspace((unsigned char)s[pos])) ++pos; }
        void parse() {
            ws();
            if (at_end()) throw ExprParseError{"empty expression"};
            add();
            ws();
            if (!at_end()) throw ExprParseError{"syntax error"};
        }
        void add() {
            mul();
            for (;;) {
                ws();
                if (peek() == '+') { ++pos; mul(); }
                else if (peek() == '-') { ++pos; mul(); }
                else break;
            }
        }
        void mul() {
            unary();
            for (;;) {
                ws();
                if (peek() == '*' && !(pos + 1 < s.size() && s[pos + 1] == '*')) { ++pos; unary(); }
                else if (peek() == '/') { ++pos; unary(); }
                else if (peek() == '%') { ++pos; unary(); }
                else break;
            }
        }
        void unary() {
            ws();
            if (peek() == '+') { ++pos; unary(); return; }
            if (peek() == '-') { ++pos; unary(); return; }
            power();
        }
        void power() {
            atom();
            ws();
            if (peek() == '*' && pos + 1 < s.size() && s[pos + 1] == '*') { pos += 2; unary(); }
        }
        void atom() {
            ws();
            if (at_end()) throw ExprParseError{"syntax error"};
            char c = peek();
            if (c == '(') {
                ++pos; add(); ws();
                if (peek() != ')') throw ExprParseError{"syntax error"};
                ++pos; return;
            }
            if (std::isdigit((unsigned char)c) || c == '.') { number(); return; }
            if (std::isalpha((unsigned char)c) || c == '_') { name_or_call(); return; }
            throw ExprParseError{"syntax error"};
        }
        void number() {
            size_t start = pos; bool dot = false, exp = false, dig = false;
            while (pos < s.size()) {
                char c = s[pos];
                if (std::isdigit((unsigned char)c)) { dig = true; ++pos; }
                else if (c == '.' && !dot && !exp) { dot = true; ++pos; }
                else if ((c == 'e' || c == 'E') && !exp && pos > start && dig) {
                    exp = true; ++pos;
                    if (pos < s.size() && (s[pos] == '+' || s[pos] == '-')) ++pos;
                } else break;
            }
            if (!dig) throw ExprParseError{"syntax error"};
        }
        void name_or_call() {
            size_t start = pos;
            while (pos < s.size() && (std::isalnum((unsigned char)s[pos]) || s[pos] == '_')) ++pos;
            string nm = s.substr(start, pos - start);
            ws();
            if (peek() == '(') {
                if (funcs.find(nm) == funcs.end()) throw ExprParseError{"unknown function"};
                ++pos; ws();
                if (peek() != ')') {
                    arg();
                    for (;;) { ws(); if (peek() == ',') { ++pos; arg(); } else break; }
                }
                ws();
                if (peek() != ')') throw ExprParseError{"syntax error"};
                ++pos;
                return;
            }
            // True/False/None are ast.Constant in Python -> "only numbers allowed".
            if (nm == "True" || nm == "False" || nm == "None") {
                throw ExprParseError{"only numbers allowed"};
            }
            // Bare name: must be var, a const, or a func name.
            if (allowed.find(nm) == allowed.end()) {
                throw ExprParseError{"unknown name '" + nm + "' (only '" + var +
                                     "', consts and math functions allowed)"};
            }
        }
        void arg() {
            add();
            ws();
            if (peek() == '=' && !(pos + 1 < s.size() && s[pos + 1] == '=')) {
                throw ExprParseError{"keyword arguments not allowed"};
            }
        }
    };

    string norm = normalize_caret(expr);
    EqParser ep{norm, 0, EQ_CURVE_FUNCS, allowed, var};
    try {
        ep.parse();
    } catch (const ExprParseError& e) {
        return e.msg.empty() ? string("syntax error") : e.msg;
    }
    return "";
}

// --------------------------------------------------------------------------- dim

vector<string> validate_dim(const json& v, const string& where,
                            const std::set<string>& globals_names) {
    vector<string> errs;
    if (!v.is_object()) {
        return {where + ": dimension must be an object {value, expr}"};
    }
    if (!has(v, "value") || !is_num(get(v, "value"))) {
        errs.push_back(where + ".value: missing or non-numeric");
    }
    const json& expr = get(v, "expr");
    if (!expr.is_null()) {
        if (!expr.is_string()) {
            errs.push_back(where + ".expr: must be string or null");
        } else {
            string e = validate_expr(expr.get<string>());
            if (!e.empty()) {
                errs.push_back(where + ".expr: " + e);
            } else {
                std::set<string> nm = expr_names(expr.get<string>());
                std::set<string> unknown;
                for (const auto& n : nm) {
                    if (globals_names.find(n) == globals_names.end()) unknown.insert(n);
                }
                if (!unknown.empty()) {
                    string list = "[";
                    bool first = true;
                    for (const auto& u : unknown) {
                        if (!first) list += ", ";
                        list += "'" + u + "'";
                        first = false;
                    }
                    list += "]";
                    errs.push_back(where + ".expr: unknown name(s) " + list);
                }
            }
        }
    }
    return errs;
}

// --------------------------------------------------------------------------- selectors

string sorted_set_str(const std::set<string>& s) {
    string out = "[";
    bool first = true;
    for (const auto& x : s) {  // std::set iterates sorted
        if (!first) out += ", ";
        out += "'" + x + "'";
        first = false;
    }
    out += "]";
    return out;
}

// A semantic geometry QUERY object ({"of":"edge"|"face", ...}) is accepted anywhere a
// pick selector / selector-list is — it is the agent's way of naming geometry it cannot
// click (doc/picks::resolve_geom_query expands it at compile time).
bool is_query_obj(const json& v) {
    return v.is_object() && (get(v, "kind") == json("query") || v.contains("of"));
}

// An EXPLICIT axis/direction spec: an object carrying a 3-vector `axis` direction.
// Small local models hand us a revolve/pattern axis as raw geometry under an invented
// wrapper (kind:"axis", or an edge selector with a bogus surf) rather than the datum
// ref. The compiler (normalize_feature::axis_from_explicit) accepts this, so validation
// must too — but ONLY in direction slots, where a direction vector is meaningful.
bool is_explicit_axis(const json& v) { return v.is_object() && is_vec(get(v, "axis"), 3); }
const std::set<string> AXIS_DIR_SLOTS = {"axis", "dir1", "dir2", "pull"};

vector<string> validate_geom_query(const json& q, const string& where) {
    const json& ofj = get(q, "of");
    string of = ofj.is_string() ? ofj.get<string>() : "";
    if (of != "edge" && of != "face")
        return {where + ".of: geometry query must be \"edge\" or \"face\""};
    return {};
}

vector<string> validate_selector(const json& sel, const string& where) {
    vector<string> errs;
    if (!sel.is_object()) {
        return {where + ": selector must be an object"};
    }
    const json& kindj = get(sel, "kind");
    string kind = kindj.is_string() ? kindj.get<string>() : "";
    if (!kindj.is_string() || SELECTOR_KINDS.find(kind) == SELECTOR_KINDS.end()) {
        return {where + ".kind: not in " + sorted_set_str(SELECTOR_KINDS)};
    }
    if (kind == "datum") {
        const json& feat = get(sel, "feature");
        bool ok = feat.is_string() &&
                  (std::regex_match(feat.get<string>(), FEATURE_ID_RE) ||
                   RESERVED_DATUMS.find(feat.get<string>()) != RESERVED_DATUMS.end());
        if (!ok) {
            errs.push_back(where + ".feature: datum selector needs a feature id or reserved datum");
        }
        return errs;
    }
    if (kind == "region") {
        if (!get(sel, "sketch").is_string()) {
            errs.push_back(where + ".sketch: region selector needs a sketch feature id");
        }
        const json& index = get(sel, "index");
        if (!index.is_null() && !is_num(index)) {
            errs.push_back(where + ".index: region index must be a number");
        }
        const json& centroid = get(sel, "centroid");
        if (!centroid.is_null()) {
            if (!is_vec(centroid, 2)) {
                errs.push_back(where + ".centroid: region centroid must be [u,v]");
            }
            if (!is_num(get(sel, "hint_index"))) {
                errs.push_back(where + ".hint_index: required when a centroid is given");
            }
        }
        return errs;
    }
    if (kind == "body") {
        if (!get(sel, "owner_feature").is_string()) {
            errs.push_back(where + ".owner_feature: required for body selectors");
        }
        return errs;
    }
    // face / edge / vertex
    if (!is_vec(get(sel, "centroid"), 3)) {
        errs.push_back(where + ".centroid: must be [x,y,z]");
    }
    const json& surf = get(sel, "surf");
    if (kind == "face" && !surf.is_null()) {
        if (!surf.is_string() || FACE_SURFS.find(surf.get<string>()) == FACE_SURFS.end()) {
            errs.push_back(where + ".surf: not a face surface type");
        }
    }
    if (kind == "edge" && !surf.is_null()) {
        if (!surf.is_string() || EDGE_SURFS.find(surf.get<string>()) == EDGE_SURFS.end()) {
            errs.push_back(where + ".surf: not an edge curve type");
        }
    }
    const json& axis = get(sel, "axis");
    if (!axis.is_null() && !is_vec(axis, 3)) {
        errs.push_back(where + ".axis: must be [x,y,z] or null");
    }
    const json& owner = get(sel, "owner_feature");
    if (!owner.is_string() && !owner.is_null()) {
        errs.push_back(where + ".owner_feature: must be a feature id or null");
    }
    return errs;
}

vector<string> validate_sel_or_datum(const json& v, const string& where) {
    if (v.is_string()) {
        string s = v.get<string>();
        if (RESERVED_DATUMS.find(s) != RESERVED_DATUMS.end() ||
            std::regex_match(s, FEATURE_ID_RE)) {
            return {};
        }
        return {where + ": '" + s + "' is neither a reserved datum nor a feature id"};
    }
    return validate_selector(v, where);
}

// --------------------------------------------------------------------------- sketch

// entities_by_id maps entity id -> entity json.
string handle_targets_entity(const string& handle,
                             const std::map<string, json>& entities_by_id) {
    auto dot = handle.find('.');
    string base = (dot == string::npos) ? handle : handle.substr(0, dot);
    string sub = (dot == string::npos) ? "" : handle.substr(dot + 1);
    auto it = entities_by_id.find(base);
    if (it == entities_by_id.end()) {
        return "references unknown entity '" + base + "'";
    }
    if (sub.empty()) return "";
    const json& ent = it->second;
    const json& etypej = get(ent, "type");
    string etype = etypej.is_string() ? etypej.get<string>() : "";
    if (etype == "spline" || etype == "style_spline") {
        if (std::regex_match(sub, P_SUB_RE)) {
            const json& pts = get(ent, "points");
            size_t npts = pts.is_array() ? pts.size() : 0;
            int idx = 0;
            try { idx = std::stoi(sub.substr(1)); } catch (...) { idx = -1; }
            if (idx >= 0 && static_cast<size_t>(idx) < npts) return "";
            return etype + " point '" + sub + "' out of range";
        }
        return "'" + sub + "' is not a " + etype + " handle";
    }
    auto sit = ENTITY_SUBS.find(etype);
    const std::set<string> empty;
    const std::set<string>& subs = (sit != ENTITY_SUBS.end()) ? sit->second : empty;
    if (subs.find(sub) == subs.end()) {
        return "'" + sub + "' is not a handle of a " + etype;
    }
    return "";
}

vector<string> validate_entity(const json& e, const string& where) {
    vector<string> errs;
    if (!e.is_object()) {
        return {where + ": entity must be an object"};
    }
    const json& etypej = get(e, "type");
    string etype = etypej.is_string() ? etypej.get<string>() : "";
    auto fit = ENTITY_FIELDS.find(etype);
    if (!etypej.is_string() || fit == ENTITY_FIELDS.end()) {
        return {where + ".type: not a sketch entity type"};
    }
    const json& id = get(e, "id");
    if (!id.is_string() || !std::regex_match(id.get<string>(), ENTITY_ID_RE)) {
        errs.push_back(where + ".id: must match e<N>");
    }
    const json& cons = get(e, "construction");
    if (!cons.is_boolean()) {
        errs.push_back(where + ".construction: required bool");
    }
    static const std::set<string> point_like = {"p", "p1", "p2", "p0", "center", "vertex", "focus", "apex"};
    for (const string& field : fit->second) {
        if (!has(e, field)) {
            errs.push_back(where + "." + field + ": required for " + etype);
            continue;
        }
        const json& v = get(e, field);
        if (point_like.find(field) != point_like.end()) {
            if (!is_vec(v, 2)) errs.push_back(where + "." + field + ": must be [x,y]");
        } else if (field == "points") {
            bool ok = v.is_array() && v.size() >= 2;
            if (ok) {
                for (const auto& p : v) {
                    if (!is_vec(p, 2)) { ok = false; break; }
                }
            }
            if (!ok) errs.push_back(where + ".points: must be >=2 [x,y] pairs");
        } else if (field == "ccw") {
            if (!v.is_boolean()) errs.push_back(where + ".ccw: must be bool");
        } else if (field == "text") {
            if (!v.is_string()) errs.push_back(where + ".text: must be string");
        } else if (field == "xt" || field == "yt") {
            if (!v.is_string()) {
                errs.push_back(where + "." + field + ": must be an expression string");
            } else {
                string err = validate_eq_curve_expr(v.get<string>(), "t");
                if (!err.empty()) errs.push_back(where + "." + field + ": " + err);
            }
        } else {
            if (!is_num(v)) errs.push_back(where + "." + field + ": must be a number");
        }
    }
    if (etype == "text" && has(e, "font")) {
        const json& font = get(e, "font");
        if (!font.is_string() && !font.is_null()) {
            errs.push_back(where + ".font: must be a string or null");
        }
    }
    return errs;
}

vector<string> validate_constraint(const json& c, const string& where,
                                   const std::map<string, json>& entities_by_id,
                                   const std::set<string>& globals_names) {
    vector<string> errs;
    if (!c.is_object()) {
        return {where + ": constraint must be an object"};
    }
    const json& ctypej = get(c, "type");
    string ctype = ctypej.is_string() ? ctypej.get<string>() : "";
    auto sit = CONSTRAINT_SPECS.find(ctype);
    if (!ctypej.is_string() || sit == CONSTRAINT_SPECS.end()) {
        return {where + ".type: not a constraint type"};
    }
    const json& id = get(c, "id");
    if (!id.is_string() || !std::regex_match(id.get<string>(), CONSTRAINT_ID_RE)) {
        errs.push_back(where + ".id: must match c<N>");
    }
    const ConstraintSpec& spec = sit->second;
    const json& refsj = get(c, "refs");
    vector<json> refs;
    bool refs_ok = refsj.is_array() &&
                   static_cast<int>(refsj.size()) >= spec.lo &&
                   static_cast<int>(refsj.size()) <= spec.hi;
    if (!refs_ok) {
        errs.push_back(where + ".refs: " + ctype + " takes " + std::to_string(spec.lo) +
                       ".." + std::to_string(spec.hi) + " refs");
        // refs = [] in Python
    } else {
        for (const auto& h : refsj) refs.push_back(h);
    }
    for (size_t i = 0; i < refs.size(); ++i) {
        const json& h = refs[i];
        if (h.is_string() && (h.get<string>() == "origin" || h.get<string>() == "origin.p")) {
            continue;
        }
        if (!h.is_string() || !std::regex_match(h.get<string>(), HANDLE_RE)) {
            string hs = h.is_string() ? h.get<string>() : h.dump();
            errs.push_back(where + ".refs[" + std::to_string(i) + "]: '" + hs +
                           "' is not a valid handle");
            continue;
        }
        string bad = handle_targets_entity(h.get<string>(), entities_by_id);
        if (!bad.empty()) {
            errs.push_back(where + ".refs[" + std::to_string(i) + "]: " + bad);
        }
    }
    if (spec.needs_value) {
        if (!has(c, "value")) {
            errs.push_back(where + ".value: required for " + ctype);
        } else {
            auto de = validate_dim(get(c, "value"), where + ".value", globals_names);
            errs.insert(errs.end(), de.begin(), de.end());
        }
        if (has(c, "driven") && !get(c, "driven").is_boolean()) {
            errs.push_back(where + ".driven: must be bool");
        }
        if (has(c, "placement") && !get(c, "placement").is_null() &&
            !is_vec(get(c, "placement"), 2)) {
            errs.push_back(where + ".placement: must be [u, v] (two numbers) or null");
        }
    } else if (!get(c, "value").is_null()) {
        errs.push_back(where + ".value: " + ctype + " must not carry a value");
    }

    // Helper mirroring Python's `base = h.split(".")[0] if isinstance(h, str) else None;
    // ent = entities_by_id.get(base)`. Returns (has_dot, type-sentinel). A non-string
    // handle => ent is None (Python's `get(None)`), so treat as "<<noent>>".
    auto ent_type_of = [&](const json& h) -> std::pair<bool, string> {
        if (!h.is_string()) return {false, "<<noent>>"};
        string hs = h.get<string>();
        bool has_dot = hs.find('.') != string::npos;
        string base = has_dot ? hs.substr(0, hs.find('.')) : hs;
        auto it = entities_by_id.find(base);
        string et;
        if (it != entities_by_id.end()) {
            const json& tj = get(it->second, "type");
            et = tj.is_string() ? tj.get<string>() : "<<absent>>";
            // distinguish "ent exists" — use sentinel for missing type? Python uses
            // ent.get("type") which is None if absent -> != "line" is true.
            if (!tj.is_string()) et = "<<none>>";
        } else {
            et = "<<noent>>";  // ent is None
        }
        return {has_dot, et};
    };

    if (ctype == "angle") {
        for (size_t i = 0; i < refs.size() && i < 2; ++i) {
            auto [has_dot, et] = ent_type_of(refs[i]);
            bool ent_present = (et != "<<noent>>");
            if (has_dot || (ent_present && et != "line")) {
                errs.push_back(where + ".refs[" + std::to_string(i) + "]: angle takes two line entity ids");
            }
        }
    }
    if (ctype == "collinear") {
        for (size_t i = 0; i < refs.size() && i < 2; ++i) {
            auto [has_dot, et] = ent_type_of(refs[i]);
            bool ent_present = (et != "<<noent>>");
            if (has_dot || (ent_present && et != "line")) {
                errs.push_back(where + ".refs[" + std::to_string(i) + "]: collinear takes two line entity ids");
            }
        }
    }
    if (ctype == "coradial") {
        for (size_t i = 0; i < refs.size() && i < 2; ++i) {
            auto [has_dot, et] = ent_type_of(refs[i]);
            bool ent_present = (et != "<<noent>>");
            if (has_dot || (ent_present && et != "circle" && et != "arc")) {
                errs.push_back(where + ".refs[" + std::to_string(i) + "]: coradial takes two arc/circle entity ids");
            }
        }
    }
    if (ctype == "distance" && !get(c, "cond").is_null()) {
        const json& cnd = get(c, "cond");
        bool ok = cnd.is_array() && cnd.size() == 2;
        if (ok) {
            for (const auto& x : cnd) {
                if (!x.is_string()) { ok = false; break; }
                string xs = x.get<string>();
                if (xs != "center" && xs != "min" && xs != "max") { ok = false; break; }
            }
        }
        if (!ok) {
            errs.push_back(where + ".cond: must be a 2-list of 'center'|'min'|'max'");
        }
    }
    if (ctype == "distance" && has(c, "axis") && !get(c, "axis").is_null()) {
        const json& ax = get(c, "axis");
        string axs = ax.is_string() ? ax.get<string>() : "";
        if (!ax.is_string() || (axs != "h" && axs != "v")) {
            errs.push_back(where + ".axis: must be 'h', 'v', or null");
        } else {
            for (size_t i = 0; i < refs.size() && i < 2; ++i) {
                auto [has_dot, et] = ent_type_of(refs[i]);
                bool ent_present = (et != "<<noent>>");
                if (ent_present && et == "line" && !has_dot) {
                    errs.push_back(where + ".axis: not allowed for a point-line distance");
                }
            }
        }
    }
    return errs;
}

vector<string> validate_sketch_params(const json& params, const string& where,
                                      const std::set<string>& globals_names) {
    vector<string> errs;
    const json& entities = get(params, "entities");
    const json& constraints = get(params, "constraints");
    if (!entities.is_array()) {
        return {where + ".entities: must be a list"};
    }
    if (!constraints.is_array()) {
        return {where + ".constraints: must be a list"};
    }
    std::map<string, json> by_id;
    for (size_t i = 0; i < entities.size(); ++i) {
        const json& e = entities[i];
        auto ee = validate_entity(e, where + ".entities[" + std::to_string(i) + "]");
        errs.insert(errs.end(), ee.begin(), ee.end());
        if (e.is_object() && get(e, "id").is_string()) {
            string eid = get(e, "id").get<string>();
            if (by_id.count(eid)) {
                errs.push_back(where + ".entities[" + std::to_string(i) + "].id: duplicate '" + eid + "'");
            }
            by_id[eid] = e;
        }
    }
    std::set<string> seen_c;
    for (size_t i = 0; i < constraints.size(); ++i) {
        const json& c = constraints[i];
        auto ce = validate_constraint(c, where + ".constraints[" + std::to_string(i) + "]",
                                      by_id, globals_names);
        errs.insert(errs.end(), ce.begin(), ce.end());
        if (c.is_object() && get(c, "id").is_string()) {
            string cid = get(c, "id").get<string>();
            if (seen_c.count(cid)) {
                errs.push_back(where + ".constraints[" + std::to_string(i) + "].id: duplicate '" + cid + "'");
            }
            seen_c.insert(cid);
        }
    }
    return errs;
}

// --------------------------------------------------------------------------- features

// "truthy" check mirroring Python's `if params.get(k):` / `if not refs.get(k):`.
// A value is truthy if present and not None/false/0/""/[]/{} .
bool truthy(const json& v) {
    if (v.is_null()) return false;
    if (v.is_boolean()) return v.get<bool>();
    if (v.is_number()) return v.get<double>() != 0.0;
    if (v.is_string()) return !v.get<string>().empty();
    if (v.is_array()) return !v.empty();
    if (v.is_object()) return !v.empty();
    return false;
}

vector<string> validate_feature(const json& f, const string& where,
                                const std::set<string>& earlier_ids,
                                const std::set<string>& globals_names,
                                const std::set<string>& component_ids) {
    vector<string> errs;
    if (!f.is_object()) {
        return {where + ": feature must be an object"};
    }
    const json& ftypej = get(f, "type");
    string ftype = ftypej.is_string() ? ftypej.get<string>() : "";
    auto spit = FEATURE_TYPES.find(ftype);
    if (!ftypej.is_string() || spit == FEATURE_TYPES.end()) {
        return {where + ".type: is not a feature type"};
    }
    const FeatureSpec& spec = spit->second;
    const json& fid = get(f, "id");
    if (!fid.is_string() || !std::regex_match(fid.get<string>(), FEATURE_ID_RE)) {
        errs.push_back(where + ".id: must match f<N>");
    }
    if (!is_nonempty_string(get(f, "name"))) {
        errs.push_back(where + ".name: required");
    }
    if (!get(f, "suppressed").is_boolean()) {
        errs.push_back(where + ".suppressed: required bool");
    }

    json params = get(f, "params");
    json refs = get(f, "refs");
    if (!params.is_object()) {
        errs.push_back(where + ".params: must be an object");
        params = json::object();
    }
    if (!refs.is_object()) {
        errs.push_back(where + ".refs: must be an object");
        refs = json::object();
    }

    for (const string& key : spec.params) {
        if (!params.contains(key)) {
            // chamfer's canonical param is "length", but the OCCT kernel + a great many
            // callers (and the compiler itself) speak "distance". normalize_feature maps
            // length<->distance, so accept either here rather than rejecting a perfectly
            // valid chamfer over the key name — that rejection read to users as "chamfer
            // doesn't work" (especially on imported bodies, where it was the FIRST op tried).
            if (ftype == "chamfer" && key == "length" && params.contains("distance"))
                continue;
            errs.push_back(where + ".params." + key + ": required for " + ftype);
        }
    }
    // Dimension-shaped params get dim validation.
    for (auto it = params.begin(); it != params.end(); ++it) {
        const json& v = it.value();
        if (v.is_object() && (v.contains("value") || v.contains("expr"))) {
            auto de = validate_dim(v, where + ".params." + it.key(), globals_names);
            errs.insert(errs.end(), de.begin(), de.end());
        }
    }

    if (ftype == "sketch") {
        auto se = validate_sketch_params(params, where + ".params", globals_names);
        errs.insert(errs.end(), se.begin(), se.end());
    }

    if (ftype == "fillet") {
        if (truthy(get(params, "full_round"))) {
            if (!truthy(get(refs, "middle_face"))) {
                errs.push_back(where + ".refs.middle_face: required for full_round fillet");
            }
        } else if (truthy(get(params, "face_fillet"))) {
            if (!truthy(get(refs, "face_set_a")) || !truthy(get(refs, "face_set_b"))) {
                errs.push_back(where + ".refs.face_set_a/face_set_b: required for face fillet");
            }
            if (!params.contains("radius")) {
                errs.push_back(where + ".params.radius: required for fillet");
            }
        } else {
            if (!params.contains("radius")) {
                errs.push_back(where + ".params.radius: required for fillet");
            }
            if (!truthy(get(refs, "edges"))) {
                errs.push_back(where + ".refs.edges: required for fillet");
            }
        }
    }

    if (ftype == "chamfer") {
        if (truthy(get(params, "vertex_chamfer"))) {
            if (!truthy(get(refs, "vertices"))) {
                errs.push_back(where + ".refs.vertices: required for vertex chamfer");
            }
        } else if (!truthy(get(refs, "edges"))) {
            errs.push_back(where + ".refs.edges: required for chamfer");
        }
    }

    // Known ref slots.
    std::set<string> known_slots;
    for (const RefSlot& slot : spec.refs) known_slots.insert(slot.name);

    for (const RefSlot& slot : spec.refs) {
        const string& name = slot.name;
        bool present = refs.contains(name) && !refs[name].is_null();
        if (!present) {
            if (slot.required) {
                // boolean accepts a params.a/params.b shorthand (target/tool body feature
                // ids) in place of refs.targets/refs.tools — the compiler reads either form
                // (ops_cheatsheet documents both). Don't reject the documented shorthand
                // over the ref-slot name (mirrors the chamfer length/distance acceptance).
                if (ftype == "boolean" && (name == "targets" || name == "tools") &&
                    params.contains("a"))
                    continue;
                errs.push_back(where + ".refs." + name + ": required for " + ftype);
            }
            continue;
        }
        const json& v = refs[name];
        string loc = where + ".refs." + name;
        // A geometry query is valid in any selector slot (edges/remove/face/profile/...).
        if (is_query_obj(v)) {
            auto e = validate_geom_query(v, loc);
            errs.insert(errs.end(), e.begin(), e.end());
            continue;
        }
        // An explicit axis/direction vector is valid in a direction slot, whatever
        // wrapper the model put around it (the compiler reads the `axis` vector).
        if (AXIS_DIR_SLOTS.count(name) && is_explicit_axis(v)) continue;
        // hole `positions` is a list of [u,v] face-local coordinate pairs — that is what
        // the compiler reads and what the cheatsheet teaches. (The slot is typed SelList
        // for a legacy vertex-selector form, which the compiler never actually consumes.)
        // Accept a clean [u,v] list here; fall through to SelList for selector objects.
        if (name == "positions" && v.is_array() && !v.empty()) {
            bool all_uv = true;
            for (const json& p : v) if (!is_vec(p, 2)) { all_uv = false; break; }
            if (all_uv) continue;
        }
        switch (slot.form) {
            case RefForm::Sel: {
                auto e = validate_selector(v, loc);
                errs.insert(errs.end(), e.begin(), e.end());
                break;
            }
            case RefForm::SelOrDatum: {
                auto e = validate_sel_or_datum(v, loc);
                errs.insert(errs.end(), e.begin(), e.end());
                break;
            }
            case RefForm::SelList: {
                if (!v.is_array() || v.empty()) {
                    errs.push_back(loc + ": must be a non-empty list");
                } else {
                    for (size_t i = 0; i < v.size(); ++i) {
                        const string eloc = loc + "[" + std::to_string(i) + "]";
                        // A list element may be a geometry QUERY ({"of":"edge",...}) just like
                        // a single-value slot — e.g. "chamfer both ends" =
                        // [{"of":"edge","side":"+z"},{"of":"edge","side":"-z"}]. Validate it as
                        // a query, not a topology selector.
                        auto e = is_query_obj(v[i]) ? validate_geom_query(v[i], eloc)
                                                    : validate_selector(v[i], eloc);
                        errs.insert(errs.end(), e.begin(), e.end());
                    }
                }
                break;
            }
            case RefForm::FeatList: {
                if (!v.is_array() || v.empty()) {
                    errs.push_back(loc + ": must be a non-empty list of feature ids");
                } else {
                    // Same-batch placeholder regex: "$<N>" (e.g. "$0", "$3"). session.cpp
                    // resolves these to real fN ids before compile, but validate_document
                    // may run against a batch that has not yet been through placeholder
                    // resolution — accept them here exactly as SelList/SelOrDatum do for
                    // profile refs (the form produced by the agent for same-batch refs).
                    static const std::regex BATCH_PLACEHOLDER_RE(R"(^\$\d+$)");
                    for (size_t i = 0; i < v.size(); ++i) {
                        const json& s = v[i];
                        if (!s.is_string()) {
                            errs.push_back(loc + "[" + std::to_string(i) + "]: not a feature id");
                        } else if (std::regex_match(s.get<string>(), BATCH_PLACEHOLDER_RE)) {
                            // Same-batch "$<index>" placeholder — valid at validation time;
                            // session.cpp rewrites to fN before compile.
                        } else if (!std::regex_match(s.get<string>(), FEATURE_ID_RE)) {
                            errs.push_back(loc + "[" + std::to_string(i) + "]: not a feature id");
                        } else if (earlier_ids.find(s.get<string>()) == earlier_ids.end()) {
                            errs.push_back(loc + "[" + std::to_string(i) + "]: '" +
                                           s.get<string>() + "' does not precede this feature");
                        }
                    }
                }
                break;
            }
            case RefForm::UvList: {
                if (!v.is_array() || v.empty()) {
                    errs.push_back(loc + ": must be a non-empty list of [u,v] points");
                } else {
                    for (size_t i = 0; i < v.size(); ++i) {
                        if (!is_vec(v[i], 2)) {
                            errs.push_back(loc + "[" + std::to_string(i) + "]: must be [u,v] (two numbers)");
                        }
                    }
                }
                break;
            }
            case RefForm::EntityIdList: {
                if (!v.is_array() || v.empty()) {
                    errs.push_back(loc + ": must be a non-empty list of sketch entity ids");
                } else {
                    for (size_t i = 0; i < v.size(); ++i) {
                        const json& s = v[i];
                        if (!s.is_string() || !std::regex_match(s.get<string>(), ENTITY_ID_RE)) {
                            string ss = s.is_string() ? s.get<string>() : s.dump();
                            errs.push_back(loc + "[" + std::to_string(i) + "]: '" + ss +
                                           "' is not a sketch entity id (e<N>)");
                        }
                    }
                }
                break;
            }
            case RefForm::CompList: {
                if (!v.is_array() || v.empty()) {
                    errs.push_back(loc + ": must be a non-empty list of component ids");
                } else {
                    for (size_t i = 0; i < v.size(); ++i) {
                        const json& s = v[i];
                        string ss = s.is_string() ? s.get<string>() : "";
                        if (!s.is_string() || component_ids.find(ss) == component_ids.end()) {
                            string disp = s.is_string() ? ss : s.dump();
                            errs.push_back(loc + "[" + std::to_string(i) + "]: unknown component '" + disp + "'");
                        }
                    }
                }
                break;
            }
        }
    }
    // Unknown ref slots.
    for (auto it = refs.begin(); it != refs.end(); ++it) {
        if (known_slots.find(it.key()) == known_slots.end()) {
            errs.push_back(where + ".refs." + it.key() + ": unknown ref slot for " + ftype);
        }
    }
    return errs;
}

// --------------------------------------------------------------------------- documents

std::pair<vector<string>, std::set<string>> validate_equations(const json& doc,
                                                               const string& where) {
    vector<string> errs;
    std::set<string> names;
    json eq = has(doc, "equations") ? get(doc, "equations") : json{{"globals", json::array()}};
    if (!eq.is_object() || !get(eq, "globals").is_array()) {
        return {{where + ".equations: must be {globals: [...]}"}, names};
    }
    const json& globals = get(eq, "globals");
    for (size_t i = 0; i < globals.size(); ++i) {
        const json& g = globals[i];
        string loc = where + ".equations.globals[" + std::to_string(i) + "]";
        if (!g.is_object() || !get(g, "name").is_string() || !get(g, "expr").is_string()) {
            errs.push_back(loc + ": must be {name, expr}");
            continue;
        }
        string name = get(g, "name").get<string>();
        if (!std::regex_match(name, IDENT_RE)) {
            errs.push_back(loc + ".name: invalid identifier '" + name + "'");
        }
        if (names.count(name)) {
            errs.push_back(loc + ".name: duplicate '" + name + "'");
        }
        names.insert(name);
        string e = validate_expr(get(g, "expr").get<string>());
        if (!e.empty()) {
            errs.push_back(loc + ".expr: " + e);
        }
    }
    return {errs, names};
}

vector<string> validate_appearances(const json& doc) {
    const json& ap = get(doc, "appearances");
    if (ap.is_null()) return {};
    if (!ap.is_object()) {
        return {"appearances: must be an object {body_key: [r,g,b] | {color,...}}"};
    }
    vector<string> errs;
    auto in01 = [](const json& arr) -> bool {
        for (const auto& c : arr) {
            double cv = c.get<double>();
            if (!(cv >= 0.0 && cv <= 1.0)) return false;
        }
        return true;
    };
    for (auto it = ap.begin(); it != ap.end(); ++it) {
        const string& key = it.key();
        const json& val = it.value();
        // Python: key is always a non-empty string for a JSON object, but mirror anyway.
        if (key.empty()) {
            errs.push_back("appearances: key '" + key + "' must be a non-empty string");
        }
        if (val.is_object()) {
            const json& col = get(val, "color");
            if (!is_vec(col, 3) || !in01(col)) {
                errs.push_back("appearances." + key + ".color: must be [r,g,b] with each component in 0..1");
            }
            for (const char* mk : {"metallic", "roughness"}) {
                if (val.contains(mk)) {
                    const json& mv = val[mk];
                    if (!is_num(mv) || !(mv.get<double>() >= 0.0 && mv.get<double>() <= 1.0)) {
                        errs.push_back(string("appearances.") + key + "." + mk + ": must be a number in 0..1");
                    }
                }
            }
        } else if (!is_vec(val, 3) || !in01(val)) {
            errs.push_back("appearances." + key + ": must be [r,g,b] (0..1) or {color, metallic, roughness}");
        }
    }
    return errs;
}

vector<string> validate_rollback(const json& doc, const std::set<string>& feature_ids) {
    const json& rb = get(doc, "rollback_after");
    if (rb.is_null()) return {};
    if (!rb.is_string()) {
        return {"rollback_after: must be a feature id string or null"};
    }
    if (feature_ids.find(rb.get<string>()) == feature_ids.end()) {
        return {"rollback_after: '" + rb.get<string>() + "' is not an existing feature id"};
    }
    return {};
}

vector<string> validate_configurations(const json& doc,
                                       const std::map<string, json>& features_by_id) {
    vector<string> errs;
    const json& configs = get(doc, "configurations");
    if (configs.is_null()) {
        const json& ac = get(doc, "active_configuration");
        if (!ac.is_null() && !ac.is_string()) {
            return {"active_configuration: must be a configuration name string or null"};
        }
        return {};
    }
    if (!configs.is_array()) {
        return {"configurations: must be a list"};
    }
    std::set<string> names;
    for (size_t i = 0; i < configs.size(); ++i) {
        const json& cfg = configs[i];
        string loc = "configurations[" + std::to_string(i) + "]";
        if (!cfg.is_object()) {
            errs.push_back(loc + ": must be an object");
            continue;
        }
        const json& namej = get(cfg, "name");
        if (!is_nonempty_string(namej)) {
            errs.push_back(loc + ".name: required non-empty string");
        } else if (names.count(namej.get<string>())) {
            errs.push_back(loc + ".name: duplicate '" + namej.get<string>() + "'");
        } else {
            names.insert(namej.get<string>());
        }

        json overrides = has(cfg, "param_overrides") ? get(cfg, "param_overrides") : json::object();
        if (overrides.is_null()) overrides = json::object();
        if (!overrides.is_object()) {
            errs.push_back(loc + ".param_overrides: must be an object");
        } else {
            for (auto it = overrides.begin(); it != overrides.end(); ++it) {
                const string& key = it.key();
                const json& val = it.value();
                string kloc = loc + ".param_overrides." + key;
                if (key.find('.') == string::npos) {
                    errs.push_back(kloc + ": key must be '<feature_id>.<param_key>'");
                    continue;
                }
                string fid = key.substr(0, key.find('.'));
                string pkey = key.substr(key.find('.') + 1);
                auto fit = features_by_id.find(fid);
                if (fit == features_by_id.end()) {
                    errs.push_back(kloc + ": feature '" + fid + "' does not exist");
                    continue;
                }
                const json& fparams = get(fit->second, "params");
                if (fparams.is_object() && !fparams.contains(pkey)) {
                    errs.push_back(kloc + ": feature '" + fid + "' has no param '" + pkey + "'");
                }
                if (val.is_object() && (val.contains("value") || val.contains("expr"))) {
                    auto de = validate_dim(val, kloc, {});
                    errs.insert(errs.end(), de.begin(), de.end());
                }
            }
        }

        json sup = has(cfg, "suppressions") ? get(cfg, "suppressions") : json::object();
        if (sup.is_null()) sup = json::object();
        if (!sup.is_object()) {
            errs.push_back(loc + ".suppressions: must be an object");
        } else {
            for (auto it = sup.begin(); it != sup.end(); ++it) {
                const string& fid = it.key();
                const json& flag = it.value();
                string sloc = loc + ".suppressions." + fid;
                if (features_by_id.find(fid) == features_by_id.end()) {
                    errs.push_back(sloc + ": feature '" + fid + "' does not exist");
                }
                if (!flag.is_boolean()) {
                    errs.push_back(sloc + ": must be a bool");
                }
            }
        }
    }
    const json& ac = get(doc, "active_configuration");
    if (!ac.is_null()) {
        if (!ac.is_string()) {
            errs.push_back("active_configuration: must be a configuration name string or null");
        } else if (names.find(ac.get<string>()) == names.end()) {
            errs.push_back("active_configuration: '" + ac.get<string>() + "' is not a defined configuration");
        }
    }
    return errs;
}

vector<string> validate_common(const json& doc, const string& schema) {
    vector<string> errs;
    const json& schj = get(doc, "schema");
    if (!(schj.is_string() && schj.get<string>() == schema)) {
        string got = schj.is_string() ? schj.get<string>() : (schj.is_null() ? "None" : schj.dump());
        errs.push_back("schema: expected '" + schema + "', got '" + got + "'");
    }
    if (!is_nonempty_string(get(doc, "id"))) {
        errs.push_back("id: required string");
    }
    if (!is_nonempty_string(get(doc, "name"))) {
        errs.push_back("name: required string");
    }
    const json& units = get(doc, "units");
    bool units_ok = units.is_object() && get(units, "system").is_string() &&
                    UNIT_SYSTEMS.find(get(units, "system").get<string>()) != UNIT_SYSTEMS.end();
    if (!units_ok) {
        errs.push_back("units.system: must be one of " + sorted_set_str(UNIT_SYSTEMS));
    }
    auto ae = validate_appearances(doc);
    errs.insert(errs.end(), ae.begin(), ae.end());
    return errs;
}

// Optional doc-level `studies` list (simulation/FEA/CFD setups; sim_study_* RPCs).
// ABSENT `studies` => no constraint. The core stores study payloads verbatim and does
// NOT interpret their fields, so this is a LIGHT structural check only: studies must be
// a list of objects, each carrying a non-empty string `id` and `type`. Port of
// engine/doc/schema.py::_validate_studies.
vector<string> validate_studies(const json& doc) {
    const json& studies = get(doc, "studies");
    if (studies.is_null()) return {};
    if (!studies.is_array()) return {"studies: must be a list"};
    vector<string> errs;
    for (size_t i = 0; i < studies.size(); ++i) {
        string loc = "studies[" + std::to_string(i) + "]";
        const json& st = studies[i];
        if (!st.is_object()) { errs.push_back(loc + ": must be an object"); continue; }
        const json& idj = get(st, "id");
        if (!(idj.is_string() && !idj.get<string>().empty()))
            errs.push_back(loc + ".id: required non-empty string");
        const json& tj = get(st, "type");
        if (!(tj.is_string() && !tj.get<string>().empty()))
            errs.push_back(loc + ".type: required non-empty string");
    }
    return errs;
}

vector<string> validate_part(const json& doc) {
    if (!doc.is_object()) {
        return {"document must be a JSON object"};
    }
    vector<string> errs = validate_common(doc, "aicad.part/1");
    const json& mat = get(doc, "material");
    if (!mat.is_null()) {
        if (!(mat.is_object() && get(mat, "key").is_string())) {
            errs.push_back("material: must be null or {key}");
        }
    }
    auto eqres = validate_equations(doc, "");
    errs.insert(errs.end(), eqres.first.begin(), eqres.first.end());
    const std::set<string>& globals_names = eqres.second;

    const json& feats = get(doc, "features");
    if (!feats.is_array()) {
        errs.push_back("features: must be a list");
        return errs;
    }
    std::set<string> earlier(RESERVED_DATUMS);
    std::set<string> seen;
    std::set<string> feat_ids;
    std::map<string, json> feats_by_id;
    static const std::set<string> no_components;  // parts -> always empty
    for (size_t i = 0; i < feats.size(); ++i) {
        const json& f = feats[i];
        string fid_str = (f.is_object() && get(f, "id").is_string()) ? get(f, "id").get<string>() : "";
        string fid_disp = (f.is_object() && get(f, "id").is_string())
                              ? get(f, "id").get<string>()
                              : (f.is_object() && !get(f, "id").is_null() ? get(f, "id").dump() : "None");
        string loc = "features[" + std::to_string(i) + "](" + fid_disp + ")";
        auto fe = validate_feature(f, loc, earlier, globals_names, no_components);
        errs.insert(errs.end(), fe.begin(), fe.end());
        if (f.is_object() && get(f, "id").is_string()) {
            if (seen.count(fid_str)) {
                errs.push_back(loc + ".id: duplicate");
            }
            seen.insert(fid_str);
            earlier.insert(fid_str);
            feat_ids.insert(fid_str);
            feats_by_id[fid_str] = f;
        }
    }
    auto re = validate_rollback(doc, feat_ids);
    errs.insert(errs.end(), re.begin(), re.end());
    auto ce = validate_configurations(doc, feats_by_id);
    errs.insert(errs.end(), ce.begin(), ce.end());
    auto se = validate_studies(doc);
    errs.insert(errs.end(), se.begin(), se.end());
    return errs;
}

// --- assembly schema (port of engine/doc/schema.py::validate_assembly) -----------
const std::set<string> MATE_TYPES = {
    "coincident", "parallel", "perpendicular", "tangent",
    "concentric", "distance", "angle", "lock",
    "width", "symmetric", "distance_limit", "angle_limit", "profile_center",
    "gear", "rack_pinion", "screw", "hinge", "slot", "cam_follower"};
const std::set<string> MATE_VALUE_TYPES = {"distance", "angle"};
// mtype -> (min, max) ref counts; default (2,2).
std::pair<int, int> mate_ref_counts(const string& t) {
    if (t == "width") return {3, 4};
    if (t == "symmetric") return {3, 3};
    if (t == "hinge") return {2, 4};
    return {2, 2};
}
// mtype -> (required numeric keys, optional numeric keys) for coupling/limit mates.
bool mate_coupling_spec(const string& t, vector<string>& req, vector<string>& opt) {
    req.clear(); opt.clear();
    if (t == "distance_limit" || t == "angle_limit") { req = {"min", "max"}; opt = {"value"}; return true; }
    if (t == "gear") { req = {"ratio"}; return true; }
    if (t == "screw") { req = {"pitch"}; return true; }
    if (t == "rack_pinion") { opt = {"pitch_radius"}; return true; }
    if (t == "hinge") { opt = {"min", "max"}; return true; }
    return false;
}

vector<string> validate_assembly(const json& doc) {
    if (!doc.is_object()) {
        return {"document must be a JSON object"};
    }
    vector<string> errs = validate_common(doc, "aicad.asm/1");
    auto eqres = validate_equations(doc, "");
    errs.insert(errs.end(), eqres.first.begin(), eqres.first.end());
    const std::set<string>& globals_names = eqres.second;

    const json& comps = get(doc, "components");
    std::set<string> comp_ids;
    if (!comps.is_array()) {
        errs.push_back("components: must be a list");
    } else {
        for (size_t i = 0; i < comps.size(); ++i) {
            const json& k = comps[i];
            string loc = "components[" + std::to_string(i) + "]";
            if (!k.is_object()) { errs.push_back(loc + ": must be an object"); continue; }
            const json& kidj = get(k, "id");
            string kid = kidj.is_string() ? kidj.get<string>() : "";
            if (!kidj.is_string() || !std::regex_match(kid, std::regex("^k[0-9]+$")))
                errs.push_back(loc + ".id: must match k<N>");
            else if (comp_ids.count(kid))
                errs.push_back(loc + ".id: duplicate '" + kid + "'");
            else
                comp_ids.insert(kid);
            if (!is_nonempty_string(get(k, "name"))) errs.push_back(loc + ".name: required");
            const json& src = get(k, "source");
            bool src_ok = src.is_object() &&
                          ((get(src, "path").is_string()) || (get(src, "embedded").is_string()));
            if (!src_ok) errs.push_back(loc + ".source: must be {path} or {embedded}");
            if (!is_vec(get(k, "transform"), 16))
                errs.push_back(loc + ".transform: must be 16 floats (row-major)");
            for (const char* flag : {"fixed", "suppressed"})
                if (!get(k, flag).is_boolean()) errs.push_back(loc + "." + flag + ": required bool");
        }
    }

    const json& mates = get(doc, "mates");
    std::set<string> seen_m;
    if (!mates.is_array()) {
        errs.push_back("mates: must be a list");
    } else {
        for (size_t i = 0; i < mates.size(); ++i) {
            const json& m = mates[i];
            string loc = "mates[" + std::to_string(i) + "]";
            if (!m.is_object()) { errs.push_back(loc + ": must be an object"); continue; }
            const json& midj = get(m, "id");
            string mid = midj.is_string() ? midj.get<string>() : "";
            if (!midj.is_string() || !std::regex_match(mid, std::regex("^m[0-9]+$")))
                errs.push_back(loc + ".id: must match m<N>");
            else if (seen_m.count(mid))
                errs.push_back(loc + ".id: duplicate '" + mid + "'");
            else
                seen_m.insert(mid);
            const json& mtj = get(m, "type");
            string mtype = mtj.is_string() ? mtj.get<string>() : "";
            if (!mtj.is_string() || !MATE_TYPES.count(mtype))
                errs.push_back(loc + ".type: '" + (mtj.is_string() ? mtype : mtj.dump()) +
                               "' not a mate type");
            const json& alj = get(m, "align");
            if (!(alj.is_string() && (alj.get<string>() == "aligned" || alj.get<string>() == "anti")))
                errs.push_back(loc + ".align: must be 'aligned' or 'anti'");
            if (!get(m, "suppressed").is_boolean())
                errs.push_back(loc + ".suppressed: required bool");
            const json& refsj = get(m, "refs");
            auto [lo_r, hi_r] = mate_ref_counts(mtype);
            json refs = json::array();
            if (!refsj.is_array() ||
                !((int)refsj.size() >= lo_r && (int)refsj.size() <= hi_r)) {
                string want = lo_r == hi_r ? std::to_string(lo_r)
                                           : std::to_string(lo_r) + ".." + std::to_string(hi_r);
                errs.push_back(loc + ".refs: " + mtype + " takes " + want + " entries");
            } else {
                refs = refsj;
            }
            for (size_t j = 0; j < refs.size(); ++j) {
                const json& r = refs[j];
                string rloc = loc + ".refs[" + std::to_string(j) + "]";
                if (!r.is_object()) { errs.push_back(rloc + ": must be {component, selector}"); continue; }
                const json& cj = get(r, "component");
                if (!(cj.is_string() && comp_ids.count(cj.get<string>())))
                    errs.push_back(rloc + ".component: unknown '" +
                                   (cj.is_string() ? cj.get<string>() : cj.dump()) + "'");
                auto se = validate_selector(get(r, "selector"), rloc + ".selector");
                errs.insert(errs.end(), se.begin(), se.end());
            }
            const json& valj = get(m, "value");
            vector<string> req, opt;
            if (MATE_VALUE_TYPES.count(mtype)) {
                if (valj.is_null()) errs.push_back(loc + ".value: required for " + mtype);
                else {
                    auto de = validate_dim(valj, loc + ".value", globals_names);
                    errs.insert(errs.end(), de.begin(), de.end());
                }
            } else if (mate_coupling_spec(mtype, req, opt)) {
                if (!valj.is_object()) {
                    if (!req.empty()) {
                        string rs;
                        for (size_t z = 0; z < req.size(); ++z) { if (z) rs += ", "; rs += req[z]; }
                        errs.push_back(loc + ".value: " + mtype + " needs an object with [" + rs + "]");
                    }
                } else {
                    for (const string& key : req)
                        if (!is_num(get(valj, key)))
                            errs.push_back(loc + ".value." + key + ": required number for " + mtype);
                    for (const string& key : opt)
                        if (!get(valj, key).is_null() && !is_num(get(valj, key)))
                            errs.push_back(loc + ".value." + key + ": must be a number");
                    if (get(valj, "value").is_number()) {
                        auto de = validate_dim(valj, loc + ".value", globals_names);
                        errs.insert(errs.end(), de.begin(), de.end());
                    }
                }
            } else if (!valj.is_null()) {
                errs.push_back(loc + ".value: " + mtype + " must not carry a value");
            }
        }
    }

    const json& feats = get(doc, "features");
    std::set<string> earlier(RESERVED_DATUMS);
    std::set<string> asm_feat_ids;
    std::map<string, json> asm_feats_by_id;
    if (feats.is_array()) {
        for (size_t i = 0; i < feats.size(); ++i) {
            const json& f = feats[i];
            string fid = (f.is_object() && get(f, "id").is_string()) ? get(f, "id").get<string>() : "";
            string fid_disp = fid.empty() ? "None" : fid;
            auto fe = validate_feature(f, "features[" + std::to_string(i) + "](" + fid_disp + ")",
                                       earlier, globals_names, comp_ids);
            errs.insert(errs.end(), fe.begin(), fe.end());
            if (!fid.empty()) {
                earlier.insert(fid);
                asm_feat_ids.insert(fid);
                if (f.is_object()) asm_feats_by_id[fid] = f;
            }
        }
    } else {
        errs.push_back("features: must be a list");
    }
    auto re = validate_rollback(doc, asm_feat_ids);
    errs.insert(errs.end(), re.begin(), re.end());
    auto ce = validate_configurations(doc, asm_feats_by_id);
    errs.insert(errs.end(), ce.begin(), ce.end());

    const json& explode = get(doc, "explode");
    if (!explode.is_null()) {
        if (!explode.is_array()) {
            errs.push_back("explode: must be a list");
        } else {
            for (size_t i = 0; i < explode.size(); ++i) {
                const json& st = explode[i];
                string loc = "explode[" + std::to_string(i) + "]";
                if (!st.is_object()) { errs.push_back(loc + ": must be an object"); continue; }
                const json& cj = get(st, "component");
                if (!(cj.is_string() && comp_ids.count(cj.get<string>())))
                    errs.push_back(loc + ".component: unknown '" +
                                   (cj.is_string() ? cj.get<string>() : cj.dump()) + "'");
                if (!is_vec(get(st, "vector"), 3)) errs.push_back(loc + ".vector: must be [x,y,z]");
                if (!is_num(get(st, "distance"))) errs.push_back(loc + ".distance: must be a number");
            }
        }
    }
    auto se = validate_studies(doc);
    errs.insert(errs.end(), se.begin(), se.end());
    return errs;
}

}  // namespace

// --------------------------------------------------------------------------- public

std::vector<std::string> validate_document(const nlohmann::json& doc) {
    if (!doc.is_object()) {
        return {"document must be a JSON object"};
    }
    const json& schj = get(doc, "schema");
    string schema = schj.is_string() ? schj.get<string>() : "";
    if (schj.is_string() && schema == "aicad.part/1") {
        return validate_part(doc);
    }
    if (schj.is_string() && schema == "aicad.asm/1") {
        return validate_assembly(doc);
    }
    string got = schj.is_string() ? schj.get<string>() : (schj.is_null() ? "None" : schj.dump());
    return {"schema: unknown '" + got + "'"};
}

}  // namespace aicad::doc
