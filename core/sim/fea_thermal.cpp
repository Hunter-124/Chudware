// Steady-state thermal FEA (heat conduction) — see fea_thermal.h.
//
// Scalar (1 DOF/node) analogue of fea_static: assemble the reduced conductivity matrix
// K_ij = ∫ k ∇N_i·∇N_j dV directly (Dirichlet DOFs mapped out during assembly, their
// prescribed temperatures moved to the RHS), add convection surface terms + flux/convection
// surface loads, then factor with Eigen's sparse Cholesky (SPD), falling back to SparseLU.
//
// The ∇N machinery mirrors fea_static.cpp's tet10 isoparametric element, but that solver
// keeps its shape functions in an anonymous namespace (not exported, and parity-critical),
// so the gradients are re-derived here from the same closed forms rather than shared.

#include "sim/fea_thermal.h"

#include <algorithm>
#include <array>
#include <cmath>
#include <unordered_set>

#include <Eigen/Dense>
#include <Eigen/SparseCholesky>
#include <Eigen/SparseCore>
#include <Eigen/SparseLU>

namespace aicad::sim {

namespace {

using Vec3 = Eigen::Vector3d;

// 4-point Gauss rule on the reference tet (degree-2 exact); weights sum to 1/6 (the
// reference-tet natural volume). Identical rule to fea_static's TetGauss4.
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

// tet10 shape-function derivatives dN/d{r,s,t} (10x3) at natural coords (r,s,t). Volume
// coords L0=1-r-s-t, L1=r, L2=s, L3=t; corner N=L(2L-1), mid-edge N=4 La Lb. Edge order
// matches the gmsh MSH_TET_10 layout {0,1},{1,2},{2,0},{3,0},{3,2},{3,1} -> nodes 4..9.
Eigen::Matrix<double, 10, 3> tet10_dN(double r, double s, double t) {
    const double L0 = 1.0 - r - s - t, L1 = r, L2 = s, L3 = t;
    const Vec3 g0(-1.0, -1.0, -1.0), g1(1.0, 0.0, 0.0), g2(0.0, 1.0, 0.0), g3(0.0, 0.0, 1.0);
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

// Spatial gradients dNi/d{x,y,z} (10x3) + |detJ| at one Gauss point of a tet10. Returns
// false on a degenerate Jacobian. dNxyz = dNrst * J^{-1}, J(i,j)=sum_n ep[n](i) dNrst(n,j).
bool tet10_grad_at(const std::array<Vec3, 10>& ep, double r, double s, double t,
                   Eigen::Matrix<double, 10, 3>& dNxyz, double& detJ) {
    const Eigen::Matrix<double, 10, 3> dNrst = tet10_dN(r, s, t);
    Eigen::Matrix3d J = Eigen::Matrix3d::Zero();
    for (int n = 0; n < 10; ++n) J += ep[n] * dNrst.row(n);
    detJ = J.determinant();
    if (!(std::abs(detJ) > 0.0)) return false;
    Eigen::Matrix3d Jinv = J.inverse();
    if (!Jinv.allFinite()) return false;
    dNxyz = dNrst * Jinv;
    return true;
}

// tet4 constant shape-function gradients dNi/d{x,y,z} (4x3) + element volume. Same T-matrix
// inverse form fea_static uses (rows 1..3 of T^{-1} are the spatial gradients). false=degenerate.
bool tet4_grad(const std::array<Vec3, 4>& ep, Eigen::Matrix<double, 4, 3>& dNxyz, double& vol) {
    Eigen::Matrix4d T;
    for (int j = 0; j < 4; ++j) {
        T(j, 0) = 1.0;
        T(j, 1) = ep[j].x();
        T(j, 2) = ep[j].y();
        T(j, 3) = ep[j].z();
    }
    const double det = T.determinant();
    vol = std::abs(det) / 6.0;
    if (vol <= 0.0) return false;
    Eigen::Matrix4d Tinv = T.inverse();
    if (!Tinv.allFinite()) { vol = 0.0; return false; }
    // dN[i,c] = d(N_i)/d(x_c); rows 1..3 of Tinv are grad components, columns index the node.
    for (int i = 0; i < 4; ++i)
        for (int c = 0; c < 3; ++c) dNxyz(i, c) = Tinv(c + 1, i);
    return true;
}

// ── Surface element tables (shared with fea_run.cpp's load logic) ───────────────────────
// tet corner faces + the tet10 mid-edge local node on each face edge (mesher edge order
// {0,1}=4 {1,2}=5 {2,0}=6 {3,0}=7 {3,2}=8 {3,1}=9). Corners CCW so the fan normal is outward.
struct FaceTab {
    int corner[3];
    int mid[3];
};
const FaceTab kTetFaces[4] = {
    {{0, 1, 2}, {4, 5, 6}},  // edges (0,1)=4 (1,2)=5 (2,0)=6
    {{0, 1, 3}, {4, 9, 7}},  // edges (0,1)=4 (1,3)=9 (3,0)=7
    {{0, 2, 3}, {6, 8, 7}},  // edges (0,2)=6 (2,3)=8 (3,0)=7
    {{1, 2, 3}, {5, 8, 9}},  // edges (1,2)=5 (2,3)=8 (3,1)=9
};

// A flat triangle's area from its three corner coords.
double tri_area(const Vec3& a, const Vec3& b, const Vec3& c) {
    return 0.5 * ((b - a).cross(c - a)).norm();
}

// Consistent surface load weights ∫N_i dS for one boundary triangle (tri3 for tet4, tri6 for
// tet10) of area A. tri3: A/3 per corner. tri6: corners A/... — the exact quadratic
// moments are ∫N_corner dS = 0, ∫N_mid dS = A/3 (same rule fea_run uses for tractions).
// Writes the 3 corner + 3 mid weights (mid unused for tet4).
void tri_load_weights(bool tet10, double A, double wc[3], double wm[3]) {
    if (tet10) {
        for (int i = 0; i < 3; ++i) { wc[i] = 0.0; wm[i] = A / 3.0; }
    } else {
        for (int i = 0; i < 3; ++i) { wc[i] = A / 3.0; wm[i] = 0.0; }
    }
}

// Consistent surface MASS matrix ∫N_i N_j dS for one boundary triangle, needed for the
// convection term h∫N N dS. For a flat linear tri3 the standard result is
//     M = (A/12) [[2,1,1],[1,2,1],[1,1,2]]        (3x3, corner nodes)
// For a flat quadratic tri6 the closed form (area-coord moments over the reference triangle,
// mapped by the constant area A) is the tabulated 6x6 matrix below (Zienkiewicz, "The Finite
// Element Method", surface consistent mass for a 6-node triangle):
//     order [c0,c1,c2, m01,m12,m20], symmetric.
// M_tri6 = (A/180) * S  with S the integer matrix hard-coded here.
// Fills M (6x6); for tet4 only the top-left 3x3 (corner) block is populated.
void tri_surface_mass(bool tet10, double A, Eigen::Matrix<double, 6, 6>& M) {
    M.setZero();
    if (!tet10) {
        const double c = A / 12.0;
        for (int i = 0; i < 3; ++i)
            for (int j = 0; j < 3; ++j) M(i, j) = c * (i == j ? 2.0 : 1.0);
        return;
    }
    // tri6 consistent surface mass, integer template S (rows/cols: c0 c1 c2 m01 m12 m20).
    // ∫_ref N_i N_j dA multiplied by 2A (2*ref-area normalisation) gives A/180 * S.
    static const double S[6][6] = {
        { 6.0, -1.0, -1.0,  0.0, -4.0,  0.0},
        {-1.0,  6.0, -1.0,  0.0,  0.0, -4.0},
        {-1.0, -1.0,  6.0, -4.0,  0.0,  0.0},
        { 0.0,  0.0, -4.0, 32.0, 16.0, 16.0},
        {-4.0,  0.0,  0.0, 16.0, 32.0, 16.0},
        { 0.0, -4.0,  0.0, 16.0, 16.0, 32.0},
    };
    const double c = A / 180.0;
    for (int i = 0; i < 6; ++i)
        for (int j = 0; j < 6; ++j) M(i, j) = c * S[i][j];
}

}  // namespace

ThermalResult fea_thermal_solve(const std::vector<double>& points, const std::vector<int>& tets,
                                double k_cond, const std::vector<int>& fixed_nodes,
                                const std::vector<double>& fixed_temps,
                                const std::vector<int>& flux_nodes, double flux_q,
                                const std::vector<ThermalConvection>& convections,
                                int nodes_per_elem) {
    ThermalResult res;
    const int n_nodes = static_cast<int>(points.size() / 3);
    res.n_nodes = n_nodes;
    if (n_nodes == 0 || tets.empty()) { res.error = "fea_thermal: empty mesh"; return res; }
    if (points.size() % 3 != 0) {
        res.error = "fea_thermal: points length must be divisible by 3";
        return res;
    }
    if (nodes_per_elem != 4 && nodes_per_elem != 10) {
        res.error = "fea_thermal: nodes_per_elem must be 4 (tet4) or 10 (tet10)";
        return res;
    }
    if (tets.size() % static_cast<size_t>(nodes_per_elem) != 0) {
        res.error = "fea_thermal: tets length must be divisible by nodes_per_elem";
        return res;
    }
    if (!std::isfinite(k_cond) || k_cond <= 0.0) {
        res.error = "fea_thermal: conductivity k must be finite and > 0";
        return res;
    }
    if (fixed_nodes.size() != fixed_temps.size()) {
        res.error = "fea_thermal: fixed_nodes and fixed_temps length mismatch";
        return res;
    }
    for (double x : points)
        if (!std::isfinite(x)) { res.error = "fea_thermal: mesh coordinates must be finite"; return res; }
    for (int nid : tets)
        if (nid < 0 || nid >= n_nodes) { res.error = "fea_thermal: tet node index out of range"; return res; }

    const bool tet10 = (nodes_per_elem == 10);
    const int stride = nodes_per_elem;
    const int n_elems = static_cast<int>(tets.size() / stride);
    res.n_elems = n_elems;

    auto node_pt = [&](int nid) {
        return Vec3(points[nid * 3], points[nid * 3 + 1], points[nid * 3 + 2]);
    };

    // ── Singularity guard: a floating datum. A pure-Neumann problem (no Dirichlet node AND
    // no convection face) has no unique steady temperature — reject it up front. (The
    // advisor precheck lints this before dispatch; this is the belt-and-suspenders backstop.)
    bool has_convection = false;
    for (const auto& cv : convections)
        if (cv.h > 0.0 && !cv.nodes.empty()) { has_convection = true; break; }
    if (fixed_nodes.empty() && !has_convection) {
        res.error = "fea_thermal: no fixed-temperature node and no convection — singular "
                    "(temperature datum undefined). Add a fixed temperature or a convection BC.";
        return res;
    }

    // ── Dirichlet map: node -> prescribed temperature (last write wins); free-DOF numbering.
    std::vector<char> is_fixed(n_nodes, 0);
    std::vector<double> pres_temp(n_nodes, 0.0);
    for (size_t i = 0; i < fixed_nodes.size(); ++i) {
        const int nid = fixed_nodes[i];
        if (nid < 0 || nid >= n_nodes) { res.error = "fea_thermal: fixed node out of range"; return res; }
        if (!std::isfinite(fixed_temps[i])) { res.error = "fea_thermal: fixed temperature not finite"; return res; }
        is_fixed[nid] = 1;
        pres_temp[nid] = fixed_temps[i];
    }
    std::vector<int> reduced(n_nodes, -1);
    int n_free = 0;
    for (int i = 0; i < n_nodes; ++i)
        if (!is_fixed[i]) reduced[i] = n_free++;
    if (n_free == 0) {
        // Fully prescribed: the temperature field IS the Dirichlet data (a valid, trivial solve).
        res.temperature = pres_temp;
        res.n_dof = 0;
        double mx = pres_temp.empty() ? 0.0 : pres_temp[0], mn = mx;
        for (double v : pres_temp) { mx = std::max(mx, v); mn = std::min(mn, v); }
        res.max_temp = mx; res.min_temp = mn; res.ok = true;
        return res;
    }

    // ── Assemble the FULL conductivity matrix as triplets over ALL node DOFs, then split
    // into the reduced free-free block Kff and the free-fixed coupling that moves the
    // prescribed temperatures to the RHS: f_reduced -= Kfp * T_prescribed. Convection surface
    // terms add into the SAME full triplet stream (they touch free+fixed DOFs alike).
    std::vector<Eigen::Triplet<double>> trips;         // -> Kff (free rows & cols)
    Eigen::VectorXd f = Eigen::VectorXd::Zero(n_free);  // reduced load vector
    const TetGauss4 gauss;

    auto add_KK = [&](int a_node, int b_node, double kab) {
        const int ra = reduced[a_node];
        if (ra < 0) return;  // fixed row contributes nothing to the reduced system
        const int rb = reduced[b_node];
        if (rb >= 0) {
            trips.emplace_back(ra, rb, kab);          // free-free block
        } else {
            f(ra) -= kab * pres_temp[b_node];         // move prescribed T to the RHS
        }
    };

    // Volume conductivity K_ij = ∫ k ∇N_i·∇N_j dV.
    if (tet10) {
        for (int e = 0; e < n_elems; ++e) {
            std::array<Vec3, 10> ep;
            std::array<int, 10> tn;
            for (int j = 0; j < 10; ++j) { tn[j] = tets[e * 10 + j]; ep[j] = node_pt(tn[j]); }
            Eigen::Matrix<double, 10, 10> Ke = Eigen::Matrix<double, 10, 10>::Zero();
            bool ok_elem = true;
            for (int g = 0; g < 4; ++g) {
                Eigen::Matrix<double, 10, 3> dNxyz;
                double detJ;
                if (!tet10_grad_at(ep, gauss.pt[g][0], gauss.pt[g][1], gauss.pt[g][2], dNxyz, detJ)) {
                    ok_elem = false;
                    break;
                }
                Ke += (dNxyz * dNxyz.transpose()) * (k_cond * std::abs(detJ) * gauss.w[g]);
            }
            if (!ok_elem) continue;
            for (int a = 0; a < 10; ++a)
                for (int b = 0; b < 10; ++b) add_KK(tn[a], tn[b], Ke(a, b));
        }
    } else {
        for (int e = 0; e < n_elems; ++e) {
            std::array<Vec3, 4> ep;
            std::array<int, 4> tn;
            for (int j = 0; j < 4; ++j) { tn[j] = tets[e * 4 + j]; ep[j] = node_pt(tn[j]); }
            Eigen::Matrix<double, 4, 3> dNxyz;
            double vol;
            if (!tet4_grad(ep, dNxyz, vol)) continue;
            // Constant gradients -> Ke = k * vol * (dN)(dN)^T.
            Eigen::Matrix<double, 4, 4> Ke = (dNxyz * dNxyz.transpose()) * (k_cond * vol);
            for (int a = 0; a < 4; ++a)
                for (int b = 0; b < 4; ++b) add_KK(tn[a], tn[b], Ke(a, b));
        }
    }

    // ── Surface faces: flux load + convection (film) matrix & load ──────────────────────
    // A boundary triangle is enumerated once per (element,local-face) whose CORNER nodes are
    // all inside the target node set. We add:
    //   flux:        f_i += q · ∫N_i dS
    //   convection:  K_ij += h · ∫N_i N_j dS ; f_i += h·T_amb · ∫N_i dS
    auto surface_pass = [&](const std::unordered_set<int>& node_set, bool do_flux, double q,
                            bool do_conv, double h, double t_amb) {
        for (int e = 0; e < n_elems; ++e) {
            const int* el = &tets[e * stride];
            for (int fidx = 0; fidx < 4; ++fidx) {
                const FaceTab& ft = kTetFaces[fidx];
                int cn[3];
                bool all_in = true;
                for (int i = 0; i < 3; ++i) {
                    cn[i] = el[ft.corner[i]];
                    if (!node_set.count(cn[i])) { all_in = false; break; }
                }
                if (!all_in) continue;
                const double A = tri_area(node_pt(cn[0]), node_pt(cn[1]), node_pt(cn[2]));
                if (A < 1e-12) continue;
                // Local node ids on this face: corners then (tet10) mid-edge nodes.
                int fn[6] = {cn[0], cn[1], cn[2], -1, -1, -1};
                if (tet10) for (int i = 0; i < 3; ++i) fn[3 + i] = el[ft.mid[i]];
                const int nfn = tet10 ? 6 : 3;
                double wc[3], wm[3];
                tri_load_weights(tet10, A, wc, wm);
                const double w[6] = {wc[0], wc[1], wc[2], wm[0], wm[1], wm[2]};
                if (do_flux)
                    for (int i = 0; i < nfn; ++i) {
                        const int ri = reduced[fn[i]];
                        if (ri >= 0) f(ri) += q * w[i];
                    }
                if (do_conv) {
                    Eigen::Matrix<double, 6, 6> Ms;
                    tri_surface_mass(tet10, A, Ms);
                    for (int i = 0; i < nfn; ++i) {
                        // convection load h·T_amb·∫N_i dS
                        const int ri = reduced[fn[i]];
                        if (ri >= 0) f(ri) += h * t_amb * w[i];
                        // convection matrix h·∫N_i N_j dS
                        for (int j = 0; j < nfn; ++j) add_KK(fn[i], fn[j], h * Ms(i, j));
                    }
                }
            }
        }
    };

    if (!flux_nodes.empty() && std::isfinite(flux_q) && flux_q != 0.0) {
        std::unordered_set<int> fs(flux_nodes.begin(), flux_nodes.end());
        surface_pass(fs, /*do_flux*/ true, flux_q, /*do_conv*/ false, 0.0, 0.0);
    }
    for (const auto& cv : convections) {
        if (cv.h <= 0.0 || cv.nodes.empty() || !std::isfinite(cv.h) || !std::isfinite(cv.t_amb))
            continue;
        std::unordered_set<int> cs(cv.nodes.begin(), cv.nodes.end());
        surface_pass(cs, /*do_flux*/ false, 0.0, /*do_conv*/ true, cv.h, cv.t_amb);
    }

    // ── Assemble + solve the reduced SPD system Kff * Tf = f ─────────────────────────────
    Eigen::SparseMatrix<double> Kff(n_free, n_free);
    Kff.setFromTriplets(trips.begin(), trips.end());
    Kff.makeCompressed();
    if (Kff.nonZeros() == 0) {
        res.error = "fea_thermal: no valid elements contributed conductivity";
        return res;
    }

    Eigen::VectorXd Tf;
    bool solved = false;
    {
        Eigen::SimplicialLDLT<Eigen::SparseMatrix<double>> ldlt;
        ldlt.compute(Kff);
        if (ldlt.info() == Eigen::Success) {
            Tf = ldlt.solve(f);
            if (ldlt.info() == Eigen::Success) solved = true;
        }
    }
    if (!solved) {
        Eigen::SparseLU<Eigen::SparseMatrix<double>> lu;
        lu.analyzePattern(Kff);
        lu.factorize(Kff);
        if (lu.info() != Eigen::Success) {
            res.error = "fea_thermal: conductivity factorization failed (singular system — "
                        "check that Dirichlet/convection BCs anchor the temperature datum)";
            return res;
        }
        Tf = lu.solve(f);
        if (lu.info() != Eigen::Success) { res.error = "fea_thermal: sparse solve failed"; return res; }
    }

    // Scatter back to the full field (fixed nodes carry their prescribed temperature).
    res.temperature.assign(n_nodes, 0.0);
    for (int i = 0; i < n_nodes; ++i)
        res.temperature[i] = is_fixed[i] ? pres_temp[i] : Tf(reduced[i]);
    for (double v : res.temperature)
        if (!std::isfinite(v)) { res.error = "fea_thermal: solve produced a non-finite temperature"; return res; }

    double mx = res.temperature[0], mn = res.temperature[0];
    for (double v : res.temperature) { mx = std::max(mx, v); mn = std::min(mn, v); }
    res.max_temp = mx;
    res.min_temp = mn;
    res.n_dof = n_free;
    res.ok = true;
    return res;
}

}  // namespace aicad::sim
