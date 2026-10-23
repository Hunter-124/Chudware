// core/agent/clarify.cpp — see clarify.h. Pre-flight clarify gate (assist one-shot).
#include "agent/clarify.h"

#include <optional>
#include <regex>
#include <string>
#include <vector>

#include "agent/ask.h"        // build_doc_context (reused for the optional doc digest)
#include "agent/ops_emit.h"   // first_balanced

namespace chudware {
namespace agent {

const char* kClarifyRole = "clarify";
const char* kClarifySystem =
    "You are the INTAKE engineer of a CAD design pipeline. Decide whether the user's request "
    "already contains the build-critical dimensions and fits needed to model a GOOD part, and "
    "if not, ask the FEWEST possible targeted questions to obtain them.\n"
    "Units are millimeters; angles degrees. Do NOT design geometry, suggest features, or write "
    "code — only surface missing CRITICAL numbers (overall size, bore/thread, hole spacing, "
    "fits/clearances, wall thickness) that materially change the part, OR a design decision "
    "with discrete options, OR a geometry pick when the user must point at the model. If a "
    "value is implied or has a strong standard default (e.g. an M3 standoff -> 3mm thread), "
    "PREFER assuming it (record it under \"assumptions\") over asking. Do NOT ask when a "
    "sensible default exists.\n"
    "CATALOG HARDWARE IS FULLY SPECIFIED BY ITS DESIGNATION: a recognizable standard part "
    "(hex/round standoff, screw/bolt, nut, washer, bearing, o-ring, dowel pin, threaded "
    "insert) named with its size code and any length modifier is buildable with ZERO "
    "questions. Set ready=true, ask NOTHING, and record the catalog dimensions under "
    "\"assumptions\" (e.g. an 'M3 clearance hex standoff, 22mm' -> {across_flats: 5.5mm hex "
    "body, bore: Ø3.4mm M3 clearance, length: 22mm}). NEVER ask the user to supply a "
    "dimension that a standard catalog fixes. Only ask when a genuinely non-standard, "
    "build-critical value is missing AND has no catalog/standard default.\n"
    "Reply with EXACTLY one ```json block and no prose outside it, shaped:\n"
    "{\n"
    "  \"ready\": <true if the request is specific enough to build well with NO questions>,\n"
    "  \"questions\": [\n"
    "    {\"key\": \"<short_snake_case>\", \"ask\": \"<one concise question>\", "
    "\"unit\": \"<mm|deg|count or empty>\", \"suggested\": \"<your best default as a string>\",\n"
    "     \"kind\": \"<optional: dimension|text|choice|approval|geometry_select>\",\n"
    "     \"options\": [{\"value\":\"…\",\"label\":\"…\"}],  // choice only\n"
    "     \"multi\": false,  // choice or geometry_select multi-select\n"
    "     \"targets\": [\"face\",\"edge\",\"feature\"]  // geometry_select only\n"
    "    }\n"
    "  ],\n"
    "  \"assumptions\": [\"<assumption you made, one per entry>\"]\n"
    "}\n"
    "Ask AT MOST 4 questions, ordered by importance. When \"ready\" is true, \"questions\" MUST "
    "be an empty list. EVERY non-geometry question MUST have a non-empty \"suggested\" default "
    "so the user can accept it with one click. Design decisions use kind=choice with options "
    "(multi=true when several may apply). Geometry the user must point at uses "
    "kind=geometry_select with targets; suggested may be empty (never invent face/edge ids).";

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
    // strings, and require a non-empty `ask`. Optional interaction fields (kind/options/
    // multi/targets) are preserved additively for the interaction mirror + host card.
    ojson questions = ojson::array();
    if (parsed.contains("questions") && parsed["questions"].is_array()) {
        for (const auto& q : parsed["questions"]) {
            if (questions.size() >= 4) break;
            if (!q.is_object()) continue;
            std::string ask = q.contains("ask") ? scalar_str(q["ask"])
                              : (q.contains("prompt") ? scalar_str(q["prompt"]) : "");
            if (strip(ask).empty()) continue;
            std::string key = q.contains("key") ? scalar_str(q["key"])
                              : (q.contains("id") ? scalar_str(q["id"]) : "");
            std::string unit = q.contains("unit") ? scalar_str(q["unit"]) : "";
            std::string suggested = q.contains("suggested") ? scalar_str(q["suggested"]) : "";
            // kind: model-authored, else infer from unit.
            std::string kind;
            if (q.contains("kind")) kind = strip(scalar_str(q["kind"]));
            if (kind.empty() && q.contains("type")) kind = strip(scalar_str(q["type"]));
            // Alias map (keep small — full alias set lives in Python normalizer).
            if (kind == "select" || kind == "design" || kind == "design_choice" ||
                kind == "decision" || kind == "multiple_choice" || kind == "mc")
                kind = "choice";
            if (kind == "geometry" || kind == "pick" || kind == "select_geometry" ||
                kind == "face" || kind == "feature")
                kind = "geometry_select";
            if (kind == "number" || kind == "dim") kind = "dimension";
            if (kind == "confirm" || kind == "approve" || kind == "yes_no") kind = "approval";
            if (kind.empty()) kind = (!unit.empty()) ? "dimension" : "text";

            bool multi = false;
            if (q.contains("multi") && q["multi"].is_boolean()) multi = q["multi"].get<bool>();
            else if (q.contains("multiple") && q["multiple"].is_boolean())
                multi = q["multiple"].get<bool>();

            ojson options = ojson::array();
            if (q.contains("options") && q["options"].is_array()) {
                for (const auto& o : q["options"]) {
                    if (o.is_object() && o.contains("value")) {
                        std::string val = scalar_str(o["value"]);
                        std::string lab = o.contains("label") ? scalar_str(o["label"]) : val;
                        if (!val.empty())
                            options.push_back(ojson{{"value", val}, {"label", lab}});
                    } else if (o.is_string()) {
                        std::string val = o.get<std::string>();
                        if (!val.empty())
                            options.push_back(ojson{{"value", val}, {"label", val}});
                    }
                }
            } else if (q.contains("choices") && q["choices"].is_array()) {
                for (const auto& c : q["choices"]) {
                    if (c.is_string()) {
                        std::string val = c.get<std::string>();
                        if (!val.empty())
                            options.push_back(ojson{{"value", val}, {"label", val}});
                    }
                }
            }
            if (!options.empty() && kind != "geometry_select") kind = "choice";

            ojson targets = ojson::array();
            if (q.contains("targets") && q["targets"].is_array()) {
                for (const auto& t : q["targets"]) {
                    if (t.is_string() && !t.get<std::string>().empty())
                        targets.push_back(t.get<std::string>());
                }
            } else if (q.contains("target") && q["target"].is_string()) {
                targets.push_back(q["target"].get<std::string>());
            }
            if (!targets.empty()) kind = "geometry_select";

            // choice suggested may be an array (multi); keep a string for legacy.
            if (suggested.empty() && q.contains("suggested") && q["suggested"].is_array()) {
                std::string joined;
                for (const auto& v : q["suggested"]) {
                    if (!joined.empty()) joined += ", ";
                    joined += scalar_str(v);
                }
                suggested = joined;
            }
            if (suggested.empty() && kind == "choice" && !options.empty())
                suggested = options[0].value("value", std::string());
            if (suggested.empty() && kind == "approval") suggested = "false";

            ojson entry = ojson{{"key", key},
                                {"ask", ask},
                                {"unit", unit},
                                {"suggested", suggested},
                                {"kind", kind},
                                {"multi", multi}};
            if (!options.empty()) entry["options"] = options;
            if (!targets.empty()) entry["targets"] = targets;
            // Preserve multi suggested list for the interaction mirror when present.
            if (q.contains("suggested") && q["suggested"].is_array())
                entry["suggested_list"] = q["suggested"];
            questions.push_back(std::move(entry));
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

    // v1.1: additive aicad.interaction/1 mirror (contracts/interaction.md §1, §3, §12).
    // Emits interaction kinds (dimension/text/choice/approval/geometry_select) alongside
    // the legacy {key, ask, unit, suggested} fields — existing callers (drawClarifyCard,
    // ClarifyState) keep working; the host prefers the interaction envelope when present.
    ojson interaction_questions = ojson::array();
    for (const auto& q : questions) {
        std::string key = q.value("key", std::string());
        std::string ask = q.value("ask", std::string());
        std::string unit = q.value("unit", std::string());
        std::string suggested_str = q.value("suggested", std::string());
        std::string kind = q.value("kind", std::string("text"));
        bool multi = q.value("multi", false);

        ojson iq = ojson::object();
        iq["id"]       = key.empty() ? ask.substr(0, 32) : key;
        iq["kind"]     = kind;
        iq["prompt"]   = ask;
        iq["required"] = false;

        if (kind == "dimension") {
            iq["family"] = (unit == "deg" || unit == "degree" || unit == "degrees")
                               ? "angle"
                               : (unit == "count" ? "count" : "length");
            iq["suggested"] = ojson{{"value", 0.0}, {"unit", unit.empty() ? "mm" : unit}};
            try { iq["suggested"]["value"] = std::stod(suggested_str); }
            catch (...) {}
        } else if (kind == "choice") {
            iq["multi"] = multi;
            iq["options"] = q.contains("options") ? q["options"] : ojson::array();
            if (multi) {
                if (q.contains("suggested_list") && q["suggested_list"].is_array()) {
                    ojson arr = ojson::array();
                    for (const auto& v : q["suggested_list"]) arr.push_back(scalar_str(v));
                    iq["suggested"] = arr;
                } else if (!suggested_str.empty()) {
                    iq["suggested"] = ojson::array({suggested_str});
                } else {
                    iq["suggested"] = ojson::array();
                }
            } else {
                iq["suggested"] = suggested_str;
            }
        } else if (kind == "approval") {
            bool yes = (suggested_str == "true" || suggested_str == "1" ||
                        suggested_str == "yes" || suggested_str == "approve");
            iq["suggested"] = yes;
            iq["proposal"] = ask;
        } else if (kind == "geometry_select") {
            iq["multi"] = multi;
            iq["targets"] = q.contains("targets") && q["targets"].is_array() &&
                                    !q["targets"].empty()
                                ? q["targets"]
                                : ojson::array({"face", "edge", "feature"});
            // Honest empty pick — never invent sids (contract §12).
            iq["suggested"] = multi ? ojson::array() : ojson::object();
            iq["required"] = true;  // a real pick is needed when we ask this
        } else {
            // text / unknown
            iq["kind"] = "text";
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
}  // namespace chudware
