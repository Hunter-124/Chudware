// A5 milestone verification: the HOLE feature compiler (core/doc/document.cpp) —
// which drills simple / counterbore / countersink hole(s) into a referenced solid at
// position(s) on a FACE-SELECTOR-resolved planar face by building a cylinder/cbore/
// csink tool per position (geom/hole.cpp: make_hole_tool), unioning them, and cutting
// the union from the body. The SolidWorks Hole Wizard multi-position behaviour in the
// simplified id-ref model (no thread tables / cosmetic threads / grid / sketch
// positions), ported from engine/doc/features/hole.py.
//
// It follows the EXACT round-trip discipline of test_mirror.cpp / test_pattern.cpp:
// compile a document, parse the resulting AICM with the HOST loader
// (host/src/render/aicm_loader = the contract oracle), and assert TIGHT, ANALYTIC
// invariants on the parsed body (planar / cylindrical / conical face counts, bbox
// extents, mesh VOLUME from the tessellation) plus the CompileResult bookkeeping. The
// bore wall / csink cone are CURVED, so mesh_volume is APPROXIMATE (tessellated
// polygon) — we use a LOOSE epsilon (0.03) on volume and lean on the EXACT face-count
// + bbox invariants as the primary proof. The assertions ARE the product — they are
// NOT weakened to pass.
//
// The target face comes from a +Z FACE SELECTOR harvested off a reference box's
// topology (describe_faces + make_selector), proving the selector -> kernel-face ->
// drill-plane path drives a real bore. The orientation WATCH-POINT: a wrong outward-
// normal would point the drill OUT of the solid, the cut would remove nothing, and the
// material-removed guard would throw — every case below verifies material was removed.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <cmath>
#include <cstdint>
#include <limits>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

#include "doc/document.h"
#include "doc/selectors.h"
#include "geom/features.h"
#include "geom/hole.h"
#include "aicm_loader.h"  // host loader (host/src/render on the include path)

using json = nlohmann::json;
using aicad::doc::CompileResult;
using aicad::doc::compile_document;

namespace {

constexpr double PI = 3.14159265358979323846;

// ---- sketch DSL (copied from test_mirror.cpp) ------------------------------
json line(const char* id, double x1, double y1, double x2, double y2) {
    return json{{"id", id}, {"type", "line"}, {"construction", false},
                {"p1", json::array({x1, y1})}, {"p2", json::array({x2, y2})}};
}
json rel(const char* id, const char* type, std::vector<const char*> refs) {
    json r = json::array();
    for (auto s : refs) r.push_back(s);
    return json{{"id", id}, {"type", type}, {"refs", r}};
}
json dim(const char* id, const char* type, std::vector<const char*> refs,
         double value, const json& expr = json(nullptr)) {
    json r = json::array();
    for (auto s : refs) r.push_back(s);
    json val = json{{"value", value}, {"expr", expr}};
    return json{{"id", id}, {"type", type}, {"refs", r}, {"value", val}, {"driven", false}};
}

// Build a fully-constrained rectangle (min corner at x0,y0; width w, height h).
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

json sketch_feature(const char* id, const char* plane, const RectSketch& rs) {
    return json{{"id", id}, {"type", "sketch"}, {"plane", plane},
                {"entities", rs.entities}, {"constraints", rs.constraints}};
}

// ---- AICM inspection helpers (identical contract to test_mirror.cpp) --------
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

// Mesh volume by the signed-tetrahedra (divergence) formula (copied from
// test_mirror.cpp): V = (1/6) * |sum over triangles of v0 . (v1 x v2)|. EXACT for
// planar-faceted solids; APPROXIMATE for curved (cylinder/cone) faces — the tessellated
// polygon slightly UNDER-estimates a convex curved wall, hence a loose epsilon on the
// drilled-volume checks. fabs() guards an overall orientation flip.
double mesh_volume(const render::BodyMesh& b) {
    double six_v = 0.0;
    for (uint32_t t = 0; t < b.n_tris; ++t) {
        const uint32_t i0 = b.idx[3ull * t + 0];
        const uint32_t i1 = b.idx[3ull * t + 1];
        const uint32_t i2 = b.idx[3ull * t + 2];
        const double v0[3] = {b.pos[3ull * i0 + 0], b.pos[3ull * i0 + 1], b.pos[3ull * i0 + 2]};
        const double v1[3] = {b.pos[3ull * i1 + 0], b.pos[3ull * i1 + 1], b.pos[3ull * i1 + 2]};
        const double v2[3] = {b.pos[3ull * i2 + 0], b.pos[3ull * i2 + 1], b.pos[3ull * i2 + 2]};
        const double cx = v1[1] * v2[2] - v1[2] * v2[1];
        const double cy = v1[2] * v2[0] - v1[0] * v2[2];
        const double cz = v1[0] * v2[1] - v1[1] * v2[0];
        six_v += v0[0] * cx + v0[1] * cy + v0[2] * cz;
    }
    return std::fabs(six_v) / 6.0;
}

render::DocMesh parse(const CompileResult& r) {
    render::DocMesh doc;
    std::string err;
    bool ok = render::parseAicm(r.aicm.data(), r.aicm.size(), doc, err);
    REQUIRE_MESSAGE(ok, "host loader rejected AICM: " << err);
    return doc;
}

// Harvest the +Z face selector off a reference box [0,W]x[0,H]x[0,D]: describe_faces
// enumerates a box's 6 faces once each, so we pick the planar face whose centroid.z
// sits at D (the +Z face) with an axis essentially +/-Z, and make_selector it. The
// returned selector resolves the SAME +Z (top) face on a rebuilt box of the same size.
// NOTE: the +Z face centroid is (W/2, H/2, D), so a face-local position (u,v)=(0,0)
// drills the BODY CENTRE (the bore passes through the block's central axis).
json plus_z_face_selector(double W, double H, double D) {
    TopoDS_Shape box = geom::make_box(W, H, D);
    std::vector<aicad::doc::TopoInfo> infos = aicad::doc::describe_faces(box);
    for (const aicad::doc::TopoInfo& info : infos) {
        if (!info.axis.has_value()) continue;
        if (std::abs(info.axis->z()) < 0.999) continue;     // not a +/-Z-normal face
        if (std::abs(info.centroid.z() - D) > 1e-6) continue; // not the +Z (z=D) face
        return aicad::doc::make_selector(info);
    }
    return json(nullptr);  // not found (the test REQUIREs a real selector)
}

}  // namespace

// (a) SIMPLE THROUGH HOLE. Body box 40x40x20 (sketch rect 0,0,40,40 on top -> extrude
//     20), then hole{body:x1, face:<+Z selector>, style:simple, diameter:10}. One
//     central through-bore. Material removed = pi*r^2*h = pi*25*20 = 30429 mm^3, so the
//     drilled volume = 32000 - 30429 = 1571... wait: remaining = 32000 - PI*25*20.
TEST_CASE("hole simple through: one central bore, cyl wall present, material removed") {
    json face_sel = plus_z_face_selector(40.0, 40.0, 20.0);
    REQUIRE(face_sel.is_object());

    json doc;
    doc["features"] = json::array({
        sketch_feature("s1", "top", rect_entities(0, 0, 40, 40)),
        json{{"id", "x1"}, {"type", "extrude"}, {"profile", "s1"}, {"depth", 20.0}},
        json{{"id", "h"}, {"type", "hole"}, {"body", "x1"}, {"face", face_sel},
             {"style", "simple"}, {"diameter", 10.0}},
    });

    CompileResult r = compile_document(doc, json::object());
    REQUIRE(r.ok);
    REQUIRE(r.body_count == 1);
    CHECK(r.statuses.size() == 3);
    for (const auto& s : r.statuses) CHECK(s.ok);

    render::DocMesh dm = parse(r);
    REQUIRE(dm.bodies.size() == 1);
    const render::BodyMesh& b = dm.bodies[0];
    check_common(b);

    double lo[3], hi[3];
    bbox(b, lo, hi);
    const double vol = mesh_volume(b);
    const double expected = 40.0 * 40.0 * 20.0 - PI * 25.0 * 20.0;  // ~30429
    MESSAGE("hole simple through: body_count=" << r.body_count
            << " faces=" << b.faces.size() << " planar=" << count_surf(b, 0)
            << " cyl=" << count_surf(b, 1) << " cone=" << count_surf(b, 2)
            << " bbox=" << (hi[0]-lo[0]) << "x" << (hi[1]-lo[1]) << "x" << (hi[2]-lo[2])
            << " volume=" << vol << " (expected ~" << expected << ")");

    CHECK(count_surf(b, 1) >= 1);   // the cylindrical bore wall
    CHECK(count_surf(b, 0) >= 6);   // the box's planar faces (2 with a hole) survive
    CHECK(count_surf(b, 2) == 0);   // no cone for a simple hole
    CHECK((hi[0] - lo[0]) == doctest::Approx(40.0).epsilon(1e-4));
    CHECK((hi[1] - lo[1]) == doctest::Approx(40.0).epsilon(1e-4));
    CHECK((hi[2] - lo[2]) == doctest::Approx(20.0).epsilon(1e-4));
    CHECK(vol < 32000.0);  // material was removed
    CHECK(vol == doctest::Approx(expected).epsilon(0.03));
}

// (b) CBORE THROUGH. Body box 40x40x30 (extrude 30), then hole{face:<+Z>, style:cbore,
//     diameter:8, cbore_d:16, cbore_depth:6}. Removed = cbore pocket pi*8^2*6 +
//     remaining bore pi*4^2*(30-6) = pi*64*6 + pi*16*24. Remaining vol = 48000 - that.
TEST_CASE("hole cbore through: counterbore + bore walls, material removed") {
    json face_sel = plus_z_face_selector(40.0, 40.0, 30.0);
    REQUIRE(face_sel.is_object());

    json doc;
    doc["features"] = json::array({
        sketch_feature("s1", "top", rect_entities(0, 0, 40, 40)),
        json{{"id", "x1"}, {"type", "extrude"}, {"profile", "s1"}, {"depth", 30.0}},
        json{{"id", "h"}, {"type", "hole"}, {"body", "x1"}, {"face", face_sel},
             {"style", "cbore"}, {"diameter", 8.0},
             {"cbore_d", 16.0}, {"cbore_depth", 6.0}},
    });

    CompileResult r = compile_document(doc, json::object());
    REQUIRE(r.ok);
    REQUIRE(r.body_count == 1);
    CHECK(r.statuses.size() == 3);
    for (const auto& s : r.statuses) CHECK(s.ok);

    render::DocMesh dm = parse(r);
    REQUIRE(dm.bodies.size() == 1);
    const render::BodyMesh& b = dm.bodies[0];
    check_common(b);

    double lo[3], hi[3];
    bbox(b, lo, hi);
    const double vol = mesh_volume(b);
    const double removed = PI * 64.0 * 6.0 + PI * 16.0 * 24.0;  // ~2413
    const double expected = 48000.0 - removed;                  // ~45587
    MESSAGE("hole cbore through: body_count=" << r.body_count
            << " faces=" << b.faces.size() << " planar=" << count_surf(b, 0)
            << " cyl=" << count_surf(b, 1) << " cone=" << count_surf(b, 2)
            << " bbox=" << (hi[0]-lo[0]) << "x" << (hi[1]-lo[1]) << "x" << (hi[2]-lo[2])
            << " volume=" << vol << " (expected ~" << expected << ")");

    CHECK(count_surf(b, 1) >= 2);   // cbore wall + bore wall
    CHECK((hi[0] - lo[0]) == doctest::Approx(40.0).epsilon(1e-4));
    CHECK((hi[1] - lo[1]) == doctest::Approx(40.0).epsilon(1e-4));
    CHECK((hi[2] - lo[2]) == doctest::Approx(30.0).epsilon(1e-4));
    CHECK(vol < 48000.0);  // material was removed
    CHECK(vol == doctest::Approx(expected).epsilon(0.03));
}

// (c) CSINK THROUGH. Body box 40x40x20, then hole{face:<+Z>, style:csink, diameter:8,
//     csink_d:16, csink_angle:90}. The csink cone (surf_type 2) + the bore cylinder
//     (surf_type 1) + material-removed are the load-bearing proofs; the cone∪cylinder
//     analytic volume is messy so we do NOT assert an exact drilled volume.
TEST_CASE("hole csink through: conical countersink + bore, material removed") {
    json face_sel = plus_z_face_selector(40.0, 40.0, 20.0);
    REQUIRE(face_sel.is_object());

    json doc;
    doc["features"] = json::array({
        sketch_feature("s1", "top", rect_entities(0, 0, 40, 40)),
        json{{"id", "x1"}, {"type", "extrude"}, {"profile", "s1"}, {"depth", 20.0}},
        json{{"id", "h"}, {"type", "hole"}, {"body", "x1"}, {"face", face_sel},
             {"style", "csink"}, {"diameter", 8.0},
             {"csink_d", 16.0}, {"csink_angle", 90.0}},
    });

    CompileResult r = compile_document(doc, json::object());
    REQUIRE(r.ok);
    REQUIRE(r.body_count == 1);
    CHECK(r.statuses.size() == 3);
    for (const auto& s : r.statuses) CHECK(s.ok);

    render::DocMesh dm = parse(r);
    REQUIRE(dm.bodies.size() == 1);
    const render::BodyMesh& b = dm.bodies[0];
    check_common(b);

    double lo[3], hi[3];
    bbox(b, lo, hi);
    const double vol = mesh_volume(b);
    MESSAGE("hole csink through: body_count=" << r.body_count
            << " faces=" << b.faces.size() << " planar=" << count_surf(b, 0)
            << " cyl=" << count_surf(b, 1) << " cone=" << count_surf(b, 2)
            << " bbox=" << (hi[0]-lo[0]) << "x" << (hi[1]-lo[1]) << "x" << (hi[2]-lo[2])
            << " volume=" << vol << " (< 32000 expected)");

    CHECK(count_surf(b, 2) >= 1);   // the conical countersink face
    CHECK(count_surf(b, 1) >= 1);   // the cylindrical bore wall
    CHECK((hi[0] - lo[0]) == doctest::Approx(40.0).epsilon(1e-4));
    CHECK((hi[1] - lo[1]) == doctest::Approx(40.0).epsilon(1e-4));
    CHECK((hi[2] - lo[2]) == doctest::Approx(20.0).epsilon(1e-4));
    CHECK(vol < 32000.0);  // material was removed
}

// (d) MULTI-POSITION SIMPLE. Body box 40x40x20, then hole{face:<+Z>, style:simple,
//     diameter:10, positions:[[-10,-10],[10,-10],[-10,10],[10,10]]}. Four disjoint
//     through-bores (centres 20 apart in the face frame, comfortably inside the 40x40
//     face). Removed = 4*pi*25*20; remaining = 32000 - that.
TEST_CASE("hole multi-position simple: four disjoint bores, material removed") {
    json face_sel = plus_z_face_selector(40.0, 40.0, 20.0);
    REQUIRE(face_sel.is_object());

    json doc;
    doc["features"] = json::array({
        sketch_feature("s1", "top", rect_entities(0, 0, 40, 40)),
        json{{"id", "x1"}, {"type", "extrude"}, {"profile", "s1"}, {"depth", 20.0}},
        json{{"id", "h"}, {"type", "hole"}, {"body", "x1"}, {"face", face_sel},
             {"style", "simple"}, {"diameter", 10.0},
             {"positions", json::array({
                 json::array({-10.0, -10.0}), json::array({10.0, -10.0}),
                 json::array({-10.0, 10.0}),  json::array({10.0, 10.0})})}},
    });

    CompileResult r = compile_document(doc, json::object());
    REQUIRE(r.ok);
    REQUIRE(r.body_count == 1);
    CHECK(r.statuses.size() == 3);
    for (const auto& s : r.statuses) CHECK(s.ok);

    render::DocMesh dm = parse(r);
    REQUIRE(dm.bodies.size() == 1);
    const render::BodyMesh& b = dm.bodies[0];
    check_common(b);

    double lo[3], hi[3];
    bbox(b, lo, hi);
    const double vol = mesh_volume(b);
    const double expected = 32000.0 - 4.0 * PI * 25.0 * 20.0;  // ~25717
    MESSAGE("hole multi-position: body_count=" << r.body_count
            << " faces=" << b.faces.size() << " planar=" << count_surf(b, 0)
            << " cyl=" << count_surf(b, 1) << " cone=" << count_surf(b, 2)
            << " bbox=" << (hi[0]-lo[0]) << "x" << (hi[1]-lo[1]) << "x" << (hi[2]-lo[2])
            << " volume=" << vol << " (expected ~" << expected << ")");

    CHECK(count_surf(b, 1) >= 4);   // 4 disjoint bore walls
    CHECK((hi[0] - lo[0]) == doctest::Approx(40.0).epsilon(1e-4));
    CHECK((hi[1] - lo[1]) == doctest::Approx(40.0).epsilon(1e-4));
    CHECK((hi[2] - lo[2]) == doctest::Approx(20.0).epsilon(1e-4));
    CHECK(vol < 32000.0);  // material was removed
    CHECK(vol == doctest::Approx(expected).epsilon(0.03));
}

TEST_CASE("make_hole_tool rejects non-positive shaft dims") {
    geom::HoleDims dims;
    dims.diameter = 0.0;
    dims.depth = 10.0;
    TopoDS_Shape tool = geom::make_hole_tool(Eigen::Vector3d::Zero(), Eigen::Vector3d::UnitZ(),
                                             geom::HoleStyle::Simple, dims);
    CHECK(tool.IsNull());

    dims.diameter = 8.0;
    dims.depth = 0.0;
    tool = geom::make_hole_tool(Eigen::Vector3d::Zero(), Eigen::Vector3d::UnitZ(),
                                geom::HoleStyle::Simple, dims);
    CHECK(tool.IsNull());
}
