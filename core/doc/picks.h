// Kernel-aware pick resolution — the bridge between the OCCT-free selector algorithm
// (doc/selectors.{h,cpp}) and a LIVE B-rep body (a TopoDS_Shape). C++ port of the
// edge/face/vertex resolution core of engine/doc/features/dress.py
// (_resolve_with_hint_fallback + _resolve_edges + _nearest_by_centroid).
//
// WHY this layer exists (the two-stage resolution it implements):
//   describe_edges(shape) enumerates one descriptor PER INCIDENT FACE — a box yields
//   24 edge descriptors for its 12 unique edges, each unique edge appearing twice with
//   IDENTICAL geometry. A plain resolve() therefore DANGLES on any solid edge: the two
//   identical duplicates co-rank, so the margin rule (refuse to guess on symmetric
//   ambiguity) fires with margin == 0. dress.py works around this with a two-stage
//   resolution we port verbatim:
//     STAGE 1 — resolve_with_hint_fallback: when resolve() dangles with a "margin"
//       reason AND the best score < 0.01, the reference is clearly correct (only
//       identical duplicates co-rank), so re-score the same-kind (owner-filtered,
//       fallback-to-all) candidates, sort by (score, hint_index), and accept the best
//       (preferring a hint_index match) provided its score < 0.01.
//     STAGE 2 — map the resolved describe-space descriptor's centroid to the NEAREST
//       UNIQUE edge of the body (TopExp::MapShapes de-dup + BRepGProp linear centroid)
//       within tol = max(bbox_diag, 1.0) * 1e-3. The describe-space index lives in the
//       WIDER 24-occurrence space and must NOT index the de-duplicated edge list, so we
//       map by centroid distance (the unique edge is the SAME geometric entity, so it
//       coincides essentially exactly; anything beyond tol means the pick landed on
//       geometry no unique edge reproduces — reported as dangling).
//
// The nearest-unique-subshape helper is generic over a TopAbs_ShapeEnum so a later
// increment (A4/A5, faces) can reuse it for face picks.
#pragma once

#include <optional>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

#include <TopoDS_Edge.hxx>
#include <TopoDS_Shape.hxx>

#include "doc/selectors.h"

namespace aicad::doc {

// Bounding-box diagonal of `shape` (Bnd_Box + BRepBndLib::Add; sqrt(dx^2+dy^2+dz^2)),
// used to normalise selector centroid distances. Returns 1.0 for a degenerate box
// (diag <= 1e-12), matching dress.py::_bbox_diag.
double bbox_diag(const TopoDS_Shape& shape);

// Resolve a single selector against `infos` with the hint_index fallback (exact port
// of dress.py::_resolve_with_hint_fallback). Returns the accepted descriptor's
// TopoInfo.index, or nullopt when the selector still dangles.
std::optional<int> resolve_with_hint_fallback(const nlohmann::json& sel,
                                              const std::vector<TopoInfo>& infos,
                                              double diag);

// The result of resolving a list of edge selectors against a body: the unique edges
// that resolved, plus one human-readable reason string per dangling selector.
struct EdgePicks {
    std::vector<TopoDS_Edge> edges;
    std::vector<std::string> dangling;  // "edges[i]: <reason>" (mirrors dress.py)
};

// Resolve edge selectors against `shape`. `selectors` may be a single selector object
// or a json array of them. Per selector: describe_edges(shape) -> resolve_with_hint_
// fallback -> nearest-unique-edge mapping (see header note). Dangling reasons are
// accumulated under the slot name "edges".
EdgePicks resolve_edge_picks(const TopoDS_Shape& shape, const nlohmann::json& selectors);

}  // namespace aicad::doc
