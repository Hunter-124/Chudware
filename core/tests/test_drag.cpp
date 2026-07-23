// Drag tests for the C++ port of sketch_solver.py's drag(). Exact Python<->C++
// coordinate parity is timing-sensitive (soft least-squares + a wall-clock budget),
// so these gate on BEHAVIOR, not on Python-parity coordinates:
//   * the dragged free endpoint moves TOWARD the target;
//   * the HARD constraints (a fixed endpoint + a length dim) stay satisfied to 1e-6.
// A generous direct-solve budget is used where determinism matters; drag() itself
// runs the production 10 ms budget (ample for a 4-variable line).
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include "sketch/sketch.h"

#include <cmath>
#include <string>

using chudware::sketch::solve_sketch;
using chudware::sketch::drag;
using chudware::sketch::SketchSolution;
using json = nlohmann::json;

namespace {

// A single line e1 from (0,0) to (10,0): p1 fixed at the origin, |e1| = 10.
// e1.p2 is the free endpoint (2 DOF pinned only by the length dim => 1 DOF: it
// rides a circle of radius 10 about the origin).
json line_sketch() {
    json s;
    s["entities"] = json::array({
        json{{"id", "e1"}, {"type", "line"}, {"construction", false},
             {"p1", json::array({0.0, 0.0})}, {"p2", json::array({10.0, 0.0})}},
    });
    s["constraints"] = json::array({
        // fix e1.p1 at the origin (coincident with the virtual origin handle).
        json{{"id", "c_fix"}, {"type", "coincident"},
             {"refs", json::array({"e1.p1", "origin"})}},
        // length |e1| = 10.
        json{{"id", "c_len"}, {"type", "distance"},
             {"refs", json::array({"e1.p1", "e1.p2"})},
             {"value", json{{"value", 10.0}, {"expr", nullptr}}}, {"driven", false}},
    });
    return s;
}

double dist(const json& a, const json& b) {
    double dx = a[0].get<double>() - b[0].get<double>();
    double dy = a[1].get<double>() - b[1].get<double>();
    return std::hypot(dx, dy);
}

}  // namespace

TEST_CASE("drag pulls the free endpoint toward the target while constraints hold") {
    json sketch = line_sketch();
    json env = json::object();

    // Cold solve to get a warm state and the starting coords.
    SketchSolution sol = solve_sketch(sketch, env);
    REQUIRE(sol.converged);
    const json& p2_start = sol.entities["e1"]["p2"];
    CHECK(p2_start[0].get<double>() == doctest::Approx(10.0).epsilon(1e-9));
    CHECK(p2_start[1].get<double>() == doctest::Approx(0.0).epsilon(1e-9));

    // Drag e1.p2 toward (7, 7). The length constraint forces it onto the radius-10
    // circle about the origin, so the closest feasible point is (10/sqrt2, 10/sqrt2)
    // ~ (7.0711, 7.0711) — up and to the left of the start (10, 0).
    std::pair<double, double> target{7.0, 7.0};
    json res = drag(sol.state, "e1.p2", target, sketch, env);

    REQUIRE(res["ok"].get<bool>());
    const json& ents = res["entities"];
    const json& p1 = ents["e1"]["p1"];
    const json& p2 = ents["e1"]["p2"];

    // (1) HARD: the fixed endpoint stays at the origin.
    CHECK(std::abs(p1[0].get<double>()) <= 1e-6);
    CHECK(std::abs(p1[1].get<double>()) <= 1e-6);

    // (2) HARD: the length dim is satisfied to 1e-6.
    double L = dist(p2, p1);
    CHECK(std::abs(L - 10.0) <= 1e-6);

    // (3) The dragged point moved TOWARD the target (its distance to the target
    // strictly decreased vs the pre-drag position).
    json tgt = json::array({target.first, target.second});
    double before = dist(p2_start, tgt);
    double after = dist(p2, tgt);
    CHECK(after < before - 1e-6);

    // (4) For THIS geometry the feasible closest point to (7,7) is (10/sqrt2,
    // 10/sqrt2) ~ (7.0711, 7.0711): y climbs off the axis and x retreats from 10.
    const double kRoot = 10.0 / std::sqrt(2.0);
    CHECK(p2[0].get<double>() == doctest::Approx(kRoot).epsilon(1e-3));
    CHECK(p2[1].get<double>() == doctest::Approx(kRoot).epsilon(1e-3));
}

TEST_CASE("drag rejects an unresolvable handle and leaves coords unchanged") {
    json sketch = line_sketch();
    json env = json::object();
    SketchSolution sol = solve_sketch(sketch, env);

    // The virtual origin handle has zero variables — nothing to drag.
    std::pair<double, double> target{5.0, 5.0};
    json res = drag(sol.state, "origin", target, sketch, env);

    CHECK_FALSE(res["ok"].get<bool>());
    REQUIRE(res["rejected_handles"].is_array());
    REQUIRE(res["rejected_handles"].size() == 1);
    CHECK(res["rejected_handles"][0].get<std::string>() == "origin");
    // Coords come back as the unchanged warm state.
    const json& p2 = res["entities"]["e1"]["p2"];
    CHECK(p2[0].get<double>() == doctest::Approx(10.0).epsilon(1e-9));
}

TEST_CASE("multi-target drag translates a whole line rigidly, length preserved") {
    json sketch = line_sketch();
    json env = json::object();
    SketchSolution sol = solve_sketch(sketch, env);

    // Drag BOTH endpoints by the same +y translation via the targets list. p1 is
    // pinned to the origin by c_fix, so the soft pull on p1 is dominated by the hard
    // fix; p2 follows onto the feasible circle. The length stays exactly 10.
    json targets = json::array({
        json{{"handle", "e1.p1"}, {"xy", json::array({0.0, 3.0})}},
        json{{"handle", "e1.p2"}, {"xy", json::array({10.0, 3.0})}},
    });
    json res = drag(sol.state, "", {0.0, 0.0}, sketch, env, targets);

    REQUIRE(res["ok"].get<bool>());
    const json& p1 = res["entities"]["e1"]["p1"];
    const json& p2 = res["entities"]["e1"]["p2"];
    // HARD fix dominates the soft pull on p1: it stays at the origin.
    CHECK(std::abs(p1[0].get<double>()) <= 1e-6);
    CHECK(std::abs(p1[1].get<double>()) <= 1e-6);
    // HARD length preserved to 1e-6.
    CHECK(std::abs(dist(p2, p1) - 10.0) <= 1e-6);
}
