// Unit tests for the C++ nsolve core. These mirror the solver-level behaviors the
// Python nsolve relies on (LM convergence, constrained drag staying on the hard
// manifold, DOF / redundant / conflicting diagnostics). The full sketch/mate
// parity lives in the differential corpus harness; this file just proves the
// numeric core builds and behaves under MSVC.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include "solver/nsolve.h"

#include <cmath>
#include <stdexcept>
#include <vector>

using chudware::solver::System;

namespace {

// Residual/Jacobian for "x_i - target" (a fix / dimension on a single var).
chudware::solver::ResidualFunc fix_r(double target) {
    return [target](const Eigen::VectorXd& xs) {
        Eigen::VectorXd r(1);
        r(0) = xs(0) - target;
        return r;
    };
}
chudware::solver::JacobianFunc fix_j() {
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

TEST_CASE("large under-defined solve preserves free coordinates and minimum-norm motion") {
    constexpr int pairs = 64;
    constexpr int variables = pairs * 3;
    std::vector<double> initial(variables, 0.0);
    for (int i = pairs * 2; i < variables; ++i) initial[i] = 3.0 + i * 0.25;
    System s;
    auto ids = s.add_vars(variables, initial);
    for (int i = 0; i < pairs; ++i) {
        const double target = (i % 2 ? -1.0 : 1.0) * (i + 1) / 100.0;
        s.add_residual(
            [target](const Eigen::VectorXd& xs) {
                Eigen::VectorXd r(1);
                r(0) = xs(0) + xs(1) - target;
                return r;
            },
            {ids[2 * i], ids[2 * i + 1]},
            [](const Eigen::VectorXd&) {
                return Eigen::MatrixXd::Ones(1, 2).eval();
            }, "coupled_pair");
    }
    const auto result = s.solve();
    INFO("iterations=" << result.iterations << " residual=" << result.max_residual
         << " first coordinates=" << result.x.head(6).transpose());
    REQUIRE(result.converged);
    for (int i = 0; i < pairs; ++i) {
        const double expected = (i % 2 ? -1.0 : 1.0) * (i + 1) / 200.0;
        CHECK(std::abs(result.x(2 * i) - expected) < 1e-8);
        CHECK(std::abs(result.x(2 * i + 1) - expected) < 1e-8);
    }
    for (int i = pairs * 2; i < variables; ++i)
        CHECK(result.x(i) == doctest::Approx(initial[i]).epsilon(1e-12));

    // Grouping the same equations into one residual must not change the motion.
    System grouped;
    const auto grouped_ids = grouped.add_vars(variables, initial);
    grouped.add_residual(
        [pairs](const Eigen::VectorXd& xs) {
            Eigen::VectorXd r(pairs);
            for (int i = 0; i < pairs; ++i) {
                const double target = (i % 2 ? -1.0 : 1.0) * (i + 1) / 100.0;
                r(i) = xs(2 * i) + xs(2 * i + 1) - target;
            }
            return r;
        }, grouped_ids,
        [pairs, variables](const Eigen::VectorXd&) {
            Eigen::MatrixXd j = Eigen::MatrixXd::Zero(pairs, variables);
            for (int i = 0; i < pairs; ++i) {
                j(i, 2 * i) = 1.0;
                j(i, 2 * i + 1) = 1.0;
            }
            return j;
        }, "grouped_pairs");
    const auto grouped_result = grouped.solve();
    REQUIRE(grouped_result.converged);
    CHECK((grouped_result.x - result.x).cwiseAbs().maxCoeff() < 1e-8);
}

TEST_CASE("large sparse solve retains weak coordinates in mixed-scale constraints") {
    int variables = 128;
    SUBCASE("fully constrained") {}
    SUBCASE("with an unrelated free coordinate") { variables = 129; }
    System s;
    std::vector<double> initial(variables, 0.0);
    if (variables > 128) initial[128] = 17.0;
    const auto ids = s.add_vars(variables, initial);
    for (int i = 0; i < 64; ++i) {
        s.add_residual(
            [](const Eigen::VectorXd& xs) {
                Eigen::VectorXd r(2);
                r << xs(0) + 3000.0 * xs(1), xs(1) - 0.1;
                return r;
            }, {ids[2 * i], ids[2 * i + 1]},
            [](const Eigen::VectorXd&) {
                Eigen::MatrixXd j(2, 2);
                j << 1.0, 3000.0, 0.0, 1.0;
                return j;
            }, "offset_rotation");
    }
    const auto result = s.solve();
    INFO("iterations=" << result.iterations << " residual=" << result.max_residual);
    REQUIRE(result.converged);
    for (int i = 0; i < 64; ++i) {
        CHECK(std::abs(result.x(2 * i) + 300.0) < 1e-6);
        CHECK(std::abs(result.x(2 * i + 1) - 0.1) < 1e-10);
    }
    if (variables > 128) CHECK(result.x(128) == 17.0);
}

TEST_CASE("high initial damping does not stop a progressing solve") {
    System s;
    const auto ids = s.add_vars(1);
    s.add_residual([](const Eigen::VectorXd& x) {
        return Eigen::VectorXd::Constant(1, 1e4 * x(0) - 1e-4).eval();
    }, ids, [](const Eigen::VectorXd&) {
        return Eigen::MatrixXd::Constant(1, 1, 1e4).eval();
    }, "scaled_coordinate");
    const auto result = s.solve();
    INFO("iterations=" << result.iterations << " residual=" << result.max_residual);
    REQUIRE(result.converged);
    CHECK(std::abs(result.x(0) - 1e-8) < 1e-14);
}

TEST_CASE("rank-only DOF counts independent hard constraints") {
    System s;
    CHECK(s.degrees_of_freedom(s.init_vector()) == 0);
    auto v = s.add_vars(2, {4.0, 3.0});
    CHECK(s.degrees_of_freedom(s.init_vector()) == 2);
    CHECK(s.degrees_of_freedom(s.init_vector()) == s.analyze(s.init_vector()).dof);

    s.add_residual(fix_r(4.0), {v[0]}, fix_j(), "xpin");
    CHECK(s.degrees_of_freedom(s.init_vector()) == 1);
    CHECK(s.degrees_of_freedom(s.init_vector()) == s.analyze(s.init_vector()).dof);

    SUBCASE("a consistent dependent row removes no freedom") {
        s.add_residual(fix_r(4.0), {v[0]}, fix_j(), "duplicate");
    }
    SUBCASE("a conflicting dependent row removes no freedom") {
        s.add_residual(fix_r(5.0), {v[0]}, fix_j(), "conflict");
    }
    CHECK(s.degrees_of_freedom(s.init_vector()) == 1);
    CHECK(s.degrees_of_freedom(s.init_vector()) == s.analyze(s.init_vector()).dof);

    s.add_residual(fix_r(3.0), {v[1]}, fix_j(), "ypin");
    CHECK(s.degrees_of_freedom(s.init_vector()) == 0);
    CHECK(s.degrees_of_freedom(s.init_vector()) == s.analyze(s.init_vector()).dof);
}

TEST_CASE("rank-only DOF excludes soft targets before and after hard constraints") {
    System s;
    auto v = s.add_vars(2, {0.0, 0.0});
    s.soft_target(v[0], 7.0);
    s.soft_target(v[1], 5.0);
    CHECK(s.degrees_of_freedom(s.init_vector()) == 2);
    CHECK(s.degrees_of_freedom(s.init_vector()) == s.analyze(s.init_vector()).dof);

    s.add_residual(fix_r(0.0), {v[1]}, fix_j(), "horizontal");
    CHECK(s.degrees_of_freedom(s.init_vector()) == 1);
    CHECK(s.degrees_of_freedom(s.init_vector()) == s.analyze(s.init_vector()).dof);

    s.clear_soft_targets();
    CHECK(s.degrees_of_freedom(s.init_vector()) == 1);
    CHECK(s.degrees_of_freedom(s.init_vector()) == s.analyze(s.init_vector()).dof);
}

TEST_CASE("rank-only DOF follows configuration-dependent constraint rank") {
    System s;
    auto v = s.add_vars(2, {1.0, 0.0});
    s.add_residual(
        [](const Eigen::VectorXd& xs) {
            Eigen::VectorXd r(1);
            r(0) = xs.squaredNorm() - 1.0;
            return r;
        },
        v,
        [](const Eigen::VectorXd& xs) {
            Eigen::MatrixXd j(1, 2);
            j.row(0) = 2.0 * xs.transpose();
            return j;
        },
        "circle");
    s.add_residual(fix_r(1.0), {v[0]}, fix_j(), "xpin");

    // At the tangent point the circle and x-pin have dependent gradients.
    Eigen::VectorXd x = s.init_vector();
    CHECK(s.degrees_of_freedom(x) == 1);
    CHECK(s.degrees_of_freedom(x) == s.analyze(x).dof);

    // Away from tangency their gradients are independent, even off the solution.
    x(1) = 1.0;
    CHECK(s.degrees_of_freedom(x) == 0);
    CHECK(s.degrees_of_freedom(x) == s.analyze(x).dof);
}

TEST_CASE("rank-only DOF preserves absolute and relative rank thresholds") {
    double scale = 0.1;
    SUBCASE("absolute tolerance below unit pivot") { scale = 0.1; }
    SUBCASE("relative tolerance above unit pivot") { scale = 10.0; }
    const double tol = 1e-5;
    const double threshold = tol * (scale > 1.0 ? scale : 1.0);
    Eigen::MatrixXd j = Eigen::MatrixXd::Zero(3, 3);
    j.diagonal() << scale, threshold, 2.0 * threshold;
    System s;
    auto v = s.add_vars(3);
    s.add_residual(
        [j](const Eigen::VectorXd& xs) -> Eigen::VectorXd { return j * xs; },
        v, [j](const Eigen::VectorXd&) { return j; }, "scaled_pins");

    // A pivot equal to the threshold is excluded, not rounded up to full rank.
    CHECK(s.degrees_of_freedom(s.init_vector(), tol) == 1);
    CHECK(s.degrees_of_freedom(s.init_vector(), tol) == s.analyze(s.init_vector(), tol).dof);
    CHECK(s.degrees_of_freedom(s.init_vector()) == 0);
}

TEST_CASE("rank-only DOF rejects wrong-sized configurations") {
    System s;
    CHECK_THROWS_AS(s.degrees_of_freedom(Eigen::VectorXd::Zero(1)), std::invalid_argument);
    auto v = s.add_vars(2);
    CHECK_THROWS_AS(s.degrees_of_freedom(Eigen::VectorXd::Zero(1)), std::invalid_argument);
    CHECK_THROWS_AS(s.degrees_of_freedom(Eigen::VectorXd::Zero(3)), std::invalid_argument);
    s.add_residual(fix_r(0.0), {v[1]}, fix_j(), "ypin");
    CHECK_THROWS_AS(s.degrees_of_freedom(Eigen::VectorXd::Zero(1)), std::invalid_argument);
    CHECK_THROWS_AS(s.degrees_of_freedom(Eigen::VectorXd::Zero(3)), std::invalid_argument);
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
