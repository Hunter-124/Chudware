// Mesh-mechanism gate: a static solve must REFUSE a mesh whose components do not
// all reach a fixture, and must state its connectivity either way.
//
// Live defect (ME490 front spine tube, O60 x 5.16 mm wall, tet10 at h = 16 mm
// ~ 3x the wall): fea_static returned ok with max displacement 9.6e6 mm and
// equilibrium_error_pct 0.00042. The mesh had a mechanism, and the equilibrium
// check structurally cannot see one — a mechanism carries no load, so the
// residual is tiny BECAUSE the model is broken. Nothing else looked, so the
// agent had to notice a nonsense number by eye.
//
// Two witnesses, one compound each:
//   * two disjoint boxes, fixture on one and load on the other -> refusal that
//     names unfixed_components == 1, and no solve;
//   * the same setup on ONE box -> connected_components == 1, a solve, and a
//     plausibility flag of "ok".
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <BRepBuilderAPI_Transform.hxx>
#include <BRepPrimAPI_MakeBox.hxx>
#include <BRep_Builder.hxx>
#include <IFSelect_ReturnStatus.hxx>
#include <STEPControl_Writer.hxx>
#include <TopoDS_Compound.hxx>
#include <TopoDS_Shape.hxx>
#include <gp_Trsf.hxx>
#include <gp_Vec.hxx>

#include <filesystem>
#include <string>

#include <nlohmann/json.hpp>

#include "sim/fea_run.h"
#include "sim/mesher.h"

using namespace chudware::sim;
using nlohmann::json;

namespace {

constexpr double kSide = 20.0;      // cube edge, mm
constexpr double kIslandX = 200.0;  // second cube's x offset: no gap, a chasm
constexpr double kTargetSize = 5.0;

std::string write_step(const TopoDS_Shape& shape, const char* name) {
  const auto path = std::filesystem::temp_directory_path() / name;
  STEPControl_Writer writer;
  REQUIRE(writer.Transfer(shape, STEPControl_AsIs) == IFSelect_RetDone);
  REQUIRE(writer.Write(path.string().c_str()) == IFSelect_RetDone);
  return path.string();
}

TopoDS_Shape cube_at(double x0) {
  return BRepPrimAPI_MakeBox(gp_Pnt(x0, 0.0, 0.0), kSide, kSide, kSide).Shape();
}

// One STEP holding TWO disjoint solids. This is what an imported assembly (and
// any multi-body part) meshes as: one node array with two islands in it.
std::string write_two_island_step() {
  TopoDS_Compound compound;
  BRep_Builder builder;
  builder.MakeCompound(compound);
  builder.Add(compound, cube_at(0.0));
  builder.Add(compound, cube_at(kIslandX));
  return write_step(compound, "chudware_mechanism_two_islands.step");
}

// Sample points over the axis-aligned face z == z0 of the cube at x0, dense
// enough (2 mm) that the faceset pick lands on every face node at h = 5 mm.
json face_samples(double x0, double z0) {
  json points = json::array();
  for (int i = 0; i <= 10; ++i)
    for (int j = 0; j <= 10; ++j)
      points.push_back({x0 + kSide * i / 10.0, kSide * j / 10.0, z0});
  return points;
}

json steel() {
  return json{{"E_mpa", 200000.0},
              {"nu", 0.29},
              {"yield_mpa", 250.0},
              {"density_g_cm3", 7.85}};
}

// Fixture on the bottom face of the cube at `fix_x`, a downward force on the top
// face of the cube at `load_x`. On the two-island compound those are different
// solids, which is exactly the mechanism.
json study_for(const std::string& step, double fix_x, double load_x) {
  return json{
      {"step_path", step},
      {"mesh_order", 1},
      {"target_size_mm", kTargetSize},
      {"material", steel()},
      {"fixtures", json::array({json{{"points", face_samples(fix_x, 0.0)},
                                     {"tol_mm", 1.5}}})},
      {"loads",
       json::array({json{{"faces",
                          {{"points", face_samples(load_x, kSide)},
                           {"tol_mm", 1.5}}},
                         {"type", "force"},
                         {"vector", {0.0, 0.0, -500.0}}}})}};
}

}  // namespace

TEST_CASE("a mesh island with no fixture is refused, not solved") {
  const std::string step = write_two_island_step();

  MeshOptions options;
  options.order = 1;
  options.target_size_mm = kTargetSize;
  const MeshOut mesh = mesh_step(step, options);
  REQUIRE_MESSAGE(mesh.ok, mesh.error);
  // The witness only witnesses while the compound really meshes as two islands.
  REQUIRE(mesh.connected_components == 2);

  const json result = study_for(step, /*fix_x=*/0.0, /*load_x=*/kIslandX);
  const json refusal = fea_run_static(result);
  REQUIRE_MESSAGE(!refusal.value("ok", true), refusal.dump());
  const std::string error = refusal.value("error", std::string());
  CHECK(error.find("with no fixture") != std::string::npos);
  CHECK(error.find("rigid-body motion") != std::string::npos);

  // The refusal carries the evidence that justifies it, not just prose.
  REQUIRE(refusal.contains("mesh_quality"));
  const json& quality = refusal.at("mesh_quality");
  CHECK(quality.at("connected_components").get<int>() == 2);
  CHECK(quality.at("unfixed_components").get<int>() == 1);
  // Nothing was solved: a refusal must not carry a displacement field.
  CHECK_FALSE(refusal.contains("displacement"));
  CHECK_FALSE(refusal.contains("summary"));
}

TEST_CASE("a single connected fixtured box solves and reports plausible motion") {
  const std::string step = write_step(cube_at(0.0), "chudware_mechanism_box.step");

  MeshOptions options;
  options.order = 1;
  options.target_size_mm = kTargetSize;
  const MeshOut mesh = mesh_step(step, options);
  REQUIRE_MESSAGE(mesh.ok, mesh.error);
  CHECK(mesh.connected_components == 1);

  const json result = fea_run_static(study_for(step, 0.0, 0.0));
  REQUIRE_MESSAGE(result.value("ok", false), result.dump());

  const json& quality = result.at("mesh_quality");
  CHECK(quality.at("connected_components").get<int>() == 1);
  CHECK(quality.at("unfixed_components").get<int>() == 0);
  // A 20 mm solid cube at h = 5 mm is the OPPOSITE of a one-element wall: the
  // thinnest wall spans several elements.
  REQUIRE(quality.at("thin_wall_ratio").is_number());
  CHECK(quality.at("thin_wall_ratio").get<double>() > 1.0);

  const json& plausibility = result.at("plausibility");
  CHECK(plausibility.at("flag").get<std::string>() == "ok");
  const double ratio = plausibility.at("max_disp_over_bbox_diag").get<double>();
  CHECK(ratio > 0.0);
  CHECK(ratio < 1.0);
  CHECK_FALSE(result.contains("warnings"));
  MESSAGE("box: max_disp_mm=" << result["summary"]["max_disp_mm"].get<double>()
                              << " disp/diag=" << ratio << " thin_wall_ratio="
                              << quality.at("thin_wall_ratio").get<double>());
}

// The third surface of the same defect: a solve whose CONNECTIVITY is sound but
// whose answer has left the linear-elastic regime. It is not refused — a real
// large deflection is worth diagnosing — but it must be flagged, because the
// live failure looked exactly like this: an ok result carrying 9.6e6 mm.
TEST_CASE("a displacement past the bounding-box diagonal is flagged implausible") {
  json study;
  // 1 mm cube, Freudenthal 6-tet subdivision about the 0-6 body diagonal.
  study["points"] = {0, 0, 0, 1, 0, 0, 1, 1, 0, 0, 1, 0,
                     0, 0, 1, 1, 0, 1, 1, 1, 1, 0, 1, 1};
  study["tets"] = {0, 1, 2, 6, 0, 2, 3, 6, 0, 3, 7, 6,
                   0, 7, 4, 6, 0, 4, 5, 6, 0, 5, 1, 6};
  // A modulus this low under this load is small-strain nonsense, which is the
  // point: the solver has no way to know, so the result has to say it.
  study["material"] = {{"E_mpa", 0.01}, {"nu", 0.29}, {"yield_mpa", 250.0}};
  study["fixtures"] = {
      {{"points", {{0, 0, 0}, {1, 0, 0}, {1, 1, 0}, {0, 1, 0}}}, {"tol_mm", 0.01}}};
  study["loads"] = {
      {{"type", "force"},
       {"vector", {0.0, 0.0, 500.0}},
       {"faces",
        {{"points", {{0, 0, 1}, {1, 0, 1}, {1, 1, 1}, {0, 1, 1}}}, {"tol_mm", 0.01}}}}};

  const json result = fea_run_static(study);
  REQUIRE_MESSAGE(result.value("ok", false), result.dump());
  const json& quality = result.at("mesh_quality");
  CHECK(quality.at("connected_components").get<int>() == 1);
  CHECK(quality.at("unfixed_components").get<int>() == 0);

  const json& plausibility = result.at("plausibility");
  CHECK(plausibility.at("flag").get<std::string>() == "implausible");
  CHECK(plausibility.at("max_disp_over_bbox_diag").get<double>() > 1.0);
  REQUIRE(result.contains("warnings"));
  REQUIRE(result.at("warnings").is_array());
  REQUIRE(result.at("warnings").size() == 1);
  const std::string warning = result.at("warnings")[0].get<std::string>();
  CHECK(warning.rfind("displacement_implausible:", 0) == 0);
  MESSAGE("soft cube: " << plausibility.dump());
}

