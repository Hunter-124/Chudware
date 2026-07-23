// Copper zone fill — C++ port of engine/ecad/zonefill.py.
//
// Grid-raster + boundary-tracing copper pour. fill_zones() returns a deep copy of
// the board with every copper zone's filled_polys recomputed; fill_zone() computes
// one zone's polygons. Deterministic and pure (never mutates input). Mirrors the
// Python oracle's grid (cell-centre sampling at `resolution` mm), even-odd PIP,
// DRC-shape clearance carving, 4-spoke thermal relief, min_width island drop, and
// 4-connected component contour trace with keyholed holes — vertex-for-vertex.
// OCCT-free; no clipping library (the fill is a pure numerical grid algorithm).
#pragma once

#include <nlohmann/json.hpp>

namespace chudware::ecad {

inline constexpr double kZoneFillDefaultResolution = 0.1;

// Filled copper polygons for one zone (the value to store in zone["filled_polys"]).
nlohmann::json fill_zone(const nlohmann::json& board, const nlohmann::json& zone,
                         double resolution = kZoneFillDefaultResolution);

// Deep copy of `board` with every copper zone's filled_polys recomputed.
nlohmann::json fill_zones(const nlohmann::json& board,
                          double resolution = kZoneFillDefaultResolution);

}  // namespace chudware::ecad
