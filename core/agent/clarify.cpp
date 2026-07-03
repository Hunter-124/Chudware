// core/agent/clarify.cpp — see clarify.h. Pre-flight clarify gate (assist one-shot).
#include "agent/clarify.h"

#include <optional>
#include <regex>
#include <string>
#include <vector>

#include "agent/ask.h"        // build_doc_context (reused for the optional doc digest)
#include "agent/ops_emit.h"   // first_balanced

namespace aicad {
namespace agent {

const char* kClarifyRole = "clarify";
const char* kClarifySystem =
    "You are the INTAKE engineer of a CAD design pipeline. Decide whether the user's request "
    "already contains the build-critical dimensions and fits needed to model a GOOD part, and "
    "if not, ask the FEWEST possible targeted questions to obtain them.\n"
    "Units are millimeters; angles degrees. Do NOT design geometry, suggest features, or write "
    "code — only surface missing CRITICAL numbers (overall size, bore/thread, hole spacing, "
    "fits/clearances, wall thickness) that materially change the part. If a value is implied or "
    "has a strong standard default (e.g. an M3 standoff -> 3mm thread), PREFER assuming it "
    "(record it under \"assumptions\") over asking.\n"
    "Reply with EXACTLY one ```json block and no prose outside it, shaped:\n"
    "{\n"
    "  \"ready\": <true if the request is specific enough to build well with NO questions>,\n"
    "  \"questions\": [\n"
    "    {\"key\": \"<short_snake_case>\", \"ask\": \"<one concise question>\", "
    "\"unit\": \"<mm|deg|count or empty>\", \"suggested\": \"<your best default as a string>\"}\n"
    "  ],\n"
    "  \"assumptions\": [\"<assumption you made, one per entry>\"]\n"
    "}\n"
    "Ask AT MOST 4 questions, ordered by importance. When \"ready\" is true, \"questions\" MUST "
    "be an empty list. EVERY question MUST have a non-empty \"suggested\" default so the user "
    "can accept it with one click.";

namespace {

// strip a string (Python str.strip default whitespace).
std::string strip(const std::string& s) {
    size_t a = s.find_first_not_of(" \t\n\r\f\v");
    if (a == std::string::npos) return "";
    size_t b = s.find_last_not_of(" \t\n\r\f\v");
    return s.substr(a, b - a + 1);
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

// str() of a JSON scalar (so a model that returns suggested:25 still yields "25").
std::string scalar_str(const ojson& v) {
    if (v.is_string()) return v.get<std::string>();
    if (v.is_boolean()) return v.get<bool>() ? "true" : "false";
    if (v.is_null()) return "";
    if (v.is_number_integer()) return std::to_string(v.get<long long>());
    if (v.is_number_unsigned()) return std::to_string(v.get<unsigned long long>());
    if (v.is_number_float()) {
        double d = v.get<double>();
        if (d == static_cast<long long>(d)) return std::to_string(static_cast<long long>(d));
        return strip(std::to_string(d));
    }
    return v.dump();
}

// First fenced ```json object, else a bare top-level {...}; null if neither parses.
// Mirrors run.cpp extract_json_obj (kept local to stay self-contained).
ojson extract_json_obj(const std::string& reply) {
    std::regex fence(R"(```(?:json|JSON)?\s*\n([\s\S]*?)```)");
    auto begin = std::sregex_iterator(reply.begin(), reply.end(), fence);
    auto end = std::sregex_iterator();
    for (auto it = begin; it != end; ++it) {
        std::string body = strip((*it)[1].str());
        if (body.empty()) continue;
        ojson d = ojson::parse(body, nullptr, false);
        if (!d.is_discarded() && d.is_object()) return d;
    }
    std::optional<std::string> blob = first_balanced(reply, '{', '}');
    if (blob) {
        ojson d = ojson::parse(*blob, nullptr, false);
        if (!d.is_discarded() && d.is_object()) return d;
    }
    return ojson(nullptr);
}

// The never-block fallback result.
ojson ready_now() {
    return ojson{{"ready", true}, {"questions", ojson::array()}, {"assumptions", ojson::array()}};
}

}  // namespace

ClarifyPrompt build_clarify_prompt(const std::string& request, const ojson& doc,
                                   const std::string& doc_id, long revision,
                                   const ojson& feature_status) {
    ClarifyPrompt out;
    out.system = std::string("ROLE: ") + kClarifyRole + "\n" + kClarifySystem;
    out.body = build_doc_context(doc, doc_id, revision, feature_status) + "\n\nREQUEST:\n" +
               request;
    return out;
}

ojson clarify(const std::string& request, const ojson& doc, const std::string& doc_id,
              long revision, const ojson& feature_status, const ClarifyCompleteFn& complete) {
    ClarifyPrompt p = build_clarify_prompt(request, doc, doc_id, revision, feature_status);
    std::string reply = complete(p.system, p.body);
    ojson parsed = extract_json_obj(reply);
    if (parsed.is_null() || !parsed.is_object()) return ready_now();

    // Normalize the questions: keep only well-shaped entries, cap at 4, coerce fields to
    // strings, and require a non-empty `ask`. A question without a suggested default still
    // passes (the host falls back to an empty field) — only `ask` is mandatory.
    ojson questions = ojson::array();
    if (parsed.contains("questions") && parsed["questions"].is_array()) {
        for (const auto& q : parsed["questions"]) {
            if (questions.size() >= 4) break;
            if (!q.is_object()) continue;
            std::string ask = q.contains("ask") ? scalar_str(q["ask"]) : "";
            if (strip(ask).empty()) continue;
            std::string key = q.contains("key") ? scalar_str(q["key"]) : "";
            std::string unit = q.contains("unit") ? scalar_str(q["unit"]) : "";
            std::string suggested = q.contains("suggested") ? scalar_str(q["suggested"]) : "";
            questions.push_back(ojson{{"key", key},
                                      {"ask", ask},
                                      {"unit", unit},
                                      {"suggested", suggested}});
        }
    }

    ojson assumptions = ojson::array();
    if (parsed.contains("assumptions") && parsed["assumptions"].is_array()) {
        for (const auto& a : parsed["assumptions"]) {
            std::string s = strip(scalar_str(a));
            if (!s.empty()) assumptions.push_back(s);
        }
    }

    // ready = explicit truthy `ready`, OR there are no questions to ask.
    bool ready = (parsed.contains("ready") && truthy(parsed["ready"])) || questions.empty();

    // T-F4: additive aicad.interaction/1 mirror (contracts/interaction.md §1, §3).
    // Emits the same questions reshaped as interaction/1 "dimension"/"text" questions
    // alongside the legacy {key, ask, unit, suggested} fields — existing callers
    // (drawClarifyCard, ClarifyState) are unaffected; new callers use "interaction".
    // The batch is non-blocking with suggested defaults, capped at 4 (the proven cap).
    ojson interaction_questions = ojson::array();
    for (const auto& q : questions) {
        std::string key = q.contains("key") ? q["key"].get<std::string>() : "";
        std::string ask = q.contains("ask") ? q["ask"].get<std::string>() : "";
        std::string unit = q.contains("unit") ? q["unit"].get<std::string>() : "";
        std::string suggested_str = q.contains("suggested") ? q["suggested"].get<std::string>() : "";
        // Decide kind: non-empty unit → dimension, else → text.
        std::string kind = (!unit.empty()) ? "dimension" : "text";
        ojson iq = ojson::object();
        iq["id"]      = key.empty() ? ask.substr(0, 32) : key;  // stable ledger key
        iq["kind"]    = kind;
        iq["prompt"]  = ask;
        iq["required"] = false;
        if (kind == "dimension") {
            iq["family"]   = "length";  // default; angle/count not in clarify vocab
            iq["suggested"] = ojson{{"value", 0.0}, {"unit", unit}};
            // If suggested_str parses as a number, embed it.
            try { iq["suggested"]["value"] = std::stod(suggested_str); }
            catch (...) {}
        } else {
            iq["suggested"] = suggested_str;
        }
        interaction_questions.push_back(iq);
    }
    // batch_id is omitted (no uuid on the C++ side); callers may add one.
    ojson interaction = ojson{
        {"schema",    "aicad.interaction/1"},
        {"scope",     "run"},
        {"blocking",  false},   // clarify is non-blocking — suggested defaults apply
        {"questions", interaction_questions},
    };

    return ojson{{"ready", ready}, {"questions", questions},
                 {"assumptions", assumptions}, {"interaction", interaction}};
}

}  // namespace agent
}  // namespace aicad
