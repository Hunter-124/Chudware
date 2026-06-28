// FreeCAD planegcs parity tests — constraint behaviours that FreeCAD's GCS test
// suite explicitly validates, plus the contract drag handles (circle/arc rim ->
// radius, arc start/end angle) proven core-side. These harden the edge cases
// known to trip up 2D constraint solvers:
//   * the unsigned angle constraint at 0 deg (parallel) and 180 deg (antiparallel),
//     where cross -> 0 and the Jacobian denominator collapses to dot^2;
//   * derived-handle constraints (coincident on an arc endpoint);
//   * a long collinear chain (the projected-LM path through near-singular Jacobians);
//   * point_on a full circle/ellipse (trim-agnostic, matching the Python oracle);
//   * the contract drag handles: the host pre-computes the target scalar (rim ->
//     radius mm, start/end_angle -> degrees) into xy[0], so each maps to ONE solver
//     variable and only that DOF moves -- the centre never drifts.
// Behavioural gates (positions / on-curve membership), not Python-parity coordinates,
// because the soft drag path is wall-clock-budgeted. Link: aicad_sketch.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include "sketch/sketch.h"

#include <cmath>
#include <string>
#include <vector>

using aicad::sketch::solve_sketch;
using aicad::sketch::drag;
using aicad::sketch::SketchSolution;
using json = nlohmann::json;

namespace {

constexpr double kPi = 3.14159265358979323846;
constexpr double kDeg = kPi / 180.0;

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
json ellipse(const char* id, double cx, double cy, double rx, double ry, double rot) {
    return json{{"id", id}, {"type", "ellipse"}, {"construction", false},
                {"center", json::array({cx, cy})}, {"rx", rx}, {"ry", ry}, {"rot", rot}};
}
json rel(const char* id, const char* type, std::vector<std::string> refs) {
    json r = json::array();
    for (auto& s : refs) r.push_back(s);
    return json{{"id", id}, {"type", type}, {"refs", r}};
}
json dim(const char* id, const char* type, std::vector<std::string> refs, double v,
         const char* expr = nullptr) {
    json r = json::array();
    for (auto& s : refs) r.push_back(s);
    json val = json{{"value", v}, {"expr", expr ? json(expr) : json(nullptr)}};
    return json{{"id", id}, {"type", type}, {"refs", r}, {"value", val}, {"driven", false}};
}

// Direction vector (p2 - p1) of a solved line entity.
std::pair<double, double> dir_of(const json& ents, const std::string& id) {
    const json& e = ents.at(id);
    return {double(e.at("p2")[0]) - double(e.at("p1")[0]),
            double(e.at("p2")[1]) - double(e.at("p1")[1])};
}
double cross(double ux, double uy, double vx, double vy) { return ux * vy - uy * vx; }
double dot(double ux, double uy, double vx, double vy) { return ux * vx + uy * vy; }

// World position of an arc endpoint from its solved (center, radius, angle).
std::pair<double, double> arc_endpoint(const json& a, bool start) {
    double cx = a.at("center")[0], cy = a.at("center")[1], r = a.at("radius");
    double ang = double(a.at(start ? "start" : "end")) * kDeg;
    return {cx + r * std::cos(ang), cy + r * std::sin(ang)};
}

}  // namespace

// --- angle constraint at the parallel / antiparallel folds -------------------
// planegcs validates angle=0 and angle=180 explicitly: at both, |cross| -> 0 so the
// Jacobian denom = dot^2 + |cross|^2 collapses to dot^2 (still nonzero). atan2(0, +) = 0
// and atan2(0, -) = pi pick the two folds apart.

TEST_CASE("parity: angle 0 deg makes two lines parallel (same direction)") {
    json s;
    s["entities"] = json::array({line("e1", 0, 0, 10, 0), line("e2", 0, 5, 8, 6)});
    s["constraints"] = json::array({
        rel("c1", "fix", {"e1.p1"}), rel("c2", "fix", {"e1.p2"}),
        rel("c3", "fix", {"e2.p1"}),
        dim("c4", "distance", {"e2.p1", "e2.p2"}, 10.0),
        dim("c5", "angle", {"e1", "e2"}, 0.0),
    });
    auto sol = solve_sketch(s, json::object());
    REQUIRE(sol.converged);
    CHECK(sol.dof == 0);
    auto [vx, vy] = dir_of(sol.entities, "e2");
    CHECK(std::abs(cross(10.0, 0.0, vx, vy)) < 1e-4);  // parallel
    CHECK(dot(10.0, 0.0, vx, vy) > 0.0);               // not antiparallel
}

TEST_CASE("parity: angle 90 deg makes two lines perpendicular") {
    json s;
    s["entities"] = json::array({line("e1", 0, 0, 10, 0), line("e2", 0, 5, 1, 13)});
    s["constraints"] = json::array({
        rel("c1", "fix", {"e1.p1"}), rel("c2", "fix", {"e1.p2"}),
        rel("c3", "fix", {"e2.p1"}),
        dim("c4", "distance", {"e2.p1", "e2.p2"}, 10.0),
        dim("c5", "angle", {"e1", "e2"}, 90.0),
    });
    auto sol = solve_sketch(s, json::object());
    REQUIRE(sol.converged);
    CHECK(sol.dof == 0);
    auto [vx, vy] = dir_of(sol.entities, "e2");
    CHECK(std::abs(dot(10.0, 0.0, vx, vy)) < 1e-4);  // perpendicular
}

TEST_CASE("parity: angle 180 deg makes two lines antiparallel") {
    json s;
    s["entities"] = json::array({line("e1", 0, 0, 10, 0), line("e2", 0, 5, -8, 6)});
    s["constraints"] = json::array({
        rel("c1", "fix", {"e1.p1"}), rel("c2", "fix", {"e1.p2"}),
        rel("c3", "fix", {"e2.p1"}),
        dim("c4", "distance", {"e2.p1", "e2.p2"}, 10.0),
        dim("c5", "angle", {"e1", "e2"}, 180.0),
    });
    auto sol = solve_sketch(s, json::object());
    REQUIRE(sol.converged);
    CHECK(sol.dof == 0);
    auto [vx, vy] = dir_of(sol.entities, "e2");
    CHECK(std::abs(cross(10.0, 0.0, vx, vy)) < 1e-4);  // collinear
    CHECK(dot(10.0, 0.0, vx, vy) < 0.0);               // opposite direction
}

// --- relation constraints ----------------------------------------------------

TEST_CASE("parity: parallel relation aligns two line directions") {
    json s;
    s["entities"] = json::array({line("e1", 0, 0, 10, 0), line("e2", 0, 5, 8, 2)});
    s["constraints"] = json::array({
        rel("c1", "fix", {"e1.p1"}), rel("c2", "fix", {"e1.p2"}),
        rel("c3", "fix", {"e2.p1"}),
        dim("c4", "distance", {"e2.p1", "e2.p2"}, 10.0),
        rel("c5", "parallel", {"e1", "e2"}),
    });
    auto sol = solve_sketch(s, json::object());
    REQUIRE(sol.converged);
    auto [vx, vy] = dir_of(sol.entities, "e2");
    CHECK(std::abs(cross(10.0, 0.0, vx, vy)) < 1e-4);
}

TEST_CASE("parity: perpendicular relation drives the direction dot to zero") {
    json s;
    s["entities"] = json::array({line("e1", 0, 0, 10, 0), line("e2", 0, 0, 7, 7)});
    s["constraints"] = json::array({
        rel("c1", "fix", {"e1.p1"}), rel("c2", "fix", {"e1.p2"}),
        rel("c3", "fix", {"e2.p1"}),
        dim("c4", "distance", {"e2.p1", "e2.p2"}, 10.0),
        rel("c5", "perpendicular", {"e1", "e2"}),
    });
    auto sol = solve_sketch(s, json::object());
    REQUIRE(sol.converged);
    auto [vx, vy] = dir_of(sol.entities, "e2");
    CHECK(std::abs(dot(10.0, 0.0, vx, vy)) < 1e-4);
}

TEST_CASE("parity: equal + perpendicular forms an L of equal arms") {
    json s;
    s["entities"] = json::array({line("e1", 0, 0, 10, 0), line("e2", 0, 0, 7, 7)});
    s["constraints"] = json::array({
        rel("c1", "fix", {"e1.p1"}), rel("c2", "fix", {"e1.p2"}),
        rel("c3", "fix", {"e2.p1"}),
        rel("c4", "equal", {"e1", "e2"}),
        rel("c5", "perpendicular", {"e1", "e2"}),
    });
    auto sol = solve_sketch(s, json::object());
    REQUIRE(sol.converged);
    auto [vx, vy] = dir_of(sol.entities, "e2");
    CHECK(std::hypot(vx, vy) == doctest::Approx(10.0).epsilon(1e-4));  // equal length
    CHECK(std::abs(dot(10.0, 0.0, vx, vy)) < 1e-4);                    // perpendicular
}

// --- derived-handle in a constraint (arc endpoint) ---------------------------
// The arc start handle exposes [cx, cy, r, a0] with chain-rule Jacobians; coincident
// on it must pull the endpoint (not the centre) onto the fixed point.

TEST_CASE("parity: coincident pulls an arc start endpoint onto a fixed point") {
    json s;
    s["entities"] = json::array({arc("a1", 0, 0, 10, 0, 90), point("p", 0, 10)});
    s["constraints"] = json::array({
        rel("c1", "fix", {"a1.center"}),
        dim("c2", "radius", {"a1"}, 10.0),
        rel("c3", "fix", {"p"}),
        rel("c4", "coincident", {"a1.start", "p"}),
    });
    auto sol = solve_sketch(s, json::object());
    REQUIRE(sol.converged);
    auto [sx, sy] = arc_endpoint(sol.entities["a1"], /*start=*/true);
    CHECK(std::hypot(sx - 0.0, sy - 10.0) < 1e-4);  // start endpoint hits the point
    CHECK(std::hypot(double(sol.entities["a1"]["center"][0]),
                     double(sol.entities["a1"]["center"][1])) < 1e-5);  // centre fixed
}

// --- long collinear chain (projected LM through near-singular Jacobians) ------

TEST_CASE("parity: 8-segment collinear chain is fully defined ending at (80,0)") {
    json ents = json::array();
    json cons = json::array();
    for (int i = 0; i < 8; ++i) {
        std::string id = "e" + std::to_string(i + 1);
        ents.push_back(line(id.c_str(), i * 10.0, 0.0, (i + 1) * 10.0, 0.0));
    }
    cons.push_back(rel("fix", "fix", {"e1.p1"}));
    for (int i = 1; i <= 8; ++i) {
        std::string id = "e" + std::to_string(i);
        std::string hid = "h" + std::to_string(i), did = "d" + std::to_string(i);
        cons.push_back(rel(hid.c_str(), "horizontal", {id}));
        cons.push_back(dim(did.c_str(), "distance", {id + ".p1", id + ".p2"}, 10.0));
    }
    for (int i = 1; i <= 7; ++i) {
        std::string cid = "c" + std::to_string(i);
        cons.push_back(rel(cid.c_str(), "coincident",
                           {"e" + std::to_string(i) + ".p2",
                            "e" + std::to_string(i + 1) + ".p1"}));
    }
    json s;
    s["entities"] = ents;
    s["constraints"] = cons;
    auto sol = solve_sketch(s, json::object());
    REQUIRE(sol.converged);
    CHECK(sol.dof == 0);
    CHECK(sol.conflicting.empty());
    CHECK(double(sol.entities["e8"]["p2"][0]) == doctest::Approx(80.0).epsilon(1e-6));
    CHECK(std::abs(double(sol.entities["e8"]["p2"][1])) < 1e-4);
}

// --- point_on is trim-agnostic on circle/arc and supports the plain ellipse ----

TEST_CASE("parity: point_on an arc places the point on the FULL circle (trim ignored)") {
    json s;
    // Arc swept [0,90] (first quadrant); the point starts in the third quadrant, whose
    // nearest circle point is OUTSIDE the sweep — point_on still lands it on the circle.
    s["entities"] = json::array({arc("a1", 0, 0, 10, 0, 90), point("p", -15, -2)});
    s["constraints"] = json::array({
        rel("c1", "fix", {"a1.center"}),
        dim("c2", "radius", {"a1"}, 10.0),
        rel("c3", "point_on", {"p", "a1"}),
    });
    auto sol = solve_sketch(s, json::object());
    REQUIRE(sol.converged);
    double px = sol.entities["p"]["p"][0], py = sol.entities["p"]["p"][1];
    CHECK(std::hypot(px, py) == doctest::Approx(10.0).epsilon(1e-5));  // on the circle
    CHECK(px < 0.0);  // landed outside the [0,90] sweep -> trim ignored
}

TEST_CASE("parity: point_on a plain ellipse satisfies the ellipse equation") {
    json s;
    s["entities"] = json::array({ellipse("e1", 0, 0, 8, 5, 30), point("p", 12, 3)});
    s["constraints"] = json::array({
        rel("c1", "fix", {"e1.center"}),  // pin centre; rx/ry/rot stay free
        rel("c2", "point_on", {"p", "e1"}),
    });
    auto sol = solve_sketch(s, json::object());
    REQUIRE(sol.converged);
    // The solved point must lie on the solved ellipse: rad == 1 in the rotated frame.
    const json& e = sol.entities["e1"];
    double cx = e["center"][0], cy = e["center"][1];
    double rx = e["rx"], ry = e["ry"], rot = double(e["rot"]) * kDeg;
    double dxp = double(sol.entities["p"]["p"][0]) - cx;
    double dyp = double(sol.entities["p"]["p"][1]) - cy;
    double qx = std::cos(rot) * dxp + std::sin(rot) * dyp;
    double qy = -std::sin(rot) * dxp + std::cos(rot) * dyp;
    double rad = std::hypot(qx / rx, qy / ry);
    CHECK(rad == doctest::Approx(1.0).epsilon(1e-3));
}

// --- contract drag handles: rim radius + arc endpoint angle (1-var scalar) ----
// The host (sketch_mode.cpp) pre-computes the target SCALAR into the drag op's xy[0]:
// rim -> radius = |cursor - centre| (mm); start/end_angle -> the endpoint angle (deg).
// resolve_handle maps each to a SINGLE variable (radius / a0 / a1) and drag() soft-
// targets it, so ONLY that DOF moves -- the centre never drifts, for ANY circle/arc
// position (the failure mode of a centre-coupled 2D-residual design off the origin).

TEST_CASE("drag c1.rim sets the circle radius from xy[0]; off-centre never drifts") {
    json s;
    s["entities"] = json::array({circle("c1", 5, 3, 10)});   // deliberately OFF the origin
    s["constraints"] = json::array({rel("c_fix", "fix", {"c1.center"})});
    json env = json::object();
    auto sol = solve_sketch(s, env);
    REQUIRE(sol.converged);
    // Host sends the pre-computed radius (mm) in xy[0]; xy[1] is unused.
    json res = drag(sol.state, "c1.rim", {16.0, 0.0}, s, env);
    REQUIRE(res["ok"].get<bool>());
    const json& c = res["entities"]["c1"];
    CHECK(double(c["center"][0]) == doctest::Approx(5.0).epsilon(1e-4));  // centre held
    CHECK(double(c["center"][1]) == doctest::Approx(3.0).epsilon(1e-4));
    CHECK(double(c["radius"]) > 10.1);                                    // it grew
    CHECK(double(c["radius"]) == doctest::Approx(16.0).epsilon(1e-2));    // to xy[0]
}

TEST_CASE("drag a1.rim sets the arc radius from xy[0]; centre held") {
    json s;
    s["entities"] = json::array({arc("a1", 2, 1, 10, 0, 90)});
    s["constraints"] = json::array({rel("c_fix", "fix", {"a1.center"})});
    json env = json::object();
    auto sol = solve_sketch(s, env);
    REQUIRE(sol.converged);
    json res = drag(sol.state, "a1.rim", {20.0, 0.0}, s, env);  // radius = 20 mm
    REQUIRE(res["ok"].get<bool>());
    const json& a = res["entities"]["a1"];
    CHECK(double(a["center"][0]) == doctest::Approx(2.0).epsilon(1e-4));
    CHECK(double(a["center"][1]) == doctest::Approx(1.0).epsilon(1e-4));
    CHECK(double(a["radius"]) == doctest::Approx(20.0).epsilon(1e-2));
}

TEST_CASE("drag a1.start_angle sets the start angle from xy[0]; centre + radius held") {
    json s;
    s["entities"] = json::array({arc("a1", 3, 4, 10, 0, 90)});
    s["constraints"] = json::array({
        rel("c_fix", "fix", {"a1.center"}),
        dim("c_r", "radius", {"a1"}, 10.0),
    });
    json env = json::object();
    auto sol = solve_sketch(s, env);
    REQUIRE(sol.converged);
    // Host sends the pre-computed start angle (deg) in xy[0].
    json res = drag(sol.state, "a1.start_angle", {120.0, 0.0}, s, env);
    REQUIRE(res["ok"].get<bool>());
    const json& a = res["entities"]["a1"];
    CHECK(double(a["center"][0]) == doctest::Approx(3.0).epsilon(1e-4));  // centre held
    CHECK(double(a["center"][1]) == doctest::Approx(4.0).epsilon(1e-4));
    CHECK(double(a["radius"]) == doctest::Approx(10.0).epsilon(1e-3));    // radius held
    CHECK(double(a["start"]) == doctest::Approx(120.0).epsilon(1e-2));    // a0 -> 120 deg
    CHECK(double(a["end"]) == doctest::Approx(90.0).epsilon(1e-3));       // a1 untouched
}

TEST_CASE("drag a1.end_angle sets the end angle from xy[0]; start untouched") {
    json s;
    s["entities"] = json::array({arc("a1", 0, 0, 10, 0, 90)});
    s["constraints"] = json::array({
        rel("c_fix", "fix", {"a1.center"}),
        dim("c_r", "radius", {"a1"}, 10.0),
    });
    json env = json::object();
    auto sol = solve_sketch(s, env);
    REQUIRE(sol.converged);
    json res = drag(sol.state, "a1.end_angle", {200.0, 0.0}, s, env);  // a1 -> 200 deg
    REQUIRE(res["ok"].get<bool>());
    const json& a = res["entities"]["a1"];
    CHECK(double(a["radius"]) == doctest::Approx(10.0).epsilon(1e-3));
    CHECK(std::abs(double(a["start"])) < 1e-4);                        // a0 untouched (0)
    CHECK(double(a["end"]) == doctest::Approx(200.0).epsilon(1e-2));   // a1 -> 200 deg
}

TEST_CASE("drag e1.p2 (identity handle) still tracks the cursor after the residual change") {
    // Regression: a plain identity-point handle (idx = {px, py}) is dragged by the
    // cursor (u, v) via two soft_targets -- the original path, distinct from the new
    // 1-var scalar handles (rim/angle). Mirrors the line case in test_drag.cpp.
    json s;
    s["entities"] = json::array({line("e1", 0, 0, 10, 0)});
    s["constraints"] = json::array({
        rel("c_fix", "coincident", {"e1.p1", "origin"}),
        dim("c_len", "distance", {"e1.p1", "e1.p2"}, 10.0),
    });
    json env = json::object();
    auto sol = solve_sketch(s, env);
    REQUIRE(sol.converged);
    json res = drag(sol.state, "e1.p2", {7.0, 7.0}, s, env);
    REQUIRE(res["ok"].get<bool>());
    const json& e = res["entities"]["e1"];
    CHECK(std::hypot(double(e["p1"][0]), double(e["p1"][1])) < 1e-6);  // p1 at origin
    double len = std::hypot(double(e["p2"][0]) - double(e["p1"][0]),
                            double(e["p2"][1]) - double(e["p1"][1]));
    CHECK(std::abs(len - 10.0) < 1e-6);  // length preserved
    const double root = 10.0 / std::sqrt(2.0);
    CHECK(double(e["p2"][0]) == doctest::Approx(root).epsilon(1e-3));
    CHECK(double(e["p2"][1]) == doctest::Approx(root).epsilon(1e-3));
}
