#pragma once

#include <string>
#include <nlohmann/json.hpp>

namespace chudware::ecad {

// All operations run the installed KiCad CLI out of process.
nlohmann::json ecad_kicad_status();
nlohmann::json ecad_kicad_check(const std::string& doc_id);
// files contains {name, bytes}; pathless results also include content, with
// encoding="base64" for binary files. paths lists only explicitly written outputs.
nlohmann::json ecad_export(const nlohmann::json& params);
// Produces a persistent STEP for the importer; defaults beside the bound board.
nlohmann::json ecad_kicad_step(const std::string& doc_id, const nlohmann::json& opts,
                                const std::string& path = {});

}  // namespace chudware::ecad
