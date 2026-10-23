#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <BRepAlgoAPI_Cut.hxx>
#include <BRepPrimAPI_MakeBox.hxx>
#include <BRepPrimAPI_MakeCylinder.hxx>
#include <IFSelect_ReturnStatus.hxx>
#include <STEPControl_Writer.hxx>
#include <gp_Ax2.hxx>
#include <gp_Dir.hxx>
#include <gp_Pnt.hxx>

#include <cmath>
#include <filesystem>
#include <string>
#include <optional>

#include <nlohmann/json.hpp>

#include "environment_test_support.h"
#include "sim/fea_adaptive.h"
#include "sim/fea_run.h"
#include "sim/mesh_size_band.h"
#include "sim/mesher.h"

using namespace chudware::sim;
using nlohmann::json;

namespace {

constexpr double kLength = 100.0;
constexpr double kWidth = 10.0;
constexpr double kHeight = 10.0;

std::string write_step(const TopoDS_Shape &shape, const char *name) {
  const auto path = std::filesystem::temp_directory_path() / name;
  STEPControl_Writer writer;
  REQUIRE(writer.Transfer(shape, STEPControl_AsIs) == IFSelect_RetDone);
  REQUIRE(writer.Write(path.string().c_str()) == IFSelect_RetDone);
  return path.string();
}

std::string write_beam_step() {
  return write_step(
      BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), kLength, kWidth, kHeight).Shape(),
      "chudware_polyfea_beam.step");
}

json face_samples(double x) {
  json points = json::array();
  for (int y = 0; y <= 4; ++y)
    for (int z = 0; z <= 4; ++z)
      points.push_back({x, kWidth * y / 4.0, kHeight * z / 4.0});
  return points;
}

json beam_study(const std::string &step, double fineness, int order,
                double force) {
  return json{
      {"step_path", step},
      {"fineness", fineness},
      {"mesh_order", order},
      {"material",
       {{"E_mpa", 200000.0},
        {"nu", 0.29},
        {"yield_mpa", 250.0},
        {"density_g_cm3", 7.85}}},
      {"fixtures",
       json::array({json{{"points", face_samples(0.0)}, {"tol_mm", 2.0}}})},
      {"loads",
       json::array({json{
           {"faces", {{"points", face_samples(kLength)}, {"tol_mm", 2.0}}},
           {"type", "force"},
           {"vector", {0.0, 0.0, -force}}}})}};
}

} // namespace

TEST_CASE("Polyhedral-FEA emits valid tet4 and tet10 meshes") {
  const std::string step = write_beam_step();
  MeshOptions options;
  options.fineness = 0.4;
  options.order = 1;

  const MeshOut tet4 = mesh_step(step, options);
  REQUIRE_MESSAGE(tet4.ok, tet4.error);
  CHECK(tet4.nodes_per_elem == 4);
  CHECK(tet4.mesh_type_used == "tet4");
  CHECK(tet4.tets.size() == static_cast<size_t>(tet4.n_elems) * 4);
  CHECK(tet4.n_nodes > 0);
  CHECK(tet4.n_elems > 0);
  CHECK(tet4.jacobian_min > 0.0);
  CHECK(tet4.min_tet_volume_mm3 > 0.0);
  CHECK(tet4.aspect_hist.size() == 10);

  options.order = 2;
  const MeshOut tet10 = mesh_step(step, options);
  REQUIRE_MESSAGE(tet10.ok, tet10.error);
  CHECK(tet10.nodes_per_elem == 10);
  CHECK(tet10.mesh_type_used == "tet10");
  CHECK(tet10.tets.size() == static_cast<size_t>(tet10.n_elems) * 10);
  CHECK(tet10.jacobian_min > 0.0);

  options.order = 1;
  options.fineness = 0.85;
  const MeshOut fine = mesh_step(step, options);
  REQUIRE_MESSAGE(fine.ok, fine.error);
  CHECK(fine.n_elems > tet4.n_elems);
}

TEST_CASE("Polyhedral-FEA tet10 solves a cantilever in the analytic band") {
  const std::string step = write_beam_step();
  constexpr double force = 100.0;
  const double inertia = kWidth * kHeight * kHeight * kHeight / 12.0;
  const double expected =
      force * kLength * kLength * kLength / (3.0 * 200000.0 * inertia);

  const json result = fea_run_static(beam_study(step, 0.6, 2, force));
  REQUIRE_MESSAGE(result.value("ok", false), result.dump());
  const double displacement = result["summary"]["max_disp_mm"].get<double>();
  CHECK(displacement > 0.0);
  CHECK(std::abs(displacement - expected) / expected < 0.30);
}

TEST_CASE("adaptive refinement spends elements and lowers estimated error") {
  constexpr double length = 120.0;
  constexpr double width = 60.0;
  constexpr double thickness = 8.0;
  constexpr double hole_radius = 8.0;
  TopoDS_Shape plate =
      BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), length, width, thickness).Shape();
  TopoDS_Shape hole =
      BRepPrimAPI_MakeCylinder(
          gp_Ax2(gp_Pnt(length / 2, width / 2, -1.0), gp_Dir(0, 0, 1)),
          hole_radius, thickness + 2.0)
          .Shape();
  const std::string step = write_step(BRepAlgoAPI_Cut(plate, hole).Shape(),
                                      "chudware_polyfea_plate.step");

  auto end_samples = [&](double x) {
    json points = json::array();
    for (int y = 0; y <= 4; ++y)
      for (int z = 0; z <= 2; ++z)
        points.push_back({x, width * y / 4.0, thickness * z / 2.0});
    return points;
  };

  json study{
      {"step_path", step},
      {"fineness", 0.35},
      {"mesh_order", 1},
      {"material",
       {{"E_mpa", 200000.0},
        {"nu", 0.3},
        {"yield_mpa", 250.0},
        {"density_g_cm3", 7.85}}},
      {"fixtures",
       json::array({json{{"points", end_samples(0.0)}, {"tol_mm", 3.0}}})},
      {"loads",
       json::array(
           {json{{"faces", {{"points", end_samples(length)}, {"tol_mm", 3.0}}},
                 {"type", "force"},
                 {"vector", {20000.0, 0.0, 0.0}}}})},
      {"adapt", {{"max_passes", 3}, {"eta_target", 0.02}}}};

  const json result = fea_run_adaptive(study);
  REQUIRE_MESSAGE(result.value("ok", false), result.dump().substr(0, 900));
  const json &passes = result.at("adapt").at("passes");
  REQUIRE(passes.is_array());
  REQUIRE(passes.size() >= 2);
  CHECK(passes.back().at("global_eta").get<double>() <
        passes.front().at("global_eta").get<double>());
  CHECK(passes.back().at("n_elems").get<int>() >
        passes.front().at("n_elems").get<int>());
}

TEST_CASE("learned advice never changes the fixed-stride mesh contract") {
  const std::string step = write_beam_step();
  MeshOptions options;
  options.fineness = 0.4;
  options.learned_advisor = true;
  const MeshOut mesh = mesh_step(step, options);
  REQUIRE_MESSAGE(mesh.ok, mesh.error);
  REQUIRE(mesh.advisor.has_value());
  CHECK((mesh.nodes_per_elem == 4 || mesh.nodes_per_elem == 10));
  CHECK(mesh.tets.size() == static_cast<size_t>(mesh.n_elems) *
                                static_cast<size_t>(mesh.nodes_per_elem));
}

TEST_CASE("removed backend selectors are rejected before geometry access") {
  const json result =
      fea_run_static(json{{"step_path", "/path/that/must/not/be/read.step"},
                          {"mesher_backend", "netgen"},
                          {"material", {{"E_mpa", 200000.0}, {"nu", 0.3},
                                        {"yield_mpa", 250.0},
                                        {"density_g_cm3", 7.85}}},
                          {"fixtures", json::array({json{{"points", {{0, 0, 0}}},
                                                          {"tol_mm", 1.0}}})},
                          {"loads", json::array({json{{"faces", {{"points", {{1, 0, 0}}},
                                                                  {"tol_mm", 1.0}}},
                                                       {"type", "force"},
                                                       {"vector", {1, 0, 0}}}})}});
  CHECK_FALSE(result.value("ok", false));
  CHECK_MESSAGE(
      result.value("error", std::string())
              .find("removed with the Polyhedral-FEA cutover") != std::string::npos,
      result.dump());
}

// ── The element-count budget may never coarsen past the geometry's own floor. ──────
// resolve_size_band predicts an element count for the chosen size and raises the size
// when it blows the interactivity/memory budget. That prediction is fitted on a BULKY
// part; on a slab it over-estimates by 7.5-9.4x (measured 2026-09-05), and coarsening on
// it produced meshes that were not meshes: a 120x60x3 mm plate raised 3.01 -> 8.80 mm
// (0.3 elements across the thickness, 32.7% poor elements), a D12 rod 2.84 -> 5.15 mm
// (aspect_max 339), and the sensor-node demo board 2.60 -> 4.46 mm, which polymesh refused to
// fill at all. The invariant is four elements across the thinnest bbox direction, and it
// binds BOTH budget rungs — a recalibration of the element-count constants must not be
// able to walk past it again.
TEST_CASE("the element budget never coarsens past four elements across the thinnest side") {
  const double thickness = 8.0;
  const TopoDS_Shape plate =
      BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 200.0, 200.0, thickness).Shape();
  MeshOptions options;
  options.fineness = 0.25;
  const SizeBand slab = resolve_size_band(plate, options);
  REQUIRE_MESSAGE(slab.ok, slab.error);
  MESSAGE("slab band: base=" << slab.base << " note=" << slab.note);
  // The automatic size already sits at the thinnest side; the budget wants ~20.8 mm and
  // must not get it. Anything above the thickness cannot fit once across the plate.
  CHECK(slab.base <= thickness * 1.001);
  CHECK(slab.base > thickness * 0.5);

  // Control: on a bulky part the floor is far away and coarsening still happens, so this
  // is a cap on the budget, not a removal of it.
  const TopoDS_Shape cube =
      BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 100.0, 100.0, 100.0).Shape();
  const SizeBand bulky = resolve_size_band(cube, options);
  REQUIRE_MESSAGE(bulky.ok, bulky.error);
  MESSAGE("bulky band: base=" << bulky.base << " note=" << bulky.note);
  CHECK(bulky.base > 12.0);            // coarsened well past the 8.25 mm auto size
  CHECK(bulky.base <= 25.0 * 1.001);   // and never past its own floor (100/4)
}

TEST_CASE("an expired probe budget does not coarsen an explicit size or skip its local band") {
  struct Environment {
    const char *name;
    std::optional<std::string> saved;
    Environment(const char *key, const char *value) : name(key) {
      if (const char *old = std::getenv(key)) saved = old;
      chudware::test::setEnvironment(key, value);
    }
    ~Environment() {
      chudware::test::setOrUnsetEnvironment(
          name, saved ? saved->c_str() : nullptr);
    }
  };
  // Zero exhausts optional probes, not the requested discretization or its
  // local contract. A bounding-box solve forecast must not replace either.
  const Environment enabled("CHUDWARE_GOVERNOR", "1");
  const Environment memory("CHUDWARE_MEM_CAP_GB", "3");
  const Environment probe_time("CHUDWARE_MESH_PROBE_BUDGET_S", "0");
  const TopoDS_Shape cube =
      BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 10.0, 10.0, 10.0).Shape();
  MeshOptions options;
  options.order = 2;
  options.target_size_mm = 0.5;
  const std::string step = write_step(cube, "chudware_governor_band.step");
  SUBCASE("background only") {}
  SUBCASE("local contact band") {
    options.local_refine.points = {5.0, 5.0, 0.0};
    options.local_refine.size_mm = 0.4;
    options.local_refine.band_mm = 1.2;
    options.local_refine.max_depth_mm = 1.2;
    options.local_refine.label = "contact regression";
  }
  const MeshOut mesh = mesh_step(step, options);
  std::filesystem::remove(step);
  REQUIRE_MESSAGE(mesh.ok, mesh.error);
  CHECK(mesh.char_len_mm == options.target_size_mm);
  CHECK(mesh.size_band_applied_floor_mm == 0.0);
  REQUIRE_FALSE(mesh.governor_probes.empty());
  // The final count, not a hypothetical background-only solve, is the
  // admission evidence even when the optional budget was already spent.
  CHECK(mesh.governor_probes.back().elems ==
        static_cast<uint64_t>(mesh.n_elems));
  if (options.local_refine.active()) {
    CHECK(mesh.local_refine_achieved_mm > 0.0);
    CHECK(mesh.local_refine_achieved_mm <=
          kLocalContractSlack * options.local_refine.size_mm);
  }
}
