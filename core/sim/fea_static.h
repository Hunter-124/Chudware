// Linear-static FEA on tet4 meshes — C++ port of engine/sim/fea_static.py.
//
// First solver of the simulation rewrite. The numerics (tet4 strain-displacement,
// isotropic elasticity, global stiffness assembly, von Mises recovery) mirror the
// Python oracle exactly; the sparse direct solve uses Eigen::SparseLU in place of
// scipy.sparse.linalg.spsolve. CUDA acceleration (batched assembly / a GPU sparse
// solve) is a later optimization layer on top of this correct CPU core.
//
// Boundary conditions arrive ALREADY RESOLVED to mesh nodes: a list of fixed node
// indices (each pins all 3 of its DOFs to zero) and a per-node force vector. The
// point-cloud fixture/load matching of run_static is reproduced by the caller (the
// parity harness uses the SAME Python helper), so this core is the pure linear-FEA
// kernel: assemble -> constrain -> solve -> recover stress.
#pragma once

#include <array>
#include <cstdint>
#include <string>
#include <vector>
#include "sim/sim_cancel.h"

namespace chudware::sim {

struct FeaStaticResult {
    std::vector<double> displacement;    // n*3, row-major (node i -> [3i,3i+1,3i+2]) mm
    std::vector<double> disp_magnitude;  // n,  ||u|| per node, mm
    std::vector<double> von_mises;       // n,  node-averaged von Mises stress, MPa

    // Additive stress fields (all length n_nodes, node-averaged, MPa). These are
    // computed from the SAME averaged stress tensor as von_mises and never alter the
    // existing von_mises/disp numerics (parity-safe).
    std::vector<double> principal1;   // max principal stress per node (MPa)
    std::vector<double> principal3;   // min principal stress per node (MPa)
    std::vector<double> max_shear;    // (p1 - p3)/2 per node (MPa)
    std::vector<double> stress_xx;    // node-averaged normal stress sx (MPa)
    std::vector<double> stress_yy;    // node-averaged normal stress sy (MPa)
    std::vector<double> stress_zz;    // node-averaged normal stress sz (MPa)

    double max_von_mises_mpa = 0.0;
    double max_disp_mm = 0.0;
    double min_fos = 0.0;
    double max_principal_mpa = 0.0;   // max over nodes of principal1
    double min_principal_mpa = 0.0;   // min over nodes of principal3
    double max_shear_mpa = 0.0;       // max over nodes of max_shear

    // ── What the summary scalars above are a reduction OF ────────────────────
    // max_von_mises_mpa / min_fos / the principal+shear extrema are NOT the
    // extrema of the nodal arrays: stress at an ideal fully-fixed boundary is a
    // discretization singularity, so nodes inside two element rings of a fixed
    // node are excluded from the reduction (see fea_static.cpp). That exclusion
    // is a decision, not a measurement, so it is reported rather than implied:
    //   summary_node[i] == 1  -> node i fed the summary scalars
    //   summary_node[i] == 0  -> node i is inside the fixture ring
    // The array is length n_nodes and all-ones when the model has no fixed nodes
    // or when the ring would consume the whole mesh (the documented fallback).
    // A reader can therefore re-derive max_von_mises_mpa from von_mises and this
    // mask, byte for byte, and max_von_mises_raw_mpa is the same reduction with
    // no mask at all — the two together are the evidence.
    std::vector<std::uint8_t> summary_node;
    double max_von_mises_raw_mpa = 0.0;  // max over ALL nodes of von_mises, MPa
    double min_fos_raw = 0.0;            // yield / max_von_mises_raw_mpa (clamped 0..15)
    int summary_excluded_nodes = 0;      // count of summary_node[i] == 0
    int max_von_mises_raw_node = -1;     // WHERE the unmasked peak is (node index)
    int n_dof = 0;
    int n_nodes = 0;
    int n_elems = 0;

    // Support reactions (the equilibrium half of a run's evidence packet).
    // f_int = K u is re-formed on the elements that touch a constrained node, so
    // reaction_force_n is the resultant the supports carry, applied_force_n the
    // resultant the loads impose, the moments are taken about the global origin
    // (N·mm, coordinates being mm), and equilibrium_error_pct is the residual
    // |R+F| / max(|F|, 1e-9) * 100 — zero for an exactly solved linear system,
    // so a non-trivial value is a solver/conditioning warning, not physics.
    std::array<double, 3> applied_force_n{0.0, 0.0, 0.0};
    std::array<double, 3> reaction_force_n{0.0, 0.0, 0.0};
    std::array<double, 3> applied_moment_nmm{0.0, 0.0, 0.0};
    std::array<double, 3> reaction_moment_nmm{0.0, 0.0, 0.0};
    double equilibrium_error_pct = 0.0;
    bool ok = false;
    std::string error;  // non-empty when ok==false

    // Which factorization actually produced this answer — "CholmodSupernodalLLT"
    // normally, "SimplicialLDLT(AMD)" in a build without SuiteSparse or after the
    // supernodal LLT refused the matrix, "SparseLU(COLAMD)" when only partial
    // pivoting could carry it; CHUDWARE_FEA_SOLVER forces a specific one. fea_run
    // surfaces it as provenance.solver and as a solver_notes line, so a result
    // always says how it was computed.
    std::string solver;
    // Milliseconds this solve spent queued behind another heavy solve on the
    // process-wide solve gate (0 = it started immediately). Reported so a slow run
    // that was merely waiting is not mistaken for a slow solve.
    double solve_wait_ms = 0.0;
    // Non-empty when the strict Cholesky rejected the reduced stiffness as NOT
    // positive definite. That matrix is SPD by construction for a well-posed
    // linear-elastic model (a sum of PSD element matrices with the Dirichlet rows
    // eliminated), so a rejection is a statement about the MODEL: it carries a
    // (near-)zero-stiffness mode, and these fields are not trustworthy even though a
    // fallback factorization returned finite numbers and the equilibrium residual
    // came out small. Surfaced as a solver_notes line; ok stays true, because the
    // field is still the most useful thing to look at while diagnosing the mesh.
    std::string conditioning_warning;
    // Nodes eliminated because no element, bond tie or contact pair references them:
    // their rows in the reduced system are exactly zero, so keeping them free makes
    // the matrix singular however it is factored. Eliminating them is exact — nothing
    // couples them — and normally this is 0; a mesh repaired by dissolving hanging
    // elements can leave a few behind, because node numbering has to stay 1:1 with
    // the mesher's own mesh. A LOAD on such a node refuses instead of being dropped.
    int unreferenced_nodes_pinned = 0;

    // Prepared-system (factor) cache observability. factor_cache_hit means this solve
    // reused the reduced DOF map + assembled K + numeric factorization and only
    // re-solved the RHS (load-only repeat). Always false on the tet4 parity path.
    bool factor_cache_hit = false;
};

// Factor-cache counters (tet10/hex8 prepared-system reuse). Thread-safe.
struct FeaFactorCacheStats {
    uint64_t hits = 0;
    uint64_t misses = 0;
    uint64_t stores = 0;
    uint64_t evictions = 0;
    uint64_t resident_bytes = 0;
};
FeaFactorCacheStats fea_factor_cache_stats();
void fea_factor_cache_reset_stats();
void fea_factor_cache_clear();

// Solve a linear-static problem on a tet OR hex mesh (4-, 10-, or 8-node elements).
//   points         : n*3 flat node coordinates (mm)
//   tets           : m*K flat node indices (0-based), K = nodes_per_elem
//   E_mpa, nu      : isotropic material (Young's modulus MPa, Poisson ratio)
//   yield_mpa      : yield strength for the factor-of-safety summary
//   fixed_nodes    : node indices held at zero displacement (all 3 DOFs)
//   nodal_forces   : n*3 flat applied nodal force vector (N); same length as points
//   nodes_per_elem : 4 (tet4, parity path), 10 (tet10), or 8 (hex8, structured grid)
//
// Penalty tie between two mesh nodes (asm-fea bonded contact across a small assembly
// gap): couples all 3 DOFs of node a to node b with stiffness k (N/mm per DOF). Springs
// to a FIXED node ground the free side (correct penalty behaviour). Used by fea_run to
// bond disconnected mesh components (an imported assembly whose parts have design
// clearances) so the load path crosses the joints instead of the system going singular.
struct BondSpring {
    int a = 0;
    int b = 0;
    double k = 0.0;
};

// ── Unilateral (compression-only) frictionless contact ───────────────────────────
// A ContactSpring is a candidate contact pair between two SURFACE nodes on different
// mesh components. Unlike BondSpring (which glues all 3 DOFs unconditionally, in
// tension as well as compression) it acts along ONE direction and only while the pair
// is pressed together:
//
//   gap(u) = gap0_mm + normal · (u_b - u_a)      (>0 separated, <0 penetrating)
//
// While the pair is ACTIVE the penalty energy is 0.5·k·gap², whose stiffness is
// k·(n nᵀ) on the (a,b) block and whose constant part k·gap0 enters the right-hand
// side. The active-set loop (fea_static_solve_contact) deactivates any pair whose
// converged normal force turns TENSILE (gap > 0, i.e. the surfaces have separated) and
// re-activates any pair whose gap has closed, so no pair ever pulls its partner back.
//
// A frictionless normal-only pair leaves the tangential rigid-body modes of a seated
// part free (a box on a box can slide sideways under a purely vertical load), which is
// physically true and numerically singular. Active pairs therefore also carry a small
// tangential stiffness k·tangential_stabilization·(I - n nᵀ). It is a numerical
// stabilizer, NOT friction: fea_static_solve_contact reports the fraction of the
// contact resultant it carries so a run where it became load-bearing is visible
// instead of silently reported as a frictionless answer.
struct ContactSpring {
    int a = 0;                              // surface node on body A
    int b = 0;                              // surface node on body B
    double k = 0.0;                         // normal penalty stiffness, N/mm
    double gap0_mm = 0.0;                   // initial signed gap along `normal`, mm
    std::array<double, 3> normal{0.0, 0.0, 1.0};  // unit, outward from A toward B
    double area_mm2 = 0.0;                  // tributary surface area of node a, mm^2
    int group = 0;                          // body-pair id for aggregate reporting
};

// Per-body-pair contact aggregate. force_n is the resultant body A pushes onto body B
// (compression only, so it always opposes penetration); contact_force_n is its
// magnitude. max_contact_pressure_mpa is the worst per-pair normal force divided by
// that pair's tributary node area — it is 0.0 when no active pair carried an area.
struct FeaContactGroup {
    int group = 0;
    int candidate_pairs = 0;
    int active_pairs = 0;
    std::array<double, 3> force_n{0.0, 0.0, 0.0};
    double contact_force_n = 0.0;
    double max_contact_pressure_mpa = 0.0;
    double max_penetration_mm = 0.0;
};

struct FeaContactReport {
    int candidate_pairs = 0;
    int active_pairs = 0;
    int contact_iterations = 0;
    // false = the active set was still changing when max_iterations ran out. The
    // displacement/stress fields are then the LAST iterate, not a converged solution.
    bool converged = false;
    double tangential_stabilization = 0.0;
    // |tangential resultant| / |normal resultant| over the active set. Near zero means
    // the stabilizer only removed the rigid-body nullspace; a large value means the
    // model needs a real tangential fixture.
    double tangential_force_fraction = 0.0;
    std::vector<FeaContactGroup> groups;
};

struct FeaContactOptions {
    int max_iterations = 25;
    double tangential_stabilization = 1e-4;
};

// nodes_per_elem==4 uses the analytical tet4 strain-displacement operator UNCHANGED
// (byte-for-byte parity with the prior solver / the Python oracle). ==10 uses an
// isoparametric quadratic tet (4-point Gauss); ==8 uses an isoparametric trilinear
// hexahedron (2x2x2 Gauss) for structured/symmetric meshes of prismatic parts. Each is a
// SEPARATE code path; default 4 keeps every existing caller on the parity path.
// bond_springs (default empty = every existing caller unchanged, parity preserved)
// appends penalty-tie stiffness AFTER element assembly.
// Never throws: a degenerate/unsolvable model returns ok=false with an error string.
FeaStaticResult fea_static_solve(const std::vector<double>& points,
                                 const std::vector<int>& tets, double E_mpa, double nu,
                                 double yield_mpa, const std::vector<int>& fixed_nodes,
                                 const std::vector<double>& nodal_forces,
                                 int nodes_per_elem = 4,
                                 const std::vector<BondSpring>& bond_springs = {},
                                 const SimCancelToken& cancel_token = {});

// Same linear-elastic kernel, plus an ACTIVE-SET loop over `contact_springs`:
//
//   1. every candidate pair starts ACTIVE (a seated-but-not-yet-touching part would
//      otherwise be a mechanism on the first solve, with no stiffness to find),
//   2. solve, then evaluate gap(u) per pair,
//   3. deactivate pairs that separated (gap > 0 — the spring would be pulling),
//      activate pairs whose gap closed (gap < 0),
//   4. repeat until the active set repeats itself or options.max_iterations is spent.
//
// Elements are assembled ONCE: an iteration only re-appends the spring triplets to the
// cached element triplet block and re-factors. Every iteration re-checks that each mesh
// component still reaches a fixture through elements, bond ties, or ACTIVE contact; a
// part left floating because its contact fully separated returns ok=false naming that,
// never a garbage rigid-body solve. The prepared-system/factor cache is bypassed (K
// changes every iteration by construction).
//
// `report` is always written, including on failure, so a refusal still shows how many
// candidates there were and how many iterations ran.
FeaStaticResult fea_static_solve_contact(
    const std::vector<double>& points, const std::vector<int>& tets, double E_mpa,
    double nu, double yield_mpa, const std::vector<int>& fixed_nodes,
    const std::vector<double>& nodal_forces, int nodes_per_elem,
    const std::vector<BondSpring>& bond_springs,
    const std::vector<ContactSpring>& contact_springs, const FeaContactOptions& options,
    FeaContactReport& report, const SimCancelToken& cancel_token = {});

}  // namespace chudware::sim
