#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include "sim/topopt_flow.h"

#include <algorithm>
#include <atomic>
#include <cmath>
#include <functional>
#include <optional>
#include <random>
#include <string>
#include <vector>

using namespace chudware::sim::topology;
using nlohmann::json;

namespace {

struct Box {
  std::vector<double> points;
  std::vector<int> tets;
  int nx, ny, nz;
  double hx, hy, hz;
  int node(int i, int j, int k) const {
    return (k * (ny + 1) + j) * (nx + 1) + i;
  }
};

// nx*ny*nz cells (mm), six Kuhn tets per cell around the 0-6 diagonal.
Box make_box(int nx, int ny, int nz, double hx, double hy, double hz) {
  Box b{{}, {}, nx, ny, nz, hx, hy, hz};
  for (int k = 0; k <= nz; ++k)
    for (int j = 0; j <= ny; ++j)
      for (int i = 0; i <= nx; ++i) {
        b.points.push_back(i * hx);
        b.points.push_back(j * hy);
        b.points.push_back(k * hz);
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

// Moves every strictly interior node by up to `fraction` of the local cell
// size per axis: a non-symmetric, nonuniform mesh with planar boundaries.
void jitter_interior(Box &b, double fraction, unsigned seed) {
  std::mt19937 rng(seed);
  std::uniform_real_distribution<double> u(-fraction, fraction);
  for (int k = 1; k < b.nz; ++k)
    for (int j = 1; j < b.ny; ++j)
      for (int i = 1; i < b.nx; ++i) {
        const int n = b.node(i, j, k);
        b.points[3 * n] += u(rng) * b.hx;
        b.points[3 * n + 1] += u(rng) * b.hy;
        b.points[3 * n + 2] += u(rng) * b.hz;
      }
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

Eigen::VectorXd random_density(int E, double lo, double hi, unsigned seed) {
  std::mt19937 rng(seed);
  std::uniform_real_distribution<double> u(lo, hi);
  Eigen::VectorXd rho(E);
  for (int e = 0; e < E; ++e)
    rho[e] = u(rng);
  return rho;
}

const Field &field(const Evaluation &ev, const std::string &name) {
  for (const Field &f : ev.fields)
    if (f.name == name)
      return f;
  FAIL("missing field " << name);
  return ev.fields.front();
}

struct FlowCase {
  double viscosity = 1.0;  // Pa s
  double density = 1000.0; // kg/m3
  double speed = 1e-3;     // m/s along +x
  double brinkman = 1e3;
  double outlet_pa = 0.0;
  std::optional<double> reference_length_mm = 1.0;
  json topology = json::object();
};

// Square duct along x: inlet face x = 0, outlet face x = length.
struct Duct {
  Box box;
  double length, width;
  std::shared_ptr<const TetMesh> mesh;
  std::vector<int> inlet, outlet;

  Duct(int nx, int nyz, double length_mm, double width_mm, double jitter = 0.0,
       unsigned seed = 1)
      : box(make_box(nx, nyz, nyz, length_mm / nx, width_mm / nyz,
                     width_mm / nyz)),
        length(length_mm), width(width_mm) {
    if (jitter > 0)
      jitter_interior(box, jitter, seed);
    mesh = build_tet_mesh(box.points, box.tets);
    inlet = nodes_where(
        box, [](double x, double, double) { return std::abs(x) < 1e-9; });
    const double L = length;
    outlet = nodes_where(
        box, [L](double x, double, double) { return std::abs(x - L) < 1e-9; });
  }

  ProblemSpec spec(const FlowCase &c) const {
    json t = {{"physics", "flow"}, {"volume_fraction", 0.6}};
    t.update(c.topology);
    ProblemSpec s;
    s.settings = parse_settings(t);
    FlowSpec f;
    f.viscosity_pa_s = c.viscosity;
    f.density_kg_m3 = c.density;
    f.brinkman_max = c.brinkman;
    f.reference_length_mm = c.reference_length_mm;
    f.inlets.push_back(
        {"in", inlet, Eigen::Vector3d(c.speed, 0, 0), std::nullopt});
    f.outlets.push_back({"out", outlet, c.outlet_pa});
    s.flow = f;
    return s;
  }
};

// Q = a^4 G / (12 mu) * F for a square duct of side a under pressure gradient
// G (Boussinesq series).
double square_duct_factor() {
  const double pi = 3.14159265358979323846;
  double sum = 0;
  for (int n = 1; n < 200; n += 2)
    sum += std::tanh(n * pi / 2.0) / std::pow(n, 5);
  return 1.0 - 192.0 / std::pow(pi, 5) * sum;
}

// Trapezoid-weighted mean nodal pressure over the cross-section plane i.
double section_pressure(const Duct &d, const Eigen::VectorXd &p, int i) {
  double sw = 0, s = 0;
  for (int k = 0; k <= d.box.nz; ++k)
    for (int j = 0; j <= d.box.ny; ++j) {
      const double w = (j == 0 || j == d.box.ny ? 0.5 : 1.0) *
                       (k == 0 || k == d.box.nz ? 0.5 : 1.0);
      s += w * p[d.box.node(i, j, k)];
      sw += w;
    }
  return s / sw;
}

double poiseuille_gradient_error(int nx, int nyz) {
  Duct d(nx, nyz, 4.0, 1.0);
  FlowCase fc;
  auto problem = prepare_problem(d.mesh, d.spec(fc));
  auto phys = make_flow_physics(problem);
  const Evaluation ev = phys->evaluate(
      Eigen::VectorXd::Zero(d.mesh->element_count()), false, nullptr);
  const Eigen::VectorXd &p = field(ev, "pressure").values;
  const double hx = d.length / nx;
  const int i1 = static_cast<int>(std::lround(1.5 / hx));
  const int i2 = static_cast<int>(std::lround(3.0 / hx));
  const double g_num =
      (section_pressure(d, p, i1) - section_pressure(d, p, i2)) /
      ((i2 - i1) * hx * 1e-3);
  const double q =
      ev.evidence["flow"]["flux"]["prescribed_inflow_m3_s"].get<double>();
  const double a = d.width * 1e-3;
  const double g_exact =
      12.0 * fc.viscosity * q / (std::pow(a, 4) * square_duct_factor());
  return std::abs(g_num / g_exact - 1.0);
}

double pressure_drop_of(const Evaluation &ev) {
  return ev.evidence["flow"]["pressure_drop_pa"].get<double>();
}

// Channel with the inlet on x = 0, a natural outlet on the end face x = length
// and a second natural outlet on the top face z = width over 1.5 <= x <= 2.5
// mm. Two open outlets at different pressures drive a flow the prescribed
// inlet speed does not bound: Stokes flow is linear, so the outlet-to-outlet
// speed grows without limit with their pressure difference.
struct TwoOutletChannel {
  Duct duct{8, 4, 4.0, 1.0};
  std::vector<int> side;

  TwoOutletChannel() {
    const double w = duct.width;
    side = nodes_where(duct.box, [w](double x, double, double z) {
      return std::abs(z - w) < 1e-9 && x >= 1.5 - 1e-9 && x <= 2.5 + 1e-9;
    });
  }

  // Water-like fluid in a millimetre channel at a 1 um/s inlet speed: inlet
  // Re = 1000 * 1e-6 * 1e-3 / 1e-3 = 1e-3. End outlet at 5 Pa, side outlet at
  // 5 Pa + delta_pa. volume_fraction 1 admits the port fluid passives.
  ProblemSpec spec(const std::string &physics, double delta_pa) const {
    FlowCase fc;
    fc.viscosity = 1e-3;
    fc.speed = 1e-6;
    fc.brinkman = 1e6;
    fc.outlet_pa = 5.0;
    fc.topology = {{"physics", physics}, {"volume_fraction", 1.0}};
    ProblemSpec s = duct.spec(fc);
    s.flow->outlets.push_back({"side", side, 5.0 + delta_pa});
    if (physics == "cht") {
      s.material.k_w_mk = 200.0;
      s.flow->fluid_k_w_mk = 0.6;
      s.flow->fluid_cp_j_kgk = 4000.0;
      s.flow->inlets[0].temperature_c = 20.0;
      ThermalSpec t;
      t.reference_temperature_c = 20.0;
      ThermalLoadSpec heater;
      heater.label = "heater";
      heater.nodes = nodes_where(duct.box, [](double x, double, double z) {
        return std::abs(z) < 1e-9 && x >= 1.5 - 1e-9 && x <= 3.0 + 1e-9;
      });
      heater.kind = ThermalLoadKind::Flux;
      heater.magnitude_w_m2 = 1000.0;
      t.loads.push_back(heater);
      s.thermal = t;
    }
    return s;
  }
};

// Independent upper bound on |u(x)| over the solved MINI velocity:
// |sum N_a u_a + b u_b| <= max_a |u_a| + |u_b| because N_a >= 0, sum N_a = 1
// and 0 <= b = 256 L0 L1 L2 L3 <= 1.
double mini_speed_bound(const FlowSystem &sys) {
  FlowElementVelocity v;
  double bound = 0;
  for (int e = 0; e < sys.problem().mesh->element_count(); ++e) {
    sys.element_velocity(e, v);
    bound =
        std::max(bound, v.nodal.colwise().norm().maxCoeff() + v.bubble.norm());
  }
  return bound;
}

double reynolds_of(const FlowBoundary &f, double speed) {
  return f.density_kg_m3 * speed * f.reference_length_m / f.viscosity_pa_s;
}

double max_abs_difference(const Eigen::VectorXd &a, const Eigen::VectorXd &b) {
  REQUIRE(a.size() == b.size());
  return (a - b).cwiseAbs().maxCoeff();
}

} // namespace

TEST_CASE("Stokes duct conserves mass and power and honours its boundaries") {
  Duct d(8, 4, 4.0, 1.0, 0.15, 7);
  const int E = d.mesh->element_count();
  const int N = d.mesh->node_count();
  FlowCase fc;
  auto problem = prepare_problem(d.mesh, d.spec(fc));
  auto phys = make_flow_physics(problem);
  CHECK(phys->kind() == PhysicsKind::Flow);
  const Eigen::VectorXd rho = random_density(E, 0.0, 0.6, 11);
  const Evaluation ev = phys->evaluate(rho, false, nullptr);
  const json &fl = ev.evidence["flow"];

  // Mass: inflow equals the prescribed discrete inflow, the walls carry no
  // flux, and the weak continuity rows sum to an exact global balance.
  const double q_in = fl["flux"]["inflow_m3_s"].get<double>();
  const double q_out = fl["flux"]["outflow_m3_s"].get<double>();
  CHECK(q_in > 0);
  CHECK(q_in ==
        doctest::Approx(fl["flux"]["prescribed_inflow_m3_s"].get<double>())
            .epsilon(1e-12));
  CHECK(std::abs(q_in - q_out) <= 1e-7 * q_in);
  CHECK(std::abs(fl["flux"]["wall_flux_m3_s"].get<double>()) <= 1e-14 * q_in);
  CHECK(fl["solver"]["relative_residual"].get<double>() <= 1e-9);

  // Power: dissipation is positive and equals the port power exactly.
  CHECK(ev.objective > 0);
  CHECK(ev.objective_unit == "W");
  CHECK(ev.objective ==
        doctest::Approx(fl["power"]["dissipation_w"].get<double>())
            .epsilon(1e-10));
  CHECK(std::abs(fl["power"]["relative_imbalance"].get<double>()) <= 1e-7);

  // Velocity boundary values: no-slip walls and rims, plug inlet interior.
  const Eigen::VectorXd &u = field(ev, "velocity").values;
  REQUIRE(u.size() == 3 * N);
  CHECK(u.allFinite());
  CHECK(field(ev, "pressure").values.allFinite());
  int walls = 0, inlet_interior = 0;
  for (int n = 0; n < N; ++n) {
    const auto own = problem->node_ownership[n];
    if (own & kNodeFlowWall) {
      ++walls;
      CHECK(u.segment<3>(3 * n).norm() == 0.0);
    } else if (own & kNodeFlowInlet) {
      ++inlet_interior;
      CHECK(u[3 * n] == fc.speed);
      CHECK(u[3 * n + 1] == 0.0);
      CHECK(u[3 * n + 2] == 0.0);
    }
  }
  CHECK(walls > 0);
  CHECK(inlet_interior == 9);

  // Linearity in the inlet speed: dp doubles, dissipation quadruples.
  FlowCase fast = fc;
  fast.speed = 2e-3;
  auto p2 = make_flow_physics(prepare_problem(d.mesh, d.spec(fast)));
  const Evaluation ev2 = p2->evaluate(rho, false, nullptr);
  CHECK(pressure_drop_of(ev2) ==
        doctest::Approx(2.0 * pressure_drop_of(ev)).epsilon(1e-8));
  CHECK(ev2.objective == doctest::Approx(4.0 * ev.objective).epsilon(1e-8));

  // Outlet pressure (Pa) shifts the pressure field by exactly that constant
  // and changes neither velocity nor dissipation.
  FlowCase shifted = fc;
  shifted.outlet_pa = 7.0;
  auto p3 = make_flow_physics(prepare_problem(d.mesh, d.spec(shifted)));
  const Evaluation ev3 = p3->evaluate(rho, false, nullptr);
  const Eigen::VectorXd &u3 = field(ev3, "velocity").values;
  CHECK((u3 - u).cwiseAbs().maxCoeff() <= 1e-9 * u.cwiseAbs().maxCoeff());
  const Eigen::VectorXd shift =
      field(ev3, "pressure").values - field(ev, "pressure").values;
  const double dp = pressure_drop_of(ev);
  for (int n = 0; n < N; ++n)
    if (d.mesh->node_referenced[n])
      CHECK(std::abs(shift[n] - 7.0) <= 1e-7 * (dp + 7.0));
  CHECK(ev3.objective == doctest::Approx(ev.objective).epsilon(1e-9));
  CHECK(pressure_drop_of(ev3) == doctest::Approx(dp).epsilon(1e-8));
}

TEST_CASE("Stokes duct pressure gradient converges to the Poiseuille series") {
  const double coarse = poiseuille_gradient_error(8, 4);
  const double fine = poiseuille_gradient_error(16, 8);
  MESSAGE("relative pressure-gradient error coarse " << coarse << " fine "
                                                     << fine);
  CHECK(fine < 0.6 * coarse);
  CHECK(fine < 0.1);
}

TEST_CASE("Brinkman resistance scales with alpha and the method "
          "interpolation") {
  Duct d(8, 4, 4.0, 1.0);
  const int E = d.mesh->element_count();
  auto drop = [&](double brinkman, double rho, json topology) {
    FlowCase fc;
    fc.brinkman = brinkman;
    fc.topology = std::move(topology);
    auto phys = make_flow_physics(prepare_problem(d.mesh, d.spec(fc)));
    return phys->evaluate(Eigen::VectorXd::Constant(E, rho), false, nullptr);
  };
  // Darcy-dominated porous duct (alpha h^2 / mu >> 1): dp is linear in alpha.
  const Evaluation solid = drop(1e6, 1.0, json::object());
  const Evaluation solid2 = drop(2e6, 1.0, json::object());
  const double ratio = pressure_drop_of(solid2) / pressure_drop_of(solid);
  CHECK(ratio == doctest::Approx(2.0).epsilon(0.01));

  // Method-specific interpolation at rho = 0.5: SIMP(p=3) f = 1/8,
  // RAMP(q=5) f = 0.5 / 3.5 = 1/7.
  const Evaluation simp = drop(1e6, 0.5, json::object());
  const Evaluation ramp =
      drop(1e6, 0.5, json{{"method", "ramp"}, {"ramp_q", 5.0}});
  CHECK(pressure_drop_of(ramp) / pressure_drop_of(simp) ==
        doctest::Approx(8.0 / 7.0).epsilon(0.01));

  // Fluid (rho = 0) is Brinkman-free; solid carries the porous leakage and
  // resists far more than the open duct.
  const Evaluation fluid = drop(1e6, 0.0, json::object());
  CHECK(fluid.evidence["flow"]["power"]["brinkman_w"].get<double>() == 0.0);
  CHECK(fluid.evidence["flow"]["leakage"]["solid_volume_m3"].get<double>() ==
        0.0);
  CHECK(pressure_drop_of(solid) > 100.0 * pressure_drop_of(fluid));
  const json &leak = solid.evidence["flow"]["leakage"];
  CHECK(leak["solid_power_fraction"].get<double>() > 0.99);
  CHECK(leak["solid_mean_speed_m_s"].get<double>() > 0);
  CHECK(leak["darcy_number_solid"].get<double>() == doctest::Approx(1e-6));
}

TEST_CASE("Flow dissipation and pressure-drop gradients match central "
          "differences") {
  Duct d(6, 3, 3.0, 1.0, 0.15, 3);
  const int E = d.mesh->element_count();
  for (const json &method : {json{{"method", "simp"}, {"penalty", 3.0}},
                             json{{"method", "ramp"}, {"ramp_q", 4.0}}}) {
    FlowCase fc;
    fc.topology = method;
    fc.topology["max_pressure_drop_pa"] = 1.0;
    auto problem = prepare_problem(d.mesh, d.spec(fc));
    auto phys = make_flow_physics(problem);
    const Eigen::VectorXd rho = random_density(E, 0.15, 0.85, 5);
    const Evaluation ev = phys->evaluate(rho, true, nullptr);
    REQUIRE(ev.objective_gradient.size() == E);
    REQUIRE(ev.constraints.size() == 1);
    const Constraint &c = ev.constraints[0];
    CHECK(c.name == "pressure_drop");
    CHECK(c.unit == "Pa");
    CHECK(c.response == doctest::Approx(pressure_drop_of(ev)));
    CHECK(c.value == doctest::Approx(c.response / 1.0 - 1.0));
    REQUIRE(c.gradient.size() == E);
    const double g_max = ev.objective_gradient.cwiseAbs().maxCoeff();
    const double c_max = c.gradient.cwiseAbs().maxCoeff();
    CHECK(g_max > 0);
    CHECK(c_max > 0);
    const double h = 1e-4;
    for (int k = 0; k < 8; ++k) {
      const int e = (k * E) / 8 + 3;
      Eigen::VectorXd rp = rho, rm = rho;
      rp[e] += h;
      rm[e] -= h;
      const Evaluation ep = phys->evaluate(rp, false, nullptr);
      const Evaluation em = phys->evaluate(rm, false, nullptr);
      const double fd = (ep.objective - em.objective) / (2 * h);
      const double fdc =
          (ep.constraints[0].value - em.constraints[0].value) / (2 * h);
      CHECK(std::abs(fd - ev.objective_gradient[e]) <=
            1e-4 * std::abs(fd) + 1e-5 * g_max);
      CHECK(std::abs(fdc - c.gradient[e]) <=
            1e-4 * std::abs(fdc) + 1e-5 * c_max);
    }
  }
}

TEST_CASE("Flow refuses invalid regimes, walled ports, mismatches and "
          "honours cancellation") {
  Duct d(4, 2, 4.0, 1.0);
  // Re = 1000 * 0.1 * 1e-3 / 1e-3 = 100: inertia matters, no Stokes claim.
  FlowCase fast;
  fast.viscosity = 1e-3;
  fast.speed = 0.1;
  CHECK(code_of([&] {
          make_flow_physics(prepare_problem(d.mesh, d.spec(fast)));
        }) == "flow_regime_invalid");

  // One cell across: every inlet node is a no-slip rim node.
  Duct thin(4, 1, 4.0, 1.0);
  FlowCase fc;
  CHECK(code_of([&] {
          make_flow_physics(prepare_problem(thin.mesh, thin.spec(fc)));
        }) == "inlet_fully_walled");

  // An outlet of a single triangle at the face corner has only rim nodes: its
  // traction acts on nothing and the pressure level would be singular.
  {
    ProblemSpec s = d.spec(fc);
    const int nx = d.box.nx;
    s.flow->outlets[0].nodes = {d.box.node(nx, 0, 0), d.box.node(nx, 1, 0),
                                d.box.node(nx, 1, 1)};
    CHECK(code_of([&] { make_flow_physics(prepare_problem(d.mesh, s)); }) ==
          "outlet_fully_walled");
  }

  // A structural problem is not a flow problem.
  {
    ProblemSpec s;
    s.settings =
        parse_settings({{"physics", "structural"}, {"volume_fraction", 0.5}});
    s.material.e_mpa = 200000;
    s.material.nu = 0.3;
    s.fixtures.push_back({"root", d.inlet, {true, true, true}});
    LoadCaseSpec lc;
    lc.name = "tip";
    lc.forces_n.assign(3 * d.mesh->node_count(), 0.0);
    for (int n : d.outlet)
      lc.forces_n[3 * n + 2] = -1.0;
    s.load_cases.push_back(lc);
    auto structural = prepare_problem(d.mesh, s);
    CHECK(code_of([&] { make_flow_physics(structural); }) ==
          "physics_mismatch");
    CHECK(code_of([&] { FlowSystem sys(structural); }) == "physics_mismatch");
  }

  auto phys = make_flow_physics(prepare_problem(d.mesh, d.spec(fc)));
  const int E = d.mesh->element_count();
  std::atomic<bool> cancel{true};
  CHECK_THROWS_AS(
      phys->evaluate(Eigen::VectorXd::Constant(E, 0.3), true, &cancel),
      EvaluationCancelled);
  Eigen::VectorXd bad = Eigen::VectorXd::Constant(E, 0.3);
  bad[2] = 1.5;
  CHECK(code_of([&] { phys->evaluate(bad, false, nullptr); }) ==
        "invalid_density");
  // The evaluator stays usable after a refusal or cancellation.
  cancel = false;
  const Evaluation ok =
      phys->evaluate(Eigen::VectorXd::Constant(E, 0.3), true, &cancel);
  CHECK(std::isfinite(ok.objective));
  CHECK(ok.objective_gradient.allFinite());
}

TEST_CASE("Outlet-to-outlet pressure driving cannot bypass the creeping-flow "
          "limit") {
  TwoOutletChannel ch;
  const int E = ch.duct.mesh->element_count();
  const Eigen::VectorXd fluid = Eigen::VectorXd::Zero(E);
  const double low = 2e-3, high = 2.0; // Pa, side outlet above end outlet

  // Low driving: a valid creeping flow dominated by the outlet pressure
  // difference rather than by the prescribed inflow.
  auto low_problem = prepare_problem(ch.duct.mesh, ch.spec("flow", low));
  const FlowBoundary &fb = *low_problem->flow;
  CHECK(fb.reynolds == doctest::Approx(1e-3).epsilon(1e-9));
  FlowSystem low_sys(low_problem);
  low_sys.solve(fluid, nullptr);
  const double re_low = reynolds_of(fb, mini_speed_bound(low_sys));
  MESSAGE("low-driving solution Reynolds bound " << re_low);
  CHECK(re_low <= FlowSystem::kMaxReynolds);
  CHECK(re_low > 10.0 * fb.reynolds);
  Eigen::VectorXd low_velocity;
  low_sys.velocity(low_velocity);

  auto low_flow = make_flow_physics(low_problem);
  const Evaluation ev = low_flow->evaluate(fluid, true, nullptr);
  CHECK(std::isfinite(ev.objective));
  CHECK(ev.objective_gradient.allFinite());
  const Eigen::VectorXd &u = field(ev, "velocity").values;
  CHECK(max_abs_difference(u, low_velocity) <=
        1e-12 * low_velocity.cwiseAbs().maxCoeff());
  // Evidence separates the inlet admission from the enforced solution bound.
  const json &regime = ev.evidence["flow"]["regime"];
  CHECK(regime["reynolds_input"].get<double>() == fb.reynolds);
  CHECK(regime["reynolds_solution_bound"].get<double>() ==
        doctest::Approx(re_low).epsilon(1e-9));
  CHECK(regime["reynolds_solution_bound"].get<double>() <=
        regime["reynolds_limit"].get<double>());
  const json &flux = ev.evidence["flow"]["flux"];
  const double q_in = flux["inflow_m3_s"].get<double>();
  REQUIRE(flux["outlets"].size() == 2);
  const double q_end = flux["outlets"][0]["outflow_m3_s"].get<double>();
  const double q_side = flux["outlets"][1]["outflow_m3_s"].get<double>();
  CHECK(flux["outlets"][1]["prescribed_pressure_pa"].get<double>() ==
        doctest::Approx(5.0 + low).epsilon(1e-15));
  CHECK(q_in == doctest::Approx(flux["prescribed_inflow_m3_s"].get<double>())
                    .epsilon(1e-12));
  // Fluid enters through the higher-pressure side outlet and leaves at the end.
  CHECK(q_side < -5.0 * q_in);
  CHECK(q_end > 5.0 * q_in);
  const double gross = q_in + std::abs(q_end) + std::abs(q_side);
  CHECK(std::abs(q_in - q_end - q_side) <= 1e-7 * gross);
  CHECK(std::abs(flux["wall_flux_m3_s"].get<double>()) <= 1e-12 * gross);
  CHECK(std::abs(
            ev.evidence["flow"]["power"]["relative_imbalance"].get<double>()) <=
        1e-7);

  // High driving: the inlet admission is identical (it cannot see outlet
  // pressures), while Stokes linearity scales the pressure-driven speed by
  // about high / low, far beyond the creeping-flow limit.
  REQUIRE(re_low * (high / low) > 10.0 * FlowSystem::kMaxReynolds);
  auto high_problem = prepare_problem(ch.duct.mesh, ch.spec("flow", high));
  CHECK(high_problem->flow->reynolds == doctest::Approx(fb.reynolds));
  auto high_flow = make_flow_physics(high_problem);
  CHECK(code_of([&] { high_flow->evaluate(fluid, false, nullptr); }) ==
        "flow_regime_invalid");
  CHECK(code_of([&] { high_flow->evaluate(fluid, true, nullptr); }) ==
        "flow_regime_invalid");
  FlowSystem high_sys(high_problem);
  CHECK(code_of([&] { high_sys.solve(fluid, nullptr); }) ==
        "flow_regime_invalid");
  CHECK_FALSE(high_sys.solved());
  // The refused high-Re state is not left behind in the public accessors.
  CHECK(high_sys.state().isZero(0.0));
  FlowElementVelocity v;
  CHECK(code_of([&] { high_sys.element_velocity(0, v); }) == "not_solved");
  CHECK(code_of([&] { high_sys.evidence(); }) == "not_solved");
  CHECK(code_of([&] { high_sys.dissipation(nullptr, nullptr); }) ==
        "not_solved");

  // The same instances accept a design whose Brinkman solid blocks the
  // outlet-to-outlet path (port fluid passives kept): the refusal judged the
  // state, not the boundary data.
  Eigen::VectorXd blocked(E);
  for (int e = 0; e < E; ++e)
    blocked[e] = high_problem->design.element_variable[e] >= 0
                     ? 1.0
                     : high_problem->design.fixed_density[e];
  high_sys.solve(blocked, nullptr);
  CHECK(high_sys.solved());
  const double re_blocked =
      reynolds_of(*high_problem->flow, mini_speed_bound(high_sys));
  MESSAGE("blocked high-driving solution Reynolds bound " << re_blocked);
  CHECK(re_blocked <= FlowSystem::kMaxReynolds);
  Eigen::VectorXd blocked_velocity;
  high_sys.velocity(blocked_velocity);
  const Evaluation evb = high_flow->evaluate(blocked, true, nullptr);
  CHECK(std::isfinite(evb.objective));
  CHECK(evb.objective_gradient.allFinite());
  CHECK(max_abs_difference(field(evb, "velocity").values, blocked_velocity) <=
        1e-12 * blocked_velocity.cwiseAbs().maxCoeff());

  // CHT consumes the same FlowSystem: same refusal, same accepted low state.
  auto cht_high =
      make_flow_physics(prepare_problem(ch.duct.mesh, ch.spec("cht", high)));
  CHECK(cht_high->kind() == PhysicsKind::Cht);
  CHECK(code_of([&] { cht_high->evaluate(fluid, true, nullptr); }) ==
        "flow_regime_invalid");
  auto cht_low =
      make_flow_physics(prepare_problem(ch.duct.mesh, ch.spec("cht", low)));
  const Evaluation evc = cht_low->evaluate(fluid, false, nullptr);
  const Eigen::VectorXd &T = field(evc, "temperature").values;
  CHECK(T.allFinite());
  CHECK(
      evc.evidence["flow"]["regime"]["reynolds_solution_bound"].get<double>() ==
      doctest::Approx(re_low).epsilon(1e-9));
  for (int n : ch.duct.inlet)
    CHECK(T[n] == doctest::Approx(293.15).epsilon(1e-12));
  CHECK(max_abs_difference(field(evc, "velocity").values, low_velocity) <=
        1e-12 * low_velocity.cwiseAbs().maxCoeff());
}
