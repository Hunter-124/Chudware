// A3 milestone verification: the PATTERN feature compilers — linear_pattern and
// circular_pattern (core/doc/document.cpp) — which REPLICATE a referenced seed solid
// with rigid transforms (geom/transform.cpp: translate_copy / rotate_copy) and FUSE
// the copies into one result body, in the simplified id-ref model of the doc.
//
// It follows the EXACT round-trip discipline of test_document.cpp: compile a document,
// parse the resulting AICM with the HOST loader (host/src/render/aicm_loader = the
// contract oracle), and assert TIGHT, ANALYTIC, OCCT-version-INDEPENDENT invariants on
// the parsed body (planar/cylindrical face counts, bbox extents, mesh VOLUME computed
// from the tessellation) plus the CompileResult bookkeeping (status ok, surviving
// body_count — the seed is consumed into the pattern result). The assertions ARE the
// product — they are NOT weakened to pass.
//
// The two seeds are deliberately DISJOINT (gaps between the patterned cubes), so the
// fuse is a clean disjoint union: the result keeps each cube's 6 faces (no merged or
// coplanar faces to collapse) and its volume is exactly N * cube_volume.
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
using chudware::doc::CompileResult;
using chudware::doc::compile_document;

namespace {

// ---- sketch DSL (mirrors the helpers in test_document.cpp) ------------------
json line(const char* id, double x1, double y1, double x2, double y2) {
    return json{{"id", id}, {"type", "line"}, {"construction", false},
                {"p1", json::array({x1, y1})}, {"p2", json::array({x2, y2})}};
}
json rel(const char* id, const char* type, std::vector<const char*> refs) {
    json r = json::array();
    for (auto s : refs) r.push_back(s);
    return json{{"id", id}, {"type", type}, {"refs", r}};
}
// A distance/radius dim. `value` is a number; `expr` is an optional expression
// string (when non-null it drives the dim, evaluated against env).
json dim(const char* id, const char* type, std::vector<const char*> refs,
         double value, const json& expr = json(nullptr)) {
    json r = json::array();
    for (auto s : refs) r.push_back(s);
    json val = json{{"value", value}, {"expr", expr}};
    return json{{"id", id}, {"type", type}, {"refs", r}, {"value", val}, {"driven", false}};
}

// Build a fully-constrained rectangle (min corner at x0,y0; width w, height h) as the
// entities+constraints of a sketch.
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

// ---- AICM inspection helpers (identical contract to test_document.cpp) ------
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

// Mesh volume by the signed-tetrahedra (divergence) formula: for a closed,
// outward-oriented triangle mesh, V = (1/6) * sum over triangles of v0 . (v1 x v2),
// where (v0,v1,v2) are the triangle's vertex positions (origin as the apex). This is
// EXACT for planar-faceted solids (the patterned cubes), independent of OCCT version /
// tessellation density. fabs() guards against an overall orientation flip.
double mesh_volume(const render::BodyMesh& b) {
    double six_v = 0.0;
    for (uint32_t t = 0; t < b.n_tris; ++t) {
        const uint32_t i0 = b.idx[3ull * t + 0];
        const uint32_t i1 = b.idx[3ull * t + 1];
        const uint32_t i2 = b.idx[3ull * t + 2];
        const double v0[3] = {b.pos[3ull * i0 + 0], b.pos[3ull * i0 + 1], b.pos[3ull * i0 + 2]};
        const double v1[3] = {b.pos[3ull * i1 + 0], b.pos[3ull * i1 + 1], b.pos[3ull * i1 + 2]};
        const double v2[3] = {b.pos[3ull * i2 + 0], b.pos[3ull * i2 + 1], b.pos[3ull * i2 + 2]};
        // v0 . (v1 x v2)
        const double cx = v1[1] * v2[2] - v1[2] * v2[1];
        const double cy = v1[2] * v2[0] - v1[0] * v2[2];
        const double cz = v1[0] * v2[1] - v1[1] * v2[0];
        six_v += v0[0] * cx + v0[1] * cy + v0[2] * cz;
    }
    return std::fabs(six_v) / 6.0;
}

// Parse a CompileResult's AICM blob through the host loader (the contract oracle).
render::DocMesh parse(const CompileResult& r) {
    render::DocMesh doc;
    std::string err;
    bool ok = render::parseAicm(r.aicm.data(), r.aicm.size(), doc, err);
    REQUIRE_MESSAGE(ok, "host loader rejected AICM: " << err);
    return doc;
}

// A circle entity for the sweep-profile J3 test (mirrors the test_document DSL).
json circle(const char* id, double cx, double cy, double r) {
    return json{{"id", id}, {"type", "circle"}, {"construction", false},
                {"center", json::array({cx, cy})}, {"radius", r}};
}

constexpr double PI_CONST = 3.14159265358979323846;

}  // namespace

// 1. LINEAR PATTERN. Seed cube [0,10]^3 (sketch rect 0,0,10,10 on top -> extrude 10),
//    then linear_pattern{seed:x1, count1:3, spacing1:20, dir1:+X}. The 3 cubes sit at
//    x in {0,20,40} (disjoint, gap 10 between them). The seed is consumed into the
//    pattern result, so exactly ONE body survives: a 50 x 10 x 10 bbox carrying 18
//    planar faces (3 cubes * 6), no cylinders, and a total volume of 3 * 1000 = 3000.
TEST_CASE("linear pattern: 3 disjoint cubes fused into one body") {
    json doc;
    doc["features"] = json::array({
        sketch_feature("s1", "top", rect_entities(0, 0, 10, 10)),
        json{{"id", "x1"}, {"type", "extrude"}, {"profile", "s1"}, {"depth", 10.0}},
        json{{"id", "lp"}, {"type", "linear_pattern"}, {"seed", "x1"},
             {"count1", 3}, {"spacing1", 20.0}, {"dir1", json::array({1, 0, 0})}},
    });

    CompileResult r = compile_document(doc, json::object());
    REQUIRE(r.ok);
    REQUIRE(r.body_count == 1);  // seed consumed into the pattern result
    CHECK(r.statuses.size() == 3);
    for (const auto& s : r.statuses) CHECK(s.ok);

    render::DocMesh dm = parse(r);
    REQUIRE(dm.bodies.size() == 1);
    const render::BodyMesh& b = dm.bodies[0];
    check_common(b);

    double lo[3], hi[3];
    bbox(b, lo, hi);
    const double vol = mesh_volume(b);
    MESSAGE("linear: body_count=" << r.body_count << " faces=" << b.faces.size()
            << " planar=" << count_surf(b, 0) << " cyl=" << count_surf(b, 1)
            << " bbox=" << (hi[0]-lo[0]) << "x" << (hi[1]-lo[1]) << "x" << (hi[2]-lo[2])
            << " volume=" << vol << " (expected 3000)");

    CHECK(count_surf(b, 0) == 18);  // 3 cubes * 6 planar faces (disjoint, none merged)
    CHECK(count_surf(b, 1) == 0);   // no curved faces
    CHECK((hi[0] - lo[0]) == doctest::Approx(50.0).epsilon(1e-4));
    CHECK((hi[1] - lo[1]) == doctest::Approx(10.0).epsilon(1e-4));
    CHECK((hi[2] - lo[2]) == doctest::Approx(10.0).epsilon(1e-4));
    CHECK(vol == doctest::Approx(3000.0).epsilon(1e-3));
}

// 2. CIRCULAR PATTERN. Seed cube [17,23]x[-3,3]x[0,6] (sketch rect 17,-3,6,6 on top ->
//    extrude 6 = a 6x6x6 cube centred at (20,0,3)), then circular_pattern{seed:x1,
//    axis_pt:origin, axis_dir:+Z, count:4, angle:360}. The 4 cubes sit centred at
//    (20,0),(0,20),(-20,0),(0,-20) (disjoint). The seed is consumed, so ONE body
//    survives: a 46 x 46 x 6 bbox (x,y span [-23,23]) carrying 24 planar faces (4*6),
//    no cylinders, total volume 4 * 216 = 864.
TEST_CASE("circular pattern: 4 disjoint cubes fused into one body") {
    json doc;
    doc["features"] = json::array({
        sketch_feature("s1", "top", rect_entities(17, -3, 6, 6)),
        json{{"id", "x1"}, {"type", "extrude"}, {"profile", "s1"}, {"depth", 6.0}},
        json{{"id", "cp"}, {"type", "circular_pattern"}, {"seed", "x1"},
             {"axis_pt", json::array({0, 0, 0})}, {"axis_dir", json::array({0, 0, 1})},
             {"count", 4}, {"angle", 360.0}},
    });

    CompileResult r = compile_document(doc, json::object());
    REQUIRE(r.ok);
    REQUIRE(r.body_count == 1);  // seed consumed into the pattern result
    CHECK(r.statuses.size() == 3);
    for (const auto& s : r.statuses) CHECK(s.ok);

    render::DocMesh dm = parse(r);
    REQUIRE(dm.bodies.size() == 1);
    const render::BodyMesh& b = dm.bodies[0];
    check_common(b);

    double lo[3], hi[3];
    bbox(b, lo, hi);
    const double vol = mesh_volume(b);
    MESSAGE("circular: body_count=" << r.body_count << " faces=" << b.faces.size()
            << " planar=" << count_surf(b, 0) << " cyl=" << count_surf(b, 1)
            << " bbox=" << (hi[0]-lo[0]) << "x" << (hi[1]-lo[1]) << "x" << (hi[2]-lo[2])
            << " volume=" << vol << " (expected 864)");

    CHECK(count_surf(b, 0) == 24);  // 4 cubes * 6 planar faces (disjoint, none merged)
    CHECK(count_surf(b, 1) == 0);   // no curved faces
    CHECK((hi[0] - lo[0]) == doctest::Approx(46.0).epsilon(1e-4));  // x in [-23,23]
    CHECK((hi[1] - lo[1]) == doctest::Approx(46.0).epsilon(1e-4));  // y in [-23,23]
    CHECK((hi[2] - lo[2]) == doctest::Approx(6.0).epsilon(1e-4));
    CHECK(vol == doctest::Approx(864.0).epsilon(1e-3));
}

// 3. NESTED CUT PATTERN (Bug J1). A box with a circular_pattern of HOLES (a cut seed),
//    then a linear_pattern OF that circular_pattern. The outer pattern must HONOUR the
//    inner seed's CUT op and REMOVE material at each cell — so the final volume is LESS
//    than the bare box. Before the fix the outer pattern treated the inner cut-pattern
//    as additive ("add" op on the whole holed body) and FUSED a translated copy, adding
//    material (and leaving only one ring of holes). Exactly one valid body survives.
TEST_CASE("nested linear pattern of a circular cut pattern removes material (J1)") {
    const double L = 60, W = 30, D = 10;  // box x[-30,30] y[-15,15] z[0,10]
    json doc;
    doc["features"] = json::array({
        sketch_feature("s1", "top", rect_entities(-30, -15, L, W)),
        json{{"id", "box"}, {"type", "extrude"}, {"profile", "s1"}, {"depth", D}},
        // Seed: a through hole offset 8 mm from the top-face centre (a CUT).
        json{{"id", "h"}, {"type", "hole"},
             {"face", json{{"of", "face"}, {"side", "top"}}},
             {"diameter", 3.0}, {"end", "through_all"},
             {"positions", json::array({json::array({8.0, 0.0})})}},
        // Inner: 3-hole ring around +Z at radius 8.
        json{{"id", "cp"}, {"type", "circular_pattern"}, {"seed", "h"},
             {"axis_pt", json::array({0, 0, 0})}, {"axis_dir", json::array({0, 0, 1})},
             {"count", 3}, {"angle", 360.0}},
        // Outer: replicate the whole ring along +X (a second ring centred at x=12).
        json{{"id", "lp"}, {"type", "linear_pattern"}, {"seed", "cp"},
             {"count1", 2}, {"spacing1", 12.0}, {"dir1", json::array({1, 0, 0})}},
    });

    CompileResult r = compile_document(doc, json::object());
    REQUIRE(r.ok);
    for (const auto& s : r.statuses) CHECK(s.ok);
    REQUIRE(r.body_count == 1);

    render::DocMesh dm = parse(r);
    REQUIRE(dm.bodies.size() == 1);
    const render::BodyMesh& b = dm.bodies[0];
    check_common(b);
    const double box_vol = L * W * D;  // 18000
    const double vol = mesh_volume(b);
    MESSAGE("J1 nested cut: body_count=" << r.body_count << " volume=" << vol
            << " (box alone=" << box_vol << ")");
    // Material was REMOVED, not added: the nested cut pattern bored two rings of holes.
    CHECK(vol < box_vol - 1.0);
}

// 4. INSTANCE CAP (Bug K1). A 100x100 linear pattern (10000 cells) used to HANG and
//    crash the server. It must now ERROR CLEANLY (before any geometry runs) with a
//    message naming the cap — and never hang. The seed cube still builds, so the doc is
//    not empty; only the pattern feature fails.
TEST_CASE("over-cap linear pattern errors instead of hanging (K1)") {
    json doc;
    doc["features"] = json::array({
        sketch_feature("s1", "top", rect_entities(0, 0, 5, 5)),
        json{{"id", "x1"}, {"type", "extrude"}, {"profile", "s1"}, {"depth", 5.0}},
        json{{"id", "lp"}, {"type", "linear_pattern"}, {"seed", "x1"},
             {"count1", 100}, {"spacing1", 10.0}, {"dir1", json::array({1, 0, 0})},
             {"count2", 100}, {"spacing2", 10.0}, {"dir2", json::array({0, 1, 0})}},
    });

    CompileResult r = compile_document(doc, json::object());
    CHECK_FALSE(r.ok);  // the pattern feature failed
    const chudware::doc::FeatureStatus* lp = nullptr;
    for (const auto& s : r.statuses) if (s.id == "lp") lp = &s;
    REQUIRE(lp != nullptr);
    CHECK_FALSE(lp->ok);
    CHECK(lp->error.find("exceeds the limit") != std::string::npos);
    // The seed cube still survives (a contained per-feature error).
    CHECK(r.body_count == 1);
}

// 5. A circular pattern over the cap errors too (same guard, count path).
TEST_CASE("over-cap circular pattern errors instead of hanging (K1)") {
    json doc;
    doc["features"] = json::array({
        sketch_feature("s1", "top", rect_entities(17, -3, 6, 6)),
        json{{"id", "x1"}, {"type", "extrude"}, {"profile", "s1"}, {"depth", 6.0}},
        json{{"id", "cp"}, {"type", "circular_pattern"}, {"seed", "x1"},
             {"axis_pt", json::array({0, 0, 0})}, {"axis_dir", json::array({0, 0, 1})},
             {"count", 9000}, {"angle", 360.0}},
    });
    CompileResult r = compile_document(doc, json::object());
    CHECK_FALSE(r.ok);
    const chudware::doc::FeatureStatus* cp = nullptr;
    for (const auto& s : r.statuses) if (s.id == "cp") cp = &s;
    REQUIRE(cp != nullptr);
    CHECK_FALSE(cp->ok);
    CHECK(cp->error.find("exceeds the limit") != std::string::npos);
}

// 6. LOFT BOSS IS PATTERNABLE (Bug J3). A loft between two offset square sections, then
//    a linear_pattern of that loft. Before the fix loft left tool_body_of unset, so the
//    pattern failed with "feature has no patternable geometry". The pattern must produce
//    a single fused body whose volume exceeds the single loft (replication happened).
TEST_CASE("loft boss is a valid pattern seed (J3)") {
    json doc;
    // Section A on Top (z=0), section B on a plane offset +20 in Z (so they are not
    // coplanar -> a real lofted prism). Both 10x10 squares at the origin.
    json planeB = json{{"id", "pb"}, {"type", "plane"}, {"method", "offset"},
                       {"base", "plane.top"}, {"offset", 20.0}};
    doc["features"] = json::array({
        sketch_feature("sa", "top", rect_entities(-5, -5, 10, 10)),
        planeB,
        json{{"id", "sb"}, {"type", "sketch"}, {"plane", "pb"},
             {"entities", rect_entities(-5, -5, 10, 10).entities},
             {"constraints", rect_entities(-5, -5, 10, 10).constraints}},
        json{{"id", "lf"}, {"type", "loft"}, {"sections", json::array({"sa", "sb"})}},
        json{{"id", "lp"}, {"type", "linear_pattern"}, {"seed", "lf"},
             {"count1", 2}, {"spacing1", 30.0}, {"dir1", json::array({1, 0, 0})}},
    });
    CompileResult r = compile_document(doc, json::object());
    REQUIRE(r.ok);
    for (const auto& s : r.statuses) CHECK(s.ok);
    REQUIRE(r.body_count == 1);
    render::DocMesh dm = parse(r);
    REQUIRE(dm.bodies.size() == 1);
    const double one_loft = 10.0 * 10.0 * 20.0;  // 2000 (prismatic loft of equal squares)
    const double vol = mesh_volume(dm.bodies[0]);
    MESSAGE("J3 loft pattern: volume=" << vol << " (one loft=" << one_loft << ")");
    CHECK(vol > one_loft + 1.0);  // the loft was replicated (>1 instance)
}

// 7. SWEEP BOSS IS PATTERNABLE (Bug J3). A sweep (circle along a vertical line), then a
//    linear_pattern of it. The sweep must register a patternable tool so the pattern
//    builds a single fused body with more volume than one sweep.
TEST_CASE("sweep boss is a valid pattern seed (J3)") {
    const double R = 3.0, Hh = 20.0;
    json prof;
    prof["id"] = "prof"; prof["type"] = "sketch"; prof["plane"] = "top";
    prof["entities"] = json::array({circle("e1", 0, 0, R)});
    prof["constraints"] = json::array({
        rel("c1", "fix", {"e1.center"}),
        dim("c2", "radius", {"e1"}, R),
    });
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
        json{{"id", "lp"}, {"type", "linear_pattern"}, {"seed", "sw"},
             {"count1", 3}, {"spacing1", 12.0}, {"dir1", json::array({1, 0, 0})}},
    });
    CompileResult r = compile_document(doc, json::object());
    REQUIRE(r.ok);
    for (const auto& s : r.statuses) CHECK(s.ok);
    REQUIRE(r.body_count == 1);
    render::DocMesh dm = parse(r);
    REQUIRE(dm.bodies.size() == 1);
    const double one_sweep = PI_CONST * R * R * Hh;
    const double vol = mesh_volume(dm.bodies[0]);
    MESSAGE("J3 sweep pattern: volume=" << vol << " (one sweep=" << one_sweep << ")");
    CHECK(vol > one_sweep + 1.0);  // replicated (3 disjoint cylinders)
}
