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
#include <vector>

#include <BRepAdaptor_Curve.hxx>
#include <BRepCheck_Analyzer.hxx>
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

// --- 3b. GUIDE CURVE: a guide line that diverges from the straight spine scales the
//         circular profile as it travels, so the same circle+spine that makes a plain
//         cylinder instead sweeps into a truncated cone. The assertion is ANALYTIC:
//         the profile radius grows linearly r0 -> r1 (it rides the guide's border), so
//         the volume must equal the exact frustum (pi*L/3)(r0^2 + r0*r1 + r1^2). This
//         both proves the guide is consumed AND that it scales the section correctly.
TEST_CASE("guide curve scales the circular profile into an exact frustum") {
    const double r0 = 2.0, r1 = 4.0, L = 10.0;
    TopoDS_Face prof  = circle_face(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1), r0);
    TopoDS_Wire spine = line_spine(gp_Pnt(0, 0, 0), gp_Pnt(0, 0, L));

    const double cyl = solid_volume(geom::sweep_profile(prof, spine, 0));
    REQUIRE(cyl == doctest::Approx(kPi * r0 * r0 * L).epsilon(1e-3));  // un-guided baseline

    // Guide starts on the profile's rim (radius r0) and moves OUT to r1 over the same
    // length, so the swept section grows linearly from r0 to r1.
    std::vector<TopoDS_Wire> guides{line_spine(gp_Pnt(r0, 0, 0), gp_Pnt(r1, 0, L))};
    TopoDS_Shape guided = geom::sweep_profile(prof, spine, 0, guides);
    REQUIRE_FALSE(guided.IsNull());
    CHECK(BRepCheck_Analyzer(guided).IsValid());
    const double frustum = (kPi * L / 3.0) * (r0 * r0 + r0 * r1 + r1 * r1);
    CHECK(solid_volume(guided) == doctest::Approx(frustum).epsilon(0.02));
    CHECK(solid_volume(guided) > 2.0 * cyl);  // the guide clearly added material
}

// --- 3c. A guided sweep whose shell fails must NOT silently fall back to the plain
//         (guide-ignoring) MakePipe — a null profile with a guide stays null.
TEST_CASE("guided sweep does not fall back to an un-guided pipe on failure") {
    TopoDS_Wire spine = line_spine(gp_Pnt(0, 0, 0), gp_Pnt(0, 0, 5));
    std::vector<TopoDS_Wire> guides{line_spine(gp_Pnt(1, 0, 0), gp_Pnt(2, 0, 5))};
    CHECK(geom::sweep_profile(TopoDS_Face(), spine, 0, guides).IsNull());
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

// ---------------------------------------------------------------------------
// DEV HARNESS (temporary): drive geom::cut_thread directly on an EXAGGERATED
// coarse thread (steep ~45° helix) so profile/alignment/boolean errors that a
// shallow M8 hides become obvious. Run with CHUDWARE_THREAD_DEBUG=1 for a stage
// trace. Also asserts the two invariants this guards: the swept groove exists
// and the cut removes material to a VALID solid.
// ---------------------------------------------------------------------------
#include "geom/thread.h"
#include <BRepAdaptor_CompCurve.hxx>
#include <BRepPrimAPI_MakeCylinder.hxx>
#include <BRepBndLib.hxx>
#include <Bnd_Box.hxx>
#include <gp_Ax2.hxx>

TEST_CASE("HARNESS: coarse thread cut on a steep helix") {
    const double R = 10.0, H = 40.0;
    gp_Ax2 ax(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1));
    TopoDS_Shape cyl = BRepPrimAPI_MakeCylinder(ax, R, H).Shape();
    const double plain = solid_volume(cyl);
    MESSAGE("plain cylinder vol = " << plain);

    // Realistic + coarse pitches on a Ø20 rod (major=20). Real threads run pitch ~
    // major/8..major/16 (M20 coarse = 2.5); 4.0 is a coarse ACME-ish case.
    for (bool raised : {false, true}) {
        for (double pitch : {1.5, 2.5, 4.0}) {
            geom::ThreadResult tr = geom::cut_thread(
                cyl, gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1), R, pitch,
                /*major*/ 2 * R, /*length*/ H, /*starts*/ 1, /*lh*/ false,
                /*internal*/ false, raised, /*turns_cap*/ 0.0);
            const double v = solid_volume(tr.shape);
            MESSAGE((raised ? "RAISED" : "CUT  ") << " pitch=" << pitch
                    << " fell_back=" << tr.fell_back << " vol=" << v
                    << " (plain=" << plain << ") warn='" << tr.warning << "'");
            // Realistic fastener pitches (1.5, 2.5) MUST cut/raise a real solid. The
            // exaggerated 4.0 (a steep ACME-ish coarse case) is allowed to fall back
            // gracefully: at that lead angle the groove crosses the flat end rim as a
            // knife-edge that the un-trimmed cut boolean can choke on — the phase-2
            // overextend-then-trim-to-end-face pass is what makes that case robust.
            const bool exaggerated = pitch > 3.0;
            CHECK(BRepCheck_Analyzer(tr.shape).IsValid());
            if (tr.fell_back) {
                CHECK(exaggerated);      // only the exaggerated case may fall back
                CHECK(v == doctest::Approx(plain));  // fall-back returns the body untouched
            } else if (raised) {
                CHECK(v > plain + 1.0);
            } else {
                CHECK(v < plain - 1.0);
            }
        }
    }
}

// STOP-AT-OBSTRUCTION / LEAD-IN TRIM: the overextended coil is trimmed to a slab
// [start, start+length] CLAMPED to the body's own axial extent, so a `length` that
// overruns the material (a caller over-spec, or a thread that would run past a shoulder /
// bolt head) can never leave threads floating in air — it trims flush to the solid, and
// the top/bottom thread ends are flat-cut at the run boundary rather than tapering off.
TEST_CASE("HARNESS: over-long thread run is clamped to the body (no floating threads)") {
    const double R = 6.0, H = 20.0;
    gp_Ax2 ax(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1));
    TopoDS_Shape cyl = BRepPrimAPI_MakeCylinder(ax, R, H).Shape();
    const double plain = solid_volume(cyl);

    // A length 4x the body: without the body-extent clamp the coil would extend well past
    // the top face. The result must still be ONE valid solid, material actually removed,
    // and its bounding box must NOT grow past the cylinder's own [0, H] axial span.
    geom::ThreadResult tr = geom::cut_thread(
        cyl, gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1), R, /*pitch*/ 1.0, /*major*/ 2 * R,
        /*length*/ 4 * H, /*starts*/ 1, /*lh*/ false, /*internal*/ false,
        /*raised*/ false, /*turns_cap*/ 0.0);
    MESSAGE("clamped thread fell_back=" << tr.fell_back
            << " vol=" << solid_volume(tr.shape) << " (plain=" << plain << ")");
    CHECK_FALSE(tr.fell_back);
    CHECK(BRepCheck_Analyzer(tr.shape).IsValid());
    CHECK(solid_volume(tr.shape) < plain - 1.0);          // material was removed
    Bnd_Box bb; BRepBndLib::AddOptimal(tr.shape, bb);     // tight box (Add() pads by deflection)
    double xm, ym, zm, xM, yM, zM; bb.Get(xm, ym, zm, xM, yM, zM);
    MESSAGE("threaded bbox z = [" << zm << ", " << zM << "]");
    CHECK(zm > -1e-3);                                     // nothing below the base face
    CHECK(zM < H + 1e-3);                                  // nothing floating past the top
}

// INVARIANT: make_helix is exactly concentric with its axis — every sampled point sits at
// `radius` from the axis (the expected "helix aligned to the cylinder's circular profile").
TEST_CASE("HARNESS: helix is concentric with the cylinder axis") {
    const double R = 10.0, pitch = 4.0, height = 40.0;
    TopoDS_Wire hl = geom::make_helix(pitch, height, R, 0.0, 0.0, false);
    REQUIRE_FALSE(hl.IsNull());
    BRepAdaptor_CompCurve cc(hl);
    const double t0 = cc.FirstParameter(), t1 = cc.LastParameter();
    double maxerr = 0.0, minz = 1e9, maxz = -1e9;
    for (int i = 0; i <= 40; ++i) {
        gp_Pnt p = cc.Value(t0 + (t1 - t0) * i / 40.0);
        const double rr = std::sqrt(p.X() * p.X() + p.Y() * p.Y());
        maxerr = std::max(maxerr, std::abs(rr - R));
        minz = std::min(minz, p.Z()); maxz = std::max(maxz, p.Z());
    }
    MESSAGE("concentricity max |r-R| = " << maxerr << " mm; z span = " << (maxz - minz));
    CHECK(maxerr < 1e-6);
    CHECK(maxz - minz == doctest::Approx(height).epsilon(0.01));
}

// EXPORT HARNESS: write threaded parts to STEP + STL for a human to open and spin.
// Path via $THREAD_EXPORT_DIR (default scratch/threads). Set THREAD_EXPORT=1 to run.
#include <STEPControl_Writer.hxx>
#include <StlAPI_Writer.hxx>
#include <BRepMesh_IncrementalMesh.hxx>
#include <cstdlib>
#include <string>

TEST_CASE("EXPORT threaded parts (STEP+STL)") {
    if (!std::getenv("THREAD_EXPORT")) return;
    const char* d = std::getenv("THREAD_EXPORT_DIR");
    const std::string dir = d ? d : "scratch/threads";
    const double R = 10.0, H = 40.0;
    gp_Ax2 ax(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1));

    struct Case { const char* name; double pitch; bool raised; geom::ThreadForm form; };
    const Case cases[] = {
        {"v60_cut_m20x2.5",  2.5, false, geom::ThreadForm::V60},
        {"v60_cut_coarse_p4",4.0, false, geom::ThreadForm::V60},
        {"v60_raised_p4",    4.0, true,  geom::ThreadForm::V60},
        {"acme_cut_p4",      4.0, false, geom::ThreadForm::ACME},
        {"acme_raised_p4",   4.0, true,  geom::ThreadForm::ACME},
        {"square_cut_p4",    4.0, false, geom::ThreadForm::SQUARE},
    };
    for (const Case& c : cases) {
        TopoDS_Shape cyl = BRepPrimAPI_MakeCylinder(ax, R, H).Shape();
        geom::ThreadResult tr = geom::cut_thread(
            cyl, gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1), R, c.pitch, 2 * R, H, 1,
            false, false, c.raised, 0.0, c.form);
        MESSAGE(c.name << " fell_back=" << tr.fell_back << " vol=" << solid_volume(tr.shape));
        const std::string base = dir + "/" + c.name;
        { STEPControl_Writer w; w.Transfer(tr.shape, STEPControl_AsIs);
          w.Write((base + ".step").c_str()); }
        { BRepMesh_IncrementalMesh mesh(tr.shape, 0.1); mesh.Perform();
          StlAPI_Writer sw; sw.Write(tr.shape, (base + ".stl").c_str()); }
    }
}
