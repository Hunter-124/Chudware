#pragma once

#include <cstddef>
#include <filesystem>
#include <functional>
#include <memory>
#include <string>
#include <string_view>
#include <vector>

#include <nlohmann/json.hpp>

#include "device/device_id.h"

namespace chudware::mfg {

struct JournalRecord {
    device::Seq seq = 0;
    device::LaneEpoch epoch;
    std::string kind;
    std::string subject;
    nlohmann::json body;
    nlohmann::json raw;
    uint64_t t_mono_ns = 0;

    friend bool operator==(const JournalRecord&, const JournalRecord&) = default;
};

void to_json(nlohmann::json& json, const JournalRecord& value);
void from_json(const nlohmann::json& json, JournalRecord& value);

struct JournalLimits {
    size_t record_bytes = 256 * 1024;
    uint64_t total_bytes = 64 * 1024 * 1024;
    size_t total_records = 65536;
    size_t replay_records = 1024;
    size_t replay_bytes = 4 * 1024 * 1024;
};

class Journal {
public:
    Journal(std::filesystem::path install_directory, device::LaneEpoch epoch,
            JournalLimits limits = {});
    ~Journal();

    Journal(const Journal&) = delete;
    Journal& operator=(const Journal&) = delete;
    Journal(Journal&&) noexcept;
    Journal& operator=(Journal&&) noexcept;

    device::Seq append(std::string_view kind, std::string_view subject,
                       const nlohmann::json& body, const nlohmann::json& raw = nullptr);
    device::Seq head() const;
    device::Seq epochFloor() const;
    std::vector<JournalRecord> replay(device::Seq from = 1) const;
    // Streams one bounded record at a time; false stops without loading the rest.
    void visit(device::Seq from,
               const std::function<bool(const JournalRecord&)>& visitor) const;
    const JournalLimits& limits() const;

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

}  // namespace chudware::mfg
