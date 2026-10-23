// MCU family lookup + net-name → pin-number decoding (C++ port of
// engine_oracle/firmware/mcu_pinouts.py). Pure JSON in/out; see the Python
// module docstring for the two concerns (BOM → mcu_family, positional net
// names → stable pin ids).
#pragma once

#include <optional>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

namespace chudware::firmware {

// A named signal net that encodes no physical pin gets a deterministic logical
// index offset by this base (mirrors mcu_pinouts.LOGICAL_PIN_BASE).
inline constexpr int kLogicalPinBase = 1000;

// Map a part identifier (MPN / value / libid) to a firmware mcu_family, or
// nullopt. Case-insensitive substring match, first table hit wins.
std::optional<std::string> mpn_to_family(const std::string& mpn);

// Firmware family for a single BOM component dict, or nullopt when it is not
// a known MCU.
std::optional<std::string> component_family(const nlohmann::json& comp);

// BOM components of an ECAD doc in stable order (board → footprints,
// schematic → symbols).
std::vector<nlohmann::json> iter_bom_components(const nlohmann::json& doc);

// mcu_family of the FIRST non-dnp MCU component in the doc's BOM, or nullopt.
std::optional<std::string> select_mcu_family(const nlohmann::json& doc);

// Resolve a board_doc_ref to a doc dict: open ECAD session doc_id first, then
// a file path (.ecad+ container or plain JSON). Returns null when
// unresolvable — never throws (mirrors mcu_pinouts.resolve_board_doc).
nlohmann::json resolve_board_doc(const std::string& ref);

// Boot-strap / reset pin-or-net names (upper-case) for mcu_family.
std::vector<std::string> strap_pins(const std::string& mcu_family);

bool is_strap_net(const std::string& net_name, const std::string& mcu_family);
bool is_input_only_pin(const std::string& net_name, const std::string& mcu_family);

// Decode a positional net name to a stable integer pin id (STM32 P<port><n>,
// ESP32 GPIO<n>/IO<n>, Arduino D<n>), or nullopt for named signal nets.
std::optional<int> resolve_pin(const std::string& net_name,
                               const std::string& mcu_family = "");

}  // namespace chudware::firmware
