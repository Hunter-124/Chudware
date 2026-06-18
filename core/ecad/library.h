// ECAD starter footprint library — C++ port of the footprint half of
// engine/ecad/library/starter.py (FOOTPRINTS) + engine/ecad/library/native.py
// resolve_footprint(). Sync (back-annotation) is the only consumer in the C++
// core today: update_board_from_sch ADD needs the resolved footprint def to
// build a board Footprint instance, and the resolved def must be BYTE-IDENTICAL
// to the Python oracle (same pads/graphics/attr/description) for sync parity.
//
// Symbol resolution is NOT ported here (the C++ core never plans schematics from
// libids — the parity harness seeds every symbol def inline on the add op). Only
// the footprint table the sync path can reach is reproduced.
#pragma once

#include <string>

#include <nlohmann/json.hpp>

namespace aicad::ecad::library {

// Resolve a footprint def by fpid (starter library only). Returns the def dict
// (matching engine/ecad/library/starter.py::FOOTPRINTS[fpid]) or null if unknown.
nlohmann::json resolve_footprint(const std::string& fpid);

}  // namespace aicad::ecad::library
