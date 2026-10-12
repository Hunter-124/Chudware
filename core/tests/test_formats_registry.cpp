// Format-registry parity tests (T-RF19 — engine_oracle/doc cutover).
//
// The doc_formats RPC payload is a byte-parity port of the Python oracle's
// engine/doc/formats.py::list_formats (FORMAT_REGISTRY). These tests pin the
// C++ payload against the EXACT oracle dump (captured 2026-07-22 via
// `python3 -c "import json; from engine.doc import formats;
//  print(json.dumps(formats.list_formats()))"`) and lock the detect_format
// extension map to the registry. If formats.py's registry changes, regenerate
// the oracle dump below.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <cctype>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

#include "doc/formats.h"
#include "doc/session.h"

using json = nlohmann::json;

// Oracle dump of engine/doc/formats.py::list_formats() (semantic compare — the
// oracle serializes keys in insertion order, the C++ port in sorted order; the
// envelope content is identical).
static const char* kOracleFormats = R"JSON(
[{"fmt": "step", "label": "STEP", "exts": [".step", ".stp"], "lane": "brep", "can_import": true, "can_export": true}, {"fmt": "iges", "label": "IGES", "exts": [".iges", ".igs"], "lane": "brep", "can_import": true, "can_export": true}, {"fmt": "brep", "label": "BREP (OpenCASCADE)", "exts": [".brep"], "lane": "brep", "can_import": true, "can_export": true}, {"fmt": "stl", "label": "STL", "exts": [".stl"], "lane": "mesh", "can_import": true, "can_export": true}, {"fmt": "3mf", "label": "3MF", "exts": [".3mf"], "lane": "mesh", "can_import": true, "can_export": true}, {"fmt": "obj", "label": "Wavefront OBJ", "exts": [".obj"], "lane": "mesh", "can_import": true, "can_export": true}, {"fmt": "ply", "label": "Stanford PLY", "exts": [".ply"], "lane": "mesh", "can_import": true, "can_export": true}, {"fmt": "gltf", "label": "glTF", "exts": [".gltf"], "lane": "mesh", "can_import": true, "can_export": true}, {"fmt": "glb", "label": "glTF binary", "exts": [".glb"], "lane": "mesh", "can_import": true, "can_export": true}, {"fmt": "3dm", "label": "Rhino", "exts": [".3dm"], "lane": "rhino", "can_import": true, "can_export": true}, {"fmt": "dxf", "label": "AutoCAD DXF", "exts": [".dxf"], "lane": "brep", "can_import": true, "can_export": true}, {"fmt": "dwg", "label": "AutoCAD DWG", "exts": [".dwg"], "lane": "brep", "can_import": true, "can_export": true}, {"fmt": "sat", "label": "ACIS SAT", "exts": [".sat", ".sab"], "lane": "brep", "can_import": true, "can_export": true}, {"fmt": "sldprt", "label": "SolidWorks part", "exts": [".sldprt"], "lane": "proprietary", "can_import": true, "can_export": false}, {"fmt": "sldasm", "label": "SolidWorks assembly", "exts": [".sldasm"], "lane": "proprietary", "can_import": true, "can_export": false}, {"fmt": "x_t", "label": "Parasolid (text)", "exts": [".x_t"], "lane": "proprietary", "can_import": true, "can_export": false}, {"fmt": "x_b", "label": "Parasolid (binary)", "exts": [".x_b"], "lane": "proprietary", "can_import": true, "can_export": false}]
)JSON";

TEST_CASE("doc_formats payload matches the Python oracle byte-for-byte (semantic)") {
    const json payload = chudware::doc::doc_formats();
    REQUIRE(payload.is_object());
    REQUIRE(payload.contains("formats"));
    // nlohmann object equality is key-order-insensitive; array order IS compared,
    // so registry order must match the oracle exactly.
    CHECK(payload["formats"] == json::parse(kOracleFormats));
}

TEST_CASE("format_registry exposes the full matrix in oracle order") {
    const auto& reg = chudware::doc::format_registry();
    const json oracle = json::parse(kOracleFormats);
    REQUIRE(reg.size() == oracle.size());
    for (std::size_t i = 0; i < reg.size(); ++i) {
        CAPTURE(i);
        CHECK(reg[i].fmt == oracle[i]["fmt"].get<std::string>());
        CHECK(reg[i].label == oracle[i]["label"].get<std::string>());
        CHECK(reg[i].exts == oracle[i]["exts"].get<std::vector<std::string>>());
        CHECK(reg[i].lane == oracle[i]["lane"].get<std::string>());
        CHECK(reg[i].can_import == oracle[i]["can_import"].get<bool>());
        CHECK(reg[i].can_export == oracle[i]["can_export"].get<bool>());
    }
}

TEST_CASE("detect_format resolves every registry extension to its fmt") {
    // The extension map must be a projection of the registry (formats.py::
    // _EXT_TO_FORMAT is derived from the same registry — drift here means the
    // host offers a format the sniffer then rejects).
    for (const auto& fi : chudware::doc::format_registry()) {
        for (const std::string& ext : fi.exts) {
            CAPTURE(ext);
            CHECK(chudware::doc::detect_format("some/path/Body" + ext) == fi.fmt);
            // Case-insensitive, like formats.py::_detect_format (suffix.lower()).
            std::string upper = ext;
            for (char& c : upper) c = static_cast<char>(std::toupper(static_cast<unsigned char>(c)));
            CHECK(chudware::doc::detect_format("BODY" + upper) == fi.fmt);
        }
    }
}

TEST_CASE("detect_format rejects an unknown extension like the oracle") {
    bool threw = false;
    try {
        (void)chudware::doc::detect_format("model.xyz");
    } catch (const chudware::doc::OpError& e) {
        threw = true;
        CHECK(e.code == "invalid_op");
    }
    CHECK(threw);
}
