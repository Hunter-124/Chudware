// ECAD netlist → firmware pin-map header (C++ port of
// engine_oracle/firmware/pinmap.py). See contracts/firmware_rpc.md §2 and the
// Python module docstring.
#pragma once

#include <string>
#include <vector>

#include <nlohmann/json.hpp>

namespace chudware::firmware {

struct Project;  // project.h — only needed by write_pinmap

// True when `name` is a power/ground rail (never emitted as a GPIO pin).
// Empty names count as power.
bool is_power_net(const std::string& name);

// Net names of an ECAD doc (board nets list, or schematic via build_netlist).
std::vector<std::string> net_names(const nlohmann::json& doc);

// Enriched per-pin records (same selection/order/dedup as generate_pinmap):
// {"net","token","pin_id","positional","strap","input_only","direction","mux"}.
nlohmann::json pin_model(const nlohmann::json& board_doc,
                         const std::string& mcu_family = "");

// The generated C header text (deterministic, byte-stable vs the Python port).
std::string generate_pinmap(const nlohmann::json& board_doc,
                            const std::string& mcu_family = "",
                            const std::string& guard = "CHUDWARE_PINMAP_H");

}  // namespace chudware::firmware
