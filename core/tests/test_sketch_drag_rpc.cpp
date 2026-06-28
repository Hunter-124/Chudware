// Regression test for the CIRCLE-RIM DRAG-RESIZE bug, exercised through the EXACT RPC
// path the host uses (doc_sketch_enter -> doc_sketch_drag -> doc_sketch_drag_commit),
// NOT the low-level aicad::sketch::drag() the parity test calls directly.
//
// THE BUG (as the user saw it): dragging a circle's rim made the WHOLE sketch vanish
// mid-drag and never resized on release. Root cause was twofold:
//   1. The staged libaicad.so the host dlopen()s was older than the core build, so the
//      ".rim" drag handle (added to resolve_handle/drag) was not present in the running
//      kernel -> drag() rejected the handle -> ok:false.
//   2. doc_sketch_drag() returned a `{ok:false}` envelope WITHOUT an `entities` key on a
//      failed/empty drag, so the host's applyEnterResult ran parseEntities on an empty
//      result and CLEARED the on-screen sketch ("disappears while dragging").
//
// This test proves the now-correct behavior end to end at the RPC layer:
//   * doc_sketch_drag("<circle>.rim", xy=[newR,0]) returns ok-with-entities and the
//     circle's radius tracks newR (the centre never drifts);
//   * doc_sketch_drag_commit persists the new radius into the document;
//   * a drag with a BOGUS handle still returns entities (the sketch is never wiped).
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <cmath>
#include <string>

#include <nlohmann/json.hpp>

#include "doc/session.h"

using json = nlohmann::json;
using aicad::doc::doc_new;
using aicad::doc::doc_apply_ops;
using aicad::doc::doc_sketch_enter;
using aicad::doc::doc_sketch_drag;
using aicad::doc::doc_sketch_drag_commit;

namespace {

// A circle entity: centre (cx,cy), radius r. Under-defined in radius unless dimensioned.
json circle(const char* id, double cx, double cy, double r) {
    return json{{"id", id}, {"type", "circle"}, {"construction", false},
                {"center", json::array({cx, cy})}, {"radius", r}};
}
json rel(const char* id, const char* type, std::vector<const char*> refs) {
    json r = json::array();
    for (auto s : refs) r.push_back(s);
    return json{{"id", id}, {"type", type}, {"refs", r}};
}

// Pull a circle's solved radius out of a drag/enter `entities` array (records carry id).
double radius_of(const json& entities, const std::string& id) {
    if (!entities.is_array()) return -1.0;
    for (const auto& e : entities) {
        if (e.value("id", std::string()) != id) continue;
        if (e.contains("radius") && e.at("radius").is_number()) return e.at("radius").get<double>();
        if (e.contains("r") && e.at("r").is_number()) return e.at("r").get<double>();
    }
    return -1.0;
}

}  // namespace

TEST_CASE("circle rim drag resizes the radius through the doc_sketch_drag RPC") {
    const double R0 = 10.0;

    json nr = doc_new("part", "CircleRimDrag");
    const std::string doc_id = nr.at("doc_id").get<std::string>();

    // A single circle, centre fixed at the origin so ONLY the radius is free. Dragging the
    // rim must change the radius and leave the centre put.
    json ops = json::array({
        json{{"op", "add_feature"}, {"feature",
            json{{"type", "sketch"}, {"name", "S"},
                 {"params", {{"entities", json::array({circle("e1", 0, 0, R0)})},
                             {"constraints", json::array({rel("c1", "fix", {"e1.center"})})}}},
                 {"refs", {{"plane", "plane.top"}}}}}},
    });
    json resp = doc_apply_ops(doc_id, -1, ops, false);
    const std::string fid = resp.at("id_map").at("$0").get<std::string>();

    // Enter the sketch (warms the solve state the drag re-uses).
    json enter = doc_sketch_enter(doc_id, fid);
    REQUIRE(radius_of(enter.at("entities"), "e1") == doctest::Approx(R0));

    // Drag the rim to a 25 mm radius: the host pre-computes the target RADIUS into xy[0].
    const double R1 = 25.0;
    json drag = doc_sketch_drag(doc_id, fid, "e1.rim", json::array({R1, 0.0}), json());

    // The drag must NOT report a rejected handle, and MUST carry entities (the absence of
    // `entities` is exactly what wiped the on-screen sketch).
    REQUIRE_MESSAGE(!(drag.contains("ok") && !drag.at("ok").get<bool>()),
        "rim drag was rejected -> the host would wipe the sketch: " << drag.dump());
    REQUIRE_MESSAGE(drag.contains("entities"), "drag result missing entities: " << drag.dump());
    const double rDragged = radius_of(drag.at("entities"), "e1");
    REQUIRE_MESSAGE(rDragged == doctest::Approx(R1).epsilon(0.05),
        "rim drag should pull the radius toward " << R1 << " but got " << rDragged);

    // Commit must persist the resized circle into the document.
    json commit = doc_sketch_drag_commit(doc_id, fid);
    // Re-enter and confirm the persisted radius survived the commit (not snapped back to R0).
    json reenter = doc_sketch_enter(doc_id, fid);
    const double rFinal = radius_of(reenter.at("entities"), "e1");
    REQUIRE_MESSAGE(rFinal == doctest::Approx(R1).epsilon(0.05),
        "committed radius must persist at " << R1 << " but was " << rFinal);
    // The bug let go without resizing at all -> radius stayed R0. Guard that explicitly.
    CHECK(std::abs(rFinal - R0) > 1.0);
}

TEST_CASE("a failed drag preserves the sketch entities (never wipes the overlay)") {
    json nr = doc_new("part", "DragNoWipe");
    const std::string doc_id = nr.at("doc_id").get<std::string>();
    json ops = json::array({
        json{{"op", "add_feature"}, {"feature",
            json{{"type", "sketch"}, {"name", "S"},
                 {"params", {{"entities", json::array({circle("e1", 0, 0, 10.0)})},
                             {"constraints", json::array({rel("c1", "fix", {"e1.center"})})}}},
                 {"refs", {{"plane", "plane.top"}}}}}},
    });
    json resp = doc_apply_ops(doc_id, -1, ops, false);
    const std::string fid = resp.at("id_map").at("$0").get<std::string>();
    doc_sketch_enter(doc_id, fid);

    // A handle that cannot resolve must still return the (frozen) entities so the host
    json drag = doc_sketch_drag(doc_id, fid, "nonexistent99.rim", json::array({5.0, 0.0}), json());
    const bool kept = drag.contains("entities") && drag.at("entities").is_array() &&
                      !drag.at("entities").empty();
    REQUIRE_MESSAGE(kept,
        "a failed drag must still carry entities so the sketch is not wiped: " << drag.dump());
}
