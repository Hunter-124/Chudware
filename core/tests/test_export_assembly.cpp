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

#include "doc/formats.h"
#include "doc/session.h"

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
