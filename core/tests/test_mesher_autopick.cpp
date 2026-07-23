// test_mesher_autopick — regression + feature coverage for the post-gmsh mesher overhaul
// (sim/mesher.cpp, sim/hex_mesher.cpp). Builds real OCCT solids, writes them to STEP (the
// same harness pattern as test_netgen_mesh), then drives mesh_step / detect_box_geometry to
// assert:
//   • structured-hex (mesh_box_hex) gracefully falls back to tet on a NON-box part instead of
//     hard-failing (the #1 regression from ripping gmsh out), and the strict failure is still
//     reachable with fallback_to_tet=false;
//   • Simple-mode auto-pick chooses hex8 for an axis-aligned box, tet for a cylinder, and a
//     layered tet mesh for a thin-wall part (enough elements through the thin dimension);
//   • quad_surface (Netgen quad-dominated surface) keeps a valid tetrahedral VOLUME mesh
//     (the quad->tet transition pyramids are split to tets, not dropped);
//   • detect_box_geometry classifies boxes vs. cylinders vs. rotated boxes correctly.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <cmath>
#include <filesystem>
#include <set>
#include <string>
#include <vector>

#include "sim/hex_mesher.h"
#include "sim/mesher.h"

#include <BRepBuilderAPI_Transform.hxx>
#include <BRepPrimAPI_MakeBox.hxx>
#include <BRepPrimAPI_MakeCylinder.hxx>
#include <IFSelect_ReturnStatus.hxx>
#include <STEPControl_Writer.hxx>
#include <TopoDS_Shape.hxx>
#include <gp_Ax1.hxx>
#include <gp_Dir.hxx>
#include <gp_Pnt.hxx>
#include <gp_Trsf.hxx>

using namespace chudware::sim;

namespace {

// 45 degrees in radians (the codebase avoids M_PI, which isn't guaranteed under strict ISO).
constexpr double kQuarterTurn = 0.7853981633974483;

std::string write_step(const TopoDS_Shape& shape, const std::string& name) {
    auto p = std::filesystem::temp_directory_path() / name;
    STEPControl_Writer wr;
    REQUIRE(wr.Transfer(shape, STEPControl_AsIs) == IFSelect_RetDone);
    REQUIRE(wr.Write(p.string().c_str()) == IFSelect_RetDone);
    return p.string();
}

std::string write_box(double lx, double ly, double lz, const std::string& name) {
    TopoDS_Shape box = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), lx, ly, lz).Shape();
    return write_step(box, name);
}

// An axis-aligned box rotated 45 degrees about Z: still 6 planar faces, but its actual volume
// no longer matches its (larger) axis-aligned bbox volume — so it is NOT a mappable hex box.
std::string write_rotated_box(double lx, double ly, double lz, const std::string& name) {
    TopoDS_Shape box = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), lx, ly, lz).Shape();
    gp_Trsf rot;
    rot.SetRotation(gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), kQuarterTurn);
    TopoDS_Shape rotated = BRepBuilderAPI_Transform(box, rot, true).Shape();
    return write_step(rotated, name);
}

std::string write_cylinder(double r, double h, const std::string& name) {
    TopoDS_Shape cyl = BRepPrimAPI_MakeCylinder(r, h).Shape();
    return write_step(cyl, name);
}

// Count distinct through-thickness (Z) node layers by clustering node z-coordinates onto a
// coarse grid (bin = bin_mm). A single-element-thick mesh yields only the two boundary layers;
// a properly layered thin-wall mesh yields several.
int count_z_layers(const std::vector<double>& points, double bin_mm) {
    std::set<long long> layers;
    for (size_t i = 0; i + 2 < points.size(); i += 3)
        layers.insert(static_cast<long long>(std::llround(points[i + 2] / bin_mm)));
    return static_cast<int>(layers.size());
}

}  // namespace

TEST_CASE("structured hex falls back to tet on non-box geometry") {
    const std::string step = write_rotated_box(40.0, 24.0, 16.0, "chudware_ap_rotbox.step");

    // fallback_to_tet (the new default) -> a clean tet mesh + an explanatory note, NOT a hard
    // failure and NOT a bogus hex on the rotated solid.
    MeshOptions o;
    o.structured = true;
    o.fallback_to_tet = true;
    o.order = 1;
    MeshOut m = mesh_step(step, o);
    REQUIRE_MESSAGE(m.ok, m.error);
    CHECK(m.nodes_per_elem == 4);
    CHECK(m.n_elems > 0);
    CHECK(m.note.find("fallback") != std::string::npos);
    CHECK(m.mesh_type_used.find("fallback") != std::string::npos);

    // Explicitly disabling the fallback restores the strict box failure with the box message.
    MeshOptions strict = o;
    strict.fallback_to_tet = false;
    MeshOut bad = mesh_step(step, strict);
    CHECK_FALSE(bad.ok);
    CHECK(bad.error.find("axis-aligned box") != std::string::npos);
}

TEST_CASE("auto_type picks hex8 for an axis-aligned box") {
    const std::string step = write_box(50.0, 30.0, 20.0, "chudware_ap_box.step");

    MeshOptions o;
    o.auto_type = true;
    o.order = 1;
    MeshOut m = mesh_step(step, o);
    REQUIRE_MESSAGE(m.ok, m.error);
    CHECK(m.nodes_per_elem == 8);
    CHECK(m.mesh_type_used.find("hex") != std::string::npos);
    CHECK(m.note.find("axis-aligned box") != std::string::npos);
    // A regular hex grid is orthogonal: perfect worst Jacobian, no poor elements.
    CHECK(m.jacobian_min == doctest::Approx(1.0));
    CHECK(m.pct_poor == doctest::Approx(0.0));
}

TEST_CASE("auto_type picks tet for a non-box solid (cylinder)") {
    const std::string step = write_cylinder(10.0, 40.0, "chudware_ap_cyl.step");

    MeshOptions o;
    o.auto_type = true;
    o.order = 1;
    MeshOut m = mesh_step(step, o);
    REQUIRE_MESSAGE(m.ok, m.error);
    CHECK(m.nodes_per_elem == 4);
    CHECK(m.mesh_type_used.find("tet") != std::string::npos);
    CHECK(m.note.find("auto-pick") != std::string::npos);
    CHECK(m.n_elems > 0);
    CHECK(m.jacobian_min > 0.0);  // no inverted tets
}

TEST_CASE("auto_type thin-wall sizes enough tet layers through the thickness") {
    // A thin disc (round plate) is a NON-box thin-wall part: the min dimension (2mm thickness)
    // is a small fraction of the bbox diagonal, so auto-pick routes it to layered tet meshing.
    // (A perfect axis-aligned box would instead — and correctly — mesh as structured hex.)
    const std::string step = write_cylinder(10.0, 2.0, "chudware_ap_disc.step");

    MeshOptions o;
    o.auto_type = true;
    o.thin_wall_layers = 4.0;
    o.order = 1;
    MeshOut m = mesh_step(step, o);
    REQUIRE_MESSAGE(m.ok, m.error);
    CHECK(m.nodes_per_elem == 4);
    CHECK(m.note.find("thin-wall") != std::string::npos);
    // Multiple node layers through the 2mm thickness (a single-element-thick mesh would show
    // only the two boundary layers). Bin at 0.25mm; ~4 element layers => ~5-9 node layers.
    const int z_layers = count_z_layers(m.points, 0.25);
    MESSAGE("through-thickness z-layers = " << z_layers);
    CHECK(z_layers >= 3);
    CHECK(m.aspect_avg < 15.0);  // not degenerate despite the thin geometry
}

TEST_CASE("quad_surface keeps a valid tetrahedral volume mesh") {
    const std::string step = write_box(40.0, 20.0, 10.0, "chudware_ap_quad.step");

    MeshOptions o;
    o.quad_surface = true;
    o.order = 1;
    MeshOut m = mesh_step(step, o);
    REQUIRE_MESSAGE(m.ok, m.error);
    CHECK(m.nodes_per_elem == 4);  // the VOLUME stays tetrahedral
    CHECK(m.n_elems > 0);
    CHECK(m.mesh_type_used.find("quad-surface") != std::string::npos);
    // quad_surface is best-effort: the mesher requests a quad-dominated surface and splits any
    // Netgen pyramid/prism transition elements into tets. nglib 6.2.2504 cannot close a
    // quad-dominated VOLUME on a box ("open quads"), so the mesher degrades to a valid tet
    // volume and records that in mesh_type_used ("...quad-surface[-fallback]"). Either way the
    // VOLUME stays tetrahedral and valid (no inverted elements) — that is the contract.
    CHECK(m.jacobian_min > 0.0);
    CHECK(m.pct_poor < 50.0);
}

TEST_CASE("detect_box_geometry classifies boxes and non-boxes") {
    const std::string box = write_box(30.0, 20.0, 10.0, "chudware_ap_detbox.step");
    BoxGeomInfo bi = detect_box_geometry(box);
    CHECK(bi.is_box);
    CHECK(bi.Lx == doctest::Approx(30.0));
    CHECK(bi.Ly == doctest::Approx(20.0));
    CHECK(bi.Lz == doctest::Approx(10.0));
    CHECK(bi.error.empty());

    const std::string cyl = write_cylinder(10.0, 20.0, "chudware_ap_detcyl.step");
    BoxGeomInfo ci = detect_box_geometry(cyl);
    CHECK_FALSE(ci.is_box);
    CHECK_FALSE(ci.error.empty());

    const std::string rot = write_rotated_box(30.0, 20.0, 10.0, "chudware_ap_detrot.step");
    BoxGeomInfo ri = detect_box_geometry(rot);
    CHECK_FALSE(ri.is_box);  // volume != axis-aligned bbox volume
    CHECK_FALSE(ri.error.empty());
}
