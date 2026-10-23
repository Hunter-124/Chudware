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
#include <limits>
#include <map>
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
using TriFaceKey = std::array<int, 3>;

TriFaceKey tri_face_key(const int* el, const FaceTab& ft) {
    TriFaceKey key{el[ft.corner[0]], el[ft.corner[1]], el[ft.corner[2]]};
    std::sort(key.begin(), key.end());
    return key;
}

std::map<TriFaceKey, int> face_owner_counts(const std::vector<int>& tets, int stride) {
    std::map<TriFaceKey, int> counts;
    const int n_elems = static_cast<int>(tets.size() / static_cast<size_t>(stride));
    for (int e = 0; e < n_elems; ++e) {
        const int* el = &tets[static_cast<size_t>(e) * stride];
        for (const FaceTab& ft : kTetFaces) ++counts[tri_face_key(el, ft)];
    }
    return counts;
}

int selected_exterior_faces(const std::vector<int>& tets, int stride,
                            const std::map<TriFaceKey, int>& owners,
                            const std::unordered_set<int>& nodes,
                            const std::vector<std::size_t>& exterior_face_ids) {
    const std::size_t n_elems = tets.size() / static_cast<std::size_t>(stride);
    if (!exterior_face_ids.empty()) {
        if (!std::is_sorted(exterior_face_ids.begin(), exterior_face_ids.end()) ||
            std::adjacent_find(exterior_face_ids.begin(), exterior_face_ids.end()) !=
                exterior_face_ids.end())
            return -1;
        for (const std::size_t id : exterior_face_ids) {
            if (id / 4 >= n_elems) return -1;
            const int* el = &tets[(id / 4) * static_cast<std::size_t>(stride)];
            const FaceTab& ft = kTetFaces[id % 4];
            const auto own = owners.find(tri_face_key(el, ft));
            if (own == owners.end() || own->second != 1) return -1;
            for (int i = 0; i < 3; ++i)
                if (!nodes.count(el[ft.corner[i]])) return -1;
        }
        return static_cast<int>(exterior_face_ids.size());
    }
    int selected = 0;
    for (std::size_t e = 0; e < n_elems; ++e) {
        const int* el = &tets[e * static_cast<std::size_t>(stride)];
        for (const FaceTab& ft : kTetFaces) {
            const auto own = owners.find(tri_face_key(el, ft));
            if (own == owners.end() || own->second != 1) continue;
            bool all = true;
            for (int i = 0; i < 3; ++i) all = all && nodes.count(el[ft.corner[i]]);
            if (all) ++selected;
        }
    }
    return selected;
}

// Surface integrals for a linear tri3 or quadratic isoparametric tri6:
//   weights_i = ∫N_i dS, mass_ij = ∫N_i N_j dS.
// A curved tri6 has a varying surface Jacobian, so the flat-face closed form is wrong.
// A 5x5 Gauss-Legendre Duffy rule integrates the mapped face deterministically; it reduces
// to the standard tri3/flat-tri6 values to roundoff.
bool tri_surface_terms(const std::array<Vec3, 6>& p, bool tet10,
                       Eigen::Matrix<double, 6, 1>& weights,
                       Eigen::Matrix<double, 6, 6>& mass, double& area) {
    weights.setZero();
    mass.setZero();
    area = 0.0;
    if (!tet10) {
        area = 0.5 * ((p[1] - p[0]).cross(p[2] - p[0])).norm();
        if (!(area > 0.0) || !std::isfinite(area)) return false;
        weights.head<3>().setConstant(area / 3.0);
        for (int i = 0; i < 3; ++i)
            for (int j = 0; j < 3; ++j)
                mass(i, j) = area / 12.0 * (i == j ? 2.0 : 1.0);
        return true;
    }
    static constexpr double x[5] = {
        0.0469100770306680, 0.2307653449471585, 0.5,
        0.7692346550528415, 0.9530899229693320};
    static constexpr double w[5] = {
        0.1184634425280945, 0.2393143352496832, 0.2844444444444444,
        0.2393143352496832, 0.1184634425280945};
    for (int iu = 0; iu < 5; ++iu)
        for (int iv = 0; iv < 5; ++iv) {
            const double r = x[iu];
            const double s = (1.0 - r) * x[iv];
            const double L0 = 1.0 - r - s;
            Eigen::Matrix<double, 6, 1> N;
            N << L0 * (2.0 * L0 - 1.0), r * (2.0 * r - 1.0),
                s * (2.0 * s - 1.0), 4.0 * L0 * r, 4.0 * r * s,
                4.0 * s * L0;
            Eigen::Matrix<double, 6, 1> dNr, dNs;
            dNr << 1.0 - 4.0 * L0, 4.0 * r - 1.0, 0.0,
                4.0 * (L0 - r), 4.0 * s, -4.0 * s;
            dNs << 1.0 - 4.0 * L0, 0.0, 4.0 * s - 1.0,
                -4.0 * r, 4.0 * r, 4.0 * (L0 - s);
            Vec3 xr = Vec3::Zero(), xs = Vec3::Zero();
            for (int i = 0; i < 6; ++i) {
                xr += p[i] * dNr(i);
                xs += p[i] * dNs(i);
            }
            const double jac = xr.cross(xs).norm();
            if (!(jac > 0.0) || !std::isfinite(jac)) return false;
            const double dS =
                jac * (1.0 - r) * w[iu] * w[iv];
            area += dS;
            weights.noalias() += dS * N;
            mass.noalias() += dS * (N * N.transpose());
        }
    return std::isfinite(area) && area > 0.0 && weights.allFinite() &&
           mass.allFinite();
}

}  // namespace

ThermalResult fea_thermal_solve(const std::vector<double>& points, const std::vector<int>& tets,
                                double k_cond, const std::vector<int>& fixed_nodes,
                                const std::vector<double>& fixed_temps,
                                const std::vector<ThermalFluxLoad>& fluxes,
                                const std::vector<ThermalConvection>& convections,
                                int nodes_per_elem, const SimCancelToken& cancel_token) {
    ThermalResult res;
    const int n_nodes = static_cast<int>(points.size() / 3);
    res.n_nodes = n_nodes;
    auto cancelled = [&]() {
        return cancel_token && cancel_token->load(std::memory_order_relaxed);
    };
    auto cancel_result = [&]() {
        res.error = "fea_thermal: cancelled";
        return res;
    };
    if (cancelled()) return cancel_result();
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
    const auto face_owners = face_owner_counts(tets, stride);
    for (const auto& fl : fluxes) {
        if (!std::isfinite(fl.q) || (!fl.nodes.empty() && fl.q == 0.0)) {
            res.error =
                "fea_thermal: heat flux must be finite and non-zero when selected";
            return res;
        }
        for (int nid : fl.nodes)
            if (nid < 0 || nid >= n_nodes) {
                res.error = "fea_thermal: flux node index out of range";
                return res;
            }
        if (!fl.nodes.empty() || !fl.exterior_face_ids.empty()) {
            const std::unordered_set<int> ns(fl.nodes.begin(), fl.nodes.end());
            if (selected_exterior_faces(tets, stride, face_owners, ns, fl.exterior_face_ids) <= 0) {
                res.error = "fea_thermal: flux selection contains no exterior mesh face";
                return res;
            }
        }
    }
    for (const auto& cv : convections) {
        if (!std::isfinite(cv.h) || cv.h <= 0.0 || !std::isfinite(cv.t_amb)) {
            res.error = "fea_thermal: convection requires finite h>0 and ambient temperature";
            return res;
        }
        for (int nid : cv.nodes)
            if (nid < 0 || nid >= n_nodes) {
                res.error = "fea_thermal: convection node index out of range";
                return res;
            }
        if (cv.nodes.empty()) {
            res.error = "fea_thermal: convection node set is empty";
            return res;
        }
        const std::unordered_set<int> ns(cv.nodes.begin(), cv.nodes.end());
        if (selected_exterior_faces(tets, stride, face_owners, ns, cv.exterior_face_ids) <= 0) {
            res.error = "fea_thermal: convection selection contains no exterior mesh face";
            return res;
        }
    }
    // Validate the complete domain even when every temperature DOF is prescribed; a
    // fully-prescribed field must not turn a degenerate mesh into an apparent success.
    const TetGauss4 preflight_gauss;
    for (int e = 0; e < n_elems; ++e) {
        if (tet10) {
            std::array<Vec3, 10> ep;
            for (int j = 0; j < 10; ++j) ep[j] = node_pt(tets[e * 10 + j]);
            double sign = 0.0;
            for (int g = 0; g < 4; ++g) {
                Eigen::Matrix<double, 10, 3> grad;
                double detJ = 0.0;
                if (!tet10_grad_at(ep, preflight_gauss.pt[g][0],
                                   preflight_gauss.pt[g][1],
                                   preflight_gauss.pt[g][2], grad, detJ) ||
                    !std::isfinite(detJ) || (sign != 0.0 && sign * detJ <= 0.0)) {
                    res.error = "fea_thermal: degenerate or inverted element " +
                                std::to_string(e);
                    return res;
                }
                sign = detJ;
            }
        } else {
            std::array<Vec3, 4> ep;
            for (int j = 0; j < 4; ++j) ep[j] = node_pt(tets[e * 4 + j]);
            Eigen::Matrix<double, 4, 3> grad;
            double volume = 0.0;
            if (!tet4_grad(ep, grad, volume)) {
                res.error = "fea_thermal: degenerate element " + std::to_string(e);
                return res;
            }
        }
        if ((e & 255) == 0 && cancelled()) return cancel_result();
    }

    // ── Singularity guard: a floating datum. A pure-Neumann problem (no Dirichlet node AND
    // no convection face) has no unique steady temperature — reject it up front. (The
    // advisor precheck lints this before dispatch; this is the belt-and-suspenders backstop.)
    const bool has_convection = !convections.empty();  // every film was validated above
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
        if (is_fixed[nid] && pres_temp[nid] != fixed_temps[i]) {
            res.error = "fea_thermal: conflicting temperatures prescribed at one node";
            return res;
        }
        is_fixed[nid] = 1;
        pres_temp[nid] = fixed_temps[i];
    }

    // ── Nodes no element references (same rule as the transient path and as fea_static;
    // see the transient comment for the measured case). Zero rows make the conductivity
    // matrix singular however it is factored, and elimination is exact because nothing
    // couples such a node. A node outside the domain has no conducted temperature, so it
    // carries the environment datum — the ambient of the first convection boundary, else
    // the first prescribed temperature — and is left out of the reported extrema instead
    // of pulling min_temp down to that datum.
    std::vector<char> in_domain(static_cast<size_t>(n_nodes), 0);
    for (int nid : tets)
        if (nid >= 0 && nid < n_nodes) in_domain[static_cast<size_t>(nid)] = 1;
    {
        double datum = 0.0;
        if (!convections.empty())
            datum = convections.front().t_amb;
        else if (!fixed_temps.empty())
            datum = fixed_temps.front();
        for (int i = 0; i < n_nodes; ++i) {
            if (in_domain[static_cast<size_t>(i)] || is_fixed[i]) continue;
            is_fixed[i] = 1;
            pres_temp[i] = datum;
        }
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
            if (!ok_elem) {
                res.error = "fea_thermal: degenerate or inverted element " + std::to_string(e);
                return res;
            }
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
            if (!tet4_grad(ep, dNxyz, vol)) {
                res.error = "fea_thermal: degenerate element " + std::to_string(e);
                return res;
            }
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
    auto surface_pass = [&](const std::unordered_set<int>& node_set,
                            const std::vector<std::size_t>& exterior_face_ids, bool do_flux,
                            double q, bool do_conv, double h, double t_amb) {
        const bool filtered = !exterior_face_ids.empty();
        const std::size_t visits = filtered ? exterior_face_ids.size() :
                                             static_cast<std::size_t>(n_elems);
        for (std::size_t visit = 0; visit < visits; ++visit) {
            if ((visit & 255) == 0 && cancelled()) return true;
            const std::size_t e = filtered ? exterior_face_ids[visit] / 4 : visit;
            const int first_face = filtered ? static_cast<int>(exterior_face_ids[visit] % 4) : 0;
            const int end_face = filtered ? first_face + 1 : 4;
            const int* el = &tets[e * static_cast<std::size_t>(stride)];
            for (int fidx = first_face; fidx < end_face; ++fidx) {
                if (!filtered) {
                    const auto own = face_owners.find(tri_face_key(el, kTetFaces[fidx]));
                    if (own == face_owners.end() || own->second != 1) continue;
                }
                const FaceTab& ft = kTetFaces[fidx];
                int fn[6] = {el[ft.corner[0]], el[ft.corner[1]],
                             el[ft.corner[2]], -1, -1, -1};
                bool all_in = true;
                if (!filtered) {
                    for (int i = 0; i < 3; ++i)
                        if (!node_set.count(fn[i])) {
                            all_in = false;
                            break;
                        }
                    if (!all_in) continue;
                }
                if (tet10)
                    for (int i = 0; i < 3; ++i) fn[3 + i] = el[ft.mid[i]];
                const int nfn = tet10 ? 6 : 3;
                std::array<Vec3, 6> fp{};
                for (int i = 0; i < nfn; ++i) fp[i] = node_pt(fn[i]);
                Eigen::Matrix<double, 6, 1> weights;
                Eigen::Matrix<double, 6, 6> surface_mass;
                double area = 0.0;
                if (!tri_surface_terms(fp, tet10, weights, surface_mass, area))
                    return false;
                if (do_flux)
                    for (int i = 0; i < nfn; ++i) {
                        const int ri = reduced[fn[i]];
                        if (ri >= 0) f(ri) += q * weights(i);
                    }
                if (do_conv)
                    for (int i = 0; i < nfn; ++i) {
                        const int ri = reduced[fn[i]];
                        if (ri >= 0) f(ri) += h * t_amb * weights(i);
                        for (int j = 0; j < nfn; ++j)
                            add_KK(fn[i], fn[j], h * surface_mass(i, j));
                    }
            }
        }
        return true;
    };

    // Each flux load contributes its OWN surface pass with its OWN magnitude — superposition
    // over independent sources (the previous single-set/single-q form overwrote q when a
    // study carried more than one flux load; see the C3 defect note in the header).
    for (const auto& fl : fluxes) {
        if (fl.nodes.empty() || fl.q == 0.0) continue;
        std::unordered_set<int> fs;
        if (fl.exterior_face_ids.empty()) fs.insert(fl.nodes.begin(), fl.nodes.end());
        if (!surface_pass(fs, fl.exterior_face_ids, /*do_flux*/ true, fl.q, /*do_conv*/ false,
                          0.0, 0.0)) {
            res.error = "fea_thermal: invalid exterior surface element";
            return res;
        }
    }
    for (const auto& cv : convections) {
        std::unordered_set<int> cs;
        if (cv.exterior_face_ids.empty()) cs.insert(cv.nodes.begin(), cv.nodes.end());
        if (!surface_pass(cs, cv.exterior_face_ids, /*do_flux*/ false, 0.0, /*do_conv*/ true,
                          cv.h, cv.t_amb)) {
            res.error = "fea_thermal: invalid exterior surface element";
            return res;
        }
    }
    if (cancelled()) return cancel_result();

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
    if (cancelled()) return cancel_result();

    // Scatter back to the full field (fixed nodes carry their prescribed temperature).
    res.temperature.assign(n_nodes, 0.0);
    for (int i = 0; i < n_nodes; ++i)
        res.temperature[i] = is_fixed[i] ? pres_temp[i] : Tf(reduced[i]);
    for (double v : res.temperature)
        if (!std::isfinite(v)) { res.error = "fea_thermal: solve produced a non-finite temperature"; return res; }

    // Extrema over the DOMAIN only: an eliminated out-of-domain node carries the
    // environment datum, not a solved temperature, and must not define min/max.
    double mx = -std::numeric_limits<double>::infinity();
    double mn = std::numeric_limits<double>::infinity();
    for (int i = 0; i < n_nodes; ++i) {
        if (!in_domain[static_cast<size_t>(i)]) continue;
        mx = std::max(mx, res.temperature[static_cast<size_t>(i)]);
        mn = std::min(mn, res.temperature[static_cast<size_t>(i)]);
    }
    if (!std::isfinite(mx)) {  // no element referenced any node — total, not reachable
        mx = res.temperature[0];
        mn = res.temperature[0];
    }
    res.max_temp = mx;
    res.min_temp = mn;
    res.n_dof = n_free;
    res.ok = true;
    return res;
}

// Legacy single-flux entry point: one node set, one magnitude. Delegates to the per-load
// overload with exactly one ThermalFluxLoad, so both public signatures share one validated
// implementation and produce identical results.
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

// ═════════════════════════════ Transient ═════════════════════════════════════════════════
//
// Conductivity and surface assembly intentionally mirror the steady solver so the same
// element and exterior-face rules apply to both paths.

namespace {

// Curved tet10 capacitance. det(J) is cubic for a quadratic isoparametric map, so
// N_i*N_j*det(J) has degree at most seven. The 5x5x5 Gauss-Legendre Duffy rule integrates
// that polynomial exactly while also checking the Jacobian throughout the element.
// For lumped capacitance, HRZ scales the positive consistent diagonal to the integrated
// physical volume; unlike row-sum lumping it cannot create negative corner masses.
bool tet10_capacitance(const std::array<Vec3, 10>& ep, double rho_cp, bool lumped,
                       Eigen::Matrix<double, 10, 10>& C) {
    static constexpr double x[5] = {
        0.0469100770306680, 0.2307653449471585, 0.5,
        0.7692346550528415, 0.9530899229693320};
    static constexpr double w[5] = {
        0.1184634425280945, 0.2393143352496832, 0.2844444444444444,
        0.2393143352496832, 0.1184634425280945};
    C.setZero();
    double volume = 0.0;
    double jacobian_sign = 0.0;
    for (int iu = 0; iu < 5; ++iu)
        for (int iv = 0; iv < 5; ++iv)
            for (int iw = 0; iw < 5; ++iw) {
                const double u = x[iu], v = x[iv], q = x[iw];
                const double r = u;
                const double s = (1.0 - u) * v;
                const double t = (1.0 - u) * (1.0 - v) * q;
                const double L[4] = {1.0 - r - s - t, r, s, t};
                Eigen::Matrix<double, 10, 1> N;
                for (int i = 0; i < 4; ++i)
                    N(i) = L[i] * (2.0 * L[i] - 1.0);
                N(4) = 4.0 * L[0] * L[1];
                N(5) = 4.0 * L[1] * L[2];
                N(6) = 4.0 * L[2] * L[0];
                N(7) = 4.0 * L[3] * L[0];
                N(8) = 4.0 * L[3] * L[2];
                N(9) = 4.0 * L[3] * L[1];
                const Eigen::Matrix<double, 10, 3> dNrst = tet10_dN(r, s, t);
                Eigen::Matrix3d J = Eigen::Matrix3d::Zero();
                for (int n = 0; n < 10; ++n) J += ep[n] * dNrst.row(n);
                const double detJ = J.determinant();
                if (!std::isfinite(detJ) || !(std::abs(detJ) > 0.0) ||
                    (jacobian_sign != 0.0 && jacobian_sign * detJ <= 0.0))
                    return false;
                jacobian_sign = detJ;
                const double dV =
                    std::abs(detJ) * (1.0 - u) * (1.0 - u) * (1.0 - v) *
                    w[iu] * w[iv] * w[iw];
                volume += dV;
                C.noalias() += (rho_cp * dV) * (N * N.transpose());
            }
    if (!(volume > 0.0) || !std::isfinite(volume) || !C.allFinite())
        return false;
    if (lumped) {
        const double diagonal_sum = C.diagonal().sum();
        if (!(diagonal_sum > 0.0) || !std::isfinite(diagonal_sum))
            return false;
        const Eigen::Matrix<double, 10, 1> diagonal = C.diagonal();
        C.setZero();
        for (int i = 0; i < 10; ++i)
            C(i, i) = rho_cp * volume * diagonal(i) / diagonal_sum;
    }
    return C.allFinite();
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
    const std::vector<ThermalFluxLoad>& fluxes,
    const std::vector<ThermalConvection>& convections, const ThermalTransientOptions& opts,
    int nodes_per_elem, const SimCancelToken& cancel_token) {
    ThermalTransientResult res;
    const int n_nodes = static_cast<int>(points.size() / 3);
    res.n_nodes = n_nodes;
    auto cancelled = [&]() {
        return cancel_token && cancel_token->load(std::memory_order_relaxed);
    };
    auto cancel_result = [&]() {
        res.error = "fea_thermal_transient: cancelled";
        return res;
    };
    if (cancelled()) return cancel_result();
    if (n_nodes == 0 || tets.empty()) {
        res.error = "fea_thermal_transient: empty mesh";
        return res;
    }
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
    if (!std::isfinite(opts.t_end) || opts.t_end <= 0.0) {
        res.error = "fea_thermal_transient: t_end must be finite and > 0";
        return res;
    }
    if (!std::isfinite(opts.t_init)) {
        res.error = "fea_thermal_transient: initial temperature must be finite";
        return res;
    }
    const double step_ratio = opts.t_end / opts.dt;
    if (!std::isfinite(step_ratio)) {
        res.error = "fea_thermal_transient: step count must be finite and in [1,1e6]";
        return res;
    }
    const double nearest_steps = std::round(step_ratio);
    const double step_tol =
        16.0 * std::numeric_limits<double>::epsilon() *
        std::max(1.0, std::abs(step_ratio));
    const double n_steps_d =
        std::abs(step_ratio - nearest_steps) <= step_tol
            ? nearest_steps
            : std::ceil(step_ratio);
    if (!std::isfinite(n_steps_d) || n_steps_d < 1.0 || n_steps_d > 1000000.0) {
        res.error = "fea_thermal_transient: step count must be finite and in [1,1e6]";
        return res;
    }
    const long long n_steps_ll = static_cast<long long>(n_steps_d);
    if (fixed_nodes.size() != fixed_temps.size()) {
        res.error = "fea_thermal_transient: fixed_nodes and fixed_temps length mismatch";
        return res;
    }
    if (!opts.init_temps.empty() && opts.init_temps.size() != static_cast<size_t>(n_nodes)) {
        res.error = "fea_thermal_transient: init_temps must have one entry per node";
        return res;
    }
    for (double x : points)
        if (!std::isfinite(x)) {
            res.error = "fea_thermal_transient: mesh coordinates must be finite";
            return res;
        }
    for (int nid : tets)
        if (nid < 0 || nid >= n_nodes) {
            res.error = "fea_thermal_transient: element node index out of range";
            return res;
        }
    for (double v : opts.init_temps)
        if (!std::isfinite(v)) {
            res.error = "fea_thermal_transient: init_temps must be finite";
            return res;
        }
    for (double v : opts.frame_times)
        if (!std::isfinite(v) || v < 0.0 || v > opts.t_end) {
            res.error =
                "fea_thermal_transient: frame times must be finite and within [0,t_end]";
            return res;
        }
    for (const auto& fl : fluxes) {
        if (!std::isfinite(fl.q) || (!fl.nodes.empty() && fl.q == 0.0)) {
            res.error =
                "fea_thermal_transient: heat flux must be finite and non-zero "
                "when selected";
            return res;
        }
        for (int nid : fl.nodes)
            if (nid < 0 || nid >= n_nodes) {
                res.error = "fea_thermal_transient: flux node index out of range";
                return res;
            }
        if (fl.schedule.times.size() != fl.schedule.scales.size()) {
            res.error = "fea_thermal_transient: schedule times/scales length mismatch";
            return res;
        }
        for (size_t i = 0; i < fl.schedule.times.size(); ++i) {
            if (!std::isfinite(fl.schedule.times[i]) || fl.schedule.times[i] < 0.0 ||
                (i > 0 && !(fl.schedule.times[i] > fl.schedule.times[i - 1]))) {
                res.error =
                    "fea_thermal_transient: schedule times must be finite, non-negative and "
                    "strictly ascending";
                return res;
            }
            if (!std::isfinite(fl.schedule.scales[i])) {
                res.error = "fea_thermal_transient: schedule scales must be finite";
                return res;
            }
        }
        if (!fl.schedule.times.empty() && fl.schedule.times.front() != 0.0) {
            res.error = "fea_thermal_transient: schedule times must start at 0";
            return res;
        }
        if (!std::isfinite(fl.schedule.period) || fl.schedule.period < 0.0 ||
            (fl.schedule.period > 0.0 && !fl.schedule.times.empty() &&
             fl.schedule.times.back() >= fl.schedule.period)) {
            res.error =
                "fea_thermal_transient: schedule period must be finite, non-negative and "
                "greater than the final breakpoint";
            return res;
        }
    }
    for (const auto& cv : convections) {
        if (!std::isfinite(cv.h) || cv.h <= 0.0 || !std::isfinite(cv.t_amb) ||
            cv.nodes.empty()) {
            res.error =
                "fea_thermal_transient: convection requires nodes, finite h>0 and ambient";
            return res;
        }
        for (int nid : cv.nodes)
            if (nid < 0 || nid >= n_nodes) {
                res.error = "fea_thermal_transient: convection node index out of range";
                return res;
            }
    }

    const bool tet10 = nodes_per_elem == 10;
    const int stride = nodes_per_elem;
    const int n_elems = static_cast<int>(tets.size() / static_cast<size_t>(stride));
    res.n_elems = n_elems;
    auto node_pt = [&](int nid) {
        return Vec3(points[nid * 3], points[nid * 3 + 1], points[nid * 3 + 2]);
    };
    const auto face_owners = face_owner_counts(tets, stride);
    for (const auto& fl : fluxes) {
        if (fl.nodes.empty() && fl.exterior_face_ids.empty()) continue;
        const std::unordered_set<int> ns(fl.nodes.begin(), fl.nodes.end());
        if (selected_exterior_faces(tets, stride, face_owners, ns, fl.exterior_face_ids) <= 0) {
            res.error =
                "fea_thermal_transient: flux selection contains no exterior mesh face";
            return res;
        }
    }
    for (const auto& cv : convections) {
        const std::unordered_set<int> ns(cv.nodes.begin(), cv.nodes.end());
        if (selected_exterior_faces(tets, stride, face_owners, ns, cv.exterior_face_ids) <= 0) {
            res.error =
                "fea_thermal_transient: convection selection contains no exterior mesh face";
            return res;
        }
    }

    std::vector<char> is_fixed(n_nodes, 0);
    std::vector<double> pres_temp(n_nodes, 0.0);
    for (size_t i = 0; i < fixed_nodes.size(); ++i) {
        const int nid = fixed_nodes[i];
        if (nid < 0 || nid >= n_nodes || !std::isfinite(fixed_temps[i])) {
            res.error = "fea_thermal_transient: invalid fixed temperature node/value";
            return res;
        }
        if (is_fixed[nid] && pres_temp[nid] != fixed_temps[i]) {
            res.error =
                "fea_thermal_transient: conflicting temperatures prescribed at one node";
            return res;
        }
        is_fixed[nid] = 1;
        pres_temp[nid] = fixed_temps[i];
    }

    // ── Nodes no element references. The mesher's hanging-fragment dissolve drops
    // elements but deliberately keeps node numbering 1:1 with the polymesh mesh the
    // adaptive route refines through (detail::repair_lonely_nodes), so a repaired mesh
    // can carry a few nodes that belong to no element at all. Their rows in the
    // iteration matrix are exactly zero, which makes it singular however it is factored:
    // the sensor-node demo board arrived with 9 such nodes out of 50,550 and CHOLMOD, LDLT
    // and SparseLU all failed in turn ("iteration-matrix factorization failed").
    // Eliminating them is exact rather than an approximation — nothing couples them, so
    // they cannot influence any other DOF — and it is the same rule fea_static already
    // applies to the same meshes. They are outside the domain, so they simply keep their
    // initial temperature instead of being handed an invented conducted one.
    {
        std::vector<char> referenced(static_cast<size_t>(n_nodes), 0);
        for (int nid : tets)
            if (nid >= 0 && nid < n_nodes) referenced[static_cast<size_t>(nid)] = 1;
        for (int i = 0; i < n_nodes; ++i) {
            if (referenced[static_cast<size_t>(i)] || is_fixed[i]) continue;
            is_fixed[i] = 1;
            pres_temp[i] = opts.init_temps.empty()
                               ? opts.t_init
                               : opts.init_temps[static_cast<size_t>(i)];
        }
    }
    std::vector<int> reduced(n_nodes, -1);
    int n_free = 0;
    for (int i = 0; i < n_nodes; ++i)
        if (!is_fixed[i]) reduced[i] = n_free++;
    res.n_dof = n_free;

    std::vector<Eigen::Triplet<double>> ktrips;
    std::vector<Eigen::Triplet<double>> ctrips;
    Eigen::VectorXd c_lump = Eigen::VectorXd::Zero(n_free);
    Eigen::VectorXd f_base = Eigen::VectorXd::Zero(n_free);
    const TetGauss4 gauss;
    auto add_K = [&](int a_node, int b_node, double kab) {
        const int ra = reduced[a_node];
        if (ra < 0) return;
        const int rb = reduced[b_node];
        if (rb >= 0)
            ktrips.emplace_back(ra, rb, kab);
        else
            f_base(ra) -= kab * pres_temp[b_node];
    };
    auto add_C = [&](int a_node, int b_node, double cab) {
        const int ra = reduced[a_node], rb = reduced[b_node];
        if (ra < 0 || rb < 0) return;
        if (opts.lumped) {
            if (ra == rb) c_lump(ra) += cab;
        } else {
            ctrips.emplace_back(ra, rb, cab);
        }
    };

    if (tet10) {
        for (int e = 0; e < n_elems; ++e) {
            std::array<Vec3, 10> ep;
            std::array<int, 10> tn;
            for (int j = 0; j < 10; ++j) {
                tn[j] = tets[e * 10 + j];
                ep[j] = node_pt(tn[j]);
            }
            Eigen::Matrix<double, 10, 10> Ke =
                Eigen::Matrix<double, 10, 10>::Zero();
            double sign = 0.0;
            for (int g = 0; g < 4; ++g) {
                Eigen::Matrix<double, 10, 3> dNxyz;
                double detJ = 0.0;
                if (!tet10_grad_at(ep, gauss.pt[g][0], gauss.pt[g][1],
                                   gauss.pt[g][2], dNxyz, detJ) ||
                    !std::isfinite(detJ) || (sign != 0.0 && sign * detJ <= 0.0)) {
                    res.error =
                        "fea_thermal_transient: degenerate or inverted element " +
                        std::to_string(e);
                    return res;
                }
                sign = detJ;
                Ke += (dNxyz * dNxyz.transpose()) *
                      (k_cond * std::abs(detJ) * gauss.w[g]);
            }
            if (!Ke.allFinite()) {
                res.error = "fea_thermal_transient: invalid element " + std::to_string(e);
                return res;
            }
            for (int a = 0; a < 10; ++a)
                for (int b = 0; b < 10; ++b) add_K(tn[a], tn[b], Ke(a, b));
            Eigen::Matrix<double, 10, 10> Ce;
            if (!tet10_capacitance(ep, opts.rho_cp, opts.lumped, Ce)) {
                res.error =
                    "fea_thermal_transient: degenerate or inverted element " +
                    std::to_string(e);
                return res;
            }
            for (int a = 0; a < 10; ++a)
                for (int b = 0; b < 10; ++b)
                    add_C(tn[a], tn[b], Ce(a, b));
            if ((e & 255) == 0 && cancelled()) return cancel_result();
        }
    } else {
        for (int e = 0; e < n_elems; ++e) {
            std::array<Vec3, 4> ep;
            std::array<int, 4> tn;
            for (int j = 0; j < 4; ++j) {
                tn[j] = tets[e * 4 + j];
                ep[j] = node_pt(tn[j]);
            }
            Eigen::Matrix<double, 4, 3> dNxyz;
            double volume = 0.0;
            if (!tet4_grad(ep, dNxyz, volume)) {
                res.error =
                    "fea_thermal_transient: degenerate element " + std::to_string(e);
                return res;
            }
            const Eigen::Matrix<double, 4, 4> Ke =
                (dNxyz * dNxyz.transpose()) * (k_cond * volume);
            for (int a = 0; a < 4; ++a)
                for (int b = 0; b < 4; ++b) add_K(tn[a], tn[b], Ke(a, b));
            if (opts.lumped) {
                for (int a = 0; a < 4; ++a)
                    add_C(tn[a], tn[a], opts.rho_cp * volume / 4.0);
            } else {
                for (int a = 0; a < 4; ++a)
                    for (int b = 0; b < 4; ++b)
                        add_C(tn[a], tn[b],
                              opts.rho_cp * volume / 20.0 *
                                  (a == b ? 2.0 : 1.0));
            }
            if ((e & 255) == 0 && cancelled()) return cancel_result();
        }
    }

    auto selected_face = [&](const int* el, int fidx,
                             const std::unordered_set<int>& nodes) {
        const FaceTab& ft = kTetFaces[fidx];
        const auto own = face_owners.find(tri_face_key(el, ft));
        if (own == face_owners.end() || own->second != 1) return false;
        for (int i = 0; i < 3; ++i)
            if (!nodes.count(el[ft.corner[i]])) return false;
        return true;
    };
    bool surface_valid = true;
    auto surface_flux_vec = [&](const std::vector<int>& nodes, double q,
                                const std::vector<std::size_t>& exterior_face_ids) {
        Eigen::VectorXd fv = Eigen::VectorXd::Zero(n_free);
        const bool filtered = !exterior_face_ids.empty();
        std::unordered_set<int> node_set;
        if (!filtered) node_set.insert(nodes.begin(), nodes.end());
        const std::size_t visits = filtered ? exterior_face_ids.size() :
                                             static_cast<std::size_t>(n_elems);
        for (std::size_t visit = 0; visit < visits && surface_valid; ++visit) {
            if ((visit & 255) == 0 && cancelled()) return fv;
            const std::size_t e = filtered ? exterior_face_ids[visit] / 4 : visit;
            const int first_face = filtered ? static_cast<int>(exterior_face_ids[visit] % 4) : 0;
            const int end_face = filtered ? first_face + 1 : 4;
            const int* el = &tets[e * static_cast<std::size_t>(stride)];
            for (int fidx = first_face; fidx < end_face; ++fidx) {
                if (!filtered && !selected_face(el, fidx, node_set)) continue;
                const FaceTab& ft = kTetFaces[fidx];
                int fn[6] = {el[ft.corner[0]], el[ft.corner[1]],
                             el[ft.corner[2]], -1, -1, -1};
                if (tet10)
                    for (int i = 0; i < 3; ++i) fn[3 + i] = el[ft.mid[i]];
                const int nfn = tet10 ? 6 : 3;
                std::array<Vec3, 6> fp{};
                for (int i = 0; i < nfn; ++i) fp[i] = node_pt(fn[i]);
                Eigen::Matrix<double, 6, 1> weights;
                Eigen::Matrix<double, 6, 6> surface_mass;
                double area = 0.0;
                if (!tri_surface_terms(fp, tet10, weights, surface_mass, area)) {
                    surface_valid = false;
                    break;
                }
                for (int i = 0; i < nfn; ++i) {
                    const int ri = reduced[fn[i]];
                    if (ri >= 0) fv(ri) += q * weights(i);
                }
            }
        }
        return fv;
    };

    for (const auto& cv : convections) {
        const bool filtered = !cv.exterior_face_ids.empty();
        std::unordered_set<int> nodes;
        if (!filtered) nodes.insert(cv.nodes.begin(), cv.nodes.end());
        const std::size_t visits = filtered ? cv.exterior_face_ids.size() :
                                             static_cast<std::size_t>(n_elems);
        for (std::size_t visit = 0; visit < visits; ++visit) {
            if ((visit & 255) == 0 && cancelled()) return cancel_result();
            const std::size_t e = filtered ? cv.exterior_face_ids[visit] / 4 : visit;
            const int first_face = filtered ? static_cast<int>(cv.exterior_face_ids[visit] % 4) : 0;
            const int end_face = filtered ? first_face + 1 : 4;
            const int* el = &tets[e * static_cast<std::size_t>(stride)];
            for (int fidx = first_face; fidx < end_face; ++fidx) {
                if (!filtered && !selected_face(el, fidx, nodes)) continue;
                const FaceTab& ft = kTetFaces[fidx];
                int fn[6] = {el[ft.corner[0]], el[ft.corner[1]],
                             el[ft.corner[2]], -1, -1, -1};
                if (tet10)
                    for (int i = 0; i < 3; ++i) fn[3 + i] = el[ft.mid[i]];
                const int nfn = tet10 ? 6 : 3;
                std::array<Vec3, 6> fp{};
                for (int i = 0; i < nfn; ++i) fp[i] = node_pt(fn[i]);
                Eigen::Matrix<double, 6, 1> weights;
                Eigen::Matrix<double, 6, 6> surface_mass;
                double area = 0.0;
                if (!tri_surface_terms(fp, tet10, weights, surface_mass, area)) {
                    res.error =
                        "fea_thermal_transient: invalid exterior surface element";
                    return res;
                }
                for (int i = 0; i < nfn; ++i) {
                    const int ri = reduced[fn[i]];
                    if (ri >= 0)
                        f_base(ri) += cv.h * cv.t_amb * weights(i);
                    for (int j = 0; j < nfn; ++j)
                        add_K(fn[i], fn[j], cv.h * surface_mass(i, j));
                }
            }
        }
    }
    std::vector<Eigen::VectorXd> flux_vecs;
    flux_vecs.reserve(fluxes.size());
    for (const auto& fl : fluxes) {
        if (fl.nodes.empty() || fl.q == 0.0)
            flux_vecs.emplace_back(Eigen::VectorXd::Zero(n_free));
        else
            flux_vecs.push_back(surface_flux_vec(fl.nodes, fl.q, fl.exterior_face_ids));
    }
    if (!surface_valid) {
        res.error = "fea_thermal_transient: invalid exterior surface element";
        return res;
    }
    if (cancelled()) return cancel_result();

    std::vector<double> T_full(n_nodes, opts.t_init);
    if (!opts.init_temps.empty()) T_full = opts.init_temps;
    for (int i = 0; i < n_nodes; ++i)
        if (is_fixed[i]) T_full[i] = pres_temp[i];
    res.envelope_max = T_full;
    double gmax = T_full[0], gmin = T_full[0], gmax_t = 0.0;
    for (double v : T_full) {
        gmax = std::max(gmax, v);
        gmin = std::min(gmin, v);
    }
    std::vector<double> want = opts.frame_times;
    std::sort(want.begin(), want.end());
    size_t want_i = 0;
    if (!want.empty() && want.front() == 0.0) {
        res.frames.push_back({0.0, T_full});
        while (want_i < want.size() && want[want_i] == 0.0) ++want_i;
    }
    const int n_steps = static_cast<int>(n_steps_ll);
    res.n_steps = n_steps;
    if (n_free == 0) {
        for (; want_i < want.size(); ++want_i)
            res.frames.push_back({want[want_i], T_full});
        if (res.frames.empty() || res.frames.back().time != opts.t_end)
            res.frames.push_back({opts.t_end, T_full});
        res.max_temp = gmax;
        res.min_temp = gmin;
        res.max_temp_time = 0.0;
        res.ok = true;
        return res;
    }

    std::vector<Eigen::Triplet<double>> atrips = ktrips;
    if (opts.lumped) {
        for (int i = 0; i < n_free; ++i)
            if (c_lump(i) > 0.0) atrips.emplace_back(i, i, c_lump(i) / opts.dt);
    } else {
        for (const auto& t : ctrips)
            atrips.emplace_back(t.row(), t.col(), t.value() / opts.dt);
    }
    Eigen::SparseMatrix<double> A(n_free, n_free);
    A.setFromTriplets(atrips.begin(), atrips.end());
    A.makeCompressed();
    if (A.nonZeros() == 0) {
        res.error = "fea_thermal_transient: no valid elements contributed";
        return res;
    }
    Eigen::SparseMatrix<double> Cff(n_free, n_free);
    if (!opts.lumped) {
        Cff.setFromTriplets(ctrips.begin(), ctrips.end());
        Cff.makeCompressed();
    }
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
                res.error =
                    "fea_thermal_transient: iteration-matrix factorization failed";
                return res;
            }
        }
    }
    if (cancelled()) return cancel_result();

    Eigen::VectorXd Tf(n_free);
    for (int i = 0; i < n_nodes; ++i)
        if (reduced[i] >= 0) Tf(reduced[i]) = T_full[i];
    for (int n = 1; n <= n_steps; ++n) {
        if (cancelled()) return cancel_result();
        const double t_prev = static_cast<double>(n - 1) * opts.dt;
        const double t_next =
            std::min(static_cast<double>(n) * opts.dt, opts.t_end);
        const double dt_step = t_next - t_prev;
        const bool partial_step =
            std::abs(dt_step - opts.dt) >
            16.0 * std::numeric_limits<double>::epsilon() * opts.dt;
        Eigen::VectorXd rhs = f_base;
        if (opts.lumped)
            rhs += (c_lump.array() * Tf.array()).matrix() / dt_step;
        else
            rhs += (Cff * Tf) / dt_step;
        for (size_t k = 0; k < fluxes.size(); ++k) {
            const double scale =
                thermal_schedule_scale(fluxes[k].schedule, t_next);
            if (scale != 0.0) rhs += scale * flux_vecs[k];
        }
        bool step_ok = false;
        if (partial_step) {
            std::vector<Eigen::Triplet<double>> step_trips = ktrips;
            if (opts.lumped) {
                for (int i = 0; i < n_free; ++i)
                    if (c_lump(i) > 0.0)
                        step_trips.emplace_back(i, i, c_lump(i) / dt_step);
            } else {
                for (const auto& t : ctrips)
                    step_trips.emplace_back(t.row(), t.col(),
                                            t.value() / dt_step);
            }
            Eigen::SparseMatrix<double> A_step(n_free, n_free);
            A_step.setFromTriplets(step_trips.begin(), step_trips.end());
            A_step.makeCompressed();
            Eigen::SparseLU<Eigen::SparseMatrix<double>> step_lu;
            step_lu.analyzePattern(A_step);
            step_lu.factorize(A_step);
            if (step_lu.info() == Eigen::Success) {
                Tf = step_lu.solve(rhs);
                step_ok = step_lu.info() == Eigen::Success;
            }
        } else {
#ifdef CHUDWARE_WITH_CHOLMOD
            if (use_chol) {
                Tf = chol.solve(rhs);
                step_ok = chol.info() == Eigen::Success;
            } else
#endif
            if (use_ldlt) {
                Tf = ldlt.solve(rhs);
                step_ok = ldlt.info() == Eigen::Success;
            } else {
                Tf = lu.solve(rhs);
                step_ok = lu.info() == Eigen::Success;
            }
        }
        if (!step_ok || !Tf.allFinite()) {
            res.error =
                "fea_thermal_transient: sparse solve failed at step " +
                std::to_string(n);
            return res;
        }
        for (int i = 0; i < n_nodes; ++i)
            if (reduced[i] >= 0) T_full[i] = Tf(reduced[i]);
        for (int i = 0; i < n_nodes; ++i) {
            if (T_full[i] > res.envelope_max[i])
                res.envelope_max[i] = T_full[i];
            if (T_full[i] > gmax) {
                gmax = T_full[i];
                gmax_t = t_next;
            }
            if (T_full[i] < gmin) gmin = T_full[i];
        }
        const bool last = n == n_steps;
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

double thermal_flux_max(const std::vector<double>& points, const std::vector<int>& tets,
                        const std::vector<double>& temperature, double k_cond,
                        int nodes_per_elem) {
    double maximum = 0.0;
    const TetGauss4 gauss;
    for (size_t e = 0; e < tets.size(); e += nodes_per_elem) {
        if (nodes_per_elem == 10) {
            std::array<Vec3, 10> ep;
            Eigen::Matrix<double, 10, 1> temp;
            for (int j = 0; j < 10; ++j) {
                const int n = tets[e + j];
                ep[j] = Vec3(points[3*n], points[3*n+1], points[3*n+2]);
                temp[j] = temperature[n];
            }
            for (int g = 0; g < 4; ++g) {
                Eigen::Matrix<double, 10, 3> grad;
                double detJ;
                if (tet10_grad_at(ep, gauss.pt[g][0], gauss.pt[g][1],
                                  gauss.pt[g][2], grad, detJ))
                    maximum = std::max(maximum, k_cond * (grad.transpose() * temp).norm());
            }
        } else {
            std::array<Vec3, 4> ep;
            Eigen::Vector4d temp;
            for (int j = 0; j < 4; ++j) {
                const int n = tets[e + j];
                ep[j] = Vec3(points[3*n], points[3*n+1], points[3*n+2]);
                temp[j] = temperature[n];
            }
            Eigen::Matrix<double, 4, 3> grad;
            double volume;
            if (tet4_grad(ep, grad, volume))
                maximum = std::max(maximum, k_cond * (grad.transpose() * temp).norm());
        }
    }
    return maximum;
}

}  // namespace chudware::sim
