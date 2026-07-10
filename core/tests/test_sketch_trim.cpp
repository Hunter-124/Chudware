// Regression tests for sketch power-trim (op sketch_trim / sketchgeom::split_entity).
//
// THE BUGS (reported from the live sketcher — "swasi_test", DOF 1 -> DOF 12):
//   1. OVER-TRIM. The engine's op_sketch_trim gathered its cutter set from the REAL
//      entities only (`!construction`), while the GUI power-trim preview (sketch_mode.cpp
//      trimDoomedSpan) cuts against EVERY entity, construction included. So the preview
//      would highlight a short doomed span (cut at a construction line) while the engine,
//      blind to that construction cutter, trimmed the whole entity away — preview != result.
//   2. DOF EXPLOSION. op_sketch_trim called drop_constraints_touching(eid), nuking EVERY
//      constraint that referenced the trimmed entity — tangency, coincidence, horizontal —
//      even the ones anchored to endpoints the trim kept. A well-defined sketch (DOF 1)
//      sprang loose (DOF 12) the instant you trimmed a free tip.
//
// THE FIX (session.cpp op_sketch_trim + sketch_geom.cpp split_entity_ex):
//   * Cutters now include construction geometry, matching the preview (the preview is spec).
//   * Constraints are RE-ANCHORED by geometry onto the surviving spans instead of dropped:
//     whole-entity direction/centre refs stay on the survivor (co-geometric), endpoint refs
//     follow whichever survivor still carries that point, and only refs whose point vanished
//     with the removed span are dropped.
//   * New `keep_construction` op flag re-emits the trimmed-away span as construction geometry
//     (still defined against its neighbours) instead of deleting it.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <cmath>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

#include "doc/session.h"       // doc_new / doc_apply_ops / doc_sketch_overview (the op path)
#include "doc/sketch_geom.h"   // split_entity_ex (the pure geometry the op sits on)

using json = nlohmann::json;
using aicad::doc::doc_apply_ops;
using aicad::doc::doc_new;
using aicad::doc::doc_sketch_overview;
namespace sg = aicad::doc::sketchgeom;

namespace {

json line(const char* id, double x1, double y1, double x2, double y2, bool con = false) {
    return json{{"id", id}, {"type", "line"}, {"p1", {x1, y1}}, {"p2", {x2, y2}},
                {"construction", con}};
}
json geom_line(double x1, double y1, double x2, double y2) {
    return json{{"type", "line"}, {"p1", {x1, y1}}, {"p2", {x2, y2}}};
}
json arc(const char* id, double cx, double cy, double r, double a0, double a1, bool ccw = true) {
    return json{{"id", id}, {"type", "arc"}, {"center", {cx, cy}}, {"radius", r},
                {"start", a0}, {"end", a1}, {"ccw", ccw}, {"construction", false}};
}
json point(const char* id, double x, double y) {
    return json{{"id", id}, {"type", "point"}, {"p", {x, y}}, {"construction", false}};
}

// Create a part with one top-plane sketch carrying `entities`/`constraints`; return doc_id
// and (via out param) the assigned sketch feature id.
std::string make_sketch(const char* name, const json& entities, const json& constraints,
                        std::string& fid_out) {
    json nr = doc_new("part", name);
    std::string doc_id = nr.at("doc_id").get<std::string>();
    json ops = json::array({json{{"op", "add_feature"}, {"feature",
        json{{"type", "sketch"}, {"name", "S"},
             {"params", {{"entities", entities}, {"constraints", constraints}}},
             {"refs", {{"plane", "plane.top"}}}}}}});
    json r = doc_apply_ops(doc_id, -1, ops, false);
    fid_out = r.at("id_map").at("$0").get<std::string>();
    return doc_id;
}

json feature_of(const json& tree, const std::string& fid) {
    for (const auto& f : tree.at("features"))
        if (f.value("id", std::string()) == fid) return f;
    return json(nullptr);
}
const json* find_ent(const json& params, const std::string& id) {
    for (const auto& e : params.at("entities"))
        if (e.value("id", std::string()) == id) return &e;
    return nullptr;
}
// Does any constraint reference the exact handle `h` (e.g. "e1", "e1.p2")?
bool has_ref(const json& params, const std::string& h) {
    for (const auto& c : params.at("constraints"))
        if (c.contains("refs"))
            for (const auto& r : c["refs"])
                if (r.is_string() && r.get<std::string>() == h) return true;
    return false;
}

}  // namespace

// ---------------------------------------------------------------------------
// Geometry: split_entity_ex surfaces both survivors and the doomed span
// ---------------------------------------------------------------------------

TEST_CASE("geom: split_entity_ex drops the clicked span and reports the removed one") {
    json target = geom_line(0, 0, 10, 0);
    std::vector<json> cutters = {geom_line(6, -1, 6, 1)};  // crosses target at t=0.6
    sg::SplitResult res = sg::split_entity_ex(target, cutters, 8.0, 0.0);  // click on [6,10]

    REQUIRE_FALSE(res.whole);
    REQUIRE(res.survivors.size() == 1);
    CHECK(res.survivors[0]["p1"][0].get<double>() == doctest::Approx(0.0));
    CHECK(res.survivors[0]["p2"][0].get<double>() == doctest::Approx(6.0));  // cut, not past it
    REQUIRE(res.removed.has_value());
    CHECK((*res.removed)["p1"][0].get<double>() == doctest::Approx(6.0));
    CHECK((*res.removed)["p2"][0].get<double>() == doctest::Approx(10.0));
}

TEST_CASE("geom: no interior crossing => whole entity doomed, removed carries a copy") {
    json target = geom_line(0, 0, 10, 0);
    sg::SplitResult res = sg::split_entity_ex(target, {}, 5.0, 0.0);
    CHECK(res.whole);
    CHECK(res.survivors.empty());
    REQUIRE(res.removed.has_value());
    CHECK((*res.removed)["p2"][0].get<double>() == doctest::Approx(10.0));
}

// ---------------------------------------------------------------------------
// Op: over-trim fix — construction geometry acts as a trim boundary (bug #1)
// ---------------------------------------------------------------------------

TEST_CASE("op: a construction line is a valid trim boundary (no over-trim past it)") {
    std::string fid;
    json ents = json::array({
        line("e1", 0, 0, 10, 0),            // the real line we trim
        line("e2", 6, -2, 6, 2, /*con=*/true),  // construction cutter at x=6
    });
    std::string doc_id = make_sketch("TrimConCutter", ents, json::array(), fid);

    json trim = json::array({json{{"op", "sketch_trim"}, {"sketch", fid},
        {"entity", "e1"}, {"point", json::array({8.0, 0.0})}}});  // click the [6,10] span
    json r = doc_apply_ops(doc_id, -1, trim, false);

    json skf = feature_of(r.at("tree"), fid);
    REQUIRE(skf.is_object());
    const json& params = skf.at("params");
    const json* e1 = find_ent(params, "e1");
    REQUIRE_MESSAGE(e1 != nullptr, "e1 must survive — the construction line should have cut it, "
                                   "not let the whole line be trimmed away");
    CHECK(e1->at("p2")[0].get<double>() == doctest::Approx(6.0));  // trimmed exactly at x=6
}

// ---------------------------------------------------------------------------
// Op: constraint preservation / DOF (bug #2 — the DOF-1 -> DOF-12 regression)
// ---------------------------------------------------------------------------

TEST_CASE("op: trimming a free tip preserves the surviving span's relations and DOF") {
    std::string fid;
    json ents = json::array({
        line("e1", 0, 0, 20, 0),      // trimmed; p1 pinned, direction pinned, p2 free
        line("e2", 10, -5, 10, 5),    // cutter at x=10 (untouched by the trim)
    });
    json cons = json::array({
        json{{"id", "c1"}, {"type", "horizontal"}, {"refs", json::array({"e1"})}},
        json{{"id", "c2"}, {"type", "coincident"}, {"refs", json::array({"e1.p1", "origin"})}},
    });
    std::string doc_id = make_sketch("TrimDof", ents, cons, fid);

    int dof_before = doc_sketch_overview(doc_id, fid).at("dof").get<int>();

    json trim = json::array({json{{"op", "sketch_trim"}, {"sketch", fid},
        {"entity", "e1"}, {"point", json::array({15.0, 0.0})}}});  // trim the [10,20] tip
    json r = doc_apply_ops(doc_id, -1, trim, false);
    json skf = feature_of(r.at("tree"), fid);
    REQUIRE(skf.is_object());
    const json& params = skf.at("params");

    // The survivor kept id e1, cut back to x=10.
    const json* e1 = find_ent(params, "e1");
    REQUIRE(e1 != nullptr);
    CHECK(e1->at("p2")[0].get<double>() == doctest::Approx(10.0));
    // Both relations survive: the whole-entity horizontal and the pinned start point.
    CHECK(has_ref(params, "e1"));      // horizontal(e1) kept — span is co-geometric
    CHECK(has_ref(params, "e1.p1"));   // coincident(e1.p1, origin) kept — that point survived

    // DOF is preserved (the old blind constraint-drop spiked it). e2 is untouched so its DOF
    // cancels; e1's free tip stays free before and after -> identical whole-sketch DOF.
    int dof_after = doc_sketch_overview(doc_id, fid).at("dof").get<int>();
    CHECK(dof_after == dof_before);
}

TEST_CASE("op: a constraint anchored to the trimmed-away endpoint is dropped, not kept") {
    std::string fid;
    // e1 spans [0,20]; its p2 at (20,0) is pinned coincident to a point entity. Trimming the
    // [10,20] tip removes that endpoint, so ONLY that constraint should fall away.
    json ents = json::array({
        line("e1", 0, 0, 20, 0),
        line("e2", 10, -5, 10, 5),
        json{{"id", "e3"}, {"type", "point"}, {"p", {20, 0}}, {"construction", false}},
    });
    json cons = json::array({
        json{{"id", "c1"}, {"type", "horizontal"}, {"refs", json::array({"e1"})}},
        json{{"id", "c2"}, {"type", "coincident"}, {"refs", json::array({"e1.p2", "e3"})}},
    });
    std::string doc_id = make_sketch("TrimDropTip", ents, cons, fid);

    json trim = json::array({json{{"op", "sketch_trim"}, {"sketch", fid},
        {"entity", "e1"}, {"point", json::array({15.0, 0.0})}}});
    json r = doc_apply_ops(doc_id, -1, trim, false);
    json skf = feature_of(r.at("tree"), fid);
    REQUIRE(skf.is_object());
    const json& params = skf.at("params");

    CHECK(has_ref(params, "e1"));            // whole-entity horizontal survives
    CHECK_FALSE(has_ref(params, "e1.p2"));   // the trimmed-tip coincidence is gone
}

// ---------------------------------------------------------------------------
// Op: keep_construction option
// ---------------------------------------------------------------------------

TEST_CASE("op: keep_construction re-emits the doomed span as construction geometry") {
    std::string fid;
    json ents = json::array({
        line("e1", 0, 0, 10, 0),
        line("e2", 6, -2, 6, 2),
    });
    std::string doc_id = make_sketch("TrimKeepCon", ents, json::array(), fid);

    json trim = json::array({json{{"op", "sketch_trim"}, {"sketch", fid},
        {"entity", "e1"}, {"point", json::array({8.0, 0.0})},
        {"keep_construction", true}}});
    json r = doc_apply_ops(doc_id, -1, trim, false);
    json skf = feature_of(r.at("tree"), fid);
    REQUIRE(skf.is_object());
    const json& params = skf.at("params");

    // e1 survives as the real span [0,6]; the doomed [6,10] span is kept as a NEW construction
    // line rather than deleted.
    const json* e1 = find_ent(params, "e1");
    REQUIRE(e1 != nullptr);
    CHECK_FALSE(e1->value("construction", false));
    bool found_con_span = false;
    for (const auto& e : params.at("entities")) {
        if (e.value("id", std::string()) == "e1" || e.value("id", std::string()) == "e2") continue;
        if (e.value("type", std::string()) == "line" && e.value("construction", false)) {
            double a = e.at("p1")[0].get<double>(), b = e.at("p2")[0].get<double>();
            if (std::min(a, b) == doctest::Approx(6.0) && std::max(a, b) == doctest::Approx(10.0))
                found_con_span = true;
        }
    }
    CHECK_MESSAGE(found_con_span, "the trimmed-away [6,10] span should remain as construction");
}

// ---------------------------------------------------------------------------
// Op: the REPORTED "swasi_test" scenario — an ARC overshooting a CONSTRUCTION cutter,
// carrying a tangent (whole-entity) + a start coincidence. This is the exact shape from
// the screenshots (DOF 1 -> DOF 12); the line tests above never touch the arc code path
// (partition_arc / the arc branch of handle_point / span_ends) nor whole-entity tangent.
// ---------------------------------------------------------------------------

TEST_CASE("op: arc trimmed past a construction cutter keeps tangent+start relations, DOF stable") {
    std::string fid;
    // Quarter arc, radius 10, centre origin, 0deg (10,0) -> 90deg (0,10), ccw.
    // A VERTICAL CONSTRUCTION line at x=6 crosses it at 53.13deg (6,8) — the only interior
    // crossing. Old code excluded construction => no crossing => the whole arc was deleted and
    // its relations nuked (the DOF spike). A VERTICAL line at x=10 is tangent at the kept start.
    json ents = json::array({
        arc("e1", 0, 0, 10, 0, 90),
        line("e2", 6, -12, 6, 12, /*con=*/true),   // construction cutter
        point("e3", 10, 0),                         // pins the arc start
        line("e4", 10, -6, 10, 6),                  // tangent to the arc at its start (10,0)
    });
    json cons = json::array({
        json{{"id", "c1"}, {"type", "coincident"}, {"refs", json::array({"e1.center", "origin"})}},
        json{{"id", "c2"}, {"type", "coincident"}, {"refs", json::array({"e1.start", "e3"})}},
        json{{"id", "c3"}, {"type", "tangent"}, {"refs", json::array({"e4", "e1"})}},
    });
    std::string doc_id = make_sketch("TrimArcCon", ents, cons, fid);
    int dof_before = doc_sketch_overview(doc_id, fid).at("dof").get<int>();

    // Click the overshoot span (53.13deg..90deg), around 70deg = (3.42, 9.40).
    json trim = json::array({json{{"op", "sketch_trim"}, {"sketch", fid},
        {"entity", "e1"}, {"point", json::array({3.42, 9.40})}}});
    json r = doc_apply_ops(doc_id, -1, trim, false);
    json skf = feature_of(r.at("tree"), fid);
    REQUIRE(skf.is_object());
    const json& params = skf.at("params");

    // The arc SURVIVES (old code whole-deleted it) and was cut at the construction crossing.
    const json* e1 = find_ent(params, "e1");
    REQUIRE_MESSAGE(e1 != nullptr, "the arc must survive — the construction line should cut it");
    CHECK(e1->value("start", -1.0) == doctest::Approx(0.0));
    CHECK(e1->value("end", -1.0) == doctest::Approx(53.1301).epsilon(1e-3));  // cut at x=6, not 90

    // Its relations are preserved by geometry: the whole-entity tangent and the kept start
    // coincidence survive; nothing referenced the trimmed-away 90deg tip so nothing else drops.
    CHECK(has_ref(params, "e1"));        // tangent(e4, e1) — whole-entity, co-geometric survivor
    CHECK(has_ref(params, "e1.start"));  // coincident(e1.start, e3) — that point survived
    CHECK(has_ref(params, "e1.center")); // coincident(e1.center, origin) — centre unchanged

    // DOF does not spike: the trimmed 90deg tip was free, so trimming it leaves the whole-sketch
    // DOF unchanged (the old blind constraint-drop is exactly what sent DOF 1 -> 12).
    int dof_after = doc_sketch_overview(doc_id, fid).at("dof").get<int>();
    CHECK(dof_after == dof_before);
}
