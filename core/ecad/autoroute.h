// Basic grid (Lee/maze A*) PCB autorouter — C++ port of engine/ecad/autoroute.py.
//
// Connects the ratsnest airwires (see ecad/drc.h ratsnest) with orthogonal track
// segments + layer-change vias on a uniform two-layer grid, respecting copper
// clearance against other-net copper. Ported feature-for-feature from the Python
// oracle so the routed geometry is BYTE-IDENTICAL: same grid sizing/coarsening,
// same A* cost weights + heapq tuple tie-break, same shortest-first net order,
// same bounded rip-up-and-reroute, and the same parallel-offset differential-pair
// coupling + accordion length-match. PURE: `board` is never mutated; the returned
// tracks/vias are new dicts with ids allocated past the board's existing ids.
//
// Determinism: the Python A* heap is heapq over (f, g, node, dir) tuples with
// node = (layer, ix, iy). All ties resolve on the fully-ordered node tuple, so the
// pop order is reproducible; this port replicates that exact comparison. Net
// ordering, rip-up order, and set iteration are all `sorted(...)` in the oracle and
// mirrored with the same keys here. OCCT-free; pure JSON / grid / graph.
#pragma once

#include <nlohmann/json.hpp>

namespace aicad::ecad {

// autoroute(board, opts) -> {"tracks": [...], "vias": [...], "report": {...}}.
// Mirrors engine/ecad/autoroute.py::autoroute exactly. PURE.
nlohmann::json autoroute(const nlohmann::json& board, const nlohmann::json& opts);

// route_diff_pair(board, net_p, net_n, opts) -> {"tracks", "vias", "report"}.
// Mirrors engine/ecad/autoroute.py::route_diff_pair. PURE.
nlohmann::json route_diff_pair(const nlohmann::json& board, const std::string& net_p,
                               const std::string& net_n, const nlohmann::json& opts);

}  // namespace aicad::ecad
