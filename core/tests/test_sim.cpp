// Unit tests for the FEA static solver (chudware::sim::fea_static_solve) and the
// wind-tunnel LBM front-end (chudware::sim::run_wind_tunnel).
//
// The FEA tests are OCCT-free, oracle-free sanity checks on the numerics. The
// CFD test (G4) verifies that ref_length_mm comes from the PART bounding box, not
// the padded domain — a 50mm cube must report ref_length_mm=50, not 4×50=200.
// The full Python-vs-C++ differential gate lives in tools/cpp_sim_parity.py.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iterator>
#include <sstream>
#include <map>
#include <limits>
#include <set>
#include <string>
#include <stdexcept>
#include <thread>
#include <utility>
#include <vector>

#include "sim/fea_static.h"
#include "sim/flow_run.h"
#include "sim/lbm.h"
#ifdef CHUDWARE_WITH_CUDA
#include "sim/lbm_cuda.h"
#endif
#include "sim/pipe_flow.h"
#include "piping/solve.h"
#include "sim/sim_cancel.h"
#include "util/aicm_handoff.h"
#include "util/resource_governor.h"

using chudware::sim::fea_static_solve;
using chudware::sim::FeaStaticResult;

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

// A static study without a non-zero excitation is invalid setup, not a successful
// zero field (free-free modal is the intentional no-load exception).
TEST_CASE("zero-load static study is rejected") {
    std::vector<double> pts = {0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1};
    std::vector<int> tets = {0, 1, 2, 3};
    std::vector<int> fixed = {0, 1, 2};
    std::vector<double> f(12, 0.0);
    auto r = fea_static_solve(pts, tets, 200000.0, 0.3, 250.0, fixed, f);
    CHECK_FALSE(r.ok);
    CHECK_FALSE(r.error.empty());
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
    using chudware::sim::FlowParams;
    using chudware::sim::run_wind_tunnel;

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
        CHECK(r.ref_length_mm == doctest::Approx(50.0).epsilon(1e-6));
    }

    // ── Case 3: non-cube box (longest span = 50) ─────────────────────────────
    // A 50×25×10 box: longest span is 50 in X. ref_length_mm must be 50.
    {
        auto [pos, idx] = make_box_mesh(-25.f,-12.5f,-5.f, 25.f,12.5f,5.f);
        double re = 0.0;
        auto r = run_wind_tunnel({pos}, {idx}, fp, &re);
        INFO("50x25x10 box: ok=", r.ok, " ref_length_mm=", r.ref_length_mm);
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
        // Octahedron vertices sit exactly at ±25 → bbox is 50mm in all axes.
        CHECK(r.ref_length_mm == doctest::Approx(50.0).epsilon(1e-6));
    }
}

// ── run_flow_stl (sim_run_flow_stl): the STL-shaped front-end that replaces the retired ────
// Python sim worker's sim_run_flow. Verifies: (1) binary+ASCII STL load, (2) forces parity
// with the triangle-soup run_wind_tunnel path on the same geometry (direction +X / AoA 0 is
// the identity rotation, so the soup — and the cache key — is identical), (3) the worker
// envelope fields the sim panel consumes, (4) the ref-area / roughness post-processing,
// (5) the progress_path side-channel, (6) honest error envelopes.
TEST_CASE("run_flow_stl: STL front-end forces parity + worker envelope") {
    using chudware::sim::FlowParams;
    using chudware::sim::run_flow_stl;
    using chudware::sim::run_wind_tunnel;
    namespace fs = std::filesystem;

    // 20mm cube, corner at origin, 12 triangles (outward winding irrelevant to the fill).
    const std::vector<float> cube = {
        0,0,0,  20,0,0,  20,20,0,  0,20,0,
        0,0,20, 20,0,20, 20,20,20, 0,20,20,
    };
    const std::vector<uint32_t> cube_idx = {
        0,2,1, 0,3,2,  4,5,6, 4,6,7,  0,1,5, 0,5,4,
        2,3,7, 2,7,6,  0,4,7, 0,7,3,  1,2,6, 1,6,5,
    };

    const fs::path tmp = fs::temp_directory_path();
    const std::string ascii_path = (tmp / "chudware_test_flow_cube_ascii.stl").string();
    const std::string bin_path   = (tmp / "chudware_test_flow_cube_bin.stl").string();

    // ASCII STL (facet/vertex soup; normals zero — the loader ignores them).
    {
        std::ofstream f(ascii_path, std::ios::trunc);
        REQUIRE(f.good());
        f << "solid cube\n";
        for (size_t t = 0; t < cube_idx.size(); t += 3) {
            f << " facet normal 0 0 0\n  outer loop\n";
            for (int k = 0; k < 3; ++k) {
                const uint32_t vi = cube_idx[t + k];
                f << "   vertex " << cube[3 * vi] << " " << cube[3 * vi + 1] << " "
                  << cube[3 * vi + 2] << "\n";
            }
            f << "  endloop\n endfacet\n";
        }
        f << "endsolid cube\n";
    }
    // Binary STL (80-byte header + uint32 count + 50-byte records).
    {
        std::ofstream f(bin_path, std::ios::binary | std::ios::trunc);
        REQUIRE(f.good());
        char header[80] = {};
        f.write(header, sizeof header);
        const uint32_t ntri = (uint32_t)(cube_idx.size() / 3);
        f.write(reinterpret_cast<const char*>(&ntri), 4);
        for (uint32_t t = 0; t < ntri; ++t) {
            float rec[12] = {};  // normal(3) + 3 verts(9)
            for (int k = 0; k < 3; ++k) {
                const uint32_t vi = cube_idx[3 * t + k];
                rec[3 + 3 * k + 0] = cube[3 * vi];
                rec[3 + 3 * k + 1] = cube[3 * vi + 1];
                rec[3 + 3 * k + 2] = cube[3 * vi + 2];
            }
            f.write(reinterpret_cast<const char*>(rec), sizeof rec);
            const uint16_t attr = 0;
            f.write(reinterpret_cast<const char*>(&attr), 2);
        }
    }

    // Reference: the triangle-soup path on the same geometry, same resolution/steps.
    FlowParams fp;
    fp.velocity_ms = 0.1;
    fp.nu_fluid = 1.0e-4;  // low-Re regression keeps molecular tau safely above 0.5
    fp.cells = 32;
    fp.n_steps = 500;
    const chudware::sim::LbmResult open_surface =
        run_wind_tunnel({cube}, {{0, 2, 1}}, fp);
    CHECK_FALSE(open_surface.ok);
    CHECK(open_surface.steps_run == 0);
    CHECK(open_surface.error.find("closed two-manifold") != std::string::npos);
    double re_ref = 0.0;
    const chudware::sim::LbmResult ref = run_wind_tunnel({cube}, {cube_idx}, fp, &re_ref);
    REQUIRE_MESSAGE(ref.ok, "reference run_wind_tunnel failed: ", ref.error);
    // A closed skew body must surface its lateral force instead of hard-coding
    // side_n=0 in the public result.
    std::vector<float> skew = cube;
    skew[3 * 6 + 1] = 30.0f;
    const chudware::sim::LbmResult skew_result =
        run_wind_tunnel({skew}, {cube_idx}, fp);
    REQUIRE_MESSAGE(skew_result.ok, "skew-body solve failed: ", skew_result.error);
    REQUIRE(skew_result.converged);
    CHECK(std::abs(skew_result.side_n) > 1e-12);

    const std::string prog_path = (tmp / "chudware_test_flow_progress.json").string();
    nlohmann::json params = {
        {"stl_path", ascii_path},
        {"direction", {1, 0, 0}},
        {"speed_mps", 0.1},
        {"preset", "coarse"},
        {"cells", 32},
        {"n_steps", 500},
        {"aoa_deg", 0.0},
        {"sideslip_deg", 0.0},
        {"fluid", {{"name", "test-fluid"}, {"density_kgm3", 1.225}, {"kinematic_visc_m2s", 1.0e-4}}},
        {"job_id", "test_flow_stl"},
        {"progress_path", prog_path},
    };
    const nlohmann::json r = run_flow_stl(params);
    INFO("run_flow_stl result: ", r.dump());
    REQUIRE_MESSAGE(r.value("ok", false), "run_flow_stl failed: ", r.value("error", std::string()));

    // Forces parity: direction +X / AoA 0 is the identity rotation → same soup, same cache.
    CHECK(r.value("drag_n", 0.0) == doctest::Approx(ref.drag_n).epsilon(1e-9));
    CHECK(r.value("lift_n", 1.0) == doctest::Approx(ref.lift_n).epsilon(1e-9));
    CHECK(r.value("side_n", 1.0) == doctest::Approx(ref.side_n).epsilon(1e-9));
    CHECK(r.value("frontal_area_mm2", 0.0) == doctest::Approx(ref.frontal_area_mm2).epsilon(1e-9));
    CHECK(r.value("reynolds", 0.0) == doctest::Approx(re_ref).epsilon(1e-9));
    CHECK(r.value("mass_imbalance", 1.0) < 0.02);
    CHECK(r.value("converged", false));
    CHECK(r.value("drag_n", 0.0) > 0.0);

    // Envelope the sim panel consumes.
    CHECK(r.value("diverged", true) == false);
    CHECK(r.contains("reynolds_note"));
    CHECK(r.contains("solver_notes"));
    CHECK(r.contains("trace"));
    CHECK(r["scalar_fields"].is_array());
    CHECK_FALSE(r.contains("overlay_pending"));
    REQUIRE_FALSE(r.value("overlay_aicm", std::string()).empty());
    CHECK(r["scalar_fields"] == nlohmann::json{"pressure_cp", "velocity"});
    // The single spatial payload carries surface Cp/speed and both bounded line sets.
    chudware::aicm_handoff::Mapping mapping;
    std::string map_error;
    REQUIRE(chudware::aicm_handoff::map(r["overlay_aicm"].get<std::string>(), mapping, map_error));
    struct Unmap { chudware::aicm_handoff::Mapping& m; ~Unmap() { chudware::aicm_handoff::unmap(m); } } unmap{mapping};
    REQUIRE(mapping.size >= 12);
    CHECK(std::memcmp(mapping.data, "AICM", 4) == 0);
    std::set<std::string> scalar_names, line_names;
    size_t segments = 0;
    for (size_t offset = 12; offset + 12 <= mapping.size;) {
        uint64_t bytes = 0;
        std::memcpy(&bytes, mapping.data + offset + 4, sizeof bytes);
        REQUIRE(bytes <= mapping.size - offset - 12);
        const uint8_t* payload = mapping.data + offset + 12;
        const bool scalar = std::memcmp(mapping.data + offset, "SCLR", 4) == 0;
        const bool lines = std::memcmp(mapping.data + offset, "LINS", 4) == 0;
        if (scalar || lines) {
            const size_t name_offset = scalar ? 4 : 0;
            REQUIRE(bytes >= name_offset + 2);
            uint16_t length = 0;
            std::memcpy(&length, payload + name_offset, sizeof length);
            REQUIRE(bytes >= name_offset + 2 + length);
            const std::string name((const char*)payload + name_offset + 2, length);
            if (scalar) scalar_names.insert(name);
            else {
                line_names.insert(name);
                const size_t counts_offset = 2 + length;
                REQUIRE(bytes >= counts_offset + 4);
                uint32_t count = 0;
                std::memcpy(&count, payload + counts_offset, sizeof count);
                REQUIRE(bytes >= counts_offset + 4 + (uint64_t)count * 4);
                for (uint32_t i = 0; i < count; ++i) {
                    uint32_t points = 0;
                    std::memcpy(&points, payload + counts_offset + 4 + i * 4, sizeof points);
                    if (points) segments += points - 1;
                }
            }
        }
        offset += 12 + (size_t)bytes;
    }
    CHECK(scalar_names == std::set<std::string>{"pressure_cp", "velocity"});
    CHECK(line_names == std::set<std::string>{"streamlines", "slices"});
    CHECK(segments > 0);
    CHECK(segments < 100000);
    auto no_overlay = params;
    no_overlay["overlay"] = false;
    const auto numeric_only = run_flow_stl(no_overlay);
    REQUIRE(numeric_only.value("ok", false));
    CHECK(numeric_only.value("overlay_aicm", std::string()).empty());
    CHECK(numeric_only["scalar_fields"].empty());
    CHECK(numeric_only["drag_n"] == r["drag_n"]);
    // cd/cl use q·A_ref with auto-frontal area (mirror of the oracle's coefficients).
    const double q = 0.5 * 1.225 * 0.1 * 0.1;
    const double a_m2 = ref.frontal_area_mm2 * 1e-6;
    CHECK(r.value("cd", 0.0) == doctest::Approx(ref.drag_n / (q * a_m2)).epsilon(1e-9));
    CHECK(r.value("cl", 0.0) == doctest::Approx(ref.lift_n / (q * a_m2)).epsilon(1e-9));
    CHECK(r.value("vmax_mps", 0.0) >= 0.1);  // inlet speed is the field minimum

    // Binary STL → identical forces (same soup after load).
    nlohmann::json pb = params;
    pb["stl_path"] = bin_path;
    pb["job_id"] = "test_flow_stl_bin";
    const nlohmann::json rb = run_flow_stl(pb);
    REQUIRE_MESSAGE(rb.value("ok", false), "binary STL run failed: ", rb.dump());
    CHECK(rb.value("drag_n", 0.0) == doctest::Approx(ref.drag_n).epsilon(1e-9));

    // Progress side-channel: the final LBM checkpoint always writes pct=100.
    {
        std::ifstream pf(prog_path);
        REQUIRE_MESSAGE(pf.good(), "progress_path was not written");
        const nlohmann::json pj = nlohmann::json::parse(pf, nullptr, false);
        REQUIRE(!pj.is_discarded());
        CHECK(pj.value("pct", 0) == 100);
        CHECK(pj.value("stage", std::string()) == "lbm_solve");
    }

    // ref_area override: coefficients recompute against the USER area, drag unchanged.
    nlohmann::json pa = params;
    pa["ref_area_mm2"] = 800.0;  // 2x the 20x20 frontal
    pa["job_id"] = "test_flow_stl_refarea";
    const nlohmann::json ra = run_flow_stl(pa);
    REQUIRE(ra.value("ok", false));
    CHECK(ra.value("ref_area_mm2", 0.0) == doctest::Approx(800.0));
    CHECK(ra.value("cd", 0.0) == doctest::Approx(ref.drag_n / (q * 800e-6)).epsilon(1e-9));

    // Roughness: drag grows by the qualitative friction multiplier.
    nlohmann::json pr = params;
    pr["roughness_um"] = 100.0;
    pr["job_id"] = "test_flow_stl_rough";
    const nlohmann::json rr = run_flow_stl(pr);
    REQUIRE(rr.value("ok", false));
    CHECK(rr.value("drag_n", 0.0) >= ref.drag_n);

    // A rotated flow direction still solves (alignment rotation is well-formed).
    nlohmann::json pd = params;
    pd["direction"] = {0, 1, 0};
    pd["aoa_deg"] = 5.0;
    pd["sideslip_deg"] = 3.0;
    pd["job_id"] = "test_flow_stl_dir";
    const nlohmann::json rd = run_flow_stl(pd);
    REQUIRE_MESSAGE(rd.value("ok", false), "rotated-direction run failed: ", rd.dump());
    CHECK(rd.value("drag_n", 0.0) > 0.0);

    // Honest error envelopes.
    CHECK(run_flow_stl({{"stl_path", ascii_path}, {"solver", "openfoam"}})
              .value("code", std::string()) == "unsupported_solver");
    CHECK(run_flow_stl({{"stl_path", ascii_path}, {"preset", "ludicrous"}})
              .value("code", std::string()) == "invalid_params");
    CHECK(run_flow_stl({{"stl_path", (tmp / "no_such_file.stl").string()}})
              .value("code", std::string()) == "invalid_stl");
    CHECK(run_flow_stl(nlohmann::json::object()).value("code", std::string()) ==
          "invalid_params");
    CHECK(run_flow_stl({{"stl_path", ascii_path}, {"speed_mps", 0.0}})
              .value("code", std::string()) == "invalid_params");
    CHECK(run_flow_stl({{"stl_path", ascii_path}, {"direction", {0, 0, 0}}})
              .value("code", std::string()) == "invalid_params");
    CHECK(run_flow_stl({{"stl_path", ascii_path},
                        {"fluid", {{"density_kgm3", -1.0},
                                   {"kinematic_visc_m2s", 1.0e-4}}}})
              .value("code", std::string()) == "invalid_params");
    CHECK(run_flow_stl({{"stl_path", ascii_path}, {"n_steps", 0}})
              .value("code", std::string()) == "invalid_params");

    std::error_code ec;
    fs::remove(ascii_path, ec);
    fs::remove(bin_path, ec);
    fs::remove(prog_path, ec);
}

}  // namespace


// ── Acceleration caches (mesh / factor / faceset / LBM backend) ─────────────────
TEST_CASE("factor cache: load-only tet10 repeat hits and matches") {
    using chudware::sim::fea_static_solve;
    using chudware::sim::fea_factor_cache_clear;
    using chudware::sim::fea_factor_cache_reset_stats;
    using chudware::sim::fea_factor_cache_stats;

    fea_factor_cache_clear();
    fea_factor_cache_reset_stats();

    BoxMesh m4 = make_box_tet4(8, 2, 2, 100.0, 10.0, 10.0);
    BoxMesh m = promote_to_tet10(m4);
    const int n = static_cast<int>(m.points.size() / 3);
    std::vector<int> fixed;
    for (int i = 0; i < n; ++i)
        if (m.points[i * 3] < 1e-9) fixed.push_back(i);
    std::vector<double> f1(static_cast<size_t>(n) * 3, 0.0);
    std::vector<double> f2 = f1;
    for (int i = 0; i < n; ++i)
        if (m.points[i * 3] > 99.0 - 1e-9) {
            f1[i * 3 + 1] = -10.0;
            f2[i * 3 + 1] = -20.0;  // load-only change
        }

    auto r1 = fea_static_solve(m.points, m.tets, 200000.0, 0.3, 250.0, fixed, f1, 10);
    REQUIRE(r1.ok);
    CHECK_FALSE(r1.factor_cache_hit);  // first solve stores

    auto r1b = fea_static_solve(m.points, m.tets, 200000.0, 0.3, 250.0, fixed, f1, 10);
    REQUIRE(r1b.ok);
    CHECK(r1b.factor_cache_hit);
    CHECK(r1b.max_disp_mm == doctest::Approx(r1.max_disp_mm).epsilon(1e-12));
    CHECK(r1b.max_von_mises_mpa == doctest::Approx(r1.max_von_mises_mpa).epsilon(1e-12));
    CHECK(r1b.min_fos == doctest::Approx(r1.min_fos).epsilon(1e-12));

    auto r2 = fea_static_solve(m.points, m.tets, 200000.0, 0.3, 250.0, fixed, f2, 10);
    REQUIRE(r2.ok);
    CHECK(r2.factor_cache_hit);  // load-only: same K
    // Twice the load -> ~twice the tip disp (linear static).
    CHECK(r2.max_disp_mm == doctest::Approx(2.0 * r1.max_disp_mm).epsilon(1e-9));

    // Material change must miss.
    auto r3 = fea_static_solve(m.points, m.tets, 100000.0, 0.3, 250.0, fixed, f1, 10);
    REQUIRE(r3.ok);
    CHECK_FALSE(r3.factor_cache_hit);

    auto st = fea_factor_cache_stats();
    CHECK(st.hits >= 2);
    CHECK(st.misses >= 2);
}

TEST_CASE("LBM CPU backend reports backend=cpu") {
    using chudware::sim::lbm_run;
    using chudware::sim::LbmParams;
    const int nx = 16, ny = 8, nz = 8;
    std::vector<uint8_t> solid(static_cast<size_t>(nx * ny * nz), 0);
    // Small solid block in the middle.
    for (int x = 6; x < 10; ++x)
        for (int y = 3; y < 5; ++y)
            for (int z = 3; z < 5; ++z)
                solid[(x * ny + y) * nz + z] = 1;
    LbmParams p;
    p.backend = "cpu";
    p.u_lat = 0.05;
    p.tau0 = 0.6;
    p.dx_m = 1e-3;
    p.dt_s = 5e-6;
    auto r = lbm_run(solid, nx, ny, nz, p, 20);
    REQUIRE(r.ok);
    CHECK(r.backend == "cpu");
    CHECK(std::isfinite(r.drag_n));
}

TEST_CASE("LBM rejects invalid lattice parameters before allocation") {
    using chudware::sim::lbm_run;
    using chudware::sim::LbmParams;
    std::vector<uint8_t> mask(4 * 4 * 4, 0);
    LbmParams p;
    p.backend = "cpu";
    CHECK_FALSE(lbm_run(mask, 4, 4, 4, p, 0).ok);
    p.u_lat = 0.2;
    CHECK_FALSE(lbm_run(mask, 4, 4, 4, p, 10).ok);
    p.u_lat = 0.05;
    p.tau0 = 0.5;
    CHECK_FALSE(lbm_run(mask, 4, 4, 4, p, 10).ok);
    p.tau0 = 0.6;
    p.rho_air = std::numeric_limits<double>::quiet_NaN();
    CHECK_FALSE(lbm_run(mask, 4, 4, 4, p, 10).ok);
}

TEST_CASE("LBM uniform flow preserves symmetry and balances mass") {
    using chudware::sim::lbm_run;
    using chudware::sim::LbmParams;
    const int nx = 12, ny = 8, nz = 8;
    std::vector<uint8_t> mask(static_cast<size_t>(nx * ny * nz), 0);
    LbmParams p;
    p.backend = "cpu";
    p.u_lat = 0.05;
    p.tau0 = 0.6;
    p.dx_m = 1e-3;
    p.dt_s = 5e-6;
    auto r = lbm_run(mask, nx, ny, nz, p, 100);
    REQUIRE(r.ok);
    CHECK(r.converged);
    CHECK(r.mass_imbalance < 3e-6);  // D3Q19 open-boundary roundoff: <3 ppm
    CHECK(std::abs(r.drag_n) < 1e-12);
    CHECK(std::abs(r.lift_n) < 1e-12);
    CHECK(std::abs(r.side_n) < 1e-12);
    for (float v : r.uy) CHECK(std::abs(v) < 1e-6f);
    for (float v : r.uz) CHECK(std::abs(v) < 1e-6f);
}

TEST_CASE("LBM CUDA asymmetric-body fields forces and convergence match CPU") {
    using chudware::sim::lbm_run;
    using chudware::sim::lbm_cuda_available;
    using chudware::sim::LbmParams;
    const int nx = 16, ny = 8, nz = 8;
    std::vector<uint8_t> solid(static_cast<size_t>(nx * ny * nz), 0);
    for (int x = 6; x < 10; ++x)
        for (int y = 3; y < 5; ++y)
            for (int z = 3; z < 5; ++z)
                solid[(x * ny + y) * nz + z] = 1;
    solid[(8 * ny + 4) * nz + 5] = 1;  // exercise all three force components
    LbmParams p;
    p.backend = "cpu";
    p.u_lat = 0.05;
    p.tau0 = 0.6;
    p.dx_m = 1e-3;
    p.dt_s = 5e-6;
    // Final partial checkpoints, the first drift estimate, the full five-sample
    // window, and an early-stopping budget are separate behavioral boundaries.
    for (int steps : {1, 9, 10, 11, 30, 40, 50, 97, 100}) {
        CAPTURE(steps);
        p.backend = "cpu";
        auto cpu = lbm_run(solid, nx, ny, nz, p, steps);
        REQUIRE(cpu.ok);
        p.backend = "cuda";
        auto gpu = lbm_run(solid, nx, ny, nz, p, steps);
        REQUIRE(gpu.ok);
        if (!lbm_cuda_available()) {
            CHECK(gpu.backend == "cpu");
            CHECK_FALSE(gpu.backend_fallback.empty());
            continue;
        }
        // An available device must really run CUDA; a fallback is not a pass.
        REQUIRE(gpu.backend == "cuda");
        CHECK(gpu.backend_fallback.empty());
        REQUIRE(gpu.rho.size() == cpu.rho.size());
        REQUIRE(gpu.ux.size() == cpu.ux.size());
        REQUIRE(gpu.uy.size() == cpu.uy.size());
        REQUIRE(gpu.uz.size() == cpu.uz.size());
        for (size_t c = 0; c < cpu.rho.size(); ++c) {
            CHECK(std::abs(gpu.rho[c] - cpu.rho[c]) < 1e-5f);
            CHECK(std::abs(gpu.ux[c] - cpu.ux[c]) / p.u_lat < 1e-4);
            CHECK(std::abs(gpu.uy[c] - cpu.uy[c]) / p.u_lat < 1e-4);
            CHECK(std::abs(gpu.uz[c] - cpu.uz[c]) / p.u_lat < 1e-4);
        }
        const double denom = std::max(1e-12, std::abs(cpu.drag_n));
        CHECK(std::abs(gpu.drag_n - cpu.drag_n) / denom < 1e-3);
        CHECK(std::abs(gpu.lift_n - cpu.lift_n) / denom < 1e-3);
        CHECK(std::abs(gpu.side_n - cpu.side_n) / denom < 1e-3);
        // Retain the previous side-relative gate as well as the force-vector gate.
        const double side_denom = std::max(1e-12, std::abs(cpu.side_n));
        CHECK(std::abs(gpu.side_n - cpu.side_n) / side_denom < 1e-2);
        CHECK(gpu.cd == doctest::Approx(cpu.cd).epsilon(1e-3));
        CHECK(gpu.frontal_area_mm2 == cpu.frontal_area_mm2);
        CHECK(gpu.steps_run == cpu.steps_run);
        CHECK(gpu.converged == cpu.converged);
        CHECK(gpu.diverged == cpu.diverged);
        CHECK(std::abs(gpu.residual - cpu.residual) < 1e-3);
        CHECK(std::abs(gpu.mass_imbalance - cpu.mass_imbalance) < 1e-6);
        auto repeat = lbm_run(solid, nx, ny, nz, p, steps);
        REQUIRE(repeat.ok);
        REQUIRE(repeat.backend == "cuda");
        CHECK(repeat.rho == gpu.rho);
        CHECK(repeat.ux == gpu.ux);
        CHECK(repeat.uy == gpu.uy);
        CHECK(repeat.uz == gpu.uz);
        CHECK(repeat.drag_n == gpu.drag_n);
        CHECK(repeat.lift_n == gpu.lift_n);
        CHECK(repeat.side_n == gpu.side_n);
        CHECK(repeat.residual == gpu.residual);
        CHECK(repeat.steps_run == gpu.steps_run);
    }
}

TEST_CASE("LBM CUDA compact gather preserves per-direction recursive reduction") {
    using namespace chudware::sim;
    if (!lbm_cuda_available()) {
        MESSAGE("CUDA reduction parity not exercised: build/device unavailable");
        return;
    }
    const int nx = 24, ny = 18, nz = 18;
    std::vector<uint8_t> mask(nx * ny * nz, 0);
    for (int x = 8; x < 13; ++x)
        for (int y = 2; y < 15; ++y)
            for (int z = 2; z < 15; ++z)
                mask[(x * ny + y) * nz + z] = 1;  // 169 links in each X direction
    mask[(13 * ny + 11) * nz + 15] = 1;
    LbmParams p;
    p.tau0 = 0.6;
    p.dt_s = 5e-6;
    p.backend = "cpu";
    auto cpu = lbm_run(mask, nx, ny, nz, p, 11);
    p.backend = "cuda";
    auto gpu = lbm_run(mask, nx, ny, nz, p, 11);
    REQUIRE(cpu.ok);
    REQUIRE(gpu.ok);
    REQUIRE(gpu.backend == "cuda");
    CHECK(gpu.steps_run == 11);
    CHECK(gpu.residual == 1.0);  // two samples cannot establish force drift
    CHECK_FALSE(gpu.converged);
    const double scale = std::max(1e-12, std::abs(cpu.drag_n));
    CHECK(std::abs(gpu.drag_n - cpu.drag_n) / scale < 1e-3);
    CHECK(std::abs(gpu.lift_n - cpu.lift_n) / scale < 1e-3);
    CHECK(std::abs(gpu.side_n - cpu.side_n) / scale < 1e-3);
    REQUIRE(gpu.rho.size() == cpu.rho.size());
    REQUIRE(gpu.ux.size() == cpu.ux.size());
    REQUIRE(gpu.uy.size() == cpu.uy.size());
    REQUIRE(gpu.uz.size() == cpu.uz.size());
    for (size_t c = 0; c < mask.size(); ++c) {
        CHECK(std::abs(gpu.rho[c] - cpu.rho[c]) < 1e-5f);
        CHECK(std::abs(gpu.ux[c] - cpu.ux[c]) < 5e-6f);
        CHECK(std::abs(gpu.uy[c] - cpu.uy[c]) < 5e-6f);
        CHECK(std::abs(gpu.uz[c] - cpu.uz[c]) < 5e-6f);
    }
}

TEST_CASE("LBM CUDA empty mask preserves zero forces and early-stop cadence") {
    using namespace chudware::sim;
    const int nx = 12, ny = 8, nz = 8;
    std::vector<uint8_t> mask(nx * ny * nz, 0);
    LbmParams p;
    p.backend = "cpu";
    p.tau0 = 0.6;
    p.dt_s = 5e-6;
    auto cpu = lbm_run(mask, nx, ny, nz, p, 100);
    REQUIRE(cpu.ok);
    CHECK(cpu.steps_run == 50);
    p.backend = "cuda";
    std::vector<std::pair<int, int>> progress;
    auto gpu = lbm_run(mask, nx, ny, nz, p, 100,
        [&](int step, int total) { progress.emplace_back(step, total); });
    REQUIRE(gpu.ok);
    if (!lbm_cuda_available()) {
        CHECK(gpu.backend == "cpu");
        CHECK_FALSE(gpu.backend_fallback.empty());
        return;
    }
    REQUIRE(gpu.backend == "cuda");
    CHECK(gpu.steps_run == cpu.steps_run);
    CHECK(gpu.converged);
    CHECK(gpu.residual == 0.0);
    CHECK(gpu.drag_n == 0.0);
    CHECK(gpu.lift_n == 0.0);
    CHECK(gpu.side_n == 0.0);
    CHECK(gpu.cd == 0.0);
    CHECK(gpu.frontal_area_mm2 == 0.0);
    CHECK(gpu.mass_imbalance < 3e-6);
    CHECK(std::abs(gpu.mass_imbalance - cpu.mass_imbalance) < 1e-6);
    REQUIRE_FALSE(progress.empty());
    CHECK(progress.front() == std::make_pair(0, 100));
    CHECK(progress.back() == std::make_pair(100, 100));
    for (size_t c = 0; c < mask.size(); ++c) {
        CHECK(std::abs(gpu.rho[c] - cpu.rho[c]) < 1e-5f);
        CHECK(std::abs(gpu.ux[c] - cpu.ux[c]) < 5e-6f);
        CHECK(std::abs(gpu.uy[c]) < 1e-6f);
        CHECK(std::abs(gpu.uz[c]) < 1e-6f);
    }
}

TEST_CASE("LBM CUDA cancellation is terminal even for a one-shot cancel hook") {
    using namespace chudware::sim;
    const int nx = 12, ny = 8, nz = 8;
    std::vector<uint8_t> mask(nx * ny * nz, 0);
    for (const auto& checkpoint : {std::make_pair(1, 1), std::make_pair(9, 1),
                                   std::make_pair(10, 1), std::make_pair(11, 1),
                                   std::make_pair(100, 1), std::make_pair(100, 4)}) {
        const int steps = checkpoint.first;
        const int cancel_at = checkpoint.second;
        for (const char* backend : {"cpu", "cuda"}) {
            CAPTURE(steps);
            CAPTURE(backend);
            CAPTURE(cancel_at);
            LbmParams p;
            p.backend = backend;
            int polls = 0;
            auto cancelled = lbm_run(mask, nx, ny, nz, p, steps, {},
                                     [&] { return ++polls == cancel_at; });
            CHECK_FALSE(cancelled.ok);
            CHECK(cancelled.error.find("cancelled by sim_cancel") == 0);
            CHECK(cancelled.steps_run == std::min(steps, cancel_at * 10));
            CHECK(polls == cancel_at);
            if (std::string(backend) == "cuda" && lbm_cuda_available())
                CHECK(cancelled.backend == "cuda");
            else
                CHECK(cancelled.backend == "cpu");
        }
    }
}

#ifdef CHUDWARE_WITH_CUDA
TEST_CASE("LBM CUDA completion callback failure cannot return success") {
    using namespace chudware::sim;
    if (!lbm_cuda_available()) {
        MESSAGE("CUDA callback failure not exercised: device unavailable");
        return;
    }
    std::vector<uint8_t> mask(12 * 8 * 8, 0);
    LbmParams p;
    std::string reason;
    bool completed = false;
    const auto result = lbm_run_cuda(mask, 12, 8, 8, p, 100,
        [&](int step, int total) {
            if (step == total) {
                completed = true;
                throw std::runtime_error("completion callback failed");
            }
        }, {}, reason);
    REQUIRE(completed);
    CHECK_FALSE(result.ok);
    CHECK(result.error.find("completion callback failed") != std::string::npos);
    CHECK(result.steps_run < 100);
}

TEST_CASE("LBM CUDA failure observes pending cancellation before CPU fallback") {
    using namespace chudware::sim;
    if (!lbm_cuda_available()) {
        MESSAGE("CUDA fallback cancellation not exercised: device unavailable");
        return;
    }
    std::vector<uint8_t> mask(12 * 8 * 8, 0);
    LbmParams p;
    p.backend = "cuda";
    bool cancel = false;
    const auto result = lbm_run(mask, 12, 8, 8, p, 100,
        [&](int step, int) {
            if (step == 0) {
                cancel = true;
                throw std::runtime_error("startup callback failed");
            }
        }, [&] { return cancel; });
    CHECK_FALSE(result.ok);
    CHECK(result.error.find("cancelled by sim_cancel") == 0);
    CHECK(result.backend == "cuda");
    CHECK(result.steps_run == 0);
}
#endif


// ── sim_cancel: run/cancel registry + cooperative LBM cancel ────────────────────

TEST_CASE("sim_cancel registry: running job cancels, unknown/finished reports cleanly") {
    using chudware::sim::sim_cancel;
    using chudware::sim::sim_job_register;
    using chudware::sim::sim_job_unregister;

    // Unknown job (never registered): ok, but nothing cancelled (mirrors agent::cancel_run).
    auto miss = sim_cancel("sim-test-no-such-job");
    CHECK(miss.value("ok", false));
    CHECK_FALSE(miss.value("cancelled", true));

    // Empty job_id: same idempotent no-op.
    auto empty = sim_cancel("");
    CHECK(empty.value("ok", false));
    CHECK_FALSE(empty.value("cancelled", true));

    // Running job: cancel flips the token the solver polls between steps.
    auto tok = sim_job_register("sim-test-job-a");
    REQUIRE(tok != nullptr);
    CHECK_FALSE(tok->load());
    auto hit = sim_cancel("sim-test-job-a");
    CHECK(hit.value("ok", false));
    CHECK(hit.value("cancelled", false));
    CHECK(hit.value("job_id", std::string()) == "sim-test-job-a");
    CHECK(tok->load());

    // Finished job (unregistered at run end): cancel-after-finish is an idempotent miss.
    sim_job_unregister("sim-test-job-a", tok);
    auto done = sim_cancel("sim-test-job-a");
    CHECK(done.value("ok", false));
    CHECK_FALSE(done.value("cancelled", true));
}

TEST_CASE("sim_cancel isolates actors and preserves owned job generations") {
    using namespace chudware::sim;
    CellPrincipal a{"team:one", "a", false}, b{"team:one", "b", false};
    CellPrincipal manager{"team:one", "manager", true}, foreignManager{"team:two", "manager", true};
    SimCancelToken aToken, bToken;
    {
        CellRequestScope scope(&a);
        aToken = sim_job_register("a-job");
        REQUIRE(aToken != nullptr);
    }
    {
        CellRequestScope scope(&b);
        bToken = sim_job_register("b-job");
        REQUIRE(bToken != nullptr);
        CHECK(sim_cancel("a-job")["code"] == "not_found");
        CHECK(sim_cancel("missing")["code"] == "not_found");
        CHECK_THROWS_AS(sim_job_register("a-job"), CellOwnershipError);
        CHECK_FALSE(aToken->load());
        CHECK(sim_cancel("")["count"] == 1);
        CHECK(bToken->load());
        CHECK_FALSE(aToken->load());
        sim_job_unregister("a-job", aToken);
    }
    {
        CellRequestScope scope(&foreignManager);
        CHECK(sim_cancel("a-job")["code"] == "not_found");
    }
    {
        CellRequestScope scope(&manager);
        CHECK(sim_cancel("")["count"] == 0);
        CHECK(sim_cancel("a-job")["cancelled"] == true);
    }
    {
        CellRequestScope scope(&a);
        auto newer = sim_job_register("a-job");
        REQUIRE(newer != nullptr);
        sim_job_unregister("a-job", aToken);
        CHECK(sim_cancel("a-job")["cancelled"] == true);
        CHECK(newer->load());
        sim_job_unregister("a-job", newer);
    }
    {
        CellRequestScope scope(&b);
        sim_job_unregister("b-job", bToken);
    }
    CHECK(current_cell_principal() == nullptr);
    CHECK(sim_cancel("absent")["cancelled"] == false);
    auto desktop = sim_job_register("desktop");
    REQUIRE(desktop != nullptr);
    CHECK(sim_cancel("")["cancelled"] == false);
    CHECK_FALSE(desktop->load());
    CHECK(sim_cancel("desktop")["cancelled"] == true);
    sim_job_unregister("desktop", desktop);
}

TEST_CASE("sim_cancel retains revocation tombstones without blocking newer epochs") {
    using namespace chudware::sim;
    CellPrincipal current{"team:revocation", "actor", false, 2};
    CellPrincipal stale{"team:revocation", "actor", false, 1};
    SimCancelToken token, anonymous;
    {
        CellRequestScope scope(&current);
        token = sim_job_register("epoch-two");
        anonymous = sim_job_register("");
        REQUIRE(token != nullptr);
        REQUIRE(anonymous != nullptr);
        CHECK(sim_cancel("", 1)["count"] == 0);
        CHECK_FALSE(token->load());
        CHECK_FALSE(anonymous->load());
        sim_cancel("", 0);
    }
    for (const char* id : {"late-named", ""}) {
        CellRequestScope scope(&stale);
        CHECK_THROWS_AS(sim_job_register(id), CellRevokedError);
        CHECK(cell_request_revoked());
    }
    {
        CellRequestScope scope(&current);
        auto fresh = sim_job_register("fresh-epoch-two");
        REQUIRE(fresh != nullptr);
        sim_job_unregister("fresh-epoch-two", fresh);
        CHECK(sim_cancel("", 2)["count"] == 2);
        CHECK(token->load());
        CHECK(anonymous->load());
        sim_job_unregister("epoch-two", token);
        sim_job_unregister("", anonymous);
        CHECK_THROWS_AS(sim_job_register("late-after-full-revoke"), CellRevokedError);
    }
    current.epoch = 3;
    {
        CellRequestScope scope(&current);
        auto next = sim_job_register("epoch-three");
        REQUIRE(next != nullptr);
        CHECK_FALSE(cell_request_revoked());
        sim_job_unregister("epoch-three", next);
    }
}

TEST_CASE("lbm_run stops at a checkpoint when should_cancel fires") {
    using chudware::sim::lbm_run;
    using chudware::sim::LbmParams;
    const int nx = 16, ny = 8, nz = 8;
    std::vector<uint8_t> solid(static_cast<size_t>(nx * ny * nz), 0);
    for (int x = 6; x < 10; ++x)
        for (int y = 3; y < 5; ++y)
            for (int z = 3; z < 5; ++z)
                solid[(x * ny + y) * nz + z] = 1;
    LbmParams p;
    p.backend = "cpu";
    p.u_lat = 0.05;
    p.tau0 = 0.6;
    p.dx_m = 1e-3;
    p.dt_s = 5e-6;

    // Control: no cancel hook — the run completes successfully, stopping once its
    // force window and continuity have settled rather than burning the full budget.
    auto full = lbm_run(solid, nx, ny, nz, p, 100);
    REQUIRE(full.ok);
    CHECK(full.converged);
    CHECK(full.steps_run > 0);
    CHECK(full.steps_run < 100);

    // Cancel on the 4th checkpoint poll: the run stops early with a clear error envelope.
    int polls = 0;
    auto cancelled = lbm_run(solid, nx, ny, nz, p, 100, {},
                             [&polls] { return ++polls > 3; });
    CHECK_FALSE(cancelled.ok);
    CHECK(cancelled.error.find("cancelled") != std::string::npos);
    CHECK(cancelled.steps_run > 0);
    CHECK(cancelled.steps_run < 100);
}

TEST_CASE("run_wind_tunnel is cancellable through FlowParams.job_id (sim_cancel)") {
    using chudware::sim::FlowParams;
    using chudware::sim::LbmResult;
    using chudware::sim::run_wind_tunnel;
    using chudware::sim::sim_cancel;

    // Watertight 50mm cube (same construction as G4): 8 corners, 12 triangles.
    std::vector<float> pos = {
        -25,-25,-25,  25,-25,-25,  25,25,-25,  -25,25,-25,
        -25,-25, 25,  25,-25, 25,  25,25, 25,  -25,25, 25,
    };
    std::vector<uint32_t> idx = {
        0,2,1, 0,3,2,  // -Z face
        4,5,6, 4,6,7,  // +Z face
        0,1,5, 0,5,4,  // -Y face
        2,3,7, 2,7,6,  // +Y face
        0,4,7, 0,7,3,  // -X face
        1,2,6, 1,6,5,  // +X face
    };

    FlowParams fp;
    fp.cells = 16;
    fp.n_steps = 100000;  // long enough that the cancel below lands mid-run
    fp.job_id = "sim-test-wt-cancel";

    LbmResult r;
    std::thread worker([&] { r = run_wind_tunnel({pos}, {idx}, fp); });

    // Poll until the job shows up in the registry and accepts the cancel (thread-start
    // timing is not deterministic; registration happens at the top of the run).
    bool accepted = false;
    for (int i = 0; i < 200 && !accepted; ++i) {
        auto c = sim_cancel("sim-test-wt-cancel");
        accepted = c.value("ok", false) && c.value("cancelled", false);
        if (!accepted) std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }
    CHECK(accepted);
    worker.join();

    // The run stopped early at an LBM checkpoint and reported the cancel.
    CHECK_FALSE(r.ok);
    CHECK(r.error.find("cancelled") != std::string::npos);
    CHECK(r.steps_run < 100000);

    // The run's guard unregistered on exit: a second cancel is an idempotent miss.
    auto after = sim_cancel("sim-test-wt-cancel");
    CHECK(after.value("ok", false));
    CHECK_FALSE(after.value("cancelled", true));
}

// ── The summary reduction is disclosed, and the .vtu can reproduce it ───────────
// A released packet reported max_von_mises_mpa = 109.7 MPa while the .vtu written by
// the SAME solve peaked at 403 MPa: both are the node-averaged field, but the summary
// silently drops the two element rings around ideal fixtures (a clamp singularity).
// The reported number is only evidence if the file says which nodes produced it, so
// summary_node ships with von_mises and the two reductions are named.
TEST_CASE("static summary discloses its reduction and the mask reproduces it") {
    const double Lx = 100.0, Ly = 10.0, Lz = 10.0;
    BoxMesh m = make_box_tet4(10, 2, 2, Lx, Ly, Lz);
    const int n_nodes = static_cast<int>(m.points.size() / 3);
    std::vector<int> fixed;
    std::vector<double> forces(static_cast<size_t>(n_nodes) * 3, 0.0);
    int loaded = 0;
    for (int i = 0; i < n_nodes; ++i) {
        if (m.points[static_cast<size_t>(i) * 3] < 1e-9) fixed.push_back(i);
        if (std::abs(m.points[static_cast<size_t>(i) * 3] - Lx) < 1e-9) ++loaded;
    }
    for (int i = 0; i < n_nodes; ++i)
        if (std::abs(m.points[static_cast<size_t>(i) * 3] - Lx) < 1e-9)
            forces[static_cast<size_t>(i) * 3 + 2] = -1000.0 / loaded;
    auto r = fea_static_solve(m.points, m.tets, 200000.0, 0.3, 250.0, fixed, forces, 4);
    REQUIRE(r.ok);

    // The mask is complete, and it really excluded something (this cantilever's
    // nodal peak IS at the clamp — that is the whole point of the exclusion).
    REQUIRE(r.summary_node.size() == static_cast<size_t>(n_nodes));
    CHECK(r.summary_excluded_nodes > 0);
    CHECK(r.summary_excluded_nodes < n_nodes);
    CHECK(r.summary_excluded_nodes ==
          static_cast<int>(std::count(r.summary_node.begin(), r.summary_node.end(),
                                      std::uint8_t{0})));

    // Both reductions are reproducible from the field + the mask, exactly.
    double masked = 0.0, unmasked = 0.0;
    for (int i = 0; i < n_nodes; ++i) {
        const double v = r.von_mises[static_cast<size_t>(i)];
        unmasked = std::max(unmasked, v);
        if (r.summary_node[static_cast<size_t>(i)]) masked = std::max(masked, v);
    }
    CHECK(r.max_von_mises_mpa == doctest::Approx(masked).epsilon(1e-12));
    CHECK(r.max_von_mises_raw_mpa == doctest::Approx(unmasked).epsilon(1e-12));
    CHECK(r.max_von_mises_raw_mpa > r.max_von_mises_mpa);
    CHECK(r.min_fos_raw < r.min_fos);
    CHECK(r.min_fos_raw == doctest::Approx(250.0 / unmasked).epsilon(1e-12));
}

// The documented fallback: when the fixture rings would consume the whole mesh the
// summary reverts to the complete field rather than reporting zero stress — and then
// there is nothing to disclose, so the raw and reported numbers must coincide.
TEST_CASE("summary falls back to the whole field when the fixture ring eats the mesh") {
    std::vector<double> pts = {0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1};
    std::vector<int> tets = {0, 1, 2, 3};
    std::vector<int> fixed = {0, 1, 2};
    std::vector<double> forces(12, 0.0);
    forces[11] = -100.0;
    auto r = fea_static_solve(pts, tets, 200000.0, 0.3, 250.0, fixed, forces, 4);
    REQUIRE(r.ok);
    CHECK(r.summary_excluded_nodes == 0);
    CHECK(std::count(r.summary_node.begin(), r.summary_node.end(), std::uint8_t{1}) == 4);
    CHECK(r.max_von_mises_raw_mpa == doctest::Approx(r.max_von_mises_mpa).epsilon(1e-12));
    CHECK(r.min_fos_raw == doctest::Approx(r.min_fos).epsilon(1e-12));
}

// ── tet10 nodal stress recovery reaches the extreme fibre ───────────────────────
// A quadratic element's stress varies over the element, so recovery must extrapolate
// its 4 Gauss points to the nodes. Collapsing them to one element-constant tensor
// first (what this solver used to do) clips every gradient: the recovery then reports,
// at a surface node, the stress from roughly the element's centroid depth. Measured on
// this cantilever, that read 247.7 MPa where the fibre carries 300 MPa — a 17%
// under-read — and at a stress concentration it made tet10 read LOWER than tet4: the
// V&V plate-with-a-hole recovered SCF 2.44 where CalculiX C3D10, handed the identical
// mesh, recovered 3.44 (docs/vv/FEA_VV_REPORT.md, PLATE-HOLE).
//
// The contract: at a Saint-Venant station of a tip-loaded cantilever the nodal sigma_xx
// on the extreme fibre is M(x)c/I. Beam theory is exact enough here (L/h = 10, station
// at mid-span) that the gate can be tight, and it is two-sided so an extrapolation that
// overshoots — a wrong Gauss-point-to-corner permutation scrambles the linear part and
// lands well outside — fails just as loudly as one that under-reads.
TEST_CASE("tet10 nodal stress recovery reports the extreme-fibre bending stress") {
    const double Lx = 100.0, Ly = 10.0, Lz = 10.0;
    const double E = 200000.0, nu = 0.30, Fz = -1000.0, station = 50.0;
    BoxMesh m = promote_to_tet10(make_box_tet4(20, 4, 4, Lx, Ly, Lz));
    const int nn = static_cast<int>(m.points.size() / 3);
    std::vector<int> fixed, tip;
    for (int nid = 0; nid < nn; ++nid) {
        const double x = m.points[nid * 3 + 0];
        if (std::abs(x) < 1e-9) fixed.push_back(nid);
        if (std::abs(x - Lx) < 1e-9) tip.push_back(nid);
    }
    std::vector<double> forces(static_cast<size_t>(nn) * 3, 0.0);
    for (int nid : tip) forces[nid * 3 + 2] += Fz / static_cast<double>(tip.size());
    FeaStaticResult r = fea_static_solve(m.points, m.tets, E, nu, 250.0, fixed, forces, 10);
    REQUIRE(r.ok);
    REQUIRE(r.stress_xx.size() == static_cast<size_t>(nn));

    // Tip load along -Z puts the +Z fibre in tension: sigma_xx = +M c / I.
    const double I = Ly * Lz * Lz * Lz / 12.0;
    const double analytic = std::abs(Fz) * (Lx - station) * (Lz / 2.0) / I;
    double sum = 0.0;
    int count = 0;
    for (int nid = 0; nid < nn; ++nid) {
        if (std::abs(m.points[nid * 3 + 0] - station) > 1e-9) continue;
        if (std::abs(m.points[nid * 3 + 2] - Lz) > 1e-9) continue;
        sum += r.stress_xx[static_cast<size_t>(nid)];
        ++count;
    }
    REQUIRE(count >= 5);
    const double fibre = sum / static_cast<double>(count);
    MESSAGE("tet10 extreme-fibre sigma_xx=", fibre, " analytic=", analytic,
            " ratio=", fibre / analytic, " over ", count, " nodes");
    CHECK(fibre > 0.97 * analytic);
    CHECK(fibre < 1.03 * analytic);
}

// ── sim_run_pipe_flow: internal-flow LBM validation ─────────────────────────────────
// Closed, outward-oriented stepped circular duct along +X: sections {x0, x1, R} share the
// axis at (y0, z0); an annular step closes each radius change, fans cap both ends.
namespace {
struct DuctSection {
    double x0, x1, r;
};
chudware::sim::PipeFlowShell stepped_duct(const std::vector<DuctSection>& secs, double y0,
                                          double z0, int nseg) {
    chudware::sim::PipeFlowShell s;
    auto add = [&](double x, double y, double z) {
        s.pos.push_back((float)x);
        s.pos.push_back((float)y);
        s.pos.push_back((float)z);
        return (uint32_t)(s.pos.size() / 3 - 1);
    };
    auto ring = [&](double x, double r) {
        std::vector<uint32_t> out;
        for (int i = 0; i < nseg; ++i) {
            const double t = 2.0 * 3.14159265358979323846 * i / nseg;
            out.push_back(add(x, y0 + r * std::cos(t), z0 + r * std::sin(t)));
        }
        return out;
    };
    // Emit a triangle oriented so its normal has the sign of `want_x` along X (caps/steps).
    auto tri_x = [&](uint32_t a, uint32_t b, uint32_t c, double want_x) {
        const float* P = s.pos.data();
        const double ay = P[b * 3 + 1] - P[a * 3 + 1], az = P[b * 3 + 2] - P[a * 3 + 2];
        const double by = P[c * 3 + 1] - P[a * 3 + 1], bz = P[c * 3 + 2] - P[a * 3 + 2];
        const double nx = ay * bz - az * by;
        if (nx * want_x >= 0) s.idx.insert(s.idx.end(), {a, b, c});
        else s.idx.insert(s.idx.end(), {a, c, b});
    };
    const uint32_t c0 = add(secs.front().x0, y0, z0);
    const uint32_t c1 = add(secs.back().x1, y0, z0);
    std::vector<uint32_t> prev_end;
    for (size_t k = 0; k < secs.size(); ++k) {
        const auto a = ring(secs[k].x0, secs[k].r);
        const auto b = ring(secs[k].x1, secs[k].r);
        for (int i = 0; i < nseg; ++i) {
            const int j = (i + 1) % nseg;
            s.idx.insert(s.idx.end(), {a[i], a[j], b[j]});  // radial-outward lateral quads
            s.idx.insert(s.idx.end(), {a[i], b[j], b[i]});
            if (k == 0) tri_x(c0, a[i], a[j], -1.0);
            if (k + 1 == secs.size()) tri_x(c1, b[i], b[j], +1.0);
            if (k > 0) {  // annular step from the previous section's end ring
                const double want = secs[k - 1].r > secs[k].r ? +1.0 : -1.0;
                tri_x(prev_end[i], prev_end[j], a[j], want);
                tri_x(prev_end[i], a[j], a[i], want);
            }
        }
        prev_end = b;
    }
    return s;
}

nlohmann::json pipe_params(double x_in, double x_out, double y0, double z0) {
    return {{"inlet", {{"point", {x_in, y0, z0}}, {"normal", {1, 0, 0}}}},
            {"outlet", {{"point", {x_out, y0, z0}}, {"normal", {-1, 0, 0}}}},
            {"fluid", "water"},
            {"density_kgm3", 998.21},
            {"viscosity_pas", 1.0016e-3}};
}

// Closed circular tube (radius r) along a centreline: a straight leg along +X of length l_in,
// a 90-degree arc of centreline radius rb turning toward +Y, a straight leg along +Y of length
// l_out; rings of nseg vertices perpendicular to the path, fans at both ends, outward facets.
chudware::sim::PipeFlowShell bend_tube(double l_in, double rb, double l_out, double r, int nseg,
                                       int narc) {
    struct P {
        double x, y, tx, ty;
    };
    std::vector<P> path{{0.0, 0.0, 1.0, 0.0}};
    for (int k = 0; k <= narc; ++k) {
        const double th = 0.5 * 3.14159265358979323846 * k / narc;
        path.push_back({l_in + rb * std::sin(th), rb - rb * std::cos(th), std::cos(th),
                        std::sin(th)});
    }
    path.push_back({l_in + rb, rb + l_out, 0.0, 1.0});
    chudware::sim::PipeFlowShell s;
    auto add = [&](double x, double y, double z) {
        s.pos.push_back((float)x);
        s.pos.push_back((float)y);
        s.pos.push_back((float)z);
        return (uint32_t)(s.pos.size() / 3 - 1);
    };
    std::vector<std::vector<uint32_t>> rings;
    for (const auto& p : path) {
        std::vector<uint32_t> ring;  // frame (t, z x t, z) keeps the lateral quads outward
        for (int i = 0; i < nseg; ++i) {
            const double ph = 2.0 * 3.14159265358979323846 * i / nseg;
            ring.push_back(add(p.x - r * std::cos(ph) * p.ty, p.y + r * std::cos(ph) * p.tx,
                               r * std::sin(ph)));
        }
        rings.push_back(ring);
    }
    for (size_t k = 0; k + 1 < rings.size(); ++k)
        for (int i = 0; i < nseg; ++i) {
            const int j = (i + 1) % nseg;
            s.idx.insert(s.idx.end(), {rings[k][i], rings[k][j], rings[k + 1][j]});
            s.idx.insert(s.idx.end(), {rings[k][i], rings[k + 1][j], rings[k + 1][i]});
        }
    const uint32_t c0 = add(0.0, 0.0, 0.0), c1 = add(path.back().x, path.back().y, 0.0);
    auto cap = [&](uint32_t c, uint32_t a, uint32_t b, double nx, double ny) {
        const float* q = s.pos.data();
        const double ux = q[a * 3] - q[c * 3], uy = q[a * 3 + 1] - q[c * 3 + 1],
                     uz = q[a * 3 + 2] - q[c * 3 + 2];
        const double vx = q[b * 3] - q[c * 3], vy = q[b * 3 + 1] - q[c * 3 + 1],
                     vz = q[b * 3 + 2] - q[c * 3 + 2];
        const double cx = uy * vz - uz * vy, cy = uz * vx - ux * vz;
        if (cx * nx + cy * ny >= 0) s.idx.insert(s.idx.end(), {c, a, b});
        else s.idx.insert(s.idx.end(), {c, b, a});
    };
    for (int i = 0; i < nseg; ++i) {
        const int j = (i + 1) % nseg;
        cap(c0, rings.front()[i], rings.front()[j], -1.0, 0.0);
        cap(c1, rings.back()[i], rings.back()[j], 0.0, 1.0);
    }
    return s;
}

// Turbulent straight-pipe run: 10 mm smooth bore, 8 diameters, 10 cells across D.
nlohmann::json turbulent_pipe(double re) {
    const double L = 80.0, y0 = 0.37, z0 = -0.21;
    const auto shell = stepped_duct({{0.0, L, 5.0}}, y0, z0, 48);
    auto p = pipe_params(0.0, L, y0, z0);
    p["mean_velocity_ms"] = re * 1.0016e-3 / (998.21 * 0.010);
    p["cells_across"] = 10;
    return chudware::sim::run_pipe_flow({shell}, p);
}

void check_turbulent_straight(const nlohmann::json& r, double re, double f_moody) {
    REQUIRE_MESSAGE(r.value("ok", false), r.dump());
    CHECK(r["model"] == "mixing_length+wall_function");
    CHECK(r["regime"] == "turbulent");
    CHECK(r["reynolds"].get<double>() == doctest::Approx(re).epsilon(0.01).scale(0));
    CHECK(r["converged"].get<bool>());
    CHECK(r["mass_imbalance"].get<double>() < 0.01);
    CHECK(r["provenance"]["inlet_profile"].get<std::string>().rfind("recycled", 0) == 0);
    const auto& w = r["warnings"];
    CHECK(std::find(w.begin(), w.end(), "wall_function_out_of_range") == w.end());
    CHECK_FALSE(r["bends_detected"].get<bool>());  // a straight bore is not a bend
    // Smooth-pipe Colebrook (Clamond) friction vs the Moody chart value.
    const double f = r["darcy_friction_factor"].get<double>();
    CHECK(f == doctest::Approx(f_moody).epsilon(0.03).scale(0));
    const double dp = r["dp_pa"].get<double>(), darcy = r["darcy_estimate_pa"].get<double>();
    MESSAGE("turbulent straight pipe Re=", re, ": dp=", dp, " Pa, Darcy-Colebrook=", darcy,
            " Pa, ratio=", dp / darcy, ", y+=", r["provenance"]["y_plus_first_cell"]["mean"].get<double>(),
            ", steps=", r["steps"].get<int>());
    CHECK(dp == doctest::Approx(darcy).epsilon(0.20).scale(0));
}
}  // namespace

TEST_CASE("pipe_flow: laminar straight duct matches Hagen-Poiseuille") {
    using chudware::sim::run_pipe_flow;
    const double D = 10.0, L = 60.0, y0 = 0.37, z0 = -0.21;  // axis off the voxel lattice
    const auto shell = stepped_duct({{0.0, L, D / 2}}, y0, z0, 64);
    const double rho = 998.21, mu = 1.0016e-3, re_target = 30.0;
    const double U = re_target * mu / (rho * D * 1e-3);
    auto p = pipe_params(0.0, L, y0, z0);
    p["mean_velocity_ms"] = U;
    p["cells_across"] = 12;
    const auto r = run_pipe_flow({shell}, p);
    REQUIRE_MESSAGE(r.value("ok", false), r.dump());
    CHECK(r["converged"].get<bool>());
    CHECK(r["regime"] == "laminar");
    CHECK(r["reynolds"].get<double>() == doctest::Approx(re_target).epsilon(0.01).scale(0));
    CHECK(r["mass_imbalance"].get<double>() < 0.02);
    CHECK(r["path_length_mm"].get<double>() == doctest::Approx(L).epsilon(0.01).scale(0));
    CHECK(r["provenance"]["engine"] == "cpp-lbm-internal");
    CHECK(r["provenance"]["inlet_profile"] == "developed_laminar");

    // Hagen-Poiseuille on the faceted bore the mesh actually carries (its exact section).
    const double A = r["inlet_area_mm2"].get<double>() * 1e-6;
    const double Dh = r["hydraulic_diameter_mm"].get<double>() * 1e-3;
    CHECK(Dh == doctest::Approx(D * 1e-3).epsilon(0.01).scale(0));
    const double Q = U * A;
    const double hp = 128.0 * mu * (L * 1e-3) * Q / (3.14159265358979323846 * std::pow(Dh, 4));
    const double dp = r["dp_pa"].get<double>();
    MESSAGE("pipe_flow dp=", dp, " Pa, Hagen-Poiseuille=", hp, " Pa, ratio=", dp / hp,
            ", steps=", r["steps"].get<int>(), ", voxels=", r["voxels"].get<int>());
    CHECK(dp == doctest::Approx(hp).epsilon(0.10).scale(0));
    // The laminar Darcy comparison is Hagen-Poiseuille itself (f = 64/Re).
    CHECK(r["darcy_estimate_pa"].get<double>() == doctest::Approx(hp).epsilon(0.03).scale(0));

    // Developed laminar flow: the pressure profile falls linearly from inlet to outlet.
    const auto& prof = r["pressure_profile"];
    REQUIRE(prof.size() > 10);
    CHECK(prof.front()["p_pa"].get<double>() == doctest::Approx(dp).epsilon(1e-9).scale(0));
    CHECK(prof.back()["p_pa"].get<double>() == 0.0);
    const auto& mid = prof[prof.size() / 2];
    const double expect_mid = dp * (1.0 - mid["s_mm"].get<double>() / L);
    CHECK(mid["p_pa"].get<double>() == doctest::Approx(expect_mid).epsilon(0.05).scale(0));

    // The flow_m3s form gives the same answer; flipped normals are re-oriented.
    auto pq = pipe_params(0.0, L, y0, z0);
    pq["flow_m3s"] = Q;
    pq["cells_across"] = 12;
    pq["inlet"]["normal"] = {-1, 0, 0};
    pq["outlet"]["normal"] = {1, 0, 0};
    const auto rq = run_pipe_flow({shell}, pq);
    REQUIRE_MESSAGE(rq.value("ok", false), rq.dump());
    CHECK(rq["dp_pa"].get<double>() == doctest::Approx(dp).epsilon(0.02).scale(0));
}

TEST_CASE("pipe_flow: a contraction costs more pressure than the straight pipe") {
    using chudware::sim::run_pipe_flow;
    const double L = 60.0, y0 = 0.29, z0 = 0.13;
    const auto straight = stepped_duct({{0.0, L, 5.0}}, y0, z0, 64);
    const auto contraction = stepped_duct({{0.0, 30.0, 5.0}, {30.0, L, 3.0}}, y0, z0, 64);
    const double Q = 2.0e-7;  // m^3/s: Re ~25 in the 10 mm bore, ~42 in the 6 mm throat
    auto run = [&](const chudware::sim::PipeFlowShell& s) {
        auto p = pipe_params(0.0, L, y0, z0);
        p["flow_m3s"] = Q;
        p["voxel_mm"] = 0.6;
        return run_pipe_flow({s}, p);
    };
    const auto rs = run(straight);
    const auto rc = run(contraction);
    REQUIRE_MESSAGE(rs.value("ok", false), rs.dump());
    REQUIRE_MESSAGE(rc.value("ok", false), rc.dump());
    CHECK(rc["mass_imbalance"].get<double>() < 0.02);
    const double dps = rs["dp_pa"].get<double>(), dpc = rc["dp_pa"].get<double>();
    MESSAGE("straight dp=", dps, " Pa, contraction dp=", dpc, " Pa");
    // Half the length at (10/6)^4 = 7.7x the Poiseuille gradient plus the contraction loss.
    CHECK(dpc > 2.0 * dps);
    // The 1-D estimate of the same two sections (laminar friction only) is a lower bound.
    const double mu = 1.0016e-3, pi = 3.14159265358979323846;
    const double hp_lo = 128.0 * mu * 0.030 * Q / (pi * std::pow(0.010, 4)) +
                         128.0 * mu * 0.030 * Q / (pi * std::pow(0.006, 4));
    CHECK(dpc > 0.9 * hp_lo);
    CHECK(dpc < 1.6 * hp_lo);
}

TEST_CASE("pipe_flow: refusals name the problem") {
    using chudware::sim::run_pipe_flow;
    const double L = 30.0;
    const auto shell = stepped_duct({{0.0, L, 4.0}}, 0.1, 0.2, 32);
    auto base = pipe_params(0.0, L, 0.1, 0.2);
    base["mean_velocity_ms"] = 0.002;

    SUBCASE("inlet plane misses the fluid volume") {
        auto p = base;
        p["inlet"]["point"] = {500.0, 500.0, 500.0};
        const auto r = run_pipe_flow({shell}, p);
        CHECK_FALSE(r.value("ok", true));
        CHECK(r["reason"] == "no_inlet_cells");
        CHECK(r.contains("hint"));
    }
    SUBCASE("outlet plane misses the fluid volume") {
        auto p = base;
        p["outlet"]["point"] = {-400.0, 0.0, 0.0};
        const auto r = run_pipe_flow({shell}, p);
        CHECK(r["reason"] == "no_outlet_cells");
    }
    SUBCASE("open (non-watertight) shell") {
        auto open = shell;
        open.idx.resize(open.idx.size() - 3);  // drop one cap facet
        const auto r = run_pipe_flow({open}, base);
        CHECK_FALSE(r.value("ok", true));
        CHECK(r["reason"] == "mesh_not_watertight");
    }
    SUBCASE("velocity and flow rate are exclusive") {
        auto p = base;
        p["flow_m3s"] = 1e-7;
        CHECK(run_pipe_flow({shell}, p)["reason"] == "invalid_params");
    }
    SUBCASE("missing fluid properties") {
        auto p = base;
        p.erase("viscosity_pas");
        CHECK(run_pipe_flow({shell}, p)["reason"] == "invalid_params");
    }
}

TEST_CASE("pipe_flow: turbulent straight pipe at Re 1e4 tracks Darcy-Colebrook") {
    check_turbulent_straight(turbulent_pipe(1.0e4), 1.0e4, 0.0309);
}

TEST_CASE("pipe_flow: turbulent straight pipe at Re 1e5 tracks Darcy-Colebrook") {
    check_turbulent_straight(turbulent_pipe(1.0e5), 1.0e5, 0.0180);
}

TEST_CASE("pipe_flow: turbulent 90-degree long-radius bend loss vs Rennels") {
    // 10 mm bore, centreline radius 1.5 D, 2 D straight inlet leg (recycled developed inflow),
    // 4 D outlet leg, Re 5e4, 14 cells across D.
    const double D = 10.0, rb = 1.5 * D, l_in = 2.0 * D, l_out = 4.0 * D;
    const auto shell = bend_tube(l_in, rb, l_out, 0.5 * D, 48, 24);
    const double re = 5.0e4, rho = 998.21, mu = 1.0016e-3;
    const double U = re * mu / (rho * D * 1e-3);
    nlohmann::json p = {{"inlet", {{"point", {0.0, 0.0, 0.0}}, {"normal", {1, 0, 0}}}},
                        {"outlet", {{"point", {l_in + rb, rb + l_out, 0.0}}, {"normal", {0, -1, 0}}}},
                        {"fluid", "water"},
                        {"density_kgm3", rho},
                        {"viscosity_pas", mu},
                        {"mean_velocity_ms", U},
                        {"cells_across", 14}};
    const auto r = chudware::sim::run_pipe_flow({shell}, p);
    REQUIRE_MESSAGE(r.value("ok", false), r.dump());
    CHECK(r["regime"] == "turbulent");
    CHECK(r["bends_detected"].get<bool>());  // from the geometry alone (no piping given)
    CHECK(r["bend_loss_band"]["k_ratio_min"].get<double>() == 1.65);  // 14 cells across
    CHECK(r["mass_imbalance"].get<double>() < 0.01);
    const double q = 0.5 * rho * U * U;
    const double f = chudware::sim::darcy_friction_factor(re, 0.0);
    // Bend K: total loss minus Darcy friction of the two straight legs (the arc's own
    // friction stays in K, as in Rennels' definition).
    const double k_bend = r["dp_pa"].get<double>() / q - f * (l_in + l_out) / D;
    // Rennels (Pipe Flow, 2012) 90-degree smooth bend, r/D = 1.5:
    // K = f (pi/2) r/D + (0.10 + 2.4 f) sin(45) + 6.6 f (sqrt(sin 45) + sin 45) / (r/D)^2
    //   = 0.250 at Re 5e4 (f = 0.0209).
    const double s45 = std::sin(0.25 * 3.14159265358979323846);
    const double k_rennels = f * 0.5 * 3.14159265358979323846 * 1.5 + (0.10 + 2.4 * f) * s45 +
                             6.6 * f * (std::sqrt(s45) + s45) / (1.5 * 1.5);
    CHECK(k_rennels == doctest::Approx(0.250).epsilon(0.02).scale(0));
    MESSAGE("bend K=", k_bend, " vs Rennels K=", k_rennels, " (ratio ", k_bend / k_rennels,
            "), dp=", r["dp_pa"].get<double>(), " Pa, steps=", r["steps"].get<int>());
    CHECK(k_bend > 0.5 * k_rennels);
    CHECK(k_bend < 2.0 * k_rennels);
}

TEST_CASE("pipe_flow: piping + run_id adds the fittings-aware 1-D estimate") {
    // One aicad.piping/1 run: 10 mm bore, 2 D inlet leg, 90-degree LR elbow (bend radius
    // 1.5 D), 3 D outlet leg, Re 5e4. No cells_across: the run's elbow selects the 16-cell
    // bend default, and both end legs are extended to 5 D straight (+30 / +20 mm) with dp read
    // between the run's own end planes.
    const double D = 10.0, rb = 1.5 * D, l_in = 2.0 * D, l_out = 3.0 * D;
    const auto shell = bend_tube(l_in, rb, l_out, 0.5 * D, 48, 24);
    const double re = 5.0e4, rho = 998.21, mu = 1.0016e-3, pi = 3.14159265358979323846;
    const double U = re * mu / (rho * D * 1e-3);
    const double xc = l_in + rb;  // the polyline corner the arc rounds
    nlohmann::json piping = {
        {"schema", "aicad.piping/1"},
        {"id", "net"},
        {"runs",
         {{{"id", "r1"},
           {"path", {{0.0, 0.0, 0.0}, {xc, 0.0, 0.0}, {xc, rb + l_out, 0.0}}},
           {"bore_mm", D},
           {"bend_radius", rb}}}},
        {"fittings",
         {{{"id", "e1"},
           {"type", "elbow"},
           {"origin", {xc, 0.0, 0.0}},
           {"params", {{"angle", 90}, {"radius", "LR"}}}}}}};
    nlohmann::json p = {{"inlet", {{"point", {0.0, 0.0, 0.0}}, {"normal", {1, 0, 0}}}},
                        {"outlet", {{"point", {xc, rb + l_out, 0.0}}, {"normal", {0, -1, 0}}}},
                        {"fluid", "water"},
                        {"density_kgm3", rho},
                        {"viscosity_pas", mu},
                        {"mean_velocity_ms", U},
                        {"piping", piping},
                        {"run_id", "r1"}};
    const auto r = chudware::sim::run_pipe_flow({shell}, p, &chudware::piping::run_loss_1d);
    REQUIRE_MESSAGE(r.value("ok", false), r.dump());
    CHECK(r["regime"] == "turbulent");
    CHECK(r["provenance"]["cells_across_default"] == "bend");
    CHECK(r["bend_loss_band"]["k_ratio_min"].get<double>() == 1.47);  // 16 cells across
    // Straight legs: inlet 2 D -> 5 D, outlet 3 D (polyline leg minus the arc take-out) -> 5 D.
    REQUIRE(r.contains("extended_legs"));
    CHECK(r["extended_legs"]["inlet_mm"].get<double>() == doctest::Approx(3.0 * D).epsilon(0.01));
    CHECK(r["extended_legs"]["outlet_mm"].get<double>() == doctest::Approx(2.0 * D).epsilon(0.01));
    CHECK(r["path_length_mm"].get<double>() <
          r["extended_legs"]["domain_path_length_mm"].get<double>() - 4.0 * D);
    CHECK(r["dp_domain_pa"].get<double>() > r["dp_pa"].get<double>());

    // Hand value at the flow the lattice carried, on the run's nominal 10 mm bore (piping_solve
    // convention: friction over the corner-to-corner centreline L = l_in + 2 rb + l_out = 80 mm,
    // plus the elbow's Rennels K):
    //   v = Q / (pi D^2 / 4), Re = rho v D / mu, f = Clamond(Re, smooth) ~= 0.0209,
    //   K = f (pi/2)(1.5) + (0.10 + 2.4 f) sin 45 + 6.6 f (sqrt(sin 45) + sin 45) / 1.5^2
    //     ~= 0.250, total = (f L / D + K) q ~= (0.167 + 0.250) q.
    const double Q = r["flow_m3s"].get<double>();
    const double v = Q / (0.25 * pi * D * D * 1e-6);
    const double f = chudware::sim::darcy_friction_factor(rho * v * D * 1e-3 / mu, 0.0);
    const double q = 0.5 * rho * v * v;
    const double s45 = std::sin(0.25 * pi);
    const double k_rennels = f * 0.5 * pi * 1.5 + (0.10 + 2.4 * f) * s45 +
                             6.6 * f * (std::sqrt(s45) + s45) / (1.5 * 1.5);
    const double L = l_in + 2.0 * rb + l_out;
    const double hand = (f * L / D + k_rennels) * q;
    REQUIRE(r.contains("one_d_estimate_pa"));
    const double est = r["one_d_estimate_pa"].get<double>();
    CHECK(est == doctest::Approx(hand).epsilon(0.01).scale(0));
    const auto& bd = r["one_d_breakdown"];
    CHECK(bd["friction_pa"].get<double>() ==
          doctest::Approx(f * L / D * q).epsilon(0.01).scale(0));
    REQUIRE(bd["fittings"].size() == 1);
    CHECK(bd["fittings"][0]["id"] == "e1");
    CHECK(bd["fittings"][0]["K"].get<double>() == doctest::Approx(k_rennels).epsilon(0.01).scale(0));
    const double ratio = r["cfd_over_one_d"].get<double>();
    MESSAGE("1-D estimate ", est, " Pa (hand ", hand, "), CFD dp ", r["dp_pa"].get<double>(),
            " Pa, CFD/1-D ", ratio, ", steps ", r["steps"].get<int>());
    CHECK(ratio == doctest::Approx(r["dp_pa"].get<double>() / est).epsilon(1e-9).scale(0));
    // Within the measured bend over-prediction band at this resolution: friction as 1-D, the
    // bend's K over-predicted by k_ratio_min..k_ratio_max (10% slack for the model's straight
    // friction, ~0.93x Colebrook, and the window-averaged dp).
    const double fr = bd["friction_pa"].get<double>(), kp = bd["fittings_pa"].get<double>();
    const double lo = (fr + kp * r["bend_loss_band"]["k_ratio_min"].get<double>()) / est;
    const double hi = (fr + kp * r["bend_loss_band"]["k_ratio_max"].get<double>()) / est;
    MESSAGE("expected CFD/1-D band ", lo, "..", hi);
    CHECK(ratio >= 0.9 * lo);
    CHECK(ratio <= 1.1 * hi);
    CHECK(r["hint"].get<std::string>().find("1-D estimate for run r1") != std::string::npos);

    // An unknown run refuses before any lattice work.
    auto bad = p;
    bad["run_id"] = "r9";
    CHECK(chudware::sim::run_pipe_flow({shell}, bad, &chudware::piping::run_loss_1d)["reason"] ==
          "unknown_run");
}

TEST_CASE("pipe_flow: sim_cancel stops a running internal-flow job") {
    using chudware::sim::run_pipe_flow;
    using chudware::sim::sim_cancel;
    const auto shell = stepped_duct({{0.0, 60.0, 5.0}}, 0.37, -0.21, 48);
    auto p = pipe_params(0.0, 60.0, 0.37, -0.21);
    p["mean_velocity_ms"] = 0.003;
    p["cells_across"] = 10;
    p["max_steps"] = 2000000;
    p["tolerance"] = 1e-12;  // never converges on its own
    p["job_id"] = "sim-test-pipe-cancel";
    nlohmann::json r;
    std::thread worker([&] { r = run_pipe_flow({shell}, p); });
    bool accepted = false;
    for (int i = 0; i < 500 && !accepted; ++i) {
        auto c = sim_cancel("sim-test-pipe-cancel");
        accepted = c.value("ok", false) && c.value("cancelled", false);
        if (!accepted) std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }
    CHECK(accepted);
    worker.join();
    CHECK_FALSE(r.value("ok", true));
    CHECK(r["reason"] == "cancelled");
    CHECK(r["steps"].get<int>() < 2000000);
}

namespace {
struct SimThreadEnv {
    std::string old_cap, old_lbm;
    bool had_cap = false, had_lbm = false;
    static void set(const char* key, const char* value) {
#ifdef _WIN32
        _putenv_s(key, value ? value : "");
#else
        if (value) setenv(key, value, 1);
        else unsetenv(key);
#endif
    }
    SimThreadEnv() {
        if (const char* value = std::getenv("CHUDWARE_SIM_THREADS")) { old_cap = value; had_cap = true; }
        if (const char* value = std::getenv("CHUDWARE_LBM_THREADS")) { old_lbm = value; had_lbm = true; }
        set("CHUDWARE_LBM_THREADS", "4");
    }
    ~SimThreadEnv() {
        set("CHUDWARE_SIM_THREADS", had_cap ? old_cap.c_str() : nullptr);
        set("CHUDWARE_LBM_THREADS", had_lbm ? old_lbm.c_str() : nullptr);
    }
};
}

TEST_CASE("LBM slab scheduling preserves every population-derived field across thread caps") {
    SimThreadEnv environment;
    constexpr int nx = 42, ny = 40, nz = 40;  // above the parallel threshold
    std::vector<uint8_t> solid((size_t)nx * ny * nz);
    for (int x = 15; x < 20; ++x)
        for (int y = 17; y < 22; ++y)
            for (int z = 16; z < 21; ++z) solid[((size_t)x * ny + y) * nz + z] = 1;
    chudware::sim::LbmParams p;
    p.backend = "cpu";
    p.tau0 = 0.8;
    SimThreadEnv::set("CHUDWARE_SIM_THREADS", "1");
    const auto serial = chudware::sim::lbm_run(solid, nx, ny, nz, p, 20);
    SimThreadEnv::set("CHUDWARE_SIM_THREADS", "4");
    const auto parallel = chudware::sim::lbm_run(solid, nx, ny, nz, p, 20);
    REQUIRE(serial.ok);
    REQUIRE(parallel.ok);
    CHECK(serial.rho == parallel.rho);
    CHECK(serial.ux == parallel.ux);
    CHECK(serial.uy == parallel.uy);
    CHECK(serial.uz == parallel.uz);
    CHECK(serial.drag_n == parallel.drag_n);
    CHECK(serial.lift_n == parallel.lift_n);
    CHECK(serial.side_n == parallel.side_n);
    CHECK(serial.residual == parallel.residual);
    CHECK(serial.mass_imbalance == parallel.mass_imbalance);
    CHECK(serial.steps_run == parallel.steps_run);
}

TEST_CASE("LBM CUDA low-memory checkpoint is terminal and records actual steps") {
    using namespace chudware::sim;
    using namespace chudware::governor;
    struct ProbeRestore {
        HostMemoryProbe previous;
        std::string old_governor;
        bool had_governor = false;
        explicit ProbeRestore(HostMemoryProbe probe) : previous(set_host_memory_probe(probe)) {
            if (const char* value = std::getenv("CHUDWARE_GOVERNOR")) {
                old_governor = value;
                had_governor = true;
            }
            SimThreadEnv::set("CHUDWARE_GOVERNOR", "1");
        }
        ~ProbeRestore() {
            set_host_memory_probe(previous);
            SimThreadEnv::set("CHUDWARE_GOVERNOR", had_governor ? old_governor.c_str() : nullptr);
        }
    };
    static thread_local int memory_polls = 0;
    // One low-memory observation followed by recovery catches an illicit CPU retry.
    ProbeRestore restore(+[]() -> HostMem {
        return {UINT64_C(8) << 30, ++memory_polls == 1 ? UINT64_C(1) : UINT64_C(6) << 30};
    });
    const int nx = 12, ny = 8, nz = 8;
    const std::vector<uint8_t> mask(nx * ny * nz, 0);
    for (const char* backend : {"cpu", "cuda"}) {
        CAPTURE(backend);
        memory_polls = 0;
        int starts = 0;
        LbmParams p;
        p.backend = backend;
        auto stopped = lbm_run(mask, nx, ny, nz, p, 100,
            [&](int step, int) { if (step == 0) ++starts; });
        CHECK_FALSE(stopped.ok);
        CHECK(stopped.steps_run == 10);
        CHECK(stopped.error.find("host memory critically low (governor)") != std::string::npos);
        CHECK(starts == 1);
        CHECK(memory_polls == 1);
        if (std::string(backend) == "cuda" && lbm_cuda_available())
            CHECK(stopped.backend == "cuda");
        else
            CHECK(stopped.backend == "cpu");
    }
}

TEST_CASE("pipe_flow pull-collision and window reuse preserve results across thread caps") {
    SimThreadEnv environment;
    const auto shell = stepped_duct({{0.0, 60.0, 5.0}}, 0.37, -0.21, 48);
    auto p = pipe_params(0.0, 60.0, 0.37, -0.21);
    p["mean_velocity_ms"] = 0.003;
    p["cells_across"] = 12;  // >8k active nodes, unlike a tiny diagnostic duct
    SimThreadEnv::set("CHUDWARE_SIM_THREADS", "1");
    const auto serial = chudware::sim::run_pipe_flow({shell}, p);
    SimThreadEnv::set("CHUDWARE_SIM_THREADS", "4");
    const auto parallel = chudware::sim::run_pipe_flow({shell}, p);
    REQUIRE_MESSAGE(serial.value("ok", false), serial.dump());
    REQUIRE_MESSAGE(parallel.value("ok", false), parallel.dump());
    for (const char* key : {"dp_pa", "steps", "converged", "mass_imbalance",
                            "simulated_reynolds", "pressure_profile"}) {
        REQUIRE(serial.contains(key));
        REQUIRE(parallel.contains(key));
        CHECK(serial[key] == parallel[key]);
    }
}

TEST_CASE("native input boundary: flow STL rejects exact-terminal malformed vertices") {
    const auto path = std::filesystem::temp_directory_path() / "chudware_flow_stl_boundary.stl";
    struct Cleanup {
        std::filesystem::path path;
        ~Cleanup() { std::filesystem::remove(path); }
    } cleanup{path};
    for (const std::string& suffix : {std::string("vertex"), std::string("vertex 1"),
                                      std::string("vertex 1 2")}) {
        for (std::size_t padding : {12u, 42u, 102u}) {
            std::ofstream(path, std::ios::binary) << "solid facet " << std::string(padding, ' ') << suffix;
            std::vector<float> positions;
            std::vector<uint32_t> indices;
            std::string error;
            CHECK_FALSE(chudware::sim::load_stl_soup(path.string(), positions, indices, error));
            CHECK(error == "bad ASCII STL vertex");
            CHECK(positions.empty());
            CHECK(indices.empty());
        }
    }
    std::ofstream(path, std::ios::binary) <<
        "solid test\nfacet normal 0 0 1\nouter loop\nvertex 0 0 0\n"
        "vertex 1 0 0\nvertex 0 1 0\nendloop\nendfacet\nendsolid test";
    std::vector<float> positions;
    std::vector<uint32_t> indices;
    std::string error;
    REQUIRE(chudware::sim::load_stl_soup(path.string(), positions, indices, error));
    CHECK(positions == std::vector<float>{0, 0, 0, 1, 0, 0, 0, 1, 0});
    CHECK(indices == std::vector<uint32_t>{0, 1, 2});
}
