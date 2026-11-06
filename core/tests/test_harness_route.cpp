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
    p["start"]["dir"] = {-1, 0, 0};
    r = harness_route(p, box_scene(-1, 1));
    CHECK(r["ok"] == false);
    CHECK(r["code"] == "no_path");
}

TEST_CASE("invalid routing inputs are refused") {
    auto p = request(); p["start"]["dir"] = {0,0,0};
    CHECK(harness_route(p, {})["code"] == "bad_args");
    p = request(); p["min_bend_radius_mm"] = -1;
    CHECK(harness_route(p, {})["code"] == "bad_args");
    p = request(); p["prefer_paths"] = json::array({json::array({json::array({0,0,0})})});
    CHECK(harness_route(p, {})["code"] == "bad_args");
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
