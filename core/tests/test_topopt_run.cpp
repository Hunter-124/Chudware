// Native topology adapter (sim/topopt_run.h) and reconstruction reanalysis
// (sim/topopt_reanalysis.h): real inline-mesh runs through the shared
// fea_prepare resolution, the real optimizer and the real physics evaluators.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include "util/sha256.h"
#include "sim/progress.h"
#include "sim/sim_cancel.h"
#include "sim/topopt_optimizer.h"
#include "sim/topopt_reanalysis.h"
#include "sim/topopt_run.h"

#include <cmath>
#include <filesystem>
#include <fstream>
#include <iterator>
#include <random>
#include <set>
#include <sstream>
#include <string>
#include <vector>

using namespace chudware::sim::topology;
using chudware::sim::fea_run_topology;
using chudware::sim::topology_capabilities;
using nlohmann::json;
namespace fs = std::filesystem;

namespace {

constexpr double kLx = 40.0, kLy = 10.0, kLz = 10.0;

// nx*ny*nz Kuhn-split cells over [0,kLx]x[0,kLy]x[0,kLz] mm, positively
// oriented tets.
json box_mesh(int nx, int ny, int nz) {
  const double hx = kLx / nx, hy = kLy / ny, hz = kLz / nz;
  std::vector<double> points;
  auto node = [&](int i, int j, int k) {
    return (k * (ny + 1) + j) * (nx + 1) + i;
  };
  for (int k = 0; k <= nz; ++k)
    for (int j = 0; j <= ny; ++j)
      for (int i = 0; i <= nx; ++i) {
        points.push_back(i * hx);
        points.push_back(j * hy);
        points.push_back(k * hz);
      }
  static constexpr int off[8][3] = {{0, 0, 0}, {1, 0, 0}, {1, 1, 0}, {0, 1, 0},
                                    {0, 0, 1}, {1, 0, 1}, {1, 1, 1}, {0, 1, 1}};
  static constexpr int split[6][4] = {{0, 1, 2, 6}, {0, 2, 3, 6}, {0, 3, 7, 6},
                                      {0, 7, 4, 6}, {0, 4, 5, 6}, {0, 5, 1, 6}};
  std::vector<int> tets;
  for (int k = 0; k < nz; ++k)
    for (int j = 0; j < ny; ++j)
      for (int i = 0; i < nx; ++i)
        for (const auto &t : split) {
          int ids[4];
          for (int a = 0; a < 4; ++a)
            ids[a] = node(i + off[t[a]][0], j + off[t[a]][1], k + off[t[a]][2]);
          auto p = [&](int a, int c) { return points[3 * ids[a] + c]; };
          double d[3][3];
          for (int r = 1; r < 4; ++r)
            for (int c = 0; c < 3; ++c)
              d[r - 1][c] = p(r, c) - p(0, c);
          const double det = d[0][0] * (d[1][1] * d[2][2] - d[1][2] * d[2][1]) -
                             d[0][1] * (d[1][0] * d[2][2] - d[1][2] * d[2][0]) +
                             d[0][2] * (d[1][0] * d[2][1] - d[1][1] * d[2][0]);
          if (det < 0)
            std::swap(ids[2], ids[3]);
          tets.insert(tets.end(), ids, ids + 4);
        }
  return {{"points", points}, {"tets", tets}, {"nodes_per_elem", 4}};
}

// Resolved planar faceset (doc_face_points shape) of the box face x = value.
json x_face(double x) {
  json points = json::array();
  for (double y : {0.0, kLy / 2, kLy})
    for (double z : {0.0, kLz / 2, kLz})
      points.push_back({x, y, z});
  return {{"points", points},
          {"tol_mm", std::hypot(kLy, kLz)},
          {"face_normal", {x > 0 ? 1.0 : -1.0, 0.0, 0.0}},
          {"perp_tol_mm", 0.01}};
}

fs::path scratch_dir(const std::string &tag) {
  std::random_device rd;
  const fs::path p = fs::temp_directory_path() / ("chudware_topopt_run_" + tag +
                                                  "_" + std::to_string(rd()));
  fs::create_directories(p);
  return p;
}

json material() {
  return {{"E_mpa", 200000.0},
          {"nu", 0.3},
          {"density_g_cm3", 7.85},
          {"k_w_mk", 50.0},
          {"name", "steel"}};
}

json cantilever(const fs::path &dir, int iterations) {
  json s = box_mesh(8, 2, 2);
  s["type"] = "topology";
  s["name"] = "cantilever";
  s["mesh_order"] = 1;
  s["material"] = material();
  s["fixtures"] =
      json::array({{{"faces", x_face(0.0)}, {"label", "Root clamp"}}});
  s["loads"] = json::array({{{"faces", x_face(kLx)},
                             {"type", "force"},
                             {"vector", {0.0, 0.0, -100.0}},
                             {"label", "Tip load"}}});
  s["topology"] = {{"physics", "structural"},
                   {"method", "simp"},
                   {"volume_fraction", 0.5},
                   {"max_iterations", iterations},
                   {"result_dir", dir.string()}};
  return s;
}

std::string read_file(const std::string &path) {
  std::ifstream in(path, std::ios::binary);
  return std::string(std::istreambuf_iterator<char>(in), {});
}

bool inside(const fs::path &root, const std::string &path) {
  const std::string r = fs::weakly_canonical(root).string();
  const std::string p = fs::weakly_canonical(path).string();
  return p.rfind(r + "/", 0) == 0 || p.rfind(r + "\\", 0) == 0;
}

// Every hashed artifact exists, lies under `root` and matches its hash/size.
void check_artifacts(const json &result, const fs::path &root) {
  REQUIRE(result["artifacts"].is_object());
  for (auto it = result["artifacts"].begin(); it != result["artifacts"].end();
       ++it) {
    const json &a = it.value();
    INFO(it.key());
    REQUIRE(a["path"].is_string());
    CHECK(fs::is_regular_file(a["path"].get<std::string>()));
    CHECK(inside(root, a["path"].get<std::string>()));
    if (!a.contains("sha256"))
      continue;
    const std::string bytes = read_file(a["path"].get<std::string>());
    CHECK(bytes.size() == a["bytes"].get<std::uint64_t>());
    CHECK(chudware::util::sha256_hex(bytes) ==
          a["sha256"].get<std::string>());
  }
}

std::size_t children(const fs::path &dir) {
  std::size_t n = 0;
  for ([[maybe_unused]] const auto &e : fs::directory_iterator(dir))
    ++n;
  return n;
}

} // namespace

TEST_CASE("capabilities: every pair listed, the 18 required supported") {
  const json caps = topology_capabilities();
  REQUIRE(caps.value("ok", false));
  REQUIRE(caps["combinations"].size() == 28);
  std::set<std::string> supported;
  for (const json &c : caps["combinations"]) {
    const std::string key =
        c["method"].get<std::string>() + "/" + c["physics"].get<std::string>();
    if (c["supported"].get<bool>()) {
      supported.insert(key);
      CHECK_FALSE(c["objectives"].empty());
      CHECK(c["formulation"].get<std::string>().size() > 10);
      const std::string phys = c["physics"].get<std::string>();
      CHECK(c["volume_phase"] ==
            ((phys == "flow" || phys == "cht") ? "fluid" : "solid"));
      for (const json &k : c["parameters"])
        if (k == "projection_beta" || k == "move_limit")
          CHECK((c["method"] == "simp" || c["method"] == "ramp"));
    } else {
      INFO(key);
      CHECK_FALSE(c["reason"].get<std::string>().empty());
    }
  }
  for (const char *m : {"simp", "ramp"})
    for (const char *p : {"structural", "thermal", "modal", "buckling", "flow",
                          "thermomechanical", "cht"})
      CHECK(supported.count(std::string(m) + "/" + p) == 1);
  for (const char *m : {"beso", "level_set"})
    for (const char *p : {"structural", "thermal"})
      CHECK(supported.count(std::string(m) + "/" + p) == 1);
  CHECK(supported.size() >= 18);
  // Limits follow the physics: stress only where a stress response exists.
  for (const json &c : caps["combinations"])
    if (c["supported"].get<bool>() && c["physics"] == "thermal")
      for (const json &l : c["limits"])
        CHECK(l != "stress_limit_mpa");
}

TEST_CASE("unsupported envelopes are refused before any mesh or directory") {
  const fs::path dir = scratch_dir("refuse");
  auto refused = [&](json study, const std::string &code) {
    const json r = fea_run_topology(study);
    CHECK_FALSE(r["ok"].get<bool>());
    CHECK(r["error_code"] == code);
    CHECK_FALSE(r["error"].get<std::string>().empty());
    CHECK(children(dir) == 0);
  };
  json s = cantilever(dir, 5);
  s["topology"]["method"] = "beso";
  s["topology"]["physics"] = "modal";
  s.erase("loads");
  {
    const json r = fea_run_topology(s);
    CHECK_FALSE(r["ok"].get<bool>());
    CHECK_FALSE(r["error_code"].get<std::string>().empty());
    CHECK(children(dir) == 0);
  }
  s = cantilever(dir, 5);
  s["mesh_order"] = 2;
  refused(s, "unsupported_element_order");
  s = cantilever(dir, 5);
  s["nodes_per_elem"] = 10;
  refused(s, "unsupported_element_order");
  s = cantilever(dir, 5);
  s["mystery"] = 1;
  refused(s, "unknown_key");
  s = cantilever(dir, 5);
  s["thermal"] = {{"fixtures", json::array({{{"faces", x_face(0.0)},
                                             {"temperature_c", 20.0}}})}};
  refused(s, "section_not_applicable");
  s = cantilever(dir, 5);
  s["fixtures"][0]["faces"] = {{"kind", "face"}, {"sid", "F1"}};
  refused(s, "unresolved_selector");
  s = cantilever(dir, 5);
  s["topology"]["stress_limit_mpa"] = 10.0;
  s["topology"]["method"] = "beso";
  refused(s, "method_constraint_unsupported");
  fs::remove_all(dir);
}

TEST_CASE("structural inline run: honest result, consistent evidence, owned "
          "artifacts") {
  const fs::path dir = scratch_dir("structural");
  const json study = cantilever(dir, 12);
  const json r = fea_run_topology(study);
  INFO(r.value("error", std::string()));
  REQUIRE(r["ok"].get<bool>());
  CHECK(r["physics"] == "structural");
  CHECK(r["method"] == "simp");
  const json &s = r["summary"];
  const double baseline = s["objective_baseline"].get<double>();
  const double final_obj = s["objective_final"].get<double>();
  CHECK(s["objective_units"] == "N*mm");
  CHECK(std::isfinite(final_obj));
  CHECK(final_obj < baseline); // material redistribution stiffens the design
  CHECK(s["final_reevaluated"].get<bool>());
  CHECK(s["volume_phase"] == "solid");
  if (r["feasible"].get<bool>())
    CHECK(s["volume_fraction_final"].get<double>() <=
          0.5 * (1.0 + s["volume_tolerance"].get<double>()) + 1e-12);
  // Converged only when feasible and terminated by the criterion.
  if (r["converged"].get<bool>()) {
    CHECK(r["feasible"].get<bool>());
    CHECK(r["termination"] == "converged");
  }
  if (r["termination"] == "max_iterations")
    CHECK_FALSE(r["converged"].get<bool>());

  // History baseline == summary baseline; series lengths agree.
  const json &h = r["history"];
  REQUIRE(!h["iteration"].empty());
  CHECK(h["objective"][0].get<double>() ==
        doctest::Approx(baseline).epsilon(1e-12));
  CHECK(h["objective"].size() == h["iteration"].size());
  CHECK(h["volume_fraction"].size() == h["iteration"].size());

  // Final objective is the physics at the returned density (independent
  // preparation + evaluation).
  const PreparedTopology prep = prepare_topology_study(study);
  const auto density = r["density"].get<std::vector<double>>();
  REQUIRE(static_cast<int>(density.size()) == prep.mesh->element_count());
  const Eigen::VectorXd rho =
      Eigen::Map<const Eigen::VectorXd>(density.data(), density.size());
  const Evaluation ev =
      make_topology_physics(prep.problem)->evaluate(rho, false, nullptr);
  CHECK(ev.objective * 1e3 == doctest::Approx(final_obj).epsilon(1e-9));
  CHECK(
      phase_fraction(*prep.problem, rho) ==
      doctest::Approx(s["volume_fraction_final"].get<double>()).epsilon(1e-12));

  // Volume constraint reported with its own tolerance.
  bool volume = false;
  for (const json &c : r["constraints"])
    if (c["name"] == "solid_volume_fraction") {
      volume = true;
      CHECK(c["limit"].get<double>() == doctest::Approx(0.5));
    }
  CHECK(volume);

  // Reconstruction is geometry evidence plus an independent reanalysis.
  const json &rec = r["reconstruction"];
  CHECK(rec["status"] == "completed");
  CHECK(rec["passive_geometry"]["satisfied"].get<bool>());
  CHECK(rec["passive_geometry"]["boundary_solid"]["elements"].get<int>() > 0);
  CHECK(rec["passive_geometry"]["boundary_solid"]["missing_volume_mm3"]
            .get<double>() <=
        1e-9 * rec["passive_geometry"]["boundary_solid"]["volume_mm3"]
                   .get<double>());
  const json &re = rec["reanalysis"];
  CHECK(re["domain"] == "clipped_solid");
  if (re["status"] == "completed") {
    CHECK(std::isfinite(re["objective"].get<double>()));
    CHECK(re["mesh"]["volume_mm3"].get<double>() ==
          doctest::Approx(rec["reconstructed_volume_mm3"].get<double>())
              .epsilon(1e-9));
  } else {
    CHECK_FALSE(re["code"].get<std::string>().empty());
  }
  // No unearned claims.
  CHECK_FALSE(r["manufacturing"]["print_ready"].get<bool>());
  CHECK_FALSE(r["manufacturing"]["dfam"]["checked"].get<bool>());
  CHECK(r["manufacturing"]["geometry_valid"] == rec["geometry_valid"]);
  if (!rec["geometry_valid"].get<bool>())
    CHECK_FALSE(r["artifacts"].contains("surface_stl"));

  // Owned, hashed, read-back artifacts; source untouched.
  const fs::path run_dir = r["result_dir"].get<std::string>();
  CHECK(run_dir.parent_path() == dir);
  check_artifacts(r, run_dir);
  for (const char *key : {"run", "inputs", "normalized_spec", "checkpoint",
                          "history", "history_jsonl", "density_vtu", "result"})
    CHECK(r["artifacts"].contains(key));
  const json persisted =
      json::parse(read_file((run_dir / "result.json").string()));
  CHECK(persisted["run_id"] == r["run_id"]);
  CHECK(persisted["summary"]["objective_final"] == s["objective_final"]);
  const json ck =
      json::parse(read_file((run_dir / "checkpoint.json").string()));
  CHECK(ck["schema"] == "chudware.topology.native_checkpoint/1");
  const OptimizerState state = optimizer_state_from_json(ck["state"]);
  CHECK(state.design.size() == prep.problem->design.variable_count);
  CHECK(ck["density"].size() == density.size());
  CHECK(r["provenance"]["source"]["unchanged"].get<bool>());
  CHECK(r["provenance"]["hashes"]["parameters"].get<std::string>().size() ==
        64);

  // A second run never overwrites the first.
  const std::string first_hash = chudware::util::sha256_hex(
      read_file((run_dir / "result.json").string()));
  json again = cantilever(dir, 2);
  const json r2 = fea_run_topology(again);
  REQUIRE(r2["result_dir"].is_string());
  CHECK(r2["result_dir"] != r["result_dir"]);
  CHECK(chudware::util::sha256_hex(
            read_file((run_dir / "result.json").string())) == first_hash);
  CHECK(children(dir) == 2);
  fs::remove_all(dir);
}

TEST_CASE("thermal inline run through the same adapter") {
  const fs::path dir = scratch_dir("thermal");
  json s = box_mesh(8, 2, 2);
  s["material"] = material();
  s["thermal"] = {{"reference_temperature_c", 20.0},
                  {"fixtures", json::array({{{"faces", x_face(0.0)},
                                             {"temperature_c", 20.0},
                                             {"label", "Heat sink"}}})},
                  {"loads", json::array({{{"faces", x_face(kLx)},
                                          {"type", "power"},
                                          {"power_w", 5.0},
                                          {"label", "Chip"}}})}};
  s["topology"] = {{"physics", "thermal"},
                   {"method", "simp"},
                   {"volume_fraction", 0.4},
                   {"max_iterations", 10},
                   {"result_dir", dir.string()}};
  const json r = fea_run_topology(s);
  INFO(r.value("error", std::string()));
  REQUIRE(r["ok"].get<bool>());
  CHECK(r["summary"]["objective"] == "thermal_compliance");
  CHECK(r["summary"]["objective_units"] == "W*K");
  CHECK(r["summary"]["objective_final"].get<double>() <
        r["summary"]["objective_baseline"].get<double>());
  bool temperature = false;
  for (const json &f : r["fields"])
    if (f["name"] == "temperature") {
      temperature = true;
      CHECK(f["unit"] == "C");
    }
  CHECK(temperature);
  bool resolved = false;
  for (const json &e : r["evidence"]["boundary_resolution"])
    if (e["label"] == "Chip") {
      resolved = true;
      CHECK(e["complete_faces"].get<int>() > 0);
      CHECK(e["area_mm2"].get<double>() == doctest::Approx(kLy * kLz));
    }
  CHECK(resolved);
  check_artifacts(r, r["result_dir"].get<std::string>());
  fs::remove_all(dir);
}

namespace {
struct CancelProbe {
  std::string job_id;
  int iterations = 0;
};
void cancel_on_first_iteration(const char *text, void *user) {
  auto *probe = static_cast<CancelProbe *>(user);
  const json event = json::parse(text);
  if (event.contains("iteration") && ++probe->iterations == 1)
    chudware::sim::sim_cancel(probe->job_id);
}
} // namespace

TEST_CASE("cancellation keeps a partial, never-converged, recoverable result") {
  const fs::path dir = scratch_dir("cancel");
  json s = cantilever(dir, 60);
  s["job_id"] = "topopt-run-cancel-test";
  CancelProbe probe{s["job_id"].get<std::string>()};
  chudware::sim::set_progress_sink(cancel_on_first_iteration, &probe);
  const json r = fea_run_topology(s);
  chudware::sim::set_progress_sink(nullptr, nullptr);
  CHECK(probe.iterations >= 1);
  CHECK(r["termination"] == "cancelled");
  CHECK_FALSE(r["converged"].get<bool>());
  CHECK_FALSE(r["ok"].get<bool>());
  CHECK(r["summary"]["iterations"].get<int>() < 60);
  CHECK(r["reconstruction"]["status"] == "skipped");
  REQUIRE(r["artifacts"].contains("checkpoint"));
  check_artifacts(r, r["result_dir"].get<std::string>());
  const json ck = json::parse(
      read_file(r["artifacts"]["checkpoint"]["path"].get<std::string>()));
  CHECK(ck["density"].size() == r["density"].size());
  fs::remove_all(dir);
}

TEST_CASE("preserve/exclude conflicts with boundaries refuse by label") {
  const fs::path dir = scratch_dir("conflict");
  json s = cantilever(dir, 5);
  s["exclude"] =
      json::array({{{"faces", x_face(kLx)}, {"label", "Keep-out tip"}}});
  json r = fea_run_topology(s);
  CHECK_FALSE(r["ok"].get<bool>());
  CHECK(r["error_code"] == "boundary_in_keep_out");
  CHECK(r["error"].get<std::string>().find("Keep-out tip") !=
        std::string::npos);

  s = cantilever(dir, 5);
  s["preserve"] = json::array({{{"faces", x_face(kLx)}, {"label", "Pad"}}});
  s["exclude"] = json::array({{{"box_min_mm", {35.0, -1.0, -1.0}},
                               {"box_max_mm", {41.0, 11.0, 11.0}},
                               {"label", "Clearance box"}}});
  r = fea_run_topology(s);
  CHECK_FALSE(r["ok"].get<bool>());
  CHECK(r["error_code"] == "passive_conflict");
  const std::string msg = r["error"].get<std::string>();
  CHECK(msg.find("Pad") != std::string::npos);
  CHECK(msg.find("Clearance box") != std::string::npos);
  fs::remove_all(dir);
}

TEST_CASE("passive boxes hold on the reconstructed geometry, measured") {
  const fs::path dir = scratch_dir("passive");
  json s = cantilever(dir, 10);
  s["preserve"] = json::array({{{"box_min_mm", {15.0, -1.0, -1.0}},
                                {"box_max_mm", {25.0, 11.0, 11.0}},
                                {"label", "Boss"}}});
  s["exclude"] = json::array({{{"box_min_mm", {27.0, -1.0, 5.0}},
                               {"box_max_mm", {34.0, 11.0, 11.0}},
                               {"label", "Cable slot"}}});
  const json r = fea_run_topology(s);
  INFO(r.value("error", std::string()));
  REQUIRE(r["ok"].get<bool>());
  const json &pg = r["reconstruction"]["passive_geometry"];
  CHECK(pg["preserve"]["elements"].get<int>() > 0);
  CHECK(pg["exclude"]["elements"].get<int>() > 0);
  CHECK(pg["preserve"]["incomplete_elements"].get<int>() == 0);
  CHECK(pg["preserve"]["missing_volume_mm3"].get<double>() <=
        1e-9 * pg["preserve"]["volume_mm3"].get<double>());
  CHECK(pg["exclude"]["invaded_elements"].get<int>() == 0);
  CHECK(pg["exclude"]["solid_volume_mm3"].get<double>() <=
        1e-9 * pg["exclude"]["volume_mm3"].get<double>());
  CHECK(pg["satisfied"].get<bool>());
  bool row = false;
  for (const json &c : r["manufacturing"]["checks"])
    if (c["id"] == "passive_regions") {
      row = true;
      CHECK(c["status"] == "pass");
    }
  CHECK(row);
  fs::remove_all(dir);
}

TEST_CASE(
    "reanalysis of a reconstructed full-solid bar matches the exact bar") {
  // nu = 0, clamped root, uniform pressure p on the free end: tet4 represents
  // u = -p x / E exactly, so compliance = p^2 A L / E.
  json s = box_mesh(8, 2, 2);
  s["material"] = {{"E_mpa", 200000.0}, {"nu", 0.0}};
  s["fixtures"] = json::array({{{"faces", x_face(0.0)}, {"label", "Root"}}});
  s["loads"] = json::array({{{"faces", x_face(kLx)},
                             {"type", "pressure"},
                             {"magnitude", 10.0},
                             {"label", "End pressure"}}});
  s["topology"] = {
      {"physics", "structural"}, {"method", "simp"}, {"volume_fraction", 0.6}};
  const PreparedTopology prep = prepare_topology_study(s);
  const int E = prep.mesh->element_count();
  BoundaryResolver resolver = [&s](const chudware::sim::PreparedMesh &pm,
                                   const json &topo, json *ev) {
    return resolve_topology_boundaries(s, topo, pm, false, ev);
  };

  const Eigen::VectorXd solid = Eigen::VectorXd::Ones(E);
  const Reconstruction rec = reconstruct_design(*prep.problem, solid);
  CHECK(rec.geometry_valid);
  CHECK(rec.solid.volume.volume_m3 ==
        doctest::Approx(prep.mesh->total_volume_m3).epsilon(1e-12));
  const ReanalysisResult re = reanalyze_reconstruction(
      *prep.problem, rec, s["topology"], resolver, nullptr);
  INFO(re.code << ": " << re.message);
  REQUIRE(re.status == "completed");
  CHECK(re.domain == "clipped_solid");
  const double p = 10e6, A = 1e-4, L = 0.04, Ey = 2e11;
  CHECK(re.evaluation.objective ==
        doctest::Approx(p * p * A * L / Ey).epsilon(1e-6));
  // The original-grid full-solid evaluation agrees (independent discretization
  // numbering of the same geometry).
  const Evaluation direct =
      make_topology_physics(prep.problem)->evaluate(solid, false, nullptr);
  CHECK(re.evaluation.objective ==
        doctest::Approx(direct.objective).epsilon(1e-8));

  // Material cut away from the loaded end: the load boundary no longer exists
  // on the reconstruction, which is a named failure, not a repair.
  Eigen::VectorXd half = Eigen::VectorXd::Zero(E);
  for (int e = 0; e < E; ++e)
    if (prep.mesh->centroid_m(0, e) < 0.02)
      half[e] = 1.0;
  const Reconstruction cut = reconstruct_design(*prep.problem, half);
  const ReanalysisResult failed = reanalyze_reconstruction(
      *prep.problem, cut, s["topology"], resolver, nullptr);
  CHECK(failed.status == "failed");
  CHECK_FALSE(failed.code.empty());
  CHECK_FALSE(failed.message.empty());
}
