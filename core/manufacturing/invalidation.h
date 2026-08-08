#pragma once

#include <optional>
#include <span>
#include <string>
#include <string_view>
#include <vector>

#include <nlohmann/json.hpp>

#include "device/device_id.h"

namespace chudware::mfg {

enum class InvalidationClass { Falsified, Superseded };

std::string_view to_string(InvalidationClass value);
std::optional<InvalidationClass> parse_invalidation_class(std::string_view text);

struct InvalidationNotice {
    InvalidationClass classification = InvalidationClass::Falsified;
    std::string reason;
    std::string why;
    std::vector<std::string> affected;
    device::Seq at_seq = 0;
    std::optional<std::string> superseded_by;

    bool valid(std::string* error = nullptr) const;
};

void to_json(nlohmann::json& json, const InvalidationNotice& value);
void from_json(const nlohmann::json& json, InvalidationNotice& value);

struct DurableResultState {
    std::string ref;
    device::Seq seq = 0;
    nlohmann::json value;
    bool current = true;
    bool cleared = false;
    std::optional<std::string> superseded_by;
};

struct InvalidationApplyReport {
    size_t matched = 0;
    size_t changed = 0;
};

class InvalidationEngine {
public:
    InvalidationApplyReport apply(const InvalidationNotice& notice,
                                  std::span<DurableResultState> rows) const;
};

}  // namespace chudware::mfg
