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
