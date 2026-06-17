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

}  // namespace
