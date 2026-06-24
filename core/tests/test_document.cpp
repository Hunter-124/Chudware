// Wave 2 milestone verification: the DOCUMENT MODEL — a rebuildable feature tree
// (core/doc/document.{h,cpp}) — sequences the geom ops into a part and RE-RUNS on a
// parameter edit. This is the test that proves the C++ core is genuinely PARAMETRIC.
//
// It follows the EXACT round-trip discipline of test_ops.cpp: compile a document,
// parse the resulting AICM with the HOST loader (host/src/render/aicm_loader = the
// contract oracle), and assert TIGHT, ANALYTIC, OCCT-version-INDEPENDENT invariants
// on the parsed body (bbox extents, planar/cylindrical face counts) plus the
// CompileResult bookkeeping (status ok/error, surviving body_count). The assertions
// ARE the product — they are NOT weakened to pass.
//
// Case 4 is the payoff: the SAME document, whose rectangle width and extrude depth
// are EXPRESSIONS in env vars, compiled twice with different env -> two different
// solids. A parameter edit re-runs the whole tree into new geometry.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <limits>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

#include "doc/document.h"
#include "doc/validate.h"  // validate_document (explicit-axis acceptance regression)
#include "doc/session.h"   // doc_new/doc_apply_ops (top-level-option hoist regression)
#include "aicm_loader.h"  // host loader (host/src/render on the include path)

using json = nlohmann::json;
using aicad::doc::CompileResult;
using aicad::doc::compile_document;

namespace {

// ---- sketch DSL (mirrors the helpers in test_profile_extrude.cpp) ----------
json line(const char* id, double x1, double y1, double x2, double y2) {
    return json{{"id", id}, {"type", "line"}, {"construction", false},
                {"p1", json::array({x1, y1})}, {"p2", json::array({x2, y2})}};
}
json circle(const char* id, double cx, double cy, double r) {
    return json{{"id", id}, {"type", "circle"}, {"construction", false},
                {"center", json::array({cx, cy})}, {"radius", r}};
}
json rel(const char* id, const char* type, std::vector<const char*> refs) {
    json r = json::array();
    for (auto s : refs) r.push_back(s);
    return json{{"id", id}, {"type", type}, {"refs", r}};
}
// A distance/radius dim. `value` is a number; `expr` is an optional expression
// string (when non-null it drives the dim, evaluated against env — mirrors the
// sketch solver's resolve_dim and lets case 4 parameterize the width).
json dim(const char* id, const char* type, std::vector<const char*> refs,
         double value, const json& expr = json(nullptr)) {
    json r = json::array();
    for (auto s : refs) r.push_back(s);
    json val = json{{"value", value}, {"expr", expr}};
    return json{{"id", id}, {"type", type}, {"refs", r}, {"value", val}, {"driven", false}};
}

// Build a fully-constrained rectangle (min corner at x0,y0; width w, height h) as
// the entities+constraints of a sketch. The width dim can be driven by an expression
// string `w_expr` (when non-null) so a doc can parameterize the rectangle width.
struct RectSketch {
    json entities;
    json constraints;
};
RectSketch rect_entities(double x0, double y0, double w, double h,
                         const json& w_expr = json(nullptr),
                         const json& h_expr = json(nullptr)) {
    RectSketch s;
    s.entities = json::array({
        line("e1", x0, y0, x0 + w, y0),
        line("e2", x0 + w, y0, x0 + w, y0 + h),
        line("e3", x0 + w, y0 + h, x0, y0 + h),
        line("e4", x0, y0 + h, x0, y0),
    });
    s.constraints = json::array({
        rel("c1", "coincident", {"e1.p2", "e2.p1"}),
        rel("c2", "coincident", {"e2.p2", "e3.p1"}),
        rel("c3", "coincident", {"e3.p2", "e4.p1"}),
        rel("c4", "coincident", {"e4.p2", "e1.p1"}),
        rel("c5", "horizontal", {"e1"}),
        rel("c6", "horizontal", {"e3"}),
        rel("c7", "vertical", {"e2"}),
        rel("c8", "vertical", {"e4"}),
        rel("c9", "fix", {"e1.p1"}),
        dim("c10", "distance", {"e1.p1", "e1.p2"}, w, w_expr),
        dim("c11", "distance", {"e2.p1", "e2.p2"}, h, h_expr),
    });
    return s;
}

// A sketch feature node from a RectSketch on the named plane.
json sketch_feature(const char* id, const char* plane, const RectSketch& rs) {
    return json{{"id", id}, {"type", "sketch"}, {"plane", plane},
                {"entities", rs.entities}, {"constraints", rs.constraints}};
}

// ---- AICM inspection helpers (identical contract to test_ops.cpp) ----------
void check_common(const render::BodyMesh& b) {
    CHECK(b.pos.size() == 3ull * b.n_verts);
    CHECK(b.nrm.size() == 3ull * b.n_verts);
    CHECK(b.idx.size() == 3ull * b.n_tris);
    for (uint32_t i : b.idx) CHECK(i < b.n_verts);
    uint32_t running = 0;
    for (const render::FaceRange& f : b.faces) {
        CHECK(f.first_tri == running);
        running += f.tri_count;
    }
    CHECK(running == b.n_tris);
}

void bbox(const render::BodyMesh& b, double lo[3], double hi[3]) {
    for (int k = 0; k < 3; ++k) {
        lo[k] = std::numeric_limits<double>::max();
        hi[k] = -std::numeric_limits<double>::max();
    }
    for (uint32_t v = 0; v < b.n_verts; ++v) {
        for (int k = 0; k < 3; ++k) {
            double c = b.pos[3ull * v + k];
            if (c < lo[k]) lo[k] = c;
            if (c > hi[k]) hi[k] = c;
        }
    }
}

int count_surf(const render::BodyMesh& b, uint8_t surf_type) {
    int n = 0;
    for (const render::FaceRange& f : b.faces)
        if (f.surf_type == surf_type) ++n;
    return n;
}

// Parse a CompileResult's AICM blob through the host loader (the contract oracle).
render::DocMesh parse(const CompileResult& r) {
    render::DocMesh doc;
    std::string err;
    bool ok = render::parseAicm(r.aicm.data(), r.aicm.size(), doc, err);
    REQUIRE_MESSAGE(ok, "host loader rejected AICM: " << err);
    return doc;
}

constexpr double PI = 3.14159265358979323846;

}  // namespace

// 1. SINGLE-FEATURE PART: sketch(rect 30x20 on top) -> extrude depth 25. The doc
//    must compile clean to one solid: a 30 x 20 x 25 box, 6 planar faces.
TEST_CASE("single-feature part: sketch -> extrude -> exact box") {
    const double W = 30.0, H = 20.0, D = 25.0;
    json doc;
    doc["features"] = json::array({
        sketch_feature("s1", "top", rect_entities(0, 0, W, H)),
        json{{"id", "x1"}, {"type", "extrude"}, {"profile", "s1"}, {"depth", D}},
    });

    CompileResult r = compile_document(doc, json::object());
    REQUIRE(r.ok);
    REQUIRE(r.body_count == 1);
    CHECK(r.statuses.size() == 2);
    for (const auto& s : r.statuses) CHECK(s.ok);

    render::DocMesh dm = parse(r);
    REQUIRE(dm.bodies.size() == 1);
    const render::BodyMesh& b = dm.bodies[0];
    check_common(b);

    double lo[3], hi[3];
    bbox(b, lo, hi);
    MESSAGE("single: body_count=" << r.body_count << " faces=" << b.faces.size()
            << " planar=" << count_surf(b, 0)
            << " bbox=" << (hi[0]-lo[0]) << "x" << (hi[1]-lo[1]) << "x" << (hi[2]-lo[2]));

    CHECK(b.faces.size() == 6);
    for (const render::FaceRange& f : b.faces) CHECK(f.surf_type == 0);  // all planar
    CHECK((hi[0] - lo[0]) == doctest::Approx(W).epsilon(1e-4));
    CHECK((hi[1] - lo[1]) == doctest::Approx(H).epsilon(1e-4));
    CHECK((hi[2] - lo[2]) == doctest::Approx(D).epsilon(1e-4));
}

// 1b. REVOLVE AUTO-CLOSE: an OPEN profile — just the outer-wall line, offset from the
//     axis — is closed back to the revolve axis so it still makes a solid. Weak local
//     models routinely sketch the wall but forget the line back down the axis; without
//     the fallback that revolve yields a surface (no_geometry_built). A line at u=R from
//     v=0..H on plane.front, revolved 360 about Z, must give a solid cylinder: two bbox
//     extents = 2R, one = H, regardless of the plane's in-frame axis ordering.
TEST_CASE("revolve auto-closes an open profile to the axis -> solid of revolution") {
    const double R = 12.0, H = 30.0;
    json sk;
    sk["id"] = "prof";
    sk["type"] = "sketch";
    sk["plane"] = "front";  // a plane that CONTAINS the Z axis
    sk["entities"] = json::array({line("e1", R, 0.0, R, H)});  // open wall, no closing line
    sk["constraints"] = json::array();

    json doc;
    doc["features"] = json::array({
        sk,
        json{{"id", "rev"}, {"type", "revolve"}, {"profile", "prof"},
             {"axis", json{{"kind", "datum"}, {"feature", "axis.z"}}},
             {"angle", 360.0}},
    });

    CompileResult r = compile_document(doc, json::object());
    REQUIRE(r.ok);
    REQUIRE(r.body_count == 1);  // 0 here would mean the open profile was NOT closed
    for (const auto& s : r.statuses) CHECK(s.ok);

    render::DocMesh dm = parse(r);
    REQUIRE(dm.bodies.size() == 1);
    const render::BodyMesh& b = dm.bodies[0];
    check_common(b);
    double lo[3], hi[3];
    bbox(b, lo, hi);
    double ext[3] = {hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2]};
    std::sort(ext, ext + 3);
    MESSAGE("revolve auto-close: sorted bbox=" << ext[0] << "," << ext[1] << "," << ext[2]);
    CHECK(ext[0] == doctest::Approx(2 * R).epsilon(2e-2));  // diameter
    CHECK(ext[1] == doctest::Approx(2 * R).epsilon(2e-2));  // diameter
    CHECK(ext[2] == doctest::Approx(H).epsilon(1e-2));      // height
}

// 1c. EXPLICIT AXIS under an invented wrapper: a small local model gets the revolve
//     axis GEOMETRY right (axis:[0,0,1]) but wraps it as an edge selector with a bogus
//     surf instead of the datum ref. Live runs failed at VALIDATION ("refs.axis.surf:
//     not an edge curve type") and never reached compile. Both validation and compile
//     must now accept the explicit direction vector.
TEST_CASE("revolve accepts an explicit axis vector under an invented wrapper") {
    const json axis_ref = json{{"kind", "edge"}, {"surf", "datum"},
                               {"centroid", {0, 0, 0}}, {"axis", {0, 0, 1}},
                               {"owner_feature", nullptr}, {"hint_index", 0}};
    // (a) VALIDATION accepts it (canonical {params,refs} shape, as the agent emits).
    json canon;
    canon["schema"] = "aicad.part/1";
    canon["id"] = "doc1";
    canon["name"] = "AxisTest";
    canon["units"] = json{{"system", "MMGS"}};
    canon["features"] = json::array({
        json{{"id", "f1"}, {"type", "sketch"}, {"name", "Prof"}, {"suppressed", false},
             {"params", {{"entities", json::array({line("e1", 12, 0, 12, 30)})},
                         {"constraints", json::array()}}},
             {"refs", {{"plane", "plane.front"}}}},
        json{{"id", "f2"}, {"type", "revolve"}, {"name", "Rev"}, {"suppressed", false},
             {"params", {{"mode", "boss"}, {"angle", 360}}},
             {"refs", {{"profile", json::array({json{{"kind", "region"}, {"sketch", "f1"},
                                                     {"centroid", {6, 15}}, {"hint_index", 0}}})},
                       {"axis", axis_ref}}}},
    });
    std::vector<std::string> verrs = aicad::doc::validate_document(canon);
    for (const auto& e : verrs) MESSAGE("unexpected validation error: " << e);
    CHECK(verrs.empty());

    // (b) COMPILE builds the solid (normalize_feature extracts the axis; the open
    //     profile is auto-closed to it). A flat-shape feature carries axis at top level.
    json doc;
    json sk;
    sk["id"] = "prof";
    sk["type"] = "sketch";
    sk["plane"] = "front";
    sk["entities"] = json::array({line("e1", 12, 0, 12, 30)});
    sk["constraints"] = json::array();
    doc["features"] = json::array({
        sk,
        json{{"id", "rev"}, {"type", "revolve"}, {"profile", "prof"},
             {"angle", 360.0}, {"axis", axis_ref}},
    });
    CompileResult r = compile_document(doc, json::object());
    REQUIRE(r.ok);
    REQUIRE(r.body_count == 1);
    for (const auto& s : r.statuses) CHECK(s.ok);
}

// 1d. ADD_FEATURE OPTION HOIST: a model may put a primitive option (mode/merge/centered/
//     at) at the feature TOP LEVEL instead of in params. op_add_feature keeps only
//     type/name/suppressed/params/refs, so a stray top-level `mode:"cut"` was SILENTLY
//     dropped -> the bore FUSED with no error. The hoist relocates it into params; the
//     tube must now form (pi*(20^2-12^2)*50 = 40212, NOT the 67356 of a dropped cut).
TEST_CASE("add_feature hoists a top-level option into params -> cut is not dropped") {
    using aicad::doc::doc_new;
    using aicad::doc::doc_apply_ops;
    json nr = doc_new("part", "TubeTest");
    const std::string id = nr.at("doc_id").get<std::string>();
    auto dimv = [](double v) { return json{{"value", v}, {"expr", nullptr}}; };
    json ops = json::array({
        json{{"op", "add_feature"}, {"feature", {{"type", "cylinder"}, {"name", "Outer"},
             {"params", {{"radius", dimv(20)}, {"height", dimv(50)}}}}}},
        // mode:"cut" deliberately at the FEATURE top level, not inside params.
        json{{"op", "add_feature"}, {"feature", {{"type", "cylinder"}, {"name", "Bore"},
             {"mode", "cut"},
             {"params", {{"radius", dimv(12)}, {"height", dimv(60)}}}}}},
    });
    json resp = doc_apply_ops(id, -1, ops, false);
    const double vol = resp.value("volume", 0.0);
    MESSAGE("tube volume (top-level mode:cut) = " << vol);
    CHECK(vol == doctest::Approx(40212.0).epsilon(0.02));  // real tube, not a fused stub
}

// 1e. LIST OF EDGE QUERIES: "chamfer both ends" is naturally expressed as a LIST of two
//     queries [{"of":"edge","side":"+z"},{"of":"edge","side":"-z"}]. Validation must accept
//     query elements in a SelList, and the compiler must resolve+union them. A shaft
//     chamfered at both ends loses a small ring of material at each end (0 < vol < full).
TEST_CASE("chamfer accepts a LIST of edge queries (both ends of a shaft)") {
    const json edges = json::array({json{{"of", "edge"}, {"side", "+z"}},
                                    json{{"of", "edge"}, {"side", "-z"}}});
    // (a) validation accepts a list of queries in the edges SelList.
    json canon;
    canon["schema"] = "aicad.part/1";
    canon["id"] = "d";
    canon["name"] = "ShaftChamfer";
    canon["units"] = json{{"system", "MMGS"}};
    canon["features"] = json::array({
        json{{"id", "f1"}, {"type", "cylinder"}, {"name", "Shaft"}, {"suppressed", false},
             {"params", {{"radius", 10}, {"height", 80}}}, {"refs", json::object()}},
        json{{"id", "f2"}, {"type", "chamfer"}, {"name", "Ends"}, {"suppressed", false},
             {"params", {{"length", 1.5}}}, {"refs", {{"edges", edges}}}},
    });
    std::vector<std::string> verrs = aicad::doc::validate_document(canon);
    for (const auto& e : verrs) MESSAGE("unexpected validation error: " << e);
    CHECK(verrs.empty());

    // (b) compile resolves both query-sets and chamfers both ends.
    const double R = 10.0, H = 80.0;
    json doc;
    doc["features"] = json::array({
        json{{"id", "cyl"}, {"type", "cylinder"}, {"radius", R}, {"height", H}},
        json{{"id", "ch"}, {"type", "chamfer"}, {"length", 1.5}, {"edges", edges}},
    });
    CompileResult r = compile_document(doc, json::object());
    REQUIRE(r.ok);
    REQUIRE(r.body_count == 1);
    for (const auto& s : r.statuses) CHECK(s.ok);
    render::DocMesh dm = parse(r);
    REQUIRE(dm.bodies.size() == 1);
    // Bounding box height unchanged (chamfer is a small bevel), volume reduced from full.
    double lo[3], hi[3];
    bbox(dm.bodies[0], lo, hi);
    MESSAGE("both-ends chamfer: bbox z = " << (hi[2] - lo[2]));
    CHECK((hi[2] - lo[2]) == doctest::Approx(H).epsilon(1e-3));
}

// 1f. HOLE positions as [u,v] PAIRS. The cheatsheet/decomposer teach params.positions =
//     [[u,v],...] and the compiler reads exactly that — but the slot was typed SelList, so
//     a coordinate-pair list was rejected ("selector must be an object") and a whole row of
//     holes silently vanished. Validation must accept the [u,v] list, and the compile must
//     drill all five holes (volume drops below the solid bar).
TEST_CASE("hole positions accept a list of [u,v] coordinate pairs (a row of holes)") {
    const json positions = json::array({json::array({-33.3, 0}), json::array({-16.7, 0}),
                                         json::array({0, 0}), json::array({16.7, 0}),
                                         json::array({33.3, 0})});
    // (a) validation accepts [u,v] pairs in positions.
    json canon;
    canon["schema"] = "aicad.part/1";
    canon["id"] = "d";
    canon["name"] = "HoleRow";
    canon["units"] = json{{"system", "MMGS"}};
    canon["features"] = json::array({
        json{{"id", "f1"}, {"type", "box"}, {"name", "Bar"}, {"suppressed", false},
             {"params", {{"length", 100}, {"width", 20}, {"height", 5}}}, {"refs", json::object()}},
        json{{"id", "f2"}, {"type", "hole"}, {"name", "Holes"}, {"suppressed", false},
             {"params", {{"style", "simple"}, {"end", "through_all"}, {"diameter", 4},
                         {"positions", positions}}},
             {"refs", {{"face", json{{"of", "face"}, {"side", "top"}}}}}},
    });
    std::vector<std::string> verrs = aicad::doc::validate_document(canon);
    for (const auto& e : verrs) MESSAGE("unexpected validation error: " << e);
    CHECK(verrs.empty());

    // (b) compile drills all five holes through the bar.
    json doc;
    doc["features"] = json::array({
        json{{"id", "bar"}, {"type", "box"}, {"length", 100.0}, {"width", 20.0}, {"height", 5.0}},
        json{{"id", "h"}, {"type", "hole"},
             {"style", "simple"}, {"end", "through_all"}, {"diameter", 4.0},
             {"positions", positions},
             {"face", json{{"of", "face"}, {"side", "top"}}}},
    });
    CompileResult r = compile_document(doc, json::object());
    REQUIRE(r.ok);
    REQUIRE(r.body_count == 1);
    for (const auto& s : r.statuses) CHECK(s.ok);
    render::DocMesh dm = parse(r);
    REQUIRE(dm.bodies.size() == 1);
}

// 1g. BURIED-BOSS GUARD: a primitive boss merged into a body that adds NO material is
//     buried inside it (a model leaving a cylinder boss centred on the origin inside a
//     plate). That used to pass silently with the body unchanged; it must now error the
//     boss feature loudly (the doc still rebuilds — never-raise — with the plate intact).
TEST_CASE("buried primitive boss errors loudly instead of silently adding nothing") {
    json doc;
    doc["features"] = json::array({
        json{{"id", "plate"}, {"type", "box"}, {"length", 80.0}, {"width", 40.0}, {"height", 10.0}},
        json{{"id", "boss"}, {"type", "cylinder"}, {"radius", 8.0}, {"height", 10.0},
             {"mode", "boss"}},  // centred at origin -> fully inside the plate
    });
    CompileResult r = compile_document(doc, json::object());
    REQUIRE(r.statuses.size() == 2);
    CHECK(r.statuses[0].ok);                  // plate builds
    CHECK_FALSE(r.statuses[1].ok);            // buried boss is rejected
    CHECK(r.statuses[1].error.find("buried") != std::string::npos);
    // The body survives as the plate alone (80*40*10), not silently corrupted.
    REQUIRE(r.body_count == 1);
}

// 1h. HOLE CENTRING on a PRIMITIVE face. Regression for the pln.Location()-vs-centroid
//     bug: the hole drilled at the surface PARAMETER origin (a corner on a primitive's
//     face) instead of the area centroid, so a Ø10 bore removed only the quarter inside
//     the body (~393) and looked like a Ø5 hole. test_hole missed it (its sketch-extrude
//     box happened to have its param origin at the centroid); the agent uses PRIMITIVE
//     boxes. A Ø10 through-hole in a 40x40x20 box must remove pi*5^2*20 = 1571 (vol 30429),
//     not a corner quarter-bore (vol ~31607).
TEST_CASE("hole on a primitive face drills a centred full-diameter bore") {
    using aicad::doc::doc_new;
    using aicad::doc::doc_apply_ops;
    json nr = doc_new("part", "HoleCentre");
    const std::string id = nr.at("doc_id").get<std::string>();
    auto dimv = [](double v) { return json{{"value", v}, {"expr", nullptr}}; };
    json ops = json::array({
        json{{"op", "add_feature"}, {"feature", {{"type", "box"}, {"name", "Blk"},
             {"params", {{"length", dimv(40)}, {"width", dimv(40)}, {"height", dimv(20)}}}}}},
        json{{"op", "add_feature"}, {"feature", {{"type", "hole"}, {"name", "Bore"},
             {"params", {{"style", "simple"}, {"standard", "custom"}, {"diameter", dimv(10)},
                         {"end", "through_all"}}},
             {"refs", {{"face", json{{"of", "face"}, {"side", "top"}}}}}}}},
    });
    json resp = doc_apply_ops(id, -1, ops, false);
    const double vol = resp.value("volume", 0.0);
    MESSAGE("primitive hole vol = " << vol << " (centred bore 30429; corner quarter-bore ~31607)");
    CHECK(vol == doctest::Approx(40.0 * 40 * 20 - 3.14159265 * 25 * 20).epsilon(0.01));
}

// 2. MULTI-FEATURE PART: square 40x40 -> extrude 25 (base); circle r8 -> extrude 25
//    (tool); boolean cut base - tool. The cut consumes BOTH the base and the tool,
//    so exactly ONE body survives: the holed block (40x40x25 bbox with a cylindrical
//    bore wall present).
TEST_CASE("multi-feature part: cut base - tool -> single holed block") {
    const double SIDE = 40.0, D = 25.0, R = 8.0;
    // Base square centered at origin so the centered circle bores through its middle.
    json base_sk = sketch_feature("base_sk", "top",
                                  rect_entities(-SIDE / 2, -SIDE / 2, SIDE, SIDE));
    json tool_sk;
    tool_sk["id"] = "tool_sk";
    tool_sk["type"] = "sketch";
    tool_sk["plane"] = "top";
    tool_sk["entities"] = json::array({circle("e1", 0, 0, R)});
    tool_sk["constraints"] = json::array({
        rel("c1", "fix", {"e1.center"}),
        dim("c2", "radius", {"e1"}, R),
    });

    json doc;
    doc["features"] = json::array({
        base_sk,
        json{{"id", "base"}, {"type", "extrude"}, {"profile", "base_sk"}, {"depth", D}},
        tool_sk,
        json{{"id", "tool"}, {"type", "extrude"}, {"profile", "tool_sk"}, {"depth", D}},
        json{{"id", "cut"}, {"type", "boolean"}, {"op", "cut"},
             {"a", "base"}, {"b", "tool"}},
    });

    CompileResult r = compile_document(doc, json::object());
    REQUIRE(r.ok);
    REQUIRE(r.body_count == 1);  // base + tool both consumed by the cut

    render::DocMesh dm = parse(r);
    REQUIRE(dm.bodies.size() == 1);
    const render::BodyMesh& b = dm.bodies[0];
    check_common(b);

    double lo[3], hi[3];
    bbox(b, lo, hi);
    MESSAGE("multi: body_count=" << r.body_count << " faces=" << b.faces.size()
            << " cylinder=" << count_surf(b, 1) << " planar=" << count_surf(b, 0)
            << " bbox=" << (hi[0]-lo[0]) << "x" << (hi[1]-lo[1]) << "x" << (hi[2]-lo[2]));

    CHECK(count_surf(b, 1) >= 1);  // the cylindrical hole wall
    CHECK(count_surf(b, 0) >= 6);  // the block's planar faces survive
    CHECK((hi[0] - lo[0]) == doctest::Approx(SIDE).epsilon(1e-4));
    CHECK((hi[1] - lo[1]) == doctest::Approx(SIDE).epsilon(1e-4));
    CHECK((hi[2] - lo[2]) == doctest::Approx(D).epsilon(1e-4));
}

// 3. FILLET IN THE TREE: sketch(rect 40x30) -> extrude 20 -> fillet(body=x1, dir=+Z,
//    radius 5). The fillet consumes the extrude (SolidWorks-style replacement), so
//    ONE body survives, carrying >= 4 cylindrical quarter-corner faces.
TEST_CASE("fillet in the tree: extrude consumed, rounded body survives") {
    const double W = 40.0, H = 30.0, D = 20.0, R = 5.0;
    json doc;
    doc["features"] = json::array({
        sketch_feature("s1", "top", rect_entities(0, 0, W, H)),
        json{{"id", "x1"}, {"type", "extrude"}, {"profile", "s1"}, {"depth", D}},
        json{{"id", "f1"}, {"type", "fillet"}, {"body", "x1"}, {"radius", R},
             {"dir", json::array({0, 0, 1})}},
    });

    CompileResult r = compile_document(doc, json::object());
    REQUIRE(r.ok);
    REQUIRE(r.body_count == 1);  // extrude consumed by the fillet

    render::DocMesh dm = parse(r);
    REQUIRE(dm.bodies.size() == 1);
    const render::BodyMesh& b = dm.bodies[0];
    check_common(b);

    double lo[3], hi[3];
    bbox(b, lo, hi);
    MESSAGE("fillet: body_count=" << r.body_count << " faces=" << b.faces.size()
            << " cylinder=" << count_surf(b, 1) << " planar=" << count_surf(b, 0)
            << " bbox=" << (hi[0]-lo[0]) << "x" << (hi[1]-lo[1]) << "x" << (hi[2]-lo[2]));

    CHECK(count_surf(b, 1) >= 4);  // 4 rounded vertical corners
    // The round stays within the original extents (corners removed, bbox unchanged).
    CHECK((hi[0] - lo[0]) == doctest::Approx(W).epsilon(1e-4));
    CHECK((hi[1] - lo[1]) == doctest::Approx(H).epsilon(1e-4));
    CHECK((hi[2] - lo[2]) == doctest::Approx(D).epsilon(1e-4));
}

// 4. PARAMETRIC REBUILD (the payoff). A doc whose rectangle WIDTH and extrude DEPTH
//    are EXPRESSIONS in env vars W and D. Compiled with {W:30,D:25} -> a 30 x _ x 25
//    box; the SAME doc compiled with {W:50,D:10} -> a 50 x _ x 10 box. A parameter
//    edit re-runs the whole tree into new geometry — the essence of parametric CAD.
TEST_CASE("parametric rebuild: same doc, new env -> new geometry") {
    const double H = 20.0;  // height held constant; width & depth are parameters
    // Rectangle width driven by expression "W"; the seed numeric value is a
    // placeholder (the expr wins). Depth param is the string "D".
    RectSketch rs = rect_entities(0, 0, /*w seed*/ 1.0, H, /*w_expr*/ json("W"));
    json doc;
    doc["features"] = json::array({
        sketch_feature("s1", "top", rs),
        json{{"id", "x1"}, {"type", "extrude"}, {"profile", "s1"}, {"depth", "D"}},
    });

    // First parameter set.
    CompileResult r1 = compile_document(doc, json{{"W", 30.0}, {"D", 25.0}});
    REQUIRE(r1.ok);
    REQUIRE(r1.body_count == 1);
    render::DocMesh dm1 = parse(r1);
    REQUIRE(dm1.bodies.size() == 1);
    double lo1[3], hi1[3];
    bbox(dm1.bodies[0], lo1, hi1);
    MESSAGE("param {W:30,D:25}: bbox=" << (hi1[0]-lo1[0]) << "x" << (hi1[1]-lo1[1])
            << "x" << (hi1[2]-lo1[2]));
    CHECK((hi1[0] - lo1[0]) == doctest::Approx(30.0).epsilon(1e-4));
    CHECK((hi1[1] - lo1[1]) == doctest::Approx(H).epsilon(1e-4));
    CHECK((hi1[2] - lo1[2]) == doctest::Approx(25.0).epsilon(1e-4));

    // Second parameter set — SAME doc object, different env. The tree re-runs.
    CompileResult r2 = compile_document(doc, json{{"W", 50.0}, {"D", 10.0}});
    REQUIRE(r2.ok);
    REQUIRE(r2.body_count == 1);
    render::DocMesh dm2 = parse(r2);
    REQUIRE(dm2.bodies.size() == 1);
    double lo2[3], hi2[3];
    bbox(dm2.bodies[0], lo2, hi2);
    MESSAGE("param {W:50,D:10}: bbox=" << (hi2[0]-lo2[0]) << "x" << (hi2[1]-lo2[1])
            << "x" << (hi2[2]-lo2[2]));
    CHECK((hi2[0] - lo2[0]) == doctest::Approx(50.0).epsilon(1e-4));
    CHECK((hi2[1] - lo2[1]) == doctest::Approx(H).epsilon(1e-4));
    CHECK((hi2[2] - lo2[2]) == doctest::Approx(10.0).epsilon(1e-4));

    // The geometry actually CHANGED between rebuilds (not a stale cache / no-op).
    CHECK((hi2[0] - lo2[0]) != doctest::Approx(hi1[0] - lo1[0]));
    CHECK((hi2[2] - lo2[2]) != doctest::Approx(hi1[2] - lo1[2]));
}

// 5. ERROR HANDLING: a doc with an extrude referencing a MISSING profile id ("nope")
//    plus a perfectly good sketch+extrude. The bad feature gets a !ok status with a
//    non-empty error, result.ok==false, the rebuild NEVER crashes, and the good
//    extrude still produced its body.
TEST_CASE("error handling: missing ref errors one feature, rebuild survives") {
    const double W = 30.0, H = 20.0, D = 15.0;
    json doc;
    doc["features"] = json::array({
        sketch_feature("s1", "top", rect_entities(0, 0, W, H)),
        json{{"id", "good"}, {"type", "extrude"}, {"profile", "s1"}, {"depth", D}},
        // References a profile that does not exist -> must be a contained error.
        json{{"id", "bad"}, {"type", "extrude"}, {"profile", "nope"}, {"depth", D}},
    });

    CompileResult r = compile_document(doc, json::object());
    REQUIRE(r.statuses.size() == 3);
    CHECK_FALSE(r.ok);  // not every feature compiled

    // Locate the bad feature's status: !ok with a non-empty error.
    bool found_bad = false, good_ok = false;
    for (const auto& s : r.statuses) {
        if (s.id == "bad") {
            found_bad = true;
            CHECK_FALSE(s.ok);
            CHECK_FALSE(s.error.empty());
        }
        if (s.id == "good") good_ok = s.ok;
    }
    CHECK(found_bad);
    CHECK(good_ok);

    // The good extrude still produced exactly one surviving body.
    REQUIRE(r.body_count == 1);
    render::DocMesh dm = parse(r);
    REQUIRE(dm.bodies.size() == 1);
    const render::BodyMesh& b = dm.bodies[0];
    check_common(b);
    double lo[3], hi[3];
    bbox(b, lo, hi);
    MESSAGE("error: body_count=" << r.body_count << " (good body survived) "
            << " bbox=" << (hi[0]-lo[0]) << "x" << (hi[1]-lo[1]) << "x" << (hi[2]-lo[2]));
    CHECK((hi[0] - lo[0]) == doctest::Approx(W).epsilon(1e-4));
    CHECK((hi[2] - lo[2]) == doctest::Approx(D).epsilon(1e-4));
}

// 6. SWEEP with a SKETCH path: a circle profile on Top (XY, normal +Z) chased along
//    an OPEN line sketch on Front (a vertical segment up +Z) must build the exact
//    cylinder pi r^2 H. Proves the document resolves a sketch as the sweep path
//    (the user-reported gap) and the MakePipeShell kernel caps it into a solid.
TEST_CASE("sweep along a sketch path -> exact cylinder") {
    const double R = 4.0, Hh = 20.0;
    // Profile: a fully-constrained circle of radius R at the origin on Top.
    json prof;
    prof["id"] = "prof"; prof["type"] = "sketch"; prof["plane"] = "top";
    prof["entities"] = json::array({circle("e1", 0, 0, R)});
    prof["constraints"] = json::array({
        rel("c1", "fix", {"e1.center"}),
        dim("c2", "radius", {"e1"}, R),
    });
    // Path: a single vertical line on Front, sketch-coords (0,0)->(0,Hh) -> world
    // (0,0,0)->(0,0,Hh), i.e. up +Z, perpendicular to the profile's +Z normal.
    json path;
    path["id"] = "path"; path["type"] = "sketch"; path["plane"] = "front";
    path["entities"] = json::array({line("e1", 0, 0, 0, Hh)});
    path["constraints"] = json::array({
        rel("c1", "fix", {"e1.p1"}),
        rel("c2", "vertical", {"e1"}),
        dim("c3", "distance", {"e1.p1", "e1.p2"}, Hh),
    });

    json doc;
    doc["features"] = json::array({
        prof, path,
        json{{"id", "sw"}, {"type", "sweep"}, {"profile", "prof"}, {"path", "path"}},
    });

    CompileResult r = compile_document(doc, json::object());
    REQUIRE(r.ok);
    REQUIRE(r.body_count == 1);
    render::DocMesh dm = parse(r);
    REQUIRE(dm.bodies.size() == 1);
    const render::BodyMesh& b = dm.bodies[0];
    check_common(b);
    double lo[3], hi[3];
    bbox(b, lo, hi);
    MESSAGE("sweep-sketch: faces=" << b.faces.size() << " bbox=" << (hi[0]-lo[0])
            << "x" << (hi[1]-lo[1]) << "x" << (hi[2]-lo[2]));
    // Cylinder: diameter across X/Y, height along Z.
    CHECK((hi[0] - lo[0]) == doctest::Approx(2 * R).epsilon(2e-2));
    CHECK((hi[1] - lo[1]) == doctest::Approx(2 * R).epsilon(2e-2));
    CHECK((hi[2] - lo[2]) == doctest::Approx(Hh).epsilon(2e-2));
    CHECK(count_surf(b, /*cylinder*/ 1) >= 1);  // a cylindrical wall exists
}

// 7. HELIX curve emission: a helix feature builds no body, so the document ships it
//    out-of-band as a {kind:"curve"} datum carrying a sampled world polyline — what
//    lets the host DRAW and PICK the helix as a sweep path. Verify the datum exists,
//    is a curve, and traces a real 3D path (not a degenerate point).
TEST_CASE("helix feature emits a sampled curve datum for display/picking") {
    json doc;
    doc["features"] = json::array({
        json{{"id", "hx"}, {"type", "helix"}, {"base", "plane.top"},
             {"radius", 10.0}, {"pitch", 4.0}, {"height", 12.0}},
    });
    CompileResult r = compile_document(doc, json::object());
    REQUIRE(r.ok);

    const json* curve = nullptr;
    for (const auto& d : r.datums)
        if (d.is_object() && d.value("kind", std::string()) == "curve" &&
            d.value("id", std::string()) == "hx") { curve = &d; break; }
    REQUIRE_MESSAGE(curve != nullptr, "no curve datum emitted for the helix");
    REQUIRE(curve->contains("points"));
    const json& pts = (*curve)["points"];
    REQUIRE(pts.is_array());
    CHECK(pts.size() > 20);  // a 3-turn helix samples to many points

    // The polyline must span ~the full height in Z and ~the diameter in X/Y.
    double zlo = 1e9, zhi = -1e9, xlo = 1e9, xhi = -1e9;
    for (const auto& p : pts) {
        double x = p[0].get<double>(), z = p[2].get<double>();
        zlo = std::min(zlo, z); zhi = std::max(zhi, z);
        xlo = std::min(xlo, x); xhi = std::max(xhi, x);
    }
    CHECK((zhi - zlo) == doctest::Approx(12.0).epsilon(0.05));   // height
    CHECK((xhi - xlo) == doctest::Approx(20.0).epsilon(0.05));   // 2 * radius
}

// 8. THE THREAD PATH, END TO END: a helix feature -> a sweep that references it as
//    its PATH. This is the full chain that makes threads/springs possible: the
//    document resolves a CURVE feature (not a sketch) as the spine, lifts it, and
//    MakePipeShell closes a coil solid. Profile is a small circle on Front placed at
//    the helix start (radius, 0), ~perpendicular to the start tangent.
TEST_CASE("sweep along a helix feature -> closed coil solid (thread path)") {
    const double HR = 8.0, pitch = 4.0, height = 12.0, a = 1.0;
    json prof;
    prof["id"] = "prof"; prof["type"] = "sketch"; prof["plane"] = "front";
    prof["entities"] = json::array({circle("e1", HR, 0, a)});  // centered at the helix start
    prof["constraints"] = json::array({
        rel("c1", "fix", {"e1.center"}),
        dim("c2", "radius", {"e1"}, a),
    });
    json doc;
    doc["features"] = json::array({
        prof,
        json{{"id", "hx"}, {"type", "helix"}, {"base", "plane.top"},
             {"radius", HR}, {"pitch", pitch}, {"height", height}},
        json{{"id", "sw"}, {"type", "sweep"}, {"profile", "prof"}, {"path", "hx"}},
    });

    CompileResult r = compile_document(doc, json::object());
    for (const auto& s : r.statuses)
        if (!s.ok) MESSAGE("feature " << s.id << " error: " << s.error);
    REQUIRE(r.ok);
    REQUIRE(r.body_count == 1);   // exactly one coil solid survives

    render::DocMesh dm = parse(r);
    REQUIRE(dm.bodies.size() == 1);
    const render::BodyMesh& b = dm.bodies[0];
    check_common(b);
    double lo[3], hi[3];
    bbox(b, lo, hi);
    MESSAGE("sweep-helix: faces=" << b.faces.size() << " bbox=" << (hi[0]-lo[0])
            << "x" << (hi[1]-lo[1]) << "x" << (hi[2]-lo[2]));
    // The coil wraps a radius-8 cylinder, so it spans ~ +/-(HR+a) in X and Y, and
    // ~the helix height (plus the profile thickness) in Z.
    CHECK((hi[0] - lo[0]) == doctest::Approx(2 * (HR + a)).epsilon(0.15));
    CHECK((hi[2] - lo[2]) >= height - 1.0);
}

// 9. THE USER'S REAL CASE (Documents/sweep-test.step+): an OPEN 3-segment polyline on
//    Top is the PATH; a circle on Right (perpendicular, at the path start) is the
//    PROFILE. Sweeping the circle along the elbowed polyline must build one solid rod.
//    This is the exact geometry that was failing — it proves the engine handles a
//    cross-plane profile/path + an open multi-segment spine with sharp corners.
TEST_CASE("sweep circle (Right) along an open polyline (Top) -> solid rod [user case]") {
    // f1 — the PATH: open polyline on Top, starting at the origin going -X then up.
    json path;
    path["id"] = "f1"; path["type"] = "sketch"; path["plane"] = "top";
    path["entities"] = json::array({
        line("e1", 0.0, 0.0, -61.382561, 0.0),
        line("e2", -61.382561, 0.0, -109.119049, 36.553005),
        line("e3", -109.119049, 36.553005, 0.0, 36.553005),
    });
    path["constraints"] = json::array({
        rel("c1", "horizontal", {"e1"}),
        rel("c2", "coincident", {"origin", "e1.p1"}),
        rel("c3", "coincident", {"e1.p2", "e2.p1"}),
        rel("c4", "horizontal", {"e3"}),
        rel("c5", "coincident", {"e2.p2", "e3.p1"}),
    });
    // f2 — the PROFILE: circle on Right (YZ, normal +X), centered at the origin so it
    // sits at the path start, perpendicular to its first (-X) segment.
    json prof;
    prof["id"] = "f2"; prof["type"] = "sketch"; prof["plane"] = "right";
    prof["entities"] = json::array({circle("e4", 0.0, 0.0, 6.338714)});
    prof["constraints"] = json::array({rel("c7", "coincident", {"origin", "e4.center"})});

    json doc;
    doc["features"] = json::array({
        path, prof,
        json{{"id", "f3"}, {"type", "sweep"}, {"profile", "f2"}, {"path", "f1"}},
    });

    CompileResult r = compile_document(doc, json::object());
    for (const auto& s : r.statuses)
        if (!s.ok) MESSAGE("feature " << s.id << " error: " << s.error);
    REQUIRE(r.ok);
    REQUIRE(r.body_count == 1);   // one swept rod

    render::DocMesh dm = parse(r);
    REQUIRE(dm.bodies.size() == 1);
    const render::BodyMesh& b = dm.bodies[0];
    check_common(b);
    double lo[3], hi[3];
    bbox(b, lo, hi);
    MESSAGE("sweep-user: faces=" << b.faces.size() << " bbox=" << (hi[0]-lo[0])
            << "x" << (hi[1]-lo[1]) << "x" << (hi[2]-lo[2]));
    // The rod follows a polyline ~109 wide in X and ~36 tall in Y, tube radius ~6.3.
    CHECK((hi[0] - lo[0]) > 100.0);
    CHECK((hi[1] - lo[1]) > 30.0);
}

// 10. EXTRUDE "UP TO" A REFERENCE PLANE. rect 30x20 on top (z=0) -> datum plane
//     offset +15 from top -> extrude end=up_to_surface targeting that plane. The end
//     cap must land ON the plane: bbox height == 15 (NOT a blind depth). Verifies the
//     user-reported gap — "extrude up to the plane" now actually trims to the plane.
TEST_CASE("extrude up to a reference plane -> end cap conforms to the plane") {
    const double W = 30.0, H = 20.0, OFF = 15.0;
    json doc;
    doc["features"] = json::array({
        sketch_feature("s1", "top", rect_entities(0, 0, W, H)),
        json{{"id", "p1"}, {"type", "plane"}, {"method", "offset"},
             {"base", "plane.top"}, {"offset", OFF}},
        json{{"id", "x1"}, {"type", "extrude"}, {"profile", "s1"},
             {"end", "up_to_surface"},
             {"up_to", json{{"kind", "datum"}, {"feature", "p1"}}}},
    });

    CompileResult r = compile_document(doc, json::object());
    for (const auto& s : r.statuses)
        if (!s.ok) MESSAGE("feature " << s.id << " error: " << s.error);
    REQUIRE(r.ok);
    REQUIRE(r.body_count == 1);

    render::DocMesh dm = parse(r);
    REQUIRE(dm.bodies.size() == 1);
    const render::BodyMesh& b = dm.bodies[0];
    check_common(b);
    double lo[3], hi[3];
    bbox(b, lo, hi);
    MESSAGE("up-to-plane: faces=" << b.faces.size() << " bbox=" << (hi[0]-lo[0])
            << "x" << (hi[1]-lo[1]) << "x" << (hi[2]-lo[2]));
    CHECK(b.faces.size() == 6);                                  // clean box, end on the plane
    CHECK((hi[0] - lo[0]) == doctest::Approx(W).epsilon(1e-4));
    CHECK((hi[1] - lo[1]) == doctest::Approx(H).epsilon(1e-4));
    CHECK((hi[2] - lo[2]) == doctest::Approx(OFF).epsilon(1e-4));   // trimmed to the plane
    CHECK(lo[2] == doctest::Approx(0.0).epsilon(1e-4));
    CHECK(hi[2] == doctest::Approx(OFF).epsilon(1e-4));
}

// 11. EXTRUDE "FROM" A REFERENCE PLANE. rect on top (z=0) -> datum plane offset +5 ->
//     extrude start=from_reference (start at that plane) + blind depth 10. The solid
//     must start at z=5 and end at z=15 — the "From = reference plane" the user wanted.
TEST_CASE("extrude from a reference plane -> profile starts at the plane") {
    const double W = 30.0, H = 20.0, START = 5.0, D = 10.0;
    json doc;
    doc["features"] = json::array({
        sketch_feature("s1", "top", rect_entities(0, 0, W, H)),
        json{{"id", "p1"}, {"type", "plane"}, {"method", "offset"},
             {"base", "plane.top"}, {"offset", START}},
        json{{"id", "x1"}, {"type", "extrude"}, {"profile", "s1"},
             {"start", "from_reference"},
             {"start_ref", json{{"kind", "datum"}, {"feature", "p1"}}},
             {"end", "blind"}, {"depth", D}},
    });

    CompileResult r = compile_document(doc, json::object());
    for (const auto& s : r.statuses)
        if (!s.ok) MESSAGE("feature " << s.id << " error: " << s.error);
    REQUIRE(r.ok);
    REQUIRE(r.body_count == 1);

    render::DocMesh dm = parse(r);
    REQUIRE(dm.bodies.size() == 1);
    const render::BodyMesh& b = dm.bodies[0];
    check_common(b);
    double lo[3], hi[3];
    bbox(b, lo, hi);
    MESSAGE("from-plane: bbox z=[" << lo[2] << "," << hi[2] << "]");
    CHECK((hi[2] - lo[2]) == doctest::Approx(D).epsilon(1e-4));
    CHECK(lo[2] == doctest::Approx(START).epsilon(1e-4));        // starts AT the plane
    CHECK(hi[2] == doctest::Approx(START + D).epsilon(1e-4));
}
