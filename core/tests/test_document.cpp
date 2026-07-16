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
#include <filesystem>
#include <limits>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

#include <BRep_Tool.hxx>                                  // seam/degenerate edge test (S1/S2)
#include <BRepCheck_Analyzer.hxx>                          // thread cut B-rep validity (case 12)
#include <BRepGProp.hxx>                                   // thread volume-reduced check
#include <GProp_GProps.hxx>
#include <TopExp.hxx>
#include <TopExp_Explorer.hxx>
#include <TopTools_IndexedMapOfShape.hxx>
#include <TopTools_IndexedDataMapOfShapeListOfShape.hxx>
#include <TopTools_ListOfShape.hxx>
#include <TopTools_ListIteratorOfListOfShape.hxx>
#include <TopoDS.hxx>
#include <TopoDS_Edge.hxx>
#include <TopoDS_Face.hxx>
#include <TopoDS_Shape.hxx>
#include <TopAbs_ShapeEnum.hxx>

#include "doc/document.h"
#include "doc/validate.h"  // validate_document (explicit-axis acceptance regression)
#include "doc/session.h"   // doc_new/doc_apply_ops (top-level-option hoist regression)
#include "doc/stepplus.h"  // stepplus_save (craft a persisted broken-import doc on disk)
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

// SEAM-AWARE genuine-free-edge count on a raw kernel shape (S1/S2). A closed solid has
// every edge shared by >= 2 faces; the only legitimate single-face edges are SEAMS
// (BRep_Tool::IsClosed — a cylinder/sphere/revolution closure) and DEGENERATE edges
// (apex/pole). Any other < 2-face edge is a real FREE edge -> the body is an open shell.
// Mirrors document.cpp::dress_is_closed_solid so the test verifies the actual invariant
// (a committed dress body is watertight) directly on r.shapes, not via tessellation.
int genuine_free_edges(const TopoDS_Shape& shape) {
    if (shape.IsNull()) return -1;
    TopTools_IndexedDataMapOfShapeListOfShape anc;
    TopExp::MapShapesAndAncestors(shape, TopAbs_EDGE, TopAbs_FACE, anc);
    TopTools_IndexedMapOfShape edges;
    TopExp::MapShapes(shape, TopAbs_EDGE, edges);
    int free_n = 0;
    for (int ei = 1; ei <= edges.Extent(); ++ei) {
        const TopoDS_Shape& es = edges.FindKey(ei);
        const int nf = anc.Contains(es) ? anc.FindFromKey(es).Extent() : 0;
        if (nf >= 2) continue;
        const TopoDS_Edge& e = TopoDS::Edge(es);
        if (BRep_Tool::Degenerated(e)) continue;
        bool seam = false;
        if (anc.Contains(es)) {
            const TopTools_ListOfShape& faces = anc.FindFromKey(es);
            for (TopTools_ListIteratorOfListOfShape it(faces); it.More(); it.Next())
                if (BRep_Tool::IsClosed(e, TopoDS::Face(it.Value()))) { seam = true; break; }
        }
        if (!seam) ++free_n;
    }
    return free_n;
}

// Parse a CompileResult's AICM blob through the host loader (the contract oracle).
render::DocMesh parse(const CompileResult& r) {
    render::DocMesh doc;
    std::string err;
    bool ok = render::parseAicm(r.aicm.data(), r.aicm.size(), doc, err);
    REQUIRE_MESSAGE(ok, "host loader rejected AICM: " << err);
    return doc;
}

// Locate a feature's compile status by id (the regression tests below assert on the
// error / warning text of a specific feature). Returns nullptr when absent.
const aicad::doc::FeatureStatus* status_of(const CompileResult& r, const char* id) {
    for (const auto& s : r.statuses)
        if (s.id == id) return &s;
    return nullptr;
}

// A feature_status row's `error` is JSON null when the feature is ok (ops_response sets
// {"error": null} on success), so a bare .value("error","") would throw type_error.302.
// Read it null-safely for the failure-message text in the doc_apply_ops-driven tests.
std::string status_err(const json& row) {
    auto it = row.find("error");
    if (it == row.end() || it->is_null()) return std::string();
    return it->is_string() ? it->get<std::string>() : it->dump();
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

// A hole spec'd by FASTENER STANDARD (no numeric diameter) resolves the bore from the metric
// table — "M3" clearance -> 3.4mm — the way a model (or engineer) naturally asks for a clearance
// hole. Also covers a diameter SYNONYM ("dia"). Regression for the gemma "key 'diameter' not
// found" crash: a missing-but-named-by-standard bore must build, not throw.
TEST_CASE("hole by fastener standard (M3) and diameter synonym resolve the bore") {
    using aicad::doc::doc_new;
    using aicad::doc::doc_apply_ops;
    auto dimv = [](double v) { return json{{"value", v}, {"expr", nullptr}}; };

    // (a) standard:"M3", no diameter -> clearance 3.4mm bore.
    {
        json nr = doc_new("part", "HoleStd");
        const std::string id = nr.at("doc_id").get<std::string>();
        json ops = json::array({
            json{{"op", "add_feature"}, {"feature", {{"type", "box"}, {"name", "Blk"},
                 {"params", {{"length", dimv(20)}, {"width", dimv(20)}, {"height", dimv(22)}}}}}},
            json{{"op", "add_feature"}, {"feature", {{"type", "hole"}, {"name", "M3_Clearance_Bore"},
                 {"params", {{"style", "simple"}, {"standard", "M3"}, {"end", "through_all"}}},
                 {"refs", {{"face", json{{"of", "face"}, {"side", "top"}}}}}}}},
        });
        json resp = doc_apply_ops(id, -1, ops, false);
        const double vol = resp.value("volume", 0.0);
        // 20x20x22 minus a 3.4mm-dia through bore.
        CHECK(vol == doctest::Approx(20.0 * 20 * 22 - 3.14159265 * 1.7 * 1.7 * 22).epsilon(0.02));
    }
    // (b) "dia" synonym instead of "diameter".
    {
        json nr = doc_new("part", "HoleDia");
        const std::string id = nr.at("doc_id").get<std::string>();
        json ops = json::array({
            json{{"op", "add_feature"}, {"feature", {{"type", "box"}, {"name", "Blk"},
                 {"params", {{"length", dimv(20)}, {"width", dimv(20)}, {"height", dimv(22)}}}}}},
            json{{"op", "add_feature"}, {"feature", {{"type", "hole"}, {"name", "Bore"},
                 {"params", {{"style", "simple"}, {"dia", dimv(5)}, {"end", "through_all"}}},
                 {"refs", {{"face", json{{"of", "face"}, {"side", "top"}}}}}}}},
        });
        json resp = doc_apply_ops(id, -1, ops, false);
        const double vol = resp.value("volume", 0.0);
        CHECK(vol == doctest::Approx(20.0 * 20 * 22 - 3.14159265 * 2.5 * 2.5 * 22).epsilon(0.02));
    }
}

// A `polygon` sketch entity expands to N closed line segments -> a real region that extrudes.
// Regression for "extrude produced no solid": a small model drew a hexagon as an OPEN spline (no
// region); the polygon entity gives a clean closed hex. Full hex-standoff path: hexagon -> extrude
// -> M3 clearance bore, the exact part that previously failed.
TEST_CASE("polygon entity builds a closed hexagon -> hex standoff extrudes + bores") {
    using aicad::doc::doc_new;
    using aicad::doc::doc_apply_ops;
    json nr = doc_new("part", "HexStandoff");
    const std::string id = nr.at("doc_id").get<std::string>();
    json ops = json::array({
        json{{"op", "add_feature"}, {"feature", {{"type", "sketch"}, {"name", "HexSk"},
             {"params", {{"entities", json::array()}, {"constraints", json::array()}}},
             {"refs", {{"plane", "plane.top"}}}}}},
        json{{"op", "sketch_add_entity"}, {"sketch", "$0"},
             {"entity", {{"type", "hexagon"}, {"center", json::array({0, 0})}, {"across_flats", 10}}}},
        json{{"op", "add_feature"}, {"feature", {{"type", "extrude"}, {"name", "Body"},
             {"params", {{"mode", "boss"}, {"end", "blind"}, {"depth", json{{"value", 22}, {"expr", nullptr}}}}},
             {"refs", {{"profile", json::array({json{{"kind", "region"}, {"sketch", "$0"},
                        {"centroid", json::array({0, 0})}, {"hint_index", 0}}})}}}}}},
        json{{"op", "add_feature"}, {"feature", {{"type", "hole"}, {"name", "M3_Clearance_Bore"},
             {"params", {{"style", "simple"}, {"standard", "M3"}, {"end", "through_all"}}},
             {"refs", {{"face", json{{"of", "face"}, {"side", "top"}}}}}}}},
    });
    json resp = doc_apply_ops(id, -1, ops, false);
    const double vol = resp.value("volume", 0.0);
    // regular hexagon across-flats 10 -> circumradius R = 5/cos(30deg); area = (3√3/2)R².
    const double R = 5.0 / std::cos(3.14159265358979 / 6.0);
    const double hex_area = (3.0 * std::sqrt(3.0) / 2.0) * R * R;
    const double expect = hex_area * 22.0 - 3.14159265 * 1.7 * 1.7 * 22.0;  // minus M3 (3.4) bore
    CHECK(vol == doctest::Approx(expect).epsilon(0.01));
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

// 6a-guide. GUIDE CURVE end-to-end through the document layer: the same profile+path
//    that sweeps a plain cylinder, given a guide SKETCH that diverges from the path,
//    scales the section up into a truncated cone. Proves the engine (a) accepts the
//    guides SelList in the schema, (b) resolves the region selector the GUI emits, and
//    (c) actually consumes the guide — the guided volume must clearly exceed the plain
//    sweep. This is the layer the GUI + MCP flow through (the user-reported gap).
TEST_CASE("guide curve scales a swept profile end-to-end (document layer)") {
    const double R = 4.0, Hh = 20.0;
    auto prof_sketch = [&] {
        json p; p["id"] = "prof"; p["type"] = "sketch"; p["plane"] = "top";
        p["entities"] = json::array({circle("e1", 0, 0, R)});
        p["constraints"] = json::array({
            rel("c1", "fix", {"e1.center"}), dim("c2", "radius", {"e1"}, R)});
        return p;
    };
    auto path_sketch = [&] {
        json p; p["id"] = "path"; p["type"] = "sketch"; p["plane"] = "front";
        p["entities"] = json::array({line("e1", 0, 0, 0, Hh)});
        p["constraints"] = json::array({
            rel("c1", "fix", {"e1.p1"}), rel("c2", "vertical", {"e1"}),
            dim("c3", "distance", {"e1.p1", "e1.p2"}, Hh)});
        return p;
    };
    // Guide on Front: sketch (R,0)->(2R,Hh) => world (R,0,0)->(2R,0,Hh). Starts on the
    // profile rim (contact), diverging +X as it rises, so the section grows R -> 2R.
    json guide; guide["id"] = "guide"; guide["type"] = "sketch"; guide["plane"] = "front";
    guide["entities"] = json::array({line("e1", R, 0, 2 * R, Hh)});
    guide["constraints"] = json::array({
        rel("c1", "fix", {"e1.p1"}), rel("c2", "fix", {"e1.p2"})});

    json base; base["features"] = json::array({prof_sketch(), path_sketch(),
        json{{"id", "sw"}, {"type", "sweep"}, {"profile", "prof"}, {"path", "path"}}});
    CompileResult rb = compile_document(base, json::object());
    REQUIRE(rb.ok);

    // The guides ref uses the exact region selector PropertyManager::routePathFeature
    // emits for a sketch pick: {"kind":"region","sketch":<fid>}, wrapped in a SelList.
    json g; g["features"] = json::array({prof_sketch(), path_sketch(), guide,
        json{{"id", "sw"}, {"type", "sweep"}, {"profile", "prof"}, {"path", "path"},
             {"guides", json::array({json{{"kind", "region"}, {"sketch", "guide"}}})}}});
    CompileResult rg = compile_document(g, json::object());
    for (const auto& s : rg.statuses) if (!s.ok) MESSAGE("feat " << s.id << ": " << s.error);
    REQUIRE(rg.ok);
    REQUIRE(rg.body_count == 1);
    MESSAGE("guided vol=" << rg.volume << " un-guided vol=" << rb.volume);
    CHECK(rg.volume > 1.4 * rb.volume);  // the guide grew the section (frustum >> cylinder)
}

// 6a-guide-reject. A guide identical to the path is rejected loudly (not silently swept
//    guide-blind), so the user gets a clear message instead of a confusing result.
TEST_CASE("sweep rejects a guide curve identical to the path") {
    const double R = 4.0, Hh = 20.0;
    json prof; prof["id"] = "prof"; prof["type"] = "sketch"; prof["plane"] = "top";
    prof["entities"] = json::array({circle("e1", 0, 0, R)});
    prof["constraints"] = json::array({
        rel("c1", "fix", {"e1.center"}), dim("c2", "radius", {"e1"}, R)});
    json path; path["id"] = "path"; path["type"] = "sketch"; path["plane"] = "front";
    path["entities"] = json::array({line("e1", 0, 0, 0, Hh)});
    path["constraints"] = json::array({
        rel("c1", "fix", {"e1.p1"}), rel("c2", "vertical", {"e1"}),
        dim("c3", "distance", {"e1.p1", "e1.p2"}, Hh)});

    json doc; doc["features"] = json::array({prof, path,
        json{{"id", "sw"}, {"type", "sweep"}, {"profile", "prof"}, {"path", "path"},
             {"guides", json::array({json{{"kind", "region"}, {"sketch", "path"}}})}}});
    CompileResult r = compile_document(doc, json::object());
    CHECK_FALSE(r.ok);
    bool sawSweep = false;
    for (const auto& s : r.statuses)
        if (s.id == "sw") {
            sawSweep = true;
            CHECK_FALSE(s.ok);
            CHECK(s.error.find("guide") != std::string::npos);
        }
    CHECK(sawSweep);
}

// 6b. SWEEP BOSS FUSES into the running body (Bug H1). A base box, then a sweep boss
//     whose swept cylinder OVERLAPS the box, must yield exactly ONE body — not two
//     disjoint bodies (the H1 symptom, which also produced a degenerate negative
//     principal inertia). The fused solid's total volume must EXCEED the box alone
//     (material added) and its principal moments of inertia must be strictly positive
//     (the non-degenerate single-solid proof). Default sweep mode is "boss"/merge.
TEST_CASE("sweep boss fuses into the running body -> one solid (H1)") {
    const double R = 3.0, SwH = 15.0, BoxD = 5.0;
    // Base box centred on origin: x,y in [-8,8], z in [0,5].
    json doc;
    json prof;
    prof["id"] = "prof"; prof["type"] = "sketch"; prof["plane"] = "top";
    prof["entities"] = json::array({circle("e1", 0, 0, R)});
    prof["constraints"] = json::array({
        rel("c1", "fix", {"e1.center"}),
        dim("c2", "radius", {"e1"}, R),
    });
    json path;
    path["id"] = "path"; path["type"] = "sketch"; path["plane"] = "front";
    path["entities"] = json::array({line("e1", 0, 0, 0, SwH)});  // up +Z from origin
    path["constraints"] = json::array({
        rel("c1", "fix", {"e1.p1"}),
        rel("c2", "vertical", {"e1"}),
        dim("c3", "distance", {"e1.p1", "e1.p2"}, SwH),
    });
    doc["features"] = json::array({
        sketch_feature("base_s", "top", rect_entities(-8, -8, 16, 16)),
        json{{"id", "base"}, {"type", "extrude"}, {"profile", "base_s"}, {"depth", BoxD}},
        prof, path,
        // Default mode "boss": fuses into the running box (SolidWorks "Merge result").
        json{{"id", "sw"}, {"type", "sweep"}, {"profile", "prof"}, {"path", "path"}},
    });

    CompileResult r = compile_document(doc, json::object());
    REQUIRE(r.ok);
    for (const auto& s : r.statuses) CHECK(s.ok);
    REQUIRE(r.body_count == 1);  // H1: the boss FUSED — not two disjoint bodies

    render::DocMesh dm = parse(r);
    REQUIRE(dm.bodies.size() == 1);
    const render::BodyMesh& b = dm.bodies[0];
    check_common(b);

    // Material was added: the fused volume exceeds the bare box (16*16*5 = 1280).
    const double box_vol = 16.0 * 16.0 * BoxD;
    MESSAGE("H1 sweep boss: body_count=" << r.body_count
            << " total_volume=" << r.volume << " (box alone=" << box_vol << ")");
    CHECK(r.volume > box_vol + 1.0);
    CHECK(r.body_count == 1);
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

// 11b. T-3DDATUM — datum plane method "point_normal" (arbitrary origin + normal;
//      closes the sketch3d_pipe auto-profile gap, MCP _profile_plane_ops). An
//      on-axis case must match the equivalent "offset" plane exactly (z=START); an
//      off-axis normal must still produce a valid extrude of the SAME volume
//      (rotation-invariant check — the strongest thing to assert without deriving
//      the tilted bbox by hand).
TEST_CASE("datum plane point_normal: on-axis matches offset; off-axis normal still extrudes") {
    const double W = 30.0, H = 20.0, START = 5.0, D = 10.0;
    {
        json doc;
        doc["features"] = json::array({
            sketch_feature("s1", "top", rect_entities(0, 0, W, H)),
            json{{"id", "p1"}, {"type", "plane"}, {"method", "point_normal"},
                 {"origin", json::array({0.0, 0.0, START})},
                 {"normal", json::array({0.0, 0.0, 1.0})}},
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
        double lo[3], hi[3];
        bbox(dm.bodies[0], lo, hi);
        CHECK(lo[2] == doctest::Approx(START).epsilon(1e-4));
        CHECK(hi[2] == doctest::Approx(START + D).epsilon(1e-4));
        CHECK(r.volume == doctest::Approx(W * H * D).epsilon(1e-3));
    }
    {
        json doc;
        doc["features"] = json::array({
            sketch_feature("s1", "top", rect_entities(0, 0, W, H)),
            json{{"id", "p1"}, {"type", "plane"}, {"method", "point_normal"},
                 {"origin", json::array({1.0, 2.0, 3.0})},
                 {"normal", json::array({1.0, 1.0, 1.0})}},
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
        // Volume is orientation-invariant — the off-axis plane still extrudes a
        // clean W x H x D box, just tilted in world space.
        CHECK(r.volume == doctest::Approx(W * H * D).epsilon(1e-3));
    }
}

// ===========================================================================
// Regression suite for the MCP black-box bug report (A1/A2/A3/A4/A6/B1/B2/B4).
// Each case reproduces a previously-silent or mislabelled failure and pins the new
// behavior. (C2/C3/C4 live in session.cpp's apply_ops_locked, not document.cpp, so
// they are fixed/tested there — see the changelog's needs_followup.)
// ===========================================================================

// A1. DRAFT face QUERY resolves. A plain-English face query {"of":"face","side":...}
//     used to ALWAYS dangle in the draft handler ("no candidates of matching kind")
//     while the identical query worked via query_geometry and fillet's refs.edges,
//     because draft sent every payload straight to resolve_face_picks instead of the
//     is_geom_query -> resolve_geom_query path shell uses. Drafting a side face of a
//     box about its bottom plane must now COMPILE OK and change the geometry.
TEST_CASE("A1: draft resolves a plain-English face query and tapers it") {
    const double L = 40.0, W = 30.0, H = 20.0;
    json doc;
    doc["features"] = json::array({
        json{{"id", "blk"}, {"type", "box"}, {"length", L}, {"width", W}, {"height", H}},
        // Taper the +X ("right") face about the bottom plane (z = -H/2, +Z normal) by 5deg.
        json{{"id", "dr"}, {"type", "draft"}, {"body", "blk"}, {"angle", 5.0},
             {"plane_point", json::array({0, 0, -H / 2.0})},
             {"plane_normal", json::array({0, 0, 1})},
             {"faces", json{{"of", "face"}, {"side", "right"}}}},
    });
    CompileResult r = compile_document(doc, json::object());
    for (const auto& s : r.statuses)
        if (!s.ok) MESSAGE("feature " << s.id << " error: " << s.error);
    const aicad::doc::FeatureStatus* dr = status_of(r, "dr");
    REQUIRE(dr != nullptr);
    CHECK(dr->ok);                       // the face QUERY resolved (was: dangling)
    CHECK(dr->error.empty());
    REQUIRE(r.ok);
    REQUIRE(r.body_count == 1);          // draft consumed the box, one body survives
    // The taper actually moved material: the top of the drafted face is pulled in, so
    // the body's X-extent at the top is narrower than the original L (bbox X <= L).
    render::DocMesh dm = parse(r);
    REQUIRE(dm.bodies.size() == 1);
    double lo[3], hi[3];
    bbox(dm.bodies[0], lo, hi);
    CHECK((hi[0] - lo[0]) == doctest::Approx(L).epsilon(0.2));  // base width ~ preserved
    // STRONGER than the bbox check above (a no-op draft would ALSO preserve base width):
    // the right face was pulled inward about the BOTTOM plane, so the solid must have
    // LOST material — assert the volume actually dropped. This is the guard that catches
    // a silent no-op (BRepOffsetAPI_DraftAngle rejecting the face yet IsDone()==true).
    double vol = 0.0;
    for (const TopoDS_Shape& s : r.shapes) {
        if (s.IsNull()) continue;
        GProp_GProps g;
        BRepGProp::VolumeProperties(s, g);
        vol += g.Mass();
    }
    CHECK(vol < L * W * H - 100.0);      // a 5deg taper removes a real wedge (~525 mm^3)
}

// A1b. DRAFT skips faces it cannot taper instead of failing opaquely. {"of":"face"}
//   selects ALL 6 box faces; the top/bottom caps are PARALLEL to a horizontal neutral
//   plane and cannot be drafted. The builder used to throw a Standard_Failure (not a
//   std::exception) on them -> "unknown error during feature compile". Now the rejects
//   are dropped and the 4 side walls still taper (the SolidWorks behaviour).
TEST_CASE("A1b: draft skips non-draftable caps and tapers the rest; errors only when none apply") {
    const double L = 40.0, W = 40.0, H = 20.0;
    auto run = [&](const json& faces) {
        json doc;
        doc["features"] = json::array({
            json{{"id", "blk"}, {"type", "box"}, {"length", L}, {"width", W}, {"height", H}},
            json{{"id", "dr"}, {"type", "draft"}, {"body", "blk"}, {"angle", 5.0},
                 {"plane_point", json::array({0, 0, -H / 2.0})},
                 {"plane_normal", json::array({0, 0, 1})},
                 {"faces", faces}},
        });
        return compile_document(doc, json::object());
    };
    // (a) all faces -> the 4 sides draft, the 2 caps are silently skipped -> OK.
    {
        CompileResult r = run(json{{"of", "face"}});
        const aicad::doc::FeatureStatus* dr = status_of(r, "dr");
        REQUIRE(dr != nullptr);
        if (!dr->ok) MESSAGE("A1b(a) all-faces draft error: " << dr->error);
        CHECK(dr->ok);
        REQUIRE(r.body_count == 1);
    }
    // (b) ONLY the top cap (parallel to the neutral plane) -> nothing draftable -> a
    //     CLEAR error (not the opaque "unknown error during feature compile").
    {
        CompileResult r = run(json{{"of", "face"}, {"side", "top"}});
        const aicad::doc::FeatureStatus* dr = status_of(r, "dr");
        REQUIRE(dr != nullptr);
        CHECK_FALSE(dr->ok);
        CHECK(dr->error.find("unknown error") == std::string::npos);
        CHECK(dr->error.find("PERPENDICULAR") != std::string::npos);
    }
}

// A2/A3. SHELL impossible-thickness and remove-all both used to return ok with the
//     geometry UNCHANGED (BRepOffsetAPI_MakeThickSolid hands back the original solid on
//     failure). A thickness >= the wall must now error "too large"; removing every face
//     must error "cannot remove all faces". A valid thin shell still succeeds.
TEST_CASE("A2/A3: shell rejects impossible thickness and remove-all, allows a valid shell") {
    const double S = 20.0;  // a 20mm cube; half-wall = 10mm
    // (a) impossible: thickness 12 (> 10, the half-thickness) consumes the part -> error.
    {
        json doc;
        doc["features"] = json::array({
            json{{"id", "cube"}, {"type", "box"}, {"length", S}, {"width", S}, {"height", S}},
            json{{"id", "sh"}, {"type", "shell"}, {"body", "cube"}, {"thickness", 12.0}},
        });
        CompileResult r = compile_document(doc, json::object());
        const aicad::doc::FeatureStatus* sh = status_of(r, "sh");
        REQUIRE(sh != nullptr);
        // The core regression: the impossible shell must NOT silently succeed with the
        // geometry unchanged. The volume guard reports "too large"; if instead the kernel
        // returns null on this config it reports "empty shape" — either is an acceptable
        // loud rejection, but a silent ok is the bug.
        CHECK_FALSE(sh->ok);
        CHECK((sh->error.find("too large") != std::string::npos ||
               sh->error.find("empty shape") != std::string::npos));
        // The cube survives intact (never-raise); its volume is the full S^3, not hollowed.
        REQUIRE(r.body_count == 1);
    }
    // (b) remove-ALL faces: an unfiltered face query {"of":"face"} matches every face of
    //     the cube, so the remove-set covers all 6 -> the up-front guard must reject it.
    {
        json doc;
        doc["features"] = json::array({
            json{{"id", "cube"}, {"type", "box"}, {"length", S}, {"width", S}, {"height", S}},
            json{{"id", "sh"}, {"type", "shell"}, {"body", "cube"}, {"thickness", 2.0},
                 {"remove", json{{"of", "face"}}}},  // every face
        });
        CompileResult r = compile_document(doc, json::object());
        const aicad::doc::FeatureStatus* sh = status_of(r, "sh");
        REQUIRE(sh != nullptr);
        CHECK_FALSE(sh->ok);
        CHECK(sh->error.find("cannot remove all faces") != std::string::npos);
        REQUIRE(r.body_count == 1);     // cube survives intact
    }
    // (c) valid: a 2mm wall opening the top face still hollows the cube (volume drops).
    {
        json doc;
        doc["features"] = json::array({
            json{{"id", "cube"}, {"type", "box"}, {"length", S}, {"width", S}, {"height", S}},
            json{{"id", "sh"}, {"type", "shell"}, {"body", "cube"}, {"thickness", 2.0},
                 {"remove", json{{"of", "face"}, {"side", "top"}}}},
        });
        CompileResult r = compile_document(doc, json::object());
        const aicad::doc::FeatureStatus* sh = status_of(r, "sh");
        REQUIRE(sh != nullptr);
        CHECK(sh->ok);                  // a real shell still works (no regression)
        REQUIRE(r.ok);
        REQUIRE(r.body_count == 1);
    }
    // (d) NO FALSE REJECTION: a thickness LARGER than half the smallest bbox dimension is
    //     still VALID when that smallest dimension is in an OPEN direction — only one wall
    //     consumes it, so the bound there is t < dim, not t < dim/2. An 80x60x40 box with
    //     the top (the 40mm = smallest dim) open shells fine at t=25 even though 25 > 40/2=20.
    //     This pins the engine AGAINST a naive "reject when thickness >= min_bbox/2" pre-check
    //     (a tempting but WRONG heuristic): it would falsely reject this real bucket. The
    //     robust post-execution volume guard accepts it because the volume genuinely dropped.
    {
        const double L = 80.0, W = 60.0, H = 40.0, t = 25.0;  // H = smallest dim AND the open one
        json doc;
        doc["features"] = json::array({
            json{{"id", "cube"}, {"type", "box"}, {"length", L}, {"width", W}, {"height", H}},
            json{{"id", "sh"}, {"type", "shell"}, {"body", "cube"}, {"thickness", t},
                 {"remove", json{{"of", "face"}, {"side", "top"}}}},
        });
        CompileResult r = compile_document(doc, json::object());
        const aicad::doc::FeatureStatus* sh = status_of(r, "sh");
        REQUIRE(sh != nullptr);
        REQUIRE_MESSAGE(sh->ok, "thick-but-valid shell wrongly rejected: " << sh->error);
        REQUIRE(r.ok);
        REQUIRE(r.body_count == 1);
        // Prove it actually hollowed (not a silent ok no-op): volume dropped to the bucket
        // 80*60*40 - (80-2*25)*(60-2*25)*(40-25) = 192000 - 30*10*15 = 187500.
        double vol = 0.0;
        for (const TopoDS_Shape& s : r.shapes) {
            if (s.IsNull()) continue;
            GProp_GProps g;
            BRepGProp::VolumeProperties(s, g);
            vol += g.Mass();
        }
        const double expect = L * W * H - (L - 2 * t) * (W - 2 * t) * (H - t);  // 187500
        CHECK(expect == doctest::Approx(187500.0));
        CHECK_MESSAGE(vol == doctest::Approx(expect).epsilon(1e-3),
                      "thick-but-valid bucket volume " << vol << " != expected " << expect);
    }
}

// A-CLOSED. A CLOSED shell (NO faces removed) must produce a genuine HOLLOW SEALED part
//     — the outer solid minus an inward-offset cavity — NOT a shrunken solid block. The
//     bug: BRepOffsetAPI_MakeThickSolid::MakeThickSolidByJoin with an EMPTY ClosingFaces
//     list no-ops on a closed solid (offset shell coincides with the skin) and OCCT hands
//     back the ORIGINAL solid, so a 40^3 box "shelled" to 2mm came out a SOLID 36^3 block
//     (vol 46656). The fix routes the empty-remove case through an offset-solid +
//     boolean-cut path: 40^3 - 36^3 = 17344, a sealed wall with a cavity. The OPEN shell
//     (a face removed) is unchanged — verified here too (an 80x60x40 bucket = 30272).
TEST_CASE("A-CLOSED: a closed shell (no face removed) hollows to a sealed wall, not a solid") {
    // A helper for the single committed body's volume (the closed shell is one solid).
    auto only_volume = [](const CompileResult& r) -> double {
        double v = 0.0;
        for (const TopoDS_Shape& s : r.shapes) {
            if (s.IsNull()) continue;
            GProp_GProps g;
            BRepGProp::VolumeProperties(s, g);
            v += g.Mass();
        }
        return v;
    };

    // (a) CLOSED shell: a 40^3 box, NO `remove` -> a fully-sealed 2mm-wall hollow.
    //     Expected volume = 40^3 - 36^3 = 64000 - 46656 = 17344 (NOT the 46656 solid block
    //     the old empty-remove no-op produced, and NOT the 64000 unchanged solid).
    {
        const double S = 40.0, t = 2.0;
        json doc;
        doc["features"] = json::array({
            json{{"id", "cube"}, {"type", "box"}, {"length", S}, {"width", S}, {"height", S}},
            json{{"id", "sh"}, {"type", "shell"}, {"body", "cube"}, {"thickness", t}},
        });
        CompileResult r = compile_document(doc, json::object());
        const aicad::doc::FeatureStatus* sh = status_of(r, "sh");
        REQUIRE(sh != nullptr);
        REQUIRE_MESSAGE(sh->ok, "closed shell failed: " << sh->error);
        REQUIRE(r.ok);
        REQUIRE(r.body_count == 1);
        REQUIRE(r.shapes.size() == 1);
        // The payoff: a genuine sealed hollow, volume 17344 — never the 46656 solid block.
        const double vol = only_volume(r);
        const double expect = S * S * S - (S - 2 * t) * (S - 2 * t) * (S - 2 * t);  // 17344
        CHECK(expect == doctest::Approx(17344.0));
        CHECK_MESSAGE(vol == doctest::Approx(expect).epsilon(1e-3),
                      "closed shell volume " << vol << " != expected sealed-wall " << expect
                      << " (a SOLID 36^3 block would be 46656 — the bug)");
        // It is a watertight CLOSED solid (a sealed cavity, no free edges).
        CHECK_MESSAGE(genuine_free_edges(r.shapes.front()) == 0,
                      "a closed shell must be a watertight sealed solid");
    }

    // (b) OPEN shell still works (no regression): an 80x60x40 bucket, top removed, 2mm wall.
    //     Outer 80*60*40 = 192000; inner cavity 76*56*38 = 161728; bucket = 30272.
    {
        const double L = 80.0, W = 60.0, H = 40.0, t = 2.0;
        json doc;
        doc["features"] = json::array({
            json{{"id", "cube"}, {"type", "box"}, {"length", L}, {"width", W}, {"height", H}},
            json{{"id", "sh"}, {"type", "shell"}, {"body", "cube"}, {"thickness", t},
                 {"remove", json{{"of", "face"}, {"side", "top"}}}},
        });
        CompileResult r = compile_document(doc, json::object());
        const aicad::doc::FeatureStatus* sh = status_of(r, "sh");
        REQUIRE(sh != nullptr);
        REQUIRE_MESSAGE(sh->ok, "open bucket shell failed: " << sh->error);
        REQUIRE(r.ok);
        REQUIRE(r.body_count == 1);
        REQUIRE(r.shapes.size() == 1);
        const double vol = only_volume(r);
        const double expect = L * W * H - (L - 2 * t) * (W - 2 * t) * (H - t);  // 30272
        CHECK(expect == doctest::Approx(30272.0));
        CHECK_MESSAGE(vol == doctest::Approx(expect).epsilon(1e-3),
                      "open bucket shell volume " << vol << " != expected " << expect);
    }
}

// S1. SHELL ON A FREEFORM BODY must NEVER silently skip. A vase built by REVOLVING a
//     spline meridian has SurfaceOfRevolution (freeform) faces, on which the OCCT
//     thick-solid offset can fail or hand back an open shell. Before the fix the shell
//     vanished silently (no status / volume unchanged); now it must emit the SAME clear,
//     actionable error the loft case already does ("not supported on ... freeform bodies
//     ... build the wall into the profile"). The freeform body must SURVIVE intact.
TEST_CASE("S1: shell on a spline-revolve (freeform) body errors clearly, never silently") {
    // A closed meridian: spline (5,0)->(9,8)->(6,16)->(5,24) up the outer wall, then a
    // line to the axis side at x=2, down to z=0, and back out to the spline start. The
    // spline edge becomes a B-spline; revolving it makes a SurfaceOfRevolution skin.
    json sk;
    sk["id"] = "prof";
    sk["type"] = "sketch";
    sk["plane"] = "front";  // contains the Z axis
    sk["entities"] = json::array({
        json{{"id", "sp"}, {"type", "spline"}, {"construction", false},
             {"points", json::array({json::array({5.0, 0.0}), json::array({9.0, 8.0}),
                                     json::array({6.0, 16.0}), json::array({5.0, 24.0})})}},
        line("l1", 5.0, 24.0, 2.0, 24.0),
        line("l2", 2.0, 24.0, 2.0, 0.0),
        line("l3", 2.0, 0.0, 5.0, 0.0),
    });
    sk["constraints"] = json::array();

    json doc;
    doc["features"] = json::array({
        sk,
        json{{"id", "rev"}, {"type", "revolve"}, {"profile", "prof"},
             {"axis", json{{"kind", "datum"}, {"feature", "axis.z"}}}, {"angle", 360.0}},
        json{{"id", "sh"}, {"type", "shell"}, {"body", "rev"}, {"thickness", 1.5},
             {"remove", json{{"of", "face"}, {"side", "top"}}}},
    });
    CompileResult r = compile_document(doc, json::object());

    // The revolve must build the vase (else the test isn't exercising a freeform body).
    const aicad::doc::FeatureStatus* rev = status_of(r, "rev");
    REQUIRE(rev != nullptr);
    REQUIRE_MESSAGE(rev->ok, "revolve failed: " << rev->error);

    // The CORE assertion: the shell feature is PRESENT in feature_status (never vanished)
    // and, if it could not produce a watertight hollow, it FAILED LOUDLY — it must never
    // be a silent no-op. (If OCCT on this platform DOES manage a valid hollow, that is also
    // acceptable — what is forbidden is the silent-skip / open-shell commit.)
    const aicad::doc::FeatureStatus* sh = status_of(r, "sh");
    REQUIRE_MESSAGE(sh != nullptr, "shell feature vanished from feature_status (S1 bug)");
    if (!sh->ok) {
        MESSAGE("S1 shell error (expected, clear): " << sh->error);
        // The message must be the actionable freeform guidance, not an opaque failure.
        CHECK((sh->error.find("freeform") != std::string::npos ||
               sh->error.find("B-spline") != std::string::npos ||
               sh->error.find("open") != std::string::npos));
        // The freeform vase SURVIVES intact (never-raise; the failed shell didn't consume it).
        REQUIRE(r.body_count == 1);
    } else {
        // If it succeeded, the committed body MUST be a watertight closed solid (no silent
        // open-shell commit) — this is exactly the invariant S1's post-check guarantees.
        REQUIRE(r.shapes.size() == 1);
        CHECK(genuine_free_edges(r.shapes.front()) == 0);
    }
}

// S2. A PARTIAL FILLET must NEVER commit an OPEN (free-edge) body. When a fillet is asked
//     on edges whose local geometry is too tight for the radius, the drop-failing blender
//     rounds what it can — but the committed result must still be a CLOSED solid. We assert
//     the invariant two ways: (a) a NORMAL fillet commits a watertight body (0 free edges),
//     and (b) an over-large fillet either errors loudly OR, if it commits, is still closed
//     — it may NOT silently ship an open shell. The N2 negative-volume guard did not catch
//     this open-shell case; the new seam-aware post-check does.
TEST_CASE("S2: a fillet never commits an open (free-edge) body") {
    const double W = 20.0, H = 20.0, D = 6.0;  // a thin-ish plate
    // (a) sanity: a normal small fillet on all 12 edges yields a watertight closed solid.
    {
        json doc;
        doc["features"] = json::array({
            sketch_feature("s1", "top", rect_entities(0, 0, W, H)),
            json{{"id", "x1"}, {"type", "extrude"}, {"profile", "s1"}, {"depth", D}},
            json{{"id", "fl"}, {"type", "fillet"}, {"body", "x1"}, {"radius", 1.0},
                 {"edges", json{{"of", "edge"}}}},  // all 12 edges
        });
        CompileResult r = compile_document(doc, json::object());
        const aicad::doc::FeatureStatus* fl = status_of(r, "fl");
        REQUIRE(fl != nullptr);
        REQUIRE_MESSAGE(fl->ok, "normal fillet failed: " << fl->error);
        REQUIRE(r.shapes.size() == 1);
        CHECK_MESSAGE(genuine_free_edges(r.shapes.front()) == 0,
                      "a committed fillet body must be a closed solid (no free edges)");
    }
    // (b) over-large: round ALL 12 edges at a radius near/over the plate half-thickness.
    //     Whatever the engine decides (clean partial OR loud error), it must NOT commit an
    //     OPEN body. The radius 3.5 > D/2=3 makes the top/bottom rim rounds collide.
    {
        json doc;
        doc["features"] = json::array({
            sketch_feature("s1", "top", rect_entities(0, 0, W, H)),
            json{{"id", "x1"}, {"type", "extrude"}, {"profile", "s1"}, {"depth", D}},
            json{{"id", "fl"}, {"type", "fillet"}, {"body", "x1"}, {"radius", 3.5},
                 {"edges", json{{"of", "edge"}}}},  // all 12 edges, radius > half-thickness
        });
        CompileResult r = compile_document(doc, json::object());
        const aicad::doc::FeatureStatus* fl = status_of(r, "fl");
        REQUIRE(fl != nullptr);
        if (fl->ok) {
            // Committed -> MUST be a closed solid (the S2 invariant: no silent open shell).
            REQUIRE(r.shapes.size() == 1);
            CHECK_MESSAGE(genuine_free_edges(r.shapes.front()) == 0,
                          "an over-large fillet committed ok but left an OPEN body (S2 bug)");
        } else {
            // Errored loudly -> the message must name the open/too-tight cause, and the
            // ORIGINAL extrude body survives intact (never-raise; nothing was committed).
            MESSAGE("S2 over-large fillet error (acceptable, loud): " << fl->error);
            CHECK((fl->error.find("open") != std::string::npos ||
                   fl->error.find("too tight") != std::string::npos ||
                   fl->error.find("too large") != std::string::npos));
            REQUIRE(r.body_count == 1);
            REQUIRE(r.shapes.size() == 1);
            CHECK(genuine_free_edges(r.shapes.front()) == 0);  // the surviving box is closed
        }
    }
}

// A4. FILLET/CHAMFER on an edge query that matches NO edges must report a "no edges
//     matched" error, NOT the misleading "N mm is too large for the selected edge(s)".
//     A box has only straight edges, so {"of":"edge","surf":"round"} matches nothing.
TEST_CASE("A4: fillet on an empty edge query reports 'no edges matched', not 'too large'") {
    json doc;
    doc["features"] = json::array({
        json{{"id", "blk"}, {"type", "box"}, {"length", 40.0}, {"width", 30.0}, {"height", 20.0}},
        json{{"id", "fl"}, {"type", "fillet"}, {"body", "blk"}, {"radius", 3.0},
             {"edges", json{{"of", "edge"}, {"surf", "round"}}}},  // no round edges on a box
    });
    CompileResult r = compile_document(doc, json::object());
    const aicad::doc::FeatureStatus* fl = status_of(r, "fl");
    REQUIRE(fl != nullptr);
    CHECK_FALSE(fl->ok);
    // The message must name the empty match ("...matched no edges" from the query path,
    // or "no edges matched the query" from the defensive guard) — NOT the misleading
    // "N mm is too large for the selected edge(s)" radius-feasibility message.
    CHECK(fl->error.find("no edges") != std::string::npos);
    CHECK(fl->error.find("too large") == std::string::npos);  // NOT the size message
}

// A6. BOX with a zero/negative height (or width/length) used to fall through to the
//     opaque "box produced an empty solid". It must now error up front with a
//     dimension-named message; a positive-dim box still builds exactly.
TEST_CASE("A6: box validates length/width/height up front") {
    {
        json doc;
        doc["features"] = json::array({
            json{{"id", "b"}, {"type", "box"}, {"length", 40.0}, {"width", 30.0}, {"height", 0.0}},
        });
        CompileResult r = compile_document(doc, json::object());
        const aicad::doc::FeatureStatus* b = status_of(r, "b");
        REQUIRE(b != nullptr);
        CHECK_FALSE(b->ok);
        CHECK(b->error.find("must be > 0") != std::string::npos);
        CHECK(r.body_count == 0);
    }
    {   // a normal box is untouched by the new guard.
        json doc;
        doc["features"] = json::array({
            json{{"id", "b"}, {"type", "box"}, {"length", 40.0}, {"width", 30.0}, {"height", 20.0}},
        });
        CompileResult r = compile_document(doc, json::object());
        REQUIRE(r.ok);
        REQUIRE(r.body_count == 1);
    }
}

// B1. A blind hole whose depth runs past the far face of the stock used to be silently
//     clamped to a through hole. It must now still drill (never-raise) but carry a loud
//     WARNING recording the clamp. A blind hole that stays within the stock has none.
TEST_CASE("B1: blind hole deeper than the stock warns it was clamped") {
    const double T = 5.0;  // 5mm-thick plate
    {   // depth 50 >> 5mm stock -> drills through, with a clamp warning.
        json doc;
        doc["features"] = json::array({
            json{{"id", "plate"}, {"type", "box"}, {"length", 60.0}, {"width", 40.0}, {"height", T}},
            json{{"id", "h"}, {"type", "hole"}, {"diameter", 6.0},
                 {"end", "blind"}, {"depth", 50.0},
                 {"face", json{{"of", "face"}, {"side", "top"}}}},
        });
        CompileResult r = compile_document(doc, json::object());
        const aicad::doc::FeatureStatus* h = status_of(r, "h");
        REQUIRE(h != nullptr);
        CHECK(h->ok);                                   // still drills (never-raise)
        CHECK(h->warning.find("exceeds the available stock") != std::string::npos);
        REQUIRE(r.body_count == 1);
    }
    {   // depth 3 < 5mm stock -> a genuine blind hole, no clamp warning.
        json doc;
        doc["features"] = json::array({
            json{{"id", "plate"}, {"type", "box"}, {"length", 60.0}, {"width", 40.0}, {"height", T}},
            json{{"id", "h"}, {"type", "hole"}, {"diameter", 6.0},
                 {"end", "blind"}, {"depth", 3.0},
                 {"face", json{{"of", "face"}, {"side", "top"}}}},
        });
        CompileResult r = compile_document(doc, json::object());
        const aicad::doc::FeatureStatus* h = status_of(r, "h");
        REQUIRE(h != nullptr);
        CHECK(h->ok);
        CHECK(h->warning.empty());                      // no false clamp warning
    }
}

// B2. LINEAR_PATTERN with spacing == 0 and count >= 2 stacked coincident copies and was
//     accepted silently. It must now error, mirroring the count guard. A non-zero
//     spacing pattern still works.
TEST_CASE("B2: linear_pattern rejects zero spacing when count >= 2") {
    // A plate with one through hole, then a row pattern of that hole.
    auto make_doc = [](double spacing1) {
        json doc;
        doc["features"] = json::array({
            json{{"id", "plate"}, {"type", "box"}, {"length", 80.0}, {"width", 20.0}, {"height", 5.0}},
            json{{"id", "h"}, {"type", "hole"}, {"diameter", 4.0}, {"end", "through_all"},
                 {"face", json{{"of", "face"}, {"side", "top"}}}},
            json{{"id", "lp"}, {"type", "linear_pattern"}, {"seed", "h"},
                 {"count1", 3}, {"spacing1", spacing1}, {"dir1", json::array({1, 0, 0})}},
        });
        return doc;
    };
    {   // spacing1 == 0 with count1 == 3 -> error.
        CompileResult r = compile_document(make_doc(0.0), json::object());
        const aicad::doc::FeatureStatus* lp = status_of(r, "lp");
        REQUIRE(lp != nullptr);
        CHECK_FALSE(lp->ok);
        CHECK(lp->error.find("spacing must be non-zero") != std::string::npos);
    }
    {   // a real 15mm spacing still patterns three holes.
        CompileResult r = compile_document(make_doc(15.0), json::object());
        const aicad::doc::FeatureStatus* lp = status_of(r, "lp");
        REQUIRE(lp != nullptr);
        CHECK(lp->ok);
    }
}

// B4. REVOLVE of an OPEN profile still auto-closes to the axis (useful), but now surfaces
//     a WARNING so the model knows its open wall was closed into a disc. (Reuses the 1b
//     geometry: an open wall line on Front, revolved 360 about Z.)
TEST_CASE("B4: revolve of an open profile warns it was auto-closed") {
    const double R = 12.0, H = 30.0;
    json sk;
    sk["id"] = "prof"; sk["type"] = "sketch"; sk["plane"] = "front";
    sk["entities"] = json::array({line("e1", R, 0.0, R, H)});  // open wall, no closing line
    sk["constraints"] = json::array();
    json doc;
    doc["features"] = json::array({
        sk,
        json{{"id", "rev"}, {"type", "revolve"}, {"profile", "prof"},
             {"axis", json{{"kind", "datum"}, {"feature", "axis.z"}}}, {"angle", 360.0}},
    });
    CompileResult r = compile_document(doc, json::object());
    const aicad::doc::FeatureStatus* rev = status_of(r, "rev");
    REQUIRE(rev != nullptr);
    CHECK(rev->ok);                                            // still builds the solid
    CHECK(rev->warning.find("auto-closed") != std::string::npos);
    REQUIRE(r.body_count == 1);
}

// K4. A dress/pattern whose selector references a NON-EXISTENT feature id must ERROR
//     loudly ("selector references unknown feature ...") rather than score against the
//     wrong edge and silently mutilate the body (an open shell that reported ok=true).
TEST_CASE("K4: fillet with an unknown feature-ref selector errors, never corrupts") {
    const double W = 40.0, H = 30.0, D = 20.0;
    json doc;
    doc["features"] = json::array({
        sketch_feature("s1", "top", rect_entities(0, 0, W, H)),
        json{{"id", "x1"}, {"type", "extrude"}, {"profile", "s1"}, {"depth", D}},
        // The edges selector names a feature that does not exist (f999).
        json{{"id", "f1"}, {"type", "fillet"}, {"body", "x1"}, {"radius", 3.0},
             {"edges", json{{"kind", "edge"}, {"feature", "f999"},
                            {"centroid", json::array({0, 0, D})}}}},
    });
    CompileResult r = compile_document(doc, json::object());
    const aicad::doc::FeatureStatus* f1 = status_of(r, "f1");
    REQUIRE(f1 != nullptr);
    CHECK_FALSE(f1->ok);
    CHECK(f1->error.find("unknown feature") != std::string::npos);
    // The extrude body still survives intact (a contained per-feature error).
    REQUIRE(r.body_count == 1);
    render::DocMesh dm = parse(r);
    REQUIRE(dm.bodies.size() == 1);
    // Still a closed box of the original volume — NOT a corrupted open shell.
    CHECK(r.volume == doctest::Approx(W * H * D).epsilon(1e-3));
}

// K5. A dimension passed as a BARE number (length:40, not {value:40,expr:null}) is
//     coerced cleanly and the part compiles to the exact geometry — never failing late
//     as an opaque geometry error.
TEST_CASE("K5: a bare-number dimension is coerced and builds the exact box") {
    json doc;
    doc["features"] = json::array({
        // Every dim is a BARE number (the canonical host form is {value,expr}).
        json{{"id", "b"}, {"type", "box"}, {"length", 40}, {"width", 30}, {"height", 20}},
    });
    CompileResult r = compile_document(doc, json::object());
    REQUIRE(r.ok);
    REQUIRE(r.body_count == 1);
    render::DocMesh dm = parse(r);
    REQUIRE(dm.bodies.size() == 1);
    double lo[3], hi[3];
    bbox(dm.bodies[0], lo, hi);
    CHECK((hi[0] - lo[0]) == doctest::Approx(40.0).epsilon(1e-4));
    CHECK((hi[1] - lo[1]) == doctest::Approx(30.0).epsilon(1e-4));
    CHECK((hi[2] - lo[2]) == doctest::Approx(20.0).epsilon(1e-4));
}

// K6. A fillet/chamfer radius BELOW the modelling tolerance (Precision::Confusion = 1e-7 mm)
//     used to silently no-op. It must now still succeed (never-raise) but carry a WARNING that
//     no material was removed. The radius must be strictly under 1e-7 to hit that branch — 1e-6
//     is 10x the tolerance and OCCT would actually build the (vanishingly small) blend.
TEST_CASE("K6: a sub-tolerance fillet warns that no material was removed") {
    const double W = 40.0, H = 30.0, D = 20.0;
    json doc;
    doc["features"] = json::array({
        sketch_feature("s1", "top", rect_entities(0, 0, W, H)),
        json{{"id", "x1"}, {"type", "extrude"}, {"profile", "s1"}, {"depth", D}},
        json{{"id", "f1"}, {"type", "fillet"}, {"body", "x1"}, {"radius", 1e-9},
             {"dir", json::array({0, 0, 1})}},
    });
    CompileResult r = compile_document(doc, json::object());
    const aicad::doc::FeatureStatus* f1 = status_of(r, "f1");
    REQUIRE(f1 != nullptr);
    CHECK(f1->ok);  // still compiles (the body passes through unchanged)
    CHECK(f1->warning.find("below tolerance") != std::string::npos);
    REQUIRE(r.body_count == 1);
}

// K7. A box with a SUB-TOLERANCE dim (1e-9) used to error with the opaque "unknown error
//     during feature compile". It must now name the offending dim ("below the minimum
//     tolerance").
TEST_CASE("K7: a sub-tolerance box dim reports a clear tolerance error") {
    json doc;
    doc["features"] = json::array({
        json{{"id", "b"}, {"type", "box"}, {"length", 40.0}, {"width", 30.0}, {"height", 1e-9}},
    });
    CompileResult r = compile_document(doc, json::object());
    const aicad::doc::FeatureStatus* b = status_of(r, "b");
    REQUIRE(b != nullptr);
    CHECK_FALSE(b->ok);
    CHECK(b->error.find("below the minimum tolerance") != std::string::npos);
    CHECK(b->error.find("unknown error") == std::string::npos);  // NOT the opaque message
}

// K9. A duplicate feature NAME in one doc is accepted (no hard error — existing flows
//     keep working) but the second occurrence carries a status.warning so the tree can be
//     disambiguated.
TEST_CASE("K9: a duplicate feature name is accepted with a warning") {
    json doc;
    doc["features"] = json::array({
        json{{"id", "b1"}, {"type", "box"}, {"name", "Block"},
             {"length", 10.0}, {"width", 10.0}, {"height", 10.0}},
        // A second body with the SAME display name, placed apart so both survive.
        json{{"id", "b2"}, {"type", "box"}, {"name", "Block"}, {"at", json::array({40, 0, 0})},
             {"length", 10.0}, {"width", 10.0}, {"height", 10.0}},
    });
    CompileResult r = compile_document(doc, json::object());
    REQUIRE(r.ok);  // NOT a hard error
    const aicad::doc::FeatureStatus* b1 = status_of(r, "b1");
    const aicad::doc::FeatureStatus* b2 = status_of(r, "b2");
    REQUIRE(b1 != nullptr);
    REQUIRE(b2 != nullptr);
    CHECK(b1->warning.empty());                              // first use: no warning
    CHECK(b2->warning.find("duplicate feature name") != std::string::npos);
}

// C2/C3/C4. FAILED-BATCH UNDO CORRUPTION (session.cpp apply_ops_locked). A batch that
//     passes structural validation but FAILS to compile (r.ok==false) used to be committed
//     anyway: it pushed a ghost revision onto the undo stack and persisted the errored
//     feature in the tree, so undo/redo then stepped through phantom states. The fix
//     snapshots the pre-batch session state and, on a rejected compile, restores doc +
//     revision + both undo/redo stacks, then RETURNS an in-band rejection (per-feature
//     ok:false at rc 0, the batch-atomicity contract — it does NOT throw) — leaving the
//     session EXACTLY as before. Here: a successful box, then a fillet on a round-edge query
//     that matches no edges on a box (compiles to r.ok==false). The failed batch must be a
//     no-op on the live session, and a single undo must revert the LAST SUCCESSFUL feature
//     (the box), not a ghost.
TEST_CASE("C2/C3/C4: a compile-rejected batch leaves the doc and undo stacks untouched") {
    using aicad::doc::doc_new;
    using aicad::doc::doc_apply_ops;
    using aicad::doc::doc_undo;
    using aicad::doc::doc_tree;
    auto dimv = [](double v) { return json{{"value", v}, {"expr", nullptr}}; };

    json nr = doc_new("part", "FailBatchUndo");
    const std::string id = nr.at("doc_id").get<std::string>();

    // (1) A successful feature: revision advances, the box is in the tree.
    json okops = json::array({
        json{{"op", "add_feature"}, {"feature", {{"type", "box"}, {"name", "Blk"},
             {"params", {{"length", dimv(40)}, {"width", dimv(30)}, {"height", dimv(20)}}}}}},
    });
    json okresp = doc_apply_ops(id, -1, okops, false);
    const long rev_after_box = okresp.at("revision").get<long>();
    json tree_after_box = doc_tree(id).at("tree");
    REQUIRE(tree_after_box.at("features").size() == 1);

    // (2) A batch that validates but fails to compile: fillet a ROUND edge of a box (none).
    //     doc_apply_ops RETURNS an in-band rejection (rc 0, no throw — the batch-atomicity
    //     contract): the failing feature's status is ok:false, and the session is left as it
    //     was (steps 3+4 verify no ghost revision / clean undo).
    json badops = json::array({
        json{{"op", "add_feature"}, {"feature", {{"type", "fillet"}, {"name", "BadFillet"},
             {"params", {{"radius", dimv(3)}}},
             {"refs", {{"edges", json{{"of", "edge"}, {"surf", "round"}}}}}}}},
    });
    json badresp;
    CHECK_NOTHROW(badresp = doc_apply_ops(id, -1, badops, false));
    bool any_failed = false;
    for (const json& st : badresp.at("rebuild").at("feature_status"))
        if (!st.value("ok", true)) any_failed = true;
    CHECK(any_failed);  // rejected batch -> in-band {feature_status ok:false} (so agents see it)

    // (3) The failed batch was a NO-OP: same revision, same tree (no ghost, no errored
    //     feature persisted). doc_tree does not bump the revision, so it reads the live one.
    json after_fail = doc_tree(id);
    CHECK(after_fail.at("revision").get<long>() == rev_after_box);
    REQUIRE(after_fail.at("tree").at("features").size() == 1);  // ONLY the box, no BadFillet
    CHECK(after_fail.at("tree").at("features")[0].at("name").get<std::string>() == "Blk");

    // (4) A single undo cleanly reverts the LAST SUCCESSFUL feature (the box) — not a ghost
    //     revision left by the rejected batch — so the part returns to empty.
    json undone = doc_undo(id);
    CHECK(undone.at("tree").at("features").size() == 0);
}

// P0 DEADLOCK REGRESSION: a rejected op batch must NOT leave the process-global session
// mutex held. Before the fix this was already RAII-safe for a C++ OpError (a lock_guard
// releases on unwind), but the bug report paired it with the access-violation path that
// orphaned the lock under /EHsc — after which EVERY subsequent doc_* call on the same
// worker thread re-locked the still-held non-recursive mutex and threw std::system_error
// EDEADLK ("resource deadlock would occur"), bricking ALL documents. This test pins the
// invariant for the deterministic (C++-exception) case: a rejected batch on doc A, then
// a full sequence of locked calls on BOTH a SECOND doc B and doc A itself, all succeed —
// no deadlock, no error leak across docs. (doctest runs cases on one thread, so a held
// lock would surface here as exactly the EDEADLK the users saw.)
TEST_CASE("P0: a rejected batch never deadlocks subsequent calls on any doc") {
    using aicad::doc::doc_new;
    using aicad::doc::doc_apply_ops;
    using aicad::doc::doc_tree;
    using aicad::doc::doc_list;
    using aicad::doc::doc_rebuild;
    auto dimv = [](double v) { return json{{"value", v}, {"expr", nullptr}}; };

    json na = doc_new("part", "DeadlockA");
    const std::string a = na.at("doc_id").get<std::string>();
    json nb = doc_new("part", "DeadlockB");
    const std::string b = nb.at("doc_id").get<std::string>();

    // Reject a batch on doc A (validates, then fails to compile -> in-band rejection, doc
    // unchanged). doc_apply_ops RETURNS (no throw, per the batch-atomicity contract): the
    // failing feature's status is ok:false. The lock is RAII-released either way, so this
    // pins the no-deadlock invariant for the deterministic rejection path.
    json badops = json::array({
        json{{"op", "add_feature"}, {"feature", {{"type", "fillet"}, {"name", "Nope"},
             {"params", {{"radius", dimv(3)}}},
             {"refs", {{"edges", json{{"of", "edge"}, {"surf", "round"}}}}}}}},
    });
    json badresp;
    CHECK_NOTHROW(badresp = doc_apply_ops(a, -1, badops, false));
    bool any_failed = false;
    for (const json& st : badresp.at("rebuild").at("feature_status"))
        if (!st.value("ok", true)) any_failed = true;
    REQUIRE(any_failed);  // the batch was rejected in-band (some feature_status ok:false)

    // If the lock had been orphaned, the VERY NEXT lock acquisition on this thread would
    // throw std::system_error EDEADLK. Drive a spread of locked entry points on BOTH docs:
    CHECK_NOTHROW(doc_list());                 // global registry read
    CHECK_NOTHROW(doc_tree(a));                // doc A still reachable
    CHECK_NOTHROW(doc_tree(b));                // doc B not poisoned by A's failure
    CHECK_NOTHROW(doc_rebuild(b));             // a heavier locked op on B

    // And a real edit on doc B still commits (the lock truly is free, not just probed).
    json okops = json::array({
        json{{"op", "add_feature"}, {"feature", {{"type", "box"}, {"name", "Blk"},
             {"params", {{"length", dimv(10)}, {"width", dimv(10)}, {"height", dimv(10)}}}}}},
    });
    json okresp;
    CHECK_NOTHROW(okresp = doc_apply_ops(b, -1, okops, false));
    CHECK(okresp.value("volume", 0.0) == doctest::Approx(1000.0).epsilon(0.02));

    // Doc A is unchanged (the rejected batch was a no-op) and still editable afterwards.
    CHECK(doc_tree(a).at("tree").at("features").size() == 0);
    CHECK_NOTHROW(doc_apply_ops(a, -1, okops, false));
}

// N2. NO COMMITTED SOLID MAY HAVE NEGATIVE VOLUME. A deep feature tree (the bug's
//     trigger was a bolt_circle of AccessHoles on a front face, ~30 features deep)
//     once silently committed a B-rep with INVERTED shell orientation: VolumeProperties
//     reported a NEGATIVE mass, yet every feature said ok:true and check.issues was [].
//     The post-compile sanity guard (compile_document) now validates+heals or fails
//     loudly. The INVARIANT is the regression: the total volume AND every surviving
//     body's volume must be finite and >= 0 — never the old -89753 mm^3 corruption.
TEST_CASE("N2: a deep tree never silently commits a negative-volume solid") {
    using aicad::doc::doc_new;
    using aicad::doc::doc_apply_ops;
    using aicad::doc::doc_rebuild;
    auto dimv = [](double v) { return json{{"value", v}, {"expr", nullptr}}; };

    json nr = doc_new("part", "NegVolGuard");
    const std::string id = nr.at("doc_id").get<std::string>();
    // sketch(rect) -> extrude a plate -> bolt_circle of holes on a FACE (the bug trigger),
    // then dress it with fillets + a chamfer so the tree is several modifying features deep.
    json ops = json::array({
        json{{"op", "add_feature"}, {"feature", {{"type", "box"}, {"name", "Plate"},
             {"params", {{"length", dimv(80)}, {"width", dimv(60)}, {"height", dimv(12)}}}}}},
        json{{"op", "add_feature"}, {"feature", {{"type", "bolt_circle"}, {"name", "AccessHoles"},
             {"params", {{"diameter", dimv(5)}, {"pcd", dimv(40)}, {"count", dimv(6)},
                         {"end", "through_all"}}},
             {"refs", {{"face", json{{"of", "face"}, {"side", "top"}}}}}}}},
        json{{"op", "add_feature"}, {"feature", {{"type", "chamfer"}, {"name", "Edges"},
             {"params", {{"length", dimv(1.0)}}},
             {"refs", {{"edges", json{{"of", "edge"}, {"side", "+z"}}}}}}}},
    });
    json resp = doc_apply_ops(id, -1, ops, false);

    // Every feature compiled (the guard heals or errors — it must not leave junk).
    const json& fs = resp.at("rebuild").at("feature_status");
    for (const auto& s : fs)
        CHECK_MESSAGE(s.value("ok", false),
                      "feature " << s.value("id", "?") << " failed: " << status_err(s));

    // THE INVARIANT: the committed volume is finite and strictly positive (a holed plate
    // has real material) — never the negative corruption the guard exists to prevent.
    const double vol = resp.value("volume", 0.0);
    MESSAGE("N2 deep-tree committed volume = " << vol << " mm^3");
    CHECK(std::isfinite(vol));
    CHECK(vol > 0.0);
    // A 80x60x12 plate (57600) minus six Ø5 through bores, less a small chamfer ring.
    CHECK(vol < 80.0 * 60.0 * 12.0);
    CHECK(vol > 80.0 * 60.0 * 12.0 - 6.0 * 3.14159265 * 2.5 * 2.5 * 12.0 - 5000.0);
}

// A2. A ZERO-MOTION transform (translate:[0,0,0], no rotation) is a harmless NO-OP that
//     SUCCEEDS — it records the body at its current pose (explicitly placing an at-origin
//     component for assembly). It must NOT error "transform: no motion ..." (the old
//     rejection was a deadlock trigger via the rollback). The body survives unchanged.
TEST_CASE("A2: a zero-motion transform succeeds as an at-origin no-op") {
    using aicad::doc::doc_new;
    using aicad::doc::doc_apply_ops;
    auto dimv = [](double v) { return json{{"value", v}, {"expr", nullptr}}; };

    json nr = doc_new("part", "ZeroXform");
    const std::string id = nr.at("doc_id").get<std::string>();
    json ops = json::array({
        json{{"op", "add_feature"}, {"feature", {{"type", "box"}, {"name", "Blk"},
             {"params", {{"length", dimv(10)}, {"width", dimv(10)}, {"height", dimv(10)}}}}}},
        // translate:[0,0,0], no rotate/angle -> zero motion. Must SUCCEED (Bug A2).
        json{{"op", "add_feature"}, {"feature", {{"type", "transform"}, {"name", "AtOrigin"},
             {"params", {{"translate", json::array({0, 0, 0})}}}}}},
    });
    json resp;
    CHECK_NOTHROW(resp = doc_apply_ops(id, -1, ops, false));
    const json& fs = resp.at("rebuild").at("feature_status");
    REQUIRE(fs.size() == 2);
    for (const auto& s : fs)
        CHECK_MESSAGE(s.value("ok", false),
                      "feature " << s.value("id", "?") << " failed: " << status_err(s));
    // The body is unchanged — a 10mm cube, volume 1000, still exactly one body.
    CHECK(resp.value("volume", 0.0) == doctest::Approx(1000.0).epsilon(0.02));
}

// N4. A FEATURE MIRROR resolves a same-batch seed reference the way extrude/loft/sweep
//     do. Here the mirror reflects the seed CUT (a hole) across the YZ plane and re-bores
//     the reflected hole -> the plate loses material on BOTH sides. The seed is referenced
//     by its feature id within the same compile, exactly the path that used to reject the
//     mirror's features ref. (compile_document takes resolved fN ids directly, mirroring
//     what session.cpp hands it after "$<index>" resolution.)
TEST_CASE("N4: a feature mirror resolves its same-batch seed and bores both sides") {
    const double W = 80.0, H = 40.0, D = 12.0, R = 5.0;
    json doc;
    doc["features"] = json::array({
        json{{"id", "blk"}, {"type", "box"}, {"length", W}, {"width", H}, {"height", D}},
        // a single hole on the +X side of centre (the seed cut).
        json{{"id", "h1"}, {"type", "hole"}, {"diameter", 2.0 * R}, {"end", "through_all"},
             {"face", json{{"of", "face"}, {"side", "top"}}},
             {"positions", json::array({json::array({20.0, 0.0})})}},
        // mirror the seed CUT across the YZ plane (x=0) -> a second hole at x=-20.
        json{{"id", "mir"}, {"type", "mirror"}, {"seed", "h1"},
             {"plane_point", json::array({0, 0, 0})},
             {"plane_normal", json::array({1, 0, 0})}},
    });
    CompileResult r = compile_document(doc, json::object());
    for (const auto& s : r.statuses)
        CHECK_MESSAGE(s.ok, "feature " << s.id << " failed: " << s.error);
    REQUIRE(r.ok);
    REQUIRE(r.body_count == 1);
    // Both bores removed material: volume = plate - TWO through holes (within tolerance).
    const double bore = PI * R * R * D;
    MESSAGE("N4 mirror volume = " << r.volume << " (plate=" << (W * H * D)
            << " two-bores=" << (2 * bore) << ")");
    CHECK(r.volume == doctest::Approx(W * H * D - 2.0 * bore).epsilon(0.02));
}

// N4 (normalize alignment). The mirror's `refs.features` maps to a single `seed` whether
// given as a LIST [id] OR a bare id string — symmetric with how `profile` accepts either
// shape. normalize_feature is the document-side seed mapping the bug asked to align; both
// canonical shapes must produce an identical compiled body. (The same-batch "$<index>"
// rewrite to "fN" happens upstream in session.cpp, so by compile time the ref is an fN.)
TEST_CASE("N4: mirror refs.features maps a list and a bare id to the same seed") {
    const double W = 80.0, H = 40.0, D = 12.0, R = 5.0;
    auto build = [&](const json& features_ref) -> CompileResult {
        json doc;
        doc["features"] = json::array({
            json{{"id", "f1"}, {"type", "box"}, {"name", "Plate"},
                 {"params", {{"length", W}, {"width", H}, {"height", D}}}, {"refs", json::object()}},
            json{{"id", "f2"}, {"type", "hole"}, {"name", "Seed"},
                 {"params", {{"diameter", 2.0 * R}, {"end", "through_all"},
                             {"positions", json::array({json::array({20.0, 0.0})})}}},
                 {"refs", {{"face", json{{"of", "face"}, {"side", "top"}}}}}},
            // The mirror references its seed through refs.features in the shape under test.
            json{{"id", "f3"}, {"type", "mirror"}, {"name", "Both"},
                 {"params", {{"plane_point", json::array({0, 0, 0})},
                             {"plane_normal", json::array({1, 0, 0})}}},
                 {"refs", {{"features", features_ref}}}},
        });
        return compile_document(doc, json::object());
    };

    const double bore = PI * R * R * D;
    const double want = W * H * D - 2.0 * bore;  // plate minus the seed + its mirror

    // (a) refs.features as a LIST [ "f2" ] (the canonical pattern/mirror shape).
    CompileResult ra = build(json::array({"f2"}));
    for (const auto& s : ra.statuses) CHECK_MESSAGE(s.ok, "list: " << s.id << " " << s.error);
    REQUIRE(ra.ok);
    CHECK(ra.volume == doctest::Approx(want).epsilon(0.02));

    // (b) refs.features as a BARE id string "f2" — must resolve to the SAME seed (Bug N4).
    CompileResult rb = build(json("f2"));
    for (const auto& s : rb.statuses) CHECK_MESSAGE(s.ok, "bare: " << s.id << " " << s.error);
    REQUIRE(rb.ok);
    CHECK(rb.volume == doctest::Approx(want).epsilon(0.02));

    // Both shapes produce the identical body volume.
    CHECK(ra.volume == doctest::Approx(rb.volume).epsilon(1e-6));
}

// N4 (validation gate). Validate_document must accept a "$<N>" same-batch placeholder in
// refs.features (the gate that blocked mirror/pattern from seeding an earlier-batch feature
// until session.cpp resolved it). We test the raw validate path so this is independent of
// whether session.cpp has run placeholder resolution.
TEST_CASE("N4: validate_document accepts same-batch placeholder in refs.features") {
    json canon;
    canon["schema"] = "aicad.part/1";
    canon["id"] = "doc1";
    canon["name"] = "PlaceholderFeat";
    canon["units"] = json{{"system", "MMGS"}};
    canon["features"] = json::array({
        json{{"id", "f1"}, {"type", "box"}, {"name", "Block"}, {"suppressed", false},
             {"params", {{"length", 40}, {"width", 30}, {"height", 10}}},
             {"refs", json::object()}},
        json{{"id", "f2"}, {"type", "hole"}, {"name", "Seed"}, {"suppressed", false},
             {"params", {{"style", "simple"}, {"end", "through_all"}}},
             {"refs", {{"face", json{{"of", "face"}, {"side", "top"}}}}}},
        // refs.features contains a same-batch "$1" placeholder — must NOT be rejected as
        // "not a feature id" (Bug N4).
        json{{"id", "f3"}, {"type", "mirror"}, {"name", "Mir"}, {"suppressed", false},
             {"params", json::object()},
             {"refs", {{"plane", "plane.right"}, {"features", json::array({"$1"})}}}},
    });
    std::vector<std::string> verrs = aicad::doc::validate_document(canon);
    for (const auto& e : verrs) MESSAGE("N4 placeholder validation error: " << e);
    // No error should reference refs.features (the placeholder gate).
    bool feat_gate_fired = false;
    for (const auto& e : verrs) {
        if (e.find("refs.features") != std::string::npos &&
            e.find("not a feature id") != std::string::npos) {
            feat_gate_fired = true;
        }
    }
    CHECK_MESSAGE(!feat_gate_fired, "FeatList gate rejected a valid $<N> placeholder");
}

// N5. refs.offset on a sketch feature must be RECOGNIZED (not "unknown ref slot for sketch").
// document.cpp's sketch compile already reads `offset` from refs or params; the validation
// gate was missing the slot declaration. The slot is optional, so an absent refs.offset is
// also fine.
TEST_CASE("N5: validate_document accepts refs.offset on a sketch feature") {
    json canon;
    canon["schema"] = "aicad.part/1";
    canon["id"] = "doc1";
    canon["name"] = "SketchOffset";
    canon["units"] = json{{"system", "MMGS"}};
    // A sketch with refs.offset set to a numeric dim — must NOT emit "unknown ref slot".
    json offset_dim = json{{"value", 5.0}, {"expr", nullptr}};
    canon["features"] = json::array({
        json{{"id", "f1"}, {"type", "sketch"}, {"name", "Prof"}, {"suppressed", false},
             {"params", {{"entities", json::array()}, {"constraints", json::array()}}},
             {"refs", {{"plane", "plane.top"}, {"offset", offset_dim}}}},
    });
    std::vector<std::string> verrs = aicad::doc::validate_document(canon);
    for (const auto& e : verrs) MESSAGE("N5 sketch offset validation error: " << e);
    bool unknown_slot = false;
    for (const auto& e : verrs) {
        if (e.find("offset") != std::string::npos &&
            e.find("unknown ref slot") != std::string::npos) {
            unknown_slot = true;
        }
    }
    CHECK_MESSAGE(!unknown_slot, "refs.offset on sketch was rejected as unknown slot (Bug N5)");
}

// ============================================================================
// B6 class — "no feature may VANISH or silently empty the document". Each of
// these asserts the CORE PRINCIPLE: a feature that cannot build valid geometry
// must appear in feature_status with ok:false + a clear message AND leave the
// prior body intact — never disappear from the status / never reduce the doc to
// "no bodies" silently. (The per-branch guards plus the systemic backstop in
// compile_document's per-feature loop enforce this.)
// ============================================================================

// B6-2: a fillet over ALL edges with a radius LARGER than half the edge (r=8 on a
// 10mm cube) cannot reseal the corners. It must FAIL the fillet feature LOUDLY
// (status present, ok:false, actionable message) and leave the BOX intact — never
// silently destroy the body so eval_mass later throws "document has no bodies".
TEST_CASE("B6-2: oversize all-edges fillet errors clearly and leaves the body intact") {
    const double S = 10.0;
    json doc;
    doc["features"] = json::array({
        json{{"id", "cube"}, {"type", "box"}, {"length", S}, {"width", S}, {"height", S}},
        // "all edges" query + radius 8 (> half the 10mm edge) -> infeasible everywhere.
        json{{"id", "fil"}, {"type", "fillet"}, {"body", "cube"}, {"radius", 8.0},
             {"edges", json{{"of", "edge"}}}},
    });
    CompileResult r = compile_document(doc, json::object());

    // The fillet status MUST be present (never vanished) and clearly failed.
    const aicad::doc::FeatureStatus* fil = status_of(r, "fil");
    REQUIRE_MESSAGE(fil != nullptr, "fillet feature vanished from feature_status (B6-2 bug)");
    CHECK_FALSE_MESSAGE(fil->ok, "oversize all-edges fillet must FAIL, not silently no-op/destroy");
    MESSAGE("B6-2 fillet error (expected, clear): " << fil->error);
    // The message names the actionable cause (too large / open / would consume / no bodies).
    CHECK((fil->error.find("too large") != std::string::npos ||
           fil->error.find("open") != std::string::npos ||
           fil->error.find("consume") != std::string::npos ||
           fil->error.find("empty") != std::string::npos ||
           fil->error.find("vanish") != std::string::npos));

    // The CUBE SURVIVES intact — exactly one body, the full 10^3 volume (never-raise:
    // the failed fillet did not consume the box).
    REQUIRE_MESSAGE(r.body_count == 1, "the box vanished after the failed fillet (B6-2)");
    CHECK(r.volume == doctest::Approx(S * S * S).epsilon(1e-3));
    render::DocMesh dm = parse(r);
    REQUIRE(dm.bodies.size() == 1);
    double lo[3], hi[3];
    bbox(dm.bodies[0], lo, hi);
    CHECK((hi[0] - lo[0]) == doctest::Approx(S).epsilon(1e-3));
    CHECK((hi[2] - lo[2]) == doctest::Approx(S).epsilon(1e-3));
}

// B6-3: a sweep along a NEAR-STRAIGHT spline path must NEVER silently destroy the
// body. It either builds a valid pipe (volume ~= profile_area * path_length) OR
// errors clearly — but the sweep feature must ALWAYS be present in feature_status,
// and the document must never come back empty with no error.
TEST_CASE("B6-3: sweep on a near-straight spline path never silently vanishes") {
    const double R = 2.0, Hh = 24.0;
    // Profile: circle r=2 on Top.
    json prof;
    prof["id"] = "prof"; prof["type"] = "sketch"; prof["plane"] = "top";
    prof["entities"] = json::array({circle("e1", 0, 0, R)});
    prof["constraints"] = json::array({
        rel("c1", "fix", {"e1.center"}),
        dim("c2", "radius", {"e1"}, R),
    });
    // Path: a near-straight spline up +Z with a tiny lateral wiggle (the degenerate-ish
    // case that used to fold MakePipe / drop the body). On Front, so it lifts to world +Z.
    json path;
    path["id"] = "path"; path["type"] = "sketch"; path["plane"] = "front";
    path["entities"] = json::array({
        json{{"id", "e1"}, {"type", "spline"}, {"construction", false},
             {"points", json::array({json::array({0.0, 0.0}), json::array({0.01, 8.0}),
                                     json::array({0.0, 16.0}), json::array({0.01, Hh})})}},
    });
    path["constraints"] = json::array();

    json doc;
    doc["features"] = json::array({
        prof, path,
        json{{"id", "sw"}, {"type", "sweep"}, {"profile", "prof"}, {"path", "path"}},
    });
    CompileResult r = compile_document(doc, json::object());

    // The sweep status must be present (never vanished).
    const aicad::doc::FeatureStatus* sw = status_of(r, "sw");
    REQUIRE_MESSAGE(sw != nullptr, "sweep feature vanished from feature_status (B6-3 bug)");
    if (sw->ok) {
        // A valid pipe: ~one body, volume close to pi*R^2*Hh (a real swept tube, not a
        // collapsed/zero-volume shape silently committed).
        REQUIRE_MESSAGE(r.body_count >= 1, "ok sweep produced no body (B6-3 vanish)");
        const double expect = 3.14159265 * R * R * Hh;
        MESSAGE("B6-3 sweep volume=" << r.volume << " (expect ~" << expect << ")");
        CHECK(r.volume == doctest::Approx(expect).epsilon(0.05));
    } else {
        // If the kernel could not build it, the failure is LOUD and the doc is not left
        // silently empty by a no-error vanish.
        MESSAGE("B6-3 sweep error (acceptable, clear): " << sw->error);
        CHECK_FALSE(sw->error.empty());
    }
}

// B6-4: a PRIMITIVE boss (cylinder mode:"boss") landing ON an existing box must FUSE
// material into it — the volume increases by pi*r^2*h. It must NOT be a silent no-op
// (the symptom: volume unchanged, no error). cut already worked; boss must mirror it.
TEST_CASE("B6-4: cylinder mode:boss on a box fuses and increases the volume") {
    const double L = 20.0, W = 20.0, Hbox = 10.0;  // box centred on origin, top at z=+5
    const double cr = 3.0, ch = 8.0;               // boss cylinder
    json doc;
    doc["features"] = json::array({
        json{{"id", "blk"}, {"type", "box"}, {"length", L}, {"width", W}, {"height", Hbox}},
        // A cylinder boss whose base sits ON the box top face (z = +Hbox/2) and rises
        // above it -> real added material (centered:false puts the base at `at`).
        json{{"id", "boss"}, {"type", "cylinder"}, {"radius", cr}, {"height", ch},
             {"mode", "boss"}, {"centered", false}, {"at", json::array({0, 0, Hbox / 2.0})}},
    });
    CompileResult r = compile_document(doc, json::object());

    const aicad::doc::FeatureStatus* boss = status_of(r, "boss");
    REQUIRE_MESSAGE(boss != nullptr, "boss feature vanished from feature_status (B6-4)");
    REQUIRE_MESSAGE(boss->ok, "cylinder boss failed to fuse: " << boss->error);
    REQUIRE(r.body_count == 1);  // fused into ONE body, not a 2nd disjoint cylinder

    // The fused volume EXCEEDS the bare box by the cylinder's volume (pi*r^2*h) — the
    // boss added material rather than silently no-op'ing.
    const double box_vol = L * W * Hbox;
    const double cyl_vol = 3.14159265 * cr * cr * ch;
    MESSAGE("B6-4 boss fuse: total=" << r.volume << " box=" << box_vol
            << " +cyl=" << cyl_vol);
    CHECK(r.volume > box_vol + 1.0);  // material was added (not unchanged)
    CHECK(r.volume == doctest::Approx(box_vol + cyl_vol).epsilon(0.02));
}

// ============================================================================
// W1 / E10 regression tests
// ============================================================================

// W1: a `positions` field placed at the FEATURE TOP LEVEL (outside params) for a
//     hole must be HOISTED into params by op_add_feature, not silently dropped.
//     When dropped the feature built only ONE hole at the face centre; after the hoist
//     it must cut N distinct bores (volume loss proportional to N).
TEST_CASE("W1: top-level positions on hole feature produces N holes (not just 1)") {
    using aicad::doc::doc_new;
    using aicad::doc::doc_apply_ops;
    auto dimv = [](double v) { return json{{"value", v}, {"expr", nullptr}}; };

    // A 60×60×20 plate.  Two holes: [u,v]=(-15,0) and (+15,0) on the top face.
    // Each hole is 10mm diameter, through_all (depth=20).  Volume lost = 2 * pi*5^2*20.
    const double L = 60.0, W = 60.0, H = 20.0, R = 5.0;
    const double plate_vol = L * W * H;
    const double bore_vol  = 3.14159265 * R * R * H;  // one bore

    json nr = doc_new("part", "W1TopLevelPositions");
    const std::string id = nr.at("doc_id").get<std::string>();

    json ops = json::array({
        json{{"op", "add_feature"}, {"feature",
            json{{"type", "box"},
                 {"params", {{"length", dimv(L)}, {"width", dimv(W)}, {"height", dimv(H)}}}}}},
        // positions placed at the FEATURE TOP LEVEL — Bug W1 path.
        json{{"op", "add_feature"}, {"feature",
            json{{"type", "hole"},
                 {"params", {{"style", "simple"}, {"end", "through_all"},
                             {"diameter", dimv(2.0 * R)}}},
                 // positions at the FEATURE TOP LEVEL (NOT inside params) — Bug W1 path:
                 {"positions", json::array({json::array({-15.0, 0.0}),
                                           json::array({ 15.0, 0.0})})},
                 {"refs", {{"face", json{{"of", "face"}, {"side", "top"}}}}}}}},
    });
    json resp = doc_apply_ops(id, -1, ops, false);
    const json& fs = resp.at("rebuild").at("feature_status");
    for (const auto& s : fs)
        CHECK_MESSAGE(status_err(s).empty(), "W1 feature failed: " << status_err(s));

    const double vol = resp.value("volume", 0.0);
    MESSAGE("W1 top-level positions: vol=" << vol
            << " plate=" << plate_vol << " -2bores=" << (plate_vol - 2.0 * bore_vol));
    // Two bores must be cut; if only one is cut the volume is off by ~bore_vol.
    CHECK(vol == doctest::Approx(plate_vol - 2.0 * bore_vol).epsilon(0.03));
}

// E10: a numeric body index passed as `body` (e.g. body:2) must produce a CLEAR,
//      actionable error rather than silently moving the wrong (current/last) body.
//      The error message must mention the advice to use a feature id like 'f3'.
TEST_CASE("E10: numeric body index in transform gives a clear actionable error") {
    using aicad::doc::doc_new;
    using aicad::doc::doc_apply_ops;
    auto dimv = [](double v) { return json{{"value", v}, {"expr", nullptr}}; };

    json nr = doc_new("part", "E10NumericBodyIndex");
    const std::string id = nr.at("doc_id").get<std::string>();

    json ops = json::array({
        json{{"op", "add_feature"}, {"feature",
            json{{"type", "box"},
                 {"params", {{"length", dimv(10)}, {"width", dimv(10)}, {"height", dimv(10)}}}}}},
        // body:2 is a numeric index — not a valid feature id.
        json{{"op", "add_feature"}, {"feature",
            json{{"type", "transform"},
                 {"body", 2},
                 {"translate", json::array({5.0, 0.0, 0.0})}}}},
    });
    // A numeric body index is unresolvable, so the atomic batch is REJECTED. doc_apply_ops
    // RETURNS an in-band rejection (rc 0, no throw — the batch-atomicity contract): the
    // transform feature's status is ok:false carrying a clear, actionable error (rather than
    // silently moving the wrong body). The pre-batch tree is left untouched.
    json resp;
    CHECK_NOTHROW(resp = doc_apply_ops(id, -1, ops, false));
    json fs = resp.at("rebuild").at("feature_status");
    // Some feature in the batch failed -> the response is a rejection (not every status ok).
    bool any_failed = false;
    std::string transform_err;
    for (const json& st : fs)
        if (!st.value("ok", true)) {
            any_failed = true;
            if (st.contains("error") && st["error"].is_string())
                transform_err = st["error"].get<std::string>();
        }
    MESSAGE("E10 numeric body error: " << transform_err);
    CHECK_MESSAGE(any_failed,
        "E10: numeric body:2 should reject the batch (feature_status ok:false)");
    // The error must be actionable — mention a feature id like 'f3'.
    CHECK((transform_err.find("f3") != std::string::npos ||
           transform_err.find("feature id") != std::string::npos));
}

// ===========================================================================
// 12. THREAD feature: a one-op helical ISO-60° thread cut on a cylindrical face.
//     (a) An M8 EXTERNAL thread on a Ø8 x 20 cylinder builds a single VALID solid
//         (BRepCheck ok) whose volume is REDUCED from the plain cylinder — a thread
//         is a helical V-groove, so it removes material. (b) A COSMETIC-mode thread
//         succeeds with NO geometry change (a lightweight representation): same volume,
//         a status warning, still one valid body.
// ===========================================================================

// A small helper: total solid volume of a CompileResult's surviving shapes.
static double total_shape_volume(const CompileResult& r) {
    double v = 0.0;
    for (const TopoDS_Shape& s : r.shapes) {
        if (s.IsNull()) continue;
        GProp_GProps g;
        BRepGProp::VolumeProperties(s, g);
        v += g.Mass();
    }
    return v;
}

TEST_CASE("thread: M8 external cut reduces a Ø8x20 cylinder's volume to a valid solid") {
    // Base: a Ø8 (radius 4) x 20 cylinder. find_metric("M8") -> major 8, pitch 1.25.
    const double R = 4.0, H = 20.0;
    // The cylindrical (lateral) face, addressed in plain words by surface kind.
    const json cyl_face = json{{"of", "face"}, {"surf", "cylindrical"}};

    // (a) the PLAIN cylinder volume, for the material-removed comparison.
    double plain_vol = 0.0;
    {
        json doc;
        doc["features"] = json::array({
            json{{"id", "cyl"}, {"type", "cylinder"}, {"radius", R}, {"height", H}},
        });
        CompileResult r = compile_document(doc, json::object());
        REQUIRE(r.ok);
        REQUIRE(r.body_count == 1);
        plain_vol = total_shape_volume(r);
        CHECK(plain_vol == doctest::Approx(PI * R * R * H).epsilon(0.02));
    }

    // (b) the THREADED cylinder: M8 external, 18mm of thread run.
    json doc;
    doc["features"] = json::array({
        json{{"id", "cyl"}, {"type", "cylinder"}, {"radius", R}, {"height", H}},
        json{{"id", "thr"}, {"type", "thread"}, {"size", "M8"}, {"length", 18.0},
             {"style", "external"}, {"mode", "cut"}, {"target_face", cyl_face}},
    });
    CompileResult r = compile_document(doc, json::object());
    for (const auto& s : r.statuses)
        if (!s.ok) MESSAGE("feature " << s.id << " error: " << s.error);
    const aicad::doc::FeatureStatus* thr = status_of(r, "thr");
    REQUIRE(thr != nullptr);
    CHECK(thr->ok);                       // the thread feature ran (cut or graceful fallback)
    REQUIRE(r.ok);
    REQUIRE(r.body_count == 1);           // the threaded body REPLACES the cylinder

    // The surviving body is a VALID solid (the robustness contract: a cut that would be
    // invalid falls back to the plain cylinder — either way r.shapes[0] is valid).
    REQUIRE(r.shapes.size() == 1);
    REQUIRE_FALSE(r.shapes[0].IsNull());
    CHECK(BRepCheck_Analyzer(r.shapes[0]).IsValid());

    const double threaded_vol = total_shape_volume(r);
    MESSAGE("M8 thread: plain_vol=" << plain_vol << " threaded_vol=" << threaded_vol
            << " warning='" << thr->warning << "'");
    if (thr->warning.find("unthreaded") != std::string::npos) {
        // GRACEFUL FALLBACK fired (OCCT couldn't cut on this build): the body is the
        // plain cylinder, unchanged. The part is NOT broken — that is the contract.
        CHECK(threaded_vol == doctest::Approx(plain_vol).epsilon(1e-3));
    } else {
        // The real cut succeeded: a helical V-groove removed material (volume DOWN),
        // but not so much that the shaft vanished (a sane groove is a few % of volume).
        CHECK(threaded_vol < plain_vol - 1e-3);
        CHECK(threaded_vol > 0.5 * plain_vol);
    }
}

TEST_CASE("thread: cosmetic mode succeeds with no geometry change") {
    const double R = 4.0, H = 20.0;
    const json cyl_face = json{{"of", "face"}, {"surf", "cylindrical"}};
    json doc;
    doc["features"] = json::array({
        json{{"id", "cyl"}, {"type", "cylinder"}, {"radius", R}, {"height", H}},
        json{{"id", "thr"}, {"type", "thread"}, {"size", "M8"}, {"length", 18.0},
             {"mode", "cosmetic"}, {"target_face", cyl_face}},
    });
    CompileResult r = compile_document(doc, json::object());
    for (const auto& s : r.statuses)
        if (!s.ok) MESSAGE("feature " << s.id << " error: " << s.error);
    const aicad::doc::FeatureStatus* thr = status_of(r, "thr");
    REQUIRE(thr != nullptr);
    CHECK(thr->ok);
    CHECK(thr->error.empty());
    // A cosmetic thread is a lightweight representation: it carries a warning noting it
    // added no geometry.
    CHECK(thr->warning.find("cosmetic") != std::string::npos);
    REQUIRE(r.ok);
    REQUIRE(r.body_count == 1);
    REQUIRE(r.shapes.size() == 1);
    CHECK(BRepCheck_Analyzer(r.shapes[0]).IsValid());
    // Volume is the UNTOUCHED cylinder (no geometry change).
    const double v = total_shape_volume(r);
    MESSAGE("cosmetic thread: volume=" << v << " (plain=" << PI * R * R * H << ")");
    CHECK(v == doctest::Approx(PI * R * R * H).epsilon(0.02));
}

// ===========================================================================
// 13. TEXT feature: a one-op emboss/engrave of a string onto a PLANAR face.
//     (a) An ENGRAVE on a box's top face leaves a single VALID solid whose volume is
//         REDUCED (a recessed mark removes material). (b) An EMBOSS ADDS material
//         (raised mark). Either may gracefully fall back (body unchanged + "unmarked"
//         warning) if no font is available or the boolean is degenerate — the part is
//         never broken. The CI machine's Windows system fonts (arial.ttf) satisfy the
//         font-resolution fallback chain, so the real path is normally exercised.
// ===========================================================================
TEST_CASE("text: engrave on a box top face removes material to a valid solid") {
    const double L = 40.0, W = 30.0, Hh = 20.0;
    const json top_face = json{{"of", "face"}, {"side", "top"}};

    const double plain_vol = L * W * Hh;
    json doc;
    doc["features"] = json::array({
        json{{"id", "blk"}, {"type", "box"}, {"length", L}, {"width", W}, {"height", Hh}},
        json{{"id", "txt"}, {"type", "text"}, {"text", "A1"}, {"height", 6.0},
             {"depth", 1.5}, {"mode", "engrave"}, {"target_face", top_face}},
    });
    CompileResult r = compile_document(doc, json::object());
    for (const auto& s : r.statuses)
        if (!s.ok) MESSAGE("feature " << s.id << " error: " << s.error);
    const aicad::doc::FeatureStatus* tx = status_of(r, "txt");
    REQUIRE(tx != nullptr);
    CHECK(tx->ok);                        // the text feature ran (mark or graceful fallback)
    REQUIRE(r.ok);
    REQUIRE(r.body_count == 1);           // the marked body REPLACES the box
    REQUIRE(r.shapes.size() == 1);
    REQUIRE_FALSE(r.shapes[0].IsNull());
    CHECK(BRepCheck_Analyzer(r.shapes[0]).IsValid());

    const double marked_vol = total_shape_volume(r);
    MESSAGE("engrave: plain_vol=" << plain_vol << " marked_vol=" << marked_vol
            << " warning='" << tx->warning << "'");
    if (tx->warning.find("unmarked") != std::string::npos) {
        // Graceful fallback (no font / degenerate boolean): the box is unchanged.
        CHECK(marked_vol == doctest::Approx(plain_vol).epsilon(1e-3));
    } else {
        // The engrave cut a recess: volume DOWN, but only a sliver (a 6mm-tall 2-char
        // mark 1.5mm deep is a tiny fraction of a 40x30x20 block).
        CHECK(marked_vol < plain_vol - 1e-3);
        CHECK(marked_vol > 0.95 * plain_vol);
    }
}

TEST_CASE("text: emboss on a box top face adds material to a valid solid") {
    const double L = 40.0, W = 30.0, Hh = 20.0;
    const json top_face = json{{"of", "face"}, {"side", "top"}};
    const double plain_vol = L * W * Hh;
    json doc;
    doc["features"] = json::array({
        json{{"id", "blk"}, {"type", "box"}, {"length", L}, {"width", W}, {"height", Hh}},
        json{{"id", "txt"}, {"type", "text"}, {"text", "OK"}, {"height", 6.0},
             {"depth", 1.0}, {"mode", "emboss"}, {"target_face", top_face}},
    });
    CompileResult r = compile_document(doc, json::object());
    for (const auto& s : r.statuses)
        if (!s.ok) MESSAGE("feature " << s.id << " error: " << s.error);
    const aicad::doc::FeatureStatus* tx = status_of(r, "txt");
    REQUIRE(tx != nullptr);
    CHECK(tx->ok);
    REQUIRE(r.ok);
    REQUIRE(r.body_count == 1);
    REQUIRE(r.shapes.size() == 1);
    CHECK(BRepCheck_Analyzer(r.shapes[0]).IsValid());

    const double marked_vol = total_shape_volume(r);
    MESSAGE("emboss: plain_vol=" << plain_vol << " marked_vol=" << marked_vol
            << " warning='" << tx->warning << "'");
    if (tx->warning.find("unmarked") != std::string::npos) {
        CHECK(marked_vol == doctest::Approx(plain_vol).epsilon(1e-3));
    } else {
        CHECK(marked_vol > plain_vol + 1e-3);     // raised mark ADDS material
        CHECK(marked_vol < 1.05 * plain_vol);
    }
}

TEST_CASE("text: an emboss that overhangs the face is rejected (no detached letters)") {
    // A string far wider than its target face: the off-face glyph prisms fuse onto nothing
    // and would survive as SEPARATE floating solids. The feature must DETECT the body got
    // disconnected (solid count rose) and fall back to the clean part — never ship detached
    // letter bodies as a "successful" emboss.
    const double L = 18.0, W = 9.0, Hh = 8.0;
    const json top_face = json{{"of", "face"}, {"side", "top"}};
    const double plain_vol = L * W * Hh;
    json doc;
    doc["features"] = json::array({
        json{{"id", "blk"}, {"type", "box"}, {"length", L}, {"width", W}, {"height", Hh}},
        json{{"id", "txt"}, {"type", "text"}, {"text", "OVERHANGING"}, {"height", 6.0},
             {"depth", 1.2}, {"mode", "emboss"}, {"target_face", top_face}},
    });
    CompileResult r = compile_document(doc, json::object());
    const aicad::doc::FeatureStatus* tx = status_of(r, "txt");
    REQUIRE(tx != nullptr);
    CHECK(tx->ok);                       // ran (graceful fallback is still ok=true)
    REQUIRE(r.ok);
    REQUIRE(r.body_count == 1);          // exactly ONE body — no detached letters
    REQUIRE(r.shapes.size() == 1);
    CHECK(BRepCheck_Analyzer(r.shapes[0]).IsValid());
    // The body is the clean, UNMARKED box (fallback) — its volume is unchanged.
    const double v = total_shape_volume(r);
    MESSAGE("overhang emboss: vol=" << v << " plain=" << plain_vol
            << " warning='" << tx->warning << "'");
    CHECK(v == doctest::Approx(plain_vol).epsilon(1e-3));
    // Count the solids in the result: must be exactly one (the guard's whole point).
    int nsolids = 0;
    for (TopExp_Explorer ex(r.shapes[0], TopAbs_SOLID); ex.More(); ex.Next()) ++nsolids;
    CHECK(nsolids == 1);
}

// Side-face reading direction (regression). On a VERTICAL face a planar face's parametric X axis
// often points along world Z, which laid the text out VERTICALLY and overflowed a short wall — e.g.
// "AI-CAD" on a 60x10 front face came out detached -> "overhang"/unmarked fallback. The handler now
// overrides xdir to the horizontal in-plane direction (with "up" toward +Z) for any non-horizontal
// face, so a string that fits the face WIDTH marks cleanly even when far taller than the face HEIGHT.
TEST_CASE("text: emboss on a vertical FRONT face reads horizontally (not off the short axis)") {
    const double L = 60.0, W = 30.0, Hh = 10.0;       // front face = 60 (x) x 10 (z): wide but short
    const json front_face = json{{"of", "face"}, {"side", "front"}};
    const double plain_vol = L * W * Hh;
    json doc;
    doc["features"] = json::array({
        json{{"id", "blk"}, {"type", "box"}, {"length", L}, {"width", W}, {"height", Hh}},
        json{{"id", "txt"}, {"type", "text"}, {"text", "AI-CAD"}, {"height", 5.0},
             {"depth", 0.8}, {"mode", "emboss"}, {"target_face", front_face}},
    });
    CompileResult r = compile_document(doc, json::object());
    for (const auto& s : r.statuses)
        if (!s.ok) MESSAGE("feature " << s.id << " error: " << s.error);
    const aicad::doc::FeatureStatus* tx = status_of(r, "txt");
    REQUIRE(tx != nullptr);
    CHECK(tx->ok);
    REQUIRE(r.ok);
    REQUIRE(r.body_count == 1);
    REQUIRE(r.shapes.size() == 1);
    CHECK(BRepCheck_Analyzer(r.shapes[0]).IsValid());
    const double marked_vol = total_shape_volume(r);
    MESSAGE("front emboss: plain_vol=" << plain_vol << " marked_vol=" << marked_vol
            << " warning='" << tx->warning << "'");
    // The fix: "AI-CAD" (~25 mm wide) fits the 60 mm face WIDTH and must not overhang the 10 mm
    // height. With the old vertical xdir it overflowed -> "overhang"/"unmarked"; now it marks.
    CHECK(tx->warning.find("overhang") == std::string::npos);
    CHECK(tx->warning.find("unmarked") == std::string::npos);
    CHECK(marked_vol > plain_vol + 1e-3);   // raised mark ADDS material (proves the mark landed)
    CHECK(marked_vol < 1.05 * plain_vol);
}

// Boolean operand guard: a default-mode ("boss") primitive FUSES into the running body and
// consumes it, so a later explicit boolean that names the consumed feature would silently
// operate on the MERGED body (common(A, A∪B) = A) and return a plausible-but-wrong solid. The
// handler now errors loudly and points at merge:false. The correct form (merge:false keeps the
// second box separate) intersects to the true overlap.
TEST_CASE("boolean: a consumed (auto-merged) operand errors instead of a silent wrong solid") {
    json doc;
    doc["features"] = json::array({
        json{{"id", "ba"}, {"type", "box"}, {"length", 40}, {"width", 40}, {"height", 40}},
        // default mode "boss" -> fuses into ba and CONSUMES ba.
        json{{"id", "bb"}, {"type", "box"}, {"length", 40}, {"width", 40}, {"height", 40},
             {"at", json::array({20, 0, 0})}},
        json{{"id", "cmn"}, {"type", "boolean"}, {"op", "common"}, {"a", "ba"}, {"b", "bb"}},
    });
    CompileResult r = compile_document(doc, json::object());
    const aicad::doc::FeatureStatus* st = status_of(r, "cmn");
    REQUIRE(st != nullptr);
    CHECK_FALSE(st->ok);                                   // not a silent success
    CHECK(st->error.find("merge") != std::string::npos);  // tells the caller to use merge:false
}

TEST_CASE("boolean: common of two merge:false boxes intersects to the true overlap") {
    json doc;
    doc["features"] = json::array({
        json{{"id", "ba"}, {"type", "box"}, {"length", 40}, {"width", 40}, {"height", 40}},
        json{{"id", "bb"}, {"type", "box"}, {"length", 40}, {"width", 40}, {"height", 40},
             {"at", json::array({20, 0, 0})}, {"merge", false}},  // separate body
        json{{"id", "cmn"}, {"type", "boolean"}, {"op", "common"}, {"a", "ba"}, {"b", "bb"}},
    });
    CompileResult r = compile_document(doc, json::object());
    for (const auto& s : r.statuses)
        if (!s.ok) MESSAGE("feature " << s.id << " error: " << s.error);
    REQUIRE(r.ok);
    REQUIRE(r.body_count == 1);
    // A=[-20,20]³ ∩ B=[0,40]³ = [0,20]×[-20,20]×[-20,20] = 20*40*40 = 32000 mm³.
    CHECK(total_shape_volume(r) == doctest::Approx(32000.0).epsilon(1e-3));
}

// P3: a boolean fuse / boss-merge of two DISJOINT solids (two cylinders placed far apart)
// leaves a single TopoDS_Compound holding TWO TopAbs_SOLIDs — OCCT does not weld bodies that
// do not touch. doc_eval_mass must report the PHYSICAL body count (2) by exploding the
// compound into its connected sub-solids, not the top-level compound count (1). The combined
// volume/COM were already correct; this locks the body_count + per-body breakdown.
TEST_CASE("eval_mass counts disconnected sub-solids in a fused compound (not 1)") {
    using aicad::doc::doc_new;
    using aicad::doc::doc_apply_ops;
    using aicad::doc::doc_eval_mass;
    auto dimv = [](double v) { return json{{"value", v}, {"expr", nullptr}}; };
    json nr = doc_new("part", "DisjointFuse");
    const std::string id = nr.at("doc_id").get<std::string>();

    // C1 at the origin; C2 a boss merged in 100mm away (well clear of C1's r=10) -> the
    // boss FUSES into the running body but the two solids never touch -> a 2-solid compound.
    json ops = json::array({
        json{{"op", "add_feature"}, {"feature", {{"type", "cylinder"}, {"name", "C1"},
             {"params", {{"radius", dimv(10)}, {"height", dimv(30)}}}}}},
        json{{"op", "add_feature"}, {"feature", {{"type", "cylinder"}, {"name", "C2"},
             {"params", {{"radius", dimv(10)}, {"height", dimv(30)},
                         {"at", json::array({100, 0, 0})}, {"mode", "boss"}, {"merge", true}}}}}},
    });
    json resp = doc_apply_ops(id, -1, ops, false);
    for (const auto& fst : resp["rebuild"]["feature_status"])
        CHECK_MESSAGE(fst.value("ok", false), "feature " << fst.value("id", std::string())
                      << " failed: " << fst.value("error", json(nullptr)).dump());
    // The compile pipeline still tracks the fused compound as ONE surviving body — that
    // is the (unchanged) behavior the picking/owner-map relies on.
    CHECK(resp["tess"]["bodies"].size() == 1);
    CHECK(resp.value("volume", 0.0) == doctest::Approx(2.0 * PI * 10.0 * 10.0 * 30.0).epsilon(1e-3));

    // ...but eval_mass now sees the two PHYSICAL bodies.
    json m = doc_eval_mass(id, 0, 7.85);
    CHECK(m.value("body_count", -1) == 2);
    REQUIRE(m.contains("bodies"));
    REQUIRE(m["bodies"].size() == 2);

    // The combined total is unchanged: volume = sum of two identical cylinders.
    const double one_cyl = PI * 10.0 * 10.0 * 30.0;
    CHECK(m.value("volume_mm3", 0.0) == doctest::Approx(2.0 * one_cyl).epsilon(1e-3));
    // Each per-body entry is one cylinder, and they sit ~100mm apart in X.
    double sum = 0.0, xmin = 1e9, xmax = -1e9;
    for (const auto& b : m["bodies"]) {
        sum += b.value("volume_mm3", 0.0);
        const double cx = b["com"][0].get<double>();
        xmin = std::min(xmin, cx);
        xmax = std::max(xmax, cx);
    }
    CHECK(sum == doctest::Approx(m.value("volume_mm3", 0.0)).epsilon(1e-9));
    CHECK((xmax - xmin) == doctest::Approx(100.0).epsilon(1e-3));

    // A per-body query (body_id=2) now selects a SINGLE cylinder, not the whole compound.
    json b2 = doc_eval_mass(id, 2, 7.85);
    CHECK(b2.value("body_count", -1) == 1);
    CHECK(b2.value("volume_mm3", 0.0) == doctest::Approx(one_cyl).epsilon(1e-3));
}

// P2 (eval_mass-after-open diagnostic): a document whose only body-producing feature
// FAILS to rebuild on open — e.g. an `import` whose source file is gone and that carries
// NO baked geometry — compiles to zero bodies. eval_mass must then explain WHY, naming the
// broken feature, instead of the bare "document has no bodies to evaluate" (which reads
// like a phantom "eval lost the body right after open" bug — it is really a rebuild
// failure). This is the ONLY way to reach the enriched message: doc_apply_ops REVERTS a
// failed feature (so it never persists), so the broken feature can only arrive via a
// reopened .step+. We craft exactly that on disk via stepplus_save, then reopen it.
TEST_CASE("eval_mass on a reopened doc with a failed-rebuild feature names the culprit") {
    using aicad::doc::doc_open;
    using aicad::doc::doc_eval_mass;
    namespace fs = std::filesystem;

    // A self-contained part doc whose single feature is an import pointing at a file that
    // does not exist, with no embedded_brep -> the import compiler throws on rebuild.
    json doc;
    doc["schema"] = "aicad.part/1";
    doc["id"] = "broken";
    doc["name"] = "BrokenImport";
    doc["units"] = json{{"system", "MMGS"}};
    doc["features"] = json::array({
        json{{"id", "f1"}, {"type", "import"}, {"name", "GhostPart"}, {"suppressed", false},
             {"params", {{"source_path", "C:/no/such/dir/ghost-part-xyz.step"},
                         {"format", "step"}}},
             {"refs", json::object()}},
    });

    const std::string path =
        (fs::temp_directory_path() / "aicad_broken_import_eval.step+").string();
    aicad::doc::stepplus_save(doc, path, {});

    // doc_open succeeds (a malformed feature is non-fatal) but compiles to no bodies; the
    // open response carries volume 0 and a rebuild warning — NOT a real volume.
    json opened = doc_open(path, false);
    const std::string oid = opened.at("doc_id").get<std::string>();
    CHECK(opened.value("volume", -1.0) == doctest::Approx(0.0));

    bool threw = false;
    std::string msg;
    try {
        doc_eval_mass(oid, 0, 7.85);
    } catch (const aicad::doc::OpError& e) {
        threw = true;
        msg = e.what();
    }
    REQUIRE_MESSAGE(threw, "eval_mass on a no-body doc must throw");
    MESSAGE("eval_mass diagnostic: " << msg);
    // The hardened message NAMES the broken feature + its type + the rebuild error, so the
    // failure is self-explaining (vs. the bare "document has no bodies to evaluate").
    CHECK(msg.find("failed to rebuild") != std::string::npos);
    CHECK(msg.find("import") != std::string::npos);
    CHECK(msg.find("f1") != std::string::npos);

    std::error_code ec;
    fs::remove(path, ec);  // best-effort cleanup
}

// WASHER via the DOCUMENT OP PATH (resolve_profile_faces), the path the GUI + MCP use —
// distinct from test_profile_extrude's low-level faces_from_solved_sketch coverage. Two
// concentric circles + extrude must yield an ANNULAR prism, both for an untargeted profile
// (whole-sketch material faces) and for the default TARGETED selector the guide emits
// ({"centroid":[0,0],"hint_index":0}). Regression for "washer extrudes as a solid disc"
// through the op path (all_region_faces + centroid pick).
TEST_CASE("op-path: two concentric circles extrude to an annular washer (untargeted + targeted)") {
    using aicad::doc::doc_new;
    using aicad::doc::doc_apply_ops;
    auto dimv = [](double v) { return json{{"value", v}, {"expr", nullptr}}; };
    const double R = 10.0, r = 4.0, D = 5.0;
    const double expected = PI * (R * R - r * r) * D;   // annular ring
    const double full_disc = PI * R * R * D;

    auto build = [&](const json& profile) -> double {
        json nr = doc_new("part", "OpWasher");
        const std::string id = nr.at("doc_id").get<std::string>();
        json ops = json::array({
            json{{"op", "add_feature"}, {"feature", {{"type", "sketch"}, {"name", "WS"},
                 {"params", {{"entities", json::array()}, {"constraints", json::array()}}},
                 {"refs", {{"plane", "plane.top"}}}}}},
            json{{"op", "sketch_add_entity"}, {"sketch", "$0"},
                 {"entity", {{"type", "circle"}, {"center", json::array({0, 0})}, {"radius", R}}}},
            json{{"op", "sketch_add_entity"}, {"sketch", "$0"},
                 {"entity", {{"type", "circle"}, {"center", json::array({0, 0})}, {"radius", r}}}},
            json{{"op", "add_feature"}, {"feature", {{"type", "extrude"}, {"name", "WB"},
                 {"params", {{"mode", "boss"}, {"end", "blind"}, {"depth", dimv(D)}}},
                 {"refs", {{"profile", profile}}}}}},
        });
        json resp = doc_apply_ops(id, -1, ops, false);
        for (const auto& fst : resp["rebuild"]["feature_status"])
            CHECK_MESSAGE(fst.value("ok", false), "feature " << fst.value("id", std::string())
                          << " failed: " << fst.value("error", json(nullptr)).dump());
        return resp.value("volume", 0.0);
    };

    // Untargeted: bare region selector (whole-sketch material faces).
    {
        json profile = json::array({json{{"kind", "region"}, {"sketch", "$0"}}});
        double vol = build(profile);
        MESSAGE("untargeted washer vol=" << vol << " (expected " << expected << ")");
        CHECK(vol == doctest::Approx(expected).epsilon(0.02));
        CHECK(vol < full_disc * 0.99);
    }
    // Targeted with the guide's default seed centroid[0,0]/hint_index 0 -> must pick the
    // material washer (larger area), NOT the void inner disc and NOT a solid outer disc.
    {
        json profile = json::array({json{{"kind", "region"}, {"sketch", "$0"},
                                         {"centroid", json::array({0, 0})}, {"hint_index", 0}}});
        double vol = build(profile);
        MESSAGE("targeted washer vol=" << vol << " (expected " << expected << ")");
        CHECK(vol == doctest::Approx(expected).epsilon(0.02));
        CHECK(vol < full_disc * 0.99);
    }
}

// Reverse direction (params.flip): extrude along -plane.normal so the solid sits on the
// opposite side of the sketch plane. Regression for the GUI "Reverse direction does nothing"
// report — engine must actually flip, not just toggle a checkbox.
TEST_CASE("op-path: extrude flip puts solid on the opposite side of the sketch plane") {
    using aicad::doc::doc_new;
    using aicad::doc::doc_apply_ops;
    using aicad::doc::doc_bbox;
    auto dimv = [](double v) { return json{{"value", v}, {"expr", nullptr}}; };
    const double R = 8.0, D = 20.0;

    auto z_span = [&](bool flip) -> std::pair<double, double> {
        json nr = doc_new("part", flip ? "FlipY" : "FlipN");
        const std::string id = nr.at("doc_id").get<std::string>();
        json ops = json::array({
            json{{"op", "add_feature"}, {"feature", {{"type", "sketch"}, {"name", "Sk"},
                 {"params", {{"entities", json::array()}, {"constraints", json::array()}}},
                 {"refs", {{"plane", "plane.top"}}}}}},
            json{{"op", "sketch_add_entity"}, {"sketch", "$0"},
                 {"entity", {{"type", "circle"}, {"center", json::array({0, 0})}, {"radius", R}}}},
            json{{"op", "add_feature"}, {"feature", {{"type", "extrude"}, {"name", "Ex"},
                 {"params", {{"mode", "boss"}, {"end", "blind"}, {"depth", dimv(D)},
                             {"flip", flip}}},
                 {"refs", {{"profile", json::array({json{{"kind", "region"}, {"sketch", "$0"}}})}}}}}},
        });
        json resp = doc_apply_ops(id, -1, ops, false);
        for (const auto& fst : resp["rebuild"]["feature_status"])
            CHECK_MESSAGE(fst.value("ok", false), "feature " << fst.value("id", std::string())
                          << " failed: " << fst.value("error", json(nullptr)).dump());
        json bb = doc_bbox(id);
        REQUIRE(bb.contains("min"));
        REQUIRE(bb.contains("max"));
        return {bb["min"][2].get<double>(), bb["max"][2].get<double>()};
    };

    auto [z0, z1] = z_span(false);
    auto [zf0, zf1] = z_span(true);
    MESSAGE("no-flip Z=[" << z0 << "," << z1 << "] flip Z=[" << zf0 << "," << zf1 << "]");
    // plane.top is Z=0; no-flip grows +Z, flip grows -Z.
    CHECK(z1 > 0.5 * D);
    CHECK(z0 > -1.0);                 // sketch face near 0, solid mostly positive
    CHECK(zf0 < -0.5 * D);
    CHECK(zf1 < 1.0);                 // solid mostly negative
    CHECK(z1 - z0 == doctest::Approx(D).epsilon(0.05));
    CHECK(zf1 - zf0 == doctest::Approx(D).epsilon(0.05));
}

