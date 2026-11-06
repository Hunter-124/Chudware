#pragma once

#include <nlohmann/json.hpp>

namespace chudware::doc::scad {

// Managed, deliberately bounded native-history projection; not a SCAD parser.
nlohmann::json project_native(const nlohmann::json& document, double mm_per_source_unit);
nlohmann::json restore_native(const nlohmann::json& params);

}  // namespace chudware::doc::scad
