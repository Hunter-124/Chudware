// core/tests/test_spice_netlist.cpp — SPICE deck parser + schematic->circuit mapper.
// Covers parse_netlist (deck tokenization) and netlist_to_spice_circuit (inline sim +
// starter SYMBOL libid fallback via symbol_sim_table). Not yet wired in CMakeLists.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <string>

#include "sim/netlist.h"

using aicad::sim::netlist_to_spice_circuit;
using aicad::sim::parse_netlist;
using nlohmann::json;

TEST_CASE("parse_netlist: R + V DC deck") {
    const std::string deck =
        "* divider\n"
        "R1 in out 1k\n"
        "R2 out 0 2k\n"
        "V1 in 0 DC 3.3\n";
    json c = parse_netlist(deck);
    REQUIRE(c.contains("devices"));
    REQUIRE(c["devices"].is_array());
    CHECK(c["devices"].size() == 3);
    CHECK(c.contains("nodes"));
    CHECK(c["nodes"].is_array());
    bool saw_v = false;
    for (const json& d : c["devices"]) {
        if (d.value("kind", std::string()) == "V") {
            saw_v = true;
            CHECK(d["nodes"].size() == 2);
        }
    }
    CHECK(saw_v);
}

TEST_CASE("netlist_to_spice_circuit: Device:R libid symbol fallback") {
    json netlist = {
        {"components",
         json::array({json{{"ref", "R1"},
                           {"libid", "Device:R"},
                           {"value", "10k"},
                           {"pins", json{{"1", "N1"}, {"2", "GND"}}}}})}};
    json out = netlist_to_spice_circuit(netlist, json::object());
    REQUIRE(out.contains("circuit"));
    const json& c = out["circuit"];
    REQUIRE(c.contains("devices"));
    REQUIRE(c["devices"].is_array());
    CHECK(c["devices"].size() == 1);
    const json& dev = c["devices"][0];
    CHECK(dev.value("kind", std::string()) == "R");
    CHECK(dev.value("name", std::string()) == "R1");
    CHECK(dev["nodes"][0].get<std::string>() == "N1");
    CHECK(dev["nodes"][1].get<std::string>() == "0");  // GND -> "0"
    CHECK(dev["value"].get<double>() == doctest::Approx(10000.0));
    REQUIRE(out.contains("warnings"));
    CHECK(out["warnings"].empty());
}

TEST_CASE("netlist_to_spice_circuit: unknown libid warns and skips") {
    json netlist = {{"components",
                     json::array({json{{"ref", "U1"},
                                       {"libid", "Custom:UnknownChip"},
                                       {"pins", json{{"1", "A"}}}}})}};
    json out = netlist_to_spice_circuit(netlist, json::object());
    CHECK(out["circuit"]["devices"].empty());
    REQUIRE(out["warnings"].is_array());
    CHECK(out["warnings"].size() == 1);
    CHECK(out["warnings"][0].get<std::string>().find("no sim model") != std::string::npos);
}
