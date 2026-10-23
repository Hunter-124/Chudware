// Linear-static FEA on tet4, tet10 and hex8 meshes.
//
// The default sparse direct solver follows the CHOLMOD/LDLT/LU ladder. Explicit
// CHUDWARE_FEA_SOLVER=iccg selects bounded, incomplete-Cholesky preconditioned CG
// for linear elasticity and frictionless unilateral penalty contact, with no
// direct fallback. Assembly, material laws and stress/reaction recovery are shared.
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

// A compact material table: one index per element, never a copy of its constants.
// An empty table selects the scalar solver arguments and requires an empty index map.
// Otherwise elem_material must contain exactly one valid index per mesh element.
struct FeaMaterial {
    double E_mpa;
    double nu;
    double yield_mpa;
};

struct FeaMaterials {
    std::vector<FeaMaterial> materials;
    std::vector<uint16_t> elem_material;
};

// Explicit opt-in only; direct solver selection/defaults are unchanged.
// ICCG supports linear elasticity (including unilateral penalty contact), not plasticity.
// True relative residual tolerance is 1e-10 (never degraded). The per-linear-solve
// budget defaults to 10000 iterations; CHUDWARE_FEA_ICCG_MAX_ITERATIONS accepts
// 1..1000000. Reports always contain the actual limit and all active-set solves.
bool fea_static_uses_iccg();

struct FeaLinearSolveStep {
    int iterations = 0;
    double relative_residual = 0.0;  // independently evaluated ||K*u-rhs|| / ||rhs||
    bool converged = false;
};

struct FeaIterativeReport {
    double tolerance = 1e-10;
    int iteration_limit = 0;  // per linear solve, not per contact study
    uint64_t total_iterations = 0;
    // One entry per attempted linear solve, in active-set order for contact.
    std::vector<FeaLinearSolveStep> solves;
    uint64_t projected_peak_bytes = 0;  // latest admission's storage bound, NOT measured peak
    uint64_t maximum_projected_peak_bytes = 0;  // largest bound across admission stages
    uint64_t resident_at_admission_bytes = 0;  // measured process RSS, includes mesh
    uint64_t free_dofs = 0;
    uint64_t stored_nonzeros = 0;
    std::string admission_stage;
};

struct FeaStaticResult {
    std::vector<double> displacement;    // n*3, row-major (node i -> [3i,3i+1,3i+2]) mm
    std::vector<double> disp_magnitude;  // n,  ||u|| per node, mm
    std::vector<double> von_mises;       // n,  node-averaged von Mises stress, MPa
    // Scalar mode: yield / node-averaged von Mises. Table mode: minimum incident
    // element factor of safety. Values are clamped to [0,15]; zero stress gives 15.
    std::vector<double> factor_of_safety;
    // Peak recovered nodal von Mises within each element, before inter-element
    // averaging, and that element's own yield divided by this peak (clamped [0,15]).
    std::vector<double> element_von_mises;
    std::vector<double> element_factor_of_safety;

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
    // In material-table mode min_fos instead reduces element_factor_of_safety over
    // elements incident to any selected node; min_fos_raw reduces all elements.
    std::vector<std::uint8_t> summary_node;
    double max_von_mises_raw_mpa = 0.0;  // max over ALL nodes of von_mises, MPa
    double min_fos_raw = 0.0;            // unmasked minimum factor of safety (clamped 0..15)
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
    FeaIterativeReport iterative;
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
    // ICCG also reports that convergence cannot certify SPD or rule out unexcited
    // rigid modes; it never pretends a shifted incomplete factor is an SPD test.
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
    // re-solved the RHS (load-only repeat). Always false for tet4 and ICCG.
    bool factor_cache_hit = false;

    // ── Elastic-plastic state (empty unless FeaPlasticity::enabled) ──────────
    // Equivalent (accumulated) plastic strain per node, dimensionless: the node
    // average of the Gauss-point state variable the radial return integrates. 0
    // everywhere the material stayed elastic, and the field the VTU exports as
    // `equivalent_plastic_strain`. The peak reported in FeaPlasticReport is taken
    // over GAUSS POINTS, not over this averaged field, so it is the real maximum
    // of the state variable rather than a smoothed one.
    std::vector<double> equivalent_plastic_strain;
};

// Shared evidence serialization for both native static result entry points.
nlohmann::json fea_iterative_report_json(const FeaStaticResult& result);

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
// side. The active-set loop releases tensile pairs and re-seats penetrating pairs
// with mesh/load-scaled hysteresis (FeaContactReport): convergence requires normal
// force >= -tol_f on active pairs and gap >= -tol_g on inactive pairs.
// Where that loop STARTS is FeaContactOptions::seat_tolerance_mm: only pairs the mesh
// cannot tell from touching begin active, so the first iterate is the applied load
// acting on the seated arc instead of thousands of open pairs being dragged shut.
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
// contact_area_mm2 is the summed tributary area of the ACTIVE pairs: the contact
// patch this resultant is spread over, so contact_force_n / contact_area_mm2 is a
// mean contact pressure. It is 0.0 when nothing is pressed together, and a patch of
// one or two pairs is a point load rather than a pressure — see ContactResolution in
// fea_contact.h, which is what decides whether that division means anything.
struct FeaContactGroup {
    int group = 0;
    int candidate_pairs = 0;
    int active_pairs = 0;
    std::array<double, 3> force_n{0.0, 0.0, 0.0};
    double contact_force_n = 0.0;
    double contact_area_mm2 = 0.0;
    double max_contact_pressure_mpa = 0.0;
    double max_penetration_mm = 0.0;
};

struct FeaContactReport {
    int candidate_pairs = 0;
    int active_pairs = 0;
    int contact_iterations = 0;
    int cycles_broken = 0;
    double hysteresis_tol_g_mm = 0.0;
    double hysteresis_tol_f_n = 0.0;
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
    // Where the active set STARTS: the widest initial gap (mm) a candidate may have
    // and still begin the loop ACTIVE. Negative (the default) means the loop has no
    // seat rule of its own and every candidate starts active.
    //
    // It matters because an active pair whose gap0 > 0 carries k·gap0 of TENSION on
    // the first right-hand side — that is the mechanism by which a part held only by
    // contact travels into its seat, and it is also how a wide candidate set
    // destroys a solve: on the live keyway all 11989 candidates (gaps 0 … 0.4 mm)
    // started active, the first iterate was dominated by thousands of pairs being
    // dragged shut rather than by the 1500 N load, and the surviving active set was
    // round-off luck — 8, then 12, then 13, then 2 pairs across identical runs.
    // fea_run passes ContactCandidateSet::seat_limit_mm here (fea_contact.h), which
    // is the gap the MESH cannot tell from touching.
    double seat_tolerance_mm = -1.0;
    // Measured candidate-interface element size; zero uses the median sqrt of
    // positive pair tributary areas for callers supplying springs without a mesh probe.
    double interface_h_mm = 0.0;
};

// ── Small-strain J2 (von Mises) plasticity with isotropic hardening ─────────────
// A frictionless linear-elastic contact cannot redistribute: on a rover hinge
// saddle 1446 N funnelled through 12 node pairs on a sharp edge and the mean
// bearing stress came out at 450 MPa against a 276 MPa yield, which is not a
// result — it is the answer to a question about a material that does not exist.
// An elastic-plastic law lets the edge yield, so the bearing patch GROWS until the
// load fits inside the flow stress, which is what the steel actually does.
//
// The law is the standard one: von Mises yield surface, associated flow, linear
// isotropic hardening
//
//   sigma_y(eq_plastic_strain) = yield_mpa + hardening_mpa * eq_plastic_strain
//
// integrated by radial return at every Gauss point with the consistent (algorithmic)
// tangent, under an incremental Newton-Raphson load-stepping loop. hardening_mpa is
// a TANGENT MODULUS in MPa (0 = perfectly plastic, i.e. a flat yield plateau); it is
// H in the bilinear law, not the ratio H/E.
//
// Validity: SMALL strain. The mesh is never updated, so the answer is a
// small-strain plasticity answer; past a few percent of plastic strain the geometry
// change matters and fea_run emits a warning saying so.
struct FeaPlasticity {
    bool enabled = false;
    // Initial yield stress (MPa). 0 means "use the study's yield_mpa". This scalar
    // override is ignored when FeaMaterials supplies per-element yield strengths.
    double yield_mpa = 0.0;
    // Linear isotropic hardening tangent modulus H (MPa). 0 = perfectly plastic.
    double hardening_mpa = 0.0;
    // Equal load increments the full load is applied in. A non-converging increment
    // is retried at half the step (down to 1/32 of the requested increment), so this
    // is the starting ladder, not a hard schedule.
    int max_increments = 10;
    // Newton convergence tolerance on ||residual|| / ||applied load|| (relative).
    double tol = 1e-3;
};

// What the plastic integration did, as evidence rather than as a footnote.
struct FeaPlasticReport {
    bool enabled = false;
    // Load increments that CONVERGED (adaptive substeps included, so this can
    // exceed FeaPlasticity::max_increments).
    int increments = 0;
    int newton_iterations_total = 0;
    // true only when the FULL applied load was reached with every increment
    // converged. false means the structure stopped carrying load at
    // `load_fraction` — a collapse mechanism, not a solver preference.
    bool converged = false;
    // Fraction of the applied load the last converged increment carried (1.0 on a
    // converged run). Without it a partial result is uninterpretable, so it is
    // reported even on the refusal.
    double load_fraction = 0.0;
    double max_equivalent_plastic_strain = 0.0;
    // Integration volume whose Gauss point has yielded (mm^3), and its share of the
    // model volume.
    double plastic_volume_mm3 = 0.0;
    double plastic_fraction = 0.0;
    int yielded_nodes = 0;
};

// nodes_per_elem==4 uses the analytical tet4 strain-displacement operator UNCHANGED
// (byte-for-byte parity with the prior solver / the Python oracle). ==10 uses an
// isoparametric quadratic tet (4-point Gauss); ==8 uses an isoparametric trilinear
// hexahedron (2x2x2 Gauss) for structured/symmetric meshes of prismatic parts. Each is a
// SEPARATE code path; default 4 keeps every existing caller on the parity path.
// bond_springs (default empty = every existing caller unchanged, parity preserved)
// appends penalty-tie stiffness AFTER element assembly.
// materials overrides the scalar E_mpa/nu/yield_mpa when its table is non-empty.
// All table entries require finite E>0, -1<nu<0.5 and yield>0; at most 65536 entries.
// Heterogeneous solves bypass the prepared-system cache.
// Never throws: a degenerate/unsolvable model returns ok=false with an error string.
FeaStaticResult fea_static_solve(const std::vector<double>& points,
                                 const std::vector<int>& tets, double E_mpa, double nu,
                                 double yield_mpa, const std::vector<int>& fixed_nodes,
                                 const std::vector<double>& nodal_forces,
                                 int nodes_per_elem = 4,
                                 const std::vector<BondSpring>& bond_springs = {},
                                 const SimCancelToken& cancel_token = {},
                                 const FeaMaterials& materials = {});

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
    FeaContactReport& report, const SimCancelToken& cancel_token = {},
    const FeaMaterials& materials = {});

// Same kernel with the J2 material law switched on: radial return at every Gauss
// point, a consistent tangent, and an incremental Newton-Raphson load-stepping loop
// whose every tangent solve goes down the SAME CHOLMOD/LDLT ladder (and through the
// same process-wide solve gate) as a linear solve.
//
// `contact_springs` may be empty (plastic statics) or not (plastic contact), and
// when it is not, the active-set loop runs INSIDE each load increment: contact state
// and plastic state are both updated per increment, which is the only ordering that
// can grow a bearing patch as the edge under it yields.
//
// An increment that will not converge is retried at half the load step; when even
// the smallest step fails the result is a REFUSAL naming the load fraction reached
// (`plastic_report.load_fraction`), never a last-iterate stress field. Both reports
// are always written, including on failure.
FeaStaticResult fea_static_solve_plastic(
    const std::vector<double>& points, const std::vector<int>& tets, double E_mpa,
    double nu, double yield_mpa, const std::vector<int>& fixed_nodes,
    const std::vector<double>& nodal_forces, int nodes_per_elem,
    const std::vector<BondSpring>& bond_springs,
    const std::vector<ContactSpring>& contact_springs, const FeaContactOptions& options,
    const FeaPlasticity& plasticity, FeaContactReport& contact_report,
    FeaPlasticReport& plastic_report, const SimCancelToken& cancel_token = {},
    const FeaMaterials& materials = {});

}  // namespace chudware::sim
