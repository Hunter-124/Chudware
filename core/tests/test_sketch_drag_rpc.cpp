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

json line(const char* id, double x1, double y1, double x2, double y2) {
    return json{{"id", id}, {"type", "line"}, {"construction", false},
                {"p1", json::array({x1, y1})}, {"p2", json::array({x2, y2})}};
}
// Find a line's [p1,p2] in an enter/drag `entities` array.
json line_pts(const json& entities, const std::string& id) {
    if (entities.is_array())
        for (const auto& e : entities)
            if (e.value("id", std::string()) == id) return json::array({e.value("p1", json()), e.value("p2", json())});
    return json();
}
// Constraint type by id from an enter response's constraint list (empty if dropped/absent).
std::string cons_type(const json& enter, const std::string& id) {
    const json& cs = enter.contains("constraints") ? enter.at("constraints") : json::array();
    if (cs.is_array())
        for (const auto& c : cs)
            if (c.value("id", std::string()) == id) return c.value("type", std::string());
    return std::string();
}

// Build a fresh part with a sketch: horizontal line e1 [0,0]->[10,0] + a `horizontal` relation c1
// on it, plus a reference circle so the selection has >1 entity. Returns {doc_id, fid}.
std::pair<std::string, std::string> make_hline_sketch() {
    json nr = doc_new("part", "RotHV");
    std::string doc_id = nr.at("doc_id").get<std::string>();
    json ops = json::array({
        json{{"op", "add_feature"}, {"feature",
            json{{"type", "sketch"}, {"name", "S"},
                 {"params", {{"entities", json::array({line("e1", 0, 0, 10, 0),
                                                       circle("e2", 20, 0, 3)})},
                             {"constraints", json::array({rel("c1", "horizontal", {"e1"})})}}},
                 {"refs", {{"plane", "plane.top"}}}}}}});
    json resp = doc_apply_ops(doc_id, -1, ops, false);
    std::string fid = resp.at("id_map").at("$0").get<std::string>();
    return {doc_id, fid};
}
json rotate_op(const char* fid, double cx, double cy, double angle) {
    return json{{"op", "sketch_rotate"}, {"sketch", fid},
                {"entities", json::array({"e1", "e2"})},
                {"cx", cx}, {"cy", cy}, {"angle", angle}, {"copy", false}};
}

// Segment length of a line id in an enter `entities` array.
double seg_len(const json& entities, const std::string& id) {
    if (!entities.is_array()) return -1.0;
    for (const auto& e : entities)
        if (e.value("id", std::string()) == id) {
            const auto& p1 = e.at("p1"); const auto& p2 = e.at("p2");
            return std::hypot(p2[0].get<double>() - p1[0].get<double>(),
                              p2[1].get<double>() - p1[1].get<double>());
        }
    return -1.0;
}
// p1 of a line id.
std::pair<double, double> p1_of(const json& entities, const std::string& id) {
    for (const auto& e : entities)
        if (e.value("id", std::string()) == id)
            return {e.at("p1")[0].get<double>(), e.at("p1")[1].get<double>()};
    return {0, 0};
}

// Build a realistic 80x80 square anchored at the ORIGIN (corner e1.p1 coincident to origin, as the
// rectangle tool emits), fully defined by 4 coincident corners + H/V on all sides + 2 dims.
std::pair<std::string, std::string> make_square_at_origin() {
    json nr = doc_new("part", "RotSquare");
    std::string doc_id = nr.at("doc_id").get<std::string>();
    json ops = json::array({
        json{{"op", "add_feature"}, {"feature",
            json{{"type", "sketch"}, {"name", "S"},
                 {"params", {{"entities", json::array({
                     line("e1", 0, 0, 80, 0), line("e2", 80, 0, 80, 80),
                     line("e3", 80, 80, 0, 80), line("e4", 0, 80, 0, 0)})},
                  {"constraints", json::array({
                     rel("c1", "coincident", {"e1.p2", "e2.p1"}),
                     rel("c2", "coincident", {"e2.p2", "e3.p1"}),
                     rel("c3", "coincident", {"e3.p2", "e4.p1"}),
                     rel("c4", "coincident", {"e4.p2", "e1.p1"}),
                     rel("c5", "horizontal", {"e1"}), rel("c6", "horizontal", {"e3"}),
                     rel("c7", "vertical", {"e2"}),   rel("c8", "vertical", {"e4"}),
                     rel("c9", "coincident", {"e1.p1", "origin"}),   // realistic anchor (straddles moved set)
                     json{{"id","c10"},{"type","distance"},{"refs",{"e1.p1","e1.p2"}},{"value",{{"value",80.0}}}},
                     json{{"id","c11"},{"type","distance"},{"refs",{"e2.p1","e2.p2"}},{"value",{{"value",80.0}}}}})}}},
                 {"refs", {{"plane", "plane.top"}}}}}}});
    json resp = doc_apply_ops(doc_id, -1, ops, false);
    return {doc_id, resp.at("id_map").at("$0").get<std::string>()};
}

}  // namespace

// REPRODUCTION: a fully-defined 80x80 square anchored at the origin, rotated 30deg. The four
// corners must stay a RIGID square — side lengths AND the diagonal preserved (a sheared rhombus
// keeps side lengths but breaks the diagonal, so we assert the diagonal too).
TEST_CASE("sketch_rotate keeps an origin-anchored square rigid (sides + diagonal preserved)") {
    auto [doc_id, fid] = make_square_at_origin();
    json before = doc_sketch_enter(doc_id, fid);
    REQUIRE(seg_len(before.at("entities"), "e1") == doctest::Approx(80.0));

    json rot = json{{"op", "sketch_rotate"}, {"sketch", fid},
                    {"entities", json::array({"e1", "e2", "e3", "e4"})},
                    {"cx", 40.0}, {"cy", 40.0}, {"angle", 30.0}, {"copy", false}};
    doc_apply_ops(doc_id, -1, json::array({rot}), false);
    json after = doc_sketch_enter(doc_id, fid);
    const json& ents = after.at("entities");
    for (const char* id : {"e1", "e2", "e3", "e4"})
        CHECK_MESSAGE(seg_len(ents, id) == doctest::Approx(80.0).epsilon(1e-3),
                      "side " << id << " length changed (distorted): " << seg_len(ents, id));
    // Diagonal between opposite corners e1.p1 and e3.p1 must remain 80*sqrt(2) (rhombus breaks this).
    auto a = p1_of(ents, "e1"); auto b = p1_of(ents, "e3");
    double diag = std::hypot(b.first - a.first, b.second - a.second);
    CHECK_MESSAGE(diag == doctest::Approx(80.0 * std::sqrt(2.0)).epsilon(1e-3),
                  "diagonal changed (square sheared into a rhombus): " << diag);
}

TEST_CASE("sketch_rotate 90deg swaps a horizontal relation to vertical + rotates geometry") {
    auto [doc_id, fid] = make_hline_sketch();
    doc_apply_ops(doc_id, -1, json::array({rotate_op(fid.c_str(), 0, 0, 90.0)}), false);
    json enter = doc_sketch_enter(doc_id, fid);
    CHECK(cons_type(enter, "c1") == "vertical");           // H became V at 90deg
    json p = line_pts(enter.at("entities"), "e1");         // [0,0]-[10,0] -> [0,0]-[0,10]
    CHECK(p[1][0].get<double>() == doctest::Approx(0.0).epsilon(1e-6));
    CHECK(p[1][1].get<double>() == doctest::Approx(10.0).epsilon(1e-6));
}

TEST_CASE("sketch_rotate 45deg drops a horizontal relation (non-axis angle)") {
    auto [doc_id, fid] = make_hline_sketch();
    doc_apply_ops(doc_id, -1, json::array({rotate_op(fid.c_str(), 0, 0, 45.0)}), false);
    json enter = doc_sketch_enter(doc_id, fid);
    CHECK(cons_type(enter, "c1").empty());                 // dropped
}

TEST_CASE("sketch_rotate 180deg keeps a horizontal relation") {
    auto [doc_id, fid] = make_hline_sketch();
    doc_apply_ops(doc_id, -1, json::array({rotate_op(fid.c_str(), 0, 0, 180.0)}), false);
    json enter = doc_sketch_enter(doc_id, fid);
    CHECK(cons_type(enter, "c1") == "horizontal");         // axis preserved
}

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
