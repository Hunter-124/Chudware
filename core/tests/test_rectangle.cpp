// Rectangle convenience entity: {"type":"rectangle","center":[x,y],"width":W,"height":H}
// (and the two-corner form) expand at sketch_add_entity time into 4 closed CCW line segments.
// Models emit this constantly; without the expand the validator rejected "rectangle" and the
// agent retry-looped. Mirrors test_polygon.cpp — expand path + a full extrude area check.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <cmath>
#include <string>

#include <nlohmann/json.hpp>

#include "doc/session.h"

using json = nlohmann::json;

namespace {

// new part -> empty sketch on plane.top -> add ONE entity -> return expanded entities.
json expand_rect_entity(const json& entity) {
    using chudware::doc::doc_apply_ops;
    using chudware::doc::doc_new;
    std::string id = doc_new("part", "RectTest").at("doc_id").get<std::string>();

    json sketch_feat = {{"type", "sketch"},
                        {"name", "profile"},
                        {"params", {{"entities", json::array()}, {"constraints", json::array()}}},
                        {"refs", {{"plane", "plane.top"}}}};
    json ops = json::array({
        json{{"op", "add_feature"}, {"feature", sketch_feat}},
        json{{"op", "sketch_add_entity"}, {"sketch", "$0"}, {"entity", entity}},
    });
    json resp = doc_apply_ops(id, -1, ops, false);
    for (const json& f : resp.at("tree").at("features"))
        if (f.value("type", std::string()) == "sketch")
            return f.at("params").at("entities");
    return json::array();
}

// Apply ops that throw OpError (for malformed-entity checks).
void expand_rect_entity_throwing(const json& entity) {
    (void)expand_rect_entity(entity);
}

// Every entity is a line and the loop closes (last.p2 == first.p1). Returns the line count.
int closed_loop_sides(const json& ents) {
    REQUIRE(ents.is_array());
    for (const json& e : ents) CHECK(e.value("type", std::string()) == "line");
    if (ents.empty()) return 0;
    const json& p1 = ents.front().at("p1");
    const json& pN = ents.back().at("p2");
    CHECK(std::fabs(p1[0].get<double>() - pN[0].get<double>()) < 1e-9);
    CHECK(std::fabs(p1[1].get<double>() - pN[1].get<double>()) < 1e-9);
    return static_cast<int>(ents.size());
}

// Axis-aligned bbox of the expanded lines (min corner + size).
void bbox_of(const json& ents, double* x0, double* y0, double* x1, double* y1) {
    REQUIRE(ents.size() >= 1);
    *x0 = *y0 = 1e300;
    *x1 = *y1 = -1e300;
    for (const json& e : ents) {
        for (const char* k : {"p1", "p2"}) {
            double x = e.at(k)[0].get<double>();
            double y = e.at(k)[1].get<double>();
            *x0 = std::min(*x0, x);
            *y0 = std::min(*y0, y);
            *x1 = std::max(*x1, x);
            *y1 = std::max(*y1, y);
        }
    }
}

// Signed polygon area via shoelace on the chain of p1 points (CCW => positive).
double signed_loop_area(const json& ents) {
    double a = 0.0;
    const int n = static_cast<int>(ents.size());
    for (int i = 0; i < n; ++i) {
        double x0 = ents[i].at("p1")[0].get<double>();
        double y0 = ents[i].at("p1")[1].get<double>();
        double x1 = ents[i].at("p2")[0].get<double>();
        double y1 = ents[i].at("p2")[1].get<double>();
        a += x0 * y1 - x1 * y0;
    }
    return 0.5 * a;
}

}  // namespace

TEST_CASE("rectangle center-form expands to 4 closed CCW lines") {
    json ents = expand_rect_entity({{"type", "rectangle"},
                                    {"center", json::array({0.0, 0.0})},
                                    {"width", 200.0},
                                    {"height", 120.0}});
    CHECK(closed_loop_sides(ents) == 4);
    double x0, y0, x1, y1;
    bbox_of(ents, &x0, &y0, &x1, &y1);
    CHECK(x0 == doctest::Approx(-100.0));
    CHECK(y0 == doctest::Approx(-60.0));
    CHECK(x1 == doctest::Approx(100.0));
    CHECK(y1 == doctest::Approx(60.0));
    // CCW winding => positive shoelace area == width*height.
    CHECK(signed_loop_area(ents) == doctest::Approx(200.0 * 120.0).epsilon(1e-9));
}

TEST_CASE("rectangle accepts {value,expr} dim objects and default center") {
    // No center => [0,0]. Dim objects unwrap like every other entity param.
    json ents = expand_rect_entity(
        {{"type", "rectangle"},
         {"width", json{{"value", 40.0}, {"expr", nullptr}}},
         {"height", json{{"value", 30.0}, {"expr", nullptr}}}});
    CHECK(closed_loop_sides(ents) == 4);
    double x0, y0, x1, y1;
    bbox_of(ents, &x0, &y0, &x1, &y1);
    CHECK(x0 == doctest::Approx(-20.0));
    CHECK(y0 == doctest::Approx(-15.0));
    CHECK(x1 == doctest::Approx(20.0));
    CHECK(y1 == doctest::Approx(15.0));
}

TEST_CASE("rectangle two-corner form expands correctly") {
    json ents = expand_rect_entity({{"type", "rectangle"},
                                    {"corner1", json::array({-40.0, -25.0})},
                                    {"corner2", json::array({40.0, 25.0})}});
    CHECK(closed_loop_sides(ents) == 4);
    double x0, y0, x1, y1;
    bbox_of(ents, &x0, &y0, &x1, &y1);
    CHECK(x0 == doctest::Approx(-40.0));
    CHECK(y0 == doctest::Approx(-25.0));
    CHECK(x1 == doctest::Approx(40.0));
    CHECK(y1 == doctest::Approx(25.0));
    CHECK(signed_loop_area(ents) == doctest::Approx(80.0 * 50.0).epsilon(1e-9));
}

TEST_CASE("rectangle alias rect is accepted") {
    json ents = expand_rect_entity(
        {{"type", "rect"}, {"center", json::array({5.0, 5.0})}, {"width", 10.0}, {"height", 10.0}});
    CHECK(closed_loop_sides(ents) == 4);
}

TEST_CASE("rectangle center-form extrudes to correct 200x120 area volume") {
    // Full op path: rectangle -> closed region -> extrude depth 10 => vol = 200*120*10.
    using chudware::doc::doc_apply_ops;
    using chudware::doc::doc_new;
    json nr = doc_new("part", "RectArea");
    const std::string id = nr.at("doc_id").get<std::string>();
    json ops = json::array({
        json{{"op", "add_feature"},
             {"feature",
              {{"type", "sketch"},
               {"name", "Sk"},
               {"params", {{"entities", json::array()}, {"constraints", json::array()}}},
               {"refs", {{"plane", "plane.top"}}}}}},
        json{{"op", "sketch_add_entity"},
             {"sketch", "$0"},
             {"entity",
              {{"type", "rectangle"},
               {"center", json::array({0.0, 0.0})},
               {"width", json{{"value", 200.0}, {"expr", nullptr}}},
               {"height", json{{"value", 120.0}, {"expr", nullptr}}}}}},
        json{{"op", "add_feature"},
             {"feature",
              {{"type", "extrude"},
               {"name", "Boss"},
               {"params",
                {{"mode", "boss"},
                 {"end", "blind"},
                 {"depth", json{{"value", 10.0}, {"expr", nullptr}}}}},
               {"refs",
                {{"profile",
                  json::array({json{{"kind", "region"},
                                    {"sketch", "$0"},
                                    {"centroid", json::array({0.0, 0.0})},
                                    {"hint_index", 0}}})}}}}}},
    });
    json resp = doc_apply_ops(id, -1, ops, false);
    const double vol = resp.value("volume", 0.0);
    CHECK(vol == doctest::Approx(200.0 * 120.0 * 10.0).epsilon(0.01));
}

TEST_CASE("malformed rectangle is rejected with a clear message") {
    using chudware::doc::OpError;
    // Missing width/height (and no corners).
    try {
        expand_rect_entity_throwing({{"type", "rectangle"}, {"center", json::array({0.0, 0.0})}});
        FAIL("expected OpError for missing width/height");
    } catch (const OpError& e) {
        CHECK(e.code == "invalid_op");
        // Message must name the expected form so the agent can self-correct.
        const std::string msg = e.what();
        CHECK(msg.find("rectangle:") != std::string::npos);
        CHECK(msg.find("width") != std::string::npos);
        CHECK(msg.find("height") != std::string::npos);
    }
    // Non-positive size.
    CHECK_THROWS_AS(
        expand_rect_entity_throwing(
            {{"type", "rectangle"}, {"width", 0.0}, {"height", 10.0}}),
        OpError);
    CHECK_THROWS_AS(
        expand_rect_entity_throwing(
            {{"type", "rectangle"}, {"width", -5.0}, {"height", 10.0}}),
        OpError);
    // Degenerate corners.
    CHECK_THROWS_AS(
        expand_rect_entity_throwing({{"type", "rectangle"},
                                     {"corner1", json::array({1.0, 1.0})},
                                     {"corner2", json::array({1.0, 5.0})}}),
        OpError);
}
