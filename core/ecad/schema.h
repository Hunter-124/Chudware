// ECAD document schema + geometry helpers — C++ port of engine/ecad/schema.py.
//
// Structural validation only (envelopes, types, id prefixes, enum membership,
// cross-refs) for the two ECAD document kinds: aicad.sch/1 (schematic) and
// aicad.pcb/1 (board). Connectivity passes (netlist, ERC, DRC, ratsnest, zonefill)
// live in sibling modules and are invoked from session.cpp::compile_field. Coordinates
// are [x, y] in mm; angles in degrees CCW. Mirrors the pattern of
// core/doc/validate.{h,cpp}.
#pragma once

#include <set>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

namespace aicad::ecad::schema {

inline constexpr const char* kSchemaSch = "aicad.sch/1";
inline constexpr const char* kSchemaPcb = "aicad.pcb/1";
inline constexpr int kCopperLayersMax = 32;

// enum membership sets (kept in one place; ops.cpp + validate share them)
const std::set<std::string>& sym_mirror();
const std::set<std::string>& label_scopes();
const std::set<std::string>& sheet_pin_shapes();
const std::set<std::string>& pin_types();
const std::set<std::string>& tech_layers();
const std::set<std::string>& pad_shapes();
const std::set<std::string>& pad_types();
const std::set<std::string>& fp_sides();
const std::set<std::string>& via_types();
const std::set<std::string>& track_kinds();
const std::set<std::string>& zone_fills();

// factories (id is filled by the session)
nlohmann::json new_schematic(const std::string& name);
nlohmann::json new_board(const std::string& name);

bool is_schematic(const nlohmann::json& doc);
bool is_board(const nlohmann::json& doc);
std::string doc_kind(const nlohmann::json& doc);  // "sch" | "pcb" | ""

// id allocation: next unused "<prefix><n>" scanning current ids (never reused).
std::string next_id(const nlohmann::json& doc, const std::string& prefix);

// canonical ordered copper-layer ids for an N-copper stackup (F.Cu, In1.Cu, ..., B.Cu)
std::vector<std::string> copper_layer_ids(int copper_layers);
std::set<std::string> valid_layers(int copper_layers);

// geometry: world endpoint of a symbol pin (the connection tip away from the body)
std::pair<double, double> pin_world(const nlohmann::json& symbol, const nlohmann::json& pin);

// geometry: world position of a footprint pad (bottom-side footprints mirror across x)
std::pair<double, double> pad_world(const nlohmann::json& footprint, const nlohmann::json& pad);

// validation — returns a list of locator-prefixed error strings (empty = valid).
std::vector<std::string> validate_schematic(const nlohmann::json& doc);
std::vector<std::string> validate_board(const nlohmann::json& doc);
std::vector<std::string> validate(const nlohmann::json& doc);

}  // namespace aicad::ecad::schema
