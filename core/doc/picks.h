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
//       reason AND the best score < 0.35 (resolve()'s acceptance bound), the reference
//       is geometrically close (often just duplicate explorer occurrences of the same
//       edge), so re-score the same-kind (owner-filtered, fallback-to-all) candidates,
//       sort by (score, hint_index), and accept the best (preferring a hint_index match)
//       provided its score < 0.35.
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
#include <TopoDS_Face.hxx>
#include <TopoDS_Shape.hxx>

#include "doc/selectors.h"

namespace chudware::doc {

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

// The result of resolving a list of face selectors against a body: the unique faces
// that resolved, plus one human-readable reason string per dangling selector.
struct FacePicks {
    std::vector<TopoDS_Face> faces;
    std::vector<std::string> dangling;  // "<slot>[i]: <reason>"
};

// Resolve face selectors against `shape` — the FACE counterpart of resolve_edge_picks
// (the mirror feature's plane comes from a picked planar face). Per selector:
// describe_faces(shape) -> resolve_with_hint_fallback -> nearest-unique-face mapping
// (reusing the generic centroid-mapping helper with TopAbs_FACE). Unlike edges, a
// solid's faces enumerate ONCE each (describe_faces is already de-duplicated), so the
// hint fallback is rarely needed, but the path is identical for robustness. Dangling
// reasons are accumulated under the given `slot` name (e.g. "plane[0]: <reason>").
FacePicks resolve_face_picks(const TopoDS_Shape& shape, const nlohmann::json& selectors,
                             const std::string& slot = "faces");

// --------------------------------------------------------------------------- geom query
// Semantic geometry query — how an AGENT (which cannot click a viewport) names geometry
// on an EXISTING body, so it can fillet/chamfer/shell/probe human-made parts the same
// way a human picks. Resolves a query object into concrete UNIQUE sub-shapes.
//
// The vocabulary is built for a model with little spatial sense: every predicate accepts
// the plain WORD a person would use as well as the precise form. Words are case-insensitive
// and desugar to the primitive below, so full capability is preserved.
//
// Schema (all predicates AND-combine; absent/empty => ALL sub-shapes of `of`):
//   of            "edge" | "face"                       (required)
//   surf          type filter. faces: "flat" (= plane) | "round"/"curved" (= ANY non-planar
//                 face: cylinder|cone|sphere|torus|bspline) | the everyday curved words
//                 cylindrical/conical/spherical/toroidal | the exact kernel name
//                 (plane|cylinder|cone|sphere|torus). edges: "straight" (= line) |
//                 "round"/"curved" (= ANY non-line edge: circle|ellipse|bspline) |
//                 line|circle|ellipse|bspline.
//   parallel      [x,y,z] OR axis word "x"|"y"|"z"|"vertical" — edge direction / face normal
//                 parallel to this axis (|dot|~1). parallel:"z" = the vertical edges.
//   perpendicular [x,y,z] OR axis word — |dot|~0
//   side          direction WORD "top"|"bottom"|"left"|"right"|"front"|"back" (also up/down)
//                 OR signed axis "+x"|"-x"|"+y"|"-y"|"+z"|"-z" — keep only the sub-shapes at
//                 the EXTREME centroid along that direction. Word->axis matches the named
//                 views: top=+z, bottom=-z, right=+x, left=-x, front=-y, back=+y.
//   pick          "largest"|"smallest" (faces, by area) or "longest"|"shortest" (edges, by
//                 length) — keep the extreme-measure set. OR "inner"|"outer" (also bore/
//                 innermost/outermost) — keep the min/max-RADIUS round face(s)/edge(s), the
//                 plain way to tell coaxial geometry apart (tube inner vs outer wall, bore vs
//                 rim). Ties co-rank, so a symmetric body keeps the opposite pair; add
//                 count:1 or a side to force one. When the body has NO round faces/edges,
//                 inner/outer return an EMPTY result (0 matches) — not an error.
//   near          [x,y,z]   keep the nearest `count` (default 1). Distance = min(point -> the
//                 trimmed sub-shape geometry, point -> its centroid), so a point ON the entity
//                 or AT its reported centroid both score 0; ties (1e-4 mm / 1e-6 rel) go to the
//                 nearer centroid, then MapShapes order (contracts/selectors.md).
//   count         int       cap the result size
//   index         [i,...]   keep these ordinals of the filtered set
//
// Examples:  {"of":"edge"}                           -> every edge (fillet/chamfer all)
//            {"of":"edge","parallel":"z"}            -> the vertical edges (corner rounds)
//            {"of":"edge","side":"top"}              -> the top edges / top rim
//            {"of":"face","side":"top"}              -> the top face
//            {"of":"face","surf":"round"}            -> every non-planar face (cyl/cone/sphere/torus/bspline)
//            {"of":"face","surf":"round","pick":"outer"} -> the OUTER wall of a tube
//            {"of":"face","pick":"largest"}          -> the biggest face(s)
//            {"of":"edge","near":[20,0,5],"count":1} -> the edge nearest a point
struct QueryPicks {
    std::vector<TopoDS_Edge> edges;
    std::vector<TopoDS_Face> faces;
    nlohmann::json selectors = nlohmann::json::array();  // make_selector per match (introspection)
    std::string error;  // non-empty => malformed query; no geometry touched
};
QueryPicks resolve_geom_query(const TopoDS_Shape& shape, const nlohmann::json& query);

// True if `v` is a geometry-query object (a json object carrying "of", or kind=="query").
// Feature compilers use this to branch a query payload vs an explicit pick selector.
bool is_geom_query(const nlohmann::json& v);

// Canonical geometry-query grammar tail, appended to EVERY error that rejects a value in a
// query / selector slot. A model that guessed one key wrong has no other way to discover the
// vocabulary, so the message must carry the whole shape rather than the one offending field.
// Single definition on purpose: doc/picks.cpp (query resolution) and doc/validate.cpp
// (document validation) both emit it, and two copies would silently drift apart.
constexpr const char* kQueryGrammar =
    " | query keys: of(face|edge, default face), side(top|bottom|left|right|front|back|"
    "+x|-x|+y|-y|+z|-z), surf, parallel, perpendicular, pick, near, count, index. Example: "
    "{\"of\":\"face\",\"side\":\"top\"}. For a free-text description call describe_part / "
    "hover(sid) instead.";
// Return every key the engine does not understand, sorted for deterministic diagnostics.
// Non-object values have no query keys and return an empty vector.
std::vector<std::string> unknown_query_keys(const nlohmann::json& q);

// Format a teaching rejection for unknown query keys and append kQueryGrammar.
std::string unknown_query_keys_error(const std::string& where,
                                     const std::vector<std::string>& keys);

}  // namespace chudware::doc
