// fea_prepare.h — reusable, solver-generic preparation seams over the FEA glue.
//
// The study routes in fea_run.cpp (static / modal / thermal / buckling) each
// mesh, resolve their face selections and deposit loads inline, once per solve.
// A solver that iterates on ONE discretization (an optimizer, a sweep) needs
// the same resolution done once and then held immutable. These wrappers expose
// exactly that, implemented on the SAME code the studies run — resolve_mesh
// (STEP cache or inline mesh), boundary_faceset_membership (the geometric
// surface oracle intersected with the exterior skin), the tet10 mid-side
// completion and the work-consistent force/pressure deposition — so a prepared
// problem lands its boundary conditions on the nodes a static study of the same
// selectors would.
//
// Scope is a single conforming linear continuum. Contact, bonded-gap/tie
// interfaces and meshes that fall apart into several disconnected components
// are REFUSED by prepare_fea_mesh rather than approximated: nothing here can
// couple two islands.
//
// Units are the study's: lengths mm, forces N, pressure MPa (+p acts inward),
// areas mm². Errors are returned as text (`error`), never thrown; JSON parse
// problems in a caller-built study surface the same way.
//
// OCCT-free and mesher-backend-free (mesher.h is declarations only), like
// fea_run.h.
#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

#include "sim/mesher.h"
#include "sim/sim_cancel.h"

namespace chudware::sim {

// ── Job scope: one registration of study.job_id for the whole run
// ────────────── Registers `study.job_id` (absent/empty -> anonymous, token is
// null and never cancels) for the lifetime of the scope, so `sim_cancel
// {job_id}` flips the token every kernel of the run polls. Passing a non-null
// `inherited_token` reuses an enclosing run's registration instead (a composite
// study keeps ONE cancellable generation); the scope then does not unregister
// it. Every fea_run.cpp route uses this class; a new route should too, rather
// than calling sim_job_register itself.
class SimJobScope {
public:
  explicit SimJobScope(const nlohmann::json &study,
                       const SimCancelToken &inherited_token = {})
      : id_(study.is_object() && study.contains("job_id") &&
                    study["job_id"].is_string()
                ? study["job_id"].get<std::string>()
                : std::string()),
        token_(inherited_token ? inherited_token : sim_job_register(id_)),
        owns_registration_(!inherited_token) {}
  ~SimJobScope() {
    if (owns_registration_)
      sim_job_unregister(id_, token_);
  }
  SimJobScope(const SimJobScope &) = delete;
  SimJobScope &operator=(const SimJobScope &) = delete;

  const SimCancelToken &token() const { return token_; }
  bool cancelled() const {
    return token_ && token_->load(std::memory_order_relaxed);
  }

private:
  std::string id_;
  SimCancelToken token_;
  bool owns_registration_ = false;
};

// ── Prepared mesh
// ────────────────────────────────────────────────────────────── The
// discretization a prepared problem solves, resolved once and shared read-only.
struct PreparedMesh {
  std::vector<double> points; // n*3, mm
  std::vector<int> tets;      // m*nodes_per_elem, 0-based
  int nodes_per_elem = 4;     // 4 (tet4), 10 (tet10) or 8 (hex8, inline only)
  // 1 when an element references the node. Mesh repair keeps node numbering, so
  // a node no element references can exist; it carries no stiffness and a
  // solver must eliminate (pin) it. `unreferenced_nodes` counts the zeros.
  std::vector<std::uint8_t> referenced;
  int unreferenced_nodes = 0;
  // Node-adjacency components over the referenced nodes (always 1 once
  // prepared).
  int connected_components = 0;
  // Meshed STEP bodies (empty for an inline mesh): contiguous node ranges with
  // the 1-based STEP solid ordinals fused into each. A multi-solid STEP that
  // prepared successfully was fused by the mesher into one conforming domain.
  std::vector<MeshOut::Body> bodies;
  std::string mesh_type_used;     // mesher's label; empty for an inline mesh
  std::vector<std::string> notes; // mesher note + repair facts, for the result
  // Same objects the study routes report: top-level `mesh_quality`
  // (connected_components authoritative from the union-find above), the mesher
  // governor evidence for `provenance.governor`, and the learned-advisor
  // envelope (null when none applied).
  nlohmann::json mesh_quality;
  nlohmann::json governor;
  nlohmann::json advisor;

  std::size_t n_nodes() const { return points.size() / 3; }
  std::size_t n_elems() const {
    return nodes_per_elem > 0
               ? tets.size() / static_cast<std::size_t>(nodes_per_elem)
               : 0;
  }
};

struct PrepareMeshOptions {
  // Prefix for every error/note ("topology: ...").
  std::string context = "fea_prepare";
  // Forwarded to the mesher (MeshOptions::iterative_solver): size the mesh for
  // an iterative rather than a direct factorization.
  bool iterative_solver = false;
};

// Mesh `study` once: `step_path` through the process mesh cache with the
// study's mesh options (mesh_order, fineness, mesh_target_mm, …), or the inline
// `points`/`tets`/`nodes_per_elem` arrays. Emits sim.progress "mesh.begin" and
// "setup". Refuses (null + `error`):
//   - no mesh source, malformed inline arrays (non-finite points, indices out
//   of
//     range, element stride not 4/8/10), mesher failure;
//   - a declared contact/interfaces block or non-zero bond_gap_mm (not a single
//     continuum);
//   - more than one connected component (disconnected bodies cannot be coupled
//     here), or hanging-fragment nodes the mesher could not dissolve
//     (singular).
// A successful result is immutable and safe to share across
// threads/evaluations.
std::shared_ptr<const PreparedMesh>
prepare_fea_mesh(const nlohmann::json &study, std::string &error,
                 const PrepareMeshOptions &options = {});

// ── Resolved selections
// ───────────────────────────────────────────────────────── One study selector
// resolved on a prepared mesh. `label` is the static route's selector label
// ("<selector|sid> (<arg>[<ordinal>])", else "<arg>[<ordinal>]"), and `nodes`
// are sorted unique.
struct ResolvedSelection {
  std::string label;
  std::vector<int> nodes;
};

// One exterior element face of a boundary selection, with the consistent
// surface integrals the FEA load deposition uses (tet10: mapped tri6
// quadrature; tet4/hex8: area/n per corner).
struct BoundaryFacet {
  int element = -1; // element index (into tets / nodes_per_elem)
  int face = -1;    // local face index (tet 0..3, hex 0..5)
  std::array<int, 6> nodes{{-1, -1, -1, -1, -1, -1}}; // global node ids
  int node_count = 0; // 3 tet4, 6 tet10 (corners then mids), 4 hex8
  double area_mm2 = 0.0;
  std::array<double, 3> normal{{0.0, 0.0, 0.0}}; // unit, OUTWARD, area-averaged
  double weights[6] = {}; // ∫ N_i dA, mm²; entries past node_count are 0
  // ∫ N_i n̂ dA, mm² (outward). A uniform pressure p (+ inward) deposits
  // −p · normal_weights[i] on nodes[i]. Rows past node_count are 0.
  double normal_weights[6][3] = {};
};

// A face selector resolved to the exterior skin: nodes (geometric oracle ∩
// exterior-face nodes, tet10 mid-sides completed), and every exterior element
// face whose corners are all selected. `node_area_mm2[i]` = Σ_facets ∫ N dA at
// nodes[i] (0 for a node on no complete facet). `facets` is empty when the
// nodes hold no complete face (a point/edge pick): a nodal (Dirichlet) use is
// still valid, a flux or traction use is not, and the caller must refuse it.
struct BoundarySelection {
  std::string label;
  std::vector<int> nodes;
  std::vector<double> node_area_mm2;
  std::vector<BoundaryFacet> facets;
  double area_mm2 = 0.0;
};

// Resolve one faceset object ({points, tol_mm, face_normal|surface,
// perp_tol_mm} as doc_face_points emits) on `mesh`. `label` names it in errors.
// Refuses a malformed faceset, a selection that resolves to no exterior node,
// or a folded tet10 face.
bool resolve_boundary_selection(const nlohmann::json &faceset,
                                const PreparedMesh &mesh,
                                const std::string &label,
                                BoundarySelection &out, std::string &error);

// ── Mechanical boundary conditions
// ────────────────────────────────────────────── Fixtures: an array of faceset
// objects in the static-study syntax, each with an optional `dofs` subset of
// [0,1,2] (x,y,z; omitted = all three). Shared and per-load-case fixtures are
// resolved together by concatenating the arrays.
struct MechanicalFixtureSet {
  std::vector<std::uint8_t>
      fixed_dof;                // n_nodes*3; 1 = prescribed zero displacement
  std::vector<int> fixed_nodes; // nodes with all three DOFs fixed, sorted
  std::vector<int> constrained_nodes; // nodes with any DOF fixed, sorted
  std::size_t fixed_dof_count = 0;
  std::vector<ResolvedSelection> selections; // per fixture, study order
};

// Resolve fixtures through the static route's oracle
// (boundary_faceset_membership, no mid-side completion — identical node sets to
// fea_run_static's fixtures). Refuses an empty/malformed list, an invalid
// `dofs`, a fixture that resolves to 0 nodes, and a constraint set that leaves
// a rigid-body mode free (rank of the six rigid-body modes restricted to the
// fixed DOFs < 6) — a singular problem is never handed on.
bool resolve_mechanical_fixtures(const nlohmann::json &fixtures,
                                 const PreparedMesh &mesh,
                                 MechanicalFixtureSet &out, std::string &error,
                                 const std::string &context = "fea_prepare");

// One load case's nodal force vector.
struct MechanicalLoadVector {
  std::vector<double> forces;                         // n_nodes*3, N
  std::array<double, 3> resultant_n{{0.0, 0.0, 0.0}}; // Σ forces
  std::vector<ResolvedSelection> selections; // per non-zero load, study order
  std::vector<std::string> notes; // deposition notes (as static reports them)
};

// Deposit `loads` (static-study load syntax: [{faces, type:"force", vector:[N]}
// | {faces, type:"pressure", magnitude:MPa (+ inward)}]) exactly as
// fea_run_static and fea_run_buckling do (tet10 work-consistent force split,
// consistent pressure). Call once per load case. Refuses an empty/malformed
// list, an all-zero case, a load face that resolves to 0 nodes, a pressure
// without a complete exterior face, or a folded tet10 face.
bool resolve_mechanical_load_case(const nlohmann::json &loads,
                                  const PreparedMesh &mesh,
                                  MechanicalLoadVector &out, std::string &error,
                                  const std::string &context = "fea_prepare");

} // namespace chudware::sim
