// Polygon convenience entity: named regular polygons (triangle/pentagon/hexagon/heptagon/octagon/
// nonagon/decagon/dodecagon) and the generic {type:"polygon",sides:N} expand, at sketch_add_entity
// time, into N closed line segments. This proves the named shorthands added beyond "hexagon" produce
// the right side count, honour an explicit "sides" override, accept the across_flats / circumradius /
// inradius size conventions, and reject n<3 — i.e. the agent/MCP-facing vocabulary now offers more
// than just hexagons, while staying plain lines downstream (no solver/region awareness needed).
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <cmath>
#include <string>

#include <nlohmann/json.hpp>

#include "doc/session.h"

using json = nlohmann::json;

namespace {

// new part -> add an empty sketch on plane.top -> add ONE entity -> return the sketch's expanded
// entities array. The sketch is the first op, so its placeholder ref is "$0".
json expand_polygon_entity(const json& entity) {
    using aicad::doc::doc_apply_ops;
    using aicad::doc::doc_new;
    std::string id = doc_new("part", "PolyTest").at("doc_id").get<std::string>();

    json sketch_feat = {{"type", "sketch"}, {"name", "profile"},
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

}  // namespace

TEST_CASE("named regular polygons expand to the right side count") {
    struct { const char* type; int sides; } cases[] = {
        {"triangle", 3}, {"pentagon", 5}, {"hexagon", 6}, {"heptagon", 7},
        {"octagon", 8}, {"nonagon", 9}, {"decagon", 10}, {"dodecagon", 12},
    };
    for (const auto& c : cases) {
        json ents = expand_polygon_entity(
            {{"type", c.type}, {"center", json::array({0.0, 0.0})}, {"across_flats", 20.0}});
        CHECK_MESSAGE(closed_loop_sides(ents) == c.sides, c.type);
    }
}

TEST_CASE("generic polygon takes an arbitrary side count") {
    json ents = expand_polygon_entity({{"type", "polygon"},
                                       {"center", json::array({0.0, 0.0})},
                                       {"sides", 20},
                                       {"circumradius", 15.0}});
    CHECK(closed_loop_sides(ents) == 20);
}

TEST_CASE("explicit sides overrides a named polygon") {
    // {"type":"hexagon","sides":5} -> 5 sides, not the implied 6.
    json ents = expand_polygon_entity({{"type", "hexagon"}, {"sides", 5}, {"across_flats", 12.0}});
    CHECK(closed_loop_sides(ents) == 5);
}

TEST_CASE("size conventions all build the loop") {
    for (const char* sizekey : {"across_flats", "circumradius", "inradius"}) {
        json ent = {{"type", "octagon"}, {"center", json::array({0.0, 0.0})}};
        ent[sizekey] = 10.0;
        CHECK_MESSAGE(closed_loop_sides(expand_polygon_entity(ent)) == 8, sizekey);
    }
}

TEST_CASE("degenerate polygons are rejected") {
    using aicad::doc::OpError;
    // sides < 3 is not a polygon.
    CHECK_THROWS_AS(
        expand_polygon_entity({{"type", "polygon"}, {"sides", 2}, {"across_flats", 10.0}}), OpError);
    // A named polygon with no size can't be drawn (needs across_flats / circumradius / inradius).
    CHECK_THROWS_AS(expand_polygon_entity({{"type", "octagon"}}), OpError);
}
