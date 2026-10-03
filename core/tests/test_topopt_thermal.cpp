#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include "sim/topopt_thermal.h"

#include <atomic>
#include <cmath>
#include <functional>
#include <memory>
#include <random>
#include <stdexcept>
#include <string>
#include <vector>

using namespace chudware::sim::topology;
using nlohmann::json;

namespace {

// Box of nx*ny*nz cells (mm), each split into six Kuhn tets around the 0-6
// diagonal. `jitter` moves interior nodes by up to that fraction of the cell
// size so the mesh is non-symmetric while boundary faces stay planar.
struct Box {
  std::vector<double> points;
  std::vector<int> tets;
  int nx, ny, nz;
  double hx, hy, hz;
  int node(int i, int j, int k) const {
    return (k * (ny + 1) + j) * (nx + 1) + i;
  }
};

Box make_box(int nx, int ny, int nz, double hx, double hy, double hz,
             double jitter = 0.0, unsigned seed = 1) {
  Box b{{}, {}, nx, ny, nz, hx, hy, hz};
  std::mt19937 rng(seed);
  std::uniform_real_distribution<double> u(-jitter, jitter);
  for (int k = 0; k <= nz; ++k)
    for (int j = 0; j <= ny; ++j)
      for (int i = 0; i <= nx; ++i) {
        const bool interior =
            i > 0 && i < nx && j > 0 && j < ny && k > 0 && k < nz;
        b.points.push_back(i * hx + (interior ? u(rng) * hx : 0.0));
        b.points.push_back(j * hy + (interior ? u(rng) * hy : 0.0));
        b.points.push_back(k * hz + (interior ? u(rng) * hz : 0.0));
      }
  static constexpr int off[8][3] = {{0, 0, 0}, {1, 0, 0}, {1, 1, 0}, {0, 1, 0},
                                    {0, 0, 1}, {1, 0, 1}, {1, 1, 1}, {0, 1, 1}};
  static constexpr int split[6][4] = {{0, 1, 2, 6}, {0, 2, 3, 6}, {0, 3, 7, 6},
                                      {0, 7, 4, 6}, {0, 4, 5, 6}, {0, 5, 1, 6}};
  for (int k = 0; k < nz; ++k)
    for (int j = 0; j < ny; ++j)
      for (int i = 0; i < nx; ++i)
        for (const auto &t : split)
          for (int v : t)
            b.tets.push_back(
                b.node(i + off[v][0], j + off[v][1], k + off[v][2]));
  return b;
}

std::vector<int>
nodes_where(const Box &b,
            const std::function<bool(double, double, double)> &pred) {
  std::vector<int> out;
  for (int n = 0; n < static_cast<int>(b.points.size() / 3); ++n)
    if (pred(b.points[3 * n], b.points[3 * n + 1], b.points[3 * n + 2]))
      out.push_back(n);
  return out;
}

std::string code_of(const std::function<void()> &f) {
  try {
    f();
  } catch (const ModelError &e) {
    return e.code();
  }
  return "";
}

const Field &field(const Evaluation &ev, const std::string &name) {
  for (const Field &f : ev.fields)
    if (f.name == name)
      return f;
  throw std::runtime_error("missing field " + name);
}

ThermalLoadSpec power_load(const std::string &label, std::vector<int> nodes,
                           double watts) {
  ThermalLoadSpec l;
  l.label = label;
  l.nodes = std::move(nodes);
  l.kind = ThermalLoadKind::Power;
  l.power_w = watts;
  return l;
}

ThermalLoadSpec flux_load(const std::string &label, std::vector<int> nodes,
                          double w_m2) {
  ThermalLoadSpec l;
  l.label = label;
  l.nodes = std::move(nodes);
  l.kind = ThermalLoadKind::Flux;
  l.magnitude_w_m2 = w_m2;
  return l;
}

ThermalLoadSpec convection_load(const std::string &label,
                                std::vector<int> nodes, double h,
                                double ambient_c) {
  ThermalLoadSpec l;
  l.label = label;
  l.nodes = std::move(nodes);
  l.kind = ThermalLoadKind::Convection;
  l.h_w_m2k = h;
  l.ambient_c = ambient_c;
  return l;
}

// 20 x 10 x 10 mm slab of 5 mm cells along x, k = 200 W/(m K).
struct Slab {
  Box box = make_box(4, 2, 2, 5, 5, 5);
  std::shared_ptr<const TetMesh> mesh = build_tet_mesh(box.points, box.tets);
  std::vector<int> x0 =
      nodes_where(box, [](double x, double, double) { return x == 0; });
  std::vector<int> xL =
      nodes_where(box, [](double x, double, double) { return x == 20; });

  ProblemSpec spec(const ThermalSpec &t) const {
    ProblemSpec s;
    s.settings =
        parse_settings({{"physics", "thermal"}, {"volume_fraction", 0.8}});
    s.material.k_w_mk = 200;
    s.thermal = t;
    return s;
  }
};

double x_mm(const TetMesh &m, int n) { return m.nodes_m(0, n) * 1e3; }

} // namespace

TEST_CASE("fixed-temperature slab: analytic temperature, flux and supports") {
  Slab s;
  ThermalSpec t;
  t.fixtures.push_back({"sink", s.x0, 25.0});
  t.loads.push_back(power_load("heater", s.xL, 10.0));
  auto p = prepare_problem(s.mesh, s.spec(t));
  auto phys = make_thermal_physics(p);
  const int E = s.mesh->element_count();
  const Evaluation ev =
      phys->evaluate(Eigen::VectorXd::Ones(E), false, nullptr);

  // 10 W through 10 mm x 10 mm -> q = 1e5 W/m^2; dT/dx = q/k = 500 K/m, so
  // the 20 mm slab rises 10 K above the 25 C sink.
  const Field &T = field(ev, "temperature");
  for (int n = 0; n < s.mesh->node_count(); ++n)
    CHECK(T.values[n] ==
          doctest::Approx(298.15 + 0.5 * x_mm(*s.mesh, n)).epsilon(1e-10));
  const Field &q = field(ev, "heat_flux");
  for (int e = 0; e < E; ++e) {
    CHECK(q.values[3 * e] == doctest::Approx(-1e5).epsilon(1e-9));
    CHECK(std::abs(q.values[3 * e + 1]) < 1e-4);
    CHECK(std::abs(q.values[3 * e + 2]) < 1e-4);
  }
  const json &hf = ev.evidence["heat_flow_w"];
  CHECK(hf["flux"][0]["power_w"].get<double>() == doctest::Approx(10.0));
  CHECK(hf["fixtures"][0]["power_w"].get<double>() ==
        doctest::Approx(-10.0).epsilon(1e-9));
  CHECK(hf["relative_imbalance"].get<double>() < 1e-10);
  // Default reference 20 C: heated face sits 15 K above it; C = 10 W * 15 K.
  CHECK(ev.objective == doctest::Approx(150.0).epsilon(1e-10));
  CHECK(ev.objective_unit == "W*K");

  // Two fixed temperatures and no source: the reusable system lifts both
  // Dirichlet values. 50 K over 20 mm -> 2500 K/m -> 5e5 W/m^2 -> 50 W.
  Problem two = *p;
  two.thermal->fluxes.clear();
  two.thermal->fixtures.push_back({"hot", s.xL, 348.15});
  std::vector<std::pair<int, double>> fixed;
  for (int n : s.x0)
    fixed.push_back({n, 298.15});
  for (int n : s.xL)
    fixed.push_back({n, 348.15});
  two.thermal->fixed = make_dirichlet(s.mesh->node_count(), fixed);
  ThermalSystem sys(std::make_shared<const Problem>(two));
  sys.solve(Eigen::VectorXd::Ones(E), nullptr);
  Eigen::VectorXd Tk;
  sys.temperature(Tk);
  for (int n = 0; n < s.mesh->node_count(); ++n)
    CHECK(Tk[n] ==
          doctest::Approx(298.15 + 2.5 * x_mm(*s.mesh, n)).epsilon(1e-10));
  const json sev = sys.evidence();
  CHECK(sev["heat_flow_w"]["fixtures"][0]["power_w"].get<double>() ==
        doctest::Approx(-50.0).epsilon(1e-9));
  CHECK(sev["heat_flow_w"]["fixtures"][1]["power_w"].get<double>() ==
        doctest::Approx(50.0).epsilon(1e-9));
  CHECK(sys.thermal_compliance(nullptr, nullptr) == 0.0);
}

TEST_CASE("Robin slab: convection film temperature and convected power") {
  Slab s;
  ThermalSpec t;
  t.reference_temperature_c = 20.0;
  t.loads.push_back(convection_load("film", s.x0, 5000.0, 30.0));
  t.loads.push_back(power_load("heater", s.xL, 10.0));
  auto p = prepare_problem(s.mesh, s.spec(t));
  auto phys = make_thermal_physics(p);
  const Evaluation ev = phys->evaluate(
      Eigen::VectorXd::Ones(s.mesh->element_count()), false, nullptr);
  // q = h (T(0) - T_amb): 1e5 / 5000 = 20 K film rise, then 500 K/m.
  const Field &T = field(ev, "temperature");
  for (int n = 0; n < s.mesh->node_count(); ++n)
    CHECK(T.values[n] ==
          doctest::Approx(323.15 + 0.5 * x_mm(*s.mesh, n)).epsilon(1e-10));
  const json &hf = ev.evidence["heat_flow_w"];
  CHECK(hf["convection"][0]["power_w"].get<double>() ==
        doctest::Approx(-10.0).epsilon(1e-9));
  CHECK(hf["fixtures"].empty());
  CHECK(hf["relative_imbalance"].get<double>() < 1e-10);
  // Heated face at 60 C, 40 K above the explicit 20 C reference.
  CHECK(ev.objective == doctest::Approx(400.0).epsilon(1e-10));
}

TEST_CASE("per-face power balance at mixed density on a skewed mesh") {
  const Box b = make_box(5, 3, 3, 4, 3, 5, 0.1, 3);
  auto mesh = build_tet_mesh(b.points, b.tets);
  auto on = [&](int axis, double v) {
    return nodes_where(b, [=](double x, double y, double z) {
      const double c[3] = {x, y, z};
      return std::abs(c[axis] - v) < 1e-9;
    });
  };
  const std::vector<int> x0 = on(0, 0), xL = on(0, 20), z0 = on(2, 0),
                         ymax = on(1, 9);
  ProblemSpec spec;
  spec.settings =
      parse_settings({{"physics", "thermal"}, {"volume_fraction", 0.9}});
  spec.material.k_w_mk = 45;
  ThermalSpec t;
  t.reference_temperature_c = 20.0;
  t.fixtures.push_back({"sink", x0, 60.0});
  t.loads.push_back(power_load("chip", xL, 7.0));
  t.loads.push_back(flux_load("plate", z0, 2e4));
  t.loads.push_back(convection_load("fin", ymax, 800.0, 15.0));
  spec.thermal = t;
  auto p = prepare_problem(mesh, spec);
  auto phys = make_thermal_physics(p);
  const int E = mesh->element_count();
  std::mt19937 rng(5);
  std::uniform_real_distribution<double> u(0.05, 1.0);
  Eigen::VectorXd rho(E);
  for (int e = 0; e < E; ++e)
    rho[e] = u(rng);
  const Evaluation ev = phys->evaluate(rho, false, nullptr);
  const json &hf = ev.evidence["heat_flow_w"];
  CHECK(hf["flux"][0]["power_w"].get<double>() == doctest::Approx(7.0));
  // 20 mm x 9 mm plate at 2e4 W/m^2.
  CHECK(hf["flux"][1]["power_w"].get<double>() == doctest::Approx(3.6));
  // Convected power recomputed from the nodal temperatures of the film faces.
  const Field &T = field(ev, "temperature");
  double film = 0;
  for (int f : surface_faces_with_nodes(*mesh, ymax)) {
    double mean = 0;
    for (int i = 0; i < 3; ++i)
      mean += T.values[mesh->surface_nodes(i, f)] / 3.0;
    film += 800.0 * mesh->surface_area_m2[f] * (288.15 - mean);
  }
  CHECK(hf["convection"][0]["power_w"].get<double>() ==
        doctest::Approx(film).epsilon(1e-10));
  const double sink = hf["fixtures"][0]["power_w"].get<double>();
  CHECK(std::abs(sink + 7.0 + 3.6 + film) < 1e-9 * 10.6);
  CHECK(hf["relative_imbalance"].get<double>() < 1e-10);
  CHECK(ev.evidence["relative_residual"].get<double>() < 1e-10);
}

TEST_CASE("nonzero fixed temperature shifts the objective, not the gradient") {
  const Box b = make_box(4, 2, 2, 5, 5, 5, 0.1, 9);
  auto mesh = build_tet_mesh(b.points, b.tets);
  const auto x0 =
      nodes_where(b, [](double x, double, double) { return x == 0; });
  const auto xL =
      nodes_where(b, [](double x, double, double) { return x == 20; });
  auto run = [&](double sink_c) {
    ProblemSpec spec;
    spec.settings =
        parse_settings({{"physics", "thermal"}, {"volume_fraction", 0.8}});
    spec.material.k_w_mk = 200;
    ThermalSpec t;
    t.reference_temperature_c = 20.0;
    t.fixtures.push_back({"sink", x0, sink_c});
    t.loads.push_back(power_load("heater", xL, 10.0));
    spec.thermal = t;
    auto phys = make_thermal_physics(prepare_problem(mesh, spec));
    Eigen::VectorXd rho(mesh->element_count());
    for (int e = 0; e < rho.size(); ++e)
      rho[e] = 0.2 + 0.7 * std::fmod(0.37 * e, 1.0);
    return phys->evaluate(rho, true, nullptr);
  };
  const Evaluation cold = run(20.0), warm = run(45.0);
  // A 25 K sink raise lifts every temperature by 25 K (adiabatic sides), so
  // the heat-input-weighted rise grows by exactly 10 W * 25 K.
  CHECK(warm.objective - cold.objective ==
        doctest::Approx(250.0).epsilon(1e-9));
  const Eigen::VectorXd dT =
      field(warm, "temperature").values - field(cold, "temperature").values;
  CHECK((dT.array() - 25.0).abs().maxCoeff() < 1e-9);
  const double scale = cold.objective_gradient.cwiseAbs().maxCoeff();
  REQUIRE(scale > 0);
  CHECK((warm.objective_gradient - cold.objective_gradient)
            .cwiseAbs()
            .maxCoeff() < 1e-9 * scale);
}

TEST_CASE("objective and max-temperature gradients match central "
          "differences") {
  const Box b = make_box(5, 3, 3, 4, 3, 5, 0.12, 21);
  auto mesh = build_tet_mesh(b.points, b.tets);
  auto on = [&](int axis, double v) {
    return nodes_where(b, [=](double x, double y, double z) {
      const double c[3] = {x, y, z};
      return std::abs(c[axis] - v) < 1e-9;
    });
  };
  struct Scenario {
    json method;
    double sink_c;
    bool convection;
  };
  const std::vector<Scenario> scenarios = {
      {{{"method", "simp"}, {"penalty", 3.0}}, 60.0, true},
      {{{"method", "ramp"}, {"ramp_q", 5.0}}, 60.0, true},
      {{{"method", "simp"}, {"penalty", 3.0}}, 20.0, false}, // self-adjoint
      {{{"method", "ramp"}, {"ramp_q", 5.0}}, 5.0, false},
  };
  for (const Scenario &sc : scenarios) {
    CAPTURE(sc.method.dump());
    CAPTURE(sc.sink_c);
    json topo = {{"physics", "thermal"},
                 {"volume_fraction", 0.9},
                 {"max_temperature_c", 90.0}};
    topo.update(sc.method);
    ProblemSpec spec;
    spec.settings = parse_settings(topo);
    spec.material.k_w_mk = 30;
    ThermalSpec t;
    t.reference_temperature_c = 20.0;
    t.fixtures.push_back({"sink", on(0, 0), sc.sink_c});
    t.loads.push_back(power_load("chip", on(0, 20), 4.0));
    if (sc.convection)
      t.loads.push_back(convection_load("fin", on(1, 9), 600.0, 35.0));
    spec.thermal = t;
    auto phys = make_thermal_physics(prepare_problem(mesh, spec));
    const int E = mesh->element_count();
    std::mt19937 rng(17);
    std::uniform_real_distribution<double> u(0.15, 0.95);
    Eigen::VectorXd rho(E);
    for (int e = 0; e < E; ++e)
      rho[e] = u(rng);
    const Evaluation base = phys->evaluate(rho, true, nullptr);
    REQUIRE(base.constraints.size() == 1);
    const Constraint &g = base.constraints[0];
    CHECK(g.name == "max_temperature");
    // KS is an upper bound on the exact nodal peak.
    CHECK(g.response >= field(base, "temperature").values.maxCoeff());
    const double obj_scale = base.objective_gradient.cwiseAbs().maxCoeff();
    const double con_scale = g.gradient.cwiseAbs().maxCoeff();
    REQUIRE(obj_scale > 0);
    REQUIRE(con_scale > 0);
    const double h = 1e-5;
    for (int e = 0; e < E; e += 7) {
      CAPTURE(e);
      Eigen::VectorXd r = rho;
      r[e] = rho[e] + h;
      const Evaluation up = phys->evaluate(r, false, nullptr);
      r[e] = rho[e] - h;
      const Evaluation dn = phys->evaluate(r, false, nullptr);
      const double fd_obj = (up.objective - dn.objective) / (2 * h);
      const double fd_con =
          (up.constraints[0].value - dn.constraints[0].value) / (2 * h);
      CHECK(std::abs(fd_obj - base.objective_gradient[e]) <=
            1e-5 * obj_scale + 1e-5 * std::abs(fd_obj));
      CHECK(std::abs(fd_con - g.gradient[e]) <=
            1e-5 * con_scale + 1e-5 * std::abs(fd_con));
    }
  }
}

TEST_CASE("unconstrained, singular, sourceless and cancelled evaluations "
          "refuse") {
  Slab s;
  ThermalSpec t;
  t.fixtures.push_back({"sink", s.x0, 25.0});
  t.loads.push_back(power_load("heater", s.xL, 10.0));
  auto p = prepare_problem(s.mesh, s.spec(t));
  const int E = s.mesh->element_count();

  Problem loose = *p;
  loose.thermal->fixtures.clear();
  loose.thermal->fixed = make_dirichlet(s.mesh->node_count(), {});
  CHECK(code_of([&] {
          make_thermal_physics(std::make_shared<const Problem>(loose));
        }) == "thermal_unconstrained");

  Problem sourceless = *p;
  sourceless.thermal->fluxes.clear();
  CHECK(code_of([&] {
          make_thermal_physics(std::make_shared<const Problem>(sourceless));
        }) == "zero_load");

  Problem structural = *p;
  structural.settings.physics = PhysicsKind::Structural;
  CHECK(code_of([&] {
          make_thermal_physics(std::make_shared<const Problem>(structural));
        }) == "physics_mismatch");

  // Without a conductivity floor a void density has no conductance at all:
  // the operator is singular and the evaluation refuses instead of returning
  // a temperature; a solid density afterwards solves normally.
  Problem bare = *p;
  bare.interpolation.floor = 0.0;
  auto phys = make_thermal_physics(std::make_shared<const Problem>(bare));
  CHECK(code_of([&] {
          phys->evaluate(Eigen::VectorXd::Zero(E), true, nullptr);
        }) == "singular_system");
  const Evaluation solid =
      phys->evaluate(Eigen::VectorXd::Ones(E), false, nullptr);
  CHECK(solid.objective == doctest::Approx(150.0).epsilon(1e-10));

  std::atomic<bool> stop{true};
  CHECK_THROWS_AS(phys->evaluate(Eigen::VectorXd::Ones(E), true, &stop),
                  EvaluationCancelled);
  stop = false;
  const Evaluation resumed =
      phys->evaluate(Eigen::VectorXd::Ones(E), true, &stop);
  CHECK(resumed.objective == doctest::Approx(150.0).epsilon(1e-10));
  CHECK(code_of([&] {
          phys->evaluate(Eigen::VectorXd::Constant(E, 1.5), false, nullptr);
        }) == "invalid_density");
}
