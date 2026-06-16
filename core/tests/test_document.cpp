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

#include <cmath>
#include <cstdint>
#include <limits>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

#include "doc/document.h"
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
