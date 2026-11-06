// Reverse-engineering kernel primitives (WS-C / C1+C2) — pure OCCT geometry, no Session
// dependency. The doc-resolving glue (require_session, body/face selector resolution) lives
// in session.cpp's doc_eval_compare / doc_face_wires wrappers, which call straight through to
// the functions declared here. Keeping this file Session-free mirrors the geom/doc.h split:
// independently testable, no knowledge of the document/feature-tree contract.
#pragma once

#include <optional>
#include <vector>

#include <nlohmann/json.hpp>
#include <TopoDS_Edge.hxx>
#include <TopoDS_Face.hxx>
#include <TopoDS_Shape.hxx>

#include "geom/plane.h"  // chudware::geom::SketchPlane — projection target frame

namespace chudware::doc {

// C1 — eval_compare. Shape-vs-shape comparison, the geometric ground truth the WS-C
// reverse-engineering loop scores its rebuild against (precedent: doc_eval_symmetry's
// mirror + BRepAlgoAPI_Common deviation, session.cpp ~L6514).
//
// FRAME ASSUMPTION: `a` and `b` are compared AS-IS in whatever world coordinates their
// callers hand over — there is no registration/ICP step. The intended RE workflow rebuilds
// directly in the reference solid's own coordinate frame for exactly this reason (datum-plane
// sketches use the source face's own plane_frame); a caller comparing two independently
// positioned solids must align them itself first.
//
// Normal return (both booleans converged):
//   {ok:true, degraded:false, volume_a_mm3, volume_b_mm3, common_mm3, missing_mm3, extra_mm3,
//    match_pct, residual_regions:[{side:"missing"|"extra", volume_mm3, center:[x,y,z],
//    size:[dx,dy,dz]}, ...] (volume desc, capped at 10, < 1e-3 mm^3 dropped), summary}
// match_pct = 100 * common_mm3 / max(volume_a_mm3, volume_b_mm3) (100 when both volumes are
// ~0 — two empty/degenerate shapes are trivially "identical").
//
// Degraded return (a boolean threw or failed to converge — OCCT booleans are weakest on
// near-identical/coincident geometry, exactly the case this RPC exists to handle):
//   {ok:true, degraded:true, match_pct:null, volume_a_mm3, volume_b_mm3, volume_ratio,
//    bbox_a, bbox_b, centroid_dist_mm, summary}
// Never throws: a totally unusable pair of shapes (null) degrades rather than raising, so the
// RE loop always gets a signal (even if "no signal").
nlohmann::json compare_shapes(const TopoDS_Shape& a, const TopoDS_Shape& b);

// C2 — convert-entities primitive. Converts one PLANAR face's boundary wires into doc-schema
// §3.1 sketch entities expressed in the face's own 2D plane frame (origin = area centroid,
// axes/normal built via chudware::geom::SketchPlane::from(origin, normal) — the SAME convention
// resolve_sketch_plane's face-sketch branch uses, so a sketch authored from this output lands
// exactly where "sketch on this face" would).
//
// Per edge: GeomAbs_Line -> "line"; GeomAbs_Circle, full loop -> "circle", trimmed -> "arc"
// (center/radius/start/end in DEGREES measured in the face's own plane frame + an explicit
// `ccw` sweep-direction flag, resolved from an actual traversal-midpoint sample so it is
// correct regardless of how the underlying curve's own axis relates to the face normal);
// GeomAbs_Ellipse, full loop -> "ellipse" (center/rx/ry/rot), trimmed -> "ellipse_arc"
// (adds a0/a1 — the curve's OWN un-rotated-frame trim parameters in degrees, per doc_schema.md
// §3.1: these are always stored increasing/CCW in the ellipse's local frame, so no separate
// ccw flag is needed there). Anything else (BSpline/Bezier/offset/etc.) is sampled into a
// 24-segment "line" polyline and the top-level `approx` flag is set.
//
// Returns {ok:true, plane_frame (16-float row-major, same layout as doc_sketch_overview's
// plane_frame), entities:[...], wires:[{outer:bool, entity_ids:[...]}], approx:bool}.
// Throws OpError("invalid_op", ...) if `face` is not planar.
nlohmann::json face_wires(const TopoDS_Face& face);

// C3 — "Convert Entities" / external-reference projection. Project arbitrary external
// model edges ORTHOGRAPHICALLY onto a TARGET sketch plane (drop along the plane normal), then
// classify the projected 2D image as doc-schema §3.1 sketch entities. Unlike face_wires (which
// converts a face's OWN in-plane boundary), the source edges here can sit on ANY plane relative
// to the sketch — projection-first-then-classify makes the result correct by construction:
//   * a line stays a "line";
//   * a circle whose axis is parallel to the sketch normal stays a "circle"/"arc";
//   * a TILTED circle projects to an ellipse -> "ellipse"/"ellipse_arc" (this is the specific
//     Convert-Entities correctness fix — the naive "reuse the source circle's radius" path
//     produced wrong geometry);
//   * BSpline/Bezier/offset curves fall through to a sampled 24-seg polyline (approx);
//   * an edge (near) perpendicular to the sketch plane collapses to a single "point".
// Every emitted entity carries construction:true (reference geometry — solves/constrains other
// sketch entities but does not itself bound a region). `any_approx` is set if any edge sampled.
struct ProjectedEntity {
    nlohmann::json entity;  // doc-schema §3.1 entity WITHOUT an id (caller assigns on insert)
};
std::vector<ProjectedEntity> project_edges_to_plane(const std::vector<TopoDS_Edge>& edges,
                                                    const chudware::geom::SketchPlane& plane,
                                                    bool& any_approx);

// Isolated crossings of a trimmed model edge with a sketch plane. Multiple crossings
// choose the one nearest `preferred_point`, or the projected edge midpoint when it is absent.
// A coplanar edge has no isolated pierce point and returns nullopt.
std::optional<nlohmann::json> pierce_edge_to_plane(
    const TopoDS_Edge& edge, const chudware::geom::SketchPlane& plane,
    const std::optional<Eigen::Vector2d>& preferred_point = std::nullopt);

// Lift one solved sketch entity into world space, then project onto another plane.
std::vector<ProjectedEntity> project_sketch_entity_to_plane(
    const nlohmann::json& entity, const chudware::geom::SketchPlane& source,
    const chudware::geom::SketchPlane& target, bool& any_approx);

// All boundary edges of a face (every wire, outer + holes) — the edge set "Convert Entities on
// a whole face" projects. Order follows OCCT's TopExp_Explorer edge traversal.
std::vector<TopoDS_Edge> face_boundary_edges(const TopoDS_Face& face);

}  // namespace chudware::doc
