#pragma once

#include <cstdint>
#include <optional>
#include <stdexcept>
#include <string>
#include <string_view>

#include <nlohmann/json.hpp>

#include "device/device_id.h"

namespace chudware::mfg {

enum class FreshState { Ok, Stale, Unknown, Refused, Absent };

enum class FreshSource { Probed, Simulated, Advisory, Declared };

inline std::string_view to_string(FreshState value) {
    switch (value) {
        case FreshState::Ok: return "ok";
        case FreshState::Stale: return "stale";
        case FreshState::Unknown: return "unknown";
        case FreshState::Refused: return "refused";
        case FreshState::Absent: return "absent";
    }
    throw std::invalid_argument("invalid FreshState");
}

inline std::optional<FreshState> parse_fresh_state(std::string_view text) {
    if (text == "ok") return FreshState::Ok;
    if (text == "stale") return FreshState::Stale;
    if (text == "unknown") return FreshState::Unknown;
    if (text == "refused") return FreshState::Refused;
    if (text == "absent") return FreshState::Absent;
    return std::nullopt;
}

inline std::string_view to_string(FreshSource value) {
    switch (value) {
        case FreshSource::Probed: return "probed";
        case FreshSource::Simulated: return "simulated";
        case FreshSource::Advisory: return "advisory";
        case FreshSource::Declared: return "declared";
    }
    throw std::invalid_argument("invalid FreshSource");
}

inline std::optional<FreshSource> parse_fresh_source(std::string_view text) {
    if (text == "probed") return FreshSource::Probed;
    if (text == "simulated") return FreshSource::Simulated;
    if (text == "advisory") return FreshSource::Advisory;
    if (text == "declared") return FreshSource::Declared;
    return std::nullopt;
}

template <typename T>
struct LastOk {
    T v;
    uint64_t t_mono_ns = 0;
    device::Seq seq = 0;
};

template <typename T>
struct FreshValue {
    std::optional<T> v;
    FreshState state = FreshState::Unknown;
    uint64_t t_mono_ns = 0;
    device::Seq seq = 0;
    uint64_t age_ms = 0;
    std::optional<uint64_t> ttl_ms;
    FreshSource src = FreshSource::Declared;
    std::string why = "not observed";
    std::optional<LastOk<T>> last_ok;

    void normalize() {
        if (state != FreshState::Ok) v.reset();
        if (state == FreshState::Ok) why.clear();
        if (state != FreshState::Ok && why.empty()) why = "fresh value is not ok";
    }

    void force_sequence_gap(device::Seq expected, device::Seq observed) {
        state = FreshState::Unknown;
        v.reset();
        why = "sequence gap " + std::to_string(expected) + ".." +
              std::to_string(observed > 0 ? observed - 1 : 0);
    }
};

template <typename T>
inline void to_json(nlohmann::json& json, const FreshValue<T>& value) {
    json = nlohmann::json::object();
    if (value.state == FreshState::Ok && value.v) json["v"] = *value.v;
    else json["v"] = nullptr;
    json["state"] = to_string(value.state);
    json["t_mono_ns"] = value.t_mono_ns;
    json["seq"] = value.seq;
    json["age_ms"] = value.age_ms;
    if (value.ttl_ms) json["ttl_ms"] = *value.ttl_ms;
    else json["ttl_ms"] = nullptr;
    json["src"] = to_string(value.src);
    json["why"] = value.state == FreshState::Ok ? std::string{} : value.why;
    if (value.last_ok) {
        json["last_ok"] = {{"v", value.last_ok->v}, {"t_mono_ns", value.last_ok->t_mono_ns},
                           {"seq", value.last_ok->seq}};
    }
}

template <typename T>
inline void from_json(const nlohmann::json& json, FreshValue<T>& value) {
    const auto state = parse_fresh_state(json.at("state").template get<std::string>());
    const auto source = parse_fresh_source(json.at("src").template get<std::string>());
    if (!state || !source) throw std::invalid_argument("invalid FreshValue vocabulary");
    value.state = *state;
    value.src = *source;
    value.t_mono_ns = json.at("t_mono_ns").template get<uint64_t>();
    value.seq = json.at("seq").template get<device::Seq>();
    value.age_ms = json.at("age_ms").template get<uint64_t>();
    value.ttl_ms.reset();
    if (json.contains("ttl_ms") && !json.at("ttl_ms").is_null())
        value.ttl_ms = json.at("ttl_ms").template get<uint64_t>();
    value.v.reset();
    if (value.state == FreshState::Ok && json.contains("v") && !json.at("v").is_null())
        value.v = json.at("v").template get<T>();
    value.why = json.value("why", std::string{});
    value.last_ok.reset();
    if (json.contains("last_ok") && !json.at("last_ok").is_null()) {
        const auto& prior = json.at("last_ok");
        value.last_ok = LastOk<T>{prior.at("v").template get<T>(),
                                  prior.at("t_mono_ns").template get<uint64_t>(),
                                  prior.at("seq").template get<device::Seq>()};
    }
    if (value.state != FreshState::Ok && value.why.empty())
        throw std::invalid_argument("non-ok FreshValue requires why");
    value.normalize();
}

}  // namespace chudware::mfg
