// Modal FEA on tet4 meshes — C++ port of engine/sim/fea_modal.py.
//
// The lowest n_modes natural frequencies + mode shapes of K phi = omega^2 M phi via
// shift-invert generalized eigensolution at sigma=0 (Spectra::SymGEigsShiftSolver in
// place of scipy.sparse.linalg.eigsh). K is the same tet4 stiffness as the static
// solver; M is the CONSISTENT tet4 mass matrix (rho*V/20 * (1+delta_ij) blocks) — it
// keeps a square section's first bending pair near-degenerate and removes the lumped-
// mass low bias (Bug N-1). Units: mm / MPa / g, so omega[rad/s] = sqrt(eigenvalue *
// 1e6), freq[Hz] = omega / 2pi.
#pragma once

#include <string>
#include <vector>

namespace aicad::sim {

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
// pin all 3 DOFs). Empty fixed_nodes => free-free analysis. Never throws.
FeaModalResult fea_modal_solve(const std::vector<double>& points, const std::vector<int>& tets,
                               double E_mpa, double nu, double density_g_cm3,
                               const std::vector<int>& fixed_nodes, int n_modes);

}  // namespace aicad::sim
