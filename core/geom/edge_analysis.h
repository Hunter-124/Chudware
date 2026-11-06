#pragma once

#include <nlohmann/json.hpp>
#include <TopoDS_Shape.hxx>

namespace chudware::geom {

// Exact, SIGNED edge classification of one solid body, plus its concave blend faces.
//
// For every manifold edge shared by two distinct faces the outward face normals are
// evaluated on each face's own pcurve at five interior samples. The turning angle between
// the normals gives the dihedral deviation; the sign comes from the oriented edge
// traversal of the two faces (an edge is traversed in opposite directions by its two faces
// in a valid shell), never from a centroid heuristic:
//     e_A = edge tangent as face A's wire traverses it
//     e_A . (n_A x n_B) > 0  -> CONVEX  (material angle = 180 - theta, e.g. a box edge = 90)
//     e_A . (n_A x n_B) < 0  -> CONCAVE (material angle = 180 + theta, an inside corner of
//                                        an L-bracket = 270)
// Both faces' traversals are evaluated; edges whose two signs disagree are reported as
// `unclassified_edges` rather than guessed.
//
// Result (all topology IDs are 1-based TopExp::MapShapes indices of `shape`, the same
// numbering tessellate_body / the AICM owner map / query matches `topo` use):
//   { face_count, edge_count, min_dihedral_dev_deg,
//     edges:[{edge_id, kind:"concave"|"convex"|"smooth", dihedral_deg (interior angle in
//             material, 180 = smooth), deviation_deg (max sampled normal turning),
//             deviation_min_deg, length_mm, mid:[x,y,z], dir:[x,y,z], face_ids:[a,b],
//             curve, radius_mm? (circle edges), axis? (circle edges), mixed_sign?}],
//     concave_blends:[{face_id, surf:"cylinder"|"torus"|"other", radius_mm, arc_deg?,
//             axis?, axis_point?, major_radius_mm?, center, area_mm2, adjacent_face_ids,
//             tangent_face_ids}],
//     unclassified_edges:[{edge_id, reason}], skipped_edges:{seam_or_degenerate,
//             non_manifold_or_free} }
//
// A concave blend is a concave cylinder / torus / sphere face (outward normal points toward
// its axis / tube circle / centre) that is joined G1-smoothly to at least one neighbour.
// A bore with no smooth neighbour is not a blend. `radius_mm`
// is the cylinder radius, the torus MINOR (section) radius, or the sphere radius; `arc_deg` is
// the swept section angle (cylinder: angular extent; torus: tube extent), ~90 for a typical
// fillet and ~180 for a slot end.
//
// With boolean option `include_ligaments` true, `ligaments` reports distances between
// finite faces within each solid, not axis-to-axis estimates. It defaults to false, in
// which case `ligaments` is {"evaluated":false}; evaluated results have evaluated=true.
// Only complete, inward-facing cylindrical faces with full cylindrical area
// are treated as bores; partial/split cylindrical faces are excluded and make the result
// incomplete. No through/blind inference is made. For each bore the closest non-incident
// non-bore boundary face is reported (this need not be an exterior wall), and each pair
// of non-incident, non-coaxial bores in the same solid is measured. Rows contain kind,
// face_id, other_face_id, radius_mm, optional other_radius_mm, distance_mm, point,
// other_point, and basis="minimum_distance_between_finite_faces". IDs use this shape's
// face map; points are in the shape's model frame. No boundary row is emitted if any
// eligible face distance fails. `incomplete`, `unavailable_boundary_count`,
// `excluded_partial_cylinders`, `failed_distances`, `truncated`, and
// `attempt_limit_reached` disclose exclusions or limits. At most 2000 rows are returned;
// total_rows counts computed rows, and at most 100000 face-pair distances are attempted.
//
// Options: min_dihedral_dev_deg (default 5, in [0, 90)): edges whose deviation is below it
// are "smooth". Throws std::invalid_argument for malformed options.
nlohmann::json edge_analysis(const TopoDS_Shape& shape, const nlohmann::json& options);

} // namespace chudware::geom
