// core/agent/ops_emit.cpp — see ops_emit.h. Byte-for-byte vs ops_emit.py.
//
// The fence scans are HAND-ROLLED (not std::regex) to guarantee identical
// matches to Python's `re` with re.DOTALL on the gate corpus — std::regex's
// `.` newline handling + lazy-quantifier backtracking are easy to get subtly
// wrong, so we implement the exact finite-state scan the patterns describe:
//
//   _JSON_FENCE_RE = ```(?:json|JSON)?\s*\n(.*?)```        (DOTALL)
//   _PY_FENCE_RE   = ```(?:python|py)?\s*\n(.*?)```        (DOTALL)
//   any-tag fence  = ```[A-Za-z0-9_+-]*\s*\n(.*?)```       (DOTALL)
//   _SCRIPTISH_RE  = ^\s*(?:result|parts|assembly)\s*=     (MULTILINE)
#include "agent/ops_emit.h"

#include <cctype>
#include <cstring>
#include <set>
#include <vector>

namespace aicad {
namespace agent {

namespace {

// Python str.strip(): trim leading/trailing ASCII+unicode whitespace. The corpus
// is ASCII; strip the standard Python whitespace set " \t\n\r\f\v".
bool py_is_space(char c) {
    return c == ' ' || c == '\t' || c == '\n' || c == '\r' || c == '\f' || c == '\v';
}
std::string py_strip(const std::string& s) {
    size_t a = 0, b = s.size();
    while (a < b && py_is_space(s[a])) ++a;
    while (b > a && py_is_space(s[b - 1])) --b;
    return s.substr(a, b - a);
}

// One fence kind. `tags` is the set of allowed optional tag literals to consume
// after the opening ```; if `any_tag` is true we consume [A-Za-z0-9_+-]* instead.
struct FenceSpec {
    std::vector<std::string> tags;  // tried longest-first by the caller order.
    bool any_tag = false;
};

// Find all fenced-block BODIES per the given spec, mirroring re.finditer over the
// corresponding pattern. Returns the captured group-1 substrings, in order, with
// the same non-overlapping left-to-right semantics as Python's finditer.
std::vector<std::string> find_fenced_bodies(const std::string& text, const FenceSpec& spec) {
    std::vector<std::string> out;
    const std::string fence = "```";
    size_t pos = 0;
    const size_t n = text.size();
    while (pos < n) {
        size_t open = text.find(fence, pos);
        if (open == std::string::npos) break;
        size_t cur = open + fence.size();
        // Optional tag.
        if (spec.any_tag) {
            // [A-Za-z0-9_+-]*  (greedy; no backtracking needed — \s* + \n follow)
            while (cur < n) {
                char c = text[cur];
                bool ok = std::isalnum(static_cast<unsigned char>(c)) || c == '_' ||
                          c == '+' || c == '-';
                if (!ok) break;
                ++cur;
            }
        } else {
            // (?:tag)?  — try each literal; consume the first that matches.
            for (const std::string& tag : spec.tags) {
                if (text.compare(cur, tag.size(), tag) == 0) {
                    cur += tag.size();
                    break;
                }
            }
        }
        // \s*\n : greedy \s* then a required \n. Because \n is whitespace, the
        // greedy \s* would swallow it then backtrack one; the net effect is the
        // match requires at least one \n in the run of whitespace and consumes
        // through the LAST \n in that maximal whitespace run.
        size_t ws_start = cur;
        size_t ws = cur;
        while (ws < n && py_is_space(text[ws])) ++ws;
        // Need at least one '\n' within [ws_start, ws). The required \n is the
        // last newline in the run (greedy \s* backtracks minimally).
        size_t last_nl = std::string::npos;
        for (size_t i = ws_start; i < ws; ++i)
            if (text[i] == '\n') last_nl = i;
        if (last_nl == std::string::npos) {
            // No newline after the (optional) tag — this ``` is not a valid open
            // fence; advance past it and keep scanning (regex would try next pos).
            pos = open + fence.size();
            continue;
        }
        size_t body_start = last_nl + 1;  // after the consumed \n
        // (.*?)``` : shortest body up to the next ```.
        size_t close = text.find(fence, body_start);
        if (close == std::string::npos) {
            // Unterminated fence: regex cannot match; stop (no further ``` opener
            // can have a closing one either if this is the last). Advance to avoid
            // infinite loop, but there will be no more closers.
            pos = open + fence.size();
            continue;
        }
        out.push_back(text.substr(body_start, close - body_start));
        pos = close + fence.size();  // finditer continues after the full match.
    }
    return out;
}

// _SCRIPTISH_RE: ^\s*(?:result|parts|assembly)\s*=  with re.MULTILINE.
// ^ matches at start-of-string and after every '\n'. \s* here can cross lines
// (\s includes \n) — but anchored at a line start, it consumes leading inline
// whitespace (and possibly blank lines) before the keyword.
bool scriptish(const std::string& body) {
    const std::vector<std::string> kw = {"result", "parts", "assembly"};
    const size_t n = body.size();
    // Candidate ^ positions: index 0 and every index right after a '\n'.
    auto try_at = [&](size_t start) -> bool {
        size_t i = start;
        while (i < n && py_is_space(body[i])) ++i;  // \s*
        for (const std::string& k : kw) {
            if (body.compare(i, k.size(), k) == 0) {
                size_t j = i + k.size();
                while (j < n && py_is_space(body[j])) ++j;  // \s*
                if (j < n && body[j] == '=') return true;
            }
        }
        return false;
    };
    if (try_at(0)) return true;
    for (size_t i = 0; i < n; ++i)
        if (body[i] == '\n' && try_at(i + 1)) return true;
    return false;
}

// Parse a JSON blob. Returns nullopt (the SENTINEL) on any parse failure.
std::optional<json> try_load(const std::string& blob) {
    try {
        return json::parse(blob);
    } catch (...) {
        return std::nullopt;
    }
}

}  // namespace

std::optional<std::string> first_balanced(const std::string& text, char open_ch, char close_ch) {
    size_t start = text.find(open_ch);
    if (start == std::string::npos) return std::nullopt;
    int depth = 0;
    bool in_str = false, esc = false;
    for (size_t i = start; i < text.size(); ++i) {
        char c = text[i];
        if (in_str) {
            if (esc) {
                esc = false;
            } else if (c == '\\') {
                esc = true;
            } else if (c == '"') {
                in_str = false;
            }
            continue;
        }
        if (c == '"') {
            in_str = true;
        } else if (c == open_ch) {
            ++depth;
        } else if (c == close_ch) {
            --depth;
            if (depth == 0) return text.substr(start, i - start + 1);
        }
    }
    return std::nullopt;
}

OpsResult extract_ops(const std::string& reply) {
    FenceSpec json_spec;
    json_spec.tags = {"json", "JSON"};
    std::vector<std::string> candidates;
    for (const std::string& body : find_fenced_bodies(reply, json_spec))
        candidates.push_back(py_strip(body));
    auto bare = first_balanced(reply, '[', ']');
    if (bare) candidates.push_back(*bare);

    std::string last_err = "no JSON ops array found in reply";
    for (const std::string& blob : candidates) {
        auto data = try_load(blob);
        if (!data) {
            last_err = "ops block was not valid JSON";
            continue;
        }
        if (data->is_array()) {
            OpsResult r;
            r.ops = *data;
            return r;
        }
        if (data->is_object() && data->contains("ops") && (*data)["ops"].is_array()) {
            OpsResult r;
            r.ops = (*data)["ops"];
            return r;
        }
        last_err = "JSON parsed but was not an ops array";
    }
    OpsResult r;
    r.error = last_err;
    return r;
}

OpsResult extract_ops_last_nonempty(const std::string& reply) {
    FenceSpec json_spec;
    json_spec.tags = {"json", "JSON"};
    // Return the LAST candidate that is a non-empty array of OBJECTS (ops are {op:...};
    // requiring object elements rejects coordinate/number fragments like [-8, 0] that a
    // reasoning stream is full of).
    auto pick = [](const std::vector<std::string>& cands) -> std::optional<json> {
        std::optional<json> best;
        for (const std::string& blob : cands) {
            auto data = try_load(blob);
            if (!data) continue;
            const json* arr = nullptr;
            if (data->is_array()) {
                arr = &*data;
            } else if (data->is_object() && data->contains("ops") && (*data)["ops"].is_array()) {
                arr = &(*data)["ops"];
            }
            if (arr && !arr->empty() && arr->front().is_object()) best = *arr;
        }
        return best;
    };
    std::vector<std::string> fenced;
    for (const std::string& body : find_fenced_bodies(reply, json_spec))
        fenced.push_back(py_strip(body));
    OpsResult r;
    if (auto ops = pick(fenced)) {  // fenced blocks are the authoritative source
        r.ops = *ops;
        return r;
    }
    // Only when NO fenced block yielded ops: the first bare balanced [...] (drafts in a
    // reasoning stream are usually fenced, so the bare span is the model's single answer).
    if (auto bare = first_balanced(reply, '[', ']'))
        if (auto ops = pick({*bare})) {
            r.ops = *ops;
            return r;
        }
    r.error = "no non-empty ops array in reasoning";
    return r;
}

namespace {

// Canonical domain class names the model may request. full_ops upgrades the
// Solo slim sheet; the rest append shared/cheatsheets/<file>.md.
const char* normalize_tool_class(const std::string& raw) {
    std::string t;
    t.reserve(raw.size());
    for (unsigned char c : raw) t.push_back(static_cast<char>(std::tolower(c)));
    // trim
    size_t a = 0, b = t.size();
    while (a < b && (t[a] == ' ' || t[a] == '\t')) ++a;
    while (b > a && (t[b - 1] == ' ' || t[b - 1] == '\t')) --b;
    t = t.substr(a, b - a);
    if (t == "full_ops" || t == "full" || t == "ops_full") return "full_ops";
    if (t == "sheetmetal" || t == "sheet_metal" || t == "sm") return "sheetmetal";
    // Disk file is weldment.md; accept both singular/plural from the model.
    if (t == "weldments" || t == "weldment" || t == "weld") return "weldments";
    if (t == "surfacing" || t == "surface" || t == "surfaces") return "surfacing";
    return nullptr;
}

}  // namespace

ToolScopeRequest peel_request_tools(const json& ops) {
    ToolScopeRequest out;
    if (!ops.is_array()) {
        out.remaining = ops;
        return out;
    }
    std::set<std::string> seen;
    for (const auto& op : ops) {
        if (!op.is_object()) {
            out.remaining.push_back(op);
            continue;
        }
        const std::string name = op.value("op", std::string());
        if (name != "request_tools") {
            out.remaining.push_back(op);
            continue;
        }
        out.had_request = true;
        if (!op.contains("classes")) continue;
        const json& cls = op["classes"];
        auto push_one = [&](const std::string& raw) {
            const char* norm = normalize_tool_class(raw);
            if (!norm) return;
            if (seen.insert(norm).second) out.requested.push_back(norm);
        };
        if (cls.is_array()) {
            for (const auto& c : cls) {
                if (c.is_string()) push_one(c.get<std::string>());
            }
        } else if (cls.is_string()) {
            push_one(cls.get<std::string>());
        }
    }
    return out;
}

AdviceRequest peel_request_advice(const json& ops) {
    AdviceRequest out;
    if (!ops.is_array()) {
        out.remaining = ops;
        return out;
    }
    for (const auto& op : ops) {
        if (!op.is_object()) {
            out.remaining.push_back(op);
            continue;
        }
        const std::string name = op.value("op", std::string());
        if (name != "request_advice") {
            out.remaining.push_back(op);
            continue;
        }
        out.had_request = true;
        if (out.question.empty()) {
            if (op.contains("question") && op["question"].is_string())
                out.question = op["question"].get<std::string>();
            else if (op.contains("q") && op["q"].is_string())
                out.question = op["q"].get<std::string>();
            if (op.contains("context") && op["context"].is_string())
                out.context = op["context"].get<std::string>();
        }
    }
    return out;
}

DelegateRequest peel_delegate(const json& ops) {
    DelegateRequest out;
    if (!ops.is_array()) {
        out.remaining = ops;
        return out;
    }
    for (const auto& op : ops) {
        if (!op.is_object()) {
            out.remaining.push_back(op);
            continue;
        }
        const std::string name = op.value("op", std::string());
        if (name != "delegate") {
            out.remaining.push_back(op);
            continue;
        }
        out.had_request = true;
        if (out.task.empty()) {
            if (op.contains("task") && op["task"].is_string())
                out.task = op["task"].get<std::string>();
            else if (op.contains("request") && op["request"].is_string())
                out.task = op["request"].get<std::string>();
            if (op.contains("why") && op["why"].is_string())
                out.why = op["why"].get<std::string>();
        }
    }
    return out;
}

namespace {

// First fenced/bare JSON object from a reply (advisor payloads are objects).
std::optional<json> first_json_object(const std::string& reply) {
    FenceSpec json_spec;
    json_spec.tags = {"json", "JSON"};
    for (const std::string& body : find_fenced_bodies(reply, json_spec)) {
        auto data = try_load(py_strip(body));
        if (data && data->is_object()) return data;
    }
    FenceSpec any_spec;
    any_spec.any_tag = true;
    for (const std::string& body : find_fenced_bodies(reply, any_spec)) {
        auto data = try_load(py_strip(body));
        if (data && data->is_object()) return data;
    }
    auto obj = first_balanced(reply, '{', '}');
    if (obj) {
        auto data = try_load(*obj);
        if (data && data->is_object()) return data;
    }
    return std::nullopt;
}

std::string lower_ascii(std::string s) {
    for (char& c : s) {
        unsigned char u = static_cast<unsigned char>(c);
        if (u >= 'A' && u <= 'Z') c = static_cast<char>(u - 'A' + 'a');
    }
    return s;
}

}  // namespace

AdvisorAdvice parse_advisor_advice(const std::string& reply) {
    AdvisorAdvice out;
    auto data = first_json_object(reply);
    if (!data) return out;
    if ((*data).contains("advice") && (*data)["advice"].is_string())
        out.advice = (*data)["advice"].get<std::string>();
    else if ((*data).contains("note") && (*data)["note"].is_string() &&
             !(*data).contains("verdict"))
        out.advice = (*data)["note"].get<std::string>();
    if ((*data).contains("confidence") && (*data)["confidence"].is_string())
        out.confidence = lower_ascii((*data)["confidence"].get<std::string>());
    out.ok = !out.advice.empty();
    return out;
}

AdvisorPlanVerdict parse_advisor_plan_verdict(const std::string& reply) {
    AdvisorPlanVerdict out;
    auto data = first_json_object(reply);
    if (!data) {
        // Unparseable → approve (do not block the run on a bad advisor reply).
        out.verdict = "approve";
        out.note = "advisor reply unparseable; continuing";
        out.fallback = "unparseable";
        return out;
    }
    std::string v;
    if ((*data).contains("verdict") && (*data)["verdict"].is_string())
        v = lower_ascii((*data)["verdict"].get<std::string>());
    else if ((*data).contains("decision") && (*data)["decision"].is_string())
        v = lower_ascii((*data)["decision"].get<std::string>());
    if (v == "approve" || v == "approved" || v == "ok" || v == "pass") {
        out.verdict = "approve";
        out.ok = true;
    } else if (v == "revise" || v == "revision" || v == "reject" || v == "retry") {
        out.verdict = "revise";
        out.ok = true;
    } else if (v == "escalate" || v == "escalation" || v == "user" || v == "human") {
        out.verdict = "escalate";
        out.ok = true;
    } else {
        out.verdict = "approve";
        out.note = "unrecognized verdict; continuing";
        out.fallback = "unparseable";
        return out;
    }
    if ((*data).contains("note") && (*data)["note"].is_string())
        out.note = (*data)["note"].get<std::string>();
    else if ((*data).contains("reason") && (*data)["reason"].is_string())
        out.note = (*data)["reason"].get<std::string>();
    else if ((*data).contains("advice") && (*data)["advice"].is_string())
        out.note = (*data)["advice"].get<std::string>();
    if ((*data).contains("confidence") && (*data)["confidence"].is_string())
        out.confidence = lower_ascii((*data)["confidence"].get<std::string>());
    return out;
}

const char* execution_style_from_structure(const char* structure_lab) {
    if (!structure_lab) return "edit";
    // solo/subagent → edit; dag/company → swarm. eng_firm never recommended.
    if (std::strcmp(structure_lab, "dag") == 0 || std::strcmp(structure_lab, "company") == 0)
        return "swarm";
    return "edit";
}

bool extract_revert(const std::string& reply) {
    FenceSpec json_spec;
    json_spec.tags = {"json", "JSON"};
    for (const std::string& body : find_fenced_bodies(reply, json_spec)) {
        auto data = try_load(py_strip(body));
        if (data && data->is_object() && data->contains("revert") &&
            (*data)["revert"].is_boolean() && (*data)["revert"].get<bool>())
            return true;
    }
    auto obj = first_balanced(reply, '{', '}');
    if (obj) {
        auto data = try_load(*obj);
        if (data && data->is_object() && data->contains("revert") &&
            (*data)["revert"].is_boolean() && (*data)["revert"].get<bool>())
            return true;
    }
    return false;
}

json script_escape_op(const std::string& code, const std::string& name) {
    return json{
        {"op", "add_feature"},
        {"feature", {{"type", "script"},
                     {"name", name},
                     {"params", {{"code", code}}},
                     {"refs", json::object()}}},
    };
}

std::optional<std::string> extract_script(const std::string& reply) {
    FenceSpec py_spec;
    py_spec.tags = {"python", "py"};
    for (const std::string& body : find_fenced_bodies(reply, py_spec)) {
        std::string stripped = py_strip(body);
        if (scriptish(stripped)) return stripped;
    }
    FenceSpec any_spec;
    any_spec.any_tag = true;
    for (const std::string& body : find_fenced_bodies(reply, any_spec)) {
        std::string stripped = py_strip(body);
        if (scriptish(stripped)) return stripped;
    }
    return std::nullopt;
}

}  // namespace agent
}  // namespace aicad
