#include "ask_form.h"

#include <nlohmann/json.hpp>
#include <sstream>

namespace aicad::ui {

namespace {

AskFieldKind kindFromString(const std::string& k, bool& ok) {
    ok = true;
    if (k == "choice")       return AskFieldKind::Choice;
    if (k == "dimension")    return AskFieldKind::Dimension;
    if (k == "text")         return AskFieldKind::Text;
    if (k == "file_request") return AskFieldKind::FileRequest;
    if (k == "approval")     return AskFieldKind::Approval;
    ok = false;
    return AskFieldKind::Text;
}

// "<num> <unit>" -> {value, unit}. Tolerant of a bare number (unit left empty).
void parseDimensionRaw(const std::string& raw, double& value, std::string& unit) {
    value = 0.0;
    unit.clear();
    std::istringstream iss(raw);
    iss >> value;
    std::string rest;
    std::getline(iss, rest);
    size_t b = rest.find_first_not_of(" \t\r\n");
    if (b == std::string::npos) return;
    size_t e = rest.find_last_not_of(" \t\r\n");
    unit = rest.substr(b, e - b + 1);
}

} // namespace

AskForm parseAskBatch(const std::string& batchJson) {
    AskForm form;
    nlohmann::json j = nlohmann::json::parse(batchJson, nullptr, /*allow_exceptions=*/false);
    if (j.is_discarded() || !j.is_object()) return form;

    form.batchId = j.value("batch_id", std::string());
    form.scope = j.value("scope", std::string("run"));
    form.blocking = j.value("blocking", true);

    auto qit = j.find("questions");
    if (qit == j.end() || !qit->is_array()) return form;

    for (const auto& q : *qit) {
        if (!q.is_object()) continue;
        bool kindOk = false;
        AskFieldKind kind = kindFromString(q.value("kind", std::string()), kindOk);
        if (!kindOk) continue;  // unknown kind: tolerate by skipping (contract §10)

        AskField f;
        f.id = q.value("id", std::string());
        // "prompt" is the current field name; "ask" is the tolerated legacy name (§2).
        f.prompt = q.contains("prompt") ? q.value("prompt", std::string())
                                         : q.value("ask", std::string());
        f.why = q.value("why", std::string());
        f.kind = kind;
        f.required = q.value("required", false);
        f.family = q.value("family", std::string("length"));
        f.suggestedJson = q.contains("suggested") ? q["suggested"].dump() : std::string("null");

        if (kind == AskFieldKind::Choice) {
            f.multi = q.value("multi", false);
            auto oit = q.find("options");
            if (oit != q.end() && oit->is_array()) {
                for (const auto& o : *oit) {
                    if (o.is_object() && o.contains("value"))
                        f.options.push_back(o["value"].is_string() ? o["value"].get<std::string>()
                                                                     : o["value"].dump());
                    else if (o.is_string())
                        f.options.push_back(o.get<std::string>());
                }
            }
        }
        if (kind == AskFieldKind::Text) {
            f.multiline = q.value("multiline", false);
        }

        form.fields.push_back(std::move(f));
    }

    return form;
}

std::string buildAnswersJson(const AskForm& form,
                              const std::map<std::string, std::string>& rawValues) {
    nlohmann::json answers = nlohmann::json::object();

    for (const auto& f : form.fields) {
        auto it = rawValues.find(f.id);
        if (it == rawValues.end()) continue;  // unanswered: omit (§4 tolerates missing keys)
        const std::string& raw = it->second;

        nlohmann::json value;
        switch (f.kind) {
        case AskFieldKind::Dimension: {
            double v = 0.0;
            std::string unit;
            parseDimensionRaw(raw, v, unit);
            value = nlohmann::json{{"value", v}, {"unit", unit}};
            break;
        }
        case AskFieldKind::Approval:
            value = (raw == "true" || raw == "1");
            break;
        case AskFieldKind::FileRequest:
            value = nlohmann::json{{"paths", raw.empty() ? nlohmann::json::array()
                                                           : nlohmann::json::array({raw})}};
            break;
        case AskFieldKind::Choice:
            if (f.multi) {
                // multi-select: rawValues[id] is a JSON array string (e.g. `["a","b"]`)
                // produced by the inline card; parse it back to a proper JSON array.
                nlohmann::json arr = nlohmann::json::parse(raw, nullptr, false);
                value = (arr.is_array()) ? arr : nlohmann::json::array();
            } else {
                value = raw;
            }
            break;
        case AskFieldKind::Text:
        default:
            value = raw;
            break;
        }

        answers[f.id] = nlohmann::json{{"value", value}, {"source", "user"}};
    }

    nlohmann::json env{
        {"schema", "aicad.interaction.answers/1"},
        {"batch_id", form.batchId},
        {"answers", answers},
    };
    return env.dump();
}

} // namespace aicad::ui
