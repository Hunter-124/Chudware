#include "manufacturing/invalidation.h"

#include <algorithm>
#include <stdexcept>

namespace chudware::mfg {

std::string_view to_string(InvalidationClass value) {
    switch (value) {
        case InvalidationClass::Falsified: return "falsified";
        case InvalidationClass::Superseded: return "superseded";
    }
    throw std::invalid_argument("invalid InvalidationClass");
}

std::optional<InvalidationClass> parse_invalidation_class(std::string_view text) {
    if (text == "falsified") return InvalidationClass::Falsified;
    if (text == "superseded") return InvalidationClass::Superseded;
    return std::nullopt;
}

bool InvalidationNotice::valid(std::string* error) const {
    auto fail = [&](std::string message) {
        if (error) *error = std::move(message);
        return false;
    };
    if (reason.empty()) return fail("invalidation reason is required");
    if (why.empty()) return fail("invalidation why is required");
    if (affected.empty()) return fail("invalidation affected must not be empty");
    if (at_seq == 0) return fail("invalidation at_seq must be durable");
    if (classification == InvalidationClass::Superseded &&
        (!superseded_by || superseded_by->empty()))
        return fail("superseded invalidation requires superseded_by");
    if (classification == InvalidationClass::Falsified && superseded_by)
        return fail("falsified invalidation must not carry superseded_by");
    return true;
}

void to_json(nlohmann::json& json, const InvalidationNotice& value) {
    std::string error;
    if (!value.valid(&error)) throw std::invalid_argument(error);
    json = {{"class", to_string(value.classification)}, {"reason", value.reason},
            {"why", value.why}, {"affected", value.affected}, {"at_seq", value.at_seq}};
    if (value.superseded_by) json["superseded_by"] = *value.superseded_by;
}

void from_json(const nlohmann::json& json, InvalidationNotice& value) {
    const auto classification = parse_invalidation_class(json.at("class").get<std::string>());
    if (!classification) throw std::invalid_argument("invalid invalidation class");
    value.classification = *classification;
    value.reason = json.at("reason").get<std::string>();
    value.why = json.at("why").get<std::string>();
    value.affected = json.at("affected").get<std::vector<std::string>>();
    value.at_seq = json.at("at_seq").get<device::Seq>();
    value.superseded_by.reset();
    if (json.contains("superseded_by") && !json.at("superseded_by").is_null())
        value.superseded_by = json.at("superseded_by").get<std::string>();
    std::string error;
    if (!value.valid(&error)) throw std::invalid_argument(error);
}

InvalidationApplyReport InvalidationEngine::apply(
        const InvalidationNotice& notice, std::span<DurableResultState> rows) const {
    std::string error;
    if (!notice.valid(&error)) throw std::invalid_argument(error);
    InvalidationApplyReport report;
    for (auto& row : rows) {
        if (row.seq > notice.at_seq ||
            std::find(notice.affected.begin(), notice.affected.end(), row.ref) == notice.affected.end())
            continue;
        ++report.matched;
        if (notice.classification == InvalidationClass::Falsified) {
            if (row.cleared && !row.current && row.value.is_null() && !row.superseded_by) continue;
            row.value = nullptr;
            row.current = false;
            row.cleared = true;
            row.superseded_by.reset();
            ++report.changed;
        } else {
            if (!row.cleared && !row.current && row.superseded_by == notice.superseded_by) continue;
            if (row.cleared) continue;  // falsification is stronger and never resurrected by supersession
            row.current = false;
            row.superseded_by = notice.superseded_by;
            ++report.changed;
        }
    }
    return report;
}

}  // namespace chudware::mfg
