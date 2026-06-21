// Unit tests for the FEA static solver (aicad::sim::fea_static_solve).
//
// These are OCCT-free, oracle-free sanity checks on the numerics: a uniaxial bar
// under tension has an analytic displacement, and a fixed-everywhere or unloaded
// model must behave. The full Python-vs-C++ differential gate lives in
// tools/cpp_sim_parity.py (cantilever beam).
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <array>
#include <cmath>
#include <vector>

#include "sim/fea_static.h"

using aicad::sim::fea_static_solve;

namespace {

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

}  // namespace
