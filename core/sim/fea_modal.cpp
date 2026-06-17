// Modal FEA solver implementation — see fea_modal.h.
//
// Reuses the static solver's tet4 stiffness; adds the diagonal-lumped mass matrix and a
// shift-invert generalized eigensolve. The eigenvalue->frequency conversion + the
// unit-max mode-shape normalization mirror engine/sim/fea_modal.py exactly.

#include "sim/fea_modal.h"

#include <algorithm>
#include <array>
#include <cmath>

#include <Eigen/Dense>
#include <Eigen/SparseCore>

#include <Spectra/MatOp/SparseSymMatProd.h>
#include <Spectra/MatOp/SymShiftInvert.h>
#include <Spectra/SymGEigsShiftSolver.h>

namespace aicad::sim {

namespace {

constexpr double kPi = 3.14159265358979323846;

void lame(double E, double nu, double& lam, double& mu) {
    lam = E * nu / ((1.0 + nu) * (1.0 - 2.0 * nu));
    mu = E / (2.0 * (1.0 + nu));
}

Eigen::Matrix<double, 6, 6> elasticity(double lam, double mu) {
    Eigen::Matrix<double, 6, 6> C = Eigen::Matrix<double, 6, 6>::Zero();
    C(0, 0) = C(1, 1) = C(2, 2) = lam + 2 * mu;
    C(0, 1) = C(0, 2) = C(1, 0) = C(1, 2) = C(2, 0) = C(2, 1) = lam;
    C(3, 3) = C(4, 4) = C(5, 5) = mu;
    return C;
}

bool tet4_B(const std::array<Eigen::Vector3d, 4>& ep, Eigen::Matrix<double, 6, 12>& B,
            double& vol) {
    Eigen::Matrix4d T;
    for (int j = 0; j < 4; ++j) {
        T(j, 0) = 1.0; T(j, 1) = ep[j].x(); T(j, 2) = ep[j].y(); T(j, 3) = ep[j].z();
    }
    vol = std::abs(T.determinant()) / 6.0;
    if (vol <= 0.0) return false;
    Eigen::Matrix4d Tinv = T.inverse();
    if (!Tinv.allFinite()) { vol = 0.0; return false; }
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

FeaModalResult fea_modal_solve(const std::vector<double>& points, const std::vector<int>& tets,
                               double E_mpa, double nu, double density_g_cm3,
                               const std::vector<int>& fixed_nodes, int n_modes) {
    FeaModalResult res;
    const int n_nodes = static_cast<int>(points.size() / 3);
    const int n_dof = n_nodes * 3;
    res.n_nodes = n_nodes;
    res.n_dof = n_dof;
    if (n_nodes == 0 || tets.empty()) { res.error = "fea_modal: empty mesh"; return res; }

    double lam, mu;
    lame(E_mpa, nu, lam, mu);
    const Eigen::Matrix<double, 6, 6> C = elasticity(lam, mu);
    const double density = density_g_cm3 * 1e-3;  // g/cm^3 -> g/mm^3

    auto node_pt = [&](int nid) {
        return Eigen::Vector3d(points[nid * 3], points[nid * 3 + 1], points[nid * 3 + 2]);
    };
    const int n_tets = static_cast<int>(tets.size() / 4);

    std::vector<char> fixed(n_dof, 0);
    for (int nid : fixed_nodes)
        if (nid >= 0 && nid < n_nodes)
            for (int k = 0; k < 3; ++k) fixed[nid * 3 + k] = 1;
    std::vector<int> reduced(n_dof, -1);
    int n_free = 0;
    for (int d = 0; d < n_dof; ++d)
        if (!fixed[d]) reduced[d] = n_free++;
    if (n_free < 3) { res.error = "fea_modal: too few free DOFs"; return res; }

    // Reduced free-free K + diagonal-lumped M.
    std::vector<Eigen::Triplet<double>> trips;
    trips.reserve(static_cast<std::size_t>(n_tets) * 144);
    Eigen::VectorXd mdiag = Eigen::VectorXd::Zero(n_free);
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
        double m_per_node = density * vol / 4.0;
        for (int j = 0; j < 4; ++j)
            for (int k = 0; k < 3; ++k) {
                int rd = reduced[tn[j] * 3 + k];
                if (rd >= 0) mdiag(rd) += m_per_node;
            }
    }
    Eigen::SparseMatrix<double> Kff(n_free, n_free);
    Kff.setFromTriplets(trips.begin(), trips.end());
    Kff.makeCompressed();
    Eigen::SparseMatrix<double> Mff(n_free, n_free);
    {
        std::vector<Eigen::Triplet<double>> mt;
        mt.reserve(n_free);
        for (int i = 0; i < n_free; ++i) mt.emplace_back(i, i, mdiag(i));
        Mff.setFromTriplets(mt.begin(), mt.end());
        Mff.makeCompressed();
    }

    // Shift-invert generalized eigensolve at sigma=0 for the lowest modes.
    int k_req = std::min(n_modes, n_free - 2);
    if (k_req < 1) k_req = 1;
    int ncv = std::min(n_free, std::max(2 * k_req + 1, 20));

    using OpType = Spectra::SymShiftInvert<double, Eigen::Sparse, Eigen::Sparse>;
    using BOpType = Spectra::SparseSymMatProd<double>;
    OpType op(Kff, Mff);
    BOpType Bop(Mff);
    Spectra::SymGEigsShiftSolver<OpType, BOpType, Spectra::GEigsMode::ShiftInvert> geigs(
        op, Bop, k_req, ncv, 0.0);
    geigs.init();
    geigs.compute(Spectra::SortRule::LargestMagn, 10 * n_free, 1e-10, Spectra::SortRule::SmallestAlge);
    if (geigs.info() != Spectra::CompInfo::Successful) {
        res.error = "fea_modal: eigensolve did not converge";
        return res;
    }
    Eigen::VectorXd evals = geigs.eigenvalues();        // generalized eigenvalues (omega^2-ish)
    Eigen::MatrixXd evecs = geigs.eigenvectors();        // (n_free, k)
    const int k = static_cast<int>(evals.size());

    // omega[rad/s] = sqrt(max(eig,0) * 1e6); freq[Hz] = omega / 2pi. Sort ascending.
    std::vector<std::pair<double, int>> order;
    order.reserve(k);
    for (int i = 0; i < k; ++i) {
        double ev = std::max(evals(i), 0.0);
        double freq = std::sqrt(ev * 1e6) / (2.0 * kPi);
        order.emplace_back(freq, i);
    }
    std::sort(order.begin(), order.end(),
              [](const auto& a, const auto& b) { return a.first < b.first; });

    res.n_modes = k;
    res.freqs_hz.resize(k);
    res.mode_shapes.assign(static_cast<std::size_t>(k) * n_nodes * 3, 0.0);
    for (int m = 0; m < k; ++m) {
        res.freqs_hz[m] = order[m].first;
        int src = order[m].second;
        // Scatter the reduced eigenvector to full DOFs, normalize by unit max |u|.
        std::vector<double> u_full(n_dof, 0.0);
        double scale = 0.0;
        for (int d = 0; d < n_dof; ++d)
            if (reduced[d] >= 0) {
                double v = evecs(reduced[d], src);
                u_full[d] = v;
                scale = std::max(scale, std::abs(v));
            }
        double inv = scale > 0.0 ? 1.0 / scale : 1.0;
        for (int i = 0; i < n_nodes; ++i)
            for (int c = 0; c < 3; ++c)
                res.mode_shapes[(static_cast<std::size_t>(m) * n_nodes + i) * 3 + c] =
                    u_full[i * 3 + c] * inv;
    }
    res.ok = true;
    return res;
}

}  // namespace aicad::sim
