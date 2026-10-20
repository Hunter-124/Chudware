// T-RF17 — KiCad importers (ecad/kicad.cpp) + the ecad_open extension dispatch.
// Mirrors engine/ecad/test_formats.py tests 3-6 (import_kicad_sch /
// import_kicad_pcb / file-based import / open dispatch) against the same inline
// fixtures. Pure JSON, OCCT-free; links chudware_ecad only.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include "ecad/container.h"
#include "ecad/kicad.h"
#include "ecad/schema.h"
#include "ecad/session.h"

#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <string>

#include <nlohmann/json.hpp>
#include <doctest/doctest.h>
#include <miniz.h>
#include <map>

using nlohmann::json;

namespace {

// engine/ecad/test_formats.py::_MINIMAL_SCH (verbatim)
const char* kMinimalSch = R"KICAD(
(kicad_sch (version 20221018) (generator AI-ECAD-test)
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
(kicad_pcb (version 20221018) (generator AI-ECAD-test)
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
    CHECK(chudware::ecad::ecad_close(resp.value("doc_id", std::string())));
    std::remove(sch_path.c_str());

    const std::string pcb_path = write_temp("r4c_open.kicad_pcb", kMinimalPcb);
    const json resp2 = chudware::ecad::ecad_open(pcb_path);
    CHECK(resp2.value("kind", std::string()) == "pcb");
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

std::string fixture_zip(const std::map<std::string, std::string>& extras) {
    mz_zip_archive zip{};
    REQUIRE(mz_zip_writer_init_heap(&zip, 0, 0));
    auto add = [&](const std::string& name, const std::string& data) {
        REQUIRE(mz_zip_writer_add_mem(&zip, name.c_str(), data.data(), data.size(),
                                      MZ_DEFAULT_COMPRESSION));
    };
    add("meta.json", R"({"container":"aicad.ecad+/1"})");
    add("document.json", R"({"schema":"aicad.sch/1","name":"Fixture"})");
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

TEST_CASE("ecad: valid compressed unknown attachments round-trip byte faithfully") {
    const auto path = std::filesystem::temp_directory_path() / "chudware_ecad_attachment.ecad+";
    const json doc = chudware::ecad::schema::new_schematic("Attachments");
    const std::map<std::string, std::string> extras{
        {"notes/vendor.bin", std::string("vendor\0payload", 14)},
        {"ai/checkpoint.json", R"({"revision":3})"},
        {std::string(600, 'x'), "long member name preserved"}};
    chudware::ecad::ecad_container_save(doc, path.string(), extras);
    const auto opened = chudware::ecad::ecad_container_open(path.string());
    CHECK(opened.doc == doc);
    for (const auto& [name, data] : extras) CHECK(opened.extra.at(name) == data);
    chudware::ecad::ecad_container_save(opened.doc, path.string(), opened.extra);
    const auto reopened = chudware::ecad::ecad_container_open(path.string());
    CHECK(reopened.doc == doc);
    CHECK(reopened.extra == opened.extra);
    std::filesystem::remove(path);
}
