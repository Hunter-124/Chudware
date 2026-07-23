// Solved 3D-sketch -> OCCT wire compiler (the sketch3d counterpart of
// profile.h's wire_from_solved_sketch). Turns the world-space output of
// chudware::sketch3d::solve_sketch3d into TopoDS_Wire chains a sweep consumes as
// its spine/path or guide (and a future loft consumes as guide curves).
//
// ALGORITHM:
//   - One OCCT edge per non-construction line/arc/spline (points never edge).
//     line   -> BRepBuilderAPI_MakeEdge(p1, p2)
//     arc    -> GC_MakeArcOfCircle(p1, pm, p2)   (three-point: start/through/end)
//     spline -> GeomAPI_Interpolate through `points` (cubic through-points)
//   - Edges chain into wires by endpoint proximity (ShapeAnalysis_FreeBounds,
//     tol 1e-4 — the same float-gap stitcher profile.cpp's make_wire uses), so a
//     contiguous line+arc+spline path becomes ONE wire; disconnected groups
//     become separate wires. Open and closed wires are both returned.
//   - Wire order is deterministic for a given entity list: wires are sorted by
//     the lowest entity-list index of any edge they contain, so "wire 0" is the
//     chain containing the earliest-authored entity.
#pragma once

#include <vector>

#include <nlohmann/json.hpp>
#include <TopoDS_Wire.hxx>

namespace chudware::geom {

// Build 3D wires from solved sketch3d entities (Sketch3DSolution::entities,
// id -> param dict). Construction geometry and points are skipped; entities that
// fail to build an edge (zero-length line, collinear arc) are skipped —
// defensive, never throws across the API boundary. Empty when nothing builds.
std::vector<TopoDS_Wire> wires_from_sketch3d(const nlohmann::json& solved_entities);

}  // namespace chudware::geom
