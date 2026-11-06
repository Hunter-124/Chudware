#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include "sketch/sketch.h"
#include "sketch/sketch_define.h"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <iostream>
#include <string>

using nlohmann::json;
using chudware::sketch::DefineOptions;
using chudware::sketch::DefineResult;
using chudware::sketch::fully_define;
using chudware::sketch::solve_sketch;

namespace {
json line(const std::string& id, double x1, double y1, double x2, double y2) {
    return {{"id", id}, {"type", "line"}, {"p1", {x1, y1}}, {"p2", {x2, y2}}};
}
json circle(const std::string& id, double x, double y, double r) {
    return {{"id", id}, {"type", "circle"}, {"center", {x, y}}, {"radius", r}};
}
json arc(const std::string& id, double x, double y, double r, double start, double end) {
    return {{"id", id}, {"type", "arc"}, {"center", {x, y}}, {"radius", r},
            {"start", start}, {"end", end}, {"ccw", true}};
}
json point(const std::string& id, double x, double y) {
    return {{"id", id}, {"type", "point"}, {"p", {x, y}}};
}
json sketch(json::initializer_list_t entities) {
    // A singleton {json-returning-call()} can copy the object in C++17 instead of wrapping it.
    return {{"entities", json::array(entities)}, {"constraints", json::array()}};
}
json rectangle(double x = 13, double y = 17) {
    return sketch({line("e1", x, y, x + 40, y), line("e2", x + 40, y, x + 40, y + 20),
                   line("e3", x + 40, y + 20, x, y + 20), line("e4", x, y + 20, x, y)});
}
json vase(const char* type = "spline") {
    auto result = sketch({json{{"id", "e1"}, {"type", type},
                              {"points", {{0, 0}, {15, 0}, {18, 20}, {10, 45}, {12, 60}, {0, 60}}}},
                          line("e2", 0, 60, 0, 0)});
    result["constraints"] = {
        {{"id", "c1"}, {"type", "coincident"}, {"refs", {"e1.start", "e2.p2"}}},
        {{"id", "c2"}, {"type", "coincident"}, {"refs", {"e1.end", "e2.p1"}}},
        {{"id", "c3"}, {"type", "coincident"}, {"refs", {"e2.p2", "origin"}}},
        {{"id", "c4"}, {"type", "vertical"}, {"refs", {"e2"}}},
        {{"id", "c5"}, {"type", "length"}, {"refs", {"e2"}}, {"value", {{"value", 60}, {"expr", nullptr}}}}
    };
    return result;
}
std::size_t count(const DefineResult& result, const char* type) {
    return std::count_if(result.constraints.begin(), result.constraints.end(),
                         [&](const json& c) { return c.at("type") == type; });
}
json append_result(json input, const DefineResult& result) {
    int index = 0;
    for (auto c : result.constraints) {
        CHECK_FALSE(c.contains("id"));
        if (c.contains("value")) {
            REQUIRE(c.at("value").contains("value"));
            REQUIRE(c.at("placement").is_array());
            REQUIRE(c.at("placement").size() == 2);
            CHECK(std::isfinite(c["value"]["value"].get<double>()));
        }
        c["id"] = "added" + std::to_string(index++);
        input["constraints"].push_back(std::move(c));
    }
    return input;
}
void check_coordinates(const json& a, const json& b, double tolerance = 1e-7) {
    if (a.is_number()) {
        REQUIRE(b.is_number());
        CHECK(b.get<double>() == doctest::Approx(a.get<double>()).epsilon(tolerance).scale(1));
    } else if (a.is_array()) {
        REQUIRE(b.is_array());
        REQUIRE(a.size() == b.size());
        for (std::size_t i = 0; i < a.size(); ++i) check_coordinates(a[i], b[i], tolerance);
    } else if (a.is_object()) {
        REQUIRE(b.is_object());
        for (auto it = a.begin(); it != a.end(); ++it) {
            REQUIRE(b.contains(it.key()));
            check_coordinates(it.value(), b.at(it.key()), tolerance);
        }
    } else CHECK(a == b);
}
void check_result(const json& input, const DefineResult& result) {
    auto before = solve_sketch(input, json::object());
    auto after = solve_sketch(append_result(input, result), json::object());
    REQUIRE(after.converged);
    CHECK(after.conflicting.empty());
    CHECK(after.redundant.empty());
    CHECK(result.dof_before == before.dof);
    CHECK(result.dof_after == after.dof);
    check_coordinates(before.entities, after.entities);
}
void check_each_addition(json input, const DefineResult& result) {
    auto previous = solve_sketch(input, json::object());
    const auto original = previous.entities;
    int index = 0;
    for (auto c : result.constraints) {
        c["id"] = "incremental" + std::to_string(index++);
        input["constraints"].push_back(std::move(c));
        auto trial = solve_sketch(input, json::object());
        REQUIRE(trial.converged);
        CHECK(trial.conflicting.empty());
        CHECK(trial.redundant.empty());
        CHECK(trial.dof < previous.dof);
        check_coordinates(original, trial.entities);
        previous = std::move(trial);
    }
}
}  // namespace

TEST_CASE("fully define rectangle uses geometric relations and four sensible dimensions") {
    auto input = rectangle();
    const auto unchanged = input;
    auto result = fully_define(input, json::object());
    CHECK(input == unchanged);
    CHECK(result.dof_before == 16);
    CHECK(result.dof_after == 0);
    CHECK(count(result, "coincident") == 4);
    CHECK(count(result, "horizontal") == 2);
    CHECK(count(result, "vertical") == 2);
    CHECK(count(result, "distance") == 4);
    int lengths = 0, locations = 0;
    for (const auto& c : result.constraints) {
        if (c.at("type") != "distance") continue;
        if (c.at("refs").size() == 1) ++lengths;
        else {
            ++locations;
            CHECK(c.at("refs")[0] == "origin");
            CHECK((c.at("axis") == "h" || c.at("axis") == "v"));
        }
    }
    CHECK(lengths == 2);
    CHECK(locations == 2);
    check_result(input, result);
    check_each_addition(input, result);
    CHECK(fully_define(input, json::object()).constraints == result.constraints);
    CHECK(fully_define(append_result(input, result), json::object()).constraints.empty());
}

TEST_CASE("fully define centered rectangle preserves symmetry with two size dimensions") {
    auto input = rectangle(-20, -10);
    auto result = fully_define(input, json::object());
    INFO("Centered rectangle constraints: " << result.constraints.dump());
    CHECK(result.dof_after == 0);
    CHECK(count(result, "symmetric") > 0);
    CHECK(count(result, "distance") == 2);
    for (const auto& c : result.constraints) {
        if (c.at("type") == "distance") CHECK(c.at("refs").size() == 1);
        for (const auto& ref : c.at("refs")) CHECK(ref != "origin");
    }
    check_result(input, result);
    check_each_addition(input, result);
    CHECK(fully_define(input, json::object()).constraints == result.constraints);
    CHECK(fully_define(append_result(input, result), json::object()).constraints.empty());
}

TEST_CASE("fully define centers an authored h/v rectangle with edge midpoints on the axes") {
    // The rectangle tool authors coincident + h/v itself; point-pair symmetry would then
    // be partially redundant, so centering must come from edge midpoints on the axes.
    auto input = rectangle(-20, -10);
    const char* hv[] = {"horizontal", "vertical", "horizontal", "vertical"};
    for (int i = 0; i < 4; ++i) {
        const auto id = "e" + std::to_string(i + 1), next = "e" + std::to_string((i + 1) % 4 + 1);
        input["constraints"].push_back({{"id", "c" + std::to_string(i)}, {"type", "coincident"},
                                        {"refs", {id + ".p2", next + ".p1"}}});
        input["constraints"].push_back({{"id", "o" + std::to_string(i)}, {"type", hv[i]}, {"refs", {id}}});
    }
    auto result = fully_define(input, json::object());
    INFO("Authored rectangle constraints: " << result.constraints.dump());
    CHECK(result.dof_after == 0);
    CHECK(count(result, "point_on") == 2);
    CHECK(count(result, "distance") == 2);
    for (const auto& c : result.constraints) {
        if (c.at("type") == "point_on") CHECK(c.at("refs")[0].get<std::string>().find(".mid") != std::string::npos);
        for (const auto& ref : c.at("refs")) CHECK(ref != "origin");
    }
    check_result(input, result);
    CHECK(fully_define(append_result(input, result), json::object()).constraints.empty());
}

TEST_CASE("fully define four mirrored holes uses one diameter and full pattern spacing") {
    auto input = sketch({circle("e1", -30, -20, 4), circle("e2", 30, -20, 4),
                         circle("e3", -30, 20, 4), circle("e4", 30, 20, 4)});
    auto result = fully_define(input, json::object());
    INFO("Four-hole constraints: " << result.constraints.dump());
    CHECK(result.dof_after == 0);
    CHECK(count(result, "diameter") == 1);
    CHECK(count(result, "symmetric") > 0);
    CHECK(count(result, "distance") == 2);
    bool horizontal_spacing = false, vertical_spacing = false;
    for (const auto& c : result.constraints) {
        for (const auto& ref : c.at("refs")) CHECK(ref != "origin");
        if (c.at("type") != "distance") continue;
        REQUIRE(c.at("refs").size() == 2);
        if (c.at("axis") == "h") {
            CHECK(c.at("value").at("value").get<double>() == doctest::Approx(60));
            horizontal_spacing = true;
        } else if (c.at("axis") == "v") {
            CHECK(c.at("value").at("value").get<double>() == doctest::Approx(40));
            vertical_spacing = true;
        }
    }
    CHECK(horizontal_spacing);
    CHECK(vertical_spacing);
    check_result(input, result);
    check_each_addition(input, result);
    CHECK(fully_define(input, json::object()).constraints == result.constraints);
    CHECK(fully_define(append_result(input, result), json::object()).constraints.empty());
}

TEST_CASE("fully define circle on y axis uses point-on rather than zero location") {
    auto input = sketch({circle("e1", 0, 30, 4)});
    auto result = fully_define(input, json::object());
    INFO("Axis-centered circle constraints: " << result.constraints.dump());
    CHECK(result.dof_after == 0);
    CHECK(count(result, "point_on") == 1);
    CHECK(count(result, "diameter") == 1);
    CHECK(count(result, "distance") == 1);
    for (const auto& c : result.constraints) {
        if (c.at("type") == "point_on") CHECK(c.at("refs") == json({"e1.center", "axis.v"}));
        if (c.at("type") == "distance") {
            CHECK(c.at("axis") == "v");
            CHECK(c.at("value").at("value").get<double>() == doctest::Approx(30));
        }
    }
    check_result(input, result);
    check_each_addition(input, result);
}

TEST_CASE("fully define a row of holes aligns centers with a relation chain") {
    auto input = sketch({circle("e1", 10, 10, 4), circle("e2", 30, 10, 4), circle("e3", 50, 10, 4)});
    auto result = fully_define(input, json::object());
    INFO("Aligned-hole constraints: " << result.constraints.dump());
    CHECK(result.dof_after == 0);
    CHECK(count(result, "horizontal") == 2);
    CHECK(count(result, "diameter") == 1);
    int vertical_locations = 0;
    for (const auto& c : result.constraints) {
        if (c.at("type") == "horizontal") REQUIRE(c.at("refs").size() == 2);
        if (c.at("type") == "distance" && c.value("axis", "") == "v") ++vertical_locations;
    }
    CHECK(vertical_locations == 1);
    check_result(input, result);
    check_each_addition(input, result);
    CHECK(fully_define(input, json::object()).constraints == result.constraints);
    CHECK(fully_define(append_result(input, result), json::object()).constraints.empty());
}

TEST_CASE("fully define preserves existing equal radii when adding circle symmetry") {
    auto input = sketch({circle("e1", -30, 20, 4), circle("e2", 30, 20, 4)});
    input["constraints"].push_back({{"id", "equal"}, {"type", "equal"}, {"refs", {"e1", "e2"}}});
    auto result = fully_define(input, json::object());
    CHECK(result.dof_after == 0);
    CHECK(count(result, "symmetric") == 1);
    for (const auto& c : result.constraints)
        if (c.at("type") == "symmetric")
            CHECK(c.at("refs") == json({"e1.center", "e2.center", "axis.v"}));
    check_result(input, result);
    check_each_addition(input, result);
}

TEST_CASE("fully define axis inference includes construction endpoints and selected points") {
    DefineOptions opt;
    opt.dimensions = false;
    SUBCASE("construction endpoints") {
        auto input = sketch({line("e1", -20, 10, 20, 10)});
        input["entities"][0]["construction"] = true;
        auto result = fully_define(input, json::object(), opt);
        CHECK(count(result, "symmetric") == 1);
        CHECK(count(result, "horizontal") == 0);
        check_result(input, result);
    }
    SUBCASE("selected point with an unselected mirror") {
        auto input = sketch({point("e1", -20, 10), point("e2", 20, 10), point("e3", 0, 40)});
        opt.only_entities = {"e1"};
        auto result = fully_define(input, json::object(), opt);
        CHECK(count(result, "symmetric") == 1);
        CHECK(count(result, "point_on") == 0);
        for (const auto& c : result.constraints)
            for (const auto& ref : c.at("refs")) CHECK(ref != "e3");
        check_result(input, result);
    }
    SUBCASE("origin-free centered geometry") {
        auto input = rectangle(-20, -10);
        opt.anchor_origin = false;
        auto result = fully_define(input, json::object(), opt);
        CHECK(result.dof_after == 4);
        CHECK(count(result, "symmetric") == 0);
        for (const auto& c : result.constraints)
            for (const auto& ref : c.at("refs")) {
                CHECK(ref != "axis.v");
                CHECK(ref != "axis.h");
            }
        check_result(input, result);
    }
    SUBCASE("near symmetry must not move geometry") {
        auto input = sketch({line("e1", -20, 10, 20.0008, 10)});
        auto result = fully_define(input, json::object(), opt);
        CHECK(count(result, "symmetric") == 0);
        check_result(input, result);
    }
}

TEST_CASE("fully define off-origin circle uses diameter and center location") {
    auto input = sketch({circle("e1", -23, 41, 7)});
    auto result = fully_define(input, json::object());
    CHECK(result.dof_after == 0);
    CHECK(count(result, "diameter") == 1);
    CHECK(count(result, "radius") == 0);
    CHECK(count(result, "distance") == 2);
    check_result(input, result);
}

TEST_CASE("fully define slot keeps endpoint tangencies and equal cap radii") {
    auto input = sketch({line("e1", 10, 15, 40, 15), arc("e2", 40, 20, 5, 270, 90),
                         line("e3", 40, 25, 10, 25), arc("e4", 10, 20, 5, 90, 270)});
    auto result = fully_define(input, json::object());
    CHECK(result.dof_after == 0);
    CHECK(count(result, "coincident") == 4);
    CHECK(count(result, "tangent") >= 2);
    bool equal_radii = false;
    for (const auto& c : result.constraints)
        if (c.at("type") == "equal" &&
            ((c.at("refs")[0] == "e2" && c.at("refs")[1] == "e4") ||
             (c.at("refs")[0] == "e4" && c.at("refs")[1] == "e2"))) equal_radii = true;
    CHECK(equal_radii);
    check_result(input, result);
}

TEST_CASE("fully define never introduces origin references when anchoring is disabled") {
    DefineOptions opt;
    opt.anchor_origin = false;
    auto input = rectangle(0, 0);
    auto result = fully_define(input, json::object(), opt);
    CHECK(result.dof_after == 2);
    for (const auto& c : result.constraints)
        for (const auto& ref : c.at("refs")) CHECK(ref != "origin");
    CHECK_FALSE(result.notes.empty());
    check_result(input, result);
}

TEST_CASE("fully define uses an existing fixed datum without origin") {
    auto input = sketch({point("datum", -12, 9), circle("e1", 18, -7, 3)});
    input["constraints"].push_back({{"id", "fixed"}, {"type", "fix"}, {"refs", {"datum"}}});
    DefineOptions opt;
    opt.anchor_origin = false;
    auto result = fully_define(input, json::object(), opt);
    CHECK(result.dof_after == 0);
    for (const auto& c : result.constraints)
        for (const auto& ref : c.at("refs")) CHECK(ref != "origin");
    check_result(input, result);
}

TEST_CASE("fully define recognizes a fixed subhandle as a location datum") {
    auto input = sketch({circle("e1", 18, -7, 3), line("e2", -12, 9, -2, 9)});
    input["constraints"].push_back({{"id", "fixed"}, {"type", "fix"}, {"refs", {"e2.p1"}}});
    DefineOptions opt;
    opt.anchor_origin = false;
    auto result = fully_define(input, json::object(), opt);
    CHECK(result.dof_after == 0);
    for (const auto& c : result.constraints)
        for (const auto& ref : c.at("refs")) CHECK(ref != "origin");
    check_result(input, result);
}

TEST_CASE("fully define dimensions a sloped line with an angle rather than coordinate pinning") {
    auto input = sketch({line("e1", -7, 13, 23, 33)});
    auto result = fully_define(input, json::object());
    CHECK(result.dof_after == 0);
    CHECK(count(result, "angle") == 1);
    CHECK(count(result, "distance") == 3);
    CHECK(count(result, "fix") == 0);
    check_result(input, result);
}

TEST_CASE("fully define dimensions the solved shape rather than unsolved authored seeds") {
    auto input = sketch({line("e1", -7, 13, 23, 33)});
    input["constraints"].push_back({{"id", "length"}, {"type", "distance"},
                                    {"refs", {"e1"}}, {"value", {{"value", 50}}}});
    auto result = fully_define(input, json::object());
    CHECK(result.dof_after == 0);
    CHECK(count(result, "angle") == 1);
    check_result(input, result);
    check_each_addition(input, result);
}

TEST_CASE("fully define origin coincidence and selective inference") {
    auto input = sketch({circle("e1", 0, 0, 4), circle("e2", 30, 17, 9)});
    DefineOptions opt;
    opt.only_entities = {"e1"};
    auto result = fully_define(input, json::object(), opt);
    CHECK(result.dof_after == 3);
    CHECK(count(result, "coincident") == 1);
    for (const auto& c : result.constraints)
        for (const auto& ref : c.at("refs")) CHECK(ref.get<std::string>().find("e2") == std::string::npos);
    check_result(input, result);
}

TEST_CASE("fully define honors relation-only and dimension-only modes") {
    auto input = rectangle();
    DefineOptions opt;
    opt.dimensions = false;
    auto relations = fully_define(input, json::object(), opt);
    CHECK(relations.dof_after == 4);
    CHECK(count(relations, "distance") == 0);
    check_result(input, relations);
    opt.dimensions = true;
    opt.relations = false;
    auto dimensions = fully_define(sketch({circle("e1", 8, -6, 2)}), json::object(), opt);
    CHECK(dimensions.dof_after == 0);
    CHECK(count(dimensions, "diameter") == 1);
}

TEST_CASE("fully define infers concentric midpoint collinear and point-on relations") {
    DefineOptions opt;
    opt.dimensions = false;
    SUBCASE("concentric") {
        auto input = sketch({circle("e1", 10, 11, 3), circle("e2", 10, 11, 8)});
        auto result = fully_define(input, json::object(), opt);
        CHECK(count(result, "concentric") == 1);
        check_result(input, result);
    }
    SUBCASE("midpoint") {
        auto input = sketch({line("e1", 10, 11, 30, 21), point("e2", 20, 16)});
        auto result = fully_define(input, json::object(), opt);
        CHECK(count(result, "midpoint") == 1);
        check_result(input, result);
    }
    SUBCASE("collinear") {
        auto input = sketch({line("e1", 10, 11, 30, 21), line("e2", 50, 31, 70, 41)});
        auto result = fully_define(input, json::object(), opt);
        CHECK(count(result, "collinear") == 1);
        check_result(input, result);
    }
    SUBCASE("horizontal collinear") {
        auto input = sketch({line("e1", 10, 11, 30, 11), line("e2", 50, 11, 70, 11)});
        auto result = fully_define(input, json::object(), opt);
        CHECK(count(result, "collinear") == 1);
        check_result(input, result);
    }
    SUBCASE("parallel") {
        auto input = sketch({line("e1", 10, 11, 30, 21), line("e2", 50, 40, 80, 55)});
        auto result = fully_define(input, json::object(), opt);
        CHECK(count(result, "parallel") == 1);
        check_result(input, result);
    }
    SUBCASE("perpendicular") {
        auto input = sketch({line("e1", 10, 11, 30, 21), line("e2", 50, 40, 40, 60)});
        auto result = fully_define(input, json::object(), opt);
        CHECK(count(result, "perpendicular") == 1);
        check_result(input, result);
    }
    SUBCASE("arc endpoint tangent") {
        auto input = sketch({arc("e1", 10, 10, 5, 0, 90), arc("e2", 20, 10, 5, 180, 270)});
        auto result = fully_define(input, json::object(), opt);
        CHECK(count(result, "tangent") == 1);
        check_result(input, result);
    }
    SUBCASE("point_on") {
        auto input = sketch({line("e1", 10, 11, 30, 21), point("e2", 14, 13)});
        auto result = fully_define(input, json::object(), opt);
        CHECK(count(result, "point_on") == 1);
        check_result(input, result);
    }
}

TEST_CASE("fully define does not infer equal from merely similar lengths") {
    DefineOptions opt;
    opt.dimensions = false;
    auto input = sketch({circle("e1", 10, 11, 3), circle("e2", 40, 31, 3.0001)});
    auto result = fully_define(input, json::object(), opt);
    CHECK(count(result, "equal") == 0);
    check_result(input, result);
}

TEST_CASE("fully define rejects near-axis inference that would move a long line") {
    auto input = sketch({line("e1", 13, 17, 1013, 17.1)});
    auto result = fully_define(input, json::object());
    CHECK(count(result, "horizontal") == 0);
    check_result(input, result);
}

TEST_CASE("fully define dimensions spline through-points and construction geometry") {
    SUBCASE("spline") {
        auto input = sketch({json{{"id", "e1"}, {"type", "spline"},
                                  {"points", {{10, 11}, {20, 35}, {30, 16}, {40, 21}}}}});
        auto result = fully_define(input, json::object());
        CHECK(result.dof_after == 0);
        CHECK(result.notes.empty());
        check_result(input, result);
    }
    SUBCASE("construction line") {
        auto input = sketch({line("e1", 10, 11, 30, 11)});
        input["entities"][0]["construction"] = true;
        auto result = fully_define(input, json::object());
        CHECK(result.dof_after == 0);
        check_result(input, result);
    }
}

TEST_CASE("fully define vase profiles dimensions each free spline coordinate without moving the curve") {
    for (const char* type : {"spline", "style_spline"}) {
        CAPTURE(type);
        auto input = vase(type);
        auto result = fully_define(input, json::object());
        CHECK(result.dof_before == 8);
        CHECK(result.dof_after == 0);
        CHECK(result.notes.empty());
        CHECK(count(result, "distance") == 8);
        for (const auto& c : result.constraints) {
            CHECK(c.at("type") == "distance");
            CHECK((c.at("axis") == "h" || c.at("axis") == "v"));
            CHECK(c.at("refs")[0] == "origin");
            CHECK(c.at("refs")[1].get<std::string>().find("e1.p") == 0);
        }
        check_result(input, result);
        check_each_addition(input, result);
        CHECK(fully_define(input, json::object()).constraints == result.constraints);
        CHECK(fully_define(append_result(input, result), json::object()).constraints.empty());
    }
}

TEST_CASE("fully define spline points preserves existing driving coordinates") {
    auto input = vase();
    input["constraints"].push_back({{"id", "width"}, {"type", "distance"},
                                    {"refs", {"origin", "e1.p2"}}, {"axis", "h"},
                                    {"value", {{"value", 18}}}});
    auto result = fully_define(input, json::object());
    CHECK(result.dof_before == 7);
    CHECK(result.dof_after == 0);
    CHECK(count(result, "distance") == 7);
    for (const auto& c : result.constraints)
        CHECK_FALSE((c.at("refs")[1] == "e1.p2" && c.at("axis") == "h"));
    check_result(input, result);
    check_each_addition(input, result);
}

TEST_CASE("fully define spline coordinates honors selected entities and origin-free anchoring") {
    auto input = sketch({json{{"id", "e1"}, {"type", "style_spline"},
                              {"points", {{10, 11}, {20, 35}, {30, 16}, {40, 21}}}},
                          circle("e2", 70, 80, 5)});
    DefineOptions opt;
    opt.only_entities = {"e1"};
    opt.anchor_origin = false;
    auto result = fully_define(input, json::object(), opt);
    CHECK(result.dof_after == 5);  // spline's floating translation + untouched circle
    for (const auto& c : result.constraints) {
        CHECK(c.at("type") == "distance");
        for (const auto& ref : c.at("refs")) {
            CHECK(ref != "origin");
            CHECK(ref.get<std::string>().find("e2") == std::string::npos);
        }
    }
    check_result(input, result);
}

TEST_CASE("fully define reports free spline coordinates when dimensioning is disabled") {
    auto input = vase();
    DefineOptions opt;
    opt.dimensions = false;
    auto result = fully_define(input, json::object(), opt);
    CHECK(result.dof_after == 8);
    CHECK(std::any_of(result.notes.begin(), result.notes.end(), [](const std::string& note) {
        return note.find("spline e1 left free:") != std::string::npos;
    }));
    check_result(input, result);
}

TEST_CASE("fully define batches a 200-entity sketch") {
    auto input = sketch({});
    for (int i = 0; i < 50; ++i) {
        double x = i * 100 + 13, y = i * 100 + 17;
        double w = 10 + i * 0.01, h = 5 + i * 0.013;
        auto id = [&](int n) { return "e" + std::to_string(i * 4 + n); };
        input["entities"].push_back(line(id(1), x, y, x + w, y));
        input["entities"].push_back(line(id(2), x + w, y, x + w, y + h));
        input["entities"].push_back(line(id(3), x + w, y + h, x, y + h));
        input["entities"].push_back(line(id(4), x, y + h, x, y));
    }
    auto start = std::chrono::steady_clock::now();
    auto result = fully_define(input, json::object());
    double elapsed = std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - start).count();
    std::cout << "Fully Define 200 entities: " << elapsed << " ms\n";
    CHECK(result.dof_after == 0);
    auto solved = solve_sketch(append_result(input, result), json::object(), std::nullopt, std::nullopt, true);
    CHECK(solved.converged);
    CHECK(solved.dof == 0);
    CHECK(solved.conflicting.empty());
    CHECK(solved.redundant.empty());
}

TEST_CASE("fully define continues past redundant geometric relations and explains them") {
    auto input = sketch({line("e1", 0, 0, 10, 0)});
    input["constraints"] = json::array({
        {{"id", "c1"}, {"type", "horizontal"}, {"refs", {"e1"}}},
        {{"id", "c2"}, {"type", "horizontal"}, {"refs", {"e1"}}}
    });
    const auto result = fully_define(input, json::object());
    CHECK(result.dof_after < result.dof_before);
    CHECK_FALSE(result.constraints.empty());
    CHECK_FALSE(result.notes.empty());
    const auto solved = solve_sketch(append_result(input, result), json::object());
    CHECK(solved.converged);
    CHECK(solved.conflicting.empty());
}

TEST_CASE("fully define explains redundant driving dimensions instead of silently refusing") {
    auto input = sketch({line("e1", 0, 0, 10, 0)});
    input["constraints"] = json::array({
        {{"id", "c1"}, {"type", "length"}, {"refs", {"e1"}}, {"value", {{"value", 10}}}},
        {{"id", "c2"}, {"type", "length"}, {"refs", {"e1"}}, {"value", {{"value", 10}}}}
    });
    const auto result = fully_define(input, json::object());
    CHECK(result.constraints.empty());
    CHECK(result.dof_after == result.dof_before);
    CHECK_FALSE(result.notes.empty());
}

TEST_CASE("auto dimension rounding is optional and reports its change") {
    auto input = sketch({line("e1", 0, 0, 39.98, 0)});
    DefineOptions options;
    options.round_dimensions = true;
    const auto rounded = fully_define(input, json::object(), options);
    bool found = false;
    for (const auto& constraint : rounded.constraints)
        if (constraint.value("type", std::string()) == "distance" &&
            constraint.value("refs", json::array()) == json::array({"e1"})) {
            CHECK(constraint.at("value").at("value").get<double>() == doctest::Approx(40));
            found = true;
        }
    CHECK(found);
    CHECK(rounded.rounded > 0);
    options.round_dimensions = false;
    const auto exact = fully_define(input, json::object(), options);
    CHECK(exact.rounded == 0);
    for (const auto& constraint : exact.constraints)
        if (constraint.value("type", std::string()) == "distance" &&
            constraint.value("refs", json::array()) == json::array({"e1"}))
            CHECK(constraint.at("value").at("value").get<double>() == doctest::Approx(39.98));
}
