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
#include <vector>

#include <nlohmann/json.hpp>

#include "doc/session.h"

using json = nlohmann::json;
using chudware::doc::doc_new;
using chudware::doc::doc_apply_ops;
using chudware::doc::doc_eval_interference;
using chudware::doc::doc_eval_feature_overlap;
using chudware::doc::doc_eval_compare;

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
    json far = doc_eval_interference(b.id, json{{"members", json::array({n2})}});
    CHECK(far["parts"].size() == 1);
    CHECK(far["interferences"].empty());
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
