// core/tests/test_parts_place.cpp — part_place placement router (C++ port of
// place.py). Mirrors the offline-capable legs of
// services/parts/test_parts_hub_p3.py: every placer returns a PlacementResult
// dict, success always has editable=true, and doc-mutating legs hand the
// updated document back under result["doc"]. The hub lookup + doc-creating
// RPCs are stubbed via PlaceSeams (the same monkeypatch shape the Python
// oracle tests use); the ecad/piping/hydraulic legs run the REAL C++ schema
// factories + apply_ops.
//
// Also doubles as the C++ half of the Python↔C++ parity harness:
//   test_parts_place --parity-dump <fixtures.json> <out.json>
// reads the harness-produced fixture docs (core/tests/parts_place_parity.py)
// and writes the C++ scenario outputs for the harness to diff.
#define DOCTEST_CONFIG_IMPLEMENT
#include <doctest/doctest.h>

#include <filesystem>
#include <fstream>
#include <iostream>
#include <string>
#include <algorithm>

#include "ecad/schema.h"
#include "hydraulic/schema.h"
#include "parts/place.h"
#include "piping/schema.h"

using namespace chudware::parts;
using nlohmann::json;

namespace fs = std::filesystem;

// ── shared fixture helpers (used by the doctests AND --parity-dump) ─────────

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
    s.import_file = [](const std::string&, bool) -> json {
        return json{{"doc_id", "doc-gen-1"}, {"body_count", 1}};
    };
    s.build_model = [step_path](const json&) -> std::string { return step_path; };
    return s;
}

// The scenario set the parity harness diffs. Input fixture docs come from the
// Python side so BOTH sides start from byte-identical documents.
static json run_parity_scenarios(const json& fixtures) {
    const std::string step_path = fixtures.value("step_path", std::string());
    PlaceSeams seams = stub_seams(step_path);
    json out = json::object();

    auto run = [&](const char* name, const json& params) {
        json result;
        std::string err;
        if (!place_call("part_place", params, result, err, seams))
            throw std::runtime_error(std::string("dispatch failed: ") + name);
        out[name] = result;
    };

    // 1. core_family (catalog part into a new doc — parts_make seam).
    run("core_family",
        {{"part_id", "fastener:ISO4762:M8"},
         {"record", record_with_gref(
                       {{"kind", "core_family"}, {"family", "shcs"}, {"size", "M8"}})},
         {"params", {{"length_mm", 20}}}});

    // 2. generate_model (bolts match → STEP → import_file seam).
    run("generate_model",
        {{"part_id", "fastener:BOLTS:M8x25"},
         {"record", record_with_gref({{"kind", "generate_model"},
                                      {"match", {{"source", "bolts"},
                                                 {"family", "shcs"},
                                                 {"size", "M8"},
                                                 {"length_mm", 25}}}})}});

    // 3. import_file (browser-acquired STEP placement).
    run("import_file",
        {{"part_id", "catalog:mcmaster:1234"},
         {"record", record_with_gref({{"kind", "import_file"}, {"path", step_path}},
                                     "catalog")}});

    // 4. symbol_footprint into a live schematic (REAL ecad apply_ops).
    run("symbol_footprint",
        {{"part_id", "ecad:Device:R"},
         {"record", record_with_gref({{"kind", "symbol_footprint"}, {"libid", "Device:R"}},
                                     "ecad")},
         {"doc", fixtures["sch_doc"]},
         {"params", {{"pos", {10, 20}}, {"value", "1k"}, {"ref", "R1"}}}});

    // 5. section_profile into a structure doc.
    run("section_profile",
        {{"part_id", "section:AISC:W8x31"},
         {"record", record_with_gref({{"kind", "section_profile"},
                                      {"standard", "AISC"},
                                      {"designation", "W8x31"}},
                                     "section")},
         {"doc", fixtures["struct_doc"]},
         {"params", {{"kind", "beam"}, {"length_mm", 3000.0}, {"material", "steel"}}}});

    // 6. fitting_id into a piping doc (REAL piping schema factory).
    run("fitting_id",
        {{"part_id", "fitting:elbow:NPS1"},
         {"record", record_with_gref({{"kind", "fitting_id"}, {"fitting_id", "elbow:NPS1"}},
                                     "fitting")},
         {"doc", fixtures["piping_doc"]},
         {"params", {{"type", "elbow"}, {"nps", 1.0}}}});

    // 7. fmu_component into a hydraulic doc (REAL hydraulic schema factory).
    run("fmu_component",
        {{"part_id", "hydraulic:pump:gear-1cc"},
         {"record", record_with_gref(
                        {{"kind", "fmu_component"}, {"component_id", "pump:gear-1cc"}},
                        "hydraulic")},
         {"doc", fixtures["hydraulic_doc"]},
         {"params", {{"type", "pump"}, {"displacement_cc_rev", 1.0}}}});

    // 8. Error envelopes.
    run("unknown_part_id", {{"part_id", "fastener:NONSTD:XXXXX_NOT_REAL"}});
    run("unknown_kind",
        {{"part_id", "fastener:X:1"},
         {"record", record_with_gref({{"kind", "wat"}})}});
    run("symbol_no_doc",
        {{"part_id", "ecad:Device:R"},
         {"record", record_with_gref({{"kind", "symbol_footprint"}, {"libid", "Device:R"}},
                                     "ecad")}});
    run("section_no_doc",
        {{"part_id", "section:AISC:W8x31"},
         {"record", record_with_gref({{"kind", "section_profile"},
                                      {"standard", "AISC"},
                                      {"designation", "W8x31"}},
                                     "section")}});
    run("acquire_login_required",
        {{"part_id", "catalog:mcmaster:91292A029"},
         {"record",
          {{"part_id", "catalog:mcmaster:91292A029"},
           {"domain", "catalog"},
           {"ok", false},
           {"reason", "login_required"},
           {"site", "mcmaster"},
           {"source_url", "https://www.mcmaster.com/91292A029/"},
           {"title", "Alloy Steel Socket Head Screw"}}}});
    return out;
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
    CHECK(result["reason"] == "file not found: /nonexistent/model.step");

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

TEST_CASE("section_profile: no structural doc is an actionable error") {
    PlaceSeams seams = stub_seams("");
    json result;
    std::string err;
    CHECK(place_call("part_place",
                     json{{"part_id", "section:AISC:W8x31"},
                          {"record", record_with_gref({{"kind", "section_profile"},
                                                       {"designation", "W8x31"}},
                                                      "section")}},
                     result, err, seams));
    CHECK(result["ok"] == false);
    CHECK(result["hint"].get<std::string>().find("new_structure()") != std::string::npos);
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

TEST_CASE("place_call: unowned methods fall through") {
    PlaceSeams seams = stub_seams("");
    json result;
    std::string err;
    CHECK(!place_call("parts_catalog_list", json::object(), result, err, seams));
}

// ── parity-dump entry (driven by core/tests/parts_place_parity.py) ──────────

static int parity_dump(const char* fixtures_path, const char* out_path) {
    std::ifstream in(fixtures_path);
    if (!in) {
        std::cerr << "cannot open fixtures: " << fixtures_path << "\n";
        return 2;
    }
    const json fixtures = json::parse(in);
    const json out = run_parity_scenarios(fixtures);
    std::ofstream f(out_path);
    f << out.dump(2);
    return 0;
}

int main(int argc, char** argv) {
    if (argc >= 4 && std::string(argv[1]) == "--parity-dump")
        return parity_dump(argv[2], argv[3]);
    doctest::Context ctx(argc, argv);
    return ctx.run();
}
