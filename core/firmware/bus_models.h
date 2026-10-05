#pragma once
#include <map>
#include <string>
#include <vector>
#include <nlohmann/json.hpp>

namespace chudware::firmware {
// Pure declarative validation/resolution. Physical inputs never get ideal defaults.
std::vector<std::string> validate_bus_links(const nlohmann::json& scenario);
nlohmann::json resolve_bus_links(const nlohmann::json& scenario,
                                 const nlohmann::json& capabilities,
                                 const std::string& platform_text,
                                 std::vector<std::string>& errors);
std::string generated_bus_source(const nlohmann::json& resolved_links);
// Existing native MNA/Branin solver reference seam; no second analog engine.
nlohmann::json bus_reference_circuit(const nlohmann::json& signal);
nlohmann::json simulate_bus_reference(const nlohmann::json& signal,
                                       const nlohmann::json& study);
}  // namespace chudware::firmware
