#pragma once

#include <algorithm>
#include <array>
#include <chrono>
#include <cstdint>
#include <mutex>
#include <map>
#include <optional>
#include <random>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

#include <nlohmann/json.hpp>

#include "util/sha256.h"

namespace chudware::device {

enum class MachineClass {
    OpenFrameRouter,
    OpenFrameMill,
    EnclosedInterlockedMill,
    FdmPrinterOpen,
    FdmPrinterEnclosed,
    Emulated,
};

inline std::string_view to_string(MachineClass value) {
    switch (value) {
        case MachineClass::OpenFrameRouter: return "open_frame_router";
        case MachineClass::OpenFrameMill: return "open_frame_mill";
        case MachineClass::EnclosedInterlockedMill: return "enclosed_interlocked_mill";
        case MachineClass::FdmPrinterOpen: return "fdm_printer_open";
        case MachineClass::FdmPrinterEnclosed: return "fdm_printer_enclosed";
        case MachineClass::Emulated: return "emulated";
    }
    throw std::invalid_argument("invalid MachineClass");
}

inline std::optional<MachineClass> parse_machine_class(std::string_view text) {
    if (text == "open_frame_router") return MachineClass::OpenFrameRouter;
    if (text == "open_frame_mill") return MachineClass::OpenFrameMill;
    if (text == "enclosed_interlocked_mill") return MachineClass::EnclosedInterlockedMill;
    if (text == "fdm_printer_open") return MachineClass::FdmPrinterOpen;
    if (text == "fdm_printer_enclosed") return MachineClass::FdmPrinterEnclosed;
    if (text == "emulated") return MachineClass::Emulated;
    return std::nullopt;
}

namespace detail {

inline nlohmann::json canonical_json(const nlohmann::json& value) {
    if (value.is_object()) {
        std::vector<std::string> keys;
        keys.reserve(value.size());
        for (auto it = value.begin(); it != value.end(); ++it) keys.push_back(it.key());
        std::sort(keys.begin(), keys.end());
        nlohmann::json result = nlohmann::json::object();
        for (const auto& key : keys) result[key] = canonical_json(value.at(key));
        return result;
    }
    if (value.is_array()) {
        nlohmann::json result = nlohmann::json::array();
        for (const auto& item : value) result.push_back(canonical_json(item));
        return result;
    }
    return value;
}

inline bool valid_ulid(std::string_view text) {
    if (text.size() != 26 || text.front() > '7') return false;
    static constexpr std::string_view alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";
    return std::all_of(text.begin(), text.end(), [](char c) {
        return alphabet.find(c) != std::string_view::npos;
    });
}

inline std::string mint_ulid() {
    std::array<uint8_t, 16> bytes{};
    const auto now_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
                            std::chrono::system_clock::now().time_since_epoch()).count();
    const uint64_t timestamp = static_cast<uint64_t>(now_ms);
    for (int i = 0; i < 6; ++i) bytes[i] = static_cast<uint8_t>(timestamp >> (40 - i * 8));
    static std::mutex mutex;
    static std::mt19937_64 generator(std::random_device{}());
    {
        std::lock_guard lock(mutex);
        for (size_t i = 6; i < bytes.size(); i += 8) {
            const uint64_t random = generator();
            const size_t count = std::min<size_t>(8, bytes.size() - i);
            for (size_t j = 0; j < count; ++j)
                bytes[i + j] = static_cast<uint8_t>(random >> (56 - j * 8));
        }
    }
    static constexpr char alphabet[] = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";
    std::string result(26, '0');
    for (int out = 0; out < 26; ++out) {
        unsigned value = 0;
        for (int bit = 0; bit < 5; ++bit) {
            const int source_bit = out * 5 + bit - 2;
            value <<= 1;
            if (source_bit >= 0 && source_bit < 128) {
                value |= (bytes[static_cast<size_t>(source_bit / 8)] >>
                          (7 - source_bit % 8)) & 1u;
            }
        }
        result[static_cast<size_t>(out)] = alphabet[value];
    }
    return result;
}

inline bool valid_kind(std::string_view kind) {
    if (kind.empty()) return false;
    return std::all_of(kind.begin(), kind.end(), [](char c) {
        return (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '_' || c == '.' || c == '-';
    });
}

}  // namespace detail

struct ControllerIdentitySnapshot {
    std::string firmware_banner;
    std::string build_options;
    std::map<std::string, std::string> settings;
    uint32_t axis_count = 0;
    MachineClass machine_class = MachineClass::Emulated;
    std::string transport_kind;
    std::string transport_endpoint_id;
    std::vector<std::string> installed_accessories;

    nlohmann::json canonical() const {
        auto accessories = installed_accessories;
        std::sort(accessories.begin(), accessories.end());
        return {{"firmware_banner", firmware_banner}, {"build_options", build_options},
                {"settings", settings}, {"axis_count", axis_count},
                {"machine_class", to_string(machine_class)}, {"transport_kind", transport_kind},
                {"transport_endpoint_id", transport_endpoint_id},
                {"installed_accessories", accessories}};
    }
};

struct PrinterIdentitySnapshot {
    std::string firmware_name_or_software_version;
    std::string config_hash;
    std::string kinematics;
    uint32_t extruder_count = 0;
    std::string adapter_kind;

    nlohmann::json canonical() const {
        return {{"firmware_name_or_software_version", firmware_name_or_software_version},
                {"config_hash", config_hash}, {"kinematics", kinematics},
                {"extruder_count", extruder_count}, {"adapter_kind", adapter_kind}};
    }
};

class MachineFingerprint {
public:
    static MachineFingerprint from_identity(const nlohmann::json& identity_snapshot) {
        return MachineFingerprint(util::sha256_hex(detail::canonical_json(identity_snapshot).dump()));
    }
    static MachineFingerprint from_identity(const ControllerIdentitySnapshot& identity_snapshot) {
        return from_identity(identity_snapshot.canonical());
    }

    static MachineFingerprint from_identity(const PrinterIdentitySnapshot& identity_snapshot) {
        return from_identity(identity_snapshot.canonical());
    }

    static std::optional<MachineFingerprint> parse(std::string_view text) {
        if (text.size() != 64 || !std::all_of(text.begin(), text.end(), [](char c) {
                return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f');
            })) return std::nullopt;
        return MachineFingerprint(std::string(text));
    }

    const std::string& str() const { return value_; }
    friend bool operator==(const MachineFingerprint&, const MachineFingerprint&) = default;

private:
    explicit MachineFingerprint(std::string value) : value_(std::move(value)) {}
    std::string value_;
};

class DeviceId {
public:
    static DeviceId mint(MachineClass machine_class, const MachineFingerprint& fingerprint) {
        return DeviceId("dev:" + std::string(to_string(machine_class)) + ":" +
                        fingerprint.str().substr(0, 12), machine_class);
    }

    static std::optional<DeviceId> parse(std::string_view text) {
        if (!text.starts_with("dev:")) return std::nullopt;
        const size_t split = text.find(':', 4);
        if (split == std::string_view::npos) return std::nullopt;
        const auto machine_class = parse_machine_class(text.substr(4, split - 4));
        const auto hash = text.substr(split + 1);
        if (!machine_class || hash.size() != 12 || !std::all_of(hash.begin(), hash.end(), [](char c) {
                return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f');
            })) return std::nullopt;
        return DeviceId(std::string(text), *machine_class);
    }

    const std::string& str() const { return value_; }
    MachineClass machine_class() const { return machine_class_; }
    friend bool operator==(const DeviceId&, const DeviceId&) = default;

private:
    DeviceId(std::string value, MachineClass machine_class)
        : value_(std::move(value)), machine_class_(machine_class) {}
    std::string value_;
    MachineClass machine_class_;
};

template <typename Tag>
class PrefixedUlid {
public:
    static PrefixedUlid mint() { return PrefixedUlid(std::string(Tag::prefix) + detail::mint_ulid()); }
    static std::optional<PrefixedUlid> parse(std::string_view text) {
        if (!text.starts_with(Tag::prefix) || !detail::valid_ulid(text.substr(Tag::prefix.size())))
            return std::nullopt;
        return PrefixedUlid(std::string(text));
    }
    const std::string& str() const { return value_; }
    friend bool operator==(const PrefixedUlid&, const PrefixedUlid&) = default;

private:
    explicit PrefixedUlid(std::string value) : value_(std::move(value)) {}
    std::string value_;
};

struct LeaseTag { static constexpr std::string_view prefix = "lease:"; };
struct RunTag { static constexpr std::string_view prefix = "run:"; };
struct ApprovalTag { static constexpr std::string_view prefix = "appr:"; };
using LeaseId = PrefixedUlid<LeaseTag>;
using RunId = PrefixedUlid<RunTag>;
using ApprovalId = PrefixedUlid<ApprovalTag>;

class ResultRef {
public:
    static ResultRef mint(std::string_view kind) {
        if (!detail::valid_kind(kind)) throw std::invalid_argument("invalid result kind");
        return ResultRef("res:" + std::string(kind) + ":" + detail::mint_ulid(), std::string(kind));
    }
    static std::optional<ResultRef> parse(std::string_view text) {
        if (!text.starts_with("res:")) return std::nullopt;
        const size_t split = text.rfind(':');
        if (split <= 4 || !detail::valid_kind(text.substr(4, split - 4)) ||
            !detail::valid_ulid(text.substr(split + 1))) return std::nullopt;
        return ResultRef(std::string(text), std::string(text.substr(4, split - 4)));
    }
    const std::string& str() const { return value_; }
    const std::string& kind() const { return kind_; }
    friend bool operator==(const ResultRef&, const ResultRef&) = default;

private:
    ResultRef(std::string value, std::string kind)
        : value_(std::move(value)), kind_(std::move(kind)) {}
    std::string value_;
    std::string kind_;
};

struct LaneEpoch {
    std::string process_start_id;
    std::string lane_id;
    uint32_t epoch = 0;

    static LaneEpoch mint(std::string lane_id, uint32_t epoch = 0) {
        if (lane_id.empty()) throw std::invalid_argument("lane_id must not be empty");
        return {detail::mint_ulid(), std::move(lane_id), epoch};
    }

    static std::optional<LaneEpoch> parse(const nlohmann::json& value) {
        if (!value.is_object() || !value.contains("process_start_id") ||
            !value.contains("lane_id") || !value.contains("epoch") ||
            !value["process_start_id"].is_string() || !value["lane_id"].is_string() ||
            !value["epoch"].is_number_unsigned()) return std::nullopt;
        LaneEpoch result{value["process_start_id"].get<std::string>(),
                         value["lane_id"].get<std::string>(), value["epoch"].get<uint32_t>()};
        if (!detail::valid_ulid(result.process_start_id) || result.lane_id.empty()) return std::nullopt;
        return result;
    }

    friend bool operator==(const LaneEpoch&, const LaneEpoch&) = default;
};

inline void to_json(nlohmann::json& json, const LaneEpoch& value) {
    json = {{"process_start_id", value.process_start_id}, {"lane_id", value.lane_id},
            {"epoch", value.epoch}};
}

using Seq = uint64_t;

}  // namespace chudware::device
