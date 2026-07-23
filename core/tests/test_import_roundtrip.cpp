// Import / save round-trip suite — locks the round-3 file-plumbing fixes:
//   N-8: a saved .step+ BAKES imported geometry in, so it survives the source file being
//        deleted (the "assembly reopened as just the plate" P0).
//   N-5: a multi-solid STEP imports as SEPARATE bodies, not one merged compound.
//   N-4: doc_import_file accepts the native .step+ extension (reopens it as a doc).
//   manual import: doc_open on a NON-.step+ file imports it as a fresh part doc (the
//        "nothing happens when I open a .step" bug).
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <filesystem>
#include <string>
#include <vector>

#include <BRepGProp.hxx>
#include <BRepPrimAPI_MakeBox.hxx>
#include <GProp_GProps.hxx>
#include <TopoDS_Shape.hxx>
#include <gp_Pnt.hxx>

#include "doc/formats.h"
#include "doc/session.h"

using nlohmann::json;
namespace fs = std::filesystem;

namespace {
double vol(const TopoDS_Shape& s) {
    GProp_GProps g;
    BRepGProp::VolumeProperties(s, g);
    return g.Mass();
}
int body_count(const json& resp) {
    if (resp.contains("tess") && resp["tess"].contains("bodies") &&
        resp["tess"]["bodies"].is_array())
        return static_cast<int>(resp["tess"]["bodies"].size());
    return -1;
}
fs::path tmp(const std::string& name) {
    return fs::temp_directory_path() / ("chudware_imp_" + name);
}
}  // namespace

// --- N-8 mechanism: the BREP base64 round-trip is lossless (volume preserved). --------
TEST_CASE("shape <-> brep base64 round-trips exactly") {
    TopoDS_Shape box = BRepPrimAPI_MakeBox(10.0, 20.0, 30.0).Shape();
    std::string b64 = chudware::doc::shape_to_brep_b64(box);
    REQUIRE_FALSE(b64.empty());
    TopoDS_Shape back = chudware::doc::brep_b64_to_shape(b64);
    REQUIRE_FALSE(back.IsNull());
    CHECK(vol(back) == doctest::Approx(vol(box)).epsilon(1e-9));
}

// --- N-5: a two-solid STEP imports as TWO separate bodies. -----------------------------
TEST_CASE("multi-solid STEP imports as separate bodies") {
    using chudware::doc::doc_import_file;
    fs::path step = tmp("two_solids.step");
    std::vector<TopoDS_Shape> bodies = {
        BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 10.0, 10.0, 10.0).Shape(),
        BRepPrimAPI_MakeBox(gp_Pnt(50, 0, 0), 10.0, 10.0, 10.0).Shape()};
    chudware::doc::export_bodies(bodies, step.string(), "step");

    json r = doc_import_file(step.string(), "", false, "");
    CHECK(body_count(r) == 2);  // the bug merged them into one compound body
    fs::remove(step);
}

// --- N-8 + manual import + N-4 round-trip. ---------------------------------------------
TEST_CASE("saved .step+ survives source deletion; open/import route by extension") {
    using chudware::doc::doc_import_file;
    using chudware::doc::doc_open;
    using chudware::doc::doc_save;

    fs::path step = tmp("one_solid.step");
    std::vector<TopoDS_Shape> one = {BRepPrimAPI_MakeBox(12.0, 12.0, 12.0).Shape()};
    chudware::doc::export_bodies(one, step.string(), "step");

    // Manual import: doc_open on a NON-.step+ file imports it as a fresh part doc.
    json opened = doc_open(step.string(), false);
    CHECK(body_count(opened) == 1);

    // Import + bake + save as native .step+.
    json imp = doc_import_file(step.string(), "", false, "");
    const std::string did = imp.at("doc_id").get<std::string>();
    CHECK(body_count(imp) == 1);
    fs::path native = tmp("baked.step+");
    doc_save(did, native.string());

    // DELETE the source STEP — the saved .step+ must still rebuild the body from its
    // embedded geometry (N-8). Reopen and confirm the body is still there.
    fs::remove(step);
    json reopened = doc_open(native.string(), false);
    CHECK(body_count(reopened) == 1);

    // N-4: doc_import_file on a .step+ routes to doc_open (reopens the doc) rather than
    // failing the format sniffer.
    json reimported = doc_import_file(native.string(), "", false, "");
    CHECK(body_count(reimported) == 1);

    fs::remove(native);
}
