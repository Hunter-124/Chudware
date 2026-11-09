// B5 (WS-B core coverage) — ctest coverage for the three new evaluate RPCs added this
// wave: doc_eval_interference's B4 params (include_coincident / min_clearance_mm),
// doc_eval_feature_overlap (B2, intra-part tool-solid overlap), and doc_eval_compare (C1,
// shape-vs-shape reverse-engineering ground truth). Unlike test_document.cpp/test_mirror.cpp
// this file never touches the host AICM loader — every assertion is made directly on the
// JSON these RPCs return (doc_new -> doc_apply_ops -> doc_eval_*), so there is no OCCT
// mesh-parsing dependency and no render include path.
//
// Geometry is built entirely from primitive "box"/"cylinder" features (core/doc/document.cpp
// type=="box"||"cylinder"||... branch) placed with explicit params.at + params.centered:false
// (corner-at-`at` convention) so every expected volume/overlap is an exact closed-form
// rectangular/cylindrical calculation — no sketch/extrude machinery needed. `params.mode`
// ("boss"/"cut") and `params.merge` (false -> a standalone new body) drive the ADD/CUT
// ToolRec role that doc_eval_feature_overlap classifies pairs on (session.cpp ~L6712).
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <string>
#include <algorithm>
#include <cmath>
#include <filesystem>
#include <vector>
#include <set>
#include <map>

#include <nlohmann/json.hpp>

#include "doc/session.h"
#include "doc/formats.h"
#include "doc/validate.h"
#include <BRepAlgoAPI_Cut.hxx>
#include <BRepPrimAPI_MakeBox.hxx>
#include <BRepPrimAPI_MakeCylinder.hxx>
#include <BRepPrimAPI_MakeCone.hxx>
#include <BRepPrimAPI_MakeSphere.hxx>
#include <BRepBuilderAPI_MakeFace.hxx>
#include <BRepBuilderAPI_MakePolygon.hxx>
#include <BRepBuilderAPI_Transform.hxx>
#include <BRepBuilderAPI_GTransform.hxx>
#include <BRepPrimAPI_MakePrism.hxx>
#include <BRep_Builder.hxx>
#include <BRepTools.hxx>
#include <TopExp_Explorer.hxx>
#include <TopoDS_Shell.hxx>
#include <TopoDS_Solid.hxx>
#include <TopoDS_Compound.hxx>
#include <TopLoc_Location.hxx>
#include <gp_Ax1.hxx>
#include <gp_Ax2.hxx>
#include <gp_Dir.hxx>
#include <gp_GTrsf.hxx>
#include <gp_Trsf.hxx>
#include <gp_Vec.hxx>
#include <gp_Pln.hxx>
#include <gp_Pnt.hxx>

namespace chudware::doc {
nlohmann::json interference_report_with_injected_failure_for_testing(
    const TopoDS_Shape& a, const TopoDS_Shape& b, bool threshold, bool coincident,
    bool fail_common, bool fail_cut, bool fail_retries, double retry_seconds = 10.0);
nlohmann::json interference_report_with_unclipped_failure_for_testing(
    const TopoDS_Shape& a, const TopoDS_Shape& b, bool threshold);
}

using json = nlohmann::json;
using chudware::doc::doc_new;
using chudware::doc::doc_apply_ops;
using chudware::doc::doc_eval_interference;
using chudware::doc::doc_eval_feature_overlap;
using chudware::doc::doc_eval_compare;
using chudware::doc::doc_eval_model_integrity;

namespace {

// One primitive add_feature op: type box/cylinder, `at`+`centered:false` give the
// SolidWorks corner/base-center placement convention document.cpp's primitive branch
// implements (see doc comment above). `extra` merges in any additional params (mode,
// merge, radius, height, translate, copy, ...).
json prim(const char* type, const char* name, json extra) {
    json params = json::object();
    params["centered"] = false;
    for (auto it = extra.begin(); it != extra.end(); ++it) params[it.key()] = it.value();
    return json{{"op", "add_feature"},
               {"feature", json{{"type", type}, {"name", name}, {"params", params}}}};
}

json box(const char* name, double L, double W, double H, std::vector<double> at,
         json extra = json::object()) {
    json p = extra;
    p["length"] = L;
    p["width"] = W;
    p["height"] = H;
    p["at"] = at;
    return prim("box", name, p);
}

json cyl(const char* name, double radius, double height, std::vector<double> at,
        json extra = json::object()) {
    json p = extra;
    p["radius"] = radius;
    p["height"] = height;
    p["at"] = at;
    return prim("cylinder", name, p);
}

// Apply `ops` to a fresh part doc, REQUIRE every feature ok, return {doc_id, resp}.
struct Built {
    std::string id;
    json resp;
};
Built build(const char* name, const json& ops) {
    json nr = doc_new("part", name);
    Built b;
    b.id = nr.at("doc_id").get<std::string>();
    b.resp = doc_apply_ops(b.id, -1, ops, false);
    const json& fs = b.resp.at("rebuild").at("feature_status");
    for (const json& st : fs) {
        const std::string err = st.contains("error") && st["error"].is_string()
                                     ? st["error"].get<std::string>() : std::string();
        REQUIRE_MESSAGE(st.value("ok", false),
                        "feature '" << st.value("id", std::string()) << "' failed: " << err);
    }
    return b;
}

// The assigned "fN" id of the Kth (0-based) feature in a build's response.
std::string fid(const Built& b, std::size_t k) {
    return b.resp.at("rebuild").at("feature_status").at(k).value("id", std::string());
}

bool has_dead(const json& dead_features, const std::string& id) {
    for (const json& v : dead_features)
        if (v.is_string() && v.get<std::string>() == id) return true;
    return false;
}

struct IntegrityFixtureFile {
    std::filesystem::path path;
    ~IntegrityFixtureFile() { std::error_code ec; std::filesystem::remove(path, ec); }
};

json imported_shape_health(const TopoDS_Shape& shape) {
    const std::string id = doc_new("part", "PlacedHealthFixture").at("doc_id").get<std::string>();
    IntegrityFixtureFile fixture{
        std::filesystem::temp_directory_path() / ("placed_health_" + id + ".brep")};
    REQUIRE(BRepTools::Write(shape, fixture.path.string().c_str()));
    chudware::doc::doc_import_file(fixture.path.string(), id, false, "brep");
    const json health = chudware::doc::doc_eval_solid_health(id, 1);
    chudware::doc::doc_close(id);
    REQUIRE(health["bodies"].size() == 1);
    return health["bodies"][0];
}

TopoDS_Shape folded_health_prism() {
    // Unequal lobes keep the signed volume positive despite two crossing side faces.
    BRepBuilderAPI_MakePolygon polygon;
    polygon.Add(gp_Pnt(0, 0, 0));
    polygon.Add(gp_Pnt(40, 20, 0));
    polygon.Add(gp_Pnt(40, 0, 0));
    polygon.Add(gp_Pnt(0, 5, 0));
    polygon.Close();
    return BRepPrimAPI_MakePrism(
        BRepBuilderAPI_MakeFace(polygon.Wire()).Face(), gp_Vec(0, 0, 10)).Shape();
}

TopoDS_Compound interference_compound(const std::vector<TopoDS_Shape>& solids) {
    BRep_Builder builder;
    TopoDS_Compound compound;
    builder.MakeCompound(compound);
    for (const auto& solid : solids) builder.Add(compound, solid);
    return compound;
}

struct InterferenceAssemblyFixture {
    std::string id;
    IntegrityFixtureFile a, b;

    InterferenceAssemblyFixture(const TopoDS_Shape& left, const TopoDS_Shape& right,
                                const json& transform = nullptr)
        : id(doc_new("assembly", "CompoundInterference").at("doc_id").get<std::string>()),
          a{std::filesystem::temp_directory_path() / ("compound_a_" + id + ".brep")},
          b{std::filesystem::temp_directory_path() / ("compound_b_" + id + ".brep")} {
        REQUIRE(BRepTools::Write(left, a.path.string().c_str()));
        REQUIRE(BRepTools::Write(right, b.path.string().c_str()));
        json first = {{"op", "asm_insert_component"}, {"name", "Left"}, {"fixed", true},
                      {"source", {{"path", a.path.string()}}}};
        json second = {{"op", "asm_insert_component"}, {"name", "Right"}, {"fixed", true},
                       {"source", {{"path", b.path.string()}}}};
        if (!transform.is_null()) {
            first["transform"] = transform;
            second["transform"] = transform;
        }
        doc_apply_ops(id, -1, json::array({first, second}), false);
    }
    ~InterferenceAssemblyFixture() { chudware::doc::doc_close(id); }
};

}  // namespace

// (1) Two overlapping bodies via a copy:true transform (assembly interference, whole-body
// check). Box1 spans [0,20]x[0,20]x[0,20]; a copy translated +10 in x spans [10,30]x[0,20]
// x[0,20] and stays a SEPARATE body (copy:true never consumes the source). The two axis-
// aligned boxes overlap on x:[10,20], full y/z -> an EXACT 10*20*20 = 4000 mm^3 common
// volume, closed-form, so the assertion is tight (rel 1e-6) rather than a fuzzy bound.
TEST_CASE("eval_interference: transform-copied overlapping boxes report the exact overlap volume") {
    json ops = json::array({
        box("Base", 20, 20, 20, {0, 0, 0}),
        json{{"op", "add_feature"},
            {"feature", json{{"type", "transform"}, {"name", "Copy"},
                {"params", {{"copy", true}, {"translate", json::array({10.0, 0.0, 0.0})}}}}}},
    });
    Built b = build("XformOverlap", ops);

    json params = json::object();
    SUBCASE("exact default") {}
    SUBCASE("threshold still computes the real Common") { params["clearance_mode"] = "threshold"; }
    json ir = doc_eval_interference(b.id, params);
    CHECK(ir.at("complete") == true);
    CHECK(ir.at("pairs_checked") == 1);
    CHECK(ir.at("pairs_total") == 1);
    REQUIRE(ir.contains("interferences"));
    REQUIRE_MESSAGE(ir["interferences"].size() == 1,
                    "expected exactly one interfering pair, got: " << ir.dump());
    const json& hit = ir["interferences"][0];
    CHECK_FALSE(hit.contains("method"));
    CHECK_FALSE(hit.contains("approximate"));
    CHECK(hit["volume"].get<double>() ==
          doctest::Approx(4000.0).epsilon(1e-6));
    // Wave-2: explicit overlap volume + world bbox + coarse Common mesh.
    REQUIRE(hit.contains("overlap_volume_mm3"));
    CHECK(hit["overlap_volume_mm3"].get<double>() ==
          doctest::Approx(4000.0).epsilon(1e-2));
    REQUIRE(hit.contains("overlap_bbox"));
    REQUIRE(hit["overlap_bbox"].is_array());
    REQUIRE(hit["overlap_bbox"].size() == 2);
    REQUIRE(hit["overlap_bbox"][0].size() == 3);
    REQUIRE(hit["overlap_bbox"][1].size() == 3);
    // Overlap of [0,20]^3 and [10,30]x[0,20]x[0,20] is [10,20]x[0,20]x[0,20].
    CHECK(hit["overlap_bbox"][0][0].get<double>() == doctest::Approx(10.0).epsilon(1e-3));
    CHECK(hit["overlap_bbox"][1][0].get<double>() == doctest::Approx(20.0).epsilon(1e-3));
    REQUIRE(hit.contains("overlap_mesh"));
    REQUIRE(hit["overlap_mesh"].contains("positions"));
    REQUIRE(hit["overlap_mesh"].contains("indices"));
    CHECK_MESSAGE(!hit["overlap_mesh"]["positions"].empty(),
                  "overlap_mesh.positions should be non-empty for a solid common: "
                      << hit["overlap_mesh"].dump());
    CHECK_MESSAGE(!hit["overlap_mesh"]["indices"].empty(),
                  "overlap_mesh.indices should be non-empty for a solid common: "
                      << hit["overlap_mesh"].dump());
    CHECK(hit["overlap_mesh"]["positions"].size() % 3 == 0);
    CHECK(hit["overlap_mesh"]["indices"].size() % 3 == 0);
    CHECK(ir.value("ok", true) == false);  // an interference exists -> ok:false
}

TEST_CASE("eval_interference: compound intersections count distinct material once") {
    auto block = [](double x, double length) {
        return BRepPrimAPI_MakeBox(gp_Pnt(x, 0, 0), length, 10, 10).Shape();
    };
    const TopoDS_Compound a = interference_compound({
        block(0, 10), block(5, 10), block(30, 10)});
    const TopoDS_Compound b = interference_compound({
        block(2, 10), block(8, 6), block(35, 2), block(2, 10)});
    // Both components have overlapping solids, including a geometrically
    // identical but independently built duplicate. The distinct common is
    // [2,14] and [35,37], each with a 10 x 10 cross-section: 1400 mm^3.
    InterferenceAssemblyFixture fixture(a, b,
        json::array({1,0,0,100, 0,1,0,-20, 0,0,1,5, 0,0,0,1}));
    for (const char* mode : {"exact", "threshold"}) {
        const json report = doc_eval_interference(fixture.id, {
            {"clearance_mode", mode}, {"include_coincident", true}, {"min_clearance_mm", 20}});
        INFO(mode << ": " << report.dump(2));
        REQUIRE(report.at("complete") == true);
        CHECK(report.at("ok") == false);
        CHECK(report.at("pairs_total") == 1);
        CHECK(report.at("pairs_checked") == 1);
        CHECK(report.at("pair_failures").empty());
        REQUIRE(report.at("interferences").size() == 1);
        const json& hit = report.at("interferences").front();
        CHECK(hit.at("volume").get<double>() == doctest::Approx(1400.0).epsilon(1e-7));
        CHECK(hit.at("overlap_volume_mm3").get<double>() ==
              doctest::Approx(1400.0).epsilon(1e-7));
        REQUIRE(hit.at("overlap_bbox").size() == 2);
        const auto& bounds = hit.at("overlap_bbox");
        CHECK(bounds[0][0].get<double>() == doctest::Approx(102.0).epsilon(1e-6));
        CHECK(bounds[1][0].get<double>() == doctest::Approx(137.0).epsilon(1e-6));
        CHECK(bounds[0][1].get<double>() == doctest::Approx(-20.0).epsilon(1e-6));
        CHECK(bounds[1][1].get<double>() == doctest::Approx(-10.0).epsilon(1e-6));
        CHECK(bounds[0][2].get<double>() == doctest::Approx(5.0).epsilon(1e-6));
        CHECK(bounds[1][2].get<double>() == doctest::Approx(15.0).epsilon(1e-6));
        const json& mesh = hit.at("overlap_mesh");
        const json& positions = mesh.at("positions");
        REQUIRE_FALSE(positions.empty());
        REQUIRE(positions.size() % 3 == 0);
        CHECK_FALSE(mesh.at("indices").empty());
        bool left_region = false, right_region = false;
        for (std::size_t p = 0; p < positions.size(); p += 3) {
            const double x = positions[p].get<double>();
            left_region = left_region || x < 110.0;
            right_region = right_region || x > 135.0;
        }
        CHECK(left_region);
        CHECK(right_region);
    }
}

TEST_CASE("eval_interference: failed Common uses a marked fuzzy fallback") {
    using chudware::doc::interference_report_with_injected_failure_for_testing;
    const TopoDS_Shape a = BRepPrimAPI_MakeBox(10, 10, 10).Shape();
    const TopoDS_Shape b = BRepPrimAPI_MakeBox(gp_Pnt(5, 0, 0), 10, 10, 10).Shape();
    for (bool threshold : {false, true}) {
        const json report = interference_report_with_injected_failure_for_testing(
            a, b, threshold, false, true, false, false);
        INFO(report.dump(2));
        CHECK(report["complete"] == true);
        CHECK(report["ok"] == false);
        CHECK(report["pairs_checked"] == 1);
        CHECK(report["pair_failures"].empty());
        REQUIRE(report["interferences"].size() == 1);
        const json& hit = report["interferences"][0];
        CHECK(hit["method"] == "fallback_fuzzy_common");
        CHECK(hit["approximate"] == true);
        CHECK(hit["volume_approximate"] == true);
        CHECK(hit["fuzzy_tolerance_mm"].get<double>() > 0.0);
        CHECK(hit["overlap_volume_mm3"].get<double>() == doctest::Approx(500.0).epsilon(1e-4));
        CHECK_FALSE(hit["overlap_mesh"]["indices"].empty());
    }
}

TEST_CASE("eval_interference: fuzzy Common marks a resolved contact") {
    using chudware::doc::interference_report_with_injected_failure_for_testing;
    const TopoDS_Shape a = BRepPrimAPI_MakeBox(10, 10, 10).Shape();
    const TopoDS_Shape b = BRepPrimAPI_MakeBox(gp_Pnt(10, 0, 0), 10, 10, 10).Shape();
    const json report = interference_report_with_injected_failure_for_testing(
        a, b, false, true, true, false, false);
    INFO(report.dump(2));
    REQUIRE(report["complete"] == true);
    CHECK(report["ok"] == true);
    CHECK(report["pairs_checked"] == 1);
    CHECK(report["interferences"].empty());
    REQUIRE(report["contacts"].size() == 1);
    CHECK(report["contacts"][0]["method"] == "fallback_fuzzy_common");
    CHECK(report["contacts"][0]["approximate"] == true);
    CHECK(report["contacts"][0]["distance"].get<double>() == doctest::Approx(0.0));
}

TEST_CASE("eval_interference: fuzzy Cut preserves distinct compound overlap") {
    using chudware::doc::interference_report_with_injected_failure_for_testing;
    auto block = [](double x, double length) {
        return BRepPrimAPI_MakeBox(gp_Pnt(x, 0, 0), length, 10, 10).Shape();
    };
    const TopoDS_Compound a = interference_compound({block(0, 10), block(5, 10)});
    const TopoDS_Shape b = block(2, 12);
    for (bool fail_common : {false, true}) {
        const json report = interference_report_with_injected_failure_for_testing(
            a, b, false, false, fail_common, true, false);
        INFO(report.dump(2));
        REQUIRE(report["complete"] == true);
        CHECK(report["pairs_checked"] == 1);
        CHECK(report["pair_failures"].empty());
        REQUIRE(report["interferences"].size() == 1);
        const json& hit = report["interferences"][0];
        CHECK(hit["method"] == (fail_common ? "fallback_fuzzy_common_and_cut" :
                                              "fallback_fuzzy_cut"));
        CHECK(hit["volume_approximate"] == true);
        CHECK(hit["overlap_volume_mm3"].get<double>() == doctest::Approx(1200.0).epsilon(1e-4));
    }
}

TEST_CASE("eval_interference: extrema resolves disjoint shapes after failed Booleans") {
    using chudware::doc::interference_report_with_injected_failure_for_testing;
    auto cylinder = [](double x, double y) {
        return BRepPrimAPI_MakeCylinder(
            gp_Ax2(gp_Pnt(x, y, 0), gp_Dir(0, 0, 1)), 1.0, 10.0).Shape();
    };
    // The circular cross sections are separated even though both AABBs overlap.
    const TopoDS_Shape a = cylinder(0, 0), b = cylinder(1.9, 1.9);
    for (bool threshold : {false, true}) {
        const json report = interference_report_with_injected_failure_for_testing(
            a, b, threshold, false, true, false, true);
        INFO(report.dump(2));
        REQUIRE(report["complete"] == true);
        CHECK(report["ok"] == true);
        CHECK(report["pairs_checked"] == 1);
        CHECK(report["pair_failures"].empty());
        CHECK(report["interferences"].empty());
        REQUIRE(report["clearances"].size() == 1);
        const json& clear = report["clearances"][0];
        CHECK(clear["method"] == "fallback_extrema_disjoint");
        CHECK(clear["fallback_extrema_disjoint"] == true);
        CHECK_FALSE(clear.contains("approximate"));
        if (!threshold)
            CHECK(clear["distance"].get<double>() ==
                  doctest::Approx(std::sqrt(1.9 * 1.9 * 2.0) - 2.0).epsilon(1e-5));
    }
}

TEST_CASE("eval_interference: failed fallback remains incomplete") {
    using chudware::doc::interference_report_with_injected_failure_for_testing;
    const TopoDS_Shape a = BRepPrimAPI_MakeBox(10, 10, 10).Shape();
    TopoDS_Shape b;
    SUBCASE("partially overlapping") {
        b = BRepPrimAPI_MakeBox(gp_Pnt(5, 0, 0), 10, 10, 10).Shape();
    }
    SUBCASE("one body contained inside the other") {
        b = BRepPrimAPI_MakeBox(gp_Pnt(2, 2, 2), 4, 4, 4).Shape();
    }
    const json report = interference_report_with_injected_failure_for_testing(
        a, b, true, false, true, false, true);
    INFO(report.dump(2));
    CHECK(report["complete"] == false);
    CHECK(report["ok"] == false);
    CHECK(report["pairs_checked"] == 0);
    CHECK(report["interferences"].empty());
    CHECK(report["clearances"].empty());
    REQUIRE(report["pair_failures"].size() == 1);
    CHECK(report["pair_failures"][0]["reason"] == "boolean_failed");
    CHECK(report["summary"].get<std::string>().find(
              "First failed pair: a<->b (boolean_failed)") != std::string::npos);
}

TEST_CASE("eval_interference: a degenerated edge resting on a face is not an orientation failure") {
    using chudware::doc::interference_report_with_injected_failure_for_testing;
    // OCCT 7.9.1 raises UnableToOrientTheShape for the degenerated edge of a sphere pole or a
    // cone apex that touches another solid, although Common is right. No injected failure.
    const TopoDS_Shape plate = BRepPrimAPI_MakeBox(gp_Pnt(-10, -10, -10), gp_Pnt(10, 10, 0)).Shape();
    SUBCASE("a sphere pole resting on the plate is a contact") {
        const TopoDS_Shape ball = BRepPrimAPI_MakeSphere(gp_Pnt(0, 0, 5), 5.0).Shape();
        for (bool threshold : {false, true}) {
            const json report = interference_report_with_injected_failure_for_testing(
                ball, plate, threshold, true, false, false, false);
            INFO(report.dump(2));
            REQUIRE(report["complete"] == true);
            CHECK(report["pair_failures"].empty());
            CHECK(report["interferences"].empty());
            REQUIRE(report["contacts"].size() == 1);
            CHECK_FALSE(report["contacts"][0].contains("approximate"));
            CHECK(report["contacts"][0]["distance"].get<double>() == doctest::Approx(0.0));
        }
    }
    SUBCASE("a cone apex on the plate face keeps the exact overlap volume of the cone inside") {
        const TopoDS_Shape cone = BRepPrimAPI_MakeCone(
            gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(0, 0, -1)), 0.0, 3.0, 6.0).Shape();
        const json report = interference_report_with_injected_failure_for_testing(
            cone, plate, false, false, false, false, false);
        INFO(report.dump(2));
        REQUIRE(report["complete"] == true);
        CHECK(report["pair_failures"].empty());
        REQUIRE(report["interferences"].size() == 1);
        const json& hit = report["interferences"][0];
        CHECK_FALSE(hit.contains("method"));
        const double pi = 3.14159265358979323846;
        CHECK(hit["overlap_volume_mm3"].get<double>() ==
              doctest::Approx(pi * 3.0 * 3.0 * 6.0 / 3.0).epsilon(1e-6));
    }
}

TEST_CASE("eval_interference: a failed Common is resolved exactly by clipping to the operand bounds") {
    using chudware::doc::interference_report_with_unclipped_failure_for_testing;
    const TopoDS_Shape a = BRepPrimAPI_MakeBox(10, 10, 10).Shape();
    const TopoDS_Shape b = BRepPrimAPI_MakeBox(gp_Pnt(5, 0, 0), 10, 10, 10).Shape();
    for (bool threshold : {false, true}) {
        const json report = interference_report_with_unclipped_failure_for_testing(a, b, threshold);
        INFO(report.dump(2));
        REQUIRE(report["complete"] == true);
        CHECK(report["pair_failures"].empty());
        REQUIRE(report["interferences"].size() == 1);
        const json& hit = report["interferences"][0];
        CHECK(hit["method"] == "clipped_common");
        CHECK(hit["clipped_common"] == true);
        CHECK_FALSE(hit.contains("approximate"));
        CHECK(hit["overlap_volume_mm3"].get<double>() == doctest::Approx(500.0).epsilon(1e-6));
    }
}

TEST_CASE("eval_interference: exhausted fuzzy retry budget is not a clear pair") {
    using chudware::doc::interference_report_with_injected_failure_for_testing;
    const TopoDS_Shape a = BRepPrimAPI_MakeBox(10, 10, 10).Shape();
    const TopoDS_Shape b = BRepPrimAPI_MakeBox(gp_Pnt(5, 0, 0), 10, 10, 10).Shape();
    const json report = interference_report_with_injected_failure_for_testing(
        a, b, true, false, true, false, false, 0.0);
    INFO(report.dump(2));
    CHECK(report["complete"] == false);
    CHECK(report["pairs_checked"] == 0);
    CHECK(report["interferences"].empty());
    CHECK(report["clearances"].empty());
    REQUIRE(report["pair_failures"].size() == 1);
    CHECK(report["pair_failures"][0]["reason"] == "pair_timeout");
}

TEST_CASE("eval_interference: compound contact and internal clearance remain exact") {
    auto block = [](double x, double length) {
        return BRepPrimAPI_MakeBox(gp_Pnt(x, 0, 0), length, 10, 10).Shape();
    };
    const TopoDS_Compound a = interference_compound({block(0, 10), block(30, 10)});
    TopoDS_Compound b;
    double expected_distance = 0.0;
    bool contact = true;
    SUBCASE("one constituent touches") {
        b = interference_compound({block(10, 10), block(20, 5)});
    }
    SUBCASE("all constituents disjoint despite overlapping member bounds") {
        b = interference_compound({block(12, 8), block(21, 4)});
        expected_distance = 2.0;
        contact = false;
    }
    InterferenceAssemblyFixture fixture(a, b);
    for (const char* mode : {"exact", "threshold"}) {
        const json report = doc_eval_interference(fixture.id, {
            {"clearance_mode", mode}, {"include_coincident", true}, {"min_clearance_mm", 20}});
        INFO(mode << ": " << report.dump(2));
        REQUIRE(report.at("complete") == true);
        CHECK(report.at("ok") == true);
        CHECK(report.at("pairs_total") == 1);
        CHECK(report.at("pairs_checked") == 1);
        CHECK(report.at("pair_failures").empty());
        CHECK(report.at("interferences").empty());
        const char* kind = contact ? "contacts" : "clearances";
        const char* other = contact ? "clearances" : "contacts";
        CHECK(report.at(other).empty());
        REQUIRE(report.at(kind).size() == 1);
        CHECK(std::abs(report.at(kind).front().at("distance").get<double>() -
                       expected_distance) <= 1e-6);
        CHECK(report.at(kind).front().at("warning") == "below min clearance");
    }
}

TEST_CASE("eval_interference: an unusable compound constituent cannot be hidden") {
    BRep_Builder builder;
    TopoDS_Solid empty;
    builder.MakeSolid(empty);
    const TopoDS_Compound a = interference_compound({
        BRepPrimAPI_MakeBox(10, 10, 10).Shape(), empty});
    const TopoDS_Shape b = BRepPrimAPI_MakeBox(gp_Pnt(5, 0, 0), 10, 10, 10).Shape();
    InterferenceAssemblyFixture fixture(a, b);
    const json report = doc_eval_interference(fixture.id);
    INFO(report.dump(2));
    CHECK(report.at("ok") == false);
    CHECK(report.at("complete") == false);
    CHECK(report.at("pairs_checked") == 0);
    CHECK(report.at("interferences").empty());
    REQUIRE(report.at("pair_failures").size() == 1);
    const json& failure = report.at("pair_failures").front();
    CHECK(failure.at("reason") == "boolean_or_volume_failed");
    CHECK(failure.at("detail").get<std::string>().find("solid 2") != std::string::npos);
    CHECK(failure.at("detail").get<std::string>().find("bounds") != std::string::npos);
}

// members filter: with only one body name selected the pairwise set shrinks to empty
// (a single member has no pairs) while parts[] still reflects the filtered set.
TEST_CASE("eval_interference: members filter shrinks the pairwise set") {
    // Three independent bodies: A/B overlap, C is far away. Unfiltered -> 1 interference
    // (A∩B). members=["b1"] -> parts size 1, zero pairs. members=[b1,b2] keeps the pair.
    json ops = json::array({
        box("A", 20, 20, 20, {0, 0, 0}, json{{"merge", false}}),
        box("B", 20, 20, 20, {10, 0, 0}, json{{"merge", false}}),
        box("C", 10, 10, 10, {100, 0, 0}, json{{"merge", false}}),
    });
    Built b = build("MembersFilter", ops);

    json all = doc_eval_interference(b.id);
    REQUIRE(all.contains("parts"));
    REQUIRE_MESSAGE(all["parts"].size() == 3,
                    "expected 3 bodies, got: " << all.dump());
    REQUIRE_MESSAGE(all["interferences"].size() == 1,
                    "expected exactly one A∩B interference: " << all.dump());
    const std::string n0 = all["parts"][0]["name"].get<std::string>();
    const std::string n1 = all["parts"][1]["name"].get<std::string>();
    const std::string n2 = all["parts"][2]["name"].get<std::string>();

    // One name -> no pairs.
    json one = doc_eval_interference(b.id, json{{"members", json::array({n0})}});
    REQUIRE(one.contains("parts"));
    CHECK_MESSAGE(one["parts"].size() == 1,
                  "members filter should leave one part, got: " << one.dump());
    CHECK(one["parts"][0]["name"].get<std::string>() == n0);
    CHECK_MESSAGE(one["interferences"].empty(),
                  "one member cannot form a pair: " << one.dump());
    CHECK(one["scope"]["kind"] == "members");
    CHECK(one["scope"]["checked_members"] == 1);
    CHECK(one["scope"]["total_members"] == 3);
    CHECK(one["pairs_checked"] == 0);

    // Two overlapping names (exclude far C) -> still one interference.
    // A is b1, B is b2 for merge:false boxes in order.
    json two = doc_eval_interference(b.id, json{{"members", json::array({n0, n1})}});
    REQUIRE_MESSAGE(two["parts"].size() == 2,
                    "two-name filter should leave two parts: " << two.dump());
    REQUIRE_MESSAGE(two["interferences"].size() == 1,
                    "A∩B must survive a members filter that includes both: " << two.dump());
    CHECK(two["interferences"][0]["overlap_volume_mm3"].get<double>() ==
          doctest::Approx(4000.0).epsilon(1e-2));

    // Far body alone with one overlap partner is not needed; filtering to C alone is empty.
    json c_only = doc_eval_interference(b.id, json{{"members", json::array({n2})}});
    CHECK(c_only["parts"].size() == 1);
    CHECK(c_only["interferences"].empty());

    // Never turn an invalid requested pair into a clean single-member result.
    CHECK_THROWS(doc_eval_interference(b.id, json{{"members", json::array({n0, "missing"})}}));
    CHECK_THROWS(doc_eval_interference(b.id, json{{"members", json::array({n0, 7})}}));
    CHECK_THROWS(doc_eval_interference(b.id, json{{"members", n0}}));
}

// (2) box + boss + engulfing cut -> doc_eval_feature_overlap must report a boss_swallowed
// pair (ok:false) and list the boss as a dead feature (the cut removes 100% of its tool
// volume). Base [0,40]x[0,40]x[0,20]; Boss [15,25]x[15,25]x[15,25] merged in (adds the
// [15,25]x[15,25]x[20,25] slab above the base -> a real, non-buried boss); Cut
// [10,30]x[10,30]x[10,30] fully contains the boss's own tool box -> common/add_vol = 100%.
TEST_CASE("eval_feature_overlap: a cut that fully engulfs an earlier boss reports boss_swallowed") {
    json ops = json::array({
        box("Base", 40, 40, 20, {0, 0, 0}),
        box("Boss", 10, 10, 10, {15, 15, 15}),  // mode defaults to "boss" (merge:true)
        box("Cut", 20, 20, 20, {10, 10, 10}, json{{"mode", "cut"}}),
    });
    Built b = build("BossSwallowed", ops);
    const std::string boss_id = fid(b, 1);
    const std::string cut_id = fid(b, 2);

    json ov = doc_eval_feature_overlap(b.id);
    REQUIRE_MESSAGE(ov.value("ok", true) == false, "expected ok:false, got: " << ov.dump());
    CHECK(ov.at("complete") == true);
    CHECK(ov.at("basis") == "pre_boolean_tool_intersection");
    CHECK(ov.at("note").get<std::string>().find("not proof") != std::string::npos);
    bool found = false;
    for (const json& p : ov.at("pairs")) {
        if (p.value("kind", std::string()) != "boss_swallowed") continue;
        const std::string a = p.value("a", std::string()), pb = p.value("b", std::string());
        if ((a == boss_id && pb == cut_id) || (a == cut_id && pb == boss_id)) {
            found = true;
            CHECK(p.at("tool_kind") == "add_cut_tool_overlap");
            CHECK(p.at("volume_basis") == "pre_boolean_tool_intersection");
        }
    }
    CHECK_MESSAGE(found, "no boss_swallowed(" << boss_id << "," << cut_id
                  << ") pair in: " << ov.at("pairs").dump());
    CHECK_MESSAGE(has_dead(ov.at("dead_features"), boss_id),
                  "boss '" << boss_id << "' missing from dead_features: "
                  << ov.at("dead_features").dump());
}

// (3) Two overlapping hole cuts (drilled into the same base) -> a cut_overlap pair between
// the two cut tool cylinders. Cyl1 axis at (20,30), Cyl2 axis at (25,30), both r=5 (centre
// distance 5 < 2r=10 -> the two r=5 circles overlap) and height 30 (taller than the 20mm
// plate, so both are guaranteed full through-cuts).
TEST_CASE("eval_feature_overlap: two overlapping hole cuts report cut_overlap") {
    json ops = json::array({
        box("Plate", 60, 60, 20, {0, 0, 0}),
        cyl("Cut1", 5, 30, {20, 30, 0}, json{{"mode", "cut"}}),
        cyl("Cut2", 5, 30, {25, 30, 0}, json{{"mode", "cut"}}),
    });
    Built b = build("CutOverlap", ops);
    const std::string cut1 = fid(b, 1);
    const std::string cut2 = fid(b, 2);

    json ov = doc_eval_feature_overlap(b.id);
    bool found = false;
    for (const json& p : ov.at("pairs")) {
        if (p.value("kind", std::string()) != "cut_overlap") continue;
        const std::string a = p.value("a", std::string()), pb = p.value("b", std::string());
        if ((a == cut1 && pb == cut2) || (a == cut2 && pb == cut1)) {
            found = true;
            CHECK(p.at("volume_mm3").get<double>() > 0.0);
            CHECK(p.at("tool_kind") == "cut_tool_overlap");
            CHECK(p.at("volume_basis") == "pre_boolean_tool_intersection");
            // The tools extend 30 mm through 20 mm stock. Their common
            // volume includes the 10 mm outside the material.
            const double lens = 50.0 * std::acos(0.5) - 2.5 * std::sqrt(75.0);
            CHECK(p.at("volume_mm3").get<double>() == doctest::Approx(30.0 * lens).epsilon(1e-6));
            CHECK(p.at("volume_mm3").get<double>() > 20.0 * lens);
        }
    }
    CHECK_MESSAGE(found, "no cut_overlap(" << cut1 << "," << cut2 << ") pair in: "
                  << ov.at("pairs").dump());
}

TEST_CASE("eval_feature_overlap: pattern excludes only its seed and keeps genuine overlaps") {
    double spacing = 12.0;
    bool overlapping = false;
    SUBCASE("separate copies") {}
    SUBCASE("copy intersects seed") { spacing = 4.0; overlapping = true; }
    Built b = build("PatternToolOverlap", json::array({
        box("Plate", 60, 40, 4, {0, 0, 0}),
        {{"op", "add_feature"}, {"feature", {{"type", "hole"}, {"name", "Seed"},
            {"params", {{"style", "simple"}, {"standard", "custom"},
                        {"diameter", 8.0}, {"end", "through_all"}}},
            {"refs", {{"face", {{"of", "face"}, {"side", "top"}}}}}}}},
        {{"op", "add_feature"}, {"feature", {{"type", "linear_pattern"}, {"name", "Copies"},
            {"params", {{"count1", 2}, {"spacing1", spacing}}},
            {"refs", {{"dir1", "axis.x"}, {"features", json::array({"$1"})}}}}}}
    }));
    const std::string seed = fid(b, 1), pattern = fid(b, 2);
    const json ov = doc_eval_feature_overlap(b.id, {{"features", {pattern}}});
    REQUIRE_MESSAGE(ov.at("complete") == true, ov.dump());
    bool found = false;
    for (const json& p : ov.at("pairs")) {
        if (!((p.at("a") == seed && p.at("b") == pattern) ||
              (p.at("a") == pattern && p.at("b") == seed))) continue;
        found = true;
        CHECK(p.at("tool_kind") == "cut_tool_overlap");
        CHECK(p.at("volume_basis") == "pre_boolean_tool_intersection");
    }
    CHECK(found == overlapping);
    chudware::doc::doc_close(b.id);
}

// (4) Disjoint features (two additive boxes 100mm apart, never touching) -> zero pairs,
// ok:true. Uses merge:false so both survive as independent bodies/tool_recs without any
// boolean between them at all -- the common-volume test between their tool solids is
// exactly 0, below min_volume_mm3, so no pair is ever emitted.
TEST_CASE("eval_feature_overlap: disjoint features report zero pairs") {
    json ops = json::array({
        box("A", 20, 20, 20, {0, 0, 0}),
        box("B", 20, 20, 20, {100, 0, 0}, json{{"merge", false}}),
    });
    Built b = build("Disjoint", ops);

    json ov = doc_eval_feature_overlap(b.id);
    CHECK(ov.value("ok", false) == true);
    CHECK_MESSAGE(ov.at("pairs").empty(), "expected zero pairs, got: " << ov.at("pairs").dump());
}

// (5a) eval_compare: identical geometry built independently in two docs -> near-total
// match (>= 99.5%) and no meaningful residual regions (any sliver from boolean fuzz is
// below the 1e-3 mm^3 drop floor).
TEST_CASE("eval_compare: identical geometry in two docs matches >= 99.5% with no residuals") {
    json ops = json::array({box("Blk", 30, 20, 10, {0, 0, 0})});
    Built a = build("CompareA", ops);
    Built b = build("CompareB", ops);

    json cmp = doc_eval_compare(json{{"doc_a", a.id}, {"doc_b", b.id}});
    REQUIRE(cmp.value("ok", false) == true);
    REQUIRE_MESSAGE(cmp.value("degraded", false) == false,
                    "unexpected degraded compare: " << cmp.dump());
    CHECK(cmp.at("match_pct").get<double>() >= 99.5);
    CHECK(cmp.at("residual_regions").empty());
}

// (5b) eval_compare: a 10mm-translated copy of the SAME 30x20x10 box overlaps only on
// x:[10,30] (20mm of the 30mm length) -> common = 20*20*10 = 4000, volume = 6000 each,
// match_pct = 100*4000/6000 = 66.67% (< 90), with a real 2000 mm^3 slice missing from A
// (x:[0,10]) and a real 2000 mm^3 slice extra in B (x:[30,40]) -- >=1 region each side.
TEST_CASE("eval_compare: a translated copy drops below 90% match with missing+extra regions") {
    Built a = build("CompareC1", json::array({box("Blk", 30, 20, 10, {0, 0, 0})}));
    Built b = build("CompareC2", json::array({box("Blk", 30, 20, 10, {10, 0, 0})}));

    json cmp = doc_eval_compare(json{{"doc_a", a.id}, {"doc_b", b.id}});
    REQUIRE_MESSAGE(cmp.value("degraded", false) == false,
                    "unexpected degraded compare: " << cmp.dump());
    CHECK(cmp.at("match_pct").get<double>() < 90.0);
    CHECK(cmp.at("match_pct").get<double>() == doctest::Approx(66.6667).epsilon(0.01));

    bool has_missing = false, has_extra = false;
    for (const json& r : cmp.at("residual_regions")) {
        const std::string side = r.value("side", std::string());
        if (side == "missing") has_missing = true;
        if (side == "extra") has_extra = true;
        CHECK(r.at("volume_mm3").get<double>() > 0.0);
    }
    CHECK_MESSAGE(has_missing, "no 'missing' residual region in: "
                  << cmp.at("residual_regions").dump());
    CHECK_MESSAGE(has_extra, "no 'extra' residual region in: "
                  << cmp.at("residual_regions").dump());
}

// (6a) include_coincident: two boxes flush-stacked with ZERO gap (box2's base sits exactly
// on box1's top, both merge:false -> two independent bodies) must show up as a `contacts`
// entry when include_coincident:true (the SW-parity "coincident faces" gap, B4).
TEST_CASE("eval_interference: flush-stacked boxes report a contacts entry with include_coincident") {
    json ops = json::array({
        box("Lower", 20, 20, 10, {0, 0, 0}),
        box("Upper", 20, 20, 10, {0, 0, 10}, json{{"merge", false}}),
    });
    Built b = build("Coincident", ops);

    json plain = doc_eval_interference(b.id);
    CHECK_MESSAGE(plain.at("contacts").empty(),
                  "contacts should be empty without include_coincident: " << plain.dump());

    json params{{"include_coincident", true}};
    SUBCASE("exact default") {}
    SUBCASE("threshold must measure coincident contact") { params["clearance_mode"] = "threshold"; }
    json ir = doc_eval_interference(b.id, params);
    CHECK_MESSAGE(ir.at("interferences").empty(),
                  "flush-stacked boxes must not read as an overlap: " << ir.dump());
    REQUIRE_MESSAGE(!ir.at("contacts").empty(),
                    "expected a contacts entry, got: " << ir.dump());
    CHECK(ir["contacts"][0].at("distance").get<double>() < 1e-5);
}

// (6b) min_clearance_mm: two boxes with a real but tiny 0.05mm gap (below a 0.1mm
// threshold) must carry a "below min clearance" warning on their clearance entry.
TEST_CASE("eval_interference: min_clearance_mm tags a below-threshold gap with a warning") {
    json ops = json::array({
        box("Lower", 20, 20, 10, {0, 0, 0}),
        box("Upper", 20, 20, 10, {0, 0, 10.05}, json{{"merge", false}}),
    });
    Built b = build("TightGap", ops);

    json ir = doc_eval_interference(b.id, json{{"min_clearance_mm", 0.1}});
    REQUIRE_MESSAGE(!ir.at("clearances").empty(), "expected a clearance entry: " << ir.dump());
    bool found = false;
    for (const json& c : ir.at("clearances")) {
        if (c.at("distance").get<double>() > 0.06) continue;  // the ~0.05mm gap specifically
        found = true;
        CHECK(c.value("warning", std::string()) == "below min clearance");
    }
    CHECK_MESSAGE(found, "no ~0.05mm clearance entry in: " << ir.at("clearances").dump());
}

TEST_CASE("eval_interference: threshold classifies every pair without inventing exact gaps") {
    // Cylinders are separated but their AABBs overlap: Common, not an AABB alone,
    // must prove the close pair has no volumetric interference.
    Built b = build("ThresholdCylinders", json::array({
        cyl("A", 10, 10, {0, 0, 0}, {{"merge", false}}),
        cyl("B", 10, 10, {15, 15, 0}, {{"merge", false}}),
        box("Far", 10, 10, 10, {100, 100, 0}, {{"merge", false}}),
    }));
    const json classified = doc_eval_interference(b.id, {{"clearance_mode", "threshold"}});
    INFO(classified.dump(2));
    REQUIRE(classified.at("complete") == true);
    CHECK(classified.at("ok") == true);
    CHECK(classified.at("pairs_total") == 3);
    CHECK(classified.at("pairs_checked") == 3);
    CHECK(classified.at("scope").at("checked_members") == 3);
    CHECK(classified.at("interferences").empty());
    CHECK(classified.at("pair_failures").empty());
    CHECK(classified.at("geometry_failures").empty());
    REQUIRE(classified.at("clearances").size() == 3);
    for (const auto& pair : classified.at("clearances")) {
        CHECK(pair.at("distance_status") == "lower_bound");
        CHECK(pair.at("distance_lower_bound_mm").get<double>() >= 0);
        CHECK_FALSE(pair.contains("distance"));
    }
    CHECK(classified.at("clearances")[0].at("distance_lower_bound_mm") == 0.0);

    // Omitted mode and explicit exact both retain all minimum distances.
    const json exact = doc_eval_interference(b.id);
    REQUIRE(exact.at("complete") == true);
    REQUIRE(exact.at("clearances").size() == 3);
    CHECK(exact.at("clearances")[0].at("distance").get<double>() ==
          doctest::Approx(1.2132034356).epsilon(1e-6));
    for (const auto& pair : exact.at("clearances")) {
        CHECK(pair.at("distance").is_number());
        CHECK_FALSE(pair.contains("distance_lower_bound_mm"));
    }
    CHECK(doc_eval_interference(b.id, {{"clearance_mode", "exact"}}) == exact);
    CHECK_THROWS(doc_eval_interference(b.id, {{"clearance_mode", "approximate"}}));
    CHECK_THROWS(doc_eval_interference(b.id, {{"clearance_mode", false}}));
}

TEST_CASE("eval_interference: inconclusive threshold bounds require exact positive clearance") {
    Built b = build("ThresholdPositiveClearance", json::array({
        cyl("A", 10, 10, {0, 0, 0}, {{"merge", false}}),
        cyl("B", 10, 10, {15, 15, 0}, {{"merge", false}}),
        box("Far", 10, 10, 10, {100, 100, 0}, {{"merge", false}}),
    }));
    double requested = 0.5;
    bool below_minimum = false;
    SUBCASE("exact gap proves the request satisfied despite overlapping AABBs") {}
    SUBCASE("exact gap proves a clearance violation") { requested = 2; below_minimum = true; }
    const json report = doc_eval_interference(b.id, {
        {"clearance_mode", "threshold"}, {"min_clearance_mm", requested},
        {"members", {"b1", "b2"}}, {"quick", true}});
    INFO(report.dump(2));
    REQUIRE(report.at("complete") == true);
    CHECK(report.at("pairs_checked") == 1);
    CHECK(report.at("pairs_total") == 1);
    CHECK(report.at("scope").at("total_members") == 3);
    CHECK(report.at("scope").at("checked_members") == 2);
    REQUIRE(report.at("clearances").size() == 1);
    const auto& pair = report.at("clearances")[0];
    CHECK(pair.at("distance_status") == "exact");
    CHECK(pair.at("distance").get<double>() == doctest::Approx(1.2132034356).epsilon(1e-6));
    CHECK_FALSE(pair.contains("distance_lower_bound_mm"));
    CHECK((pair.value("warning", std::string()) == "below min clearance") == below_minimum);
}

TEST_CASE("eval_interference: proven-disjoint boundary distance agrees with full-solid extrema") {
    TopoDS_Shape a = BRepPrimAPI_MakeBox(10, 10, 10).Shape();
    TopoDS_Shape b = BRepPrimAPI_MakeBox(gp_Pnt(10.25, 0, 0), 1, 1, 1).Shape();
    double expected_distance = 0.25;
    double expected_overlap = 0.0;
    bool tolerance_case = false;
    SUBCASE("separated boxes") {}
    SUBCASE("touching boxes retain exact contact") {
        b = BRepPrimAPI_MakeBox(gp_Pnt(10, 0, 0), 1, 1, 1).Shape();
        expected_distance = 0;
    }
    SUBCASE("geometric-tolerance-scale separation matches the original algorithm") {
        b = BRepPrimAPI_MakeBox(gp_Pnt(10 + 5e-8, 0, 0), 1, 1, 1).Shape();
        tolerance_case = true;
    }
    SUBCASE("hollow solid retains the inner cavity boundary") {
        a = BRepAlgoAPI_Cut(a,
            BRepPrimAPI_MakeBox(gp_Pnt(2, 2, 2), 6, 6, 6).Shape()).Shape();
        b = BRepPrimAPI_MakeBox(gp_Pnt(2.25, 4, 4), 1, 1, 1).Shape();
    }
    SUBCASE("contained solid remains volumetric interference") {
        b = BRepPrimAPI_MakeBox(gp_Pnt(4, 4, 4), 1, 1, 1).Shape();
        expected_overlap = 1;
    }
    SUBCASE("tiny contained solid below the volume epsilon still has zero distance") {
        // Its boundaries are almost 4 mm apart: boundary-only extrema would
        // invent a positive gap even though the common mass is below 1e-3.
        b = BRepPrimAPI_MakeBox(gp_Pnt(4, 4, 4), 0.05, 0.05, 0.05).Shape();
        expected_distance = 0;
    }
    SUBCASE("tiny crossing overlap below the volume epsilon remains contact") {
        b = BRepPrimAPI_MakeBox(gp_Pnt(9.98, 4, 4), 0.05, 0.05, 0.05).Shape();
        expected_distance = 0;
    }
    SUBCASE("multi-solid member uses every solid boundary") {
        BRep_Builder builder;
        TopoDS_Compound compound;
        builder.MakeCompound(compound);
        builder.Add(compound, a);
        builder.Add(compound,
            BRepPrimAPI_MakeBox(gp_Pnt(30, 0, 0), 10, 10, 10).Shape());
        a = compound;  // B lies within A's aggregate AABB, but outside both solids.
    }
    SUBCASE("overlapping solids within a member do not hide its external boundary") {
        BRep_Builder builder;
        TopoDS_Compound compound;
        builder.MakeCompound(compound);
        builder.Add(compound, a);
        builder.Add(compound,
            BRepPrimAPI_MakeBox(gp_Pnt(5, 0, 0), 10, 10, 10).Shape());
        a = compound;
        b = BRepPrimAPI_MakeBox(gp_Pnt(15.25, 0, 0), 1, 1, 1).Shape();
    }
    SUBCASE("multi-solid member with a tiny contained solid keeps full-solid treatment") {
        BRep_Builder builder;
        TopoDS_Compound compound;
        builder.MakeCompound(compound);
        builder.Add(compound, b);
        builder.Add(compound,
            BRepPrimAPI_MakeBox(gp_Pnt(4, 4, 4), 0.05, 0.05, 0.05).Shape());
        b = compound;
        expected_distance = 0;
    }

    const auto did = doc_new("assembly", "BoundaryDistance").at("doc_id").get<std::string>();
    IntegrityFixtureFile file_a{
        std::filesystem::temp_directory_path() / ("boundary_a_" + did + ".brep")};
    IntegrityFixtureFile file_b{
        std::filesystem::temp_directory_path() / ("boundary_b_" + did + ".brep")};
    REQUIRE(BRepTools::Write(a, file_a.path.string().c_str()));
    REQUIRE(BRepTools::Write(b, file_b.path.string().c_str()));
    doc_apply_ops(did, -1, json::array({
        {{"op", "asm_insert_component"}, {"source", {{"path", file_a.path.string()}}}},
        {{"op", "asm_insert_component"}, {"source", {{"path", file_b.path.string()}}}}
    }), false);

    const json exact = doc_eval_interference(did,
        {{"include_coincident", true}, {"min_clearance_mm", 20}});
    const json threshold = doc_eval_interference(did,
        {{"include_coincident", true}, {"min_clearance_mm", 20}, {"clearance_mode", "threshold"}});
    INFO(exact.dump(2));
    INFO(threshold.dump(2));
    for (const auto* report : {&exact, &threshold}) {
        REQUIRE(report->at("complete") == true);
        CHECK(report->at("pairs_total") == 1);
        CHECK(report->at("pairs_checked") == 1);
        CHECK(report->at("pairs_reused") == 0);
        CHECK(report->at("pair_failures").empty());
        CHECK(report->at("geometry_failures").empty());
    }
    for (const char* kind : {"interferences", "contacts", "clearances"})
        CHECK(threshold.at(kind).size() == exact.at(kind).size());
    if (expected_overlap > 0) {
        REQUIRE(exact.at("interferences").size() == 1);
        CHECK(exact.at("interferences").front().at("volume").get<double>() ==
              doctest::Approx(expected_overlap));
        CHECK(threshold.at("interferences").front().at("volume") ==
              exact.at("interferences").front().at("volume"));
    } else {
        REQUIRE(exact.at("interferences").empty());
        const char* kind = exact.at("contacts").empty() ? "clearances" : "contacts";
        REQUIRE(exact.at(kind).size() == 1);
        REQUIRE(threshold.at(kind).size() == 1);
        const auto& reference = exact.at(kind).front();
        const auto& measured = threshold.at(kind).front();
        CHECK(measured.at("distance_status") == "exact");
        CHECK_FALSE(measured.contains("distance_lower_bound_mm"));
        CHECK(measured.at("distance").get<double>() ==
              doctest::Approx(reference.at("distance").get<double>()).epsilon(1e-10));
        CHECK(measured.value("warning", std::string()) == reference.value("warning", std::string()));
        if (!tolerance_case)
            CHECK(measured.at("distance").get<double>() == doctest::Approx(expected_distance));
    }
    chudware::doc::doc_close(did);
}

TEST_CASE("model integrity: positive gap is proximity, not touching, and nearest is exact") {
    Built b = build("IntegrityGap", json::array({
        box("A", 10, 10, 10, {0, 0, 0}, {{"merge", false}}),
        box("B", 10, 10, 10, {10.02, 0, 0}, {{"merge", false}}),
    }));
    // Not near/far: <windows.h> defines both as empty macros (MSVC build).
    json within = doc_eval_model_integrity(b.id, {{"proximity_mm", 0.05}});
    REQUIRE_MESSAGE(within["complete"] == true, within.dump());
    REQUIRE(within["pairs"].size() == 1);
    CHECK(within["pairs"][0]["relation"] == "proximity");
    CHECK(within["pairs"][0]["distance_mm"].get<double>() == doctest::Approx(0.02));
    CHECK(within["pairs"][0]["overlap_volume_mm3"].get<double>() == doctest::Approx(0.0));
    CHECK(within["isolated_solids"].empty());
    CHECK(within["islands"].size() == 1);

    json beyond = doc_eval_model_integrity(b.id, {{"proximity_mm", 0.01}});
    REQUIRE_MESSAGE(beyond["complete"] == true, beyond.dump());
    CHECK(beyond["pairs"].empty());
    CHECK(beyond["islands"].size() == 2);
    CHECK(beyond["isolated_solids"].size() == 2);
    CHECK(beyond["summary"]["theoretical_pairs"] == 1);
    CHECK(beyond["summary"]["candidate_pairs"] == 0);
    REQUIRE(beyond["nearest"].size() == 2);
    for (const json& nearest : beyond["nearest"]) {
        CHECK(nearest["status"] == "exact");
        CHECK(nearest["nearest_id"] != nearest["solid_id"]);
        CHECK(nearest["distance_mm"].get<double>() == doctest::Approx(0.02));
    }
}

TEST_CASE("model integrity: touching and positive volume overlap remain distinct") {
    Built touching = build("IntegrityTouch", json::array({
        box("A", 10, 10, 10, {0, 0, 0}, {{"merge", false}}),
        box("B", 10, 10, 10, {10, 0, 0}, {{"merge", false}}),
    }));
    json touch = doc_eval_model_integrity(touching.id, json::object());
    REQUIRE_MESSAGE(touch["complete"] == true, touch.dump());
    REQUIRE(touch["pairs"].size() == 1);
    CHECK(touch["pairs"][0]["relation"] == "touching");
    CHECK(touch["pairs"][0]["distance_mm"] == 0.0);
    CHECK(touch["pairs"][0]["overlap_volume_mm3"].get<double>() == doctest::Approx(0.0));
    Built overlapping = build("IntegrityOverlap", json::array({
        box("A", 10, 10, 10, {0, 0, 0}, {{"merge", false}}),
        box("B", 10, 10, 10, {5, 0, 0}, {{"merge", false}}),
    }));
    json overlap = doc_eval_model_integrity(overlapping.id, json::object());
    REQUIRE_MESSAGE(overlap["complete"] == true, overlap.dump());
    CHECK(overlap["ok"] == true);  // query success, not a geometry safety verdict
    REQUIRE(overlap["pairs"].size() == 1);
    CHECK(overlap["pairs"][0]["relation"] == "overlap");
    CHECK(overlap["pairs"][0]["overlap_volume_mm3"].get<double>() == doctest::Approx(500.0));
}

TEST_CASE("model integrity: reused assembly evidence follows a moved component and current owners") {
    const std::string did = doc_new("assembly", "IntegrityMovedEvidence").at("doc_id");
    IntegrityFixtureFile fixture{
        std::filesystem::temp_directory_path() / ("integrity_moved_" + did + ".step")};
    chudware::doc::export_bodies(
        {BRepPrimAPI_MakeBox(10, 10, 10).Shape()}, fixture.path.string(), "step");
    json ops = json::array();
    for (double x : {0.0, 5.0, 20.0})
        ops.push_back({{"op", "asm_insert_component"}, {"fixed", true},
            {"source", {{"path", fixture.path.string()}}},
            {"transform", {1,0,0,x, 0,1,0,0, 0,0,1,0, 0,0,0,1}}});
    const json built = doc_apply_ops(did, -1, ops, false);
    const std::string first = built.at("id_map").at("$0");
    const std::string moving = built.at("id_map").at("$1");
    const std::string last = built.at("id_map").at("$2");
    const json params{{"proximity_mm", 1.0}, {"include_nearest", true}};
    const json cold = doc_eval_model_integrity(did, params);
    REQUIRE_MESSAGE(cold["complete"] == true, cold.dump());
    const json warm = doc_eval_model_integrity(did, params);
    REQUIRE_MESSAGE(warm["complete"] == true, warm.dump());
    REQUIRE(warm["summary"]["reused_exact_pairs"].get<int>() > 0);
    CHECK(warm["pairs"] == cold["pairs"]);
    REQUIRE(warm["pairs"].size() == 1);
    CHECK(warm["pairs"][0]["overlap_volume_mm3"].get<double>() == doctest::Approx(500.0));

    doc_apply_ops(did, -1, json::array({
        {{"op", "asm_edit_component"}, {"id", moving},
         {"transform", {1,0,0,15, 0,1,0,0, 0,0,1,0, 0,0,0,1}}}
    }), false);
    const json moved = doc_eval_model_integrity(did, params);
    REQUIRE_MESSAGE(moved["complete"] == true, moved.dump());
    REQUIRE(moved["solids"].size() == 3);
    std::string first_solid, moving_solid, last_solid;
    for (const json& solid : moved["solids"]) {
        CHECK(solid["source_path"] == fixture.path.string());
        CHECK(solid["volume_mm3"].get<double>() == doctest::Approx(1000.0));
        if (solid["component_id"] == first) {
            first_solid = solid["id"];
            CHECK(solid["centroid_mm"][0].get<double>() == doctest::Approx(5.0));
        } else if (solid["component_id"] == moving) {
            moving_solid = solid["id"];
            CHECK(solid["centroid_mm"][0].get<double>() == doctest::Approx(20.0));
        } else {
            CHECK(solid["component_id"] == last);
            last_solid = solid["id"];
            CHECK(solid["centroid_mm"][0].get<double>() == doctest::Approx(25.0));
        }
    }
    REQUIRE(!first_solid.empty());
    REQUIRE(!moving_solid.empty());
    REQUIRE(!last_solid.empty());
    REQUIRE(moved["pairs"].size() == 1);
    const json& pair = moved["pairs"][0];
    CHECK((std::set<std::string>{pair["a"], pair["b"]} ==
           std::set<std::string>{moving_solid, last_solid}));
    CHECK(pair["relation"] == "overlap");
    CHECK(pair["overlap_volume_mm3"].get<double>() == doctest::Approx(500.0));
    CHECK(moved["isolated_solids"] == json::array({first_solid}));
    REQUIRE(moved["nearest"].size() == 1);
    CHECK(moved["nearest"][0]["solid_id"] == first_solid);
    CHECK(moved["nearest"][0]["nearest_id"] == moving_solid);
    CHECK(moved["nearest"][0]["status"] == "exact");
    CHECK(moved["nearest"][0]["distance_mm"].get<double>() == doctest::Approx(5.0));
}

TEST_CASE("model integrity: replacing source geometry invalidates cached volume and distance") {
    const std::string did = doc_new("assembly", "IntegrityChangedSource").at("doc_id");
    IntegrityFixtureFile original{
        std::filesystem::temp_directory_path() / ("integrity_original_" + did + ".step")};
    IntegrityFixtureFile replacement{
        std::filesystem::temp_directory_path() / ("integrity_replacement_" + did + ".step")};
    chudware::doc::export_bodies(
        {BRepPrimAPI_MakeBox(10, 10, 10).Shape()}, original.path.string(), "step");
    chudware::doc::export_bodies(
        {BRepPrimAPI_MakeBox(4, 10, 10).Shape()}, replacement.path.string(), "step");
    const json built = doc_apply_ops(did, -1, json::array({
        {{"op", "asm_insert_component"}, {"fixed", true},
         {"source", {{"path", original.path.string()}}}},
        {{"op", "asm_insert_component"}, {"fixed", true},
         {"source", {{"path", original.path.string()}}},
         {"transform", {1,0,0,8, 0,1,0,0, 0,0,1,0, 0,0,0,1}}}
    }), false);
    const std::string changed = built.at("id_map").at("$0");
    const json params{{"proximity_mm", 5.0}, {"include_nearest", false}};
    const json cold = doc_eval_model_integrity(did, params);
    REQUIRE_MESSAGE(cold["complete"] == true, cold.dump());
    const json warm = doc_eval_model_integrity(did, params);
    REQUIRE_MESSAGE(warm["complete"] == true, warm.dump());
    REQUIRE(warm["summary"]["reused_exact_pairs"].get<int>() > 0);
    REQUIRE(warm["pairs"].size() == 1);
    CHECK(warm["pairs"][0]["overlap_volume_mm3"].get<double>() == doctest::Approx(200.0));

    doc_apply_ops(did, -1, json::array({
        {{"op", "asm_edit_component"}, {"id", changed},
         {"source", {{"path", replacement.path.string()}}}}
    }), false);
    const json updated = doc_eval_model_integrity(did, params);
    REQUIRE_MESSAGE(updated["complete"] == true, updated.dump());
    REQUIRE(updated["solids"].size() == 2);
    bool found = false;
    for (const json& solid : updated["solids"]) {
        if (solid["component_id"] != changed) continue;
        found = true;
        CHECK(solid["source_path"] == replacement.path.string());
        CHECK(solid["volume_mm3"].get<double>() == doctest::Approx(400.0));
        CHECK(solid["centroid_mm"][0].get<double>() == doctest::Approx(2.0));
    }
    REQUIRE(found);
    REQUIRE(updated["pairs"].size() == 1);
    CHECK(updated["pairs"][0]["relation"] == "proximity");
    CHECK(updated["pairs"][0]["distance_mm"].get<double>() == doctest::Approx(4.0));
    CHECK(updated["pairs"][0]["overlap_volume_mm3"].get<double>() == doctest::Approx(0.0));
}

TEST_CASE("assembly readers invalidate saved descendant geometry without a parent edit") {
    using namespace chudware::doc;
    const Built source = build("FreshnessSource", json::array({
        box("Block", 10, 10, 10, {0, 0, 0})}));
    IntegrityFixtureFile part{
        std::filesystem::temp_directory_path() / ("freshness_part_" + source.id + ".step+")};
    IntegrityFixtureFile nested{
        std::filesystem::temp_directory_path() / ("freshness_nested_" + source.id + ".step+")};
    doc_save(source.id, part.path.string());
    const std::string child = doc_new("assembly", "FreshnessChild").at("doc_id");
    doc_apply_ops(child, -1, json::array({
        {{"op", "asm_insert_component"}, {"fixed", true},
         {"source", {{"path", part.path.string()}}}}
    }), false);
    doc_save(child, nested.path.string());
    const auto nested_time = std::filesystem::last_write_time(nested.path);
    const std::string parent = doc_new("assembly", "FreshnessParent").at("doc_id");
    doc_apply_ops(parent, -1, json::array({
        {{"op", "asm_insert_component"}, {"name", "Left"}, {"fixed", true},
         {"source", {{"path", nested.path.string()}}}},
        {{"op", "asm_insert_component"}, {"name", "Right"}, {"fixed", true},
         {"source", {{"path", nested.path.string()}}},
         {"transform", {1,0,0,15, 0,1,0,0, 0,0,1,0, 0,0,0,1}}}
    }), false);
    REQUIRE(doc_eval_interference(parent)["interferences"].empty());

    doc_apply_ops(source.id, -1, json::array({
        {{"op", "edit_feature"}, {"id", fid(source, 0)}, {"params", {{"length", 20.0}}}}
    }), false);
    doc_save(source.id, part.path.string());
    REQUIRE(std::filesystem::last_write_time(nested.path) == nested_time);

    SUBCASE("interference reads the new exact overlap") {
        const json report = doc_eval_interference(parent);
        REQUIRE(report["complete"] == true);
        REQUIRE(report["interferences"].size() == 1);
        CHECK(report["interferences"][0]["overlap_volume_mm3"].get<double>() ==
              doctest::Approx(500.0));
    }
    SUBCASE("measurement resolves against the new face") {
        const json measured = doc_eval_measure(parent, json::array({
            {{"body_id", 1}, {"of", "face"}, {"side", "right"}}
        }));
        CHECK(measured["resolved_refs"][0]["centroid"][0].get<double>() ==
              doctest::Approx(20.0));
    }
    SUBCASE("render tessellation contains the new geometry") {
        const auto bodies = doc_tessellate_bodies(parent);
        REQUIRE(bodies.size() == 2);
        double right = 0;
        for (std::size_t i = 0; i < bodies[0].pos.size(); i += 3)
            right = std::max(right, double(bodies[0].pos[i]));
        CHECK(right == doctest::Approx(20.0));
    }
    doc_close(parent);
    doc_close(child);
    doc_close(source.id);
}

TEST_CASE("cached assembly activation does not hide a deleted descendant source") {
    using namespace chudware::doc;
    const Built source = build("ActivationSource", json::array({
        box("Block", 10, 10, 10, {0, 0, 0})}));
    IntegrityFixtureFile part{
        std::filesystem::temp_directory_path() / ("activation_part_" + source.id + ".step+")};
    IntegrityFixtureFile saved{
        std::filesystem::temp_directory_path() / ("activation_parent_" + source.id + ".step+")};
    doc_save(source.id, part.path.string());
    const std::string parent = doc_new("assembly", "ActivationParent").at("doc_id");
    doc_apply_ops(parent, -1, json::array({
        {{"op", "asm_insert_component"}, {"fixed", true},
         {"source", {{"path", part.path.string()}}}}
    }), false);
    doc_save(parent, saved.path.string());
    doc_close(parent);
    const json opened = doc_open(saved.path.string());
    REQUIRE(opened["cache_hit"] == true);
    const std::string reopened = opened["doc_id"];
    REQUIRE(std::filesystem::remove(part.path));
    const json activated = doc_activate(reopened);
    REQUIRE(activated["rebuild"]["components"].size() == 1);
    CHECK(activated["rebuild"]["components"][0]["ok"] == false);
    CHECK(activated["rebuild"]["converged"] == false);
    CHECK(activated["tess"]["bodies"].empty());
    doc_close(reopened);
    doc_close(source.id);
}

TEST_CASE("model integrity: nearest-only evidence cannot replace Common or retain a prior graph") {
    Built b = build("IntegrityEvidencePromotion", json::array({
        box("A", 10, 10, 10, {0, 0, 0}, {{"merge", false}}),
        box("B", 10, 10, 10, {10.02, 0, 0}, {{"merge", false}}),
    }));
    const json distance_only = doc_eval_model_integrity(
        b.id, {{"proximity_mm", 0.01}, {"include_nearest", true}});
    REQUIRE_MESSAGE(distance_only["complete"] == true, distance_only.dump());
    REQUIRE(distance_only["summary"]["candidate_pairs"] == 0);
    REQUIRE(distance_only["nearest"].size() == 2);
    for (const json& nearest : distance_only["nearest"]) {
        CHECK(nearest["status"] == "exact");
        CHECK(nearest["distance_mm"].get<double>() == doctest::Approx(0.02));
    }
    const json params{{"proximity_mm", 0.05}, {"include_nearest", false}};
    const json full = doc_eval_model_integrity(b.id, params);
    REQUIRE_MESSAGE(full["complete"] == true, full.dump());
    // A successful distance-only result is not successful Common evidence.
    CHECK(full["summary"]["computed_exact_pairs"] == 1);
    CHECK(full["summary"]["reused_exact_pairs"] == 0);
    REQUIRE(full["pairs"].size() == 1);
    CHECK(full["pairs"][0]["relation"] == "proximity");
    CHECK(full["pairs"][0]["distance_mm"].get<double>() == doctest::Approx(0.02));
    CHECK(full["pairs"][0]["overlap_volume_mm3"].get<double>() == doctest::Approx(0.0));
    CHECK(full["islands"].size() == 1);
    CHECK(full["nearest"].empty());
    const json warm = doc_eval_model_integrity(b.id, params);
    REQUIRE_MESSAGE(warm["complete"] == true, warm.dump());
    REQUIRE(warm["summary"]["reused_exact_pairs"] == 1);
    CHECK(warm["pairs"] == full["pairs"]);

    const json disconnected = doc_eval_model_integrity(
        b.id, {{"proximity_mm", 0.01}, {"include_nearest", false}});
    REQUIRE_MESSAGE(disconnected["complete"] == true, disconnected.dump());
    CHECK(disconnected["pairs"].empty());
    CHECK(disconnected["islands"].size() == 2);
    CHECK(disconnected["isolated_solids"].size() == 2);
    CHECK(disconnected["nearest"].empty());
    const json nearest_again = doc_eval_model_integrity(
        b.id, {{"proximity_mm", 0.01}, {"include_nearest", true}});
    REQUIRE_MESSAGE(nearest_again["complete"] == true, nearest_again.dump());
    CHECK(nearest_again["pairs"].empty());
    CHECK(nearest_again["islands"].size() == 2);
    REQUIRE(nearest_again["nearest"].size() == 2);
    for (const json& nearest : nearest_again["nearest"]) {
        CHECK(nearest["status"] == "exact");
        CHECK(nearest["distance_mm"].get<double>() == doctest::Approx(0.02));
    }
}

TEST_CASE("model integrity: canceled pair work is unknown, never a clean minimum") {
    Built b = build("IntegrityTimeout", json::array({
        box("A", 10, 10, 10, {0, 0, 0}, {{"merge", false}}),
        box("B", 10, 10, 10, {5, 0, 0}, {{"merge", false}}),
    }));
    const json report = doc_eval_model_integrity(b.id, {{"pair_budget_s", 1e-12}});
    CHECK(report["ok"] == true);
    CHECK(report["complete"] == false);
    CHECK(report["pairs"].empty());
    REQUIRE(report["pair_failures"].size() == 1);
    CHECK(report["pair_failures"][0]["reason"] == "pair_timeout");
    CHECK(report["summary"]["theoretical_pairs"] == 1);
    CHECK(report["summary"]["candidate_pairs"] == 1);
    CHECK(report["summary"]["exact_pairs"] == 0);
    CHECK(report["summary"]["failed_pairs"] == 1);
    REQUIRE(report["nearest"].size() == 2);
    for (const auto& nearest : report["nearest"]) {
        CHECK(nearest["status"] == "unknown");
        CHECK(nearest["distance_mm"].is_null());
        CHECK(nearest["nearest_id"].is_null());
    }
    const json retried = doc_eval_model_integrity(b.id, {{"pair_budget_s", 30.0}});
    REQUIRE_MESSAGE(retried["complete"] == true, retried.dump());
    CHECK(retried["pair_failures"].empty());
    REQUIRE(retried["pairs"].size() == 1);
    CHECK(retried["pairs"][0]["relation"] == "overlap");
    CHECK(retried["pairs"][0]["overlap_volume_mm3"].get<double>() == doctest::Approx(500.0));
}

TEST_CASE("model integrity: empty refusal and single solid census do not claim connectivity") {
    const std::string empty = doc_new("part", "IntegrityEmpty").at("doc_id").get<std::string>();
    const json refused = doc_eval_model_integrity(empty, json::object());
    CHECK(refused["ok"] == false);
    CHECK(refused["complete"] == false);
    CHECK(refused["reason"] == "no_solids");
    Built one = build("IntegrityOne", json::array({box("A", 10, 10, 10, {0, 0, 0})}));
    const json census = doc_eval_model_integrity(one.id, json::object());
    REQUIRE_MESSAGE(census["complete"] == true, census.dump());
    REQUIRE(census["solids"].size() == 1);
    CHECK(census["solids"][0]["brep_valid"] == true);
    CHECK(census["solids"][0]["volume_mm3"].get<double>() == doctest::Approx(1000.0));
    CHECK(census["summary"]["theoretical_pairs"] == 0);
    CHECK(census["pairs"].empty());
    REQUIRE(census["nearest"].size() == 1);
    CHECK(census["nearest"][0]["status"] == "no_other_solids");
}

TEST_CASE("model integrity: one placed assembly component exposes both disconnected solids and missing source") {
    const std::string did = doc_new("assembly", "IntegrityCompound").at("doc_id").get<std::string>();
    IntegrityFixtureFile fixture{
        std::filesystem::temp_directory_path() / ("integrity_" + did + ".step")};
    chudware::doc::export_bodies({
        BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 10, 10, 10).Shape(),
        BRepPrimAPI_MakeBox(gp_Pnt(30, 0, 0), 10, 10, 10).Shape()
    }, fixture.path.string(), "step");
    doc_apply_ops(did, -1, json::array({
        {{"op", "asm_insert_component"}, {"name", "Disconnected"},
         {"source", {{"path", fixture.path.string()}}}}
    }), false);
    const json tree = chudware::doc::doc_tree(did);
    const json& tree_body = tree.contains("tree") ? tree["tree"] : tree;
    const std::string cid = tree_body.at("components").at(0).at("id").get<std::string>();
    // Quarter-turn about Z followed by translation: local +X separation becomes world +Y.
    doc_apply_ops(did, -1, json::array({
        {{"op", "asm_edit_component"}, {"id", cid},
         {"transform", {0, -1, 0, 100, 1, 0, 0, 200, 0, 0, 1, 300, 0, 0, 0, 1}}}
    }), false);
    const json report = doc_eval_model_integrity(did, json::object());
    REQUIRE_MESSAGE(report["complete"] == true, report.dump());
    REQUIRE(report["solids"].size() == 2);
    CHECK(report["pairs"].empty());
    CHECK(report["isolated_solids"].size() == 2);
    CHECK(report["islands"].size() == 2);
    for (const json& solid : report["solids"]) {
        CHECK(solid["component_id"] == cid);
        CHECK(solid["source_path"] == fixture.path.string());
        CHECK(solid["body_id"] == 1);
        CHECK(solid["volume_mm3"].get<double>() == doctest::Approx(1000.0));
        CHECK(solid["centroid_mm"][0].get<double>() == doctest::Approx(95.0));
        CHECK(solid["centroid_mm"][2].get<double>() == doctest::Approx(305.0));
    }
    const double y0 = report["solids"][0]["centroid_mm"][1].get<double>();
    const double y1 = report["solids"][1]["centroid_mm"][1].get<double>();
    CHECK(std::min(y0, y1) == doctest::Approx(205.0));
    CHECK(std::max(y0, y1) == doctest::Approx(235.0));
    REQUIRE(report["nearest"].size() == 2);
    CHECK(report["nearest"][0]["distance_mm"].get<double>() == doctest::Approx(20.0));
    const json health = chudware::doc::doc_eval_solid_health(did, 1);
    REQUIRE(health["bodies"].size() == 1);
    CHECK(health["bodies"][0]["solids"] == 2);
    CHECK(health["bodies"][0]["body_id"] == 1);
    CHECK(chudware::doc::doc_eval_check(did, 1)["issues"].empty());

    std::filesystem::remove(fixture.path);
    const json missing = doc_eval_model_integrity(did, json::object());
    CHECK(missing["ok"] == false);
    CHECK(missing["complete"] == false);
    CHECK(missing["reason"] == "no_solids");
    REQUIRE(missing["source_failures"].size() == 1);
    CHECK(missing["assembly"]["components"][0]["status"] == "failed");
}

TEST_CASE("assembly health: mating surfaces are not self-piercing solid skins") {
    const std::string did = doc_new("assembly", "MatingSolidHealth").at("doc_id").get<std::string>();
    IntegrityFixtureFile fixture{
        std::filesystem::temp_directory_path() / ("mating_health_" + did + ".step")};
    chudware::doc::export_bodies({
        BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 10, 10, 10).Shape(),
        BRepPrimAPI_MakeBox(gp_Pnt(10, 0, 0), 10, 10, 10).Shape()
    }, fixture.path.string(), "step");
    doc_apply_ops(did, -1, json::array({
        {{"op", "asm_insert_component"}, {"name", "MatingParts"},
         {"source", {{"path", fixture.path.string()}}}}
    }), false);
    const json health = chudware::doc::doc_eval_solid_health(did, 0);
    REQUIRE(health["bodies"].size() == 1);
    const json& body = health["bodies"][0];
    CHECK(body["solids"] == 2);
    CHECK(body["self_intersection"]["checked"] == true);
    CHECK(body["self_intersection"]["self_intersecting"] == false);
    CHECK(body["sound"] == true);
    const json census = doc_eval_model_integrity(did, json::object());
    REQUIRE(census["complete"] == true);
    REQUIRE(census["pairs"].size() == 1);
    CHECK(census["pairs"][0]["distance_mm"].get<double>() == doctest::Approx(0.0));
    CHECK(census["pairs"][0]["overlap_volume_mm3"].get<double>() == doctest::Approx(0.0));
    CHECK(census["islands"].size() == 1);
}

TEST_CASE("solid health: adjacent fillet skins are invariant under rigid placement") {
    // Mechanically reduced from a valid 51-face native solid to its two adjacent
    // radius-0.7 cylindrical fillet faces. Original curves/pcurves and tolerances
    // are retained; there is no healing or project-file dependency. OCCT's world
    // CheckerSI reports a face-face interference after a 4e-9-radian Y rotation
    // and -6mm Z shift, but not in the identical TShape's authoring coordinates.
    TopoDS_Shape skin;
    BRep_Builder builder;
    const auto fixture = std::filesystem::path(__FILE__).parent_path() /
                         "fixtures/placed_fillet_skin.brep";
    REQUIRE(BRepTools::Read(skin, fixture.string().c_str(), builder));
    const json local = imported_shape_health(skin);
    REQUIRE(local["faces"] == 2);
    REQUIRE(local["self_intersection"]["checked"] == true);
    CHECK(local["self_intersection"]["interference_count"] == 0);
    gp_Trsf placement;
    placement.SetRotation(gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(0, 1, 0)), 4e-9);
    placement.SetTranslationPart(gp_Vec(0, 0, -6));
    const TopoDS_Shape world = skin.Moved(TopLoc_Location(placement));
    REQUIRE(world.IsPartner(skin));  // placement did not reconstruct geometry
    REQUIRE(world.Located(skin.Location()).IsSame(skin));
    const json placed = imported_shape_health(world);
    CHECK(placed["self_intersection"]["checked"] == true);
    CHECK(placed["self_intersection"]["self_intersecting"] == false);
    CHECK(placed["self_intersection"]["interference_count"] == 0);
    CHECK(placed["self_intersection"]["scope"] == "surface_body");
    CHECK(placed["area_mm2"].get<double>() ==
          doctest::Approx(local["area_mm2"].get<double>()));

    const std::string did = doc_new("assembly", "PlacedFilletHealth").at("doc_id").get<std::string>();
    doc_apply_ops(did, -1, json::array({
        {{"op", "asm_insert_component"}, {"name", "FilletSkin"},
         {"source", {{"path", std::filesystem::absolute(fixture).string()}}},
         {"transform", {placement.Value(1, 1), placement.Value(1, 2), placement.Value(1, 3), 0,
                        placement.Value(2, 1), placement.Value(2, 2), placement.Value(2, 3), 0,
                        placement.Value(3, 1), placement.Value(3, 2), placement.Value(3, 3), -6,
                        0, 0, 0, 1}}}
    }), false);
    const json assembly = chudware::doc::doc_eval_solid_health(did, 0);
    REQUIRE(assembly["bodies"].size() == 1);
    CHECK(assembly["bodies"][0]["self_intersection"]["checked"] == true);
    CHECK(assembly["bodies"][0]["self_intersection"]["interference_count"] == 0);
    CHECK(assembly["bodies"][0]["area_mm2"].get<double>() ==
          doctest::Approx(local["area_mm2"].get<double>()));
    chudware::doc::doc_close(did);
}

TEST_CASE("solid health: proper rigid placement preserves full self-intersection checks") {
    const TopoDS_Shape valid = BRepPrimAPI_MakeBox(10, 10, 10).Shape();
    const TopoDS_Shape invalid = folded_health_prism();
    std::vector<gp_Trsf> placements(3);
    placements[1].SetRotation(gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(0, 1, 0)), 4e-9);
    placements[1].SetTranslationPart(gp_Vec(0, 0, -6));
    placements[2].SetRotation(gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(.23, .71, .48)), .73);
    placements[2].SetTranslationPart(gp_Vec(100, -200, 300));
    for (const gp_Trsf& placement : placements) {
        const json good = imported_shape_health(valid.Moved(TopLoc_Location(placement)));
        CHECK(good["self_intersection"]["checked"] == true);
        CHECK(good["self_intersection"]["self_intersecting"] == false);
        CHECK(good["self_intersection"]["interference_count"] == 0);
        CHECK(good["self_intersection"]["constituents_checked"] == 1);
        CHECK(good["volume_mm3"].get<double>() == doctest::Approx(1000));
        CHECK(good["sound"] == true);

        const json bad = imported_shape_health(invalid.Moved(TopLoc_Location(placement)));
        CHECK(bad["self_intersection"]["checked"] == true);
        CHECK(bad["self_intersection"]["self_intersecting"] == true);
        CHECK(bad["self_intersection"]["interference_count"].get<int>() > 0);
        CHECK(bad["self_intersection"]["constituents_checked"] == 1);
        CHECK(bad["self_intersection"]["scope"] == "within_each_solid");
        CHECK(bad["free_edges"]["count"] == 0);
        CHECK(bad["non_manifold_edges"]["count"] == 0);
        CHECK(bad["volume_positive"] == true);
        CHECK(bad["watertight"] == true);
        CHECK(bad["sound"] == false);
    }
}

TEST_CASE("solid health: scaled and sheared geometry retains genuine intersections") {
    const TopoDS_Shape invalid = folded_health_prism();
    gp_Trsf scale;
    scale.SetScale(gp_Pnt(0, 0, 0), 2);
    const TopoDS_Shape scaled = BRepBuilderAPI_Transform(invalid, scale, true).Shape();
    gp_GTrsf shear;
    shear.SetValue(1, 1, 2);
    shear.SetValue(1, 2, .4);
    const TopoDS_Shape sheared = BRepBuilderAPI_GTransform(invalid, shear, true).Shape();
    gp_Trsf placement;
    placement.SetRotation(gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(1, 2, 3)), .43);
    placement.SetTranslationPart(gp_Vec(-80, 40, 120));
    for (const TopoDS_Shape& shape : {scaled, sheared}) {
        const json body = imported_shape_health(shape.Moved(TopLoc_Location(placement)));
        CHECK(body["self_intersection"]["checked"] == true);
        CHECK(body["self_intersection"]["self_intersecting"] == true);
        CHECK(body["self_intersection"]["interference_count"].get<int>() > 0);
        CHECK(body["sound"] == false);
    }
}

TEST_CASE("solid health: tangent fused stock imprints do not open the skin") {
    // A round base overlaps both rectangular ears. Their outside faces are tangent
    // to the cylinder, leaving internal imprint edges inside otherwise closed faces.
    const Built fork = build("TangentForkHealth", json::array({
        cyl("Base", 19.0, 5.0, {0, 0, 0},
            {{"axis", {0, -1, 0}}, {"centered", true}, {"merge", false}}),
        box("LeftEar", 5.0, 22.0, 16.0, {-16.5, -9.5, 0},
            {{"centered", true}, {"merge", true}, {"mode", "add"}}),
        box("RightEar", 5.0, 22.0, 16.0, {16.5, -9.5, 0},
            {{"centered", true}, {"merge", true}, {"mode", "add"}})
    }));
    const json health = chudware::doc::doc_eval_solid_health(fork.id, 1);
    const json& body = health.at("bodies").at(0);
    REQUIRE(body["brep_valid"] == true);
    CHECK(body["free_edges"]["count"] == 0);
    CHECK(body["is_watertight"] == true);
    CHECK(body["sound"] == true);
    const json checked = chudware::doc::doc_eval_check(fork.id, 1);
    CHECK(checked["issues"].empty());
    chudware::doc::doc_close(fork.id);
}

TEST_CASE("model integrity: two patterns of one seed keep distinct world identities and graph nodes") {
    const std::string did = doc_new("assembly", "IntegrityPatterns").at("doc_id").get<std::string>();
    IntegrityFixtureFile fixture{
        std::filesystem::temp_directory_path() / ("integrity_patterns_" + did + ".step")};
    chudware::doc::export_bodies(
        {BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 10, 10, 10).Shape()},
        fixture.path.string(), "step");
    doc_apply_ops(did, -1, json::array({
        {{"op", "asm_insert_component"}, {"name", "Seed"},
         {"source", {{"path", fixture.path.string()}}}}
    }), false);
    const json tree = chudware::doc::doc_tree(did);
    const json& tree_body = tree.contains("tree") ? tree["tree"] : tree;
    const std::string cid = tree_body.at("components").at(0).at("id").get<std::string>();
    json ops = json::array();
    for (double spacing : {10.0, 20.0})
        ops.push_back({{"op", "add_feature"},
            {"feature", {{"type", "asm_linear_pattern"}, {"name", "Pattern"},
                {"refs", {{"components", json::array({cid})}, {"dir1", "axis.x"}}},
                {"params", {{"count1", 2}, {"spacing1", spacing}}}}}});
    doc_apply_ops(did, -1, ops, false);

    const json report = doc_eval_model_integrity(did, json::object());
    REQUIRE_MESSAGE(report["complete"] == true, report.dump());
    REQUIRE(report["solids"].size() == 3);
    std::set<std::string> ids;
    std::set<int> body_ids;
    int pattern_labels = 0;
    for (const auto& solid : report["solids"]) {
        CHECK(ids.insert(solid["id"].get<std::string>()).second);
        body_ids.insert(solid["body_id"].get<int>());
        CHECK(solid["source_path"] == fixture.path.string());
        CHECK(solid["source_ordinal"] == 1);
        if (solid["component_id"] == cid + "#1") ++pattern_labels;
    }
    CHECK(pattern_labels == 2);  // provenance labels can repeat; world identities cannot
    REQUIRE(report["pairs"].size() == 2);
    for (const auto& pair : report["pairs"]) {
        CHECK(pair["a"] != pair["b"]);
        CHECK(ids.count(pair["a"].get<std::string>()) == 1);
        CHECK(ids.count(pair["b"].get<std::string>()) == 1);
    }
    REQUIRE(report["islands"].size() == 1);
    const auto island_ids = report["islands"][0]["solids"].get<std::vector<std::string>>();
    CHECK(std::set<std::string>(island_ids.begin(), island_ids.end()) == ids);
    CHECK(report["isolated_solids"].empty());

    const json health = chudware::doc::doc_eval_solid_health(did, 0);
    REQUIRE(health["bodies"].size() == 3);
    std::set<int> health_ids;
    for (const auto& body : health["bodies"]) health_ids.insert(body["body_id"].get<int>());
    CHECK(health_ids == body_ids);
    const int pattern_id = *body_ids.rbegin();
    REQUIRE(pattern_id >= 1000000);
    const json selected = chudware::doc::doc_eval_solid_health(did, pattern_id);
    REQUIRE(selected["bodies"].size() == 1);
    CHECK(selected["bodies"][0]["body_id"] == pattern_id);
    CHECK(selected["bodies"][0]["volume_mm3"].get<double>() == doctest::Approx(1000.0));
    const json checked = chudware::doc::doc_eval_check(did, pattern_id);
    CHECK(checked["body_id"] == pattern_id);
    CHECK(checked["issues"].empty());
    CHECK(chudware::doc::doc_eval_check(did, 0)["body_id"] == pattern_id);
    CHECK_THROWS(chudware::doc::doc_eval_check(did, 2));  // no dense alias for an instance
    CHECK_THROWS(chudware::doc::doc_eval_solid_health(did, 2));
}

TEST_CASE("assembly health: surviving surface is inspected and missing sources prevent a clean aggregate") {
    const std::string did = doc_new("assembly", "IntegritySurfaces").at("doc_id").get<std::string>();
    IntegrityFixtureFile solid_file{
        std::filesystem::temp_directory_path() / ("integrity_solid_" + did + ".step")};
    IntegrityFixtureFile surface_file{
        std::filesystem::temp_directory_path() / ("integrity_surface_" + did + ".step")};
    chudware::doc::export_bodies(
        {BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 10, 10, 10).Shape()},
        solid_file.path.string(), "step");
    chudware::doc::export_bodies(
        {BRepBuilderAPI_MakeFace(gp_Pln(gp_Pnt(30, 0, 0), gp_Dir(0, 0, 1)),
                                0, 10, 0, 10).Shape()},
        surface_file.path.string(), "step");
    doc_apply_ops(did, -1, json::array({
        {{"op", "asm_insert_component"}, {"name", "Solid"},
         {"source", {{"path", solid_file.path.string()}}}},
        {{"op", "asm_insert_component"}, {"name", "Surface"},
         {"source", {{"path", surface_file.path.string()}}}}
    }), false);

    const json census = doc_eval_model_integrity(did, json::object());
    REQUIRE(census["solids"].size() == 1);
    REQUIRE(census["non_solids"].size() == 1);
    const json interference = doc_eval_interference(did, {{"clearance_mode", "threshold"}});
    CHECK(interference.at("ok") == false);
    CHECK(interference.at("complete") == false);
    CHECK(interference.at("pairs_total") == 1);
    CHECK(interference.at("pairs_checked") == 0);
    CHECK(interference.at("clearances").empty());
    REQUIRE(interference.at("pair_failures").size() == 1);
    CHECK(interference.at("pair_failures")[0].at("reason") == "member_geometry_unavailable");
    REQUIRE(interference.at("geometry_failures").size() == 1);
    CHECK(interference.at("geometry_failures")[0].at("name") == "Surface");
    const int surface_id = census["non_solids"][0]["body_id"].get<int>();
    const json health = chudware::doc::doc_eval_solid_health(did, 0);
    REQUIRE(health["bodies"].size() == 2);
    CHECK(health["all_sound"] == false);
    const json surface = chudware::doc::doc_eval_solid_health(did, surface_id);
    REQUIRE(surface["bodies"].size() == 1);
    CHECK(surface["bodies"][0]["body_id"] == surface_id);
    CHECK(surface["bodies"][0]["solids"] == 0);
    CHECK(surface["bodies"][0]["faces"] == 1);
    CHECK(surface["bodies"][0]["sound"] == false);
    const json checked = chudware::doc::doc_eval_check(did, surface_id);
    CHECK(std::any_of(checked["issues"].begin(), checked["issues"].end(),
        [](const json& issue) { return issue["kind"] == "free_edges"; }));

    // Delete only the surface source: the solid survives, but is not the whole model.
    std::filesystem::remove(surface_file.path);
    const json partial = chudware::doc::doc_eval_solid_health(did, 0);
    REQUIRE(partial["bodies"].size() == 1);
    CHECK(partial["bodies"][0]["sound"] == true);
    CHECK(partial["complete"] == false);
    CHECK(partial["all_sound"] == false);
    CHECK(partial["all_watertight"] == false);
    CHECK(std::any_of(partial["source_failures"].begin(), partial["source_failures"].end(),
        [](const json& failure) { return failure.contains("component_id"); }));
    const json partial_check = chudware::doc::doc_eval_check(did, 1);
    CHECK(partial_check["complete"] == false);
    CHECK(partial_check["source_failures"] == partial["source_failures"]);
}

TEST_CASE("invalid solid topology always produces an evaluate-check error") {
    const std::string id = doc_new("part", "InvalidOrientation").at("doc_id").get<std::string>();
    IntegrityFixtureFile fixture{
        std::filesystem::temp_directory_path() / ("invalid_orientation_" + id + ".brep")};
    const TopoDS_Shape cube = BRepPrimAPI_MakeBox(10.0, 10.0, 10.0).Shape();
    BRep_Builder builder;
    TopoDS_Shell shell;
    builder.MakeShell(shell);
    bool reverse = true;
    for (TopExp_Explorer it(cube, TopAbs_FACE); it.More(); it.Next()) {
        TopoDS_Shape face = it.Current();
        if (reverse) face.Reverse();
        reverse = false;
        builder.Add(shell, face);
    }
    TopoDS_Solid invalid;
    builder.MakeSolid(invalid);
    builder.Add(invalid, shell);
    REQUIRE(BRepTools::Write(invalid, fixture.path.string().c_str()));
    chudware::doc::doc_import_file(fixture.path.string(), id, false, "brep");
    const json health = chudware::doc::doc_eval_solid_health(id, 1);
    REQUIRE(health["bodies"][0]["brep_valid"] == false);
    const json interference = doc_eval_interference(id, {{"clearance_mode", "threshold"}});
    CHECK(interference.at("ok") == false);
    CHECK(interference.at("complete") == false);
    REQUIRE(interference.at("geometry_failures").size() == 1);
    CHECK(interference.at("geometry_failures")[0].at("reason") == "invalid_geometry");
    const json checked = chudware::doc::doc_eval_check(id, 1);
    CHECK(std::any_of(checked["issues"].begin(), checked["issues"].end(),
        [](const json& issue) {
            return issue["kind"] == "brep_check" && issue["severity"] == "error";
        }));
    chudware::doc::doc_close(id);
}

// eval_feature_attachment: the "floating / overhanging rib" defect class. Plate
// [0,40]x[0,40]x[0,4]; ribs sit on its top face (z=4). Every verdict below is closed-form:
//   Good     [10,12]x[10,30]  fully on the plate           -> attached (never flagged)
//   Overhang [30,32]x[30,50]  half its base past y=40       -> overhang, ~50 %, reach ~10 mm
//   Floating [20,24]x[2,6]x[12,16]  8 mm above the plate    -> floating, gap 8
//   Touching [40,44]x[0,4]x[4,8]  meets the plate on one edge -> barely_touching
//   Solo     mode:"new" standalone body                    -> independent, not flagged
TEST_CASE("eval_feature_attachment: floating, overhanging and edge-touching features are flagged") {
    const Built b = build("attach_ribs", json::array({
        box("Plate", 40, 40, 4, {0, 0, 0}),
        box("Good", 2, 20, 8, {10, 10, 4}),
        box("Overhang", 2, 20, 8, {30, 30, 4}),
        box("Floating", 4, 4, 4, {20, 2, 12}),
        box("Touching", 4, 4, 4, {40, 0, 4}),
        box("Solo", 5, 5, 5, {80, 0, 0}, json{{"mode", "new"}}),
    }));
    const std::string good = fid(b, 1), over = fid(b, 2), flo = fid(b, 3), touch = fid(b, 4),
                      solo = fid(b, 5);
    const json res = chudware::doc::doc_eval_feature_attachment(b.id);
    INFO(res.dump(2));
    CHECK(res.at("ok") == false);
    std::map<std::string, json> by_id;
    for (const json& e : res.at("features")) by_id[e.at("feature_id").get<std::string>()] = e;
    CHECK_FALSE(by_id.count(good));
    CHECK(std::find(res.at("attached").begin(), res.at("attached").end(), json(good)) !=
          res.at("attached").end());
    CHECK(std::find(res.at("independent").begin(), res.at("independent").end(), json(solo)) !=
          res.at("independent").end());
    CHECK_FALSE(by_id.count(solo));

    REQUIRE(by_id.count(over));
    const json& o = by_id[over];
    CHECK(o.at("verdict") == "overhang");
    CHECK(o.at("overhang_frac").get<double>() == doctest::Approx(0.5).epsilon(0.06));
    CHECK(o.at("overhang_mm").get<double>() == doctest::Approx(10.0).epsilon(0.1));
    CHECK(o.at("location").at(1).get<double>() > 40.0);   // the unsupported half
    CHECK(o.at("location").at(2).get<double>() == doctest::Approx(4.0).epsilon(1e-6));

    REQUIRE(by_id.count(flo));
    CHECK(by_id[flo].at("verdict") == "floating");
    CHECK(by_id[flo].at("gap_mm").get<double>() == doctest::Approx(8.0).epsilon(1e-6));

    REQUIRE(by_id.count(touch));
    CHECK(by_id[touch].at("verdict") == "barely_touching");

    // Restricting to the new batch (add_features' use) reports only those ids.
    const json only = chudware::doc::doc_eval_feature_attachment(
        b.id, json{{"features", json::array({good})}});
    CHECK(only.at("ok") == true);
    CHECK(only.at("features").empty());
    chudware::doc::doc_close(b.id);
}

TEST_CASE("eval_feature_attachment: a part whose ribs all sit on the plate passes") {
    const Built b = build("attach_ok", json::array({
        box("Plate", 40, 40, 4, {0, 0, 0}),
        box("RibA", 2, 30, 6, {5, 5, 4}),
        box("RibB", 30, 2, 6, {5, 20, 4}),
    }));
    const json res = chudware::doc::doc_eval_feature_attachment(b.id);
    INFO(res.dump(2));
    CHECK(res.at("ok") == true);
    CHECK(res.at("features").empty());
    CHECK(res.at("attached_count") == 2);
    CHECK(res.at("solid_count") == 1);
    chudware::doc::doc_close(b.id);
}

// A later shell hollows the part under a boss: the boss's base disk now sits mostly over the
// cavity, but that part of the boss is gone too (the shell carved the boss into a 2 mm cup).
// Only the boss's surviving ring rests on the box's top wall, and it is fully supported —
// removed material is not an overhang (a collar over a shelled duct read 89 % "overhang").
TEST_CASE("eval_feature_attachment: a boss over a later shell's cavity is attached") {
    json shell = json{{"op", "add_feature"}, {"feature",
        json{{"type", "shell"}, {"name", "Hollow"},
             {"params", {{"thickness", 2.0}}},
             {"refs", {{"remove", {{"of", "face"}, {"side", "bottom"}}}}}}}};
    const Built b = build("attach_shell", json::array({
        box("Body", 40, 40, 20, {0, 0, 0}),
        cyl("Boss", 8, 10, {20, 20, 20}),
        shell,
    }));
    const std::string boss = fid(b, 1);
    const json res = chudware::doc::doc_eval_feature_attachment(b.id);
    INFO(res.dump(2));
    CHECK(res.at("ok") == true);
    CHECK(res.at("features").empty());
    CHECK(std::find(res.at("attached").begin(), res.at("attached").end(), json(boss)) !=
          res.at("attached").end());
    chudware::doc::doc_close(b.id);
}

namespace {

bool in_list(const json& arr, const std::string& id) {
    return std::find(arr.begin(), arr.end(), json(id)) != arr.end();
}

// A sketch on plane.top at height `z` holding two concentric circles (an annulus profile).
json annulus_sketch(const char* name, double cx, double cy, double r_out, double r_in,
                    double z) {
    auto circle = [&](const char* id, double r) {
        return json{{"id", id}, {"type", "circle"}, {"construction", false},
                    {"center", json::array({cx, cy})}, {"radius", r}};
    };
    return json{{"op", "add_feature"}, {"feature",
        json{{"type", "sketch"}, {"name", name},
             {"params", {{"entities", json::array({circle("e1", r_out), circle("e2", r_in)})},
                         {"constraints", json::array()}, {"offset", z}}},
             {"refs", {{"plane", "plane.top"}}}}}};
}

// A blind boss extrude of every material region of the sketch created by batch op `sketch`.
json extrude_boss(const char* name, const char* sketch, double depth) {
    return json{{"op", "add_feature"}, {"feature",
        json{{"type", "extrude"}, {"name", name},
             {"params", {{"mode", "boss"}, {"end", "blind"},
                         {"depth", json{{"value", depth}, {"expr", nullptr}}}}},
             {"refs", {{"profile", json::array({json{{"kind", "region"},
                                                     {"sketch", sketch}}})}}}}}};
}

}  // namespace

// Defect 5 (6-axis session, tool_flange f24). The prior solid fills z 307.5..309.2 over a
// 7.6 mm pocket ceiling with a R1.2 bore. An annulus R3.6/R1.2 sketched at z 307.6 and
// extruded 2.6 mm stands 1.0 mm proud of the z 309.2 top face; its foot is 100 % on solid.
// The old base pick took the outer wall (coarse 36.75 mm^2 buried band > 36.19 mm^2 foot)
// and reported "39 % (22.9 of 58.8 mm^2) base unsupported". The foot rule must report the
// proud boss attached, and must still flag a floating annulus and one whose foot hangs half
// off the plate edge.
TEST_CASE("eval_feature_attachment: a proud boss whose foot sits on solid is attached") {
    const Built b = build("attach_proud_boss", json::array({
        box("Plate", 20, 20, 4.2, {-10, -10, 305}),                      // z 305..309.2
        box("Pocket", 7.6, 7.6, 2.5, {-3.8, -3.8, 305}, json{{"mode", "cut"}}),
        cyl("Bore", 1.2, 4.2, {0, 0, 305}, json{{"mode", "cut"}}),
        annulus_sketch("ProudSk", 0, 0, 3.6, 1.2, 307.6),
        extrude_boss("ProudBoss", "$3", 2.6),                            // z 307.6..310.2
        annulus_sketch("FloatSk", -6, -6, 3.6, 1.2, 312.0),
        extrude_boss("FloatBoss", "$5", 2.6),
        annulus_sketch("EdgeSk", 10, 6, 3.6, 1.2, 309.2),
        extrude_boss("EdgeBoss", "$7", 2.6),
    }));
    const std::string proud = fid(b, 4), floating = fid(b, 6), edge = fid(b, 8);
    const json res = chudware::doc::doc_eval_feature_attachment(b.id);
    INFO(res.dump(2));
    std::map<std::string, json> by_id;
    for (const json& e : res.at("features")) by_id[e.at("feature_id").get<std::string>()] = e;

    CHECK_FALSE(by_id.count(proud));
    CHECK(in_list(res.at("attached"), proud));

    REQUIRE(by_id.count(floating));
    CHECK(by_id[floating].at("verdict") == "floating");
    CHECK(by_id[floating].at("gap_mm").get<double>() > 1.0);

    REQUIRE(by_id.count(edge));
    const json& o = by_id[edge];
    CHECK(o.at("verdict") == "overhang");
    CHECK(o.at("overhang_frac").get<double>() == doctest::Approx(0.5).epsilon(0.1));
    // The measured face is the foot (normal -Z at z 309.2), not a side wall.
    REQUIRE(o.contains("base_face_normal"));
    CHECK(o.at("base_face_normal").at(2).get<double>() == doctest::Approx(-1.0).epsilon(1e-6));
    CHECK(o.at("base_face_center").at(2).get<double>() == doctest::Approx(309.2).epsilon(1e-6));
    CHECK(o.at("detail").get<std::string>().find("foot face") != std::string::npos);
    chudware::doc::doc_close(b.id);
}

// Defect 5 tie-break (6-axis session, claw_base f59/f60). Two tabs are exact mirror images
// about x = 0. Each stands in a channel: the outer wall backs its outer face over the full
// height, the inner wall has a 2.2 mm gap that the coarse 8x8 grid misses but the 36x36 grid
// sees (8 % of the face). The old pick took the FIRST of the two equal coarse faces, which is
// the outer face on one tab and the inner face on the other: one tab passed and its twin
// overhung. Both tabs must get the same verdict, from the best-supported face.
TEST_CASE("eval_feature_attachment: mirrored twins get the same verdict") {
    const Built b = build("attach_mirror_twins", json::array({
        box("Plate", 24, 30, 10, {-12, -5, 0}),
        box("OuterR", 2, 20, 5, {6, 0, 10}),
        box("InnerR1", 2, 8.9, 5, {2, 0, 10}),
        box("InnerR2", 2, 8.9, 5, {2, 11.1, 10}),
        box("OuterL", 2, 20, 5, {-8, 0, 10}),
        box("InnerL1", 2, 8.9, 5, {-4, 0, 10}),
        box("InnerL2", 2, 8.9, 5, {-4, 11.1, 10}),
        box("TabR", 2, 20, 7, {4, 0, 8}),
        box("TabL", 2, 20, 7, {-6, 0, 8}),
    }));
    const std::string tab_r = fid(b, 7), tab_l = fid(b, 8);
    const json res = chudware::doc::doc_eval_feature_attachment(b.id);
    INFO(res.dump(2));
    std::map<std::string, json> by_id;
    for (const json& e : res.at("features")) by_id[e.at("feature_id").get<std::string>()] = e;
    CHECK(by_id.count(tab_r) == by_id.count(tab_l));
    CHECK_FALSE(by_id.count(tab_r));
    CHECK_FALSE(by_id.count(tab_l));
    CHECK(in_list(res.at("attached"), tab_r));
    CHECK(in_list(res.at("attached"), tab_l));
    CHECK(res.at("ok") == true);
    chudware::doc::doc_close(b.id);
}

// Defect 6 (6-axis session, forearm f56/f66 vs f72). A boss is built on a body and a later
// in-place `transform` lifts the whole body 30 mm. A standalone box then fills the space the
// boss had BEFORE the move. The tool records must follow the body: no boss_overlap with the
// box, and the moved boss still reads as attached to the moved body.
TEST_CASE("eval_feature_overlap: tools moved by a later transform are checked as built") {
    const Built b = build("overlap_after_transform", json::array({
        box("Body", 20, 20, 20, {0, 0, 0}),
        cyl("Boss", 3, 5, {10, 10, 20}),                                 // z 20..25
        json{{"op", "add_feature"},
             {"feature", json{{"type", "transform"}, {"name", "Lift"},
                              {"params", {{"translate", json::array({0.0, 0.0, 30.0})}}}}}},
        box("Filler", 10, 10, 5, {5, 5, 20}, json{{"merge", false}}),   // the boss's old place
    }));
    const std::string boss = fid(b, 1), filler = fid(b, 3);
    const json ov = doc_eval_feature_overlap(b.id);
    INFO(ov.dump(2));
    for (const json& p : ov.at("pairs")) {
        const bool boss_filler = (p.at("a") == boss && p.at("b") == filler) ||
                                 (p.at("a") == filler && p.at("b") == boss);
        CHECK_FALSE_MESSAGE(boss_filler, "stale pre-transform tool: " << p.dump());
    }
    CHECK(ov.at("pairs").empty());

    const json at = chudware::doc::doc_eval_feature_attachment(b.id);
    INFO(at.dump(2));
    CHECK(in_list(at.at("attached"), boss));
    CHECK(in_list(at.at("independent"), filler));
    CHECK(at.at("ok") == true);
    chudware::doc::doc_close(b.id);
}

namespace {
struct HistoryPart {
    std::string id;
    explicit HistoryPart(const char* name)
        : id(doc_new("part", name).at("doc_id").get<std::string>()) {}
    ~HistoryPart() { chudware::doc::doc_close(id); }
};

json history_plane(const char* name, const json& base = "plane.top", const json& offset = 10.0) {
    return {{"op", "add_feature"}, {"feature", {
        {"type", "plane"}, {"name", name}, {"params", {{"method", "offset"}, {"offset", offset}}},
        {"refs", {{"base", base}}}}}};
}

bool history_has(const json& report, const std::string& code, const std::string& id) {
    for (const auto& finding : report.at("findings"))
        if (finding.value("code", std::string()) == code)
            for (const auto& feature : finding.at("feature_ids"))
                if (feature == id) return true;
    return false;
}

bool history_safe(const json& report, const std::string& id) {
    for (const auto& fix : report.at("safe_fixes"))
        for (const auto& feature : fix.at("feature_ids"))
            if (feature == id) return true;
    return false;
}

bool history_blocked(const json& report, const std::string& id) {
    for (const auto& fix : report.at("blocked_fixes"))
        for (const auto& feature : fix.at("feature_ids"))
            if (feature == id) return true;
    return false;
}

bool history_dirty(const std::string& id) {
    const json docs = chudware::doc::doc_list();
    for (const auto& d : docs.at("docs"))
        if (d.at("doc_id") == id) return d.at("dirty").get<bool>();
    throw std::runtime_error("test document not listed");
}

void append_history_counter_pair(json& features, const std::string& cylinder_id,
                                 const std::string& cone_id, const json& at,
                                 const json& cylinder_axis, const json& cone_axis) {
    features.push_back({{"id",cylinder_id}, {"type","cylinder"}, {"params",{
        {"at",at}, {"axis",cylinder_axis}, {"centered",false}, {"radius",1.7},
        {"height",3.6}, {"mode","cut"}, {"merge",true}}}});
    features.push_back({{"id",cone_id}, {"type","cone"}, {"params",{
        {"at",at}, {"axis",cone_axis}, {"centered",false}, {"r1",3.4}, {"r2",1.7},
        {"height",1.62}, {"mode","cut"}, {"merge",true}}}});
}

json history_counter_document() {
    return {{"schema","aicad.part/1"}, {"name","HistoryCounterFixture"},
            {"features",json::array()}};
}

json history_triangle() {
    return {{"entities", json::array({
                {{"id","e1"}, {"type","line"}, {"p1",{0,0}}, {"p2",{10,0}}},
                {{"id","e2"}, {"type","line"}, {"p1",{10,0}}, {"p2",{0,10}}},
                {{"id","e3"}, {"type","line"}, {"p1",{0,10}}, {"p2",{0,0}}}})},
            {"constraints", json::array({
                {{"id","c1"}, {"type","coincident"}, {"refs",{"e1.p2","e2.p1"}}},
                {{"id","c2"}, {"type","coincident"}, {"refs",{"e2.p2","e3.p1"}}},
                {{"id","c3"}, {"type","coincident"}, {"refs",{"e3.p2","e1.p1"}}}})}};
}
}

TEST_CASE("model history: unused datum cleanup is one atomic undoable edit") {
    HistoryPart part("HistoryCleanupSafe");
    const json built = doc_apply_ops(part.id, -1, json::array({
        box("Base", 20, 20, 10, {0, 0, 0}), history_plane("UnneededPlane")
    }), false);
    const std::string plane = built.at("id_map").at("$1");
    const json before = built.at("tree");
    const long revision = built.at("revision");
    const json report = chudware::doc::doc_eval_model_history(part.id);
    REQUIRE(report.at("ok") == true);
    CHECK(report.at("complete") == true);
    CHECK(report.at("revision") == revision);
    CHECK(history_has(report, "unused_datum", plane));
    REQUIRE(history_safe(report, plane));
    CHECK(built.at("rebuild").at("doc_status").at("model_history") == built.at("model_history"));
    CHECK(history_has(built.at("model_history"), "unused_datum", plane));

    const json cleaned = doc_apply_ops(part.id, revision, json::array({
        {{"op", "simplify_history"}, {"features", json::array({plane})}}
    }), false);
    CHECK(cleaned.at("revision") == revision + 1);
    REQUIRE(cleaned.at("tree").at("features").size() == 1);
    CHECK(cleaned.at("tree").at("features")[0].at("id") == built.at("id_map").at("$0"));
    CHECK(cleaned.at("tree").at("material") == before.at("material"));
    CHECK(cleaned.at("volume").get<double>() == doctest::Approx(built.at("volume").get<double>()));
    const json after = cleaned.at("tree");
    CHECK(chudware::doc::doc_undo(part.id).at("tree") == before);
    CHECK(chudware::doc::doc_redo(part.id).at("tree") == after);
}

TEST_CASE("model history: empty cleanup changes neither revision, dirty state nor undo") {
    HistoryPart part("HistoryNoOpFresh");
    const json before = chudware::doc::doc_tree(part.id);
    CHECK_FALSE(history_dirty(part.id));
    const json result = doc_apply_ops(part.id, before.at("revision").get<long>(),
        json::array({{{"op", "simplify_history"}}}), false);
    CHECK(result.at("revision") == before.at("revision"));
    CHECK(result.at("tree") == before.at("tree"));
    CHECK_FALSE(history_dirty(part.id));
    CHECK_THROWS(chudware::doc::doc_undo(part.id));
}

TEST_CASE("model history: explicit empty selection preserves an existing candidate and undo stack") {
    HistoryPart part("HistoryExplicitNoOp");
    const json built = doc_apply_ops(part.id, -1, json::array({history_plane("Candidate")}), false);
    chudware::doc::doc_clear_history(part.id);
    const std::string id = built.at("id_map").at("$0");
    REQUIRE(history_safe(chudware::doc::doc_eval_model_history(part.id), id));
    const bool dirty = history_dirty(part.id);
    const json result = doc_apply_ops(part.id, built.at("revision").get<long>(), json::array({
        {{"op","simplify_history"}, {"features",json::array()}}
    }), false);
    CHECK(result.at("revision") == built.at("revision"));
    CHECK(result.at("tree") == built.at("tree"));
    CHECK(history_dirty(part.id) == dirty);
    const json copied = doc_apply_ops(part.id, built.at("revision").get<long>(), json::array({
        {{"op","simplify_history"}, {"features",json::array()}},
        {{"op","copy_feature"}, {"id",id}}
    }), false);
    CHECK(copied.at("revision") == built.at("revision"));
    CHECK(copied.at("tree") == built.at("tree"));
    CHECK(copied.at("id_map").contains("$bundle"));
    CHECK(history_dirty(part.id) == dirty);
    CHECK_THROWS(chudware::doc::doc_undo(part.id));
}

TEST_CASE("model history: implicit selection removes eligible nodes without cascading") {
    HistoryPart part("HistoryNonCascade");
    const json built = doc_apply_ops(part.id, -1, json::array({
        history_plane("DependencyRoot"), history_plane("DependencyLeaf", "$0"),
        history_plane("OtherLeaf")
    }), false);
    const std::string root = built.at("id_map").at("$0");
    const std::string leaf = built.at("id_map").at("$1");
    const std::string other = built.at("id_map").at("$2");
    const json report = chudware::doc::doc_eval_model_history(part.id);
    CHECK_FALSE(history_safe(report, root));
    REQUIRE(history_safe(report, leaf));
    REQUIRE(history_safe(report, other));
    const json cleaned = doc_apply_ops(part.id, report.at("revision").get<long>(),
        json::array({{{"op","simplify_history"}}}), false);
    REQUIRE(cleaned.at("tree").at("features").size() == 1);
    CHECK(cleaned.at("tree").at("features")[0].at("id") == root);
    CHECK(chudware::doc::doc_undo(part.id).at("tree") == built.at("tree"));
    CHECK(chudware::doc::doc_redo(part.id).at("tree") == cleaned.at("tree"));
}

TEST_CASE("model history: references, stale revisions and public snapshots block removal") {
    HistoryPart part("HistoryReferenceGuard");
    const json built = doc_apply_ops(part.id, -1, json::array({
        history_plane("ReferencedDatum"),
        history_plane("DependentDatum", "$0", 5.0),
        history_plane("UnusedDatum")
    }), false);
    const std::string first = built.at("id_map").at("$0");
    const std::string unused = built.at("id_map").at("$2");
    const json report = chudware::doc::doc_eval_model_history(part.id);
    CHECK_FALSE(history_safe(report, first));
    CHECK(history_blocked(report, first));
    REQUIRE(history_safe(report, unused));
    CHECK_THROWS(doc_apply_ops(part.id, built.at("revision").get<long>(), json::array({
        {{"op", "simplify_history"}, {"features", json::array({first, unused})}}
    }), false));
    CHECK(chudware::doc::doc_tree(part.id).at("tree") == built.at("tree"));
    CHECK_THROWS(doc_apply_ops(part.id, built.at("revision").get<long>() - 1, json::array({
        {{"op", "simplify_history"}, {"features", json::array({unused})}}
    }), false));
    CHECK(chudware::doc::doc_tree(part.id).at("revision") == built.at("revision"));

    chudware::doc::doc_dep_register(part.id, "history-consumer", "drawing", built.at("revision"));
    CHECK_FALSE(history_safe(chudware::doc::doc_eval_model_history(part.id), unused));
    chudware::doc::doc_dep_unregister("history-consumer", "drawing");
    {
        HistoryPart consumer("HistoryOtherLiveConsumer");
        chudware::doc::doc_sim_study_add(consumer.id,
            {{"source_doc_id",part.id}, {"owner_feature",unused}});
        CHECK_FALSE(history_safe(chudware::doc::doc_eval_model_history(part.id), unused));
    }

    IntegrityFixtureFile file{std::filesystem::temp_directory_path() / (part.id + "-history.step+")};
    chudware::doc::doc_save(part.id, file.path.string(), true);
    const json saved = chudware::doc::doc_eval_model_history(part.id);
    CHECK(history_has(saved, "unused_datum", unused));
    CHECK(history_blocked(saved, unused));
    CHECK_FALSE(history_safe(saved, unused));
    CHECK_THROWS(doc_apply_ops(part.id, saved.at("revision").get<long>(), json::array({
        {{"op", "simplify_history"}, {"features", json::array({unused})}}
    }), false));
}

TEST_CASE("model history: publication marker changes eligibility but not document state") {
    SUBCASE("clean part") {
        HistoryPart part("HistoryPublicationClean");
        const json before = chudware::doc::doc_tree(part.id);
        REQUIRE_FALSE(history_dirty(part.id));
        chudware::doc::doc_mark_history_published(part.id);
        chudware::doc::doc_mark_history_published(part.id);
        const json after = chudware::doc::doc_tree(part.id);
        CHECK(after.at("tree") == before.at("tree"));
        CHECK(after.at("revision") == before.at("revision"));
        CHECK_FALSE(history_dirty(part.id));
        CHECK_THROWS(chudware::doc::doc_undo(part.id));
    }
    SUBCASE("dirty part with a candidate") {
        HistoryPart part("HistoryPublicationDirty");
        const json built = doc_apply_ops(part.id, -1, json::array({history_plane("Unused")}), false);
        const std::string feature = built.at("id_map").at("$0");
        chudware::doc::doc_clear_history(part.id);
        REQUIRE(history_dirty(part.id));
        REQUIRE(history_safe(chudware::doc::doc_eval_model_history(part.id), feature));
        chudware::doc::doc_mark_history_published(part.id);
        chudware::doc::doc_mark_history_published(part.id);
        const json report = chudware::doc::doc_eval_model_history(part.id);
        CHECK(history_blocked(report, feature));
        CHECK_FALSE(history_safe(report, feature));
        CHECK(report.at("revision") == built.at("revision"));
        CHECK(history_dirty(part.id));
        CHECK_THROWS(doc_apply_ops(part.id, built.at("revision").get<long>(), json::array({
            {{"op","simplify_history"}, {"features",json::array({feature})}}
        }), false));
        const json after = chudware::doc::doc_tree(part.id);
        CHECK(after.at("tree") == built.at("tree"));
        CHECK(after.at("revision") == built.at("revision"));
        CHECK(history_dirty(part.id));
        CHECK_THROWS(chudware::doc::doc_undo(part.id));
    }
    SUBCASE("already closed source") {
        std::string id;
        {
            HistoryPart part("HistoryPublicationClosed");
            id = part.id;
        }
        CHECK_NOTHROW(chudware::doc::doc_mark_history_published(id));
    }
}

TEST_CASE("model history: suppression, configuration and rollback do not imply safe deletion") {
    HistoryPart part("HistoryConfigurationGuard");
    const json built = doc_apply_ops(part.id, -1, json::array({
        history_plane("Dormant"), history_plane("LiveDatum")
    }), false);
    const std::string dormant = built.at("id_map").at("$0");
    const std::string live = built.at("id_map").at("$1");
    doc_apply_ops(part.id, -1, json::array({
        {{"op", "set_suppressed"}, {"id", dormant}, {"value", true}}
    }), false);
    json report = chudware::doc::doc_eval_model_history(part.id);
    CHECK_FALSE(history_has(report, "unused_datum", dormant));
    CHECK_FALSE(history_safe(report, dormant));
    REQUIRE(history_safe(report, live));
    chudware::doc::doc_config_add(part.id, "Alternate", nullptr);
    report = chudware::doc::doc_eval_model_history(part.id);
    CHECK_FALSE(history_safe(report, live));
    CHECK(history_blocked(report, live));
    CHECK_THROWS(doc_apply_ops(part.id, report.at("revision").get<long>(), json::array({
        {{"op", "simplify_history"}, {"features", json::array({live})}}
    }), false));
    chudware::doc::doc_config_set_suppression(part.id, "Alternate", live, true);
    chudware::doc::doc_config_activate(part.id, "Alternate");
    report = chudware::doc::doc_eval_model_history(part.id);
    CHECK_FALSE(history_has(report, "unused_datum", live));
    CHECK_FALSE(history_safe(report, live));
    doc_apply_ops(part.id, -1, json::array({{{"op","set_rollback"},{"after",dormant}}}), false);
    report = chudware::doc::doc_eval_model_history(part.id);
    CHECK_FALSE(history_has(report, "unused_datum", live));
    CHECK_FALSE(history_safe(report, live));
}

TEST_CASE("model history: literal identity can be removed only with preserved body identity") {
    HistoryPart part("HistoryIdentitySafe");
    const json built = doc_apply_ops(part.id, -1, json::array({
        box("Base", 20, 20, 10, {0, 0, 0}),
        {{"op","add_feature"}, {"feature", {{"type","transform"}, {"name","Identity"},
             {"params", {{"translate", {0,0,0}}, {"angle", 0}, {"body", "$0"}}}}}},
        cyl("Cut", 2, 10, {10,10,0}, {{"mode","cut"}})
    }), false);
    const std::string id = built.at("id_map").at("$1");
    const json report = chudware::doc::doc_eval_model_history(part.id);
    CHECK(history_has(report, "identity_transform", id));
    REQUIRE(history_safe(report, id));
    const json cleaned = doc_apply_ops(part.id, report.at("revision").get<long>(), json::array({
        {{"op","simplify_history"}, {"features",json::array({id})}}
    }), false);
    CHECK(cleaned.at("tree").at("features").size() == 2);
    CHECK(cleaned.at("volume").get<double>() == doctest::Approx(built.at("volume").get<double>()));
    CHECK(chudware::doc::doc_undo(part.id).at("tree") == built.at("tree"));
    CHECK(chudware::doc::doc_redo(part.id).at("tree") == cleaned.at("tree"));
}

TEST_CASE("model history: copies, expressions and final output transforms are never removable") {
    SUBCASE("copy remains a separate body") {
        HistoryPart part("HistoryIdentityCopy");
        const json built = doc_apply_ops(part.id, -1, json::array({
            box("Base", 20, 20, 10, {0,0,0}),
            {{"op","add_feature"}, {"feature", {{"type","transform"}, {"name","Copy"},
                {"params", {{"copy",true}, {"translate",{0,0,0}}}}}}}
        }), false);
        const std::string id = built.at("id_map").at("$1");
        const json report = chudware::doc::doc_eval_model_history(part.id);
        CHECK(history_has(report, "identity_transform", id));
        CHECK(history_blocked(report, id));
        CHECK_FALSE(history_safe(report, id));
    }
    SUBCASE("expression is not a literal zero") {
        HistoryPart part("HistoryIdentityExpression");
        const json built = doc_apply_ops(part.id, -1, json::array({
            box("Base", 20, 20, 10, {0,0,0}),
            {{"op","add_feature"}, {"feature", {{"type","transform"}, {"name","Driven"},
                {"params", {{"angle",{{"value",0},{"expr","0"}}}}}}}}
        }), false);
        const std::string id = built.at("id_map").at("$1");
        const json report = chudware::doc::doc_eval_model_history(part.id);
        CHECK_FALSE(history_safe(report, id));
        CHECK_FALSE(history_has(report, "identity_transform", id));
    }
    SUBCASE("final body owner remains observable") {
        HistoryPart part("HistoryIdentityFinal");
        const json built = doc_apply_ops(part.id, -1, json::array({
            box("Base", 20, 20, 10, {0,0,0}),
            {{"op","add_feature"}, {"feature", {{"type","transform"}, {"name","Final"},
                {"params", {{"translate",{0,0,0}}}}}}}
        }), false);
        const std::string id = built.at("id_map").at("$1");
        const json report = chudware::doc::doc_eval_model_history(part.id);
        CHECK(history_has(report, "identity_transform", id));
        CHECK(history_blocked(report, id));
        CHECK_FALSE(history_safe(report, id));
    }
}

TEST_CASE("model history: repeated real countersinks suggest semantic holes") {
    HistoryPart part("HistoryCountersinks");
    const json built = doc_apply_ops(part.id, -1, json::array({
        box("Plate", 50, 30, 10, {0,0,0}),
        cyl("BoreA", 2, 10, {10,10,0}, {{"mode","cut"}}),
        prim("cone", "SinkA", {{"r1",4}, {"r2",2}, {"height",2}, {"at",{10,10,0}}, {"mode","cut"}}),
        cyl("BoreB", 2, 10, {30,10,0}, {{"mode","cut"}}),
        prim("cone", "SinkB", {{"r1",4}, {"r2",2}, {"height",2}, {"at",{30,10,0}}, {"mode","cut"}})
    }), false);
    for (const auto& status : built.at("rebuild").at("feature_status"))
        REQUIRE_MESSAGE(status.at("ok").get<bool>(), status.dump());
    const json report = chudware::doc::doc_eval_model_history(part.id);
    for (int i = 1; i <= 4; ++i)
        CHECK(history_has(report, "repeated_countersink", built.at("id_map").at("$" + std::to_string(i))));
    CHECK(history_has(report, "repeated_dimension", built.at("id_map").at("$1")));
    CHECK(report.at("safe_fixes").empty());
    const json changed = doc_apply_ops(part.id, report.at("revision").get<long>(), json::array({
        {{"op","edit_feature"}, {"id",built.at("id_map").at("$4")},
         {"params",{{"at",{31,10,0}}}}}
    }), false);
    for (const auto& status : changed.at("rebuild").at("feature_status"))
        REQUIRE_MESSAGE(status.at("ok").get<bool>(), status.dump());
    const json unmatched = chudware::doc::doc_eval_model_history(part.id);
    CHECK_FALSE(history_has(unmatched, "repeated_countersink", built.at("id_map").at("$1")));
    CHECK_FALSE(history_has(unmatched, "repeated_countersink", built.at("id_map").at("$4")));
}

TEST_CASE("model history: all four clamp countersinks share one signed-axis finding") {
    json document = history_counter_document();
    auto& features = document["features"];
    append_history_counter_pair(features, "f44", "f45", {18.5,-8.6,-7.75}, "+y", "+y");
    append_history_counter_pair(features, "f46", "f47", {27,-8.6,-4.4}, "+y", "+y");
    append_history_counter_pair(features, "f48", "f49", {37.5,-8.6,-9.6}, "+y", "+y");
    append_history_counter_pair(features, "f93", "f94", {-10.5,-8.6,-17},
                                json::array({0,1,0}), json::array({0,1,0}));
    const std::string id = chudware::doc::doc_open_embedded(document).at("doc_id");
    const json report = chudware::doc::doc_eval_model_history(id);
    int found = 0;
    for (const json& finding : report.at("findings")) {
        if (finding.at("code") != "repeated_countersink") continue;
        ++found;
        CHECK(finding.at("feature_ids") == json::array(
            {"f44","f45","f46","f47","f48","f49","f93","f94"}));
        CHECK(finding.at("evidence").at("axis") == json::array({0,1,0}));
        CHECK_FALSE(finding.at("auto_fixable").get<bool>());
    }
    CHECK(found == 1);
    chudware::doc::doc_close(id);
}

TEST_CASE("model history: signed axes, placement and cone orientation stay distinct") {
    json document = history_counter_document();
    auto& features = document["features"];
    append_history_counter_pair(features, "p1", "p2", {0,0,0}, " +Y ", json::array({0,4,0}));
    append_history_counter_pair(features, "p3", "p4", {10,0,0}, json::array({0,1,0}), "y");
    append_history_counter_pair(features, "n1", "n2", {20,0,0}, "-y", json::array({0,-3,0}));
    append_history_counter_pair(features, "m1", "m2", {30,0,0}, "+y", "-y");
    append_history_counter_pair(features, "q1", "q2", {40,0,0}, "+y", "+y");
    features.back()["params"]["at"] = {41,0,0};
    append_history_counter_pair(features, "r1", "r2", {50,0,0}, "+y", "+y");
    features.back()["params"]["r1"] = 1.7;
    features.back()["params"]["r2"] = 3.4;
    append_history_counter_pair(features, "d1", "d2", {60,0,0},
                                json::array({1e-8,1,0}), json::array({1e-8,1,0}));
    append_history_counter_pair(features, "b1", "b2", {61,0,0}, "+y", "+y");
    features[features.size() - 2]["params"]["height"] = 4.0;
    append_history_counter_pair(features, "c1", "c2", {62,0,0}, "+y", "+y");
    features[features.size() - 2]["params"]["centered"] = true;
    features.back()["params"]["centered"] = true;
    append_history_counter_pair(features, "z1", "z2", {70,0,0}, nullptr, "vertical");
    append_history_counter_pair(features, "z3", "z4", {80,0,0}, "+Z", json::array({0,0,2}));
    append_history_counter_pair(features, "x1", "x2", {90,0,0}, "-X", json::array({-2,0,0}));
    append_history_counter_pair(features, "x3", "x4", {100,0,0}, json::array({-1,0,0}), "-x");
    append_history_counter_pair(features, "v1", "v2", {110,0,0},
                                json::array({1,2,3}), json::array({2,4,6}));
    append_history_counter_pair(features, "v3", "v4", {120,0,0},
                                json::array({2,4,6}), json::array({1,2,3}));
    append_history_counter_pair(features, "t1", "t2", {130,0,0}, "-z", json::array({0,0,-2}));
    append_history_counter_pair(features, "t3", "t4", {140,0,0}, json::array({0,0,-1}), " -Z ");
    append_history_counter_pair(features, "u1", "u2", {150,0,0}, "+x", json::array({1,0,0}));
    append_history_counter_pair(features, "bad1", "bad2", {160,0,0},
                                json::array({0,0,0}), json::array({0,0,0}));
    const std::string id = chudware::doc::doc_open_embedded(document).at("doc_id");
    const json report = chudware::doc::doc_eval_model_history(id);
    int found = 0;
    for (const json& finding : report.at("findings")) {
        if (finding.at("code") != "repeated_countersink") continue;
        ++found;
        const json& ids = finding.at("feature_ids");
        CHECK((ids == json::array({"p1","p2","p3","p4"}) ||
               ids == json::array({"z1","z2","z3","z4"}) ||
               ids == json::array({"x1","x2","x3","x4"}) ||
               ids == json::array({"v1","v2","v3","v4"}) ||
               ids == json::array({"t1","t2","t3","t4"})));
    }
    CHECK(found == 5);
    for (const char* excluded : {"n1","n2","m1","m2","q1","q2","r1","r2","d1","d2",
                                 "b1","b2","c1","c2","u1","u2","bad1","bad2"})
        CHECK_FALSE(history_has(report, "repeated_countersink", excluded));
    chudware::doc::doc_close(id);
}

TEST_CASE("model history: standalone primitives with cut mode are not countersink cuts") {
    HistoryPart part("HistoryStandaloneCountersinks");
    const json built = doc_apply_ops(part.id, -1, json::array({
        box("Plate", 50, 30, 10, {0,0,0}),
        cyl("BoreA", 2, 10, {10,10,0}, {{"mode","cut"},{"merge",false}}),
        prim("cone", "SinkA", {{"r1",4},{"r2",2},{"height",2},{"at",{10,10,0}},
                               {"mode","cut"},{"merge",false}}),
        cyl("BoreB", 2, 10, {30,10,0}, {{"mode","cut"},{"merge",false}}),
        prim("cone", "SinkB", {{"r1",4},{"r2",2},{"height",2},{"at",{30,10,0}},
                               {"mode","cut"},{"merge",false}})
    }), false);
    for (const auto& status : built.at("rebuild").at("feature_status"))
        REQUIRE_MESSAGE(status.at("ok").get<bool>(), status.dump());
    const json report = chudware::doc::doc_eval_model_history(part.id);
    for (int i = 1; i <= 4; ++i)
        CHECK_FALSE(history_has(report, "repeated_countersink",
                                built.at("id_map").at("$" + std::to_string(i))));
}

TEST_CASE("model history: either member of a pair must have effective cut semantics") {
    json document = history_counter_document();
    auto& features = document["features"];
    append_history_counter_pair(features, "f1", "f2", {0,0,0}, "+y", "+y");
    append_history_counter_pair(features, "f3", "f4", {10,0,0}, "+y", "+y");
    SUBCASE("standalone cylinders") {
        features[0]["params"]["merge"] = false;
        features[2]["params"]["merge"] = false;
    }
    SUBCASE("standalone cones") {
        features[1]["params"]["merge"] = false;
        features[3]["params"]["merge"] = false;
    }
    SUBCASE("configuration overrides cylinders") {
        document["configurations"] = json::array({
            {{"name","Standalone"}, {"param_overrides",{{"f1.merge",false},{"f3.merge",false}}}}
        });
        document["active_configuration"] = "Standalone";
    }
    SUBCASE("configuration overrides cones") {
        document["configurations"] = json::array({
            {{"name","Standalone"}, {"param_overrides",{{"f2.merge",false},{"f4.merge",false}}}}
        });
        document["active_configuration"] = "Standalone";
    }
    const std::string id = chudware::doc::doc_open_embedded(document).at("doc_id");
    const json report = chudware::doc::doc_eval_model_history(id);
    for (const char* fid : {"f1","f2","f3","f4"})
        CHECK_FALSE(history_has(report, "repeated_countersink", fid));
    chudware::doc::doc_close(id);
}

TEST_CASE("model history: active configuration merge overrides change cut semantics") {
    HistoryPart part("HistoryConfiguredCountersinks");
    const json built = doc_apply_ops(part.id, -1, json::array({
        box("Plate", 50, 30, 10, {0,0,0}),
        cyl("BoreA", 2, 10, {10,10,0}, {{"mode","cut"},{"merge",true}}),
        prim("cone", "SinkA", {{"r1",4},{"r2",2},{"height",2},{"at",{10,10,0}},
                               {"mode","cut"},{"merge",true}}),
        cyl("BoreB", 2, 10, {30,10,0}, {{"mode","cut"},{"merge",true}}),
        prim("cone", "SinkB", {{"r1",4},{"r2",2},{"height",2},{"at",{30,10,0}},
                               {"mode","cut"},{"merge",true}})
    }), false);
    for (const auto& status : built.at("rebuild").at("feature_status"))
        REQUIRE_MESSAGE(status.at("ok").get<bool>(), status.dump());
    const std::string first = built.at("id_map").at("$1");
    REQUIRE(history_has(chudware::doc::doc_eval_model_history(part.id), "repeated_countersink", first));
    chudware::doc::doc_config_add(part.id, "Standalone", nullptr);
    for (int i = 1; i <= 4; ++i) {
        const std::string feature = built.at("id_map").at("$" + std::to_string(i));
        const json result = chudware::doc::doc_config_set_override(
            part.id, "Standalone", feature, "merge", false, false);
        REQUIRE(!result.contains("rejected_tree"));
    }
    REQUIRE(history_has(chudware::doc::doc_eval_model_history(part.id), "repeated_countersink", first));
    const json changed = chudware::doc::doc_config_activate(part.id, "Standalone");
    REQUIRE(!changed.contains("rejected_tree"));
    for (const auto& status : changed.at("rebuild").at("feature_status"))
        REQUIRE_MESSAGE(status.at("ok").get<bool>(), status.dump());
    json report = chudware::doc::doc_eval_model_history(part.id);
    CHECK(report.at("metrics").at("active_configuration") == "Standalone");
    for (int i = 1; i <= 4; ++i)
        CHECK_FALSE(history_has(report, "repeated_countersink",
                                built.at("id_map").at("$" + std::to_string(i))));
    REQUIRE(!chudware::doc::doc_config_activate(part.id, "Default").contains("rejected_tree"));
    report = chudware::doc::doc_eval_model_history(part.id);
    CHECK(history_has(report, "repeated_countersink", first));
}

TEST_CASE("model history: only active sketch consumers and authored relations produce static guidance") {
    json sk = history_triangle();
    json document = {
        {"schema","aicad.part/1"}, {"name","HistoryStaticFixture"}, {"units",{{"system","MMGS"}}},
        {"material",nullptr}, {"equations",{{"globals",json::array()}}},
        {"features",json::array({
            {{"id","f1"}, {"type","sketch"}, {"name","Used"}, {"params",sk}, {"refs",{{"plane","plane.top"}}}},
            {{"id","f2"}, {"type","extrude"}, {"params",{{"mode","boss"},{"depth",3}}},
                {"refs",{{"profile",json::array({{{"kind","region"},{"sketch","f1"}}})}}}},
            {{"id","f3"}, {"type","sketch"}, {"name","Unused"}, {"params",sk}, {"refs",{{"plane","plane.top"}}}},
            {{"id","f4"}, {"type","extrude"}, {"suppressed",true}, {"profile","f3"}, {"depth",3}}
        })}
    };
    const std::string id = chudware::doc::doc_open_embedded(document).at("doc_id");
    json report = chudware::doc::doc_eval_model_history(id);
    CHECK(history_has(report, "sketch_intent_missing", "f1"));
    CHECK_FALSE(history_has(report, "sketch_intent_missing", "f3"));
    chudware::doc::doc_close(id);
    document["features"][0]["params"]["constraints"].push_back(
        {{"id","c4"},{"type","horizontal"},{"refs",{"e1"}}});
    const std::string constrained = chudware::doc::doc_open_embedded(document).at("doc_id");
    report = chudware::doc::doc_eval_model_history(constrained);
    CHECK_FALSE(history_has(report, "sketch_intent_missing", "f1"));
    chudware::doc::doc_close(constrained);
    document["rollback_after"] = "f1";
    const std::string rolled = chudware::doc::doc_open_embedded(document).at("doc_id");
    report = chudware::doc::doc_eval_model_history(rolled);
    CHECK_FALSE(history_has(report, "sketch_intent_missing", "f1"));
    CHECK(report.at("metrics").at("active_feature_count") == 1);
    chudware::doc::doc_close(rolled);
}

TEST_CASE("model history: expression tokens cannot silently remove referenced datums") {
    HistoryPart part("HistoryExpressionTokenGuard");
    const json built = doc_apply_ops(part.id, -1, json::array({history_plane("TokenDatum")}), false);
    const std::string id = built.at("id_map").at("$0");
    doc_apply_ops(part.id, -1, json::array({
        {{"op","eq_set"}, {"name","Other"}, {"expr", id + "0 + 1"}}
    }), false);
    CHECK(history_safe(chudware::doc::doc_eval_model_history(part.id), id));
    doc_apply_ops(part.id, -1, json::array({
        {{"op","eq_set"}, {"name","Other"}, {"expr", id + " + 1"}}
    }), false);
    const json report = chudware::doc::doc_eval_model_history(part.id);
    CHECK_FALSE(history_safe(report, id));
    CHECK(history_blocked(report, id));
    CHECK_THROWS(doc_apply_ops(part.id, report.at("revision").get<long>(), json::array({
        {{"op","simplify_history"}, {"features",json::array({id})}}
    }), false));
}

TEST_CASE("model history: transformed standalone tools are tied to their real cut features") {
    HistoryPart part("HistoryTransformedTools");
    auto sketch = [](const char* name, double x) -> json {
        return {{"op","add_feature"}, {"feature", {{"type","sketch"}, {"name",name},
            {"params", {{"entities",json::array({json{{"type","circle"},
                {"center",{x,5.0}}, {"radius",2.0}}})}, {"constraints",json::array()}}},
            {"refs",{{"plane","plane.top"}}}}}};
    };
    auto prism = [](const char* profile) -> json {
        return {{"op","add_feature"}, {"feature", {{"type","extrude"},
            {"params",{{"mode","boss"},{"merge",false},{"depth",10.0}}},
            {"refs",{{"profile",json::array({profile})}}}}}};
    };
    auto move = [](const char* body) -> json {
        return {{"op","add_feature"}, {"feature", {{"type","transform"},
            {"params",{{"body",body},{"translate",{1.0,0.0,0.0}}}}}}};
    };
    auto cut = [](const char* target, const char* tool) -> json {
        return {{"op","add_feature"}, {"feature", {{"type","boolean"},
            {"params",{{"op","cut"},{"a",target},{"b",tool}}}}}};
    };
    const json built = doc_apply_ops(part.id, -1, json::array({
        box("Plate",20,20,10,{0,0,0}), sketch("ToolSketchA",4), prism("$1"),
        move("$2"), cut("$0","$3"), sketch("ToolSketchB",12), prism("$5"),
        move("$6"), cut("$4","$7")
    }), false);
    for (const auto& status : built.at("rebuild").at("feature_status"))
        REQUIRE_MESSAGE(status.at("ok").get<bool>(), status.dump());
    const json report = chudware::doc::doc_eval_model_history(part.id);
    for (int i : {2,3,4,6,7,8})
        CHECK(history_has(report, "transformed_cut_tool", built.at("id_map").at("$" + std::to_string(i))));
    CHECK(history_has(report, "repeated_transform", built.at("id_map").at("$3")));
    CHECK(history_has(report, "repeated_transform", built.at("id_map").at("$7")));
    CHECK(report.at("safe_fixes").empty());
}

TEST_CASE("model history: feature count alone is not a finding") {
    HistoryPart part("HistoryPlainSolid");
    const json built = doc_apply_ops(part.id, -1, json::array({box("Plain", 8, 9, 10, {0,0,0})}), false);
    const json report = chudware::doc::doc_eval_model_history(part.id);
    CHECK(report.at("metrics").at("feature_count") == 1);
    CHECK(report.at("findings").empty());
    CHECK(report.at("safe_fixes").empty());
    CHECK(report.at("revision") == built.at("revision"));
}

TEST_CASE("model history: selectors, annotations and feature metadata protect referenced nodes") {
    SUBCASE("study selector owner") {
        HistoryPart part("HistoryStudySelectorGuard");
        const json built = doc_apply_ops(part.id, -1, json::array({history_plane("StudyPlane")}), false);
        const std::string id = built.at("id_map").at("$0");
        chudware::doc::doc_sim_study_add(part.id,
            {{"name","Study"}, {"selection",{{"kind","datum"},{"owner_feature",id}}}});
        const json report = chudware::doc::doc_eval_model_history(part.id);
        CHECK_FALSE(history_safe(report, id));
        CHECK(history_blocked(report, id));
        CHECK_THROWS(doc_apply_ops(part.id, report.at("revision").get<long>(), json::array({
            {{"op","simplify_history"}, {"features",json::array({id})}}
        }), false));
    }
    SUBCASE("datum folder metadata") {
        HistoryPart part("HistoryDatumMetadataGuard");
        json plane = history_plane("TaggedPlane");
        plane["feature"]["folder"] = "References";
        const json built = doc_apply_ops(part.id, -1, json::array({plane}), false);
        const std::string id = built.at("id_map").at("$0");
        const json report = chudware::doc::doc_eval_model_history(part.id);
        CHECK(history_has(report, "unused_datum", id));
        CHECK_FALSE(history_safe(report, id));
        CHECK(history_blocked(report, id));
    }
    SUBCASE("a later transform references the identity owner") {
        HistoryPart part("HistoryTransformReferenceGuard");
        const json built = doc_apply_ops(part.id, -1, json::array({
            box("Body",20,20,10,{0,0,0}),
            {{"op","add_feature"}, {"feature",{{"type","transform"},{"name","Identity"},
                {"params",{{"translate",{0,0,0}}}}}}},
            {{"op","add_feature"}, {"feature",{{"type","transform"},{"name","Later"},
                {"params",{{"body","$1"},{"translate",{1,0,0}}}}}}}
        }), false);
        const std::string id = built.at("id_map").at("$1");
        const json report = chudware::doc::doc_eval_model_history(part.id);
        CHECK(history_has(report, "identity_transform", id));
        CHECK(history_blocked(report, id));
        CHECK_FALSE(history_safe(report, id));
    }
}

TEST_CASE("model history: structural work after a dress-up is advisory, not a reorder") {
    json document = {{"schema","aicad.part/1"}, {"name","HistoryOrderFixture"},
        {"features",json::array({
            {{"id","f1"},{"type","box"},{"length",10},{"width",10},{"height",10}},
            {{"id","f2"},{"type","fillet"},{"radius",1}},
            {{"id","f3"},{"type","box"},{"length",2},{"width",2},{"height",2}},
            {{"id","f4"},{"type","chamfer"},{"length",1}}
        })}};
    std::string id = chudware::doc::doc_open_embedded(document).at("doc_id");
    json report = chudware::doc::doc_eval_model_history(id);
    CHECK(history_has(report, "dressup_before_structural", "f2"));
    CHECK(history_has(report, "dressup_before_structural", "f3"));
    CHECK_FALSE(history_has(report, "dressup_before_structural", "f4"));
    CHECK(report.at("safe_fixes").empty());
    chudware::doc::doc_close(id);
    document["features"][1]["suppressed"] = true;
    id = chudware::doc::doc_open_embedded(document).at("doc_id");
    report = chudware::doc::doc_eval_model_history(id);
    CHECK_FALSE(history_has(report, "dressup_before_structural", "f2"));
    chudware::doc::doc_close(id);
}

TEST_CASE("model history: failed mixed cleanup and refine batch restores the original tree") {
    HistoryPart part("HistoryAtomicRefineFailure");
    const json built = doc_apply_ops(part.id, -1, json::array({history_plane("OnlyPlane")}), false);
    const std::string id = built.at("id_map").at("$0");
    const json report = chudware::doc::doc_eval_model_history(part.id);
    REQUIRE(history_safe(report, id));
    const json rejected = doc_apply_ops(part.id, report.at("revision").get<long>(), json::array({
        {{"op","simplify_history"},{"features",json::array({id})}},
        {{"op","add_feature"},{"feature",{{"type","refine"},{"name","Refine"}}}}
    }), false);
    CHECK(rejected.contains("rejected_tree"));
    CHECK(rejected.at("revision") == built.at("revision"));
    CHECK(rejected.at("tree") == built.at("tree"));
}

TEST_CASE("assembly rejects refine at validation and native admission") {
    const std::string id = doc_new("assembly", "RefineAssemblyBoundary").at("doc_id");
    const json before = chudware::doc::doc_tree(id);
    json candidate = before.at("tree");
    const json feature = {{"id", "f1"}, {"type", "refine"}, {"name", "Refine"},
                          {"suppressed", false}, {"params", json::object()},
                          {"refs", json::object()}};
    candidate["features"].push_back(feature);
    const auto errors = chudware::doc::validate_document(candidate);
    bool found = false;
    for (const auto& error : errors)
        if (error.find("refine is not supported in assembly documents") != std::string::npos)
            found = true;
    CHECK(found);
    // Suppression or a configuration change must not allow an inert node to become active.
    candidate["features"][0]["suppressed"] = true;
    CHECK_FALSE(chudware::doc::validate_document(candidate).empty());

    bool rejected = false;
    try {
        doc_apply_ops(id, before.at("revision").get<long>(), json::array({
            {{"op", "add_feature"}, {"feature", {{"type", "refine"}, {"name", "Refine"}}}}
        }), false);
    } catch (const std::exception& error) {
        rejected = true;
        CHECK(std::string(error.what()).find("refine is not supported in assembly documents") !=
              std::string::npos);
    }
    CHECK(rejected);
    const json after = chudware::doc::doc_tree(id);
    CHECK(after.at("tree") == before.at("tree"));
    CHECK(after.at("revision") == before.at("revision"));
    CHECK_THROWS(chudware::doc::doc_undo(id));
    chudware::doc::doc_close(id);
}
