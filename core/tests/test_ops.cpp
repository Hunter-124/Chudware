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

#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <functional>
#include <filesystem>
#include <thread>
#include <cstdint>
#include <limits>
#include <stdexcept>
#include <string>
#include <vector>

#if defined(__unix__) || defined(__APPLE__)
#include <cerrno>
#include <csignal>
#include <new>
#include <spawn.h>
#include <sys/mman.h>
#include <sys/wait.h>
#include <unistd.h>
#endif
#if defined(__APPLE__)
#include <crt_externs.h>
#elif defined(__unix__)
extern char** environ;
#endif

#include <Bnd_Box.hxx>
#include <BRepAdaptor_Curve.hxx>
#include <BRepAdaptor_Curve2d.hxx>
#include <BRepAdaptor_Surface.hxx>
#include <BRepBndLib.hxx>
#include <BRepCheck_Analyzer.hxx>
#include <BRep_Builder.hxx>
#include <BRepTools.hxx>
#include <BRepBuilderAPI_Transform.hxx>
#include <BRepBuilderAPI_MakeEdge.hxx>
#include <BRepBuilderAPI_MakeFace.hxx>
#include <BRepBuilderAPI_MakePolygon.hxx>
#include <BRepBuilderAPI_MakeWire.hxx>
#include <BRepBuilderAPI_MakeVertex.hxx>
#include <BRepClass3d_SolidClassifier.hxx>
#include <BRepExtrema_DistShapeShape.hxx>
#include <BRepGProp.hxx>
#include <BRepFilletAPI_MakeChamfer.hxx>
#include <BRepFilletAPI_MakeFillet.hxx>
#include <BRepPrimAPI_MakeCylinder.hxx>
#include <BRepPrimAPI_MakePrism.hxx>
#include <GProp_GProps.hxx>
#include <GeomConvert.hxx>
#include <Geom_BSplineCurve.hxx>
#include <Geom_BezierCurve.hxx>
#include <Standard_Failure.hxx>
#include <TColgp_Array1OfPnt.hxx>
#include <TopoDS.hxx>
#include <TopoDS_Face.hxx>
#include <TopoDS_Compound.hxx>
#include <TopoDS_Shape.hxx>
#include <TopoDS_Solid.hxx>
#include <gp_Ax2.hxx>
#include <gp_Ax3.hxx>
#include <gp_Cylinder.hxx>
#include <gp_Dir.hxx>
#include <gp_Lin.hxx>
#include <gp_Pnt.hxx>
#include <gp_Trsf.hxx>
#include <gp_Vec.hxx>

#include "geom/aicm.h"
#include "geom/booleans.h"
#include "geom/dress.h"
#include "geom/features.h"
#include "geom/thread.h"
#include "geom/tessellate.h"
#include "util/process_isolation.h"
#include "util/platform_paths.h"
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

#if defined(__unix__) || defined(__APPLE__)
class ScopedInProcessBlend {
public:
    ScopedInProcessBlend() {
        const char* previous = std::getenv("CHUDWARE_BLEND_ISOLATION");
        had_value_ = previous != nullptr;
        if (previous) value_ = previous;
        if (::setenv("CHUDWARE_BLEND_ISOLATION", "0", 1) != 0)
            throw std::runtime_error("could not select in-process blend policy");
    }
    ~ScopedInProcessBlend() {
        if (had_value_) ::setenv("CHUDWARE_BLEND_ISOLATION", value_.c_str(), 1);
        else ::unsetenv("CHUDWARE_BLEND_ISOLATION");
    }
private:
    bool had_value_ = false;
    std::string value_;
};
#endif

geom::FilletValidationResult validate_generated_cap(const TopoDS_Shape& candidate,
                                                   double z, double limit,
                                                   double minimum, double maximum) {
    double cap = std::numeric_limits<double>::infinity();
    for (TopExp_Explorer it(candidate, TopAbs_VERTEX); it.More(); it.Next()) {
        const gp_Pnt p = BRep_Tool::Pnt(TopoDS::Vertex(it.Current()));
        if (std::abs(p.Z() - z) <= 1e-7 && std::abs(p.Y()) <= 1e-7 &&
            p.X() > minimum && p.X() < maximum)
            cap = std::min(cap, p.X());
    }
    if (!std::isfinite(cap) || cap >= limit)
        return {geom::FilletValidationStatus::rejected, "generated end cap exceeds its limit"};
    return {};
}

geom::FilletValidationResult validate_precision_caps(const TopoDS_Shape& candidate,
                                                    double first_z, double last_z) {
    auto first = validate_generated_cap(candidate, first_z, 1.9e-4, 2e-5, 0.01);
    if (first.status != geom::FilletValidationStatus::valid) return first;
    return validate_generated_cap(candidate, last_z, 8e-5, 2e-5, 0.01);
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

// A fillet corner patch (a cylindrical face with a degenerated edge) next to same-domain
// cylinders, taken from a real part. Merging them made OCCT 7.9.1 dereference a null
// pcurve and killed the daemon on every later boolean of that part.
TEST_CASE("unify_same_domain survives a fillet corner patch beside same-domain faces") {
    TopoDS_Shape patch;
    BRep_Builder builder;
    const auto fixture = std::filesystem::path(__FILE__).parent_path() /
                         "fixtures/fillet_corner_patch.brep";
    REQUIRE(BRepTools::Read(patch, fixture.string().c_str(), builder));
    const auto area = [](const TopoDS_Shape& shape) {
        GProp_GProps props;
        BRepGProp::SurfaceProperties(shape, props);
        return props.Mass();
    };
    const TopoDS_Shape unified = geom::unify_same_domain(patch);
    REQUIRE_FALSE(unified.IsNull());
    CHECK(area(unified) == doctest::Approx(area(patch)).epsilon(1e-9));
    bool patch_kept = false;
    for (TopExp_Explorer fx(unified, TopAbs_FACE); fx.More() && !patch_kept; fx.Next())
        patch_kept = geom::is_degenerate_patch(TopoDS::Face(fx.Current()));
    CHECK(patch_kept);
}

// The servo_lever screw-hole cut: the cut result has a planar face carrying a degenerated
// edge beside a coplanar face. Merging them made OCCT ask the planar reference face for
// the degenerated edge's pcurve, which it derives from a 3D curve the edge lacks, and
// dereference the null result (daemon SIGSEGV in ComputeMinEdgeSize).
TEST_CASE("unify_same_domain survives a planar face with a degenerated edge") {
    TopoDS_Shape body;
    BRep_Builder builder;
    const auto fixture = std::filesystem::path(__FILE__).parent_path() /
                         "fixtures/planar_degenerate_patch.brep";
    REQUIRE(BRepTools::Read(body, fixture.string().c_str(), builder));
    const auto volume = [](const TopoDS_Shape& shape) {
        GProp_GProps props;
        BRepGProp::VolumeProperties(shape, props);
        return props.Mass();
    };
    const TopoDS_Shape unified = geom::unify_same_domain(body);
    REQUIRE_FALSE(unified.IsNull());
    CHECK(volume(unified) == doctest::Approx(volume(body)).epsilon(1e-9));
}

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

    const geom::FilletResult rounded = geom::fillet_edges(box, R, Eigen::Vector3d(0, 0, 1));
    CHECK(rounded.failure == geom::FilletFailure::none);
    CHECK(rounded.requested_radius == R);
    CHECK(rounded.effective_radius == R);
    CHECK(rounded.probes == 1);
    CHECK(rounded.selected_edges == 4);
    CHECK(rounded.covered_edges == 4);
    TopoDS_Shape part = rounded.shape;
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
    TopoDS_Shape part = geom::fillet_edges(bored, R_FILLET, Eigen::Vector3d(0, 0, 1)).shape;
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

// Regression (3-way corner): all three selected edges must be covered. Also
// exercise the full 12-edge round (every vertex is a 3-way corner) and chamfer parity.
// Explicit geometry keeps selection resolution out of this regression.
#include <TopExp.hxx>
#include <TopExp_Explorer.hxx>
#include <TopoDS.hxx>
#include <TopoDS_Edge.hxx>
#include <TopoDS_Vertex.hxx>
#include <TopTools_IndexedMapOfShape.hxx>
#include <BRep_Tool.hxx>
#include <TopTools_IndexedDataMapOfShapeListOfShape.hxx>
#include <TopTools_ListIteratorOfListOfShape.hxx>
TEST_CASE("fillet explains a tangent seam on an existing fillet") {
    const TopoDS_Shape box = geom::make_box(20, 20, 20);
    TopExp_Explorer edges(box, TopAbs_EDGE);
    REQUIRE(edges.More());
    BRepFilletAPI_MakeFillet first(box);
    first.Add(2.0, TopoDS::Edge(edges.Current()));
    first.Build();
    REQUIRE(first.IsDone());
    const TopoDS_Shape rounded = first.Shape();
    TopTools_IndexedDataMapOfShapeListOfShape edge_faces;
    TopExp::MapShapesAndAncestors(rounded, TopAbs_EDGE, TopAbs_FACE, edge_faces);
    TopoDS_Edge seam;
    for (int i = 1; i <= edge_faces.Extent(); ++i) {
        const auto& faces = edge_faces.FindFromIndex(i);
        if (faces.Extent() != 2) continue;
        bool plane = false, cylinder = false;
        for (TopTools_ListIteratorOfListOfShape it(faces); it.More(); it.Next()) {
            const auto type = BRepAdaptor_Surface(TopoDS::Face(it.Value())).GetType();
            plane |= type == GeomAbs_Plane;
            cylinder |= type == GeomAbs_Cylinder;
        }
        if (plane && cylinder) {
            seam = TopoDS::Edge(edge_faces.FindKey(i));
            break;
        }
    }
    REQUIRE_FALSE(seam.IsNull());
    std::optional<double> radius2;
    SUBCASE("constant radius") {}
    SUBCASE("variable radius") { radius2 = 1.5; }
    const auto result = geom::fillet_edges(rounded, 1.0, std::vector<TopoDS_Edge>{seam}, radius2);
    CHECK(result.shape.IsNull());
    CHECK(result.failure == geom::FilletFailure::unsupported_topology);
    CHECK(result.covered_edges == 0);
    CHECK(result.reason.find("selected edge 1: joins two tangent faces") != std::string::npos);
    CHECK(result.reason.find("Pick a sharp edge instead, or edit the existing fillet") != std::string::npos);
}

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

    const geom::FilletResult corner_round = geom::fillet_edges(box, R, corner_edges);
    REQUIRE_FALSE(corner_round.shape.IsNull());
    CHECK(corner_round.selected_edges == 3);
    CHECK(corner_round.covered_edges == 3);
    CHECK(corner_round.effective_radius == R);

    // Full 12-edge round: every one of the 8 vertices is a 3-way corner.
    std::vector<TopoDS_Edge> all_edges;
    for (int i = 1; i <= emap.Extent(); ++i) all_edges.push_back(TopoDS::Edge(emap(i)));
    REQUIRE(all_edges.size() == 12);
    const geom::FilletResult full = geom::fillet_edges(box, R, all_edges);
    REQUIRE_FALSE(full.shape.IsNull());
    CHECK(full.selected_edges == 12);
    CHECK(full.covered_edges == 12);
    CHECK(full.effective_radius == R);

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
    const auto failed = geom::fillet_edges(box, 4.0, one);
    unsetenv("CHUDWARE_TEST_BLEND_SIGSEGV");
    if (isolated) {
        CHECK(failed.shape.IsNull());
        CHECK(failed.failure == geom::FilletFailure::isolation_failed);
        CHECK(failed.covered_edges == 0);
        CHECK(failed.probes == 1);
    } else {
        MESSAGE("blend isolation already retired in this process; fault not armed");
    }
    // The same process remains usable after the isolated child dies.
    const auto healthy = geom::fillet_edges(box, 4.0, one);
    CHECK_FALSE(healthy.shape.IsNull());
    CHECK(healthy.covered_edges == 1);

    // The whole-body ("round every edge") overload is isolated too — it reaches the same
    // OCCT corner resolver, so an unguarded build there would kill the daemon just as dead.
    const bool stillIsolated = chudware::util::shape_isolation_active();
    setenv("CHUDWARE_TEST_BLEND_SIGSEGV", "1", 1);
    const auto allFailed = geom::fillet_edges(box, 2.0, std::nullopt);
    unsetenv("CHUDWARE_TEST_BLEND_SIGSEGV");
    if (stillIsolated) {
        CHECK(allFailed.shape.IsNull());
        CHECK(allFailed.failure == geom::FilletFailure::isolation_failed);
        CHECK(allFailed.covered_edges == 0);
        CHECK(allFailed.probes == 1);
    } else {
        MESSAGE("blend isolation retired before the whole-body case; fault not armed");
    }
    const auto allHealthy = geom::fillet_edges(box, 2.0, std::nullopt);
    CHECK_FALSE(allHealthy.shape.IsNull());
#endif
}

TEST_CASE("fillet remains isolated after a legacy blend timeout") {
#if defined(__unix__) || defined(__APPLE__)
    // Exercise the process-wide legacy retirement in a fresh executable so it cannot
    // contaminate the other geometry tests. A timed-out chamfer-style build must not
    // silently turn the next fillet's crash containment into an in-process build.
    if (std::getenv("CHUDWARE_TEST_FILLET_AFTER_RETIREMENT")) {
        REQUIRE(::setenv("CHUDWARE_BLEND_ISOLATION", "1", 1) == 0);
        const TopoDS_Shape box = geom::make_box(40.0, 30.0, 20.0);
        const double source_volume = solid_volume(box);
        const auto legacy = chudware::util::isolate_shape_build([&] {
            std::this_thread::sleep_for(std::chrono::milliseconds(50));
            return box;
        }, std::chrono::milliseconds(1));
        REQUIRE(legacy.ok);
        REQUIRE_FALSE(chudware::util::shape_isolation_active());

        REQUIRE(::setenv("CHUDWARE_TEST_BLEND_SIGSEGV", "1", 1) == 0);
        const auto crashed = geom::fillet_edges(box, 2.0, Eigen::Vector3d(0, 0, 1));
        ::unsetenv("CHUDWARE_TEST_BLEND_SIGSEGV");
        CHECK(crashed.failure == geom::FilletFailure::isolation_failed);
        CHECK(crashed.shape.IsNull());
        CHECK(crashed.covered_edges == 0);
        CHECK(crashed.probes == 1);

        const auto healthy = geom::fillet_edges(box, 2.0, Eigen::Vector3d(0, 0, 1));
        REQUIRE_MESSAGE(healthy.failure == geom::FilletFailure::none, healthy.reason);
        REQUIRE_FALSE(healthy.shape.IsNull());
        CHECK(healthy.selected_edges == 4);
        CHECK(healthy.covered_edges == 4);
        CHECK(healthy.probes == 1);
        CHECK(BRepCheck_Analyzer(healthy.shape).IsValid());
        CHECK(solid_volume(healthy.shape) ==
              doctest::Approx(source_volume - (4.0 - PI) * 4.0 * 20.0).epsilon(1e-6));
        CHECK(solid_volume(box) == doctest::Approx(source_volume));
        return;
    }

    const std::string executable = chudware::platform::executablePath();
    REQUIRE_FALSE(executable.empty());
    const std::string filter = "--test-case=fillet remains isolated after a legacy blend timeout";
    char* argv[] = {const_cast<char*>(executable.c_str()),
                    const_cast<char*>(filter.c_str()), nullptr};
    std::vector<char*> environment;
#if defined(__APPLE__)
    char** inherited = *_NSGetEnviron();
#else
    char** inherited = environ;
#endif
    for (char** item = inherited; item && *item; ++item)
        environment.push_back(*item);
    environment.push_back(const_cast<char*>("CHUDWARE_TEST_FILLET_AFTER_RETIREMENT=1"));
    environment.push_back(nullptr);
    pid_t pid = -1;
    REQUIRE(::posix_spawn(&pid, executable.c_str(), nullptr, nullptr, argv,
                          environment.data()) == 0);
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(20);
    int status = 0;
    pid_t waited = 0;
    do {
        waited = ::waitpid(pid, &status, WNOHANG);
        if (waited == 0 || (waited < 0 && errno == EINTR))
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
    } while ((waited == 0 || (waited < 0 && errno == EINTR)) &&
             std::chrono::steady_clock::now() < deadline);
    if (waited == 0 || (waited < 0 && errno == EINTR)) {
        ::kill(pid, SIGKILL);
        while (::waitpid(pid, &status, 0) < 0 && errno == EINTR) {}
    }
    REQUIRE_MESSAGE(waited == pid, "isolated regression process did not complete");
    REQUIRE(WIFEXITED(status));
    CHECK(WEXITSTATUS(status) == 0);
#endif
}

// Allocator pressure must not make a successful isolated blend lose crash
// containment or silently retry a timed-out build in the host process.
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
    const auto rounded = geom::fillet_edges(box, 3.0, edges);
    const double ms = std::chrono::duration<double, std::milli>(
                          std::chrono::steady_clock::now() - t0).count();
    stop.store(true, std::memory_order_relaxed);
    for (auto& t : hammers) t.join();

    MESSAGE("threaded isolated fillet: " << ms << " ms");
    REQUIRE_FALSE(rounded.shape.IsNull());
    CHECK(rounded.covered_edges == static_cast<int>(edges.size()));
    CHECK(ms < 4000.0);
#endif
}

namespace {

TopoDS_Edge edge_between(const TopoDS_Shape& shape, const gp_Pnt& a, const gp_Pnt& b) {
    TopTools_IndexedMapOfShape edges;
    TopExp::MapShapes(shape, TopAbs_EDGE, edges);
    for (int i = 1; i <= edges.Extent(); ++i) {
        const TopoDS_Edge e = TopoDS::Edge(edges(i));
        const TopoDS_Vertex first = TopExp::FirstVertex(e);
        const TopoDS_Vertex last = TopExp::LastVertex(e);
        if (first.IsNull() || last.IsNull()) continue;
        const gp_Pnt p = BRep_Tool::Pnt(first), q = BRep_Tool::Pnt(last);
        if ((p.Distance(a) < 1e-6 && q.Distance(b) < 1e-6) ||
            (p.Distance(b) < 1e-6 && q.Distance(a) < 1e-6)) return e;
    }
    return {};
}

bool has_vertex_near(const TopoDS_Shape& shape, const gp_Pnt& p, double tolerance = 1e-5) {
    for (TopExp_Explorer it(shape, TopAbs_VERTEX); it.More(); it.Next())
        if (BRep_Tool::Pnt(TopoDS::Vertex(it.Current())).Distance(p) <= tolerance) return true;
    return false;
}

int cylinders_at_radius(const TopoDS_Shape& shape, double radius) {
    int count = 0;
    for (TopExp_Explorer it(shape, TopAbs_FACE); it.More(); it.Next()) {
        BRepAdaptor_Surface surface(TopoDS::Face(it.Current()));
        if (surface.GetType() == GeomAbs_Cylinder &&
            std::abs(surface.Cylinder().Radius() - radius) <= std::max(1e-7, radius * 1e-5))
            ++count;
    }
    return count;
}

double first_cylinder_radius(const TopoDS_Shape& shape) {
    for (TopExp_Explorer it(shape, TopAbs_FACE); it.More(); it.Next()) {
        BRepAdaptor_Surface surface(TopoDS::Face(it.Current()));
        if (surface.GetType() == GeomAbs_Cylinder) return surface.Cylinder().Radius();
    }
    return 0.0;
}

std::vector<TopoDS_Edge> narrow_box_vertical_edges(const TopoDS_Shape& box) {
    std::vector<TopoDS_Edge> edges;
    for (double x : {0.0, 40.0})
        for (double y : {0.0, 6.0})
            edges.push_back(edge_between(box, gp_Pnt(x, y, 0), gp_Pnt(x, y, 20)));
    return edges;
}

double nonplanar_face_tolerance(const TopoDS_Shape& shape, int& count) {
    double tolerance = 0.0;
    count = 0;
    for (TopExp_Explorer it(shape, TopAbs_FACE); it.More(); it.Next()) {
        const TopoDS_Face face = TopoDS::Face(it.Current());
        if (BRepAdaptor_Surface(face).GetType() == GeomAbs_Plane) continue;
        ++count;
        tolerance = std::max(tolerance, BRep_Tool::Tolerance(face));
    }
    return tolerance;
}

std::array<double, 2> nonplanar_endpoint_tolerance(const TopoDS_Shape& shape,
                                                    const TopoDS_Edge& source, int& count) {
    const TopoDS_Vertex first = TopExp::FirstVertex(source, Standard_True);
    const TopoDS_Vertex last = TopExp::LastVertex(source, Standard_True);
    const gp_Pnt start = BRep_Tool::Pnt(first), end = BRep_Tool::Pnt(last);
    std::array<double, 2> tolerance{0.0, 0.0};
    count = 0;
    auto mask = [&](const TopoDS_Vertex& vertex) {
        if (vertex.IsNull()) return 3;
        const gp_Pnt p = BRep_Tool::Pnt(vertex);
        const double d1 = p.Distance(start), d2 = p.Distance(end);
        const double tol = std::max({1e-7, BRep_Tool::Tolerance(first),
                                     BRep_Tool::Tolerance(last), BRep_Tool::Tolerance(vertex)});
        return std::abs(d1 - d2) <= tol ? 3 : (d1 < d2 ? 1 : 2);
    };
    auto add = [&](int endpoints, double tol) {
        if (endpoints & 1) tolerance[0] = std::max(tolerance[0], tol);
        if (endpoints & 2) tolerance[1] = std::max(tolerance[1], tol);
    };
    for (TopExp_Explorer it(shape, TopAbs_FACE); it.More(); it.Next()) {
        const TopoDS_Face face = TopoDS::Face(it.Current());
        if (BRepAdaptor_Surface(face).GetType() == GeomAbs_Plane) continue;
        ++count;
        add(3, BRep_Tool::Tolerance(face));
        TopTools_IndexedMapOfShape edges, vertices;
        TopExp::MapShapes(face, TopAbs_EDGE, edges);
        TopExp::MapShapes(face, TopAbs_VERTEX, vertices);
        for (int i = 1; i <= edges.Extent(); ++i) {
            const TopoDS_Edge edge = TopoDS::Edge(edges(i));
            add(mask(TopExp::FirstVertex(edge)) | mask(TopExp::LastVertex(edge)),
                BRep_Tool::Tolerance(edge));
        }
        for (int i = 1; i <= vertices.Extent(); ++i) {
            const TopoDS_Vertex vertex = TopoDS::Vertex(vertices(i));
            add(mask(vertex), BRep_Tool::Tolerance(vertex));
        }
    }
    return tolerance;
}


int count_solids(const TopoDS_Shape& shape) {
    int count = 0;
    for (TopExp_Explorer it(shape, TopAbs_SOLID); it.More(); it.Next()) ++count;
    return count;
}

bool inside_any_solid(const TopoDS_Shape& shape, const gp_Pnt& point) {
    for (TopExp_Explorer it(shape, TopAbs_SOLID); it.More(); it.Next()) {
        Bnd_Box bounds;
        BRepBndLib::Add(it.Current(), bounds, Standard_False);
        if (!bounds.IsVoid() && bounds.IsOut(point)) continue;
        BRepClass3d_SolidClassifier classifier(it.Current(), point, 1e-7);
        if (classifier.State() == TopAbs_IN) return true;
    }
    return false;
}

int free_blend_edges(const TopoDS_Shape& shape) {
    TopTools_IndexedDataMapOfShapeListOfShape faces;
    TopExp::MapShapesAndAncestors(shape, TopAbs_EDGE, TopAbs_FACE, faces);
    TopTools_IndexedMapOfShape edges;
    TopExp::MapShapes(shape, TopAbs_EDGE, edges);
    int count = 0;
    for (int i = 1; i <= edges.Extent(); ++i) {
        const TopoDS_Edge edge = TopoDS::Edge(edges(i));
        if (BRep_Tool::Degenerated(edge)) continue;
        if (!faces.Contains(edge)) { ++count; continue; }
        const auto& adjacent = faces.FindFromKey(edge);
        if (adjacent.Extent() >= 2) continue;
        bool seam = false;
        for (TopTools_ListIteratorOfListOfShape it(adjacent); it.More(); it.Next())
            seam = seam || BRep_Tool::IsClosed(edge, TopoDS::Face(it.Value()));
        if (!seam) ++count;
    }
    return count;
}

TopoDS_Shape shallow_stock(double left_y) {
    BRepBuilderAPI_MakePolygon polygon;
    for (const gp_Pnt& p : {gp_Pnt(0, 0, 0), gp_Pnt(40, 0, 0), gp_Pnt(40, 20, 0),
                            gp_Pnt(20, 20, 0), gp_Pnt(0, left_y, 0)})
        polygon.Add(p);
    polygon.Close();
    const TopoDS_Face face = BRepBuilderAPI_MakeFace(polygon.Wire()).Face();
    return BRepPrimAPI_MakePrism(face, gp_Vec(0, 0, 20)).Shape();
}

TopoDS_Shape shallow_spline_stock() {
    const gp_Pnt p0(0, 0, 0), p1(40, 0, 0), p2(40, 20, 0);
    const gp_Pnt corner(20, 20, 0), end(0, 18, 0);
    TColgp_Array1OfPnt poles(1, 3);
    poles.SetValue(1, corner);
    poles.SetValue(2, gp_Pnt(10, 19.1, 0));
    poles.SetValue(3, end);
    // Keep the quadratic exactly, expressed as the B-spline extrusion used by
    // the saved stock. OCCT 7.9.1's SI checker itself fails on the equivalent
    // unmodified Bezier-surface prism, before any fillet is applied.
    const Handle(Geom_BezierCurve) bezier = new Geom_BezierCurve(poles);
    const Handle(Geom_BSplineCurve) spline = GeomConvert::CurveToBSplineCurve(bezier);
    BRepBuilderAPI_MakeWire wire;
    wire.Add(BRepBuilderAPI_MakeEdge(p0, p1).Edge());
    wire.Add(BRepBuilderAPI_MakeEdge(p1, p2).Edge());
    wire.Add(BRepBuilderAPI_MakeEdge(p2, corner).Edge());
    wire.Add(BRepBuilderAPI_MakeEdge(spline).Edge());
    wire.Add(BRepBuilderAPI_MakeEdge(end, p0).Edge());
    const TopoDS_Face face = BRepBuilderAPI_MakeFace(wire.Wire()).Face();
    return BRepPrimAPI_MakePrism(face, gp_Vec(0, 0, 20)).Shape();
}
}  // namespace

TEST_CASE("fillet covers a shallow C0 crease as well as an ordinary edge") {
    // The stock regression's rejected crease has a 5.16-degree face-normal
    // angle. This reduced prism has a 5.14-degree crease and the same radius.
    const TopoDS_Shape stock = shallow_stock(18.2);
    const TopoDS_Edge shallow = edge_between(stock, gp_Pnt(20, 20, 0), gp_Pnt(20, 20, 20));
    const TopoDS_Edge sharp = edge_between(stock, gp_Pnt(40, 0, 0), gp_Pnt(40, 0, 20));
    REQUIRE_FALSE(shallow.IsNull());
    REQUIRE_FALSE(sharp.IsNull());
    const double radius = 8.0;
    const auto rounded = geom::fillet_edges(stock, radius, std::vector<TopoDS_Edge>{shallow, sharp});
    REQUIRE_MESSAGE(rounded.failure == geom::FilletFailure::none, rounded.reason);
    REQUIRE_FALSE(rounded.shape.IsNull());
    CHECK(rounded.selected_edges == 2);
    CHECK(rounded.covered_edges == 2);
    CHECK(rounded.effective_radius == radius);
    CHECK(BRepCheck_Analyzer(rounded.shape).IsValid());
    CHECK(free_blend_edges(rounded.shape) == 0);
    CHECK(cylinders_at_radius(rounded.shape, radius) >= 2);

    const double angle = std::atan(1.8 / 20.0);
    const double shallow_area = radius * radius * (std::tan(angle / 2.0) - angle / 2.0);
    const double square_area = radius * radius * (1.0 - PI / 4.0);
    const double expected = (40.0 * 20.0 - 0.5 * 20.0 * 1.8 - shallow_area - square_area) * 20.0;
    CHECK(solid_volume(rounded.shape) == doctest::Approx(expected).epsilon(1e-7));
    CHECK(solid_volume(stock) - solid_volume(rounded.shape) > square_area * 20.0 + 0.02);
}

TEST_CASE("fillet rounds a shallow line to spline extrusion crease") {
    const TopoDS_Shape stock = shallow_spline_stock();
    const TopoDS_Edge shallow = edge_between(stock, gp_Pnt(20, 20, 0), gp_Pnt(20, 20, 20));
    const TopoDS_Edge sharp = edge_between(stock, gp_Pnt(40, 0, 0), gp_Pnt(40, 0, 20));
    REQUIRE_FALSE(shallow.IsNull());
    REQUIRE_FALSE(sharp.IsNull());
    const auto rounded = geom::fillet_edges(stock, 8.0, std::vector<TopoDS_Edge>{shallow, sharp});
    REQUIRE_MESSAGE(rounded.failure == geom::FilletFailure::none, rounded.reason);
    REQUIRE_FALSE(rounded.shape.IsNull());
    CHECK(rounded.selected_edges == 2);
    CHECK(rounded.covered_edges == 2);
    CHECK(rounded.effective_radius == 8.0);
    CHECK(BRepCheck_Analyzer(rounded.shape).IsValid());
    CHECK(free_blend_edges(rounded.shape) == 0);
    CHECK(cylinders_at_radius(rounded.shape, 8.0) >= 2);
    TopTools_IndexedDataMapOfShapeListOfShape edge_faces;
    TopExp::MapShapesAndAncestors(rounded.shape, TopAbs_EDGE, TopAbs_FACE, edge_faces);
    bool checked_tangency = false;
    for (int i = 1; i <= edge_faces.Extent(); ++i) {
        const TopTools_ListOfShape& adjacent = edge_faces.FindFromIndex(i);
        if (adjacent.Extent() != 2) continue;
        TopTools_ListIteratorOfListOfShape it(adjacent);
        const TopoDS_Face f1 = TopoDS::Face(it.Value());
        it.Next();
        const TopoDS_Face f2 = TopoDS::Face(it.Value());
        BRepAdaptor_Surface s1(f1), s2(f2);
        const bool pair = (s1.GetType() == GeomAbs_Cylinder &&
                           s2.GetType() == GeomAbs_SurfaceOfExtrusion) ||
                          (s2.GetType() == GeomAbs_Cylinder &&
                           s1.GetType() == GeomAbs_SurfaceOfExtrusion);
        if (!pair) continue;
        const TopoDS_Edge edge = TopoDS::Edge(edge_faces.FindKey(i));
        std::array<gp_Vec, 2> normals;
        int k = 0;
        for (const TopoDS_Face& face : {f1, f2}) {
            BRepAdaptor_Curve2d curve(edge, face);
            const gp_Pnt2d uv = curve.Value((curve.FirstParameter() + curve.LastParameter()) * 0.5);
            BRepAdaptor_Surface surface(face);
            gp_Pnt point;
            gp_Vec du, dv;
            surface.D1(uv.X(), uv.Y(), point, du, dv);
            normals[k] = du.Crossed(dv);
            REQUIRE(normals[k].Magnitude() > 0.0);
            normals[k++].Normalize();
        }
        CHECK(std::abs(normals[0].Dot(normals[1])) > 1.0 - 1e-8);
        checked_tangency = true;
    }
    CHECK(checked_tangency);
    const double square_volume = 64.0 * (1.0 - PI / 4.0) * 20.0;
    CHECK(solid_volume(stock) - solid_volume(rounded.shape) > square_volume + 0.02);
}

TEST_CASE("fillet covers both sides of a shallow propagated contour") {
    const TopoDS_Shape stock = shallow_stock(18.2);
    const TopoDS_Edge shallow = edge_between(stock, gp_Pnt(20, 20, 0), gp_Pnt(20, 20, 20));
    const TopoDS_Edge left = edge_between(stock, gp_Pnt(0, 18.2, 20), gp_Pnt(20, 20, 20));
    const TopoDS_Edge right = edge_between(stock, gp_Pnt(20, 20, 20), gp_Pnt(40, 20, 20));
    REQUIRE_FALSE(shallow.IsNull());
    REQUIRE_FALSE(left.IsNull());
    REQUIRE_FALSE(right.IsNull());
    const double radius = 2.0;
    const auto rounded = geom::fillet_edges(stock, radius,
                                            std::vector<TopoDS_Edge>{left, right, shallow});
    REQUIRE_MESSAGE(rounded.failure == geom::FilletFailure::none, rounded.reason);
    REQUIRE_FALSE(rounded.shape.IsNull());
    CHECK(rounded.selected_edges == 3);
    CHECK(rounded.covered_edges == 3);
    CHECK(rounded.effective_radius == radius);
    CHECK(BRepCheck_Analyzer(rounded.shape).IsValid());
    for (const gp_Pnt& p : {gp_Pnt(10, 19.1, 20), gp_Pnt(30, 20, 20)}) {
        const TopoDS_Vertex vertex = BRepBuilderAPI_MakeVertex(p);
        BRepExtrema_DistShapeShape distance(vertex, rounded.shape);
        REQUIRE(distance.IsDone());
        CHECK(distance.Value() == doctest::Approx(radius * (std::sqrt(2.0) - 1.0)).epsilon(1e-4));
    }
}

TEST_CASE("a truly tangent selected edge cannot be reported as a shallow fillet") {
    const TopoDS_Shape stock = shallow_stock(20.0);
    const TopoDS_Edge smooth = edge_between(stock, gp_Pnt(20, 20, 0), gp_Pnt(20, 20, 20));
    const TopoDS_Edge sharp = edge_between(stock, gp_Pnt(40, 0, 0), gp_Pnt(40, 0, 20));
    REQUIRE_FALSE(smooth.IsNull());
    REQUIRE_FALSE(sharp.IsNull());
    const auto rounded = geom::fillet_edges(stock, 8.0, std::vector<TopoDS_Edge>{sharp, smooth});
    CHECK(rounded.shape.IsNull());
    CHECK(rounded.failure == geom::FilletFailure::unsupported_topology);
    CHECK(rounded.covered_edges == 0);
}

TEST_CASE("fillet correction retains all vertical edges and sound geometry") {
    const TopoDS_Shape box = geom::make_box(40, 30, 20);
    const auto rounded = geom::fillet_edges(box, 100.0, Eigen::Vector3d(0, 0, 1));
    REQUIRE_FALSE(rounded.shape.IsNull());
    CHECK(rounded.failure == geom::FilletFailure::none);
    CHECK(rounded.requested_radius == 100.0);
    CHECK(rounded.effective_radius > 14.0);
    CHECK(rounded.effective_radius < 15.0);
    CHECK(rounded.selected_edges == 4);
    CHECK(rounded.covered_edges == 4);
    CHECK(rounded.probes > 1);
    CHECK(rounded.tolerance_mm > 0.0);
    CHECK(BRepCheck_Analyzer(rounded.shape).IsValid());
    CHECK(cylinders_at_radius(rounded.shape, rounded.effective_radius) >= 4);
    const double expected = 40.0 * 30.0 * 20.0 -
        (4.0 - PI) * rounded.effective_radius * rounded.effective_radius * 20.0;
    CHECK(solid_volume(rounded.shape) == doctest::Approx(expected).epsilon(1e-4));
}

TEST_CASE("oversize fillet clamps multiple spline edges") {
    const auto picks = [](const TopoDS_Shape& stock) {
        return std::vector<TopoDS_Edge>{
            edge_between(stock, gp_Pnt(20, 20, 20), gp_Pnt(0, 18, 20)),
            edge_between(stock, gp_Pnt(20, 20, 0), gp_Pnt(0, 18, 0))};
    };
    const TopoDS_Shape stock = shallow_spline_stock();
    const auto edges = picks(stock);
    for (const TopoDS_Edge& edge : edges) {
        REQUIRE_FALSE(edge.IsNull());
        CHECK(BRepAdaptor_Curve(edge).GetType() == GeomAbs_BSplineCurve);
    }
    const double requested = 100.0;
    const auto result = geom::fillet_edges(stock, requested, edges);
    INFO("failure=" << static_cast<int>(result.failure) << ", reason=" << result.reason
         << ", probes=" << result.probes << ", effective=" << result.effective_radius);
    REQUIRE_MESSAGE(result.failure == geom::FilletFailure::none, result.reason);
    REQUIRE_FALSE(result.shape.IsNull());
    CHECK(result.requested_radius == requested);
    CHECK(result.effective_radius > 0.0);
    CHECK(result.effective_radius < requested);
    CHECK(result.selected_edges == 2);
    CHECK(result.covered_edges == 2);
    CHECK(result.probes > 1);
    CHECK(BRepCheck_Analyzer(result.shape).IsValid());
    CHECK(free_blend_edges(result.shape) == 0);
}

TEST_CASE("fillet searches between rejected sizes after a multi-edge construction failure") {
#if defined(__unix__) || defined(__APPLE__)
    const ScopedInProcessBlend isolation;
#endif
    const TopoDS_Shape box = geom::make_box(40, 6, 20);
    const std::vector<TopoDS_Edge> edges = narrow_box_vertical_edges(box);
    for (const TopoDS_Edge& edge : edges) REQUIRE_FALSE(edge.IsNull());
    const double requested = 100.0;
    const TopoDS_Shape direct_box = geom::make_box(40, 6, 20);
    const std::vector<TopoDS_Edge> direct_edges = narrow_box_vertical_edges(direct_box);
    bool construction_failed = false;
    try {
        BRepFilletAPI_MakeFillet direct(direct_box);
        for (const TopoDS_Edge& edge : direct_edges) direct.Add(requested, edge);
        direct.Build();
        construction_failed = !direct.IsDone();
    } catch (const Standard_Failure&) {
        construction_failed = true;
    }
    REQUIRE(construction_failed);

    // A non-monotonic acceptance interval models a local face restriction:
    // plain halving skips it, but the actual candidates are real four-edge blends.
    const geom::FilletValidation validate{geom::FilletValidationPolicy::basic_solid,
        [](const TopoDS_Shape& candidate) -> geom::FilletValidationResult {
            const double radius = first_cylinder_radius(candidate);
            if (radius <= 1.75 || radius >= 2.25)
                return {geom::FilletValidationStatus::rejected, "outside local face interval"};
            return {};
        }};
    const auto result = geom::fillet_edges(box, requested, edges, std::nullopt, validate);
    INFO("failure=" << static_cast<int>(result.failure) << ", reason=" << result.reason
         << ", probes=" << result.probes << ", effective=" << result.effective_radius);
    REQUIRE_MESSAGE(result.failure == geom::FilletFailure::none, result.reason);
    REQUIRE_FALSE(result.shape.IsNull());
    CHECK(result.requested_radius == requested);
    CHECK(result.effective_radius > 2.20);
    CHECK(result.effective_radius < 2.25);
    CHECK(result.selected_edges == 4);
    CHECK(result.covered_edges == 4);
    CHECK(result.probes > 1);
    CHECK(result.probes <= 72);
    CHECK(cylinders_at_radius(result.shape, result.effective_radius) >= 4);
    CHECK(BRepCheck_Analyzer(result.shape).IsValid());
    CHECK(free_blend_edges(result.shape) == 0);
}

TEST_CASE("fillet keeps a validated corrected radius when later validation times out") {
#if defined(__unix__) || defined(__APPLE__)
    const ScopedInProcessBlend isolation;
#endif
    const TopoDS_Shape box = geom::make_box(40, 6, 20);
    const std::vector<TopoDS_Edge> edges = narrow_box_vertical_edges(box);
    for (const TopoDS_Edge& edge : edges) REQUIRE_FALSE(edge.IsNull());
    static bool saw_timeout = false;
    saw_timeout = false;
    const geom::FilletValidation validate{geom::FilletValidationPolicy::basic_solid,
        [](const TopoDS_Shape& candidate) -> geom::FilletValidationResult {
            const double radius = first_cylinder_radius(candidate);
            if (radius > 2.6) {
                saw_timeout = true;
                return {geom::FilletValidationStatus::timed_out, "validation timed out"};
            }
            if (radius <= 0.0)
                return {geom::FilletValidationStatus::rejected, "missing blend face"};
            return {};
        }};
    const auto result = geom::fillet_edges(box, 100.0, edges, std::nullopt, validate);
    INFO("failure=" << static_cast<int>(result.failure) << ", reason=" << result.reason
         << ", probes=" << result.probes << ", effective=" << result.effective_radius);
    REQUIRE(saw_timeout);
    REQUIRE_MESSAGE(result.failure == geom::FilletFailure::none, result.reason);
    REQUIRE_FALSE(result.shape.IsNull());
    CHECK(result.requested_radius == 100.0);
    CHECK(result.effective_radius > 0.0);
    CHECK(result.effective_radius <= 2.6);
    CHECK(result.selected_edges == 4);
    CHECK(result.covered_edges == 4);
    CHECK(result.probes > 1);
    CHECK(result.tolerance_mm >= result.requested_radius - result.effective_radius);
    CHECK(cylinders_at_radius(result.shape, result.effective_radius) >= 4);
    CHECK(BRepCheck_Analyzer(result.shape).IsValid());
    CHECK(free_blend_edges(result.shape) == 0);
}

TEST_CASE("fillet returns failure when no selected-edge candidate validates") {
#if defined(__unix__) || defined(__APPLE__)
    const ScopedInProcessBlend isolation;
#endif
    const TopoDS_Shape box = geom::make_box(0.1, 0.05, 0.1);
    static bool inspected_candidate = false;
    inspected_candidate = false;
    const geom::FilletValidation validate{geom::FilletValidationPolicy::basic_solid,
        [](const TopoDS_Shape&) -> geom::FilletValidationResult {
            inspected_candidate = true;
            return {geom::FilletValidationStatus::rejected, "no valid blend"};
        }};
    const auto result = geom::fillet_edges(box, 0.06, Eigen::Vector3d(0, 0, 1),
                                           std::nullopt, validate);
    CHECK(inspected_candidate);
    CHECK(result.failure != geom::FilletFailure::none);
    CHECK(result.shape.IsNull());
    CHECK(result.selected_edges == 4);
    CHECK(result.covered_edges == 0);
    CHECK(result.probes > 1);
}

TEST_CASE("fillet correction never accepts a radius below the real-fillet minimum") {
#if defined(__unix__) || defined(__APPLE__)
    const ScopedInProcessBlend isolation;
#endif
    // 40x30x20 block, 2 mm request: the minimum is min(0.05, 0.1% of 20, 0.2) = 0.02 mm.
    const TopoDS_Shape box = geom::make_box(40, 30, 20);
    static double largest_valid = 0.0;
    static double smallest_probed = 0.0;
    smallest_probed = std::numeric_limits<double>::max();
    const geom::FilletValidation validate{geom::FilletValidationPolicy::basic_solid,
        [](const TopoDS_Shape& candidate) -> geom::FilletValidationResult {
            const double radius = first_cylinder_radius(candidate);
            smallest_probed = std::min(smallest_probed, radius);
            if (radius > largest_valid)
                return {geom::FilletValidationStatus::rejected, "local face restriction"};
            return {};
        }};
    SUBCASE("only smaller candidates would validate") {
        largest_valid = 0.015;
        const auto result = geom::fillet_edges(box, 2.0, Eigen::Vector3d(0, 0, 1),
                                               std::nullopt, validate);
        CHECK(result.failure != geom::FilletFailure::none);
        CHECK(result.shape.IsNull());
        CHECK(result.covered_edges == 0);
        CHECK(smallest_probed >= 0.02 - 1e-9);
        // The reply carries the cause observed at the requested radius.
        CHECK(result.reason.find("local face restriction") != std::string::npos);
    }
    SUBCASE("a candidate at or above the minimum is kept") {
        largest_valid = 0.03;
        const auto result = geom::fillet_edges(box, 2.0, Eigen::Vector3d(0, 0, 1),
                                               std::nullopt, validate);
        REQUIRE_MESSAGE(result.failure == geom::FilletFailure::none, result.reason);
        CHECK(result.effective_radius >= 0.02);
        CHECK(result.effective_radius <= 0.03);
        CHECK(result.covered_edges == 4);
        CHECK(cylinders_at_radius(result.shape, result.effective_radius) >= 4);
    }
}

TEST_CASE("a fillet build failure names the failing selected edge where it is") {
#if defined(__unix__) || defined(__APPLE__)
    const ScopedInProcessBlend isolation;
#endif
    // 100 mm on a 6 mm-thick box cannot start on any vertical edge; refusing every
    // smaller candidate makes the reply report the failure at the request.
    const TopoDS_Shape box = geom::make_box(40, 6, 20);
    const std::vector<TopoDS_Edge> edges = narrow_box_vertical_edges(box);
    for (const TopoDS_Edge& edge : edges) REQUIRE_FALSE(edge.IsNull());
    const geom::FilletValidation validate{geom::FilletValidationPolicy::basic_solid,
        [](const TopoDS_Shape&) -> geom::FilletValidationResult {
            return {geom::FilletValidationStatus::rejected, "refused"};
        }};
    const auto result = geom::fillet_edges(box, 100.0, edges, std::nullopt, validate);
    INFO("reason=" << result.reason);
    REQUIRE(result.failure != geom::FilletFailure::none);
    const auto at = result.reason.find("blend along selected edge ");
    REQUIRE(at != std::string::npos);
    int pick = 0;
    double x = 0, y = 0, z = 0;
    REQUIRE(std::sscanf(result.reason.c_str() + at,
                        "blend along selected edge %d near (%lf, %lf, %lf)",
                        &pick, &x, &y, &z) == 4);
    REQUIRE(pick >= 1);
    REQUIRE(pick <= static_cast<int>(edges.size()));
    BRepExtrema_DistShapeShape distance(BRepBuilderAPI_MakeVertex(gp_Pnt(x, y, z)).Vertex(),
                                        edges[pick - 1]);
    REQUIRE(distance.IsDone());
    CHECK(distance.Value() < 2e-3);
}

TEST_CASE("fillet lower correction creates real geometry without a fixed floor") {
    const TopoDS_Shape box = geom::make_box(40, 30, 20);
    double request = 0.0;
    SUBCASE("zero") { request = 0.0; }
    SUBCASE("negative") { request = -1.0; }
    SUBCASE("sub-tolerance") { request = 1e-10; }
    const auto rounded = geom::fillet_edges(box, request, Eigen::Vector3d(0, 0, 1));
    REQUIRE_FALSE(rounded.shape.IsNull());
    CHECK(rounded.requested_radius == request);
    CHECK(rounded.effective_radius > request);
    CHECK(rounded.effective_radius > 0.0);
    CHECK(rounded.effective_radius < 0.2);
    CHECK(rounded.selected_edges == 4);
    CHECK(rounded.covered_edges == 4);
    CHECK(rounded.probes >= 1);
    CHECK(BRepCheck_Analyzer(rounded.shape).IsValid());
    CHECK(cylinders_at_radius(rounded.shape, rounded.effective_radius) >= 4);
}

TEST_CASE("variable fillet preserves unequal radii at the picked edge endpoints") {
    const TopoDS_Shape box = geom::make_box(40, 30, 20);
    const TopoDS_Edge edge = edge_between(box, gp_Pnt(0, 0, 0), gp_Pnt(0, 0, 20));
    REQUIRE_FALSE(edge.IsNull());
    const double r1 = 2.0, r2 = 5.0;
    const auto rounded = geom::fillet_edges(box, r1, std::vector<TopoDS_Edge>{edge}, r2);
    REQUIRE_FALSE(rounded.shape.IsNull());
    CHECK(rounded.effective_radius == r1);
    REQUIRE(rounded.effective_radius2.has_value());
    CHECK(*rounded.effective_radius2 == r2);
    CHECK(rounded.selected_edges == 1);
    CHECK(rounded.covered_edges == 1);
    CHECK(rounded.probes == 1);
    const gp_Pnt first = BRep_Tool::Pnt(TopExp::FirstVertex(edge, Standard_True));
    const gp_Pnt last = BRep_Tool::Pnt(TopExp::LastVertex(edge, Standard_True));
    CHECK(has_vertex_near(rounded.shape, gp_Pnt(r1, 0, first.Z())));
    CHECK(has_vertex_near(rounded.shape, gp_Pnt(r2, 0, last.Z())));
    const auto small = geom::fillet_edges(box, r1, std::vector<TopoDS_Edge>{edge});
    const auto large = geom::fillet_edges(box, r2, std::vector<TopoDS_Edge>{edge});
    REQUIRE_FALSE(small.shape.IsNull());
    REQUIRE_FALSE(large.shape.IsNull());
    CHECK(solid_volume(large.shape) < solid_volume(rounded.shape));
    CHECK(solid_volume(rounded.shape) < solid_volume(small.shape));
}

TEST_CASE("variable correction preserves a feasible endpoint and both picked edges") {
    const TopoDS_Shape box = geom::make_box(40, 30, 20);
    const TopoDS_Edge first = edge_between(box, gp_Pnt(0, 0, 0), gp_Pnt(0, 0, 20));
    const TopoDS_Edge second = edge_between(box, gp_Pnt(40, 30, 0), gp_Pnt(40, 30, 20));
    REQUIRE_FALSE(first.IsNull());
    REQUIRE_FALSE(second.IsNull());
    const auto rounded = geom::fillet_edges(box, 100.0, std::vector<TopoDS_Edge>{first, second}, 2.0);
    REQUIRE_FALSE(rounded.shape.IsNull());
    CHECK(rounded.effective_radius < 100.0);
    REQUIRE(rounded.effective_radius2.has_value());
    CHECK(*rounded.effective_radius2 == 2.0);
    CHECK(rounded.selected_edges == 2);
    CHECK(rounded.covered_edges == 2);
    CHECK(BRepCheck_Analyzer(rounded.shape).IsValid());
}

TEST_CASE("variable lower correction keeps the valid endpoint and applies the other") {
    const TopoDS_Shape box = geom::make_box(40, 30, 20);
    const TopoDS_Edge edge = edge_between(box, gp_Pnt(0, 0, 0), gp_Pnt(0, 0, 20));
    REQUIRE_FALSE(edge.IsNull());
    double requested2 = 0.0;
    SUBCASE("zero end radius") { requested2 = 0.0; }
    SUBCASE("negative end radius") { requested2 = -2.0; }
    const auto rounded = geom::fillet_edges(box, 2.0, std::vector<TopoDS_Edge>{edge}, requested2);
    REQUIRE_FALSE(rounded.shape.IsNull());
    CHECK(rounded.effective_radius == 2.0);
    REQUIRE(rounded.effective_radius2.has_value());
    CHECK(*rounded.effective_radius2 > 0.0);
    CHECK(*rounded.effective_radius2 < 0.2);
    CHECK(rounded.covered_edges == 1);
    const double start_z = BRep_Tool::Pnt(TopExp::FirstVertex(edge, Standard_True)).Z();
    const double end_z = BRep_Tool::Pnt(TopExp::LastVertex(edge, Standard_True)).Z();
    CHECK(has_vertex_near(rounded.shape, gp_Pnt(2.0, 0, start_z)));
    CHECK(has_vertex_near(rounded.shape, gp_Pnt(*rounded.effective_radius2, 0, end_z)));
}

TEST_CASE("equal variable endpoints remain equal after lower correction") {
    const TopoDS_Shape box = geom::make_box(40, 30, 20);
    const TopoDS_Edge edge = edge_between(box, gp_Pnt(0, 0, 0), gp_Pnt(0, 0, 20));
    REQUIRE_FALSE(edge.IsNull());
    const auto rounded = geom::fillet_edges(box, 0.0, std::vector<TopoDS_Edge>{edge}, 0.0);
    REQUIRE_FALSE(rounded.shape.IsNull());
    REQUIRE(rounded.effective_radius2.has_value());
    CHECK(rounded.effective_radius > 0.0);
    CHECK(rounded.effective_radius == *rounded.effective_radius2);
    CHECK(rounded.covered_edges == 1);
    CHECK(cylinders_at_radius(rounded.shape, rounded.effective_radius) >= 1);
}

TEST_CASE("variable fillet corrects only endpoints below local model precision") {
    const TopoDS_Shape box = geom::make_box(40, 30, 20);
    TopoDS_Edge edge = edge_between(box, gp_Pnt(0, 0, 0), gp_Pnt(0, 0, 20));
    REQUIRE_FALSE(edge.IsNull());
    double requested1 = 1e-12, requested2 = 3.0;
    bool both_small = false, raise_first_tolerance = false;
    SUBCASE("tiny first") {}
    SUBCASE("tiny first on reversed edge") { edge.Reverse(); }
    SUBCASE("tiny second") { requested1 = 3.0; requested2 = 1e-12; }
    SUBCASE("source vertex tolerance") {
        raise_first_tolerance = true;
        requested1 = 5e-4;
    }
    SUBCASE("both positive and subresolution") {
        requested1 = 1e-12;
        requested2 = 2e-12;
        both_small = true;
    }
    const TopoDS_Vertex first = TopExp::FirstVertex(edge, Standard_True);
    const TopoDS_Vertex last = TopExp::LastVertex(edge, Standard_True);
    REQUIRE_FALSE(first.IsNull());
    REQUIRE_FALSE(last.IsNull());
    if (raise_first_tolerance) {
        BRep_Builder builder;
        builder.UpdateVertex(first, 1e-3);
    }
    const auto result = geom::fillet_edges(box, requested1, std::vector<TopoDS_Edge>{edge}, requested2);
    INFO("failure=" << static_cast<int>(result.failure) << ", reason=" << result.reason
         << ", probes=" << result.probes << ", first=" << result.effective_radius
         << ", second=" << result.effective_radius2.value_or(0.0));
    REQUIRE_FALSE(result.shape.IsNull());
    REQUIRE(result.effective_radius2.has_value());
    CHECK(result.selected_edges == 1);
    CHECK(result.covered_edges == 1);
    CHECK(result.probes > 1);
    CHECK(result.effective_radius > BRep_Tool::Tolerance(first));
    CHECK(*result.effective_radius2 > BRep_Tool::Tolerance(last));
    CHECK(result.effective_radius > BRep_Tool::Tolerance(edge));
    CHECK(*result.effective_radius2 > BRep_Tool::Tolerance(edge));
    TopTools_IndexedDataMapOfShapeListOfShape source_faces;
    TopExp::MapShapesAndAncestors(box, TopAbs_EDGE, TopAbs_FACE, source_faces);
    REQUIRE(source_faces.Contains(edge));
    for (TopTools_ListIteratorOfListOfShape it(source_faces.FindFromKey(edge)); it.More(); it.Next()) {
        const double tolerance = BRep_Tool::Tolerance(TopoDS::Face(it.Value()));
        CHECK(result.effective_radius > tolerance);
        CHECK(*result.effective_radius2 > tolerance);
    }
    CHECK(BRepCheck_Analyzer(result.shape).IsValid());
    CHECK(free_blend_edges(result.shape) == 0);
    int generated_faces = 0;
    const auto generated_tolerance = nonplanar_endpoint_tolerance(result.shape, edge, generated_faces);
    CHECK(generated_faces >= 1);
    CHECK(result.effective_radius > generated_tolerance[0]);
    CHECK(*result.effective_radius2 > generated_tolerance[1]);
    if (!both_small) CHECK(solid_volume(result.shape) < solid_volume(box));
    if (both_small) {
        CHECK(result.effective_radius > requested1);
        CHECK(*result.effective_radius2 > requested2);
        CHECK(result.effective_radius != *result.effective_radius2);
    } else if (requested1 < requested2) {
        CHECK(result.effective_radius > requested1);
        CHECK(*result.effective_radius2 == requested2);
        const double end_z = BRep_Tool::Pnt(last).Z();
        CHECK(has_vertex_near(result.shape, gp_Pnt(requested2, 0, end_z)));
    } else {
        CHECK(result.effective_radius == requested1);
        CHECK(*result.effective_radius2 > requested2);
        const double start_z = BRep_Tool::Pnt(first).Z();
        CHECK(has_vertex_near(result.shape, gp_Pnt(requested1, 0, start_z)));
    }
}

TEST_CASE("variable correction searches between local precision and a rejected radius") {
#if defined(__unix__) || defined(__APPLE__)
    const ScopedInProcessBlend isolation;
#endif
    const TopoDS_Shape box = geom::make_box(40, 30, 20);
    const TopoDS_Edge edge = edge_between(box, gp_Pnt(0, 0, 0), gp_Pnt(0, 0, 20));
    REQUIRE_FALSE(edge.IsNull());
    const TopoDS_Vertex first = TopExp::FirstVertex(edge, Standard_True);
    const TopoDS_Vertex last = TopExp::LastVertex(edge, Standard_True);
    REQUIRE_FALSE(first.IsNull());
    REQUIRE_FALSE(last.IsNull());
    bool second_small = false, equal_small = false, unequal_small = false;
    SUBCASE("first endpoint") {}
    SUBCASE("second endpoint") { second_small = true; }
    SUBCASE("equal endpoints") { equal_small = true; }
    SUBCASE("both unequal endpoints") { unequal_small = true; }
    const TopoDS_Vertex limited = second_small ? last : first;
    const double z = BRep_Tool::Pnt(limited).Z();
    const double local_precision = 1e-3;
    BRep_Builder builder;
    builder.UpdateVertex(limited, local_precision);
    if (equal_small || unequal_small) builder.UpdateVertex(last, local_precision);
    // Inspect real generated caps while testing the in-process search policy.
    const geom::FilletValidationFn validate_start = [](const TopoDS_Shape& candidate) {
        return validate_generated_cap(candidate, 0.0, 1.9e-3, 1e-5, 1.0);
    };
    const geom::FilletValidationFn validate_end = [](const TopoDS_Shape& candidate) {
        return validate_generated_cap(candidate, 20.0, 1.9e-3, 1e-5, 1.0);
    };
    const geom::FilletValidation validate{
        geom::FilletValidationPolicy::basic_solid, z < 10.0 ? validate_start : validate_end};
    const double requested1 = second_small ? 3.0 : 1e-12;
    const double requested2 = unequal_small ? 2e-12 : ((second_small || equal_small) ? 1e-12 : 3.0);
    const auto result = geom::fillet_edges(box, requested1, std::vector<TopoDS_Edge>{edge},
                                           requested2, validate);
    INFO("failure=" << static_cast<int>(result.failure) << ", reason=" << result.reason
         << ", probes=" << result.probes << ", first=" << result.effective_radius
         << ", second=" << result.effective_radius2.value_or(0.0));
    REQUIRE_FALSE(result.shape.IsNull());
    REQUIRE(result.effective_radius2.has_value());
    const double corrected = second_small ? *result.effective_radius2 : result.effective_radius;
    CHECK(corrected > local_precision);
    CHECK(corrected < 2.0 * local_precision);
    if (equal_small) CHECK(result.effective_radius == *result.effective_radius2);
    else if (unequal_small) {
        CHECK(*result.effective_radius2 > local_precision);
        CHECK(*result.effective_radius2 < 2.0 * local_precision);
        CHECK(result.effective_radius != *result.effective_radius2);
    } else CHECK((second_small ? result.effective_radius : *result.effective_radius2) == 3.0);
    CHECK(result.selected_edges == 1);
    CHECK(result.covered_edges == 1);
    CHECK(result.probes > 1);
    CHECK(BRepCheck_Analyzer(result.shape).IsValid());
    CHECK(free_blend_edges(result.shape) == 0);
}

TEST_CASE("variable descent probes a precision interval before changing the opposite endpoint") {
#if defined(__unix__) || defined(__APPLE__)
    const ScopedInProcessBlend isolation;
#endif
    const TopoDS_Shape box = geom::make_box(40, 30, 20);
    const TopoDS_Edge edge = edge_between(box, gp_Pnt(0, 0, 0), gp_Pnt(0, 0, 20));
    REQUIRE_FALSE(edge.IsNull());
    const TopoDS_Vertex first = TopExp::FirstVertex(edge, Standard_True);
    const TopoDS_Vertex last = TopExp::LastVertex(edge, Standard_True);
    REQUIRE_FALSE(first.IsNull());
    REQUIRE_FALSE(last.IsNull());
    const double first_z = BRep_Tool::Pnt(first).Z();
    const double last_z = BRep_Tool::Pnt(last).Z();
    const double local_precision = 1e-4;
    BRep_Builder builder;
    builder.UpdateVertex(first, local_precision);
    const geom::FilletValidationFn forward = [](const TopoDS_Shape& candidate) {
        return validate_precision_caps(candidate, 0.0, 20.0);
    };
    const geom::FilletValidationFn reversed = [](const TopoDS_Shape& candidate) {
        return validate_precision_caps(candidate, 20.0, 0.0);
    };
    const geom::FilletValidation validate{
        geom::FilletValidationPolicy::basic_solid, first_z < last_z ? forward : reversed};
    const auto result = geom::fillet_edges(box, 8e-4, std::vector<TopoDS_Edge>{edge}, 2.4e-3, validate);
    INFO("failure=" << static_cast<int>(result.failure) << ", reason=" << result.reason
         << ", probes=" << result.probes << ", first=" << result.effective_radius
         << ", second=" << result.effective_radius2.value_or(0.0));
    REQUIRE_FALSE(result.shape.IsNull());
    REQUIRE(result.effective_radius2.has_value());
    CHECK(result.effective_radius > local_precision);
    CHECK(result.effective_radius < 1.9e-4);
    CHECK(*result.effective_radius2 < 8e-5);
    CHECK(result.selected_edges == 1);
    CHECK(result.covered_edges == 1);
    CHECK(BRepCheck_Analyzer(result.shape).IsValid());
    CHECK(free_blend_edges(result.shape) == 0);
}

TEST_CASE("variable correction descends both oversized endpoints within the probe budget") {
    const TopoDS_Shape box = geom::make_box(0.1, 0.1, 1000);
    const auto result = geom::fillet_edges(box, 1000.0, Eigen::Vector3d(0, 0, 1), 500.0);
    INFO("failure=" << static_cast<int>(result.failure) << ", reason=" << result.reason
         << ", probes=" << result.probes << ", first=" << result.effective_radius
         << ", second=" << result.effective_radius2.value_or(0.0));
    REQUIRE_FALSE(result.shape.IsNull());
    REQUIRE(result.effective_radius2.has_value());
    CHECK(result.failure == geom::FilletFailure::none);
    CHECK(result.effective_radius > 0.0);
    CHECK(result.effective_radius < 0.1);
    CHECK(*result.effective_radius2 > 0.0);
    CHECK(*result.effective_radius2 < 0.1);
    CHECK(result.selected_edges == 4);
    CHECK(result.covered_edges == 4);
    CHECK(result.probes <= 72);
    CHECK(BRepCheck_Analyzer(result.shape).IsValid());
    CHECK(free_blend_edges(result.shape) == 0);
}

TEST_CASE("tiny variable endpoint correction retains all selected box edges") {
    const TopoDS_Shape box = geom::make_box(40, 30, 20);
    const auto result = geom::fillet_edges(box, 1e-12, Eigen::Vector3d(0, 0, 1), 3.0);
    INFO("failure=" << static_cast<int>(result.failure) << ", reason=" << result.reason
         << ", probes=" << result.probes << ", first=" << result.effective_radius
         << ", second=" << result.effective_radius2.value_or(0.0));
    REQUIRE_FALSE(result.shape.IsNull());
    REQUIRE(result.effective_radius2.has_value());
    CHECK(result.effective_radius > 1e-12);
    CHECK(*result.effective_radius2 == 3.0);
    CHECK(result.selected_edges == 4);
    CHECK(result.covered_edges == 4);
    CHECK(result.probes > 1);
    CHECK(BRepCheck_Analyzer(result.shape).IsValid());
    CHECK(free_blend_edges(result.shape) == 0);
    int generated_faces = 0;
    const double generated_tolerance = nonplanar_face_tolerance(result.shape, generated_faces);
    CHECK(generated_faces >= 4);
    CHECK(result.effective_radius > generated_tolerance);
    CHECK(solid_volume(result.shape) < solid_volume(box));
}

TEST_CASE("contact and ordinary picks are both covered or neither is committed") {
    const TopoDS_Shape first = geom::make_box(10, 10, 10);
    const TopoDS_Shape second = translated(geom::make_box(10, 10, 10), 10, 0, 10);
    const TopoDS_Shape body = geom::fuse(first, second);
    REQUIRE_FALSE(body.IsNull());
    const TopoDS_Edge contact = edge_between(body, gp_Pnt(10, 0, 10), gp_Pnt(10, 10, 10));
    const TopoDS_Edge ordinary = edge_between(body, gp_Pnt(0, 0, 0), gp_Pnt(0, 10, 0));
    REQUIRE_FALSE(contact.IsNull());
    REQUIRE_FALSE(ordinary.IsNull());
    std::vector<TopoDS_Edge> picks;
    SUBCASE("contact first") { picks = {contact, ordinary}; }
    SUBCASE("ordinary first") { picks = {ordinary, contact}; }
    const auto joined = geom::fillet_edges(body, 2.0, picks);
    INFO("failure=" << static_cast<int>(joined.failure) << ", reason=" << joined.reason
         << ", probes=" << joined.probes << ", selected=" << joined.selected_edges
         << ", covered=" << joined.covered_edges);
    REQUIRE_FALSE(joined.shape.IsNull());
    CHECK(joined.selected_edges == 2);
    CHECK(joined.covered_edges == 2);
    CHECK(joined.effective_radius == 2.0);
    CHECK(joined.probes == 1);
    CHECK(count_solids(joined.shape) == 1);
    CHECK(free_blend_edges(joined.shape) == 0);
    CHECK(BRepCheck_Analyzer(joined.shape).IsValid());
    CHECK(solid_volume(joined.shape) ==
          doctest::Approx(2000.0 + 4.0 * (1.0 - PI / 4.0) * 10.0).epsilon(1e-5));
    // Net volume alone can hide a missing contact corner and a missing ordinary cut.
    for (const gp_Pnt& point : {gp_Pnt(10.2, 5, 9.8), gp_Pnt(9.8, 5, 10.2)}) {
        CHECK_FALSE(inside_any_solid(body, point));
        CHECK(inside_any_solid(joined.shape, point));
    }
    const gp_Pnt removed(0.2, 5, 0.2);
    CHECK(inside_any_solid(body, removed));
    CHECK_FALSE(inside_any_solid(joined.shape, removed));
    const auto variable = geom::fillet_edges(body, 2.0,
                                              std::vector<TopoDS_Edge>{contact, ordinary}, 3.0);
    CHECK(variable.shape.IsNull());
    CHECK(variable.covered_edges == 0);
    CHECK(variable.failure == geom::FilletFailure::unsupported_topology);
}

TEST_CASE("a contact join preserves an unrelated solid") {
    const TopoDS_Shape first = geom::make_box(10, 10, 10);
    const TopoDS_Shape second = translated(geom::make_box(10, 10, 10), 10, 0, 10);
    const TopoDS_Shape contact_body = geom::fuse(first, second);
    REQUIRE_FALSE(contact_body.IsNull());
    const TopoDS_Shape other = translated(geom::make_box(10, 10, 10), 50, 0, 0);
    BRep_Builder builder;
    TopoDS_Compound body;
    builder.MakeCompound(body);
    builder.Add(body, contact_body);
    builder.Add(body, other);
    const TopoDS_Edge contact = edge_between(body, gp_Pnt(10, 0, 10), gp_Pnt(10, 10, 10));
    REQUIRE_FALSE(contact.IsNull());
    const auto joined = geom::fillet_edges(body, 2.0, std::vector<TopoDS_Edge>{contact});
    INFO("failure=" << static_cast<int>(joined.failure) << ", reason=" << joined.reason
         << ", probes=" << joined.probes << ", selected=" << joined.selected_edges
         << ", covered=" << joined.covered_edges);
    REQUIRE_FALSE(joined.shape.IsNull());
    CHECK(joined.selected_edges == 1);
    CHECK(joined.covered_edges == 1);
    CHECK(joined.effective_radius == 2.0);
    CHECK(joined.probes == 1);
    CHECK(count_solids(joined.shape) == 2);
    CHECK(BRepCheck_Analyzer(joined.shape).IsValid());
    CHECK(free_blend_edges(joined.shape) == 0);
    CHECK(inside_any_solid(joined.shape, gp_Pnt(55, 5, 5)));
    CHECK(inside_any_solid(joined.shape, gp_Pnt(10.2, 5, 9.8)));
    CHECK(inside_any_solid(joined.shape, gp_Pnt(9.8, 5, 10.2)));
    CHECK(solid_volume(joined.shape) ==
          doctest::Approx(3000.0 + 8.0 * (1.0 - PI / 4.0) * 10.0).epsilon(1e-5));
}

TEST_CASE("multiple selected contacts all join their own source bodies") {
    const TopoDS_Shape a = geom::make_box(10, 10, 10);
    const TopoDS_Shape b = translated(geom::make_box(10, 10, 10), 10, 0, 10);
    const TopoDS_Shape c = translated(geom::make_box(10, 10, 10), 20, 0, 20);
    const TopoDS_Shape ab = geom::fuse(a, b);
    REQUIRE_FALSE(ab.IsNull());
    const TopoDS_Shape body = geom::fuse(ab, c);
    REQUIRE_FALSE(body.IsNull());
    const TopoDS_Edge first = edge_between(body, gp_Pnt(10, 0, 10), gp_Pnt(10, 10, 10));
    const TopoDS_Edge second = edge_between(body, gp_Pnt(20, 0, 20), gp_Pnt(20, 10, 20));
    REQUIRE_FALSE(first.IsNull());
    REQUIRE_FALSE(second.IsNull());
    const auto joined = geom::fillet_edges(body, 1.0, std::vector<TopoDS_Edge>{first, second});
    INFO("failure=" << static_cast<int>(joined.failure) << ", reason=" << joined.reason
         << ", probes=" << joined.probes << ", selected=" << joined.selected_edges
         << ", covered=" << joined.covered_edges);
    REQUIRE_FALSE(joined.shape.IsNull());
    CHECK(joined.selected_edges == 2);
    CHECK(joined.covered_edges == 2);
    CHECK(joined.effective_radius == 1.0);
    CHECK(joined.probes == 1);
    CHECK(count_solids(joined.shape) == 1);
    CHECK(BRepCheck_Analyzer(joined.shape).IsValid());
    CHECK(free_blend_edges(joined.shape) == 0);
    for (double x : {10.0, 20.0}) {
        for (const gp_Pnt& point : {gp_Pnt(x + 0.1, 5, x - 0.1),
                                     gp_Pnt(x - 0.1, 5, x + 0.1)}) {
            CHECK_FALSE(inside_any_solid(body, point));
            CHECK(inside_any_solid(joined.shape, point));
        }
    }
    CHECK(solid_volume(joined.shape) ==
          doctest::Approx(3000.0 + 40.0 * (1.0 - PI / 4.0)).epsilon(1e-5));
}

TEST_CASE("oversize join on long narrow contact corrects to the local face width") {
    const TopoDS_Shape first = geom::make_box(0.1, 1000, 0.1);
    const TopoDS_Shape second = translated(geom::make_box(0.1, 1000, 0.1), 0.1, 0, 0.1);
    const TopoDS_Shape body = geom::fuse(first, second);
    REQUIRE_FALSE(body.IsNull());
    const TopoDS_Edge contact = edge_between(body, gp_Pnt(0.1, 0, 0.1), gp_Pnt(0.1, 1000, 0.1));
    REQUIRE_FALSE(contact.IsNull());
    double requested = 1000.0;
    SUBCASE("full-length request") { requested = 1000.0; }
    SUBCASE("intermediate oversize request") { requested = 31.25; }
    const auto joined = geom::fillet_edges(body, requested, std::vector<TopoDS_Edge>{contact});
    INFO("failure=" << static_cast<int>(joined.failure) << ", reason=" << joined.reason
         << ", probes=" << joined.probes << ", selected=" << joined.selected_edges
         << ", covered=" << joined.covered_edges << ", effective=" << joined.effective_radius
         << ", tolerance=" << joined.tolerance_mm);
    CHECK(joined.failure != geom::FilletFailure::unsupported_topology);
    REQUIRE_FALSE(joined.shape.IsNull());
    CHECK(joined.requested_radius == requested);
    CHECK(joined.effective_radius > 0.05);
    CHECK(joined.effective_radius < 0.1);
    CHECK(joined.selected_edges == 1);
    CHECK(joined.covered_edges == 1);
    CHECK(joined.probes > 1);
    CHECK(count_solids(joined.shape) == 1);
    CHECK(free_blend_edges(joined.shape) == 0);
    CHECK(BRepCheck_Analyzer(joined.shape).IsValid());
    const double d = 0.1 * joined.effective_radius;
    for (double y : {100.0, 500.0, 900.0}) {
        for (const gp_Pnt& point : {gp_Pnt(0.1 + d, y, 0.1 - d),
                                     gp_Pnt(0.1 - d, y, 0.1 + d)}) {
            CHECK_FALSE(inside_any_solid(body, point));
            CHECK(inside_any_solid(joined.shape, point));
        }
    }
    const double expected = 20.0 + 2.0 * joined.effective_radius * joined.effective_radius *
                            (1.0 - PI / 4.0) * 1000.0;
    CHECK(solid_volume(joined.shape) == doctest::Approx(expected).epsilon(1e-4));
}

TEST_CASE("overlapping solids sharing topology are rejected as contact joins") {
    const TopoDS_Shape first = geom::make_box(10, 10, 10);
    BRep_Builder builder;
    TopoDS_Solid duplicate;
    builder.MakeSolid(duplicate);
    for (TopExp_Explorer it(first, TopAbs_SHELL); it.More(); it.Next())
        builder.Add(duplicate, it.Current());
    TopoDS_Compound overlapping;
    builder.MakeCompound(overlapping);
    builder.Add(overlapping, first);
    builder.Add(overlapping, duplicate);
    const TopoDS_Edge edge = edge_between(overlapping, gp_Pnt(0, 0, 0), gp_Pnt(0, 10, 0));
    REQUIRE_FALSE(edge.IsNull());
    const auto result = geom::fillet_edges(overlapping, 2.0, std::vector<TopoDS_Edge>{edge});
    INFO("failure=" << static_cast<int>(result.failure) << ", reason=" << result.reason
         << ", probes=" << result.probes << ", selected=" << result.selected_edges
         << ", covered=" << result.covered_edges);
    CHECK(result.shape.IsNull());
    CHECK(result.failure == geom::FilletFailure::unsupported_topology);
    CHECK(result.selected_edges == 1);
    CHECK(result.covered_edges == 0);
}

TEST_CASE("fillet rejects invalid selected topology without returning a partial shape") {
    const TopoDS_Shape box = geom::make_box(40, 30, 20);
    const TopoDS_Edge edge = edge_between(box, gp_Pnt(0, 0, 0), gp_Pnt(0, 0, 20));
    REQUIRE_FALSE(edge.IsNull());
    SUBCASE("duplicate") {
        const auto result = geom::fillet_edges(box, 2, std::vector<TopoDS_Edge>{edge, edge});
        CHECK(result.failure == geom::FilletFailure::invalid_selection);
        CHECK(result.shape.IsNull());
        CHECK(result.covered_edges == 0);
    }
    SUBCASE("foreign edge") {
        const TopoDS_Shape other = geom::make_box(10, 10, 10);
        const TopoDS_Edge foreign = edge_between(other, gp_Pnt(0, 0, 0), gp_Pnt(0, 0, 10));
        REQUIRE_FALSE(foreign.IsNull());
        const auto result = geom::fillet_edges(box, 2, std::vector<TopoDS_Edge>{edge, foreign});
        CHECK(result.failure == geom::FilletFailure::invalid_selection);
        CHECK(result.shape.IsNull());
        CHECK(result.covered_edges == 0);
    }
    SUBCASE("non-finite radius") {
        const auto result = geom::fillet_edges(box, std::numeric_limits<double>::infinity(),
                                               std::vector<TopoDS_Edge>{edge});
        CHECK(result.failure == geom::FilletFailure::invalid_input);
        CHECK(result.shape.IsNull());
        CHECK(result.probes == 0);
    }
    const auto healthy = geom::fillet_edges(box, 2, std::vector<TopoDS_Edge>{edge});
    CHECK_FALSE(healthy.shape.IsNull());
    CHECK(healthy.covered_edges == 1);
}

TEST_CASE("explicit seam and degenerate edges are classified instead of skipped") {
    SUBCASE("cylinder seam") {
        const TopoDS_Shape cylinder = cylinder_z(gp_Pnt(0, 0, 0), 10, 20);
        TopTools_IndexedDataMapOfShapeListOfShape edge_faces;
        TopExp::MapShapesAndAncestors(cylinder, TopAbs_EDGE, TopAbs_FACE, edge_faces);
        TopoDS_Edge seam;
        for (int i = 1; i <= edge_faces.Extent() && seam.IsNull(); ++i) {
            const TopoDS_Edge e = TopoDS::Edge(edge_faces.FindKey(i));
            for (TopTools_ListIteratorOfListOfShape f(edge_faces.FindFromIndex(i)); f.More(); f.Next())
                if (BRep_Tool::IsClosed(e, TopoDS::Face(f.Value()))) { seam = e; break; }
        }
        REQUIRE_FALSE(seam.IsNull());
        const auto result = geom::fillet_edges(cylinder, 2, std::vector<TopoDS_Edge>{seam});
        CHECK(result.failure == geom::FilletFailure::unsupported_topology);
        CHECK(result.shape.IsNull());
        CHECK(result.covered_edges == 0);
    }
    SUBCASE("sphere degenerate edge") {
        const TopoDS_Shape sphere = geom::make_sphere(10);
        TopoDS_Edge degenerate;
        for (TopExp_Explorer it(sphere, TopAbs_EDGE); it.More(); it.Next()) {
            const TopoDS_Edge e = TopoDS::Edge(it.Current());
            if (BRep_Tool::Degenerated(e)) { degenerate = e; break; }
        }
        REQUIRE_FALSE(degenerate.IsNull());
        const auto result = geom::fillet_edges(sphere, 2, std::vector<TopoDS_Edge>{degenerate});
        CHECK(result.failure == geom::FilletFailure::unsupported_topology);
        CHECK(result.shape.IsNull());
        CHECK(result.covered_edges == 0);
    }
}

TEST_CASE("incomplete fillet validation is terminal without correcting the radius") {
#if defined(__unix__) || defined(__APPLE__)
    const ScopedInProcessBlend isolation;
#endif
    const TopoDS_Shape box = geom::make_box(40, 30, 20);
    const TopoDS_Edge edge = edge_between(box, gp_Pnt(0, 0, 0), gp_Pnt(0, 0, 20));
    REQUIRE_FALSE(edge.IsNull());
    const geom::FilletValidation validate{geom::FilletValidationPolicy::basic_solid,
        [](const TopoDS_Shape& candidate) -> geom::FilletValidationResult {
            constexpr double original_volume = 40.0 * 30.0 * 20.0;
            if (!BRepCheck_Analyzer(candidate).IsValid() || solid_volume(candidate) >= original_volume)
                return {geom::FilletValidationStatus::rejected, "candidate geometry was not built"};
            return {geom::FilletValidationStatus::timed_out, "self-intersection validation incomplete"};
        }};
    const auto result = geom::fillet_edges(box, 2.0, std::vector<TopoDS_Edge>{edge},
                                           std::nullopt, validate);
    INFO("failure=" << static_cast<int>(result.failure) << ", reason=" << result.reason
         << ", probes=" << result.probes << ", effective=" << result.effective_radius);
    CHECK(result.failure == geom::FilletFailure::timed_out);
    CHECK(result.requested_radius == 2.0);
    CHECK(result.effective_radius == 2.0);
    CHECK(result.selected_edges == 1);
    CHECK(result.covered_edges == 0);
    CHECK(result.probes == 1);
    CHECK(result.shape.IsNull());
}

TEST_CASE("a slow isolated fillet probe within the search budget is not failed") {
#if defined(__unix__) || defined(__APPLE__)
    if (chudware::util::shape_isolation_active()) {
        // A sound build that takes longer than five seconds (a large spline body plus
        // full-solid validation) must build the same on a busy machine as on an idle one.
        const TopoDS_Shape box = geom::make_box(40, 30, 20);
        const TopoDS_Edge edge = edge_between(box, gp_Pnt(0, 0, 0), gp_Pnt(0, 0, 20));
        REQUIRE_FALSE(edge.IsNull());
        setenv("CHUDWARE_TEST_BLEND_DELAY_MS", "5500", 1);
        const auto slow = geom::fillet_edges(box, 2, std::vector<TopoDS_Edge>{edge});
        unsetenv("CHUDWARE_TEST_BLEND_DELAY_MS");
        INFO("failure=" << static_cast<int>(slow.failure) << ", reason=" << slow.reason);
        CHECK(slow.failure == geom::FilletFailure::none);
        REQUIRE_FALSE(slow.shape.IsNull());
        CHECK(slow.effective_radius == 2.0);
        CHECK(slow.probes == 1);
        CHECK(cylinders_at_radius(slow.shape, 2.0) >= 1);
    }
#endif
}

TEST_CASE("advisory isolation returns payload without changing parent inputs") {
    using namespace std::chrono_literals;
    using chudware::util::IsolatedTaskStatus;
    int source = 7;
    std::string expected = "candidate diagnostics";
    SUBCASE("text payload") { expected = "candidate diagnostics"; }
    SUBCASE("empty payload") { expected.clear(); }
    SUBCASE("binary payload") { expected = std::string("a\0b", 3); }
    SUBCASE("payload larger than the pipe buffer") { expected.assign(128 * 1024, 'x'); }
    const auto result = chudware::util::isolate_task([&] {
        source = 99;
        return expected;
    }, std::chrono::steady_clock::now() + 2s);
#if defined(__unix__) || defined(__APPLE__)
    REQUIRE(result.status == IsolatedTaskStatus::completed);
    CHECK(result.payload == expected);
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

TEST_CASE("advisory isolation contains crashes and rejects oversized output") {
#if defined(__unix__) || defined(__APPLE__)
    using namespace std::chrono_literals;
    std::function<std::string()> task;
    SUBCASE("native crash") {
        task = [] {
            ::raise(SIGSEGV);
            return std::string("must not escape");
        };
    }
    SUBCASE("callback exception") {
        task = []() -> std::string { throw std::runtime_error("isolated failure"); };
    }
    SUBCASE("payload exceeds the result limit") {
        task = [] { return std::string(16 * 1024 * 1024 + 1, 'x'); };
    }
    const auto started = std::chrono::steady_clock::now();
    const auto result = chudware::util::isolate_task(task, started + 2s);
    CHECK(result.status == chudware::util::IsolatedTaskStatus::failed);
    CHECK(result.payload.empty());
    CHECK(std::chrono::steady_clock::now() - started < 3s);
    const auto healthy = chudware::util::isolate_task([] { return std::string("ok"); },
                                                     std::chrono::steady_clock::now() + 2s);
    CHECK(healthy.status == chudware::util::IsolatedTaskStatus::completed);
    CHECK(healthy.payload == "ok");
#endif
}

TEST_CASE("thread: full-length M12 cuts a sound helical groove along the whole shaft") {
    constexpr double radius = 6.0, height = 10.0, pitch = 1.75;
    const TopoDS_Shape cylinder = cylinder_z(gp_Pnt(0, 0, 0), radius, height);
    const double original_volume = solid_volume(cylinder);
    auto tolerances = [](const TopoDS_Shape& shape) {
        std::vector<double> values;
        for (TopExp_Explorer ex(shape, TopAbs_FACE); ex.More(); ex.Next())
            values.push_back(BRep_Tool::Tolerance(TopoDS::Face(ex.Current())));
        for (TopExp_Explorer ex(shape, TopAbs_EDGE); ex.More(); ex.Next())
            values.push_back(BRep_Tool::Tolerance(TopoDS::Edge(ex.Current())));
        for (TopExp_Explorer ex(shape, TopAbs_VERTEX); ex.More(); ex.Next())
            values.push_back(BRep_Tool::Tolerance(TopoDS::Vertex(ex.Current())));
        return values;
    };
    const std::vector<double> original_tolerances = tolerances(cylinder);
    const geom::ThreadResult result = geom::cut_thread(
        cylinder, gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1), radius, pitch, 12.0,
        height, 1, false, false, false, 0.0);
    REQUIRE_MESSAGE(result.ok, result.warning);
    CHECK_FALSE(result.fell_back);
    REQUIRE_FALSE(result.shape.IsNull());
    CHECK(BRepCheck_Analyzer(result.shape).IsValid());
    CHECK(count_solids(result.shape) == 1);
    const double volume = solid_volume(result.shape);
    CHECK(volume < original_volume * 0.99);
    CHECK(volume > original_volume * 0.5);
    // The original cached input must remain unchanged by the boolean.
    CHECK(solid_volume(cylinder) == doctest::Approx(original_volume).epsilon(1e-10));
    CHECK(BRepCheck_Analyzer(cylinder).IsValid());
    const std::vector<double> after_tolerances = tolerances(cylinder);
    REQUIRE(after_tolerances.size() == original_tolerances.size());
    for (std::size_t i = 0; i < after_tolerances.size(); ++i)
        CHECK(after_tolerances[i] == original_tolerances[i]);

    // At the groove centre, a point well inside the original skin is removed;
    // opposite it the thread crest and the core remain. Check near both ends and
    // through the middle, not just total volume, to detect incomplete runs.
    const gp_Ax3 frame(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1));
    const gp_Vec x(frame.XDirection()), y(frame.YDirection());
    for (double z : {0.2, 2.5, 5.0, 7.5, 9.8}) {
        const double angle = 2.0 * PI * z / pitch;
        const gp_Vec radial = x.Multiplied(std::cos(angle)) + y.Multiplied(std::sin(angle));
        const gp_Pnt groove((radius - 0.35) * radial.X(), (radius - 0.35) * radial.Y(), z);
        const gp_Pnt crest(-(radius - 0.35) * radial.X(), -(radius - 0.35) * radial.Y(), z);
        CHECK(inside_any_solid(cylinder, groove));
        CHECK_FALSE(inside_any_solid(result.shape, groove));
        CHECK(inside_any_solid(result.shape, crest));
        CHECK(inside_any_solid(result.shape, gp_Pnt(0, 0, z)));
    }
}

TEST_CASE("thread: incomplete or invalid real construction is not successful") {
    const TopoDS_Shape cylinder = cylinder_z(gp_Pnt(0, 0, 0), 6.0, 10.0);
    SUBCASE("turn cap cannot cover the run") {
        const geom::ThreadResult result = geom::cut_thread(
            cylinder, gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1), 6.0, 1.75, 12.0,
            10.0, 1, false, false, false, 1.0);
        CHECK_FALSE(result.ok);
        CHECK(result.fell_back);
        CHECK_FALSE(result.warning.empty());
        CHECK(result.shape.IsSame(cylinder));
    }
    SUBCASE("degenerate pitch") {
        const geom::ThreadResult result = geom::cut_thread(
            cylinder, gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1), 6.0, 0.0, 12.0,
            10.0, 1, false, false, false, 0.0);
        CHECK_FALSE(result.ok);
        CHECK(result.fell_back);
        CHECK_FALSE(result.warning.empty());
        CHECK(result.shape.IsSame(cylinder));
    }
    SUBCASE("multi-start work exceeds the bounded section budget") {
        const geom::ThreadResult result = geom::cut_thread(
            cylinder, gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1), 6.0, 1.75, 12.0,
            10.0, 129, false, false, false, 0.0);
        CHECK_FALSE(result.ok);
        CHECK(result.fell_back);
        CHECK_FALSE(result.warning.empty());
        CHECK(result.shape.IsSame(cylinder));
    }
    SUBCASE("disjoint helical tool removes no material") {
        const double before = solid_volume(cylinder);
        const geom::ThreadResult result = geom::cut_thread(
            cylinder, gp_Pnt(100, 0, 0), gp_Dir(0, 0, 1), 6.0, 1.75, 12.0,
            10.0, 1, false, false, false, 0.0);
        CHECK_FALSE(result.ok);
        CHECK(result.fell_back);
        CHECK_FALSE(result.warning.empty());
        CHECK(result.shape.IsSame(cylinder));
        CHECK(solid_volume(cylinder) == doctest::Approx(before).epsilon(1e-10));
        CHECK(BRepCheck_Analyzer(cylinder).IsValid());
    }
}

TEST_CASE("thread: over-long run on an oblique cylinder respects its axial extent") {
    constexpr double radius = 6.0, height = 10.0, pitch = 1.75;
    const gp_Pnt origin(2, 3, 4);
    const gp_Dir axis(1, 1, 1);
    const TopoDS_Shape cylinder =
        BRepPrimAPI_MakeCylinder(gp_Ax2(origin, axis), radius, height).Shape();
    // The requested run overhangs the body, but its actual ten-millimetre
    // extent plus the end extensions fits in eight turns.
    const geom::ThreadResult result = geom::cut_thread(
        cylinder, origin, axis, radius, pitch, 12.0, 80.0, 1, false, false, false, 8.0);
    REQUIRE_MESSAGE(result.ok, result.warning);
    REQUIRE_FALSE(result.shape.IsNull());
    CHECK(BRepCheck_Analyzer(result.shape).IsValid());
    CHECK(count_solids(result.shape) == 1);
    CHECK(solid_volume(result.shape) < solid_volume(cylinder) * 0.99);
    const gp_Ax3 frame(origin, axis);
    const gp_Vec x(frame.XDirection()), y(frame.YDirection()), z_axis(axis);
    for (double z : {0.2, 9.8}) {
        const double angle = 2.0 * PI * z / pitch;
        const gp_Vec radial = x.Multiplied(std::cos(angle)) + y.Multiplied(std::sin(angle));
        const gp_Pnt p = origin.Translated(
            z_axis.Multiplied(z) + radial.Multiplied(radius - 0.35));
        CHECK(inside_any_solid(cylinder, p));
        CHECK_FALSE(inside_any_solid(result.shape, p));
    }
}

TEST_CASE("thread: partial run stops at the requested boundary") {
    constexpr double radius = 6.0, pitch = 1.75;
    const TopoDS_Shape cylinder = cylinder_z(gp_Pnt(0, 0, 0), radius, 10.0);
    const double original_volume = solid_volume(cylinder);
    const geom::ThreadResult result = geom::cut_thread(
        cylinder, gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1), radius, pitch, 12.0,
        4.0, 1, false, false, false, 0.0);
    REQUIRE_MESSAGE(result.ok, result.warning);
    REQUIRE_FALSE(result.shape.IsNull());
    CHECK(BRepCheck_Analyzer(result.shape).IsValid());
    CHECK(count_solids(result.shape) == 1);
    CHECK(solid_volume(result.shape) < original_volume - 1.0);
    CHECK(solid_volume(cylinder) == doctest::Approx(original_volume).epsilon(1e-10));
    CHECK(BRepCheck_Analyzer(cylinder).IsValid());
    const gp_Ax3 frame(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1));
    const gp_Vec x(frame.XDirection()), y(frame.YDirection());
    auto at_groove = [&](double z) {
        const double angle = 2.0 * PI * z / pitch;
        const gp_Vec r = x.Multiplied(std::cos(angle)) + y.Multiplied(std::sin(angle));
        return gp_Pnt((radius - 0.35) * r.X(), (radius - 0.35) * r.Y(), z);
    };
    CHECK_FALSE(inside_any_solid(result.shape, at_groove(3.7)));
    CHECK(inside_any_solid(result.shape, at_groove(4.3)));
}

TEST_CASE("thread: left-handed two-start cut has both reversed helices") {
    constexpr double radius = 6.0, pitch = 1.75, lead = 2.0 * pitch;
    double height = 10.0;
    SUBCASE("one section") {}
    SUBCASE("crossing a section boundary") { height = 30.0; }
    const TopoDS_Shape cylinder = cylinder_z(gp_Pnt(0, 0, 0), radius, height);
    const geom::ThreadResult result = geom::cut_thread(
        cylinder, gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1), radius, pitch, 12.0,
        height, 2, true, false, false, 0.0);
    REQUIRE_MESSAGE(result.ok, result.warning);
    REQUIRE_FALSE(result.shape.IsNull());
    CHECK(BRepCheck_Analyzer(result.shape).IsValid());
    CHECK(count_solids(result.shape) == 1);
    CHECK(solid_volume(result.shape) < solid_volume(cylinder) - 1.0);
    const gp_Ax3 frame(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1));
    const gp_Vec x(frame.XDirection()), y(frame.YDirection());
    // The eight-turn boundary is at z=28; test both sides as well as a
    // position that distinguishes a left-handed helix from a right-handed one.
    for (double z : {2.1875, 27.8, 28.2, 29.8}) {
        if (z >= height) continue;
        CAPTURE(height);
        CAPTURE(z);
        const double angle = -2.0 * PI * z / lead;
        auto point = [&](double a) {
            const gp_Vec r = x.Multiplied(std::cos(a)) + y.Multiplied(std::sin(a));
            return gp_Pnt((radius - 0.35) * r.X(), (radius - 0.35) * r.Y(), z);
        };
        CHECK_FALSE(inside_any_solid(result.shape, point(angle)));
        CHECK_FALSE(inside_any_solid(result.shape, point(angle + PI)));
        CHECK(inside_any_solid(result.shape, point(angle + 0.5 * PI)));
    }
}

TEST_CASE("thread: form, bore and raised geometry produce valid material changes") {
    geom::ThreadForm form = geom::ThreadForm::V60;
    bool internal = false, raised = false;
    double height = 10.0;
    SUBCASE("ACME") { form = geom::ThreadForm::ACME; }
    SUBCASE("trapezoidal") { form = geom::ThreadForm::TRAP; }
    SUBCASE("square") { form = geom::ThreadForm::SQUARE; }
    SUBCASE("internal") { internal = true; }
    SUBCASE("raised") { raised = true; }
    SUBCASE("ACME across sections") { form = geom::ThreadForm::ACME; height = 16.0; }

    const TopoDS_Shape shaft = cylinder_z(gp_Pnt(0, 0, 0), 6.0, height);
    const TopoDS_Shape body = internal
        ? geom::cut(cylinder_z(gp_Pnt(0, 0, 0), 9.0, height), shaft) : shaft;
    REQUIRE_FALSE(body.IsNull());
    REQUIRE(BRepCheck_Analyzer(body).IsValid());
    const double before = solid_volume(body);
    const geom::ThreadResult result = geom::cut_thread(
        body, gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1), 6.0, 1.75, 12.0,
        height, 1, false, internal, raised, 0.0, form);
    REQUIRE_MESSAGE(result.ok, result.warning);
    CHECK_FALSE(result.fell_back);
    REQUIRE_FALSE(result.shape.IsNull());
    CHECK(BRepCheck_Analyzer(result.shape).IsValid());
    CHECK(count_solids(result.shape) == 1);
    const double after = solid_volume(result.shape);
    if (raised) CHECK(after > before + 1.0);
    else CHECK(after < before - 1.0);
}

TEST_CASE("thread: segmented internal cut continues across the section boundary") {
    constexpr double radius = 6.0, height = 16.0, pitch = 1.75;
    const TopoDS_Shape inner = cylinder_z(gp_Pnt(0, 0, 0), radius, height);
    const TopoDS_Shape body = geom::cut(cylinder_z(gp_Pnt(0, 0, 0), 9.0, height), inner);
    REQUIRE_FALSE(body.IsNull());
    const geom::ThreadResult result = geom::cut_thread(
        body, gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1), radius, pitch, 12.0,
        height, 1, false, true, false, 0.0);
    REQUIRE_MESSAGE(result.ok, result.warning);
    REQUIRE_FALSE(result.shape.IsNull());
    CHECK(BRepCheck_Analyzer(result.shape).IsValid());
    CHECK(count_solids(result.shape) == 1);
    CHECK(solid_volume(result.shape) < solid_volume(body) - 1.0);
    const gp_Ax3 frame(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1));
    const gp_Vec x(frame.XDirection()), y(frame.YDirection());
    for (double z : {0.2, 13.8, 14.2, 15.8}) {
        const double angle = 2.0 * PI * z / pitch;
        const gp_Vec radial = x.Multiplied(std::cos(angle)) + y.Multiplied(std::sin(angle));
        const gp_Pnt groove((radius + 0.35) * radial.X(), (radius + 0.35) * radial.Y(), z);
        const gp_Pnt crest(-(radius + 0.35) * radial.X(), -(radius + 0.35) * radial.Y(), z);
        CHECK(inside_any_solid(body, groove));
        CHECK_FALSE(inside_any_solid(result.shape, groove));
        CHECK(inside_any_solid(result.shape, crest));
    }
}

#include <BRepAlgoAPI_BuilderAlgo.hxx>
#include <BRepPrimAPI_MakeBox.hxx>
#include <TopTools_ListOfShape.hxx>
#include <TopoDS_Shell.hxx>

namespace {

// 40 x 30 x 10 block with a 2 x 2 mm relief slot open at its x = 0 end and at the bottom
// (x -1..25, y 12..14, z -1..2): the slot ceiling (z = 2) meets the end wall (x = 0) along a
// 2 mm edge, like the ear relief of servo_bracket_L. `imprinted` also carries an edge
// imprinted INTO its top face by a general fuse, which is what a boss tangent to that face
// leaves behind: an INTERNAL edge of the face with no skin use.
TopoDS_Shape relief_block(bool imprinted) {
    const TopoDS_Shape slotted = geom::cut(
        BRepPrimAPI_MakeBox(40.0, 30.0, 10.0).Shape(),
        BRepPrimAPI_MakeBox(gp_Pnt(-1, 12, -1), 26.0, 2.0, 3.0).Shape());
    REQUIRE_FALSE(slotted.IsNull());
    if (!imprinted) return slotted;
    TopTools_ListOfShape args;
    args.Append(slotted);
    args.Append(BRepBuilderAPI_MakeEdge(gp_Pnt(10, 10, 10), gp_Pnt(30, 20, 10)).Edge());
    BRepAlgoAPI_BuilderAlgo imprint;
    imprint.SetArguments(args);
    imprint.Build();
    REQUIRE(imprint.IsDone());
    TopExp_Explorer solids(imprint.Shape(), TopAbs_SOLID);
    REQUIRE(solids.More());
    return solids.Current();
}

int internal_edge_uses(const TopoDS_Shape& shape) {
    int uses = 0;
    for (TopExp_Explorer f(shape, TopAbs_FACE); f.More(); f.Next())
        for (TopExp_Explorer e(f.Current(), TopAbs_EDGE); e.More(); e.Next())
            if (e.Current().Orientation() == TopAbs_INTERNAL) ++uses;
    return uses;
}

}  // namespace

TEST_CASE("is_watertight: seams and boolean imprints keep a body closed, a missing face opens it") {
    const TopoDS_Shape box = BRepPrimAPI_MakeBox(10.0, 10.0, 10.0).Shape();
    CHECK(geom::is_watertight(box));
    CHECK(geom::is_watertight(cylinder_z(gp_Pnt(0, 0, 0), 4.0, 10.0)));  // seam + apex-free skin
    CHECK(geom::is_watertight(relief_block(false)));

    const TopoDS_Shape imprinted = relief_block(true);
    REQUIRE(internal_edge_uses(imprinted) == 1);
    CHECK(geom::is_watertight(imprinted));
    TopTools_IndexedMapOfShape edges;
    TopExp::MapShapes(imprinted, TopAbs_EDGE, edges);
    int embedded_only = 0;
    const auto uses = geom::edge_boundary_uses(imprinted, edges);
    for (int i = 1; i <= edges.Extent(); ++i)
        if (uses[static_cast<std::size_t>(i)].boundary == 0 &&
            uses[static_cast<std::size_t>(i)].embedded)
            ++embedded_only;
    CHECK(embedded_only == 1);  // the imprint is embedded, never free

    // Negative control: five of the six faces is a genuinely open shell.
    BRep_Builder builder;
    TopoDS_Shell open_shell;
    builder.MakeShell(open_shell);
    int added = 0;
    for (TopExp_Explorer f(box, TopAbs_FACE); f.More() && added < 5; f.Next(), ++added)
        builder.Add(open_shell, f.Current());
    CHECK_FALSE(geom::is_watertight(open_shell));
    CHECK_FALSE(geom::is_watertight(TopoDS_Shape()));
}

// servo_bracket_L: an earlier boss left a 0.1 mm boolean imprint INTERNAL to an ear face. The
// closure check counted ancestor faces, so it read that imprint as a free edge in every
// chamfer result and rejected the chamfer of the ear relief's end edge at ANY distance (down
// to 0.1 mm) as "could not construct a valid closed blend ... size feasibility is
// undetermined". Symmetric, two-distance and distance-angle chamfers share the check.
TEST_CASE("chamfer: an internal imprint elsewhere on the body does not fail a shallow relief edge") {
    const TopoDS_Shape body = relief_block(true);
    REQUIRE(internal_edge_uses(body) == 1);
    REQUIRE(count_solids(body) == 1);

    TopoDS_Edge relief_edge;
    TopTools_IndexedMapOfShape edges;
    TopExp::MapShapes(body, TopAbs_EDGE, edges);
    for (int i = 1; i <= edges.Extent(); ++i) {
        const TopoDS_Edge edge = TopoDS::Edge(edges(i));
        BRepAdaptor_Curve curve(edge);
        if (curve.GetType() != GeomAbs_Line) continue;
        const gp_Pnt mid = curve.Value(0.5 * (curve.FirstParameter() + curve.LastParameter()));
        if (mid.Distance(gp_Pnt(0, 13, 2)) < 1e-6) relief_edge = edge;
    }
    REQUIRE_FALSE(relief_edge.IsNull());
    const std::vector<TopoDS_Edge> picked{relief_edge};
    const double before = solid_volume(body);

    // Each chamfer removes a right-triangle prism along the 2 mm edge.
    int dropped = -1;
    const auto check = [&](const TopoDS_Shape& out, double removed) {
        REQUIRE_FALSE(out.IsNull());
        CHECK(dropped == 0);
        CHECK(geom::is_watertight(out));
        CHECK(BRepCheck_Analyzer(out).IsValid());
        CHECK(count_solids(out) == 1);
        CHECK(internal_edge_uses(out) == 1);  // the unrelated imprint is carried through
        CHECK(before - solid_volume(out) == doctest::Approx(removed).epsilon(1e-6));
    };
    SUBCASE("symmetric 2 x 2 mm") {
        check(geom::chamfer_edges(body, 2.0, picked, &dropped), 4.0);
    }
    SUBCASE("two-distance 2 x 1 mm") {
        check(geom::chamfer_edges_two_distance(body, 2.0, 1.0, picked, &dropped), 2.0);
    }
    SUBCASE("distance-angle 2 mm at 45 degrees") {
        check(geom::chamfer_edges_distance_angle(body, 2.0, PI / 4.0, picked, std::nullopt, &dropped),
              4.0);
    }
}

// ---- inside-corner fillets wider than the faces they roll on ---------------------------
//
// Each body is a 60 x 40 x 5 plate with a feature standing on it. Expected volumes come from
// the rolling-ball profile, not from a kernel result: a radius-R blend between a wall and a
// floor leaves, at distance t from the wall, a gap g(t) = R - sqrt(R^2 - (R - t)^2) above
// the floor (and, by symmetry, a width g(y) at height y).
namespace {

double blend_gap(double radius, double t) {
    return radius - std::sqrt(std::max(0.0, radius * radius - (radius - t) * (radius - t)));
}

// Cross-section between wall, floor and blend, out to `reach` from the wall.
double blend_area(double radius, double reach) {
    constexpr int kSteps = 200000;
    double area = 0.0;
    for (int i = 0; i < kSteps; ++i) area += blend_gap(radius, (i + 0.5) * reach / kSteps);
    return area * reach / kSteps;
}

// Volume of the blend around a cylinder of radius `boss`, up to `height` above the floor.
double boss_blend_volume(double boss, double radius, double height) {
    constexpr int kSteps = 200000;
    double volume = 0.0;
    for (int i = 0; i < kSteps; ++i) {
        const double w = blend_gap(radius, (i + 0.5) * height / kSteps);
        volume += PI * (2.0 * boss * w + w * w);
    }
    return volume * height / kSteps;
}

TopoDS_Shape plate_with(const TopoDS_Shape& feature) {
    return geom::unify_same_domain(geom::fuse(geom::make_box(60, 40, 5), feature));
}

// The edges where a feature meets the plate's top face (z = 5), not the plate's own rim.
// `min_x` leaves out edges nearer the x = 0 side, such as the rim of an edge break.
std::vector<TopoDS_Edge> foot_edges(const TopoDS_Shape& body, double min_x = 1e-6) {
    std::vector<TopoDS_Edge> out;
    TopTools_IndexedMapOfShape edges;
    TopExp::MapShapes(body, TopAbs_EDGE, edges);
    for (int i = 1; i <= edges.Extent(); ++i) {
        const TopoDS_Edge edge = TopoDS::Edge(edges(i));
        const BRepAdaptor_Curve curve(edge);
        bool flat = true;
        for (const double f : {0.0, 0.5, 1.0}) {
            const gp_Pnt p = curve.Value(curve.FirstParameter() +
                                         f * (curve.LastParameter() - curve.FirstParameter()));
            flat = flat && std::abs(p.Z() - 5.0) < 1e-6;
        }
        const gp_Pnt mid = curve.Value(0.5 * (curve.FirstParameter() + curve.LastParameter()));
        if (flat && mid.X() > min_x && mid.X() < 60 - 1e-6 && mid.Y() > 1e-6 && mid.Y() < 40 - 1e-6)
            out.push_back(edge);
    }
    return out;
}

// The plain kernel fillet at the full radius must fail, or the test proves nothing.
bool plain_fillet_fails(const TopoDS_Shape& body, const std::vector<TopoDS_Edge>& edges,
                        double radius) {
    try {
        BRepFilletAPI_MakeFillet plain(body);
        for (const TopoDS_Edge& edge : edges) plain.Add(radius, edge);
        plain.Build();
        return !plain.IsDone() || plain.Shape().IsNull() ||
               !BRepCheck_Analyzer(plain.Shape()).IsValid();
    } catch (const Standard_Failure&) {
        return true;
    }
}

void check_runoff(const geom::FilletResult& result, const TopoDS_Shape& body, double radius,
                  double added_volume) {
    INFO("failure=" << static_cast<int>(result.failure) << ", reason=" << result.reason
         << ", probes=" << result.probes << ", effective=" << result.effective_radius);
    REQUIRE(result.failure == geom::FilletFailure::none);
    REQUIRE_FALSE(result.shape.IsNull());
    CHECK(result.effective_radius == radius);  // the request itself, not a corrected size
    CHECK(result.covered_edges == result.selected_edges);
    CHECK(count_solids(result.shape) == 1);
    CHECK(geom::is_watertight(result.shape));
    CHECK(BRepCheck_Analyzer(result.shape).IsValid());
    CHECK(solid_volume(result.shape) - solid_volume(body) ==
          doctest::Approx(added_volume).epsilon(2e-3));
}

}  // namespace

TEST_CASE("a fillet taller than its boss ends flush with the boss top at the requested radius") {
    constexpr double kBoss = 8.0, kHeight = 3.0, kRadius = 5.0;
    const TopoDS_Shape body = plate_with(cylinder_z(gp_Pnt(30, 20, 5), kBoss, kHeight));
    const auto edges = foot_edges(body);
    REQUIRE(edges.size() == 1);
    REQUIRE(plain_fillet_fails(body, edges, kRadius));
    check_runoff(geom::fillet_edges(body, kRadius, edges), body, kRadius,
                 boss_blend_volume(kBoss, kRadius, kHeight));
}

TEST_CASE("fillets taller than a low rib end flush with its top on both sides") {
    constexpr double kRadius = 5.0;
    const TopoDS_Shape body = plate_with(geom::make_box_at(28.5, 0, 5, 3, 40, 3));
    const auto edges = foot_edges(body);
    REQUIRE(edges.size() == 2);
    REQUIRE(plain_fillet_fails(body, edges, kRadius));
    check_runoff(geom::fillet_edges(body, kRadius, edges), body, kRadius,
                 2.0 * 40.0 * blend_area(kRadius, 3.0));
}

TEST_CASE("a fillet wider than the strip of plate beside a rib runs off the plate edge") {
    constexpr double kRadius = 4.0;
    // The rib's left foot is 2 mm from the plate edge: the blend reaches 4 mm, so only the
    // part inside the plate stays. Its right foot has room for the whole blend.
    const TopoDS_Shape body = plate_with(geom::make_box_at(2, 0, 5, 3, 40, 15));
    const auto edges = foot_edges(body);
    REQUIRE(edges.size() == 2);
    REQUIRE(plain_fillet_fails(body, edges, kRadius));
    check_runoff(geom::fillet_edges(body, kRadius, edges), body, kRadius,
                 40.0 * (blend_area(kRadius, kRadius) + blend_area(kRadius, 2.0)));
}

TEST_CASE("an inside-corner fillet taller than its wall runs off the wall top") {
    constexpr double kRadius = 30.0;
    // L bracket: a 35 mm base beside a wall that rises only 25 mm above it.
    const TopoDS_Shape body = geom::unify_same_domain(geom::fuse(
        geom::make_box_at(0, 0, 0, 40, 20, 5), geom::make_box_at(0, 0, 0, 5, 20, 30)));
    const TopoDS_Edge corner = edge_between(body, gp_Pnt(5, 0, 5), gp_Pnt(5, 20, 5));
    REQUIRE_FALSE(corner.IsNull());
    const std::vector<TopoDS_Edge> edges{corner};
    REQUIRE(plain_fillet_fails(body, edges, kRadius));
    check_runoff(geom::fillet_edges(body, kRadius, edges), body, kRadius,
                 20.0 * blend_area(kRadius, 25.0));
}

TEST_CASE("blends that collide when built together merge where they meet") {
    constexpr double kBoss = 8.0, kHeight = 15.0, kRadius = 5.0;
    // Two bosses 4 mm apart: each blend alone fits, but the two overlap.
    const TopoDS_Shape first = cylinder_z(gp_Pnt(15, 20, 5), kBoss, kHeight);
    const TopoDS_Shape second = cylinder_z(gp_Pnt(35, 20, 5), kBoss, kHeight);
    const TopoDS_Shape body = plate_with(geom::fuse(first, second));
    const auto edges = foot_edges(body);
    REQUIRE(edges.size() == 2);
    REQUIRE(plain_fillet_fails(body, edges, kRadius));

    // The merged blend is the union of the two blends built one at a time.
    const auto alone = [&](const TopoDS_Edge& edge) {
        const auto one = geom::fillet_edges(body, kRadius, std::vector<TopoDS_Edge>{edge});
        REQUIRE(one.failure == geom::FilletFailure::none);
        return one.shape;
    };
    const double united = solid_volume(geom::fuse(alone(edges[0]), alone(edges[1])));
    const double single = boss_blend_volume(kBoss, kRadius, kRadius);
    REQUIRE(united - solid_volume(body) < 2.0 * single - 1.0);  // the blends really overlap
    check_runoff(geom::fillet_edges(body, kRadius, edges), body, kRadius,
                 united - solid_volume(body));
}

TEST_CASE("run-off continues over a chamfered plate edge") {
    constexpr double kRadius = 4.0;
    // The plate's x = 0 top edge is chamfered 1 mm, so its top face ends at x = 1. The rib's
    // left foot is 2 mm beyond that: the blend runs off over the chamfer and is cut back
    // flush with the plane through the rim.
    TopoDS_Shape plate = geom::make_box(60, 40, 5);
    const TopoDS_Edge lip = edge_between(plate, gp_Pnt(0, 0, 5), gp_Pnt(0, 40, 5));
    REQUIRE_FALSE(lip.IsNull());
    BRepFilletAPI_MakeChamfer chamfer(plate);
    chamfer.Add(1.0, lip);
    chamfer.Build();
    REQUIRE(chamfer.IsDone());
    const TopoDS_Shape body = geom::unify_same_domain(
        geom::fuse(chamfer.Shape(), geom::make_box_at(3, 0, 5, 3, 40, 15)));
    const auto edges = foot_edges(body, 2.5);
    REQUIRE(edges.size() == 2);
    REQUIRE(plain_fillet_fails(body, edges, kRadius));
    check_runoff(geom::fillet_edges(body, kRadius, edges), body, kRadius,
                 40.0 * (blend_area(kRadius, kRadius) + blend_area(kRadius, 2.0)));
}

// ---- blends whose end vertex joins more than one other face -----------------------------
//
// A hook finger stands against a wall's end face (y = 0, material at y < 0). Its tip is the
// intersection of two radius-2.2 cylinders, one along z (the hook's outer curve, tangent to
// the wall's front face x = 1.6) and one along x (the rounded tip, whose top line meets the
// wall at z = kTop). The inside corner along that top line, from x = 0 to x = 1.6, ends at
// (1.6, 0, kTop) where the wall's front face, the hook's curve and the tip all meet: the
// kernel builder cannot close the blend there (it "succeeds" with a tolerance tens of
// millimetres wide). The blend's cross-section is constant along the edge, so it is swept.
namespace {

constexpr double kHookTop = 10.0, kHookRadius = 2.2;

TopoDS_Shape hook_against_wall() {
    const TopoDS_Shape wall = geom::make_box_at(0, -10, 0, 1.6, 10, kHookTop + 5);
    // The flat part of the finger (y < 0) ends level with the tip's top line, tangent to it.
    const TopoDS_Shape flat = geom::make_box_at(-2.8, -1, -5, 4.4, 1, kHookTop + 5);
    const TopoDS_Shape curve = BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(-0.6, 0, -5), gp_Dir(0, 0, 1)), kHookRadius, kHookTop + 5).Shape();
    const TopoDS_Shape tip = BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(-3, 0, kHookTop - kHookRadius), gp_Dir(1, 0, 0)), kHookRadius, 4.6).Shape();
    const TopoDS_Shape round = geom::common(geom::common(curve, tip),
                                            geom::make_box_at(-3, 0, -5, 5, 3, kHookTop + 5));
    return geom::unify_same_domain(geom::fuse(geom::fuse(wall, flat), round));
}

// The inside corner along the tip's top line, where it meets the wall.
TopoDS_Edge hook_root_edge(const TopoDS_Shape& body) {
    TopTools_IndexedMapOfShape edges;
    TopExp::MapShapes(body, TopAbs_EDGE, edges);
    for (int i = 1; i <= edges.Extent(); ++i) {
        const BRepAdaptor_Curve curve(TopoDS::Edge(edges(i)));
        if (curve.GetType() != GeomAbs_Line) continue;
        const gp_Pnt a = curve.Value(curve.FirstParameter()), b = curve.Value(curve.LastParameter());
        if (a.Distance(gp_Pnt(0, 0, kHookTop)) + b.Distance(gp_Pnt(1.6, 0, kHookTop)) < 1e-6 ||
            b.Distance(gp_Pnt(0, 0, kHookTop)) + a.Distance(gp_Pnt(1.6, 0, kHookTop)) < 1e-6)
            return TopoDS::Edge(edges(i));
    }
    return {};
}

// The kernel builder alone either fails or bridges the corner with a tolerance no blend of
// this size needs; otherwise the test proves nothing.
bool plain_blend_unusable(const TopoDS_Shape& body, const TopoDS_Edge& edge, double radius) {
    try {
        BRepFilletAPI_MakeFillet plain(body);
        plain.Add(radius, edge);
        plain.Build();
        if (!plain.IsDone() || plain.Shape().IsNull()) return true;
        double worst = 0.0;
        for (TopExp_Explorer it(plain.Shape(), TopAbs_VERTEX); it.More(); it.Next())
            worst = std::max(worst, BRep_Tool::Tolerance(TopoDS::Vertex(it.Current())));
        return worst > 0.05 * radius || !BRepCheck_Analyzer(plain.Shape()).IsValid();
    } catch (const Standard_Failure&) {
        return true;
    }
}

}  // namespace

TEST_CASE("an inside-corner fillet ending where three faces meet the tip of a hook is built at its radius") {
    constexpr double kRadius = 1.0;
    const TopoDS_Shape body = hook_against_wall();
    REQUIRE(count_solids(body) == 1);
    const TopoDS_Edge edge = hook_root_edge(body);
    REQUIRE_FALSE(edge.IsNull());
    REQUIRE(plain_blend_unusable(body, edge, kRadius));

    const geom::FilletResult result = geom::fillet_edges(body, kRadius, std::vector<TopoDS_Edge>{edge});
    INFO("failure=" << static_cast<int>(result.failure) << ", reason=" << result.reason);
    REQUIRE(result.failure == geom::FilletFailure::none);
    CHECK(result.effective_radius == kRadius);
    CHECK(count_solids(result.shape) == 1);
    CHECK(BRepCheck_Analyzer(result.shape).IsValid());

    // The rolling-ball centre stays kRadius from the wall and kHookRadius + kRadius from the
    // tip's axis; the blend face is that cylinder.
    const double centre_z = kHookTop - kHookRadius +
        std::sqrt((kHookRadius + kRadius) * (kHookRadius + kRadius) - kRadius * kRadius);
    bool blend_face = false;
    for (TopExp_Explorer it(result.shape, TopAbs_FACE); it.More(); it.Next()) {
        BRepAdaptor_Surface surface(TopoDS::Face(it.Current()));
        if (surface.GetType() != GeomAbs_Cylinder) continue;
        const gp_Cylinder cylinder = surface.Cylinder();
        if (std::abs(cylinder.Radius() - kRadius) < 1e-6 &&
            cylinder.Axis().Direction().IsParallel(gp_Dir(1, 0, 0), 1e-9) &&
            gp_Lin(cylinder.Axis()).Distance(gp_Pnt(0, kRadius, centre_z)) < 1e-6)
            blend_face = true;
    }
    CHECK(blend_face);

    // The blend adds its cross-section along the 1.6 mm edge, less the sliver beyond the
    // hook's curve at the far end.
    const double contact = kRadius * kHookRadius / (kHookRadius + kRadius);
    constexpr int kSteps = 20000;
    double area = 0.0;
    for (int i = 0; i < kSteps; ++i) {
        const double y = (i + 0.5) * contact / kSteps;
        const double arc = centre_z - std::sqrt(kRadius * kRadius - (y - kRadius) * (y - kRadius));
        const double tip = kHookTop - kHookRadius + std::sqrt(kHookRadius * kHookRadius - y * y);
        area += arc - tip;
    }
    area *= contact / kSteps;
    const double added = solid_volume(result.shape) - solid_volume(body);
    CHECK(added < area * 1.6);
    CHECK(added > area * 1.6 * 0.95);
}

TEST_CASE("a chamfer on the hook's inside corner keeps its edge") {
    const TopoDS_Shape body = hook_against_wall();
    const TopoDS_Edge edge = hook_root_edge(body);
    REQUIRE_FALSE(edge.IsNull());
    int dropped = -1;
    const TopoDS_Shape out = geom::chamfer_edges(body, 1.0, std::vector<TopoDS_Edge>{edge}, &dropped);
    REQUIRE_FALSE(out.IsNull());
    CHECK(dropped == 0);
    CHECK(count_solids(out) == 1);
    CHECK(BRepCheck_Analyzer(out).IsValid());
    // Setbacks of 1 mm along the wall and along the tip's arc: the triangle they span with
    // the corner, less the arc's bulge into it, along the 1.6 mm edge (less the end sliver).
    const double theta = 1.0 / kHookRadius;
    const double triangle = 0.5 * kHookRadius * std::sin(theta);
    const double bulge = 0.5 * kHookRadius * kHookRadius * (theta - std::sin(theta));
    const double added = solid_volume(out) - solid_volume(body);
    CHECK(added < (triangle - bulge) * 1.6 * 1.001);
    CHECK(added > (triangle - bulge) * 1.6 * 0.95);
}

TEST_CASE("both top edges of a wall rounded at half its thickness make a full round") {
    // A 1.6 mm wall, 20 mm long, standing on a plate: rounding both top edges at 0.8 mm
    // consumes the whole top face.
    constexpr double kRadius = 0.8, kLength = 20.0;
    const TopoDS_Shape body = geom::unify_same_domain(
        geom::fuse(geom::make_box(30, 20, 3), geom::make_box_at(5, 9.2, 3, kLength, 1.6, 8)));
    TopTools_IndexedMapOfShape edges;
    TopExp::MapShapes(body, TopAbs_EDGE, edges);
    std::vector<TopoDS_Edge> top;
    for (int i = 1; i <= edges.Extent(); ++i) {
        const BRepAdaptor_Curve curve(TopoDS::Edge(edges(i)));
        const gp_Pnt m = curve.Value(0.5 * (curve.FirstParameter() + curve.LastParameter()));
        if (std::abs(m.Z() - 11) < 1e-6 && std::abs(m.X() - 15) < 1e-6) top.push_back(TopoDS::Edge(edges(i)));
    }
    REQUIRE(top.size() == 2);
    const geom::FilletResult result = geom::fillet_edges(body, kRadius, top);
    INFO("failure=" << static_cast<int>(result.failure) << ", reason=" << result.reason);
    REQUIRE(result.failure == geom::FilletFailure::none);
    CHECK(result.effective_radius == kRadius);
    CHECK(count_solids(result.shape) == 1);
    CHECK(BRepCheck_Analyzer(result.shape).IsValid());
    // Each round removes the square corner less its quarter disc.
    CHECK(solid_volume(body) - solid_volume(result.shape) ==
          doctest::Approx(2.0 * kLength * kRadius * kRadius * (1.0 - PI / 4.0)).epsilon(1e-3));
    // The wall's top is one half cylinder about the wall's mid-plane: the rounds share their
    // axis and together cover half a turn, with no strip of the old top face left between.
    double round_area = 0.0;
    for (TopExp_Explorer it(result.shape, TopAbs_FACE); it.More(); it.Next()) {
        BRepAdaptor_Surface surface(TopoDS::Face(it.Current()));
        if (surface.GetType() == GeomAbs_Plane) {
            const bool old_top = surface.Plane().Axis().Direction().IsParallel(gp_Dir(0, 0, 1), 1e-9) &&
                                 std::abs(surface.Plane().Location().Z() - 11.0) < 1e-6;
            CHECK_FALSE(old_top);
            continue;
        }
        if (surface.GetType() != GeomAbs_Cylinder) continue;
        const gp_Cylinder cylinder = surface.Cylinder();
        CHECK(cylinder.Radius() == doctest::Approx(kRadius));
        CHECK(gp_Lin(cylinder.Axis()).Distance(gp_Pnt(15, 10.0, 11.0 - kRadius)) < 1e-6);
        GProp_GProps props;
        BRepGProp::SurfaceProperties(it.Current(), props);
        round_area += props.Mass();
    }
    CHECK(round_area == doctest::Approx(PI * kRadius * kLength).epsilon(1e-4));
}
