// core/agent/pm_assist.cpp — see pm_assist.h. Mirrors agent/pm_assist.py.
#include "agent/pm_assist.h"

#include <cmath>
#include <regex>
#include <vector>

#include "agent/ask.h"  // build_doc_context

namespace chudware {
namespace agent {

const char* kPmAssistRole = "pm_assist";
const char* kPmAssistSystem =
    "You are Chudware's PropertyManager assistant. Return ONLY strict JSON. "
    "You help fill the currently active feature form using the provided visible field schema. "
    "Allowed top-level keys: answer, patches, activate_well, reveal_fields, highlights, questions. "
    "patches is an array of {key,value}; patch only ordinary fields from the visible schema. "
    "Never invent topology selectors or references. If a selector/reference is needed, set "
    "activate_well to the visible selector well key and explain what the user should pick. "
    "questions may contain at most 3 multiple-choice questions with 2-3 choices each.";

namespace {

// Python truthiness for a JSON value (used where Python does `if x`).
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

// Python str() of a JSON scalar/value (str() of a non-str behaves like repr-ish;
// only used where the Python code wraps with str(...)).
std::string py_str(const ojson& v) {
    if (v.is_string()) return v.get<std::string>();
    if (v.is_boolean()) return v.get<bool>() ? "True" : "False";
    if (v.is_null()) return "None";
    if (v.is_number_integer()) return std::to_string(v.get<long long>());
    if (v.is_number_unsigned()) return std::to_string(v.get<unsigned long long>());
    if (v.is_number_float()) return py_float_repr(v.get<double>());
    // str(list)/str(dict) — not exercised by the oracle's str() call sites here.
    return v.dump();
}

std::string strip(const std::string& s) {
    size_t a = s.find_first_not_of(" \t\n\r\f\v");
    if (a == std::string::npos) return "";
    size_t b = s.find_last_not_of(" \t\n\r\f\v");
    return s.substr(a, b - a + 1);
}

// field.get("kind") as a string ("" when absent/non-string-ish via str? — the
// oracle compares kind == "dim" etc., so a non-string kind never matches).
std::string field_kind(const ojson& field) {
    if (field.is_object() && field.contains("kind") && field["kind"].is_string())
        return field["kind"].get<std::string>();
    return "";
}

// Selector-kind test used by _selector_keys.
bool is_selector_kind(const ojson& field) {
    std::string k = field_kind(field);
    return k == "selector" || k == "feature_well";
}

std::vector<std::string> selector_keys(const ojson& fields) {
    std::vector<std::string> out;
    if (fields.is_object())
        for (auto it = fields.begin(); it != fields.end(); ++it)
            if (is_selector_kind(it.value())) out.push_back(it.key());
    return out;
}

std::vector<std::string> empty_selector_keys(const ojson& fields, const ojson& current) {
    std::vector<std::string> keys;
    ojson cur_fields = ojson::object();
    if (current.is_object() && current.contains("fields") && truthy(current["fields"]) &&
        current["fields"].is_object())
        cur_fields = current["fields"];
    for (const std::string& k : selector_keys(fields)) {
        // fcur = cur_fields.get(k) if dict else {}
        ojson fcur = (cur_fields.is_object() && cur_fields.contains(k)) ? cur_fields[k]
                                                                       : ojson(nullptr);
        // not isinstance(fcur, dict) OR int(fcur.get("count") or 0) == 0
        bool is_empty = true;
        if (fcur.is_object()) {
            ojson cnt = fcur.contains("count") ? fcur["count"] : ojson(nullptr);
            // int(cnt or 0): if cnt falsy -> 0; else int(cnt).
            long long ic = 0;
            if (truthy(cnt)) {
                if (cnt.is_number_integer()) ic = cnt.get<long long>();
                else if (cnt.is_number_unsigned())
                    ic = static_cast<long long>(cnt.get<unsigned long long>());
                else if (cnt.is_number_float())
                    ic = static_cast<long long>(cnt.get<double>());  // int() truncates
                else if (cnt.is_string())
                    ic = std::atoll(cnt.get<std::string>().c_str());  // int("..")
                else
                    ic = 0;
            }
            is_empty = (ic == 0);
        }
        if (is_empty) keys.push_back(k);
    }
    return keys;
}

ojson clean_questions(const ojson& raw) {
    ojson out = ojson::array();
    if (!raw.is_array()) return out;
    int i = 0;
    for (auto it = raw.begin(); it != raw.end() && i < 3; ++it, ++i) {
        const ojson& q = *it;
        if (!q.is_object()) continue;
        // prompt = str(q.get("prompt") or q.get("question") or "").strip()
        ojson pv = q.contains("prompt") ? q["prompt"] : ojson(nullptr);
        if (!truthy(pv)) pv = q.contains("question") ? q["question"] : ojson(nullptr);
        std::string prompt = truthy(pv) ? strip(py_str(pv)) : strip("");
        // choices = q.get("choices") or []
        ojson choices = (q.contains("choices") && truthy(q["choices"])) ? q["choices"]
                                                                       : ojson::array();
        // Python: [str(c).strip() for c in choices if str(c).strip()][:3]
        // (filter first, then cap to 3).
        std::vector<std::string> clean;
        if (choices.is_array())
            for (const auto& c : choices) {
                std::string cs = strip(py_str(c));
                if (!cs.empty()) clean.push_back(cs);
            }
        if (clean.size() > 3) clean.resize(3);
        if (!prompt.empty() && clean.size() >= 2 && clean.size() <= 3) {
            ojson item = ojson::object();
            item["prompt"] = prompt;
            ojson ch = ojson::array();
            for (const auto& c : clean) ch.push_back(c);
            item["choices"] = ch;
            out.push_back(item);
        }
    }
    return out;
}

}  // namespace

ojson pm_field_map(const ojson& form_schema, bool visible_only) {
    ojson out = ojson::object();
    ojson fields = (form_schema.is_object() && form_schema.contains("fields") &&
                    truthy(form_schema["fields"]))
                       ? form_schema["fields"]
                       : ojson::array();
    if (!fields.is_array()) return out;
    for (const auto& f : fields) {
        if (!f.is_object()) continue;
        // key = str(f.get("key") or "")
        ojson kv = f.contains("key") ? f["key"] : ojson(nullptr);
        std::string key = truthy(kv) ? py_str(kv) : "";
        if (key.empty()) continue;
        if (visible_only) {
            // skip where f.get("visible", True) is falsy
            bool visible = true;
            if (f.contains("visible")) visible = truthy(f["visible"]);
            if (!visible) continue;
        }
        out[key] = f;
    }
    return out;
}

ojson pm_extract_json(const std::string& reply) {
    std::string text = strip(reply);
    if (text.empty()) return ojson::object();
    // fenced: ```(?:json)?\s*(\{.*?\})\s*``` with DOTALL | IGNORECASE
    try {
        std::regex fenced(R"(```(?:json)?\s*(\{[\s\S]*?\})\s*```)",
                          std::regex::icase);
        std::smatch m;
        if (std::regex_search(text, m, fenced)) text = m[1].str();
    } catch (...) {
        // regex failure -> leave text unchanged (degrades to the brace-scan path)
    }
    if (text.empty() || text[0] != '{') {
        size_t start = text.find('{');
        size_t end = text.rfind('}');
        if (start != std::string::npos && end != std::string::npos && end > start)
            text = text.substr(start, end - start + 1);
    }
    try {
        ojson obj = ojson::parse(text);
        return obj.is_object() ? obj : ojson::object();
    } catch (...) {
        return ojson::object();
    }
}

ojson pm_coerce_patch_value(const ojson& field, const ojson& value) {
    std::string kind = field_kind(field);
    if (kind == "dim") {
        // isinstance(value, (int, float, str)) — NOTE bool is a subclass of int
        // in Python, so a bool value passes here and is returned as-is.
        if (value.is_number_integer() || value.is_number_unsigned() || value.is_number_float() ||
            value.is_string() || value.is_boolean())
            return value;
        if (value.is_object()) {
            if (value.contains("expr") && value["expr"].is_string()) {
                ojson r = ojson::object();
                r["expr"] = value["expr"];
                return r;
            }
            if (value.contains("value")) {
                const ojson& vv = value["value"];
                if (vv.is_number_integer() || vv.is_number_unsigned() || vv.is_number_float() ||
                    vv.is_string() || vv.is_boolean())
                    return vv;
            }
        }
        return ojson(nullptr);
    }
    if (kind == "combo" || kind == "datum_ref") {
        if (!value.is_string()) return ojson(nullptr);
        ojson opts = (field.is_object() && field.contains("options") && truthy(field["options"]))
                         ? field["options"]
                         : ojson::array();
        if (opts.is_array())
            for (const auto& o : opts)
                if (o == value) return value;
        return ojson(nullptr);
    }
    if (kind == "check") {
        return value.is_boolean() ? value : ojson(nullptr);
    }
    if (kind == "int") {
        if (value.is_boolean()) return ojson(nullptr);
        if (value.is_number_integer() || value.is_number_unsigned()) return value;
        if (value.is_number_float()) {
            double d = value.get<double>();
            if (std::floor(d) == d && std::isfinite(d))
                return ojson(static_cast<long long>(d));  // int(value)
            return ojson(nullptr);
        }
        return ojson(nullptr);
    }
    // Selector wells and feature wells are never patched by AI.
    return ojson(nullptr);
}

ojson pm_validate_plan(const ojson& plan, const ojson& form_schema, const ojson& current) {
    ojson fields = pm_field_map(form_schema, true);
    ojson all_fields = pm_field_map(form_schema, false);
    std::vector<std::string> sel = selector_keys(all_fields);
    auto in_selectors = [&](const std::string& k) {
        for (const auto& s : sel) if (s == k) return true;
        return false;
    };

    ojson out = ojson::object();
    // answer = str(plan.get("answer") or "").strip()
    ojson av = (plan.is_object() && plan.contains("answer")) ? plan["answer"] : ojson(nullptr);
    out["answer"] = truthy(av) ? strip(py_str(av)) : std::string("");
    out["patches"] = ojson::array();
    out["activate_well"] = std::string("");
    out["reveal_fields"] = ojson::array();
    out["highlights"] = ojson::array();
    out["questions"] =
        clean_questions((plan.is_object() && plan.contains("questions")) ? plan["questions"]
                                                                        : ojson(nullptr));

    // patches
    ojson patches = (plan.is_object() && plan.contains("patches") && truthy(plan["patches"]))
                        ? plan["patches"]
                        : ojson::array();
    if (patches.is_array())
        for (const auto& p : patches) {
            if (!p.is_object()) continue;
            ojson kv = p.contains("key") ? p["key"] : ojson(nullptr);
            std::string key = truthy(kv) ? py_str(kv) : "";
            if (!fields.contains(key) || in_selectors(key)) continue;
            ojson pv = p.contains("value") ? p["value"] : ojson(nullptr);
            ojson v = pm_coerce_patch_value(fields[key], pv);
            if (!v.is_null()) {
                ojson patch = ojson::object();
                patch["key"] = key;
                patch["value"] = v;
                out["patches"].push_back(patch);
            }
        }

    // activate_well
    ojson actv = (plan.is_object() && plan.contains("activate_well")) ? plan["activate_well"]
                                                                     : ojson(nullptr);
    std::string act = truthy(actv) ? strip(py_str(actv)) : "";
    if (in_selectors(act)) {
        out["activate_well"] = act;
    } else if (out["patches"].empty() && out["questions"].empty()) {
        std::vector<std::string> empties = empty_selector_keys(fields, current);
        if (!empties.empty()) out["activate_well"] = empties[0];
    }

    // reveal_fields / highlights — keys present in `fields`
    ojson reveal = (plan.is_object() && plan.contains("reveal_fields") &&
                    truthy(plan["reveal_fields"]))
                       ? plan["reveal_fields"]
                       : ojson::array();
    if (reveal.is_array())
        for (const auto& k : reveal) {
            std::string key = py_str(k);
            if (fields.contains(key)) out["reveal_fields"].push_back(key);
        }
    ojson highlights = (plan.is_object() && plan.contains("highlights") &&
                        truthy(plan["highlights"]))
                           ? plan["highlights"]
                           : ojson::array();
    if (highlights.is_array())
        for (const auto& k : highlights) {
            std::string key = py_str(k);
            if (fields.contains(key)) out["highlights"].push_back(key);
        }

    // synthesized answer
    if (out["answer"].get<std::string>().empty()) {
        std::vector<std::string> bits;
        if (!out["patches"].empty())
            bits.push_back("I can fill " + std::to_string(out["patches"].size()) + " field(s).");
        if (!out["activate_well"].get<std::string>().empty())
            bits.push_back("Pick a reference for " + out["activate_well"].get<std::string>() + ".");
        if (!out["questions"].empty()) bits.push_back("I need one intent answer first.");
        std::string joined;
        for (size_t i = 0; i < bits.size(); ++i) {
            if (i) joined += " ";
            joined += bits[i];
        }
        out["answer"] = joined.empty() ? std::string("I do not see a safe field change to make yet.")
                                       : joined;
    }
    return out;
}

ojson pm_heuristic_plan(const ojson& form_schema, const ojson& current,
                        const std::string& intent) {
    ojson fields = pm_field_map(form_schema, true);
    ojson all_fields = pm_field_map(form_schema, false);
    std::string lower;
    lower.reserve(intent.size());
    for (char c : intent) lower += static_cast<char>(std::tolower((unsigned char)c));
    ojson patches = ojson::array();
    std::string activate;

    std::string ftype = (form_schema.is_object() && form_schema.contains("type") &&
                         form_schema["type"].is_string())
                            ? form_schema["type"].get<std::string>()
                            : "";
    auto has = [](const ojson& m, const char* k) { return m.is_object() && m.contains(k); };
    if (ftype == "extrude") {
        if (lower.find("up to") != std::string::npos && has(fields, "end") &&
            has(all_fields, "end_ref")) {
            ojson p = ojson::object();
            p["key"] = "end";
            p["value"] = "up_to_reference";
            patches.push_back(p);
            activate = "end_ref";
        } else if (lower.find("offset from reference") != std::string::npos &&
                   has(fields, "end") && has(all_fields, "end_ref")) {
            ojson p = ojson::object();
            p["key"] = "end";
            p["value"] = "offset_from_reference";
            patches.push_back(p);
            activate = "end_ref";
        } else if (lower.find("from reference") != std::string::npos && has(fields, "start") &&
                   has(all_fields, "start_ref")) {
            ojson p = ojson::object();
            p["key"] = "start";
            p["value"] = "from_reference";
            patches.push_back(p);
            activate = "start_ref";
        }
    }

    if (activate.empty()) {
        std::vector<std::string> empties = empty_selector_keys(fields, current);
        activate = empties.empty() ? "" : empties[0];
    }

    ojson out = ojson::object();
    out["answer"] = "I used the visible form options and will only ask you to pick real references.";
    out["patches"] = patches;
    out["activate_well"] = activate;
    out["reveal_fields"] = ojson::array();
    ojson hl = ojson::array();
    if (!activate.empty()) hl.push_back(activate);
    out["highlights"] = hl;
    out["questions"] = ojson::array();
    return out;
}

PmAssistPrompt build_pm_assist_prompt(const ojson& params, const ojson& doc,
                                      const std::string& doc_id, long revision,
                                      const ojson& feature_status) {
    ojson form_schema = (params.is_object() && params.contains("form_schema") &&
                         truthy(params["form_schema"]))
                            ? params["form_schema"]
                            : ojson::object();
    ojson cur = (params.is_object() && params.contains("current") && truthy(params["current"]))
                    ? params["current"]
                    : ojson::object();
    std::string intent = (params.is_object() && params.contains("intent") &&
                          truthy(params["intent"]))
                             ? py_str(params["intent"])
                             : "";
    ojson selected_refs = (params.is_object() && params.contains("selected_refs") &&
                           truthy(params["selected_refs"]))
                              ? params["selected_refs"]
                              : ojson::array();
    // body dict — insertion order matters for json.dumps.
    ojson body = ojson::object();
    body["document_context"] = build_doc_context(doc, doc_id, revision, feature_status);
    body["intent"] = intent;
    body["form_schema"] = form_schema;
    body["current"] = cur;
    body["selected_refs"] = selected_refs;
    // json.dumps(body, ensure_ascii=False) — DEFAULT separators, literal non-ASCII.
    std::string prompt = py_json_dumps_default(body, /*ensure_ascii=*/false);
    // len(prompt) is Python code-point length.
    int n = 0;
    for (unsigned char c : prompt)
        if ((c & 0xC0) != 0x80) ++n;
    if (n > 12000) {
        // prompt[:12000] in code points
        int seen = 0;
        size_t i = 0;
        while (i < prompt.size() && seen < 12000) {
            unsigned char c = static_cast<unsigned char>(prompt[i]);
            size_t adv = (c & 0x80) == 0 ? 1 : (c & 0xE0) == 0xC0 ? 2 : (c & 0xF0) == 0xE0 ? 3 : 4;
            i += adv;
            ++seen;
        }
        prompt = prompt.substr(0, i) + "\n...[truncated]";
    }
    PmAssistPrompt out;
    out.system = std::string("ROLE: ") + kPmAssistRole + "\n" + kPmAssistSystem;
    out.body = prompt;
    return out;
}

ojson assist_property_manager(const ojson& params, const ojson& doc, const std::string& doc_id,
                              long revision, const ojson& feature_status,
                              const PmCompleteFn& complete) {
    ojson form_schema = (params.is_object() && params.contains("form_schema") &&
                         truthy(params["form_schema"]))
                            ? params["form_schema"]
                            : ojson::object();
    ojson cur = (params.is_object() && params.contains("current") && truthy(params["current"]))
                    ? params["current"]
                    : ojson::object();
    std::string intent = (params.is_object() && params.contains("intent") &&
                          truthy(params["intent"]))
                             ? py_str(params["intent"])
                             : "";

    ojson plan = ojson::object();
    std::string provider_error;
    if (complete) {
        try {
            PmAssistPrompt p = build_pm_assist_prompt(params, doc, doc_id, revision, feature_status);
            std::string err;
            std::string reply = complete(p.system, p.body, &err);
            if (!err.empty()) {
                provider_error = err;  // mirror the except branch
            } else {
                plan = pm_extract_json(reply);
            }
        } catch (const std::exception& exc) {
            provider_error = exc.what();
        }
    }

    if (plan.empty()) {
        plan = pm_heuristic_plan(form_schema, cur, intent);
        if (!provider_error.empty()) {
            // plan.get("answer", "") + f" Provider fallback: {err}"
            std::string a = (plan.contains("answer") && plan["answer"].is_string())
                                ? plan["answer"].get<std::string>()
                                : "";
            plan["answer"] = a + " Provider fallback: " + provider_error;
        }
    }

    return pm_validate_plan(plan, form_schema, cur);
}

}  // namespace agent
}  // namespace chudware
