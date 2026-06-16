// Unit tests for the C++ nsolve core. These mirror the solver-level behaviors the
// Python nsolve relies on (LM convergence, constrained drag staying on the hard
// manifold, DOF / redundant / conflicting diagnostics). The full sketch/mate
// parity lives in the differential corpus harness; this file just proves the
// numeric core builds and behaves under MSVC.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include "solver/nsolve.h"

#include <cmath>

using aicad::solver::System;

namespace {

// Residual/Jacobian for "x_i - target" (a fix / dimension on a single var).
aicad::solver::ResidualFunc fix_r(double target) {
    return [target](const Eigen::VectorXd& xs) {
        Eigen::VectorXd r(1);
        r(0) = xs(0) - target;
        return r;
    };
}
aicad::solver::JacobianFunc fix_j() {
    return [](const Eigen::VectorXd&) {
        Eigen::MatrixXd j(1, 1);
        j(0, 0) = 1.0;
        return j;
    };
}

}  // namespace

TEST_CASE("empty system solves trivially") {
    System s;
    s.add_vars(3, {1.0, 2.0, 3.0});
    auto res = s.solve();
    CHECK(res.converged);
    CHECK(res.x.size() == 3);
    CHECK(res.x(1) == doctest::Approx(2.0));
}

TEST_CASE("two fixes drive two vars to targets (fully defined, dof 0)") {
    System s;
    auto v = s.add_vars(2, {0.0, 0.0});
    s.add_residual(fix_r(5.0), {v[0]}, fix_j(), "cx");
    s.add_residual(fix_r(-3.0), {v[1]}, fix_j(), "cy");

    auto res = s.solve();
    REQUIRE(res.converged);
    CHECK(res.x(0) == doctest::Approx(5.0));
    CHECK(res.x(1) == doctest::Approx(-3.0));

    auto a = s.analyze(res.x);
    CHECK(a.dof == 0);
    CHECK(a.well_posed);
    CHECK(a.conflicting_tags.empty());
    CHECK(a.redundant_tags.empty());
}

TEST_CASE("nonlinear: point on unit circle plus an x-pin solves") {
    // var0=x, var1=y. Constraints: x^2 + y^2 = 1, and x = 0.6 -> y = +-0.8.
    System s;
    auto v = s.add_vars(2, {0.5, 0.5});  // initial guess in the +y branch
    s.add_residual(
        [](const Eigen::VectorXd& xs) {
            Eigen::VectorXd r(1);
            r(0) = xs(0) * xs(0) + xs(1) * xs(1) - 1.0;
            return r;
        },
        {v[0], v[1]},
        [](const Eigen::VectorXd& xs) {
            Eigen::MatrixXd j(1, 2);
            j(0, 0) = 2.0 * xs(0);
            j(0, 1) = 2.0 * xs(1);
            return j;
        },
        "circle");
    s.add_residual(fix_r(0.6), {v[0]}, fix_j(), "xpin");

    auto res = s.solve();
    REQUIRE(res.converged);
    CHECK(res.x(0) == doctest::Approx(0.6));
    CHECK(std::abs(res.x(1)) == doctest::Approx(0.8));
    CHECK(res.x(1) > 0.0);  // started in +y branch, should stay there
}

TEST_CASE("redundant vs conflicting diagnostics") {
    SUBCASE("duplicate consistent dim -> redundant, still well posed") {
        System s;
        auto v = s.add_vars(1, {0.0});
        s.add_residual(fix_r(10.0), {v[0]}, fix_j(), "d1");
        s.add_residual(fix_r(10.0), {v[0]}, fix_j(), "d2");  // same value
        auto res = s.solve();
        REQUIRE(res.converged);
        auto a = s.analyze(res.x);
        CHECK(a.well_posed);
        CHECK(a.conflicting_tags.empty());
        // chronological attribution blames the later duplicate
        REQUIRE(a.redundant_tags.size() == 1);
        CHECK(a.redundant_tags[0] == "d2");
    }
    SUBCASE("contradictory dim -> conflicting, not well posed") {
        System s;
        auto v = s.add_vars(1, {0.0});
        s.add_residual(fix_r(10.0), {v[0]}, fix_j(), "d1");
        s.add_residual(fix_r(12.0), {v[0]}, fix_j(), "d2");  // contradiction
        auto res = s.solve();
        auto a = s.analyze(res.x);
        CHECK_FALSE(a.well_posed);
        REQUIRE(a.conflicting_tags.size() == 1);
        CHECK(a.conflicting_tags[0] == "d2");
    }
}

TEST_CASE("under-defined system reports dof") {
    System s;
    auto v = s.add_vars(2, {0.0, 0.0});
    s.add_residual(fix_r(4.0), {v[0]}, fix_j(), "xpin");  // only x pinned
    auto res = s.solve();
    REQUIRE(res.converged);
    auto a = s.analyze(res.x);
    CHECK(a.dof == 1);  // y is free
}

TEST_CASE("constrained drag keeps the hard constraint, follows the cursor") {
    // var0=x, var1=y, hard constraint y = 0 (horizontal). Drag toward (7, 5):
    // x should follow to ~7, y must stay pinned at 0 (the soft pull never wins).
    System s;
    auto v = s.add_vars(2, {1.0, 0.0});
    s.add_residual(fix_r(0.0), {v[1]}, fix_j(), "horiz");  // hard: y == 0
    s.soft_target(v[0], 7.0, 0.01);
    s.soft_target(v[1], 5.0, 0.01);

    auto res = s.solve(std::nullopt, 1e-10, 50, /*time_budget_ms=*/50.0);
    CHECK(res.x(0) == doctest::Approx(7.0).epsilon(0.02));  // followed cursor in x
    CHECK(std::abs(res.x(1)) < 1e-6);                       // hard constraint held
}
