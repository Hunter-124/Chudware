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
#include <string>
#include <vector>

#include <Eigen/Dense>
#include <TopoDS_Edge.hxx>
#include <TopoDS_Face.hxx>
#include <TopoDS_Vertex.hxx>
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

// --- explicit-edge overloads (the doc model's edge-selector path) ---
// Round/bevel EXACTLY the given `edges` (already resolved against the body's topology
// by doc/picks::resolve_edge_picks). Rounds every edge that CAN be rounded at this radius;
// if some can't (radius exceeds the local geometry, a corner combo OCCT can't resolve), they
// are dropped and `*n_dropped` (when given) reports how many — the caller surfaces that as a
// non-fatal warning rather than failing the whole feature. Empty shape only when NOTHING could
// be rounded OR `edges` is empty.
//
// CONTACT EDGES: an edge shared by two solids of one body (two bodies that meet only along a
// line, e.g. a ring resting on a tube's rim) has four faces, which BRepFilletAPI cannot blend.
// fillet_edges fills every concave corner around such an edge with rolling-ball fillet
// material and fuses it in, so the two solids become one. Supported for straight or circular
// shared edges whose adjoining faces run straight away from the edge (planes, cylinders,
// cones). `*why` (when given) names the reason a contact edge could not be joined.
TopoDS_Shape fillet_edges(const TopoDS_Shape& shape, double r,
                          const std::vector<TopoDS_Edge>& edges, int* n_dropped = nullptr,
                          std::string* why = nullptr);
TopoDS_Shape chamfer_edges(const TopoDS_Shape& shape, double d,
                           const std::vector<TopoDS_Edge>& edges, int* n_dropped = nullptr);

// Variable-radius fillet: a LINEAR radius law per edge from `r1` at the edge start to
// `r2` at the edge end (BRepFilletAPI_MakeFillet::Add(r1, r2, edge)). Port of
// dress.py::_variable_fillet. Same drop-and-report behaviour as fillet_edges.
TopoDS_Shape fillet_edges_variable(const TopoDS_Shape& shape, double r1, double r2,
                                   const std::vector<TopoDS_Edge>& edges, int* n_dropped = nullptr);

// Two-distance (asymmetric) chamfer: leg `d1` on the first incident face, `d2` on the
// other (BRepFilletAPI_MakeChamfer::Add(d1, d2, edge, face)). Matches build123d
// chamfer(length=d1, length2=d2). Same drop-and-report behaviour as fillet_edges: edges the
// kernel can't bevel at this size are dropped (counted in `n_dropped`) instead of failing the
// whole feature. Empty shape on total failure / empty `edges`.
TopoDS_Shape chamfer_edges_two_distance(const TopoDS_Shape& shape, double d1, double d2,
                                        const std::vector<TopoDS_Edge>& edges,
                                        int* n_dropped = nullptr);

// Distance-angle chamfer: setback `d` is measured on the reference face and
// `angle_rad` is the angle to that face. If no face is given, each edge uses
// its first adjacent face in OCCT's stable topology traversal order.
TopoDS_Shape chamfer_edges_distance_angle(const TopoDS_Shape& shape, double d,
                                          double angle_rad,
                                          const std::vector<TopoDS_Edge>& edges,
                                          const std::optional<TopoDS_Face>& reference_face = std::nullopt,
                                          int* n_dropped = nullptr);

// Cut off each selected three-edge convex corner with a triangular face at a
// setback `d` along its incident straight edges. Failure details go to `diag`.
TopoDS_Shape chamfer_vertices(const TopoDS_Shape& shape, double d,
                              const std::vector<TopoDS_Vertex>& vertices,
                              std::string* diag = nullptr);

// --- shell (hollow to a wall) + draft (taper faces) — port of dress.py/draft.py ---

// Hollow `body` to a wall of thickness `t`. `remove` faces become the open mouths of
// the shell; `outward` thickens outward (+t) instead of the SolidWorks default inward
// (-t).
//   OPEN shell (`remove` non-empty): BRepOffsetAPI_MakeThickSolid::MakeThickSolidByJoin
//     opens the removed faces and offsets the remaining skin (Arc join first, then an
//     Intersection join + looser tolerance for curved/B-spline skins). When no offset
//     strategy validates and the shell is inward, geom::shell_by_sections
//     (shell_sections.h) hollows a tube-like body by lofting its exact eroded
//     cross-sections instead — the path a rectangle-to-circle loft takes, since OCCT's
//     offset refuses its corner seams (BRepOffset_MixedConnectivity).
//   CLOSED shell (`remove` EMPTY = a fully-sealed hollow part, e.g. a hollow ball /
//     sealed enclosure): MakeThickSolidByJoin no-ops with no faces to open (it hands
//     back the original solid), so this case instead builds an offset solid
//     (PerformByJoin by ±t) and BOOLEAN-CUTs it against the original to leave a closed
//     wall of thickness `t` (a 40^3 box at t=2 -> 40^3-36^3 = 17344, sealed cavity).
// Every strategy's result is ACCEPTED only when it is a BRepCheck-valid, watertight solid
// whose volume moved the right way (inward: smaller, outward: larger); otherwise the next
// join/tolerance strategy is tried. When none validates, a valid result whose volume did
// NOT move (the OCCT "thickness too large" no-op, Bug A2/A3) is returned so the caller's
// volume guard names it; anything else returns an empty shape. `diag` (optional) receives
// the precise kernel reason per strategy — the BRepOffset_Error name (e.g.
// MixedConnectivity, with the offending crease-to-tangent edges located) or the
// validation that rejected the result.
TopoDS_Shape shell_solid(const TopoDS_Shape& body, double t, bool outward,
                         const std::vector<TopoDS_Face>& remove,
                         std::string* diag = nullptr);

// True if `shape` has any freeform (B-spline/Bezier/revolution/extrusion) face — the
// surface types a loft/sweep/revolve makes, on which the thick-solid offset often
// fails. Lets the shell feature emit an actionable error instead of "empty shape".
bool has_freeform_faces(const TopoDS_Shape& shape);

// Taper the given `faces` of `body` by `angle_rad` about the neutral plane
// (`neutral_pt`, `neutral_n`), pulled along `pull` (BRepOffsetAPI_DraftAngle::Add per
// face). Each face tilts about the line where it meets the neutral plane. Faces OCCT
// cannot draft about this plane (e.g. caps parallel to it) are SKIPPED so a valid
// subset still drafts; returns an empty shape only when NO face is draftable or the
// kernel build fails, with the reason written to `*diag` (if non-null) for an
// actionable error. (neutral_plane mode only; parting_line/step are deferred.)
TopoDS_Shape draft_faces(const TopoDS_Shape& body, const std::vector<TopoDS_Face>& faces,
                         const Eigen::Vector3d& neutral_pt, const Eigen::Vector3d& neutral_n,
                         const Eigen::Vector3d& pull, double angle_rad,
                         std::string* diag = nullptr);

}  // namespace geom
