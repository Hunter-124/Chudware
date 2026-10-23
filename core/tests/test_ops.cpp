// Wave 2 milestone verification: the modeling OPERATIONS that turn single profiles
// into real multi-feature parts — boolean fuse/cut/common (geom/booleans.h) and
// dress-up fillet/chamfer (geom/dress.h) — composed and round-tripped through the
// HOST loader (host/src/render/aicm_loader = the contract oracle).
//
// It follows the EXACT round-trip pattern of test_profile_extrude.cpp: build a base
// shape with geom::make_box / geom::extrude_face, apply an op, tessellate -> AICM ->
// render::parseAicm, then assert TIGHT, ANALYTIC, OCCT-version-INDEPENDENT invariants:
// the exact solid volume (BRepGProp) the op must produce, plus the surf_type face
// stats that prove the new geometry (hole walls, fillet cylinders, chamfer planes)
// physically exists. The assertions ARE the product, in the SolidWorks-parity
// discipline of the rewrite — they are NOT weakened to pass.
//
// Case 6 is the payoff: extrude a base block, CUT a through-hole, then FILLET the
// outer vertical edges — three ops composing into one part with both the hole's
// cylinder face and the fillet cylinder faces present in the final tessellation.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <atomic>
#include <chrono>
#include <cmath>
#include <thread>
#include <cstdint>
#include <limits>
#include <string>
#include <vector>

#if defined(__unix__) || defined(__APPLE__)
#include <cerrno>
#include <csignal>
#include <new>
#include <sys/mman.h>
#include <sys/wait.h>
#include <unistd.h>
#endif

#include <BRepBuilderAPI_Transform.hxx>
#include <BRepGProp.hxx>
#include <BRepPrimAPI_MakeCylinder.hxx>
#include <GProp_GProps.hxx>
#include <TopoDS_Face.hxx>
#include <TopoDS_Shape.hxx>
#include <gp_Ax2.hxx>
#include <gp_Dir.hxx>
#include <gp_Pnt.hxx>
#include <gp_Trsf.hxx>
#include <gp_Vec.hxx>

#include "geom/aicm.h"
#include "geom/booleans.h"
#include "geom/dress.h"
#include "geom/features.h"
#include "geom/tessellate.h"
#include "util/process_isolation.h"
#include "aicm_loader.h"  // host loader (host/src/render on the include path)

namespace {

// ---- round-trip helper (identical contract to test_profile_extrude.cpp) -----
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

// Translate a shape by (dx,dy,dz) — used to position the second box / the cut cylinder.
TopoDS_Shape translated(const TopoDS_Shape& s, double dx, double dy, double dz) {
    gp_Trsf t;
    t.SetTranslation(gp_Vec(dx, dy, dz));
    return BRepBuilderAPI_Transform(s, t, true).Shape();
}

// A cylinder of radius `r`, length `h`, with its base centre at `base` and its axis
// along +Z (BRepPrimAPI_MakeCylinder on a gp_Ax2). Used as the cut tool for through
// holes — precise axis control is easier here than lifting a solved circle, and the
// spec explicitly allows BRepPrimAPI_MakeCylinder for the boolean cases.
TopoDS_Shape cylinder_z(const gp_Pnt& base, double r, double h) {
    gp_Ax2 ax(base, gp_Dir(0.0, 0.0, 1.0));
    return BRepPrimAPI_MakeCylinder(ax, r, h).Shape();
}

constexpr double PI = 3.14159265358979323846;

}  // namespace

// 1. FUSE two overlapping boxes. Box A: 20x20x20 at the origin. Box B: 20x20x20
//    translated by (+10,+10,+10) -> the two overlap in a 10x10x10 cube. The union
//    volume must be V_A + V_B - V_overlap = 8000 + 8000 - 1000 = 15000, and the
//    result must be a SINGLE solid (one shell -> a connected body).
TEST_CASE("fuse: two overlapping boxes -> union volume, single solid") {
    const double S = 20.0, OFF = 10.0;
    TopoDS_Shape a = geom::make_box(S, S, S);
    TopoDS_Shape b = translated(geom::make_box(S, S, S), OFF, OFF, OFF);

    TopoDS_Shape u = geom::fuse(a, b);
    REQUIRE_FALSE(u.IsNull());

    const float color[3] = {0.71f, 0.76f, 0.82f};
    Loaded ld = roundtrip(u, "Fuse", color);
    REQUIRE(ld.doc.bodies.size() == 1);
    const render::BodyMesh& bm = ld.doc.bodies[0];
    check_common(bm);

    const double overlap = OFF * OFF * OFF;                    // 10x10x10 = 1000
    const double expected = S * S * S + S * S * S - overlap;   // 15000
    const double vol = solid_volume(u);
    double lo[3], hi[3];
    bbox(bm, lo, hi);
    MESSAGE("fuse: faces=" << bm.faces.size() << " planar=" << count_surf(bm, 0)
            << " volume=" << vol << " (expected " << expected << ")"
            << " bbox=" << (hi[0]-lo[0]) << "x" << (hi[1]-lo[1]) << "x" << (hi[2]-lo[2]));

    CHECK(vol == doctest::Approx(expected).epsilon(1e-3));
    // The union of two overlapping boxes is one connected solid: an L-shaped step
    // block, all-planar, with the staircase union bbox spanning 0..(S+OFF) on each axis.
    for (const render::FaceRange& f : bm.faces) CHECK(f.surf_type == 0);  // all planar
    CHECK((hi[0] - lo[0]) == doctest::Approx(S + OFF).epsilon(1e-4));
    CHECK((hi[1] - lo[1]) == doctest::Approx(S + OFF).epsilon(1e-4));
    CHECK((hi[2] - lo[2]) == doctest::Approx(S + OFF).epsilon(1e-4));
}

// 2. CUT a box minus a fully-penetrating cylinder -> a drilled block. Box 30x30x20 at
//    the origin; cylinder r=6, axis +Z through the centre, length 20 (flush through the
//    top & bottom faces). Volume = box - pi r^2 h = 30*30*20 - pi*36*20. The hole wall
//    must appear as a cylindrical face (surf_type==1) in the tessellation.
TEST_CASE("cut: box minus a through-cylinder -> drilled hole, cyl wall present") {
    const double W = 30.0, H = 30.0, D = 20.0, R = 6.0;
    TopoDS_Shape box = geom::make_box(W, H, D);
    // Base at the box's centre on the bottom face; full depth D so it exits the top.
    TopoDS_Shape drill = cylinder_z(gp_Pnt(W / 2, H / 2, 0.0), R, D);

    TopoDS_Shape part = geom::cut(box, drill);
    REQUIRE_FALSE(part.IsNull());

    const float color[3] = {0.6f, 0.6f, 0.6f};
    Loaded ld = roundtrip(part, "Cut", color);
    REQUIRE(ld.doc.bodies.size() == 1);
    const render::BodyMesh& bm = ld.doc.bodies[0];
    check_common(bm);

    const double expected = W * H * D - PI * R * R * D;
    const double vol = solid_volume(part);
    MESSAGE("cut: faces=" << bm.faces.size() << " cylinder=" << count_surf(bm, 1)
            << " planar=" << count_surf(bm, 0)
            << " volume=" << vol << " (expected " << expected << ")");

    CHECK(vol == doctest::Approx(expected).epsilon(1e-3));
    CHECK(count_surf(bm, 1) >= 1);  // the cylindrical hole wall
    CHECK(count_surf(bm, 0) >= 6);  // the box's 6 planar faces survive (2 with a hole)
}

// 3. COMMON of two overlapping boxes -> exactly the shared volume. Same geometry as
//    case 1 (A at origin, B at +10,+10,+10), so the intersection is precisely the
//    10x10x10 overlap cube: volume = 1000, a 6-plane box spanning 10..20 on each axis.
TEST_CASE("common: two overlapping boxes -> exact overlap cube") {
    const double S = 20.0, OFF = 10.0;
    TopoDS_Shape a = geom::make_box(S, S, S);
    TopoDS_Shape b = translated(geom::make_box(S, S, S), OFF, OFF, OFF);

    TopoDS_Shape x = geom::common(a, b);
    REQUIRE_FALSE(x.IsNull());

    const float color[3] = {0.5f, 0.55f, 0.6f};
    Loaded ld = roundtrip(x, "Common", color);
    REQUIRE(ld.doc.bodies.size() == 1);
    const render::BodyMesh& bm = ld.doc.bodies[0];
    check_common(bm);

    const double expected = OFF * OFF * OFF;  // 10x10x10 = 1000
    const double vol = solid_volume(x);
    double lo[3], hi[3];
    bbox(bm, lo, hi);
    MESSAGE("common: faces=" << bm.faces.size() << " planar=" << count_surf(bm, 0)
            << " volume=" << vol << " (expected " << expected << ")"
            << " bbox=" << (hi[0]-lo[0]) << "x" << (hi[1]-lo[1]) << "x" << (hi[2]-lo[2]));

    CHECK(vol == doctest::Approx(expected).epsilon(1e-4));
    // The intersection is an axis-aligned cube: 6 planar faces, OFF on a side.
    CHECK(bm.faces.size() == 6);
    for (const render::FaceRange& f : bm.faces) CHECK(f.surf_type == 0);
    CHECK((hi[0] - lo[0]) == doctest::Approx(OFF).epsilon(1e-4));
    CHECK((hi[1] - lo[1]) == doctest::Approx(OFF).epsilon(1e-4));
    CHECK((hi[2] - lo[2]) == doctest::Approx(OFF).epsilon(1e-4));
}

// 4. FILLET the 4 vertical edges of a box (dir=+Z) by radius r. Each vertical edge
//    becomes a quarter-cylinder, replacing a square corner column (side r, area r^2)
//    with a quarter-disc (area pi r^2 / 4). The 4 corners together remove
//    (4 - pi) r^2 of cross-section over the full depth D: volume = W*H*D - (4-pi) r^2 D.
//    The round must introduce >= 4 cylindrical faces (one per filleted edge).
TEST_CASE("fillet: 4 vertical box edges -> rounded corners, cyl faces present") {
    const double W = 40.0, H = 30.0, D = 20.0, R = 5.0;  // R well under H/2, W/2
    TopoDS_Shape box = geom::make_box(W, H, D);

    TopoDS_Shape part = geom::fillet_edges(box, R, Eigen::Vector3d(0, 0, 1));
    REQUIRE_FALSE(part.IsNull());

    const float color[3] = {0.4f, 0.5f, 0.6f};
    Loaded ld = roundtrip(part, "Fillet", color);
    REQUIRE(ld.doc.bodies.size() == 1);
    const render::BodyMesh& bm = ld.doc.bodies[0];
    check_common(bm);

    const double expected = W * H * D - (4.0 - PI) * R * R * D;
    const double vol = solid_volume(part);
    double lo[3], hi[3];
    bbox(bm, lo, hi);
    MESSAGE("fillet: faces=" << bm.faces.size() << " cylinder=" << count_surf(bm, 1)
            << " planar=" << count_surf(bm, 0)
            << " volume=" << vol << " (expected " << expected << ")"
            << " bbox=" << (hi[0]-lo[0]) << "x" << (hi[1]-lo[1]) << "x" << (hi[2]-lo[2]));

    CHECK(vol == doctest::Approx(expected).epsilon(1e-3));
    CHECK(count_surf(bm, 1) >= 4);  // 4 quarter-cylinder corner faces
    // Rounding stays within the original box bbox (corners removed, extents unchanged).
    CHECK((hi[0] - lo[0]) == doctest::Approx(W).epsilon(1e-4));
    CHECK((hi[1] - lo[1]) == doctest::Approx(H).epsilon(1e-4));
    CHECK((hi[2] - lo[2]) == doctest::Approx(D).epsilon(1e-4));
}

// 5. CHAMFER the 4 vertical edges of a box (dir=+Z) by setback d. Each vertical edge
//    loses a right-triangular prism of cross-section 1/2 d^2; 4 corners remove 2 d^2
//    over the depth D: volume = W*H*D - 2 d^2 D. The bevel must introduce new PLANAR
//    faces (one per chamfered edge) so the face count exceeds the plain box's 6.
TEST_CASE("chamfer: 4 vertical box edges -> beveled corners, extra planar faces") {
    const double W = 40.0, H = 30.0, D = 20.0, DD = 5.0;
    TopoDS_Shape box = geom::make_box(W, H, D);

    TopoDS_Shape part = geom::chamfer_edges(box, DD, Eigen::Vector3d(0, 0, 1));
    REQUIRE_FALSE(part.IsNull());

    const float color[3] = {0.55f, 0.5f, 0.45f};
    Loaded ld = roundtrip(part, "Chamfer", color);
    REQUIRE(ld.doc.bodies.size() == 1);
    const render::BodyMesh& bm = ld.doc.bodies[0];
    check_common(bm);

    const double expected = W * H * D - 2.0 * DD * DD * D;
    const double vol = solid_volume(part);
    MESSAGE("chamfer: faces=" << bm.faces.size() << " planar=" << count_surf(bm, 0)
            << " cylinder=" << count_surf(bm, 1)
            << " volume=" << vol << " (expected " << expected << ")");

    CHECK(vol == doctest::Approx(expected).epsilon(1e-3));
    // A chamfer is all-planar; the 4 new bevel facets push the face count above 6.
    CHECK(bm.faces.size() > 6);
    CHECK(count_surf(bm, 0) >= 10);  // 6 box faces (4 truncated) + 4 bevel facets
    CHECK(count_surf(bm, 1) == 0);   // no curved faces from a chamfer
}

// 6. COMPOSED MULTI-FEATURE PART (the payoff). Build a base block, CUT a through-hole
//    (a bore), then FILLET the outer vertical edges. This proves the ops COMPOSE into a
//    real part: the final volume = box - pi r_hole^2 D - (4 - pi) r_fillet^2 D, and the
//    tessellation carries BOTH the hole's cylinder wall AND the 4 fillet cylinder faces
//    (>= 5 cylindrical faces total).
TEST_CASE("composed: block + through-hole + filleted outer edges -> real part") {
    const double W = 50.0, H = 40.0, D = 25.0;
    const double R_HOLE = 8.0, R_FILLET = 6.0;

    // Base block (built through the prism path, like a real extrude feature).
    TopoDS_Shape block = geom::extrude_rect(W, H, D);

    // Bore a clean through-hole on the +Z axis at the block centre.
    TopoDS_Shape drill = cylinder_z(gp_Pnt(W / 2, H / 2, 0.0), R_HOLE, D);
    TopoDS_Shape bored = geom::cut(block, drill);
    REQUIRE_FALSE(bored.IsNull());

    // Round the 4 outer vertical edges. The inner hole wall is curved, so the dir=+Z
    // line filter selects ONLY the 4 straight outer vertical edges — not the bore.
    TopoDS_Shape part = geom::fillet_edges(bored, R_FILLET, Eigen::Vector3d(0, 0, 1));
    REQUIRE_FALSE(part.IsNull());

    const float color[3] = {0.71f, 0.76f, 0.82f};
    Loaded ld = roundtrip(part, "Part", color);
    REQUIRE(ld.doc.bodies.size() == 1);
    const render::BodyMesh& bm = ld.doc.bodies[0];
    check_common(bm);

    const double expected = W * H * D
                          - PI * R_HOLE * R_HOLE * D
                          - (4.0 - PI) * R_FILLET * R_FILLET * D;
    const double vol = solid_volume(part);
    double lo[3], hi[3];
    bbox(bm, lo, hi);
    MESSAGE("composed: faces=" << bm.faces.size() << " cylinder=" << count_surf(bm, 1)
            << " planar=" << count_surf(bm, 0)
            << " volume=" << vol << " (expected " << expected << ")"
            << " bbox=" << (hi[0]-lo[0]) << "x" << (hi[1]-lo[1]) << "x" << (hi[2]-lo[2]));

    CHECK(vol == doctest::Approx(expected).epsilon(1e-3));
    // >= 5 cylindrical faces: 1 hole wall + 4 fillet quarter-cylinders.
    CHECK(count_surf(bm, 1) >= 5);
    // Outer extents are unchanged (the fillets only remove the corners, the hole is interior).
    CHECK((hi[0] - lo[0]) == doctest::Approx(W).epsilon(1e-4));
    CHECK((hi[1] - lo[1]) == doctest::Approx(H).epsilon(1e-4));
    CHECK((hi[2] - lo[2]) == doctest::Approx(D).epsilon(1e-4));
}

// Regression (3-way corner): three box edges meeting at ONE vertex, all filleted at the same
// radius, must all round (n_dropped==0) — the "corner approached from all 3 edges" case. Also
// exercises the full 12-edge round (every vertex is a 3-way corner) and chamfer parity. Pure
// geometry (explicit edges) isolates the OCCT blend from selector resolution.
#include <TopExp.hxx>
#include <TopoDS.hxx>
#include <TopoDS_Edge.hxx>
#include <TopoDS_Vertex.hxx>
#include <TopTools_IndexedMapOfShape.hxx>
#include <BRep_Tool.hxx>
TEST_CASE("fillet: 3-way corner (3 edges at one vertex) all round") {
    const double W = 40.0, H = 30.0, D = 20.0, R = 4.0;  // R well under every half-extent
    TopoDS_Shape box = geom::make_box(W, H, D);
    const gp_Pnt corner(W, H, D);

    // The 3 unique edges touching the (W,H,D) corner vertex.
    TopTools_IndexedMapOfShape emap;
    TopExp::MapShapes(box, TopAbs_EDGE, emap);
    std::vector<TopoDS_Edge> corner_edges;
    for (int i = 1; i <= emap.Extent(); ++i) {
        TopoDS_Edge e = TopoDS::Edge(emap(i));
        TopoDS_Vertex v1, v2;
        TopExp::Vertices(e, v1, v2);
        gp_Pnt p1 = BRep_Tool::Pnt(v1), p2 = BRep_Tool::Pnt(v2);
        if (p1.Distance(corner) < 1e-6 || p2.Distance(corner) < 1e-6)
            corner_edges.push_back(e);
    }
    REQUIRE(corner_edges.size() == 3);

    int dropped = -1;
    TopoDS_Shape part = geom::fillet_edges(box, R, corner_edges, &dropped);
    const double vol = part.IsNull() ? -1.0 : solid_volume(part);
    MESSAGE("3-way corner: dropped=" << dropped << " null=" << part.IsNull()
            << " volume=" << vol);
    CHECK(dropped == 0);
    REQUIRE_FALSE(part.IsNull());

    // Full 12-edge round: every one of the 8 vertices is a 3-way corner.
    std::vector<TopoDS_Edge> all_edges;
    for (int i = 1; i <= emap.Extent(); ++i) all_edges.push_back(TopoDS::Edge(emap(i)));
    REQUIRE(all_edges.size() == 12);
    int dropped12 = -1;
    TopoDS_Shape full = geom::fillet_edges(box, R, all_edges, &dropped12);
    MESSAGE("full 12-edge round: dropped=" << dropped12 << " null=" << full.IsNull()
            << " volume=" << (full.IsNull() ? -1.0 : solid_volume(full)));
    CHECK(dropped12 == 0);

    // Parity: CHAMFER must drop nothing on the same 3-way corner and full-edge sets.
    int cdrop = -1;
    TopoDS_Shape cpart = geom::chamfer_edges(box, R, corner_edges, &cdrop);
    MESSAGE("chamfer 3-way corner: dropped=" << cdrop << " null=" << cpart.IsNull());
    CHECK(cdrop == 0);
    REQUIRE_FALSE(cpart.IsNull());
    int cdrop12 = -1;
    TopoDS_Shape cfull = geom::chamfer_edges(box, R, all_edges, &cdrop12);
    MESSAGE("chamfer full 12-edge: dropped=" << cdrop12 << " null=" << cfull.IsNull());
    CHECK(cdrop12 == 0);
    // Two-distance (asymmetric) chamfer now drop-failing too.
    int cdrop2 = -1;
    TopoDS_Shape ctwo = geom::chamfer_edges_two_distance(box, R, R * 0.5, corner_edges, &cdrop2);
    MESSAGE("two-distance chamfer 3-way: dropped=" << cdrop2 << " null=" << ctwo.IsNull());
    CHECK(cdrop2 == 0);
    REQUIRE_FALSE(ctwo.IsNull());
}

TEST_CASE("fillet: isolated OCCT fault is a failed feature and process survives") {
#if defined(__unix__) || defined(__APPLE__)
    TopoDS_Shape box = geom::make_box(40.0, 30.0, 20.0);
    TopTools_IndexedMapOfShape emap;
    TopExp::MapShapes(box, TopAbs_EDGE, emap);
    std::vector<TopoDS_Edge> one{TopoDS::Edge(emap(1))};
    // The injected fault only exists inside the forked child, and isolation retires
    // itself for the whole process the first time a fork times out (a loaded CI runner
    // does hit that: this case took 10 s there against 0.9 s here). Once retired, the
    // build runs in-process, no child is forked, and the fault is never armed — so the
    // containment assertions are only meaningful while isolation is still active.
    const bool isolated = chudware::util::shape_isolation_active();
    setenv("CHUDWARE_TEST_BLEND_SIGSEGV", "1", 1);
    int dropped = -1;
    TopoDS_Shape failed = geom::fillet_edges(box, 4.0, one, &dropped);
    unsetenv("CHUDWARE_TEST_BLEND_SIGSEGV");
    if (isolated) {
        CHECK(failed.IsNull());
        CHECK(dropped == 1);
    } else {
        MESSAGE("blend isolation already retired in this process; fault not armed");
    }
    // The same process remains usable after the isolated child dies.
    TopoDS_Shape healthy = geom::fillet_edges(box, 4.0, one, &dropped);
    CHECK_FALSE(healthy.IsNull());
    CHECK(dropped == 0);

    // The whole-body ("round every edge") overload is isolated too — it reaches the same
    // OCCT corner resolver, so an unguarded build there would kill the daemon just as dead.
    const bool stillIsolated = chudware::util::shape_isolation_active();
    setenv("CHUDWARE_TEST_BLEND_SIGSEGV", "1", 1);
    TopoDS_Shape allFailed = geom::fillet_edges(box, 2.0, std::nullopt);
    unsetenv("CHUDWARE_TEST_BLEND_SIGSEGV");
    if (stillIsolated) {
        CHECK(allFailed.IsNull());
    } else {
        MESSAGE("blend isolation retired before the whole-body case; fault not armed");
    }
    TopoDS_Shape allHealthy = geom::fillet_edges(box, 2.0, std::nullopt);
    CHECK_FALSE(allHealthy.IsNull());
#endif
}

// Does fork-based blend isolation survive a MULTITHREADED process?
//
// fork() hands the child a snapshot of every lock, so if another thread held the malloc arena
// at fork time the child deadlocks on its first allocation - and OCCT allocates immediately.
// That is not theory: with isolation on, CHUDWARE_SHOT=fea went from a 90 s scenario to a hard
// hang, because the desktop GUI links libchudware in-process alongside its worker-bridge and
// RPC threads. Isolation now falls back in-process on a timeout, which is what un-hung it.
//
// This case pins the property the DAEMON depends on: with allocator-hammering threads running,
// an isolated fillet still returns the right body, and it does so fast enough to prove the
// child was not wedged for its whole budget.
TEST_CASE("fillet: isolated blend survives allocator pressure from other threads") {
#if defined(__unix__) || defined(__APPLE__)
    std::atomic<bool> stop{false};
    std::vector<std::thread> hammers;
    for (int i = 0; i < 4; ++i)
        hammers.emplace_back([&stop] {
            while (!stop.load(std::memory_order_relaxed)) {
                // Churn the allocator so a fork is likely to land while the arena lock is held.
                std::vector<std::string> junk;
                junk.reserve(64);
                for (int k = 0; k < 64; ++k) junk.emplace_back(256, 'x');
            }
        });

    TopoDS_Shape box = geom::make_box(60.0, 40.0, 10.0);
    TopTools_IndexedMapOfShape emap;
    TopExp::MapShapes(box, TopAbs_EDGE, emap);
    std::vector<TopoDS_Edge> edges;
    for (int i = 1; i <= emap.Extent(); ++i) edges.push_back(TopoDS::Edge(emap(i)));

    const auto t0 = std::chrono::steady_clock::now();
    int dropped = -1;
    TopoDS_Shape rounded = geom::fillet_edges(box, 3.0, edges, &dropped);
    const double ms = std::chrono::duration<double, std::milli>(
                          std::chrono::steady_clock::now() - t0).count();
    stop.store(true, std::memory_order_relaxed);
    for (auto& t : hammers) t.join();

    MESSAGE("threaded isolated fillet: " << ms << " ms, dropped=" << dropped);
    REQUIRE_FALSE(rounded.IsNull());
    CHECK(dropped == 0);
    // The child budget is 5 s per attempt. Anything near it means the fork wedged and we only
    // survived through the in-process fallback - which is worth failing the build over, because
    // it is the daemon that loses its crash containment.
    CHECK(ms < 4000.0);
#endif
}

TEST_CASE("advisory isolation returns payload without changing parent inputs") {
    using namespace std::chrono_literals;
    using chudware::util::IsolatedTaskStatus;
    int source = 7;
    const auto result = chudware::util::isolate_task([&] {
        source = 99;
        return std::string("candidate diagnostics");
    }, std::chrono::steady_clock::now() + 2s);
#if defined(__unix__) || defined(__APPLE__)
    REQUIRE(result.status == IsolatedTaskStatus::completed);
    CHECK(result.payload == "candidate diagnostics");
#else
    CHECK(result.status == IsolatedTaskStatus::unavailable);
#endif
    CHECK(source == 7);
}

TEST_CASE("advisory isolation kills cumulative slow probes without a parent fallback") {
#if defined(__unix__) || defined(__APPLE__)
    using namespace std::chrono_literals;
    struct Shared {
        std::atomic<long> child{0};
        std::atomic<int> completed{0};
    };
    void* memory = ::mmap(nullptr, sizeof(Shared), PROT_READ | PROT_WRITE,
                          MAP_SHARED | MAP_ANONYMOUS, -1, 0);
    REQUIRE(memory != MAP_FAILED);
    auto* shared = new (memory) Shared;
    const auto started = std::chrono::steady_clock::now();
    int source = 7;
    const auto result = chudware::util::isolate_task([&] {
        shared->child.store(static_cast<long>(::getpgrp()));
        source = 99;
        for (int probe = 0; probe < 20; ++probe) {
            // Every probe individually fits, but their cumulative work does not.
            chudware::util::isolate_shape_build([&] {
                std::this_thread::sleep_for(100ms);
                shared->completed.fetch_add(1);
                return TopoDS_Shape();
            }, 2s);
        }
        return std::string("must not finish");
    }, started + 250ms);
    const long child = shared->child.load();
    CHECK(result.status == chudware::util::IsolatedTaskStatus::timed_out);
    CHECK(result.payload.empty());
    CHECK(source == 7);
    CHECK(shared->completed.load() < 20);
    CHECK(std::chrono::steady_clock::now() - started < 3s);
    if (child > 0) {
        CHECK(::kill(static_cast<pid_t>(child), 0) == -1);
        CHECK(errno == ESRCH);
        int status;
        CHECK(::waitpid(static_cast<pid_t>(child), &status, WNOHANG) == -1);
        CHECK(errno == ECHILD);
    }
    const int finished = shared->completed.load();
    std::this_thread::sleep_for(150ms);
    CHECK(shared->completed.load() == finished);  // no surviving native probe
    shared->~Shared();
    ::munmap(memory, sizeof(Shared));
    // A timeout does not retire isolation or launch an unbounded retry.
    const auto healthy = chudware::util::isolate_task([] { return std::string("ok"); },
                                                     std::chrono::steady_clock::now() + 2s);
    CHECK(healthy.status == chudware::util::IsolatedTaskStatus::completed);
    CHECK(healthy.payload == "ok");
#endif
}
