// Cyclic-sector regression: a 90 deg sector of a wheel disc must mesh and
// SOLVE at both element orders.
//
// Live defect (a wheel-disc mass study): the 90 deg sector of
// CD-210-01 (Ø508 disc, 3.175 mm web, Ø25.4 bore, one lightening window) was
// refused twice, at fineness 0.5 and 0.7, with
//   "polyfea: mesh contains unsupported tet4 connectivity;
//    Chudware studies currently require tet4 or tet10"
// on a valid watertight solid, so the disc's factor of safety was never measured.
//
// Three independent defects wore that one message, all invisible at order 1:
//   * LEFT-HANDED cells (negative signed volume) on the wedge's sharp radial
//     faces. Chudware's tet4 contract takes |det| everywhere, but
//     pm::fea::promote_to_quadratic admits an element only on a POSITIVE tet10
//     Jacobian, so it silently declined them and left them linear inside a
//     quadratic mesh — 33 of 467162 at fineness 0.5 on the full-size sector.
//   * FLAT cells the fill's own ship gate shipped (it can only relax INTERIOR
//     nodes, and in a thin web every node of a boundary sliver is a boundary
//     node): shape quality 7e-17, signed volume 2e-15 mm3.
//   * And behind those, once the mesh reached the solver: the hanging-fragment
//     rule read a 102750-element body as a hanging fragment, because "touches
//     the rest of the mesh at two corners or fewer" is symmetric and the only
//     corners the body shared outside itself were two isolated tets' (see
//     Fragments::is_body in mesh_quality.cpp).
// mesher.cpp now relabels the first and inflates the second, both reported on
// MeshOut (reoriented_elems / degenerate_elems_repaired) — never a refusal.
//
// The witness here is the same geometry at quarter radii, which carries all
// three at a size a gate can afford. Set CHUDWARE_MESH_SECTOR_FULL=1 to add the
// full-size sector at fineness 0.5 (about 11 minutes, ~0.5M elements).
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <BRepAlgoAPI_Cut.hxx>
#include <BRepPrimAPI_MakeCylinder.hxx>
#include <IFSelect_ReturnStatus.hxx>
#include <STEPControl_Writer.hxx>
#include <gp_Ax2.hxx>
#include <gp_Dir.hxx>
#include <gp_Pnt.hxx>

#include <array>
#include <cmath>
#include <cstdlib>
#include <filesystem>
#include <memory>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

#include "sim/fea_run.h"
#include "sim/mesher.h"

using namespace chudware::sim;
using nlohmann::json;

namespace {

// The real sector, from CD-210-01A_Slice_Light_90deg_Sector: an annular
// sector spanning 22.5..112.5 deg between the Ø25.4 bore and the Ø508 rim, web
// 3.175 mm, with the 45..90 deg lightening window (R101.6..R203.2) cut through.
// `scale` shrinks the radii only — the 3.175 mm web is what makes the aspect
// ratio hard, and it is kept.
struct Sector {
  double rim = 254.0;
  double bore = 12.7;
  double window_inner = 101.6;
  double window_outer = 203.2;
  double thickness = 3.175;

  explicit Sector(double scale)
      : rim(254.0 * scale), bore(12.7 * scale), window_inner(101.6 * scale),
        window_outer(203.2 * scale) {}
};

constexpr double kSectorStartDeg = 22.5;
constexpr double kSectorSweepDeg = 90.0;
constexpr double kWindowStartDeg = 45.0;
constexpr double kWindowSweepDeg = 45.0;

double radians(double degrees) { return degrees * M_PI / 180.0; }

gp_Ax2 wedge_axis(double start_deg) {
  return gp_Ax2(gp_Pnt(0, 0, -1.0), gp_Dir(0, 0, 1),
                gp_Dir(std::cos(radians(start_deg)),
                       std::sin(radians(start_deg)), 0.0));
}

std::string write_sector_step(const Sector &sector, const char *name) {
  const double tall = sector.thickness + 2.0;
  const TopoDS_Shape plate =
      BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1),
                                      gp_Dir(std::cos(radians(kSectorStartDeg)),
                                             std::sin(radians(kSectorStartDeg)),
                                             0.0)),
                               sector.rim, sector.thickness,
                               radians(kSectorSweepDeg))
          .Shape();
  const TopoDS_Shape bore =
      BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0, 0, -1.0), gp_Dir(0, 0, 1)),
                               sector.bore, tall)
          .Shape();
  const TopoDS_Shape window_outer =
      BRepPrimAPI_MakeCylinder(wedge_axis(kWindowStartDeg), sector.window_outer,
                               tall, radians(kWindowSweepDeg))
          .Shape();
  const TopoDS_Shape window_inner =
      BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0, 0, -1.0), gp_Dir(0, 0, 1)),
                               sector.window_inner, tall)
          .Shape();
  const TopoDS_Shape window =
      BRepAlgoAPI_Cut(window_outer, window_inner).Shape();
  const TopoDS_Shape solid =
      BRepAlgoAPI_Cut(BRepAlgoAPI_Cut(plate, bore).Shape(), window).Shape();

  const auto path = std::filesystem::temp_directory_path() / name;
  STEPControl_Writer writer;
  REQUIRE(writer.Transfer(solid, STEPControl_AsIs) == IFSelect_RetDone);
  REQUIRE(writer.Write(path.string().c_str()) == IFSelect_RetDone);
  return path.string();
}

// Points on a cylindrical band of the sector, dense enough for the geometric
// faceset pick to land on every node of that face at these element sizes.
json band_samples(const Sector &sector, double radius) {
  json points = json::array();
  for (int a = 0; a <= 24; ++a) {
    const double theta = radians(kSectorStartDeg + kSectorSweepDeg * a / 24.0);
    for (int k = 0; k <= 3; ++k)
      points.push_back({radius * std::cos(theta), radius * std::sin(theta),
                        sector.thickness * k / 3.0});
  }
  return points;
}

// Signed 6V of a tet in the mesher's own node order. Negative means the cell's
// node ordering is left-handed — exactly what the quadratic promotion declines.
double signed_volume6(const std::vector<double> &points, const int *element) {
  const auto node = [&](int local) {
    const size_t base = static_cast<size_t>(element[local]) * 3;
    return std::array<double, 3>{points[base], points[base + 1],
                                 points[base + 2]};
  };
  const auto a = node(0), b = node(1), c = node(2), d = node(3);
  const double u[3] = {b[0] - a[0], b[1] - a[1], b[2] - a[2]};
  const double v[3] = {c[0] - a[0], c[1] - a[1], c[2] - a[2]};
  const double w[3] = {d[0] - a[0], d[1] - a[1], d[2] - a[2]};
  return u[0] * (v[1] * w[2] - v[2] * w[1]) -
         u[1] * (v[0] * w[2] - v[2] * w[0]) +
         u[2] * (v[0] * w[1] - v[1] * w[0]);
}

int left_handed_elements(const MeshOut &mesh) {
  int count = 0;
  for (int e = 0; e < mesh.n_elems; ++e)
    if (signed_volume6(mesh.points,
                       &mesh.tets[static_cast<size_t>(e) *
                                  static_cast<size_t>(mesh.nodes_per_elem)]) <
        0.0)
      ++count;
  return count;
}

MeshOptions mesh_options(double target_size_mm, int order) {
  MeshOptions options;
  options.order = order;
  if (target_size_mm > 0.0)
    options.target_size_mm = target_size_mm;
  else
    options.fineness = 0.5;
  return options;
}

// `target_size_mm` 0 selects the automatic size at fineness 0.5.
json sector_study(const Sector &sector, const std::string &step, int order,
                  double target_size_mm, double force) {
  json study{
      {"step_path", step},
      {"mesh_order", order},
      {"material",
       {{"E_mpa", 68900.0},
        {"nu", 0.33},
        {"yield_mpa", 276.0},
        {"density_g_cm3", 2.7}}},
      {"fixtures",
       json::array({json{{"points", band_samples(sector, sector.bore)},
                         {"tol_mm", 1.5}}})},
      {"loads",
       json::array({json{{"faces",
                          {{"points", band_samples(sector, sector.rim)},
                           {"tol_mm", 1.5}}},
                         {"type", "force"},
                         {"vector", {0.0, 0.0, -force}}}})}};
  if (target_size_mm > 0.0)
    study["target_size_mm"] = target_size_mm;
  else
    study["fineness"] = 0.5;
  return study;
}

// Meshes through the cache the study path uses too, so the solve leg below
// re-uses this fill instead of paying for a second one.
std::shared_ptr<const MeshOut> check_mesh_leg(const std::string &step,
                                              double target, int order) {
  const std::shared_ptr<const MeshOut> cached =
      mesh_step_cached(step, mesh_options(target, order));
  REQUIRE(cached != nullptr);
  const MeshOut &mesh = *cached;
  REQUIRE_MESSAGE(mesh.ok, "order " << order << " target " << target << ": "
                                    << mesh.error << " | note: " << mesh.note);
  CHECK(mesh.nodes_per_elem == (order == 2 ? 10 : 4));
  CHECK(mesh.mesh_type_used == (order == 2 ? "tet10" : "tet4"));
  CHECK(mesh.n_elems > 0);
  CHECK(mesh.tets.size() == static_cast<size_t>(mesh.n_elems) *
                                static_cast<size_t>(mesh.nodes_per_elem));
  MESSAGE("sector order=" << order << " target=" << target
                          << " h=" << mesh.char_len_mm
                          << " elems=" << mesh.n_elems
                          << " nodes=" << mesh.n_nodes
                          << " reoriented=" << mesh.reoriented_elems
                          << " inflated=" << mesh.degenerate_elems_repaired
                          << " fallback=" << mesh.quadratic_fallback_elems);
  // No cell may reach a solver left-handed: that is what the promotion declines
  // and what refused the whole study as "unsupported tet4 connectivity".
  CHECK(left_handed_elements(mesh) == 0);
  CHECK(mesh.degenerate_elems_remaining == 0);
  return cached;
}

void check_solve_leg(const Sector &sector, const std::string &step,
                     double target, int order) {
  constexpr double force = 400.0; // N, axial on the rim band
  const json result =
      fea_run_static(sector_study(sector, step, order, target, force));
  REQUIRE_MESSAGE(result.value("ok", false),
                  "order " << order << " target " << target << ": "
                           << result.dump().substr(0, 900));
  const json &quality = result.at("mesh_quality");
  CHECK(quality.at("order").get<int>() == order);
  const double displacement = result["summary"]["max_disp_mm"].get<double>();
  CHECK(std::isfinite(displacement));
  CHECK(displacement > 0.0);
  // A solve is evidence only if it balances: the reaction returns the load.
  CHECK(result.at("reactions").at("equilibrium_error_pct").get<double>() < 2.0);
  MESSAGE("sector solve order=" << order << " target=" << target << " elems="
                                << quality.at("n_elems").get<int>()
                                << " max_disp=" << displacement);
}

} // namespace

// The witness: quarter-radius sector (Ø127 rim, 3.175 mm web, Ø6.35 bore, one
// window), which the fill still hands over with left-handed and flat cells on
// the wedge faces. Two sizes x two orders, each meshed AND solved.
TEST_CASE("a 90 deg wheel-disc sector meshes and solves at tet4 and tet10") {
  const Sector sector(0.25);
  const std::string step =
      write_sector_step(sector, "chudware_wheel_sector_90_q.step");

  double target = 0.0;
  int order = 1;
  SUBCASE("automatic size, tet4") { target = 0.0; order = 1; }
  SUBCASE("automatic size, tet10") { target = 0.0; order = 2; }
  SUBCASE("explicit 4 mm, tet4") { target = 4.0; order = 1; }
  SUBCASE("explicit 4 mm, tet10") { target = 4.0; order = 2; }

  check_mesh_leg(step, target, order);
  check_solve_leg(sector, step, target, order);
}

// The full-size geometry itself, opt-in: ~0.5M elements per leg, ~11 minutes for
// the three legs below.
//
// It stops at a tet4 solve deliberately. Both element orders MESH at fineness
// 0.5 (467162 elements, 33 left-handed cells relabelled and 14 flat cells
// inflated — this is the exact refusal the mass study hit), and tet4 solves.
// The order-2 SOLVE of this part is out of reach of any machine this runs on,
// and the two ways to get there are both refused for the right reason: at
// fineness 0.5 the 2.08M-DOF tet10 system needs an estimated 39.1 GB against a
// 12.0 GB study budget, and coarsening it (explicit 6 mm, which the governor
// raises to 9.21 mm) fills a 3.175 mm web with 9 mm cells and hands back three
// connected components, two of which reach no fixture. Neither is the mesher
// defect this file exists for; asserting either would pin a machine, not a
// contract.
TEST_CASE("the full-size wheel sector meshes at both orders") {
  const char *enabled = std::getenv("CHUDWARE_MESH_SECTOR_FULL");
  if (enabled == nullptr || std::string(enabled) == "0") {
    MESSAGE("skipped: set CHUDWARE_MESH_SECTOR_FULL=1 to mesh the Ø508 sector");
    return;
  }
  const Sector sector(1.0);
  const std::string step =
      write_sector_step(sector, "chudware_wheel_sector_90_full.step");
  CHECK(check_mesh_leg(step, 0.0, 1)->reoriented_elems > 0);
  CHECK(check_mesh_leg(step, 0.0, 2)->nodes_per_elem == 10);
  check_solve_leg(sector, step, 0.0, 1);
}
