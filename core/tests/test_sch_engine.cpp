// Schematic engine contract tests — the connectivity, ERC, library, export and
// save-safety behaviors that the 2026-08 schematic overhaul fixed. Each case pins a
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
//   * a schematic could export nothing at all;
//   * a pathless Save over an imported .kicad_sch replaced it with ZIP bytes.
//
// Pure JSON, OCCT-free; links chudware_ecad only.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include "ecad/container.h"
#include "ecad/erc.h"
#include "ecad/kicad.h"
#include "ecad/netlist.h"
#include "ecad/ops.h"
#include "ecad/schema.h"
#include "ecad/schexport.h"
#include "ecad/schplot.h"
#include "ecad/session.h"
#include "ecad/symlib.h"

#include <cstdio>
#include <filesystem>
#include <fstream>
#include <set>
#include <iterator>
#include <string>

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
    const fs::path path = fs::temp_directory_path() / "chudware_ecad_ref_guard.ecad+";
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
        ecad_container_save(doc, path.string(), {});
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

    const json ne555 = symlib::resolve_symbol("Timer:NE555");
    REQUIRE(ne555.is_object());
    CHECK(ne555["pins"].size() == 8);

    CHECK(symlib::resolve_symbol("Nope:DoesNotExist").is_null());
    CHECK(symlib::symbol_libids().size() > 40);

    const json hits = symlib::search_symbols("resistor", 5);
    CHECK(hits.value("count", 0) > 0);
}

TEST_CASE("sch_add_symbol resolves from the library instead of fabricating") {
    const json doc = build(json::array({json{{"op", "sch_add_symbol"},
                                             {"libid", "Timer:NE555"},
                                             {"pos", json::array({0.0, 0.0})}}}));
    REQUIRE(doc["symbols"].size() == 1);
    const json def = doc["lib_cache"]["Timer:NE555"];
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

// -------------------------------------------------------------------- exports

TEST_CASE("a schematic exports every deliverable kind") {
    json created = ecad_new("sch", "Exports");
    const std::string id = created.value("doc_id", std::string());
    ecad_apply_ops(id, created.value("revision", 0L),
                   json::array({json{{"op", "sch_add_symbol"},
                                     {"libid", "Device:R"},
                                     {"pos", json::array({0.0, 0.0})},
                                     {"ref", "R1"},
                                     {"value", "10k"}},
                                json{{"op", "sch_add_symbol"},
                                     {"libid", "Device:R"},
                                     {"pos", json::array({25.4, 0.0})},
                                     {"ref", "R2"},
                                     {"value", "10k"}}}));

    const fs::path dir = fs::temp_directory_path() / "chudware_sch_export_test";
    fs::remove_all(dir);

    for (const char* kind : {"netlist", "bom", "spice", "kicad", "svg", "pdf"}) {
        const json res =
            ecad_export(json{{"doc_id", id}, {"kind", kind}, {"path", dir.string()}});
        INFO("kind=", kind);
        REQUIRE(res.contains("files"));
        CHECK(!res["files"].empty());
        // `paths` is the honesty contract: it lists what was ACTUALLY written.
        REQUIRE(res.contains("paths"));
        REQUIRE(!res["paths"].empty());
        for (const auto& p : res["paths"]) {
            const fs::path written = p.get<std::string>();
            CHECK(fs::exists(written));
            CHECK(fs::file_size(written) > 0);
        }
    }

    // Content spot-checks: the BOM groups the two identical resistors, and the PDF is real.
    const json bom = ecad_export(json{{"doc_id", id}, {"kind", "bom"}});
    const std::string csv = bom["files"]["bom"].get<std::string>();
    CHECK(csv.find("R1,R2") != std::string::npos);

    const json pdf = ecad_export(json{{"doc_id", id}, {"kind", "pdf"}});
    CHECK(pdf["files"]["pdf"].get<std::string>().rfind("%PDF", 0) == 0);

    const json svg = ecad_export(json{{"doc_id", id}, {"kind", "svg"}});
    CHECK(svg["files"].begin().value().get<std::string>().find("<svg") != std::string::npos);

    // A pcb-only kind must still be refused on a schematic.
    CHECK_THROWS_AS(ecad_export(json{{"doc_id", id}, {"kind", "gerber"}}), OpError);
    // …and an unknown kind is an error, not a silent empty success.
    CHECK_THROWS_AS(ecad_export(json{{"doc_id", id}, {"kind", "nonsense"}}), OpError);

    fs::remove_all(dir);
    ecad_close(id);
}

TEST_CASE("ecad_export without a path writes nothing and says so") {
    json created = ecad_new("sch", "NoPath");
    const std::string id = created.value("doc_id", std::string());
    const json res = ecad_export(json{{"doc_id", id}, {"kind", "netlist"}});
    CHECK(!res["files"].empty());
    CHECK(res["paths"].empty());  // the GUI reports from this, never from "no error"
    ecad_close(id);
}

TEST_CASE("SVG sheet identifiers cannot escape the selected export directory") {
    const fs::path root = fs::temp_directory_path() / "chudware_svg_leaf_security";
    fs::remove_all(root);
    const fs::path alice = root / "alice";
    const fs::path marker = root / "bob" / "proof.svg";
    fs::create_directories(alice / "Schematic-x");
    fs::create_directories(marker.parent_path());
    { std::ofstream out(marker); out << "sibling marker"; }
    auto marker_text = [&]() {
        std::ifstream in(marker, std::ios::binary);
        return std::string(std::istreambuf_iterator<char>(in), std::istreambuf_iterator<char>());
    };
    const json doc{{"schema", "aicad.sch/1"}, {"name", "Schematic"}};
    for (const std::string sheet : {"x/../../bob/proof", "..", ".", "../proof",
                                    "x\\..\\..\\bob\\proof", "C:proof"}) {
        CHECK_THROWS_AS(ecad_export(json{{"doc", doc}, {"kind", "svg"}, {"path", alice.string()},
                                         {"opts", {{"sheet", sheet}}}}), OpError);
        CHECK(marker_text() == "sibling marker");
    }
    json hostile_doc = doc;
    hostile_doc["sheets"] = json::array({json{{"id", "x/../../bob/proof"}}});
    CHECK_THROWS_AS(ecad_export(json{{"doc", hostile_doc}, {"kind", "svg"},
                                     {"path", alice.string()}}), OpError);
    CHECK(marker_text() == "sibling marker");

    const json exported = ecad_export(json{{"doc", doc}, {"kind", "svg"},
                                          {"path", alice.string()}, {"opts", {{"sheet", "sh1"}}}});
    REQUIRE(exported["paths"].size() == 1);
    const fs::path output = exported["paths"][0].get<std::string>();
    CHECK(fs::canonical(output.parent_path()) == fs::canonical(alice));
    std::ifstream in(output);
    const std::string text{std::istreambuf_iterator<char>(in), std::istreambuf_iterator<char>()};
    in.close();
    CHECK(text.find("<svg") != std::string::npos);
    CHECK(marker_text() == "sibling marker");
    fs::remove_all(root);
}

TEST_CASE("SVG export never follows an existing output symlink") {
    const fs::path root = fs::temp_directory_path() / "chudware_svg_nofollow_security";
    fs::remove_all(root);
    fs::create_directories(root / "alice");
    fs::create_directories(root / "bob");
    const fs::path marker = root / "bob" / "proof.svg";
    { std::ofstream out(marker); out << "sibling marker"; }
    const fs::path output = root / "alice" / "Schematic-sh1.svg";
    std::error_code ec;
    fs::create_symlink(marker, output, ec);
    if (!ec) {
        const json doc{{"schema", "aicad.sch/1"}, {"name", "Schematic"}};
        CHECK_THROWS_AS(ecad_export(json{{"doc", doc}, {"kind", "svg"},
                                         {"path", (root / "alice").string()}}), OpError);
        CHECK_THROWS_AS(ecad_export(json{{"doc", doc}, {"kind", "svg"},
                                         {"path", output.string()}}), OpError);
        std::ifstream in(marker);
        const std::string text{std::istreambuf_iterator<char>(in), std::istreambuf_iterator<char>()};
        CHECK(text == "sibling marker");
    } else {
        INFO("Platform does not permit creating a fixture symlink: ", ec.message());
    }
    fs::remove_all(root);
}

// --------------------------------------------------------------- save safety

TEST_CASE("saving an imported KiCad schematic never overwrites the source") {
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
    const auto original_size = fs::file_size(src);

    const json opened = ecad_open(src.string());
    const std::string id = opened.value("doc_id", std::string());
    REQUIRE(!id.empty());
    // The import warns that Save targets a native sibling.
    CHECK(!opened["warnings"].empty());

    // A PATHLESS save must write the sibling .ecad+, not the source.
    const json saved = ecad_save(id, "");
    const fs::path out = saved.value("path", std::string());
    CHECK(out != src);
    CHECK(out.extension() == ".ecad+");
    CHECK(fs::exists(out));
    CHECK(saved.value("source_path", std::string()) == src.string());
    // The KiCad file is byte-for-byte untouched.
    CHECK(fs::file_size(src) == original_size);

    // An EXPLICIT attempt to write over the source is refused, not obeyed.
    CHECK_THROWS_AS(ecad_save(id, src.string()), OpError);
    CHECK(fs::file_size(src) == original_size);

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

    ecad_close(id);
}

TEST_CASE("ECAD lifecycle dirty count follows edit save undo redo and close") {
    std::uint64_t before = 0, count = 0;
    REQUIRE(dirty_document_count(before));
    const auto id = ecad_new("sch", "Activity").at("doc_id").get<std::string>();
    const auto path = fs::temp_directory_path() / (id + "-activity.ecad+");
    struct RemoveFile {
        fs::path path;
        ~RemoveFile() { std::error_code ec; fs::remove(path, ec); }
    } remove{path};
    ecad_apply_ops(id, -1, json::array());
    REQUIRE(dirty_document_count(count)); CHECK(count == before + 1);
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
