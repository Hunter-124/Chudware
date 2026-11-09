#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <cmath>
#include <string>
#include "harness/route.h"
#include "harness/obstacles.h"
#include "harness/path_check.h"

using namespace chudware::harness;
using json = nlohmann::json;

namespace {
Scene box_scene(float lo, float hi) {
    SceneBody box;
    box.id = 7; box.name = "box";
    box.pos = {lo,lo,lo, hi,lo,lo, hi,hi,lo, lo,hi,lo,
               lo,lo,hi, hi,lo,hi, hi,hi,hi, lo,hi,hi};
    box.idx = {0,2,1, 0,3,2, 4,5,6, 4,6,7, 0,1,5, 0,5,4,
               1,2,6, 1,6,5, 2,3,7, 2,7,6, 3,0,4, 3,4,7};
    Scene result; result.bodies.push_back(box); return result;
}
json request() {
    return {{"start", {{"pos", {-10,0,0}}}}, {"end", {{"pos", {10,0,0}}}},
            {"od_mm", 1}, {"min_bend_radius_mm", 0.3}, {"clearance_mm", 0.5},
            {"grid_mm", 1}, {"max_nodes", 30000}};
}
V3 point(const json& a) { return {a[0].get<double>(), a[1].get<double>(), a[2].get<double>()}; }
// Exact samples of one circular bend of radius R: straight leg of `leg_in` into the entry at
// the origin, `steps` equal chords over `total_deg` (arc in the XY plane, curving toward +Y),
// straight leg of `leg_out` after the exit. When `mid_chord` >= 0 the midpoint of that chord
// (collinear with its neighbours) is inserted as an extra path point.
json arc_path(double R, double total_deg, int steps, double leg_in, double leg_out, int mid_chord = -1) {
    const double theta = total_deg * kPi / 180;
    auto on_arc = [&](double a) { return V3{R * std::sin(a), R - R * std::cos(a), 0}; };
    json path = json::array({json::array({-leg_in, 0.0, 0.0})});
    auto push = [&](const V3& v) { path.push_back(json::array({v.x, v.y, v.z})); };
    for (int j = 0; j <= steps; ++j) {
        push(on_arc(theta * j / steps));
        if (j == mid_chord) {
            const V3 a = on_arc(theta * j / steps), b = on_arc(theta * (j + 1) / steps);
            push((a + b) * 0.5);
        }
    }
    const V3 exit = on_arc(theta);
    push(exit + V3{std::cos(theta), std::sin(theta), 0} * leg_out);
    return path;
}
json bend_check(const json& path, double min_bend) {
    return harness_path_check({{"path", path}, {"od_mm", 1}, {"min_bend_radius_mm", min_bend}}, {});
}
}

TEST_CASE("empty scene routes straight with exact endpoints") {
    auto p = request();
    auto r = harness_route(p, {});
    REQUIRE(r["ok"] == true);
    CHECK(r["path"].front() == p["start"]["pos"]);
    CHECK(r["path"].back() == p["end"]["pos"]);
    CHECK(r["path"].size() == 2);
    CHECK(r["length_mm"].get<double>() == doctest::Approx(20));
    CHECK(r["min_clearance_mm"].is_null());
    CHECK(r["clearance_basis"].get<std::string>().find("triangle") != std::string::npos);
}

TEST_CASE("routes around triangle box and verifies every returned segment") {
    auto p = request();
    auto scene = box_scene(-1, 1);
    auto r = harness_route(p, scene);
    REQUIRE(r["ok"] == true);
    CHECK(r["path"].front() == p["start"]["pos"]);
    CHECK(r["path"].back() == p["end"]["pos"]);
    CHECK(r["length_mm"].get<double>() > 20);
    CHECK(r["min_clearance_mm"].get<double>() >= 0.5 - 1e-7);
    ObstacleSet obs;
    REQUIRE(build_obstacles(p, scene, 1, 0.5, obs).empty());
    for (size_t i = 1; i < r["path"].size(); ++i) {
        const auto a = point(r["path"][i - 1]), b = point(r["path"][i]);
        CHECK_FALSE(obs.bvh.any_within_segment(a, b, obs.thr, obs.thr_max));
    }
    for (const auto& bend : r["bends"]) CHECK(bend["radius_mm"].get<double>() >= 0.3);
}

TEST_CASE("outward endpoint directions produce bounded-radius bends") {
    auto p = request();
    p["start"] = {{"pos", {0,0,0}}, {"dir", {1,0,0}}};
    p["end"] = {{"pos", {20,20,0}}, {"dir", {0,-1,0}}};
    p["min_bend_radius_mm"] = 2;
    auto r = harness_route(p, {});
    REQUIRE(r["ok"] == true);
    REQUIRE(r["path"].size() > 3);
    REQUIRE(r["bends"].size() == 2);
    CHECK(r["path"].front() == p["start"]["pos"]);
    CHECK(r["path"].back() == p["end"]["pos"]);
    auto initial = unit(point(r["path"][1]) - point(r["path"][0]));
    auto outward = unit(point(r["path"][r["path"].size()-2]) - point(r["path"].back()));
    CHECK(initial.x == doctest::Approx(1)); CHECK(initial.y == doctest::Approx(0));
    CHECK(outward.y == doctest::Approx(-1)); CHECK(outward.x == doctest::Approx(0));
    for (const auto& bend : r["bends"]) CHECK(bend["radius_mm"].get<double>() == doctest::Approx(2));
    CHECK(r["exit_stub_mm"].get<double>() == doctest::Approx(4));
}

TEST_CASE("enclosed endpoints are refused even with relaxation") {
    auto p = request();
    p["start"]["pos"] = {0, 0, 0};
    p["endpoint_relax_mm"] = 20;
    auto r = harness_route(p, box_scene(-2, 2));
    CHECK(r["ok"] == false);
    CHECK(r["code"] == "start_blocked");
}

TEST_CASE("obstructed route honors node budget") {
    auto p = request(); p["max_nodes"] = 1;
    auto r = harness_route(p, box_scene(-1, 1));
    CHECK(r["ok"] == false);
    CHECK(r["code"] == "budget");
    CHECK_FALSE(r.contains("path"));
    CHECK(r["max_nodes"] == 1);
    CHECK(r["expanded_nodes"] == 1);
    CHECK(r["grid_mm"].get<double>() == doctest::Approx(1));
}

TEST_CASE("relaxation does not excuse unrelated clearance or crossing a solid") {
    auto p = request();
    p["start"]["pos"] = {1.25, 0, 0};
    p["endpoint_relax_mm"] = 2;
    auto scene = box_scene(-1, 1);
    auto second = scene.bodies.front();
    second.id = 8; second.name = "second";
    for (size_t i = 0; i < second.pos.size(); i += 3) second.pos[i] += 2.9f;
    scene.bodies.push_back(second);
    auto r = harness_route(p, scene);
    CHECK(r["ok"] == false);
    CHECK(r["code"] == "start_blocked");
    CHECK(r["obstacle"] == "second (#8)");
    CHECK(r["distance_mm"].get<double>() == doctest::Approx(0.65).epsilon(1e-3));
    p["start"]["dir"] = {-1, 0, 0};
    r = harness_route(p, box_scene(-1, 1));
    CHECK(r["ok"] == false);
    CHECK(r["code"] == "exit_blocked");
    CHECK(r["endpoint"] == "start");
    CHECK(r["obstacle"] == "box (#7)");
    CHECK(r["free_mm"].get<double>() == doctest::Approx(0.25).epsilon(0.05));
}

TEST_CASE("invalid routing inputs are refused") {
    auto p = request(); p["start"]["dir"] = {0,0,0};
    CHECK(harness_route(p, {})["code"] == "bad_args");
    p = request(); p["min_bend_radius_mm"] = -1;
    CHECK(harness_route(p, {})["code"] == "bad_args");
    p = request(); p["prefer_paths"] = json::array({json::array({json::array({0,0,0})})});
    CHECK(harness_route(p, {})["code"] == "bad_args");
    // A refusal identifies the offending parameter and value, not a list of every parameter.
    p = request(); p["max_nodes"] = 400000;
    auto r = harness_route(p, {});
    CHECK(r["code"] == "bad_args");
    CHECK(r["error"].get<std::string>().find("max_nodes = 400000") != std::string::npos);
    p = request(); p["grid_mm"] = 0;
    r = harness_route(p, {});
    CHECK(r["code"] == "bad_args");
    CHECK(r["error"].get<std::string>().find("grid_mm = 0") != std::string::npos);
}

TEST_CASE("path check detects isolated long-leg corners and reports straight paths") {
    json p = {{"path", {{-100,0,0}, {0,0,0}, {0,100,0}}},
              {"od_mm", 1}, {"min_bend_radius_mm", 10}};
    auto r = harness_path_check(p, {});
    REQUIRE(r["ok"] == true);
    REQUIRE(r["bend_violations"].size() == 1);
    const auto& bend = r["bend_violations"][0];
    CHECK(bend["index"] == 1);
    CHECK(bend["kind"] == "isolated_kink");
    CHECK(bend["radius_mm"].get<double>() == doctest::Approx(0));
    CHECK(bend["available_fillet_radius_mm"].get<double>() == doctest::Approx(100));
    CHECK(r["min_bend_radius_found_mm"].get<double>() == doctest::Approx(0));
    CHECK(r["min_clearance_mm"].is_null());
    p["path"] = {{-100,0,0}, {0,0,0}, {100,0,0}};
    r = harness_path_check(p, {});
    REQUIRE(r["ok"] == true);
    CHECK(r["bend_violations"].empty());
    CHECK(r["min_bend_radius_found_mm"].is_null());
}

TEST_CASE("path check recognizes a sampled circular arc") {
    json path = json::array();
    for (int i = 0; i <= 8; ++i) {
        const double angle = kPi * i / 16;
        path.push_back({20 * std::cos(angle), 20 * std::sin(angle), 0});
    }
    const json p = {{"path", path}, {"od_mm", 1}, {"min_bend_radius_mm", 10}};
    const auto r = harness_path_check(p, {});
    REQUIRE(r["ok"] == true);
    CHECK(r["bend_violations"].empty());
    CHECK(r["min_bend_radius_found_mm"].get<double>() == doctest::Approx(20));
}

TEST_CASE("path check detects segment intersection and enclosed centerline") {
    json p = {{"path", {{-4,0,0}, {4,0,0}}},
              {"od_mm", 1}, {"min_bend_radius_mm", 1}, {"clearance_mm", 0.5}};
    auto r = harness_path_check(p, box_scene(-1, 1));
    REQUIRE(r["ok"] == true);
    REQUIRE(r["clearance_violations"].size() == 1);
    const auto& crossing = r["clearance_violations"][0];
    CHECK(crossing["body"] == 7);
    REQUIRE(crossing["at"].is_array());
    CHECK(std::abs(crossing["at"][0].get<double>()) == doctest::Approx(1));
    CHECK(crossing["at"][1].get<double>() == doctest::Approx(0));
    CHECK(crossing["distance_mm"].get<double>() == doctest::Approx(0));
    p["path"] = {{-0.5,0,0}, {0.5,0,0}};
    r = harness_path_check(p, box_scene(-1, 1));
    REQUIRE(r["ok"] == true);
    REQUIRE(r["clearance_violations"].size() == 1);
    CHECK(r["clearance_violations"][0]["body"] == 7);
    CHECK(r["clearance_violations"][0]["reason"] == "centerline_inside_solid");
    CHECK(r["min_clearance_mm"].get<double>() == doctest::Approx(0));
}

TEST_CASE("path check identifies farther owner with stricter clearance") {
    auto scene = box_scene(-1, 1);
    auto second = scene.bodies.front();
    second.id = 8;
    second.name = "farther";
    for (size_t i = 1; i < second.pos.size(); i += 3) second.pos[i] += 4;
    scene.bodies.push_back(second);
    const json p = {{"path", {{-4,1.8,0}, {4,1.8,0}}}, {"od_mm", 1},
                    {"min_bend_radius_mm", 1}, {"clearance_mm", 0},
                    {"keepouts", {{{"body", 8}, {"clearance_mm", 1}}}}};
    const auto r = harness_path_check(p, scene);
    REQUIRE(r["ok"] == true);
    REQUIRE(r["clearance_violations"].size() == 1);
    const auto& violation = r["clearance_violations"][0];
    CHECK(violation["body"] == 8);
    REQUIRE(violation["at"].is_array());
    CHECK(violation["at"][1].get<double>() == doctest::Approx(1.8));
    CHECK(violation["distance_mm"].get<double>() == doctest::Approx(0.7));
}

TEST_CASE("route refuses a path from a closed cavity to the exterior") {
    auto shell = box_scene(-5, 5);
    const auto inner = box_scene(-3, 3).bodies.front();
    auto& body = shell.bodies.front();
    const auto offset = static_cast<uint32_t>(body.pos.size() / 3);
    body.pos.insert(body.pos.end(), inner.pos.begin(), inner.pos.end());
    for (const auto index : inner.idx) body.idx.push_back(index + offset);
    auto p = request();
    p["start"]["pos"] = {0,0,0};
    p["end"]["pos"] = {10,0,0};
    auto r = harness_route(p, shell);
    CHECK(r["ok"] == false);
    CHECK(r["code"] == "no_path");
}

TEST_CASE("path check passes exact arc samples at the required radius") {
    // Zero-slack requirement: the measured radius equals R up to floating-point noise.
    // One-, two- and many-chord arcs, with unequal legs on both sides.
    const struct { double deg; int steps; } arcs[] = {{8, 1}, {18, 2}, {30, 3}, {90, 9}, {170, 17}};
    for (const auto& arc : arcs) {
        for (double R : {0.2, 5.0, 37.5}) {
            CAPTURE(arc.deg); CAPTURE(arc.steps); CAPTURE(R);
            const auto r = bend_check(arc_path(R, arc.deg, arc.steps, R * 1.7 + 1, R * 9 + 3), R);
            REQUIRE(r["ok"] == true);
            CHECK(r["bend_violations"].empty());
            CHECK(r["min_bend_radius_found_mm"].get<double>() ==
                  doctest::Approx(R).epsilon(1e-6));
            CHECK(r["bend_tolerance_rel"].get<double>() == doctest::Approx(1e-6));
        }
    }
    // A genuinely tighter arc is still reported.
    const auto tight = bend_check(arc_path(4.99, 90, 9, 20, 20), 5);
    REQUIRE(tight["ok"] == true);
    CHECK(tight["bend_violation_count"].get<int>() > 0);
}

TEST_CASE("path check verdict is invariant to points inserted mid-chord") {
    const struct { double deg; int steps; } arcs[] = {{8, 1}, {18, 2}, {90, 9}};
    for (const auto& arc : arcs) {
        for (int chord = -1; chord < arc.steps; ++chord) {
            CAPTURE(arc.deg); CAPTURE(chord);
            const auto r = bend_check(arc_path(5, arc.deg, arc.steps, 12, 40, chord), 5);
            REQUIRE(r["ok"] == true);
            CHECK(r["bend_violations"].empty());
            CHECK(r["min_bend_radius_found_mm"].get<double>() == doctest::Approx(5).epsilon(1e-6));
        }
    }
    // The same holds for a violation: it is reported once, at the caller's original index.
    const auto path = arc_path(3, 90, 9, 12, 40, 4);
    const auto r = bend_check(path, 5);
    REQUIRE(r["ok"] == true);
    CHECK(r["bend_violation_count"].get<int>() > 0);
    for (const auto& bend : r["bend_violations"]) {
        const auto index = bend["index"].get<size_t>();
        REQUIRE(index < path.size());
        CHECK(point(bend["at"]).x == doctest::Approx(point(path[index]).x));
        CHECK(point(bend["at"]).y == doctest::Approx(point(path[index]).y));
    }
}

TEST_CASE("path check keeps a sharp corner an isolated kink, with collinear points ignored") {
    // Extra points on both legs must not turn the corner into an "arc" or move its index.
    json p = {{"path", {{-100,0,0}, {-50,0,0}, {-20,0,0}, {0,0,0}, {0,25,0}, {0,100,0}}},
              {"od_mm", 1}, {"min_bend_radius_mm", 10}};
    const auto r = harness_path_check(p, {});
    REQUIRE(r["ok"] == true);
    REQUIRE(r["bend_violations"].size() == 1);
    CHECK(r["bend_violations"][0]["index"] == 3);
    CHECK(r["bend_violations"][0]["kind"] == "isolated_kink");
    CHECK(r["bend_violations"][0]["radius_mm"].get<double>() == doctest::Approx(0));
    CHECK(r["bend_violations"][0]["available_fillet_radius_mm"].get<double>() == doctest::Approx(100));
}

TEST_CASE("router arcs satisfy path check at the radius the router used") {
    json p = {{"start", {{"pos", {0,0,0}}, {"dir", {1,0,0}}}},
              {"end", {{"pos", {80,40,0}}, {"dir", {0,-1,0}}}},
              {"od_mm", 1.9}, {"min_bend_radius_mm", 5}, {"clearance_mm", 0.3}};
    const auto route = harness_route(p, {});
    REQUIRE(route["ok"] == true);
    REQUIRE(route["bends"].size() == 2);
    const auto r = harness_path_check({{"path", route["path"]}, {"od_mm", 1.9},
                                       {"min_bend_radius_mm", 5}}, {});
    REQUIRE(r["ok"] == true);
    CHECK(r["bend_violations"].empty());
}

TEST_CASE("zero-area display triangles are skipped with a per-body warning") {
    auto scene = box_scene(-1, 1);
    scene.bodies.front().idx.insert(scene.bodies.front().idx.end(), {0, 0, 1});
    SceneBody flat;
    flat.id = 9; flat.name = "flat";
    flat.pos = {30,30,30, 31,30,30, 32,30,30};
    flat.idx = {0, 1, 2};
    scene.bodies.push_back(flat);
    auto p = request();
    const auto clean = harness_route(p, box_scene(-1, 1));
    REQUIRE(clean["ok"] == true);
    const auto r = harness_route(p, scene);
    REQUIRE(r["ok"] == true);
    CHECK(r["length_mm"].get<double>() > 20);
    CHECK(r["warnings"].size() == clean["warnings"].size() + 2);  // one per body with skipped triangles

    const auto c = harness_path_check({{"path", {{-4,0,0}, {4,0,0}}}, {"od_mm", 1},
                                       {"min_bend_radius_mm", 1}, {"clearance_mm", 0.5}}, scene);
    REQUIRE(c["ok"] == true);
    CHECK(c["clearance_violation_count"] == 1);  // the real box is still an obstacle
    CHECK(c["warnings"].size() == 2);
}

TEST_CASE("scene body errors name the body as name (#id)") {
    auto scene = box_scene(-1, 1);
    scene.bodies.front().idx.push_back(99);  // index list no longer a multiple of 3: malformed geometry
    auto r = harness_route(request(), scene);
    REQUIRE(r["ok"] == false);
    CHECK(r["code"] == "bad_args");
    CHECK(r["error"].get<std::string>().find("box (#7)") != std::string::npos);
}

TEST_CASE("blocked endpoint replies name the connector, obstacle and distances") {
    auto p = request();
    p["start"]["pos"] = {-1.5, 0, 0};  // 0.5 mm from the box, 1.0 mm (clearance + od/2) required
    auto r = harness_route(p, box_scene(-1, 1));
    REQUIRE(r["ok"] == false);
    CHECK(r["code"] == "start_blocked");
    CHECK(r["endpoint"] == "start");
    CHECK(r["obstacle"] == "box (#7)");
    CHECK(r["distance_mm"].get<double>() == doctest::Approx(0.5));
    CHECK(r["required_mm"].get<double>() == doctest::Approx(1.0));
    CHECK(r["inside_solid"] == false);
    CHECK(r["pos"] == json::array({-1.5, 0.0, 0.0}));

    p = request();
    p["end"]["pos"] = {0.2, 0.1, 0};
    r = harness_route(p, box_scene(-1, 1));
    REQUIRE(r["ok"] == false);
    CHECK(r["code"] == "end_blocked");
    CHECK(r["endpoint"] == "end");
    CHECK(r["inside_solid"] == true);
    CHECK(r["distance_mm"].get<double>() == doctest::Approx(0));
    CHECK(r["obstacle"] == "box (#7)");
}

TEST_CASE("blocked exit stub reports free length and obstacle, independent of the far endpoint") {
    auto p = request();
    p["min_bend_radius_mm"] = 2;  // stub = 2 x R = 4 mm
    p["start"] = {{"pos", {-3, 0, 0}}, {"dir", {1, 0, 0}}};  // the box envelope begins at x = -2
    auto r = harness_route(p, box_scene(-1, 1));
    REQUIRE(r["ok"] == false);
    CHECK(r["code"] == "exit_blocked");
    CHECK(r["endpoint"] == "start");
    CHECK(r["dir"] == json::array({1.0, 0.0, 0.0}));
    CHECK(r["stub_mm"].get<double>() == doctest::Approx(4));
    CHECK(r["free_mm"].get<double>() <= 1.0 + 1e-6);
    CHECK(r["free_mm"].get<double>() >= 0.99);
    CHECK(r["obstacle"] == "box (#7)");
    // Moving the far endpoint (hence the automatic grid) does not change the verdict.
    p["end"]["pos"] = {10, 300, 0};
    r = harness_route(p, box_scene(-1, 1));
    CHECK(r["code"] == "exit_blocked");

    p = request();
    p["min_bend_radius_mm"] = 2;
    p["end"] = {{"pos", {3, 0, 0}}, {"dir", {-1, 0, 0}}};
    r = harness_route(p, box_scene(-1, 1));
    REQUIRE(r["ok"] == false);
    CHECK(r["code"] == "exit_blocked");
    CHECK(r["endpoint"] == "end");
    CHECK(r["free_mm"].get<double>() >= 0.99);
}

TEST_CASE("exit stub is twice the bend radius and does not grow with the grid") {
    auto p = request();
    p["start"] = {{"pos", {-10, 0, 0}}, {"dir", {0, 1, 0}}};
    p["end"] = {{"pos", {10, 0, 0}}, {"dir", {0, 1, 0}}};
    p["grid_mm"] = 10;
    // A wall 5 mm above the endpoints would block a grid-sized (20 mm) stub, not a 0.6 mm one.
    p["keepouts"] = json::array({json{{"aabb", json::array({json::array({-20, 5, -1}),
                                                             json::array({20, 6, 1})})}}});
    const auto r = harness_route(p, {});
    REQUIRE(r["ok"] == true);
    CHECK(r["exit_stub_mm"].get<double>() == doctest::Approx(0.6));
}

TEST_CASE("junction corners are judged by the fillet the adjacent runs can hold") {
    auto check = [](json path, json junctions, double min_bend) {
        json p = {{"path", std::move(path)}, {"od_mm", 1}, {"min_bend_radius_mm", min_bend}};
        if (!junctions.is_null()) p["junctions"] = std::move(junctions);
        return harness_path_check(p, {});
    };
    // (a) 90-degree corner at a junction, both runs >= R: passes with the available fillet.
    auto r = check({{-20,0,0}, {0,0,0}, {0,20,0}}, {1}, 5);
    REQUIRE(r["ok"] == true);
    CHECK(r["bend_violations"].empty());
    CHECK(r["min_bend_radius_found_mm"].get<double>() == doctest::Approx(20));
    // (b) a run shorter than R fails as a junction fillet reporting the available radius.
    r = check({{-3,0,0}, {0,0,0}, {0,20,0}}, {1}, 5);
    REQUIRE(r["ok"] == true);
    REQUIRE(r["bend_violations"].size() == 1);
    CHECK(r["bend_violations"][0]["kind"] == "junction_fillet");
    CHECK(r["bend_violations"][0]["index"] == 1);
    CHECK(r["bend_violations"][0]["radius_mm"].get<double>() == doctest::Approx(3));
    CHECK(r["bend_violations"][0]["available_fillet_radius_mm"].get<double>() == doctest::Approx(3));
    // Exactly R passes (tolerance).
    r = check({{-5,0,0}, {0,0,0}, {0,20,0}}, {1}, 5);
    CHECK(r["bend_violations"].empty());
    // (c) the same corner without the junction index stays an isolated kink.
    r = check({{-20,0,0}, {0,0,0}, {0,20,0}}, nullptr, 5);
    REQUIRE(r["bend_violations"].size() == 1);
    CHECK(r["bend_violations"][0]["kind"] == "isolated_kink");
    CHECK(r["bend_violations"][0]["radius_mm"].get<double>() == doctest::Approx(0));
    // (d) a junction index on a collinear vertex is no finding.
    r = check({{-20,0,0}, {-10,0,0}, {0,0,0}}, {1}, 5);
    REQUIRE(r["ok"] == true);
    CHECK(r["bend_violations"].empty());
    CHECK(r["min_bend_radius_found_mm"].is_null());
    // Runs are measured after collinear filtering; duplicates are ignored.
    r = check({{-8,0,0}, {-5,0,0}, {0,0,0}, {0,4,0}, {0,10,0}}, {2, 2}, 12);
    REQUIRE(r["bend_violations"].size() == 1);
    CHECK(r["bend_violations"][0]["index"] == 2);
    CHECK(r["bend_violations"][0]["radius_mm"].get<double>() == doctest::Approx(8));
    CHECK(r["bend_violations"][0]["kind"] == "junction_fillet");
    // Corners at consecutive junction vertices share the run between them (half each):
    // the 6 mm middle run offers 3 mm per corner, below R = 5.
    r = check({{-20,0,0}, {0,0,0}, {0,6,0}, {20,6,0}}, {1, 2}, 5);
    REQUIRE(r["bend_violations"].size() == 2);
    CHECK(r["bend_violations"][0]["radius_mm"].get<double>() == doctest::Approx(3));
    CHECK(r["bend_violations"][1]["radius_mm"].get<double>() == doctest::Approx(3));
    // ...whereas as plain kinks they are not shared, and at a single junction the full run counts.
    r = check({{-20,0,0}, {0,0,0}, {0,6,0}, {20,6,0}}, {1}, 5);
    REQUIRE(r["ok"] == true);
    CHECK(r["min_bend_radius_found_mm"].get<double>() == doctest::Approx(0));  // vertex 2 is a kink
    CHECK(r["bend_violations"].size() == 1);
    CHECK(r["bend_violations"][0]["index"] == 2);
}

TEST_CASE("invalid junction indices are refused") {
    const json path = {{-20,0,0}, {0,0,0}, {0,20,0}};
    for (const json& bad : {json::array({0}), json::array({2}), json::array({3}), json::array({-1}),
                            json::array({1.5}), json::array({"1"}), json::array({true}),
                            json::array({json::array({1})}), json("1"), json(1)}) {
        CAPTURE(bad.dump());
        const auto r = harness_path_check({{"path", path}, {"od_mm", 1}, {"min_bend_radius_mm", 5},
                                           {"junctions", bad}}, {});
        CHECK(r["ok"] == false);
        CHECK(r["code"] == "bad_args");
    }
    const auto ok = harness_path_check({{"path", path}, {"od_mm", 1}, {"min_bend_radius_mm", 5},
                                        {"junctions", json::array()}}, {});
    CHECK(ok["ok"] == true);
}

TEST_CASE("an end inside the start's minimum turning circle routes with a detour that passes path check") {
    // The end is behind-left of a +X exit: the turn at the end of the 2R stub would be ~131.5
    // degrees, whose trim (6.6 mm) exceeds the 6 mm stub, so the direct shot and every lattice
    // shortcut are infeasible. A feasible route must loop out past the stub.
    const json base = {{"start", {{"pos", {60, 30, 0}}, {"dir", {1, 0, 0}}}},
                       {"end", {{"pos", {61.4327, 24.8424, 0}}}},
                       {"od_mm", 1.3}, {"min_bend_radius_mm", 3}, {"clearance_mm", 0.3}};
    for (const bool keepout : {false, true}) {
        CAPTURE(keepout);
        auto p = base;
        if (keepout)
            p["keepouts"] = json::array({json{{"aabb", json::array({json::array({30, -10, -10}),
                                                                     json::array({50, 10, 10})})}}});
        const auto r = harness_route(p, {});
        REQUIRE(r["ok"] == true);
        CHECK(r["path"].front() == p["start"]["pos"]);
        CHECK(r["path"].back() == p["end"]["pos"]);
        const auto heading = unit(point(r["path"][1]) - point(r["path"][0]));
        CHECK(heading.x == doctest::Approx(1));
        CHECK(heading.y == doctest::Approx(0).epsilon(1e-9));
        REQUIRE_FALSE(r["bends"].empty());
        for (const auto& bend : r["bends"]) CHECK(bend["radius_mm"].get<double>() == doctest::Approx(3));

        json check = {{"path", r["path"]}, {"od_mm", 1.3}, {"min_bend_radius_mm", 3}, {"clearance_mm", 0.3}};
        if (keepout) check["keepouts"] = p["keepouts"];
        const auto c = harness_path_check(check, {});
        REQUIRE(c["ok"] == true);
        CHECK(c["bend_violations"].empty());
        CHECK(c["clearance_violations"].empty());
    }
}

TEST_CASE("a detour keeps the straight exit stub of both endpoints") {
    // Both 27.6 mm stubs end where the other endpoint's turning circle forbids a direct turn, so the
    // route loops out past the start's stub and comes back up the end's stub; replacing the corner at
    // the end of the end's stub would have cut across it into the end at a slant.
    const json base = {{"start", {{"pos", {60, 30, 0}}, {"dir", {1, 0, 0}}}},
                       {"end", {{"pos", {80, 40, 0}}, {"dir", {0, -1, 0}}}},
                       {"od_mm", 3.95}, {"min_bend_radius_mm", 13.82}, {"clearance_mm", 0.3}};
    for (const bool swapped : {false, true}) {
        CAPTURE(swapped);
        auto p = base;
        if (swapped) std::swap(p["start"], p["end"]);
        const auto r = harness_route(p, {});
        REQUIRE(r["ok"] == true);
        const auto& path = r["path"];
        const V3 leaving = unit(point(path[1]) - point(path[0]));
        const V3 arriving = unit(point(path[path.size() - 1]) - point(path[path.size() - 2]));
        CHECK(dot(leaving, point(p["start"]["dir"])) == doctest::Approx(1));
        CHECK(dot(arriving, point(p["end"]["dir"])) == doctest::Approx(-1));
        for (const auto& bend : r["bends"]) CHECK(bend["radius_mm"].get<double>() == doctest::Approx(13.82));

        const auto c = harness_path_check({{"path", path}, {"od_mm", 3.95}, {"min_bend_radius_mm", 13.82},
                                           {"clearance_mm", 0.3}}, {});
        REQUIRE(c["ok"] == true);
        CHECK(c["bend_violations"].empty());
    }
}

TEST_CASE("a large bend radius routes over a keepout and the path passes path check") {
    // Auto grid is R/2 = 5.6 mm and both stubs are 22.4 mm; the straight shot is blocked by the
    // keepout and every lattice shortcut hugging it is infeasible for R = 11.2.
    json p = {{"start", {{"pos", {0, 0, 0}}, {"dir", {1, 0, 0}}}},
              {"end", {{"pos", {80, 40, 0}}, {"dir", {0, -1, 0}}}},
              {"od_mm", 3.2}, {"min_bend_radius_mm", 11.2}, {"clearance_mm", 0.3}};
    p["keepouts"] = json::array({json{{"aabb", json::array({json::array({30, -10, -10}),
                                                             json::array({50, 10, 10})})}}});
    const auto r = harness_route(p, {});
    REQUIRE(r["ok"] == true);
    CHECK(r["path"].front() == p["start"]["pos"]);
    CHECK(r["path"].back() == p["end"]["pos"]);
    CHECK(r["exit_stub_mm"].get<double>() == doctest::Approx(22.4));
    CHECK(r["min_clearance_mm"].get<double>() >= 0.3 - 1e-7);
    for (const auto& bend : r["bends"]) CHECK(bend["radius_mm"].get<double>() == doctest::Approx(11.2));

    json check = {{"path", r["path"]}, {"od_mm", 3.2}, {"min_bend_radius_mm", 11.2},
                  {"clearance_mm", 0.3}, {"keepouts", p["keepouts"]}};
    const auto c = harness_path_check(check, {});
    REQUIRE(c["ok"] == true);
    CHECK(c["bend_violations"].empty());
    CHECK(c["clearance_violations"].empty());
}

TEST_CASE("report_range evaluates an arc cut by a split point with its context") {
    const json whole = arc_path(5, 90, 9, 12, 40);  // legstart, 10 arc samples, legend: 12 points
    auto slice = [&](size_t first, size_t last) {
        json part = json::array();
        for (size_t i = first; i <= last; ++i) part.push_back(whole[i]);
        return part;
    };
    // The tail half ends with the exit sample of the arc: alone, that single turn is a kink.
    auto r = bend_check(slice(9, 11), 5);
    REQUIRE(r["ok"] == true);
    REQUIRE(r["bend_violations"].size() == 1);
    CHECK(r["bend_violations"][0]["kind"] == "isolated_kink");
    CHECK(r["bend_violations"][0]["index"] == 1);
    // The head half's first turn is the same false kink.
    r = bend_check(slice(0, 2), 5);
    REQUIRE(r["ok"] == true);
    CHECK(r["bend_violations"].size() == 1);
    CHECK(r["bend_violations"][0]["kind"] == "isolated_kink");
    // With the other half as context and report_range, both halves pass.
    for (const auto& range : {json::array({9, 11}), json::array({0, 2})}) {
        CAPTURE(range.dump());
        const json p = {{"path", whole}, {"od_mm", 1}, {"min_bend_radius_mm", 5}, {"report_range", range}};
        r = harness_path_check(p, {});
        REQUIRE(r["ok"] == true);
        CHECK(r["bend_violations"].empty());
        CHECK(r["bend_violation_count"] == 0);
        CHECK(r["min_bend_radius_found_mm"].get<double>() == doctest::Approx(5).epsilon(1e-6));
    }
    // report_length_mm covers the range; length_mm stays the full path.
    const json p = {{"path", whole}, {"od_mm", 1}, {"min_bend_radius_mm", 5}, {"report_range", {9, 11}}};
    r = harness_path_check(p, {});
    double expected = 0, total = 0;
    for (size_t i = 0; i + 1 < whole.size(); ++i) {
        const V3 a = point(whole[i]), b = point(whole[i + 1]);
        const double leg = dist(a, b);
        total += leg;
        if (i >= 9) expected += leg;
    }
    CHECK(r["report_length_mm"].get<double>() == doctest::Approx(expected));
    CHECK(r["length_mm"].get<double>() == doctest::Approx(total));
}

TEST_CASE("report_range restricts bend and clearance findings to the range") {
    json p = {{"path", {{-100,0,0}, {0,0,0}, {0,100,0}}}, {"od_mm", 1}, {"min_bend_radius_mm", 10}};
    auto run = [&](json range) {
        auto q = p;
        q["report_range"] = std::move(range);
        return harness_path_check(q, {});
    };
    auto r = run({0, 2});
    CHECK(r["bend_violations"].size() == 1);
    r = run({1, 1});  // the corner vertex itself is inside the range
    CHECK(r["bend_violations"].size() == 1);
    r = run({2, 2});
    REQUIRE(r["ok"] == true);
    CHECK(r["bend_violations"].empty());
    CHECK(r["min_bend_radius_found_mm"].is_null());
    CHECK(r["report_length_mm"].get<double>() == doctest::Approx(0));
    r = run({0, 1});
    CHECK(r["bend_violations"].size() == 1);
    CHECK(r["report_length_mm"].get<double>() == doctest::Approx(100));

    // Clearance: only legs wholly inside the range are measured.
    json c = {{"path", {{-4,0,0}, {4,0,0}, {4,5,0}}}, {"od_mm", 1}, {"min_bend_radius_mm", 1},
              {"clearance_mm", 0.5}};
    auto scene = box_scene(-1, 1);
    r = harness_path_check(c, scene);
    CHECK(r["clearance_violation_count"] == 1);
    c["report_range"] = {1, 2};
    r = harness_path_check(c, scene);
    REQUIRE(r["ok"] == true);
    CHECK(r["clearance_violations"].empty());
    CHECK(r["min_clearance_mm"].get<double>() > 1.0);
    c["report_range"] = {0, 1};
    r = harness_path_check(c, scene);
    CHECK(r["clearance_violation_count"] == 1);
    c["report_range"] = {0, 0};  // no whole leg inside: nothing measured
    r = harness_path_check(c, scene);
    CHECK(r["clearance_violations"].empty());
    CHECK(r["min_clearance_mm"].is_null());
}

TEST_CASE("invalid report_range is refused") {
    const json path = {{-20,0,0}, {0,0,0}, {0,20,0}};
    for (const json& bad : {json::array({1}), json::array({0, 1, 2}), json::array({2, 1}),
                            json::array({0, 3}), json::array({-1, 1}), json::array({0.5, 1}),
                            json::array({"0", 1}), json::array({true, 1}), json("0,1"), json(1)}) {
        CAPTURE(bad.dump());
        const auto r = harness_path_check({{"path", path}, {"od_mm", 1}, {"min_bend_radius_mm", 5},
                                           {"report_range", bad}}, {});
        CHECK(r["ok"] == false);
        CHECK(r["code"] == "bad_args");
    }
    const auto ok = harness_path_check({{"path", path}, {"od_mm", 1}, {"min_bend_radius_mm", 5},
                                        {"report_range", json::array({0, 2})}}, {});
    CHECK(ok["ok"] == true);
}
