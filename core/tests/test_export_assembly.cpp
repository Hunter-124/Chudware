// Doc-geometry writer regression — every writer that turns a document into geometry.
//
// Bug 1: doc_export_step / doc_export_file / doc_export_estimate built their writer input
// from compile_document(s.doc). An assembly doc has NO features of its own — its geometry
// is the loaded components placed by the solved mates (compile_assembly) — so
// compile_document returned zero shapes and every export of an assembly threw
// "document has no bodies to export". The fix routes assembly docs through a
// gather_export_shapes() that world-places each component body.
//
// Bug 2: doc_save wrote thumbnail/meta/document + the cache.* entries and NO
// `geometry.step`, so the .step+ container's whole backward-compatibility promise
// (contracts/stepplus.md §1: "AP214 snapshot of the last successful rebuild, all bodies …
// omitted only if the document has never rebuilt successfully") was unmet — no foreign CAD
// could open a Chudware native file. doc_save now embeds it for parts (compiled bodies) and
// assemblies (the flattened world-placed component bodies).
//
// This test builds a two-component assembly from two STEP files and verifies the
// interchange (STEP) + native (doc_export_step) + mesh (STL) writers all emit the
// assembled geometry (both component solids), not an empty file — then that a saved
// .step+ carries a real, re-importable geometry.step for both a part and an assembly.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <cmath>
#include <filesystem>
#include <fstream>
#include <string>
#include <utility>
#include <vector>

#include <BRepGProp.hxx>
#include <BRepPrimAPI_MakeBox.hxx>
#include <GProp_GProps.hxx>
#include <TopoDS_Shape.hxx>
#include <gp_Pnt.hxx>
#include <miniz.h>

#include "doc/assembly.h"
#include "doc/formats.h"
#include "doc/session.h"
#include "doc/stepplus.h"

using nlohmann::json;
namespace fs = std::filesystem;

namespace {
fs::path tmp(const std::string& name) {
    return fs::temp_directory_path() / ("chudware_asmexp_" + name);
}

// Build a two-component assembly: box A at the origin, box B offset along +X so the two
// solids stay disjoint (no mates -> floating components rest at their source frame).
std::string make_two_part_assembly(const fs::path& a_step, const fs::path& b_step) {
    chudware::doc::export_bodies({BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 10.0, 10.0, 10.0).Shape()},
                              a_step.string(), "step");
    chudware::doc::export_bodies({BRepPrimAPI_MakeBox(gp_Pnt(50, 0, 0), 10.0, 10.0, 10.0).Shape()},
                              b_step.string(), "step");
    json nresp = chudware::doc::doc_new("assembly", "asm_export_test");
    const std::string did = nresp.at("doc_id").get<std::string>();
    json ops = json::array({
        {{"op", "asm_insert_component"}, {"source", {{"path", a_step.string()}}}},
        {{"op", "asm_insert_component"}, {"source", {{"path", b_step.string()}}}},
    });
    chudware::doc::doc_apply_ops(did, -1, ops, false);
    return did;
}

// The container's entries, in archive ORDER, as (name, stored?, bytes) — read straight out
// of the ZIP rather than through our own reader, because what a foreign tool sees is the
// file, not chudware::doc's notion of which entries it owns.
struct ZipEntry { std::string name; bool stored = false; std::string data; };
std::vector<ZipEntry> container_entries(const fs::path& path) {
    mz_zip_archive zip{};
    REQUIRE(mz_zip_reader_init_file(&zip, path.string().c_str(), 0));
    std::vector<ZipEntry> out;
    for (mz_uint i = 0; i < mz_zip_reader_get_num_files(&zip); ++i) {
        mz_zip_archive_file_stat st;
        REQUIRE(mz_zip_reader_file_stat(&zip, i, &st));
        ZipEntry e;
        e.name = st.m_filename;
        e.stored = (st.m_method == 0);
        e.data.resize((std::size_t)st.m_uncomp_size);
        if (!e.data.empty())
            REQUIRE(mz_zip_reader_extract_to_mem(&zip, i, e.data.data(), e.data.size(), 0));
        out.push_back(std::move(e));
    }
    mz_zip_reader_end(&zip);
    return out;
}

// The geometry.step member's bytes, or "" when the container carries none.
std::string geometry_step_of(const fs::path& container) {
    for (const ZipEntry& e : container_entries(container))
        if (e.name == "geometry.step") return e.data;
    return std::string();
}

// The geometry.step bytes carried by a saved container, re-imported through the same lane a
// foreign reader uses. Returns the solids OCCT recovered from the entry.
std::vector<TopoDS_Shape> reimport_geometry_step(const std::string& step_bytes,
                                                 const fs::path& scratch) {
    std::ofstream out(scratch, std::ios::binary | std::ios::trunc);
    out.write(step_bytes.data(), (std::streamsize)step_bytes.size());
    out.close();
    std::vector<TopoDS_Shape> solids;
    for (auto& named : chudware::doc::import_bodies(scratch.string(), "step"))
        if (!named.second.IsNull()) solids.push_back(named.second);
    return solids;
}

double total_volume(const std::vector<TopoDS_Shape>& solids) {
    double v = 0.0;
    for (const TopoDS_Shape& s : solids) {
        GProp_GProps props;
        BRepGProp::VolumeProperties(s, props);
        v += std::abs(props.Mass());
    }
    return v;
}

void save_box_part(const fs::path& path) {
    using namespace chudware::doc;
    const std::string id = doc_new("part", "nested_source").at("doc_id").get<std::string>();
    doc_apply_ops(id, -1, json::array({
        {{"op", "add_feature"}, {"feature", {
            {"type", "box"}, {"name", "survivor"},
            {"params", {{"length", 10.0}, {"width", 10.0}, {"height", 10.0}}}
        }}}
    }), false);
    doc_save(id, path.string());
}

std::pair<std::string, json> insert_native_source(const fs::path& path) {
    using namespace chudware::doc;
    const std::string id = doc_new("assembly", "nested_parent").at("doc_id").get<std::string>();
    json response = doc_apply_ops(id, -1, json::array({
        {{"op", "asm_insert_component"}, {"source", {{"path", path.string()}}}, {"fixed", true}}
    }), false);
    return {id, std::move(response)};
}
}  // namespace

// --- STEP interchange export of an assembly recovers BOTH component solids. -------------
TEST_CASE("assembly exports to STEP with all component solids (was: empty)") {
    fs::path a = tmp("a.step"), b = tmp("b.step"), out = tmp("asm_out.step");
    const std::string did = make_two_part_assembly(a, b);

    json r = chudware::doc::doc_export_file(did, out.string(), "step", 0.0, 0.0, false, false);
    CHECK(fs::exists(out));
    CHECK(r.value("validated", false));                 // round-trip re-read passed
    CHECK(r.value("expected_solids", 0) == 2);          // both components handed to the writer
    CHECK(r.value("solid_count", 0) >= 2);              // and both re-read out of the file
    CHECK(r.value("bytes", 0.0) > 0.0);

    fs::remove(a); fs::remove(b); fs::remove(out);
}

// --- doc_export_step (native saver / mesher feed / handoff) also sees the assembly. -----
TEST_CASE("assembly exports via doc_export_step (was: no bodies to export)") {
    fs::path a = tmp("c.step"), b = tmp("d.step"), out = tmp("asm_step.step");
    const std::string did = make_two_part_assembly(a, b);

    json r = chudware::doc::doc_export_step(did, out.string());
    CHECK(fs::exists(out));
    CHECK(fs::file_size(out) > 0);
    CHECK(r.contains("path"));

    fs::remove(a); fs::remove(b); fs::remove(out);
}

// --- Mesh-lane (STL) export + the dialog tessellation estimate are assembly-aware too. --
TEST_CASE("assembly exports to STL and estimates a non-empty mesh") {
    fs::path a = tmp("e.step"), b = tmp("f.step"), out = tmp("asm_out.stl");
    const std::string did = make_two_part_assembly(a, b);

    json r = chudware::doc::doc_export_file(did, out.string(), "stl", 0.0, 0.0, false, false);
    CHECK(fs::exists(out));
    CHECK(r.value("bytes", 0.0) > 0.0);

    json est = chudware::doc::doc_export_estimate(did, 0.05, 0.35, false);
    CHECK(est.value("bodies", 0) == 2);                 // both components tessellated
    CHECK(est.value("triangles", 0) > 0);

    fs::remove(a); fs::remove(b); fs::remove(out);
}

// --- contracts/stepplus.md §1: a saved PART container carries a real geometry.step. -----
// The container promises "any other CAD opens the embedded geometry.step". So the assertion
// is not that the member exists — it is that its bytes are an AP214 STEP that re-imports to
// the same body count and the same volume as the document's own rebuild.
TEST_CASE("doc_save embeds an AP214 geometry.step for a part (was: member absent)") {
    const fs::path out = tmp("part_geom.step+");
    const fs::path scratch = tmp("part_geom_extract.step");
    fs::remove(out);

    // 40 x 40 x 20 block with a Ø10 through-bore: a real feature tree ending in a boolean,
    // which is the case a naive compound AsIs transfer writes as one invalid solid.
    auto dimv = [](double v) { return json{{"value", v}, {"expr", nullptr}}; };
    json nresp = chudware::doc::doc_new("part", "geom_step_part");
    const std::string did = nresp.at("doc_id").get<std::string>();
    json ops = json::array({
        json{{"op", "add_feature"}, {"feature", {{"type", "box"}, {"name", "Blk"},
             {"params", {{"length", dimv(40)}, {"width", dimv(40)}, {"height", dimv(20)}}}}}},
        json{{"op", "add_feature"}, {"feature", {{"type", "hole"}, {"name", "Bore"},
             {"params", {{"style", "simple"}, {"standard", "custom"}, {"diameter", dimv(10)},
                         {"end", "through_all"}}},
             {"refs", {{"face", json{{"of", "face"}, {"side", "top"}}}}}}}},
    });
    const double doc_volume = chudware::doc::doc_apply_ops(did, -1, ops, false)
                                  .value("volume", 0.0);
    REQUIRE(doc_volume > 0.0);

    chudware::doc::doc_save(did, out.string());
    REQUIRE(fs::exists(out));

    // The entry-ordering rule (contracts/stepplus.md §1): thumbnail FIRST and STORED so a
    // shell previewer reads it cheaply. A new member must not displace it.
    const std::vector<ZipEntry> entries = container_entries(out);
    REQUIRE(!entries.empty());
    CHECK(entries.front().name == "thumbnail.png");
    CHECK(entries.front().stored);

    const std::string bytes = geometry_step_of(out);
    REQUIRE_FALSE(bytes.empty());
    CHECK(bytes.rfind("ISO-10303-21;", 0) == 0);                       // a STEP file
    CHECK(bytes.find("AUTOMOTIVE_DESIGN") != std::string::npos);       // AP214, per contract

    const std::vector<TopoDS_Shape> solids = reimport_geometry_step(bytes, scratch);
    CHECK(solids.size() == 1);
    CHECK(total_volume(solids) == doctest::Approx(doc_volume).epsilon(0.001));

    fs::remove(out); fs::remove(scratch);
}

// --- ... and an ASSEMBLY container carries the FLATTENED placed bodies. -----------------
// An assembly doc has no features of its own, so this can only work off the solved
// component poses — the same gather_export_shapes path every other writer uses.
TEST_CASE("doc_save embeds geometry.step for an assembly as flattened placed bodies") {
    fs::path a = tmp("g.step"), b = tmp("h.step");
    const fs::path out = tmp("asm_geom.step+");
    const fs::path scratch = tmp("asm_geom_extract.step");
    fs::remove(out);
    const std::string did = make_two_part_assembly(a, b);

    chudware::doc::doc_save(did, out.string());
    REQUIRE(fs::exists(out));

    const std::string bytes = geometry_step_of(out);
    REQUIRE_FALSE(bytes.empty());
    const std::vector<TopoDS_Shape> solids = reimport_geometry_step(bytes, scratch);
    CHECK(solids.size() == 2);                                  // both components, flattened
    CHECK(total_volume(solids) == doctest::Approx(2000.0).epsilon(0.001));  // 2 x 10mm cube

    fs::remove(a); fs::remove(b); fs::remove(out); fs::remove(scratch);
}

// --- A document that has NOT rebuilt successfully must omit the member, not ship stale ---
// bytes: the one omission contracts/stepplus.md §1 allows. An empty part (no features) has
// never rebuilt to any geometry, so its container carries no geometry.step at all.
TEST_CASE("doc_save omits geometry.step when the document has no successful rebuild") {
    const fs::path out = tmp("empty_geom.step+");
    fs::remove(out);
    json nresp = chudware::doc::doc_new("part", "geom_step_empty");
    const std::string did = nresp.at("doc_id").get<std::string>();

    chudware::doc::doc_save(did, out.string());
    REQUIRE(fs::exists(out));
    CHECK(geometry_step_of(out).empty());

    fs::remove(out);
}

TEST_CASE("partial native child reports its failed feature without dropping surviving geometry") {
    using namespace chudware::doc;
    const fs::path part = tmp("partial_child.step+");
    const fs::path nested = tmp("partial_nested.step+");
    save_box_part(part);
    json child = stepplus_open(part.string()).doc;
    child["features"].push_back({{"id", "broken_child_feature"}, {"type", "unsupported_feature"},
                                 {"params", json::object()}, {"refs", json::object()}});
    // No solved cache: rebuild the actual authored feature tree, not its old good snapshot.
    stepplus_save(child, part.string(), {});

    const auto [id, response] = insert_native_source(part);
    REQUIRE(response["rebuild"]["components"].size() == 1);
    CHECK(response["rebuild"]["components"][0]["ok"] == false);
    CHECK(response["rebuild"]["converged"] == false);
    const std::string error = response["rebuild"]["components"][0]["error"].get<std::string>();
    CHECK(error.find("broken_child_feature") != std::string::npos);
    const json report = doc_eval_model_integrity(id, json::object());
    CHECK(report["complete"] == false);
    REQUIRE(report["solids"].size() == 1);
    CHECK(report["assembly"]["components"][0]["status"] == "failed");
    CHECK_FALSE(report["source_failures"].empty());

    // The same partial geometry and diagnostic must survive another assembly boundary.
    doc_save(id, nested.string());
    const auto [outer_id, outer_response] = insert_native_source(nested);
    CHECK(outer_response["rebuild"]["components"][0]["ok"] == false);
    CHECK(outer_response["rebuild"]["components"][0]["error"].get<std::string>().find(
              "broken_child_feature") != std::string::npos);
    const json outer_report = doc_eval_model_integrity(outer_id, json::object());
    CHECK(outer_report["complete"] == false);
    CHECK(outer_report["solids"].size() == 1);
    fs::remove(part); fs::remove(nested);
}

TEST_CASE("nested unresolved mate preserves provisional bodies and prevents clean parent rebuild") {
    using namespace chudware::doc;
    const fs::path part = tmp("unsolved_child_part.step+");
    const fs::path nested = tmp("unsolved_child_assembly.step+");
    save_box_part(part);
    const auto [child_id, child_response] = insert_native_source(part);
    REQUIRE(child_response["rebuild"]["converged"] == true);
    doc_save(child_id, nested.string());
    json child = stepplus_open(nested.string()).doc;
    const std::string component = child["components"][0]["id"].get<std::string>();
    child["mates"] = json::array({{
        {"id", "unresolved_child_mate"}, {"type", "coincident"},
        {"refs", json::array({
            {{"component", component}, {"selector", {{"kind", "unsupported"}}}},
            {{"component", "missing_component"}, {"selector", {{"kind", "face"}}}}
        })}
    }});
    stepplus_save(child, nested.string(), {});

    const auto [id, response] = insert_native_source(nested);
    CHECK(response["rebuild"]["converged"] == false);
    REQUIRE(response["rebuild"]["components"].size() == 1);
    CHECK(response["rebuild"]["components"][0]["ok"] == false);
    CHECK(response["rebuild"]["components"][0]["error"].get<std::string>().find(
              "unresolved_child_mate") != std::string::npos);
    const json report = doc_eval_model_integrity(id, json::object());
    CHECK(report["complete"] == false);
    CHECK(report["solids"].size() == 1);
    CHECK_FALSE(report["source_failures"].empty());
    fs::remove(part); fs::remove(nested);
}

TEST_CASE("fully rebuilt nested native source remains complete and exports unchanged geometry") {
    using namespace chudware::doc;
    const fs::path part = tmp("complete_child_part.step+");
    const fs::path nested = tmp("complete_child_assembly.step+");
    const fs::path output = tmp("complete_nested_export.step");
    save_box_part(part);
    const auto [child_id, child_response] = insert_native_source(part);
    REQUIRE(child_response["rebuild"]["converged"] == true);
    doc_save(child_id, nested.string());
    const auto [id, response] = insert_native_source(nested);
    CHECK(response["rebuild"]["converged"] == true);
    CHECK(response["rebuild"]["components"][0]["ok"] == true);
    const json report = doc_eval_model_integrity(id, json::object());
    CHECK(report["complete"] == true);
    CHECK(report["source_failures"].empty());
    REQUIRE(report["solids"].size() == 1);
    const json exported = doc_export_file(id, output.string(), "step", 0.0, 0.0, false, false);
    CHECK(exported["validated"] == true);
    CHECK(exported["expected_solids"] == 1);
    std::vector<TopoDS_Shape> solids;
    for (const auto& body : import_bodies(output.string(), "step")) solids.push_back(body.second);
    CHECK(total_volume(solids) == doctest::Approx(1000.0).epsilon(0.001));
    fs::remove(part); fs::remove(nested); fs::remove(output);
}

TEST_CASE("assembly pattern failure retains earlier instances and reports authored feature") {
    using namespace chudware::doc;
    const fs::path part = tmp("pattern_status_part.step+");
    save_box_part(part);
    json assembly{
        {"schema", "aicad.asm/1"},
        {"components", json::array({{{"id", "seed"}, {"fixed", true},
                                     {"source", {{"path", part.string()}}}}})},
        {"features", json::array({
            {{"id", "valid_pattern"}, {"type", "asm_linear_pattern"},
             {"refs", {{"components", json::array({"seed"})}, {"dir1", "x"}}},
             {"params", {{"count1", 2}, {"spacing1", 20.0}}}},
            {{"id", "broken_pattern"}, {"type", "asm_circular_pattern"},
             {"refs", {{"components", json::array({"seed"})}, {"axis", "z"}}},
             {"params", {{"count", 2}, {"equal_spacing", true}}}}
        })}
    };
    SUBCASE("exception in later pattern") {
        assembly["features"][1]["params"]["equal_spacing"] = "not_a_boolean";
    }
    SUBCASE("unresolved direction instead of exception") {
        assembly["features"][1]["refs"]["axis"] = "missing_axis";
    }
    SUBCASE("missing seed alongside surviving seed") {
        assembly["features"][1]["refs"]["components"] = json::array({"seed", "missing_seed"});
    }
    const AssemblyResult result = compile_assembly(assembly);
    CHECK_FALSE(result.converged);
    REQUIRE(result.feature_status.size() == 2);
    CHECK(result.feature_status[0].ok);
    CHECK_FALSE(result.feature_status[1].ok);
    CHECK(result.feature_status[1].id == "broken_pattern");
    CHECK_FALSE(result.feature_status[1].error.empty());
    REQUIRE(result.bodies.size() >= 2);
    CHECK(result.bodies[0].component_id == "seed");
    CHECK(result.bodies[0].body_id == 1);
    CHECK(result.bodies[1].component_id == "seed#1");
    CHECK(total_volume({placed_world_shape(result.bodies[0]),
                        placed_world_shape(result.bodies[1])}) == doctest::Approx(2000.0));
    fs::remove(part);
}

TEST_CASE("component source replacement changes saved and exported geometry without moving it") {
    using namespace chudware::doc;
    const fs::path a = tmp("source_edit_a.step"), b = tmp("source_edit_b.step");
    const fs::path replacement = tmp("source_edit_replacement.step");
    const fs::path saved = tmp("source_edit.step+"), output = tmp("source_edit_out.step");
    const std::string id = make_two_part_assembly(a, b);
    const json pose = json::array({1, 0, 0, 25, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1});
    doc_apply_ops(id, -1, json::array({
        {{"op", "asm_edit_component"}, {"id", "k2"}, {"fixed", true}, {"transform", pose}}
    }), false);
    REQUIRE(doc_bbox(id)["max"][0].get<double>() == doctest::Approx(85.0));
    export_bodies({
        BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 20.0, 10.0, 10.0).Shape(),
        BRepPrimAPI_MakeBox(gp_Pnt(30, 0, 0), 10.0, 10.0, 10.0).Shape()
    }, replacement.string(), "step");
    doc_apply_ops(id, -1, json::array({
        {{"op", "asm_edit_component"}, {"id", "k2"},
         {"source", {{"path", replacement.string()}}}}
    }), false);
    CHECK(doc_bbox(id)["max"][0].get<double>() == doctest::Approx(65.0));
    CHECK_THROWS(doc_apply_ops(id, -1, json::array({
        {{"op", "asm_edit_component"}, {"id", "k2"}, {"source", 42}}
    }), false));
    CHECK(doc_bbox(id)["max"][0].get<double>() == doctest::Approx(65.0));
    doc_save(id, saved.string());
    doc_close(id);
    const std::string reopened = doc_open(saved.string()).at("doc_id").get<std::string>();
    CHECK(doc_bbox(reopened)["max"][0].get<double>() == doctest::Approx(65.0));
    const json result = doc_export_file(reopened, output.string(), "step", 0.0, 0.0, false, false);
    REQUIRE(result["validated"] == true);
    CHECK(result["expected_solids"] == 3);
    std::vector<TopoDS_Shape> solids;
    for (const auto& named : import_bodies(output.string(), "step"))
        solids.push_back(named.second);
    CHECK(total_volume(solids) == doctest::Approx(4000.0));
    doc_close(reopened);
    for (const fs::path& path : {a, b, replacement, saved, output})
        fs::remove(path);
}

TEST_CASE("nonbinding snapshot preserves live relative sources and unsaved state") {
    using namespace chudware::doc;
    const fs::path directory = tmp("nonbinding_snapshot");
    fs::create_directories(directory / "source");
    fs::create_directories(directory / "recovery");
    const fs::path a = directory / "source/a.step";
    const fs::path b = directory / "source/b.step";
    const fs::path saved = directory / "source/assembly.step+";
    const fs::path snapshot = directory / "recovery/snapshot.step+";
    const std::string id = make_two_part_assembly(a, b);
    doc_save(id, saved.string());
    doc_apply_ops(id, -1, json::array({
        {{"op", "asm_edit_component"}, {"id", "k1"}, {"source", {{"path", "a.step"}}}},
        {{"op", "asm_edit_component"}, {"id", "k2"}, {"source", {{"path", "b.step"}}}}
    }), false);
    const auto live_state = [&]() {
        const json listed = doc_list();
        for (const json& row : listed.at("docs"))
            if (row.at("doc_id") == id) return row;
        return json();
    };
    const json before = live_state();
    const json tree = doc_tree(id).at("tree");
    REQUIRE(before.at("dirty") == true);
    doc_save(id, snapshot.string(), true);
    const json after = live_state();
    CHECK(after.at("path") == before.at("path"));
    CHECK(after.at("dirty") == true);
    CHECK(after.at("revision") == before.at("revision"));
    CHECK(doc_tree(id).at("tree") == tree);
    const std::string recovered = doc_open(snapshot.string()).at("doc_id");
    CHECK(doc_bbox(recovered).at("max").at(0).get<double>() == doctest::Approx(60.0));
    const auto shapes = doc_tessellate_bodies(recovered);
    REQUIRE(shapes.size() == 2);
    doc_close(recovered);
    // A failed snapshot must not clear pending edits or change their base path.
    const fs::path invalid = directory / "recovery/directory.step+";
    fs::create_directories(invalid);
    CHECK_THROWS(doc_save(id, invalid.string(), true));
    CHECK(live_state().at("path") == before.at("path"));
    CHECK(live_state().at("dirty") == true);
    CHECK(doc_tree(id).at("tree") == tree);
    // Ordinary Save still binds and clears dirty, with relative children intact.
    doc_save(id, saved.string());
    CHECK(live_state().at("dirty") == false);
    doc_close(id);
    const std::string reopened = doc_open(saved.string()).at("doc_id");
    CHECK(doc_tessellate_bodies(reopened).size() == 2);
    CHECK(doc_bbox(reopened).at("max").at(0).get<double>() == doctest::Approx(60.0));
    doc_close(reopened);
    fs::remove_all(directory);
}

TEST_CASE("nested load cache follows changed descendant geometry and appearance") {
    using namespace chudware::doc;
    const fs::path part = tmp("cache_descendant_part.step+");
    const fs::path nested = tmp("cache_descendant_assembly.step+");
    save_box_part(part);
    auto source = stepplus_open(part.string()).doc;
    source["appearances"]["b1"] = {0.8, 0.1, 0.2};
    stepplus_save(source, part.string(), {});
    json child{{"schema", "aicad.asm/1"}, {"components", json::array({
        {{"id", "leaf"}, {"fixed", true}, {"source", {{"path", part.string()}}}}
    })}};
    stepplus_save(child, nested.string(), {});
    const auto parent_time = fs::last_write_time(nested);
    const auto part_time = fs::last_write_time(part);
    json root{{"schema", "aicad.asm/1"}, {"components", json::array({
        {{"id", "left"}, {"fixed", true}, {"source", {{"path", nested.string()}}}},
        {{"id", "right"}, {"fixed", true}, {"source", {{"path", nested.string()}}},
         {"transform", {1,0,0,100, 0,1,0,0, 0,0,1,0, 0,0,0,1}}}
    })}};
    const auto fingerprints = asm_component_fingerprints(root, {});
    for (int warm = 0; warm < 2; ++warm) {
        const auto built = compile_assembly(root);
        REQUIRE(built.converged);
        REQUIRE(built.bodies.size() == 2);
        CHECK(total_volume({placed_world_shape(built.bodies[0]),
                            placed_world_shape(built.bodies[1])}) == doctest::Approx(2000));
        CHECK(built.bodies[1].transform[3] == doctest::Approx(100));
        REQUIRE_FALSE(built.bodies[0].appearance_regions.empty());
        CHECK(built.bodies[0].appearance_regions[0].color[0] == doctest::Approx(0.8));
    }
    source["features"][0]["params"]["length"] = 20.0;
    source["appearances"]["b1"] = {0.2, 0.1, 0.8};
    stepplus_save(source, part.string(), {});
    // Keep the containing assembly unchanged, and use a one-tick child timestamp
    // delta to catch the old lossy double-converted mtime identity.
    fs::last_write_time(part, part_time + fs::file_time_type::duration(1));
    CHECK(fs::last_write_time(nested) == parent_time);
    CHECK_FALSE(asm_fingerprints_match(root, {}, fingerprints));
    const auto changed = compile_assembly(root);
    REQUIRE(changed.converged);
    REQUIRE(changed.bodies.size() == 2);
    CHECK(total_volume({placed_world_shape(changed.bodies[0]),
                        placed_world_shape(changed.bodies[1])}) == doctest::Approx(4000));
    CHECK(changed.bodies[1].transform[3] == doctest::Approx(100));
    REQUIRE_FALSE(changed.bodies[0].appearance_regions.empty());
    CHECK(changed.bodies[0].appearance_regions[0].color[0] == doctest::Approx(0.2));
    fs::remove(part); fs::remove(nested);
}

TEST_CASE("parallel cyclic native roots report failure and recover after descendant repair") {
    using namespace chudware::doc;
    const fs::path part = tmp("cache_cycle_part.step+");
    const fs::path a = tmp("cache_cycle_a.step+"), b = tmp("cache_cycle_b.step+");
    save_box_part(part);
    const auto component = [](const char* id, const fs::path& path) {
        return json{{"id", id}, {"fixed", true}, {"source", {{"path", path.string()}}}};
    };
    const json left{{"schema", "aicad.asm/1"}, {"components", json::array({
        component("leaf", part), component("other", b)
    })}};
    json right{{"schema", "aicad.asm/1"}, {"components", json::array({
        component("leaf", part), component("other", a)
    })}};
    stepplus_save(left, a.string(), {});
    stepplus_save(right, b.string(), {});
    const json root{{"schema", "aicad.asm/1"}, {"components", json::array({
        component("a", a), component("b", b)
    })}};
    for (int warm = 0; warm < 2; ++warm) {
        const auto broken = compile_assembly(root);
        CHECK_FALSE(broken.converged);
        REQUIRE(broken.component_status.size() == 2);
        for (const auto& status : broken.component_status) {
            CHECK_FALSE(status.ok);
            REQUIRE(status.error.has_value());
            CHECK(status.error->find("cyclic component reference") != std::string::npos);
        }
    }
    right["components"].erase(1);
    stepplus_save(right, b.string(), {});
    const auto repaired = compile_assembly(root);
    REQUIRE(repaired.converged);
    REQUIRE(repaired.bodies.size() == 2);
    CHECK(total_volume({placed_world_shape(repaired.bodies[0]),
                        placed_world_shape(repaired.bodies[1])}) == doctest::Approx(3000));
    fs::remove(part); fs::remove(a); fs::remove(b);
}

TEST_CASE("nested legacy import invalidates cached geometry when only external source changes") {
    using namespace chudware::doc;
    const fs::path neutral = tmp("cache_legacy_source.brep");
    const fs::path part = tmp("cache_legacy_part.step+");
    const fs::path nested = tmp("cache_legacy_assembly.step+");
    const auto box = BRepPrimAPI_MakeBox(10.0, 10.0, 10.0).Shape();
    export_bodies({box}, neutral.string(), "brep");
    json source{{"schema", "aicad.part/1"}, {"features", json::array({
        {{"id", "imported"}, {"type", "import"},
         {"params", {{"source_path", neutral.string()}, {"format", "brep"}}}}
    })}};
    SUBCASE("unconfigured file-backed import") {}
    SUBCASE("active configuration overrides embedded provenance with a file source") {
        source["features"][0]["params"]["embedded_brep"] = "unused embedded provenance";
        source["features"][0]["params"]["source_path"] = "unused original path";
        source["configurations"] = json::array({{
            {"name", "FileSource"}, {"param_overrides", {
                {"imported.embedded_brep", ""}, {"imported.source_path", neutral.string()}
            }}
        }});
        source["active_configuration"] = "FileSource";
    }
    const json header{{"engine", stepplus_cache_engine_key()},
                      {"doc_hash", stepplus_doc_hash(source)}, {"solved", true}};
    stepplus_save(source, part.string(), {{"cache.json", header.dump()},
                                          {"cache.brep", brep_cache_encode({box})}});
    const json child{{"schema", "aicad.asm/1"}, {"components", json::array({
        {{"id", "leaf"}, {"fixed", true}, {"source", {{"path", part.string()}}}}
    })}};
    stepplus_save(child, nested.string(), {});
    const json root{{"schema", "aicad.asm/1"}, {"components", json::array({
        {{"id", "nested"}, {"fixed", true}, {"source", {{"path", nested.string()}}}}
    })}};
    const auto before = compile_assembly(root);
    REQUIRE(before.converged);
    REQUIRE(before.bodies.size() == 1);
    CHECK(total_volume({placed_world_shape(before.bodies[0])}) == doctest::Approx(1000));
    const auto fingerprints = asm_component_fingerprints(root, {});
    const auto source_time = fs::last_write_time(neutral);
    export_bodies({BRepPrimAPI_MakeBox(20.0, 10.0, 10.0).Shape()}, neutral.string(), "brep");
    fs::last_write_time(neutral, source_time + fs::file_time_type::duration(1));
    CHECK_FALSE(asm_fingerprints_match(root, {}, fingerprints));
    const auto after = compile_assembly(root);
    REQUIRE(after.converged);
    REQUIRE(after.bodies.size() == 1);
    CHECK(total_volume({placed_world_shape(after.bodies[0])}) == doctest::Approx(2000));
    fs::remove(neutral); fs::remove(part); fs::remove(nested);
}

TEST_CASE("symlinked native assemblies fingerprint each relative source directory") {
    using namespace chudware::doc;
    const fs::path directory = tmp("cache_symlink_context");
    fs::create_directories(directory / "left");
    fs::create_directories(directory / "right");
    struct RemoveDirectory {
        fs::path path;
        ~RemoveDirectory() { std::error_code ec; fs::remove_all(path, ec); }
    } cleanup{directory};
    const fs::path shared = directory / "shared.step+";
    const fs::path container = directory / "container.step+";
    const json shared_doc{{"schema", "aicad.asm/1"}, {"components", json::array({
        {{"id", "part"}, {"fixed", true}, {"source", {{"path", "part.step+"}}}}
    })}};
    stepplus_save(shared_doc, shared.string(), {});
    for (const auto* side : {"left", "right"}) {
        std::error_code ec;
        fs::create_symlink(shared, directory / side / "sub.step+", ec);
        if (ec) return;  // symlink creation may require privileges on Windows
        save_box_part(directory / side / "part.step+");
    }
    const json child{{"schema", "aicad.asm/1"}, {"components", json::array({
        {{"id", "left"}, {"fixed", true}, {"source", {{"path", "left/sub.step+"}}}},
        {{"id", "right"}, {"fixed", true}, {"source", {{"path", "right/sub.step+"}}}}
    })}};
    stepplus_save(child, container.string(), {});
    const json root{{"schema", "aicad.asm/1"}, {"components", json::array({
        {{"id", "nested"}, {"fixed", true}, {"source", {{"path", container.string()}}}}
    })}};
    const auto before = compile_assembly(root);
    REQUIRE(before.converged);
    REQUIRE(before.bodies.size() == 1);
    CHECK(total_volume({placed_world_shape(before.bodies[0])}) == doctest::Approx(2000));
    const auto fingerprints = asm_component_fingerprints(root, {});
    const auto right = directory / "right/part.step+";
    auto updated = stepplus_open(right.string()).doc;
    updated["features"][0]["params"]["length"] = 20.0;
    stepplus_save(updated, right.string(), {});
    CHECK_FALSE(asm_fingerprints_match(root, {}, fingerprints));
    const auto after = compile_assembly(root);
    REQUIRE(after.converged);
    REQUIRE(after.bodies.size() == 1);
    CHECK(total_volume({placed_world_shape(after.bodies[0])}) == doctest::Approx(3000));
}

TEST_CASE("component geometry mode preserves curved display across compile cache mode switches") {
    using namespace chudware::doc;
    const json source{{"schema", "aicad.part/1"}, {"features", json::array({
        {{"id", "cyl"}, {"type", "cylinder"},
         {"params", {{"radius", 5.0}, {"height", 30.0}}}}
    })}};
    const auto cache = make_compile_cache();
    const auto displayed = compile_document(source, json::object(), cache.get());
    REQUIRE(displayed.ok);
    REQUIRE(displayed.shapes.size() == 1);
    const auto component = compile_document(source, json::object(), cache.get(),
                                             ::geom::DisplayQuality::Fine, false);
    REQUIRE(component.ok);
    REQUIRE(component.shapes.size() == 1);
    const auto deflections = ::geom::display_deflections(
        ::geom::DisplayQuality::Fine, component.shapes[0]);
    const float color[3] = {0.71f, 0.76f, 0.82f};
    const auto extracted = ::geom::tessellate_body(
        component.shapes[0], 1, "cyl", color, deflections.lin, deflections.ang);
    // A component consumer extracts exactly the standalone part's curved display,
    // even though the component compile did not allocate a serialized part packet.
    CHECK(aicm::write_aicm({extracted}) == displayed.aicm);
    const auto displayed_again = compile_document(source, json::object(), cache.get());
    REQUIRE(displayed_again.ok);
    CHECK(displayed_again.aicm == displayed.aicm);
}
