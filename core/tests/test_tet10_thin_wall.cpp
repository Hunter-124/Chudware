// Thin-wall tet10 regression: a graded quadratic mesh on a tube whose wall is
// thinner than the element size must still SOLVE.
//
// Live defect (a vehicle tube frame, release case): an explicit sizes_mm ladder on a
// O60.325 x 4.000 mm wall tube refused with
//   "fea_static: invalid or folded quadratic boundary face"
// so the release evidence had to be produced at tet4 — a stiff, lower-bound
// displacement. Cause: promote_to_quadratic + project_quadratic_boundary_mids
// pushes each free-surface mid-edge node onto the exact BRep, and when the
// element size is comparable to the wall thickness / curvature radius that
// projection folds a handful of elements (sign-changing quadratic Jacobian, or a
// reversed tri6 face the consistent-load quadrature then refuses).
//
// The fix is element-local: detail::repair_folded_quadratic reverts ONLY the
// folded elements' mid nodes to their straight-edge midpoints and reports the
// count as mesh_quality.quadratic_fallback_elems. Nothing downgrades the mesh.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <BRepAlgoAPI_Cut.hxx>
#include <BRepPrimAPI_MakeCylinder.hxx>
#include <IFSelect_ReturnStatus.hxx>
#include <STEPControl_Writer.hxx>
#include <gp_Ax2.hxx>
#include <gp_Dir.hxx>
#include <gp_Pnt.hxx>

#include <cmath>
#include <filesystem>
#include <string>

#include <nlohmann/json.hpp>

#include "sim/fea_run.h"
#include "sim/mesher.h"

using namespace chudware::sim;
using nlohmann::json;

namespace {

// The real tube-frame section: O60.325 mm OD, 4.000 mm wall.
constexpr double kOuterRadius = 60.325 / 2.0;
constexpr double kWall = 4.0;
constexpr double kInnerRadius = kOuterRadius - kWall;
constexpr double kLength = 120.0;
// Deliberately coarser than the wall: this is the regime the projection folds.
constexpr double kTargetSize = 6.0;

std::string write_tube_step() {
  const TopoDS_Shape outer =
      BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)),
                               kOuterRadius, kLength)
          .Shape();
  const TopoDS_Shape bore =
      BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0, 0, -1.0), gp_Dir(0, 0, 1)),
                               kInnerRadius, kLength + 2.0)
          .Shape();
  const TopoDS_Shape tube = BRepAlgoAPI_Cut(outer, bore).Shape();
  const auto path =
      std::filesystem::temp_directory_path() / "chudware_tet10_thin_tube.step";
  STEPControl_Writer writer;
  REQUIRE(writer.Transfer(tube, STEPControl_AsIs) == IFSelect_RetDone);
  REQUIRE(writer.Write(path.string().c_str()) == IFSelect_RetDone);
  return path.string();
}

// Sample the annular end face densely enough that the geometric faceset pick
// lands on every ring node at this element size.
json ring_samples(double z) {
  json points = json::array();
  for (int a = 0; a < 72; ++a) {
    const double theta = 2.0 * M_PI * a / 72.0;
    for (int i = 0; i <= 4; ++i) {
      const double r = kInnerRadius + kWall * i / 4.0;
      points.push_back({r * std::cos(theta), r * std::sin(theta), z});
    }
  }
  return points;
}

} // namespace

TEST_CASE("tet10 survives mid-edge projection on a 4 mm tube wall") {
  const std::string step = write_tube_step();

  MeshOptions options;
  options.order = 2;
  options.target_size_mm = kTargetSize;
  const MeshOut mesh = mesh_step(step, options);
  REQUIRE_MESSAGE(mesh.ok, mesh.error);
  CHECK(mesh.nodes_per_elem == 10);
  // The whole point: this geometry/size combination DOES fold elements, and the
  // fallback is what keeps the mesh usable. A zero here means the witness has
  // stopped witnessing (mesher or projection changed) — not that all is well.
  CHECK(mesh.quadratic_fallback_elems > 0);
  CHECK(mesh.quadratic_fallback_elems < mesh.n_elems);

  MESSAGE("thin-wall tet10: n_elems=" << mesh.n_elems << " quadratic_fallback_elems="
                                      << mesh.quadratic_fallback_elems);
  constexpr double force = 5000.0;  // N, axial
  const json study{
      {"step_path", step},
      {"mesh_order", 2},
      {"target_size_mm", kTargetSize},
      {"material",
       {{"E_mpa", 68900.0},
        {"nu", 0.33},
        {"yield_mpa", 276.0},
        {"density_g_cm3", 2.7}}},
      {"fixtures",
       json::array({json{{"points", ring_samples(0.0)}, {"tol_mm", 1.5}}})},
      {"loads", json::array({json{
                    {"faces", {{"points", ring_samples(kLength)}, {"tol_mm", 1.5}}},
                    {"type", "force"},
                    {"vector", {0.0, 0.0, -force}}}})}};

  const json result = fea_run_static(study);
  REQUIRE_MESSAGE(result.value("ok", false), result.dump());

  const json& quality = result.at("mesh_quality");
  CHECK(quality.at("order").get<int>() == 2);
  CHECK(quality.at("quadratic_fallback_elems").get<int>() ==
        mesh.quadratic_fallback_elems);
  CHECK(quality.at("quadratic_fallback_elems").get<int>() > 0);
  // The solve is only evidence if it balances: reaction must return the load.
  // applied/reaction_force_n are 3-vectors in N.
  const json& reactions = result.at("reactions");
  const auto magnitude = [](const json& v) {
    double sum = 0.0;
    for (const auto& c : v) sum += c.get<double>() * c.get<double>();
    return std::sqrt(sum);
  };
  CHECK(magnitude(reactions.at("applied_force_n")) ==
        doctest::Approx(force).epsilon(0.01));
  CHECK(magnitude(reactions.at("reaction_force_n")) ==
        doctest::Approx(force).epsilon(0.02));
  CHECK(reactions.at("equilibrium_error_pct").get<double>() < 2.0);

  // Axial compression of a tube: delta = F*L/(A*E). A fallback mesh is still a
  // quadratic mesh, so it must stay in the analytic band rather than reading
  // like the stiff tet4 lower bound.
  const double area =
      M_PI * (kOuterRadius * kOuterRadius - kInnerRadius * kInnerRadius);
  const double expected = force * kLength / (area * 68900.0);
  const double displacement = result["summary"]["max_disp_mm"].get<double>();
  CHECK(displacement > 0.0);
  CHECK(std::abs(displacement - expected) / expected < 0.30);
}
