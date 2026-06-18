// Design-For-Manufacturing (DFM) check suite — C++ port of engine/ecad/dfm.py.
//
// Pure, advisory geometry/fab audit over a board document (aicad.pcb/1).
// run_dfm() returns a deterministic, sorted list of violation dicts
// {rule, severity, locs, items, message}. Never mutates the board, never raises.
// Mirrors the Python oracle exactly: rule catalogue + thresholds, geometry
// approximations, the violation record shape, and the final ordering key.
// OCCT-free; pure JSON/geometry.
#pragma once

#include <nlohmann/json.hpp>

namespace aicad::ecad {

// run_dfm(board, rules?) -> deterministic sorted list of DFM violations.
// `rules` is an optional dict of threshold overrides (defaults match dfm.py).
nlohmann::json run_dfm(const nlohmann::json& board,
                       const nlohmann::json& rules = nlohmann::json(nullptr));

}  // namespace aicad::ecad
