// core/tests/test_parts_place.cpp — part_place placement router (C++ port of
// place.py). Mirrors the offline-capable legs of
// services/parts/test_parts_hub_p3.py: every placer returns a PlacementResult
// dict, success always has editable=true, and doc-mutating legs hand the
// updated document back under result["doc"]. The hub lookup + doc-creating
// RPCs are stubbed via PlaceSeams (the same monkeypatch shape the Python
// oracle tests use); the ecad/piping/hydraulic legs run the REAL C++ schema
// factories + apply_ops.
#define DOCTEST_CONFIG_IMPLEMENT
#include <doctest/doctest.h>

#include <cmath>
#include <filesystem>
#include <fstream>
#include <string>
#include <algorithm>

#include <cstdlib>
#include "ecad/schema.h"
#include "hydraulic/schema.h"
#include "parts/place.h"
#include "piping/schema.h"
#ifdef CHUDWARE_PARTS_NATIVE_TESTS
#include "api/libchudware.h"
#endif

using namespace chudware::parts;
using nlohmann::json;

namespace fs = std::filesystem;

// ── shared fixture helpers ─────────────────────────────────────────────────

static json struct_doc() {
    // engine_oracle.structural.elements.StructureDoc("TestStructure").to_dict()
    return json{{"schema", "aicad.structure/1"},
                {"name", "TestStructure"},
                {"units", "mm"},
                {"elements", json::array()},
                {"load_cases", json::array()}};
}

static json sch_doc() { return chudware::ecad::schema::new_schematic("TestSchematic"); }
static json piping_doc() { return chudware::piping::new_piping("TestPiping"); }
static json hydraulic_doc() { return chudware::hydraulic::new_hydraulic("TestHydraulic"); }

// A PartRecord stub for part_place (services/parts hub shape).
static json record_with_gref(json gref, const std::string& domain = "fastener") {
    return json{{"part_id", domain + ":X:1"},
                {"domain", domain},
                {"title", "fixture"},
                {"geometry_ref", std::move(gref)}};
}

// Stub seams mirroring the Python tests' monkeypatches: parts_make /
// import_file return canned core responses; build_model "generates" a STEP.
static PlaceSeams stub_seams(const std::string& step_path, json* captured_make = nullptr) {
    PlaceSeams s;
    s.get_record = [](const std::string&) { return json(); };  // unknown part
    s.parts_make = [captured_make](const json& call_params) -> json {
        if (captured_make) *captured_make = call_params;
        return json{{"ok", true},
                    {"doc_id", "doc-stub-1"},
                    {"name", "SHCS M8x20"},
                    {"bbox", {{"min", {0, 0, 0}}, {"max", {8, 8, 20}}}},
                    {"datum", {{"base_center", {4, 4, 0}}, {"axis", {0, 0, 1}}}}};
    };
    s.import_file = [](const std::string& path, bool) -> json {
        if (!fs::is_regular_file(fs::u8path(path)))
            throw std::runtime_error("file not found: " + path);
        return json{{"doc_id", "doc-gen-1"}, {"body_count", 1}};
    };
    s.build_model = [step_path](const json&) -> std::string { return step_path; };
    s.set_catalog_meta = [](const std::string&, const json&) {};
    return s;
}


// ── doctests ────────────────────────────────────────────────────────────────

TEST_CASE("part_place: unknown part_id returns the oracle error envelope") {
    PlaceSeams seams = stub_seams("");
    json result;
    std::string err;
    CHECK(place_call("part_place", json{{"part_id", "fastener:NONSTD:XXXXX_NOT_REAL"}}, result,
                     err, seams));
    CHECK(result["ok"] == false);
    CHECK(result["editable"] == false);
    CHECK(result["domain"] == "unknown");
    CHECK(result["reason"] == "part_id not found: 'fastener:NONSTD:XXXXX_NOT_REAL'");
    CHECK(result["hint"] == "Use part_search to discover valid part_ids.");
}

TEST_CASE("part_place: record without geometry_ref is actionable") {
    PlaceSeams seams = stub_seams("");
    json rec = record_with_gref(json());
    rec.erase("geometry_ref");
    json result;
    std::string err;
    CHECK(place_call("part_place", json{{"part_id", "fastener:X:1"}, {"record", rec}}, result,
                     err, seams));
    CHECK(result["ok"] == false);
    CHECK(result["reason"] == "PartRecord 'fastener:X:1' has no geometry_ref — cannot place.");
    CHECK(result["hint"] == "This part has no known instantiation recipe.");
}

TEST_CASE("part_place: unknown geometry_ref kind lists the supported kinds") {
    PlaceSeams seams = stub_seams("");
    json result;
    std::string err;
    CHECK(place_call("part_place",
                     json{{"part_id", "fastener:X:1"},
                          {"record", record_with_gref({{"kind", "wat"}})}},
                     result, err, seams));
    CHECK(result["ok"] == false);
    CHECK(result["reason"] == "Unknown geometry_ref kind 'wat'.");
    CHECK(result["hint"] ==
          "Supported kinds: ['core_family', 'fitting_id', 'fmu_component', 'generate_model', "
          "'import_file', 'section_profile', 'symbol_footprint']");
}

TEST_CASE("part_place: acquire-failure envelope surfaces the sign-in path") {
    PlaceSeams seams = stub_seams("");
    json result;
    std::string err;
    CHECK(place_call("part_place",
                     json{{"part_id", "catalog:mcmaster:91292A029"},
                          {"record",
                           {{"part_id", "catalog:mcmaster:91292A029"},
                            {"domain", "catalog"},
                            {"ok", false},
                            {"reason", "login_required"},
                            {"site", "mcmaster"},
                            {"source_url", "https://www.mcmaster.com/91292A029/"},
                            {"title", "Alloy Steel Socket Head Screw"}}}},
                     result, err, seams));
    CHECK(result["ok"] == false);
    CHECK(result["domain"] == "catalog");
    CHECK(result["reason"] == "Cannot place 'catalog:mcmaster:91292A029': login_required.");
    CHECK(result["hint"] ==
          "Sign in first: run vendor_login('mcmaster') to open a browser window, complete "
          "the login once, then retry part_place. Part page: "
          "https://www.mcmaster.com/91292A029/.");
    CHECK(result["detail"]["site"] == "mcmaster");
}

TEST_CASE("core_family: parts_make seam creates an editable part doc") {
    json captured;
    PlaceSeams seams = stub_seams("", &captured);
    json result;
    std::string err;
    CHECK(place_call("part_place",
                     json{{"part_id", "fastener:ISO4762:M8"},
                          {"record", record_with_gref({{"kind", "core_family"},
                                                       {"family", "shcs"},
                                                       {"size", "M8"},
                                                       {"length_mm", 20}})},
                          {"params", {{"length_mm", 25}, {"family", "IGNORED"}}}},
                     result, err, seams));
    CHECK(result["ok"] == true);
    CHECK(result["editable"] == true);
    CHECK(result["domain"] == "core_family");
    CHECK(result["doc_id"] == "doc-stub-1");
    CHECK(result["entity_id"] == "doc-stub-1");
    CHECK(result["detail"]["family"] == "shcs");
    CHECK(result["detail"]["name"] == "SHCS M8x20");
    // gref length_mm wins; params merge skips family/size but overrides other keys.
    CHECK(captured["family"] == "shcs");
    CHECK(captured["size"] == "M8");
    CHECK(captured["length_mm"] == 25);
}

TEST_CASE("core_family: missing family/size is an oracle error") {
    PlaceSeams seams = stub_seams("");
    json result;
    std::string err;
    CHECK(place_call("part_place",
                     json{{"part_id", "fastener:X:1"},
                          {"record", record_with_gref({{"kind", "core_family"}})}},
                     result, err, seams));
    CHECK(result["ok"] == false);
    CHECK(result["reason"] == "geometry_ref missing 'family' or 'size'");
}

TEST_CASE("generate_model: produced STEP imports as a real doc id") {
    const fs::path step = fs::temp_directory_path() / "chudware_place_test_gen.step";
    { std::ofstream f(step); f << "ISO-10303-21;\nEND-ISO-10303-21;\n"; }
    PlaceSeams seams = stub_seams(step.string());
    json result;
    std::string err;
    CHECK(place_call("part_place",
                     json{{"part_id", "fastener:BOLTS:M8x25"},
                          {"record", record_with_gref({{"kind", "generate_model"},
                                                       {"match", {{"source", "bolts"}}}})}},
                     result, err, seams));
    CHECK(result["ok"] == true);
    CHECK(result["editable"] == true);
    CHECK(result["domain"] == "generate_model");
    CHECK(result["doc_id"] == "doc-gen-1");
    CHECK(result["detail"]["step_path"] == step.string());
    CHECK(result["detail"].contains("hint"));
    fs::remove(step);
}

TEST_CASE("generate_model: no buildable geometry is an error, never a crash") {
    PlaceSeams seams = stub_seams("");
    json result;
    std::string err;
    CHECK(place_call("part_place",
                     json{{"part_id", "fastener:BOLTS:X"},
                          {"record", record_with_gref({{"kind", "generate_model"},
                                                       {"match", {{"source", "kicad"}}}})}},
                     result, err, seams));
    CHECK(result["ok"] == false);
    CHECK(result["reason"].get<std::string>().find("None") != std::string::npos);
}

TEST_CASE("part_import_file: file-not-found and success envelopes") {
    PlaceSeams seams = stub_seams("");
    json result;
    std::string err;
    CHECK(place_call("part_import_file", json{{"path", "/nonexistent/model.step"}}, result, err,
                     seams));
    CHECK(result["ok"] == false);
    CHECK(result["reason"] == "import_file RPC failed: file not found: /nonexistent/model.step");

    const fs::path step = fs::temp_directory_path() / "chudware_place_test_import.step";
    { std::ofstream f(step); f << "ISO-10303-21;\nEND-ISO-10303-21;\n"; }
    CHECK(place_call("part_import_file", json{{"path", step.string()}}, result, err, seams));
    CHECK(result["ok"] == true);
    CHECK(result["domain"] == "import_file");
    CHECK(result["doc_id"] == "doc-gen-1");
    CHECK(result["editable"] == true);
    CHECK(result["detail"]["imported_from"] == step.string());
    CHECK(result["detail"]["body_count"] == 1);
    fs::remove(step);
}

TEST_CASE("relative catalog imports reach the authoritative import seam") {
    PlaceSeams seams = stub_seams("");
    std::string imported;
    seams.import_file = [&](const std::string& path, bool) {
        imported = path;
        return json{{"doc_id", "doc-relative"}, {"body_count", 1}};
    };
    const std::string path = ".chudware/catalog/abc/model.step+";
    json result;
    std::string err;
    CHECK(place_call("part_import_file", {{"path", path}}, result, err, seams));
    CHECK(result["ok"] == true);
    CHECK(imported == path);
}

TEST_CASE("symbol_footprint: sch_add_symbol lands an editable op in the live doc") {
    PlaceSeams seams = stub_seams("");
    json result;
    std::string err;
    CHECK(place_call("part_place",
                     json{{"part_id", "ecad:Device:R"},
                          {"record", record_with_gref({{"kind", "symbol_footprint"},
                                                       {"libid", "Device:R"}}, "ecad")},
                          {"doc", sch_doc()},
                          {"params", {{"pos", {10, 20}}, {"value", "1k"}, {"ref", "R1"}}}},
                     result, err, seams));
    REQUIRE(result["ok"] == true);
    CHECK(result["editable"] == true);
    CHECK(result["domain"] == "symbol_footprint");
    CHECK(result["entity_id"] == "s1");
    CHECK(result["detail"]["libid"] == "Device:R");
    CHECK(result["detail"]["ref"] == "R1");
    CHECK(result["detail"]["pos"] == json::array({10.0, 20.0}));
    // The updated doc comes back under result["doc"]; the symbol is schema, not
    // an opaque body — directly mutable (R1 editability).
    REQUIRE(result.contains("doc"));
    const json symbols = result["doc"]["symbols"];
    REQUIRE(symbols.size() == 1);
    CHECK(symbols[0]["id"] == "s1");
    CHECK(symbols[0]["ref"] == "R1");
    CHECK(symbols[0]["libid"] == "Device:R");
}

TEST_CASE("symbol_footprint: no doc is an actionable error; doc_id-only returns the op") {
    PlaceSeams seams = stub_seams("");
    json result;
    std::string err;
    const json rec = record_with_gref({{"kind", "symbol_footprint"}, {"libid", "Device:R"}},
                                      "ecad");
    CHECK(place_call("part_place", json{{"part_id", "ecad:Device:R"}, {"record", rec}}, result,
                     err, seams));
    CHECK(result["ok"] == false);
    CHECK(result["hint"].get<std::string>().find("ecad_new_schematic") != std::string::npos);

    CHECK(place_call("part_place",
                     json{{"part_id", "ecad:Device:R"},
                          {"record", rec},
                          {"params", {{"doc_id", "doc-ext-1"}, {"pos", {{ "x", 5 }, { "y", 6 }}}}}},
                     result, err, seams));
    REQUIRE(result["ok"] == true);
    CHECK(result["doc_id"] == "doc-ext-1");
    CHECK(result["entity_id"].is_null());
    CHECK(result["detail"]["op"]["op"] == "sch_add_symbol");
    CHECK(result["detail"]["op"]["pos"] == json::array({5.0, 6.0}));
    CHECK(result["detail"].contains("note"));
}

TEST_CASE("section_profile: appends an editable StructuralElement to the doc") {
    PlaceSeams seams = stub_seams("");
    json doc = struct_doc();
    json result;
    std::string err;
    CHECK(place_call("part_place",
                     json{{"part_id", "section:AISC:W8x31"},
                          {"record", record_with_gref({{"kind", "section_profile"},
                                                       {"standard", "AISC"},
                                                       {"designation", "W8x31"}},
                                                      "section")},
                          {"doc", doc},
                          {"params", {{"kind", "beam"},
                                      {"length_mm", 3000.0},
                                      {"material", "steel"},
                                      {"id", "el1"}}}},
                     result, err, seams));
    REQUIRE(result["ok"] == true);
    CHECK(result["editable"] == true);
    CHECK(result["entity_id"] == "el1");
    CHECK(result["detail"]["section_name"] == "AISC:W8x31");
    CHECK(result["detail"]["length_mm"] == 3000.0);
    const json elements = result["doc"]["elements"];
    REQUIRE(elements.size() == 1);
    CHECK(elements[0]["element_type"] == "beam");
    CHECK(elements[0]["section_name"] == "AISC:W8x31");
    CHECK(elements[0]["transform"].size() == 16);
    CHECK(elements[0]["transform"][0] == 1.0);
    // Editable by removal (proves schema op, not opaque body).
    json els = elements;
    els.erase(std::remove_if(els.begin(), els.end(),
                             [](const json& e) { return e.value("id", "") == "el1"; }),
              els.end());
    CHECK(els.empty());
}

TEST_CASE("section_profile: standalone placement calls native section family") {
    json captured;
    PlaceSeams seams = stub_seams("", &captured);
    json result;
    std::string err;
    const json rec = record_with_gref({{"kind", "section_profile"}, {"standard", "AISC360"},
                                       {"designation", "C6x13"}}, "section");
    CHECK(place_call("part_place", {{"part_id", "section:AISC360:C6x13"}, {"record", rec},
                                    {"params", {{"length_mm", 500.0}}}}, result, err, seams));
    REQUIRE(result["ok"] == true);
    CHECK(result["domain"] == "section_profile");
    CHECK(result["entity_id"] == result["doc_id"]);
    CHECK(captured["family"] == "structural_section");
    CHECK(captured["size"] == "AISC360:C6x13");
    CHECK(captured["length_mm"] == 500.0);
    CHECK(place_call("part_place", {{"part_id", "section:AISC360:C6x13"}, {"record", rec},
                                    {"params", {{"length_mm", 0}}}}, result, err, seams));
    CHECK(result["ok"] == false);
    CHECK(result["reason"].get<std::string>().find("length_mm") != std::string::npos);
    json missing = rec;
    missing["geometry_ref"].erase("standard");
    CHECK(place_call("part_place", {{"part_id", "section:AISC360:C6x13"}, {"record", missing}},
                     result, err, seams));
    CHECK(result["ok"] == false);
    CHECK(result["reason"].get<std::string>().find("standard") != std::string::npos);
}

TEST_CASE("fitting_id: appends a fitting to an aicad.piping/1 doc") {
    PlaceSeams seams = stub_seams("");
    json result;
    std::string err;
    CHECK(place_call("part_place",
                     json{{"part_id", "fitting:elbow:NPS1"},
                          {"record", record_with_gref({{"kind", "fitting_id"},
                                                       {"fitting_id", "elbow:NPS1"}},
                                                      "fitting")},
                          {"doc", piping_doc()},
                          {"params", {{"type", "elbow"}, {"nps", 1.0}, {"schedule", "80"}}}},
                     result, err, seams));
    REQUIRE(result["ok"] == true);
    CHECK(result["editable"] == true);
    CHECK(result["entity_id"] == "ft1");
    CHECK(result["detail"]["fitting_type"] == "elbow");
    CHECK(result["detail"]["catalog_id"] == "elbow:NPS1");
    const json fittings = result["doc"]["fittings"];
    REQUIRE(fittings.size() == 1);
    CHECK(fittings[0]["type"] == "elbow");
    CHECK(fittings[0]["schedule"] == "80");  // extra catalog field propagated
}

TEST_CASE("fitting_id: catalog identity survives standalone and piping placement") {
    json captured;
    PlaceSeams seams = stub_seams("", &captured);
    json result;
    std::string err;
    json rec = record_with_gref({{"kind", "fitting_id"},
                                 {"fitting_id", "flange:NPS0.5:cl150"}}, "fitting");
    rec["params"] = {{"nps", 0.5}, {"flange_class", 150},
                     {"od", 88.9}, {"thickness", 11.18}};
    const json base = {{"part_id", "fitting:flange:NPS0.5:cl150"}, {"record", rec}};
    CHECK(place_call("part_place", base, result, err, seams));
    REQUIRE(result["ok"] == true);
    CHECK(result["domain"] == "fitting_id");
    CHECK(result["entity_id"] == result["doc_id"]);
    CHECK(captured["family"] == "piping_fitting");
    CHECK(captured["size"] == "flange:NPS0.5:cl150");
    json pipe_request = base;
    pipe_request["doc"] = piping_doc();
    CHECK(place_call("part_place", pipe_request, result, err, seams));
    REQUIRE(result["ok"] == true);
    const json fitting = result["doc"]["fittings"][0];
    CHECK(fitting["type"] == "flange");
    CHECK(fitting["nps"] == 0.5);
    CHECK(fitting["params"]["flange_class"] == 150);
    CHECK(fitting["catalog_ref"] == "flange:NPS0.5:cl150");
    pipe_request["params"] = {{"type", "elbow"}};
    CHECK(place_call("part_place", pipe_request, result, err, seams));
    CHECK(result["ok"] == false);
    pipe_request["params"] = {{"nps", 1.0}};
    CHECK(place_call("part_place", pipe_request, result, err, seams));
    CHECK(result["ok"] == false);
    rec["geometry_ref"]["fitting_id"] = "reducer:NPS2.0x1.5";
    rec["params"] = {{"nps", 2.0}, {"nps2", 1.5}};
    CHECK(place_call("part_place", {{"part_id", "fitting:reducer:NPS2.0x1.5"},
                                    {"record", rec}, {"doc", piping_doc()}}, result, err, seams));
    REQUIRE(result["ok"] == true);
    CHECK(result["doc"]["fittings"][0]["type"] == "reducer");
    CHECK(result["doc"]["fittings"][0]["nps2"] == 1.5);
    rec["geometry_ref"]["fitting_id"] = "flange:NPS999:cl150";
    rec["params"] = json::object();
    CHECK(place_call("part_place", {{"part_id", "fitting:unknown"}, {"record", rec}},
                     result, err, seams));
    CHECK(result["ok"] == false);
}

TEST_CASE("fmu_component: appends a component to an aicad.hydraulic/1 doc") {
    PlaceSeams seams = stub_seams("");
    json result;
    std::string err;
    CHECK(place_call("part_place",
                     json{{"part_id", "hydraulic:pump:gear-1cc"},
                          {"record", record_with_gref({{"kind", "fmu_component"},
                                                       {"component_id", "pump:gear-1cc"}},
                                                      "hydraulic")},
                          {"doc", hydraulic_doc()},
                          {"params", {{"type", "pump"}, {"displacement_cc_rev", 1.0}}}},
                     result, err, seams));
    REQUIRE(result["ok"] == true);
    CHECK(result["editable"] == true);
    CHECK(result["entity_id"] == "c1");
    CHECK(result["detail"]["comp_type"] == "pump");
    const json comps = result["doc"]["components"];
    REQUIRE(comps.size() == 1);
    CHECK(comps[0]["type"] == "pump");
    CHECK(comps[0]["displacement_cc_rev"] == 1.0);  // top-level, mirroring the oracle
}

TEST_CASE("fmu_component: ISO catalog identity is never substituted") {
    PlaceSeams seams = stub_seams("");
    json result;
    std::string err;
    for (const std::string& type : chudware::hydraulic::hyd_component_types_sorted()) {
        json rec = record_with_gref({{"kind", "fmu_component"}, {"component_id", type}},
                                    "hydraulic");
        rec["params"] = {{"component_type", type}};
        CHECK(place_call("part_place", {{"part_id", "hydraulic:ISO1219:" + type},
                                        {"record", rec}, {"doc", hydraulic_doc()}},
                         result, err, seams));
        REQUIRE(result["ok"] == true);
        CHECK(result["doc"]["components"][0]["type"] == type);
        CHECK(result["detail"]["comp_type"] == type);
    }
    json rec = record_with_gref({{"kind", "fmu_component"}, {"component_id", "valve_check"}},
                                "hydraulic");
    CHECK(place_call("part_place", {{"part_id", "hydraulic:ISO1219:valve_check"},
                                    {"record", rec}, {"doc", hydraulic_doc()},
                                    {"params", {{"type", "pump"}}}}, result, err, seams));
    CHECK(result["ok"] == false);
    rec["geometry_ref"]["component_id"] = "not_a_component";
    CHECK(place_call("part_place", {{"part_id", "hydraulic:unknown"},
                                    {"record", rec}, {"doc", hydraulic_doc()}},
                     result, err, seams));
    CHECK(result["ok"] == false);
}

TEST_CASE("place_call: unowned methods fall through") {
    PlaceSeams seams = stub_seams("");
    json result;
    std::string err;
    CHECK(!place_call("parts_catalog_list", json::object(), result, err, seams));
}

TEST_CASE("raw stock uses record dimensions and material, with explicit overrides") {
    PlaceSeams seams = stub_seams("");
    json rec = record_with_gref({{"kind", "section_profile"}, {"standard", "ROUND-BAR"},
                                 {"designation", "6061-T6:ROUND-BAR:10mm"}}, "section");
    rec["params"] = {{"shape", "round_bar"}, {"diameter_mm", 10}, {"material", "6061-T6"}};
    json result;
    std::string err;
    CHECK(place_call("part_place", {{"part_id", "section:X:1"}, {"record", rec},
                                    {"doc", struct_doc()}, {"params", {{"diameter_mm", 12}}}},
                     result, err, seams));
    REQUIRE(result["ok"] == true);
    CHECK(result["doc"]["elements"][0]["section_name"] == "ROUND 12.0");
    CHECK(result["doc"]["elements"][0]["material"] == "6061-T6");
    rec["params"] = {{"shape", "plate"}, {"thickness_mm", 6}, {"material", "6061-T6"}};
    CHECK(place_call("part_place", {{"part_id", "section:X:1"}, {"record", rec},
                                    {"doc", struct_doc()}}, result, err, seams));
    CHECK(result["ok"] == false);
    CHECK(result["reason"].get<std::string>().find("width_mm") != std::string::npos);
}

TEST_CASE("mechanical placement rejects invalid pose and target before calling seams") {
    PlaceSeams seams = stub_seams("");
    int calls = 0;
    seams.parts_make = [&](const json&) { ++calls; return json{{"ok", true}}; };
    json rec = record_with_gref({{"kind", "core_family"}, {"family", "bearing"}, {"size", "608"}});
    json result;
    std::string err;
    for (const json& p : {json{{"pos", {1, 2}}}, json{{"rotation", {0, "bad", 0}}},
                          json{{"doc_id", "existing"}}}) {
        CHECK(place_call("part_place", {{"part_id", "bearing:ISO 15:608"}, {"record", rec},
                                        {"params", p}}, result, err, seams));
        CHECK(result["ok"] == false);
    }
    CHECK(place_call("part_place", {{"part_id", "bearing:ISO 15:608"}, {"record", rec},
                                    {"doc", json::object()}}, result, err, seams));
    CHECK(result["ok"] == false);
    CHECK(calls == 0);
}

#ifdef CHUDWARE_PARTS_NATIVE_TESTS
static json native_rpc(const char* method, const json& params) {
    char* raw = nullptr;
    const std::string encoded = params.dump();
    const int rc = chudware_call(method, encoded.c_str(), &raw);
    const std::string response = raw ? raw : "null";
    chudware_free(raw);
    INFO(method << " rc=" << rc << " response=" << response);
    REQUIRE(rc == 0);
    return json::parse(response);
}
TEST_CASE("native hobby servos build as solids with genuinely open MG996R slots") {
    for (const char* size : {"MG996R", "MG995", "SG90", "MG90S", "DS3218", "micro_9g"}) {
        INFO(size);
        const json part = native_rpc("parts_make", {{"family", "hobby_servo"}, {"size", size}});
        REQUIRE_MESSAGE(part.value("ok", false), part.dump());
        const std::string id = part.at("doc_id").get<std::string>();
        for (const auto& status : part.at("rebuild").at("feature_status"))
            REQUIRE_MESSAGE(status.value("ok", false), status.dump());
        const json integrity = native_rpc("eval_model_integrity", {{"doc_id", id}});
        REQUIRE_MESSAGE(integrity.at("complete") == true, integrity.dump());
        REQUIRE(integrity.at("solids").size() == 1);
        CHECK(integrity.at("solids")[0].at("brep_valid") == true);

        if (std::string(size) == "MG996R") {
            CHECK(part["bbox"]["min"][2].get<double>() == doctest::Approx(0.0).epsilon(1e-5));
            CHECK(part["bbox"]["max"][2].get<double>() == doctest::Approx(42.9).epsilon(1e-5));
            CHECK(part["output_axis"]["origin"][0].get<double>() == doctest::Approx(10.45));
            CHECK(part["output_axis"]["origin"][2].get<double>() == doctest::Approx(40.0));
            CHECK(part["output_axis"]["axis"] == json::array({0, 0, 1}));
            CHECK(part["meta"]["specs"]["hole_pitch"].get<double>() == doctest::Approx(49.5));
            const json health = native_rpc("eval_solid_health", {{"doc_id", id}});
            REQUIRE(health["bodies"].size() == 1);
            CHECK(health["bodies"][0]["solids"] == 1);
            CHECK(health["bodies"][0]["brep_valid"] == true);
            CHECK(health["bodies"][0]["faces"].get<int>() >= 75);

            // Small independent cubes lie just beyond the circular portion of
            // each bore, inside the straight mouth; the fifth is an intact-tab
            // control. A closed or only circular slot fails the common-volume test.
            auto probe = [](const char* name, double x, double y) {
                return json{{"op", "add_feature"}, {"feature", {
                    {"type", "box"}, {"name", name}, {"params", {
                        {"length", 0.1}, {"width", 0.1}, {"height", 0.2},
                        {"at", {x, y, 27.9}}, {"centered", false}, {"merge", false}}}}}};
            };
            const json probes = native_rpc("doc_apply_ops", {{"doc_id", id}, {"ops",
                json::array({probe("slot -X -Y", -26.9, -3.7),
                             probe("slot -X +Y", -26.9, 6.3),
                             probe("slot +X -Y", 26.8, -3.7),
                             probe("slot +X +Y", 26.8, 6.3),
                             probe("flange control", 26.8, 0.0)})}});
            REQUIRE_FALSE(probes.contains("rejected_tree"));
            for (const auto& status : probes.at("rebuild").at("feature_status"))
                REQUIRE_MESSAGE(status.value("ok", false), status.dump());
            const json overlaps = native_rpc("eval_interference", {{"doc_id", id}});
            REQUIRE_MESSAGE(overlaps.at("complete") == true, overlaps.dump());
            REQUIRE_MESSAGE(overlaps.at("interferences").size() == 1, overlaps.dump());
            CHECK(overlaps["interferences"][0]["volume"].get<double>()
                  == doctest::Approx(0.002).epsilon(1e-3));
        }
        native_rpc("doc_close", {{"doc_id", id}});
    }
}


static std::string file_bytes(const fs::path& path) {
    std::ifstream file(path, std::ios::binary);
    return std::string(std::istreambuf_iterator<char>(file), std::istreambuf_iterator<char>());
}

TEST_CASE("native catalog imports resolve workspace-relative artifacts under the configured root") {
    const json source = native_rpc("parts_make", {{"family", "bearing"}, {"size", "608"}});
    REQUIRE(source["ok"] == true);
    const std::string source_id = source["doc_id"].get<std::string>();
    const fs::path root = fs::canonical(fs::temp_directory_path()) / ("chudware-catalog-root-" + source_id);
    const fs::path catalog = root / ".chudware" / "catalog" / "fixture";
    fs::create_directories(catalog);
    native_rpc("doc_export_step", {{"doc_id", source_id}, {"path", (catalog / "model.step").string()}});
    native_rpc("doc_save", {{"doc_id", source_id}, {"path", (catalog / "model.step+").string()}});
    native_rpc("doc_close", {{"doc_id", source_id}});
    struct RootEnvironment {
        std::string previous;
        bool present;
        explicit RootEnvironment(const fs::path& root)
            : present(std::getenv("CHUDWARE_NATIVE_ROOT") != nullptr) {
            if (present) previous = std::getenv("CHUDWARE_NATIVE_ROOT");
#ifdef _WIN32
            _putenv_s("CHUDWARE_NATIVE_ROOT", root.string().c_str());
#else
            setenv("CHUDWARE_NATIVE_ROOT", root.string().c_str(), 1);
#endif
        }
        ~RootEnvironment() {
#ifdef _WIN32
            _putenv_s("CHUDWARE_NATIVE_ROOT", present ? previous.c_str() : "");
#else
            if (present) setenv("CHUDWARE_NATIVE_ROOT", previous.c_str(), 1);
            else unsetenv("CHUDWARE_NATIVE_ROOT");
#endif
        }
    };
    {
        RootEnvironment rooted(root);
        for (const char* name : {"model.step", "model.step+"}) {
            const std::string relative = std::string(".chudware/catalog/fixture/") + name;
            json rec = record_with_gref({{"kind", "import_file"}, {"path", relative}}, "catalog");
            const json placed = native_rpc("part_place", {{"part_id", "catalog:X:1"}, {"record", rec}});
            REQUIRE(placed["ok"] == true);
            native_rpc("doc_close", {{"doc_id", placed["doc_id"]}});
        }
        json rec = record_with_gref({{"kind", "import_file"},
                                     {"path", "../outside.step"}}, "catalog");
        CHECK(native_rpc("part_place", {{"part_id", "catalog:X:1"}, {"record", rec}})["ok"] == false);
    }
    fs::remove_all(root);
}

TEST_CASE("native generated recipes reject traversal and retain placement pose") {
    const fs::path victim = fs::temp_directory_path().parent_path() / "victim.step";
    const bool existed = fs::exists(victim);
    const std::string before = existed ? file_bytes(victim) : std::string();
    json rec = record_with_gref({{"kind", "generate_model"},
        {"match", {{"source", "bolts"}, {"family", "dowel_pin"},
                   {"size", "6/../../victim"}, {"length_mm", 10}}}});
    json placed = native_rpc("part_place", {{"part_id", "fastener:X:1"}, {"record", rec}});
    CHECK(placed["ok"] == false);
    CHECK(fs::exists(victim) == existed);
    if (existed) CHECK(file_bytes(victim) == before);

    rec["geometry_ref"]["match"]["size"] = "6";
    placed = native_rpc("part_place", {{"part_id", "fastener:X:1"}, {"record", rec},
        {"params", {{"pos", {10, 20, 30}}, {"rotation", {90, 0, 0}}}}});
    REQUIRE(placed["ok"] == true);
    const std::string id = placed["doc_id"].get<std::string>();
    const json bbox = native_rpc("doc_bbox", {{"doc_id", id}});
    CHECK(bbox["min"][0].get<double>() == doctest::Approx(7).epsilon(1e-5));
    CHECK(bbox["max"][0].get<double>() == doctest::Approx(13).epsilon(1e-5));
    CHECK(bbox["min"][1].get<double>() == doctest::Approx(10).epsilon(1e-5));
    CHECK(bbox["max"][1].get<double>() == doctest::Approx(20).epsilon(1e-5));
    CHECK(placed["detail"]["datum"]["axis"][1].get<double>() == doctest::Approx(-1));
    CHECK(native_rpc("doc_get_meta", {{"doc_id", id}, {"key", "parts_catalog"}})
              ["value"]["part_record"] == rec);
    const fs::path output = placed["detail"]["step_path"].get<std::string>();
    CHECK(output.filename().string().find("dowel_pin") == std::string::npos);
    native_rpc("doc_close", {{"doc_id", id}});
    fs::remove(output);
}

TEST_CASE("native catalog instance edits and saves without changing its source") {
    const json source = native_rpc("doc_new", {{"kind", "part"}, {"name", "Catalog source"}});
    const std::string source_id = source["doc_id"].get<std::string>();
    const json built = native_rpc("doc_apply_ops", {{"doc_id", source_id}, {"ops", json::array({
        {{"op", "add_feature"}, {"feature", {{"type", "box"}, {"name", "Editable block"},
            {"params", {{"length", 10}, {"width", 20}, {"height", 30},
                        {"centered", false}}}}}}
    })}});
    REQUIRE_FALSE(built.contains("rejected_tree"));
    const std::string feature = built["id_map"]["$0"].get<std::string>();
    native_rpc("doc_set_meta", {{"doc_id", source_id}, {"key", "parts_catalog"},
                                {"value", {{"original", "publisher"}}}});
    const fs::path seed_path = fs::temp_directory_path() / "chudware_catalog_pinned_seed.step+";
    const fs::path source_path = fs::temp_directory_path() / "chudware_catalog_pinned_source.step+";
    const fs::path saved_path = fs::temp_directory_path() / "chudware_catalog_editable_instance.step+";
    native_rpc("doc_save", {{"doc_id", source_id}, {"path", seed_path.string()}});
    native_rpc("doc_close", {{"doc_id", source_id}});
    fs::remove(source_path);
    const json relocated = native_rpc("doc_file_operations", {{"action", "relocate"},
        {"entries", json::array({{{"path", seed_path.string()}, {"destination", source_path.string()}}})}});
    REQUIRE(relocated["ok"] == true);
    CHECK_FALSE(relocated["files"][0]["invalidated_caches"].empty());
    fs::remove(seed_path);
    const std::string original = file_bytes(source_path);
    REQUIRE_FALSE(original.empty());
    json rec = record_with_gref({{"kind", "import_file"}, {"path", source_path.string()}}, "catalog");
    rec["part_id"] = "catalog:example:pinned";
    const json placed = native_rpc("part_place", {{"part_id", rec["part_id"]}, {"record", rec},
        {"params", {{"pos", {10, 20, 30}}, {"rotation", {0, 0, 90}}}}});
    REQUIRE(placed["ok"] == true);
    const std::string id = placed["doc_id"].get<std::string>();
    CHECK(file_bytes(source_path) == original);
    char* raw = nullptr;
    const std::string no_path = json{{"doc_id", id}}.dump();
    const int save_rc = chudware_call("doc_save", no_path.c_str(), &raw);
    chudware_free(raw);
    CHECK(save_rc != 0);
    CHECK(file_bytes(source_path) == original);
    const json meta = native_rpc("doc_get_meta", {{"doc_id", id}, {"key", "parts_catalog"}})["value"];
    CHECK(meta["part_record"] == rec);
    CHECK(meta["source_catalog"]["original"] == "publisher");
    const json edited = native_rpc("doc_apply_ops", {{"doc_id", id}, {"ops", json::array({
        {{"op", "edit_feature"}, {"id", feature}, {"params", {{"length", 25}}}}
    })}});
    REQUIRE_FALSE(edited.contains("rejected_tree"));
    const json docs = native_rpc("doc_list", json::object())["docs"];
    auto instance = std::find_if(docs.begin(), docs.end(), [&](const json& row) {
        return row.value("doc_id", std::string()) == id;
    });
    REQUIRE(instance != docs.end());
    CHECK((*instance)["path"].is_null());
    CHECK((*instance)["native_source_path"] == source_path.string());
    CHECK((*instance)["native_source_edited"] == true);
    native_rpc("doc_save", {{"doc_id", id}, {"path", saved_path.string()}});
    native_rpc("doc_close", {{"doc_id", id}});
    CHECK(file_bytes(source_path) == original);
    const json reopened = native_rpc("doc_open", {{"path", saved_path.string()}});
    const std::string reopened_id = reopened["doc_id"].get<std::string>();
    const json tree = native_rpc("doc_tree", {{"doc_id", reopened_id}})["tree"];
    CHECK(tree["features"][0]["params"]["length"] == 25);
    CHECK(tree["features"].size() > 1);
    CHECK(native_rpc("doc_get_meta", {{"doc_id", reopened_id}, {"key", "parts_catalog"}})
              ["value"]["source_catalog"]["original"] == "publisher");
    native_rpc("doc_close", {{"doc_id", reopened_id}});
    CHECK(file_bytes(source_path) == original);
    fs::remove(source_path);
    fs::remove(saved_path);
}

TEST_CASE("native shielded bearings preserve envelope and have two recessed faces") {
    const struct { const char* size; double bore, od, width; } cases[] = {
        {"623", 3, 10, 4}, {"608", 8, 22, 7}, {"6205", 25, 52, 15}
    };
    for (const auto& c : cases) {
        INFO(c.size);
        const json part = native_rpc("parts_make", {{"family", "bearing"}, {"size", c.size}});
        REQUIRE_MESSAGE(part.value("ok", false), part.dump());
        const std::string id = part["doc_id"].get<std::string>();
        const json bbox = native_rpc("doc_bbox", {{"doc_id", id}});
        for (int axis = 0; axis < 2; ++axis) {
            CHECK(bbox["min"][axis].get<double>() == doctest::Approx(-c.od / 2).epsilon(1e-5));
            CHECK(bbox["max"][axis].get<double>() == doctest::Approx(c.od / 2).epsilon(1e-5));
        }
        CHECK(bbox["min"][2].get<double>() == doctest::Approx(0).epsilon(1e-5));
        CHECK(bbox["max"][2].get<double>() == doctest::Approx(c.width).epsilon(1e-5));
        const json health = native_rpc("eval_solid_health", {{"doc_id", id}});
        REQUIRE(health["bodies"].size() == 1);
        CHECK(health["bodies"][0]["solids"] == 1);
        CHECK(health["bodies"][0]["brep_valid"] == true);
        const double radial = (c.od - c.bore) / 2;
        const double inner = c.bore / 2 + radial * 0.25;
        const double outer = c.od / 2 - radial * 0.25;
        const double depth = std::min(0.3, 0.04 * c.width);
        const double pi = std::acos(-1.0);
        const double volume = pi * ((c.od * c.od - c.bore * c.bore) / 4 * c.width -
                                    2 * (outer * outer - inner * inner) * depth);
        const json mass = native_rpc("eval_mass", {{"doc_id", id}});
        CHECK(mass["volume_mm3"].get<double>() == doctest::Approx(volume).epsilon(1e-6));
        native_rpc("doc_close", {{"doc_id", id}});
    }
}

TEST_CASE("native bearing pose is editable and catalog metadata survives reopening") {
    json rec = record_with_gref({{"kind", "core_family"}, {"family", "bearing"},
                                 {"size", "608"}}, "bearing");
    rec["part_id"] = "bearing:ISO 15:608";
    rec["standard"] = "ISO 15";
    rec["provenance"] = "catalog source";
    rec["license"] = "public-domain";
    rec["units"] = "mm";
    rec["references"] = json::array({{{"identifier", "ISO 15"}}});
    rec["artifact"] = {{"sha256", "example source digest"}};
    const json placed = native_rpc("part_place", {{"part_id", rec["part_id"]}, {"record", rec},
        {"params", {{"pos", {10, 20, 30}}, {"rotation", {90, 0, 0}}}}});
    REQUIRE(placed["ok"] == true);
    const std::string id = placed["doc_id"].get<std::string>();
    const json bbox = native_rpc("doc_bbox", {{"doc_id", id}});
    CHECK(bbox["min"][0].get<double>() == doctest::Approx(-1.0).epsilon(1e-5));
    CHECK(bbox["max"][0].get<double>() == doctest::Approx(21.0).epsilon(1e-5));
    CHECK(bbox["min"][1].get<double>() == doctest::Approx(13.0).epsilon(1e-5));
    CHECK(bbox["max"][1].get<double>() == doctest::Approx(20.0).epsilon(1e-5));
    CHECK(bbox["min"][2].get<double>() == doctest::Approx(19.0).epsilon(1e-5));
    CHECK(bbox["max"][2].get<double>() == doctest::Approx(41.0).epsilon(1e-5));
    const json datum = placed["detail"]["datum"];
    CHECK(datum["base_center"][0].get<double>() == doctest::Approx(10));
    CHECK(datum["base_center"][1].get<double>() == doctest::Approx(20));
    CHECK(datum["base_center"][2].get<double>() == doctest::Approx(30));
    CHECK(datum["axis"][1].get<double>() == doctest::Approx(-1));
    CHECK(std::abs(datum["axis"][2].get<double>()) < 1e-8);
    const fs::path file = fs::temp_directory_path() / "chudware_parts_pose_test.step+";
    native_rpc("doc_save", {{"doc_id", id}, {"path", file.string()}});
    native_rpc("doc_close", {{"doc_id", id}});
    const json opened = native_rpc("doc_open", {{"path", file.string()}});
    const std::string reopened = opened["doc_id"].get<std::string>();
    const json meta = native_rpc("doc_get_meta", {{"doc_id", reopened}, {"key", "parts_catalog"}})["value"];
    CHECK(meta["part_record"] == rec);
    CHECK(meta["generated"]["family"] == "bearing");
    CHECK(meta["generated"]["specs"].is_object());
    CHECK(meta["geometry_quality"] == "nominal_generated_envelope");
    native_rpc("doc_close", {{"doc_id", reopened}});
    json native_rec = record_with_gref({{"kind", "import_file"}, {"path", file.string()}}, "catalog");
    native_rec["part_id"] = "catalog:example:native";
    const json native_placed = native_rpc("part_place", {{"part_id", native_rec["part_id"]},
        {"record", native_rec}, {"params", {{"pos", {100, 0, 0}}}}});
    REQUIRE(native_placed["ok"] == true);
    const std::string native_id = native_placed["doc_id"].get<std::string>();
    const json native_bbox = native_rpc("doc_bbox", {{"doc_id", native_id}});
    CHECK(native_bbox["min"][0].get<double>() == doctest::Approx(99).epsilon(1e-5));
    CHECK(native_bbox["max"][0].get<double>() == doctest::Approx(121).epsilon(1e-5));
    const json native_tree = native_rpc("doc_tree", {{"doc_id", native_id}});
    CHECK(native_tree["tree"]["features"].size() > 2);
    const json native_meta = native_rpc("doc_get_meta", {{"doc_id", native_id}, {"key", "parts_catalog"}})["value"];
    CHECK(native_meta["part_record"] == native_rec);
    CHECK(native_meta["source_catalog"]["generated"]["family"] == "bearing");
    native_rpc("doc_close", {{"doc_id", native_id}});
    fs::remove(file);
}
TEST_CASE("native invalid pose does not create a document") {
    const json before = native_rpc("doc_list", json::object())["docs"];
    json rec = record_with_gref({{"kind", "core_family"}, {"family", "bearing"},
                                 {"size", "608"}}, "bearing");
    const json result = native_rpc("part_place", {{"part_id", "bearing:ISO 15:608"},
        {"record", rec}, {"params", {{"pos", {1, 2}}}}});
    CHECK(result["ok"] == false);
    CHECK(native_rpc("doc_list", json::object())["docs"] == before);
}

TEST_CASE("generated fastener metadata survives saving and reopening") {
    const json part = native_rpc("parts_make",
        {{"family", "shcs"}, {"size", "M6"}, {"length_mm", 20.0}});
    REQUIRE(part.value("ok", false));
    const std::string id = part.at("doc_id").get<std::string>();
    CHECK(native_rpc("doc_get_meta", {{"doc_id", id}, {"key", "standard_part"}})["value"]
          == part["meta"]);
    const fs::path saved = fs::temp_directory_path() / ("chudware_standard_part_" + id + ".step+");
    native_rpc("doc_save", {{"doc_id", id}, {"path", saved.string()}});
    native_rpc("doc_close", {{"doc_id", id}});
    const json opened = native_rpc("doc_open", {{"path", saved.string()}, {"internal", true},
                                                {"detach_path", true}});
    const std::string reopened = opened.at("doc_id").get<std::string>();
    const json meta = native_rpc("doc_get_meta",
                                  {{"doc_id", reopened}, {"key", "standard_part"}})["value"];
    CHECK(meta == part["meta"]);
    CHECK(meta["family"] == "shcs");
    CHECK(meta["size"] == "M6");
    CHECK(meta["specs"]["length"] == 20.0);
    native_rpc("doc_close", {{"doc_id", reopened}});
    fs::remove(saved);
}

TEST_CASE("native imported catalog metadata survives reopening") {
    const json source = native_rpc("parts_make", {{"family", "bearing"}, {"size", "608"}});
    REQUIRE(source["ok"] == true);
    const std::string source_id = source["doc_id"].get<std::string>();
    const fs::path step = fs::temp_directory_path() / "chudware_catalog_import_test.step";
    const fs::path saved = fs::temp_directory_path() / "chudware_catalog_import_test.step+";
    native_rpc("doc_export_step", {{"doc_id", source_id}, {"path", step.string()}});
    native_rpc("doc_close", {{"doc_id", source_id}});
    json rec = record_with_gref({{"kind", "import_file"}, {"path", step.string()}}, "catalog");
    rec["part_id"] = "catalog:example:part";
    rec["license"] = "CC0-1.0";
    rec["provenance"] = "source";
    rec["units"] = "mm";
    rec["references"] = json::array({{{"identifier", "catalog-1"}}});
    rec["artifact"] = {{"sha256", "source digest"}};
    const json placed = native_rpc("part_place", {{"part_id", rec["part_id"]}, {"record", rec}});
    REQUIRE(placed["ok"] == true);
    const std::string id = placed["doc_id"].get<std::string>();
    native_rpc("doc_save", {{"doc_id", id}, {"path", saved.string()}});
    native_rpc("doc_close", {{"doc_id", id}});
    const auto opened = native_rpc("doc_open", {{"path", saved.string()}});
    const std::string reopened = opened["doc_id"].get<std::string>();
    const json meta = native_rpc("doc_get_meta", {{"doc_id", reopened}, {"key", "parts_catalog"}})["value"];
    CHECK(meta["part_record"] == rec);
    native_rpc("doc_close", {{"doc_id", reopened}});
    fs::remove(saved);
    fs::remove(step);
}
TEST_CASE("native multi-body imported placement transforms every body and persists history") {
    const json source = native_rpc("doc_new", {{"kind", "part"}, {"name", "Two stock bodies"}});
    const std::string source_id = source["doc_id"].get<std::string>();
    const json ops = json::array({
        {{"op", "add_feature"}, {"feature", {{"type", "box"}, {"name", "First"},
            {"params", {{"length", 2}, {"width", 2}, {"height", 2}}}}}},
        {{"op", "add_feature"}, {"feature", {{"type", "box"}, {"name", "Second"},
            {"params", {{"length", 2}, {"width", 2}, {"height", 2},
                        {"at", {10, 0, 0}}, {"merge", false}}}}}}
    });
    const json built = native_rpc("doc_apply_ops", {{"doc_id", source_id}, {"ops", ops}});
    REQUIRE_FALSE(built.contains("rejected_tree"));
    REQUIRE(built["tess"]["bodies"].size() == 2);
    const fs::path step = fs::temp_directory_path() / "chudware_catalog_two_body.step";
    const fs::path saved = fs::temp_directory_path() / "chudware_catalog_two_body.step+";
    native_rpc("doc_export_step", {{"doc_id", source_id}, {"path", step.string()}});
    native_rpc("doc_close", {{"doc_id", source_id}});
    json rec = record_with_gref({{"kind", "import_file"}, {"path", step.string()}}, "catalog");
    rec["part_id"] = "catalog:example:two-body";
    rec["license"] = "CC0-1.0";
    const json placed = native_rpc("part_place", {{"part_id", rec["part_id"]}, {"record", rec},
        {"params", {{"pos", {10, 20, 30}}, {"rotation", {0, 0, 90}}}}});
    REQUIRE(placed["ok"] == true);
    CHECK(placed["detail"]["body_count"] == 2);
    const std::string id = placed["doc_id"].get<std::string>();
    const json bbox = native_rpc("doc_bbox", {{"doc_id", id}});
    CHECK(bbox["min"][0].get<double>() == doctest::Approx(9).epsilon(1e-5));
    CHECK(bbox["max"][0].get<double>() == doctest::Approx(11).epsilon(1e-5));
    CHECK(bbox["min"][1].get<double>() == doctest::Approx(19).epsilon(1e-5));
    CHECK(bbox["max"][1].get<double>() == doctest::Approx(31).epsilon(1e-5));
    CHECK(bbox["min"][2].get<double>() == doctest::Approx(29).epsilon(1e-5));
    CHECK(bbox["max"][2].get<double>() == doctest::Approx(31).epsilon(1e-5));
    CHECK(placed["detail"]["datum"]["base_center"][1].get<double>() == doctest::Approx(25));
    const json tree = native_rpc("doc_tree", {{"doc_id", id}});
    CHECK(tree["tree"]["features"].size() == 5);
    native_rpc("doc_save", {{"doc_id", id}, {"path", saved.string()}});
    native_rpc("doc_close", {{"doc_id", id}});
    const auto opened = native_rpc("doc_open", {{"path", saved.string()}});
    const std::string reopened = opened["doc_id"].get<std::string>();
    const json restored = native_rpc("doc_bbox", {{"doc_id", reopened}});
    CHECK(restored["min"][1].get<double>() == doctest::Approx(19).epsilon(1e-5));
    CHECK(restored["max"][1].get<double>() == doctest::Approx(31).epsilon(1e-5));
    CHECK(native_rpc("doc_get_meta", {{"doc_id", reopened}, {"key", "parts_catalog"}})
              ["value"]["part_record"] == rec);
    native_rpc("doc_close", {{"doc_id", reopened}});
    fs::remove(saved);
    fs::remove(step);
}
#endif


int main(int argc, char** argv) {
    doctest::Context ctx(argc, argv);
    return ctx.run();
}
