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

#include "sim/solver_threads.h"

#include <algorithm>
#include <array>
#include <cmath>
#include <unordered_set>

#include <Eigen/Dense>
#include <Eigen/SparseCholesky>
#include <Eigen/SparseCore>
#include <Eigen/SparseLU>
#ifdef CHUDWARE_WITH_CHOLMOD
#include <Eigen/CholmodSupport>  // Eigen::CholmodSupernodalLLT (SuiteSparse BLAS3 Cholesky)
#endif

namespace chudware::sim {

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
                                const std::vector<ThermalFluxLoad>& fluxes,
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

    // Each flux load contributes its OWN surface pass with its OWN magnitude — superposition
    // over independent sources (the previous single-set/single-q form overwrote q when a
    // study carried more than one flux load; see the C3 defect note in the header).
    for (const auto& fl : fluxes) {
        if (fl.nodes.empty() || !std::isfinite(fl.q) || fl.q == 0.0) continue;
        std::unordered_set<int> fs(fl.nodes.begin(), fl.nodes.end());
        surface_pass(fs, /*do_flux*/ true, fl.q, /*do_conv*/ false, 0.0, 0.0);
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
#ifdef CHUDWARE_WITH_CHOLMOD
    // P2: CHOLMOD supernodal LLT first — BLAS3 + multithreaded, 3-15x the scalar
    // single-threaded SimplicialLDLT on real conduction systems. Kff is SPD (reduced
    // Dirichlet system, or Neumann + convection surface terms anchoring the datum);
    // if CHOLMOD's strict-SPD LLT rejects a near-singular system we fall through to
    // the LDLT → SparseLU ladder exactly as before.
    {
        cap_solver_threads_for_host();
        Eigen::CholmodSupernodalLLT<Eigen::SparseMatrix<double>> chol;
        chol.compute(Kff);
        if (chol.info() == Eigen::Success) {
            Tf = chol.solve(f);
            if (chol.info() == Eigen::Success) solved = true;
        }
    }
#endif
    if (!solved) {
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

// Legacy single-flux entry point: one node set, one magnitude. Delegates to the per-load
// overload with exactly one ThermalFluxLoad, so results are bit-for-bit identical to the
// historical behaviour (same surface pass, same guards).
ThermalResult fea_thermal_solve(const std::vector<double>& points, const std::vector<int>& tets,
                                double k_cond, const std::vector<int>& fixed_nodes,
                                const std::vector<double>& fixed_temps,
                                const std::vector<int>& flux_nodes, double flux_q,
                                const std::vector<ThermalConvection>& convections,
                                int nodes_per_elem) {
    std::vector<ThermalFluxLoad> fluxes;
    if (!flux_nodes.empty()) {
        ThermalFluxLoad fl;
        fl.nodes = flux_nodes;
        fl.q = flux_q;
        fluxes.push_back(std::move(fl));
    }
    return fea_thermal_solve(points, tets, k_cond, fixed_nodes, fixed_temps, fluxes,
                             convections, nodes_per_elem);
}

// ═════════════════════════════ Transient (T-FIN18) ═══════════════════════════════════════
//
// NOTE ON DUPLICATION: the conductivity/surface assembly below intentionally repeats the
// steady solver's loops instead of refactoring them into a shared helper. The steady path
// is under a HARD bit-for-bit regression gate; keeping fea_thermal_solve() byte-identical
// (same triplet emission order, same summation order) is worth ~100 duplicated lines.

namespace {

// Element capacitance matrices (constant-Jacobian straight tets; mid-edge nodes at edge
// midpoints — what both the box promoter and the mesher produce; curved boundary tet10s
// see a small, mesh-converging approximation).
//
// tet4 consistent: C = rho_cp·V/20 · (1+δij). Row-sum lumped: rho_cp·V/4 per node.
//
// tet10: shape functions are quadratics in the volume coordinates L0..L3, so every entry
// ∫ N_i N_j dV is an exact volume-coordinate moment  ∫ L0^a L1^b L2^c L3^d dV =
// 6V·a!b!c!d!/(a+b+c+d+3)!  — evaluated below by expanding each N as monomial terms
// (corner: 2L² − L; mid(a,b): 4·La·Lb). No quadrature, no tabulated magic matrix.
//
// tet10 LUMPED uses HRZ (diagonal scaling): row-sum lumping fails for tet10 because
// ∫N_corner dV = −V/20 < 0. HRZ: lump_i = diag_i · V/Σdiag. With diag_corner = ∫N_c² = V/70
// and diag_mid = ∫N_m² = 8V/105, Σdiag = 18V/35, so lump_corner = V/36, lump_mid = 4V/27
// (checks: 4·V/36 + 6·4V/27 = V).
struct Tet10Term {
    double coef;
    int e[4];  // exponents of L0..L3
};

std::vector<Tet10Term> tet10_shape_terms(int n) {
    if (n < 4) {
        Tet10Term a{2.0, {0, 0, 0, 0}}, b{-1.0, {0, 0, 0, 0}};
        a.e[n] = 2;
        b.e[n] = 1;
        return {a, b};
    }
    static const int LE[6][2] = {{0, 1}, {1, 2}, {2, 0}, {3, 0}, {3, 2}, {3, 1}};
    Tet10Term m{4.0, {0, 0, 0, 0}};
    m.e[LE[n - 4][0]] += 1;
    m.e[LE[n - 4][1]] += 1;
    return {m};
}

// ∫ L0^a L1^b L2^c L3^d dV over a straight tet of volume V.
double tet_moment(int a, int b, int c, int d, double V) {
    auto fact = [](int n) {
        double f = 1.0;
        for (int i = 2; i <= n; ++i) f *= i;
        return f;
    };
    return 6.0 * V * fact(a) * fact(b) * fact(c) * fact(d) / fact(a + b + c + d + 3);
}

// Consistent tet10 capacitance/V template  T_ij = (1/V) ∫ N_i N_j dV, computed once.
const Eigen::Matrix<double, 10, 10>& tet10_mass_template() {
    static const Eigen::Matrix<double, 10, 10> T = [] {
        Eigen::Matrix<double, 10, 10> M;
        for (int i = 0; i < 10; ++i) {
            const auto ti = tet10_shape_terms(i);
            for (int j = i; j < 10; ++j) {
                const auto tj = tet10_shape_terms(j);
                double v = 0.0;
                for (const auto& a : ti)
                    for (const auto& b : tj)
                        v += a.coef * b.coef *
                             tet_moment(a.e[0] + b.e[0], a.e[1] + b.e[1], a.e[2] + b.e[2],
                                        a.e[3] + b.e[3], 1.0);
                M(i, j) = v;
                M(j, i) = v;
            }
        }
        return M;
    }();
    return T;
}

}  // namespace

double thermal_schedule_scale(const ThermalSchedule& sched, double t) {
    if (sched.times.empty()) return 1.0;
    double tt = t;
    if (sched.period > 0.0) {
        tt = std::fmod(t, sched.period);
        if (tt < 0.0) tt += sched.period;
    }
    // Hold the scale of the last breakpoint <= tt (times start at 0, ascending).
    double s = sched.scales.front();
    for (size_t i = 0; i < sched.times.size(); ++i) {
        if (sched.times[i] <= tt + 1e-12) s = sched.scales[i];
        else break;
    }
    return s;
}

ThermalTransientResult fea_thermal_transient(
    const std::vector<double>& points, const std::vector<int>& tets, double k_cond,
    const std::vector<int>& fixed_nodes, const std::vector<double>& fixed_temps,
    const std::vector<ThermalFluxLoad>& fluxes, const std::vector<ThermalConvection>& convections,
    const ThermalTransientOptions& opts, int nodes_per_elem) {
    ThermalTransientResult res;
    const int n_nodes = static_cast<int>(points.size() / 3);
    res.n_nodes = n_nodes;
    if (n_nodes == 0 || tets.empty()) { res.error = "fea_thermal_transient: empty mesh"; return res; }
    if (points.size() % 3 != 0) {
        res.error = "fea_thermal_transient: points length must be divisible by 3";
        return res;
    }
    if (nodes_per_elem != 4 && nodes_per_elem != 10) {
        res.error = "fea_thermal_transient: nodes_per_elem must be 4 (tet4) or 10 (tet10)";
        return res;
    }
    if (tets.size() % static_cast<size_t>(nodes_per_elem) != 0) {
        res.error = "fea_thermal_transient: tets length must be divisible by nodes_per_elem";
        return res;
    }
    if (!std::isfinite(k_cond) || k_cond <= 0.0) {
        res.error = "fea_thermal_transient: conductivity k must be finite and > 0";
        return res;
    }
    if (!std::isfinite(opts.rho_cp) || opts.rho_cp <= 0.0) {
        res.error = "fea_thermal_transient: volumetric heat capacity rho_cp must be > 0";
        return res;
    }
    if (!std::isfinite(opts.dt) || opts.dt <= 0.0) {
        res.error = "fea_thermal_transient: time step dt must be > 0";
        return res;
    }
    if (!std::isfinite(opts.t_end) || opts.t_end < opts.dt) {
        res.error = "fea_thermal_transient: t_end must be >= dt";
        return res;
    }
    const long long n_steps_ll = static_cast<long long>(std::ceil(opts.t_end / opts.dt - 1e-9));
    if (n_steps_ll > 1000000) {
        res.error = "fea_thermal_transient: t_end/dt exceeds 1e6 steps — coarsen dt";
        return res;
    }
    if (fixed_nodes.size() != fixed_temps.size()) {
        res.error = "fea_thermal_transient: fixed_nodes and fixed_temps length mismatch";
        return res;
    }
    if (!opts.init_temps.empty() && opts.init_temps.size() != static_cast<size_t>(n_nodes)) {
        res.error = "fea_thermal_transient: init_temps must have one entry per node";
        return res;
    }
    for (double x : points)
        if (!std::isfinite(x)) { res.error = "fea_thermal_transient: mesh coordinates must be finite"; return res; }
    for (int nid : tets)
        if (nid < 0 || nid >= n_nodes) { res.error = "fea_thermal_transient: tet node index out of range"; return res; }
    for (const auto& fl : fluxes) {
        if (fl.schedule.times.size() != fl.schedule.scales.size()) {
            res.error = "fea_thermal_transient: schedule times/scales length mismatch";
            return res;
        }
        for (size_t i = 1; i < fl.schedule.times.size(); ++i)
            if (!(fl.schedule.times[i] > fl.schedule.times[i - 1])) {
                res.error = "fea_thermal_transient: schedule times must be strictly ascending";
                return res;
            }
        if (!fl.schedule.times.empty() && fl.schedule.times.front() != 0.0) {
            res.error = "fea_thermal_transient: schedule times must start at 0";
            return res;
        }
    }

    const bool tet10 = (nodes_per_elem == 10);
    const int stride = nodes_per_elem;
    const int n_elems = static_cast<int>(tets.size() / stride);
    res.n_elems = n_elems;

    auto node_pt = [&](int nid) {
        return Vec3(points[nid * 3], points[nid * 3 + 1], points[nid * 3 + 2]);
    };

    // Dirichlet map + free-DOF numbering (constant-in-time prescribed temperatures).
    std::vector<char> is_fixed(n_nodes, 0);
    std::vector<double> pres_temp(n_nodes, 0.0);
    for (size_t i = 0; i < fixed_nodes.size(); ++i) {
        const int nid = fixed_nodes[i];
        if (nid < 0 || nid >= n_nodes) { res.error = "fea_thermal_transient: fixed node out of range"; return res; }
        if (!std::isfinite(fixed_temps[i])) { res.error = "fea_thermal_transient: fixed temperature not finite"; return res; }
        is_fixed[nid] = 1;
        pres_temp[nid] = fixed_temps[i];
    }
    std::vector<int> reduced(n_nodes, -1);
    int n_free = 0;
    for (int i = 0; i < n_nodes; ++i)
        if (!is_fixed[i]) reduced[i] = n_free++;
    res.n_dof = n_free;

    // ── Assembly: Kff + constant base load (Dirichlet move + convection), capacitance C,
    // and one load vector PER scheduled flux (so only scale factors change per step).
    std::vector<Eigen::Triplet<double>> ktrips;
    std::vector<Eigen::Triplet<double>> ctrips;                 // consistent C only
    Eigen::VectorXd c_lump = Eigen::VectorXd::Zero(n_free);     // lumped C diagonal
    Eigen::VectorXd f_base = Eigen::VectorXd::Zero(n_free);
    const TetGauss4 gauss;

    auto add_K = [&](int a_node, int b_node, double kab) {
        const int ra = reduced[a_node];
        if (ra < 0) return;
        const int rb = reduced[b_node];
        if (rb >= 0) ktrips.emplace_back(ra, rb, kab);
        else f_base(ra) -= kab * pres_temp[b_node];
    };
    // Capacitance couplings to fixed nodes vanish (C multiplies dT/dt and prescribed
    // temperatures are constant), so only the free-free block is kept.
    auto add_C = [&](int a_node, int b_node, double cab) {
        const int ra = reduced[a_node];
        const int rb = reduced[b_node];
        if (ra < 0 || rb < 0) return;
        if (opts.lumped) { if (ra == rb) c_lump(ra) += cab; }
        else ctrips.emplace_back(ra, rb, cab);
    };

    if (tet10) {
        const auto& Tm = tet10_mass_template();
        for (int e = 0; e < n_elems; ++e) {
            std::array<Vec3, 10> ep;
            std::array<int, 10> tn;
            for (int j = 0; j < 10; ++j) { tn[j] = tets[e * 10 + j]; ep[j] = node_pt(tn[j]); }
            Eigen::Matrix<double, 10, 10> Ke = Eigen::Matrix<double, 10, 10>::Zero();
            bool ok_elem = true;
            double vol6 = 0.0;  // Σ|detJ|·w over the rule = element volume
            for (int g = 0; g < 4; ++g) {
                Eigen::Matrix<double, 10, 3> dNxyz;
                double detJ;
                if (!tet10_grad_at(ep, gauss.pt[g][0], gauss.pt[g][1], gauss.pt[g][2], dNxyz, detJ)) {
                    ok_elem = false;
                    break;
                }
                Ke += (dNxyz * dNxyz.transpose()) * (k_cond * std::abs(detJ) * gauss.w[g]);
                vol6 += std::abs(detJ) * gauss.w[g];
            }
            if (!ok_elem) continue;
            const double V = vol6;
            for (int a = 0; a < 10; ++a)
                for (int b = 0; b < 10; ++b) add_K(tn[a], tn[b], Ke(a, b));
            if (opts.lumped) {
                // HRZ diagonal (derivation in the comment above): corners V/36, mids 4V/27.
                for (int a = 0; a < 10; ++a)
                    add_C(tn[a], tn[a], opts.rho_cp * V * (a < 4 ? 1.0 / 36.0 : 4.0 / 27.0));
            } else {
                for (int a = 0; a < 10; ++a)
                    for (int b = 0; b < 10; ++b) add_C(tn[a], tn[b], opts.rho_cp * V * Tm(a, b));
            }
        }
    } else {
        for (int e = 0; e < n_elems; ++e) {
            std::array<Vec3, 4> ep;
            std::array<int, 4> tn;
            for (int j = 0; j < 4; ++j) { tn[j] = tets[e * 4 + j]; ep[j] = node_pt(tn[j]); }
            Eigen::Matrix<double, 4, 3> dNxyz;
            double vol;
            if (!tet4_grad(ep, dNxyz, vol)) continue;
            Eigen::Matrix<double, 4, 4> Ke = (dNxyz * dNxyz.transpose()) * (k_cond * vol);
            for (int a = 0; a < 4; ++a)
                for (int b = 0; b < 4; ++b) add_K(tn[a], tn[b], Ke(a, b));
            if (opts.lumped) {
                for (int a = 0; a < 4; ++a) add_C(tn[a], tn[a], opts.rho_cp * vol / 4.0);
            } else {
                for (int a = 0; a < 4; ++a)
                    for (int b = 0; b < 4; ++b)
                        add_C(tn[a], tn[b], opts.rho_cp * vol / 20.0 * (a == b ? 2.0 : 1.0));
            }
        }
    }

    // Surface terms. Convection is time-constant here (matrix into K, load into f_base);
    // each flux load gets its OWN consistent load vector for per-step scaling.
    auto surface_flux_vec = [&](const std::vector<int>& nodes, double q) {
        Eigen::VectorXd fv = Eigen::VectorXd::Zero(n_free);
        std::unordered_set<int> node_set(nodes.begin(), nodes.end());
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
                int fn[6] = {cn[0], cn[1], cn[2], -1, -1, -1};
                if (tet10) for (int i = 0; i < 3; ++i) fn[3 + i] = el[ft.mid[i]];
                const int nfn = tet10 ? 6 : 3;
                double wc[3], wm[3];
                tri_load_weights(tet10, A, wc, wm);
                const double w[6] = {wc[0], wc[1], wc[2], wm[0], wm[1], wm[2]};
                for (int i = 0; i < nfn; ++i) {
                    const int ri = reduced[fn[i]];
                    if (ri >= 0) fv(ri) += q * w[i];
                }
            }
        }
        return fv;
    };

    for (const auto& cv : convections) {
        if (cv.h <= 0.0 || cv.nodes.empty() || !std::isfinite(cv.h) || !std::isfinite(cv.t_amb))
            continue;
        std::unordered_set<int> cs(cv.nodes.begin(), cv.nodes.end());
        for (int e = 0; e < n_elems; ++e) {
            const int* el = &tets[e * stride];
            for (int fidx = 0; fidx < 4; ++fidx) {
                const FaceTab& ft = kTetFaces[fidx];
                int cn[3];
                bool all_in = true;
                for (int i = 0; i < 3; ++i) {
                    cn[i] = el[ft.corner[i]];
                    if (!cs.count(cn[i])) { all_in = false; break; }
                }
                if (!all_in) continue;
                const double A = tri_area(node_pt(cn[0]), node_pt(cn[1]), node_pt(cn[2]));
                if (A < 1e-12) continue;
                int fn[6] = {cn[0], cn[1], cn[2], -1, -1, -1};
                if (tet10) for (int i = 0; i < 3; ++i) fn[3 + i] = el[ft.mid[i]];
                const int nfn = tet10 ? 6 : 3;
                double wc[3], wm[3];
                tri_load_weights(tet10, A, wc, wm);
                const double w[6] = {wc[0], wc[1], wc[2], wm[0], wm[1], wm[2]};
                Eigen::Matrix<double, 6, 6> Ms;
                tri_surface_mass(tet10, A, Ms);
                for (int i = 0; i < nfn; ++i) {
                    const int ri = reduced[fn[i]];
                    if (ri >= 0) f_base(ri) += cv.h * cv.t_amb * w[i];
                    for (int j = 0; j < nfn; ++j) add_K(fn[i], fn[j], cv.h * Ms(i, j));
                }
            }
        }
    }

    std::vector<Eigen::VectorXd> flux_vecs;
    flux_vecs.reserve(fluxes.size());
    for (const auto& fl : fluxes) {
        if (fl.nodes.empty() || !std::isfinite(fl.q) || fl.q == 0.0)
            flux_vecs.emplace_back(Eigen::VectorXd::Zero(n_free));
        else
            flux_vecs.push_back(surface_flux_vec(fl.nodes, fl.q));
    }

    // ── Initial field ─────────────────────────────────────────────────────────────────────
    std::vector<double> T_full(n_nodes, opts.t_init);
    if (!opts.init_temps.empty()) T_full = opts.init_temps;
    for (int i = 0; i < n_nodes; ++i)
        if (is_fixed[i]) T_full[i] = pres_temp[i];

    res.envelope_max = T_full;
    double gmax = T_full[0], gmin = T_full[0], gmax_t = 0.0;
    for (double v : T_full) { gmax = std::max(gmax, v); gmin = std::min(gmin, v); }

    // Requested frame times, sorted; t=0 records the initial field.
    std::vector<double> want = opts.frame_times;
    std::sort(want.begin(), want.end());
    size_t want_i = 0;
    while (want_i < want.size() && want[want_i] <= 0.0) {
        res.frames.push_back({0.0, T_full});
        // collapse duplicate <=0 requests into one initial frame
        while (want_i < want.size() && want[want_i] <= 0.0) ++want_i;
    }

    const int n_steps = static_cast<int>(n_steps_ll);
    res.n_steps = n_steps;

    if (n_free == 0) {
        // Fully prescribed field: nothing evolves; frames are the Dirichlet data.
        for (; want_i < want.size(); ++want_i) res.frames.push_back({want[want_i], T_full});
        if (res.frames.empty() || res.frames.back().time != opts.t_end)
            res.frames.push_back({opts.t_end, T_full});
        res.max_temp = gmax; res.min_temp = gmin; res.max_temp_time = 0.0;
        res.ok = true;
        return res;
    }

    // ── Iteration matrix A = Kff + Cff/dt, factored ONCE (Cholesky → SparseLU fallback,
    // matching the steady solver's convention). Fixed dt + constant material + constant
    // Dirichlet data keep A constant for the whole run even under scheduled loads.
    Eigen::SparseMatrix<double> A(n_free, n_free);
    {
        std::vector<Eigen::Triplet<double>> atrips = ktrips;
        if (opts.lumped) {
            for (int i = 0; i < n_free; ++i)
                if (c_lump(i) != 0.0) atrips.emplace_back(i, i, c_lump(i) / opts.dt);
        } else {
            for (const auto& t : ctrips)
                atrips.emplace_back(t.row(), t.col(), t.value() / opts.dt);
        }
        A.setFromTriplets(atrips.begin(), atrips.end());
        A.makeCompressed();
    }
    if (A.nonZeros() == 0) {
        res.error = "fea_thermal_transient: no valid elements contributed";
        return res;
    }
    Eigen::SparseMatrix<double> Cff(n_free, n_free);
    if (!opts.lumped) {
        Cff.setFromTriplets(ctrips.begin(), ctrips.end());
        Cff.makeCompressed();
    }

    // P2: factor A once with the fastest available ladder — CHOLMOD supernodal LLT
    // (BLAS3, multithreaded; A = Kff + Cff/dt is SPD) → SimplicialLDLT → SparseLU.
    // The one factorization is amortized over EVERY backward-Euler step, so this is
    // where the transient solver's wall-clock actually lives.
#ifdef CHUDWARE_WITH_CHOLMOD
    Eigen::CholmodSupernodalLLT<Eigen::SparseMatrix<double>> chol;
    bool use_chol = false;
    cap_solver_threads_for_host();
    chol.compute(A);
    if (chol.info() == Eigen::Success) use_chol = true;
#else
    constexpr bool use_chol = false;
#endif
    Eigen::SimplicialLDLT<Eigen::SparseMatrix<double>> ldlt;
    Eigen::SparseLU<Eigen::SparseMatrix<double>> lu;
    bool use_ldlt = false;
    if (!use_chol) {
        ldlt.compute(A);
        if (ldlt.info() == Eigen::Success) {
            use_ldlt = true;
        } else {
            lu.analyzePattern(A);
            lu.factorize(A);
            if (lu.info() != Eigen::Success) {
                res.error = "fea_thermal_transient: iteration-matrix factorization failed";
                return res;
            }
        }
    }

    Eigen::VectorXd Tf(n_free);
    for (int i = 0; i < n_nodes; ++i)
        if (reduced[i] >= 0) Tf(reduced[i]) = T_full[i];

    for (int n = 1; n <= n_steps; ++n) {
        const double t_next = std::min(static_cast<double>(n) * opts.dt, opts.t_end);
        Eigen::VectorXd rhs = f_base;
        if (opts.lumped) rhs += (c_lump.array() * Tf.array()).matrix() / opts.dt;
        else rhs += (Cff * Tf) / opts.dt;
        for (size_t k = 0; k < fluxes.size(); ++k) {
            const double s = thermal_schedule_scale(fluxes[k].schedule, t_next);
            if (s != 0.0) rhs += s * flux_vecs[k];
        }
        bool step_ok;
#ifdef CHUDWARE_WITH_CHOLMOD
        if (use_chol) {
            Tf = chol.solve(rhs);
            step_ok = (chol.info() == Eigen::Success);
        } else
#endif
        if (use_ldlt) {
            Tf = ldlt.solve(rhs);
            step_ok = (ldlt.info() == Eigen::Success);
        } else {
            Tf = lu.solve(rhs);
            step_ok = (lu.info() == Eigen::Success);
        }
        if (!step_ok) {
            res.error = "fea_thermal_transient: sparse solve failed at step " + std::to_string(n);
            return res;
        }
        if (!Tf.allFinite()) {
            res.error = "fea_thermal_transient: non-finite temperature at step " + std::to_string(n);
            return res;
        }
        for (int i = 0; i < n_nodes; ++i)
            if (reduced[i] >= 0) T_full[i] = Tf(reduced[i]);
        for (int i = 0; i < n_nodes; ++i) {
            if (T_full[i] > res.envelope_max[i]) res.envelope_max[i] = T_full[i];
            if (T_full[i] > gmax) { gmax = T_full[i]; gmax_t = t_next; }
            if (T_full[i] < gmin) gmin = T_full[i];
        }
        const bool last = (n == n_steps);
        bool record = last;
        while (want_i < want.size() && want[want_i] <= t_next + 1e-12) {
            record = true;
            ++want_i;
        }
        if (record) res.frames.push_back({t_next, T_full});
    }

    res.max_temp = gmax;
    res.min_temp = gmin;
    res.max_temp_time = gmax_t;
    res.ok = true;
    return res;
}

}  // namespace chudware::sim
