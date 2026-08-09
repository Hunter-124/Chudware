#pragma once

#include <map>
#include <optional>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

#include <nlohmann/json.hpp>

namespace chudware::device {

enum class CapabilityLevel {
    Plan,
    Generate,
    Post,
    Parse,
    Simulate,
    Monitor,
    Transport,
    Execute,
    SafetyIntegration,
};

inline constexpr CapabilityLevel capability_levels[] = {
    CapabilityLevel::Plan, CapabilityLevel::Generate, CapabilityLevel::Post,
    CapabilityLevel::Parse, CapabilityLevel::Simulate, CapabilityLevel::Monitor,
    CapabilityLevel::Transport, CapabilityLevel::Execute, CapabilityLevel::SafetyIntegration};

inline std::string_view to_string(CapabilityLevel value) {
    switch (value) {
        case CapabilityLevel::Plan: return "plan";
        case CapabilityLevel::Generate: return "generate";
        case CapabilityLevel::Post: return "post";
        case CapabilityLevel::Parse: return "parse";
        case CapabilityLevel::Simulate: return "simulate";
        case CapabilityLevel::Monitor: return "monitor";
        case CapabilityLevel::Transport: return "transport";
        case CapabilityLevel::Execute: return "execute";
        case CapabilityLevel::SafetyIntegration: return "safety_integration";
    }
    throw std::invalid_argument("invalid CapabilityLevel");
}

inline std::optional<CapabilityLevel> parse_capability_level(std::string_view text) {
    for (const auto level : capability_levels)
        if (text == to_string(level)) return level;
    return std::nullopt;
}

enum class CapabilityTruthValue { Verified, Declared, Assumed, Unsupported, NotApplicable };

inline std::string_view to_string(CapabilityTruthValue value) {
    switch (value) {
        case CapabilityTruthValue::Verified: return "verified";
        case CapabilityTruthValue::Declared: return "declared";
        case CapabilityTruthValue::Assumed: return "assumed";
        case CapabilityTruthValue::Unsupported: return "unsupported";
        case CapabilityTruthValue::NotApplicable: return "not_applicable";
    }
    throw std::invalid_argument("invalid CapabilityTruthValue");
}

inline std::optional<CapabilityTruthValue> parse_capability_truth(std::string_view text) {
    if (text == "verified") return CapabilityTruthValue::Verified;
    if (text == "declared") return CapabilityTruthValue::Declared;
    if (text == "assumed") return CapabilityTruthValue::Assumed;
    if (text == "unsupported") return CapabilityTruthValue::Unsupported;
    if (text == "not_applicable") return CapabilityTruthValue::NotApplicable;
    return std::nullopt;
}

enum class EmulatorStage { None, E0, E1, E2, E3, E4, E5, E6 };

inline std::string_view to_string(EmulatorStage value) {
    switch (value) {
        case EmulatorStage::None: return "none";
        case EmulatorStage::E0: return "E0";
        case EmulatorStage::E1: return "E1";
        case EmulatorStage::E2: return "E2";
        case EmulatorStage::E3: return "E3";
        case EmulatorStage::E4: return "E4";
        case EmulatorStage::E5: return "E5";
        case EmulatorStage::E6: return "E6";
    }
    throw std::invalid_argument("invalid EmulatorStage");
}

inline std::optional<EmulatorStage> parse_emulator_stage(std::string_view text) {
    if (text == "none") return EmulatorStage::None;
    if (text == "E0") return EmulatorStage::E0;
    if (text == "E1") return EmulatorStage::E1;
    if (text == "E2") return EmulatorStage::E2;
    if (text == "E3") return EmulatorStage::E3;
    if (text == "E4") return EmulatorStage::E4;
    if (text == "E5") return EmulatorStage::E5;
    if (text == "E6") return EmulatorStage::E6;
    return std::nullopt;
}

struct CapabilityTruth {
    CapabilityLevel level = CapabilityLevel::Plan;
    CapabilityTruthValue truth = CapabilityTruthValue::Unsupported;
    std::vector<std::string> evidence_ref;
    EmulatorStage emulator_stage = EmulatorStage::None;
    std::optional<CapabilityLevel> missing_level;
    std::string why;

    bool valid(std::string* error = nullptr) const {
        auto fail = [&](std::string message) {
            if (error) *error = std::move(message);
            return false;
        };
        if (truth == CapabilityTruthValue::Assumed) {
            if (!missing_level) return fail("assumed truth requires missing_level");
            if (why.empty()) return fail("assumed truth requires why");
        } else if (missing_level) {
            return fail("missing_level is valid only when truth is assumed");
        }
        if (truth != CapabilityTruthValue::Verified && why.empty())
            return fail("non-verified truth requires why");
        return true;
    }

    bool enables_action() const { return truth == CapabilityTruthValue::Verified; }
};

inline void to_json(nlohmann::json& json, const CapabilityTruth& value) {
    std::string error;
    if (!value.valid(&error)) throw std::invalid_argument(error);
    json = {{"level", to_string(value.level)}, {"truth", to_string(value.truth)},
            {"evidence_ref", value.evidence_ref}, {"emulator_stage", to_string(value.emulator_stage)},
            {"why", value.why}};
    if (value.missing_level) json["missing_level"] = to_string(*value.missing_level);
}

inline void from_json(const nlohmann::json& json, CapabilityTruth& value) {
    const auto level = parse_capability_level(json.at("level").get<std::string>());
    const auto truth = parse_capability_truth(json.at("truth").get<std::string>());
    const auto stage = parse_emulator_stage(json.value("emulator_stage", "none"));
    if (!level || !truth || !stage) throw std::invalid_argument("invalid CapabilityTruth vocabulary");
    value.level = *level;
    value.truth = *truth;
    value.emulator_stage = *stage;
    value.evidence_ref = json.value("evidence_ref", std::vector<std::string>{});
    value.why = json.value("why", std::string{});
    value.missing_level.reset();
    if (json.contains("missing_level")) {
        const auto missing = parse_capability_level(json.at("missing_level").get<std::string>());
        if (!missing) throw std::invalid_argument("invalid missing_level");
        value.missing_level = *missing;
    }
    std::string error;
    if (!value.valid(&error)) throw std::invalid_argument(error);
}

struct CapabilityReport {
    std::string subject_id;
    std::map<CapabilityLevel, CapabilityTruth> levels;
    CapabilityLevel max_advertised = CapabilityLevel::Plan;
    EmulatorStage emulator_stage_passed = EmulatorStage::None;

    // C++17-clean on purpose: this header is included from `chudware_cam`, which still
    // compiles as gnu++17, so no std::map::contains here.
    bool complete() const {
        for (const auto level : capability_levels) {
            const auto it = levels.find(level);
            if (it == levels.end() || it->second.level != level || !it->second.valid()) return false;
        }
        return true;
    }
};

inline void to_json(nlohmann::json& json, const CapabilityReport& value) {
    nlohmann::json levels = nlohmann::json::object();
    for (const auto& [level, truth] : value.levels) levels[std::string(to_string(level))] = truth;
    json = {{"subject_id", value.subject_id}, {"levels", std::move(levels)},
            {"max_advertised", to_string(value.max_advertised)},
            {"emulator_stage_passed", to_string(value.emulator_stage_passed)}};
}

}  // namespace chudware::device
