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

#include <cstdint>
#include <string>
#include <vector>

namespace aicad::sim {

struct FeaStaticResult {
    std::vector<double> displacement;    // n*3, row-major (node i -> [3i,3i+1,3i+2]) mm
    std::vector<double> disp_magnitude;  // n,  ||u|| per node, mm
    std::vector<double> von_mises;       // n,  node-averaged von Mises stress, MPa
    double max_von_mises_mpa = 0.0;
    double max_disp_mm = 0.0;
    double min_fos = 0.0;
    int n_dof = 0;
    bool ok = false;
    std::string error;  // non-empty when ok==false
};

// Solve a linear-static problem on a tet4 mesh.
//   points       : n*3 flat node coordinates (mm)
//   tets         : m*4 flat node indices (0-based)
//   E_mpa, nu    : isotropic material (Young's modulus MPa, Poisson ratio)
//   yield_mpa    : yield strength for the factor-of-safety summary
//   fixed_nodes  : node indices held at zero displacement (all 3 DOFs)
//   nodal_forces : n*3 flat applied nodal force vector (N); same length as points
// Never throws: a degenerate/unsolvable model returns ok=false with an error string.
FeaStaticResult fea_static_solve(const std::vector<double>& points,
                                 const std::vector<int>& tets, double E_mpa, double nu,
                                 double yield_mpa, const std::vector<int>& fixed_nodes,
                                 const std::vector<double>& nodal_forces);

}  // namespace aicad::sim
