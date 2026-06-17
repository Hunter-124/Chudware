// Linear-static FEA solver implementation — see fea_static.h.
//
// Direct port of engine/sim/fea_static.py's numerical core. The reduced free-free
// system is assembled directly (fixed DOFs mapped out during assembly) so no sparse
// slicing is needed, then factored with Eigen::SparseLU. von Mises stress is recovered
// per element from the same strain-displacement operator and averaged onto nodes.

#include "sim/fea_static.h"

#include <algorithm>
#include <array>
#include <cmath>
#include <set>

#include <Eigen/Dense>
#include <Eigen/SparseCore>
#include <Eigen/SparseLU>

namespace aicad::sim {

namespace {

// Lamé parameters (MPa) from E, nu (fea_static.py::_lame_from_material).
void lame(double E, double nu, double& lam, double& mu) {
    lam = E * nu / ((1.0 + nu) * (1.0 - 2.0 * nu));
    mu = E / (2.0 * (1.0 + nu));
}

// 6x6 Voigt isotropic elasticity matrix (_build_elasticity_matrix).
Eigen::Matrix<double, 6, 6> elasticity(double lam, double mu) {
    Eigen::Matrix<double, 6, 6> C = Eigen::Matrix<double, 6, 6>::Zero();
    C(0, 0) = C(1, 1) = C(2, 2) = lam + 2 * mu;
    C(0, 1) = C(0, 2) = C(1, 0) = C(1, 2) = C(2, 0) = C(2, 1) = lam;
    C(3, 3) = C(4, 4) = C(5, 5) = mu;
    return C;
}

// One tet4's strain-displacement matrix B (6x12) and volume (_tet4_stiffness_and_B).
// Returns false (vol=0) for a degenerate element.
bool tet4_B(const std::array<Eigen::Vector3d, 4>& ep, Eigen::Matrix<double, 6, 12>& B,
            double& vol) {
    Eigen::Matrix4d T;
    for (int j = 0; j < 4; ++j) {
        T(j, 0) = 1.0;
        T(j, 1) = ep[j].x();
        T(j, 2) = ep[j].y();
        T(j, 3) = ep[j].z();
    }
    double det = T.determinant();
    vol = std::abs(det) / 6.0;
    if (vol <= 0.0) return false;
    Eigen::Matrix4d Tinv = T.inverse();
    if (!Tinv.allFinite()) { vol = 0.0; return false; }
    // dN[i,j] = d(N_j)/d(x_i) = rows 1..3 of Tinv.
    Eigen::Matrix<double, 3, 4> dN = Tinv.block<3, 4>(1, 0);
    B.setZero();
    for (int j = 0; j < 4; ++j) {
        int col = j * 3;
        B(0, col + 0) = dN(0, j);
        B(1, col + 1) = dN(1, j);
        B(2, col + 2) = dN(2, j);
        B(3, col + 0) = dN(1, j); B(3, col + 1) = dN(0, j);
        B(4, col + 1) = dN(2, j); B(4, col + 2) = dN(1, j);
        B(5, col + 0) = dN(2, j); B(5, col + 2) = dN(0, j);
    }
    return true;
}

}  // namespace

FeaStaticResult fea_static_solve(const std::vector<double>& points, const std::vector<int>& tets,
                                 double E_mpa, double nu, double yield_mpa,
                                 const std::vector<int>& fixed_nodes,
                                 const std::vector<double>& nodal_forces) {
    FeaStaticResult res;
    const int n_nodes = static_cast<int>(points.size() / 3);
    const int n_dof = n_nodes * 3;
    res.n_dof = n_dof;
    if (n_nodes == 0 || tets.empty()) {
        res.error = "fea_static: empty mesh";
        return res;
    }
    if (static_cast<int>(nodal_forces.size()) != n_dof) {
        res.error = "fea_static: nodal_forces length must be n_nodes*3";
        return res;
    }

    double lam, mu;
    lame(E_mpa, nu, lam, mu);
    const Eigen::Matrix<double, 6, 6> C = elasticity(lam, mu);

    auto node_pt = [&](int nid) {
        return Eigen::Vector3d(points[nid * 3], points[nid * 3 + 1], points[nid * 3 + 2]);
    };
    const int n_tets = static_cast<int>(tets.size() / 4);

    // Fixed-DOF mask + reduced (free) DOF numbering: dof -> reduced index, or -1 if fixed.
    std::vector<char> fixed(n_dof, 0);
    for (int nid : fixed_nodes)
        if (nid >= 0 && nid < n_nodes)
            for (int k = 0; k < 3; ++k) fixed[nid * 3 + k] = 1;
    std::vector<int> reduced(n_dof, -1);
    int n_free = 0;
    for (int d = 0; d < n_dof; ++d)
        if (!fixed[d]) reduced[d] = n_free++;
    if (n_free == 0) {
        res.error = "fea_static: all DOFs constrained";
        return res;
    }

    // Assemble the reduced free-free stiffness directly (skip rows/cols of fixed DOFs).
    std::vector<Eigen::Triplet<double>> trips;
    trips.reserve(static_cast<std::size_t>(n_tets) * 144);
    for (int e = 0; e < n_tets; ++e) {
        std::array<Eigen::Vector3d, 4> ep;
        std::array<int, 4> tn;
        for (int j = 0; j < 4; ++j) { tn[j] = tets[e * 4 + j]; ep[j] = node_pt(tn[j]); }
        Eigen::Matrix<double, 6, 12> B;
        double vol;
        if (!tet4_B(ep, B, vol)) continue;
        Eigen::Matrix<double, 12, 12> Ke = B.transpose() * C * B * vol;
        int dof_map[12];
        for (int j = 0; j < 4; ++j)
            for (int k = 0; k < 3; ++k) dof_map[j * 3 + k] = tn[j] * 3 + k;
        for (int a = 0; a < 12; ++a) {
            int ra = reduced[dof_map[a]];
            if (ra < 0) continue;
            for (int b = 0; b < 12; ++b) {
                int rb = reduced[dof_map[b]];
                if (rb < 0) continue;
                trips.emplace_back(ra, rb, Ke(a, b));
            }
        }
    }

    Eigen::SparseMatrix<double> Kff(n_free, n_free);
    Kff.setFromTriplets(trips.begin(), trips.end());
    Kff.makeCompressed();

    Eigen::VectorXd ff(n_free);
    for (int d = 0; d < n_dof; ++d)
        if (reduced[d] >= 0) ff(reduced[d]) = nodal_forces[d];

    Eigen::SparseLU<Eigen::SparseMatrix<double>> lu;
    lu.analyzePattern(Kff);
    lu.factorize(Kff);
    if (lu.info() != Eigen::Success) {
        res.error = "fea_static: sparse factorization failed (singular system)";
        return res;
    }
    Eigen::VectorXd uf = lu.solve(ff);
    if (lu.info() != Eigen::Success) {
        res.error = "fea_static: sparse solve failed";
        return res;
    }

    // Scatter back to the full DOF vector (fixed DOFs stay zero).
    std::vector<double> u_full(n_dof, 0.0);
    for (int d = 0; d < n_dof; ++d)
        if (reduced[d] >= 0) u_full[d] = uf(reduced[d]);

    // Displacement + magnitude.
    res.displacement = u_full;
    res.disp_magnitude.assign(n_nodes, 0.0);
    for (int i = 0; i < n_nodes; ++i) {
        double x = u_full[i * 3], y = u_full[i * 3 + 1], z = u_full[i * 3 + 2];
        res.disp_magnitude[i] = std::sqrt(x * x + y * y + z * z);
    }

    // Per-element von Mises -> node-averaged (mean of neighbouring elements).
    std::vector<double> vm_accum(n_nodes, 0.0);
    std::vector<int> vm_count(n_nodes, 0);
    for (int e = 0; e < n_tets; ++e) {
        std::array<Eigen::Vector3d, 4> ep;
        std::array<int, 4> tn;
        for (int j = 0; j < 4; ++j) { tn[j] = tets[e * 4 + j]; ep[j] = node_pt(tn[j]); }
        Eigen::Matrix<double, 6, 12> B;
        double vol;
        if (!tet4_B(ep, B, vol)) continue;
        Eigen::Matrix<double, 12, 1> ue;
        for (int j = 0; j < 4; ++j)
            for (int k = 0; k < 3; ++k) ue(j * 3 + k) = u_full[tn[j] * 3 + k];
        Eigen::Matrix<double, 6, 1> strain = B * ue;
        Eigen::Matrix<double, 6, 1> stress = C * strain;
        double sx = stress(0), sy = stress(1), sz = stress(2);
        double txy = stress(3), tyz = stress(4), txz = stress(5);
        double vm = std::sqrt(0.5 * ((sx - sy) * (sx - sy) + (sy - sz) * (sy - sz) +
                                     (sz - sx) * (sz - sx) +
                                     6.0 * (txy * txy + tyz * tyz + txz * txz)));
        for (int j = 0; j < 4; ++j) { vm_accum[tn[j]] += vm; vm_count[tn[j]] += 1; }
    }
    res.von_mises.assign(n_nodes, 0.0);
    for (int i = 0; i < n_nodes; ++i)
        if (vm_count[i] > 0) res.von_mises[i] = vm_accum[i] / vm_count[i];

    // Summary (max von Mises, max disp, min factor of safety clipped to [0,15]).
    double max_vm = 0.0, max_disp = 0.0;
    for (double v : res.von_mises) max_vm = std::max(max_vm, v);
    for (double v : res.disp_magnitude) max_disp = std::max(max_disp, v);
    double min_fos = 15.0;
    bool any = false;
    for (double v : res.von_mises)
        if (v > 0.0) {
            double fos = yield_mpa / v;
            if (!any || fos < min_fos) min_fos = fos;
            any = true;
        }
    if (!any) min_fos = 15.0;
    min_fos = std::min(std::max(min_fos, 0.0), 15.0);

    res.max_von_mises_mpa = max_vm;
    res.max_disp_mm = max_disp;
    res.min_fos = min_fos;
    res.ok = true;
    return res;
}

}  // namespace aicad::sim
