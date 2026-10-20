// Focused geometry tests for PCB DRC: arc copper, filled zones, and net-class minima.
// Pure JSON, OCCT-free; links chudware_ecad only.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include "ecad/drc.h"
#include "ecad/fabformats.h"
#include "ecad/schema.h"
#include "ecad/session.h"
#include "ecad/zonefill.h"

#include <algorithm>
#include <cmath>
#include <limits>
#include <random>
#include <set>
#include <string>
#include <tuple>
#include <utility>
#include <vector>

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


// Sample the returned copper, including keyholed voids, rather than inspecting
// broad-phase ranges. Test samples sit at cell centres, never on traced edges.
bool filled_at(const json& polys, double x, double y) {
    for (const auto& poly : polys) {
        bool inside = false;
        for (size_t i = 0, j = poly.size() - 1; i < poly.size(); j = i++) {
            const double xi = poly[i][0].get<double>(), yi = poly[i][1].get<double>();
            const double xj = poly[j][0].get<double>(), yj = poly[j][1].get<double>();
            if ((yi > y) != (yj > y) && x < (xj - xi) * (y - yi) / (yj - yi) + xi)
                inside = !inside;
        }
        if (inside) return true;
    }
    return false;
}

void check_fill_cells(const json& polys, const std::vector<std::string>& expected) {
    for (size_t r = 0; r < expected.size(); ++r)
        for (size_t c = 0; c < expected[r].size(); ++c) {
            CAPTURE(r);
            CAPTURE(c);
            CHECK(filled_at(polys, -3.5 + c, -3.5 + r) == (expected[r][c] == '#'));
        }
}
}  // namespace

TEST_CASE("footprint-local pad ids and via ids do not collapse disconnected terminals") {
    json b = board();
    add_pad(b, pad("p1", "GND", 0.0, 0.0), "fp1");
    add_pad(b, pad("p1", "GND", 10.0, 0.0), "fp2");
    const json air = ratsnest(b);
    REQUIRE(air.size() == 1);
    CHECK(air[0]["p1"] == json::array({0.0, 0.0}));
    CHECK(air[0]["p2"] == json::array({10.0, 0.0}));
    CHECK(has_rule(run_drc(b), "unrouted"));

    // A via named like a pad must not electrically join that pad by identity.
    b["vias"].push_back(json{{"id", "p1"}, {"net", "GND"},
                              {"at", json::array({20.0, 0.0})}});
    CHECK(ratsnest(b).size() == 2);
}

TEST_CASE("malformed pad positions fail validation and geometry refuses them safely") {
    json b = board();
    json missing = pad("p1", "GND", 0.0, 0.0);
    missing.erase("pos");
    add_pad(b, missing);
    const auto errors = schema::validate_board(b);
    REQUIRE(std::find(errors.begin(), errors.end(),
                      "footprints[0].pads[0].pos: must be [x,y]") != errors.end());
    CHECK_THROWS_WITH(schema::pad_world(b["footprints"][0], missing),
                      "pad.pos: must be [x,y]");

    const json symbol = {{"pos", json::array({0.0, 0.0})}};
    CHECK_THROWS_WITH(schema::pin_world(symbol, json::object()),
                      "pin.pos: must be [x,y]");
    missing["pos"] = json::array({0.0});
    CHECK_THROWS_WITH(schema::pad_world(b["footprints"][0], missing),
                      "pad.pos: must be [x,y]");

    const json created = ecad_new("pcb", "Bad pad");
    b["id"] = created["doc_id"];
    try {
        ecad_swap_doc(created["doc_id"].get<std::string>(), b, 0, json::array());
        FAIL("invalid pad accepted");
    } catch (const OpError& error) {
        CHECK(error.code == "invalid_op");
        CHECK(std::string(error.what()).find("pads[0].pos") != std::string::npos);
    }
    ecad_close(created["doc_id"].get<std::string>());
}

TEST_CASE("Gerber export defaults absent or malformed stackup to two copper layers") {
    json b = board();
    b.erase("stackup");
    const auto missing = fab::write_gerbers(b);
    CHECK(missing.count("F.Cu") == 1);
    CHECK(missing.count("B.Cu") == 1);
    b["stackup"] = nullptr;
    CHECK(fab::write_gerbers(b) == missing);
}

TEST_CASE("final zone fill survives response compilation, session state and Gerber export") {
    const json created = ecad_new("pcb", "Fine fill");
    const std::string id = created["doc_id"].get<std::string>();
    json b = ecad_session_doc(id);
    b["zones"].push_back(json{{"id", "z1"}, {"net", "GND"}, {"layer", "F.Cu"},
                                {"polygon", json::array({json::array({0.0, 0.0}),
                                                          json::array({3.0, 0.0}),
                                                          json::array({3.0, 3.0})})},
                                {"filled_polys", json::array()}});
    const json expected = fill_zones(b, 0.1)["zones"][0]["filled_polys"];
    const json coarse = fill_zones(b, 0.25)["zones"][0]["filled_polys"];
    REQUIRE(!expected.empty());
    REQUIRE(expected != coarse);
    const json placed = ecad_swap_doc(id, b, created["revision"].get<long>(), json::array());
    const json filled = ecad_fill_zones(id, nullptr, placed["revision"].get<long>());
    CHECK(filled["tree"]["zones"][0]["filled_polys"] == expected);
    CHECK(ecad_session_doc(id)["zones"][0]["filled_polys"] == expected);
    const json exported = ecad_export(json{{"doc_id", id}, {"kind", "gerber"}});
    CHECK(exported["files"]["F.Cu"] ==
          fab::write_gerber_layer(filled["tree"], "F.Cu"));
    CHECK(exported["files"]["F.Cu"] !=
          fab::write_gerber_layer(fill_zones(b, 0.25), "F.Cu"));
    ecad_close(id);
}

TEST_CASE("zone clearance preserves strict cell boundaries for circles boxes and segments") {
    json b = board();
    b["rules"]["clearance"] = 0.5;
    b["net_classes"]["default"]["clearance"] = 0.5;
    json z = zone("z1", "GND", -4.0, -4.0, 4.0, 4.0);

    SUBCASE("circle at a negative-origin cell centre") {
        add_pad(b, pad("p1", "VCC", -0.5, -0.5, 1.0));
        check_fill_cells(fill_zone(b, z, 1.0),
                         {"########", "########", "########", "###.####",
                          "########", "########", "########", "########"});
    }
    SUBCASE("box includes its full unequal half extents") {
        json p = pad("p1", "VCC", -0.5, -0.5);
        p["shape"] = "rect";
        p["size"] = json::array({1.0, 3.0});
        add_pad(b, p);
        check_fill_cells(fill_zone(b, z, 1.0),
                         {"########", "########", "###.####", "###.####",
                          "###.####", "########", "########", "########"});
    }
    SUBCASE("diagonal capsule includes endpoint and off-centreline cells") {
        b["tracks"].push_back(json{{"id", "t1"}, {"net", "VCC"}, {"layer", "F.Cu"},
                                   {"kind", "segment"}, {"width", 0.5},
                                   {"p1", json::array({-2.5, -1.5})},
                                   {"p2", json::array({0.5, 1.5})}});
        check_fill_cells(fill_zone(b, z, 1.0),
                         {"########", "########", "#..#####", "#...####",
                          "##...###", "###..###", "########", "########"});
    }
    SUBCASE("an obstacle outside the zone still clears boundary cells") {
        add_pad(b, pad("p1", "VCC", -4.5, -0.5, 1.5));
        check_fill_cells(fill_zone(b, z, 1.0),
                         {"########", "########", "########", ".#######",
                          "########", "########", "########", "########"});
    }
    SUBCASE("a degenerate segment uses its first endpoint") {
        b["tracks"].push_back(json{{"id", "t1"}, {"net", "VCC"}, {"layer", "F.Cu"},
                                   {"kind", "segment"}, {"width", 1.0},
                                   {"p1", json::array({-0.5, -0.5})},
                                   {"p2", json::array({-0.5, -0.5})}});
        check_fill_cells(fill_zone(b, z, 1.0),
                         {"########", "########", "########", "###.####",
                          "########", "########", "########", "########"});
    }
}

TEST_CASE("zone thermal clipping retains pad copper and all four spokes") {
    json b = board();
    json p = pad("p1", "GND", -0.5, -0.5, 1.0);
    p["type"] = "tht";
    p["drill"] = 0.4;
    add_pad(b, p);
    json z = zone("z1", "GND", -4.0, -4.0, 4.0, 4.0);
    z["thermal"] = json{{"gap", 2.0}, {"spoke_width", 1.0}};
    check_fill_cells(fill_zone(b, z, 1.0),
                     {"########", "##.#.###", "#..#..##", "########",
                      "#..#..##", "##.#.###", "########", "########"});

    // Zero gap must not carve copper, even with no spokes.
    z["thermal"] = json{{"gap", 0.0}, {"spoke_width", 0.0}};
    check_fill_cells(fill_zone(b, z, 1.0), std::vector<std::string>(8, "########"));
}

TEST_CASE("zone clipping includes sub-cell obstacles and excludes partial-grid centres outside the zone") {
    json b = board();
    b["rules"]["clearance"] = 0.0625;
    b["net_classes"]["default"]["clearance"] = 0.0625;
    add_pad(b, pad("p1", "VCC", -1.0, -0.5, 0.125));
    json z = zone("z1", "GND", -1.25, -0.75, 2.0, 1.0);
    z["clearance"] = 0.0625;
    const json polys = fill_zone(b, z, 0.5);
    for (int r = 0; r < 4; ++r)
        for (int c = 0; c < 7; ++c) {
            CAPTURE(r);
            CAPTURE(c);
            const bool expected = r < 3 && c < 6 && (r != 0 || c != 0);
            CHECK(filled_at(polys, -1.0 + c * 0.5, -0.5 + r * 0.5) == expected);
        }

    // Zero required clearance intentionally does not carve even overlapping
    // copper: the final predicate is strictly distance < required.
    b["rules"]["clearance"] = 0.0;
    b["net_classes"]["default"]["clearance"] = 0.0;
    z["clearance"] = 0.0;
    CHECK(filled_at(fill_zone(b, z, 0.5), -1.0, -0.5));
}

TEST_CASE("thermal spoke boundaries remain copper at inclusive half width") {
    json b = board();
    json p = pad("p1", "GND", -1.0, -1.0, 0.5);
    p["type"] = "tht";
    p["drill"] = 0.2;
    add_pad(b, p);
    json z = zone("z1", "GND", -4.0, -4.0, 4.0, 4.0);
    z["thermal"] = json{{"gap", 2.0}, {"spoke_width", 1.0}};
    check_fill_cells(fill_zone(b, z, 1.0),
                     {"########", "#.##.###", "########", "########",
                      "#.##.###", "########", "########", "########"});
}

TEST_CASE("zone clipping treats huge and nonfinite obstacle bounds conservatively") {
    const double inf = std::numeric_limits<double>::infinity();
    json z = zone("z1", "GND", -4.0, -4.0, 4.0, 4.0);
    for (const double size : {std::numeric_limits<double>::max(), inf}) {
        CAPTURE(size);
        json b = board();
        add_pad(b, pad("p1", "VCC", 0.0, 0.0, size));
        CHECK(fill_zone(b, z, 1.0).empty());
    }

    json b = board();
    add_pad(b, pad("p1", "VCC", 1e300, -1e300, 1.0));
    check_fill_cells(fill_zone(b, z, 1.0), std::vector<std::string>(8, "########"));
    b["footprints"].clear();
    add_pad(b, pad("p1", "VCC", 0.0, 0.0, 1.0));
    z["clearance"] = inf;
    CHECK(fill_zone(b, z, 1.0).empty());

    b["footprints"].clear();
    json p = pad("p1", "GND", -0.5, -0.5, 1.0);
    p["type"] = "tht";
    p["drill"] = 0.4;
    add_pad(b, p);
    z["clearance"] = 0.2;
    z["thermal"] = json{{"gap", inf}, {"spoke_width", 0.0}};
    check_fill_cells(fill_zone(b, z, 1.0),
                     {"...#....", "...#....", "...#....", "########",
                      "...#....", "...#....", "...#....", "...#...."});

    // Preserve the original nonfinite thermal predicate, not just the ordinary
    // finite annulus: hypot(NaN, dy) does not reject distant rows by r_gap.
    b["footprints"][0]["pos"][0] = std::numeric_limits<double>::quiet_NaN();
    z["thermal"] = json{{"gap", 0.25}, {"spoke_width", 0.0}};
    check_fill_cells(fill_zone(b, z, 1.0),
                     {"........", "........", "........", "########",
                      "........", "........", "........", "........"});
}

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

// --------------------------------------------------------------------------
// Ratsnest differential harness. ratsnest() keeps one cached candidate edge per
// remaining component and only measures the component it just added; the
// reference below is the straightforward version it replaced, which re-sorted
// both sides and recomputed the whole component cross product every round. The
// two must agree exactly — tie-breaks included — so the fixtures deliberately
// contain mirrored layouts whose candidate distances are bit-for-bit equal.
namespace {

struct RefTerminal {
    std::string id;
    double x = 0.0;
    double y = 0.0;
};
using RefComponent = std::vector<RefTerminal>;

RefComponent solo(const std::string& id, double x, double y) {
    return RefComponent{{id, x, y}};
}

std::string term_id(int n) {
    const std::string digits = std::to_string(n);
    return "p" + std::string(digits.size() < 3 ? 3 - digits.size() : 0, '0') + digits;
}

// Reference Prim over pre-grouped components: components ranked by their
// smallest terminal id, terminals inside a component ordered by id, each round's
// edge chosen row-major over (in-tree component, remaining component) and
// row-major over the terminal cross product, keeping the FIRST strict minimum.
json reference_ratsnest(const std::string& net, std::vector<RefComponent> comps) {
    for (RefComponent& c : comps)
        std::sort(c.begin(), c.end(),
                  [](const RefTerminal& a, const RefTerminal& b) { return a.id < b.id; });
    std::sort(comps.begin(), comps.end(), [](const RefComponent& a, const RefComponent& b) {
        return a.front().id < b.front().id;
    });

    json out = json::array();
    if (comps.size() < 2) return out;

    auto comp_dist = [&](size_t ca, size_t cb) {
        double best = std::numeric_limits<double>::infinity();
        const RefTerminal* bta = nullptr;
        const RefTerminal* btb = nullptr;
        for (const RefTerminal& ta : comps[ca])
            for (const RefTerminal& tb : comps[cb]) {
                const double d = std::hypot(ta.x - tb.x, ta.y - tb.y);
                if (d < best) {
                    best = d;
                    bta = &ta;
                    btb = &tb;
                }
            }
        return std::make_tuple(best, bta, btb);
    };

    std::vector<size_t> in_tree{0};
    std::vector<size_t> remaining;
    for (size_t i = 1; i < comps.size(); ++i) remaining.push_back(i);
    while (!remaining.empty()) {
        double best = std::numeric_limits<double>::infinity();
        size_t best_to = 0;
        const RefTerminal* bta = nullptr;
        const RefTerminal* btb = nullptr;
        for (const size_t fr : in_tree)
            for (const size_t to : remaining) {
                const auto [d, ta, tb] = comp_dist(fr, to);
                if (d < best) {
                    best = d;
                    best_to = to;
                    bta = ta;
                    btb = tb;
                }
            }
        out.push_back(json{{"net", net},
                           {"p1", json::array({bta->x, bta->y})},
                           {"p2", json::array({btb->x, btb->y})}});
        in_tree.insert(std::upper_bound(in_tree.begin(), in_tree.end(), best_to), best_to);
        remaining.erase(std::find(remaining.begin(), remaining.end(), best_to));
    }

    std::sort(out.begin(), out.end(), [](const json& a, const json& b) {
        const std::vector<double> a1{a["p1"][0].get<double>(), a["p1"][1].get<double>()};
        const std::vector<double> b1{b["p1"][0].get<double>(), b["p1"][1].get<double>()};
        if (a1 != b1) return a1 < b1;
        const std::vector<double> a2{a["p2"][0].get<double>(), a["p2"][1].get<double>()};
        const std::vector<double> b2{b["p2"][0].get<double>(), b["p2"][1].get<double>()};
        return a2 < b2;
    });
    return out;
}

// A board whose net-`net` terminals are exactly `comps`: one pad per terminal,
// plus a track chain per multi-pad component whose endpoints sit exactly on those
// pads, which is what fuses them into a single ratsnest component. Every pad sits
// on its own grid cell, so nothing else can merge; if the grouping came out
// different the differential comparison fails.
json board_from_components(const std::string& net, const std::vector<RefComponent>& comps) {
    json b = board();
    json pads = json::array();
    json tracks = json::array();
    int track_index = 0;
    for (const RefComponent& c : comps) {
        for (const RefTerminal& t : c) pads.push_back(pad(t.id, net, t.x, t.y));
        for (size_t i = 1; i < c.size(); ++i)
            tracks.push_back(json{{"id", "t" + std::to_string(track_index++)},
                                  {"net", net},
                                  {"layer", "F.Cu"},
                                  {"width", 0.2},
                                  {"kind", "segment"},
                                  {"p1", json::array({c[i - 1].x, c[i - 1].y})},
                                  {"p2", json::array({c[i].x, c[i].y})}});
    }
    b["footprints"].push_back(json{{"id", "fp1"},
                                   {"pos", json::array({0.0, 0.0})},
                                   {"angle", 0.0},
                                   {"side", "top"},
                                   {"pads", pads}});
    b["tracks"] = tracks;
    return b;
}

}  // namespace

TEST_CASE("ratsnest matches the reference Prim over randomized net layouts") {
    std::mt19937 rng(20240613u);
    for (int iteration = 0; iteration < 40; ++iteration) {
        const int component_count = 2 + static_cast<int>(rng() % 6);
        std::vector<RefComponent> comps;
        std::set<std::pair<int, int>> used;
        int total = 0;
        for (int c = 0; c < component_count; ++c) {
            const size_t size = 1 + static_cast<size_t>(rng() % 3);
            RefComponent comp;
            while (comp.size() < size) {
                const int gx = static_cast<int>(rng() % 24);
                const int gy = static_cast<int>(rng() % 24);
                if (!used.insert({gx, gy}).second) continue;
                // A 0.5 mm grid keeps pads apart while making many candidate
                // distances exactly equal, which is what exercises the tie-break.
                comp.push_back({std::string(), gx * 0.5, gy * 0.5});
                ++total;
            }
            comps.push_back(std::move(comp));
        }
        std::vector<std::string> ids;
        ids.reserve(static_cast<size_t>(total));
        for (int i = 0; i < total; ++i) ids.push_back(term_id(i));
        std::shuffle(ids.begin(), ids.end(), rng);  // component ranking varies too
        size_t next = 0;
        for (RefComponent& c : comps)
            for (RefTerminal& t : c) t.id = ids[next++];

        const json b = board_from_components("A", comps);
        CHECK(ratsnest(b) == reference_ratsnest("A", comps));
    }
}

TEST_CASE("ratsnest tie-breaks match the reference on mirrored layouts") {
    std::vector<std::vector<RefComponent>> fixtures;
    // Square: all four sides measure exactly 10 mm, both diagonals exactly equal.
    fixtures.push_back(std::vector<RefComponent>{solo("p000", 0.0, 0.0), solo("p001", 10.0, 0.0),
                                                 solo("p002", 0.0, 10.0),
                                                 solo("p003", 10.0, 10.0)});
    // Star: the centre pad is exactly 5 mm from four mirrored satellites.
    fixtures.push_back(std::vector<RefComponent>{solo("p000", 0.0, 0.0), solo("p001", 5.0, 0.0),
                                                 solo("p002", -5.0, 0.0), solo("p003", 0.0, 5.0),
                                                 solo("p004", 0.0, -5.0)});
    // Mirrored two-pad components: both component pairs measure exactly 10 mm and
    // each of those measurements has two equidistant terminal pairs.
    fixtures.push_back(std::vector<RefComponent>{
        RefComponent{{"p000", 0.0, 0.0}, {"p001", 0.0, 1.0}},
        RefComponent{{"p002", 10.0, 0.0}, {"p003", 10.0, 1.0}},
        RefComponent{{"p004", -10.0, 0.0}, {"p005", -10.0, 1.0}}});
    // Same geometry with the ids rotated, so the component ranking mirrors too.
    fixtures.push_back(std::vector<RefComponent>{
        RefComponent{{"p004", 0.0, 0.0}, {"p005", 0.0, 1.0}},
        RefComponent{{"p000", 10.0, 0.0}, {"p001", 10.0, 1.0}},
        RefComponent{{"p002", -10.0, 0.0}, {"p003", -10.0, 1.0}}});
    // Diagonal mirror pinning the FROM-side tie-break: p003 ends up exactly
    // equidistant from p001 and p002 (the deltas are each other's transpose), and
    // p002 joins the tree BEFORE the lower-ranked p001, so the airwire must be
    // drawn from p001 — the tie-break is on component rank, not on join order.
    fixtures.push_back(std::vector<RefComponent>{solo("p000", 0.0, 4.0), solo("p001", 10.0, 0.0),
                                                 solo("p002", 0.0, 10.0),
                                                 solo("p003", 20.0, 20.0)});
    // Mirrored pair of pairs sharing one equidistant bridge candidate.
    fixtures.push_back(std::vector<RefComponent>{
        RefComponent{{"p003", -4.0, 0.0}, {"p002", -4.0, 4.0}},
        RefComponent{{"p001", 4.0, 0.0}, {"p000", 4.0, 4.0}}, solo("p004", 0.0, 8.0),
        solo("p005", 0.0, -8.0)});
    // Transposed deltas pinning the TO-side tie-break: p003 and p004 are exactly
    // the same distance from p001 (deltas (1.5,1.0) and (1.0,1.5)), and taking the
    // higher-ranked one first yields a different spanning tree, so the round must
    // pick the lower-ranked component.
    fixtures.push_back(std::vector<RefComponent>{solo("p000", 3.5, 0.0), solo("p001", 3.0, 1.0),
                                                 solo("p002", 2.5, 3.5), solo("p003", 1.5, 2.0),
                                                 solo("p004", 2.0, 2.5)});

    for (const std::vector<RefComponent>& comps : fixtures) {
        const json b = board_from_components("A", comps);
        const json wires = ratsnest(b);
        CHECK(wires.size() == comps.size() - 1);  // the fixture grouping survived
        CHECK(wires == reference_ratsnest("A", comps));
    }
}

TEST_CASE("the first nets entry for a name defines its net class") {
    json b = board();
    // The net-class index is built once per run; like the linear scan it replaced,
    // the first entry wins. 'power' allows the 0.6 mm track, 'tight' would not.
    b["nets"].push_back(json{{"name", "PWR"}, {"class", "power"}});
    b["nets"].push_back(json{{"name", "PWR"}, {"class", "tight"}});
    b["net_classes"]["power"] = json{{"clearance", 0.2}, {"track_width", 0.5}};
    b["net_classes"]["tight"] = json{{"clearance", 0.2}, {"track_width", 1.0}};
    b["tracks"].push_back(json{{"id", "t_power"},
                                  {"net", "PWR"},
                                  {"layer", "F.Cu"},
                                  {"width", 0.6},
                                  {"kind", "segment"},
                                  {"p1", json::array({0.0, 0.0})},
                                  {"p2", json::array({5.0, 0.0})}});

    CHECK(!has_rule(run_drc(b), "track_width"));
}
