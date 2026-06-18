// core/agent/state.cpp — see state.h. Mirrors agent/orchestrator/state.py.
#include "agent/state.h"

#include <algorithm>
#include <cctype>
#include <cmath>
#include <cstdio>
#include <sstream>

#include "agent/roles.h"

namespace aicad {
namespace agent {

// Python truthiness for a JSON value (used where Python does `if x`).
static bool truthy(const ojson& v) {
    if (v.is_null()) return false;
    if (v.is_boolean()) return v.get<bool>();
    if (v.is_string()) return !v.get<std::string>().empty();
    if (v.is_number_integer()) return v.get<long long>() != 0;
    if (v.is_number_unsigned()) return v.get<unsigned long long>() != 0;
    if (v.is_number_float()) return v.get<double>() != 0.0;
    if (v.is_array() || v.is_object()) return !v.empty();
    return false;
}

// Python len(str) counts Unicode CODE POINTS, not bytes. Count UTF-8 lead bytes
// (every byte that is not a 0x80-0xBF continuation byte). Astral chars (encoded
// as 4 bytes / a single Python str char) thus count as 1, matching Python.
static int utf8_len(const std::string& s) {
    int n = 0;
    for (unsigned char c : s)
        if ((c & 0xC0) != 0x80) ++n;
    return n;
}

int est_tokens(const std::string& text) {
    return (utf8_len(text) + kCharsPerToken - 1) / kCharsPerToken;
}

// --------------------------------------------------------------------------- ojson.dumps port
namespace {

// Python ojson.dumps string escaping (ensure_ascii=True): escapes ", \, control
// chars via \b \t \n \f \r, other <0x20 via \u00XX, and all >=0x7f via \uXXXX
// (surrogate pairs for astral code points). Input is UTF-8.
void dump_string(const std::string& s, std::string& out) {
    out += '"';
    size_t i = 0;
    const size_t n = s.size();
    while (i < n) {
        unsigned char c = static_cast<unsigned char>(s[i]);
        if (c == '"') { out += "\\\""; ++i; continue; }
        if (c == '\\') { out += "\\\\"; ++i; continue; }
        if (c == '\b') { out += "\\b"; ++i; continue; }
        if (c == '\t') { out += "\\t"; ++i; continue; }
        if (c == '\n') { out += "\\n"; ++i; continue; }
        if (c == '\f') { out += "\\f"; ++i; continue; }
        if (c == '\r') { out += "\\r"; ++i; continue; }
        if (c < 0x20) {
            char buf[8];
            std::snprintf(buf, sizeof(buf), "\\u%04x", c);
            out += buf;
            ++i;
            continue;
        }
        if (c < 0x80) {  // plain ASCII
            out += static_cast<char>(c);
            ++i;
            continue;
        }
        // Decode one UTF-8 code point, then emit \uXXXX (ensure_ascii).
        unsigned int cp = 0;
        int extra = 0;
        if ((c & 0xE0) == 0xC0) { cp = c & 0x1F; extra = 1; }
        else if ((c & 0xF0) == 0xE0) { cp = c & 0x0F; extra = 2; }
        else if ((c & 0xF8) == 0xF0) { cp = c & 0x07; extra = 3; }
        else { cp = c; extra = 0; }  // malformed; pass byte through
        ++i;
        for (int k = 0; k < extra && i < n; ++k, ++i)
            cp = (cp << 6) | (static_cast<unsigned char>(s[i]) & 0x3F);
        char buf[16];
        if (cp <= 0xFFFF) {
            std::snprintf(buf, sizeof(buf), "\\u%04x", cp);
            out += buf;
        } else {
            cp -= 0x10000;
            unsigned int hi = 0xD800 + (cp >> 10);
            unsigned int lo = 0xDC00 + (cp & 0x3FF);
            std::snprintf(buf, sizeof(buf), "\\u%04x\\u%04x", hi, lo);
            out += buf;
        }
    }
    out += '"';
}

// Python repr(float) shortest round-tripping representation, then mapped to
// ojson.dumps output (inf/nan -> Infinity/-Infinity/NaN; integral floats keep
// the trailing ".0"; exponents use Python's "e+NN"/"e-NN" 2-digit-min form).
std::string dump_double(double v) {
    if (std::isnan(v)) return "NaN";
    if (std::isinf(v)) return v < 0 ? "-Infinity" : "Infinity";
    if (v == 0.0) return std::signbit(v) ? "-0.0" : "0.0";
    // CPython float_repr: the SHORTEST decimal digit string that round-trips, then
    // formatted by repr's fixed-vs-scientific rule. `%.*g`'s fixed/sci threshold is
    // precision-dependent (so minimal-prec `%g` wrongly renders 10.0 as "1e+01"); we
    // instead extract shortest (mantissa digits, decimal exponent) via `%.*e`, then
    // apply Python's rule: scientific iff decexp < -4 or decexp >= 16, else fixed.
    char buf[64];
    int digits10 = 17;
    for (int prec = 0; prec <= 16; ++prec) {
        std::snprintf(buf, sizeof(buf), "%.*e", prec, v);
        if (std::strtod(buf, nullptr) == v) {
            digits10 = prec + 1;
            break;
        }
    }
    // Re-render at the chosen significant-digit count in scientific form to read off
    // the mantissa digits + decimal exponent unambiguously.
    std::snprintf(buf, sizeof(buf), "%.*e", digits10 - 1, v);
    std::string sci(buf);
    bool neg = false;
    size_t p = 0;
    if (sci[p] == '-') { neg = true; ++p; }
    // mantissa: D[.DDD]
    std::string mant_digits;
    mant_digits += sci[p++];          // leading digit
    if (p < sci.size() && sci[p] == '.') {
        ++p;
        while (p < sci.size() && sci[p] != 'e' && sci[p] != 'E') mant_digits += sci[p++];
    }
    // exponent
    int decexp = 0;
    {
        size_t ep = sci.find_first_of("eE");
        decexp = std::atoi(sci.c_str() + ep + 1);
    }
    // Strip trailing zeros from the mantissa digit string (keep >=1 digit).
    while (mant_digits.size() > 1 && mant_digits.back() == '0') mant_digits.pop_back();
    int ndig = static_cast<int>(mant_digits.size());

    std::string out;
    if (decexp < -4 || decexp >= 16) {
        // Scientific: D[.DDD]e±NN (>=2 exponent digits, like Python). A single
        // significant digit gets NO fractional part (Python: repr(1e16) == "1e+16").
        out += mant_digits[0];
        if (ndig > 1) out += "." + mant_digits.substr(1);
        char esign = decexp < 0 ? '-' : '+';
        int eabs = decexp < 0 ? -decexp : decexp;
        std::string ed = std::to_string(eabs);
        if (ed.size() < 2) ed = "0" + ed;
        out += "e";
        out += esign;
        out += ed;
    } else if (decexp >= 0) {
        // Fixed, integer part spans decexp+1 digits.
        int intlen = decexp + 1;
        if (ndig <= intlen) {
            out += mant_digits;
            out.append(intlen - ndig, '0');
            out += ".0";
        } else {
            out += mant_digits.substr(0, intlen);
            out += ".";
            out += mant_digits.substr(intlen);
        }
    } else {
        // Fixed, value < 1: 0.00...DDD with (-decexp-1) leading zeros after the point.
        out += "0.";
        out.append(-decexp - 1, '0');
        out += mant_digits;
    }
    return neg ? "-" + out : out;
}

// Recursively dump `j` with indent=0 + separators (",", ": "). `depth` only
// drives the newline structure (no leading spaces since indent=0).
void dump_value(const ojson& j, std::string& out) {
    switch (j.type()) {
        case ojson::value_t::null:
            out += "null";
            break;
        case ojson::value_t::boolean:
            out += j.get<bool>() ? "true" : "false";
            break;
        case ojson::value_t::string:
            dump_string(j.get<std::string>(), out);
            break;
        case ojson::value_t::number_integer:
            out += std::to_string(j.get<long long>());
            break;
        case ojson::value_t::number_unsigned:
            out += std::to_string(j.get<unsigned long long>());
            break;
        case ojson::value_t::number_float:
            out += dump_double(j.get<double>());
            break;
        case ojson::value_t::array: {
            if (j.empty()) { out += "[]"; break; }
            out += "[\n";
            bool first = true;
            for (const auto& e : j) {
                if (!first) out += ",\n";
                first = false;
                dump_value(e, out);
            }
            out += "\n]";
            break;
        }
        case ojson::value_t::object: {
            if (j.empty()) { out += "{}"; break; }
            out += "{\n";
            bool first = true;
            for (auto it = j.begin(); it != j.end(); ++it) {
                if (!first) out += ",\n";
                first = false;
                dump_string(it.key(), out);
                out += ": ";
                dump_value(it.value(), out);
            }
            out += "\n}";
            break;
        }
        default:
            out += "null";
            break;
    }
}

}  // namespace

std::string py_json_dumps_indent0(const ojson& obj) {
    std::string out;
    dump_value(obj, out);
    return out;
}

// --------------------------------------------------------------------------- ledger

int AttemptLedger::bump(const std::string& stage) {
    attempts[stage] = attempts.count(stage) ? attempts[stage] + 1 : 1;
    return attempts[stage];
}

int AttemptLedger::count(const std::string& stage) const {
    auto it = attempts.find(stage);
    return it == attempts.end() ? 0 : it->second;
}

int AttemptLedger::note_sig(const std::string& stage, const std::string& sig) {
    auto it = last_sigs.find(stage);
    if (!sig.empty() && it != last_sigs.end() && it->second == sig) {
        sig_streak[stage] = (sig_streak.count(stage) ? sig_streak[stage] : 0) + 1;
    } else {
        sig_streak[stage] = 1;
    }
    last_sigs[stage] = sig;
    return sig_streak[stage];
}

void DesignState::update_tree(const ojson& tree) { tree_digest = digest_tree(tree); }

// --------------------------------------------------------------------------- digests
namespace {

// rstrip a string (Python str.rstrip default whitespace).
std::string rstrip(const std::string& s) {
    size_t b = s.size();
    while (b > 0) {
        unsigned char c = static_cast<unsigned char>(s[b - 1]);
        if (c == ' ' || c == '\t' || c == '\n' || c == '\r' || c == '\f' || c == '\v') --b;
        else break;
    }
    return s.substr(0, b);
}

std::string to_lower(const std::string& s) {
    std::string r;
    r.reserve(s.size());
    for (char c : s) r += static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    return r;
}

// Python str() of a JSON number used in "{k}={v}" — ints plain, floats repr-like.
std::string number_str(const ojson& v) {
    if (v.is_number_integer()) return std::to_string(v.get<long long>());
    if (v.is_number_unsigned()) return std::to_string(v.get<unsigned long long>());
    return dump_double(v.get<double>());
}

}  // namespace

std::string digest_params(const ojson& params, int limit) {
    std::vector<std::string> out;
    if (params.is_object()) {
        int i = 0;
        for (auto it = params.begin(); it != params.end() && i < limit; ++it, ++i) {
            const ojson& v = it.value();
            if (v.is_object() && v.contains("value")) {
                std::string vs;
                const ojson& vv = v["value"];
                // Python f"{v['value']}" — str() of the value.
                if (vv.is_string()) vs = vv.get<std::string>();
                else if (vv.is_boolean()) vs = vv.get<bool>() ? "True" : "False";
                else if (vv.is_null()) vs = "None";
                else if (vv.is_number()) vs = number_str(vv);
                else vs = vv.dump();
                out.push_back(it.key() + "=" + vs);
            } else if (v.is_number_integer() || v.is_number_unsigned() ||
                       v.is_number_float()) {
                // Note: Python isinstance(v,(int,float)) — bool is a subclass of
                // int, but nlohmann separates bool, so bool falls through here as
                // not-number; Python would format True as "k=True". Handle bool:
                out.push_back(it.key() + "=" + number_str(v));
            } else if (v.is_boolean()) {
                out.push_back(it.key() + "=" + (v.get<bool>() ? "True" : "False"));
            }
        }
    }
    std::string joined;
    for (size_t i = 0; i < out.size(); ++i) {
        if (i) joined += ", ";
        joined += out[i];
    }
    return joined;
}

std::string digest_tree(const ojson& tree, int max_lines) {
    ojson feats = ojson::array();
    if (tree.is_object() && tree.contains("features") && tree["features"].is_array())
        feats = tree["features"];

    std::vector<std::string> lines;
    int n = static_cast<int>(feats.size());
    int upto = std::min(n, max_lines);
    for (int i = 0; i < upto; ++i) {
        const ojson& f = feats[i];
        if (!f.is_object()) continue;
        std::string sup =
            (f.contains("suppressed") && truthy(f["suppressed"])) ? " [supp]" : "";
        std::string id = f.contains("id") && !f["id"].is_null()
                             ? (f["id"].is_string() ? f["id"].get<std::string>() : f["id"].dump())
                             : "?";
        std::string type = f.contains("type") && !f["type"].is_null()
                               ? (f["type"].is_string() ? f["type"].get<std::string>()
                                                        : f["type"].dump())
                               : "?";
        std::string name = f.contains("name") && !f["name"].is_null()
                               ? (f["name"].is_string() ? f["name"].get<std::string>()
                                                        : f["name"].dump())
                               : "";
        std::string line = rstrip(id + " " + type + " " + name + sup);
        ojson params = (f.contains("params") && !f["params"].is_null()) ? f["params"]
                                                                       : ojson::object();
        std::string dims = digest_params(params);
        if (!dims.empty()) line += " (" + dims + ")";
        lines.push_back(line);
    }
    if (n > max_lines)
        lines.push_back("... (+" + std::to_string(n - max_lines) + " more features)");
    if (lines.empty()) return "(empty — no features yet)";
    std::string joined;
    for (size_t i = 0; i < lines.size(); ++i) {
        if (i) joined += "\n";
        joined += lines[i];
    }
    return joined;
}

namespace {
const char* kEditWords[] = {
    "make ",   "change",   "set ",      "resize",  "increase", "decrease",
    "widen",   "narrow",   "shorten",   "lengthen","thicker",  "thinner",
    "wider",   "taller",   "shorter",   "deeper",  "shallower","bigger",
    "smaller", "larger",   "edit",      "modify",  "update",   "adjust",
    "rename",  "instead",  "should be", "reduce",  "enlarge",  "scale",
    "move ",   "remove",   "delete",
};
}  // namespace

bool looks_like_edit(const std::string& request) {
    std::string r = " " + to_lower(request) + " ";
    for (const char* w : kEditWords)
        if (r.find(w) != std::string::npos) return true;
    return false;
}

std::vector<std::string> feature_status_errors(const ojson& feature_status) {
    std::vector<std::string> out;
    if (!feature_status.is_array()) return out;
    for (const auto& s : feature_status) {
        if (!s.is_object()) continue;
        bool ok = s.contains("ok") && truthy(s["ok"]);
        bool has_err = s.contains("error") && truthy(s["error"]);
        if (!ok && has_err) {
            std::string id = s.contains("id") && !s["id"].is_null()
                                 ? (s["id"].is_string() ? s["id"].get<std::string>()
                                                        : s["id"].dump())
                                 : "?";
            std::string err = s["error"].is_string() ? s["error"].get<std::string>()
                                                     : s["error"].dump();
            out.push_back(id + ": " + err);
        }
    }
    return out;
}

const char* kEditBiasText =
    "EDIT BIAS: the document ALREADY HAS the features listed above (ids, names and current "
    "params shown). This request modifies something that exists — PREFER `edit_feature` on the "
    "existing feature's id over `add_feature`. Example: \"make the plate 60mm wide\" => "
    "[{\"op\":\"edit_feature\",\"id\":\"<the plate feature id>\",\"params\":{\"width\":{\"value\":60,"
    "\"expr\":null}}}] — do NOT add a second plate. Use `delete_feature` with the feature id for "
    "removals. Only use add_feature for genuinely NEW geometry.";

// --------------------------------------------------------------------------- block texts
namespace {

// Python truthiness for a JSON value (used where Python does `if x`).
bool jtruthy(const ojson& v) {
    if (v.is_null()) return false;
    if (v.is_boolean()) return v.get<bool>();
    if (v.is_string()) return !v.get<std::string>().empty();
    if (v.is_number_integer()) return v.get<long long>() != 0;
    if (v.is_number_unsigned()) return v.get<unsigned long long>() != 0;
    if (v.is_number_float()) return v.get<double>() != 0.0;
    if (v.is_array() || v.is_object()) return !v.empty();
    return false;
}

std::string str_of(const ojson& v) {
    if (v.is_string()) return v.get<std::string>();
    if (v.is_boolean()) return v.get<bool>() ? "True" : "False";
    if (v.is_null()) return "None";
    if (v.is_number()) return number_str(v);
    return v.dump();
}

std::string memory_text(const ojson& memory) {
    if (!memory.is_object() || memory.empty()) return "";
    std::vector<std::string> parts;
    // decisions
    ojson decisions = memory.contains("decisions") ? memory["decisions"] : ojson(nullptr);
    if (jtruthy(decisions) && decisions.is_array()) {
        std::string s = "Prior decisions: ";
        int i = 0;
        for (auto it = decisions.begin(); it != decisions.end() && i < 8; ++it, ++i) {
            if (i) s += "; ";
            s += str_of(*it);
        }
        parts.push_back(s);
    }
    // open_issues filtered for truthy
    ojson issues = memory.contains("open_issues") ? memory["open_issues"] : ojson(nullptr);
    std::vector<ojson> filt;
    if (issues.is_array())
        for (const auto& i : issues)
            if (jtruthy(i)) filt.push_back(i);
    if (!filt.empty()) {
        std::string s = "Known open issues: ";
        int i = 0;
        for (auto it = filt.begin(); it != filt.end() && i < 4; ++it, ++i) {
            if (i) s += "; ";
            s += str_of(*it);
        }
        parts.push_back(s);
    }
    // summary -> inserted at front
    ojson summary = memory.contains("summary") ? memory["summary"] : ojson(nullptr);
    if (jtruthy(summary))
        parts.insert(parts.begin(), "Memory: " + str_of(summary));
    std::string joined;
    for (size_t i = 0; i < parts.size(); ++i) {
        if (i) joined += "\n";
        joined += parts[i];
    }
    return joined;
}

std::string spec_text(const ojson& spec) {
    return "SPEC:\n" + py_json_dumps_indent0(spec);
}

std::string plan_text(const ojson& plan, bool compact) {
    if (compact) {
        std::string names;
        bool first = true;
        ojson parts = (plan.is_object() && plan.contains("parts")) ? plan["parts"] : ojson::array();
        if (parts.is_array())
            for (const auto& p : parts) {
                if (!first) names += ", ";
                first = false;
                std::string nm = "?";
                if (p.is_object() && p.contains("name") && !p["name"].is_null())
                    nm = p["name"].is_string() ? p["name"].get<std::string>() : p["name"].dump();
                names += nm;
            }
        std::string name;
        if (plan.is_object() && plan.contains("name") && !plan["name"].is_null())
            name = plan["name"].is_string() ? plan["name"].get<std::string>() : plan["name"].dump();
        return "PLAN: " + name + " — parts: " + names;
    }
    return "PLAN:\n" + py_json_dumps_indent0(plan);
}

bool has_features(const DesignState& state) {
    if (state.tree_digest.empty()) return false;
    return state.tree_digest.rfind("(empty", 0) != 0;
}

std::string join_lines(const std::vector<std::string>& v) {
    std::string s;
    for (size_t i = 0; i < v.size(); ++i) {
        if (i) s += "\n";
        s += v[i];
    }
    return s;
}

// Returns ordered (key, text) pairs already filtered to non-empty texts.
std::vector<std::pair<std::string, std::string>> role_blocks(const DesignState& state,
                                                             const std::string& role) {
    std::string req = "REQUEST:\n" + state.request;
    std::string mem = memory_text(state.memory);
    std::string tree =
        !state.tree_digest.empty() ? "CURRENT FEATURE TREE:\n" + state.tree_digest : "";
    std::string errs =
        !state.last_errors.empty() ? "ERRORS:\n" + join_lines(state.last_errors) : "";
    std::string issues =
        !state.last_issues.empty() ? "REVIEW ISSUES:\n" + join_lines(state.last_issues) : "";
    std::string bias = (has_features(state) && looks_like_edit(state.request)) ? kEditBiasText : "";

    std::vector<std::pair<std::string, std::string>> blocks;
    if (role == "intake") {
        blocks = {{"opt:mem", mem}, {"req", req}};
    } else if (role == "planner") {
        std::string spec = jtruthy(state.spec) ? spec_text(state.spec) : "";
        blocks = {{"opt:mem", mem}, {"req", req}, {"spec", spec}};
    } else if (role == "modeler") {
        std::string plan = jtruthy(state.plan) ? plan_text(state.plan, false) : "";
        std::string step =
            !state.current_step.empty() ? "STEP TO IMPLEMENT NOW:\n" + state.current_step : "";
        blocks = {{"opt:mem", mem}, {"opt:plan", plan}, {"req", req},
                  {"tree", tree},   {"bias", bias},     {"step", step},
                  {"errors", errs}};
    } else if (role == "reviewer") {
        std::string plan = jtruthy(state.plan) ? plan_text(state.plan, true) : "";
        blocks = {{"opt:mem", mem}, {"opt:plan", plan}, {"req", req}, {"tree", tree}};
    } else if (role == "fixer") {
        std::string plan = jtruthy(state.plan) ? plan_text(state.plan, true) : "";
        std::string prob = !errs.empty() ? errs : issues;
        blocks = {{"opt:mem", mem}, {"opt:plan", plan}, {"req", req},
                  {"tree", tree},   {"bias", bias},     {"problem", prob}};
    } else if (role == "memory") {
        std::string plan = jtruthy(state.plan) ? plan_text(state.plan, true) : "";
        blocks = {{"opt:plan", plan}, {"req", req}, {"tree", tree}};
    } else {
        blocks = {{"req", req}};
    }
    std::vector<std::pair<std::string, std::string>> out;
    for (auto& kv : blocks)
        if (!kv.second.empty()) out.push_back(kv);
    return out;
}

std::string join_body(const std::vector<std::pair<std::string, std::string>>& blocks) {
    std::vector<std::string> nonempty;
    for (auto& kv : blocks)
        if (!kv.second.empty()) nonempty.push_back(kv.second);
    std::string s;
    for (size_t i = 0; i < nonempty.size(); ++i) {
        if (i) s += "\n\n";
        s += nonempty[i];
    }
    return s;
}

}  // namespace

BuiltContext build_context(const DesignState& state, const std::string& role,
                           const std::string& prompts_dir) {
    auto pr = load_prompt(role, prompts_dir);
    std::string system = pr.first;

    auto blocks = role_blocks(state, role);

    int sys_tokens = est_tokens(system);
    while (!blocks.empty()) {
        std::string body = join_body(blocks);
        int total = sys_tokens + est_tokens(body);
        if (total <= kContextTokenCap) break;
        // Drop the first opt: block, else hard-truncate the largest essential.
        int opt_idx = -1;
        for (size_t i = 0; i < blocks.size(); ++i)
            if (blocks[i].first.rfind("opt:", 0) == 0) { opt_idx = static_cast<int>(i); break; }
        if (opt_idx < 0) {
            size_t largest = 0;
            for (size_t i = 1; i < blocks.size(); ++i)
                if (blocks[i].second.size() > blocks[largest].second.size()) largest = i;
            int keep = std::max(200, (kContextTokenCap - sys_tokens) * kCharsPerToken - 200);
            std::string& t = blocks[largest].second;
            if (static_cast<int>(t.size()) > keep) t = t.substr(0, keep);
            t += "\n...[truncated]";
            break;
        }
        blocks.erase(blocks.begin() + opt_idx);
    }

    std::string body = join_body(blocks);
    BuiltContext out;
    out.system = system;
    out.body = body;
    // Python: len(system) + len(body) — Unicode code points, not bytes.
    out.context_chars = utf8_len(system) + utf8_len(body);
    out.context_tokens_est = est_tokens(system) + est_tokens(body);
    return out;
}

}  // namespace agent
}  // namespace aicad
