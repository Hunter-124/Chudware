// test_mesh_quality — proves the Netgen/nglib tet mesher (sim/mesher.cpp) produces an EVEN,
// well-graded mesh on a curved body. The user report was "extremely rough / uneven mesh for
// cylinders"; this pins the fix (lower grading, stronger curvature refinement, more optimizer
// passes) with measurable thresholds so a regression that re-roughens a cylinder fails here.
//
// Same harness pattern as test_netgen_mesh / test_mesher_autopick: build a REAL OCCT solid
// (BRepPrimAPI_MakeCylinder), write it to STEP, drive mesh_step, and assert on the quality
// metrics MeshOut already reports (aspect_min/avg/max, jacobian_min, pct_poor) plus a derived
// circumferential-resolution count taken from the node cloud.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <cmath>
#include <filesystem>
#include <string>
#include <vector>

#include "sim/mesher.h"

#include <BRepPrimAPI_MakeCylinder.hxx>
#include <IFSelect_ReturnStatus.hxx>
#include <STEPControl_Writer.hxx>
#include <TopoDS_Shape.hxx>

using namespace chudware::sim;

namespace {

// Cylinder geometry (mm): axis = global Z, base circle centred on the origin (the default
// BRepPrimAPI_MakeCylinder frame), radius R, height H.
constexpr double R = 10.0, H = 40.0;
constexpr double kTwoPi = 6.283185307179586;

std::string write_cylinder(double r, double h, const std::string& name) {
    TopoDS_Shape cyl = BRepPrimAPI_MakeCylinder(r, h).Shape();
    auto p = std::filesystem::temp_directory_path() / name;
    STEPControl_Writer wr;
    REQUIRE(wr.Transfer(cyl, STEPControl_AsIs) == IFSelect_RetDone);
    REQUIRE(wr.Write(p.string().c_str()) == IFSelect_RetDone);
    return p.string();
}

// Count how many of `n_sectors` angular sectors around the cylinder axis (Z) contain at least
// one node on/near the curved wall (radius >= 0.9*r). A faceted "rough" cylinder fills only a
// handful of sectors (one per facet); an even, well-resolved wall fills nearly all of them. The
// axis passes through (0,0), so a node's circumferential angle is atan2(y, x).
int circumferential_sectors(const std::vector<double>& pts, double r, int n_sectors) {
    std::vector<char> hit(static_cast<size_t>(n_sectors), 0);
    const double wall = 0.9 * r;
    for (size_t i = 0; i + 2 < pts.size(); i += 3) {
        const double x = pts[i], y = pts[i + 1];
        const double rad = std::sqrt(x * x + y * y);
        if (rad < wall) continue;  // skip interior / near-axis nodes (angle ill-defined)
        double a = std::atan2(y, x);   // (-pi, pi]
        if (a < 0.0) a += kTwoPi;      // [0, 2pi)
        int b = static_cast<int>(a / kTwoPi * n_sectors);
        if (b < 0) b = 0;
        if (b >= n_sectors) b = n_sectors - 1;
        hit[static_cast<size_t>(b)] = 1;
    }
    int c = 0;
    for (char h : hit) c += h ? 1 : 0;
    return c;
}

}  // namespace

// ── Headline: a cylinder meshes EVENLY at the product-default settings ─────────────────────
TEST_CASE("cylinder meshes evenly with healthy curvature resolution") {
    const std::string step = write_cylinder(R, H, "chudware_mq_cyl.step");

    MeshOptions o;
    o.fineness = 0.5;     // the product default
    o.order = 1;
    o.curvature = true;   // default curv_elems_per_circle (20) -> smooth, even wall
    o.optimize = true;    // surface + volume optimizers on (the quality passes)
    MeshOut m = mesh_step(step, o);
    REQUIRE_MESSAGE(m.ok, m.error);
    CHECK(m.nodes_per_elem == 4);

    // A non-trivial tet count — a "rough" few-element mesh would be orders smaller (a ~20x40
    // cylinder at 1.4mm elements is tens of thousands of tets; 500 is a floor with huge margin).
    CHECK(m.n_elems > 500);
    CHECK(m.n_nodes > 0);
    CHECK(m.tets.size() == static_cast<size_t>(m.n_elems) * 4);

    // No inverted / degenerate elements; real positive volumes everywhere.
    CHECK(m.jacobian_min > 0.0);
    CHECK(m.min_tet_volume_mm3 > 0.0);

    // EVENNESS — the metrics the grading / curvature / optimizer changes drive:
    //  • aspect_avg near 1 => the BULK of elements are well-shaped (even);
    //  • aspect_max bounded => no gross slivers survive the optimizer;
    //  • pct_poor low => almost nothing above the SolidWorks-style poor threshold (aspect>10).
    CHECK(std::isfinite(m.aspect_max));
    CHECK(m.aspect_avg >= 1.0);
    CHECK(m.aspect_avg < 4.0);
    CHECK(m.aspect_max < 20.0);
    CHECK(m.pct_poor < 5.0);

    // Curvature resolution: the wall is sampled at many distinct angles (smooth), not a coarse
    // polygon. r=10 at fineness 0.5 yields ~40 segments around; require >=24 of 36 sectors
    // populated (~every 15deg) — comfortably past a faceted 8/12-gon, well within ~40 actual.
    const int sectors = circumferential_sectors(m.points, R, 36);
    MESSAGE("circumferential sectors filled (of 36) = " << sectors);
    CHECK(sectors >= 24);
}

// ── Curvature refinement actually ENGAGES (the elementspercurve conversion is live) ─────────
TEST_CASE("curvature refinement increases circumferential resolution") {
    const std::string step = write_cylinder(R, H, "chudware_mq_cyl_curv.step");

    // At a COARSE base size the curvature control is the binding size constraint, so raising
    // curv_elems_per_circle MUST put more elements around the wall. This proves the curvature
    // path engages end-to-end (curv_elems_per_circle -> Netgen elementspercurve -> finer wall).
    MeshOptions lo;
    lo.fineness = 0.0;    // coarse base => curvature, not the base size, governs the wall
    lo.order = 1;
    lo.curvature = true;
    lo.curv_elems_per_circle = 6.0;

    MeshOptions hi = lo;
    hi.curv_elems_per_circle = 30.0;

    MeshOut m_lo = mesh_step(step, lo);
    MeshOut m_hi = mesh_step(step, hi);
    REQUIRE_MESSAGE(m_lo.ok, m_lo.error);
    REQUIRE_MESSAGE(m_hi.ok, m_hi.error);

    const int s_lo = circumferential_sectors(m_lo.points, R, 72);
    const int s_hi = circumferential_sectors(m_hi.points, R, 72);
    MESSAGE("coarse-curvature sectors = " << s_lo << ", fine-curvature sectors = " << s_hi);
    CHECK(s_hi > s_lo);                 // finer curvature -> more positions around the wall
    CHECK(m_hi.n_elems > m_lo.n_elems); // ...and more elements overall

    // Both remain valid regardless of the curvature setting (no inverted tets).
    CHECK(m_lo.jacobian_min > 0.0);
    CHECK(m_hi.jacobian_min > 0.0);
}
