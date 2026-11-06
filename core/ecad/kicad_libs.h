#pragma once

#include <optional>
#include <string>

#include <nlohmann/json.hpp>

namespace chudware::ecad::kicad_libs {

struct Install {
    bool found = false;
    std::string version;
    std::string cli;
    std::string kicad;
    std::string eeschema;
    std::string pcbnew;
    std::string symbol_dir;
    std::string footprint_dir;
    std::string model_dir;
    std::string template_dir;
};

Install detect();
void set_project_dir(const std::string& dir);
std::optional<nlohmann::json> symbol_sexpr(const std::string& libid);
std::optional<nlohmann::json> footprint_sexpr(const std::string& fpid);
nlohmann::json search_symbols(const std::string& query, int limit);
nlohmann::json search_footprints(const std::string& query, int limit);
nlohmann::json libraries();

// Native symbol catalog metadata used by symlib's existing result shape.
nlohmann::json symbol_catalog();

}  // namespace chudware::ecad::kicad_libs
