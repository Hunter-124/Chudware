// Modal FEA suite — locks the round-3 N-1 fix (consistent mass matrix). A square-section
// cantilever has two orthogonal first-bending modes that MUST be near-degenerate. The old
// diagonal-lumped mass split them ~10% and read ~20% low; the consistent mass restores the
// pair. We mesh the beam with a y<->z SYMMETRIC Kuhn (6-tet-per-cube) decomposition so any
// residual split is the mass formulation's, not mesh asymmetry.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <array>
#include <cmath>
#include <map>
#include <utility>
#include <vector>

#include "sim/fea_modal.h"

namespace {

// Build a beam [0,L] x [0,a] x [0,a] meshed nx x 1 x 1 cells, each cube split into the 6
// Kuhn tets sharing the main diagonal. That decomposition is invariant under swapping the
// y and z axes, so the mesh itself is y<->z symmetric (no mesh-induced mode split).
struct Mesh {
    std::vector<double> pts;   // 3*N
    std::vector<int> tets;     // 4*M
    std::vector<int> fixed;    // nodes at x≈0
};

// Rectangular-section variant: [0,L] x [0,ay] x [0,az], meshed nx x ny x nz Kuhn cells.
Mesh beam_mesh_rect(double L, double ay, double az, int nx, int ny, int nz) {
    Mesh m;
    auto nid = [&](int i, int j, int k) { return (i * (ny + 1) + j) * (nz + 1) + k; };
    for (int i = 0; i <= nx; ++i)
        for (int j = 0; j <= ny; ++j)
            for (int k = 0; k <= nz; ++k) {
                m.pts.push_back(L * i / nx);
                m.pts.push_back(ay * j / ny);
                m.pts.push_back(az * k / nz);
                if (i == 0) m.fixed.push_back(nid(i, j, k));
            }
    const int paths[6][4][3] = {
        {{0,0,0},{1,0,0},{1,1,0},{1,1,1}}, {{0,0,0},{1,0,0},{1,0,1},{1,1,1}},
        {{0,0,0},{0,1,0},{1,1,0},{1,1,1}}, {{0,0,0},{0,1,0},{0,1,1},{1,1,1}},
        {{0,0,0},{0,0,1},{1,0,1},{1,1,1}}, {{0,0,0},{0,0,1},{0,1,1},{1,1,1}},
    };
    for (int ci = 0; ci < nx; ++ci)
        for (int cj = 0; cj < ny; ++cj)
            for (int ck = 0; ck < nz; ++ck)
                for (int t = 0; t < 6; ++t)
                    for (int v = 0; v < 4; ++v) {
                        const int* d = paths[t][v];
                        m.tets.push_back(nid(ci + d[0], cj + d[1], ck + d[2]));
                    }
    return m;
}

Mesh beam_mesh(double L, double a, int nx, int ny, int nz) {
    Mesh m;
    auto nid = [&](int i, int j, int k) { return (i * (ny + 1) + j) * (nz + 1) + k; };
    for (int i = 0; i <= nx; ++i)
        for (int j = 0; j <= ny; ++j)
            for (int k = 0; k <= nz; ++k) {
                m.pts.push_back(L * i / nx);
                m.pts.push_back(a * j / ny);
                m.pts.push_back(a * k / nz);
                if (i == 0) m.fixed.push_back(nid(i, j, k));
            }
    // 6 Kuhn paths (orderings of incrementing x,y,z): each is {000, mid1, mid2, 111}. The
    // set is invariant under y<->z swap, so a grid with ny==nz stays globally symmetric.
    const int paths[6][4][3] = {
        {{0,0,0},{1,0,0},{1,1,0},{1,1,1}}, {{0,0,0},{1,0,0},{1,0,1},{1,1,1}},
        {{0,0,0},{0,1,0},{1,1,0},{1,1,1}}, {{0,0,0},{0,1,0},{0,1,1},{1,1,1}},
        {{0,0,0},{0,0,1},{1,0,1},{1,1,1}}, {{0,0,0},{0,0,1},{0,1,1},{1,1,1}},
    };
    for (int ci = 0; ci < nx; ++ci)
        for (int cj = 0; cj < ny; ++cj)
            for (int ck = 0; ck < nz; ++ck)
                for (int t = 0; t < 6; ++t)
                    for (int v = 0; v < 4; ++v) {
                        const int* d = paths[t][v];
                        m.tets.push_back(nid(ci + d[0], cj + d[1], ck + d[2]));
                    }
    return m;
}

// Promote a tet4 mesh to tet10 by inserting a mid-edge node on every unique edge. The
// 6 local edges are emitted in the SAME order fea_modal_solve's tet10 shape functions
// expect: {0,1},{1,2},{2,0},{3,0},{3,2},{3,1}. Mid-edge nodes whose two endpoints are
// both at x≈0 are added to `fixed` so the clamped face stays fully built-in.
struct Mesh10 {
    std::vector<double> pts;
    std::vector<int> tets;   // 10*M
    std::vector<int> fixed;
};

Mesh10 to_tet10(const Mesh& m4) {
    Mesh10 m;
    m.pts = m4.pts;
    std::map<std::pair<int, int>, int> mid;
    auto midnode = [&](int a, int b) {
        auto key = std::minmax(a, b);
        auto it = mid.find({key.first, key.second});
        if (it != mid.end()) return it->second;
        int id = static_cast<int>(m.pts.size() / 3);
        for (int c = 0; c < 3; ++c)
            m.pts.push_back(0.5 * (m.pts[a * 3 + c] + m.pts[b * 3 + c]));
        mid.emplace(std::make_pair(key.first, key.second), id);
        return id;
    };
    const int LE[6][2] = {{0, 1}, {1, 2}, {2, 0}, {3, 0}, {3, 2}, {3, 1}};
    const int nt = static_cast<int>(m4.tets.size() / 4);
    for (int e = 0; e < nt; ++e) {
        int c[4];
        for (int j = 0; j < 4; ++j) c[j] = m4.tets[e * 4 + j];
        for (int j = 0; j < 4; ++j) m.tets.push_back(c[j]);
        for (int ed = 0; ed < 6; ++ed) m.tets.push_back(midnode(c[LE[ed][0]], c[LE[ed][1]]));
    }
    const int nn = static_cast<int>(m.pts.size() / 3);
    for (int n = 0; n < nn; ++n)
        if (std::abs(m.pts[n * 3]) < 1e-9) m.fixed.push_back(n);
    return m;
}

}  // namespace

// The CONSISTENT mass matrix is correct iff a FREE-FREE body has exactly 6 (near-)zero
// rigid-body modes and a positive first elastic mode. A diagonal-lumped mass passes the
// rank test too, but a botched consistent-mass assembly (asymmetric / wrong coefficients)
// would corrupt the rigid-body null space. This is the formulation check behind Bug N-1.
TEST_CASE("modal: free-free body has 6 rigid-body modes then a positive elastic mode") {
    Mesh m = beam_mesh(100.0, 12.0, 10, 2, 2);
    auto r = aicad::sim::fea_modal_solve(m.pts, m.tets, 210000.0, 0.30, 7.85,
                                         /*fixed*/ {}, /*n_modes*/ 9);
    REQUIRE(r.ok);
    REQUIRE(r.freqs_hz.size() >= 8);
    // First six are rigid-body (≈0); the lowest elastic mode (index 6) is clearly nonzero.
    for (int i = 0; i < 6; ++i) CHECK(r.freqs_hz[i] < 5.0);
    CHECK(r.freqs_hz[6] > 100.0);
    CHECK(r.freqs_hz[7] >= r.freqs_hz[6]);  // ascending
}

// A cantilever's bending modes come out as REAL, positive, ascending frequencies in a
// physically sane band. (Tight first-pair degeneracy needs a 4-fold-symmetric mesh; a
// single-diagonal tet mesh stays mildly anisotropic — see N-1 notes — so we don't assert
// it here. The consistent mass removes the lumped-mass low bias, which this band checks.)
TEST_CASE("modal: square cantilever bending modes are sane") {
    Mesh m = beam_mesh(100.0, 12.0, 20, 3, 3);
    auto r = aicad::sim::fea_modal_solve(m.pts, m.tets, 210000.0, 0.30, 7.85,
                                         m.fixed, /*n_modes*/ 4);
    REQUIRE(r.ok);
    REQUIRE(r.freqs_hz.size() >= 2);
    const double f1 = r.freqs_hz[0], f2 = r.freqs_hz[1];
    CHECK(f1 > 400.0);     // not collapsed / not the lumped-mass ~20%-low reading
    CHECK(f1 < 1700.0);    // not absurd (coarse linear tets read a little stiff)
    CHECK(f2 >= f1);       // ascending
    CHECK(f2 < 1.5 * f1);  // the two are the first-bending pair, not f1 + a far higher mode
}

// N-1, the real cure: 2nd-order (tet10) elements. On the SAME beam the quadratic element
// (a) reads close to the Euler-Bernoulli reference (~1000 Hz for this 100x12x12 steel
// cantilever) instead of the coarse-tet4 bias, and (b) keeps the first bending PAIR tightly
// degenerate (no ~10% square-section split). We assert both, plus that tet10 reads LOWER
// (less over-stiff) than tet4 on the identical geometry.
TEST_CASE("modal: tet10 reads near Euler-Bernoulli with a tight bending pair (N-1)") {
    Mesh m4 = beam_mesh(100.0, 12.0, 16, 2, 2);
    Mesh10 m = to_tet10(m4);
    auto r = aicad::sim::fea_modal_solve(m.pts, m.tets, 210000.0, 0.30, 7.85,
                                         m.fixed, /*n_modes*/ 4, /*nodes_per_elem*/ 10);
    REQUIRE(r.ok);
    REQUIRE(r.freqs_hz.size() >= 2);
    const double f1 = r.freqs_hz[0], f2 = r.freqs_hz[1];
    CHECK(f1 > 850.0);      // near the ~1000 Hz Euler-Bernoulli first bending, not low/stiff
    CHECK(f1 < 1150.0);
    CHECK(f2 >= f1);        // ascending
    CHECK(f2 < 1.06 * f1);  // first bending PAIR near-degenerate — the N-1 split is gone

    // Same geometry, tet4: more over-stiff than tet10 (so its f1 sits higher).
    auto r4 = aicad::sim::fea_modal_solve(m4.pts, m4.tets, 210000.0, 0.30, 7.85,
                                          m4.fixed, /*n_modes*/ 4, /*nodes_per_elem*/ 4);
    REQUIRE(r4.ok);
    CHECK(r4.freqs_hz[0] >= f1);  // tet4 stiffer => not below the tet10 reading
}

// F3 — NON-SQUARE cantilever first bending near the analytic value. A 200(x) x 20(y) x
// 10(z) steel cantilever bends most easily about the y-axis (the 10 mm dimension), giving
// Euler-Bernoulli f1 = (1.875^2/2pi) sqrt(E I / (rho A L^4)) ~ 209 Hz with I = b h^3/12,
// b=20, h=10. The consistent tet10 mass (M_ij = rho * mapped-volume * exact N_iN_j moments)
// must read in that band — the old corner-tet-volume mass mis-stated the inertia on
// distorted high-aspect elements and read several-fold high (Bug F3). On a STRAIGHT mesh the
// Gauss-integrated mapped volume equals the corner volume exactly, so this also confirms the
// fix did not perturb a regular mesh. f2 (bending about the 20 mm dimension) sits higher,
// near ~418 Hz, and must be well above f1.
TEST_CASE("modal: non-square cantilever f1 near Euler-Bernoulli (~209 Hz) (F3)") {
    Mesh m4 = beam_mesh_rect(200.0, 20.0, 10.0, 24, 3, 2);
    Mesh10 m = to_tet10(m4);
    auto r = aicad::sim::fea_modal_solve(m.pts, m.tets, 210000.0, 0.30, 7.85,
                                         m.fixed, /*n_modes*/ 3, /*nodes_per_elem*/ 10);
    REQUIRE(r.ok);
    REQUIRE(r.freqs_hz.size() >= 2);
    const double f1 = r.freqs_hz[0], f2 = r.freqs_hz[1];
    MESSAGE("F3 non-square f1=", f1, " f2=", f2, " (analytic weak~209, strong~418)");
    // f1 near the 209 Hz weak-axis Euler-Bernoulli value. The band is generous on the high
    // side for the coarse straight mesh (slightly over-stiff), but its UPPER bound (300 Hz)
    // categorically rejects the ~762 Hz the corner-volume mass produced — F3's 3.x-too-high
    // signature. Lower bound guards against a collapse.
    CHECK(f1 > 150.0);
    CHECK(f1 < 300.0);
    CHECK(f2 >= f1);               // ascending
    CHECK(f2 > 1.25 * f1);         // strong-axis bending is distinctly higher (not degenerate)
}

// F6 — a FREE-FREE (no fixtures) non-square body is a VALID unconstrained solve: it must
// return ~6 rigid-body modes near 0 Hz and then positive elastic modes, NOT an error. The
// solver already supports fixtures=={} (the negative spectral shift keeps the singular K
// factorable); this pins that contract for a rectangular section too.
TEST_CASE("modal: free-free non-square body returns rigid-body modes, not an error (F6)") {
    Mesh m = beam_mesh_rect(120.0, 20.0, 10.0, 12, 2, 1);
    auto r = aicad::sim::fea_modal_solve(m.pts, m.tets, 210000.0, 0.30, 7.85,
                                         /*fixed*/ {}, /*n_modes*/ 9);
    REQUIRE(r.ok);                       // empty fixtures is NOT an error
    REQUIRE(r.freqs_hz.size() >= 8);
    for (int i = 0; i < 6; ++i) CHECK(r.freqs_hz[i] < 5.0);   // 6 rigid-body modes ~0 Hz
    CHECK(r.freqs_hz[6] > 50.0);                              // first elastic mode is positive
    CHECK(r.freqs_hz[7] >= r.freqs_hz[6]);                    // ascending
}

// C4 regression (fixed 2026-07-12): with n_modes=8 on a slender tet10 free-free beam the
// old solver (k_req = n_modes, ncv = 2k+1) converged only PART of the 6-fold-degenerate
// rigid cluster and silently substituted elastic modes — observed 3 rigid + 5 elastic.
// The solver now over-requests + widens the subspace + verifies the rigid count
// internally, so the caller gets all 6 rigid modes even at small n_modes.
TEST_CASE("modal: free-free tet10 slender beam resolves ALL 6 rigid modes at n_modes=8 (C4)") {
    Mesh m4 = beam_mesh(100.0, 12.0, 16, 2, 2);
    Mesh10 m = to_tet10(m4);
    auto r = aicad::sim::fea_modal_solve(m.pts, m.tets, 210000.0, 0.30, 7.85,
                                         /*fixed*/ {}, /*n_modes*/ 8, /*npe*/ 10);
    REQUIRE(r.ok);
    REQUIRE(r.freqs_hz.size() == 8);  // truncated back to the caller's count
    int rigid = 0;
    for (double f : r.freqs_hz) if (f < 0.5) ++rigid;
    CHECK(rigid == 6);                                    // the full degenerate cluster
    CHECK(r.freqs_hz[6] > 100.0);                         // then real elastic modes
    CHECK(r.freqs_hz[7] >= r.freqs_hz[6]);                // ascending
}

TEST_CASE("modal: free-free rigid cluster survives even when n_modes < 6") {
    Mesh m = beam_mesh(100.0, 12.0, 10, 2, 2);
    auto r = aicad::sim::fea_modal_solve(m.pts, m.tets, 210000.0, 0.30, 7.85,
                                         /*fixed*/ {}, /*n_modes*/ 4);
    REQUIRE(r.ok);
    REQUIRE(r.freqs_hz.size() == 4);
    for (double f : r.freqs_hz) CHECK(f < 0.5);  // the 4 lowest are all rigid (~0 Hz)
}
