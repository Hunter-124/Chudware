#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>
#include "doc/session.h"
#include "doc/scad.h"
#include "doc/formats.h"
#include <BRepGProp.hxx>
#include <GProp_GProps.hxx>
#include <filesystem>
#include <fstream>
#include <random>
#include <vector>

using json = nlohmann::json;
namespace fs = std::filesystem;
using namespace chudware::doc;

namespace {
struct Workspace {
    fs::path dir = fs::temp_directory_path() / ("chudware_scad_integration_" + std::to_string(std::random_device{}()));
    Workspace() { fs::create_directories(dir); }
    ~Workspace() { std::error_code ec; fs::remove_all(dir, ec); }
};
void put(const fs::path& path, const std::string& bytes) {
    std::ofstream f(path, std::ios::binary);
    REQUIRE(f.good());
    f << bytes;
    REQUIRE(f.good());
}
bool renderer_ready() { return scad::capability().value("available", false); }
}

TEST_CASE("failed SCAD open disposes the temporary session and preserves an existing document") {
    Workspace ws;
    const auto bad = ws.dir / "invalid.scad";
    put(bad, "cube([);\n");
    const json existing = doc_new("part", "Existing document");
    const std::string id = existing.at("doc_id");
    const json before_tree = doc_tree(id);
    const json before_docs = doc_list().at("docs");
    try {
        (void)doc_open(bad.string());
        FAIL("invalid SCAD must not be opened");
    } catch (const OpError& e) {
        CHECK(e.code == "import_failed");
        CHECK(std::string(e.what()).find("SCAD import failed") != std::string::npos);
        CHECK_FALSE(e.detail.contains("doc_id"));
        CHECK(e.detail.contains("rebuild"));
    }
    CHECK(doc_list().at("docs") == before_docs);
    CHECK(doc_tree(id).at("tree") == before_tree.at("tree"));
    CHECK(doc_tree(id).at("revision") == before_tree.at("revision"));
    CHECK_THROWS_AS(doc_import_file(bad.string(), id, false, "scad"), OpError);
    CHECK(doc_list().at("docs") == before_docs);
    CHECK(doc_tree(id).at("tree") == before_tree.at("tree"));
    CHECK(doc_tree(id).at("revision") == before_tree.at("revision"));
    CHECK(doc_close(id));
}

TEST_CASE("real renderer preserves use and included module semantics through project export") {
    if (!renderer_ready()) { MESSAGE("real isolated OpenSCAD unavailable; integration smoke requires it"); return; }
    Workspace ws;
    put(ws.dir / "lib.scad",
        "function lib_width() = 2;\nmodule libbox() { cube([lib_width(),3,4]); }\n");
    put(ws.dir / "mid.scad",
        "use <lib.scad>\nfunction shift() = 1;\n"
        "module midbox() { translate([shift(),0,0]) libbox(); }\n");
    const std::vector<std::string> sources = {
        "use <lib.scad>\nlibbox();\n",
        "include <mid.scad>\nfunction factor() = 1;\n"
        "module part() { scale([factor(),1,1]) midbox(); }\npart();\n"
    };
    for (std::size_t i = 0; i < sources.size(); ++i) {
        const fs::path source = ws.dir / ("source" + std::to_string(i) + ".scad");
        const fs::path output_dir = ws.dir / ("export" + std::to_string(i));
        fs::create_directory(output_dir);
        put(source, sources[i]);
        const json params = scad::import_project(source.string());
        const auto first = scad::build(params, {});
        REQUIRE_FALSE(first.shape.IsNull());
        GProp_GProps first_props;
        BRepGProp::VolumeProperties(first.shape, first_props);
        CHECK(first_props.Mass() == doctest::Approx(24.0).epsilon(1e-6));
        const fs::path exported = output_dir / "project.scad";
        const json report = scad::export_project(params, exported.string());
        CHECK(report.at("ok") == true);
        const auto second = scad::build(scad::import_project(exported.string()), {});
        REQUIRE_FALSE(second.shape.IsNull());
        GProp_GProps second_props;
        BRepGProp::VolumeProperties(second.shape, second_props);
        CHECK(second_props.Mass() == doctest::Approx(first_props.Mass()).epsilon(1e-6));
    }
}

TEST_CASE("SCAD disconnected loop solids survive rebuild and warm reopen") {
    if (!renderer_ready()) { MESSAGE("real isolated OpenSCAD unavailable; integration smoke requires it"); return; }
    Workspace ws;
    fs::create_directory(ws.dir / "parts");
    put(ws.dir / "parts" / "pad.scad",
        "module pad(w,t) { difference() { linear_extrude(height=t) square([w,1]); "
        "translate([w/2,0.5,-0.1]) cylinder(r=0.1,h=t+0.2,$fn=32); } }\n");
    put(ws.dir / "pads.scad",
        "include <parts/pad.scad>\nwidth=2;\nthickness=0.25;\n"
        "for(i=[0:1]) translate([i*(width+0.25),0,0]) pad(width,thickness);\n");
    json opened = doc_open((ws.dir / "pads.scad").string());
    REQUIRE_FALSE(opened.contains("rejected_tree"));
    std::string id = opened.at("doc_id");
    const std::string fid = opened.at("tree").at("features")[0].at("id");
    json edited = doc_apply_ops(id, opened.at("revision"), json::array({
        {{"op","edit_feature"},{"id",fid},{"params",{{"mm_per_source_unit",25.4}}}}
    }), false);
    INFO(edited.dump());
    REQUIRE_FALSE(edited.contains("rejected_tree"));
    CHECK(edited.at("tess").at("bodies").size() == 2);
    CHECK(edited.at("volume").get<double>() == doctest::Approx(2 * 8065.653965679829).epsilon(1e-4));
    const json size = doc_bbox(id).at("size");
    CHECK(size[0].get<double>() == doctest::Approx(107.95).epsilon(1e-5));
    CHECK(size[1].get<double>() == doctest::Approx(25.4).epsilon(1e-5));
    CHECK(size[2].get<double>() == doctest::Approx(6.35).epsilon(1e-5));
    const fs::path native = ws.dir / "pads.step+";
    doc_save(id, native.string());
    REQUIRE(doc_close(id));
    const json restored = doc_open(native.string());
    CHECK(restored.at("warm") == true);
    CHECK(restored.at("tess").at("bodies").size() == 2);
    CHECK(restored.at("volume").get<double>() == doctest::Approx(edited.at("volume").get<double>()).epsilon(1e-6));
    id = restored.at("doc_id").get<std::string>();
    CHECK(doc_close(id));
}

TEST_CASE("SCAD format routes to the source feature and preserves saved warm state") {
    CHECK(detect_format("model.SCAD") == "scad");
    if (!renderer_ready()) { MESSAGE("real isolated OpenSCAD unavailable; integration smoke requires it"); return; }
    Workspace ws;
    put(ws.dir / "dep.scad", "module peg() { cylinder(h=4,r=1,$fn=16); }\n");
    put(ws.dir / "model.scad", "include <dep.scad>\nsize=10;\ndifference(){ cube([size,size,4]); translate([5,5,0]) peg(); }\n");
    json opened = doc_open((ws.dir / "model.scad").string());
    INFO(opened.dump());
    REQUIRE_FALSE(opened.contains("rejected_tree"));
    std::string id = opened.at("doc_id");
    json original = doc_tree(id).at("tree");
    REQUIRE(original.at("features").size() == 1);
    json feature = original.at("features")[0];
    CHECK(feature.at("type") == "scad");
    CHECK(feature.at("params").at("files").size() == 1);
    const std::string fid = feature.at("id");
    const long revision = opened.at("revision");
    json changed = doc_apply_ops(id, revision, json::array({{{"op","edit_feature"},{"id",fid},
        {"params",{{"parameters",{{"size",12}}}}}}}), false);
    INFO(changed.dump());
    REQUIRE_FALSE(changed.contains("rejected_tree"));
    CHECK(changed.at("tree").at("features")[0].at("id") == fid);
    json before = doc_tree(id);
    json rejected = doc_apply_ops(id, changed.at("revision"), json::array({{{"op","edit_feature"},
        {"id",fid},{"params",{{"source","size=10;\ncube([);"}}}}}), false);
    CHECK(rejected.contains("rejected_tree"));
    CHECK(doc_tree(id).at("tree") == before.at("tree"));
    CHECK(doc_tree(id).at("revision") == before.at("revision"));
    CHECK(rejected.at("owner_map") == changed.at("owner_map"));
    CHECK(rejected.at("datums") == changed.at("datums"));
    CHECK(rejected.at("volume").get<double>() == doctest::Approx(changed.at("volume").get<double>()));
    CHECK(rejected.at("tess").at("bodies") == changed.at("tess").at("bodies"));
    json rejected_without_tess = doc_apply_ops(id, changed.at("revision"), json::array({
        {{"op","edit_feature"},{"id",fid},{"params",{{"source","size=10;\ncube([);"}}}}
    }), false, false);
    CHECK(rejected_without_tess.contains("rejected_tree"));
    CHECK(rejected_without_tess.at("owner_map") == changed.at("owner_map"));
    CHECK(rejected_without_tess.at("datums") == changed.at("datums"));
    CHECK(rejected_without_tess.at("volume").get<double>() == doctest::Approx(changed.at("volume").get<double>()));
    const auto native = ws.dir / "saved.step+";
    doc_save(id, native.string());
    REQUIRE(doc_close(id));
    fs::remove(ws.dir / "dep.scad");
    json reopened = doc_open(native.string());
    INFO(reopened.dump());
    CHECK(reopened.at("cache_hit") == true);
    CHECK(reopened.at("warm") == true);
    id = reopened.at("doc_id").get<std::string>();
    CHECK(doc_tree(id).at("tree").at("features")[0].at("params").at("parameters").at("size") == 12);
    fs::create_directory(ws.dir / "export");
    auto out = ws.dir / "export" / "model.scad";
    json exported = doc_export_file(id, out.string(), "scad");
    CHECK(exported.at("format") == "scad");
    CHECK(exported.at("exported_files").size() == 2);
    CHECK(exported.at("mm_per_source_unit") == 1);
    CHECK(exported.at("effective_overrides").at("size") == 12);
    CHECK(fs::exists(ws.dir / "export" / "dep.scad"));
    json imported = scad::import_project(out.string());
    CHECK(imported.at("source") == feature.at("params").at("source"));
    CHECK(imported.at("parameters").at("size") == 12);
    CHECK(doc_close(id));
}

TEST_CASE("unsupported native projection does not mutate the document") {
    json created = doc_new("part", "Unsupported SCAD projection");
    const std::string id = created.at("doc_id");
    json added = doc_apply_ops(id, 0, json::array({{{"op","add_feature"},
        {"feature",{{"type","torus"},{"name","Ring"},
        {"params",{{"major_r",10},{"minor_r",2}}}}}}}), false);
    REQUIRE_FALSE(added.contains("rejected_tree"));
    const json before = doc_tree(id);
    CHECK_THROWS_AS(doc_apply_ops(id, added.at("revision"), json::array({{{"op","scad_convert"},
        {"direction","to_code"}}}), false), OpError);
    CHECK(doc_tree(id).at("tree") == before.at("tree"));
    CHECK(doc_tree(id).at("revision") == before.at("revision"));
    CHECK(doc_close(id));
}

TEST_CASE("managed conversion uses atomic history and preserves native graph") {
    if (!renderer_ready()) { MESSAGE("real isolated OpenSCAD unavailable; integration smoke requires it"); return; }
    json created = doc_new("part", "SCAD conversion");
    const std::string id = created.at("doc_id");
    json added = doc_apply_ops(id, 0, json::array({
        {{"op","eq_set"},{"name","L"},{"expr","10"}},
        {{"op","add_feature"},{"feature",{{"type","box"},{"name","Block"},
        {"params",{{"length",{{"value",10},{"expr","L"}}},{"width",8},{"height",4}}}}}}
    }), false);
    REQUIRE_FALSE(added.contains("rejected_tree"));
    const json native = added.at("tree").at("features");
    json converted = doc_apply_ops(id, added.at("revision"), json::array({{{"op","scad_convert"},
        {"direction","to_code"},{"mm_per_source_unit",25.4}}}), false);
    INFO(converted.dump());
    REQUIRE_FALSE(converted.contains("rejected_tree"));
    REQUIRE(converted.at("tree").at("features")[0].at("type") == "scad");
    Workspace ws;
    const auto projected_file = ws.dir / "managed.scad";
    doc_export_file(id, projected_file.string(), "scad");
    const json imported = doc_open(projected_file.string());
    REQUIRE_FALSE(imported.contains("rejected_tree"));
    const std::string imported_id = imported.at("doc_id");
    json imported_native = doc_apply_ops(imported_id, imported.at("revision"),
        json::array({{{"op","scad_convert"},{"direction","to_native"}}}), false);
    REQUIRE_FALSE(imported_native.contains("rejected_tree"));
    CHECK(imported_native.at("tree").at("features") == native);
    CHECK(doc_close(imported_id));
    const auto code_feature = converted.at("tree").at("features")[0];
    const std::string code_id = code_feature.at("id");
    const std::string modified_source = code_feature.at("params").at("source").get<std::string>() + "\n// changed\n";
    json edited = doc_apply_ops(id, converted.at("revision"), json::array({{{"op","edit_feature"},
        {"id",code_id},{"params",{{"source",modified_source}}}}}), false);
    REQUIRE_FALSE(edited.contains("rejected_tree"));
    CHECK_THROWS_AS(doc_apply_ops(id, edited.at("revision"), json::array({{{"op","scad_convert"},
        {"direction","to_native"}}}), false), OpError);
    CHECK(doc_tree(id).at("revision") == edited.at("revision"));
    converted = doc_undo(id);
    json restored = doc_apply_ops(id, converted.at("revision"), json::array({{{"op","scad_convert"},
        {"direction","to_native"}}}), false);
    REQUIRE_FALSE(restored.contains("rejected_tree"));
    CHECK(restored.at("tree").at("features") == native);
    json undo = doc_undo(id);
    CHECK(undo.at("tree").at("features")[0].at("type") == "scad");
    CHECK(doc_close(id));
}
