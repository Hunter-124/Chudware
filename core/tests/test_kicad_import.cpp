// KiCad importers (ecad/kicad.cpp) + the ecad_open extension dispatch.
// Mirrors engine/ecad/test_formats.py tests 3-6 (import_kicad_sch /
// import_kicad_pcb / file-based import / open dispatch) against the same inline
// fixtures. Pure JSON, OCCT-free; links chudware_ecad only.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include "ecad/container.h"
#include "ecad/kicad.h"
#include "ecad/kicad_cli.h"
#include "ecad/kicad_libs.h"
#include "ecad/schema.h"
#include "ecad/session.h"

#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iterator>
#include <string>

#include <nlohmann/json.hpp>
#include <doctest/doctest.h>
#include <miniz.h>
#include <map>

using nlohmann::json;

namespace {

// engine/ecad/test_formats.py::_MINIMAL_SCH (verbatim)
const char* kMinimalSch = R"KICAD(
(kicad_sch (version 20221018) (generator chudware-test)
  (lib_symbols
    (symbol "Device:R"
      (symbol "R_0_1"
        (polyline (pts (xy -1.016 0) (xy 1.016 0)) (stroke (width 0)) (fill (type none)))
      )
      (symbol "R_1_1"
        (pin passive line (at -2.54 0 0) (length 2.54) (name "~" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
        (pin passive line (at 2.54 0 180) (length 2.54) (name "~" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27)))))
      )
      (property "Reference" "R" (at 1.524 1.778 0) (effects (font (size 1.27 1.27))))
      (property "Value" "R" (at 1.524 -1.778 0) (effects (font (size 1.27 1.27))))
      (property "Footprint" "" (at 0 0 0) (effects (font (size 1.27 1.27)) hide))
    )
    (symbol "Device:C"
      (symbol "C_1_1"
        (pin passive line (at 0 2.54 270) (length 2.54) (name "~" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
        (pin passive line (at 0 -2.54 90) (length 2.54) (name "~" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27)))))
      )
      (property "Reference" "C" (at 1.524 1.778 0) (effects (font (size 1.27 1.27))))
      (property "Value" "C" (at 1.524 -1.778 0) (effects (font (size 1.27 1.27))))
    )
  )
  (symbol (lib_id "Device:R") (at 50.8 50.8 0) (unit 1)
    (property "Reference" "R1" (at 52.324 52.578 0) (effects (font (size 1.27 1.27))))
    (property "Value" "10k" (at 52.324 50.038 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "Resistor_SMD:R_0603_1608Metric" (at 50.8 50.8 0) (effects (font (size 1.27 1.27)) hide))
  )
  (symbol (lib_id "Device:C") (at 60.96 50.8 0) (unit 1)
    (property "Reference" "C1" (at 62.484 52.578 0) (effects (font (size 1.27 1.27))))
    (property "Value" "100nF" (at 62.484 50.038 0) (effects (font (size 1.27 1.27))))
  )
  (wire (pts (xy 45.72 50.8) (xy 48.26 50.8)))
  (label "CLK" (at 45.72 50.8 0) (effects (font (size 1.27 1.27))))
  (global_label "VCC" (at 60.96 45.72 90) (shape input) (effects (font (size 1.27 1.27))))
  (junction (at 50.8 50.8))
  (no_connect (at 60.96 55.88))
)
)KICAD";

// engine/ecad/test_formats.py::_MINIMAL_PCB (verbatim)
const char* kMinimalPcb = R"KICAD(
(kicad_pcb (version 20221018) (generator chudware-test)
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "B.CrtYd" user)
    (45 "F.CrtYd" user)
    (46 "B.Fab" user)
    (47 "F.Fab" user)
    (48 "B.SilkS" user)
    (49 "F.SilkS" user)
    (50 "Edge.Cuts" user)
  )
  (net 1 "GND")
  (net 2 "VCC")
  (net 3 "NET1")
  (footprint "Resistor_SMD:R_0402"
    (layer "F.Cu")
    (at 10 10 0)
    (fp_text reference "R1" (at 0 -1.5) (layer "F.SilkS"))
    (fp_text value "10k" (at 0 1.5) (layer "F.Fab"))
    (pad "1" smd rect (at -0.5 0) (size 0.5 0.6) (layers "F.Cu" "F.Mask" "F.Paste")
      (net 1 "GND"))
    (pad "2" smd rect (at 0.5 0) (size 0.5 0.6) (layers "F.Cu" "F.Mask" "F.Paste")
      (net 3 "NET1"))
  )
  (footprint "Resistor_SMD:R_0402"
    (layer "F.Cu")
    (at 20 10 0)
    (fp_text reference "R2" (at 0 -1.5) (layer "F.SilkS"))
    (fp_text value "10k" (at 0 1.5) (layer "F.Fab"))
    (pad "1" smd rect (at -0.5 0) (size 0.5 0.6) (layers "F.Cu" "F.Mask" "F.Paste")
      (net 3 "NET1"))
    (pad "2" smd rect (at 0.5 0) (size 0.5 0.6) (layers "F.Cu" "F.Mask" "F.Paste")
      (net 2 "VCC"))
  )
  (segment (start 9.5 10) (end 10 10) (width 0.25) (layer "F.Cu") (net 1))
  (gr_line (start 0 0) (end 50 0) (layer "Edge.Cuts") (width 0.05))
  (gr_line (start 50 0) (end 50 50) (layer "Edge.Cuts") (width 0.05))
  (gr_line (start 50 50) (end 0 50) (layer "Edge.Cuts") (width 0.05))
  (gr_line (start 0 50) (end 0 0) (layer "Edge.Cuts") (width 0.05))
)
)KICAD";

// The account's own temp directory (TMPDIR/TEMP), never a literal /tmp: on a
// Mac shared with the operator lane, a fixed /tmp name left by another account
// is unwritable here (sticky /tmp), so the test would read a stale file or fail.
std::string write_temp(const std::string& name, const std::string& text) {
    const std::string path = (std::filesystem::temp_directory_path() / name).string();
    std::ofstream f(path, std::ios::binary);
    f << text;
    return path;
}

}  // namespace

TEST_CASE("kicad: import_kicad_sch (inline text)") {
    const json doc = chudware::ecad::kicad::import_kicad_sch(kMinimalSch);

    CHECK(doc.value("schema", std::string()) == "aicad.sch/1");
    const auto errs = chudware::ecad::schema::validate_schematic(doc);
    CHECK(errs.empty());

    // 2 placed symbols (R1, C1)
    REQUIRE(doc["symbols"].size() == 2);
    bool have_r1 = false, have_c1 = false;
    for (const json& s : doc["symbols"]) {
        if (s.value("ref", std::string()) == "R1") have_r1 = true;
        if (s.value("ref", std::string()) == "C1") have_c1 = true;
    }
    CHECK(have_r1);
    CHECK(have_c1);

    // lib_cache populated with Device:R / Device:C (+ pins on Device:R)
    const json& cache = doc["lib_cache"];
    REQUIRE(cache.size() >= 2);
    CHECK(cache.contains("Device:R"));
    CHECK(cache.contains("Device:C"));
    CHECK(cache["Device:R"]["pins"].size() >= 2);
    // Library pin coordinates are electrical endpoints in a +Y-up frame;
    // native pins store the body end and point out toward that endpoint.
    const auto rtip = chudware::ecad::schema::pin_world(doc["symbols"][0], cache["Device:R"]["pins"][0]);
    CHECK(rtip.first == doctest::Approx(48.26));
    CHECK(rtip.second == doctest::Approx(-50.8));
    const auto ctip = chudware::ecad::schema::pin_world(doc["symbols"][1], cache["Device:C"]["pins"][0]);
    CHECK(ctip.first == doctest::Approx(60.96));
    CHECK(ctip.second == doctest::Approx(-48.26));
    json rotated = doc["symbols"][1];
    rotated["angle"] = 90;
    rotated["mirror"] = "x";
    const auto mirrored = chudware::ecad::schema::pin_world(rotated, cache["Device:C"]["pins"][0]);
    CHECK(mirrored.first == doctest::Approx(63.5));
    CHECK(mirrored.second == doctest::Approx(-50.8));
    CHECK(cache["Device:R"]["pins"][0]["name"] == "");

    // wire / CLK label / global VCC label / junction / no_connect
    CHECK(doc["wires"].size() >= 1);
    bool have_clk = false, have_vcc_global = false;
    for (const json& l : doc["labels"]) {
        if (l.value("text", std::string()) == "CLK") have_clk = true;
        if (l.value("text", std::string()) == "VCC" &&
            l.value("scope", std::string()) == "global")
            have_vcc_global = true;
    }
    CHECK(have_clk);
    CHECK(have_vcc_global);
    CHECK(doc["junctions"].size() >= 1);
    CHECK(doc["no_connect"].size() >= 1);

    // y-flip: R1 at (50.8, -50.8) in +y-up coords
    CHECK(doc["symbols"][0]["pos"][0].get<double>() == doctest::Approx(50.8));
    CHECK(doc["symbols"][0]["pos"][1].get<double>() == doctest::Approx(-50.8));
}

TEST_CASE("kicad: import_kicad_pcb (inline text)") {
    const json doc = chudware::ecad::kicad::import_kicad_pcb(kMinimalPcb);

    CHECK(doc.value("schema", std::string()) == "aicad.pcb/1");
    const auto errs = chudware::ecad::schema::validate_board(doc);
    CHECK(errs.empty());

    // 2 footprints (R1, R2), 2 pads each
    REQUIRE(doc["footprints"].size() == 2);
    bool have_r1 = false, have_r2 = false;
    for (const json& fp : doc["footprints"]) {
        if (fp.value("ref", std::string()) == "R1") have_r1 = true;
        if (fp.value("ref", std::string()) == "R2") have_r2 = true;
        CHECK(fp["pads"].size() == 2);
    }
    CHECK(have_r1);
    CHECK(have_r2);

    // nets: GND, VCC, NET1
    bool have_gnd = false, have_vcc = false, have_net1 = false;
    for (const json& n : doc["nets"]) {
        const std::string nm = n.value("name", std::string());
        if (nm == "GND") have_gnd = true;
        if (nm == "VCC") have_vcc = true;
        if (nm == "NET1") have_net1 = true;
    }
    CHECK(have_gnd);
    CHECK(have_vcc);
    CHECK(have_net1);

    // pad net binding from (net N "name") inside the footprint pads
    CHECK(doc["footprints"][0]["pads"][0]["net"] == "GND");
    CHECK(doc["footprints"][0]["pads"][1]["net"] == "NET1");

    // track + Edge.Cuts outline (4 gr_lines)
    CHECK(doc["tracks"].size() >= 1);
    CHECK(doc["outline"]["shapes"].size() >= 4);
}

TEST_CASE("kicad: import from file (name stems from path)") {
    const std::string sch_path = write_temp("r4c_test.kicad_sch", kMinimalSch);
    const json doc = chudware::ecad::kicad::import_kicad_sch(sch_path);
    const auto errs = chudware::ecad::schema::validate_schematic(doc);
    CHECK(errs.empty());
    CHECK(doc.value("name", std::string()) == "r4c_test");
    std::remove(sch_path.c_str());
}

TEST_CASE("kicad: ecad_open dispatches .kicad_sch/.kicad_pcb to the importers") {
    const std::string sch_path = write_temp("r4c_open.kicad_sch", kMinimalSch);
    const json resp = chudware::ecad::ecad_open(sch_path);
    CHECK(resp.contains("doc_id"));
    CHECK(resp.contains("warnings"));
    CHECK(resp.value("kind", std::string()) == "sch");
    CHECK(resp.value("path", std::string()) == sch_path);
    CHECK(resp["warnings"].empty());
    CHECK(chudware::ecad::ecad_close(resp.value("doc_id", std::string())));
    std::remove(sch_path.c_str());

    const std::string pcb_path = write_temp("r4c_open.kicad_pcb", kMinimalPcb);
    const json resp2 = chudware::ecad::ecad_open(pcb_path);
    CHECK(resp2.value("kind", std::string()) == "pcb");
    CHECK(resp2.value("path", std::string()) == pcb_path);
    CHECK(chudware::ecad::ecad_close(resp2.value("doc_id", std::string())));
    std::remove(pcb_path.c_str());
}

TEST_CASE("kicad: ecad_open import failure mirrors the oracle's io_error envelope") {
    const std::string bad_path = write_temp("r4c_bad.kicad_sch", "(not_a_schematic 1 2 3)");
    try {
        (void)chudware::ecad::ecad_open(bad_path);
        FAIL("expected OpError");
    } catch (const chudware::ecad::OpError& oe) {
        CHECK(oe.envelope().value("code", std::string()) == "io_error");
        const std::string msg = oe.envelope().value("message", std::string());
        CHECK(msg.find("import_kicad_sch failed for '") == 0);
        CHECK(msg.find("Expected kicad_sch root") != std::string::npos);
    }
    std::remove(bad_path.c_str());
}

TEST_CASE("kicad: sexpr parser error shapes (sexpr.py)") {
    CHECK_THROWS_WITH(chudware::ecad::kicad::sexpr_parse(""), "Empty input");
    CHECK_THROWS_WITH(chudware::ecad::kicad::sexpr_parse("(a b"),
                      "Unexpected end of input: unmatched '('");
    CHECK_THROWS_WITH(chudware::ecad::kicad::sexpr_parse("(a) (b)"),
                      "Trailing content at token 3: '('");
}

TEST_CASE("kicad: untrusted parse work has depth, node and file-byte ceilings") {
    using chudware::ecad::kicad::sexpr_parse;
    CHECK_NOTHROW(sexpr_parse(std::string(128, '(') + "a" + std::string(128, ')')));
    CHECK_THROWS_WITH(sexpr_parse(std::string(4096, '(') + "a" + std::string(4096, ')')),
                      "KiCad depth limit exceeded");
    std::string wide = "(";
    for (int i = 0; i < 1000000; ++i) wide += "a ";
    wide += ")";
    CHECK_THROWS_WITH(sexpr_parse(wide), "KiCad node limit exceeded");

    // Sparse input proves file reads stop at the byte budget without allocating
    // or writing a huge hostile fixture in the test harness.
    const auto path = std::filesystem::temp_directory_path() / "chudware_kicad_budget.kicad_sch";
    {
        std::ofstream out(path, std::ios::binary);
        out << "(kicad_sch)";
    }
    std::filesystem::resize_file(path, 64 * 1024 * 1024 + 1);
    CHECK_THROWS_WITH(chudware::ecad::kicad::import_kicad_sch(path.string()),
                      "KiCad input byte limit exceeded");
    std::filesystem::remove(path);
}

namespace {

std::string fixture_zip(const std::map<std::string, std::string>& extras,
                        const std::string& document = R"({"schema":"aicad.sch/1","name":"Fixture"})") {
    mz_zip_archive zip{};
    REQUIRE(mz_zip_writer_init_heap(&zip, 0, 0));
    auto add = [&](const std::string& name, const std::string& data) {
        REQUIRE(mz_zip_writer_add_mem(&zip, name.c_str(), data.data(), data.size(),
                                      MZ_DEFAULT_COMPRESSION));
    };
    add("meta.json", R"({"container":"aicad.ecad+/1"})");
    add("document.json", document);
    for (const auto& [name, data] : extras) add(name, data);
    void* bytes = nullptr;
    std::size_t n = 0;
    REQUIRE(mz_zip_writer_finalize_heap_archive(&zip, &bytes, &n));
    std::string out(static_cast<const char*>(bytes), n);
    mz_free(bytes);
    mz_zip_writer_end(&zip);
    return out;
}

// ZIP central-directory uncompressed sizes are attacker-controlled metadata.
// Keep the actual compressed fixture tiny while testing production ceilings.
void set_zip_declared_sizes(std::string& bytes, unsigned size) {
    std::size_t pos = 0;
    while ((pos = bytes.find(std::string("PK\x01\x02", 4), pos)) != std::string::npos) {
        REQUIRE(pos + 28 <= bytes.size());
        for (unsigned i = 0; i < 4; ++i)
            bytes[pos + 24 + i] = static_cast<char>((size >> (8 * i)) & 0xff);
        pos += 28;
    }
}

}  // namespace

TEST_CASE("ecad: compressed containers reject inflated member, aggregate and count bounds") {
    using chudware::ecad::ecad_container_open;
    const auto path = std::filesystem::temp_directory_path() / "chudware_ecad_zip_budget.ecad+";
    auto open_bytes = [&](const std::string& bytes) {
        {
            std::ofstream out(path, std::ios::binary);
            out.write(bytes.data(), static_cast<std::streamsize>(bytes.size()));
        }
        return ecad_container_open(path.string());
    };

    std::string bytes = fixture_zip({{"attachment.bin", std::string(1024 * 1024, '\0')}});
    const auto valid = open_bytes(bytes);
    CHECK(valid.extra.at("attachment.bin") == std::string(1024 * 1024, '\0'));
    set_zip_declared_sizes(bytes, 64 * 1024 * 1024 + 1);
    CHECK_THROWS_WITH_AS(open_bytes(bytes),
                         "ecad_open: ZIP entry byte limit exceeded", chudware::ecad::OpError);

    bytes = fixture_zip({{"a", "a"}, {"b", "b"}, {"c", "c"}});
    set_zip_declared_sizes(bytes, 64 * 1024 * 1024);
    CHECK_THROWS_WITH_AS(open_bytes(bytes),
                         "ecad_open: ZIP aggregate byte limit exceeded", chudware::ecad::OpError);

    std::map<std::string, std::string> many;
    for (int i = 0; i < 4095; ++i) many["attachment-" + std::to_string(i)] = "";
    CHECK_THROWS_WITH_AS(open_bytes(fixture_zip(many)),
                         "ecad_open: ZIP entry count limit exceeded", chudware::ecad::OpError);

    // A dishonest stream larger than its allowed output buffer fails cleanly.
    bytes = fixture_zip({{"attachment.bin", std::string(4096, '\0')}});
    set_zip_declared_sizes(bytes, 1);
    CHECK_THROWS_AS(open_bytes(bytes), chudware::ecad::OpError);
    std::filesystem::remove(path);
}

TEST_CASE("ecad: legacy container reader retains unknown attachments") {
    const auto path = std::filesystem::temp_directory_path() / "chudware_ecad_attachment.ecad+";
    const json doc = chudware::ecad::schema::new_schematic("Attachments");
    const std::map<std::string, std::string> extras{
        {"notes/vendor.bin", std::string("vendor\0payload", 14)},
        {"ai/checkpoint.json", R"({"revision":3})"},
        {std::string(600, 'x'), "long member name preserved"}};
    const auto bytes = fixture_zip(extras, doc.dump());
    { std::ofstream out(path, std::ios::binary); out.write(bytes.data(), bytes.size()); }
    const auto opened = chudware::ecad::ecad_container_open(path.string());
    CHECK(opened.doc == doc);
    for (const auto& [name, data] : extras) CHECK(opened.extra.at(name) == data);
    std::filesystem::remove(path);
}

namespace {

struct SessionTestDir {
    std::filesystem::path path = std::filesystem::temp_directory_path() /
        ("chudware-kicad-session-" +
         std::to_string(std::chrono::high_resolution_clock::now().time_since_epoch().count()));
    SessionTestDir() { std::filesystem::create_directories(path); }
    ~SessionTestDir() { std::error_code ec; std::filesystem::remove_all(path, ec); }
};

template <class F> void check_code(const std::string& expected, F&& f) {
    try {
        f();
        FAIL("expected ECAD error " << expected);
    } catch (const chudware::ecad::OpError& error) {
        CHECK(error.code == expected);
    }
}

json add_note(const std::string& id, const std::string& text) {
    return chudware::ecad::ecad_apply_ops(id, -1, json::array({
        json{{"op", "sch_add_text"}, {"pos", json::array({8.0, 9.0})}, {"text", text}}}));
}

}  // namespace

TEST_CASE("KiCad projects create and reopen both document sessions") {
    using namespace chudware::ecad;
    namespace fs = std::filesystem;
    SessionTestDir tmp;
    const auto dir = tmp.path / "nested";
    const auto created = ecad_new_project(dir.string(), "controller");
    const auto project = dir / "controller.kicad_pro";
    const auto sch = dir / "controller.kicad_sch";
    const auto pcb = dir / "controller.kicad_pcb";
    CHECK(created["project"] == project.string());
    CHECK(fs::exists(project));
    CHECK(fs::exists(sch));
    CHECK(fs::exists(pcb));
    CHECK(created["sch"]["path"] == sch.string());
    CHECK(created["pcb"]["path"] == pcb.string());
    std::ifstream project_file(project);
    const json project_json = json::parse(project_file);
    CHECK(project_json["meta"]["version"] == 1);
    const auto sch_id = created["sch"]["doc_id"].get<std::string>();
    const auto pcb_id = created["pcb"]["doc_id"].get<std::string>();
    CHECK(ecad_tree(sch_id)["project"] == project.string());
    CHECK(ecad_tree(pcb_id)["project"] == project.string());
    check_code("io_error", [&] { ecad_new_project(dir.string(), "controller"); });
    if (kicad_libs::detect().found) {
        // The actual KiCad executable must parse the generated files.
        const auto erc = ecad_kicad_check(sch_id);
        const auto drc = ecad_kicad_check(pcb_id);
        CHECK(erc["kind"] == "erc");
        CHECK(drc["kind"] == "drc");
    }
    ecad_close(sch_id);
    ecad_close(pcb_id);
    const json opened = ecad_open_project(project.string());
    CHECK(opened["sch"]["path"] == sch.string());
    CHECK(opened["pcb"]["path"] == pcb.string());
    CHECK(opened["sch"]["project"] == project.string());
    ecad_close(opened["sch"]["doc_id"].get<std::string>());
    ecad_close(opened["pcb"]["doc_id"].get<std::string>());
}

TEST_CASE("KiCad sessions preserve untouched schematic data across edit and save") {
    using namespace chudware::ecad;
    SessionTestDir tmp;
    const auto path = tmp.path / "import.kicad_sch";
    { std::ofstream out(path); out << kMinimalSch; }
    const auto opened = ecad_open(path.string());
    const auto id = opened["doc_id"].get<std::string>();
    REQUIRE(opened["tree"]["symbols"].size() == 2);
    const auto original_wire = opened["tree"]["wires"][0]["pts"];
    add_note(id, "agent edit");
    const auto saved = ecad_save(id, "");
    CHECK(saved["format"] == "kicad");
    CHECK(saved["saved_revision"] == 1);
    const auto uuid_doc = ecad_session_doc(id);
    REQUIRE(uuid_doc["texts"].size() == 1);
    CHECK(uuid_doc["texts"][0]["uuid"].is_string());
    std::ifstream first_file(path, std::ios::binary);
    const std::string first_bytes{std::istreambuf_iterator<char>(first_file),
                                  std::istreambuf_iterator<char>()};
    first_file.close();
    CHECK(ecad_save(id, "")["saved_revision"] == 1);
    std::ifstream second_file(path, std::ios::binary);
    const std::string second_bytes{std::istreambuf_iterator<char>(second_file),
                                   std::istreambuf_iterator<char>()};
    CHECK(first_bytes == second_bytes);
    ecad_close(id);
    const auto reopened = ecad_open(path.string());
    CHECK(reopened["tree"]["symbols"].size() == 2);
    CHECK(reopened["tree"]["wires"][0]["pts"] == original_wire);
    CHECK(reopened["tree"]["texts"][0]["text"] == "agent edit");
    ecad_close(reopened["doc_id"].get<std::string>());
}

TEST_CASE("unrelated board edits preserve imported KiCad copper fills") {
    using namespace chudware::ecad;
    SessionTestDir tmp;
    const auto path = tmp.path / "zone.kicad_pcb";
    std::string source = kMinimalPcb;
    const auto closing = source.rfind(')');
    REQUIRE(closing != std::string::npos);
    source.insert(closing, R"KICAD(
  (zone (net 1) (net_name "GND") (layer "F.Cu")
    (uuid "7c8f318c-73a8-4c21-b831-61eb520bde7a") (hatch edge 0.5)
    (connect_pads (clearance 0.3)) (min_thickness 0.25)
    (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3))
    (polygon (pts (xy 1 1) (xy 5 1) (xy 5 5) (xy 1 5)))
    (filled_polygon (layer "F.Cu") (pts (xy 1.5 1.5) (xy 4.5 1.5)
      (xy 4.5 4.5) (xy 1.5 4.5))))
)KICAD");
    { std::ofstream out(path); out << source; }
    const auto opened = ecad_open(path.string());
    const auto id = opened["doc_id"].get<std::string>();
    REQUIRE(opened["tree"]["zones"].size() == 1);
    const json fill = opened["tree"]["zones"][0]["filled_polys"];
    REQUIRE_FALSE(fill.empty());
    const auto changed = ecad_apply_ops(id, -1, json::array({
        json{{"op", "pcb_add_track"}, {"net", "GND"}, {"layer", "F.Cu"},
             {"p1", json::array({30.0, -30.0})}, {"p2", json::array({31.0, -30.0})}}}));
    CHECK(changed["tree"]["zones"][0]["filled_polys"] == fill);
    ecad_save(id, "");
    ecad_close(id);
    const auto reopened = ecad_open(path.string());
    CHECK(reopened["tree"]["zones"][0]["filled_polys"] == fill);
    ecad_close(reopened["doc_id"].get<std::string>());
}

TEST_CASE("KiCad session refuses locks and concurrent edits and reloads safely") {
    using namespace chudware::ecad;
    namespace fs = std::filesystem;
    SessionTestDir tmp;
    const auto path = tmp.path / "shared.kicad_sch";
    const auto first = ecad_new("sch", "Shared", path.string());
    const auto first_id = first["doc_id"].get<std::string>();
    const auto lock = tmp.path / "~shared.kicad_sch.lck";
    { std::ofstream out(lock); out << "KiCad"; }
    CHECK(ecad_tree(first_id)["kicad_locked"] == true);
    check_code("kicad_locked", [&] { ecad_save(first_id, ""); });
    fs::remove(lock);

    const auto second = ecad_open(path.string());
    const auto second_id = second["doc_id"].get<std::string>();
    add_note(second_id, "external");
    ecad_save(second_id, "");
    const auto refreshed = ecad_tree(first_id);
    CHECK(refreshed["file_changed"] == true);
    REQUIRE(refreshed["tree"]["texts"].size() == 1);
    CHECK(refreshed["tree"]["texts"][0]["text"] == "external");
    CHECK(ecad_tree(first_id)["file_changed"] == false);

    add_note(first_id, "unsaved");
    add_note(second_id, "external again");
    ecad_save(second_id, "");
    CHECK(ecad_tree(first_id)["file_changed"] == true);
    check_code("external_change", [&] { ecad_save(first_id, ""); });
    check_code("dirty", [&] { ecad_reload(first_id, false); });
    const auto reloaded = ecad_reload(first_id, true);
    CHECK(reloaded["tree"]["texts"].size() == 2);
    CHECK(ecad_tree(first_id)["dirty"] == false);
    ecad_close(first_id);
    ecad_close(second_id);
}

TEST_CASE("Save As preserves dirty work after the original changes") {
    using namespace chudware::ecad;
    SessionTestDir tmp;
    const auto original = tmp.path / "original.kicad_sch";
    const auto copy = tmp.path / "copy.kicad_sch";
    const auto id = ecad_new("sch", "Original", original.string())["doc_id"].get<std::string>();
    const auto other = ecad_open(original.string())["doc_id"].get<std::string>();
    add_note(id, "local work");
    add_note(other, "external work");
    ecad_save(other, "");
    check_code("external_change", [&] { ecad_save(id, ""); });
    const auto saved = ecad_save(id, copy.string());
    CHECK(saved["path"] == copy.string());
    CHECK(ecad_tree(id)["path"] == copy.string());
    CHECK(ecad_tree(id)["dirty"] == false);
    const auto reopened = ecad_open(copy.string());
    CHECK(reopened["tree"]["texts"][0]["text"] == "local work");
    ecad_close(reopened["doc_id"].get<std::string>());
    ecad_close(id);
    ecad_close(other);
}

TEST_CASE("schematic saves ignore companion edits while Save As copies their latest state") {
    using namespace chudware::ecad;
    SessionTestDir tmp;
    const auto project = ecad_new_project(tmp.path.string(), "shared");
    const auto sch = project["sch"]["doc_id"].get<std::string>();
    const auto pcb = project["pcb"]["doc_id"].get<std::string>();
    const auto project_path = tmp.path / "shared.kicad_pro";
    const auto rules_path = tmp.path / "shared.kicad_dru";
    json changed;
    { std::ifstream in(project_path); in >> changed; }
    changed["text_variables"]["EXTERNAL"] = "preserve";
    const std::string rules = "(version 1)\n(rule \"external\" (constraint clearance (min 0.3mm)))\n";
    add_note(sch, "edited schematic");
    { std::ofstream out(project_path); out << changed.dump(2); }
    { std::ofstream out(rules_path); out << rules; }
    ecad_save(sch, "");
    CHECK_FALSE(ecad_tree(sch)["dirty"].get<bool>());
    check_code("external_change", [&] { ecad_save(pcb, ""); });
    const auto copy = tmp.path / "copied.kicad_sch";
    ecad_save(sch, copy.string());
    json copied;
    { std::ifstream in(tmp.path / "copied.kicad_pro"); in >> copied; }
    changed["meta"]["filename"] = "copied.kicad_pro";
    CHECK(copied == changed);
    std::ifstream rule_input(tmp.path / "copied.kicad_dru", std::ios::binary);
    CHECK(std::string(std::istreambuf_iterator<char>(rule_input),
                      std::istreambuf_iterator<char>()) == rules);
    // Even a temporarily malformed companion must not block a document-only save.
    add_note(sch, "another edit");
    { std::ofstream out(tmp.path / "copied.kicad_pro"); out << "{"; }
    ecad_save(sch, "");
    CHECK_FALSE(ecad_tree(sch)["dirty"].get<bool>());
    ecad_close(sch);
    ecad_close(pcb);
}

TEST_CASE("Save As preserves the full KiCad project and custom rules without clobbering") {
    using namespace chudware::ecad;
    namespace fs = std::filesystem;
    SessionTestDir tmp;
    const auto created = ecad_new_project(tmp.path.string(), "source");
    ecad_close(created["sch"]["doc_id"].get<std::string>());
    const auto id = created["pcb"]["doc_id"].get<std::string>();
    const auto project_path = tmp.path / "source.kicad_pro";
    json project;
    { std::ifstream in(project_path); in >> project; }
    project["board"]["design_settings"]["rules"]["min_via_annular_width"] = 0.27;
    project["board"]["design_settings"]["rule_severities"]["track_width"] = "warning";
    project["pcbnew"]["custom_setting"] = {{"preserved", true}};
    project["net_settings"]["netclass_patterns"] =
        json::array({json{{"netclass", "Default"}, {"pattern", "USB*"}}});
    { std::ofstream out(project_path); out << project.dump(2); }
    const auto rules_path = tmp.path / "source.kicad_dru";
    const std::string rules = "(version 1)\n(rule \"custom\" (constraint clearance (min 0.4mm)))\n";
    { std::ofstream out(rules_path); out << rules; }
    CHECK(ecad_tree(id)["file_changed"] == true);
    const auto copy = tmp.path / "copy.kicad_pcb";
    ecad_save(id, copy.string());
    json copied_project;
    { std::ifstream in(tmp.path / "copy.kicad_pro"); in >> copied_project; }
    project["meta"]["filename"] = "copy.kicad_pro";
    CHECK(copied_project == project);
    std::ifstream rules_in(tmp.path / "copy.kicad_dru", std::ios::binary);
    const std::string copied_rules{std::istreambuf_iterator<char>(rules_in),
                                   std::istreambuf_iterator<char>()};
    CHECK(copied_rules == rules);

    const auto occupied = tmp.path / "occupied.kicad_pcb";
    const auto occupied_project = tmp.path / "occupied.kicad_pro";
    const json sentinel = {{"meta", {{"filename", "occupied.kicad_pro"}}}, {"other", 42}};
    { std::ofstream out(occupied_project); out << sentinel.dump(); }
    check_code("external_change", [&] { ecad_save(id, occupied.string()); });
    CHECK_FALSE(fs::exists(occupied));
    json unchanged;
    { std::ifstream in(occupied_project); in >> unchanged; }
    CHECK(unchanged == sentinel);
    ecad_close(id);
}

TEST_CASE("KiCad project preserves board rules, classes, assignments, and SI data") {
    using namespace chudware::ecad;
    SessionTestDir tmp;
    const auto created = ecad_new_project(tmp.path.string(), "rules");
    const auto id = created["pcb"]["doc_id"].get<std::string>();
    const auto sch_id = created["sch"]["doc_id"].get<std::string>();
    add_note(sch_id, "independent schematic edit");
    auto board = ecad_session_doc(id);
    board["rules"]["track_width_min"] = 0.17;
    board["net_classes"]["fast"] = {{"clearance", 0.23}, {"track_width", 0.31},
                                      {"via_diameter", 0.7}, {"via_drill", 0.35},
                                      {"diff_gap", 0.19}, {"rise_time_ps", 250}};
    REQUIRE_FALSE(board["nets"].empty());
    board["nets"][0]["class"] = "fast";
    ecad_swap_doc(id, board, -1, json::array());
    const auto project_lock = tmp.path / "~rules.kicad_pro.lck";
    { std::ofstream out(project_lock); out << "KiCad"; }
    check_code("kicad_locked", [&] { ecad_save(id, ""); });
    std::filesystem::remove(project_lock);
    ecad_save(id, "");
    // The PCB's own project-settings write must not be mistaken for an
    // external conflict by a dirty sibling schematic.
    ecad_save(sch_id, "");
    CHECK(ecad_tree(sch_id)["dirty"] == false);
    const auto project_path = tmp.path / "rules.kicad_pro";
    std::ifstream input(project_path);
    json project = json::parse(input);
    input.close();
    bool found = false;
    for (const auto& cls : project["net_settings"]["classes"])
        if (cls.value("name", std::string()) == "fast") {
            found = true;
            CHECK(cls["track_width"] == 0.31);
            CHECK(cls["diff_pair_gap"] == 0.19);
        }
    CHECK(found);
    CHECK(project["net_settings"]["netclass_assignments"]["GND"][0] == "fast");
    ecad_close(id);
    ecad_close(created["sch"]["doc_id"].get<std::string>());

    const auto reopened = ecad_open((tmp.path / "rules.kicad_pcb").string());
    const auto opened_id = reopened["doc_id"].get<std::string>();
    CHECK(reopened["tree"]["rules"]["track_width_min"] == 0.17);
    CHECK(reopened["tree"]["net_classes"]["fast"]["rise_time_ps"] == 250);
    CHECK(reopened["tree"]["nets"][0]["class"] == "fast");
    project["board"]["design_settings"]["rules"]["min_track_width"] = 0.21;
    { std::ofstream out(project_path); out << project.dump(2); }
    const auto refreshed = ecad_tree(opened_id);
    CHECK(refreshed["file_changed"] == true);
    CHECK(refreshed["tree"]["rules"]["track_width_min"] == 0.21);
    auto changed = refreshed["tree"];
    changed["rules"]["track_width_min"] = 0.24;
    ecad_swap_doc(opened_id, changed, -1, json::array());
    project["board"]["design_settings"]["rules"]["min_track_width"] = 0.25;
    { std::ofstream out(project_path); out << project.dump(2); }
    check_code("external_change", [&] { ecad_save(opened_id, ""); });
    const auto copy = tmp.path / "rules-copy.kicad_pcb";
    CHECK(ecad_save(opened_id, copy.string())["path"] == copy.string());
    const auto copy_opened = ecad_open(copy.string());
    CHECK(copy_opened["tree"]["rules"]["track_width_min"] == 0.24);
    CHECK(copy_opened["tree"]["net_classes"]["fast"]["rise_time_ps"] == 250);
    ecad_close(copy_opened["doc_id"].get<std::string>());
    ecad_close(opened_id);
}

TEST_CASE("KiCad file fingerprint detects same-size edits with unchanged timestamps") {
    using namespace chudware::ecad;
    namespace fs = std::filesystem;
    SessionTestDir tmp;
    const auto path = tmp.path / "fingerprint.kicad_sch";
    const auto id = ecad_new("sch", "Fingerprint", path.string())["doc_id"].get<std::string>();
    const auto timestamp = fs::last_write_time(path);
    std::ifstream in(path, std::ios::binary);
    std::string bytes{std::istreambuf_iterator<char>(in), std::istreambuf_iterator<char>()};
    in.close();
    const auto pos = bytes.find("chudware");
    REQUIRE(pos != std::string::npos);
    bytes[pos] = 'C';
    { std::ofstream out(path, std::ios::binary | std::ios::trunc); out << bytes; }
    fs::last_write_time(path, timestamp);
    CHECK(ecad_tree(id)["file_changed"] == true);
    CHECK(ecad_tree(id)["file_changed"] == false);
    ecad_close(id);
}

TEST_CASE("KiCad sessions support UTF-8 project paths") {
    using namespace chudware::ecad;
    namespace fs = std::filesystem;
    SessionTestDir tmp;
    const auto dir = tmp.path / fs::u8path(u8"électronique");
    const auto created = ecad_new_project(dir.u8string(), u8"capteur");
    const auto id = created["sch"]["doc_id"].get<std::string>();
    add_note(id, "measurement");
    ecad_save(id, "");
    ecad_close(id);
    ecad_close(created["pcb"]["doc_id"].get<std::string>());
    const auto opened = ecad_open_project(created["project"].get<std::string>());
    CHECK(opened["sch"]["tree"]["texts"][0]["text"] == "measurement");
    ecad_close(opened["sch"]["doc_id"].get<std::string>());
    ecad_close(opened["pcb"]["doc_id"].get<std::string>());
}

TEST_CASE("legacy ECAD opens only as an unbound migration") {
    using namespace chudware::ecad;
    SessionTestDir tmp;
    const auto path = tmp.path / "legacy.ecad+";
    const auto doc = schema::new_schematic("Legacy");
    const auto bytes = fixture_zip({}, doc.dump());
    { std::ofstream out(path, std::ios::binary); out.write(bytes.data(), bytes.size()); }
    const auto opened = ecad_open(path.string());
    const auto id = opened["doc_id"].get<std::string>();
    CHECK(opened["path"] == "");
    CHECK_FALSE(opened["warnings"].empty());
    check_code("needs_path", [&] { ecad_save(id, ""); });
    const auto occupied = tmp.path / "occupied.kicad_sch";
    { std::ofstream out(occupied); out << "existing"; }
    check_code("io_error", [&] { ecad_save(id, occupied.string()); });
    { std::ifstream in(occupied); std::string text; in >> text; CHECK(text == "existing"); }
    const auto migrated = tmp.path / "migrated.kicad_sch";
    CHECK(ecad_save(id, migrated.string())["format"] == "kicad");
    CHECK(std::filesystem::exists(migrated));
    ecad_close(id);
    const auto raw = tmp.path / "legacy.json";
    { std::ofstream out(raw); out << doc.dump(); }
    const auto json_open = ecad_open(raw.string());
    CHECK(json_open["path"] == "");
    ecad_close(json_open["doc_id"].get<std::string>());
}

TEST_CASE("KiCad recovery restores dirty document and baseline without overwriting source") {
    using namespace chudware::ecad;
    SessionTestDir tmp;
    const auto path = tmp.path / "live.kicad_sch";
    const auto id = ecad_new("sch", "Recovery", path.string())["doc_id"].get<std::string>();
    add_note(id, "unsaved");
    check_code("io_error", [&] { ecad_save(id, path.string(), true); });
    const auto recovery_dir = tmp.path / "autosave";
    const auto saved = ecad_save(id, "", json{{"pid", 1}, {"name", "Recovery"},
                                               {"recovery_dir", recovery_dir.string()}});
    const auto snapshot = std::filesystem::path(saved["path"].get<std::string>());
    CHECK(snapshot.parent_path() == recovery_dir);
    CHECK(saved["format"] == "kicad");
    CHECK(std::filesystem::exists(snapshot.string() + ".recovery.json"));
    const auto disk = ecad_open(path.string());
    CHECK(disk["tree"]["texts"].empty());
    ecad_close(disk["doc_id"].get<std::string>());
    ecad_close(id);
    const auto recovered = ecad_open(snapshot.string());
    const auto recovered_id = recovered["doc_id"].get<std::string>();
    CHECK(recovered["path"] == path.string());
    CHECK(recovered["tree"]["name"] == "Recovery");
    CHECK(ecad_tree(recovered_id)["dirty"] == true);
    CHECK(recovered["tree"]["texts"][0]["text"] == "unsaved");
    ecad_save(recovered_id, "");
    ecad_close(recovered_id);
    std::error_code ec;
    std::filesystem::remove(snapshot, ec);
    std::filesystem::remove(snapshot.string() + ".recovery.json", ec);
}

TEST_CASE("PCB recovery retains unsaved project-level design settings") {
    using namespace chudware::ecad;
    SessionTestDir tmp;
    const auto path = tmp.path / "recover.kicad_pcb";
    const auto id = ecad_new("pcb", "Recover PCB", path.string())["doc_id"].get<std::string>();
    auto doc = ecad_session_doc(id);
    doc["net_classes"]["default"]["rise_time_ps"] = 180;
    doc["rules"]["track_width_min"] = 0.19;
    ecad_swap_doc(id, doc, -1, json::array());
    const auto snapshot = ecad_save(id, "", json{{"recovery_dir", (tmp.path / "snapshots").string()}});
    ecad_close(id);
    const auto recovered = ecad_open(snapshot["path"].get<std::string>());
    const auto recovered_id = recovered["doc_id"].get<std::string>();
    CHECK(recovered["path"] == path.string());
    CHECK(recovered["tree"]["net_classes"]["default"]["rise_time_ps"] == 180);
    CHECK(recovered["tree"]["rules"]["track_width_min"] == 0.19);
    ecad_save(recovered_id, "");
    ecad_close(recovered_id);
    const auto reopened = ecad_open(path.string());
    CHECK(reopened["tree"]["net_classes"]["default"]["rise_time_ps"] == 180);
    CHECK(reopened["tree"]["rules"]["track_width_min"] == 0.19);
    ecad_close(reopened["doc_id"].get<std::string>());
}
