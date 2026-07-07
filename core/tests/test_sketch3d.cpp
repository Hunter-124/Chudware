// Sketch3D solver tests — per-constraint solves for the v1 3D free-space sketch
// constraint set, an over-constrained conflict case, statuses/DOF shape parity
// with the 2D solver, and the drag RPC core. Mirrors tests/test_sketch.cpp.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include "sketch/sketch3d.h"

#include <cmath>
#include <string>
#include <vector>

using aicad::sketch3d::drag3;
using aicad::sketch3d::solve_sketch3d;
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

// ---- eq_curve3d (T-3DEQ) ------------------------------------------------------

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
