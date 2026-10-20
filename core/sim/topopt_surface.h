// Topology optimization: geometry artifacts from an element density field
// (OCCT-free; Eigen + JSON + the OCCT-free AICM writer).
//
// Interpolated solid. Element densities are volume-weighted onto nodes
// (d_n = sum_e V_e rho_e / sum_e V_e over incident elements) and the P1
// interpolant of d over each tet defines the solid {d >= threshold}. Inside a
// tet the level set of the linear field is one planar polygon, so the solid
// part of a tet is the whole tet, a corner tet, or a prism. Two artifacts are
// derived from ONE vertex set:
//   * the surface: marching-tet polygons plus caps (the parts of the original
//     exterior triangles where d >= threshold);
//   * the clipped volume (clip_density_domain): those per-tet solid pieces
//     split into tetrahedra, conforming across tets, whose boundary is exactly
//     the surface (measured, see ClippedDomain::boundary_matches_surface).
// Exact reconstruction rules (raw surface and reanalysis are never smoothed):
//   * Crossing vertices are keyed by their global mesh edge and computed once,
//     so neighbouring tets and caps share them bit-exactly (no cracks).
//   * Nodal values within `threshold_snap` of the threshold snap onto it and
//     count as solid (tie rule d >= threshold). A crossing whose solid end lies
//     on the threshold IS that node: one node-keyed vertex. Every other
//     crossing then lies at least threshold_snap (as an edge fraction) away
//     from both edge ends, so no triangle has coincident corners.
//   * Quads split along the diagonal through their lowest-keyed vertex (key =
//     global node/edge identity, not creation order); prisms split by the same
//     rule, so shared faces triangulate identically on both sides.
//   * Pieces collapsing at threshold nodes to a point or segment have zero
//     area/volume and are not emitted (counted). Coincident triangle pairs of
//     opposite orientation bound zero volume (a solid sheet exactly at the
//     threshold) and are removed in pairs (counted).
//   * Isosurface triangles face down the density gradient, caps keep the
//     domain's outward winding: the surface is oriented outward.
// Evidence (watertight, manifold, shells, bodies, cavities, shell contact and
// nesting, signed volume, minimum area) is measured on the emitted triangles;
// a surface is never repaired to pass. `GeometryState` names what the geometry
// is; nothing here certifies printability (this module has no print_ready
// notion).
// A separate print surface applies 12 Taubin lambda=0.5, mu=-0.53 iterations
// only to vertices whose incident triangles are all Isosurface-origin. Every
// domain-cap vertex stays bit-exact. Crossing, degenerate or STL-precision
// failures pin the affected vertices and their one-ring, then restart from raw
// (at most eight rollback rounds per iteration count). Remaining validation
// failures halve iterations down to 1, then refuse the print surface.
//
// Retained domain. The whole elements with rho_e >= threshold (a different,
// coarser discretization of the thresholded design), compacted into
// build_tet_mesh input with node maps; compare_discretizations() reports its
// volume against the interpolated surface.
//
// Reanalysis seam. ClippedDomain/RetainedDomain points_mm + tets are
// build_tet_mesh input. remap_to_clipped/remap_to_retained re-resolve a mesh
// node set (fixture, load, port) on the new numbering: retained nodes, plus for
// the clipped volume the crossing vertices on edges of exterior triangles whose
// three corners are all selected (the same all-corner rule as
// surface_faces_with_nodes), so the clipped patch is re-resolved geometrically.
//
// Units. Mesh, surface, volume and evidence are SI (m, m^2, m^3) like TetMesh;
// points_mm arrays are millimetres (build_tet_mesh input). Files: STL and
// AICM positions are millimetres; VTU point coordinates are millimetres and
// field arrays carry the caller's values unchanged (each Field's unit is
// reported in the ArtifactFile descriptor).
//
// Files. Writers stream into a newly created file (exclusive create: an
// existing path is refused with "artifact_exists", never overwritten); a
// failed write removes only the partial file it created. Errors throw
// ModelError with codes invalid_input, invalid_density, invalid_field,
// empty_surface, surface_too_large, stl_precision_loss, aicm_precision_loss,
// artifact_exists, artifact_io, resource_limit.
#pragma once

#include "sim/topopt_model.h"

#include <Eigen/Dense>
#include <nlohmann/json.hpp>

#include <cstdint>
#include <string>
#include <vector>

namespace chudware {
namespace sim {
namespace topology {

struct SurfaceOptions {
  double threshold = 0.5; // (0,1): solid where interpolated density >= it
  // [0, 0.01] and < min(threshold, 1 - threshold) / 2: nodal densities with
  // |d - threshold| <= threshold_snap are set to the threshold first.
  double threshold_snap = 1e-4;
};

// Isosurface: marching-tet level-set polygon. DomainBoundary: (part of) an
// exterior triangle of the design domain. ThresholdFacet: an interior tet face
// between a retained and a removed element (retained-domain boundary only).
enum class TriangleOrigin : std::uint8_t {
  Isosurface = 0,
  DomainBoundary = 1,
  ThresholdFacet = 2
};

// node_b < 0: the vertex is mesh node node_a. Otherwise it lies on mesh edge
// (node_a, node_b) at x = x_a + weight (x_b - x_a), weight in (0,1), node_a
// on the solid side.
struct SurfaceVertexSource {
  int node_a = -1;
  int node_b = -1;
  double weight = 0.0;
};

struct SurfaceMesh {
  Eigen::Matrix3Xd points_m;  // 3 x V
  Eigen::Matrix3Xi triangles; // 3 x T, right-hand normal points outward
  std::vector<SurfaceVertexSource> vertex_source; // V
  Eigen::VectorXi triangle_element;               // T, tet that produced it
  std::vector<TriangleOrigin> triangle_origin;    // T

  int vertex_count() const { return static_cast<int>(points_m.cols()); }
  int triangle_count() const { return static_cast<int>(triangles.cols()); }
};

struct ExtractionStats {
  double threshold = 0, threshold_snap = 0;
  int snapped_nodes = 0;   // nodal values moved onto the threshold
  int threshold_nodes = 0; // referenced nodes exactly at threshold afterwards
  int solid_nodes = 0;     // referenced nodes with d >= threshold
  int collapsed_polygons = 0;        // zero-area point/segment pieces
  int cancelled_sheet_triangles = 0; // removed opposite coincident pairs * 2
};

enum class GeometryState {
  Valid,        // closed, consistently oriented, manifold, one solid body;
                // shells disjoint, every negative shell inside material
  Disconnected, // as Valid but more than one solid body
  Empty,        // no triangles: nothing at or above the threshold has volume
  Unresolved    // open/non-manifold/inconsistent/degenerate/intersecting; see
                // issues
};
const char *to_string(GeometryState); // valid|disconnected|empty|unresolved

struct ShellEvidence {
  int triangles = 0;
  double signed_volume_m3 = 0; // > 0 solid body, < 0 cavity
  double area_m2 = 0;
};

struct SurfaceEvidence {
  GeometryState state = GeometryState::Empty;
  // Machine codes, each present iff its count is nonzero: open_edges,
  // nonmanifold_edges, orientation_conflicts, nonmanifold_vertices,
  // degenerate_triangles, isolated_vertices, zero_volume_shells,
  // intersecting_shells, unenclosed_cavities, inconsistent_nesting,
  // nonpositive_volume.
  std::vector<std::string> issues;
  int vertices = 0, triangles = 0;
  int isosurface_triangles = 0, boundary_triangles = 0, facet_triangles = 0;
  int open_edges = 0;            // edges used by one triangle
  int nonmanifold_edges = 0;     // edges used by more than two triangles
  int orientation_conflicts = 0; // two-triangle edges traversed the same way
  int nonmanifold_vertices = 0;  // vertex fan split into several pieces
  int degenerate_triangles = 0;  // repeated corner or zero/non-finite area
  int isolated_vertices = 0;     // vertices used by no triangle
  bool watertight = false; // every edge in exactly two oppositely wound tris
  bool manifold = false;   // watertight and no non-manifold vertex
  int shells = 0;          // edge-connected triangle sets
  int bodies = 0;          // shells with positive volume
  // Shell classification. Distinct shells that share a vertex or whose
  // triangles touch or cross (within rounding) are intersecting_shells and
  // classified no further. Every other shell lies in one region of the rest,
  // where their winding number w is constant: a negative shell with w = 1 is
  // a cavity, with w = 0 an unenclosed cavity; a positive shell needs w = 0;
  // anything else (a body inside material, a cavity inside nested bodies) is
  // inconsistent_nesting. Contact is tested between shells only; pairs of
  // triangles inside one shell are not intersection-tested.
  int cavities = 0;             // negative shells enclosed by material
  int unenclosed_cavities = 0;  // negative shells not inside any material
  int intersecting_shells = 0;  // shells touching or crossing another shell
  int inconsistent_nesting = 0; // shells whose surroundings are not {0,1} solid
  int zero_volume_shells = 0;   // |volume| within its rounding bound
  int vertex_components = 0;    // vertex-connected triangle sets
  std::vector<ShellEvidence> shell_details; // largest |volume| first, <= 32
  double signed_volume_m3 = 0, area_m2 = 0;
  double min_triangle_area_m2 = 0, min_edge_m = 0;
  double largest_body_volume_fraction = 0; // largest body / all bodies
};

struct SurfaceResult {
  SurfaceMesh mesh;
  ExtractionStats stats;
  SurfaceEvidence evidence;
};

// Volume-weighted nodal average of per-element values (E*components,
// element-major) -> N*components. Nodes no element uses get quiet NaN: they
// have no value, and no consumer here reads them.
Eigen::VectorXd nodal_average(const TetMesh &,
                              const Eigen::VectorXd &element_values,
                              int components = 1);

// Element densities (E, finite, [0,1]) -> nodal_average -> extract.
SurfaceResult extract_density_surface(const TetMesh &,
                                      const Eigen::VectorXd &element_density,
                                      const SurfaceOptions &);
// Nodal densities (N, finite in [0,1] on referenced nodes) -> extract.
SurfaceResult extract_nodal_surface(const TetMesh &,
                                    const Eigen::VectorXd &nodal_density,
                                    const SurfaceOptions &);
// The design domain's exterior triangles (vertices = exterior nodes).
SurfaceMesh domain_boundary_surface(const TetMesh &);
// Topological and metric evidence for any SurfaceMesh.
SurfaceEvidence validate_surface(const SurfaceMesh &);

struct PrintSurfaceOptions {
  int iterations = 12;
  double lambda = 0.5, mu = -0.53;
};
struct PrintSurfaceEvidence {
  std::string state = "skipped";
  int iterations_requested = 12, iterations_used = 0;
  int pinned_vertices = 0; // original boundary pins, excludes rollback vertices
  int smoothed_vertices = 0;
  int rollback_vertices = 0, rollback_rounds = 0;
  double max_displacement_mm = 0, volume_change_fraction = 0;
  bool self_intersections_checked = false;
  std::string reason;
};
// BVH broadphase, triangle-plane interval / coplanar clipping narrowphase.
// Shared vertex/edge contact is permitted, but crossings beyond it are not.
// When requested, collects all affected triangle ids (unique, ascending,
// storage bounded by triangle_count); otherwise stops at the first crossing.
bool surface_self_intersects(const SurfaceMesh &,
                              std::vector<int> *crossing_triangles = nullptr);
// Refuses print_surface_invalid after all iteration counts fail. The evidence
// is populated on success and refusal; the raw topology and sources are retained.
SurfaceMesh smooth_print_surface(const SurfaceMesh &, const PrintSurfaceOptions &,
                                 PrintSurfaceEvidence &);
nlohmann::json to_json(const PrintSurfaceEvidence &);

struct SurfaceOverhang {
  Eigen::VectorXd vertex_fraction;
  double unsupported_area_m2 = 0, total_area_m2 = 0;
};
// Plate height and tolerance are from the design domain (OverhangModel rule).
SurfaceOverhang measure_surface_overhang(const SurfaceMesh &,
                                         const Eigen::Vector3d &direction,
                                         double angle_deg,
                                         double plate_height_m,
                                         double plate_tolerance_m);

// Per surface vertex (V*components): node values are taken exactly, edge
// vertices interpolate linearly along their edge (exact for P1 fields).
// nodal_values is N*components; refuses non-finite values at used nodes.
Eigen::VectorXd interpolate_nodal_field(const SurfaceMesh &,
                                        const Eigen::VectorXd &nodal_values,
                                        int components = 1);
// Per triangle (T*components): the value of the element that produced it.
Eigen::VectorXd
element_field_on_triangles(const SurfaceMesh &,
                           const Eigen::VectorXd &element_values,
                           int components = 1);

// ── Conforming clipped volume of the interpolated solid
// ─────────────────────────────
struct ClippedDomain {
  double threshold = 0, threshold_snap = 0;
  std::vector<double> points_mm; // 3 per vertex (build_tet_mesh input)
  std::vector<int> tets;         // 4 per tet, positive orientation
  std::vector<SurfaceVertexSource>
      vertex_source;           // per vertex (mesh nodes/edges)
  std::vector<int> tet_parent; // per tet: design-domain element it came from
  double volume_m3 = 0, domain_volume_m3 = 0;
  double min_tet_volume_m3 = 0;
  double min_tet_shape =
      0; // min 6 V / l_max^3 (build_tet_mesh refuses <= 1e-10)
  int dropped_degenerate_tets = 0; // distinct corners but non-positive volume
  int face_components = 0;         // face-connected tet sets
  int nonconforming_faces = 0;     // faces shared by more than two tets
  // The faces used by exactly one tet equal the surface triangles (same vertex
  // triples, same orientation) and the volumes agree to rounding.
  bool boundary_matches_surface = false;
  int boundary_faces = 0, unmatched_boundary_faces = 0,
      unmatched_surface_triangles = 0;
  // Surface triangle t uses vertices surface_vertex[v] of this domain.
  std::vector<int> surface_vertex; // surface vertex -> clipped vertex
};

struct ClippedResult {
  SurfaceResult surface;
  ClippedDomain volume;
};
// One pass: the same vertices, rules and surface as extract_density_surface,
// plus the conforming tetrahedra of the solid pieces.
ClippedResult clip_density_domain(const TetMesh &,
                                  const Eigen::VectorXd &element_density,
                                  const SurfaceOptions &);
ClippedResult clip_nodal_domain(const TetMesh &,
                                const Eigen::VectorXd &nodal_density,
                                const SurfaceOptions &);

// ── Whole-element retained domain
// ───────────────────────────────────────────────────
struct RetainedDomain {
  double threshold = 0.5;
  std::vector<int> elements;        // ascending element ids, rho >= threshold
  std::vector<int> nodes;           // compact node -> mesh node
  std::vector<int> node_to_compact; // N, -1 when no retained element uses it
  std::vector<double> points_mm;    // 3 per compact node (build_tet_mesh input)
  std::vector<int> tets;            // 4 per retained element, compact ids
  double volume_m3 = 0, domain_volume_m3 = 0;
  int face_components = 0; // face-connected retained element sets
  SurfaceMesh boundary;    // faces of retained elements not shared with one
  SurfaceEvidence boundary_evidence;
};
// threshold in (0,1); rho finite in [0,1]; rho == threshold is retained.
RetainedDomain retained_domain(const TetMesh &,
                               const Eigen::VectorXd &element_density,
                               double threshold);

struct RetainedNodeSet {
  std::vector<int> nodes;   // sorted unique ids in the new numbering
  std::vector<int> dropped; // sorted unique mesh nodes with no counterpart
};
// Re-resolve a mesh node set (fixture, load, port) on a reanalysis domain.
RetainedNodeSet remap_to_retained(const RetainedDomain &,
                                  const std::vector<int> &mesh_nodes);
// Clipped vertices that are selected nodes, plus crossing vertices on edges of
// exterior triangles with all three corners selected.
RetainedNodeSet remap_to_clipped(const TetMesh &, const ClippedDomain &,
                                 const std::vector<int> &mesh_nodes);

struct DiscretizationComparison {
  double retained_volume_m3 = 0, surface_volume_m3 = 0;
  double difference_m3 = 0;        // surface - retained
  double relative_to_retained = 0; // difference / retained; NaN if retained 0
  double relative_to_domain = 0;   // difference / domain volume
  GeometryState retained_state = GeometryState::Empty;
  GeometryState surface_state = GeometryState::Empty;
};
DiscretizationComparison compare_discretizations(const RetainedDomain &,
                                                 const SurfaceResult &);

// ── Files
// ─────────────────────────────────────────────────────────────────────────
struct ArtifactFile {
  std::string path, format; // format: stl_binary | aicm_v1 | vtu_ascii
  std::string sha256;       // lowercase hex of the bytes written
  std::uint64_t bytes = 0;
  std::string length_unit = "mm";
  std::int64_t points = 0, triangles = 0, cells = 0;
  nlohmann::json arrays =
      nlohmann::json::array(); // [{name,location,unit,components}]
};

// Binary STL, millimetres. Refuses an empty surface and any triangle whose
// float32 corners are coincident or collinear ("stl_precision_loss").
ArtifactFile write_stl(const std::string &path, const SurfaceMesh &,
                       const std::string &label);

// One per-vertex scalar (V values) emitted as an AICM SCLR chunk.
struct SurfaceScalar {
  std::string name, unit;
  Eigen::VectorXd values;
};
// AICM v1 body (positions mm, area-weighted vertex normals of the float32
// triangles, one picking face) plus SCLR chunks with exact finite ranges.
// Refuses surfaces above kMaxOverlayTris (the viewport result-surface budget,
// sim/overlay.h) and any triangle whose float32 millimetre corners are
// coincident or collinear ("aicm_precision_loss"; coordinates are never
// rebased, so such a surface has no faithful AICM form).
ArtifactFile write_surface_aicm(const std::string &path, const SurfaceMesh &,
                                const std::vector<SurfaceScalar> &scalars,
                                const std::string &body_name);

// VTK XML UnstructuredGrid of a whole tet4 domain (referenced nodes only).
// Points mm. PointData: node_id (mesh node), nodal_density, every Node field.
// CellData: density, retained (rho >= threshold), every Element field. Field
// names must be unique and distinct from those four; values finite.
ArtifactFile write_density_vtu(const std::string &path, const TetMesh &,
                               const Eigen::VectorXd &element_density,
                               double threshold,
                               const std::vector<Field> &fields);

// SI-keyed summaries for the run adapter.
nlohmann::json to_json(const ExtractionStats &);
nlohmann::json to_json(const SurfaceEvidence &);
nlohmann::json to_json(const DiscretizationComparison &);
nlohmann::json to_json(const ArtifactFile &);
// Counts, volumes, components and evidence (not the arrays).
nlohmann::json summary_json(const RetainedDomain &);
nlohmann::json summary_json(const ClippedDomain &);

} // namespace topology
} // namespace sim
} // namespace chudware
