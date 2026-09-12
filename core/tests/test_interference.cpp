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
#include <filesystem>
#include <vector>
#include <set>

#include <nlohmann/json.hpp>

#include "doc/session.h"
#include "doc/formats.h"
#include <BRepPrimAPI_MakeBox.hxx>
#include <BRepBuilderAPI_MakeFace.hxx>
#include <BRep_Builder.hxx>
#include <BRepTools.hxx>
#include <TopExp_Explorer.hxx>
#include <TopoDS_Shell.hxx>
#include <TopoDS_Solid.hxx>
#include <gp_Pln.hxx>
#include <gp_Pnt.hxx>

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

    json ir = doc_eval_interference(b.id);
    REQUIRE(ir.contains("interferences"));
    REQUIRE_MESSAGE(ir["interferences"].size() == 1,
                    "expected exactly one interfering pair, got: " << ir.dump());
    const json& hit = ir["interferences"][0];
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
    bool found = false;
    for (const json& p : ov.at("pairs")) {
        if (p.value("kind", std::string()) != "boss_swallowed") continue;
        const std::string a = p.value("a", std::string()), pb = p.value("b", std::string());
        if ((a == boss_id && pb == cut_id) || (a == cut_id && pb == boss_id)) found = true;
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
        }
    }
    CHECK_MESSAGE(found, "no cut_overlap(" << cut1 << "," << cut2 << ") pair in: "
                  << ov.at("pairs").dump());
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

    json ir = doc_eval_interference(b.id, json{{"include_coincident", true}});
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

TEST_CASE("model integrity: positive gap is proximity, not touching, and nearest is exact") {
    Built b = build("IntegrityGap", json::array({
        box("A", 10, 10, 10, {0, 0, 0}, {{"merge", false}}),
        box("B", 10, 10, 10, {10.02, 0, 0}, {{"merge", false}}),
    }));
    json near = doc_eval_model_integrity(b.id, {{"proximity_mm", 0.05}});
    REQUIRE_MESSAGE(near["complete"] == true, near.dump());
    REQUIRE(near["pairs"].size() == 1);
    CHECK(near["pairs"][0]["relation"] == "proximity");
    CHECK(near["pairs"][0]["distance_mm"].get<double>() == doctest::Approx(0.02));
    CHECK(near["pairs"][0]["overlap_volume_mm3"].get<double>() == doctest::Approx(0.0));
    CHECK(near["isolated_solids"].empty());
    CHECK(near["islands"].size() == 1);

    json far = doc_eval_model_integrity(b.id, {{"proximity_mm", 0.01}});
    REQUIRE_MESSAGE(far["complete"] == true, far.dump());
    CHECK(far["pairs"].empty());
    CHECK(far["islands"].size() == 2);
    CHECK(far["isolated_solids"].size() == 2);
    CHECK(far["summary"]["theoretical_pairs"] == 1);
    CHECK(far["summary"]["candidate_pairs"] == 0);
    REQUIRE(far["nearest"].size() == 2);
    for (const json& nearest : far["nearest"]) {
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
    const json checked = chudware::doc::doc_eval_check(id, 1);
    CHECK(std::any_of(checked["issues"].begin(), checked["issues"].end(),
        [](const json& issue) {
            return issue["kind"] == "brep_check" && issue["severity"] == "error";
        }));
    chudware::doc::doc_close(id);
}
