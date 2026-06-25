// Unit tests for the FEA static solver (aicad::sim::fea_static_solve) and the
// wind-tunnel LBM front-end (aicad::sim::run_wind_tunnel).
//
// The FEA tests are OCCT-free, oracle-free sanity checks on the numerics. The
// CFD test (G4) verifies that ref_length_mm comes from the PART bounding box, not
// the padded domain — a 50mm cube must report ref_length_mm=50, not 4×50=200.
// The full Python-vs-C++ differential gate lives in tools/cpp_sim_parity.py.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <algorithm>
#include <array>
#include <cmath>
#include <map>
#include <set>
#include <utility>
#include <vector>

#include "sim/fea_static.h"
#include "sim/flow_run.h"

using aicad::sim::fea_static_solve;
using aicad::sim::FeaStaticResult;

namespace {

// ── Structured box-beam meshes (OCCT/gmsh-free) for the analytic regressions ──────
// A box [0,Lx]x[0,Ly]x[0,Lz] split into nx*ny*nz hex cells, each -> 6 Freudenthal tets;
// optionally promoted to tet10 with deduplicated mid-edge nodes in the gmsh edge order
// {0,1},{1,2},{2,0},{3,0},{3,2},{3,1}. Mirrors the helpers in test_tet10.cpp.
struct BoxMesh {
    std::vector<double> points;
    std::vector<int> tets;
    int nodes_per_elem = 4;
    double Lx = 0, Ly = 0, Lz = 0;
};

BoxMesh make_box_tet4(int nx, int ny, int nz, double Lx, double Ly, double Lz) {
    BoxMesh m;
    m.Lx = Lx; m.Ly = Ly; m.Lz = Lz; m.nodes_per_elem = 4;
    const int NX = nx + 1, NY = ny + 1, NZ = nz + 1;
    auto gid = [&](int i, int j, int k) { return (k * NY + j) * NX + i; };
    m.points.resize(static_cast<size_t>(NX) * NY * NZ * 3);
    for (int k = 0; k < NZ; ++k)
        for (int j = 0; j < NY; ++j)
            for (int i = 0; i < NX; ++i) {
                const int id = gid(i, j, k);
                m.points[id * 3 + 0] = Lx * i / nx;
                m.points[id * 3 + 1] = Ly * j / ny;
                m.points[id * 3 + 2] = Lz * k / nz;
            }
    static const int TET[6][4] = {{0, 1, 2, 6}, {0, 2, 3, 6}, {0, 3, 7, 6},
                                  {0, 7, 4, 6}, {0, 4, 5, 6}, {0, 5, 1, 6}};
    for (int k = 0; k < nz; ++k)
        for (int j = 0; j < ny; ++j)
            for (int i = 0; i < nx; ++i) {
                const int c[8] = {gid(i, j, k),         gid(i + 1, j, k),
                                  gid(i + 1, j + 1, k), gid(i, j + 1, k),
                                  gid(i, j, k + 1),     gid(i + 1, j, k + 1),
                                  gid(i + 1, j + 1, k + 1), gid(i, j + 1, k + 1)};
                for (auto& tt : TET)
                    for (int v = 0; v < 4; ++v) m.tets.push_back(c[tt[v]]);
            }
    return m;
}

BoxMesh promote_to_tet10(const BoxMesh& m4) {
    BoxMesh m = m4;
    m.nodes_per_elem = 10;
    m.tets.clear();
    std::map<std::pair<int, int>, int> edge_mid;
    auto mid_node = [&](int a, int b) {
        auto key = std::make_pair(std::min(a, b), std::max(a, b));
        auto it = edge_mid.find(key);
        if (it != edge_mid.end()) return it->second;
        const int id = static_cast<int>(m.points.size() / 3);
        for (int c = 0; c < 3; ++c)
            m.points.push_back(0.5 * (m4.points[a * 3 + c] + m4.points[b * 3 + c]));
        edge_mid[key] = id;
        return id;
    };
    static const int EDGES[6][2] = {{0, 1}, {1, 2}, {2, 0}, {3, 0}, {3, 2}, {3, 1}};
    const int n4 = static_cast<int>(m4.tets.size() / 4);
    for (int e = 0; e < n4; ++e) {
        const int* t = &m4.tets[e * 4];
        for (int v = 0; v < 4; ++v) m.tets.push_back(t[v]);
        for (auto& ed : EDGES) m.tets.push_back(mid_node(t[ed[0]], t[ed[1]]));
    }
    return m;
}

// A single unit tet (corner at origin + the 3 axis unit points). Not physical on its
// own but exercises assembly + solve + stress recovery without degeneracy.
TEST_CASE("single tet solves and recovers finite stress") {
    std::vector<double> pts = {0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1};
    std::vector<int> tets = {0, 1, 2, 3};
    std::vector<int> fixed = {0, 1, 2};                 // pin 3 of the 4 nodes
    std::vector<double> f(12, 0.0);
    f[3 * 3 + 2] = -10.0;                                // pull node 3 in -z
    auto r = fea_static_solve(pts, tets, 200000.0, 0.3, 250.0, fixed, f);
    CHECK(r.ok);
    CHECK(r.n_dof == 12);
    CHECK(r.max_disp_mm > 0.0);
    CHECK(std::isfinite(r.max_von_mises_mpa));
    CHECK(r.max_von_mises_mpa > 0.0);
}

TEST_CASE("single tet matches closed-form displacement and von Mises") {
    std::vector<double> pts = {0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1};
    std::vector<int> tets = {0, 1, 2, 3};
    std::vector<int> fixed = {0, 1, 2};
    std::vector<double> f(12, 0.0);
    f[3 * 3 + 2] = -10.0;

    const double E = 200000.0, nu = 0.3;
    auto r = fea_static_solve(pts, tets, E, nu, 250.0, fixed, f);
    REQUIRE(r.ok);

    const double lam = E * nu / ((1.0 + nu) * (1.0 - 2.0 * nu));
    const double mu = E / (2.0 * (1.0 + nu));
    const double czz = lam + 2.0 * mu;
    const double expected_uz = -10.0 / (czz / 6.0);
    const double expected_vm = std::abs((czz - lam) * expected_uz);

    CHECK(r.displacement[3 * 3 + 0] == doctest::Approx(0.0).epsilon(1e-12));
    CHECK(r.displacement[3 * 3 + 1] == doctest::Approx(0.0).epsilon(1e-12));
    CHECK(r.displacement[3 * 3 + 2] == doctest::Approx(expected_uz).epsilon(1e-10));
    CHECK(r.max_disp_mm == doctest::Approx(std::abs(expected_uz)).epsilon(1e-10));
    CHECK(r.max_von_mises_mpa == doctest::Approx(expected_vm).epsilon(1e-10));
    CHECK(r.min_fos == doctest::Approx(250.0 / expected_vm).epsilon(1e-10));
}

TEST_CASE("repeated static solves on same mesh are deterministic and isolated") {
    std::vector<double> pts = {0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1};
    std::vector<int> tets = {0, 1, 2, 3};
    std::vector<int> fixed = {0, 1, 2};
    std::vector<double> f1(12, 0.0), f2(12, 0.0);
    f1[3 * 3 + 2] = -10.0;
    f2[3 * 3 + 2] = -25.0;

    auto a = fea_static_solve(pts, tets, 200000.0, 0.3, 250.0, fixed, f1);
    auto b = fea_static_solve(pts, tets, 200000.0, 0.3, 250.0, fixed, f2);
    auto c = fea_static_solve(pts, tets, 200000.0, 0.3, 250.0, fixed, f1);
    REQUIRE(a.ok);
    REQUIRE(b.ok);
    REQUIRE(c.ok);

    CHECK(c.max_disp_mm == doctest::Approx(a.max_disp_mm).epsilon(1e-12));
    CHECK(c.max_von_mises_mpa == doctest::Approx(a.max_von_mises_mpa).epsilon(1e-12));
    CHECK(b.max_disp_mm == doctest::Approx(a.max_disp_mm * 2.5).epsilon(1e-12));
    CHECK(b.max_von_mises_mpa == doctest::Approx(a.max_von_mises_mpa * 2.5).epsilon(1e-12));
}

// A uniaxial bar (two tets sharing a face is fiddly; use a structured 1x1x1 cube of
// 6 tets) fixed at z=0, pulled at z=1: displacement is positive and finite, and a
// fully-pinned model reports the all-constrained error.
TEST_CASE("fully constrained model is an error, not a crash") {
    std::vector<double> pts = {0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1};
    std::vector<int> tets = {0, 1, 2, 3};
    std::vector<int> fixed = {0, 1, 2, 3};               // every node pinned
    std::vector<double> f(12, 0.0);
    auto r = fea_static_solve(pts, tets, 200000.0, 0.3, 250.0, fixed, f);
    CHECK_FALSE(r.ok);
    CHECK(r.error.find("constrained") != std::string::npos);
}

// Zero load on a free model -> zero displacement, zero stress, max FoS.
TEST_CASE("zero load gives zero response") {
    std::vector<double> pts = {0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1};
    std::vector<int> tets = {0, 1, 2, 3};
    std::vector<int> fixed = {0, 1, 2};
    std::vector<double> f(12, 0.0);
    auto r = fea_static_solve(pts, tets, 200000.0, 0.3, 250.0, fixed, f);
    CHECK(r.ok);
    CHECK(r.max_disp_mm == doctest::Approx(0.0));
    CHECK(r.max_von_mises_mpa == doctest::Approx(0.0));
    CHECK(r.min_fos == doctest::Approx(15.0));  // no stress -> clipped max FoS
}

TEST_CASE("invalid simulation inputs fail cleanly") {
    std::vector<double> pts = {0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1};
    std::vector<int> tets = {0, 1, 2, 99};
    std::vector<int> fixed = {0, 1, 2};
    std::vector<double> f(12, 0.0);

    auto bad_idx = fea_static_solve(pts, tets, 200000.0, 0.3, 250.0, fixed, f);
    CHECK_FALSE(bad_idx.ok);
    CHECK(bad_idx.error.find("out of range") != std::string::npos);

    auto bad_mat = fea_static_solve(pts, {0, 1, 2, 3}, 200000.0, 0.5, 250.0, fixed, f);
    CHECK_FALSE(bad_mat.ok);
    CHECK(bad_mat.error.find("nu") != std::string::npos);

    auto bad_conn = fea_static_solve(pts, {0, 1, 2}, 200000.0, 0.3, 250.0, fixed, f);
    CHECK_FALSE(bad_conn.ok);
    CHECK(bad_conn.error.find("divisible") != std::string::npos);
}

// ── Analytic regressions (F1 nominal stress, F4 E-scaling) ───────────────────────

// ── tet10 work-equivalent end-face load builder (mirrors fea_run.cpp) ─────────────
// Builds the nodal force vector for a uniform axial traction on the x=Lx end face EXACTLY
// the way fea_run_static does for a real gmsh tet10 mesh, so this kernel test exercises the
// SAME load-distribution path the live FEA uses (the previous F1 test bypassed it with a
// hand-rolled equal split, which is why it passed while the live path was broken — Bug Z1).
//
// Two pieces reproduce the production logic:
//   (a) corner_face_pick(): the faceset resolution as it behaves on a real gmsh tet10 mesh.
//       promote_to_tet10 appends the mid-side nodes AFTER all corner nodes, so corner nodes
//       are the low indices [0, n_corner). The geometric faceset resolution lands on the
//       CORNER nodes lying on the loaded plane (they sit at the CAD triangulation's grid),
//       but the interior MID-SIDE nodes on that plane are the quadratic d.o.f. the consistent
//       weights actually load — and those are exactly the nodes the pick drops. This is the
//       corner-dominated set that makes w_sum collapse to 0 (Bug Z1).
//   (b) consistent_axial_forces(): accumulate_face_weights' tri6 rule (corner 0, mid-side
//       A/3) PLUS the Z1 fix (add_midside_face_nodes completes the picked set with mid-edge
//       nodes whose two endpoint corners are picked) → F distributed F/3 onto each end-face
//       mid-side node, 0 on corners, resultant exactly F. With `apply_z1_fix=false` it
//       reproduces the BUG: weights land on mid-side nodes but the deposition set is the
//       corner-only pick → w_sum≈0 → equal split over the corners (wrong + non-convergent).
namespace tet10load {
// gmsh edge order for the 4 tet faces (corners) and their mid-edge local node per edge.
static const int FACE_CORNERS[4][3] = {{0, 1, 2}, {0, 1, 3}, {0, 2, 3}, {1, 2, 3}};
static const int FACE_MIDS[4][3]    = {{4, 5, 6}, {4, 9, 7}, {6, 8, 7}, {5, 8, 9}};
static const int EDGE_ENDS[6][3] = {{4, 0, 1}, {5, 1, 2}, {6, 2, 0},
                                    {7, 3, 0}, {8, 3, 2}, {9, 3, 1}};

// (a) end-face node pick = the CORNER nodes on the loaded plane (indices < n_corner). This
// is the corner-dominated set the geometric faceset resolution yields on a gmsh tet10 mesh:
// the quadratic mid-side nodes on the same plane (indices >= n_corner) are dropped, which is
// precisely the Z1 bug condition (the consistent weights land on those dropped mid-sides).
std::set<int> corner_face_pick(const BoxMesh& m, int n_corner, double xface, double tol) {
    std::set<int> picked;
    for (int nid = 0; nid < n_corner; ++nid)
        if (std::abs(m.points[nid * 3 + 0] - xface) <= tol) picked.insert(nid);
    return picked;
}

// (b) build the nodal force vector: consistent tri6 weights on whatever nodes are in `pick`,
// then split F by w_i/Σw (work-consistent) OR equal-split when Σw≈0 (the bug's fallback).
std::vector<double> consistent_axial_forces(const BoxMesh& m, std::set<int> pick, double F,
                                            bool apply_z1_fix) {
    const int nn = static_cast<int>(m.points.size() / 3);
    if (apply_z1_fix) {
        // add_midside_face_nodes: add every mid-edge node whose two corners are picked.
        const int ne = static_cast<int>(m.tets.size() / 10);
        bool grew = true;
        while (grew) {  // single pass suffices here; loop is belt-and-suspenders.
            grew = false;
            for (int e = 0; e < ne; ++e) {
                const int* el = &m.tets[e * 10];
                for (auto& ee : EDGE_ENDS)
                    if (pick.count(el[ee[1]]) && pick.count(el[ee[2]]) && !pick.count(el[ee[0]]))
                        { pick.insert(el[ee[0]]); grew = true; }
            }
        }
    }
    // accumulate_face_weights over faces whose 3 corners are all picked: corner 0, mid A/3.
    std::vector<double> w(static_cast<size_t>(nn), 0.0);
    const int ne = static_cast<int>(m.tets.size() / 10);
    for (int e = 0; e < ne; ++e) {
        const int* el = &m.tets[e * 10];
        for (int f = 0; f < 4; ++f) {
            const int c0 = el[FACE_CORNERS[f][0]], c1 = el[FACE_CORNERS[f][1]],
                      c2 = el[FACE_CORNERS[f][2]];
            if (!pick.count(c0) || !pick.count(c1) || !pick.count(c2)) continue;
            const double* p0 = &m.points[c0 * 3];
            const double* p1 = &m.points[c1 * 3];
            const double* p2 = &m.points[c2 * 3];
            const double ax = p1[0]-p0[0], ay = p1[1]-p0[1], az = p1[2]-p0[2];
            const double bx = p2[0]-p0[0], by = p2[1]-p0[1], bz = p2[2]-p0[2];
            const double nx = ay*bz-az*by, ny = az*bx-ax*bz, nz = ax*by-ay*bx;
            const double area = 0.5 * std::sqrt(nx*nx + ny*ny + nz*nz);
            if (area < 1e-12) continue;
            for (int i = 0; i < 3; ++i) w[el[FACE_MIDS[f][i]]] += area / 3.0;
        }
    }
    double w_sum = 0.0;
    for (int nid : pick) w_sum += w[nid];
    std::vector<double> forces(static_cast<size_t>(nn) * 3, 0.0);
    if (w_sum > 1e-9) {
        // FIX path: F split by the consistent tributary w_i/Σw — corners 0, mid-sides F/3.
        // (Resultant == F; the load is the true work-equivalent of a uniform end traction.)
        const double invw = 1.0 / w_sum;
        for (int nid : pick) forces[nid * 3 + 0] += F * w[nid] * invw;  // +x axial
    } else {
        // BUG path: the consistent weights all landed on mid-side nodes that are NOT in the
        // corner pick, so w_sum collapses to 0 and the live code degenerates to an equal split
        // over the CORNER pick. The resultant is still F (so the Saint-Venant mid-slab nominal
        // is unharmed), but the load is piled onto the end-face corner nodes → a spurious,
        // mesh-fineness-dependent stress spike AT THE LOADED END (the ~21× a user reads off the
        // summary max von Mises, plus the inflated max displacement). This is Bug Z1's symptom.
        const double inv = 1.0 / static_cast<double>(pick.size());
        for (int nid : pick) forces[nid * 3 + 0] += F * inv;
    }
    return forces;
}

// Mid-length-slab average von Mises for a solved axial bar (the NOMINAL field, Saint-Venant
// clear of both ends — insensitive to the exact end distribution as long as the resultant is
// F, so this stays ≈ F/A even for the bug; it is the convergence anchor, not the discriminator).
double mid_slab_vm(const BoxMesh& m, const FeaStaticResult& r, double Lx) {
    const int nn = static_cast<int>(m.points.size() / 3);
    double acc = 0.0; int cnt = 0;
    for (int nid = 0; nid < nn; ++nid)
        if (std::abs(m.points[nid * 3 + 0] - Lx / 2.0) < Lx / 8.0) { acc += r.von_mises[nid]; ++cnt; }
    return cnt ? acc / cnt : 0.0;
}

// PEAK von Mises in the LOADED-END region (x in (Lx*7/8, Lx]) — i.e. the loaded end, away
// from the fixed end. With the work-consistent load this stays ~F/A (a uniform traction has
// no stress raiser). With the Z1 bug the load piles onto a few near-corner nodes → a large,
// MESH-FINENESS-DEPENDENT spike here (the ~21× number a user reads off the result summary).
double loaded_end_peak_vm(const BoxMesh& m, const FeaStaticResult& r, double Lx) {
    const int nn = static_cast<int>(m.points.size() / 3);
    double peak = 0.0;
    for (int nid = 0; nid < nn; ++nid)
        if (m.points[nid * 3 + 0] > Lx * 7.0 / 8.0) peak = std::max(peak, r.von_mises[nid]);
    return peak;
}
}  // namespace tet10load

// F1/Z1 — AXIAL bar on a tet10 mesh: with the WORK-EQUIVALENT end load the nominal stress is
// F/A AND the loaded-end PEAK von Mises is bounded ~F/A and MESH-INDEPENDENT; with the Z1 bug
// the loaded-end peak spikes and is mesh-fineness dependent. The load is applied through the
// SAME corner-pick + tri6-weight + add_midside_face_nodes path fea_run_static uses live.
//
// WHY THE OLD F1 TEST MISSED THE BUG: it hand-built an equal split over the end nodes whose
// RESULTANT was F, and only checked the MID-SLAB nominal. By Saint-Venant the mid-slab is F/A
// for ANY end distribution with resultant F — including the broken one — so the old check
// could never fail on Z1. The real symptom (~21×) is the loaded-END PEAK that the summary
// max_von_mises_mpa reports, plus the inflated max displacement. THIS test asserts both:
//   (1) FIX: loaded-end peak ≈ F/A on a coarse AND a fine mesh, and the two agree (convergent);
//   (2) BUG: loaded-end peak is inflated AND mesh-fineness dependent (would fail pre-Z1).
struct AxialOut { double vm_mid; double vm_end_peak; double max_disp; };
TEST_CASE("axial bar: tet10 work-equivalent end load is bounded & mesh-independent (Z1)") {
    const double Lx = 100.0, Ly = 10.0, Lz = 10.0;
    const double E = 200000.0, nu = 0.3;            // steel
    const double A = Ly * Lz, F = 10000.0;          // sigma_nom = F/A = 10000/100 = 100 MPa
    const double sigma_nom = F / A;                 // 100 MPa
    const double disp_nom = F * Lx / (A * E);       // axial δ = F L /(A E) = 0.05 mm
    const double tol = 0.5;                         // plane tol: picks corner nodes on x=Lx

    // Solve the axial bar at a given mesh fineness with the chosen distribution.
    auto solve_axial = [&](int nx, int ny, int nz, bool apply_z1_fix) -> AxialOut {
        const int n_corner = (nx + 1) * (ny + 1) * (nz + 1);  // tet4 nodes = tet10 corners
        BoxMesh m = promote_to_tet10(make_box_tet4(nx, ny, nz, Lx, Ly, Lz));
        const int nn = static_cast<int>(m.points.size() / 3);
        // Fix the WHOLE x=0 end (corners + mid-sides) so the built-in face is fully clamped.
        std::vector<int> fixed;
        for (int nid = 0; nid < nn; ++nid)
            if (std::abs(m.points[nid * 3 + 0]) < 1e-9) fixed.push_back(nid);
        // Corner-only pick on the x=Lx plane — the Z1 bug condition.
        std::set<int> pick = tet10load::corner_face_pick(m, n_corner, Lx, tol);
        REQUIRE(!pick.empty());
        std::vector<double> forces =
            tet10load::consistent_axial_forces(m, pick, F, apply_z1_fix);
        // Total applied +x force MUST equal F regardless of distribution (resultant sanity).
        double fx = 0.0;
        for (int nid = 0; nid < nn; ++nid) fx += forces[nid * 3 + 0];
        CHECK(fx == doctest::Approx(F).epsilon(1e-9));
        FeaStaticResult r = fea_static_solve(m.points, m.tets, E, nu, 250.0, fixed, forces, 10);
        REQUIRE(r.ok);
        return {tet10load::mid_slab_vm(m, r, Lx), tet10load::loaded_end_peak_vm(m, r, Lx),
                r.max_disp_mm};
    };

    // ── (1) FIX: nominal AND loaded-end peak ≈ F/A, both mesh-independent ──────────────────
    const AxialOut fc = solve_axial(10, 2, 2, /*fix=*/true);
    const AxialOut ff = solve_axial(20, 4, 4, /*fix=*/true);
    MESSAGE("Z1 FIX  mid vm coarse=", fc.vm_mid, " fine=", ff.vm_mid, " | end-peak coarse=",
            fc.vm_end_peak, " fine=", ff.vm_end_peak, " | F/A=", sigma_nom);
    // Nominal mid-slab stress = F/A (Saint-Venant; the convergence anchor).
    CHECK(fc.vm_mid == doctest::Approx(sigma_nom).epsilon(0.12));
    CHECK(ff.vm_mid == doctest::Approx(sigma_nom).epsilon(0.12));
    // The uniform work-consistent traction raises NO real stress concentration: the loaded-end
    // peak stays bounded near F/A (small quadratic-recovery scatter only — NOT the bug's spike)
    // and barely moves with mesh refinement.
    CHECK(fc.vm_end_peak < 2.0 * sigma_nom);                       // bounded (no spike)
    CHECK(ff.vm_end_peak < 2.0 * sigma_nom);
    CHECK(std::abs(ff.vm_end_peak - fc.vm_end_peak) / sigma_nom < 0.50);   // ~mesh-independent
    // Max displacement matches the analytic axial elongation δ = F L /(A E).
    CHECK(fc.max_disp == doctest::Approx(disp_nom).epsilon(0.15));
    CHECK(ff.max_disp == doctest::Approx(disp_nom).epsilon(0.15));

    // ── (2) BUG: the corner-concentrated load spikes the loaded end AND is non-convergent ──
    // With the fix disabled, w_sum collapses → F piles onto the end-face corner nodes → a large
    // loaded-end stress spike whose magnitude RISES with mesh refinement (the tributary per
    // loaded corner shrinks), and an inflated max displacement. These assertions FAIL on the
    // pre-Z1 code path, so the test genuinely guards the fix rather than just the analytic value.
    const AxialOut bc = solve_axial(10, 2, 2, /*fix=*/false);
    const AxialOut bf = solve_axial(20, 4, 4, /*fix=*/false);
    MESSAGE("Z1 BUG  end-peak coarse=", bc.vm_end_peak, " fine=", bf.vm_end_peak,
            " | max_disp coarse=", bc.max_disp, " fine=", bf.max_disp);
    // PRIMARY, ROBUST DISCRIMINATOR (independent of absolute magnitudes): the work-consistent
    // (fixed) loaded-end peak must be DRAMATICALLY tamer than the corner-concentrated (bug) one
    // on the SAME fine mesh. This single comparison fails on the pre-Z1 path and passes after.
    CHECK(ff.vm_end_peak < 0.6 * bf.vm_end_peak);
    // The broken load drives a loaded-end peak clearly above the bounded work-consistent value.
    CHECK(bf.vm_end_peak > 1.6 * ff.vm_end_peak);
    // And it is NON-CONVERGENT: the concentrated peak changes materially between the two meshes
    // (a bogus point-load signature), whereas the work-consistent peak above stayed put.
    CHECK(std::abs(bf.vm_end_peak - bc.vm_end_peak) >
          std::abs(ff.vm_end_peak - fc.vm_end_peak));
}

// F4 — static deflection scales as 1/E (the solver uses the E it is GIVEN). A cantilever
// solved with steel E and again with aluminium E must deflect in EXACT inverse proportion:
// delta_Al / delta_steel == E_steel / E_Al. This is the kernel-level proof that the static
// solver is NOT silently substituting a wrong (e.g. steel) modulus for a non-steel material
// — if it ever defaulted to steel, this ratio would collapse to ~1. (The displacement field
// is linear in 1/E for fixed geometry/load, so the ratio is exact to solver tolerance.)
TEST_CASE("static deflection scales exactly as 1/E across materials (F4)") {
    const double Lx = 100.0, Ly = 10.0, Lz = 10.0;
    const double nu = 0.30, Fy = -500.0;
    const double E_steel = 200000.0, E_alu = 69000.0, E_ti = 113800.0;
    BoxMesh m4 = make_box_tet4(16, 3, 3, Lx, Ly, Lz);
    BoxMesh m = promote_to_tet10(m4);
    const int nn = static_cast<int>(m.points.size() / 3);

    std::vector<int> fixed, tip;
    for (int nid = 0; nid < nn; ++nid) {
        const double x = m.points[nid * 3 + 0];
        if (std::abs(x) < 1e-9) fixed.push_back(nid);
        if (std::abs(x - Lx) < 1e-9) tip.push_back(nid);
    }
    std::vector<double> forces(static_cast<size_t>(nn) * 3, 0.0);
    const double per = Fy / static_cast<double>(tip.size());
    for (int nid : tip) forces[nid * 3 + 1] += per;

    auto solve = [&](double E) {
        return fea_static_solve(m.points, m.tets, E, nu, 250.0, fixed, forces, 10);
    };
    FeaStaticResult rs = solve(E_steel), ra = solve(E_alu), rt = solve(E_ti);
    REQUIRE(rs.ok); REQUIRE(ra.ok); REQUIRE(rt.ok);
    MESSAGE("delta steel=", rs.max_disp_mm, " alu=", ra.max_disp_mm, " ti=", rt.max_disp_mm);
    // Aluminium (lower E) MUST deflect MORE than steel, exactly in the E ratio.
    CHECK(ra.max_disp_mm > rs.max_disp_mm);
    CHECK(ra.max_disp_mm / rs.max_disp_mm == doctest::Approx(E_steel / E_alu).epsilon(1e-3));
    CHECK(rt.max_disp_mm / rs.max_disp_mm == doctest::Approx(E_steel / E_ti).epsilon(1e-3));
}

// Cantilever tip deflection near the Euler-Bernoulli reference (locks the static bending
// magnitude the work-consistent load + tet10 element must reproduce). delta = F L^3/(3 E I),
// I = b h^3/12. tet10 lands within ~25% on a coarse mesh (shear + discretization keep it a
// touch under); the gate is that it is in the right ballpark, not collapsed or inflated.
TEST_CASE("cantilever tip deflection matches F L^3/(3 E I) within tolerance") {
    const double Lx = 100.0, Ly = 10.0, Lz = 10.0;
    const double E = 200000.0, nu = 0.30, Fy = -1000.0;
    BoxMesh m4 = make_box_tet4(20, 4, 4, Lx, Ly, Lz);
    BoxMesh m = promote_to_tet10(m4);
    const int nn = static_cast<int>(m.points.size() / 3);
    std::vector<int> fixed, tip;
    for (int nid = 0; nid < nn; ++nid) {
        const double x = m.points[nid * 3 + 0];
        if (std::abs(x) < 1e-9) fixed.push_back(nid);
        if (std::abs(x - Lx) < 1e-9) tip.push_back(nid);
    }
    std::vector<double> forces(static_cast<size_t>(nn) * 3, 0.0);
    const double per = Fy / static_cast<double>(tip.size());
    for (int nid : tip) forces[nid * 3 + 1] += per;
    FeaStaticResult r = fea_static_solve(m.points, m.tets, E, nu, 250.0, fixed, forces, 10);
    REQUIRE(r.ok);
    const double I = Ly * Lz * Lz * Lz / 12.0;
    const double delta = std::abs(Fy) * Lx * Lx * Lx / (3.0 * E * I);
    MESSAGE("cantilever delta=", r.max_disp_mm, " analytic=", delta);
    CHECK(r.max_disp_mm > 0.75 * delta);
    CHECK(r.max_disp_mm < 1.25 * delta);
}

// ── G4: run_wind_tunnel ref_length_mm uses the PART bbox, not the padded domain ──────
//
// Root cause: if lo[]/hi[] were ever accumulated from domain-offset coordinates the span
// would remain correct (translation-invariant), but if ref_length were taken from the
// domain extent it would be 4-5× the part.  The fix isolates the bbox pass over the
// original body_pos vertices BEFORE the in-place axis-swap mutation.
//
// Invariants:
//   - A 50mm cube (vertices -25..+25, centered) → ref_length_mm == 50.
//   - A Ø50 sphere approximation (icosphere-like, max vertex radius ~25) → ref_length_mm ~50.
//   - A 50×25×10 box → ref_length_mm == 50 (longest span).
//   - At AoA=0 all three pass; at nonzero AoA the rotated spans must still be finite.
TEST_CASE("G4: run_wind_tunnel ref_length_mm equals the part's longest bbox span") {
    using aicad::sim::FlowParams;
    using aicad::sim::run_wind_tunnel;

    // Build a watertight box mesh for a cuboid [lo,hi]^3 in each axis.
    // 6 faces × 2 triangles each = 12 triangles; vertices at the 8 corners.
    auto make_box_mesh = [](float x0, float y0, float z0,
                            float x1, float y1, float z1)
        -> std::pair<std::vector<float>, std::vector<uint32_t>> {
        // 8 corners
        std::vector<float> p = {
            x0,y0,z0,  x1,y0,z0,  x1,y1,z0,  x0,y1,z0,  // z=z0 face
            x0,y0,z1,  x1,y0,z1,  x1,y1,z1,  x0,y1,z1,  // z=z1 face
        };
        // 12 triangles (2 per face), outward normals
        std::vector<uint32_t> idx = {
            0,2,1, 0,3,2,  // -Z face
            4,5,6, 4,6,7,  // +Z face
            0,1,5, 0,5,4,  // -Y face
            2,3,7, 2,7,6,  // +Y face
            0,4,7, 0,7,3,  // -X face
            1,2,6, 1,6,5,  // +X face
        };
        return {p, idx};
    };

    FlowParams fp;
    fp.velocity_ms = 10.0;
    fp.cells       = 32;   // coarse — just enough to voxelize a 50mm body
    fp.n_steps     = 50;   // minimal steps; we only care about the bbox metadata

    // ── Case 1: 50mm cube centered on the origin ──────────────────────────────
    // make_box_centered(50,50,50) produces vertices from -25 to +25 in each axis.
    // ref_length_mm must be 50, not 4×50=200 (domain padding) or 5×50=250 (domain total).
    {
        auto [pos, idx] = make_box_mesh(-25.f,-25.f,-25.f, 25.f,25.f,25.f);
        double re = 0.0;
        auto r = run_wind_tunnel({pos}, {idx}, fp, &re);
        INFO("50mm centered cube: ok=", r.ok, " ref_length_mm=", r.ref_length_mm,
             " error=", r.error);
        REQUIRE(r.ok);
        // ref_length_mm must equal the part's longest span (50), NOT the domain extent.
        CHECK(r.ref_length_mm == doctest::Approx(50.0).epsilon(1e-6));
    }

    // ── Case 2: 50mm cube at a non-zero origin (corner at 0) ─────────────────
    // Translation must not change the span.
    {
        auto [pos, idx] = make_box_mesh(0.f,0.f,0.f, 50.f,50.f,50.f);
        double re = 0.0;
        auto r = run_wind_tunnel({pos}, {idx}, fp, &re);
        INFO("50mm corner-at-0 cube: ok=", r.ok, " ref_length_mm=", r.ref_length_mm);
        REQUIRE(r.ok);
        CHECK(r.ref_length_mm == doctest::Approx(50.0).epsilon(1e-6));
    }

    // ── Case 3: non-cube box (longest span = 50) ─────────────────────────────
    // A 50×25×10 box: longest span is 50 in X. ref_length_mm must be 50.
    {
        auto [pos, idx] = make_box_mesh(-25.f,-12.5f,-5.f, 25.f,12.5f,5.f);
        double re = 0.0;
        auto r = run_wind_tunnel({pos}, {idx}, fp, &re);
        INFO("50x25x10 box: ok=", r.ok, " ref_length_mm=", r.ref_length_mm);
        REQUIRE(r.ok);
        CHECK(r.ref_length_mm == doctest::Approx(50.0).epsilon(1e-6));
    }

    // ── Case 4: sphere approximation — Ø50 (radius 25) ───────────────────────
    // An octahedron of radius 25 (6 vertices, 8 faces) is the simplest closed
    // convex hull that bounds a sphere.  Vertices sit at ±25 on each axis, so
    // the bounding box is exactly 50mm in all three axes.
    // Vertices: 0=+X, 1=-X, 2=+Y, 3=-Y, 4=+Z, 5=-Z
    {
        const float R = 25.0f;
        std::vector<float> pos = {
             R, 0, 0,   // 0: +X
            -R, 0, 0,   // 1: -X
             0, R, 0,   // 2: +Y
             0,-R, 0,   // 3: -Y
             0, 0, R,   // 4: +Z
             0, 0,-R,   // 5: -Z
        };
        // 8 outward-facing triangles (two fans around +Z and -Z poles)
        std::vector<uint32_t> idx = {
            0,4,2,  2,4,1,  1,4,3,  3,4,0,   // +Z fan
            0,2,5,  2,1,5,  1,3,5,  3,0,5,   // -Z fan
        };
        double re = 0.0;
        auto r = run_wind_tunnel({pos}, {idx}, fp, &re);
        INFO("Ø50 octahedron: ok=", r.ok, " ref_length_mm=", r.ref_length_mm);
        REQUIRE(r.ok);
        // Octahedron vertices sit exactly at ±25 → bbox is 50mm in all axes.
        CHECK(r.ref_length_mm == doctest::Approx(50.0).epsilon(1e-6));
    }
}

}  // namespace
