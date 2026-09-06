// Polyhedral-FEA quality regression on a curved STEP body. It pins positive
// Jacobians, bounded shape metrics, and meaningful circumferential resolution
// at product defaults.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <cmath>
#include <filesystem>
#include <limits>
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
TEST_CASE("cylinder meshes evenly with healthy boundary resolution") {
  const std::string step = write_cylinder(R, H, "chudware_mq_cyl.step");

  MeshOptions o;
  o.fineness = 0.5;
  o.order = 1;
  MeshOut m = mesh_step(step, o);
  REQUIRE_MESSAGE(m.ok, m.error);
  CHECK(m.nodes_per_elem == 4);

  // A non-trivial tet count — a "rough" few-element mesh would be orders
  // smaller (a ~20x40 cylinder at 1.4mm elements is tens of thousands of tets;
  // 500 is a floor with huge margin).
  CHECK(m.n_elems > 500);
  CHECK(m.n_nodes > 0);
  CHECK(m.tets.size() == static_cast<size_t>(m.n_elems) * 4);

  // No inverted / degenerate elements; real positive volumes everywhere.
  CHECK(m.jacobian_min > 0.0);
  CHECK(m.min_tet_volume_mm3 > 0.0);

  CHECK(std::isfinite(m.aspect_max));
  CHECK(m.aspect_avg >= 1.0);
  CHECK(m.aspect_avg < 4.0);
  CHECK(m.aspect_max < 20.0);
  CHECK(m.pct_poor < 5.0);

  const int sectors = circumferential_sectors(m.points, R, 36);
  MESSAGE("circumferential sectors filled (of 36) = " << sectors);
  CHECK(sectors >= 24);
}

TEST_CASE("finer sizing increases curved-boundary resolution") {
  const std::string step = write_cylinder(R, H, "chudware_mq_cyl_fine.step");

  MeshOptions coarse;
  coarse.fineness = 0.0;
  coarse.order = 1;
  MeshOptions fine = coarse;
  fine.fineness = 0.8;

  MeshOut coarse_mesh = mesh_step(step, coarse);
  MeshOut fine_mesh = mesh_step(step, fine);
  REQUIRE_MESSAGE(coarse_mesh.ok, coarse_mesh.error);
  REQUIRE_MESSAGE(fine_mesh.ok, fine_mesh.error);

  const int coarse_sectors = circumferential_sectors(coarse_mesh.points, R, 72);
  const int fine_sectors = circumferential_sectors(fine_mesh.points, R, 72);
  CHECK(fine_sectors > coarse_sectors);
  CHECK(fine_mesh.n_elems > coarse_mesh.n_elems);
  CHECK(coarse_mesh.jacobian_min > 0.0);
  CHECK(fine_mesh.jacobian_min > 0.0);
}

TEST_CASE("invalid mesh controls fail before STEP parsing") {
    MeshOptions options;
    options.fineness = std::numeric_limits<double>::quiet_NaN();
    MeshOut result = mesh_step("/path/that/must/not/be/read.step", options);
    CHECK_FALSE(result.ok);
    CHECK(result.error.find("fineness") != std::string::npos);

    options = MeshOptions{};
    options.order = 3;
    result = mesh_step("/path/that/must/not/be/read.step", options);
    CHECK_FALSE(result.ok);
    CHECK(result.error.find("order") != std::string::npos);

    options = MeshOptions{};
    options.min_size_mm = 2.0;
    options.max_size_mm = 1.0;
    std::string error;
    CHECK_FALSE(validate_mesh_options(options, error));
    CHECK(error.find("min_size_mm") != std::string::npos);
}

// ── The hanging-fragment dissolve may not delete the domain ────────────────────────────────
// detail::repair_lonely_nodes drops face-connected fragments that touch the rest of the mesh
// at two corners or fewer, because such a fragment rotates about that node or edge at zero
// strain energy. Two bodies fused at a single NODE are two such fragments — each hangs off
// the other by exactly one corner — and the "the fragment IS the mesh" carve-out spares
// neither, so the unguarded rule marked EVERY element and returned an empty mesh: a
// diagnosable mechanism traded for a mesh nobody can solve or read. The repair must refuse,
// leave the mesh byte-identical, and report the mechanism it did not repair.
TEST_CASE("the hanging-fragment dissolve refuses to delete the whole mesh") {
    MeshOut mesh;
    mesh.nodes_per_elem = 4;
    // Two unit tets sharing exactly ONE node (index 3), and nothing else.
    mesh.points = {0.0, 0.0, 0.0,  1.0, 0.0, 0.0,  0.0, 1.0, 0.0,  0.0, 0.0, 1.0,
                   1.0, 0.0, 2.0,  0.0, 1.0, 2.0,  0.0, 0.0, 2.0};
    mesh.tets = {0, 1, 2, 3,  3, 4, 5, 6};
    mesh.n_nodes = 7;
    mesh.n_elems = 2;
    const std::vector<int> before = mesh.tets;

    const int dropped = detail::repair_lonely_nodes(mesh);
    CHECK(dropped == 0);
    CHECK(mesh.tets == before);              // the mesh is returned untouched
    CHECK(mesh.n_elems == 2);
    CHECK(mesh.hanging_elems_dropped == 0);
    CHECK(mesh.dropped_volume_mm3 == 0.0);
    // …and the mechanism is REPORTED, which is what makes the solve refuse and name it.
    CHECK(mesh.lonely_nodes_remaining > 0);
    CHECK(mesh.note.find("dissolve refused") != std::string::npos);
}
