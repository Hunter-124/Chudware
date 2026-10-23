// Mesh-mechanism gate: a static solve must REFUSE a mesh whose components do not
// all reach a fixture, and must state its connectivity either way.
//
// Live defect (a vehicle tube frame's front spine tube, O60 x 5.16 mm wall, tet10 at h = 16 mm
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
#include <BRepAlgoAPI_Cut.hxx>
#include <BRepAlgoAPI_Fuse.hxx>
#include <BRepPrimAPI_MakeBox.hxx>
#include <BRepPrimAPI_MakeCylinder.hxx>
#include <BRep_Builder.hxx>
#include <IFSelect_ReturnStatus.hxx>
#include <STEPControl_Writer.hxx>
#include <TopoDS_Compound.hxx>
#include <TopoDS_Shape.hxx>
#include <gp_Trsf.hxx>
#include <gp_Vec.hxx>

#include <filesystem>
#include <algorithm>
#include <array>
#include <cmath>
#include <map>
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

TEST_CASE("zero-gap press-fit preserves delivered bore and shaft boundary patches") {
  constexpr double radius = 5.0, height = 10.0;
  const auto shaft = BRepPrimAPI_MakeCylinder(radius, height).Shape();
  const auto stock = BRepPrimAPI_MakeCylinder(2.0 * radius, height).Shape();
  const auto sleeve = BRepAlgoAPI_Cut(stock, shaft).Shape();
  TopoDS_Compound compound;
  BRep_Builder builder;
  builder.MakeCompound(compound);
  builder.Add(compound, sleeve);
  builder.Add(compound, shaft);
  const auto step = write_step(compound, "chudware_zero_gap_press_fit.step");
  MeshOptions options;
  options.order = 1;
  options.target_size_mm = 0.35 * radius;
  options.declared_interfaces = true;
  const auto mesh = mesh_step(step, options);
  REQUIRE_MESSAGE(mesh.ok, mesh.error);
  REQUIRE(mesh.bodies.size() == 2);
  std::map<std::array<int, 3>, int> uses;
  for (int e = 0; e < mesh.n_elems; ++e) {
    for (int omitted = 0; omitted < 4; ++omitted) {
      std::array<int, 3> face{};
      int j = 0;
      for (int k = 0; k < 4; ++k)
        if (k != omitted) face[j++] = mesh.tets[4 * e + k];
      std::sort(face.begin(), face.end());
      ++uses[face];
    }
  }
  std::array<double, 2> wall_area{};
  for (const auto& [face, count] : uses) {
    if (count != 1) continue;
    std::array<std::array<double, 3>, 3> p{};
    bool wall = true;
    for (int i = 0; i < 3; ++i) {
      for (int k = 0; k < 3; ++k) p[i][k] = mesh.points[3 * face[i] + k];
      wall = wall && std::abs(std::hypot(p[i][0], p[i][1]) - radius) < 0.1;
    }
    if (!wall) continue;
    const std::array<double, 3> a{p[1][0]-p[0][0], p[1][1]-p[0][1], p[1][2]-p[0][2]};
    const std::array<double, 3> b{p[2][0]-p[0][0], p[2][1]-p[0][1], p[2][2]-p[0][2]};
    const double area = 0.5 * std::hypot(a[1]*b[2]-a[2]*b[1],
                                       a[2]*b[0]-a[0]*b[2],
                                       a[0]*b[1]-a[1]*b[0]);
    for (std::size_t body = 0; body < mesh.bodies.size(); ++body)
      if (face[0] >= mesh.bodies[body].node_begin && face[0] < mesh.bodies[body].node_end)
        wall_area[body] += area;
  }
  for (double area : wall_area)
    CHECK(area > 0.5 * 2.0 * std::acos(-1.0) * radius * height);
  std::filesystem::remove(step);
}

TEST_CASE("sub-resolution raised rim is reported as absorbed rather than missing") {
  const auto stock = BRepPrimAPI_MakeBox(gp_Pnt(-10, -10, 0), 20, 20, 5).Shape();
  const auto pad = BRepPrimAPI_MakeCylinder(
      gp_Ax2(gp_Pnt(0, 0, 4), gp_Dir(0, 0, 1)), 3.0, 1.001).Shape();
  const auto step = write_step(BRepAlgoAPI_Fuse(stock, pad).Shape(),
                               "chudware_subresolution_rim.step");
  MeshOptions options;
  options.order = 1;
  options.target_size_mm = 3.5;
  const auto mesh = mesh_step(step, options);
  REQUIRE_MESSAGE(mesh.ok, mesh.error);
  CHECK(mesh.note.find("feature_absorbed face=") != std::string::npos);
  CHECK(mesh.connected_components == 1);
  CHECK(mesh.char_len_mm > 0.8 * options.target_size_mm);
  std::filesystem::remove(step);
}

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

// ── A mesh the hanging-fragment dissolve repaired must SOLVE ───────────────────
// detail::repair_lonely_nodes dissolves hanging fragments but deliberately does
// NOT renumber nodes (the adaptive driver requires disp_mm.size() ==
// nodes*3), so a repaired mesh carries nodes no element references — up to ten
// per dissolved tet10. fea_static eliminates them exactly (their rows are zero)
// and still refuses a LOAD applied to one, but the rigid-body gate used to count
// every node, so each orphan read as its own "connected component with no
// fixture": the repair the solver needs produced a refusal instead of a solve.
// The gate must judge the DOMAIN — the nodes elements actually reference.
TEST_CASE("nodes left behind by mesh repair are not unfixtured components") {
  json study;
  // 1 mm cube, Freudenthal 6-tet subdivision, PLUS two nodes (indices 8, 9)
  // that no element references — exactly what dissolving a fragment leaves.
  study["points"] = {0, 0, 0, 1, 0, 0, 1, 1, 0, 0, 1, 0,
                     0, 0, 1, 1, 0, 1, 1, 1, 1, 0, 1, 1,
                     5, 5, 5, 5, 5, 6};
  study["tets"] = {0, 1, 2, 6, 0, 2, 3, 6, 0, 3, 7, 6,
                   0, 7, 4, 6, 0, 4, 5, 6, 0, 5, 1, 6};
  study["material"] = {{"E_mpa", 200000.0}, {"nu", 0.29}, {"yield_mpa", 250.0}};
  study["fixtures"] = {
      {{"points", {{0, 0, 0}, {1, 0, 0}, {1, 1, 0}, {0, 1, 0}}}, {"tol_mm", 0.01}}};
  study["loads"] = {
      {{"type", "force"},
       {"vector", {0.0, 0.0, -500.0}},
       {"faces",
        {{"points", {{0, 0, 1}, {1, 0, 1}, {1, 1, 1}, {0, 1, 1}}}, {"tol_mm", 0.01}}}}};

  const json result = fea_run_static(study);
  REQUIRE_MESSAGE(result.value("ok", false), result.dump());
  const json& quality = result.at("mesh_quality");
  // The orphans are neither components of the domain nor mechanisms in it.
  CHECK(quality.at("connected_components").get<int>() == 1);
  CHECK(quality.at("unfixed_components").get<int>() == 0);
  // The load bit: a real displacement field, not the arbitrary one a mechanism
  // returns. (max_von_mises is NOT asserted: on a 6-tet cube every node touches
  // the fixture ring the stress summary excludes, so the reported peak is 0.)
  CHECK(result.at("summary").at("max_disp_mm").get<double>() > 0.0);
  // …and the solve says out loud that it dropped them, rather than doing it quietly.
  REQUIRE(result.contains("solver_notes"));
  bool named = false;
  for (const auto& note : result.at("solver_notes"))
    if (note.get<std::string>().find("reference no element") != std::string::npos)
      named = true;
  CHECK(named);
}

// ── Solids that interpenetrate are ONE body, and a real gap is named ─────────
// A vehicle caliper bracket (source body 201) is a welded rod-and-lug upright
// whose members were never unioned: `check_interference` records 9299.81 mm^3
// of overlap against its neighbour alone. Separately meshed solids share no
// nodes, so every such study was refused for rigid-body motion at every element
// size, and the refusal named no remedy. Two witnesses:
//   * three solids that interpenetrate (a plate and two rods that pass through
//     it and through each other) must FUSE, mesh as ONE component, solve, and
//     report the volume the fuse removed;
//   * two rods 1 mm apart with no contact/bond declared must still be REFUSED,
//     naming the island's body ordinal and that 1 mm.
namespace {

constexpr double kPlateX = 50.0;
constexpr double kPlateY = 30.0;
constexpr double kPlateZ = 10.0;
constexpr double kRodR = 6.0;
constexpr double kRodZ0 = 5.0;   // rods start INSIDE the plate
constexpr double kRodLen = 30.0; // ...and stand 25 mm above it
constexpr double kRod1X = 20.0;
constexpr double kRod2X = 28.0;  // axes 8 mm apart: r+r = 12, so they overlap
constexpr double kRodY = 15.0;

TopoDS_Shape rod_at(double x, double y, double z0, double length) {
  return BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(x, y, z0), gp_Dir(0, 0, 1)), kRodR,
                                  length)
      .Shape();
}

// Plate + two rods, every pair interpenetrating.
std::string write_fused_step() {
  TopoDS_Compound compound;
  BRep_Builder builder;
  builder.MakeCompound(compound);
  builder.Add(compound, BRepPrimAPI_MakeBox(gp_Pnt(0.0, 0.0, 0.0), kPlateX, kPlateY,
                                            kPlateZ)
                            .Shape());
  builder.Add(compound, rod_at(kRod1X, kRodY, kRodZ0, kRodLen));
  builder.Add(compound, rod_at(kRod2X, kRodY, kRodZ0, kRodLen));
  return write_step(compound, "chudware_fuse_plate_two_rods.step");
}

// Two rods whose surfaces are exactly 1.000 mm apart (axes 13 mm, radii 6+6).
std::string write_gap_step() {
  TopoDS_Compound compound;
  BRep_Builder builder;
  builder.MakeCompound(compound);
  builder.Add(compound, rod_at(0.0, 0.0, 0.0, kRodLen));
  builder.Add(compound, rod_at(2.0 * kRodR + 1.0, 0.0, 0.0, kRodLen));
  return write_step(compound, "chudware_fuse_two_rods_1mm_gap.step");
}

// Sample points over a disc of radius r about (cx, cy) at height z on a
// `divisions`-square grid. The grid pitch has to be tight enough that a
// TOLERANCE below the 1 mm rod gap still captures every node of the end face:
// at 30 divisions over a 12 mm disc the pitch is 0.4 mm, so no node is further
// than 0.29 mm from a sample and tol_mm = 0.3 picks the face without reaching
// across the gap to the OTHER rod (which is what a 1.2 mm tolerance did — it
// fixed both bodies and the study then "solved" a non-positive-definite system
// at 1e-7 mm instead of refusing).
json disc_samples(double cx, double cy, double z, double r, int divisions = 12) {
  json points = json::array();
  for (int i = 0; i <= divisions; ++i)
    for (int j = 0; j <= divisions; ++j) {
      const double x = cx - r + 2.0 * r * i / divisions;
      const double y = cy - r + 2.0 * r * j / divisions;
      if ((x - cx) * (x - cx) + (y - cy) * (y - cy) <= r * r + 1e-9)
        points.push_back({x, y, z});
    }
  return points;
}

json rectangle_samples(double x1, double y1, double z) {
  json points = json::array();
  for (int i = 0; i <= 20; ++i)
    for (int j = 0; j <= 12; ++j)
      points.push_back({x1 * i / 20.0, y1 * j / 12.0, z});
  return points;
}

}  // namespace

TEST_CASE("interpenetrating solids are fused into one body and solve") {
  const std::string step = write_fused_step();

  MeshOptions options;
  options.order = 1;
  options.target_size_mm = 4.0;
  const MeshOut mesh = mesh_step(step, options);
  REQUIRE_MESSAGE(mesh.ok, mesh.error);
  // The point of the fuse: ONE fill, so the weld carries nodes. Meshed per
  // solid, this same STEP is three islands and no static study can run.
  CHECK(mesh.connected_components == 1);
  REQUIRE(mesh.fused_groups == 1);
  CHECK(mesh.fused_solids == 3);
  REQUIRE(mesh.bodies.size() == 1);
  CHECK(mesh.bodies[0].source_ordinals.size() == 3);
  // The volume the fuse removed is the model's own interpenetration, and it is
  // closed-form here. Inclusion-exclusion over plate P and rods A, B:
  //   |A∩P| = |B∩P| = pi*36*5                       = 565.4867 mm^3 each
  //   |A∩B| = lens(d=8, r=6) * 30 = 24.7799 * 30    = 743.3982 mm^3
  //   |A∩B∩P| = 24.7799 * 5                         = 123.8997 mm^3
  //   removed = sum|V| - |union| = Σpairwise - triple = 1750.4720 mm^3
  constexpr double kOverlap = 1750.4720;
  CHECK(mesh.fused_overlap_mm3 == doctest::Approx(kOverlap).epsilon(0.005));
  MESSAGE("fused: elems=" << mesh.n_elems << " overlap_mm3=" << mesh.fused_overlap_mm3);

  json study{
      {"step_path", step},
      {"mesh_order", 1},
      {"target_size_mm", 4.0},
      {"material", steel()},
      // Fixture: the plate's underside. Load: the top disc of the first rod.
      {"fixtures", json::array({json{{"points", rectangle_samples(kPlateX, kPlateY, 0.0)},
                                     {"tol_mm", 1.2}}})},
      {"loads",
       json::array({json{{"faces",
                          {{"points",
                            disc_samples(kRod1X, kRodY, kRodZ0 + kRodLen, kRodR)},
                           {"tol_mm", 1.2}}},
                         {"type", "force"},
                         {"vector", {600.0, 0.0, 0.0}}}})}};
  const json result = fea_run_static(study);
  REQUIRE_MESSAGE(result.value("ok", false), result.dump().substr(0, 1200));
  const json& quality = result.at("mesh_quality");
  CHECK(quality.at("connected_components").get<int>() == 1);
  CHECK(quality.at("unfixed_components").get<int>() == 0);
  // The packet names what was welded, so a reader never mistakes a fused study
  // for three bodies in contact.
  REQUIRE(quality.contains("fused_bodies"));
  REQUIRE(quality.at("fused_bodies").size() == 1);
  const json& fused = quality.at("fused_bodies")[0];
  CHECK(fused.at("body_ordinals").size() == 3);
  CHECK(fused.at("overlap_removed_mm3").get<double>() ==
        doctest::Approx(kOverlap).epsilon(0.005));
  CHECK(quality.at("fused_solids").get<int>() == 3);
  CHECK(result.at("summary").at("max_disp_mm").get<double>() > 0.0);
  MESSAGE("fused solve: " << result.at("summary").dump());
}

TEST_CASE("a body a real gap away is refused by ordinal and by that gap") {
  const std::string step = write_gap_step();

  MeshOptions options;
  options.order = 1;
  options.target_size_mm = 4.0;
  const MeshOut mesh = mesh_step(step, options);
  REQUIRE_MESSAGE(mesh.ok, mesh.error);
  // A millimetre is a GAP, not a weld: nothing is fused and the two rods mesh
  // as two bodies, each with its own ordinal.
  CHECK(mesh.fused_groups == 0);
  CHECK(mesh.connected_components == 2);
  REQUIRE(mesh.bodies.size() == 2);

  json study{
      {"step_path", step},
      {"mesh_order", 1},
      {"target_size_mm", 4.0},
      {"material", steel()},
      // tol BELOW the 1 mm gap, or the selector reaches across it and grounds
      // both rods — the defect fea_selection.h calls a selection spanning an
      // interface, here on an interface nobody declared.
      {"fixtures",
       json::array({json{{"points", disc_samples(0.0, 0.0, 0.0, kRodR, 30)},
                         {"tol_mm", 0.3}}})},
      {"loads",
       json::array({json{{"faces",
                          {{"points", disc_samples(2.0 * kRodR + 1.0, 0.0, kRodLen,
                                                   kRodR, 30)},
                           {"tol_mm", 0.3}}},
                         {"type", "force"},
                         {"vector", {0.0, 0.0, -400.0}}}})}};
  const json refusal = fea_run_static(study);
  REQUIRE_MESSAGE(!refusal.value("ok", true), refusal.dump().substr(0, 1200));
  const std::string error = refusal.value("error", std::string());
  CHECK(error.find("with no fixture") != std::string::npos);
  CHECK(error.find("from the nearest fixtured body") != std::string::npos);
  MESSAGE("gap refusal: " << error);

  // The evidence, not just the prose: which body floated, how big it is, how
  // far it is from the structure, and which body that is.
  REQUIRE(refusal.contains("unfixed_islands"));
  REQUIRE(refusal.at("unfixed_islands").size() == 1);
  const json& island = refusal.at("unfixed_islands")[0];
  REQUIRE(island.at("body_ordinals").size() == 1);
  const int island_ordinal = island.at("body_ordinals")[0].get<int>();
  REQUIRE(island.contains("nearest_fixed_body_ordinals"));
  REQUIRE(island.at("nearest_fixed_body_ordinals").size() == 1);
  const int fixed_ordinal = island.at("nearest_fixed_body_ordinals")[0].get<int>();
  // The STEP reader decides which rod is solid 1; what must hold is that the
  // two are named, and named differently.
  CHECK(island_ordinal != fixed_ordinal);
  CHECK(island_ordinal + fixed_ordinal == 3);
  CHECK(island.at("nodes").get<int>() > 0);
  // The distance is measured between mesh NODES, and the closest approach of
  // two cylinders is a single line the fill has no reason to sample exactly, so
  // the reported gap is the true 1.000 mm plus at most a lateral element of
  // sampling error. It must never be less than the CAD gap, and it must be
  // recognizably a millimetre rather than the 13 mm axis spacing.
  const double gap = island.at("gap_to_nearest_fixed_body_mm").get<double>();
  CHECK(gap >= 1.0);
  CHECK(gap < 1.0 + 4.0);
  MESSAGE("island gap_mm=" << gap << " ordinal=" << island_ordinal
                           << " nearest=" << fixed_ordinal);
}

