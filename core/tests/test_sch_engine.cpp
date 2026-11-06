// Schematic engine contract tests — connectivity, ERC, library and save safety
// behaviors that the 2026-08 schematic overhaul fixed. Each case pins a
// SPECIFIC defect that shipped before it:
//
//   * connectivity was keyed on a bare point, so two sheets with coincident geometry
//     were electrically shorted;
//   * a pin landing on a wire's interior floated (only exact vertex coincidence united);
//   * local labels merged across sheets because the emitted map keyed on bare text;
//   * buses were parse-only, so a bus + bus entry carried no connectivity;
//   * multi-unit parts emitted one component per unit (two BOM lines, two footprints);
//   * `passive` counted as a driver and any power PORT counted as a power source;
//   * an NC pin wired to a stub was not reported;
//   * `sch_add_symbol` fabricated a 2-pin body for every libid (no native symbol library);
//   * `sch_import_kicad` was a registered no-op;
//   * a pathless Save over an imported .kicad_sch replaced it with ZIP bytes.
//
// Pure JSON, OCCT-free; links chudware_ecad only.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include "ecad/erc.h"
#include "ecad/kicad.h"
#include "ecad/kicad_cli.h"
#include "ecad/netlist.h"
#include "ecad/ops.h"
#include "ecad/schema.h"
#include "ecad/session.h"
#include "ecad/symlib.h"
#include "sim/netlist.h"

#include <cstdio>
#include <filesystem>
#include <fstream>
#include <map>
#include <set>
#include <iterator>
#include <string>
#include <utility>
#include <vector>

#include <doctest/doctest.h>
#include <nlohmann/json.hpp>

namespace fs = std::filesystem;
using nlohmann::json;
using namespace chudware::ecad;

namespace {

// A two-pin passive definition on the 2.54 grid: pin 1 tip at (-2.54,0), pin 2 tip at
// (+2.54,0) relative to the placement origin.
json res_def() {
    return json{
        {"libid", "Device:R"},
        {"name", "R"},
        {"prefix", "R"},
        {"pins",
         json::array({json{{"number", "1"}, {"name", "~"}, {"pos", json::array({-1.27, 0.0})},
                           {"angle", 180}, {"length", 1.27}, {"type", "passive"}},
                      json{{"number", "2"}, {"name", "~"}, {"pos", json::array({1.27, 0.0})},
                           {"angle", 0}, {"length", 1.27}, {"type", "passive"}}})},
        {"graphics", json::array({json{{"kind", "rect"},
                                       {"pts", json::array({json::array({-1.27, -0.635}),
                                                            json::array({1.27, 0.635})})}}})}};
}

// Apply one op batch to a fresh in-memory schematic and hand back the resulting doc.
json build(const json& ops, const char* name = "T") {
    json created = ecad_new("sch", name);
    const std::string id = created.value("doc_id", std::string());
    REQUIRE(!id.empty());
    ecad_apply_ops(id, created.value("revision", 0L), ops);
    json doc = ecad_session_doc(id);
    ecad_close(id);
    return doc;
}

bool has_rule(const json& violations, const std::string& rule) {
    for (const auto& v : violations)
        if (v.value("rule", std::string()) == rule) return true;
    return false;
}

// The net a given placed pin resolved onto ("" when floating).
std::string net_of(const json& netlist, const std::string& sym, const std::string& pin) {
    const json by_pin = netlist.value("by_pin", json::object());
    return by_pin.value(sym + "." + pin, std::string());
}

}  // namespace

// ---------------------------------------------------------------- connectivity

TEST_CASE("board sync refuses conflicting schematic refs without mutating the board") {
    const json board_created = ecad_new("pcb", "Ref guard");
    const std::string board_id = board_created["doc_id"].get<std::string>();
    const fs::path path = fs::temp_directory_path() / (board_id + "-conflicting-refs.json");
    for (bool different_library : {true, false}) {
        json doc = schema::new_schematic("Conflicting refs");
        doc["lib_cache"]["Device:R"] = res_def();
        if (different_library) {
            doc["lib_cache"]["Device:R2"] = res_def();
            doc["lib_cache"]["Device:R2"]["libid"] = "Device:R2";
        }
        for (int i = 0; i < 2; ++i)
            doc["symbols"].push_back(json{{"id", "s" + std::to_string(i + 1)},
                                           {"libid", i && different_library ? "Device:R2" : "Device:R"},
                                           {"ref", "R1"}, {"value", "10k"}, {"sheet", "sh1"},
                                           {"pos", json::array({20.0 * i, 0.0})},
                                           {"angle", 0}, {"mirror", "none"}, {"unit", 1}});
        const std::string rule = different_library ? "duplicate_ref" : "unit_ref_conflict";
        REQUIRE(has_rule(run_erc(doc, build_netlist(doc)), rule));
        { std::ofstream out(path); out << doc.dump(); }
        const json opened = ecad_open(path.string());
        const std::string sch_id = opened["doc_id"].get<std::string>();
        try {
            ecad_update_from_sch(board_id, sch_id, false, nullptr, 0);
            FAIL("lossy board sync accepted a conflicting schematic ref");
        } catch (const OpError& error) {
            CHECK(error.code == "schematic_ref_conflict");
            CHECK(error.detail.value("rule", std::string()) == rule);
        }
        CHECK(ecad_session_doc(board_id)["footprints"].empty());
        ecad_close(sch_id);
    }
    fs::remove(path);
    ecad_close(board_id);

    // Inline netlists do not include the source symbols/ERC; reject duplicate
    // component entries at the sync boundary as well.
    const json netlist = {{"components", json::array({json{{"ref", "R1"}},
                                                      json{{"ref", "R1"}}})}};
    try {
        ecad_update_from_sch_inline(schema::new_board("B"), netlist, false, nullptr);
        FAIL("duplicate netlist components accepted");
    } catch (const OpError& error) {
        CHECK(error.code == "schematic_ref_conflict");
    }
}

TEST_CASE("coincident geometry on two sheets does not short") {
    json doc = schema::new_schematic("TwoSheets");
    doc["sheets"] = json::array({json{{"id", "sh1"}, {"name", "A"}},
                                 json{{"id", "sh2"}, {"name", "B"}}});
    doc["lib_cache"]["Device:R"] = res_def();
    // Identical coordinates on two different sheets.
    for (const char* sh : {"sh1", "sh2"}) {
        const std::string s = std::string("s") + (sh[3] == '1' ? "1" : "2");
        doc["symbols"].push_back(json{{"id", s},
                                      {"libid", "Device:R"},
                                      {"ref", std::string("R") + (sh[3] == '1' ? "1" : "2")},
                                      {"value", "10k"},
                                      {"sheet", sh},
                                      {"pos", json::array({0.0, 0.0})},
                                      {"angle", 0},
                                      {"mirror", "none"},
                                      {"unit", 1}});
    }

    const json nl = build_netlist(doc);
    const std::string a = net_of(nl, "s1", "1");
    const std::string b = net_of(nl, "s2", "1");
    // Both pins sit at exactly (-2.54, 0) — on the SAME sheet that would be one net.
    CHECK(a != b);
}

TEST_CASE("a pin landing on a wire interior connects") {
    json doc = schema::new_schematic("Tee");
    doc["lib_cache"]["Device:R"] = res_def();
    // A horizontal wire from (-10,0) to (10,0); the resistor's pin 1 tip lands at (0,0),
    // strictly INSIDE that segment (not on a vertex).
    doc["wires"].push_back(json{{"id", "w1"},
                                {"sheet", "sh1"},
                                {"pts", json::array({json::array({-10.0, 0.0}),
                                                     json::array({10.0, 0.0})})},
                                {"bus", false}});
    doc["symbols"].push_back(json{{"id", "s1"},
                                  {"libid", "Device:R"},
                                  {"ref", "R1"},
                                  {"value", "1k"},
                                  {"sheet", "sh1"},
                                  {"pos", json::array({2.54, 0.0})},
                                  {"angle", 0},
                                  {"mirror", "none"},
                                  {"unit", 1}});
    doc["labels"].push_back(json{{"id", "l1"},
                                 {"sheet", "sh1"},
                                 {"text", "SIG"},
                                 {"scope", "local"},
                                 {"pos", json::array({-10.0, 0.0})},
                                 {"angle", 0}});

    const json nl = build_netlist(doc);
    CHECK(net_of(nl, "s1", "1") == "SIG");
}

TEST_CASE("same local label text on two sheets stays two nets") {
    json doc = schema::new_schematic("Scoped");
    doc["sheets"] = json::array({json{{"id", "sh1"}, {"name", "A"}},
                                 json{{"id", "sh2"}, {"name", "B"}}});
    doc["lib_cache"]["Device:R"] = res_def();
    int n = 0;
    for (const char* sh : {"sh1", "sh2"}) {
        ++n;
        const double y = n * 20.0;  // distinct geometry, same LABEL text
        doc["symbols"].push_back(json{{"id", "s" + std::to_string(n)},
                                      {"libid", "Device:R"},
                                      {"ref", "R" + std::to_string(n)},
                                      {"value", "1k"},
                                      {"sheet", sh},
                                      {"pos", json::array({0.0, y})},
                                      {"angle", 0},
                                      {"mirror", "none"},
                                      {"unit", 1}});
        doc["labels"].push_back(json{{"id", "l" + std::to_string(n)},
                                     {"sheet", sh},
                                     {"text", "CLK"},
                                     {"scope", "local"},
                                     {"pos", json::array({-2.54, y})},
                                     {"angle", 0}});
    }

    const json nl = build_netlist(doc);
    const std::string a = net_of(nl, "s1", "1");
    const std::string b = net_of(nl, "s2", "1");
    CHECK(!a.empty());
    CHECK(!b.empty());
    CHECK(a != b);  // one of them is disambiguated (CLK vs CLK@sh2)
}

TEST_CASE("a global label DOES merge across sheets") {
    json doc = schema::new_schematic("Global");
    doc["sheets"] = json::array({json{{"id", "sh1"}, {"name", "A"}},
                                 json{{"id", "sh2"}, {"name", "B"}}});
    doc["lib_cache"]["Device:R"] = res_def();
    int n = 0;
    for (const char* sh : {"sh1", "sh2"}) {
        ++n;
        const double y = n * 20.0;
        doc["symbols"].push_back(json{{"id", "s" + std::to_string(n)},
                                      {"libid", "Device:R"},
                                      {"ref", "R" + std::to_string(n)},
                                      {"value", "1k"},
                                      {"sheet", sh},
                                      {"pos", json::array({0.0, y})},
                                      {"angle", 0},
                                      {"mirror", "none"},
                                      {"unit", 1}});
        doc["labels"].push_back(json{{"id", "l" + std::to_string(n)},
                                     {"sheet", sh},
                                     {"text", "RESET"},
                                     {"scope", "global"},
                                     {"pos", json::array({-2.54, y})},
                                     {"angle", 0}});
    }

    const json nl = build_netlist(doc);
    CHECK(net_of(nl, "s1", "1") == "RESET");
    CHECK(net_of(nl, "s2", "1") == "RESET");
}

TEST_CASE("a bus expands to members and by_object maps entities to nets") {
    json doc = schema::new_schematic("Bussed");
    doc["buses"].push_back(json{{"id", "b1"},
                                {"sheet", "sh1"},
                                {"name", "D[0..3]"},
                                {"pts", json::array({json::array({0.0, 0.0}),
                                                     json::array({50.8, 0.0})})}});
    const json nl = build_netlist(doc);
    REQUIRE(nl.contains("buses"));
    REQUIRE(nl["buses"].contains("D[0..3]"));
    const json members = nl["buses"]["D[0..3]"];
    CHECK(members.size() == 4);
    CHECK(members[0] == "D0");
    CHECK(members[3] == "D3");
    // by_object is the GUI's net-highlight source; a named bus must appear in it.
    REQUIRE(nl.contains("by_object"));
    CHECK(nl["by_object"].contains("b1"));
}

TEST_CASE("two units of one reference emit ONE component") {
    json def = res_def();
    def["libid"] = "Amp:DUAL";
    def["units"] = 2;
    def["pins"][0]["unit"] = 1;
    def["pins"][1]["unit"] = 2;

    json doc = schema::new_schematic("MultiUnit");
    doc["lib_cache"]["Amp:DUAL"] = def;
    for (int u = 1; u <= 2; ++u)
        doc["symbols"].push_back(json{{"id", "s" + std::to_string(u)},
                                      {"libid", "Amp:DUAL"},
                                      {"ref", "U1"},  // SAME reference, different unit
                                      {"value", "DUAL"},
                                      {"sheet", "sh1"},
                                      {"pos", json::array({0.0, u * 20.0})},
                                      {"angle", 0},
                                      {"mirror", "none"},
                                      {"unit", u}});

    const json nl = build_netlist(doc);
    int u1 = 0;
    for (const auto& c : nl["components"])
        if (c.value("ref", std::string()) == "U1") ++u1;
    CHECK(u1 == 1);

    // …and the schema must ACCEPT that document (v1 rejected any shared ref).
    CHECK(schema::validate_schematic(doc).empty());
}

TEST_CASE("assembly flags reach the netlist components") {
    json doc = schema::new_schematic("Flags");
    doc["lib_cache"]["Device:R"] = res_def();
    doc["symbols"].push_back(json{{"id", "s1"},
                                  {"libid", "Device:R"},
                                  {"ref", "R1"},
                                  {"value", "1k"},
                                  {"sheet", "sh1"},
                                  {"pos", json::array({0.0, 0.0})},
                                  {"angle", 0},
                                  {"mirror", "none"},
                                  {"unit", 1},
                                  {"dnp", true},
                                  {"in_bom", false},
                                  {"on_board", true}});
    const json nl = build_netlist(doc);
    REQUIRE(nl["components"].size() == 1);
    const json& c = nl["components"][0];
    CHECK(c.value("dnp", false) == true);
    CHECK(c.value("in_bom", true) == false);
    CHECK(c.value("on_board", false) == true);
}

// ------------------------------------------------------------------------- ERC

TEST_CASE("a passive-only net raises no_driver") {
    json doc = schema::new_schematic("Passive");
    doc["lib_cache"]["Device:R"] = res_def();
    // Two resistors abutting pin-to-pin: an unnamed, driverless, purely passive net.
    doc["symbols"].push_back(json{{"id", "s1"}, {"libid", "Device:R"}, {"ref", "R1"},
                                  {"value", "1k"}, {"sheet", "sh1"},
                                  {"pos", json::array({0.0, 0.0})}, {"angle", 0},
                                  {"mirror", "none"}, {"unit", 1}});
    doc["symbols"].push_back(json{{"id", "s2"}, {"libid", "Device:R"}, {"ref", "R2"},
                                  {"value", "1k"}, {"sheet", "sh1"},
                                  {"pos", json::array({5.08, 0.0})}, {"angle", 0},
                                  {"mirror", "none"}, {"unit", 1}});
    const json nl = build_netlist(doc);
    // s1.2 tip (2.54,0) meets s2.1 tip (2.54,0) — one shared passive net.
    REQUIRE(net_of(nl, "s1", "2") == net_of(nl, "s2", "1"));
    CHECK(has_rule(run_erc(doc, nl), "no_driver"));
}

TEST_CASE("a plain GND port does not satisfy power_not_driven") {
    json pwr = res_def();
    pwr["libid"] = "IC:U";
    pwr["pins"][0]["type"] = "power_in";

    json doc = schema::new_schematic("Supply");
    doc["lib_cache"]["IC:U"] = pwr;
    doc["symbols"].push_back(json{{"id", "s1"}, {"libid", "IC:U"}, {"ref", "U1"},
                                  {"value", "U"}, {"sheet", "sh1"},
                                  {"pos", json::array({0.0, 0.0})}, {"angle", 0},
                                  {"mirror", "none"}, {"unit", 1}});
    // An ordinary power PORT on the power_in pin — NOT a source.
    doc["power"].push_back(json{{"id", "p1"}, {"sheet", "sh1"}, {"net", "VCC"},
                                {"symbol", "power:VCC"},
                                {"pos", json::array({-2.54, 0.0})}});

    const json v = run_erc(doc, build_netlist(doc));
    // The old code silently accepted this. Now it must say something actionable.
    CHECK((has_rule(v, "power_not_driven") || has_rule(v, "power_flag_needed")));
}

TEST_CASE("PWR_FLAG drives only its physical net and never shorts supplies") {
    json doc = schema::new_schematic("Separate supplies");
    doc["lib_cache"]["Device:R"] = res_def();
    for (int i = 0; i < 2; ++i)
        doc["symbols"].push_back(json{{"id", "s" + std::to_string(i + 1)},
                                      {"libid", "Device:R"}, {"ref", "R" + std::to_string(i + 1)},
                                      {"value", "1k"}, {"sheet", "sh1"},
                                      {"pos", json::array({25.4 * i, 0.0})},
                                      {"angle", 0}, {"mirror", "none"}, {"unit", 1}});
    doc["power"] = json::array({
        {{"id", "p1"}, {"sheet", "sh1"}, {"net", "PWR_FLAG"}, {"symbol", "PWR_FLAG"},
         {"pos", {-2.54, 0.0}}},
        {{"id", "p2"}, {"sheet", "sh1"}, {"net", "PWR_FLAG"}, {"symbol", "power:PWR_FLAG"},
         {"pos", {22.86, 0.0}}},
        {{"id", "p3"}, {"sheet", "sh1"}, {"net", "VCC"}, {"symbol", "VCC"},
         {"pos", {-2.54, 0.0}}},
        {{"id", "p4"}, {"sheet", "sh1"}, {"net", "GND"}, {"symbol", "GND"},
         {"pos", {22.86, 0.0}}}});
    const json nl = build_netlist(doc);
    CHECK(net_of(nl, "s1", "1") == "VCC");
    CHECK(net_of(nl, "s2", "1") == "GND");
    CHECK(nl["net_info"]["VCC"]["has_power_source"] == true);
    CHECK(nl["net_info"]["GND"]["has_power_source"] == true);
}

TEST_CASE("a no-connect flag on a wired pin is an error") {
    json doc = schema::new_schematic("NC");
    doc["lib_cache"]["Device:R"] = res_def();
    doc["symbols"].push_back(json{{"id", "s1"}, {"libid", "Device:R"}, {"ref", "R1"},
                                  {"value", "1k"}, {"sheet", "sh1"},
                                  {"pos", json::array({0.0, 0.0})}, {"angle", 0},
                                  {"mirror", "none"}, {"unit", 1}});
    // A wire on pin 1 …and an NC marker on the same pin. Contradiction.
    doc["wires"].push_back(json{{"id", "w1"}, {"sheet", "sh1"},
                                {"pts", json::array({json::array({-2.54, 0.0}),
                                                     json::array({-12.7, 0.0})})},
                                {"bus", false}});
    doc["no_connect"].push_back(json{{"id", "nc1"}, {"sheet", "sh1"},
                                     {"pos", json::array({-2.54, 0.0})}});

    CHECK(has_rule(run_erc(doc, build_netlist(doc)), "no_connect_connected"));
}

TEST_CASE("ERC on a non-schematic says so instead of returning silence") {
    const json board = schema::new_board("B");
    const json v = run_erc(board, json::object());
    CHECK(has_rule(v, "not_a_schematic"));
}

// ------------------------------------------------------------- symbol library

TEST_CASE("the native symbol library resolves real parts") {
    const json r = symlib::resolve_symbol("Device:R");
    REQUIRE(r.is_object());
    CHECK(r["pins"].size() == 2);
    CHECK(r.value("prefix", std::string()) == "R");

    const json gnd = symlib::resolve_symbol("power:GND");
    REQUIRE(gnd.is_object());
    CHECK(gnd.value("power", false) == true);

    const json flag = symlib::resolve_symbol("power:PWR_FLAG");
    REQUIRE(flag.is_object());
    // PWR_FLAG is the ONLY power symbol that drives a supply net — ERC depends on it.
    REQUIRE(flag["pins"].size() == 1);
    CHECK(flag["pins"][0].value("type", std::string()) == "power_out");

    const json ne555 = symlib::resolve_symbol("Timer:NE555D");
    REQUIRE(ne555.is_object());
    CHECK(ne555["pins"].size() == 8);
    const json ne555p = symlib::resolve_symbol("Timer:NE555P");
    REQUIRE(ne555p.is_object());
    CHECK(ne555p["pins"].size() == 8);
    CHECK(ne555p.value("default_footprint", std::string()) == "Package_DIP:DIP-8_W7.62mm");

    CHECK(symlib::resolve_symbol("Nope:DoesNotExist").is_null());
    CHECK(symlib::symbol_libids().size() > 40);

    const json hits = symlib::search_symbols("resistor", 5);
    CHECK(hits.value("count", 0) > 0);
}

TEST_CASE("sch_add_symbol resolves from the library instead of fabricating") {
    const json doc = build(json::array({json{{"op", "sch_add_symbol"},
                                             {"libid", "Timer:NE555D"},
                                             {"pos", json::array({0.0, 0.0})}}}));
    REQUIRE(doc["symbols"].size() == 1);
    const json def = doc["lib_cache"]["Timer:NE555D"];
    REQUIRE(def.is_object());
    CHECK(def["pins"].size() == 8);
    CHECK(def.value("generated", false) == false);
    // …and the reference prefix comes from the definition, not a guess.
    CHECK(doc["symbols"][0].value("ref", std::string()).rfind("U", 0) == 0);
}

TEST_CASE("an unknown libid degrades visibly, not silently") {
    const json doc = build(json::array({json{{"op", "sch_add_symbol"},
                                             {"libid", "Bogus:Nothing"},
                                             {"pos", json::array({0.0, 0.0})}}}));
    REQUIRE(doc["symbols"].size() == 1);
    // The batch still applies (an import must not fail wholesale) but the fabricated
    // body is flagged so no layer can mistake it for a real part.
    CHECK(doc["lib_cache"]["Bogus:Nothing"].value("generated", false) == true);
}

// --------------------------------------------------------------- op vocabulary

TEST_CASE("every schematic entity can be created, edited and deleted") {
    json created = ecad_new("sch", "Vocab");
    const std::string id = created.value("doc_id", std::string());
    long rev = created.value("revision", 0L);

    json add = json::array({
        json{{"op", "sch_add_bus"}, {"name", "D[0..1]"},
             {"pts", json::array({json::array({0.0, 0.0}), json::array({25.4, 0.0})})}},
        json{{"op", "sch_add_bus_entry"}, {"pos", json::array({5.08, 0.0})}},
        json{{"op", "sch_add_text"}, {"pos", json::array({0.0, 10.0})}, {"text", "note"}},
        json{{"op", "sch_add_graphic"}, {"kind", "rect"},
             {"pts", json::array({json::array({0.0, 0.0}), json::array({10.0, 10.0})})}},
        json{{"op", "sch_add_power"}, {"pos", json::array({0.0, -5.0})}, {"net", "GND"}},
        json{{"op", "sch_add_no_connect"}, {"pos", json::array({0.0, -10.0})}},
        json{{"op", "sch_add_junction"}, {"pos", json::array({0.0, -15.0})}},
        json{{"op", "sch_set_title_block"}, {"title", "Vocab sheet"}, {"rev", "A"}},
    });
    json r = ecad_apply_ops(id, rev, add);
    rev = r.value("revision", rev);

    json doc = ecad_session_doc(id);
    CHECK(doc["buses"].size() == 1);
    CHECK(doc["bus_entries"].size() == 1);
    CHECK(doc["texts"].size() == 1);
    CHECK(doc["graphics"].size() == 1);
    CHECK(doc["power"].size() == 1);
    CHECK(doc["no_connect"].size() == 1);
    CHECK(doc["junctions"].size() == 1);
    CHECK(doc["title_block"].value("title", std::string()) == "Vocab sheet");

    // Now delete each one — v1 could create these but never remove them.
    json del = json::array({
        json{{"op", "sch_delete_bus_entry"}, {"id", doc["bus_entries"][0]["id"]}},
        json{{"op", "sch_delete_bus"}, {"id", doc["buses"][0]["id"]}},
        json{{"op", "sch_delete_text"}, {"id", doc["texts"][0]["id"]}},
        json{{"op", "sch_delete_graphic"}, {"id", doc["graphics"][0]["id"]}},
        json{{"op", "sch_delete_power"}, {"id", doc["power"][0]["id"]}},
        json{{"op", "sch_delete_no_connect"}, {"id", doc["no_connect"][0]["id"]}},
        json{{"op", "sch_delete_junction"}, {"id", doc["junctions"][0]["id"]}},
    });
    ecad_apply_ops(id, rev, del);
    doc = ecad_session_doc(id);
    CHECK(doc["buses"].empty());
    CHECK(doc["bus_entries"].empty());
    CHECK(doc["texts"].empty());
    CHECK(doc["graphics"].empty());
    CHECK(doc["power"].empty());
    CHECK(doc["no_connect"].empty());
    CHECK(doc["junctions"].empty());

    ecad_close(id);
}


// --------------------------------------------------------------- save safety

TEST_CASE("saving an imported KiCad schematic writes KiCad to its bound source") {
    const char* kMinimal = R"KICAD(
(kicad_sch (version 20221018) (generator test)
  (lib_symbols
    (symbol "Device:R"
      (symbol "R_1_1"
        (pin passive line (at -2.54 0 0) (length 2.54) (name "~") (number "1"))
        (pin passive line (at 2.54 0 180) (length 2.54) (name "~") (number "2"))
      )
      (property "Reference" "R" (at 0 0 0))
      (property "Value" "R" (at 0 0 0))
    )
  )
  (symbol (lib_id "Device:R") (at 50 50 0) (unit 1)
    (property "Reference" "R1" (at 0 0 0))
    (property "Value" "10k" (at 0 0 0))
  )
)
)KICAD";

    const fs::path dir = fs::temp_directory_path() / "chudware_sch_save_test";
    fs::remove_all(dir);
    fs::create_directories(dir);
    const fs::path src = dir / "board.kicad_sch";
    {
        std::ofstream fh(src);
        fh << kMinimal;
    }

    const json opened = ecad_open(src.string());
    const std::string id = opened.value("doc_id", std::string());
    REQUIRE(!id.empty());
    CHECK(opened.value("path", std::string()) == src.string());
    CHECK(opened["warnings"].empty());
    const json saved = ecad_save(id, "");
    CHECK(saved.value("path", std::string()) == src.string());
    CHECK(saved.value("format", std::string()) == "kicad");
    std::ifstream fh(src, std::ios::binary);
    const std::string bytes{std::istreambuf_iterator<char>(fh), std::istreambuf_iterator<char>()};
    CHECK(bytes.find("(kicad_sch") != std::string::npos);
    const json reopened = ecad_open(src.string());
    CHECK(reopened["tree"]["symbols"].size() == 1);
    ecad_close(reopened["doc_id"].get<std::string>());

    ecad_close(id);
    fs::remove_all(dir);
}

// ---------------------------------------------------------------- ecad_design

TEST_CASE("ecad_design compiles a recipe into a real, connected schematic") {
    const json res = ecad_design("a 5V to 3.3V resistive divider", "");
    const std::string id = res.value("doc_id", std::string());
    REQUIRE(!id.empty());
    CHECK(res.value("recipe_used", std::string()) == "voltage_divider");

    const json doc = ecad_session_doc(id);
    // Real placed symbols with real library bodies — not an abstract dict.
    CHECK(doc["symbols"].size() >= 3);
    CHECK(!doc["wires"].empty());
    CHECK(doc["title_block"].value("title", std::string()).empty() == false);

    // And it is genuinely wired: the divider midpoint carries two pins.
    const json nl = build_netlist(doc);
    bool found_multi_pin_net = false;
    for (auto it = nl["nets"].begin(); it != nl["nets"].end(); ++it)
        if (it.value().is_array() && it.value().size() >= 2) found_multi_pin_net = true;
    CHECK(found_multi_pin_net);

    ecad_close(id);
}

TEST_CASE("all design recipes use numbered real KiCad pins and export simulatable circuits") {
    const std::vector<std::pair<std::string, std::string>> cases = {
        {"a 5V to 3.3V voltage divider", "voltage_divider"},
        {"an RC low-pass filter", "rc_lowpass"},
        {"an inverting op-amp amplifier", "inverting_amp"},
        {"a 5V LED driver", "led_driver"},
        {"a 555 astable oscillator", "rc_555_astable"},
    };
    const bool have_kicad = ecad_kicad_status().value("found", false);
    for (const auto& [request, expected] : cases) {
        INFO("recipe: " << expected);
        const json result = ecad_design(request, expected);
        REQUIRE(result["recipe_used"] == expected);
        const std::string id = result["doc_id"].get<std::string>();
        const json doc = ecad_session_doc(id);
        const json netlist = build_netlist(doc);
        std::map<std::string, std::string> ids_by_ref;
        for (const auto& symbol : doc["symbols"]) {
            ids_by_ref[symbol["ref"].get<std::string>()] = symbol["id"].get<std::string>();
            CHECK_FALSE(doc["lib_cache"][symbol["libid"].get<std::string>()].value("generated", false));
        }
        std::set<std::string> resolved_nets;
        for (auto net = result["design"]["nets"].begin(); net != result["design"]["nets"].end(); ++net) {
            std::string resolved;
            for (const auto& member : net.value()) {
                const auto ref = member["ref"].get<std::string>();
                const auto number = member["pin"].get<std::string>();
                REQUIRE(ids_by_ref.count(ref) == 1);
                const auto actual = net_of(netlist, ids_by_ref.at(ref), number);
                INFO("net " << net.key() << " member " << ref << "." << number);
                CHECK_FALSE(actual.empty());
                if (resolved.empty()) resolved = actual;
                CHECK(actual == resolved);
            }
            CHECK(resolved_nets.insert(resolved).second);
        }
        const json circuit = chudware::sim::netlist_to_spice_circuit(netlist, json::object());
        INFO("native simulation warnings: " << circuit["warnings"].dump());
        CHECK(circuit["warnings"].empty());
        CHECK_FALSE(circuit["circuit"]["devices"].empty());
        if (expected == "voltage_divider" || expected == "inverting_amp" || expected == "led_driver") {
            const json expect = result["design"]["study"]["expect"];
            const std::string probe = expect["probe"].get<std::string>();
            const json study{{"analyses", json::array({json{{"type", "op"}}})},
                             {"probes", json::array({probe})}};
            const json simulation = ecad_simulate(id, study, json::object());
            INFO("native operating point: " << simulation.dump());
            REQUIRE(simulation["converged"].get<bool>());
            REQUIRE(simulation["results"]["op"][probe].is_number());
            CHECK(simulation["results"]["op"][probe].get<double>() ==
                  doctest::Approx(expect["value"].get<double>()).epsilon(0.05));
        }
        if (have_kicad) {
            const fs::path dir = fs::temp_directory_path() / ("chudware-recipe-" + id);
            const fs::path file = dir / (expected + ".kicad_sch");
            fs::create_directories(dir);
            ecad_save(id, file.string());
            const json check = ecad_kicad_check(id);
            INFO("KiCad ERC: " << check.dump());
            CHECK(check["counts"]["error"] == 0);
            const json exported = ecad_export(json{{"doc_id", id}, {"kind", "spice"}});
            REQUIRE(exported["files"].size() == 1);
            const std::string spice = exported["files"][0]["content"].get<std::string>();
            CHECK(spice.find("V1 ") != std::string::npos);
            if (expected == "inverting_amp") CHECK(spice.find("kicad_builtin_opamp") != std::string::npos);
            if (expected == "led_driver") CHECK(spice.find(".model __D1 D") != std::string::npos);
            if (expected == "rc_555_astable") {
                CHECK(spice.find(".subckt NE555") != std::string::npos);
                CHECK(spice.find("XU1 ") != std::string::npos);
            }
            fs::remove_all(dir);
        }
        ecad_close(id);
    }
}

// -------------------------------------------------------------- resolve_pick

TEST_CASE("resolve_pick names the entity under a point") {
    json created = ecad_new("sch", "Pick");
    const std::string id = created.value("doc_id", std::string());
    ecad_apply_ops(id, created.value("revision", 0L),
                   json::array({json{{"op", "sch_add_symbol"},
                                     {"libid", "Device:R"},
                                     {"pos", json::array({0.0, 0.0})},
                                     {"ref", "R1"}}}));
    const json doc = ecad_session_doc(id);
    const json def = doc["lib_cache"]["Device:R"];
    const auto tip = schema::pin_world(doc["symbols"][0], def["pins"][0]);

    const json hit = ecad_resolve_pick(id, json::array({tip.first, tip.second}), json(nullptr),
                                       0.5, "");
    REQUIRE(hit["selector"].is_object());
    CHECK(hit["selector"].value("kind", std::string()) == "pin");
    CHECK(hit["selector"].value("ref", std::string()) == "R1");

    // Far away: an honest null, not a nearest-neighbour guess.
    const json miss =
        ecad_resolve_pick(id, json::array({500.0, 500.0}), json(nullptr), 0.5, "");
    CHECK(miss["selector"].is_null());

    ecad_close(id);
}

// ------------------------------------------------------------------- describe

TEST_CASE("describe reports honestly and covers every entity kind") {
    json created = ecad_new("sch", "Described");
    const std::string id = created.value("doc_id", std::string());
    ecad_apply_ops(id, created.value("revision", 0L),
                   json::array({json{{"op", "sch_add_symbol"},
                                     {"libid", "Device:R"},
                                     {"pos", json::array({0.0, 0.0})},
                                     {"ref", "R1"}}}));

    const json d = ecad_describe(id, 0);
    CHECK(d.value("kind", std::string()) == "sch");
    CHECK(d.value("compiled", false) == true);
    REQUIRE(d.contains("counts"));
    // v1 reported only symbols/wires/nets; buses, texts and hierarchy were invisible.
    for (const char* k : {"sheets", "symbols", "wires", "buses", "bus_entries", "junctions",
                          "labels", "power", "no_connect", "texts", "graphics",
                          "sheet_symbols"})
        CHECK(d["counts"].contains(k));
    REQUIRE(d.contains("erc"));
    CHECK(d["erc"].value("available", false) == true);
    REQUIRE(d["erc"]["top"].is_array());
    bool saw_nullable_net = false;
    for (const auto& violation : d["erc"]["top"])
        if (violation.contains("net") && violation["net"].is_null())
            saw_nullable_net = true;
    CHECK(saw_nullable_net);

    ecad_close(id);
}

TEST_CASE("ECAD lifecycle dirty count follows edit save undo redo and close") {
    std::uint64_t before = 0, count = 0;
    REQUIRE(dirty_document_count(before));
    const auto id = ecad_new("sch", "Activity").at("doc_id").get<std::string>();
    const auto path = fs::temp_directory_path() / (id + "-activity.kicad_sch");
    struct RemoveFile {
        fs::path path;
        ~RemoveFile() {
            std::error_code ec;
            fs::remove(path, ec);
            auto project = path; project.replace_extension(".kicad_pro");
            fs::remove(project, ec);
        }
    } remove{path};
    ecad_apply_ops(id, -1, json::array());
    REQUIRE(dirty_document_count(count)); CHECK(count == before);
    ecad_apply_ops(id, -1, json::array({json{{"op", "sch_add_symbol"},
                                              {"libid", "Device:R"},
                                              {"pos", json::array({0.0, 0.0})},
                                              {"ref", "R1"}}}));
    count = 0; REQUIRE(dirty_document_count(count)); CHECK(count == before + 1);
    ecad_save(id, path.string());
    count = 0; REQUIRE(dirty_document_count(count)); CHECK(count == before);
    ecad_undo(id);
    count = 0; REQUIRE(dirty_document_count(count)); CHECK(count == before + 1);
    ecad_save(id, path.string());
    ecad_redo(id);
    count = 0; REQUIRE(dirty_document_count(count)); CHECK(count == before + 1);
    REQUIRE(ecad_close(id));
    count = 0; REQUIRE(dirty_document_count(count)); CHECK(count == before);
}
