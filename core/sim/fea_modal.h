// Modal FEA on tet4 meshes — C++ port of engine/sim/fea_modal.py.
//
// The lowest n_modes natural frequencies + mode shapes of K phi = omega^2 M phi via
// shift-invert generalized eigensolution at sigma=0 (Spectra::SymGEigsShiftSolver in
// place of scipy.sparse.linalg.eigsh). Both tet4 and tet10 (2nd-order) elements are
// supported, each with the matching CONSISTENT mass matrix. tet10 is strongly preferred
// for slender/bending modes: linear tet4 is over-stiff and mesh-orientation-dependent,
// which on a square cross-section splits the (theoretically degenerate) first bending
// PAIR by ~10% and biases the frequency low on a coarse mesh (Bug N-1). Quadratic tet10
// captures bending far better, collapsing that split and removing the bias. Units: mm /
// MPa / g, so omega[rad/s] = sqrt(eigenvalue * 1e6), freq[Hz] = omega / 2pi.
#pragma once

#include <string>
#include <vector>

namespace chudware::sim {

struct FeaModalResult {
    std::vector<double> freqs_hz;     // n_modes, sorted ascending
    std::vector<double> mode_shapes;  // n_modes * n * 3, unit-max-normalized, row-major
    int n_modes = 0;
    int n_nodes = 0;
    int n_dof = 0;
    bool ok = false;
    std::string error;  // non-empty when ok==false
};

// Solve for the lowest n_modes free-vibration modes. BCs arrive resolved (fixed_nodes
// pin all 3 DOFs). Empty fixed_nodes => free-free analysis. `nodes_per_elem` selects the
// element type: 4 (tet4, the connectivity is 4 ints/elem) or 10 (tet10, 10 ints/elem).
// Never throws.
FeaModalResult fea_modal_solve(const std::vector<double>& points, const std::vector<int>& tets,
                               double E_mpa, double nu, double density_g_cm3,
                               const std::vector<int>& fixed_nodes, int n_modes,
                               int nodes_per_elem = 4);

}  // namespace chudware::sim
