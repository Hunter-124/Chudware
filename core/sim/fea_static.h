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

}  // namespace chudware::sim
