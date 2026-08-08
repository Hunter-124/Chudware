#pragma once

#include <filesystem>
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

class Journal {
public:
    Journal(std::filesystem::path install_directory, device::LaneEpoch epoch);
    ~Journal();

    Journal(const Journal&) = delete;
    Journal& operator=(const Journal&) = delete;
    Journal(Journal&&) noexcept;
    Journal& operator=(Journal&&) noexcept;

    device::Seq append(std::string_view kind, std::string_view subject,
                       nlohmann::json body, nlohmann::json raw = nullptr);
    device::Seq head() const;
    device::Seq epochFloor() const;
    std::vector<JournalRecord> replay(device::Seq from = 1) const;

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

}  // namespace chudware::mfg
