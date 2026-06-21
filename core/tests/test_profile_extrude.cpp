// Wave 2 milestone verification: solved 2D sketch -> OCCT planar profile ->
// extrude/revolve -> tessellate -> AICM -> HOST render::parseAicm round-trip.
// This is the end-to-end "solve a dimensioned sketch, get a solid of EXACTLY the
// right size" proof that connects aicad::sketch::solve_sketch to 3D geometry.
//
// It follows the EXACT round-trip pattern of test_geom_aicm.cpp: the host loader
// (host/src/render/aicm_loader) is the contract oracle. Assertions are tight and
// OCCT-version-independent (face counts + surf_type classification + watertight
// bbox + analytic volume via BRepGProp) — the assertions ARE the product, in the
// SolidWorks-parity discipline of the rewrite.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <array>
#include <cmath>
#include <cstdint>
#include <limits>
#include <string>
#include <vector>

#include <BRepGProp.hxx>
#include <GProp_GProps.hxx>
#include <TopoDS_Face.hxx>
#include <TopoDS_Shape.hxx>

#include "geom/aicm.h"
#include "geom/features.h"
#include "geom/plane.h"
#include "geom/profile.h"
#include "geom/tessellate.h"
#include "sketch/sketch.h"
#include "aicm_loader.h"  // host loader (host/src/render on the include path)

using aicad::geom::SketchPlane;
using aicad::geom::faces_from_solved_sketch;
using aicad::sketch::solve_sketch;
using json = nlohmann::json;

namespace {

// ---- sketch DSL (mirrors the helpers in test_sketch.cpp) -------------------
json line(const char* id, double x1, double y1, double x2, double y2) {
    return json{{"id", id}, {"type", "line"}, {"construction", false},
                {"p1", json::array({x1, y1})}, {"p2", json::array({x2, y2})}};
}
json circle(const char* id, double cx, double cy, double r) {
    return json{{"id", id}, {"type", "circle"}, {"construction", false},
                {"center", json::array({cx, cy})}, {"radius", r}};
}
json spline(const char* id, const std::vector<std::array<double, 2>>& pts) {
    json p = json::array();
    for (const auto& q : pts) p.push_back(json::array({q[0], q[1]}));
    return json{{"id", id}, {"type", "spline"}, {"construction", false}, {"points", p}};
}
json rel(const char* id, const char* type, std::vector<const char*> refs) {
    json r = json::array();
    for (auto s : refs) r.push_back(s);
    return json{{"id", id}, {"type", type}, {"refs", r}};
}
json dim(const char* id, const char* type, std::vector<const char*> refs, double v) {
    json r = json::array();
    for (auto s : refs) r.push_back(s);
    json val = json{{"value", v}, {"expr", json(nullptr)}};
    return json{{"id", id}, {"type", type}, {"refs", r}, {"value", val}, {"driven", false}};
}

// A fully-constrained rectangle of 4 lines, min corner at (x0,y0), width w, height
// h: coincident corner chain + h/v + corner fixed + two distance dims. Returns the
// sketch dict. (Mirrors the bracket() fixture in test_sketch.cpp.)
json rect_sketch(double x0, double y0, double w, double h) {
    json s;
    s["entities"] = json::array({
        line("e1", x0, y0, x0 + w, y0),
        line("e2", x0 + w, y0, x0 + w, y0 + h),
        line("e3", x0 + w, y0 + h, x0, y0 + h),
        line("e4", x0, y0 + h, x0, y0),
    });
    s["constraints"] = json::array({
        rel("c1", "coincident", {"e1.p2", "e2.p1"}),
        rel("c2", "coincident", {"e2.p2", "e3.p1"}),
        rel("c3", "coincident", {"e3.p2", "e4.p1"}),
        rel("c4", "coincident", {"e4.p2", "e1.p1"}),
        rel("c5", "horizontal", {"e1"}),
        rel("c6", "horizontal", {"e3"}),
        rel("c7", "vertical", {"e2"}),
        rel("c8", "vertical", {"e4"}),
        rel("c9", "fix", {"e1.p1"}),
        dim("c10", "distance", {"e1.p1", "e1.p2"}, w),
        dim("c11", "distance", {"e2.p1", "e2.p2"}, h),
    });
    return s;
}

// ---- round-trip helper (identical contract to test_geom_aicm.cpp) ----------
struct Loaded {
    render::DocMesh doc;
    std::vector<uint8_t> bytes;
};

Loaded roundtrip(const TopoDS_Shape& shape, const std::string& name,
                 const float color[3]) {
    aicm::AicmBody body = geom::tessellate_body(shape, 1, name, color);
    std::vector<aicm::AicmBody> bodies{body};
    std::vector<uint8_t> bytes = aicm::write_aicm(bodies);

    render::DocMesh doc;
    std::string err;
    bool ok = render::parseAicm(bytes.data(), bytes.size(), doc, err);
    REQUIRE_MESSAGE(ok, "host loader rejected AICM: " << err);
    return {std::move(doc), std::move(bytes)};
}

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
    const uint32_t total_pts = static_cast<uint32_t>(b.edge_pts.size() / 3);
    for (const render::EdgeSegment& e : b.edges) {
        CHECK(e.first_pt + e.pt_count <= total_pts);
    }
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

double solid_volume(const TopoDS_Shape& s) {
    GProp_GProps p;
    BRepGProp::VolumeProperties(s, p);
    return p.Mass();
}

int count_surf(const render::BodyMesh& b, uint8_t surf_type) {
    int n = 0;
    for (const render::FaceRange& f : b.faces)
        if (f.surf_type == surf_type) ++n;
    return n;
}

constexpr double PI = 3.14159265358979323846;

}  // namespace

// 1. Dimensioned rectangle -> box. Solve a fully-constrained 30x20 rectangle on
//    the TOP plane, extrude +Z by 25. End to end this must yield a box of EXACTLY
//    30 x 20 x 25: 6 planar faces, 12 edges, 8 B-rep vertices.
TEST_CASE("dimensioned rectangle solves + extrudes to an exact box") {
    const double W = 30.0, H = 20.0, D = 25.0;
    auto sol = solve_sketch(rect_sketch(0, 0, W, H), json::object());
    REQUIRE(sol.converged);

    auto faces = faces_from_solved_sketch(sol.entities, SketchPlane::top());
    REQUIRE(faces.size() == 1);

    TopoDS_Shape solid =
        geom::extrude_face(faces[0], Eigen::Vector3d(0, 0, 1), D, /*mid_plane=*/false);

    const float color[3] = {0.71f, 0.76f, 0.82f};
    Loaded ld = roundtrip(solid, "Rect", color);
    REQUIRE(ld.doc.bodies.size() == 1);
    const render::BodyMesh& b = ld.doc.bodies[0];
    check_common(b);

    double lo[3], hi[3];
    bbox(b, lo, hi);
    MESSAGE("rect->box: faces=" << b.faces.size() << " edges=" << b.edges.size()
            << " brep_verts=" << b.brep_verts.size()
            << " planar=" << count_surf(b, 0)
            << " bbox=" << (hi[0]-lo[0]) << "x" << (hi[1]-lo[1]) << "x" << (hi[2]-lo[2])
            << " volume=" << solid_volume(solid));

    CHECK(b.faces.size() == 6);
    for (const render::FaceRange& f : b.faces) CHECK(f.surf_type == 0);  // plane
    CHECK(b.edges.size() == 12);
    CHECK(b.brep_verts.size() == 8);
    CHECK((hi[0] - lo[0]) == doctest::Approx(W).epsilon(1e-4));
    CHECK((hi[1] - lo[1]) == doctest::Approx(H).epsilon(1e-4));
    CHECK((hi[2] - lo[2]) == doctest::Approx(D).epsilon(1e-4));
    CHECK(solid_volume(solid) == doctest::Approx(W * H * D).epsilon(1e-4));
}

// 2. Dimensioned circle -> cylinder. One circle (center fixed at origin, radius
//    dim 10) extruded +Z by 8. Must have >=1 cylinder face + 2 planar caps and a
//    20 x 20 x 8 bbox.
TEST_CASE("dimensioned circle solves + extrudes to a cylinder") {
    const double R = 10.0, D = 8.0;
    json s;
    s["entities"] = json::array({circle("e1", 0, 0, R)});
    s["constraints"] = json::array({
        rel("c1", "fix", {"e1.center"}),
        dim("c2", "radius", {"e1"}, R),
    });
    auto sol = solve_sketch(s, json::object());
    REQUIRE(sol.converged);

    auto faces = faces_from_solved_sketch(sol.entities, SketchPlane::top());
    REQUIRE(faces.size() == 1);

    TopoDS_Shape solid =
        geom::extrude_face(faces[0], Eigen::Vector3d(0, 0, 1), D, false);

    const float color[3] = {0.4f, 0.5f, 0.6f};
    Loaded ld = roundtrip(solid, "Cyl", color);
    REQUIRE(ld.doc.bodies.size() == 1);
    const render::BodyMesh& b = ld.doc.bodies[0];
    check_common(b);

    double lo[3], hi[3];
    bbox(b, lo, hi);
    MESSAGE("circle->cyl: faces=" << b.faces.size()
            << " cylinder=" << count_surf(b, 1) << " planar=" << count_surf(b, 0)
            << " bbox=" << (hi[0]-lo[0]) << "x" << (hi[1]-lo[1]) << "x" << (hi[2]-lo[2])
            << " volume=" << solid_volume(solid));

    CHECK(count_surf(b, 1) >= 1);  // at least one cylindrical side
    CHECK(count_surf(b, 0) == 2);  // two planar caps
    CHECK((hi[0] - lo[0]) == doctest::Approx(2 * R).epsilon(1e-2));
    CHECK((hi[1] - lo[1]) == doctest::Approx(2 * R).epsilon(1e-2));
    CHECK((hi[2] - lo[2]) == doctest::Approx(D).epsilon(1e-4));
    CHECK(solid_volume(solid) == doctest::Approx(PI * R * R * D).epsilon(1e-3));
}

// 3. Circle inside square -> prism WITH a hole (the key multi-loop parity case).
//    Outer 40x40 square (min corner at -20,-20 so it is centered at origin) +
//    inner circle (center origin, radius 8). faces_from_solved_sketch must return
//    ONE face (square minus circular hole). Extrude +Z by 10; the hole must
//    physically exist: volume = 40*40*10 - pi*8^2*10.
TEST_CASE("circle inside square -> single washer face -> prism with a hole") {
    const double SIDE = 40.0, R = 8.0, D = 10.0;
    json sq = rect_sketch(-SIDE / 2, -SIDE / 2, SIDE, SIDE);
    // Append a dimensioned inner circle to the square's sketch.
    sq["entities"].push_back(circle("ic", 0, 0, R));
    sq["constraints"].push_back(rel("c20", "fix", {"ic.center"}));
    sq["constraints"].push_back(dim("c21", "radius", {"ic"}, R));

    auto sol = solve_sketch(sq, json::object());
    REQUIRE(sol.converged);

    auto faces = faces_from_solved_sketch(sol.entities, SketchPlane::top());
    REQUIRE(faces.size() == 1);  // ONE region = square with a circular hole

    TopoDS_Shape solid =
        geom::extrude_face(faces[0], Eigen::Vector3d(0, 0, 1), D, false);

    const float color[3] = {0.6f, 0.6f, 0.6f};
    Loaded ld = roundtrip(solid, "Washer", color);
    REQUIRE(ld.doc.bodies.size() == 1);
    const render::BodyMesh& b = ld.doc.bodies[0];
    check_common(b);

    double expected_vol = SIDE * SIDE * D - PI * R * R * D;
    double vol = solid_volume(solid);
    MESSAGE("washer: faces=" << b.faces.size()
            << " cylinder=" << count_surf(b, 1) << " planar=" << count_surf(b, 0)
            << " volume=" << vol << " (expected " << expected_vol << ")");

    CHECK(count_surf(b, 1) >= 1);  // the cylindrical hole wall
    CHECK(count_surf(b, 0) >= 4);  // 4 square sides (caps are also planar)
    CHECK(vol == doctest::Approx(expected_vol).epsilon(1e-3));
}

// 4. Revolve. On the FRONT plane (u=+X, v=+Z, normal=-Y) a rectangle with u in
//    [10,20] and v in [0,30] -> a profile at radius 10..20, height 0..30 lying in
//    the XZ plane. Revolved 360 deg about the world Z axis it sweeps a hollow
//    cylinder (annular tube): volume = pi*(20^2 - 10^2)*30. We choose the FRONT
//    plane + world-Z axis because the profile is then exactly axisymmetric about
//    Z, giving an analytically exact annulus volume.
TEST_CASE("rectangle revolved 360 about Z -> annular tube of exact volume") {
    const double R0 = 10.0, R1 = 20.0, HGT = 30.0;
    // Rectangle in sketch (u,v): u in [R0,R1], v in [0,HGT].
    auto sol = solve_sketch(rect_sketch(R0, 0.0, R1 - R0, HGT), json::object());
    REQUIRE(sol.converged);

    auto faces = faces_from_solved_sketch(sol.entities, SketchPlane::front());
    REQUIRE(faces.size() == 1);

    TopoDS_Shape solid = geom::revolve_face(
        faces[0], Eigen::Vector3d(0, 0, 0), Eigen::Vector3d(0, 0, 1), 2.0 * PI);

    const float color[3] = {0.5f, 0.55f, 0.6f};
    Loaded ld = roundtrip(solid, "Tube", color);
    REQUIRE(ld.doc.bodies.size() == 1);
    const render::BodyMesh& b = ld.doc.bodies[0];
    check_common(b);

    double expected_vol = PI * (R1 * R1 - R0 * R0) * HGT;
    double vol = solid_volume(solid);
    double lo[3], hi[3];
    bbox(b, lo, hi);
    MESSAGE("tube: faces=" << b.faces.size()
            << " cylinder=" << count_surf(b, 1) << " planar=" << count_surf(b, 0)
            << " bbox=" << (hi[0]-lo[0]) << "x" << (hi[1]-lo[1]) << "x" << (hi[2]-lo[2])
            << " volume=" << vol << " (expected " << expected_vol << ")");

    CHECK(count_surf(b, 1) >= 2);  // inner + outer cylindrical walls
    CHECK(vol == doctest::Approx(expected_vol).epsilon(1e-3));
    // Bbox: outer diameter 2*R1 in X and Y, height HGT in Z.
    CHECK((hi[2] - lo[2]) == doctest::Approx(HGT).epsilon(1e-4));
    CHECK((hi[0] - lo[0]) == doctest::Approx(2 * R1).epsilon(1e-2));
    CHECK((hi[1] - lo[1]) == doctest::Approx(2 * R1).epsilon(1e-2));
}

// 5. Sphere by revolution: a FULL circle centered ON the axis (the axis passes
//    through the profile's center) revolved 360 deg. This is the regression case
//    for the axis-straddle clip — a naive MakeRevol sweeps each half-disk through
//    the other and yields a self-intersecting, degenerate solid. revolve_face must
//    clip the profile to one half-plane first so the result is a CLEAN sphere of
//    exact volume 4/3 pi r^3 with one spherical face. Circle on the FRONT plane
//    (XZ) centered at origin, axis = world Z (lies in-plane, through the center).
TEST_CASE("full circle on the axis revolved 360 -> clean sphere of exact volume") {
    const double R = 25.4;
    json s;
    s["entities"] = json::array({circle("e1", 0, 0, R)});
    s["constraints"] = json::array({
        rel("c1", "fix", {"e1.center"}),
        dim("c2", "radius", {"e1"}, R),
    });
    auto sol = solve_sketch(s, json::object());
    REQUIRE(sol.converged);

    auto faces = faces_from_solved_sketch(sol.entities, SketchPlane::front());
    REQUIRE(faces.size() == 1);

    TopoDS_Shape solid = geom::revolve_face(
        faces[0], Eigen::Vector3d(0, 0, 0), Eigen::Vector3d(0, 0, 1), 2.0 * PI);

    const float color[3] = {0.6f, 0.62f, 0.66f};
    Loaded ld = roundtrip(solid, "Sphere", color);
    REQUIRE(ld.doc.bodies.size() == 1);
    const render::BodyMesh& b = ld.doc.bodies[0];
    check_common(b);

    double expected_vol = 4.0 / 3.0 * PI * R * R * R;
    double vol = solid_volume(solid);
    double lo[3], hi[3];
    bbox(b, lo, hi);
    MESSAGE("sphere: faces=" << b.faces.size()
            << " sphere=" << count_surf(b, 4) << " planar=" << count_surf(b, 0)
            << " bbox=" << (hi[0]-lo[0]) << "x" << (hi[1]-lo[1]) << "x" << (hi[2]-lo[2])
            << " volume=" << vol << " (expected " << expected_vol << ")");

    // Exact sphere volume (proves no double-coverage / self-intersection) and a
    // 2R cube bounding box on every axis.
    CHECK(vol == doctest::Approx(expected_vol).epsilon(1e-3));
    CHECK((hi[0] - lo[0]) == doctest::Approx(2 * R).epsilon(1e-2));
    CHECK((hi[1] - lo[1]) == doctest::Approx(2 * R).epsilon(1e-2));
    CHECK((hi[2] - lo[2]) == doctest::Approx(2 * R).epsilon(1e-2));
}

// 6. Self-closing through-point spline -> extrudable face. A spline whose first and last
//    points coincide is a single closed loop (like a circle): build_edge must interpolate it
//    PERIODICALLY and detect_loops must treat it as a one-edge loop using the spline's own
//    sample ring for the area polygon. Sampling a circle of radius R at 24 stations and closing
//    it gives a near-circular profile; extruded +Z by D the volume is ~pi R^2 D (the cubic
//    interpolation bulges very slightly outward between samples, so allow 3%). This is the
//    regression for "closed splines / eq-curves were silently non-extrudable in the C++ core".
TEST_CASE("self-closing through-point spline solves + extrudes to a closed prism") {
    const double R = 12.0, D = 5.0;
    std::vector<std::array<double, 2>> pts;
    const int N = 24;
    for (int i = 0; i <= N; ++i) {          // i==N repeats i==0 -> closes the ring
        double th = 2.0 * PI * i / N;
        pts.push_back({R * std::cos(th), R * std::sin(th)});
    }
    json s;
    s["entities"] = json::array({spline("e1", pts)});
    s["constraints"] = json::array();
    auto sol = solve_sketch(s, json::object());
    REQUIRE(sol.converged);

    auto faces = faces_from_solved_sketch(sol.entities, SketchPlane::top());
    REQUIRE(faces.size() == 1);

    TopoDS_Shape solid = geom::extrude_face(faces[0], Eigen::Vector3d(0, 0, 1), D, false);
    double vol = solid_volume(solid);
    MESSAGE("closed spline: faces=" << faces.size() << " volume=" << vol
            << " (~" << PI * R * R * D << ")");
    CHECK(vol == doctest::Approx(PI * R * R * D).epsilon(0.03));
}

// 7. Airfoil topology: two open through-point splines that SHARE their leading- and trailing-
//    edge endpoints chain into ONE closed loop (exactly how SketchMode::addAirfoil emits a
//    NACA section with a sharp/closed trailing edge). faces_from_solved_sketch must chain the
//    two curved edges by endpoint proximity, splice each spline's interior samples into the
//    area polygon, and return a single positive-area face that extrudes to a finite prism.
TEST_CASE("two endpoint-sharing splines -> closed airfoil-style loop -> prism") {
    const double c = 30.0, h = 4.0, D = 6.0;
    std::vector<std::array<double, 2>> up = {
        {0, 0}, {c * 0.25, h * 0.9}, {c * 0.5, h}, {c * 0.75, h * 0.6}, {c, 0}};
    std::vector<std::array<double, 2>> lo = {
        {0, 0}, {c * 0.25, -h * 0.9}, {c * 0.5, -h}, {c * 0.75, -h * 0.6}, {c, 0}};
    json s;
    s["entities"] = json::array({spline("eu", up), spline("el", lo)});
    s["constraints"] = json::array();
    auto sol = solve_sketch(s, json::object());
    REQUIRE(sol.converged);

    auto faces = faces_from_solved_sketch(sol.entities, SketchPlane::top());
    REQUIRE(faces.size() == 1);

    TopoDS_Shape solid = geom::extrude_face(faces[0], Eigen::Vector3d(0, 0, 1), D, false);
    double vol = solid_volume(solid);
    const double bboxVol = c * (2 * h) * D;   // loose envelope: profile fits inside c x 2h
    MESSAGE("airfoil loop: faces=" << faces.size() << " volume=" << vol
            << " (envelope " << bboxVol << ")");
    CHECK(vol > 0.2 * bboxVol);   // a real lens, not a sliver
    CHECK(vol < bboxVol);         // strictly inside its bounding box
}
