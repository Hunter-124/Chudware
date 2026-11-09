// Regression test for the CIRCLE-RIM DRAG-RESIZE bug, exercised through the EXACT RPC
// path the host uses (doc_sketch_enter -> doc_sketch_drag -> doc_sketch_drag_commit),
// NOT the low-level chudware::sketch::drag() the parity test calls directly.
//
// THE BUG (as the user saw it): dragging a circle's rim made the WHOLE sketch vanish
// mid-drag and never resized on release. Root cause was twofold:
//   1. The staged libchudware.so the host dlopen()s was older than the core build, so the
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
using chudware::doc::doc_new;
using chudware::doc::doc_apply_ops;
using chudware::doc::doc_sketch_enter;
using chudware::doc::doc_sketch_drag;
using chudware::doc::doc_sketch_drag_commit;

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
    const auto anchored = p1_of(ents, "e1");
    CHECK(anchored.first == doctest::Approx(0.0).epsilon(1e-6));
    CHECK(anchored.second == doctest::Approx(0.0).epsilon(1e-6));
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

TEST_CASE("axis-aligned rotation remains editable without locking its reference relation") {
    auto [doc_id, fid] = make_hline_sketch();
    json result = doc_apply_ops(doc_id, -1,
        json::array({rotate_op(fid.c_str(), 0, 0, 90.0)}), false);
    std::string rotation;
    for (const auto& feature : result.at("tree").at("features")) {
        if (feature.value("id", std::string()) != fid) continue;
        for (const auto& c : feature.at("params").at("constraints"))
            if (c.value("type", std::string()) == "rotate_point" && !c.contains("angle_ref"))
                rotation = c.at("id").get<std::string>();
    }
    REQUIRE_FALSE(rotation.empty());
    doc_apply_ops(doc_id, -1, json::array({
        {{"op", "sketch_set_dim"}, {"sketch", fid}, {"id", rotation}, {"value", 45.0}}
    }), false);
    const json enter = doc_sketch_enter(doc_id, fid);
    const json p = line_pts(enter.at("entities"), "e1");
    CHECK(p[1][0].get<double>() == doctest::Approx(10.0 / std::sqrt(2.0)).epsilon(1e-6));
    CHECK(p[1][1].get<double>() == doctest::Approx(10.0 / std::sqrt(2.0)).epsilon(1e-6));
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

TEST_CASE("guarded sketch preview preserves foreign edits and undo history") {
    using namespace chudware::doc;
    const std::string doc_id = doc_new("part", "GuardedSketch").at("doc_id");
    const json built = doc_apply_ops(doc_id, -1, json::array({
        json{{"op", "add_feature"}, {"feature",
            json{{"type", "sketch"}, {"name", "S"},
                 {"params", {{"entities", json::array({circle("e1", 0, 0, 10)})},
                             {"constraints", json::array({rel("c1", "fix", {"e1.center"})})}}},
                 {"refs", {{"plane", "plane.top"}}}}}}
    }), false);
    const std::string fid = built.at("id_map").at("$0");
    const json original = doc_tree(doc_id);
    const json enter = doc_sketch_enter(doc_id, fid);
    const auto token = enter.at("session_id").get<std::uint64_t>();
    REQUIRE(token > 0);
    const json drag = doc_sketch_drag(doc_id, fid, "e1.rim", json::array({25.0, 0.0}), json(), token);
    REQUIRE(drag.at("revision") == enter.at("revision"));
    const json peer = doc_apply_ops(doc_id, -1, json::array({
        {{"op", "sketch_edit_entity"}, {"sketch", fid}, {"id", "e1"},
         {"params", {{"radius", 15.0}}}}
    }), false);
    const json before = doc_tree(doc_id);
    for (bool exit : {false, true}) {
        try {
            if (exit) doc_sketch_exit(doc_id, fid, drag.at("revision"), token);
            else doc_sketch_drag_commit(doc_id, fid, drag.at("revision"), token);
            FAIL("stale sketch commit/exit must throw");
        } catch (const OpError& error) {
            CHECK(error.code == "stale_revision");
            CHECK(error.detail.at("current_revision") == peer.at("revision"));
        }
        CHECK(doc_tree(doc_id).at("tree") == before.at("tree"));
        CHECK(doc_tree(doc_id).at("revision") == before.at("revision"));
    }
    CHECK(doc_undo(doc_id).at("tree") == original.at("tree"));
    const json fresh = doc_sketch_solve(doc_id, fid, token);
    const json sample = doc_sketch_drag(doc_id, fid, "e1.rim", json::array({20.0, 0.0}), json(), token);
    CHECK(sample.at("revision") == fresh.at("revision"));
    const json committed = doc_sketch_drag_commit(doc_id, fid, sample.at("revision"), token);
    CHECK(committed.at("revision").get<long>() > fresh.at("revision").get<long>());
    const json exited = doc_sketch_exit(doc_id, fid, committed.at("revision"), token);
    CHECK(exited.contains("revision"));
    doc_close(doc_id);
}

namespace {
// The status row of feature `id` in an ops response (null when absent).
json status_of(const json& response, const std::string& id) {
    for (const auto& row : response.at("rebuild").at("feature_status"))
        if (row.value("id", std::string()) == id) return row;
    return json();
}
// Construction geometry never changes the profile regions the extrude consumes.
json construction_line(const char* id, double y) {
    return json{{"id", id}, {"type", "line"}, {"construction", true},
                {"p1", json::array({100.0, y})}, {"p2", json::array({120.0, y})}};
}
size_t entity_count(const json& tree, const std::string& fid) {
    for (const auto& f : tree.at("features"))
        if (f.value("id", std::string()) == fid) return f.at("params").at("entities").size();
    return 0;
}
}  // namespace

// An interactive sketch session compiles the history only up to its sketch: the edit reports every
// later feature rolled back, leaves no rollback bar in the document, keeps one undo step per edit,
// and sketch_exit rebuilds the complete history under a new revision.
TEST_CASE("sketch session edits roll the history back to the sketch until exit") {
    using namespace chudware::doc;
    auto [doc_id, fid] = make_square_at_origin();
    const json built = doc_apply_ops(doc_id, -1, json::array({
        json{{"op", "add_feature"}, {"feature", json{{"type", "extrude"}, {"name", "E"},
            {"params", {{"depth", 5}, {"mode", "new"}}},
            {"refs", {{"profile", json::array({json{{"kind", "region"}, {"sketch", fid},
                                                    {"hint_index", 0}}})}}}}}},
        json{{"op", "add_feature"}, {"feature", json{{"type", "box"}, {"name", "B"},
            {"params", {{"length", 5}, {"width", 5}, {"height", 5}}}}}}}), false);
    const std::string extrude = built.at("id_map").at("$0").get<std::string>();
    const std::string box = built.at("id_map").at("$1").get<std::string>();
    REQUIRE_MESSAGE(status_of(built, box).value("ok", false), built.dump());
    const size_t before = entity_count(built.at("tree"), fid);

    const auto session = doc_sketch_enter(doc_id, fid).at("session_id").get<std::uint64_t>();
    const json add = json::array({json{{"op", "sketch_add_entity"}, {"sketch", fid},
                                       {"entity", construction_line("e9", 0)}}});
    const json edit = doc_apply_ops(doc_id, -1, add, false, true, false, session, false);
    CHECK(status_of(edit, fid).value("rolled_back", true) == false);
    CHECK(status_of(edit, extrude).value("rolled_back", false));
    CHECK(status_of(edit, box).value("rolled_back", false));
    CHECK(edit.at("rebuild").at("doc_status").at("rolled_back_to") == fid);
    CHECK_FALSE(edit.contains("exact"));
    CHECK_FALSE(edit.at("tree").contains("rollback_after"));
    CHECK(entity_count(edit.at("tree"), fid) == before + 1);

    // The sketch's own queries keep working against the rolled-back view.
    CHECK(doc_sketch_solve(doc_id, fid, session).at("entities").size() == before + 1);
    CHECK(doc_sketch_regions(doc_id, fid).is_object());

    // Without the session the same kind of batch rebuilds everything.
    const json other = doc_apply_ops(doc_id, -1, json::array({json{{"op", "sketch_add_entity"},
        {"sketch", fid}, {"entity", construction_line("e10", 5)}}}), false);
    CHECK_FALSE(status_of(other, box).value("rolled_back", true));
    CHECK(doc_undo(doc_id).at("tree") == edit.at("tree"));

    // Exit rolls forward under a new revision; the edit is one undo step.
    const json shown = doc_apply_ops(doc_id, -1, json::array({json{{"op", "sketch_add_entity"},
        {"sketch", fid}, {"entity", construction_line("e11", 10)}}}), false, true, false, session, false);
    doc_sketch_solve(doc_id, fid, session);  // the editor re-solves after every edit
    const json exited = doc_sketch_exit(doc_id, fid, shown.at("revision"), session);
    CHECK(exited.at("revision").get<long>() > shown.at("revision").get<long>());
    CHECK_FALSE(status_of(exited, extrude).value("rolled_back", true));
    CHECK_FALSE(status_of(exited, box).value("rolled_back", true));
    CHECK_FALSE(exited.at("tree").contains("rollback_after"));
    CHECK(entity_count(doc_undo(doc_id).at("tree"), fid) == before + 1);
    doc_close(doc_id);
}

// Convert Entities and the associative references it records resolve against the history through
// the edited sketch, so they keep the session's edits rolled back instead of forcing the complete
// rebuild. A property-manager preview of a feature behaves the same way until its final edit.
TEST_CASE("sketch session and preview keep convert references in the rolled-back view") {
    using namespace chudware::doc;
    auto [doc_id, source] = make_square_at_origin();
    const json built = doc_apply_ops(doc_id, -1, json::array({
        json{{"op", "add_feature"}, {"feature", json{{"type", "sketch"}, {"name", "B"},
            {"params", {{"entities", json::array({construction_line("e1", 0)})},
                        {"constraints", json::array()}}},
            {"refs", {{"plane", "plane.top"}}}}}},
        json{{"op", "add_feature"}, {"feature", json{{"type", "extrude"}, {"name", "E"},
            {"params", {{"depth", 5}, {"mode", "new"}}},
            {"refs", {{"profile", json::array({json{{"kind", "region"}, {"sketch", source},
                                                    {"hint_index", 0}}})}}}}}},
        json{{"op", "add_feature"}, {"feature", json{{"type", "box"}, {"name", "Bx"},
            {"params", {{"length", 5}, {"width", 5}, {"height", 5}}}}}}}), false);
    const std::string target = built.at("id_map").at("$0").get<std::string>();
    const std::string extrude = built.at("id_map").at("$1").get<std::string>();
    const std::string box = built.at("id_map").at("$2").get<std::string>();
    REQUIRE_MESSAGE(status_of(built, box).value("ok", false), built.dump());

    const auto session = doc_sketch_enter(doc_id, target).at("session_id").get<std::uint64_t>();
    const json convert = doc_apply_ops(doc_id, -1, json::array({json{{"op", "sketch_convert"},
        {"sketch", target},
        {"refs", json::array({json{{"kind", "sketch_entity"}, {"sketch", source}, {"entity", "e1"}}})}}}),
        false, true, false, session, false);
    CHECK(convert.at("rebuild").at("doc_status").at("rolled_back_to") == target);
    CHECK(status_of(convert, box).value("rolled_back", false));
    bool has_refs = false;
    for (const auto& f : convert.at("tree").at("features"))
        if (f.value("id", std::string()) == target)
            has_refs = f.at("params").contains("external_refs") && !f.at("params").at("external_refs").empty();
    CHECK(has_refs);
    // The sketch now carries associative references; its next edit is still rolled back.
    const json next = doc_apply_ops(doc_id, -1, json::array({json{{"op", "sketch_add_entity"},
        {"sketch", target}, {"entity", construction_line("e20", 15)}}}),
        false, true, false, session, false);
    CHECK(next.at("rebuild").at("doc_status").at("rolled_back_to") == target);
    CHECK(status_of(next, box).value("rolled_back", false));
    doc_sketch_solve(doc_id, target, session);
    const json exited = doc_sketch_exit(doc_id, target, next.at("revision"), session);
    CHECK_FALSE(status_of(exited, box).value("rolled_back", true));
    CHECK_FALSE(exited.at("tree").contains("rollback_after"));

    // A property-manager preview of the extrude rebuilds only through it.
    const json edit = json::array({json{{"op", "edit_feature"}, {"id", extrude},
                                        {"params", {{"depth", 6}}}}});
    const json preview = doc_apply_ops(doc_id, -1, edit, false, true, false, 0, false, extrude);
    CHECK(status_of(preview, box).value("rolled_back", false));
    CHECK_FALSE(status_of(preview, extrude).value("rolled_back", true));
    CHECK(preview.at("rebuild").at("doc_status").at("rolled_back_to") == extrude);
    CHECK_FALSE(preview.at("tree").contains("rollback_after"));
    const json final_edit = doc_apply_ops(doc_id, -1, edit, true);
    CHECK_FALSE(status_of(final_edit, box).value("rolled_back", true));
    CHECK_FALSE(final_edit.at("rebuild").at("doc_status").contains("rolled_back_to"));
    doc_close(doc_id);
}
