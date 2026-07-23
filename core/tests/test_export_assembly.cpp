// Assembly export regression — locks the fix for "file exports do nothing for assemblies".
//
// Bug: doc_export_step / doc_export_file / doc_export_estimate built their writer input
// from compile_document(s.doc). An assembly doc has NO features of its own — its geometry
// is the loaded components placed by the solved mates (compile_assembly) — so
// compile_document returned zero shapes and every export of an assembly threw
// "document has no bodies to export". The fix routes assembly docs through a
// gather_export_shapes() that world-places each component body.
//
// This test builds a two-component assembly from two STEP files and verifies the
// interchange (STEP) + native (doc_export_step) + mesh (STL) writers all emit the
// assembled geometry (both component solids), not an empty file.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <filesystem>
#include <string>
#include <vector>

#include <BRepPrimAPI_MakeBox.hxx>
#include <TopoDS_Shape.hxx>
#include <gp_Pnt.hxx>

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
