// Sketch3D solver tests — per-constraint solves for the v1 3D free-space sketch
// constraint set, an over-constrained conflict case, statuses/DOF shape parity
// with the 2D solver, and the drag RPC core. Mirrors tests/test_sketch.cpp.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include "sketch/sketch3d.h"

#include <cmath>
#include <string>
#include <vector>

using chudware::sketch3d::drag3;
using chudware::sketch3d::solve_sketch3d;
using json = nlohmann::json;

namespace {

json point(const char* id, double x, double y, double z) {
    return json{{"id", id}, {"type", "point"}, {"construction", false},
                {"p", json::array({x, y, z})}};
}
json line(const char* id, double x1, double y1, double z1, double x2, double y2,
          double z2) {
    return json{{"id", id}, {"type", "line"}, {"construction", false},
                {"p1", json::array({x1, y1, z1})}, {"p2", json::array({x2, y2, z2})}};
}
json arc3(const char* id, std::vector<double> p1, std::vector<double> pm,
          std::vector<double> p2) {
    return json{{"id", id}, {"type", "arc"}, {"construction", false},
                {"p1", p1}, {"pm", pm}, {"p2", p2}};
}
json rel(const char* id, const char* type, std::vector<const char*> refs) {
    json r = json::array();
    for (auto s : refs) r.push_back(s);
    return json{{"id", id}, {"type", type}, {"refs", r}};
}
json dim(const char* id, const char* type, std::vector<const char*> refs, double v) {
    json r = json::array();
    for (auto s : refs) r.push_back(s);
    return json{{"id", id}, {"type", type}, {"refs", r},
                {"value", json{{"value", v}, {"expr", nullptr}}}, {"driven", false}};
}

double dist3(const json& a, const json& b) {
    double dx = a[0].get<double>() - b[0].get<double>();
    double dy = a[1].get<double>() - b[1].get<double>();
    double dz = a[2].get<double>() - b[2].get<double>();
    return std::sqrt(dx * dx + dy * dy + dz * dz);
}

Eigen::Vector3d vec3(const json& p) {
    return {p[0].get<double>(), p[1].get<double>(), p[2].get<double>()};
}
Eigen::Vector3d center3(const json& arc) {
    Eigen::Vector3d a = vec3(arc["p1"]), u = vec3(arc["pm"]) - a, v = vec3(arc["p2"]) - a;
    Eigen::Vector3d n = u.cross(v);
    return a + (u.squaredNorm() * v.cross(n) + v.squaredNorm() * n.cross(u)) / (2 * n.squaredNorm());
}
double radius3(const json& arc) { return (vec3(arc["p1"]) - center3(arc)).norm(); }
Eigen::Vector3d tangent3(const json& arc, const char* end) {
    Eigen::Vector3d n = (vec3(arc["pm"]) - vec3(arc["p1"])).cross(
        vec3(arc["p2"]) - vec3(arc["p1"]));
    return n.cross(vec3(arc[end]) - center3(arc)).normalized();
}
json plane_rel(const char* type, std::vector<const char*> refs, const json& plane) {
    json c = rel("c2", type, refs);
    c["plane"] = plane;
    return c;
}

}  // namespace

TEST_CASE("coincident pulls endpoints together; fix pins the anchor") {
    json sk;
    sk["entities"] = json::array({line("e1", 0, 0, 0, 10, 0, 0),
                                  line("e2", 10.5, 0.3, -0.2, 10, 10, 5)});
    sk["constraints"] = json::array({rel("c1", "fix", {"e1"}),
                                     rel("c2", "coincident", {"e1.p2", "e2.p1"})});
    auto sol = solve_sketch3d(sk, json::object());
    REQUIRE(sol.converged);
    const json& e1 = sol.entities["e1"];
    const json& e2 = sol.entities["e2"];
    // e1 pinned exactly where authored.
    CHECK(dist3(e1["p1"], json::array({0, 0, 0})) < 1e-8);
    CHECK(dist3(e1["p2"], json::array({10, 0, 0})) < 1e-8);
    // e2.p1 snapped onto e1.p2.
    CHECK(dist3(e1["p2"], e2["p1"]) < 1e-7);
    CHECK(sol.statuses["e1"] == "full");
    CHECK(sol.statuses["e2"] == "under");  // e2.p2 still free
    CHECK(sol.dof == 3);                   // e2.p2 x/y/z
}

TEST_CASE("distance point-point and length dims") {
    json sk;
    sk["entities"] = json::array({point("e1", 0, 0, 0), point("e2", 3, 1, 1),
                                  line("e3", 0, 0, 0, 5, 0, 0)});
    sk["constraints"] = json::array({rel("c1", "fix", {"e1"}),
                                     dim("c2", "distance", {"e1.p", "e2.p"}, 25.0),
                                     dim("c3", "length", {"e3"}, 42.0)});
    auto sol = solve_sketch3d(sk, json::object());
    REQUIRE(sol.converged);
    CHECK(dist3(sol.entities["e1"]["p"], sol.entities["e2"]["p"]) ==
          doctest::Approx(25.0).epsilon(1e-6));
    CHECK(dist3(sol.entities["e3"]["p1"], sol.entities["e3"]["p2"]) ==
          doctest::Approx(42.0).epsilon(1e-6));
}

TEST_CASE("distance point-line (perpendicular gap to the infinite line)") {
    json sk;
    sk["entities"] = json::array({line("e1", 0, 0, 0, 10, 0, 0),
                                  point("e2", 5, 2, 0)});
    sk["constraints"] = json::array({rel("c1", "fix", {"e1"}),
                                     dim("c2", "distance", {"e2.p", "e1"}, 7.0)});
    auto sol = solve_sketch3d(sk, json::object());
    REQUIRE(sol.converged);
    const json& p = sol.entities["e2"]["p"];
    // Perpendicular distance to the x-axis line = sqrt(y^2 + z^2).
    double d = std::hypot(p[1].get<double>(), p[2].get<double>());
    CHECK(d == doctest::Approx(7.0).epsilon(1e-6));
}

TEST_CASE("parallel / perpendicular / angle between free-space lines") {
    json sk;
    sk["entities"] = json::array({line("e1", 0, 0, 0, 10, 0, 0),
                                  line("e2", 0, 5, 3, 8, 7, 4),
                                  line("e3", 0, 0, 10, 5, 5, 12),
                                  line("e4", 1, 1, 1, 6, 2, 3)});
    sk["constraints"] = json::array({rel("c1", "fix", {"e1"}),
                                     rel("c2", "parallel", {"e1", "e2"}),
                                     rel("c3", "perpendicular", {"e1", "e3"}),
                                     dim("c4", "angle", {"e1", "e4"}, 60.0)});
    auto sol = solve_sketch3d(sk, json::object());
    REQUIRE(sol.converged);
    auto dir = [&](const char* id) {
        const json& e = sol.entities[id];
        return std::array<double, 3>{
            e["p2"][0].get<double>() - e["p1"][0].get<double>(),
            e["p2"][1].get<double>() - e["p1"][1].get<double>(),
            e["p2"][2].get<double>() - e["p1"][2].get<double>()};
    };
    auto d1 = dir("e1"), d2 = dir("e2"), d3 = dir("e3"), d4 = dir("e4");
    auto norm = [](const std::array<double, 3>& v) {
        return std::sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2]);
    };
    auto dot = [](const std::array<double, 3>& a, const std::array<double, 3>& b) {
        return a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
    };
    // parallel: |cos| == 1
    CHECK(std::abs(dot(d1, d2)) / (norm(d1) * norm(d2)) ==
          doctest::Approx(1.0).epsilon(1e-6));
    // perpendicular: cos == 0
    CHECK(dot(d1, d3) / (norm(d1) * norm(d3)) == doctest::Approx(0.0).epsilon(1e-6));
    // angle: cos == cos(60 deg)
    CHECK(dot(d1, d4) / (norm(d1) * norm(d4)) == doctest::Approx(0.5).epsilon(1e-5));
}

TEST_CASE("along_axis snaps a line to Z and to an explicit direction") {
    json sk;
    sk["entities"] = json::array({line("e1", 0, 0, 0, 1, 1, 8),
                                  line("e2", 5, 0, 0, 9, 1, 0)});
    json ax = rel("c1", "along_axis", {"e1"});
    ax["axis"] = "z";
    json dr = rel("c2", "along_axis", {"e2"});
    dr["dir"] = json::array({1, 1, 0});
    sk["constraints"] = json::array({ax, dr});
    auto sol = solve_sketch3d(sk, json::object());
    REQUIRE(sol.converged);
    const json& e1 = sol.entities["e1"];
    CHECK(std::abs(e1["p2"][0].get<double>() - e1["p1"][0].get<double>()) < 1e-6);
    CHECK(std::abs(e1["p2"][1].get<double>() - e1["p1"][1].get<double>()) < 1e-6);
    const json& e2 = sol.entities["e2"];
    double dx = e2["p2"][0].get<double>() - e2["p1"][0].get<double>();
    double dy = e2["p2"][1].get<double>() - e2["p1"][1].get<double>();
    double dz = e2["p2"][2].get<double>() - e2["p1"][2].get<double>();
    CHECK(std::abs(dx - dy) < 1e-6);  // along (1,1,0): equal x/y components
    CHECK(std::abs(dz) < 1e-6);
}

TEST_CASE("point_on line and point_on arc") {
    json sk;
    sk["entities"] = json::array({
        line("e1", 0, 0, 0, 10, 0, 0),
        point("e2", 4, 3, 1),
        // Unit-ish arc in the XY plane through (20,0,0)-(25,5,0)-(30,0,0):
        // circumcircle center (25,0,0), radius 5.
        arc3("e3", {20, 0, 0}, {25, 5, 0}, {30, 0, 0}),
        point("e4", 27, 3, 2),
    });
    sk["constraints"] = json::array({rel("c1", "fix", {"e1"}),
                                     rel("c2", "point_on", {"e2.p", "e1"}),
                                     rel("c3", "fix", {"e3"}),
                                     rel("c4", "point_on", {"e4.p", "e3"})});
    auto sol = solve_sketch3d(sk, json::object());
    REQUIRE(sol.converged);
    const json& p2 = sol.entities["e2"]["p"];
    CHECK(std::abs(p2[1].get<double>()) < 1e-6);  // on the x-axis line
    CHECK(std::abs(p2[2].get<double>()) < 1e-6);
    const json& p4 = sol.entities["e4"]["p"];
    CHECK(std::abs(p4[2].get<double>()) < 1e-6);  // pulled into the arc plane
    double dx = p4[0].get<double>() - 25.0, dy = p4[1].get<double>();
    CHECK(std::hypot(dx, dy) == doctest::Approx(5.0).epsilon(1e-5));  // on the rim
}

TEST_CASE("over-constrained conflict is reported, entities colored over") {
    json sk;
    sk["entities"] = json::array({point("e1", 0, 0, 0), point("e2", 10, 0, 0)});
    sk["constraints"] = json::array({rel("c1", "fix", {"e1"}), rel("c2", "fix", {"e2"}),
                                     dim("c3", "distance", {"e1.p", "e2.p"}, 50.0)});
    auto sol = solve_sketch3d(sk, json::object());
    // Both points pinned 10 apart but dimensioned 50 -> unsatisfiable.
    CHECK(!sol.converged);
    CHECK(!sol.conflicting.empty());
    bool e_over = sol.statuses["e1"] == "over" || sol.statuses["e2"] == "over";
    CHECK(e_over);
}

TEST_CASE("redundant duplicate constraint flagged, still solves") {
    json sk;
    sk["entities"] = json::array({line("e1", 0, 0, 0, 10, 0, 0),
                                  line("e2", 0, 5, 0, 10, 6, 1)});
    sk["constraints"] = json::array({rel("c1", "fix", {"e1"}),
                                     rel("c2", "parallel", {"e1", "e2"}),
                                     rel("c3", "parallel", {"e1", "e2"})});
    auto sol = solve_sketch3d(sk, json::object());
    REQUIRE(sol.converged);
    CHECK(!sol.redundant.empty());
    CHECK(sol.conflicting.empty());
}

TEST_CASE("spline points participate: coincident chains a spline onto a line end") {
    json sk;
    json sp = json{{"id", "e2"}, {"type", "spline"}, {"construction", false},
                   {"points", json::array({json::array({10.4, 0.2, 0.1}),
                                           json::array({15, 5, 2}),
                                           json::array({20, 0, 4})})}};
    sk["entities"] = json::array({line("e1", 0, 0, 0, 10, 0, 0), sp});
    sk["constraints"] = json::array({rel("c1", "fix", {"e1"}),
                                     rel("c2", "coincident", {"e1.p2", "e2.p0"})});
    auto sol = solve_sketch3d(sk, json::object());
    REQUIRE(sol.converged);
    CHECK(dist3(sol.entities["e1"]["p2"], sol.entities["e2"]["points"][0]) < 1e-7);
}

TEST_CASE("dimension expression resolves against env") {
    json sk;
    sk["entities"] = json::array({point("e1", 0, 0, 0), point("e2", 10, 0, 0)});
    json d = dim("c2", "distance", {"e1.p", "e2.p"}, 1.0);
    d["value"]["expr"] = "W*2";
    sk["constraints"] = json::array({rel("c1", "fix", {"e1"}), d});
    auto sol = solve_sketch3d(sk, json{{"W", 12.0}});
    REQUIRE(sol.converged);
    CHECK(dist3(sol.entities["e1"]["p"], sol.entities["e2"]["p"]) ==
          doctest::Approx(24.0).epsilon(1e-6));
}

TEST_CASE("drag3 soft-targets a handle and honors constraints") {
    json sk;
    sk["entities"] = json::array({line("e1", 0, 0, 0, 10, 0, 0)});
    json ax = rel("c1", "along_axis", {"e1"});
    ax["axis"] = "x";
    json fx = rel("c2", "fix", {"e1.p1"});
    sk["constraints"] = json::array({ax, fx});

    auto sol = solve_sketch3d(sk, json::object());
    REQUIRE(sol.converged);
    // Drag p2 toward (20, 5, 5); the along-x + fixed p1 constraints must keep it
    // on the x axis, stretching the line instead.
    json res = drag3(sol.state, "e1.p2", Eigen::Vector3d(20, 5, 5), sk, json::object());
    REQUIRE(res["ok"].get<bool>());
    const json& p2 = res["entities"]["e1"]["p2"];
    CHECK(std::abs(p2[1].get<double>()) < 1e-5);
    CHECK(std::abs(p2[2].get<double>()) < 1e-5);
    CHECK(p2[0].get<double>() > 15.0);  // pulled toward the target along x

    // Unknown handle is rejected loudly, entities preserved.
    json bad = drag3(sol.state, "e9.p1", Eigen::Vector3d(0, 0, 0), sk, json::object());
    CHECK(!bad["ok"].get<bool>());
    CHECK(bad["rejected_handles"].size() == 1);
    CHECK(bad["entities"].contains("e1"));
}

// ---- eq_curve3d ----------------------------------------------------------------

TEST_CASE("eq_curve3d: helix endpoints match x(t1)/y(t1)/z(t1) and x(t2)/y(t2)/z(t2)") {
    json sk;
    json ec = json{{"id", "e1"}, {"type", "eq_curve3d"}, {"construction", false},
                   {"xt", "10*cos(t)"}, {"yt", "10*sin(t)"}, {"zt", "2*t/360"},
                   {"t1", 0}, {"t2", 360}};
    sk["entities"] = json::array({ec});
    sk["constraints"] = json::array();
    auto sol = solve_sketch3d(sk, json::object());
    REQUIRE(sol.converged);
    const json& p1 = sol.entities["e1"]["p1"];
    const json& p2 = sol.entities["e1"]["p2"];
    CHECK(p1[0].get<double>() == doctest::Approx(10.0).epsilon(1e-6));
    CHECK(p1[1].get<double>() == doctest::Approx(0.0).epsilon(1e-6));
    CHECK(p1[2].get<double>() == doctest::Approx(0.0).epsilon(1e-6));
    CHECK(p2[0].get<double>() == doctest::Approx(10.0).epsilon(1e-6));
    CHECK(p2[1].get<double>() == doctest::Approx(0.0).epsilon(1e-6));
    CHECK(p2[2].get<double>() == doctest::Approx(2.0).epsilon(1e-6));
    // 0 DOF — equation-fixed.
    CHECK(sol.dof == 0);
    CHECK(sol.entities["e1"]["samples"].size() > 2);
}

TEST_CASE("eq_curve3d: endpoint is a valid coincident target and stays pinned") {
    json sk;
    json ec = json{{"id", "e1"}, {"type", "eq_curve3d"}, {"construction", false},
                   {"xt", "t"}, {"yt", "0"}, {"zt", "0"}, {"t1", 0}, {"t2", 10}};
    json ln = line("e2", 20, 5, 5, 30, 5, 5);
    sk["entities"] = json::array({ec, ln});
    sk["constraints"] = json::array({rel("c1", "coincident", {"e1.p2", "e2.p1"})});
    auto sol = solve_sketch3d(sk, json::object());
    REQUIRE(sol.converged);
    // eq_curve3d endpoint never moves (equation-fixed); the line's p1 is pulled to it.
    CHECK(dist3(sol.entities["e1"]["p2"], json::array({10.0, 0.0, 0.0})) < 1e-6);
    CHECK(dist3(sol.entities["e2"]["p1"], sol.entities["e1"]["p2"]) < 1e-6);
}

TEST_CASE("3D tangent aligns line and arc directions at their coincident endpoint") {
    json sk{{"entities", {arc3("e1", {10, 0, 0}, {std::sqrt(50.), std::sqrt(50.), 0}, {0, 10, 0}),
                          line("e2", 10, 0, 0, 10.7, 7, 1)}},
            {"constraints", {rel("c1", "fix", {"e1"}), rel("c2", "fix", {"e2.p1"}),
                              rel("c3", "tangent", {"e1", "e2"})}}};
    auto sol = solve_sketch3d(sk, json::object());
    REQUIRE(sol.converged);
    CHECK(sol.skipped.empty());
    Eigen::Vector3d direction = (vec3(sol.entities["e2"]["p2"]) - vec3(sol.entities["e2"]["p1"])).normalized();
    CHECK(direction.cross(tangent3(sol.entities["e1"], "p1")).norm() < 1e-7);
    CHECK(sol.dof == 1); // the tangent line's length remains editable
}

TEST_CASE("3D tangent joins two arcs at the selected end without imposing equal radius") {
    json sk{{"entities", {arc3("e1", {10, 0, 0}, {std::sqrt(50.), std::sqrt(50.), 0}, {0, 10, 0}),
                          arc3("e2", {0, 10, 0}, {7, 13, .4}, {10, 20, 1})}},
            {"constraints", {rel("c1", "fix", {"e1"}), rel("c2", "fix", {"e2.p1"}),
                              rel("c3", "tangent", {"e1.p2", "e2.p1"})}}};
    auto sol = solve_sketch3d(sk, json::object());
    REQUIRE(sol.converged);
    CHECK(sol.skipped.empty());
    CHECK(tangent3(sol.entities["e1"], "p2").cross(tangent3(sol.entities["e2"], "p1")).norm() < 1e-7);
}

TEST_CASE("3D tangent uses the interpolated spline endpoint derivative, not its first chord") {
    json spline{{"id", "e1"}, {"type", "spline"}, {"points", {{0, 0, 0}, {5, 3, 2}, {10, 0, 4}}}};
    json sk{{"entities", {spline, line("e2", 0, 0, 0, -3, 1, .2)}},
            {"constraints", {rel("c1", "fix", {"e1"}), rel("c2", "fix", {"e2.p1"}),
                              rel("c3", "tangent", {"e1.p0", "e2"})}}};
    auto sol = solve_sketch3d(sk, json::object());
    REQUIRE(sol.converged);
    CHECK(sol.skipped.empty());
    // Equal chord lengths: the quadratic end derivative is (-3 P0 + 4 P1 - P2)/2h.
    Eigen::Vector3d expected(10, 12, 4);
    Eigen::Vector3d actual = vec3(sol.entities["e2"]["p2"]) - vec3(sol.entities["e2"]["p1"]);
    CHECK(actual.normalized().cross(expected.normalized()).norm() < 1e-7);
}

TEST_CASE("3D equal preserves editable line lengths and arc radii") {
    SUBCASE("lines") {
        json sk{{"entities", {line("e1", 0, 0, 0, 10, 0, 0), line("e2", 1, 3, 2, 7, 7, 4)}},
                {"constraints", {rel("c1", "fix", {"e1"}), rel("c2", "equal", {"e1", "e2"})}}};
        auto sol = solve_sketch3d(sk, json::object());
        REQUIRE(sol.converged);
        CHECK(sol.skipped.empty());
        CHECK(dist3(sol.entities["e2"]["p1"], sol.entities["e2"]["p2"]) == doctest::Approx(10.).epsilon(1e-7));
    }
    SUBCASE("arcs") {
        json sk{{"entities", {arc3("e1", {0, 0, 0}, {5, 5, 0}, {10, 0, 0}),
                              arc3("e2", {20, 0, 1}, {26, 6, 1}, {32, 0, 1})}},
                {"constraints", {rel("c1", "fix", {"e1"}), rel("c2", "equal", {"e1", "e2"})}}};
        auto sol = solve_sketch3d(sk, json::object());
        REQUIRE(sol.converged);
        CHECK(sol.skipped.empty());
        CHECK(radius3(sol.entities["e2"]) == doctest::Approx(5.).epsilon(1e-7));
    }
}

TEST_CASE("3D midpoint targets a line midpoint and an arc mid-sweep, not the through point") {
    SUBCASE("line") {
        json sk{{"entities", {line("e1", 1, 2, 3, 9, 6, 7), point("e2", 6, 3, 4)}},
                {"constraints", {rel("c1", "fix", {"e1"}), rel("c2", "midpoint", {"e2.p", "e1"})}}};
        auto sol = solve_sketch3d(sk, json::object());
        REQUIRE(sol.converged);
        CHECK(sol.skipped.empty());
        CHECK(dist3(sol.entities["e2"]["p"], json::array({5, 4, 5})) < 1e-7);
    }
    SUBCASE("arc") {
        json sk{{"entities", {arc3("e1", {10, 0, 0}, {8, 6, 0}, {0, 10, 0}), point("e2", 6, 8, 1)}},
                {"constraints", {rel("c1", "fix", {"e1"}), rel("c2", "midpoint", {"e2.p", "e1"})}}};
        auto sol = solve_sketch3d(sk, json::object());
        REQUIRE(sol.converged);
        CHECK(sol.skipped.empty());
        CHECK(dist3(sol.entities["e2"]["p"], json::array({std::sqrt(50.), std::sqrt(50.), 0})) < 1e-7);
    }
}

TEST_CASE("3D collinear removes offset as well as angular error") {
    json sk{{"entities", {line("e1", 0, 0, 0, 10, 0, 0), line("e2", 1, 2, 1, 8, 3, -1)}},
            {"constraints", {rel("c1", "fix", {"e1"}), rel("c2", "collinear", {"e1", "e2"})}}};
    auto sol = solve_sketch3d(sk, json::object());
    REQUIRE(sol.converged);
    CHECK(sol.skipped.empty());
    for (const char* end : {"p1", "p2"}) CHECK(vec3(sol.entities["e2"][end]).tail<2>().norm() < 1e-7);
    CHECK(sol.dof == 2);
}

TEST_CASE("3D concentric aligns arc centers while leaving their radii independent") {
    json sk{{"entities", {arc3("e1", {0, 0, 0}, {5, 5, 0}, {10, 0, 0}),
                          arc3("e2", {1, 1, 1}, {5, 7, 1}, {9, 1, 1})}},
            {"constraints", {rel("c1", "fix", {"e1"}), rel("c2", "concentric", {"e1", "e2"})}}};
    auto sol = solve_sketch3d(sk, json::object());
    REQUIRE(sol.converged);
    CHECK(sol.skipped.empty());
    CHECK((center3(sol.entities["e1"]) - center3(sol.entities["e2"])).norm() < 1e-7);
    CHECK(sol.dof == 6);
}

TEST_CASE("3D coradial aligns both the center and radius") {
    json sk{{"entities", {arc3("e1", {0, 0, 0}, {5, 5, 0}, {10, 0, 0}),
                          arc3("e2", {1, 1, 1}, {5, 7, 1}, {9, 1, 1})}},
            {"constraints", {rel("c1", "fix", {"e1"}), rel("c2", "coradial", {"e1", "e2"})}}};
    auto sol = solve_sketch3d(sk, json::object());
    REQUIRE(sol.converged);
    CHECK(sol.skipped.empty());
    CHECK((center3(sol.entities["e1"]) - center3(sol.entities["e2"])).norm() < 1e-7);
    CHECK(radius3(sol.entities["e2"]) == doctest::Approx(5.).epsilon(1e-7));
    CHECK(sol.dof == 5);
}

TEST_CASE("3D on_plane uses the engine datum convention and constrains whole curves") {
    json entities, c;
    SUBCASE("front is XZ") {
        entities = {point("e1", 1, 2, 3)};
        c = plane_rel("on_plane", {"e1"}, "front");
    }
    SUBCASE("top is XY") {
        entities = {line("e1", 1, 2, 3, 4, 5, 6)};
        c = plane_rel("on_plane", {"e1"}, "top");
    }
    SUBCASE("right is YZ") {
        entities = {arc3("e1", {1, 0, 0}, {2, 5, 5}, {1, 10, 0})};
        c = plane_rel("on_plane", {"e1"}, "right");
    }
    json sk{{"entities", entities}, {"constraints", {c}}};
    auto sol = solve_sketch3d(sk, json::object());
    REQUIRE(sol.converged);
    CHECK(sol.skipped.empty());
    int coordinate = c["plane"] == "front" ? 1 : c["plane"] == "top" ? 2 : 0;
    const auto& e = sol.entities["e1"];
    for (const char* key : {"p", "p1", "pm", "p2"})
        if (e.contains(key)) CHECK(std::abs(e[key][coordinate].get<double>()) < 1e-7);
}

TEST_CASE("3D parallel_plane leaves the line offset unchanged and removes normal direction") {
    json sk{{"entities", {line("e1", 1, 2, 3, 8, 6, 5)}},
            {"constraints", {plane_rel("parallel_plane", {"e1"}, "top")}}};
    auto sol = solve_sketch3d(sk, json::object());
    REQUIRE(sol.converged);
    CHECK(sol.skipped.empty());
    CHECK(sol.entities["e1"]["p1"][2].get<double>() == doctest::Approx(4.).epsilon(1e-7));
    CHECK(sol.entities["e1"]["p2"][2].get<double>() == doctest::Approx(4.).epsilon(1e-7));
    CHECK(sol.dof == 5);
}

TEST_CASE("3D normal_plane accepts a normalized arbitrary working plane") {
    json plane{{"origin", {4, 5, 6}}, {"normal", {2, 2, 0}}};
    json sk{{"entities", {line("e1", 0, 0, 0, 8, 6, 1)}},
            {"constraints", {plane_rel("normal_plane", {"e1"}, plane)}}};
    auto sol = solve_sketch3d(sk, json::object());
    REQUIRE(sol.converged);
    CHECK(sol.skipped.empty());
    Eigen::Vector3d d = vec3(sol.entities["e1"]["p2"]) - vec3(sol.entities["e1"]["p1"]);
    CHECK(d.cross(Eigen::Vector3d(1, 1, 0)).norm() < 1e-7);
    CHECK(sol.dof == 4);
}

TEST_CASE("3D symmetric mirrors points about an offset, tilted working plane") {
    json plane{{"origin", {1, 2, 3}}, {"normal", {2, 2, 0}}};
    json sk{{"entities", {point("e1", 5, 6, 7), point("e2", -2, -1, 8)}},
            {"constraints", {rel("c1", "fix", {"e1"}), plane_rel("symmetric", {"e1.p", "e2.p"}, plane)}}};
    auto sol = solve_sketch3d(sk, json::object());
    REQUIRE(sol.converged);
    CHECK(sol.skipped.empty());
    CHECK(dist3(sol.entities["e2"]["p"], json::array({-3, -2, 7})) < 1e-7);
    CHECK(sol.dof == 0);
}

TEST_CASE("3D point_on spline projects onto the interpolated curve from a perturbed point") {
    // All points lie on y=x^2/10 and z=0, with equal chord lengths on each half.
    // The three-point OCCT interpolation is the corresponding quadratic in chord parameter.
    json spline{{"id", "e1"}, {"type", "spline"}, {"points", {{-10, 10, 0}, {0, 0, 0}, {10, 10, 0}}}};
    json sk{{"entities", {spline, point("e2", 4, 2.8, 1.2)}},
            {"constraints", {rel("c1", "fix", {"e1"}), rel("c2", "point_on", {"e2.p", "e1"})}}};
    auto sol = solve_sketch3d(sk, json::object());
    REQUIRE(sol.converged);
    CHECK(sol.skipped.empty());
    const auto p = vec3(sol.entities["e2"]["p"]);
    CHECK(std::abs(p.z()) < 1e-7);
    CHECK(std::abs(p.y() - p.x() * p.x() / 10) < 1e-7);
    CHECK(sol.dof == 1); // sliding along the spline must remain a genuine free DOF
    REQUIRE(sol.entities["e1"]["samples"].size() > 3);
    for (const auto& sample : sol.entities["e1"]["samples"]) {
        const auto q = vec3(sample);
        CHECK(std::abs(q.y() - q.x() * q.x() / 10) < 1e-7);
    }
}

TEST_CASE("3D cubic spline point_on and display samples match the compiled interpolation") {
    json spline{{"id", "e1"}, {"type", "spline"}, {"points", {{0, 0, 0}, {2, 3, 1}, {4, 0, 2}, {6, -3, 3}}}};
    json sk{{"entities", {spline, point("e2", 3.2, 1.4, 1.8)}},
            {"constraints", {rel("c1", "fix", {"e1"}), rel("c2", "point_on", {"e2.p", "e1"})}}};
    auto sol = solve_sketch3d(sk, json::object());
    REQUIRE(sol.converged);
    CHECK(sol.skipped.empty());
    CHECK(sol.dof == 1);
    auto check_curve = [](const json& point) {
        Eigen::Vector3d p = vec3(point);
        const double u = p.x() / 2;
        CHECK(std::abs(p.z() - u) < 1e-7);
        CHECK(std::abs(p.y() - (u * u * u - 6 * u * u + 8 * u)) < 1e-7);
    };
    check_curve(sol.entities["e2"]["p"]);
    for (const auto& sample : sol.entities["e1"]["samples"]) check_curve(sample);
}

TEST_CASE("3D unsupported relations and bad planes report skipped ids and reasons") {
    json sk{{"entities", {point("e1", 1, 2, 3), line("e2", 0, 0, 0, 10, 0, 0)}},
            {"constraints", {rel("c1", "equal", {"e1", "e2"}),
                              plane_rel("on_plane", {"e1"}, json{{"origin", {0, 0, 0}}, {"normal", {0, 0, 0}}}),
                              rel("c3", "midpoint", {"e9.p", "e2"})}}};
    auto sol = solve_sketch3d(sk, json::object());
    REQUIRE(sol.skipped.size() == 3);
    CHECK(sol.skipped[0].first == "c1");
    CHECK(sol.skipped[1].first == "c2");
    CHECK(sol.skipped[2].first == "c3");
    for (const auto& skipped : sol.skipped) CHECK_FALSE(skipped.second.empty());
    CHECK(sol.dof == 9);
}
