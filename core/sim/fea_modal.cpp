// Modal FEA solver implementation — see fea_modal.h.
//
// Reuses the static solver's tet4 stiffness; adds the CONSISTENT tet4 mass matrix and a
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

// ── tet10 (quadratic) element helpers ───────────────────────────────────────────────
// Node ordering MIRRORS fea_static.cpp's validated tet10_dN exactly (corners 0-3, then
// edges {0,1},{1,2},{2,0},{3,0},{3,2},{3,1}) so the stiffness here matches the static
// solver and the consistent mass below uses the SAME shape functions.
Eigen::Matrix<double, 10, 3> tet10_dN(double r, double s, double t) {
    const double L0 = 1.0 - r - s - t, L1 = r, L2 = s, L3 = t;
    const Eigen::Vector3d g0(-1.0, -1.0, -1.0), g1(1.0, 0.0, 0.0),
        g2(0.0, 1.0, 0.0), g3(0.0, 0.0, 1.0);
    Eigen::Matrix<double, 10, 3> dN;
    dN.row(0) = (4.0 * L0 - 1.0) * g0.transpose();
    dN.row(1) = (4.0 * L1 - 1.0) * g1.transpose();
    dN.row(2) = (4.0 * L2 - 1.0) * g2.transpose();
    dN.row(3) = (4.0 * L3 - 1.0) * g3.transpose();
    dN.row(4) = (4.0 * (L0 * g1 + L1 * g0)).transpose();  // edge {0,1}
    dN.row(5) = (4.0 * (L1 * g2 + L2 * g1)).transpose();  // edge {1,2}
    dN.row(6) = (4.0 * (L2 * g0 + L0 * g2)).transpose();  // edge {2,0}
    dN.row(7) = (4.0 * (L3 * g0 + L0 * g3)).transpose();  // edge {3,0}
    dN.row(8) = (4.0 * (L3 * g2 + L2 * g3)).transpose();  // edge {3,2}
    dN.row(9) = (4.0 * (L3 * g1 + L1 * g3)).transpose();  // edge {3,1}
    return dN;
}

// 4-point degree-2 tet rule (exact for the tet10 stiffness integrand B^T C B).
struct TetGauss4 {
    double pt[4][3];
    double w[4];
    TetGauss4() {
        const double a = 0.5854101966249685, b = 0.1381966011250105;
        const double P[4][3] = {{a, b, b}, {b, a, b}, {b, b, a}, {b, b, b}};
        for (int g = 0; g < 4; ++g) {
            for (int k = 0; k < 3; ++k) pt[g][k] = P[g][k];
            w[g] = 1.0 / 24.0;
        }
    }
};

bool tet10_B_at(const std::array<Eigen::Vector3d, 10>& ep, double r, double s, double t,
                Eigen::Matrix<double, 6, 30>& B, double& detJ) {
    const Eigen::Matrix<double, 10, 3> dNrst = tet10_dN(r, s, t);
    Eigen::Matrix3d J = Eigen::Matrix3d::Zero();
    for (int n = 0; n < 10; ++n) J += ep[n] * dNrst.row(n);
    detJ = J.determinant();
    if (!(std::abs(detJ) > 0.0)) return false;
    Eigen::Matrix3d Jinv = J.inverse();
    if (!Jinv.allFinite()) return false;
    const Eigen::Matrix<double, 10, 3> dNxyz = dNrst * Jinv;
    B.setZero();
    for (int n = 0; n < 10; ++n) {
        const int col = n * 3;
        const double dx = dNxyz(n, 0), dy = dNxyz(n, 1), dz = dNxyz(n, 2);
        B(0, col + 0) = dx;
        B(1, col + 1) = dy;
        B(2, col + 2) = dz;
        B(3, col + 0) = dy; B(3, col + 1) = dx;
        B(4, col + 1) = dz; B(4, col + 2) = dy;
        B(5, col + 0) = dz; B(5, col + 2) = dx;
    }
    return true;
}

// Consistent tet10 mass, built ANALYTICALLY (exact for a straight-edged tet of volume V).
// Each shape function N_i is a polynomial in the barycentric coords (L0,L1,L2,L3); the
// element mass block M_ij = rho * integral(N_i N_j) over the tet. The exact monomial
// integral is  integral(L0^a L1^b L2^c L3^d) dV = V * 6 * a!b!c!d! / (a+b+c+d+3)!  — so we
// expand N_i N_j into monomials and sum. This avoids a hand-transcribed high-order tet
// quadrature rule and is exact (no integration error) when the element is straight-edged,
// which gmsh's tet10 are for the planar/analytic faces of CAD bodies.
Eigen::Matrix<double, 10, 10> tet10_mass_scalar(double rho, double vol) {
    struct Term { double c; int e[4]; };
    // N_i term tables — SAME node ordering as tet10_dN above.
    static const std::array<std::vector<Term>, 10> N = {{
        {{2.0, {2, 0, 0, 0}}, {-1.0, {1, 0, 0, 0}}},  // N0 = L0(2L0-1)
        {{2.0, {0, 2, 0, 0}}, {-1.0, {0, 1, 0, 0}}},  // N1 = L1(2L1-1)
        {{2.0, {0, 0, 2, 0}}, {-1.0, {0, 0, 1, 0}}},  // N2 = L2(2L2-1)
        {{2.0, {0, 0, 0, 2}}, {-1.0, {0, 0, 0, 1}}},  // N3 = L3(2L3-1)
        {{4.0, {1, 1, 0, 0}}},                        // N4 = 4 L0 L1
        {{4.0, {0, 1, 1, 0}}},                        // N5 = 4 L1 L2
        {{4.0, {1, 0, 1, 0}}},                        // N6 = 4 L2 L0
        {{4.0, {1, 0, 0, 1}}},                        // N7 = 4 L3 L0
        {{4.0, {0, 0, 1, 1}}},                        // N8 = 4 L3 L2
        {{4.0, {0, 1, 0, 1}}},                        // N9 = 4 L3 L1
    }};
    auto fact = [](int n) { double f = 1.0; for (int i = 2; i <= n; ++i) f *= i; return f; };
    auto moment = [&](int a, int b, int c, int d) {  // integral / V
        return 6.0 * fact(a) * fact(b) * fact(c) * fact(d) / fact(a + b + c + d + 3);
    };
    Eigen::Matrix<double, 10, 10> M;
    for (int i = 0; i < 10; ++i)
        for (int j = 0; j < 10; ++j) {
            double s = 0.0;
            for (const Term& ti : N[i])
                for (const Term& tj : N[j])
                    s += ti.c * tj.c * moment(ti.e[0] + tj.e[0], ti.e[1] + tj.e[1],
                                              ti.e[2] + tj.e[2], ti.e[3] + tj.e[3]);
            M(i, j) = rho * vol * s;
        }
    return M;
}

}  // namespace

FeaModalResult fea_modal_solve(const std::vector<double>& points, const std::vector<int>& tets,
                               double E_mpa, double nu, double density_g_cm3,
                               const std::vector<int>& fixed_nodes, int n_modes,
                               int nodes_per_elem) {
    FeaModalResult res;
    const int n_nodes = static_cast<int>(points.size() / 3);
    const int n_dof = n_nodes * 3;
    res.n_nodes = n_nodes;
    res.n_dof = n_dof;
    if (n_nodes == 0 || tets.empty()) { res.error = "fea_modal: empty mesh"; return res; }
    if (nodes_per_elem != 4 && nodes_per_elem != 10) {
        res.error = "fea_modal: nodes_per_elem must be 4 (tet4) or 10 (tet10)";
        return res;
    }
    if (tets.size() % static_cast<size_t>(nodes_per_elem) != 0) {
        res.error = "fea_modal: tets length must be divisible by nodes_per_elem";
        return res;
    }

    double lam, mu;
    lame(E_mpa, nu, lam, mu);
    const Eigen::Matrix<double, 6, 6> C = elasticity(lam, mu);
    const double density = density_g_cm3 * 1e-3;  // g/cm^3 -> g/mm^3

    auto node_pt = [&](int nid) {
        return Eigen::Vector3d(points[nid * 3], points[nid * 3 + 1], points[nid * 3 + 2]);
    };
    const int n_tets = static_cast<int>(tets.size() / nodes_per_elem);

    std::vector<char> fixed(n_dof, 0);
    for (int nid : fixed_nodes)
        if (nid >= 0 && nid < n_nodes)
            for (int k = 0; k < 3; ++k) fixed[nid * 3 + k] = 1;
    std::vector<int> reduced(n_dof, -1);
    int n_free = 0;
    for (int d = 0; d < n_dof; ++d)
        if (!fixed[d]) reduced[d] = n_free++;
    if (n_free < 3) { res.error = "fea_modal: too few free DOFs"; return res; }

    // Reduced free-free K + CONSISTENT M. A consistent mass matrix (vs the old diagonal-
    // lumped one) restores the near-degenerate first bending PAIR on a square cross-section
    // and removes the systematic low-frequency bias a lumped mass introduces on a coarse
    // tet mesh. For tet4 the consistent block for a node pair (a,b) is rho*V/20*(1+delta_ab);
    // for tet10 (2nd-order) both K and M are integrated/derived from the quadratic shape
    // functions — far more accurate in bending, which is the real cure for the square-section
    // split + low reading (Bug N-1).
    std::vector<Eigen::Triplet<double>> trips;
    std::vector<Eigen::Triplet<double>> mtrips;
    const int npe = nodes_per_elem;
    trips.reserve(static_cast<std::size_t>(n_tets) * (npe == 10 ? 900 : 144));
    mtrips.reserve(static_cast<std::size_t>(n_tets) * (npe == 10 ? 300 : 48));
    if (npe == 4) {
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
            // Consistent mass: block(a,b) = rho*V/20 * (2 if a==b else 1) on each of the 3
            // translational DOFs (the mass couples only same-direction DOFs).
            const double m0 = density * vol / 20.0;
            for (int a = 0; a < 4; ++a)
                for (int b = 0; b < 4; ++b) {
                    const double coef = m0 * (a == b ? 2.0 : 1.0);
                    for (int k = 0; k < 3; ++k) {
                        int ra = reduced[tn[a] * 3 + k];
                        int rb = reduced[tn[b] * 3 + k];
                        if (ra < 0 || rb < 0) continue;
                        mtrips.emplace_back(ra, rb, coef);
                    }
                }
        }
    } else {  // tet10
        const TetGauss4 gauss;
        for (int e = 0; e < n_tets; ++e) {
            std::array<Eigen::Vector3d, 10> ep;
            std::array<int, 10> tn;
            for (int j = 0; j < 10; ++j) { tn[j] = tets[e * 10 + j]; ep[j] = node_pt(tn[j]); }
            // Stiffness Ke (30x30): sum_g w_g * detJ_g * B_g^T C B_g.
            Eigen::Matrix<double, 30, 30> Ke = Eigen::Matrix<double, 30, 30>::Zero();
            bool ok = true;
            for (int g = 0; g < 4; ++g) {
                Eigen::Matrix<double, 6, 30> B;
                double detJ;
                if (!tet10_B_at(ep, gauss.pt[g][0], gauss.pt[g][1], gauss.pt[g][2], B, detJ)) {
                    ok = false; break;
                }
                // |detJ|: the element connectivity may be negatively oriented (a CW tet);
                // the physical volume is positive, so use the magnitude (mirrors fea_static).
                Ke += (gauss.w[g] * std::abs(detJ)) * (B.transpose() * C * B);
            }
            if (!ok) continue;
            // Consistent mass (10x10 scalar) from the corner-tet volume (exact for straight
            // edges); expanded to the 3 translational DOFs per node.
            const Eigen::Vector3d e1 = ep[1] - ep[0], e2 = ep[2] - ep[0], e3 = ep[3] - ep[0];
            const double vol = std::abs(e1.dot(e2.cross(e3))) / 6.0;
            if (!(vol > 0.0)) continue;
            const Eigen::Matrix<double, 10, 10> Ms = tet10_mass_scalar(density, vol);
            int dof_map[30];
            for (int j = 0; j < 10; ++j)
                for (int k = 0; k < 3; ++k) dof_map[j * 3 + k] = tn[j] * 3 + k;
            for (int a = 0; a < 30; ++a) {
                int ra = reduced[dof_map[a]];
                if (ra < 0) continue;
                for (int b = 0; b < 30; ++b) {
                    int rb = reduced[dof_map[b]];
                    if (rb < 0) continue;
                    trips.emplace_back(ra, rb, Ke(a, b));
                }
            }
            for (int a = 0; a < 10; ++a)
                for (int b = 0; b < 10; ++b) {
                    const double coef = Ms(a, b);
                    for (int k = 0; k < 3; ++k) {
                        int ra = reduced[tn[a] * 3 + k];
                        int rb = reduced[tn[b] * 3 + k];
                        if (ra < 0 || rb < 0) continue;
                        mtrips.emplace_back(ra, rb, coef);
                    }
                }
        }
    }
    Eigen::SparseMatrix<double> Kff(n_free, n_free);
    Kff.setFromTriplets(trips.begin(), trips.end());
    Kff.makeCompressed();
    Eigen::SparseMatrix<double> Mff(n_free, n_free);
    Mff.setFromTriplets(mtrips.begin(), mtrips.end());
    Mff.makeCompressed();

    // Shift-invert generalized eigensolve for the lowest modes. A SMALL NEGATIVE spectral
    // shift sigma makes (K - sigma*M) = (K + |sigma|*M) strictly positive-definite, so the
    // shift-invert factorization is robust even when K is ill-conditioned (a slender tet10
    // beam can hand an exactly-zero shift a non-positive LDLT pivot and fail). Any sigma < 0
    // still targets the smallest-positive eigenvalues (they remain the ones closest to it),
    // and Spectra returns the TRUE generalized eigenvalues, so frequencies are unbiased. We
    // scale |sigma| to the spectrum via the average K/M diagonal ratio (~ the eigenvalue
    // scale) with a tiny coefficient — enough to dominate roundoff, far below the 1st mode.
    int k_req = std::min(n_modes, n_free - 2);
    if (k_req < 1) k_req = 1;
    int ncv = std::min(n_free, std::max(2 * k_req + 1, 20));

    double kdiag = 0.0, mdiag = 0.0;
    for (int i = 0; i < n_free; ++i) { kdiag += Kff.coeff(i, i); mdiag += Mff.coeff(i, i); }
    const double evscale = (mdiag > 0.0) ? kdiag / mdiag : 1.0;
    const double sigma = -1e-5 * evscale;

    using OpType = Spectra::SymShiftInvert<double, Eigen::Sparse, Eigen::Sparse>;
    using BOpType = Spectra::SparseSymMatProd<double>;
    OpType op(Kff, Mff);
    BOpType Bop(Mff);
    Spectra::SymGEigsShiftSolver<OpType, BOpType, Spectra::GEigsMode::ShiftInvert> geigs(
        op, Bop, k_req, ncv, sigma);
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
