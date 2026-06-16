// OCCT dress-up operators: fillet (round) + chamfer (bevel) on a solid's edges.
// C++ port of the edge-path core of engine/doc/features/dress.py. These are the
// finishing ops that soften a part: rounding the four vertical edges of a block,
// breaking a sharp rim with a chamfer. Together with the boolean ops they turn a raw
// extrude into a manufacturable, SolidWorks-class part.
//
// WHY a separate translation unit (not part of features.cpp):
//   Same rationale as booleans.cpp — features.cpp makes primitives, dress.cpp MODIFIES
//   a finished solid by blending/beveling its edges. Mirrors the Python split
//   (dress.py is its own compiler) and keeps each builder a clean shape->shape function.
//
// EDGE SELECTION (`dir` filter):
//   With no `dir` the op rounds/bevels EVERY edge of the solid. With a `dir` it selects
//   only STRAIGHT edges whose line direction is parallel to `dir` (either orientation) —
//   e.g. dir=+Z picks the four vertical edges of a box but leaves the eight horizontal
//   rim edges alone. This is the kernel-level analogue of the doc model's edge selectors:
//   "fillet the vertical edges" without per-edge picking. Non-line edges are skipped
//   when a `dir` filter is active (a curved edge has no single direction).
//
// EDGE ENUMERATION GOTCHA:
//   A raw TopExp_Explorer over TopAbs_EDGE visits each shared edge ONCE PER INCIDENT
//   FACE — so a box yields 24 edge hits for its 12 unique edges (dress.py documents the
//   exact same trap). We therefore enumerate via TopExp::MapShapes into a
//   TopTools_IndexedMapOfShape, which de-duplicates: each unique edge is added to the
//   MakeFillet/MakeChamfer builder exactly once.
//
// ERROR CONTAINMENT: each op gates on IsDone() and returns an empty TopoDS_Shape on
// failure (e.g. a radius too large for the geometry) — never throws across the API
// boundary, matching the contained-failure discipline of booleans.cpp / boolean.py.
#pragma once

#include <optional>

#include <Eigen/Dense>
#include <TopoDS_Shape.hxx>

namespace geom {

// Round the edges of `shape` with radius `r` (BRepFilletAPI_MakeFillet + Add(r, edge)).
// If `dir` is given, only straight edges parallel to `dir` are rounded; otherwise ALL
// edges. Empty shape on kernel failure (e.g. r exceeds the local geometry).
TopoDS_Shape fillet_edges(const TopoDS_Shape& shape, double r,
                          std::optional<Eigen::Vector3d> dir = std::nullopt);

// Bevel the edges of `shape` with a symmetric setback `d` (BRepFilletAPI_MakeChamfer +
// Add(d, edge)). Same `dir` selection rule as fillet_edges. Empty shape on kernel
// failure.
TopoDS_Shape chamfer_edges(const TopoDS_Shape& shape, double d,
                           std::optional<Eigen::Vector3d> dir = std::nullopt);

}  // namespace geom
