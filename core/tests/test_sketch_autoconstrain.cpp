// Sketch completion suite — auto-coincident pass + sketch_health, exercised through
// the doc RPC path (doc_new -> doc_apply_ops -> doc_sketch_auto_constrain / doc_sketch_health).
// The ui/imgui sketch entry is NOT wired, so everything here is headless RPC, never the GUI.
//
// Covered:
//   1. An open rectangle whose four corners TOUCH (line endpoints coincide geometrically but
//      carry no coincident constraint) gains 4 coincident constraints and the profile closes
//      into one material region — proving the auto pass keeps REDUNDANT coincidents (a closed
//      loop is over-determined at the last corner) rather than dropping them.
//   2. A near-miss corner beyond tolerance is left untouched (no phantom constraint).
//   3. A doubly-pinned corner (coincident is REDUNDANT) is KEPT, not skipped, and the auto
//      pass never leaves the sketch conflicting (the rollback safety invariant).
//   4. doc_sketch_health narrates the free variables of a known under-constrained sketch by
//      NAME (a loose line's endpoint coordinates), reports the open profile, and emits
//      advisory/1-shaped items.
//   5. The on-edit auto pass fires WITHOUT any explicit auto_constrain call (config default
//      ON), so a sketch built with touching corners is already coincident-constrained.
//
// Sketch entity ids must match e<N>, constraint ids c<N> (doc/validate.cpp).
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <array>
#include <cmath>
#include <string>
#include <utility>

#include <nlohmann/json.hpp>

#include "doc/session.h"

using json = nlohmann::json;
using chudware::doc::doc_new;
using chudware::doc::doc_apply_ops;
using chudware::doc::doc_set_meta;
using chudware::doc::doc_tree;
using chudware::doc::doc_sketch_auto_constrain;
using chudware::doc::doc_sketch_health;
using chudware::doc::doc_sketch_regions;

namespace {

// Count persisted coincident constraints on a sketch feature (read from doc_tree). This
// asserts on the actual CONSTRAINT set, not geometry — a touching rectangle is
// geometrically closed with zero constraints, so only a constraint count proves the
// auto-coincident pass ran.
int persisted_coincident_count(const std::string& doc_id, const std::string& fid) {
    json tree = doc_tree(doc_id);
    if (!tree.contains("tree") || !tree["tree"].contains("features")) return -1;
    for (const json& f : tree["tree"]["features"]) {
        if (f.value("id", std::string()) != fid) continue;
        int n = 0;
        if (f.contains("params") && f["params"].contains("constraints"))
            for (const json& c : f["params"]["constraints"])
                if (c.value("type", std::string()) == "coincident") ++n;
        return n;
    }
    return -1;
}

// A line entity p1->p2 (no constraints of its own).
json line(const char* id, double x1, double y1, double x2, double y2) {
    return json{{"id", id}, {"type", "line"}, {"construction", false},
                {"p1", json::array({x1, y1})}, {"p2", json::array({x2, y2})}};
}
json spline(const char* id, std::initializer_list<std::array<double, 2>> points) {
    json pts = json::array();
    for (const auto& p : points) pts.push_back(json::array({p[0], p[1]}));
    return json{{"id", id}, {"type", "spline"}, {"construction", false},
                {"points", std::move(pts)}};
}
json rel(const char* id, const char* type, std::vector<const char*> refs) {
    json r = json::array();
    for (auto s : refs) r.push_back(s);
    return json{{"id", id}, {"type", type}, {"refs", r}};
}
// Build a part doc whose one sketch feature carries `entities` + `constraints`.
// auto_coincident is DISABLED via doc meta unless `auto_on` is true, so the tests that
// call the explicit RPC start from a sketch the on-edit pass did not already close.
std::pair<std::string, std::string> make_sketch_doc(const json& entities,
                                                    const json& constraints,
                                                    bool auto_on) {
    json nr = doc_new("part", "AutoConstrain");
    const std::string doc_id = nr.at("doc_id").get<std::string>();
    if (!auto_on) doc_set_meta(doc_id, "sketch.auto_coincident", false);
    json ops = json::array({json{{"op", "add_feature"}, {"feature",
        json{{"type", "sketch"}, {"name", "S"},
             {"params", {{"entities", entities}, {"constraints", constraints}}},
             {"refs", {{"plane", "plane.top"}}}}}}});
    json resp = doc_apply_ops(doc_id, -1, ops, false);
    std::string fid;
    const json& idm = resp.at("id_map");
    for (auto it = idm.begin(); it != idm.end(); ++it)
        if (it.value().is_string() && it.value().get<std::string>().rfind("f", 0) == 0)
            fid = it.value().get<std::string>();
    return {doc_id, fid};
}

// Four lines forming a 10x10 square whose consecutive endpoints TOUCH exactly but carry
// no coincident constraints — the canonical "profile looks closed but isn't constrained".
json touching_rect_entities() {
    return json::array({
        line("e1", 0, 0, 10, 0),
        line("e2", 10, 0, 10, 10),
        line("e3", 10, 10, 0, 10),
        line("e4", 0, 10, 0, 0),
    });
}

}  // namespace

TEST_CASE("auto_constrain closes a touching-corner rectangle with 4 coincidents") {
    auto [doc_id, fid] = make_sketch_doc(touching_rect_entities(), json::array(), false);

    // apply=false first: should PROPOSE 4 coincident pairs (one per shared corner).
    json preview = doc_sketch_auto_constrain(doc_id, fid, json{{"apply", false}});
    REQUIRE(preview.contains("proposals"));
    CHECK_MESSAGE(preview["proposals"].size() == 4,
        "expected 4 corner proposals, got " << preview["proposals"].dump());

    // apply=true: adds the coincidents (keeping the redundant closing one) and closes the loop.
    json res = doc_sketch_auto_constrain(doc_id, fid, json{{"apply", true}});
    CHECK_MESSAGE(res["applied"].size() == 4,
        "expected 4 coincidents applied, got " << res.dump());
    CHECK(res["skipped_conflicting"].get<int>() == 0);
    // Closure gained: material region count rises to >= 1 once the loop closes.
    if (res.contains("regions_before") && res.contains("regions_after") &&
        res["regions_after"].is_number() && res["regions_after"].get<int>() >= 0) {
        CHECK_MESSAGE(res["regions_after"].get<int>() >= 1,
            "profile should close to >=1 material region: " << res.dump());
    }

    // Health now reports the sketch as closed (no open endpoints).
    json h = doc_sketch_health(doc_id, fid);
    CHECK(h["closure"]["open_endpoints"].empty());
}

TEST_CASE("auto_constrain leaves a beyond-tolerance near-miss untouched") {
    // Corner e1.p2 (10,0) vs e2.p1 (10, 0.5) — 0.5 mm apart, far beyond the 1e-4 default tol.
    json ents = json::array({
        line("e1", 0, 0, 10, 0),
        line("e2", 10, 0.5, 10, 10),
    });
    auto [doc_id, fid] = make_sketch_doc(ents, json::array(), false);
    json preview = doc_sketch_auto_constrain(doc_id, fid, json{{"apply", false}});
    CHECK_MESSAGE(preview["proposals"].empty(),
        "a 0.5mm gap must NOT be proposed as coincident: " << preview["proposals"].dump());
}

TEST_CASE("auto_constrain keeps a redundant coincident and stays non-conflicting") {
    // A doubly-pinned shared corner: e1.p2 fixed at (10,0), e2.p1 fixed at (10,0) too. The
    // proposed coincident there is REDUNDANT (both endpoints already pinned to the same
    // point) — the pass must KEEP it (not mistake redundant for conflicting) and never leave
    // the sketch conflicting.
    json ents = json::array({
        line("e1", 0, 0, 10, 0),
        line("e2", 10, 0, 10, 10),
    });
    json cons = json::array({rel("c1", "fix", {"e1.p2"}), rel("c2", "fix", {"e2.p1"})});
    auto [doc_id, fid] = make_sketch_doc(ents, cons, false);

    json res = doc_sketch_auto_constrain(doc_id, fid, json{{"apply", true}});
    CHECK_MESSAGE(res["skipped_conflicting"].get<int>() == 0,
        "a redundant (not conflicting) coincident must not be skipped: " << res.dump());
    CHECK_MESSAGE(res["applied"].size() >= 1,
        "the redundant coincident must be applied: " << res.dump());
    json h = doc_sketch_health(doc_id, fid);
    CHECK_MESSAGE(h["conflicting"].empty(),
        "auto pass must never leave the sketch conflicting: " << h["conflicting"].dump());

    // Idempotence: re-running proposes nothing (the pair is now coincident-constrained).
    json again = doc_sketch_auto_constrain(doc_id, fid, json{{"apply", false}});
    CHECK_MESSAGE(again["proposals"].empty(),
        "already-coincident pair must not be re-proposed: " << again["proposals"].dump());
}

TEST_CASE("auto_constrain rolls back a proposal that would conflict") {
    // Two endpoints WITHIN tolerance of each other (3e-5 mm < 1e-4 tol) -> a coincident is
    // proposed. But each endpoint is `fix`ed to a DISTINCT point, so pinning them coincident
    // is unsatisfiable: the proposal turns the system CONFLICTING and must be rolled back
    // (skipped_conflicting = 1, applied = 0). The sketch itself builds fine (two fixes at
    // nearly-coincident but distinct points is valid, just conflicting once merged).
    json ents = json::array({
        line("e1", 0, 0, 10, 0),
        line("e2", 10.00003, 0, 10.00003, 10),
    });
    // Fix the whole of each line so e1.p2 is pinned at (10,0) and e2.p1 at (10.00003,0).
    json cons = json::array({rel("c1", "fix", {"e1"}), rel("c2", "fix", {"e2"})});
    auto [doc_id, fid] = make_sketch_doc(ents, cons, false);

    // A coincident IS proposed (the endpoints are within tol).
    json preview = doc_sketch_auto_constrain(doc_id, fid, json{{"apply", false}});
    REQUIRE_MESSAGE(preview["proposals"].size() == 1,
        "the sub-tolerance endpoints should be proposed: " << preview.dump());

    // apply=true: the proposal conflicts (both endpoints fixed elsewhere) -> rolled back.
    json res = doc_sketch_auto_constrain(doc_id, fid, json{{"apply", true}});
    CHECK_MESSAGE(res["applied"].empty(),
        "a conflicting coincident must NOT be applied: " << res.dump());
    CHECK_MESSAGE(res["skipped_conflicting"].get<int>() == 1,
        "the conflicting proposal must be counted as skipped: " << res.dump());
    // The sketch is left as it was — not conflicting (the bad constraint never landed).
    json h = doc_sketch_health(doc_id, fid);
    CHECK_MESSAGE(h["conflicting"].empty(),
        "rollback must leave the sketch non-conflicting: " << h["conflicting"].dump());
}

TEST_CASE("auto_constrain retains valid corners when another batch proposal conflicts") {
    json ents = json::array({
        line("e1", 0, 0, 10, 0), line("e2", 10, 0, 10, 10),
        line("e3", 30, 0, 40, 0), line("e4", 40.00003, 0, 40.00003, 10),
    });
    json cons = json::array({rel("c1", "fix", {"e3"}), rel("c2", "fix", {"e4"})});
    auto [doc_id, fid] = make_sketch_doc(ents, cons, false);
    const json preview = doc_sketch_auto_constrain(doc_id, fid, json{{"apply", false}});
    REQUIRE(preview["proposals"].size() == 2);
    const json result = doc_sketch_auto_constrain(doc_id, fid, json{{"apply", true}});
    REQUIRE(result["applied"].size() == 1);
    CHECK(result["skipped_conflicting"] == 1);
    const json tree = doc_tree(doc_id);
    bool retained_valid_corner = false;
    for (const json& feature : tree["tree"]["features"]) {
        if (feature.value("id", std::string()) != fid) continue;
        for (const json& constraint : feature["params"]["constraints"]) {
            if (constraint["id"] != result["applied"][0]) continue;
            const auto& refs = constraint["refs"];
            retained_valid_corner =
                (refs[0] == "e1.p2" && refs[1] == "e2.p1") ||
                (refs[1] == "e1.p2" && refs[0] == "e2.p1");
        }
    }
    CHECK(retained_valid_corner);
    CHECK(doc_sketch_health(doc_id, fid)["conflicting"].empty());
    const json again = doc_sketch_auto_constrain(doc_id, fid, json{{"apply", false}});
    CHECK(again["proposals"].size() == 1);
    chudware::doc::doc_close(doc_id);
}

TEST_CASE("sketch_health names the free variables of an under-constrained sketch") {
    // One free line, one point pinned at origin. The line is wholly loose: p1x,p1y,p2x,p2y.
    json ents = json::array({
        {{"id", "e1"}, {"type", "point"}, {"construction", false}, {"p", json::array({0, 0})}},
        line("e2", 2, 2, 8, 3),
    });
    json cons = json::array({rel("c1", "fix", {"e1"})});
    auto [doc_id, fid] = make_sketch_doc(ents, cons, false);

    json h = doc_sketch_health(doc_id, fid);
    REQUIRE(h.contains("free_variables"));
    // e2 (the loose line) must appear with its endpoint coordinate names.
    bool saw_p1x = false, saw_p2y = false;
    for (const json& fv : h["free_variables"]) {
        if (fv.value("entity", std::string()) != "e2") continue;
        std::string p = fv.value("param", std::string());
        if (p == "p1 x") saw_p1x = true;
        if (p == "p2 y") saw_p2y = true;
    }
    CHECK_MESSAGE(saw_p1x, "health should name e2 p1 x as free: " << h["free_variables"].dump());
    CHECK_MESSAGE(saw_p2y, "health should name e2 p2 y as free: " << h["free_variables"].dump());
    // dof_narration is a human phrase mentioning e2.
    CHECK(h["dof_narration"].get<std::string>().find("e2") != std::string::npos);
    // advisory items are present and advisory/1-shaped.
    REQUIRE(h["advisory"].is_array());
    REQUIRE(!h["advisory"].empty());
    bool shaped = false;
    for (const json& item : h["advisory"]) {
        if (item.value("schema", std::string()) != "aicad.advisory/1") continue;
        if (item.contains("severity") && item.contains("finding") &&
            item.contains("candidate_fixes"))
            shaped = true;
    }
    CHECK_MESSAGE(shaped, "advisory items must be aicad.advisory/1-shaped: " << h["advisory"].dump());
}

TEST_CASE("on-edit auto-coincident fires on add_feature without an explicit call") {
    // Build the touching rectangle with auto_coincident LEFT ON (default). The on-edit pass
    // in apply_ops_locked must add 4 coincident constraints DURING the add_feature op itself
    // (the primary path the MCP add_features tool / copilot use). We assert on the persisted
    // CONSTRAINT set — geometry alone is closed with zero constraints, so this is the real
    // proof the automatic default-on pass ran.
    auto [doc_id, fid] = make_sketch_doc(touching_rect_entities(), json::array(), true);

    CHECK_MESSAGE(persisted_coincident_count(doc_id, fid) == 4,
        "default-on auto pass must add 4 coincidents on add_feature, got "
        << persisted_coincident_count(doc_id, fid));

    json h = doc_sketch_health(doc_id, fid);
    CHECK(h["closure"]["open_endpoints"].empty());
    json regs = doc_sketch_regions(doc_id, fid);
    int material = 0;
    for (const json& r : regs.value("regions", json::array()))
        if (r.value("is_material", false)) ++material;
    CHECK_MESSAGE(material >= 1,
        "the auto-closed profile should yield a material region: " << regs.dump());
}

TEST_CASE("on-edit auto-coincident closes spline endpoints without moving interior knots") {
    json ents = json::array({
        spline("e1", {{0, 0}, {15, 0}, {18, 20}, {10, 45}, {12, 60}, {0, 60}}),
        line("e2", 0, 60, 0, 0),
    });
    auto [doc_id, fid] = make_sketch_doc(ents, json::array(), true);

    json tree = doc_tree(doc_id);
    const json* sketch = nullptr;
    for (const json& feature : tree["tree"]["features"])
        if (feature.value("id", std::string()) == fid) sketch = &feature;
    REQUIRE(sketch != nullptr);
    const json& constraints = (*sketch)["params"]["constraints"];
    REQUIRE(constraints.size() == 2);
    std::string refs = constraints.dump();
    CHECK(refs.find("e1.start") != std::string::npos);
    CHECK(refs.find("e1.end") != std::string::npos);
    CHECK(refs.find("e1.p1") == std::string::npos);
    CHECK(refs.find("e1.p2") == std::string::npos);

    json regions = doc_sketch_regions(doc_id, fid);
    int material = 0;
    for (const json& region : regions.value("regions", json::array()))
        if (region.value("is_material", false)) ++material;
    CHECK_MESSAGE(material == 1,
        "spline plus closing line should yield one material region: " << regions.dump());
}

TEST_CASE("auto_constrain default-off leaves add_feature sketches untouched") {
    // With sketch.auto_coincident=false the on-edit pass must NOT run — 0 coincidents added.
    auto [doc_id, fid] = make_sketch_doc(touching_rect_entities(), json::array(), false);
    CHECK_MESSAGE(persisted_coincident_count(doc_id, fid) == 0,
        "auto pass must be OFF when the meta flag is false, got "
        << persisted_coincident_count(doc_id, fid));
}

TEST_CASE("auto_constrain mints non-colliding ids on a pre-constrained sketch") {
    // A rectangle already carrying c1..c4 coincidents on THREE corners, with the fourth
    // corner left open (no coincident). The auto pass must mint the new constraint id as
    // c5+ (IdGen scans nested sketch constraint ids), not collide with c1..c4 -> the batch
    // must validate and persist. Author 3 explicit coincidents + a 4th touching-but-loose
    // corner; auto adds the missing one.
    json ents = touching_rect_entities();  // e1..e4, corners touch
    json cons = json::array({
        rel("c1", "coincident", {"e1.p2", "e2.p1"}),
        rel("c2", "coincident", {"e2.p2", "e3.p1"}),
        rel("c3", "coincident", {"e3.p2", "e4.p1"}),
        // e4.p2 <-> e1.p1 left UNcoincident (the corner at the origin).
    });
    auto [doc_id, fid] = make_sketch_doc(ents, cons, false);
    json res = doc_sketch_auto_constrain(doc_id, fid, json{{"apply", true}});
    CHECK_MESSAGE(res["applied"].size() == 1,
        "exactly the one missing corner should be added: " << res.dump());
    // The minted id must be c5 (not a duplicate of c1..c4) — proven by the batch persisting.
    std::string minted = res["applied"][0].get<std::string>();
    // Only c1..c3 were authored, so the mint must be c4+ (IdGen scans nested sketch ids) —
    // never a duplicate of an existing id.
    bool collides = (minted == "c1" || minted == "c2" || minted == "c3");
    CHECK_MESSAGE(!collides, "auto pass minted a colliding constraint id: " << minted);
    CHECK(persisted_coincident_count(doc_id, fid) == 4);
    json h = doc_sketch_health(doc_id, fid);
    CHECK(h["conflicting"].empty());
}
