// Regression suite for geom::surf_replace_face_solid.
//
// Found during a surfaces audit:
// surf_replace_face_solid BOP-splits the solid by the replacement surface and bails
// with "could not heal" unless that split yields >=2 positive-volume pieces. A
// replacement genuinely cutting through the solid works; the intuitive pick — a
// patch coincident with (or smaller than) the target face — does not, because a
// tool face that merely touches the solid's boundary can't divide it. These tests
// pin both the still-working cut-through case and the newly-healed coincident case.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <cmath>
#include <vector>

#include <BRepBuilderAPI_MakeFace.hxx>
#include <BRepGProp.hxx>
#include <BRepPrimAPI_MakeBox.hxx>
#include <GProp_GProps.hxx>
#include <TopoDS.hxx>
#include <TopoDS_Face.hxx>
#include <TopoDS_Shape.hxx>
#include <gp_Dir.hxx>
#include <gp_Pln.hxx>
#include <gp_Pnt.hxx>

#include "geom/surfacing.h"

namespace {

double solid_volume(const TopoDS_Shape& s) {
    GProp_GProps props;
    BRepGProp::VolumeProperties(s, props);
    return props.Mass();
}

// A generously oversized planar patch lying in the z=`z` plane, well beyond the
// 10x10x10 test box's footprint so it always spans it.
TopoDS_Face big_patch_at_z(double z) {
    gp_Pln plane(gp_Pnt(0, 0, z), gp_Dir(0, 0, 1));
    return TopoDS::Face(BRepBuilderAPI_MakeFace(plane, -20, 20, -20, 20).Face());
}

}  // namespace

// --- 1. A replacement that genuinely cuts through the solid must still heal (the
//        previously-working case: the kept piece is the side NOT carrying the
//        original target face, i.e. the box body below the cutting plane).
TEST_CASE("surf_replace_face_solid heals when the replacement cuts through the solid") {
    const double L = 10.0;
    TopoDS_Shape box = BRepPrimAPI_MakeBox(L, L, L).Shape();
    const double diag = std::sqrt(3.0) * L;
    std::vector<Eigen::Vector3d> target_centroids{Eigen::Vector3d(L / 2, L / 2, L)};  // top face

    TopoDS_Face cutter = big_patch_at_z(L / 2);  // slices the box in half
    TopoDS_Shape healed = geom::surf_replace_face_solid(box, target_centroids, cutter, diag);

    REQUIRE_FALSE(healed.IsNull());
    CHECK(solid_volume(healed) == doctest::Approx(L * L * (L / 2)).epsilon(1e-6));
}

// --- 2. The intuitive pick: a replacement patch coincident with the target face
//        (same plane, same footprint). This used to return a null shape ("could not
//        heal"); it must now heal into (practically) the original box.
TEST_CASE("surf_replace_face_solid heals a replacement coincident with the target face") {
    const double L = 10.0;
    TopoDS_Shape box = BRepPrimAPI_MakeBox(L, L, L).Shape();
    const double diag = std::sqrt(3.0) * L;
    std::vector<Eigen::Vector3d> target_centroids{Eigen::Vector3d(L / 2, L / 2, L)};  // top face

    TopoDS_Face coincident = big_patch_at_z(L);  // same plane as the top face, not cutting through
    TopoDS_Shape healed = geom::surf_replace_face_solid(box, target_centroids, coincident, diag);

    REQUIRE_FALSE(healed.IsNull());
    CHECK(solid_volume(healed) == doctest::Approx(L * L * L).epsilon(1e-3));
}

// --- 3. A replacement smaller than the target face's own footprint (still coincident)
//        must also heal — its finite trim alone wouldn't reach across the solid.
TEST_CASE("surf_replace_face_solid heals a replacement smaller than the target face") {
    const double L = 10.0;
    TopoDS_Shape box = BRepPrimAPI_MakeBox(L, L, L).Shape();
    const double diag = std::sqrt(3.0) * L;
    std::vector<Eigen::Vector3d> target_centroids{Eigen::Vector3d(L / 2, L / 2, L)};  // top face

    gp_Pln plane(gp_Pnt(0, 0, L), gp_Dir(0, 0, 1));
    TopoDS_Face small_patch =
        TopoDS::Face(BRepBuilderAPI_MakeFace(plane, 1.0, L - 1.0, 1.0, L - 1.0).Face());
    TopoDS_Shape healed = geom::surf_replace_face_solid(box, target_centroids, small_patch, diag);

    REQUIRE_FALSE(healed.IsNull());
    CHECK(solid_volume(healed) == doctest::Approx(L * L * L).epsilon(1e-3));
}
