// core/sourcing/bolts_data.h — clean-room ISO/DIN standard-part tables for the
// spec-sheet matcher's deterministic lane. Transcribed 1:1 from the oracle's
// engine_oracle/data/bolts/{bearings,iso_metric_fasteners}.blt.yaml (published
// dimensional standards; NOT the BOLTS project's GPL data — see that folder's
// README.md and contracts/licenses.md). ORDER MATTERS: the Python loader reads
// files in sorted order (bearings first), and match preference follows table order.
#pragma once

#include <string>
#include <vector>

#include <nlohmann/json.hpp>

namespace chudware {
namespace sourcing {

// One governing standard (engine/bolts_loader.py::Standard).
struct BoltStandard {
    std::string id;            // e.g. "ISO 4762"
    std::string name;          // e.g. "Socket Head Cap Screw"
    std::string family;        // core/parts catalog family key
    std::string kind;          // "fastener" | "bearing"
    bool needs_length = false;
    std::vector<std::string> aliases;
    nlohmann::json sizes;      // {designation: {param: value, ...}} (object)
};

// All tables in the oracle's load order (bearings.blt.yaml, then iso_metric_fasteners).
inline const std::vector<BoltStandard>& bolts_tables() {
    static const std::vector<BoltStandard> tables = {
        {"ISO 15", "Deep-Groove Ball Bearing", "bearing", "bearing", false,
         {"deep groove", "ball bearing"},
         {{"623", {{"bore", 3.0}, {"outer_d", 10.0}, {"width", 4.0}}},
          {"624", {{"bore", 4.0}, {"outer_d", 13.0}, {"width", 5.0}}},
          {"625", {{"bore", 5.0}, {"outer_d", 16.0}, {"width", 5.0}}},
          {"626", {{"bore", 6.0}, {"outer_d", 19.0}, {"width", 6.0}}},
          {"608", {{"bore", 8.0}, {"outer_d", 22.0}, {"width", 7.0}}},
          {"6000", {{"bore", 10.0}, {"outer_d", 26.0}, {"width", 8.0}}},
          {"6001", {{"bore", 12.0}, {"outer_d", 28.0}, {"width", 8.0}}},
          {"6002", {{"bore", 15.0}, {"outer_d", 32.0}, {"width", 9.0}}},
          {"6003", {{"bore", 17.0}, {"outer_d", 35.0}, {"width", 10.0}}},
          {"6004", {{"bore", 20.0}, {"outer_d", 42.0}, {"width", 12.0}}},
          {"6005", {{"bore", 25.0}, {"outer_d", 47.0}, {"width", 12.0}}},
          {"6006", {{"bore", 30.0}, {"outer_d", 55.0}, {"width", 13.0}}},
          {"6007", {{"bore", 35.0}, {"outer_d", 62.0}, {"width", 14.0}}},
          {"6008", {{"bore", 40.0}, {"outer_d", 68.0}, {"width", 15.0}}},
          {"6200", {{"bore", 10.0}, {"outer_d", 30.0}, {"width", 9.0}}},
          {"6201", {{"bore", 12.0}, {"outer_d", 32.0}, {"width", 10.0}}},
          {"6202", {{"bore", 15.0}, {"outer_d", 35.0}, {"width", 11.0}}},
          {"6203", {{"bore", 17.0}, {"outer_d", 40.0}, {"width", 12.0}}},
          {"6204", {{"bore", 20.0}, {"outer_d", 47.0}, {"width", 14.0}}},
          {"6205", {{"bore", 25.0}, {"outer_d", 52.0}, {"width", 15.0}}},
          {"6206", {{"bore", 30.0}, {"outer_d", 62.0}, {"width", 16.0}}},
          {"6207", {{"bore", 35.0}, {"outer_d", 72.0}, {"width", 17.0}}},
          {"6208", {{"bore", 40.0}, {"outer_d", 80.0}, {"width", 18.0}}},
          {"6300", {{"bore", 10.0}, {"outer_d", 35.0}, {"width", 11.0}}},
          {"6301", {{"bore", 12.0}, {"outer_d", 37.0}, {"width", 12.0}}},
          {"6302", {{"bore", 15.0}, {"outer_d", 42.0}, {"width", 13.0}}},
          {"6303", {{"bore", 17.0}, {"outer_d", 47.0}, {"width", 14.0}}},
          {"6304", {{"bore", 20.0}, {"outer_d", 52.0}, {"width", 15.0}}},
          {"6305", {{"bore", 25.0}, {"outer_d", 62.0}, {"width", 17.0}}},
          {"6800", {{"bore", 10.0}, {"outer_d", 19.0}, {"width", 5.0}}},
          {"6801", {{"bore", 12.0}, {"outer_d", 21.0}, {"width", 5.0}}},
          {"6802", {{"bore", 15.0}, {"outer_d", 24.0}, {"width", 5.0}}},
          {"6900", {{"bore", 10.0}, {"outer_d", 22.0}, {"width", 6.0}}},
          {"6901", {{"bore", 12.0}, {"outer_d", 24.0}, {"width", 6.0}}}}},
        {"ISO 4762", "Socket Head Cap Screw", "shcs", "fastener", true,
         {"DIN 912", "SHCS"},
         {{"M1.6", {{"d", 1.6}, {"pitch", 0.35}, {"head_d", 3.0}, {"head_h", 1.6}, {"hex_key", 1.5}}},
          {"M2", {{"d", 2.0}, {"pitch", 0.40}, {"head_d", 3.8}, {"head_h", 2.0}, {"hex_key", 1.5}}},
          {"M2.5", {{"d", 2.5}, {"pitch", 0.45}, {"head_d", 4.5}, {"head_h", 2.5}, {"hex_key", 2.0}}},
          {"M3", {{"d", 3.0}, {"pitch", 0.50}, {"head_d", 5.5}, {"head_h", 3.0}, {"hex_key", 2.5}}},
          {"M4", {{"d", 4.0}, {"pitch", 0.70}, {"head_d", 7.0}, {"head_h", 4.0}, {"hex_key", 3.0}}},
          {"M5", {{"d", 5.0}, {"pitch", 0.80}, {"head_d", 8.5}, {"head_h", 5.0}, {"hex_key", 4.0}}},
          {"M6", {{"d", 6.0}, {"pitch", 1.00}, {"head_d", 10.0}, {"head_h", 6.0}, {"hex_key", 5.0}}},
          {"M8", {{"d", 8.0}, {"pitch", 1.25}, {"head_d", 13.0}, {"head_h", 8.0}, {"hex_key", 6.0}}},
          {"M10", {{"d", 10.0}, {"pitch", 1.50}, {"head_d", 16.0}, {"head_h", 10.0}, {"hex_key", 8.0}}},
          {"M12", {{"d", 12.0}, {"pitch", 1.75}, {"head_d", 18.0}, {"head_h", 12.0}, {"hex_key", 10.0}}}}},
        {"ISO 7380", "Button Head Cap Screw", "bhcs", "fastener", true,
         {"BHCS"},
         {{"M2", {{"d", 2.0}, {"pitch", 0.40}, {"head_d", 3.8}, {"head_h", 1.3}}},
          {"M2.5", {{"d", 2.5}, {"pitch", 0.45}, {"head_d", 4.7}, {"head_h", 1.5}}},
          {"M3", {{"d", 3.0}, {"pitch", 0.50}, {"head_d", 5.7}, {"head_h", 1.65}}},
          {"M4", {{"d", 4.0}, {"pitch", 0.70}, {"head_d", 7.6}, {"head_h", 2.2}}},
          {"M5", {{"d", 5.0}, {"pitch", 0.80}, {"head_d", 9.5}, {"head_h", 2.75}}},
          {"M6", {{"d", 6.0}, {"pitch", 1.00}, {"head_d", 10.5}, {"head_h", 3.3}}},
          {"M8", {{"d", 8.0}, {"pitch", 1.25}, {"head_d", 14.0}, {"head_h", 4.4}}},
          {"M10", {{"d", 10.0}, {"pitch", 1.50}, {"head_d", 17.5}, {"head_h", 5.5}}}}},
        {"ISO 10642", "Flat Head Cap Screw", "fhcs", "fastener", true,
         {"DIN 7991", "FHCS"},
         {{"M2.5", {{"d", 2.5}, {"pitch", 0.45}, {"head_d", 5.5}}},
          {"M3", {{"d", 3.0}, {"pitch", 0.50}, {"head_d", 6.3}}},
          {"M4", {{"d", 4.0}, {"pitch", 0.70}, {"head_d", 8.4}}},
          {"M5", {{"d", 5.0}, {"pitch", 0.80}, {"head_d", 10.4}}},
          {"M6", {{"d", 6.0}, {"pitch", 1.00}, {"head_d", 12.6}}},
          {"M8", {{"d", 8.0}, {"pitch", 1.25}, {"head_d", 17.3}}}}},
        {"ISO 4017", "Hex Head Bolt", "hex_bolt", "fastener", true,
         {"ISO 4014", "HHCS"},
         {{"M3", {{"d", 3.0}, {"pitch", 0.50}, {"af", 5.5}}},
          {"M4", {{"d", 4.0}, {"pitch", 0.70}, {"af", 7.0}}},
          {"M5", {{"d", 5.0}, {"pitch", 0.80}, {"af", 8.0}}},
          {"M6", {{"d", 6.0}, {"pitch", 1.00}, {"af", 10.0}}},
          {"M8", {{"d", 8.0}, {"pitch", 1.25}, {"af", 13.0}}},
          {"M10", {{"d", 10.0}, {"pitch", 1.50}, {"af", 16.0}}},
          {"M12", {{"d", 12.0}, {"pitch", 1.75}, {"af", 18.0}}}}},
        {"ISO 4032", "Hex Nut", "hex_nut", "fastener", false,
         {"DIN 934"},
         {{"M2", {{"d", 2.0}, {"pitch", 0.40}, {"af", 4.0}, {"thickness", 1.6}}},
          {"M2.5", {{"d", 2.5}, {"pitch", 0.45}, {"af", 5.0}, {"thickness", 2.0}}},
          {"M3", {{"d", 3.0}, {"pitch", 0.50}, {"af", 5.5}, {"thickness", 2.4}}},
          {"M4", {{"d", 4.0}, {"pitch", 0.70}, {"af", 7.0}, {"thickness", 3.2}}},
          {"M5", {{"d", 5.0}, {"pitch", 0.80}, {"af", 8.0}, {"thickness", 4.0}}},
          {"M6", {{"d", 6.0}, {"pitch", 1.00}, {"af", 10.0}, {"thickness", 5.0}}},
          {"M8", {{"d", 8.0}, {"pitch", 1.25}, {"af", 13.0}, {"thickness", 6.5}}},
          {"M10", {{"d", 10.0}, {"pitch", 1.50}, {"af", 16.0}, {"thickness", 8.0}}},
          {"M12", {{"d", 12.0}, {"pitch", 1.75}, {"af", 18.0}, {"thickness", 10.0}}}}},
        {"ISO 7089", "Flat Washer", "washer_flat", "fastener", false,
         {"DIN 125"},
         {{"M2", {{"inner_d", 2.2}, {"outer_d", 5.5}, {"thickness", 0.3}}},
          {"M2.5", {{"inner_d", 2.7}, {"outer_d", 6.0}, {"thickness", 0.5}}},
          {"M3", {{"inner_d", 3.2}, {"outer_d", 7.0}, {"thickness", 0.5}}},
          {"M4", {{"inner_d", 4.3}, {"outer_d", 9.0}, {"thickness", 0.8}}},
          {"M5", {{"inner_d", 5.3}, {"outer_d", 10.0}, {"thickness", 1.0}}},
          {"M6", {{"inner_d", 6.4}, {"outer_d", 12.0}, {"thickness", 1.6}}},
          {"M8", {{"inner_d", 8.4}, {"outer_d", 16.0}, {"thickness", 1.6}}},
          {"M10", {{"inner_d", 10.5}, {"outer_d", 20.0}, {"thickness", 2.0}}},
          {"M12", {{"inner_d", 13.0}, {"outer_d", 24.0}, {"thickness", 2.5}}}}},
    };
    return tables;
}

}  // namespace sourcing
}  // namespace chudware
