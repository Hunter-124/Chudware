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
