// Linear-static FEA solver implementation — see fea_static.h.
//
// Direct port of engine/sim/fea_static.py's numerical core. The reduced free-free
// system is assembled directly (fixed DOFs mapped out during assembly) so no sparse
// slicing is needed, then factored with Eigen::SparseLU. von Mises stress is recovered
// per element from the same strain-displacement operator and averaged onto nodes.

#include "sim/fea_static.h"

#include <algorithm>
#include <array>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <set>

#include <Eigen/Dense>
#include <Eigen/SparseCholesky>
#include <Eigen/SparseCore>
#include <Eigen/SparseLU>

namespace aicad::sim {

namespace {

// ── Lightweight, env-gated phase profiler ────────────────────────────────────────
// Set AI_CAD_FEA_PROFILE=1 to get a per-phase wall-clock + size/memory breakdown of a
// static solve on stderr. Each line is flushed immediately so, if the solve aborts
// (e.g. std::bad_alloc on a too-fine mesh -> SIGABRT), the LAST line printed localizes
// the death — a sampling profiler's report() never runs after an abort, this does.
// Costs nothing when the env var is unset (one getenv per solve).
struct FeaProf {
    bool on = false;
    std::chrono::steady_clock::time_point t0;
    explicit FeaProf() {
        const char* e = std::getenv("AI_CAD_FEA_PROFILE");
        on = e && e[0] && e[0] != '0';
        if (on) t0 = std::chrono::steady_clock::now();
    }
    // Milliseconds since the previous mark(), then re-anchor. Prints "[fea] <label> = <ms>".
    void mark(const char* label) {
        if (!on) return;
        const auto now = std::chrono::steady_clock::now();
        const double ms =
            std::chrono::duration<double, std::milli>(now - t0).count();
        std::fprintf(stderr, "[fea] %-22s %10.2f ms\n", label, ms);
        std::fflush(stderr);
        t0 = now;
    }
    template <typename... A>
    void note(const char* fmt, A... args) {
        if (!on) return;
        std::fprintf(stderr, "[fea] ");
        std::fprintf(stderr, fmt, args...);
        std::fprintf(stderr, "\n");
        std::fflush(stderr);
    }
};

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

// ── Quadratic (10-node) isoparametric tet ─────────────────────────────────────
//
// Natural coords (r,s,t); volume coords L0=1-r-s-t, L1=r, L2=s, L3=t. Node ordering
// matches the gmsh MSH_TET_10 layout (mesher.cpp): corners 0..3, then mid-edge nodes
// on edges {0,1},{1,2},{2,0},{3,0},{3,2},{3,1} -> nodes 4..9.

// Shape-function derivatives dN/d{r,s,t} (10x3) at natural coords (r,s,t).
// dL0/dr=dL0/ds=dL0/dt=-1; dL1/dr=1; dL2/ds=1; dL3/dt=1 (others 0).
Eigen::Matrix<double, 10, 3> tet10_dN(double r, double s, double t) {
    const double L0 = 1.0 - r - s - t, L1 = r, L2 = s, L3 = t;
    // Per-volume-coord gradients in (r,s,t).
    const Eigen::Vector3d g0(-1.0, -1.0, -1.0), g1(1.0, 0.0, 0.0),
        g2(0.0, 1.0, 0.0), g3(0.0, 0.0, 1.0);
    Eigen::Matrix<double, 10, 3> dN;
    // Corner: N = L(2L-1) -> dN = (4L-1) dL.
    dN.row(0) = (4.0 * L0 - 1.0) * g0.transpose();
    dN.row(1) = (4.0 * L1 - 1.0) * g1.transpose();
    dN.row(2) = (4.0 * L2 - 1.0) * g2.transpose();
    dN.row(3) = (4.0 * L3 - 1.0) * g3.transpose();
    // Mid-edge: N = 4 La Lb -> dN = 4 (La dLb + Lb dLa).
    dN.row(4) = (4.0 * (L0 * g1 + L1 * g0)).transpose();  // edge {0,1}
    dN.row(5) = (4.0 * (L1 * g2 + L2 * g1)).transpose();  // edge {1,2}
    dN.row(6) = (4.0 * (L2 * g0 + L0 * g2)).transpose();  // edge {2,0}
    dN.row(7) = (4.0 * (L3 * g0 + L0 * g3)).transpose();  // edge {3,0}
    dN.row(8) = (4.0 * (L3 * g2 + L2 * g3)).transpose();  // edge {3,2}
    dN.row(9) = (4.0 * (L3 * g1 + L1 * g3)).transpose();  // edge {3,1}
    return dN;
}

// 4-point Gauss rule for a tet (degree-2 exact). Points in (r,s,t); each weight 1/24
// so the weights sum to 1/6 (the reference-tet volume in natural coords).
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

// Build B (6x30) at one Gauss point for a tet10 with corner+mid-edge node coords `ep`
// (10 entries). Returns false if the Jacobian is degenerate; on success sets detJ and
// the spatial shape-function gradients dNxyz (10x3, dNi/d{x,y,z}).
bool tet10_B_at(const std::array<Eigen::Vector3d, 10>& ep, double r, double s, double t,
                Eigen::Matrix<double, 6, 30>& B, double& detJ,
                Eigen::Matrix<double, 10, 3>& dNxyz) {
    const Eigen::Matrix<double, 10, 3> dNrst = tet10_dN(r, s, t);
    // Jacobian J(3x3): J(i,j) = d x_i / d xi_j = sum_n ep[n](i) * dNrst(n,j).
    Eigen::Matrix3d J = Eigen::Matrix3d::Zero();
    for (int n = 0; n < 10; ++n) J += ep[n] * dNrst.row(n);
    detJ = J.determinant();
    if (!(std::abs(detJ) > 0.0)) return false;
    Eigen::Matrix3d Jinv = J.inverse();
    if (!Jinv.allFinite()) return false;
    // dN/d{x,y,z} = dN/d{r,s,t} * Jinv  (row n: grad in natural * Jinv = grad in space).
    dNxyz = dNrst * Jinv;
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

// ── hex8 (8-node trilinear hexahedron) ──────────────────────────────────────────
// Natural-coord node ordering matches the structured grid emitted by mesh_box_hex:
// bottom face (zeta=-1) corners 0,1,2,3 CCW, top face (zeta=+1) corners 4,5,6,7.
constexpr int HEX_NAT[8][3] = {{-1, -1, -1}, {1, -1, -1}, {1, 1, -1}, {-1, 1, -1},
                               {-1, -1, 1},  {1, -1, 1},  {1, 1, 1},  {-1, 1, 1}};

// 2x2x2 Gauss rule (8 points at +-1/sqrt(3), unit weights).
struct HexGauss8 {
    double pt[8][3];
    double w[8];
    HexGauss8() {
        const double g = 0.5773502691896257;  // 1/sqrt(3)
        for (int i = 0; i < 8; ++i) {
            for (int k = 0; k < 3; ++k) pt[i][k] = HEX_NAT[i][k] * g;
            w[i] = 1.0;
        }
    }
};

// Shape-function natural gradients (8x3) at (xi,eta,zeta): dNi/d{xi,eta,zeta}.
Eigen::Matrix<double, 8, 3> hex8_dN(double xi, double eta, double zeta) {
    Eigen::Matrix<double, 8, 3> dN;
    for (int n = 0; n < 8; ++n) {
        const double X = HEX_NAT[n][0], Y = HEX_NAT[n][1], Z = HEX_NAT[n][2];
        dN(n, 0) = 0.125 * X * (1 + Y * eta) * (1 + Z * zeta);
        dN(n, 1) = 0.125 * (1 + X * xi) * Y * (1 + Z * zeta);
        dN(n, 2) = 0.125 * (1 + X * xi) * (1 + Y * eta) * Z;
    }
    return dN;
}

// Build B (6x24) at one Gauss point for a hex8 with corner coords `ep` (8 entries).
// Returns false if the Jacobian is degenerate; on success sets detJ.
bool hex8_B_at(const std::array<Eigen::Vector3d, 8>& ep, double xi, double eta, double zeta,
               Eigen::Matrix<double, 6, 24>& B, double& detJ) {
    const Eigen::Matrix<double, 8, 3> dNrst = hex8_dN(xi, eta, zeta);
    Eigen::Matrix3d J = Eigen::Matrix3d::Zero();
    for (int n = 0; n < 8; ++n) J += ep[n] * dNrst.row(n);
    detJ = J.determinant();
    if (!(std::abs(detJ) > 0.0)) return false;
    Eigen::Matrix3d Jinv = J.inverse();
    if (!Jinv.allFinite()) return false;
    const Eigen::Matrix<double, 8, 3> dNxyz = dNrst * Jinv;
    B.setZero();
    for (int n = 0; n < 8; ++n) {
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

}  // namespace

FeaStaticResult fea_static_solve(const std::vector<double>& points, const std::vector<int>& tets,
                                 double E_mpa, double nu, double yield_mpa,
                                 const std::vector<int>& fixed_nodes,
                                 const std::vector<double>& nodal_forces,
                                 int nodes_per_elem) {
    FeaProf prof;
    FeaStaticResult res;
    const int n_nodes = static_cast<int>(points.size() / 3);
    const int n_dof = n_nodes * 3;
    res.n_dof = n_dof;
    if (n_nodes == 0 || tets.empty()) {
        res.error = "fea_static: empty mesh";
        return res;
    }
    if (points.size() % 3 != 0) {
        res.error = "fea_static: points length must be divisible by 3";
        return res;
    }
    if (static_cast<int>(nodal_forces.size()) != n_dof) {
        res.error = "fea_static: nodal_forces length must be n_nodes*3";
        return res;
    }
    if (nodes_per_elem != 4 && nodes_per_elem != 10 && nodes_per_elem != 8) {
        res.error = "fea_static: nodes_per_elem must be 4 (tet4), 10 (tet10) or 8 (hex8)";
        return res;
    }
    if (tets.size() % static_cast<size_t>(nodes_per_elem) != 0) {
        res.error = "fea_static: tets length must be divisible by nodes_per_elem";
        return res;
    }
    if (!std::isfinite(E_mpa) || E_mpa <= 0.0 || !std::isfinite(nu) || nu <= -1.0 ||
        nu >= 0.5) {
        res.error = "fea_static: material requires E>0 and -1<nu<0.5";
        return res;
    }
    for (double x : points) {
        if (!std::isfinite(x)) {
            res.error = "fea_static: mesh coordinates must be finite";
            return res;
        }
    }
    for (double x : nodal_forces) {
        if (!std::isfinite(x)) {
            res.error = "fea_static: nodal forces must be finite";
            return res;
        }
    }
    const bool tet10 = (nodes_per_elem == 10);
    const bool hex8 = (nodes_per_elem == 8);
    const int stride = nodes_per_elem;
    for (int nid : tets) {
        if (nid < 0 || nid >= n_nodes) {
            res.error = "fea_static: tet node index out of range";
            return res;
        }
    }

    double lam, mu;
    lame(E_mpa, nu, lam, mu);
    const Eigen::Matrix<double, 6, 6> C = elasticity(lam, mu);

    auto node_pt = [&](int nid) {
        return Eigen::Vector3d(points[nid * 3], points[nid * 3 + 1], points[nid * 3 + 2]);
    };
    const int n_tets = static_cast<int>(tets.size() / stride);
    const TetGauss4 gauss;

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

    prof.mark("setup+dof-map");
    // Assemble the reduced free-free stiffness directly (skip rows/cols of fixed DOFs).
    std::vector<Eigen::Triplet<double>> trips;
    const int edof = stride * 3;  // element DOFs (12 for tet4, 30 for tet10)
    const std::size_t n_trips =
        static_cast<std::size_t>(n_tets) * static_cast<std::size_t>(edof) * edof;
    // Size + memory census BEFORE the big reserve. On a too-fine mesh this reserve (or the
    // factorization below) is where std::bad_alloc throws -> SIGABRT; this flushed line is
    // the last thing printed, so it pins the ceiling DOF/element count that the box can take.
    prof.note("nodes=%d  n_dof=%d  n_elems=%d  nodes/elem=%d  free_dof=%d", n_nodes, n_dof,
              n_tets, nodes_per_elem, n_free);
    prof.note("triplets=%zu  triplet_reserve=%.1f MB (%zu B/elem)", n_trips,
              static_cast<double>(n_trips * sizeof(Eigen::Triplet<double>)) / 1.048576e6,
              static_cast<std::size_t>(edof) * edof * sizeof(Eigen::Triplet<double>));
    trips.reserve(n_trips);
    if (hex8) {
        // ── hex8 path (8-node trilinear, 2x2x2 Gauss) ────────────────────────
        const HexGauss8 hg;
        for (int e = 0; e < n_tets; ++e) {
            std::array<Eigen::Vector3d, 8> ep;
            std::array<int, 8> tn;
            for (int j = 0; j < 8; ++j) { tn[j] = tets[e * 8 + j]; ep[j] = node_pt(tn[j]); }
            Eigen::Matrix<double, 24, 24> Ke = Eigen::Matrix<double, 24, 24>::Zero();
            bool ok_elem = true;
            for (int g = 0; g < 8; ++g) {
                Eigen::Matrix<double, 6, 24> B;
                double detJ;
                if (!hex8_B_at(ep, hg.pt[g][0], hg.pt[g][1], hg.pt[g][2], B, detJ)) {
                    ok_elem = false;
                    break;
                }
                Ke += B.transpose() * C * B * (std::abs(detJ) * hg.w[g]);
            }
            if (!ok_elem) continue;
            int dof_map[24];
            for (int j = 0; j < 8; ++j)
                for (int k = 0; k < 3; ++k) dof_map[j * 3 + k] = tn[j] * 3 + k;
            for (int a = 0; a < 24; ++a) {
                int ra = reduced[dof_map[a]];
                if (ra < 0) continue;
                for (int b = 0; b < 24; ++b) {
                    int rb = reduced[dof_map[b]];
                    if (rb < 0) continue;
                    trips.emplace_back(ra, rb, Ke(a, b));
                }
            }
        }
    } else if (!tet10) {
        // ── tet4 path (UNCHANGED — parity-critical) ──────────────────────────
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
    } else {
        // ── tet10 path (quadratic isoparametric, 4-point Gauss) ──────────────
        for (int e = 0; e < n_tets; ++e) {
            std::array<Eigen::Vector3d, 10> ep;
            std::array<int, 10> tn;
            for (int j = 0; j < 10; ++j) {
                tn[j] = tets[e * 10 + j];
                ep[j] = node_pt(tn[j]);
            }
            Eigen::Matrix<double, 30, 30> Ke = Eigen::Matrix<double, 30, 30>::Zero();
            bool ok_elem = true;
            for (int g = 0; g < 4; ++g) {
                Eigen::Matrix<double, 6, 30> B;
                double detJ;
                Eigen::Matrix<double, 10, 3> dNxyz;
                if (!tet10_B_at(ep, gauss.pt[g][0], gauss.pt[g][1], gauss.pt[g][2], B, detJ,
                                dNxyz)) {
                    ok_elem = false;
                    break;
                }
                Ke += B.transpose() * C * B * (std::abs(detJ) * gauss.w[g]);
            }
            if (!ok_elem) continue;
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
        }
    }

    prof.mark("assemble-elements");
    Eigen::SparseMatrix<double> Kff(n_free, n_free);
    Kff.setFromTriplets(trips.begin(), trips.end());
    Kff.makeCompressed();
    if (Kff.nonZeros() == 0) {
        res.error = "fea_static: no valid elements contributed stiffness";
        return res;
    }
    prof.note("Kff %dx%d  nnz=%lld  (~%.1f MB compressed)", n_free, n_free,
              static_cast<long long>(Kff.nonZeros()),
              static_cast<double>(Kff.nonZeros()) * (sizeof(double) + sizeof(int)) / 1.048576e6);
    prof.mark("build-sparse-matrix");

    Eigen::VectorXd ff(n_free);
    for (int d = 0; d < n_dof; ++d)
        if (reduced[d] >= 0) ff(reduced[d]) = nodal_forces[d];

    // The reduced stiffness is symmetric positive-definite. For tet10 (large systems, no Python
    // parity oracle) use a Cholesky factorization (SimplicialLDLT) — markedly faster + lower memory
    // than SparseLU on SPD systems — and fall back to SparseLU only if it reports failure. tet4 keeps
    // SparseLU verbatim so its results stay byte-identical to the Python oracle (parity harness).
    Eigen::VectorXd uf;
    bool solved = false;
    if (tet10 || hex8) {
        // SPD systems with no Python-parity constraint: Cholesky is faster + lighter.
        prof.note("solver=SimplicialLDLT (Cholesky, SPD)");
        Eigen::SimplicialLDLT<Eigen::SparseMatrix<double>> ldlt;
        ldlt.compute(Kff);
        prof.mark("factorize (LDLT)");
        if (ldlt.info() == Eigen::Success) {
            uf = ldlt.solve(ff);
            prof.mark("back-substitute");
            if (ldlt.info() == Eigen::Success) solved = true;
        }
    }
    if (!solved) {
        prof.note("solver=SparseLU%s", (tet10 || hex8) ? " (LDLT fell back)" : " (tet4 parity)");
        Eigen::SparseLU<Eigen::SparseMatrix<double>> lu;
        lu.analyzePattern(Kff);
        prof.mark("LU analyzePattern");
        lu.factorize(Kff);
        prof.mark("LU factorize");
        if (lu.info() != Eigen::Success) {
            res.error = "fea_static: sparse factorization failed (singular system)";
            return res;
        }
        uf = lu.solve(ff);
        prof.mark("LU back-substitute");
        if (lu.info() != Eigen::Success) {
            res.error = "fea_static: sparse solve failed";
            return res;
        }
    }

    // Scatter back to the full DOF vector (fixed DOFs stay zero).
    std::vector<double> u_full(n_dof, 0.0);
    for (int d = 0; d < n_dof; ++d)
        if (reduced[d] >= 0) u_full[d] = uf(reduced[d]);
    for (double x : u_full) {
        if (!std::isfinite(x)) {
            res.error = "fea_static: sparse solve produced non-finite displacement";
            return res;
        }
    }

    // Displacement + magnitude.
    res.displacement = u_full;
    res.disp_magnitude.assign(n_nodes, 0.0);
    for (int i = 0; i < n_nodes; ++i) {
        double x = u_full[i * 3], y = u_full[i * 3 + 1], z = u_full[i * 3 + 2];
        res.disp_magnitude[i] = std::sqrt(x * x + y * y + z * z);
    }

    // Per-element von Mises -> node-averaged (mean of neighbouring elements).
    // Additive (parity-safe): in the SAME loop, accumulate the full symmetric stress
    // tensor (sx,sy,sz,txy,tyz,txz) per node with the SAME vm_count divisor, so the
    // von Mises accumulation below is byte-for-byte unchanged.
    std::vector<double> vm_accum(n_nodes, 0.0);
    std::vector<int> vm_count(n_nodes, 0);
    std::vector<double> sx_accum(n_nodes, 0.0), sy_accum(n_nodes, 0.0), sz_accum(n_nodes, 0.0);
    std::vector<double> txy_accum(n_nodes, 0.0), tyz_accum(n_nodes, 0.0), txz_accum(n_nodes, 0.0);
    if (hex8) {
        // ── hex8 stress recovery: average stress over the 8 Gauss points, then
        // scatter that element stress onto all 8 corner nodes (same node-averaging
        // scheme as tet4/tet10). ───────────────────────────────────────────────
        const HexGauss8 hg;
        for (int e = 0; e < n_tets; ++e) {
            std::array<Eigen::Vector3d, 8> ep;
            std::array<int, 8> tn;
            for (int j = 0; j < 8; ++j) { tn[j] = tets[e * 8 + j]; ep[j] = node_pt(tn[j]); }
            Eigen::Matrix<double, 24, 1> ue;
            for (int j = 0; j < 8; ++j)
                for (int k = 0; k < 3; ++k) ue(j * 3 + k) = u_full[tn[j] * 3 + k];
            Eigen::Matrix<double, 6, 1> stress_avg = Eigen::Matrix<double, 6, 1>::Zero();
            int ngp = 0;
            for (int g = 0; g < 8; ++g) {
                Eigen::Matrix<double, 6, 24> B;
                double detJ;
                if (!hex8_B_at(ep, hg.pt[g][0], hg.pt[g][1], hg.pt[g][2], B, detJ)) continue;
                stress_avg += C * (B * ue);
                ++ngp;
            }
            if (ngp == 0) continue;
            stress_avg /= static_cast<double>(ngp);
            double sx = stress_avg(0), sy = stress_avg(1), sz = stress_avg(2);
            double txy = stress_avg(3), tyz = stress_avg(4), txz = stress_avg(5);
            double vm = std::sqrt(0.5 * ((sx - sy) * (sx - sy) + (sy - sz) * (sy - sz) +
                                         (sz - sx) * (sz - sx) +
                                         6.0 * (txy * txy + tyz * tyz + txz * txz)));
            for (int j = 0; j < 8; ++j) {
                vm_accum[tn[j]] += vm; vm_count[tn[j]] += 1;
                sx_accum[tn[j]] += sx;  sy_accum[tn[j]] += sy;  sz_accum[tn[j]] += sz;
                txy_accum[tn[j]] += txy; tyz_accum[tn[j]] += tyz; txz_accum[tn[j]] += txz;
            }
        }
    } else if (!tet10) {
        // ── tet4 stress recovery (UNCHANGED — parity-critical) ───────────────
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
            for (int j = 0; j < 4; ++j) {
                vm_accum[tn[j]] += vm; vm_count[tn[j]] += 1;
                sx_accum[tn[j]] += sx;  sy_accum[tn[j]] += sy;  sz_accum[tn[j]] += sz;
                txy_accum[tn[j]] += txy; tyz_accum[tn[j]] += tyz; txz_accum[tn[j]] += txz;
            }
        }
    } else {
        // ── tet10 stress recovery: evaluate at the 4 Gauss points, average to a
        // single element stress, then scatter that element stress onto ALL 10
        // nodes (same node-averaging scheme as tet4). This reuses the per-node
        // vm/principal/shear recovery below unchanged.
        for (int e = 0; e < n_tets; ++e) {
            std::array<Eigen::Vector3d, 10> ep;
            std::array<int, 10> tn;
            for (int j = 0; j < 10; ++j) {
                tn[j] = tets[e * 10 + j];
                ep[j] = node_pt(tn[j]);
            }
            Eigen::Matrix<double, 30, 1> ue;
            for (int j = 0; j < 10; ++j)
                for (int k = 0; k < 3; ++k) ue(j * 3 + k) = u_full[tn[j] * 3 + k];
            Eigen::Matrix<double, 6, 1> stress_avg = Eigen::Matrix<double, 6, 1>::Zero();
            int ngp = 0;
            for (int g = 0; g < 4; ++g) {
                Eigen::Matrix<double, 6, 30> B;
                double detJ;
                Eigen::Matrix<double, 10, 3> dNxyz;
                if (!tet10_B_at(ep, gauss.pt[g][0], gauss.pt[g][1], gauss.pt[g][2], B, detJ,
                                dNxyz))
                    continue;
                Eigen::Matrix<double, 6, 1> strain = B * ue;
                stress_avg += C * strain;
                ++ngp;
            }
            if (ngp == 0) continue;
            stress_avg /= static_cast<double>(ngp);
            double sx = stress_avg(0), sy = stress_avg(1), sz = stress_avg(2);
            double txy = stress_avg(3), tyz = stress_avg(4), txz = stress_avg(5);
            double vm = std::sqrt(0.5 * ((sx - sy) * (sx - sy) + (sy - sz) * (sy - sz) +
                                         (sz - sx) * (sz - sx) +
                                         6.0 * (txy * txy + tyz * tyz + txz * txz)));
            for (int j = 0; j < 10; ++j) {
                vm_accum[tn[j]] += vm; vm_count[tn[j]] += 1;
                sx_accum[tn[j]] += sx;  sy_accum[tn[j]] += sy;  sz_accum[tn[j]] += sz;
                txy_accum[tn[j]] += txy; tyz_accum[tn[j]] += tyz; txz_accum[tn[j]] += txz;
            }
        }
    }
    res.von_mises.assign(n_nodes, 0.0);
    for (int i = 0; i < n_nodes; ++i)
        if (vm_count[i] > 0) res.von_mises[i] = vm_accum[i] / vm_count[i];

    // ── Additive principal/shear/normal-component recovery ───────────────────
    // Average each tensor component (same divisor), build the 3x3 symmetric stress
    // tensor per node, and take its eigenvalues (ascending: p3=eig[0], p1=eig[2]).
    res.principal1.assign(n_nodes, 0.0);
    res.principal3.assign(n_nodes, 0.0);
    res.max_shear.assign(n_nodes, 0.0);
    res.stress_xx.assign(n_nodes, 0.0);
    res.stress_yy.assign(n_nodes, 0.0);
    res.stress_zz.assign(n_nodes, 0.0);
    Eigen::SelfAdjointEigenSolver<Eigen::Matrix3d> eig_solver;
    for (int i = 0; i < n_nodes; ++i) {
        if (vm_count[i] <= 0) continue;  // no incident elements -> leave zeros
        const double inv = 1.0 / static_cast<double>(vm_count[i]);
        const double sx = sx_accum[i] * inv;
        const double sy = sy_accum[i] * inv;
        const double sz = sz_accum[i] * inv;
        const double txy = txy_accum[i] * inv;
        const double tyz = tyz_accum[i] * inv;
        const double txz = txz_accum[i] * inv;
        res.stress_xx[i] = sx;
        res.stress_yy[i] = sy;
        res.stress_zz[i] = sz;
        Eigen::Matrix3d T;
        T << sx, txy, txz,
             txy, sy, tyz,
             txz, tyz, sz;
        if (!T.allFinite()) continue;  // guard NaN/inf -> leave zeros
        eig_solver.computeDirect(T);
        if (eig_solver.info() != Eigen::Success) continue;
        const Eigen::Vector3d ev = eig_solver.eigenvalues();  // ascending
        const double p3 = ev(0), p1 = ev(2);
        if (!std::isfinite(p1) || !std::isfinite(p3)) continue;
        res.principal1[i] = p1;
        res.principal3[i] = p3;
        res.max_shear[i] = (p1 - p3) * 0.5;
    }

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

    // Additive summary extrema over the per-node principal/shear arrays.
    double max_p1 = 0.0, min_p3 = 0.0, max_sh = 0.0;
    bool first_p1 = true, first_p3 = true;
    for (int i = 0; i < n_nodes; ++i) {
        if (vm_count[i] <= 0) continue;
        const double p1 = res.principal1[i], p3 = res.principal3[i], sh = res.max_shear[i];
        if (first_p1 || p1 > max_p1) { max_p1 = p1; first_p1 = false; }
        if (first_p3 || p3 < min_p3) { min_p3 = p3; first_p3 = false; }
        if (sh > max_sh) max_sh = sh;
    }
    res.max_principal_mpa = first_p1 ? 0.0 : max_p1;
    res.min_principal_mpa = first_p3 ? 0.0 : min_p3;
    res.max_shear_mpa = max_sh;
    res.n_nodes = n_nodes;
    res.n_elems = n_tets;
    res.ok = true;
    prof.mark("stress-recovery");
    return res;
}

}  // namespace aicad::sim
