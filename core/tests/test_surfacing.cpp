// Regression: the ruled / fill / boundary surface ops must accept BODY-EDGE
// selectors — the exact refs the ribbon's K_EDGE wells emit — not just sketch/region
// refs. Before the fix, `surf_sketch_wire()` funnelled every ref through require_sketch,
// so a body-edge pick threw "… is a solid body, not a sketch" and the whole
// "Curves & Patches" group was dead from the GUI (found by a surfaces audit).
//
// These cases drive the DOCUMENT COMPILE path (compile_document) with the SAME selector
// json that `make_selector` stamps for a viewport edge pick — harvested off the body's
// live topology via describe_edges, exactly like test_picks.cpp's fillet-by-selector
// case. They assert the op now builds a surface body, AND that the pre-existing
// sketch-ref path still works (the fix is additive, not a swap).
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <cmath>
#include <cstdint>
#include <limits>
#include <set>
#include <string>
#include <tuple>
#include <vector>

#include <nlohmann/json.hpp>

#include "doc/document.h"
#include "doc/picks.h"
#include "doc/selectors.h"
#include "geom/features.h"  // make_box (harvest the body-edge selectors off raw topology)

using nlohmann::json;
using chudware::doc::CompileResult;
using chudware::doc::compile_document;

namespace {

// ---- sketch DSL (subset copied from test_picks.cpp) -------------------------
json line(const char* id, double x1, double y1, double x2, double y2) {
    return json{{"id", id}, {"type", "line"}, {"construction", false},
                {"p1", json::array({x1, y1})}, {"p2", json::array({x2, y2})}};
}
json rel(const char* id, const char* type, std::vector<const char*> refs) {
    json r = json::array();
    for (auto s : refs) r.push_back(s);
    return json{{"id", id}, {"type", type}, {"refs", r}};
}
json dim(const char* id, const char* type, std::vector<const char*> refs, double value) {
    json r = json::array();
    for (auto s : refs) r.push_back(s);
    json val = json{{"value", value}, {"expr", json(nullptr)}};
    return json{{"id", id}, {"type", type}, {"refs", r}, {"value", val}, {"driven", false}};
}
// A single straight line segment sketch on `plane` from (x0,y0) to (x1,y1) — a ruled
// rail as a SKETCH ref (for the backward-compat case). One entity, endpoints fixed.
json seg_sketch(const char* id, const char* plane,
                double x0, double y0, double x1, double y1) {
    return json{{"id", id}, {"type", "sketch"}, {"plane", plane},
                {"entities", json::array({line("e1", x0, y0, x1, y1)})},
                {"constraints", json::array({
                    rel("c1", "fix", {"e1.p1"}),
                    rel("c2", "fix", {"e1.p2"}),
                })}};
}

json corner_box(const char* id, double L, double W, double H) {
    return json{{"id", id}, {"type", "box"}, {"length", L}, {"width", W},
                {"height", H}, {"centered", false}};
}

// Harvest the body-edge selector for the single VERTICAL (+/-Z) edge of a box
// [0,W]x[0,H]x[0,D] whose foot sits at (cx,cy). describe_edges enumerates each edge
// once per incident face, so we match on axis (vertical) + rounded xy centroid.
json vertical_edge_selector_at(double W, double H, double D, double cx, double cy) {
    TopoDS_Shape box = geom::make_box(W, H, D);
    std::vector<chudware::doc::TopoInfo> infos = chudware::doc::describe_edges(box);
    for (const chudware::doc::TopoInfo& info : infos) {
        if (!info.axis.has_value()) continue;
        if (std::abs(info.axis->z()) < 0.999) continue;              // not vertical
        if (std::abs(info.centroid.x() - cx) > 1e-4) continue;
        if (std::abs(info.centroid.y() - cy) > 1e-4) continue;
        return chudware::doc::make_selector(info);
    }
    return json(nullptr);
}

}  // namespace

// (1) THE BUG: surf_ruled between two BODY EDGES. A box's two front-face vertical edges
//     (feet at (0,0) and (W,0)) are picked as edge_a / edge_b — the exact K_EDGE-well
//     selectors a viewport pick produces. The ruled surface between them is the box's
//     front face: a planar patch at y=0 spanning x in [0,W], z in [0,D]. Pre-fix this
//     threw "edge_a -> 'x1' is a solid body, not a sketch"; the feature must now build a
//     surface body (the box survives alongside it -> 2 bodies).
TEST_CASE("surf_ruled from two body edges builds a surface") {
    const double W = 40.0, H = 30.0, D = 20.0;

    json ea = vertical_edge_selector_at(W, H, D, 0.0, 0.0);
    json eb = vertical_edge_selector_at(W, H, D, W, 0.0);
    REQUIRE_FALSE(ea.is_null());
    REQUIRE_FALSE(eb.is_null());

    json doc;
    doc["features"] = json::array({
        corner_box("x1", W, H, D),
        json{{"id", "r1"}, {"type", "surf_ruled"}, {"edge_a", ea}, {"edge_b", eb}},
    });

    CompileResult r = compile_document(doc, json::object());
    REQUIRE(r.ok);
    // Locate the ruled feature's status and assert it succeeded (not "solid body,
    // not a sketch"). The box also compiles ok; both bodies survive.
    bool found = false;
    for (const auto& s : r.statuses) {
        if (s.id == "r1") { found = true; CHECK_MESSAGE(s.ok, "surf_ruled failed: " << s.error); }
        CHECK(s.ok);
    }
    CHECK(found);
    CHECK(r.body_count == 2);  // box + the new ruled surface body
}

// (2) surf_fill from the FOUR body edges of the box's top face -> a filled planar patch
//     bounded by that face's perimeter. Same body-edge selectors, the fill/boundary code
//     path (shares surf_sketch_wire). Proves the fix covers all three edge-fed forms, not
//     just ruled. Uses the top-face edges (z=D) as a closed boundary loop.
TEST_CASE("surf_fill from four body edges builds a patch") {
    const double W = 40.0, H = 30.0, D = 20.0;

    // Harvest the 4 top-face (z=D) edges, deduped by rounded centroid.
    TopoDS_Shape box = geom::make_box(W, H, D);
    std::vector<chudware::doc::TopoInfo> infos = chudware::doc::describe_edges(box);
    json edges = json::array();
    std::set<std::tuple<long long, long long, long long>> seen;
    for (const chudware::doc::TopoInfo& info : infos) {
        if (std::abs(info.centroid.z() - D) > 1e-4) continue;  // not on the top face
        auto key = std::make_tuple(
            static_cast<long long>(std::llround(info.centroid.x() * 1e4)),
            static_cast<long long>(std::llround(info.centroid.y() * 1e4)),
            static_cast<long long>(std::llround(info.centroid.z() * 1e4)));
        if (!seen.insert(key).second) continue;
        edges.push_back(chudware::doc::make_selector(info));
    }
    REQUIRE(edges.size() == 4);  // the 4 perimeter edges of the top face

    json doc;
    doc["features"] = json::array({
        corner_box("x1", W, H, D),
        json{{"id", "p1"}, {"type", "surf_fill"}, {"edges", edges}},
    });

    CompileResult r = compile_document(doc, json::object());
    REQUIRE(r.ok);
    bool found = false;
    for (const auto& s : r.statuses) {
        if (s.id == "p1") { found = true; CHECK_MESSAGE(s.ok, "surf_fill failed: " << s.error); }
        CHECK(s.ok);
    }
    CHECK(found);
    CHECK(r.body_count == 2);  // box + the fill patch
}

// (3) BACKWARD COMPAT: surf_ruled from two SKETCH refs still works after the fix (the
//     change is additive — a sketch owner still routes to the whole-sketch wire path).
//     Two parallel line-segment sketches on offset planes ruled into a strip.
TEST_CASE("surf_ruled from two sketch refs still works (no regression)") {
    json doc;
    doc["features"] = json::array({
        seg_sketch("s1", "top", 0.0, 0.0, 40.0, 0.0),
        seg_sketch("s2", "top", 0.0, 30.0, 40.0, 30.0),
        json{{"id", "r1"}, {"type", "surf_ruled"}, {"edge_a", "s1"}, {"edge_b", "s2"}},
    });

    CompileResult r = compile_document(doc, json::object());
    REQUIRE(r.ok);
    bool found = false;
    for (const auto& s : r.statuses) {
        if (s.id == "r1") { found = true; CHECK_MESSAGE(s.ok, "surf_ruled(sketch) failed: " << s.error); }
        CHECK(s.ok);
    }
    CHECK(found);
}

// (4) surf_boundary from body edges. Shares the exact add_wire_edges -> surf_sketch_wire
//     path as surf_fill, but the ref slot is dir1 (+ optional dir2) — cover it explicitly
//     so the test literally exercises all three edge-fed forms. dir1 =
//     the four top-face edges as a boundary loop.
TEST_CASE("surf_boundary from body edges builds a patch") {
    const double W = 40.0, H = 30.0, D = 20.0;

    TopoDS_Shape box = geom::make_box(W, H, D);
    std::vector<chudware::doc::TopoInfo> infos = chudware::doc::describe_edges(box);
    json dir1 = json::array();
    std::set<std::tuple<long long, long long, long long>> seen;
    for (const chudware::doc::TopoInfo& info : infos) {
        if (std::abs(info.centroid.z() - D) > 1e-4) continue;  // top-face edges only
        auto key = std::make_tuple(
            static_cast<long long>(std::llround(info.centroid.x() * 1e4)),
            static_cast<long long>(std::llround(info.centroid.y() * 1e4)),
            static_cast<long long>(std::llround(info.centroid.z() * 1e4)));
        if (!seen.insert(key).second) continue;
        dir1.push_back(chudware::doc::make_selector(info));
    }
    REQUIRE(dir1.size() == 4);

    json doc;
    doc["features"] = json::array({
        corner_box("x1", W, H, D),
        json{{"id", "b1"}, {"type", "surf_boundary"}, {"dir1", dir1}},
    });

    CompileResult r = compile_document(doc, json::object());
    REQUIRE(r.ok);
    bool found = false;
    for (const auto& s : r.statuses) {
        if (s.id == "b1") { found = true; CHECK_MESSAGE(s.ok, "surf_boundary failed: " << s.error); }
        CHECK(s.ok);
    }
    CHECK(found);
    CHECK(r.body_count == 2);  // box + the boundary patch
}
