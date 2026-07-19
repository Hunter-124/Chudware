// core/agent/micro.cpp — see micro.h. Mirrors agent/orchestrator/micro.py (P54a).
#include "agent/micro.h"

#include <algorithm>
#include <cctype>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <functional>
#include <optional>
#include <sstream>
#include <memory>
#include "agent/machine.h"

#include "agent/config.h"
#include "agent/events.h"
#include "agent/ops_emit.h"
#include "agent/providers.h"
#include "agent/roles.h"
#include "agent/state.h"
#include "doc/session.h"
#include "agent/domain.h"
#include "agent/complexity.h"

namespace aicad {
namespace agent {

namespace {

// ----------------------------------------------------------------- verbatim prompt text

// Wave D edit-bias strings — copied BYTE-FOR-BYTE from micro.py.
const char* kDecomposeEditBias =
    "EDIT BIAS: the document above ALREADY HAS those features. If the request modifies one of "
    "them, emit a step that EDITS it — name the existing feature id in params (e.g. "
    "{\"type\":\"box\",\"desc\":\"edit f1: set width to 60\",\"params\":{\"feature_id\":\"f1\","
    "\"width\":60}}) "
    "— do NOT emit a step that rebuilds it from scratch.";

const char* kExecEditBias =
    "EDIT BIAS: the CURRENT DOC above already has features (ids + current params shown). Act on the "
    "EXISTING feature by id, do NOT rebuild it:\n"
    "- change a dimension -> edit_feature: [{\"op\":\"edit_feature\",\"id\":\"f1\",\"params\":"
    "{\"width\":{\"value\":60,\"expr\":null}}}]\n"
    "- remove a feature   -> delete_feature: [{\"op\":\"delete_feature\",\"id\":\"f2\",\"cascade\":true}]\n"
    "- turn one off/on    -> set_suppressed: [{\"op\":\"set_suppressed\",\"id\":\"f2\",\"value\":true}]\n"
    "Never emit a duplicate add_feature for a feature that already exists.";

const char* kExecWorkflowReminder =
    "LOCAL WORKFLOW: emit one JSON doc-op array for only this step. Use existing ids from "
    "CURRENT DOC. If a required detail is missing, emit [] rather than inventing geometry.";

// ----------------------------------------------------------------- small helpers

bool py_is_space(char c) {
    return c == ' ' || c == '\t' || c == '\n' || c == '\r' || c == '\f' || c == '\v';
}
std::string py_strip(const std::string& s) {
    size_t a = 0, b = s.size();
    while (a < b && py_is_space(s[a])) ++a;
    while (b > a && py_is_space(s[b - 1])) --b;
    return s.substr(a, b - a);
}
std::string py_rstrip(const std::string& s) {
    size_t b = s.size();
    while (b > 0 && py_is_space(s[b - 1])) --b;
    return s.substr(0, b);
}
std::string to_lower(const std::string& s) {
    std::string r;
    r.reserve(s.size());
    for (char c : s) r += static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    return r;
}

// Python len(str) = unicode code points. Count UTF-8 lead bytes.
int utf8_len(const std::string& s) {
    int n = 0;
    for (unsigned char c : s)
        if ((c & 0xC0) != 0x80) ++n;
    return n;
}

// Python slice s[:max_chars] over CODE POINTS (len() and slicing are codepoint-based).
// Returns the prefix containing the first `max_chars` code points.
std::string utf8_prefix(const std::string& s, int max_chars) {
    if (max_chars <= 0) return "";
    int count = 0;
    size_t i = 0;
    while (i < s.size() && count < max_chars) {
        unsigned char c = static_cast<unsigned char>(s[i]);
        size_t adv = 1;
        if ((c & 0x80) == 0) adv = 1;
        else if ((c & 0xE0) == 0xC0) adv = 2;
        else if ((c & 0xF0) == 0xE0) adv = 3;
        else if ((c & 0xF8) == 0xF0) adv = 4;
        i += adv;
        ++count;
    }
    return s.substr(0, i);
}

// ----------------------------------------------------------------- json.dumps (DEFAULT separators)
//
// micro.py uses plain json.dumps(...) here (NOT the indent0 variant), so the
// separators are the DEFAULTS: item ", " and key ": ", no newlines, ensure_ascii.

void dump_string_ascii(const std::string& s, std::string& out) {
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
        if (c < 0x80) { out += static_cast<char>(c); ++i; continue; }
        unsigned int cp = 0;
        int extra = 0;
        if ((c & 0xE0) == 0xC0) { cp = c & 0x1F; extra = 1; }
        else if ((c & 0xF0) == 0xE0) { cp = c & 0x0F; extra = 2; }
        else if ((c & 0xF8) == 0xF0) { cp = c & 0x07; extra = 3; }
        else { cp = c; extra = 0; }
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

// CPython float_repr -> json.dumps float text (reused logic; matches state::dump_double).
std::string dump_double_py(double v) {
    if (std::isnan(v)) return "NaN";
    if (std::isinf(v)) return v < 0 ? "-Infinity" : "Infinity";
    if (v == 0.0) return std::signbit(v) ? "-0.0" : "0.0";
    char buf[64];
    int digits10 = 17;
    for (int prec = 0; prec <= 16; ++prec) {
        std::snprintf(buf, sizeof(buf), "%.*e", prec, v);
        if (std::strtod(buf, nullptr) == v) { digits10 = prec + 1; break; }
    }
    std::snprintf(buf, sizeof(buf), "%.*e", digits10 - 1, v);
    std::string sci(buf);
    bool neg = false;
    size_t p = 0;
    if (sci[p] == '-') { neg = true; ++p; }
    std::string mant_digits;
    mant_digits += sci[p++];
    if (p < sci.size() && sci[p] == '.') {
        ++p;
        while (p < sci.size() && sci[p] != 'e' && sci[p] != 'E') mant_digits += sci[p++];
    }
    int decexp = 0;
    {
        size_t ep = sci.find_first_of("eE");
        decexp = std::atoi(sci.c_str() + ep + 1);
    }
    while (mant_digits.size() > 1 && mant_digits.back() == '0') mant_digits.pop_back();
    int ndig = static_cast<int>(mant_digits.size());
    std::string out;
    if (decexp < -4 || decexp >= 16) {
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
        out += "0.";
        out.append(-decexp - 1, '0');
        out += mant_digits;
    }
    return neg ? "-" + out : out;
}

void dump_default(const ojson& j, std::string& out) {
    switch (j.type()) {
        case ojson::value_t::null: out += "null"; break;
        case ojson::value_t::boolean: out += j.get<bool>() ? "true" : "false"; break;
        case ojson::value_t::string: dump_string_ascii(j.get<std::string>(), out); break;
        case ojson::value_t::number_integer: out += std::to_string(j.get<long long>()); break;
        case ojson::value_t::number_unsigned:
            out += std::to_string(j.get<unsigned long long>());
            break;
        case ojson::value_t::number_float: out += dump_double_py(j.get<double>()); break;
        case ojson::value_t::array: {
            out += "[";
            bool first = true;
            for (const auto& e : j) {
                if (!first) out += ", ";
                first = false;
                dump_default(e, out);
            }
            out += "]";
            break;
        }
        case ojson::value_t::object: {
            out += "{";
            bool first = true;
            for (auto it = j.begin(); it != j.end(); ++it) {
                if (!first) out += ", ";
                first = false;
                dump_string_ascii(it.key(), out);
                out += ": ";
                dump_default(it.value(), out);
            }
            out += "}";
            break;
        }
        default: out += "null"; break;
    }
}

// json.dumps(obj) with DEFAULT separators (", " / ": ").
std::string py_json_dumps_default_local(const ojson& obj) {
    std::string out;
    dump_default(obj, out);
    return out;
}

// Python str(value) for a scalar inside an f-string "{v}".
std::string py_scalar_str(const ojson& v) {
    if (v.is_string()) return v.get<std::string>();
    if (v.is_boolean()) return v.get<bool>() ? "True" : "False";
    if (v.is_null()) return "None";
    if (v.is_number_integer()) return std::to_string(v.get<long long>());
    if (v.is_number_unsigned()) return std::to_string(v.get<unsigned long long>());
    if (v.is_number_float()) return dump_double_py(v.get<double>());
    return v.dump();
}

// ----------------------------------------------------------------- key dims (summarize_doc)

const std::vector<std::string> kScalarKeys = {
    "radius", "length",  "width", "height",   "depth",    "count", "count1", "count2",
    "angle",  "thickness", "mode",  "end",      "style",   "standard"};

bool is_scalar_key(const std::string& k) {
    for (const auto& w : kScalarKeys)
        if (k == w) return true;
    return false;
}

std::string key_dims(const ojson& params) {
    std::vector<std::string> out;
    if (params.is_object()) {
        int i = 0;
        for (auto it = params.begin(); it != params.end() && i < 6; ++it, ++i) {
            const ojson& v = it.value();
            if (v.is_object() && v.contains("value")) {
                out.push_back(it.key() + "=" + py_scalar_str(v["value"]));
            } else if ((v.is_number_integer() || v.is_number_unsigned() || v.is_number_float() ||
                        v.is_string()) &&
                       is_scalar_key(it.key())) {
                out.push_back(it.key() + "=" + py_scalar_str(v));
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

// Python truthiness for a JSON value.
bool truthy(const ojson& v) {
    if (v.is_null()) return false;
    if (v.is_boolean()) return v.get<bool>();
    if (v.is_string()) return !v.get<std::string>().empty();
    if (v.is_number_integer()) return v.get<long long>() != 0;
    if (v.is_number_unsigned()) return v.get<unsigned long long>() != 0;
    if (v.is_number_float()) return v.get<double>() != 0.0;
    if (v.is_array() || v.is_object()) return !v.empty();
    return false;
}

std::string str_of_feature_field(const ojson& f, const char* key, const char* dflt) {
    if (f.contains(key) && !f[key].is_null())
        return f[key].is_string() ? f[key].get<std::string>() : f[key].dump();
    return dflt;
}

// Python format f"{vol:.0f}" — round-half-to-even to integer, then no fractional digits.
std::string fmt_f0(double v) {
    char buf[64];
    std::snprintf(buf, sizeof(buf), "%.0f", v);
    // printf %.0f rounds half-to-even on glibc/MSVC; Python format(:.0f) also half-to-even.
    return std::string(buf);
}

bool doc_has_features(const std::string& doc_summary) {
    return !doc_summary.empty() && doc_summary.find("(empty") == std::string::npos;
}

// ----------------------------------------------------------------- _is_cut

bool is_cut(const Step& step) {
    std::string mode;
    if (step.params.is_object() && step.params.contains("mode"))
        mode = to_lower(py_scalar_str(step.params["mode"]));
    if (mode == "cut") return true;
    if (step.type == "hole" || step.type == "bolt_circle") return true;
    return to_lower(step.desc).find("cut") != std::string::npos;
}

// ----------------------------------------------------------------- provider plumbing
//
// One PERSISTENT provider for the whole run (RoleScriptedProvider per-role queues
// must survive across calls). Mirrors run.cpp make_provider.

struct ProvReply {
    std::string text;
    std::string thinking;
    std::string model;
    bool has_error = false;
    std::string error;
};

using ProviderFn = std::function<ProvReply(const std::string& system, const json& messages,
                                           int max_tokens)>;

ProviderFn make_provider(const json& provider_cfg) {
    std::string name = provider_cfg.value("name", std::string());
    if (name == "mock" && provider_cfg.contains("replies") && provider_cfg["replies"].is_object()) {
        auto rp = std::make_shared<RoleScriptedProvider>(provider_cfg["replies"]);
        return [rp](const std::string& system, const json&, int) -> ProvReply {
            ProvReply r;
            r.text = rp->complete(system);
            return r;
        };
    }
    json cfg = provider_cfg;
    return [name, cfg](const std::string& system, const json& messages, int max_tokens) -> ProvReply {
        ProvReply r;
        CompleteResult cr = complete(name, cfg, system, messages, max_tokens);
        r.thinking = cr.thinking;
        r.model = !cr.model.empty() ? cr.model : cfg.value("model", std::string());
        if (!cr.error.empty()) {
            r.has_error = true;
            r.error = cr.error;
            r.text = cr.text;
            return r;
        }
        r.text = cr.text;
        return r;
    };
}

// _provider_call: one bounded call; returns (reply, duration_ms, error). Never raises.
struct CallResult {
    std::string reply;
    std::string thinking;
    std::string model;
    int duration_ms = 0;
    bool has_error = false;
    std::string error;
};
CallResult provider_call(const ProviderFn& provider, const std::string& system,
                         const json& messages, int max_tokens) {
    auto t0 = std::chrono::steady_clock::now();
    ProvReply rep = provider(system, messages, max_tokens);
    auto t1 = std::chrono::steady_clock::now();
    CallResult cr;
    cr.duration_ms =
        static_cast<int>(std::chrono::duration_cast<std::chrono::milliseconds>(t1 - t0).count());
    cr.thinking = rep.thinking;
    cr.model = rep.model;
    if (rep.has_error) {
        cr.has_error = true;
        cr.error = rep.error;
        cr.reply = "";
    } else {
        cr.reply = rep.text;
    }
    return cr;
}

// Build a one-user-message body json (base.user([text_part(text)])).
json user_body(const std::string& text) {
    return json::array({user_message(json::array({text_part(text)}))});
}

// Read the live doc once: {tree, revision, feature_status, body_count, volume}.
struct LiveDoc {
    ojson tree;
    int revision = 0;
    ojson feature_status = ojson::array();  // latest build's feature_status rows
    int body_count = 0;
    double volume = 0.0;
};

LiveDoc read_live(const std::string& doc_id) {
    LiveDoc d;
    json rb = doc::doc_rebuild(doc_id);
    d.revision = rb.value("revision", 0);
    d.tree = ojson::parse((rb.contains("tree") ? rb["tree"] : json::object()).dump());
    if (rb.contains("rebuild") && rb["rebuild"].contains("feature_status"))
        d.feature_status = ojson::parse(rb["rebuild"]["feature_status"].dump());
    if (rb.contains("tess") && rb["tess"].contains("bodies") && rb["tess"]["bodies"].is_array())
        d.body_count = static_cast<int>(rb["tess"]["bodies"].size());
    d.volume = rb.value("volume", 0.0);
    return d;
}

}  // namespace

// --------------------------------------------------------------------------- engagement

bool should_engage(const json& provider_cfg, const json& cfg) {
    // micro_mode: True/"true" -> True; False/"false" -> False; "auto" (default) -> compute.
    json mode = cfg.is_object() && cfg.contains("micro_mode") ? cfg["micro_mode"] : json("auto");
    if (mode.is_boolean()) {
        if (mode.get<bool>()) return true;
        // mode == false -> return false.
        return false;
    }
    if (mode.is_string()) {
        std::string ml = to_lower(mode.get<std::string>());
        if (ml == "true") return true;
        if (ml == "false") return false;
    }
    // auto: local provider AND small context.
    // _provider_is_local
    std::string name = provider_cfg.value("name", std::string());
    bool is_local = false;
    if (name == "mock" || name == "mock-roles") {
        is_local = true;
    } else if (name == "openai" || name == "llamacpp") {
        // base_url may be present-but-NULL (openai with no override) — value() would throw
        // type_error.302 on a null, so read it null-safely.
        std::string base_url = provider_cfg.contains("base_url") &&
                                       provider_cfg["base_url"].is_string()
                                   ? provider_cfg["base_url"].get<std::string>()
                                   : std::string();
        is_local = host_is_loopback(base_url);
    }
    // auto mode: ANY local provider uses the limited (micro) modeling path. Local models — even
    // large-context local builds (e.g. a 26B loaded at 32k ctx) — do far better on the lightweight
    // decompose -> execute micro flow than the heavier intake/plan/model machine path, so locality
    // ALONE engages it. (A previous ctx>16000 carve-out used to bump big-context local models onto
    // the machine path; it's intentionally gone — a local endpoint always means the limited path.)
    // To force the full machine path regardless, set cfg.micro_mode=false explicitly.
    return is_local;  // is_local is true here (we returned false above otherwise)
}

// --------------------------------------------------------------------------- JSON array extract

ojson extract_json_array(const std::string& reply) {
    // Each fenced ```json (or untagged json-tagged) block -> parse.
    std::vector<std::string> bodies;
    {
        // _FENCE_RE = ```(?:json|JSON)?\s*\n(.*?)``` (DOTALL). Reuse ops_emit's scan
        // indirectly is not exposed; replicate the simple fence scan here.
        const std::string fence = "```";
        size_t pos = 0;
        const size_t n = reply.size();
        const std::vector<std::string> tags = {"json", "JSON"};
        while (pos < n) {
            size_t open = reply.find(fence, pos);
            if (open == std::string::npos) break;
            size_t cur = open + fence.size();
            for (const std::string& tag : tags)
                if (reply.compare(cur, tag.size(), tag) == 0) { cur += tag.size(); break; }
            size_t ws_start = cur, ws = cur;
            while (ws < n && py_is_space(reply[ws])) ++ws;
            size_t last_nl = std::string::npos;
            for (size_t i = ws_start; i < ws; ++i)
                if (reply[i] == '\n') last_nl = i;
            if (last_nl == std::string::npos) { pos = open + fence.size(); continue; }
            size_t body_start = last_nl + 1;
            size_t close = reply.find(fence, body_start);
            if (close == std::string::npos) { pos = open + fence.size(); continue; }
            bodies.push_back(reply.substr(body_start, close - body_start));
            pos = close + fence.size();
        }
    }
    for (const std::string& b : bodies) {
        std::string trimmed = py_strip(b);
        ojson data = ojson::parse(trimmed, nullptr, false);
        if (data.is_discarded()) continue;
        if (data.is_array()) return data;
        if (data.is_object() && data.contains("steps") && data["steps"].is_array())
            return data["steps"];
    }
    // Bare first-balanced [..].
    std::optional<std::string> bare = first_balanced(reply, '[', ']');
    if (bare) {
        ojson data = ojson::parse(*bare, nullptr, false);
        if (!data.is_discarded() && data.is_array()) return data;
    }
    return ojson(nullptr);
}

// --------------------------------------------------------------------------- step model

std::vector<Step> normalize_steps(const ojson& raw) {
    std::vector<Step> steps;
    if (!raw.is_array()) return steps;
    int taken = 0;
    for (auto it = raw.begin(); it != raw.end() && taken < kMaxSteps; ++it, ++taken) {
        const ojson& item = *it;
        if (!item.is_object()) continue;
        // ftype = str(item.get("type") or "").strip().lower()
        std::string ftype;
        if (item.contains("type") && truthy(item["type"]))
            ftype = item["type"].is_string() ? item["type"].get<std::string>()
                                              : py_scalar_str(item["type"]);
        ftype = to_lower(py_strip(ftype));
        if (ftype.empty()) continue;
        // desc = str(item.get("desc") or item.get("description") or "").strip()
        std::string desc;
        if (item.contains("desc") && truthy(item["desc"]))
            desc = item["desc"].is_string() ? item["desc"].get<std::string>()
                                             : py_scalar_str(item["desc"]);
        else if (item.contains("description") && truthy(item["description"]))
            desc = item["description"].is_string() ? item["description"].get<std::string>()
                                                    : py_scalar_str(item["description"]);
        desc = py_strip(desc);
        ojson params = ojson::object();
        if (item.contains("params") && item["params"].is_object()) params = item["params"];
        Step s;
        s.type = ftype;
        s.desc = desc;
        s.params = params;
        steps.push_back(std::move(s));
    }
    return steps;
}

// --------------------------------------------------------------------------- doc summary

std::optional<double> total_volume(int body_count, double volume) {
    if (body_count <= 0) return std::nullopt;
    return volume;
}

std::string summarize_doc(const ojson& tree, const std::optional<double>& vol, int budget_tokens) {
    ojson feats = ojson::array();
    if (tree.is_object() && tree.contains("features") && tree["features"].is_array())
        feats = tree["features"];
    std::vector<std::string> lines;
    for (const auto& f : feats) {
        if (!f.is_object()) continue;
        ojson params =
            (f.contains("params") && f["params"].is_object()) ? f["params"] : ojson::object();
        std::string dims = key_dims(params);
        std::string sup = (f.contains("suppressed") && truthy(f["suppressed"])) ? " [supp]" : "";
        std::string id = str_of_feature_field(f, "id", "?");
        std::string type = str_of_feature_field(f, "type", "?");
        std::string name = str_of_feature_field(f, "name", "");
        std::string line = py_rstrip(id + " " + type + " " + name + sup);
        if (!dims.empty()) line += " (" + dims + ")";
        lines.push_back(line);
    }
    std::string body;
    if (lines.empty()) {
        body = "(empty — no features yet)";
    } else {
        for (size_t i = 0; i < lines.size(); ++i) {
            if (i) body += "\n";
            body += lines[i];
        }
    }
    std::string head = "CURRENT DOC:";
    if (vol) head += " volume=" + fmt_f0(*vol) + " mm^3";
    std::string text = head + "\n" + body;
    int max_chars = budget_tokens * 4;
    if (utf8_len(text) > max_chars) {
        text = utf8_prefix(text, max_chars) + "\n...[doc truncated]";
    }
    return text;
}

// --------------------------------------------------------------------------- prompts

std::string build_decompose_prompt(const std::string& prompts_dir) {
    auto pr = load_prompt("decomposer", prompts_dir);
    // ASSERT est_tokens(system) <= 400 (the oracle asserts; we trust the shipped prompt).
    return pr.first;
}

ExecutorPrompt build_executor_prompt(const Step& step, const std::string& doc_summary_in,
                                     const std::string& request, const std::string& error_feedback,
                                     const std::string& prompts_dir,
                                     int input_cap = kExecPromptBudgetTokens) {
    auto pr = load_prompt("executor", prompts_dir);
    std::string base_system = pr.first;
    std::string cheat = load_cheatsheet(step.type, prompts_dir);
    std::string system = py_rstrip(base_system);
    if (!cheat.empty()) system += "\n\n---\nCHEAT-SHEET for this step:\n" + cheat;

    std::string step_text = "STEP TO DO NOW:\ntype: " + step.type + "\n" + step.desc;
    if (!step.params.empty()) step_text += "\nparams: " + py_json_dumps_default_local(step.params);

    std::string doc_summary = doc_summary_in;
    // parts: [REQUEST, doc_summary, step_text, workflow] (+ edit-bias) (+ error).
    auto build_parts = [&]() -> std::vector<std::string> {
        std::vector<std::string> parts;
        parts.push_back("REQUEST (overall): " + request);
        parts.push_back(doc_summary);
        parts.push_back(step_text);
        parts.push_back(kExecWorkflowReminder);
        if (doc_has_features(doc_summary) && looks_like_edit(request)) parts.push_back(kExecEditBias);
        if (!error_feedback.empty())
            parts.push_back("PREVIOUS ATTEMPT FAILED — fix this and re-emit the op array:\n" +
                            error_feedback);
        return parts;
    };
    auto join2 = [](const std::vector<std::string>& parts) -> std::string {
        std::string s;
        for (size_t i = 0; i < parts.size(); ++i) {
            if (i) s += "\n\n";
            s += parts[i];
        }
        return s;
    };

    std::string body = join2(build_parts());
    int sys_tok = est_tokens(system);
    // Budget remediation: truncate doc_summary FIRST until it fits.
    while (sys_tok + est_tokens(body) > input_cap && !doc_summary.empty()) {
        // micro.py: doc_summary[:max(40, len(doc_summary)//2)] — len() is code points.
        int cp_len = utf8_len(doc_summary);
        int keep = std::max(40, cp_len / 2);
        doc_summary = py_rstrip(utf8_prefix(doc_summary, keep)) + "\n...[doc trimmed]";
        body = join2(build_parts());
        if (doc_summary.find("[doc trimmed]") != std::string::npos &&
            utf8_len(doc_summary) <= 60)
            break;
    }
    ExecutorPrompt out;
    out.system = system;
    out.body = body;
    return out;
}

// --------------------------------------------------------------------------- verify

VerifyResult verify_step(const Step& step, const ojson& tree, const ojson& feature_status,
                         const std::optional<double>& vol_before,
                         const std::optional<double>& vol_after) {
    std::vector<std::string> notes;
    // 1) compile status of newest features.
    std::vector<const ojson*> bad;
    if (feature_status.is_array())
        for (const auto& s : feature_status)
            if (!(s.is_object() && s.contains("ok") && truthy(s["ok"]))) bad.push_back(&s);
    if (!bad.empty()) {
        std::string msg = "feature did not compile: ";
        int count = 0;
        for (const ojson* s : bad) {
            if (count >= 3) break;
            if (count) msg += "; ";
            std::string id = (s->is_object() && s->contains("id") && !(*s)["id"].is_null())
                                 ? ((*s)["id"].is_string() ? (*s)["id"].get<std::string>()
                                                           : (*s)["id"].dump())
                                 : "None";
            std::string err;
            if (s->is_object() && s->contains("error") && !(*s)["error"].is_null())
                err = (*s)["error"].is_string() ? (*s)["error"].get<std::string>()
                                                : (*s)["error"].dump();
            else
                err = "None";
            msg += id + ": " + err;
            ++count;
        }
        return {false, msg};
    }

    // 2) volume direction.
    if (vol_before && vol_after) {
        bool removes = step.type == "shell" || is_cut(step);
        bool in_adds_set =
            (step.type == "box" || step.type == "cylinder" || step.type == "sphere" ||
             step.type == "cone" || step.type == "extrude" || step.type == "revolve" ||
             step.type == "loft" || step.type == "sweep" ||
             step.type == "linear_pattern" || step.type == "circular_pattern");
        bool adds = in_adds_set && !removes;
        double delta = *vol_after - *vol_before;
        if (removes && delta >= -1e-6 && *vol_before > 0) {
            notes.push_back("expected volume to DROP (cut/shell) but it did not (Δ=" +
                            fmt_f0(delta) + ")");
            std::string joined;
            for (size_t i = 0; i < notes.size(); ++i) { if (i) joined += "; "; joined += notes[i]; }
            return {false, joined};
        }
        if (adds && *vol_before > 0 && delta <= 1e-6) {
            notes.push_back("expected volume to GROW (boss) but it did not (Δ=" + fmt_f0(delta) +
                            ")");
            std::string joined;
            for (size_t i = 0; i < notes.size(); ++i) { if (i) joined += "; "; joined += notes[i]; }
            return {false, joined};
        }
        notes.push_back("Δvol=" + fmt_f0(delta));
    }

    // 3) dims sanity (informational).
    ojson feats = ojson::array();
    if (tree.is_object() && tree.contains("features") && tree["features"].is_array())
        feats = tree["features"];
    if (!feats.empty()) {
        const ojson& last = feats[feats.size() - 1];
        std::string ltype = str_of_feature_field(last, "type", "");
        if (ltype == step.type) {
            std::string lid = str_of_feature_field(last, "id", "");
            // Python: f"added {last.get('type')} {last.get('id')}" — None -> "None".
            std::string tstr = (last.is_object() && last.contains("type") && !last["type"].is_null())
                                   ? ltype
                                   : "None";
            std::string istr = (last.is_object() && last.contains("id") && !last["id"].is_null())
                                   ? lid
                                   : "None";
            notes.push_back("added " + tstr + " " + istr);
        }
    }
    std::string joined;
    for (size_t i = 0; i < notes.size(); ++i) { if (i) joined += "; "; joined += notes[i]; }
    return {true, joined.empty() ? "ok" : joined};
}

// --------------------------------------------------------------------------- runner

json run_micro_design(const std::string& request, const std::string& doc_id,
                      const json& provider_cfg, EmitFnMicro emit, const json& options) {
    std::string run_id = options.value("run_id", std::string());
    if (run_id.empty())
        run_id = "run-" + std::to_string(static_cast<unsigned long long>(
                              std::chrono::steady_clock::now().time_since_epoch().count()));
    std::string prompts_dir = options.value("prompts_dir", std::string());

    RunControl run_ctl;
    register_run_control(run_id, run_ctl);
    struct RunCtlGuard {
        std::string id;
        ~RunCtlGuard() { unregister_run_control(id); }
    } run_ctl_guard{run_id};
    set_provider_cancel_flag(run_ctl.flag.get());
    struct ProvCancelGuard {
        ~ProvCancelGuard() { set_provider_cancel_flag(nullptr); }
    } prov_cancel_guard;

    RunEvents events(run_id, emit);
    // Live provider_call debug events → same orch stream as stage events.
    set_provider_call_emitter([&events](const json& ev) {
        json data = ev;
        std::string kind = "provider_call";
        if (data.contains("kind") && data["kind"].is_string()) {
            kind = data["kind"].get<std::string>();
            data.erase("kind");
        }
        events.emit(kind, data);
    });
    struct ProviderEmitGuard {
        ~ProviderEmitGuard() { set_provider_call_emitter(nullptr); }
    } provider_emit_guard;
    // Forward streaming token deltas to the run event stream so the GUI shows the
    // model's continuous output live (channel "text" | "thinking"). Deltas are
    // COALESCED per channel (flush at >=48 chars, newline, or >=60ms since last
    // flush) so we don't reopen events.jsonl per token — grok emits thousands.
    // The final assistant message is persisted separately, so a trailing sub-flush
    // buffer is not authoritative.
    struct DeltaCoalescer {
        RunEvents* ev;
        std::string buf_text, buf_think;
        std::chrono::steady_clock::time_point last_flush_text =
            std::chrono::steady_clock::now();
        std::chrono::steady_clock::time_point last_flush_think =
            std::chrono::steady_clock::now();
        std::string& chan(const std::string& kind) {
            return kind == "thinking" ? buf_think : buf_text;
        }
        std::chrono::steady_clock::time_point& last_flush(const std::string& kind) {
            return kind == "thinking" ? last_flush_think : last_flush_text;
        }
        void flush(const std::string& kind) {
            std::string& b = chan(kind);
            if (b.empty()) return;
            ev->emit("assistant_delta", json{{"text", b}, {"channel", kind}});
            b.clear();
            last_flush(kind) = std::chrono::steady_clock::now();
        }
        void push(const std::string& d, const std::string& kind) {
            std::string& b = chan(kind);
            b += d;
            const auto ms = std::chrono::duration_cast<std::chrono::milliseconds>(
                                std::chrono::steady_clock::now() - last_flush(kind))
                                .count();
            if (b.size() >= 48 || b.find('\n') != std::string::npos || ms >= 60)
                flush(kind);
        }
        void flush_all() { flush("text"); flush("thinking"); }
    };
    auto coalescer = std::make_shared<DeltaCoalescer>();
    coalescer->ev = &events;
    set_provider_delta_emitter([coalescer](const std::string& delta, const std::string& kind) {
        coalescer->push(delta, kind);
    });
    struct ProviderDeltaGuard {
        std::shared_ptr<DeltaCoalescer> c;
        ~ProviderDeltaGuard() { if (c) c->flush_all(); set_provider_delta_emitter(nullptr); }
    } provider_delta_guard{coalescer};

    // Offline-mode defense in depth (mirror run.cpp).
    bool offline = options.value("offline", false);
    if (offline && !provider_offline_safe(provider_cfg)) {
        int rev0 = doc::doc_tree(doc_id).value("revision", 0);
        events.emit("run_finished", json{{"status", "failed"},
                                         {"stop_reason", "offline_mode_refused"},
                                         {"revision", rev0}});
        return json{{"run_id", run_id},
                    {"status", "failed"},
                    {"stop_reason", "offline_mode_refused"},
                    {"revision", rev0},
                    {"doc_id", doc_id}};
    }

    auto cancelled = [&]() -> bool { return run_ctl.cancelled(); };

    ProviderFn provider = make_provider(provider_cfg);

    // Per-call output budget: the config "max_output_tokens" override (provider.max_tokens) wins,
    // else 8192 (reasoning headroom — see the decompose note below). The AI settings expose this
    // so a user can give a thinking-heavy local model more room when it returns empty content.
    int desired_out = 8192;
    if (provider_cfg.contains("max_tokens") && provider_cfg["max_tokens"].is_number_integer()) {
        int mt = provider_cfg["max_tokens"].get<int>();
        if (mt > 0) desired_out = mt < 512 ? 512 : mt;
    }
    // Fit output + input against the model's context window. On a small LOCAL window the 8192
    // reasoning budget alone overruns it (8192 out + ~2400 in > 8k), so the server truncates the
    // prompt and the model emits garbage. fit_call_budget shrinks BOTH so they fit; the executor
    // prompt is then bounded by the tighter of its own lean cap and the window-derived input cap.
    const CallBudget mcb = fit_call_budget(provider_cfg, desired_out);
    int micro_budget = mcb.output;
    int exec_input_cap = std::min(kExecPromptBudgetTokens, mcb.input_cap);

    auto rev_now = [&]() -> int { return doc::doc_tree(doc_id).value("revision", 0); };

    events.emit("run_started", json{{"request", request},
                                    {"doc_id", doc_id},
                                    {"stages", json::array({"decompose", "execute", "verify"})},
                                    {"profile", "micro"}});
    // Pre-flight classification (GUI "Routing" card) — same transparency as run_design so
    // the classifier is visible on the micro (local/small-model) profile too.
    {
        DomainVerdict mdom = classify_domain(request);
        ComplexityVerdict mcx = classify_complexity(request);
        std::string why = mdom.reason;
        if (!mcx.reason.empty()) {
            if (!why.empty()) why += " · ";
            why += mcx.reason;
        }
        const std::string cap_why = capability_request_reason(request);
        if (!cap_why.empty()) {
            if (!why.empty()) why += " · ";
            why += cap_why;
        }
        events.emit("agent.classification",
                    json{{"task_class", mdom.label()},
                         {"scaffold_level", mcx.label()},
                         {"tier", provider_cfg.value("model", std::string())},
                         {"reason", why},
                         {"request_snippet", request.substr(0, 120)},
                         {"picked_model", provider_cfg.value("model", std::string())},
                         {"picked_provider", provider_cfg.value("name", std::string())}});
    }

    // ---- DECOMPOSE ----
    events.emit("stage_started", json{{"stage", "decompose"}, {"role", "decomposer"},
                                      {"attempt", 1}, {"budget", 1}});
    std::string dsys = "ROLE: decomposer\n" + build_decompose_prompt(prompts_dir);
    LiveDoc live0 = read_live(doc_id);
    std::optional<double> vol0 = total_volume(live0.body_count, live0.volume);
    std::string doc_summary0 = summarize_doc(live0.tree, vol0);
    std::vector<std::string> dparts;
    dparts.push_back("REQUEST:\n" + request);
    if (doc_has_features(doc_summary0)) {
        dparts.push_back(doc_summary0);
        if (looks_like_edit(request)) dparts.push_back(kDecomposeEditBias);
    }
    dparts.push_back("Produce the step list json now.");
    std::string dbody;
    for (size_t i = 0; i < dparts.size(); ++i) { if (i) dbody += "\n\n"; dbody += dparts[i]; }

    events.emit("agent_call", json{{"stage", "decompose"}, {"role", "decomposer"}, {"attempt", 1},
                                   {"context_tokens_est", est_tokens(dsys) + est_tokens(dbody)}});
    // 8192 (not 1024/4096): reasoning models (gemma et al.) spend a LOT of tokens on hidden
    // reasoning_content BEFORE emitting any answer — bigger local models (26B) can burn 4096 on
    // reasoning alone and emit EMPTY content (finish=length) -> "no parseable step list". A larger
    // cap costs nothing on short replies (the model stops early) and lets reasoning+output both fit.
    CallResult dcall = provider_call(provider, dsys, user_body(dbody), micro_budget);
    std::string dsidecar = events.write_sidecar("decompose", 1, dcall.reply);
    std::vector<Step> steps = normalize_steps(extract_json_array(dcall.reply));

    {
        json ev = json{{"stage", "decompose"},  {"role", "decomposer"}, {"attempt", 1},
                       {"raw", utf8_prefix(dcall.reply, 4000)}, {"raw_path", dsidecar},
                       {"parsed_ok", !steps.empty()}, {"ops_count", static_cast<int>(steps.size())},
                       {"duration_ms", dcall.duration_ms}};
        events.emit("agent_output", ev);
    }
    if (!dcall.thinking.empty()) {
        emit_agent_thought(events, dcall.thinking, dcall.model, "decompose", 1,
                           /*parent_id=*/"", /*actor=*/"decomposer");
    }

    if (dcall.has_error) {
        std::string reason = "provider error: " + dcall.error;
        std::string human = humanize_provider_error(reason);
        events.emit("stage_result", json{{"stage", "decompose"},
                                         {"attempt", 1},
                                         {"verdict", "fail"},
                                         {"reason", reason},
                                         {"stop_reason", "provider_error"},
                                         {"error_detail", reason},
                                         {"error_human", human}});
        int rev = rev_now();
        events.emit("run_finished", json{{"status", "failed"},
                                         {"stop_reason", "provider_error"},
                                         {"error_detail", reason},
                                         {"error_human", human},
                                         {"revision", rev}});
        return json{{"run_id", run_id},
                    {"status", "failed"},
                    {"stop_reason", "provider_error"},
                    {"error_detail", reason},
                    {"error_human", human},
                    {"revision", rev},
                    {"doc_id", doc_id}};
    }
    if (steps.empty()) {
        events.emit("stage_result", json{{"stage", "decompose"}, {"attempt", 1},
                                         {"verdict", "fail"},
                                         {"reason", "no parseable step list"}});
        int rev = rev_now();
        events.emit("run_finished", json{{"status", "failed"},
                                         {"stop_reason", "decompose_failed"},
                                         {"revision", rev}});
        return json{{"run_id", run_id}, {"status", "failed"},
                    {"stop_reason", "decompose_failed"}, {"revision", rev}, {"doc_id", doc_id}};
    }
    events.emit("stage_result", json{{"stage", "decompose"}, {"attempt", 1}, {"verdict", "pass"},
                                     {"reason", std::to_string(steps.size()) + " step(s)"},
                                     {"step_count", static_cast<int>(steps.size())}});

    // ---- EXECUTE + VERIFY per step ----
    int n = static_cast<int>(steps.size());
    int applied_ok = 0;

    for (int i = 0; i < n; ++i) {
        const Step& step = steps[i];
        if (cancelled()) {
            int rev = rev_now();
            events.emit("run_finished", json{{"status", "cancelled"},
                                             {"stop_reason", "cancelled"}, {"revision", rev}});
            return json{{"run_id", run_id}, {"status", "cancelled"}, {"stop_reason", "cancelled"},
                        {"revision", rev}, {"doc_id", doc_id}};
        }
        LiveDoc before = read_live(doc_id);
        std::optional<double> vol_before = total_volume(before.body_count, before.volume);

        events.emit("stage_started",
                    json{{"stage", "execute"}, {"role", "executor"}, {"attempt", 1}, {"budget", 2},
                         {"step_index", i}, {"step_total", n}, {"step_type", step.type},
                         {"step_desc", step.desc},
                         {"text", "Step " + std::to_string(i + 1) + "/" + std::to_string(n) + ": " +
                                      step.type + " — " + step.desc}});

        bool applied = false;
        bool ok = false;
        std::string last_err;

        for (int attempt = 1; attempt <= 2; ++attempt) {
            std::string error_feedback = (attempt == 2) ? last_err : "";
            LiveDoc cur = read_live(doc_id);
            std::optional<double> vol_cur = total_volume(cur.body_count, cur.volume);
            std::string doc_summary = summarize_doc(cur.tree, vol_cur);
            ExecutorPrompt ep = build_executor_prompt(step, doc_summary, request, error_feedback,
                                                      prompts_dir, exec_input_cap);
            std::string system = "ROLE: executor\n" + ep.system;

            events.emit("agent_call",
                        json{{"stage", "execute"}, {"role", "executor"}, {"attempt", attempt},
                             {"step_index", i},
                             {"context_tokens_est", est_tokens(system) + est_tokens(ep.body)}});
            CallResult ecall = provider_call(provider, system, user_body(ep.body), micro_budget);  // reasoning headroom (see decompose)
            std::string sc = events.write_sidecar("execute_" + std::to_string(i), attempt,
                                                  ecall.reply);
            if (!ecall.thinking.empty()) {
                emit_agent_thought(events, ecall.thinking, ecall.model, "execute", attempt,
                                   /*parent_id=*/"", /*actor=*/"executor");
            }
            OpsResult ores = extract_ops(ecall.reply);
            if (!ores.ops) {
                std::string perr = ores.error.value_or("");
                last_err =
                    std::string("REPLY FORMAT ERROR: your previous reply did not contain a "
                                "valid ```json ops array. Reply with ONLY a fenced ```json "
                                "code block containing the ops array — no prose before or "
                                "after.\n") +
                    perr;
                events.emit("agent_output",
                            json{{"stage", "execute"}, {"role", "executor"}, {"attempt", attempt},
                                 {"step_index", i}, {"raw", utf8_prefix(ecall.reply, 4000)},
                                 {"raw_path", sc}, {"parsed_ok", false}, {"parse_error", perr},
                                 {"duration_ms", ecall.duration_ms}});
                continue;
            }
            const json& ops = *ores.ops;
            if (ops.empty()) {
                // Empty ops while the doc still has no features is not a real pass on a
                // build step — force a retry with explicit feedback.
                if (!doc_has_features(doc_summary) && !step.desc.empty()) {
                    last_err =
                        "you emitted an empty ops array but nothing has been built; emit the "
                        "ops for this step";
                    events.emit("agent_output",
                                json{{"stage", "execute"}, {"role", "executor"},
                                     {"attempt", attempt}, {"step_index", i},
                                     {"raw", utf8_prefix(ecall.reply, 4000)}, {"raw_path", sc},
                                     {"parsed_ok", true}, {"ops_count", 0},
                                     {"duration_ms", ecall.duration_ms},
                                     {"parse_error", last_err}});
                    continue;
                }
                events.emit("agent_output",
                            json{{"stage", "execute"}, {"role", "executor"}, {"attempt", attempt},
                                 {"step_index", i}, {"raw", utf8_prefix(ecall.reply, 4000)},
                                 {"raw_path", sc}, {"parsed_ok", true}, {"ops_count", 0},
                                 {"duration_ms", ecall.duration_ms}});
                applied = true;
                ok = true;
                break;
            }
            json dry = doc::doc_dry_run_ops(doc_id, ops);
            bool valid = dry.value("ok", false);
            std::string verr = dry.value("error", std::string());
            {
                json ev = json{{"stage", "execute"}, {"role", "executor"}, {"attempt", attempt},
                               {"step_index", i}, {"raw", utf8_prefix(ecall.reply, 4000)},
                               {"raw_path", sc}, {"parsed_ok", true},
                               {"ops_count", static_cast<int>(ops.size())},
                               {"duration_ms", ecall.duration_ms},
                               {"parse_error", valid ? json(nullptr) : json(verr)}};
                events.emit("agent_output", ev);
            }
            if (!valid) {
                last_err = "ops failed validation: " + verr;
                continue;
            }
            try {
                auto t0 = std::chrono::steady_clock::now();
                json resp = doc::doc_apply_ops(doc_id, cur.revision, ops, /*coalesce=*/false);
                auto t1 = std::chrono::steady_clock::now();
                int lat = static_cast<int>(
                    std::chrono::duration_cast<std::chrono::milliseconds>(t1 - t0).count());
                applied = true;
                json fs = json::array();
                if (resp.contains("rebuild") && resp["rebuild"].contains("feature_status"))
                    fs = resp["rebuild"]["feature_status"];
                ok = true;
                for (const auto& s : fs)
                    if (!(s.is_object() && s.contains("ok") && s["ok"].is_boolean() &&
                          s["ok"].get<bool>())) {
                        ok = false;
                        break;
                    }
                emit_agent_tool_call(events, /*tool_name=*/"", /*target=*/"", ops, ok, lat,
                                     /*parent_id=*/"", /*actor=*/"executor");
                std::string aicm_path;
                if (resp.contains("tess") && resp["tess"].is_object())
                    aicm_path = resp["tess"].value("aicm_path", std::string());
                if (!aicm_path.empty())
                    emit_doc_preview(events, doc_id, resp.value("revision", cur.revision),
                                     aicm_path);
                break;
            } catch (const std::exception& e) {
                last_err = std::string("apply failed: ") + e.what();
                continue;
            }
        }

        LiveDoc after = read_live(doc_id);
        std::optional<double> vol_after = total_volume(after.body_count, after.volume);

        bool verified = false;
        std::string notes;
        if (applied) {
            VerifyResult vr = verify_step(step, after.tree, after.feature_status, vol_before,
                                          vol_after);
            verified = vr.verified;
            notes = vr.notes;
        } else {
            verified = false;
            notes = !last_err.empty() ? last_err : "executor produced no valid ops";
        }
        if (applied && ok) ++applied_ok;

        events.emit("stage_result",
                    json{{"stage", "execute"}, {"attempt", 1},
                         {"verdict", (applied && ok) ? "pass" : "fail"}, {"reason", notes},
                         {"step_index", i}, {"step_total", n}, {"verified", verified},
                         {"applied", applied},
                         {"text", "Step " + std::to_string(i + 1) + "/" + std::to_string(n) + " " +
                                      ((applied && ok) ? "done" : "failed") + ": " + notes}});
    }

    // ---- finish ----
    // Honest status: "done" requires that real geometry actually exists. A step that returned an
    // empty ops array counts toward applied_ok but builds nothing, so applied_ok>0 alone is not
    // enough — a weak local model can "pass" empty steps and leave an empty doc. Require a solid.
    int revision = rev_now();
    LiveDoc final_doc = read_live(doc_id);
    bool has_solid = final_doc.body_count > 0;
    std::string status = (applied_ok > 0 && has_solid) ? "done" : "failed";
    std::string stop_reason = (status == "done")
                                  ? "completed"
                                  : (has_solid ? "all_steps_failed" : "no_geometry");
    events.emit("run_finished", json{{"status", status}, {"stop_reason", stop_reason},
                                     {"revision", revision}, {"steps_applied", applied_ok},
                                     {"steps_total", n}});
    return json{{"run_id", run_id}, {"status", status}, {"stop_reason", stop_reason},
                {"revision", revision}, {"doc_id", doc_id}};
}

}  // namespace agent
}  // namespace aicad
