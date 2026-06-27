// Sweep robustness suite — the geometry kernel behind the Sweep feature
// (geom::sweep_profile, BRepOffsetAPI_MakePipeShell) plus its #1 use case, a
// helical thread (geom::make_helix as the path). Sweeps are traditionally the
// most fragile SolidWorks feature, so these assertions are ANALYTIC and tight:
// the exact swept volume a correct kernel must produce, holding across OCCT
// versions. The helix case is the payoff — a small profile chased along a real
// 3-turn helix must close into a valid solid of the expected tube volume.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <cmath>

#include <BRepAdaptor_Curve.hxx>
#include <BRepBuilderAPI_MakeEdge.hxx>
#include <BRepBuilderAPI_MakeFace.hxx>
#include <BRepBuilderAPI_MakeWire.hxx>
#include <BRepGProp.hxx>
#include <GProp_GProps.hxx>
#include <GProp_PrincipalProps.hxx>
#include <TopExp_Explorer.hxx>
#include <TopoDS.hxx>
#include <TopoDS_Edge.hxx>
#include <TopoDS_Face.hxx>
#include <TopoDS_Shape.hxx>
#include <TopoDS_Wire.hxx>
#include <gp_Ax2.hxx>
#include <gp_Circ.hxx>
#include <gp_Dir.hxx>
#include <gp_Pnt.hxx>
#include <gp_Vec.hxx>

#include "geom/curves.h"
#include "geom/features.h"

namespace {

constexpr double kPi = 3.14159265358979323846;

double solid_volume(const TopoDS_Shape& s) {
    GProp_GProps props;
    BRepGProp::VolumeProperties(s, props);
    return props.Mass();
}

// A circular face of `r` centered at `c`, lying in the plane whose normal is `n`
// (so a sweep along a path tangent to `n` runs perpendicular through it).
TopoDS_Face circle_face(const gp_Pnt& c, const gp_Dir& n, double r) {
    gp_Circ circ(gp_Ax2(c, n), r);
    TopoDS_Edge e = BRepBuilderAPI_MakeEdge(circ);
    TopoDS_Wire w = BRepBuilderAPI_MakeWire(e);
    return BRepBuilderAPI_MakeFace(w);
}

// An annular (washer) face: outer circle `R` with a concentric `r` hole.
TopoDS_Face washer_face(const gp_Pnt& c, const gp_Dir& n, double R, double r) {
    gp_Circ co(gp_Ax2(c, n), R), ci(gp_Ax2(c, n), r);
    TopoDS_Wire wo = BRepBuilderAPI_MakeWire(BRepBuilderAPI_MakeEdge(co));
    TopoDS_Wire wi = BRepBuilderAPI_MakeWire(BRepBuilderAPI_MakeEdge(ci));
    BRepBuilderAPI_MakeFace mf(wo);
    mf.Add(TopoDS::Wire(wi.Reversed()));  // reversed inner wire = hole
    return mf.Face();
}

TopoDS_Wire line_spine(const gp_Pnt& a, const gp_Pnt& b) {
    return BRepBuilderAPI_MakeWire(BRepBuilderAPI_MakeEdge(a, b));
}

}  // namespace

// --- 1. The canonical sweep: a circle chased along a straight line is a cylinder.
//        Volume must equal pi r^2 L exactly (the proof the shell caps into a solid).
TEST_CASE("circle profile swept along a straight line -> exact cylinder") {
    const double r = 2.0, L = 10.0;
    TopoDS_Face prof = circle_face(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1), r);
    TopoDS_Wire spine = line_spine(gp_Pnt(0, 0, 0), gp_Pnt(0, 0, L));

    TopoDS_Shape solid = geom::sweep_profile(prof, spine, /*follow_path=*/0);
    REQUIRE_FALSE(solid.IsNull());
    CHECK(solid_volume(solid) == doctest::Approx(kPi * r * r * L).epsilon(1e-3));
}

// --- 2. A washer profile must sweep into a HOLLOW tube (swept hole carved out) —
//        the load-bearing requirement for pipes and threaded sections.
TEST_CASE("washer profile swept along a line -> hollow tube of annular volume") {
    const double R = 5.0, r = 2.0, L = 10.0;
    TopoDS_Face prof = washer_face(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1), R, r);
    TopoDS_Wire spine = line_spine(gp_Pnt(0, 0, 0), gp_Pnt(0, 0, L));

    TopoDS_Shape solid = geom::sweep_profile(prof, spine, 0);
    REQUIRE_FALSE(solid.IsNull());
    CHECK(solid_volume(solid) == doctest::Approx(kPi * (R * R - r * r) * L).epsilon(2e-3));
}

// --- 3. THE PAYOFF: a small circular profile chased along a real 3-turn helix
//        closes into a valid solid (a coil / thread blank). Volume ~ section area
//        times the helix arc length; curvature (a << R) keeps the error tiny.
TEST_CASE("circle profile swept along a 3-turn helix -> valid coil solid") {
    const double pitch = 4.0, height = 12.0, hr = 8.0, a = 1.0;  // a = profile radius
    TopoDS_Wire helix = geom::make_helix(pitch, height, hr, 0.0, 0.0, /*lefthand=*/false);
    REQUIRE_FALSE(helix.IsNull());

    // Profile sits at the helix start, perpendicular to the start tangent.
    TopExp_Explorer ex(helix, TopAbs_EDGE);
    REQUIRE(ex.More());
    BRepAdaptor_Curve crv(TopoDS::Edge(ex.Current()));
    gp_Pnt p0; gp_Vec t0;
    crv.D1(crv.FirstParameter(), p0, t0);
    REQUIRE(t0.Magnitude() > 1e-9);
    TopoDS_Face prof = circle_face(p0, gp_Dir(t0), a);

    TopoDS_Shape solid = geom::sweep_profile(prof, helix, /*follow_path=*/0);
    REQUIRE_FALSE(solid.IsNull());

    const double turns = height / pitch;
    const double arc = turns * std::sqrt((2.0 * kPi * hr) * (2.0 * kPi * hr) + pitch * pitch);
    const double expect = kPi * a * a * arc;
    // 8% band: curvature + the planar end caps perturb the ideal tube volume.
    CHECK(solid_volume(solid) == doctest::Approx(expect).epsilon(0.08));
}

// --- 4. Sharp-cornered polyline path (an L-elbow): the RoundCorner transition must
//        keep the shell valid where two perpendicular segments meet.
TEST_CASE("circle profile swept along an L-shaped path -> valid elbow") {
    TopoDS_Wire spine = BRepBuilderAPI_MakeWire(
        BRepBuilderAPI_MakeEdge(gp_Pnt(0, 0, 0), gp_Pnt(0, 0, 10)),
        BRepBuilderAPI_MakeEdge(gp_Pnt(0, 0, 10), gp_Pnt(0, 10, 10)));
    TopoDS_Face prof = circle_face(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1), 1.0);

    TopoDS_Shape solid = geom::sweep_profile(prof, spine, 0);
    REQUIRE_FALSE(solid.IsNull());
    CHECK(solid_volume(solid) > 0.0);
}

// --- 5. Degenerate inputs return a null shape (not a crash) so the document layer
//        can raise a clean error.
TEST_CASE("sweep with a null profile or null spine fails gracefully") {
    TopoDS_Wire spine = line_spine(gp_Pnt(0, 0, 0), gp_Pnt(0, 0, 5));
    CHECK(geom::sweep_profile(TopoDS_Face(), spine, 0).IsNull());
    CHECK(geom::sweep_profile(circle_face(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1), 1.0),
                              TopoDS_Wire(), 0).IsNull());
}

// --- 6. INERTIA SANITY (Bug H1 symptom guard). The H1 bug left a sweep boss as a SECOND
//        disjoint body fused with the running body only at the document layer; the broken
//        path produced a degenerate solid whose principal MOMENT OF INERTIA went NEGATIVE
//        (an impossible result that flagged the non-solid). A correct single swept solid
//        must have STRICTLY POSITIVE principal moments of inertia about its centroid —
//        assert that directly on the kernel solid the document layer fuses.
TEST_CASE("swept solid has strictly positive principal moments of inertia") {
    const double r = 2.0, L = 10.0;
    TopoDS_Face prof = circle_face(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1), r);
    TopoDS_Wire spine = line_spine(gp_Pnt(0, 0, 0), gp_Pnt(0, 0, L));
    TopoDS_Shape solid = geom::sweep_profile(prof, spine, 0);
    REQUIRE_FALSE(solid.IsNull());

    GProp_GProps props;
    BRepGProp::VolumeProperties(solid, props);
    GProp_PrincipalProps pp = props.PrincipalProperties();
    Standard_Real i1, i2, i3;
    pp.Moments(i1, i2, i3);
    CHECK(i1 > 0.0);
    CHECK(i2 > 0.0);
    CHECK(i3 > 0.0);
}

TEST_CASE("make_helix rejects degenerate parameters") {
    CHECK(geom::make_helix(0.0, 10.0, 5.0, 0.0, 0.0, false).IsNull());
    CHECK(geom::make_helix(2.0, 0.0, 5.0, 0.0, 0.0, false).IsNull());
    CHECK(geom::make_helix(2.0, 10.0, 0.0, 0.0, 0.0, false).IsNull());
    // 90° cone angle makes cos(cone_angle)==0 in the arc-length formula.
    CHECK(geom::make_helix(2.0, 10.0, 5.0, 90.0, 0.0, false).IsNull());
}
