// Focused geometry tests for PCB DRC: arc copper, filled zones, and net-class minima.
// Pure JSON, OCCT-free; links chudware_ecad only.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include "ecad/drc.h"
#include "ecad/schema.h"

#include <cmath>
#include <string>

#include <doctest/doctest.h>
#include <nlohmann/json.hpp>

using nlohmann::json;
using namespace chudware::ecad;

namespace {

json board() {
    json b = schema::new_board("DRC geometry");
    b["nets"] = json::array({json{{"name", "A"}, {"class", "default"}},
                              json{{"name", "B"}, {"class", "default"}},
                              json{{"name", "GND"}, {"class", "default"}},
                              json{{"name", "VCC"}, {"class", "default"}}});
    return b;
}

json pad(const std::string& id, const std::string& net, double x, double y,
         double size = 0.4) {
    return json{{"id", id},
                {"net", net},
                {"type", "smd"},
                {"shape", "circle"},
                {"pos", json::array({x, y})},
                {"size", json::array({size, size})},
                {"layers", json::array({"F.Cu"})},
                {"angle", 0.0}};
}

void add_pad(json& b, const json& p, const std::string& footprint_id = "fp1") {
    b["footprints"].push_back(json{{"id", footprint_id},
                                    {"pos", json::array({0.0, 0.0})},
                                    {"angle", 0.0},
                                    {"side", "top"},
                                    {"pads", json::array({p})}});
}

json quarter_arc(const std::string& id, const std::string& net, double radius,
                 double width = 0.4) {
    return json{{"id", id},
                {"net", net},
                {"layer", "F.Cu"},
                {"width", width},
                {"kind", "arc"},
                {"p1", json::array({radius, 0.0})},
                {"p2", json::array({0.0, radius})},
                {"center", json::array({0.0, 0.0})},
                {"a0", 0.0},
                {"a1", 90.0}};
}

json zone(const std::string& id, const std::string& net, double x0, double y0,
          double x1, double y1) {
    json poly = json::array({json::array({x0, y0}), json::array({x1, y0}),
                             json::array({x1, y1}), json::array({x0, y1})});
    return json{{"id", id},
                {"net", net},
                {"layer", "F.Cu"},
                {"fill", "solid"},
                {"clearance", 0.2},
                {"polygon", poly},
                {"filled_polys", json::array({poly})}};
}

bool has_rule(const json& violations, const std::string& rule) {
    for (const auto& v : violations)
        if (v.value("rule", std::string()) == rule) return true;
    return false;
}

}  // namespace

TEST_CASE("arc-track copper participates in clearance DRC") {
    json b = board();
    b["tracks"].push_back(quarter_arc("t_arc", "A", 10.0));
    const double q = 10.35 / std::sqrt(2.0);
    add_pad(b, pad("p_b", "B", q, q));

    CHECK(has_rule(run_drc(b), "clearance"));
}

TEST_CASE("arc capsules cover the swept copper between chord endpoints") {
    json b = board();
    b["rules"]["clearance"] = 0.0;
    b["net_classes"]["default"]["clearance"] = 0.0;
    b["tracks"].push_back(quarter_arc("t_arc", "A", 10.0));
    // At 45 degrees the 10-degree tessellation has no vertex. The real 0.2 mm
    // track radius overlaps this 0.1 mm pad radius by 0.01 mm; bare chords miss it.
    const double q = 10.29 / std::sqrt(2.0);
    add_pad(b, pad("p_b", "B", q, q, 0.2));

    CHECK(has_rule(run_drc(b), "clearance"));
}

TEST_CASE("an arc track connects its endpoint terminals in the ratsnest") {
    json b = board();
    add_pad(b, pad("p1", "A", 10.0, 0.0), "fp1");
    add_pad(b, pad("p2", "A", 0.0, 10.0), "fp2");
    b["tracks"].push_back(quarter_arc("t_arc", "A", 10.0));

    CHECK(ratsnest(b).empty());
    CHECK(!has_rule(run_drc(b), "unrouted"));
}

TEST_CASE("arc tessellation does not reach distant copper") {
    json b = board();
    b["tracks"].push_back(quarter_arc("t_arc", "A", 10.0));
    add_pad(b, pad("p_far", "B", 20.0, 20.0));

    CHECK(!has_rule(run_drc(b), "clearance"));
}

TEST_CASE("stale zone copper overlapping another net raises zone_clearance") {
    json b = board();
    b["zones"].push_back(zone("z_gnd", "GND", 0.0, 0.0, 10.0, 10.0));
    add_pad(b, pad("p_gnd", "GND", 1.0, 1.0), "fp_gnd");
    add_pad(b, pad("p_vcc", "VCC", 5.0, 5.0), "fp_vcc");

    CHECK(has_rule(run_drc(b), "zone_clearance"));
}

TEST_CASE("zone_clearance measures a near miss outside the polygon edge") {
    json b = board();
    b["zones"].push_back(zone("z_gnd", "GND", 0.0, 0.0, 10.0, 10.0));
    add_pad(b, pad("p_gnd", "GND", 1.0, 1.0), "fp_gnd");
    // Radius 0.1 leaves a 0.15 mm edge gap to the filled polygon; required is 0.2 mm.
    add_pad(b, pad("p_vcc", "VCC", 10.25, 5.0, 0.2), "fp_vcc");

    CHECK(has_rule(run_drc(b), "zone_clearance"));
}

TEST_CASE("a filled polygon disconnected from its own net raises zone_island") {
    json b = board();
    b["zones"].push_back(zone("z_float", "GND", 0.0, 0.0, 2.0, 2.0));
    add_pad(b, pad("p_gnd", "GND", 10.0, 10.0));

    CHECK(has_rule(run_drc(b), "zone_island"));
}

TEST_CASE("net-class track width is a DRC minimum") {
    json b = board();
    b["nets"].push_back(json{{"name", "PWR"}, {"class", "power"}});
    b["net_classes"]["power"] = json{{"clearance", 0.2},
                                         {"track_width", 0.5},
                                         {"via_diameter", 0.8},
                                         {"via_drill", 0.2}};
    b["tracks"].push_back(json{{"id", "t_power"},
                                  {"net", "PWR"},
                                  {"layer", "F.Cu"},
                                  {"width", 0.3},
                                  {"kind", "segment"},
                                  {"p1", json::array({0.0, 0.0})},
                                  {"p2", json::array({5.0, 0.0})}});

    CHECK(has_rule(run_drc(b), "track_width"));
}

TEST_CASE("net-class via diameter is a DRC minimum") {
    json b = board();
    b["nets"].push_back(json{{"name", "PWR"}, {"class", "power"}});
    b["net_classes"]["power"] = json{{"clearance", 0.2},
                                         {"track_width", 0.25},
                                         {"via_diameter", 0.8},
                                         {"via_drill", 0.2}};
    b["vias"].push_back(json{{"id", "v_power"},
                                {"net", "PWR"},
                                {"at", json::array({0.0, 0.0})},
                                {"diameter", 0.6},
                                {"drill", 0.3},
                                {"layers", json::array({"F.Cu", "B.Cu"})}});

    CHECK(has_rule(run_drc(b), "via_size"));
}

TEST_CASE("net-class via drill is a DRC minimum") {
    json b = board();
    b["nets"].push_back(json{{"name", "PWR"}, {"class", "power"}});
    b["net_classes"]["power"] = json{{"clearance", 0.2},
                                         {"track_width", 0.25},
                                         {"via_diameter", 0.8},
                                         {"via_drill", 0.4}};
    b["vias"].push_back(json{{"id", "v_power"},
                                {"net", "PWR"},
                                {"at", json::array({0.0, 0.0})},
                                {"diameter", 0.9},
                                {"drill", 0.3},
                                {"layers", json::array({"F.Cu", "B.Cu"})}});

    CHECK(has_rule(run_drc(b), "via_size"));
}

TEST_CASE("compliant arc zone track and via produce no violations") {
    json b = board();
    b["nets"].push_back(json{{"name", "PWR"}, {"class", "power"}});
    b["net_classes"]["power"] = json{{"clearance", 0.2},
                                         {"track_width", 0.5},
                                         {"via_diameter", 0.8},
                                         {"via_drill", 0.4}};
    b["tracks"].push_back(quarter_arc("t_arc", "A", 5.0));
    b["tracks"].push_back(json{{"id", "t_power"},
                                  {"net", "PWR"},
                                  {"layer", "F.Cu"},
                                  {"width", 0.6},
                                  {"kind", "segment"},
                                  {"p1", json::array({40.0, 40.0})},
                                  {"p2", json::array({45.0, 40.0})}});
    b["vias"].push_back(json{{"id", "v_power"},
                                {"net", "PWR"},
                                {"at", json::array({30.0, 30.0})},
                                {"diameter", 0.9},
                                {"drill", 0.5},
                                {"layers", json::array({"F.Cu", "B.Cu"})}});
    b["zones"].push_back(zone("z_gnd", "GND", 10.0, 10.0, 20.0, 20.0));
    add_pad(b, pad("p_gnd", "GND", 15.0, 15.0, 0.6));

    CHECK(run_drc(b).empty());
}
