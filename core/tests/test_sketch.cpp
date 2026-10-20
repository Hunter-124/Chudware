// Sketch-solver tests mirroring engine/doc/test_sketch_solver.py for the ported
// constraint subset. These are the C++ side of what the differential harness will
// later check against the live Python oracle; for now they pin the bracket DOF /
// redundant / conflict / parametric behaviors and a handful of relations.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include "sketch/sketch.h"

#include <algorithm>
#include <cmath>
#include <string>
#include <vector>

using chudware::sketch::solve_sketch;
using json = nlohmann::json;

namespace {

json line(const char* id, double x1, double y1, double x2, double y2) {
    return json{{"id", id}, {"type", "line"}, {"construction", false},
                {"p1", json::array({x1, y1})}, {"p2", json::array({x2, y2})}};
}
json circle(const char* id, double cx, double cy, double r) {
    return json{{"id", id}, {"type", "circle"}, {"construction", false},
                {"center", json::array({cx, cy})}, {"radius", r}};
}
json arc(const char* id, double cx, double cy, double r, double a0, double a1) {
    return json{{"id", id}, {"type", "arc"}, {"construction", false},
                {"center", json::array({cx, cy})}, {"radius", r},
                {"start", a0}, {"end", a1}};
}
json point(const char* id, double x, double y) {
    return json{{"id", id}, {"type", "point"}, {"construction", false},
                {"p", json::array({x, y})}};
}
json rel(const char* id, const char* type, std::vector<const char*> refs) {
    json r = json::array();
    for (auto s : refs) r.push_back(s);
    return json{{"id", id}, {"type", type}, {"refs", r}};
}
json dim(const char* id, const char* type, std::vector<const char*> refs, double v,
         const char* expr = nullptr) {
    json r = json::array();
    for (auto s : refs) r.push_back(s);
    json val = json{{"value", v}, {"expr", expr ? json(expr) : json(nullptr)}};
    return json{{"id", id}, {"type", type}, {"refs", r}, {"value", val}, {"driven", false}};
}

// Bracket Sketch1: rectangle of 4 lines, h/v + coincident chain, fix corner, W/H dims.
json bracket(double w = 80.0, double h = 50.0, const char* w_expr = nullptr) {
    json s;
    s["entities"] = json::array({line("e1", 0, 0, w, 0), line("e2", w, 0, w, h),
                                 line("e3", w, h, 0, h), line("e4", 0, h, 0, 0)});
    s["constraints"] = json::array({
        rel("c1", "coincident", {"e1.p2", "e2.p1"}),
        rel("c2", "coincident", {"e2.p2", "e3.p1"}),
        rel("c3", "coincident", {"e3.p2", "e4.p1"}),
        rel("c4", "coincident", {"e4.p2", "e1.p1"}),
        rel("c5", "horizontal", {"e1"}),
        rel("c6", "horizontal", {"e3"}),
        rel("c7", "vertical", {"e2"}),
        rel("c8", "vertical", {"e4"}),
        rel("c9", "fix", {"e1.p1"}),
        dim("c10", "distance", {"e1.p1", "e1.p2"}, w, w_expr),
        dim("c11", "distance", {"e2.p1", "e2.p2"}, h),
    });
    return s;
}

bool has(const std::vector<std::string>& v, const std::string& s) {
    return std::find(v.begin(), v.end(), s) != v.end();
}

}  // namespace

TEST_CASE("bracket fully defined: dof 0, all full, width 80") {
    auto sol = solve_sketch(bracket(), json::object());
    REQUIRE(sol.converged);
    CHECK(sol.dof == 0);
    CHECK(sol.conflicting.empty());
    for (auto id : {"e1", "e2", "e3", "e4"}) CHECK(sol.statuses[id] == "full");
    double x2 = sol.entities["e1"]["p2"][0];
    double y2 = sol.entities["e1"]["p2"][1];
    CHECK(x2 == doctest::Approx(80.0));
    CHECK(std::abs(y2) < 1e-5);
}

TEST_CASE("remove width dim -> dof 1 with an under entity") {
    json s = bracket();
    // drop c10 (width)
    json cs = json::array();
    for (auto& c : s["constraints"])
        if (c["id"] != "c10") cs.push_back(c);
    s["constraints"] = cs;
    auto sol = solve_sketch(s, json::object());
    CHECK(sol.dof == 1);
    int under = 0;
    for (auto& kv : sol.statuses)
        if (kv.second == "under") ++under;
    CHECK(under >= 1);
}

TEST_CASE("spline interior freedom does not unpin constrained endpoints and neighbors") {
    json points = json::array();
    for (int i = 0; i <= 40; ++i)
        points.push_back(json::array({0.25 * i, 0.01 * i * (40 - i)}));
    json s = {
        {"entities", json::array({
            point("e1", 0, 0), point("e2", 10, 0),
            json{{"id", "e3"}, {"type", "spline"}, {"points", points}},
            line("e4", 0, 0, 10, 0), line("e5", 0, 3, 10, 3)})},
        {"constraints", json::array({
            rel("c1", "fix", {"e1"}), rel("c2", "fix", {"e2"}),
            rel("c3", "coincident", {"e3.start", "e1"}),
            rel("c4", "coincident", {"e3.end", "e2"}),
            rel("c5", "coincident", {"e4.p1", "e1"}),
            rel("c6", "coincident", {"e4.p2", "e2"}),
            rel("c7", "horizontal", {"e5"})})}
    };
    const auto sol = solve_sketch(s, json::object());
    REQUIRE(sol.converged);
    CHECK(sol.dof == 81);
    for (const auto* id : {"e1", "e2", "e4"})
        CHECK(sol.statuses.at(id) == "full");
    CHECK(sol.statuses.at("e3") == "under");
    CHECK(sol.statuses.at("e5") == "under");

    const auto free = chudware::sketch::free_variables(s, json::object(), sol.state.x);
    int spline_free = 0, line_free = 0;
    for (const auto& variable : free) {
        if (variable.entity == "e3") ++spline_free;
        else if (variable.entity == "e5") ++line_free;
        else FAIL_CHECK("a pinned coordinate was reported free");
    }
    CHECK(spline_free == 78);
    CHECK(line_free == 4);
}

TEST_CASE("duplicate width dim -> redundant, still well posed") {
    json s = bracket();
    s["constraints"].push_back(dim("c12", "distance", {"e1.p1", "e1.p2"}, 80.0));
    auto sol = solve_sketch(s, json::object());
    CHECK(sol.converged);
    CHECK(sol.conflicting.empty());
    CHECK((has(sol.redundant, "c10") || has(sol.redundant, "c12")));
}

TEST_CASE("contradictory width dim -> conflicting") {
    json s = bracket();
    s["constraints"].push_back(dim("c12", "distance", {"e1.p1", "e1.p2"}, 90.0));
    auto sol = solve_sketch(s, json::object());
    CHECK_FALSE(sol.conflicting.empty());
}

TEST_CASE("parametric edit width 100") {
    auto sol = solve_sketch(bracket(100.0), json::object());
    REQUIRE(sol.converged);
    CHECK(sol.dof == 0);
    double w = std::abs(double(sol.entities["e1"]["p2"][0]) - double(sol.entities["e1"]["p1"][0]));
    CHECK(w == doctest::Approx(100.0));
}

TEST_CASE("expression dim width = W (=120)") {
    json env = json{{"W", 120.0}, {"H", 50.0}};
    auto sol = solve_sketch(bracket(120.0, 50.0, "W"), env);
    REQUIRE(sol.converged);
    double w = std::abs(double(sol.entities["e1"]["p2"][0]) - double(sol.entities["e1"]["p1"][0]));
    CHECK(w == doctest::Approx(120.0));
}

TEST_CASE("tangent line-circle: center distance equals radius") {
    json s;
    s["entities"] = json::array({line("e1", -20, 0, 20, 0), circle("e2", 0, 5, 5)});
    s["constraints"] = json::array({
        rel("c1", "fix", {"e1.p1"}),
        rel("c2", "fix", {"e1.p2"}),
        rel("c3", "fix", {"e2.center"}),
        dim("c4", "radius", {"e2"}, 5.0),
        rel("c5", "tangent", {"e1", "e2"}),
    });
    auto sol = solve_sketch(s, json::object());
    REQUIRE(sol.converged);
    double cy = sol.entities["e2"]["center"][1];
    double r = sol.entities["e2"]["radius"];
    CHECK(std::abs(cy - r) < 1e-4);
}

TEST_CASE("concentric arcs share a center") {
    json s;
    s["entities"] = json::array({arc("e1", 0, 0, 10, 0, 90), arc("e2", 1, 1, 15, 0, 90)});
    s["constraints"] = json::array({
        rel("c1", "fix", {"e1.center"}),
        rel("c2", "concentric", {"e1", "e2"}),
    });
    auto sol = solve_sketch(s, json::object());
    REQUIRE(sol.converged);
    double dx = double(sol.entities["e1"]["center"][0]) - double(sol.entities["e2"]["center"][0]);
    double dy = double(sol.entities["e1"]["center"][1]) - double(sol.entities["e2"]["center"][1]);
    CHECK(std::hypot(dx, dy) < 1e-5);
}

// N-2: the arc `ccw` flag is a DISCRETE attribute the solver doesn't vary, so it must be
// carried through solve_sketch -> extract_entities verbatim. Dropping it (the old bug) made
// every solved arc default to ccw:true downstream, so a CW arc silently became its CCW
// complement (minor<->major) in a sweep/revolve path wire — two sweeps differing only in
// `ccw` then gave byte-identical volume.
TEST_CASE("arc ccw flag survives the solver (N-2)") {
    auto solved_ccw = [](bool ccw) {
        json s;
        json a = {{"id", "e1"}, {"type", "arc"}, {"construction", false},
                  {"center", json::array({0.0, 0.0})}, {"radius", 10.0},
                  {"start", 0.0}, {"end", 90.0}, {"ccw", ccw}};
        s["entities"] = json::array({a});
        s["constraints"] = json::array({rel("c1", "fix", {"e1.center"})});
        auto sol = solve_sketch(s, json::object());
        REQUIRE(sol.entities.contains("e1"));
        REQUIRE(sol.entities["e1"].contains("ccw"));
        return sol.entities["e1"]["ccw"].get<bool>();
    };
    CHECK(solved_ccw(true) == true);
    CHECK(solved_ccw(false) == false);  // the flag is preserved, not forced to the default
}

TEST_CASE("midpoint of a line") {
    json s;
    s["entities"] = json::array({line("e1", 0, 0, 10, 0), point("e2", 3, 0.5)});
    s["constraints"] = json::array({
        rel("c1", "fix", {"e1.p1"}),
        rel("c2", "fix", {"e1.p2"}),
        rel("c3", "midpoint", {"e2", "e1"}),
    });
    auto sol = solve_sketch(s, json::object());
    REQUIRE(sol.converged);
    CHECK(double(sol.entities["e2"]["p"][0]) == doctest::Approx(5.0));
    CHECK(double(sol.entities["e2"]["p"][1]) == doctest::Approx(0.0));
}

TEST_CASE("point on line") {
    json s;
    s["entities"] = json::array({line("e1", 0, 0, 10, 0), point("e2", 5, 3)});
    s["constraints"] = json::array({
        rel("c1", "fix", {"e1.p1"}),
        rel("c2", "fix", {"e1.p2"}),
        rel("c3", "point_on", {"e2", "e1"}),
    });
    auto sol = solve_sketch(s, json::object());
    REQUIRE(sol.converged);
    double px = sol.entities["e2"]["p"][0], py = sol.entities["e2"]["p"][1];
    double cross = (10.0 - 0.0) * (py - 0.0) - (0.0) * (px - 0.0);
    CHECK(std::abs(cross) < 1e-5);
}

TEST_CASE("equal line lengths") {
    json s;
    s["entities"] = json::array({line("e1", 0, 0, 10, 0), line("e2", 0, 5, 7, 5)});
    s["constraints"] = json::array({
        rel("c1", "fix", {"e1.p1"}),
        rel("c2", "fix", {"e1.p2"}),
        rel("c3", "fix", {"e2.p1"}),
        rel("c4", "horizontal", {"e2"}),
        rel("c5", "equal", {"e1", "e2"}),
    });
    auto sol = solve_sketch(s, json::object());
    REQUIRE(sol.converged);
    double L1 = std::hypot(double(sol.entities["e1"]["p2"][0]) - double(sol.entities["e1"]["p1"][0]),
                           double(sol.entities["e1"]["p2"][1]) - double(sol.entities["e1"]["p1"][1]));
    double L2 = std::hypot(double(sol.entities["e2"]["p2"][0]) - double(sol.entities["e2"]["p1"][0]),
                           double(sol.entities["e2"]["p2"][1]) - double(sol.entities["e2"]["p1"][1]));
    CHECK(L1 == doctest::Approx(L2));
}

// --- ported-constraint tests (mirror engine/doc/test_sketch_solver.py) -------

TEST_CASE("angle between two lines (30 deg, polar placement)") {
    json s;
    s["entities"] = json::array({line("e1", 0, 0, 10, 0), line("e2", 0, 0, 8, 1)});
    s["constraints"] = json::array({
        rel("c1", "fix", {"e1.p1"}),
        rel("c2", "fix", {"e1.p2"}),
        rel("c3", "fix", {"e2.p1"}),
        dim("c4", "distance", {"e2.p1", "e2.p2"}, 10.0),
        dim("c5", "angle", {"e1", "e2"}, 30.0),
    });
    auto sol = solve_sketch(s, json::object());
    REQUIRE(sol.converged);
    CHECK(sol.dof == 0);
    CHECK(double(sol.entities["e2"]["p2"][0]) == doctest::Approx(10.0 * std::cos(30.0 * 3.14159265358979323846 / 180.0)));
    CHECK(double(sol.entities["e2"]["p2"][1]) == doctest::Approx(10.0 * std::sin(30.0 * 3.14159265358979323846 / 180.0)));
}

TEST_CASE("collinear snaps a free line onto the infinite line of a fixed one") {
    json s;
    s["entities"] = json::array({line("e1", 0, 0, 10, 0), line("e2", 20, 0, 30, 0)});
    s["constraints"] = json::array({
        rel("c1", "fix", {"e1"}),
        rel("c2", "collinear", {"e1", "e2"}),
        dim("c3", "distance", {"e1.p1", "e2.p1"}, 20.0),
        dim("c4", "distance", {"e2.p1", "e2.p2"}, 10.0),
    });
    auto sol = solve_sketch(s, json::object());
    REQUIRE(sol.converged);
    CHECK(std::abs(double(sol.entities["e2"]["p1"][1])) < 1e-5);
    CHECK(std::abs(double(sol.entities["e2"]["p2"][1])) < 1e-5);
}

TEST_CASE("collinear over parallel+coincident is redundant (not conflicting)") {
    json s;
    s["entities"] = json::array({line("e1", 0, 0, 10, 0), line("e2", 10, 0, 20, 0)});
    s["constraints"] = json::array({
        rel("c1", "fix", {"e1"}),
        rel("c2", "coincident", {"e1.p2", "e2.p1"}),
        rel("c3", "parallel", {"e1", "e2"}),
        rel("c4", "collinear", {"e1", "e2"}),
        dim("c5", "distance", {"e2.p1", "e2.p2"}, 10.0),
    });
    auto sol = solve_sketch(s, json::object());
    REQUIRE(sol.converged);
    CHECK(sol.conflicting.empty());
    CHECK(has(sol.redundant, "c4"));
}

TEST_CASE("coradial ties center and radius of two circles") {
    json s;
    s["entities"] = json::array({circle("e1", 0, 0, 8), circle("e2", 2, 1, 5)});
    s["constraints"] = json::array({
        rel("c1", "fix", {"e1"}),
        rel("c2", "coradial", {"e1", "e2"}),
    });
    auto sol = solve_sketch(s, json::object());
    REQUIRE(sol.converged);
    CHECK(sol.dof == 0);
    double cd = std::hypot(double(sol.entities["e1"]["center"][0]) - double(sol.entities["e2"]["center"][0]),
                           double(sol.entities["e1"]["center"][1]) - double(sol.entities["e2"]["center"][1]));
    CHECK(cd < 1e-5);
    CHECK(double(sol.entities["e2"]["radius"]) == doctest::Approx(8.0));
}

TEST_CASE("symmetric mirrors a free point across the axis") {
    json s;
    json axis = line("axis", 5, 0, 5, 10);
    axis["construction"] = true;
    s["entities"] = json::array({point("e1", 2, 3), point("e2", 7, -1), axis});
    s["constraints"] = json::array({
        rel("c1", "fix", {"axis.p1"}),
        rel("c2", "fix", {"axis.p2"}),
        rel("c3", "fix", {"e1"}),
        rel("c4", "symmetric", {"e1", "e2", "axis"}),
    });
    auto sol = solve_sketch(s, json::object());
    REQUIRE(sol.converged);
    CHECK(double(sol.entities["e2"]["p"][0]) == doctest::Approx(8.0));
    CHECK(double(sol.entities["e2"]["p"][1]) == doctest::Approx(3.0));
}

TEST_CASE("tangent circle-circle external: d = r0 + r1") {
    json s;
    s["entities"] = json::array({circle("e1", 0, 0, 5), circle("e2", 12, 0, 4)});
    s["constraints"] = json::array({
        rel("c1", "fix", {"e1"}),
        rel("c2", "fix", {"e2.center"}),
        rel("c3", "tangent", {"e1", "e2"}),
    });
    auto sol = solve_sketch(s, json::object());
    REQUIRE(sol.converged);
    double d = std::hypot(double(sol.entities["e1"]["center"][0]) - double(sol.entities["e2"]["center"][0]),
                          double(sol.entities["e1"]["center"][1]) - double(sol.entities["e2"]["center"][1]));
    CHECK(d == doctest::Approx(double(sol.entities["e1"]["radius"]) + double(sol.entities["e2"]["radius"])));
    CHECK(double(sol.entities["e2"]["radius"]) == doctest::Approx(7.0));
}

TEST_CASE("tangent circle-circle converges when centers start coincident") {
    // Internal branch with d=0 at init: Jacobian must use the (+x) fallback so Newton
    // can push the free center out to |r0-r1| instead of corrupting radii.
    json s;
    s["entities"] = json::array({circle("e1", 0, 0, 10), circle("e2", 0, 0, 4)});
    s["constraints"] = json::array({
        rel("c1", "fix", {"e1"}),
        dim("c2", "radius", {"e2"}, 4.0),
        rel("c3", "tangent", {"e1", "e2"}),
    });
    auto sol = solve_sketch(s, json::object());
    REQUIRE(sol.converged);
    double d = std::hypot(double(sol.entities["e1"]["center"][0]) - double(sol.entities["e2"]["center"][0]),
                          double(sol.entities["e1"]["center"][1]) - double(sol.entities["e2"]["center"][1]));
    CHECK(d == doctest::Approx(6.0).epsilon(1e-4));
    CHECK(double(sol.entities["e2"]["radius"]) == doctest::Approx(4.0));
}

TEST_CASE("tangent circle-circle internal keeps a nested pair nested") {
    json s;
    s["entities"] = json::array({circle("e1", 0, 0, 10), circle("e2", 3, 0, 4)});
    s["constraints"] = json::array({
        rel("c1", "fix", {"e1"}),
        dim("c2", "radius", {"e2"}, 4.0),
        rel("c3", "horizontal", {"e1.center", "e2.center"}),
        rel("c4", "tangent", {"e1", "e2"}),
    });
    auto sol = solve_sketch(s, json::object());
    REQUIRE(sol.converged);
    double d = std::hypot(double(sol.entities["e1"]["center"][0]) - double(sol.entities["e2"]["center"][0]),
                          double(sol.entities["e1"]["center"][1]) - double(sol.entities["e2"]["center"][1]));
    CHECK(d == doctest::Approx(std::abs(double(sol.entities["e1"]["radius"]) - double(sol.entities["e2"]["radius"]))));
    CHECK(d < double(sol.entities["e1"]["radius"]));  // still nested
}

TEST_CASE("distance cond min drives near-rim gap (point-circle)") {
    json s;
    s["entities"] = json::array({point("e1", 0, 0), circle("e2", 20, 0, 5)});
    json dc = dim("c4", "distance", {"e1", "e2"}, 10.0);
    dc["cond"] = json::array({"center", "min"});
    s["constraints"] = json::array({
        rel("c1", "fix", {"e1"}),
        dim("c2", "radius", {"e2"}, 5.0),
        rel("c3", "horizontal", {"e1", "e2.center"}),
        dc,
    });
    auto sol = solve_sketch(s, json::object());
    REQUIRE(sol.converged);
    CHECK(sol.dof == 0);
    // near-rim gap = 10 -> center distance = 10 + r = 15
    double cd = std::hypot(double(sol.entities["e2"]["center"][0]), double(sol.entities["e2"]["center"][1]));
    CHECK(cd == doctest::Approx(15.0));
}

TEST_CASE("distance axis-h rim wall on concentric circles stays put") {
    // Wall thickness as an axis-h dimension between concentric circles: outer
    // far rim (max) to inner near rim (min). Additive (radial-consistent) form:
    // |Dx| + s0*r0 + s1*r1 - d = 0 + 20 - r2 - 5 -> r2 = 15. Centres stay put.
    // (cond [min,min] here would be the degenerate concentric overlap; see the
    // SW-validation note in logs/agentB-blockers.md.)
    json s;
    s["entities"] = json::array({circle("e1", 0, 0, 20), circle("e2", 0, 0, 12)});
    json dc = dim("c4", "distance", {"e1", "e2"}, 5.0);
    dc["axis"] = "h";
    dc["cond"] = json::array({"max", "min"});
    s["constraints"] = json::array({
        rel("c1", "coincident", {"e1.center", "origin"}),
        rel("c2", "concentric", {"e1", "e2"}),
        dim("c3", "diameter", {"e1"}, 40.0),
        dc,
    });
    auto sol = solve_sketch(s, json::object());
    REQUIRE(sol.converged);
    CHECK(sol.dof == 0);
    CHECK(std::abs(double(sol.entities["e2"]["center"][0])) < 1e-5);  // not flung apart
    CHECK(double(sol.entities["e2"]["radius"]) == doctest::Approx(15.0));  // wall 5
}

TEST_CASE("slot length + width dims (distance on spine, diameter on cap)") {
    json s;
    s["entities"] = json::array(
        {json{{"id", "e1"}, {"type", "slot"}, {"construction", false},
              {"p1", json::array({0.0, 0.0})}, {"p2", json::array({40.0, 0.0})},
              {"width", 10.0}}});
    s["constraints"] = json::array({
        rel("c1", "fix", {"e1.p1"}),
        rel("c2", "fix", {"e1.p2"}),
        dim("c3", "diameter", {"e1"}, 16.0),
    });
    auto sol = solve_sketch(s, json::object());
    REQUIRE(sol.converged);
    CHECK(sol.dof == 0);
    double len = std::hypot(double(sol.entities["e1"]["p2"][0]) - double(sol.entities["e1"]["p1"][0]),
                            double(sol.entities["e1"]["p2"][1]) - double(sol.entities["e1"]["p1"][1]));
    CHECK(len == doctest::Approx(40.0));
    CHECK(double(sol.entities["e1"]["width"]) == doctest::Approx(16.0));
}

TEST_CASE("spline fix pins every through-point; coincident on a node") {
    json s;
    json sp = json{{"id", "e1"}, {"type", "spline"}, {"construction", false},
                   {"points", json::array({json::array({0.0, 0.0}), json::array({10.0, 5.0}),
                                           json::array({20.0, 0.0})})}};
    s["entities"] = json::array({sp, point("p", 1, 1)});
    s["constraints"] = json::array({
        rel("c1", "fix", {"e1"}),
        rel("c2", "coincident", {"p", "e1.p1"}),
    });
    auto sol = solve_sketch(s, json::object());
    REQUIRE(sol.converged);
    CHECK(sol.dof == 0);
    // coincident pulls p onto spline node 1 (10,5)
    CHECK(double(sol.entities["p"]["p"][0]) == doctest::Approx(10.0));
    CHECK(double(sol.entities["p"]["p"][1]) == doctest::Approx(5.0));
}

TEST_CASE("spline start/end handles name endpoints, not interior knots") {
    json s;
    json sp = json{{"id", "e1"}, {"type", "spline"}, {"construction", false},
                   {"points", json::array({json::array({0.0, 0.0}), json::array({10.0, 5.0}),
                                           json::array({20.0, 5.0}), json::array({30.0, 0.0})})}};
    s["entities"] = json::array({sp, line("e2", -5, -5, 35, -5)});
    s["constraints"] = json::array({
        rel("c1", "fix", {"e1"}),
        rel("c2", "coincident", {"e1.start", "e2.p1"}),
        rel("c3", "coincident", {"e1.end", "e2.p2"}),
    });
    auto sol = solve_sketch(s, json::object());
    REQUIRE(sol.converged);
    CHECK(sol.dof == 0);
    CHECK(double(sol.entities["e2"]["p1"][0]) == doctest::Approx(0.0));
    CHECK(double(sol.entities["e2"]["p1"][1]) == doctest::Approx(0.0));
    CHECK(double(sol.entities["e2"]["p2"][0]) == doctest::Approx(30.0));
    CHECK(double(sol.entities["e2"]["p2"][1]) == doctest::Approx(0.0));
    CHECK(double(sol.entities["e1"]["points"][1][0]) == doctest::Approx(10.0));
    CHECK(double(sol.entities["e1"]["points"][1][1]) == doctest::Approx(5.0));
}

TEST_CASE("malformed entity + constraint on it: skipped, no crash (hostd SIGSEGV regression)") {
    // An entity whose allocation throws registers ZERO solver vars (the forward-tolerant
    // fallback in the solve loop). A constraint referencing it used to index that empty
    // vector unchecked in pick() -> out-of-bounds read -> SIGSEGV killing hostd mid
    // agent-run ("provider_error — daemon connection closed", 2026-07-27). Now handle
    // resolution throws and the constraint is skipped; the healthy entities still solve.
    json s;
    s["entities"] = json::array({
        line("e1", 0, 0, 80, 0),
        json{{"id", "bad"}, {"type", "line"}, {"construction", false},
             {"p1", json::array({"oops", 0})}, {"p2", json::array({10, 0})}},
    });
    s["constraints"] = json::array({
        rel("c1", "coincident", {"e1.p1", "bad.p1"}),
        rel("c2", "coincident", {"bad.p2", "e1.p2"}),
        rel("c3", "fix", {"e1.p1"}),
        dim("c4", "distance", {"e1.p1", "e1.p2"}, 80.0),
    });
    auto sol = solve_sketch(s, json::object());
    REQUIRE(sol.converged);
    // e1: 4 vars - fix(p1) - distance = 1 rotational DOF; "bad" contributes none.
    CHECK(sol.dof == 1);
    CHECK(double(sol.entities["e1"]["p2"][0]) == doctest::Approx(80.0));
}

TEST_CASE("shorthand id-less circles do not crash the solver") {
    // Regression: the live agent turn of 2026-07-28 wrote a `PatternSk` sketch as a
    // lattice of bare circles in the shorthand dialect — {"type":"circle","cx":…,
    // "cy":…,"r":…} with no "id", no "center", no "radius". allocate_entity read
    // e["center"] / e["id"] through nlohmann's CONST operator[], which is UNDEFINED
    // BEHAVIOUR on a missing key: it dereferenced end() and handed num() a garbage
    // json, SIGSEGV'ing inside get_arithmetic_value(). That killed the python admin
    // worker mid-turn (coredump PID 3454839), which the host reported only as
    // "admin worker closed stdout" / stop_reason non_modeling_request, losing the
    // whole session's modeling work. Missing keys must degrade to a skipped entity.
    json s;
    json ents = json::array();
    // Enough entities to force the underlying vector to reallocate several times.
    for (int i = 0; i < 64; ++i)
        ents.push_back(json{{"type", "circle"},
                            {"cx", -23.0 + 1.5 * i},
                            {"cy", (i % 2) ? 4.0 : 0.0},
                            {"r", 2.25}});
    // One well-formed entity alongside them: the healthy geometry must still solve.
    ents.push_back(line("e1", 0, 0, 80, 0));
    s["entities"] = ents;
    s["constraints"] = json::array({
        rel("c1", "fix", {"e1.p1"}),
        dim("c2", "distance", {"e1.p1", "e1.p2"}, 80.0),
    });
    auto sol = solve_sketch(s, json::object());
    REQUIRE(sol.converged);
    CHECK(double(sol.entities["e1"]["p2"][0]) == doctest::Approx(80.0));
}

TEST_CASE("entities missing their coordinate keys are skipped, not dereferenced") {
    // Same UB class, one entity per affected type: every branch of allocate_entity
    // used const operator[] on keys the schema does not guarantee.
    json s;
    s["entities"] = json::array({
        line("e1", 0, 0, 80, 0),
        json{{"id", "a"}, {"type", "circle"}},                       // no center/radius
        json{{"id", "b"}, {"type", "arc"}, {"center", json::array({1})}},  // short array
        json{{"id", "c"}, {"type", "line"}},                         // no p1/p2
        json{{"id", "d"}, {"type", "spline"}},                       // no points
        json{{"id", "e"}, {"type", "ellipse"}, {"center", json::array({0, 0})}},
        json{{"id", "f"}},                                           // no type at all
    });
    s["constraints"] = json::array({
        rel("c1", "fix", {"e1.p1"}),
        dim("c2", "distance", {"e1.p1", "e1.p2"}, 80.0),
    });
    auto sol = solve_sketch(s, json::object());
    REQUIRE(sol.converged);
    CHECK(double(sol.entities["e1"]["p2"][0]) == doctest::Approx(80.0));
}

TEST_CASE("intersection drives a perturbed point onto both infinite curves") {
    json s{{"entities", json::array({point("e1", 1, 1), line("e2", -2, 3, 8, 3),
                                     line("e3", 4, -5, 4, 8)})},
           {"constraints", json::array({rel("c1", "fix", {"e2"}), rel("c2", "fix", {"e3"}),
                                         rel("c3", "intersection", {"e1", "e2", "e3"})})}};
    auto sol = solve_sketch(s, json::object());
    REQUIRE(sol.converged);
    CHECK(sol.skipped.empty());
    CHECK(double(sol.entities["e1"]["p"][0]) == doctest::Approx(4));
    CHECK(double(sol.entities["e1"]["p"][1]) == doctest::Approx(3));
}

TEST_CASE("arc midpoint follows the directed sweep across zero") {
    for (bool ccw : {true, false}) {
        json a = arc("e1", 2, 3, 5, 350, 10);
        a["ccw"] = ccw;
        json s{{"entities", json::array({a, point("e2", 3, 4)})},
               {"constraints", json::array({rel("c1", "fix", {"e1"}),
                                             rel("c2", "midpoint", {"e2", "e1"})})}};
        auto sol = solve_sketch(s, json::object());
        REQUIRE(sol.converged);
        CHECK(sol.skipped.empty());
        CHECK(double(sol.entities["e2"]["p"][0]) == doctest::Approx(ccw ? 7 : -3));
        CHECK(double(sol.entities["e2"]["p"][1]) == doctest::Approx(3));
    }
}

TEST_CASE("concentric resolves actual point handles rather than their entity center") {
    json s{{"entities", json::array({line("e1", -3, -2, 4, 6), circle("e2", 1, 1, 2)})},
           {"constraints", json::array({rel("c1", "fix", {"e1"}),
                                         rel("c2", "concentric", {"e2", "e1.p2"})})}};
    auto sol = solve_sketch(s, json::object());
    REQUIRE(sol.converged);
    CHECK(sol.skipped.empty());
    CHECK(double(sol.entities["e2"]["center"][0]) == doctest::Approx(4));
    CHECK(double(sol.entities["e2"]["center"][1]) == doctest::Approx(6));
}

TEST_CASE("equal ellipse family drives both semiaxes") {
    json a{{"id", "e1"}, {"type", "ellipse"}, {"center", {0, 0}}, {"rx", 5}, {"ry", 2}, {"rot", 20}};
    json b{{"id", "e2"}, {"type", "ellipse_arc"}, {"center", {8, 4}}, {"rx", 3}, {"ry", 4},
           {"rot", -10}, {"a0", 0}, {"a1", 150}};
    json s{{"entities", json::array({a, b})},
           {"constraints", json::array({rel("c1", "fix", {"e1"}), rel("c2", "equal", {"e1", "e2"})})}};
    auto sol = solve_sketch(s, json::object());
    REQUIRE(sol.converged);
    CHECK(sol.skipped.empty());
    CHECK(double(sol.entities["e2"]["rx"]) == doctest::Approx(5));
    CHECK(double(sol.entities["e2"]["ry"]) == doctest::Approx(2));
}

TEST_CASE("equal slots drive spine length and width") {
    json a{{"id", "e1"}, {"type", "slot"}, {"p1", {0, 0}}, {"p2", {8, 0}}, {"width", 2}};
    json b{{"id", "e2"}, {"type", "slot"}, {"p1", {0, 4}}, {"p2", {3, 4}}, {"width", 5}};
    json s{{"entities", json::array({a, b})},
           {"constraints", json::array({rel("c1", "fix", {"e1.p1"}), rel("c2", "fix", {"e1.p2"}),
                                         dim("c3", "diameter", {"e1"}, 2),
                                         rel("c4", "equal", {"e1", "e2"})})}};
    auto sol = solve_sketch(s, json::object());
    REQUIRE(sol.converged);
    CHECK(sol.skipped.empty());
    auto b0 = sol.entities["e2"];
    CHECK(std::hypot(double(b0["p2"][0]) - double(b0["p1"][0]),
                     double(b0["p2"][1]) - double(b0["p1"][1])) == doctest::Approx(8));
    CHECK(double(b0["width"]) == doctest::Approx(2));
}

TEST_CASE("equal arc slots drive centerline radius and width") {
    json a{{"id", "e1"}, {"type", "arc_slot"}, {"center", {0, 0}}, {"radius", 8},
           {"a0", 0}, {"a1", 90}, {"width", 2}};
    json b = a; b["id"] = "e2"; b["radius"] = 4; b["width"] = 6;
    json s{{"entities", json::array({a, b})},
           {"constraints", json::array({rel("c1", "equal", {"e1", "e2"})})}};
    auto sol = solve_sketch(s, json::object());
    REQUIRE(sol.converged);
    CHECK(sol.skipped.empty());
    CHECK(double(sol.entities["e1"]["radius"]) == doctest::Approx(double(sol.entities["e2"]["radius"])));
    CHECK(double(sol.entities["e1"]["width"]) == doctest::Approx(double(sol.entities["e2"]["width"])));
    CHECK(double(sol.entities["e1"]["radius"]) != doctest::Approx(8));
}

TEST_CASE("equal curve length equates a line and a trimmed arc") {
    const double pi = std::acos(-1.0);
    json s{{"entities", json::array({line("e1", 0, 0, pi * 2, 0), arc("e2", 0, 4, 4, 10, 80)})},
           {"constraints", json::array({rel("c1", "fix", {"e1"}), dim("c2", "radius", {"e2"}, 4),
                                         rel("c3", "equal_curve_length", {"e1", "e2"})})}};
    auto sol = solve_sketch(s, json::object());
    REQUIRE(sol.converged);
    CHECK(sol.skipped.empty());
    auto a = sol.entities["e2"];
    CHECK((double(a["end"]) - double(a["start"])) == doctest::Approx(90));
}

TEST_CASE("arc length dimension changes sweep and reports the solved measurement") {
    const double pi = std::acos(-1.0);
    json c = dim("c2", "arc_length", {"e1"}, 5 * pi);
    json s{{"entities", json::array({arc("e1", 2, 3, 5, 10, 65)})},
           {"constraints", json::array({dim("c1", "radius", {"e1"}, 5), c})}};
    auto sol = solve_sketch(s, json::object());
    REQUIRE(sol.converged);
    CHECK(sol.skipped.empty());
    CHECK(double(sol.entities["e1"]["end"]) - double(sol.entities["e1"]["start"]) == doctest::Approx(180));
    auto measured = chudware::sketch::measure_constraint(c, sol.entities);
    REQUIRE(measured.has_value());
    CHECK(*measured == doctest::Approx(5 * pi));
}

TEST_CASE("full ellipse tangency moves a perturbed horizontal line to its support") {
    json e{{"id", "e1"}, {"type", "ellipse"}, {"center", {0, 0}}, {"rx", 5}, {"ry", 2}, {"rot", 0}};
    json s{{"entities", json::array({e, line("e2", -4, 3, 4, 3)})},
           {"constraints", json::array({rel("c1", "fix", {"e1"}), rel("c2", "horizontal", {"e2"}),
                                         dim("c3", "length", {"e2"}, 8), rel("c4", "tangent", {"e1", "e2"})})}};
    auto sol = solve_sketch(s, json::object());
    REQUIRE(sol.converged);
    CHECK(sol.skipped.empty());
    CHECK(std::abs(double(sol.entities["e2"]["p1"][1])) == doctest::Approx(2));
    CHECK(double(sol.entities["e2"]["p1"][1]) == doctest::Approx(double(sol.entities["e2"]["p2"][1])));
}

TEST_CASE("spline endpoint tangent drives G1 without adding coincidence") {
    for (const char* type : {"spline", "style_spline"}) {
        json sp{{"id", "e1"}, {"type", type}, {"points", {{0, 4}, {1, 4.3}, {2, 5}, {3, 6}}}};
        json s{{"entities", json::array({sp, line("e2", -5, 0, 5, 0)})},
               {"constraints", json::array({rel("c1", "fix", {"e1.start"}), rel("c2", "fix", {"e2"}),
                                             rel("c3", "tangent", {"e1.p0", "e2"})})}};
        auto sol = solve_sketch(s, json::object());
        REQUIRE(sol.converged);
        CHECK(sol.skipped.empty());
        CHECK(double(sol.entities["e1"]["points"][1][1]) == doctest::Approx(4));
        CHECK(double(sol.entities["e1"]["points"][0][1]) == doctest::Approx(4));
    }
}

TEST_CASE("arc endpoint tangent uses its direction rather than a center distance extremum") {
    json s{{"entities", json::array({arc("e1", 0, 0, 3, 65, 180), line("e2", -4, 3, 4, 3)})},
           {"constraints", json::array({rel("c1", "fix", {"e1.center"}), dim("c2", "radius", {"e1"}, 3),
                                         rel("c3", "fix", {"e2"}), rel("c4", "tangent", {"e2", "e1.start"})})}};
    auto sol = solve_sketch(s, json::object());
    REQUIRE(sol.converged);
    CHECK(sol.skipped.empty());
    CHECK(double(sol.entities["e1"]["start"]) == doctest::Approx(90));
}

TEST_CASE("smooth spline to circle matches G1 and junction curvature") {
    json sp{{"id", "e1"}, {"type", "style_spline"},
            {"points", {{0, 0}, {1, .15}, {2, 1.0}, {3, 3}}}};
    json s{{"entities", json::array({sp, circle("e2", 0, 2, 2)})},
           {"constraints", json::array({rel("c1", "fix", {"e1.start"}), rel("c2", "fix", {"e1.end"}),
                                         rel("c3", "fix", {"e2"}), rel("c4", "smooth", {"e1.start", "e2"})})}};
    auto sol = solve_sketch(s, json::object());
    REQUIRE(sol.converged);
    CHECK(sol.skipped.empty());
    auto p = sol.entities["e1"]["points"];
    double dx = 3 * (double(p[1][0]) - double(p[0][0]));
    double dy = 3 * (double(p[1][1]) - double(p[0][1]));
    double ddx = 6 * (double(p[2][0]) - 2 * double(p[1][0]) + double(p[0][0]));
    double ddy = 6 * (double(p[2][1]) - 2 * double(p[1][1]) + double(p[0][1]));
    CHECK(std::abs(dy) < 1e-7);
    CHECK((dx * ddy - dy * ddx) / std::pow(std::hypot(dx, dy), 3) == doctest::Approx(.5));
}

TEST_CASE("smooth through spline to line removes junction curvature") {
    json sp{{"id", "e1"}, {"type", "spline"}, {"points", {{0, 0}, {1, .1}, {2, .4}, {4, 2}}}};
    json s{{"entities", json::array({sp, line("e2", -3, 0, 3, 0)})},
           {"constraints", json::array({rel("c1", "fix", {"e1.start"}), rel("c2", "fix", {"e2"}),
                                         rel("c3", "smooth", {"e1.start", "e2"})})}};
    auto sol = solve_sketch(s, json::object());
    REQUIRE(sol.converged);
    CHECK(sol.skipped.empty());
    auto p = sol.entities["e1"]["points"];
    CHECK(std::abs(double(p[1][1])) < 1e-7);
    CHECK(std::abs(-double(p[0][1]) + 2 * double(p[1][1]) - double(p[2][1])) < 1e-7);
}

TEST_CASE("point on through spline finds the actual curve") {
    json sp{{"id", "e1"}, {"type", "spline"}, {"points", {{0, 0}, {2, 2}, {4, 0}}}};
    json s{{"entities", json::array({sp, point("e2", 2, .7), line("e3", 2, -5, 2, 5)})},
           {"constraints", json::array({rel("c1", "fix", {"e1"}), rel("c2", "fix", {"e3"}),
                                         rel("c3", "point_on", {"e2", "e1"}),
                                         rel("c4", "point_on", {"e2", "e3"})})}};
    auto sol = solve_sketch(s, json::object());
    REQUIRE(sol.converged);
    CHECK(sol.skipped.empty());
    CHECK(double(sol.entities["e2"]["p"][0]) == doctest::Approx(2));
    CHECK(double(sol.entities["e2"]["p"][1]) == doctest::Approx(2));
}

TEST_CASE("point on style spline finds the Bezier curve rather than its control polygon") {
    json sp{{"id", "e1"}, {"type", "style_spline"}, {"points", {{0, 0}, {1, 2}, {3, 2}, {4, 0}}}};
    json s{{"entities", json::array({sp, point("e2", 2, .7), line("e3", 2, -5, 2, 5)})},
           {"constraints", json::array({rel("c1", "fix", {"e1"}), rel("c2", "fix", {"e3"}),
                                         rel("c3", "point_on", {"e2", "e1"}),
                                         rel("c4", "point_on", {"e2", "e3"})})}};
    auto sol = solve_sketch(s, json::object());
    REQUIRE(sol.converged);
    CHECK(sol.skipped.empty());
    CHECK(double(sol.entities["e2"]["p"][1]) == doctest::Approx(1.5));
}

TEST_CASE("symmetric lines pair endpoints by mirrored proximity") {
    json s{{"entities", json::array({line("e1", 2, 1, 4, 3), line("e2", -3.5, 2.5, -1.5, 1.3)})},
           {"constraints", json::array({rel("c1", "fix", {"e1"}),
                                         rel("c2", "symmetric", {"e1", "e2", "axis.v"})})}};
    auto sol = solve_sketch(s, json::object());
    REQUIRE(sol.converged);
    CHECK(sol.skipped.empty());
    CHECK(double(sol.entities["e2"]["p1"][0]) == doctest::Approx(-4));
    CHECK(double(sol.entities["e2"]["p1"][1]) == doctest::Approx(3));
    CHECK(double(sol.entities["e2"]["p2"][0]) == doctest::Approx(-2));
    CHECK(double(sol.entities["e2"]["p2"][1]) == doctest::Approx(1));
}

TEST_CASE("symmetric circles and arcs mirror centers and equate radii") {
    json s{{"entities", json::array({circle("e1", 3, 2, 4), arc("e2", -1, 4, 2, 20, 90),
                                     line("e3", 0, -4, 0, 8)})},
           {"constraints", json::array({rel("c1", "fix", {"e1"}), rel("c2", "fix", {"e3"}),
                                         rel("c3", "symmetric", {"e1", "e2", "e3"})})}};
    auto sol = solve_sketch(s, json::object());
    REQUIRE(sol.converged);
    CHECK(sol.skipped.empty());
    CHECK(double(sol.entities["e2"]["center"][0]) == doctest::Approx(-3));
    CHECK(double(sol.entities["e2"]["center"][1]) == doctest::Approx(2));
    CHECK(double(sol.entities["e2"]["radius"]) == doctest::Approx(4));
}

TEST_CASE("virtual vertical axis mirrors points without adding solver variables") {
    json s{{"entities", json::array({point("e1", 3, 2), point("e2", -1, 3)})},
           {"constraints", json::array({rel("c1", "fix", {"e1"}),
                                         rel("c2", "symmetric", {"e1", "e2", "axis.v"})})}};
    auto sol = solve_sketch(s, json::object());
    REQUIRE(sol.converged);
    CHECK(sol.skipped.empty());
    CHECK(sol.x.size() == 4);
    CHECK(sol.dof == 0);
    CHECK(double(sol.entities["e2"]["p"][0]) == doctest::Approx(-3));
    CHECK(double(sol.entities["e2"]["p"][1]) == doctest::Approx(2));
}

TEST_CASE("line line distance drives perpendicular spacing and measures it") {
    json c = dim("c4", "distance", {"e1", "e2"}, 6);
    json s{{"entities", json::array({line("e1", -5, 1, 5, 1), line("e2", -3, 3, 3, 4)})},
           {"constraints", json::array({rel("c1", "fix", {"e1"}), rel("c2", "parallel", {"e1", "e2"}),
                                         dim("c3", "length", {"e2"}, 6), c})}};
    auto sol = solve_sketch(s, json::object());
    REQUIRE(sol.converged);
    CHECK(sol.skipped.empty());
    CHECK(double(sol.entities["e2"]["p1"][1]) == doctest::Approx(7));
    CHECK(double(sol.entities["e2"]["p2"][1]) == doctest::Approx(7));
    auto measured = chudware::sketch::measure_constraint(c, sol.entities);
    REQUIRE(measured.has_value());
    CHECK(*measured == doctest::Approx(6));
}

TEST_CASE("virtual axes support direction angle collinearity and point relations") {
    SUBCASE("angle measurement") {
        json c = dim("c3", "angle", {"axis.h", "e1"}, 30);
        json s{{"entities", json::array({line("e1", 0, 0, 3, 2)})},
               {"constraints", json::array({rel("c1", "fix", {"e1.p1"}), dim("c2", "length", {"e1"}, 4), c})}};
        auto sol = solve_sketch(s, json::object());
        REQUIRE(sol.converged);
        CHECK(sol.skipped.empty());
        auto m = chudware::sketch::measure_constraint(c, sol.entities);
        REQUIRE(m.has_value());
        CHECK(*m == doctest::Approx(30));
    }
    for (const char* type : {"parallel", "perpendicular", "collinear"}) {
        json s{{"entities", json::array({line("e1", 1, 2, 4, 3)})},
               {"constraints", json::array({rel("c1", type, {"e1", "axis.h"}),
                                             dim("c2", "length", {"e1"}, 3)})}};
        auto sol = solve_sketch(s, json::object());
        REQUIRE(sol.converged);
        CHECK(sol.skipped.empty());
        auto e = sol.entities["e1"];
        if (std::string(type) == "perpendicular")
            CHECK(double(e["p1"][0]) == doctest::Approx(double(e["p2"][0])));
        else CHECK(double(e["p1"][1]) == doctest::Approx(double(e["p2"][1])));
        if (std::string(type) == "collinear") CHECK(std::abs(double(e["p1"][1])) < 1e-7);
    }
    SUBCASE("point on axis") {
        json s{{"entities", json::array({point("e1", 2, 3)})},
               {"constraints", json::array({rel("c1", "intersection", {"e1", "axis.h", "axis.v"})})}};
        auto sol = solve_sketch(s, json::object());
        REQUIRE(sol.converged);
        CHECK(sol.dof == 0);
        CHECK(sol.skipped.empty());
        CHECK(std::abs(double(sol.entities["e1"]["p"][0])) < 1e-7);
        CHECK(std::abs(double(sol.entities["e1"]["p"][1])) < 1e-7);
    }
}

TEST_CASE("wrong type and unresolved relations are reported without distorting geometry") {
    json s{{"entities", json::array({arc("e1", 2, 3, 4, 10, 80), line("e2", -2, 1, 5, 2)})},
           {"constraints", json::array({rel("c1", "parallel", {"e1", "e2"}),
                                         rel("c2", "perpendicular", {"e2", "e1"}),
                                         rel("c3", "midpoint", {"e2.p1", "e1.center"}),
                                         rel("c4", "concentric", {"e1", "e2"}),
                                         rel("c5", "tangent", {"e2", "e2"}),
                                         rel("c6", "coincident", {"e2.missing", "origin"}),
                                         rel("c7", "future_relation", {"e1"})})}};
    auto sol = solve_sketch(s, json::object());
    REQUIRE(sol.converged);
    REQUIRE(sol.skipped.size() == 7);
    for (size_t i = 0; i < sol.skipped.size(); ++i) {
        CHECK(sol.skipped[i].first == "c" + std::to_string(i + 1));
        CHECK_FALSE(sol.skipped[i].second.empty());
    }
    CHECK(double(sol.entities["e1"]["radius"]) == doctest::Approx(4));
    CHECK(double(sol.entities["e1"]["start"]) == doctest::Approx(10));
    CHECK(double(sol.entities["e2"]["p1"][0]) == doctest::Approx(-2));
    CHECK(double(sol.entities["e2"]["p2"][1]) == doctest::Approx(2));
    auto interactive = solve_sketch(s, json::object(), std::nullopt, std::nullopt, true);
    CHECK(interactive.skipped.size() == 7);
}

TEST_CASE("full ellipse curve tangency does not accept a crossing") {
    json ellipse{{"id", "e1"}, {"type", "ellipse"}, {"center", {0, 0}}, {"rx", 5}, {"ry", 2}, {"rot", 0}};
    for (bool nested : {false, true}) {
        json c = circle("e2", nested ? 1.0 : 5.5, 0, 1);
        json s{{"entities", json::array({ellipse, c})},
               {"constraints", json::array({rel("c1", "fix", {"e1"}), dim("c2", "radius", {"e2"}, 1),
                                             rel("c3", "horizontal", {"e1.center", "e2.center"}),
                                             rel("c4", "tangent", {"e1", "e2"})})}};
        auto sol = solve_sketch(s, json::object());
        REQUIRE(sol.converged);
        CHECK(sol.skipped.empty());
        // Nested contact can occur away from the ellipse's major-axis tip; prove
        // the support inequality has a zero maximum, not merely an intersection.
        double cx = sol.entities["e2"]["center"][0], best = -1e9;
        for (int i = 0; i < 20000; ++i) {
            double a = 2 * std::acos(-1.0) * i / 20000;
            double support = std::hypot(5 * std::cos(a), 2 * std::sin(a));
            best = std::max(best, cx * std::cos(a) + (nested ? 1 : -1) - support);
        }
        CHECK(std::abs(best) < 1e-6);
    }
}

TEST_CASE("free variables enumerate isolated unconstrained geometry") {
    json spline{{"id", "e3"}, {"type", "spline"}, {"points", {{0, 0}, {1, 2}, {3, 1}}}};
    json s{{"entities", json::array({circle("e1", 2, 4, 3), line("e2", 0, 0, 3, 2), spline})},
           {"constraints", json::array()}};
    auto sol = solve_sketch(s, json::object());
    REQUIRE(sol.converged);
    REQUIRE(sol.dof == 13);
    auto cold = chudware::sketch::free_variables(s, json::object());
    auto reused = chudware::sketch::free_variables(s, json::object(), sol.x);
    REQUIRE(cold.size() == 13);
    REQUIRE(reused.size() == cold.size());
    for (size_t i = 0; i < cold.size(); ++i) {
        CHECK(cold[i].var == static_cast<int>(i));
        CHECK(reused[i].var == cold[i].var);
        CHECK(reused[i].entity == cold[i].entity);
        CHECK(reused[i].param == cold[i].param);
    }
    CHECK(cold[0].entity == "e1");
    CHECK(cold[2].param == "radius");
    CHECK(cold[3].entity == "e2");
    CHECK(cold[3].param == "p1 x");
    CHECK(cold[7].entity == "e3");
    CHECK(cold[12].param == "point 2 y");
}

TEST_CASE("independent fixed conversions preserve exact solve layout warm starts and diagnostics") {
    json s{{"entities", json::array({line("e1", -5, 0, 5, 0), circle("e2", 20, 5, 2),
                                     line("e3", -3, 2, 3, 3)})},
           {"constraints", json::array({rel("c1", "fix", {"e1"}), rel("c2", "fix", {"e2"}),
                                         rel("c3", "parallel", {"e1", "e3"}),
                                         dim("c4", "distance", {"e1", "e3"}, 5),
                                         dim("c5", "length", {"e3"}, 6)})}};
    for (int i = 4; i <= 12; ++i) {
        std::string entity = "e" + std::to_string(i), constraint = "c" + std::to_string(i + 2);
        s["entities"].push_back(circle(entity.c_str(), 10 * i, 2, 1 + i));
        s["constraints"].push_back(rel(constraint.c_str(), "fix", {entity.c_str()}));
    }
    s["constraints"].push_back(rel("c15", "fix", {"e12"}));
    s["constraints"].push_back(rel("c16", "fix", {"e2"}));
    auto sol = solve_sketch(s, json::object());
    REQUIRE(sol.converged);
    CHECK(sol.skipped.empty());
    CHECK(sol.dof == 1);
    CHECK(sol.var_map.at("e3") == std::vector<int>({7, 8, 9, 10}));
    CHECK(sol.x(4) == doctest::Approx(20));
    CHECK(sol.x(8) == doctest::Approx(5));
    CHECK(sol.x(10) == doctest::Approx(5));
    REQUIRE(sol.redundant.size() == 2);
    CHECK(sol.redundant[0] == "c15");
    CHECK(sol.redundant[1] == "c16");
    for (const auto& e : s["entities"])
        if (e["id"] != "e3") CHECK(sol.statuses.at(e["id"].get<std::string>()) == "full");
    auto free = chudware::sketch::free_variables(s, json::object(), sol.x);
    REQUIRE(free.size() == 2);
    CHECK(free[0].entity == "e3");
    CHECK(free[1].entity == "e3");
    s["entities"][1]["center"] = {31, 7};
    auto warm = solve_sketch(s, json::object(), sol.state);
    REQUIRE(warm.converged);
    CHECK(warm.var_map == sol.var_map);
    CHECK(warm.x(4) == doctest::Approx(31));
    CHECK(warm.x(5) == doctest::Approx(7));
    CHECK(warm.x(8) == doctest::Approx(5));
}

TEST_CASE("coincident slot junction tangents fully define the profile from a perturbed seed") {
    json right = arc("e2", 5.1, .1, 2.1, 85, 275);
    json left = arc("e4", -5.1, -.1, 1.9, 265, 95);
    right["ccw"] = false; left["ccw"] = false;
    json s{{"entities", json::array({line("e1", -5.2, 2.2, 4.9, 1.9), right,
                                     line("e3", 5.2, -1.8, -4.8, -2.2), left})},
           {"constraints", json::array({
               rel("c1", "coincident", {"e1.p2", "e2.start"}),
               rel("c2", "coincident", {"e2.end", "e3.p1"}),
               rel("c3", "coincident", {"e3.p2", "e4.start"}),
               rel("c4", "coincident", {"e4.end", "e1.p1"}),
               rel("c5", "tangent", {"e1", "e2"}), rel("c6", "tangent", {"e2", "e3"}),
               rel("c7", "tangent", {"e3", "e4"}), rel("c8", "tangent", {"e4", "e1"}),
               rel("c9", "horizontal", {"e1"}), dim("c10", "radius", {"e2"}, 2),
               rel("c11", "symmetric", {"e2.center", "e4.center", "axis.v"}),
               rel("c12", "point_on", {"e2.center", "axis.h"}),
               dim("c13", "distance", {"e2.center", "e4.center"}, 10)})}};
    auto sol = solve_sketch(s, json::object());
    REQUIRE(sol.converged);
    CHECK(sol.skipped.empty());
    CHECK(sol.redundant.empty());
    CHECK(sol.dof == 0);
    for (const char* id : {"e1", "e2", "e3", "e4"}) CHECK(sol.statuses.at(id) == "full");
    CHECK(double(sol.entities["e1"]["p1"][0]) == doctest::Approx(-5));
    CHECK(double(sol.entities["e1"]["p2"][0]) == doctest::Approx(5));
    CHECK(double(sol.entities["e1"]["p1"][1]) == doctest::Approx(2));
    CHECK(double(sol.entities["e3"]["p1"][1]) == doctest::Approx(-2));
    CHECK(double(sol.entities["e4"]["radius"]) == doctest::Approx(2));
    CHECK(double(sol.entities["e2"]["start"]) == doctest::Approx(90));
    CHECK(double(sol.entities["e2"]["end"]) == doctest::Approx(270));
    CHECK(chudware::sketch::free_variables(s, json::object(), sol.x).empty());
    auto interactive = solve_sketch(s, json::object(), std::nullopt, std::nullopt, true);
    REQUIRE(interactive.converged);
    CHECK(interactive.dof == 0);
    s["constraints"].push_back(dim("c14", "radius", {"e4"}, 2));
    auto redundant = solve_sketch(s, json::object());
    REQUIRE(redundant.converged);
    CHECK(redundant.dof == 0);
    CHECK(has(redundant.redundant, "c14"));
}

TEST_CASE("arc junction tangent uses endpoint directions for external and internal contact") {
    for (bool internal : {false, true}) {
        json fixed = internal ? arc("e1", 0, 0, 5, -90, 0) : arc("e1", 0, 0, 3, 0, 90);
        json moving = internal ? arc("e2", 3.1, .1, 2.1, 5, 95) : arc("e2", .2, 5.1, 2.2, 260, 185);
        moving["ccw"] = false;
        json s{{"entities", json::array({fixed, moving})},
               {"constraints", json::array({
                   rel("c1", "fix", {"e1"}), dim("c2", "radius", {"e2"}, 2),
                   rel("c3", "coincident", {"e1.end", "e2.start"}),
                   rel("c4", "tangent", {"e1", "e2"}),
                   rel("c5", internal ? "vertical" : "horizontal", {"e2.end", "e2.center"})})}};
        SUBCASE("direct endpoint connection") {}
        SUBCASE("transitive connection through a point") {
            s["entities"].push_back(point("e3", internal ? 5.1 : .1, internal ? .1 : 3.1));
            s["constraints"][2] = rel("c3", "coincident", {"e1.end", "e3.p"});
            s["constraints"].push_back(rel("c6", "coincident", {"e3", "e2.start"}));
        }
        auto sol = solve_sketch(s, json::object());
        REQUIRE(sol.converged);
        CHECK(sol.skipped.empty());
        CHECK(sol.redundant.empty());
        CHECK(sol.dof == 0);
        CHECK(sol.statuses.at("e2") == "full");
        CHECK(double(sol.entities["e2"]["center"][0]) == doctest::Approx(internal ? 3 : 0));
        CHECK(double(sol.entities["e2"]["center"][1]) == doctest::Approx(internal ? 0 : 5));
        CHECK(double(sol.entities["e2"]["start"]) == doctest::Approx(internal ? 0 : 270));
        CHECK(double(sol.entities["e2"]["end"]) == doctest::Approx(internal ? 90 : 180));
    }
}

TEST_CASE("joined bare spline tangent uses the shared endpoint instead of an extremum gap") {
    json spline{{"id", "e1"}, {"type", "style_spline"},
                {"points", {{.1, .1}, {1, .4}, {2, 1}, {3, 1}}}};
    json s{{"entities", json::array({spline, line("e2", -2, 0, 0, 0)})},
           {"constraints", json::array({rel("c1", "fix", {"e2"}), rel("c2", "fix", {"e1.end"}),
                                         rel("c3", "coincident", {"e1.p0", "e2.end"}),
                                         rel("c4", "tangent", {"e2", "e1"})})}};
    auto sol = solve_sketch(s, json::object());
    REQUIRE(sol.converged);
    CHECK(sol.skipped.empty());
    CHECK(sol.redundant.empty());
    CHECK(sol.dof == 3);
    CHECK(std::abs(double(sol.entities["e1"]["points"][0][1])) < 1e-7);
    CHECK(std::abs(double(sol.entities["e1"]["points"][1][1])) < 1e-7);
}

TEST_CASE("ellipse arc junction tangent evaluates its authored endpoint parameter") {
    json ellipse{{"id", "e1"}, {"type", "ellipse_arc"}, {"center", {0, 0}},
                 {"rx", 3}, {"ry", 2}, {"rot", 5}, {"a0", 85}, {"a1", 180}};
    json s{{"entities", json::array({ellipse, line("e2", -2, 2, 0, 2)})},
           {"constraints", json::array({rel("c1", "fix", {"e2"}), rel("c2", "fix", {"e1.center"}),
                                         rel("c3", "coincident", {"e1.start", "e2.p2"}),
                                         rel("c4", "tangent", {"e1", "e2"})})}};
    auto sol = solve_sketch(s, json::object());
    REQUIRE(sol.converged);
    CHECK(sol.skipped.empty());
    CHECK(sol.redundant.empty());
    CHECK(sol.dof == 2);
    auto e = sol.entities["e1"];
    double a = double(e["a0"]) * std::acos(-1.0) / 180;
    double rot = double(e["rot"]) * std::acos(-1.0) / 180;
    double dy = -std::sin(rot) * double(e["rx"]) * std::sin(a) +
                 std::cos(rot) * double(e["ry"]) * std::cos(a);
    CHECK(std::abs(dy) < 1e-7);
}

TEST_CASE("nonjoined line circle tangent retains the signed distance formulation") {
    json s{{"entities", json::array({line("e1", -10, 0, 10, 0), circle("e2", 0, 4, 2)})},
           {"constraints", json::array({rel("c1", "fix", {"e1"}), dim("c2", "radius", {"e2"}, 2),
                                         rel("c3", "tangent", {"e1", "e2"})})}};
    auto sol = solve_sketch(s, json::object());
    REQUIRE(sol.converged);
    CHECK(sol.skipped.empty());
    CHECK(sol.redundant.empty());
    CHECK(sol.dof == 1);
    CHECK(double(sol.entities["e2"]["center"][1]) == doctest::Approx(2));
}

TEST_CASE("solved spline handle measurements cover endpoint aliases and interior points safely") {
    for (const char* type : {"spline", "style_spline"}) {
        json s{{"entities", json::array({json{{"id", "e1"}, {"type", type},
                                             {"points", {{3, 4}, {6, 8}, {9, 12}}}}})},
               {"constraints", json::array({rel("c1", "fix", {"e1"})})}};
        auto sol = solve_sketch(s, json::object());
        REQUIRE(sol.converged);
        for (const auto& handle : {"e1.start", "e1.p0", "e1.p1", "e1.end", "e1.p2"}) {
            json c = dim("c2", "distance", {handle, "origin"}, 0);
            auto measurement = chudware::sketch::measure_constraint(c, sol.entities);
            REQUIRE(measurement.has_value());
            double expected = std::string(handle) == "e1.p1" ? 10 :
                              (std::string(handle) == "e1.end" || std::string(handle) == "e1.p2" ? 15 : 5);
            CHECK(*measurement == doctest::Approx(expected));
        }
        for (const auto& handle : {"e1.p3", "e1.p-1", "e1.p1junk", "e1.p99999999999999999999999"}) {
            json c = dim("c2", "distance", {handle, "origin"}, 0);
            CHECK_FALSE(chudware::sketch::measure_constraint(c, sol.entities).has_value());
        }
    }
}

TEST_CASE("collinear projects a free line onto a fixed reference in either order without collapse") {
    for (bool reverse : {false, true}) {
        json s{{"entities", json::array({line("e1", 10, 46, 60, 47), line("e5", 100, 40, 0, 40)})},
               {"constraints", json::array({rel("c1", "fix", {"e5"}),
                   reverse ? rel("c2", "collinear", {"e5", "e1"}) :
                             rel("c2", "collinear", {"e1", "e5"})})}};
        SUBCASE("whole reference fixed") {}
        SUBCASE("both reference endpoints fixed separately") {
            s["constraints"][0] = rel("c1", "fix", {"e5.p1"});
            s["constraints"].push_back(rel("c3", "fix", {"e5.p2"}));
        }
        auto sol = solve_sketch(s, json::object());
        REQUIRE(sol.converged);
        CHECK(sol.skipped.empty());
        CHECK(sol.conflicting.empty());
        CHECK(sol.redundant.empty());
        CHECK(sol.dof == 2);
        const auto& e = sol.entities["e1"];
        CHECK(double(e["p1"][0]) == doctest::Approx(10));
        CHECK(double(e["p2"][0]) == doctest::Approx(60));
        CHECK(double(e["p1"][1]) == doctest::Approx(40));
        CHECK(double(e["p2"][1]) == doctest::Approx(40));
        double length = std::hypot(double(e["p2"][0]) - double(e["p1"][0]),
                                   double(e["p2"][1]) - double(e["p1"][1]));
        // Projection preserves the 50 mm span; the seed slope changes Euclidean
        // length by 0.02%, not by collapsing the free line.
        CHECK(length == doctest::Approx(std::hypot(50, 1)).epsilon(.00021));
        CHECK(sol.statuses.at("e1") == "under");
        CHECK(sol.statuses.at("e5") == "full");
    }
}

TEST_CASE("collinear with a virtual horizontal axis preserves the free line span in either order") {
    for (bool reverse : {false, true}) {
        json s{{"entities", json::array({line("e1", 10, 6, 60, 7)})},
               {"constraints", json::array({reverse ? rel("c1", "collinear", {"axis.h", "e1"}) :
                                                        rel("c1", "collinear", {"e1", "axis.h"})})}};
        auto sol = solve_sketch(s, json::object());
        REQUIRE(sol.converged);
        CHECK(sol.skipped.empty());
        CHECK(sol.redundant.empty());
        CHECK(sol.dof == 2);
        const auto& e = sol.entities["e1"];
        CHECK(double(e["p1"][0]) == doctest::Approx(10));
        CHECK(double(e["p2"][0]) == doctest::Approx(60));
        CHECK(std::abs(double(e["p1"][1])) < 1e-7);
        CHECK(std::abs(double(e["p2"][1])) < 1e-7);
        CHECK(double(e["p2"][0]) - double(e["p1"][0]) == doctest::Approx(50));
    }
}

TEST_CASE("direction relations rotate unconstrained lines instead of shortening them") {
    for (const char* type : {"parallel", "perpendicular"}) {
        const bool parallel = std::string(type) == "parallel";
        for (bool virtual_axis : {false, true}) {
            json moving = parallel ? line("e1", 0, 5, 0, 15) : line("e1", 0, 5, 10, 5);
            json s{{"entities", json::array({moving})}, {"constraints", json::array()}};
            if (!virtual_axis) {
                s["entities"].push_back(line("e2", 0, 0, 10, 0));
                s["constraints"].push_back(rel("c1", "fix", {"e2"}));
            }
            s["constraints"].push_back(virtual_axis ? rel("c2", type, {"e1", "axis.h"}) :
                                                      rel("c2", type, {"e1", "e2"}));
            auto sol = solve_sketch(s, json::object());
            REQUIRE(sol.converged);
            CHECK(sol.skipped.empty());
            CHECK(sol.redundant.empty());
            CHECK(sol.dof == 3);
            const auto& e = sol.entities["e1"];
            double dx = double(e["p2"][0]) - double(e["p1"][0]);
            double dy = double(e["p2"][1]) - double(e["p1"][1]);
            CHECK(std::hypot(dx, dy) >= 9.99);
            if (parallel) CHECK(std::abs(dy) < 1e-7);
            else CHECK(std::abs(dx) < 1e-7);
        }
    }
}

TEST_CASE("point on a movable line removes distance error rather than line length") {
    json s{{"entities", json::array({point("e1", 0, 5), line("e2", 1, 0, 10, 0)})},
           {"constraints", json::array({rel("c1", "fix", {"e1"}), rel("c2", "point_on", {"e1", "e2"})})}};
    auto sol = solve_sketch(s, json::object());
    REQUIRE(sol.converged);
    CHECK(sol.skipped.empty());
    CHECK(sol.dof == 3);
    const auto& e = sol.entities["e2"];
    double ax = e["p1"][0], ay = e["p1"][1], dx = double(e["p2"][0]) - ax, dy = double(e["p2"][1]) - ay;
    CHECK(std::hypot(dx, dy) >= 8.99);
    CHECK(std::abs(dx * (5 - ay) + dy * ax) / std::hypot(dx, dy) < 1e-7);
}

TEST_CASE("point symmetry about a movable line normalizes the axis direction") {
    json s{{"entities", json::array({point("e1", -3, 1), point("e2", 3, 1), line("e3", 4, -5, 5, 8)})},
           {"constraints", json::array({rel("c1", "fix", {"e1"}), rel("c2", "fix", {"e2"}),
                                         rel("c3", "symmetric", {"e1.p", "e2.p", "e3"})})}};
    auto sol = solve_sketch(s, json::object());
    REQUIRE(sol.converged);
    CHECK(sol.skipped.empty());
    CHECK(sol.redundant.empty());
    CHECK(sol.dof == 2);
    const auto& e = sol.entities["e3"];
    CHECK(std::abs(double(e["p1"][0])) < 1e-7);
    CHECK(std::abs(double(e["p2"][0])) < 1e-7);
    CHECK(std::hypot(double(e["p2"][0]) - double(e["p1"][0]),
                     double(e["p2"][1]) - double(e["p1"][1])) >= 13);
}

TEST_CASE("midpoint translates a free line without changing its direction or length") {
    json s{{"entities", json::array({point("e1", 20, 30), line("e2", 1, 0, 11, 4)})},
           {"constraints", json::array({rel("c1", "fix", {"e1"}), rel("c2", "midpoint", {"e1", "e2"})})}};
    auto sol = solve_sketch(s, json::object());
    REQUIRE(sol.converged);
    CHECK(sol.skipped.empty());
    CHECK(sol.dof == 2);
    const auto& e = sol.entities["e2"];
    CHECK((double(e["p1"][0]) + double(e["p2"][0])) * .5 == doctest::Approx(20));
    CHECK((double(e["p1"][1]) + double(e["p2"][1])) * .5 == doctest::Approx(30));
    CHECK(double(e["p2"][0]) - double(e["p1"][0]) == doctest::Approx(10));
    CHECK(double(e["p2"][1]) - double(e["p1"][1]) == doctest::Approx(4));
}
