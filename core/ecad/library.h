// KiCad footprint resolution, with a built-in starter library as fallback.
#pragma once

#include <string>

#include <nlohmann/json.hpp>

namespace chudware::ecad::library {

// Resolve a footprint def by fpid. Returns null if unknown.
nlohmann::json resolve_footprint(const std::string& fpid);

// Ranked search of KiCad and starter footprints; limit <= 0 returns all.
nlohmann::json search_footprints(const std::string& query, int limit);

}  // namespace chudware::ecad::library
