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

#include <algorithm>
#include <cmath>
#include <filesystem>
#include <cstring>
#include <fstream>
#include <iterator>
#include <random>
#include <map>
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

json z_face(double z) {
  json points = json::array();
  for (double x : {0.0, kLx / 2, kLx})
    for (double y : {0.0, kLy / 2, kLy})
      points.push_back({x, y, z});
  return {{"points", points},
          {"tol_mm", std::hypot(kLx, kLy)},
          {"face_normal", {0.0, 0.0, z > 0 ? 1.0 : -1.0}},
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

// Values of the DataArray `name` inside the <section> (PointData | CellData)
// of an ASCII VTU; empty when that section has no such array.
std::vector<double> vtu_section_array(const std::string &xml,
                                      const std::string &section,
                                      const std::string &name) {
  const std::size_t open = xml.find("<" + section);
  if (open == std::string::npos)
    return {};
  const std::size_t close = xml.find("</" + section + ">", open);
  const std::size_t at = xml.find("Name=\"" + name + "\"", open);
  if (close == std::string::npos || at == std::string::npos || at > close)
    return {};
  const std::size_t body = xml.find('>', at) + 1;
  const std::size_t end = xml.find("</DataArray>", body);
  std::istringstream in(xml.substr(body, end - body));
  std::vector<double> values;
  for (double v; in >> v;)
    values.push_back(v);
  return values;
}

std::size_t vtu_count(const std::string &xml, const std::string &attribute) {
  const std::string key = attribute + "=\"";
  const std::size_t at = xml.find(key);
  REQUIRE(at != std::string::npos);
  return std::stoul(xml.substr(at + key.size()));
}

SurfaceMesh stl_positions(const std::string &bytes, const SurfaceMesh &raw) {
  REQUIRE(bytes.size() == 84 + 50 * static_cast<std::size_t>(raw.triangle_count()));
  SurfaceMesh out = raw;
  for (int t = 0; t < raw.triangle_count(); ++t)
    for (int k = 0; k < 3; ++k)
      for (int c = 0; c < 3; ++c) {
        float value;
        std::memcpy(&value, bytes.data() + 84 + 50 * t + 12 + 12 * k + 4 * c, 4);
        out.points_m(c, raw.triangles(k, t)) = value * 1e-3;
      }
  return out;
}

double mean_normal_deviation(const SurfaceMesh &surface) {
  std::map<std::pair<int, int>, int> owner;
  double sum = 0;
  int count = 0;
  auto normal = [&](int t) -> Eigen::Vector3d {
    const Eigen::Vector3d a = surface.points_m.col(surface.triangles(0, t));
    return (surface.points_m.col(surface.triangles(1, t)) - a)
        .cross(surface.points_m.col(surface.triangles(2, t)) - a).normalized();
  };
  for (int t = 0; t < surface.triangle_count(); ++t)
    for (int k = 0; k < 3; ++k) {
      const int a = surface.triangles(k, t), b = surface.triangles((k + 1) % 3, t);
      const auto key = std::minmax(a, b);
      const auto [it, inserted] = owner.emplace(key, t);
      if (!inserted) {
        sum += std::acos(std::clamp(normal(t).dot(normal(it->second)), -1.0, 1.0));
        ++count;
      }
    }
  return count > 0 ? sum / count : 0;
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
  for (const auto &array : r["artifacts"]["overlay_aicm"]["arrays"])
    CHECK(array["name"] != "overhang");

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

TEST_CASE("reanalysis.vtu carries a point von Mises field per load case") {
  // render_animation(kind='fea') and the FEA viewers read a POINT field
  // von_mises[@case] in MPa; the reanalysis wrote stress only per element.
  const fs::path dir = scratch_dir("reanalysis_stress");
  // Few iterations at a high volume fraction keep the thresholded design one
  // connected solid, so the reanalysis completes (a loose piece has no support).
  json s = cantilever(dir, 3);
  s.erase("loads");
  auto tip = [](const char *name, json vector) {
    return json{{"name", name},
                {"weight", 1.0},
                {"loads", json::array({{{"faces", x_face(kLx)},
                                        {"type", "force"},
                                        {"vector", std::move(vector)}}})}};
  };
  s["load_cases"] = json::array({tip("LC1_down", {0.0, 0.0, -100.0}),
                                 tip("LC2_side", {0.0, 100.0, 0.0})});
  s["topology"]["volume_fraction"] = 0.9;
  const json r = fea_run_topology(s);
  INFO(r.value("error", std::string()));
  REQUIRE(r["ok"].get<bool>());
  const json &re = r["reconstruction"]["reanalysis"];
  INFO(re.dump());
  REQUIRE(re["status"] == "completed");
  REQUIRE(re["domain"] == "clipped_solid");
  REQUIRE(r["artifacts"].contains("reanalysis_vtu"));
  check_artifacts(r, r["result_dir"].get<std::string>());
  const json &art = r["artifacts"]["reanalysis_vtu"];
  CHECK(art["point_stress"].is_string());
  const std::string xml = read_file(art["path"].get<std::string>());
  const std::size_t points = vtu_count(xml, "NumberOfPoints");
  const std::size_t cells = vtu_count(xml, "NumberOfCells");
  CHECK(art["points"].get<std::size_t>() == points);
  for (const char *c : {"LC1_down", "LC2_side"}) {
    const std::string name = std::string("von_mises@") + c;
    INFO(name);
    const std::vector<double> nodal =
        vtu_section_array(xml, "PointData", name);
    const std::vector<double> element =
        vtu_section_array(xml, "CellData", "element_" + name);
    REQUIRE(nodal.size() == points);
    REQUIRE(element.size() == cells);
    CHECK(vtu_section_array(xml, "PointData", std::string("displacement@") + c)
              .size() == 3 * points);
    CHECK(std::all_of(nodal.begin(), nodal.end(),
                      [](double v) { return std::isfinite(v) && v >= 0.0; }));
    const double nodal_peak = *std::max_element(nodal.begin(), nodal.end());
    const double element_peak = *std::max_element(element.begin(), element.end());
    CHECK(nodal_peak > 0.0);
    // A volume-weighted nodal average never exceeds the element peak.
    CHECK(nodal_peak <= element_peak * (1.0 + 1e-12));
    bool listed = false;
    for (const json &a : art["arrays"])
      if (a["name"] == name && a["location"] == "node") {
        listed = true;
        CHECK(a["unit"] == "MPa");
        CHECK(a["components"] == 1);
      }
    CHECK(listed);
  }
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

TEST_CASE("build plate resolves outward boundary normals before optimization") {
  const fs::path dir = scratch_dir("build_plate");
  json study = cantilever(dir, 1);
  study["build_plate"] = {{"faces", z_face(0)}, {"label", "Printer bed"}};
  study["topology"]["overhang_penalty"] = 0.2;
  const auto prepared = prepare_topology_study(study);
  REQUIRE(prepared.settings.build_direction);
  CHECK(prepared.settings.build_direction->isApprox(Eigen::Vector3d::UnitZ()));
  REQUIRE(prepared.problem->build_plate);
  CHECK(prepared.problem->build_plate->triangles == 32);
  CHECK(prepared.problem->build_plate->area_m2 == doctest::Approx(kLx * kLy * 1e-6));
  CHECK(children(dir) == 0);
  auto refused = [&](json invalid, const char *code) {
    const json r = fea_run_topology(invalid);
    INFO(code << ": " << r.value("error", std::string()));
    CHECK_FALSE(r["ok"].get<bool>());
    CHECK(r["error_code"] == code);
    CHECK(r["field"] == "build_plate");
    CHECK(r["error"].get<std::string>().find("Printer bed") != std::string::npos);
    CHECK_FALSE(r.contains("history"));
  };
  json conflict = study;
  conflict["topology"]["build_direction"] = {1, 0, 0};
  refused(conflict, "build_plate_direction_conflict");
  json nonplanar = study;
  json picks = json::array();
  const auto coordinates = study["points"].get<std::vector<double>>();
  for (std::size_t n = 0; n < coordinates.size(); n += 3)
    if (coordinates[n] == 0 || coordinates[n + 2] == 0)
      picks.push_back({coordinates[n], coordinates[n + 1], coordinates[n + 2]});
  nonplanar["build_plate"]["faces"] = {{"points", picks}, {"tol_mm", 0.01}};
  refused(nonplanar, "build_plate_not_planar");
  json empty = study;
  empty["build_plate"]["faces"] = z_face(-20);
  refused(empty, "build_plate_empty");
  json top = study;
  top["build_plate"]["faces"] = z_face(kLz);
  const auto inverted = prepare_topology_study(top);
  CHECK(inverted.settings.build_direction->isApprox(-Eigen::Vector3d::UnitZ()));
  top["topology"]["build_direction"] = {0, 0, 1};
  refused(top, "build_plate_direction_conflict");

  // An underside ledge at z=5 is planar but another leg extends down to z=0.
  json stepped = study;
  json kept = json::array();
  const auto points = stepped["points"].get<std::vector<double>>();
  const auto tets = stepped["tets"].get<std::vector<int>>();
  for (std::size_t t = 0; t < tets.size(); t += 4) {
    double x = 0, z = 0;
    for (int k = 0; k < 4; ++k) {
      x += points[3 * tets[t + k]] / 4;
      z += points[3 * tets[t + k] + 2] / 4;
    }
    if (x >= kLx / 2 && z < kLz / 2)
      continue;
    for (int k = 0; k < 4; ++k)
      kept.push_back(tets[t + k]);
  }
  stepped["tets"] = std::move(kept);
  stepped["build_plate"]["faces"] = z_face(kLz / 2);
  stepped["build_plate"]["faces"]["face_normal"] = {0, 0, -1};
  refused(stepped, "build_plate_not_lowest");
  fs::remove_all(dir);
}

TEST_CASE("finished cantilever exports a pinned validated smoother print surface") {
  const fs::path dir = scratch_dir("print_surface");
  json study = cantilever(dir, 12);
  study.update(box_mesh(16, 4, 4));
  study["topology"]["volume_fraction"] = 0.6;
  study["topology"]["density_threshold"] = 0.4;
  study["build_plate"] = {{"faces", z_face(0)}, {"label", "Printer bed"}};
  study["topology"]["min_member_mm"] = 1.2;
  const json result = fea_run_topology(study);
  INFO(result.value("error", std::string()));
  INFO(result.value("artifact_refusals", json::object()).dump());
  REQUIRE(result["ok"].get<bool>());
  REQUIRE(result["reconstruction"]["geometry_valid"].get<bool>());
  REQUIRE(result["artifacts"].contains("surface_print_stl"));
  CHECK(result["reconstruction"]["print_surface"]["state"] == "valid");
  CHECK(result["reconstruction"]["print_surface"]["pinned_vertices"].get<int>() > 0);
  CHECK(result["reconstruction"]["print_surface"]["smoothed_vertices"].get<int>() > 0);
  CHECK(result["artifacts"]["overlay_aicm"]["surface"] == "print");
  CHECK(result["summary"]["filter_radius_source"] == "auto_2x_median_element_size");
  CHECK(result["summary"]["min_member_filter_radius_mm"].get<double>() ==
        doctest::Approx(0.6));
  CHECK(result["summary"]["mesh_floor_radius_mm"].get<double>() >
        result["summary"]["min_member_filter_radius_mm"].get<double>());
  const auto &overhang = result["manufacturing"]["enforcement"]["overhang"];
  CHECK(overhang["build_direction"] == json::array({0.0, 0.0, 1.0}));
  CHECK(overhang["build_direction_source"] == "build_plate");
  CHECK(overhang["build_plate"]["triangles"].get<int>() > 0);
  CHECK(overhang["surface_measured_on"] == "print");
  CHECK(overhang["surface_unsupported_area_mm2"].get<double>() >= 0);
  bool scalar = false;
  for (const auto &array : result["artifacts"]["overlay_aicm"]["arrays"])
    scalar = scalar || array["name"] == "overhang";
  CHECK(scalar);

  const auto prep = prepare_topology_study(study);
  const auto density = result["density"].get<std::vector<double>>();
  const Eigen::VectorXd rho =
      Eigen::Map<const Eigen::VectorXd>(density.data(), density.size());
  const auto rec = reconstruct_design(*prep.problem, rho);
  const SurfaceMesh &raw = rec.solid.surface.mesh;
  const std::string raw_bytes =
      read_file(result["artifacts"]["surface_stl"]["path"].get<std::string>());
  const std::string print_bytes =
      read_file(result["artifacts"]["surface_print_stl"]["path"].get<std::string>());
  REQUIRE(raw_bytes.size() == print_bytes.size());
  std::vector<bool> pinned(raw.vertex_count(), false);
  for (int t = 0; t < raw.triangle_count(); ++t)
    if (raw.triangle_origin[t] == TriangleOrigin::DomainBoundary)
      for (int k = 0; k < 3; ++k)
        pinned[raw.triangles(k, t)] = true;
  for (int t = 0; t < raw.triangle_count(); ++t)
    for (int k = 0; k < 3; ++k)
      if (pinned[raw.triangles(k, t)]) {
        const std::size_t offset = 84 + 50 * t + 12 + 12 * k;
        CHECK(raw_bytes.compare(offset, 12, print_bytes, offset, 12) == 0);
      }
  const auto raw_stl = stl_positions(raw_bytes, raw);
  const auto print_stl = stl_positions(print_bytes, raw);
  CHECK(mean_normal_deviation(print_stl) < mean_normal_deviation(raw_stl));
  CHECK(validate_surface(print_stl).state == GeometryState::Valid);
  CHECK_FALSE(surface_self_intersects(print_stl));
  check_artifacts(result, result["result_dir"].get<std::string>());
  fs::remove_all(dir);
}
