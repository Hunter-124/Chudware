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
// 0.35 mm request solved a 1.299 mm interface. Shared, so the mesher's own
// last-resort re-fill (fill_with_recovery) and fea_run's study refusal cannot
// disagree about whether a contract was met.
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

struct MeshOut {
  std::vector<double> points; // n*3 flat, row-major, millimetres
  std::vector<int> tets;      // m*K flat, 0-based; K is nodes_per_elem
  double char_len_mm = 0.0;
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
  bool local_refine_ceiling_bound = false;
  uint64_t local_refine_element_ceiling = 0;
  // What stopped the delivery short, when something did (a bisection closure
  // that could not conform, a wave that grew nothing). Empty when the waves
  // simply converged on the request.
  std::string local_refine_limit;

  // Fill cells whose node order was left-handed (negative signed volume) and
  // was relabelled right-handed before promotion (see
  // orient_tets_right_handed in mesher.cpp). Informational, not a defect
  // count: node order carries no meaning in the tet4 contract (every consumer
  // takes |det|), but pm::fea::promote_to_quadratic admits an element only on
  // a POSITIVE tet10 Jacobian, so a left-handed cell used to be declined,
  // left linear inside a quadratic mesh, and refused the whole order-2 study
  // as "unsupported tet4 connectivity". A nonzero value here means polymesh's
  // graded fill emitted inverted cells at this size (33 of 467162 on the
  // ME 490 wheel-disc 90 deg sector at fineness 0.5) and Chudware absorbed
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
// coarse mesh. What it CANNOT do is beat the fill's own lattice: polymesh's graded
// tet places local children at h/2 and h/4 of the background cell, so a size below
// background/4 lowers the background as far as 4x the requested local size and says
// so in the mesh note.
struct MeshLocalRefine {
  std::vector<double> points; // n*3 flat, millimetres, world coordinates
  double size_mm = 0.0;       // requested local element size, mm (0 = no request)
  double band_mm = 0.0;       // influence radius around each point, mm
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
  MeshLocalRefine local_refine;
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
