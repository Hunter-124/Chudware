// Cross-document back-annotation — C++ port of engine/ecad/sync.py.
//
// update_board_from_sch reconciles a PCB against a schematic's netlist (add/
// update footprints, bind pad nets, register missing nets, report removed parts),
// mirroring KiCad "Update PCB from Schematic". Pure-ish: works on a deep copy of
// the board; the returned board passes schema::validate_board.
//
// The document transforms (id allocation via schema::next_id, grid placement,
// deterministic ref-sorted iteration, the report shape) match the Python oracle
// EXACTLY so the C++ result == the Python result bit-for-bit.
#pragma once

#include <nlohmann/json.hpp>

namespace aicad::ecad {

// update_board_from_sch(board, netlist, place_origin?, prune?) ->
//   {"mutated_board": <board>, "report": {added, updated, removed, unresolved,
//                                          nets_bound, linked_sch}}.
// place_origin: a [x, y] array (or null for the board origin). The library is the
// starter footprint library (engine/ecad/library/native.resolve_footprint).
nlohmann::json update_board_from_sch(const nlohmann::json& board, const nlohmann::json& netlist,
                                     const nlohmann::json& place_origin, bool prune);

}  // namespace aicad::ecad
