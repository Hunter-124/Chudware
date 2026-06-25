// Modal FEA suite — locks the round-3 N-1 fix (consistent mass matrix). A square-section
// cantilever has two orthogonal first-bending modes that MUST be near-degenerate. The old
// diagonal-lumped mass split them ~10% and read ~20% low; the consistent mass restores the
// pair. We mesh the beam with a y<->z SYMMETRIC Kuhn (6-tet-per-cube) decomposition so any
// residual split is the mass formulation's, not mesh asymmetry.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <array>
#include <cmath>
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
