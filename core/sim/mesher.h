// Polyhedral-FEA volume meshing for Chudware simulation studies.
//
// The implementation loads STEP/BREP geometry through the same OpenCASCADE
// build as Chudware, generates a graded tetrahedral mesh, and normalizes it to
// the flat tet4/tet10 representation consumed by the existing static, modal,
// thermal, fatigue, overlay, and viewport paths. Coordinates and element sizes
// are millimetres.
//
// Polyhedral-FEA is BSD-3-Clause and links statically.
#pragma once

#include <cstdint>
#include <memory>
#include <optional>
#include <string>
#include <vector>

namespace chudware::sim {

// How far a DELIVERED local element size may sit above the requested one before
// the request counts as unmet. A mesher never lands exactly on a requested size
// (bisection halves elements, so the last wave overshoots downward), and the
// contact guard's own measure is a median over candidate pairs rather than a
// maximum, so a little slack is honest. 1.5x is not: at 2x the ratio the
// resolution guard applies has already moved a whole factor, which is how a
// 0.35 mm request solved a 1.299 mm interface. Shared by the mesher and the
// study refusal so they cannot disagree about whether the contract was met.
constexpr double kLocalContractSlack = 1.5;

// Optional learned-advisor telemetry. Absence means the study did not request
// advice; available=false means it requested advice but the model declined or
// was unavailable.
struct MeshAdvisor {
  bool available = false;
  bool applied = false;
  bool vetoed = false;
  bool budget_refusal = false;
  std::string mesher;
  double h_rel = 0.0;
  int order = 1;
  int adapt_passes = 0;
  double eta_target = 0.0;
  double predicted_dof = 0.0;
  double predicted_mesh_ms = 0.0;
  double predicted_solve_ms = 0.0;
  double failure_prob = 0.0;
  double ood_distance = 0.0;
  std::string model_revision;
  std::string note;
};

// Measured before solver assembly, not a factorization measurement. Count
// forecasts belong to the immediately preceding (at most one-octave) probe.
struct MeshGovernorProbe {
  double h = 0.0;
  uint64_t elems = 0;
  double rss_gb = 0.0;
  double predicted_elems = 0.0; // 0 = unmeasured bootstrap
  double exponent = 2.0;
  double elapsed_s = 0.0; // wall time of this fill, including recovery
  const char *phase = "background"; // background, local_refine, combined, admission_fallback
};

struct MeshGovernorSkippedProbe {
  double h = 0.0;
  double predicted_s = 0.0;
  double remaining_s = 0.0;
  std::string reason;
};

struct MeshOut {
  std::vector<double> points; // n*3 flat, row-major, millimetres
  std::vector<int> tets;      // m*K flat, 0-based; K is nodes_per_elem
  double char_len_mm = 0.0;
  double size_band_applied_floor_mm = 0.0;
  std::string size_band_reason = "none";
  // Unique corner-to-corner edges, not the half-edges of tet10 mids.
  double edge_min_mm = 0.0;
  double edge_median_mm = 0.0;
  double edge_max_mm = 0.0;
  std::vector<MeshGovernorProbe> governor_probes;
  std::string governor_reason;
  std::vector<MeshGovernorSkippedProbe> governor_skipped_probes;
  double governor_probe_budget_s = 0.0;
  double governor_elapsed_s = 0.0;
  const char *governor_budget_origin = "default";
  int n_nodes = 0;
  int n_elems = 0;
  int nodes_per_elem = 4; // 4 (tet4) or 10 (tet10)
  double min_tet_volume_mm3 = 0.0;
  double max_tet_edge_ratio = 0.0;
  // Elements whose projected tet10 mid-edge nodes folded the quadratic map and
  // were reverted to their straight-edge midpoints (see
  // detail::repair_folded_quadratic). 0 for tet4 and for a clean tet10 mesh.
  int quadratic_fallback_elems = 0;

  // ── Hanging-element repair (detail::repair_lonely_nodes) ─────────────────
  // A CORNER node referenced by exactly one element, or an element sharing two
  // corners or fewer with the rest of the mesh, is a mechanism dressed as a mesh:
  // the element can rotate about the node or edge it hangs from at zero strain
  // energy, so the stiffness matrix stops being positive definite while every
  // connectivity check still passes (the fragment DOES reach a fixture, through
  // that one node). Measured on a real 316466-element wheel mesh: 51 such nodes,
  // condition number 4e33, a fallback factorization returning a plausible-looking
  // 0.032 mm where an independent SparseLU returned 5.2e10 mm.
  //
  // The repair dissolves those elements (see the .cpp for the exact rule) and
  // reports what it did. lonely_nodes_remaining > 0 after the repair is a REFUSAL
  // downstream, in the same class as a component that reaches no fixture — never a
  // warning, because a warning has to be read.
  int lonely_nodes_repaired = 0;
  int lonely_nodes_remaining = 0;
  int hanging_elems_dropped = 0;
  // Raw count of corners referenced by exactly one element ("tips"). Ordinary in a
  // healthy mesh — a tet apex nobody else uses is fully constrained by its own
  // element — so this is informational only, NOT a defect count. It is what
  // localizes a mechanism when one exists.
  int single_element_nodes = 0;
  double dropped_volume_mm3 = 0.0;

  // Node-adjacency connected components over the elements (union-find over
  // every node of every element): 1 for one watertight solid, N for a compound
  // or an imported assembly, where each island is its own stiffness system. A
  // loaded island that reaches no fixture is a rigid-body mechanism, and a
  // solve on one returns a meaningless "converged" answer. 0 = not computed
  // (quality metrics unavailable — see fill_quality).
  int connected_components = 0;
  // The thinnest wall measured in element sizes: the minimum over boundary
  // elements of (local wall-thickness estimate / local element size). Below 1
  // one element spans the wall, which cannot represent bending — the regime
  // that turns a thin tube into a mechanism. 0 = unavailable (inline mesh, no
  // CAD tessellation, or an open shell the inward raycast never crossed).
  double thin_wall_ratio = 0.0;
  double min_wall_mm = 0.0;

  double aspect_min = 0.0;
  double aspect_avg = 0.0;
  double aspect_max = 0.0;
  double jacobian_min = 0.0;
  double pct_poor = 0.0;
  std::vector<int> aspect_hist;

  // ── Local size-contract delivery (MeshLocalRefine) ───────────────────────
  // What was asked for and what the mesh actually carries at the seeds. The
  // seed stamp + graded field bottom out at background/4, and the background
  // itself has a floor polymesh derives from its Cartesian grid budget, so a
  // request below that is delivered by longest-edge bisection of the elements
  // at the seeds — and the ACHIEVED size is measured, not assumed. A caller
  // whose contract is hard (contact.interface_size_mm) refuses on
  // `local_refine_achieved_mm`; `local_refine_ceiling_bound` says the element
  // ceiling is what stopped it, which is a different refusal.
  double local_refine_size_mm = 0.0;      // requested (0 = no request)
  double local_refine_achieved_mm = 0.0;  // coarsest element left at the seeds
  int local_refine_waves = 0;             // LEB waves spent
  int local_refine_elems_added = 0;
  // Cumulative centroid-grading candidate marks across waves, before deduplication
  // with boundary owners. Closure may extend beyond this depth; direct centroid
  // marks may not. Counts do not include interface-face owners or closure cells.
  double local_refine_transition_depth_mm = 0.0;
  uint64_t local_refine_transition_marks = 0;
  uint64_t local_refine_outside_transition_marks = 0;
  uint64_t local_refine_outside_transition_checks = 0;
  bool local_refine_ceiling_bound = false;
  uint64_t local_refine_element_ceiling = 0;
  // What stopped the delivery short, when something did (a bisection closure
  // that could not conform, a wave that grew nothing). Empty when the waves
  // simply converged on the request.
  std::string local_refine_limit;
  // ── The bounded quality pass over the refined region ─────────────────────
  // A bisection wave is conforming but not shapely: LEPP closure leaves
  // stretched neighbours a whole size class away from the interface, and the
  // rover hinge saddle came back with aspect_max 263 / jacobian_min 0.0029
  // and 50 cells under polymesh's ship floor while the interface itself was
  // exactly what was asked for. So each wave — and the mesh the last wave
  // leaves — gets a bounded pass of boundary-aware smoothing, 2-3/3-2 edge
  // flips and sliver collapses over the region the size field reached
  // (mesher.cpp: region_quality / quality_pass). These are the pass's own
  // before/after, measured over that region only: the whole-mesh aspect_max /
  // jacobian_min above still describe the whole mesh.
  int local_refine_smoothing_passes = 0;
  double local_refine_jacobian_min_before = 0.0;
  double local_refine_jacobian_min_after = 0.0;
  double local_refine_aspect_max_before = 0.0;
  double local_refine_aspect_max_after = 0.0;
  uint64_t local_refine_cells_below_floor_before = 0;
  uint64_t local_refine_cells_below_floor_after = 0;
  // A quality pass moves nodes, so it can lengthen the interface faces the
  // contract is measured on. When it coarsened the delivery past the contract
  // the interface is re-marked and re-split ONCE, and that is reported: a
  // delivered size nobody re-measured after smoothing is not a measurement.
  bool local_refine_resplit = false;

  // Fill cells whose node order was left-handed (negative signed volume) and
  // was relabelled right-handed before promotion (see
  // orient_tets_right_handed in mesher.cpp). Informational, not a defect
  // count: node order carries no meaning in the tet4 contract (every consumer
  // takes |det|), but pm::fea::promote_to_quadratic admits an element only on
  // a POSITIVE tet10 Jacobian, so a left-handed cell used to be declined,
  // left linear inside a quadratic mesh, and refused the whole order-2 study
  // as "unsupported tet4 connectivity". A nonzero value here means polymesh's
  // graded fill emitted inverted cells at this size (33 of 467162 on the
  // rover wheel-disc 90 deg sector at fineness 0.5) and Chudware absorbed
  // them.
  int reoriented_elems = 0;

  // Cells the fill shipped FLAT (shape quality below a millionth of a regular
  // tet, i.e. zero volume to machine precision) that Chudware inflated by
  // moving one node within a hard bound, and how many no single-node move
  // could save (see repair_degenerate_tets in mesher.cpp). polymesh's own ship
  // gate can only relax INTERIOR nodes, so on a thin web — where every node of
  // a boundary sliver is a boundary node — it ships the cell and reports it in
  // its note. Such a cell has no positive tet10 Jacobian, so it used to cost
  // the whole order-2 study; at order 1 it silently contributed noise
  // stiffness. `degenerate_elems_remaining` > 0 is a refusal, never a warning.
  int degenerate_elems_repaired = 0;
  int degenerate_elems_remaining = 0;

  // ── Which CAD solid each meshed node came from ───────────────────────────
  // A multi-solid STEP is meshed solid by solid (mesh_separate_solids), so the
  // node array is a CONCATENATION of per-body blocks and every body occupies
  // one contiguous half-open range. Recording the ranges instead of a per-node
  // or per-element body id is not a micro-optimization: a body id array costs
  // 4 bytes per element (34 MB on an 8.6M-element mesh) to carry information
  // the append order already has exactly.
  //
  // `source_ordinals` are the 1-based STEP solid ordinals — the same numbering
  // `import_part(keep_bodies=...)` uses — that ended up in this meshed body;
  // it holds more than one entry when overlapping solids were fused (see
  // fuse below). This is what lets a rigid-body refusal NAME the island's
  // body instead of reporting an anonymous "component 3".
  struct Body {
    std::vector<int> source_ordinals;
    int node_begin = 0; // into points/3
    int node_end = 0;   // exclusive
    double volume_mm3 = 0.0;        // CAD volume of the body that was meshed
    double fused_overlap_mm3 = 0.0; // volume the fuse removed (0 = not fused)
  };
  std::vector<Body> bodies;

  // ── Solids the mesher FUSED before filling ───────────────────────────────
  // Separately-meshed solids share no nodes, so two rods that merely touch (or
  // interpenetrate — a rover model records 9299.81 mm3 of overlap between
  // source bodies 200 and 201) mesh as two islands and every static study on
  // them is refused for rigid-body motion. A multi-solid STEP whose solids
  // OVERLAP or touch within tolerance is therefore fused into one solid before
  // the fill, unless the study declared how those interfaces are to be treated
  // (MeshOptions::declared_interfaces) — a declared contact or bonded-tie
  // interface must keep its two bodies. `fused_groups` counts the fusions and
  // `fused_overlap_mm3` is the total volume delta (sum of the inputs minus the
  // fused result), i.e. the interpenetration the design model carried.
  int fused_groups = 0;
  int fused_solids = 0;
  double fused_overlap_mm3 = 0.0;

  // ── Sub-element debris the fill left as separate islands ─────────────────
  // A graded fill on a solid whose lumps meet TANGENTIALLY (body 201's rods
  // pass through each other, leaving a 9.8 um knife-edge where they emerge)
  // sheds cells at the knife edge that touch nothing else: measured on that
  // isolate at tet4 h = 3.0 mm, 3 islands of 1/3/3 elements totalling
  // 0.0101 mm3 against a domain of 88504.8 mm3. They are not bodies and they
  // are not hanging flaps either (each is internally face-connected, so the
  // corner rule below never sees them) — they are debris below the fill's own
  // resolution, and each one costs the whole study a rigid-body refusal. The
  // dissolve drops any component both under a millionth of the domain volume
  // and smaller than ONE nominal element of the requested size; anything
  // bigger is real geometry and is still refused, by name.
  int debris_components_dissolved = 0;
  int debris_elems_dropped = 0;
  double debris_volume_mm3 = 0.0;

  std::string mesh_type_used; // "tet4" or "tet10"
  std::string note;
  std::optional<MeshAdvisor> advisor;

  bool ok = false;
  std::string error;
};

// LOCAL element-size contract: refine to `size_mm` within `band_mm` of these world
// points, leaving the background size alone. This exists because the two things a
// study most often has to resolve — a contact clearance and a small hole — are
// LOCAL, and resolving them by lowering the uniform size is what turns a 50k-element
// plate into a 1.2M-element one (or into "mesh element ceiling exceeded"). The
// mesher applies it as polymesh's own fine-cell seeds plus a graded size field, so
// the fine cells land at the seeds and grade back out to the background size.
//
// It is a CONTRACT, like target_size_mm: the element-count governor never coarsens
// it (see mesh_size_band.cpp — the governor moves the background size only), and a
// fill that cannot deliver it fails the study rather than silently returning the
// coarse mesh. The fill's lattice only places local children at h/2 and h/4;
// longest-edge bisection delivers smaller requests at the selected background
// size, once, and reports the achieved local size for downstream refusal.
struct MeshLocalRefine {
  std::vector<double> points; // n*3 flat, millimetres, world coordinates
  double size_mm = 0.0;       // requested local element size, mm (0 = no request)
  double band_mm = 0.0;       // influence radius around each point, mm
  // Optional maximum direct-refinement depth; conforming closure can extend beyond.
  // Contact uses 3*size_mm: two fine layers plus one graded transition layer.
  double max_depth_mm = 0.0;
  std::string label;          // what asked for it, quoted in the mesh note

  bool active() const { return size_mm > 0.0 && !points.empty(); }
};

struct MeshOptions {
  // 0 = coarse, 1 = fine. Base size derives from the shape bounding-box
  // diagonal.
  double fineness = 0.5;
  int order = 1;               // 1=tet4, 2=tet10
  double target_size_mm = 0.0; // >0 overrides derived base size
  double min_size_mm = 0.0;    // >0 overrides the automatic size-band floor
  double max_size_mm = 0.0;    // >0 overrides the automatic size-band ceiling
  bool learned_advisor = false;
  uint32_t advisor_max_dof = 0; // 0 disables the advisor's DOF budget
  // The STUDY has declared how the interfaces between its bodies are treated
  // (a `contact` block, or a positive `bond_gap_mm`). Overlapping/touching
  // solids are then meshed as SEPARATE bodies — the declaration is the whole
  // point of the study — instead of being fused into one (MeshOut::fused_*).
  bool declared_interfaces = false;
  MeshLocalRefine local_refine;
  // Measurements from preceding legs of the same geometry/order ladder.
  std::vector<MeshGovernorProbe> governor_probes;
  // Keep mesh allocation ceilings, but defer solver admission to the actual
  // sparse pattern rather than applying a direct-factor element-count model.
  bool iterative_solver = false;
};

bool validate_mesh_options(const MeshOptions &options, std::string &error);

inline constexpr double kPoorAspect = 10.0;

// Mesh a STEP/BREP file. Never throws; failures return ok=false with a
// diagnostic. Tet10 connectivity uses Chudware's canonical mid-edge order:
// {0,1},{1,2},{2,0},{3,0},{3,2},{3,1}.
MeshOut mesh_step(const std::string &step_path, const MeshOptions &options);

// Content-and-options keyed immutable mesh cache. A hit returns the resident
// mesh without copying its points/connectivity arrays.
std::shared_ptr<const MeshOut> mesh_step_cached(const std::string &step_path,
                                                const MeshOptions &options);

struct MeshCacheStats {
  uint64_t hits = 0;
  uint64_t misses = 0;
  uint64_t generations = 0;
  uint64_t resident_bytes = 0;
  uint64_t budget_bytes = 0;
};
MeshCacheStats mesh_cache_stats();
void mesh_cache_reset_stats();
void mesh_cache_note_generation();

namespace detail {
bool fill_quality(MeshOut &out);
// Unique corner-edge statistics after fill_quality validated connectivity.
void measure_edges(MeshOut &out);

// Aspect ratio and minimum corner scaled Jacobian of ONE tet, from four corner
// points given as xyz triples. These are exactly the definitions fill_quality
// aggregates into MeshOut::aspect_max / jacobian_min, and the local
// quality pass in mesher.cpp has to judge candidate node moves and edge flips
// in the same currency the report is stated in — two definitions of "aspect"
// in one mesher is how a pass converges on a floor the reader never sees.
// `aspect` is (longest edge / shortest altitude) normalized so a regular tet
// scores 1.0; `scaled_jacobian` is the min over the four corners of
// sqrt(2)*|6V| / (product of the three incident edge lengths), also 1.0 for a
// regular tet. Both are magnitudes — Chudware's tet4 contract carries no
// orientation, so a mirrored cell measures like its twin and inversion is a
// separate verdict. A zero-volume tet reports aspect = +inf and
// scaled_jacobian = 0.
struct TetShape {
  double aspect = 0.0;
  double scaled_jacobian = 0.0;
};
TetShape tet_shape(const double *a, const double *b, const double *c,
                   const double *d);

// Element-local straight-edge fallback for a promoted+projected tet10 mesh.
//
// Projecting free-surface mid-edge nodes onto the exact BRep is what lets a
// quadratic element follow a bore or a fillet, but when the element size is
// comparable to the wall thickness or the curvature radius the projection can
// push a mid node far enough to FOLD the element: the quadratic Jacobian
// changes sign inside it, or one of its tri6 faces reverses. Such a mesh is
// refused downstream ("invalid or folded quadratic boundary face"), which used
// to force the whole study back to tet4.
//
// This reverts the mid-edge nodes of ONLY the folded elements to the midpoint
// of their straight chord — those elements lose the curved geometry, the rest
// of the mesh keeps it. Reverting a shared mid node can in principle unfold one
// neighbour and fold another, so the sweep repeats until it converges (bounded).
// Returns the number of elements that took the fallback, and records it in
// out.quadratic_fallback_elems. A no-op for tet4.
int repair_folded_quadratic(MeshOut &out);

// Dissolve HANGING elements: an element whose corners shared with the rest of the
// mesh number two or fewer is free to rotate about that node or edge at zero strain
// energy. Removing it removes the mechanism; the fragment carried no load path by
// construction (nothing else touches its free corners), so the domain loses only a
// sliver, which is reported as dropped_volume_mm3. Dropping one element can expose
// another, so the sweep repeats until stable (bounded). Node numbering is preserved —
// only the element list shrinks — so every caller's node indices stay valid.
// Fills lonely_nodes_repaired / hanging_elems_dropped / dropped_volume_mm3 and
// re-counts lonely_nodes_remaining. Returns the number of elements dropped.
int repair_lonely_nodes(MeshOut &out);

// Corner nodes referenced by exactly ONE element, in the mesh as it stands. Counted
// on corners only: a tet10 mid-edge node legitimately belongs to a single element and
// carries no rigid mode of its own. Also fills out.lonely_nodes_remaining.
int count_lonely_nodes(MeshOut &out);
} // namespace detail

// Error-driven refinement wrapper. Chudware keeps its solver and uses
// Polyhedral-FEA for ZZ recovery, marking, and spatial sizing so every adaptive
// pass remains comparable with an ordinary study result.
class AdaptiveMesher {
public:
  static std::unique_ptr<AdaptiveMesher> open(const std::string &step_path,
                                              const MeshOptions &options,
                                              std::string &error);
  ~AdaptiveMesher();

  AdaptiveMesher(const AdaptiveMesher &) = delete;
  AdaptiveMesher &operator=(const AdaptiveMesher &) = delete;

  bool mesh(MeshOut &out, std::string &error);
  bool estimate_error(const std::vector<double> &disp_mm, double e_mpa,
                      double nu, std::vector<double> &element_eta,
                      double &global_eta, std::string &error);
  bool refine(const std::vector<double> &element_eta, std::string &note,
              std::string &error);
  double element_size_mm() const;

private:
  struct Impl;
  explicit AdaptiveMesher(std::unique_ptr<Impl> impl);
  std::unique_ptr<Impl> impl_;
};

} // namespace chudware::sim
