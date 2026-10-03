#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include "sim/topopt_thermomechanical.h"

#include <algorithm>
#include <array>
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

std::vector<int> on_plane(const Box &b, int axis, double v) {
  return nodes_where(b, [=](double x, double y, double z) {
    const double c[3] = {x, y, z};
    return std::abs(c[axis] - v) < 1e-9;
  });
}

// Total force spread evenly over `nodes`, as a flat 3N vector.
std::vector<double> spread_force(const Box &b, const std::vector<int> &nodes,
                                 const Eigen::Vector3d &total) {
  std::vector<double> f(b.points.size(), 0.0);
  for (int n : nodes)
    for (int d = 0; d < 3; ++d)
      f[3 * n + d] += total[d] / static_cast<double>(nodes.size());
  return f;
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

const Constraint &constraint(const Evaluation &ev, const std::string &name) {
  for (const Constraint &c : ev.constraints)
    if (c.name == name)
      return c;
  throw std::runtime_error("missing constraint " + name);
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

constexpr std::array<bool, 3> kAll{true, true, true};

MaterialInput steel() {
  MaterialInput m;
  m.e_mpa = 200000.0;
  m.nu = 0.3;
  m.k_w_mk = 50.0;
  m.alpha_per_k = 12e-6;
  return m;
}

// 30 x 15 x 15 mm bar held at a uniform 70 C over a 20 C strain-free
// reference: both x ends are fixed at 70 C and the sides are adiabatic.
struct HeatedBar {
  Box box = make_box(6, 3, 3, 5, 5, 5);
  std::shared_ptr<const TetMesh> mesh = build_tet_mesh(box.points, box.tets);
  static constexpr double kRiseK = 50.0;

  ProblemSpec spec(std::vector<FixtureSpec> fixtures) const {
    ProblemSpec s;
    s.settings = parse_settings(
        {{"physics", "thermomechanical"}, {"volume_fraction", 1.0}});
    s.material = steel();
    s.fixtures = std::move(fixtures);
    LoadCaseSpec lc;
    lc.name = "heat";
    lc.forces_n.assign(box.points.size(), 0.0); // thermal load only
    s.load_cases.push_back(lc);
    ThermalSpec t;
    t.reference_temperature_c = 20.0;
    t.fixtures.push_back({"hot_x0", on_plane(box, 0, 0), 70.0});
    t.fixtures.push_back({"hot_xL", on_plane(box, 0, 30), 70.0});
    s.thermal = t;
    return s;
  }
};

// Skewed 20 x 9 x 15 mm block: nonzero sink temperature, a heater, an
// ambient-dependent convection film, and two load cases with distinct
// supports.
struct Coupled {
  Box box = make_box(5, 3, 3, 4, 3, 5, 0.12, 7);
  std::shared_ptr<const TetMesh> mesh = build_tet_mesh(box.points, box.tets);

  std::shared_ptr<const Problem> problem(const json &method,
                                         double shift_c = 0.0) const {
    json topo = {{"physics", "thermomechanical"},
                 {"volume_fraction", 1.0},
                 {"stress_limit_mpa", 60.0},
                 {"max_displacement_mm", 0.02},
                 {"max_temperature_c", 90.0 + shift_c}};
    topo.update(method);
    ProblemSpec spec;
    spec.settings = parse_settings(topo);
    spec.material.e_mpa = 70000.0;
    spec.material.nu = 0.33;
    spec.material.k_w_mk = 30.0;
    spec.material.alpha_per_k = 23e-6;

    LoadCaseSpec bend;
    bend.name = "bend";
    bend.weight = 1.0;
    bend.fixtures.push_back({"root", on_plane(box, 0, 0), kAll});
    bend.forces_n = spread_force(
        box,
        nodes_where(
            box, [](double x, double, double z) { return x == 20 && z == 15; }),
        {0, 0, -400});
    LoadCaseSpec pull;
    pull.name = "pull";
    pull.weight = 0.6;
    pull.fixtures.push_back({"base", on_plane(box, 2, 0), kAll});
    pull.forces_n = spread_force(
        box,
        nodes_where(
            box, [](double, double y, double z) { return y == 9 && z == 15; }),
        {250, -80, 0});
    spec.load_cases = {bend, pull};

    ThermalSpec t;
    t.reference_temperature_c = 20.0 + shift_c;
    t.fixtures.push_back({"sink", on_plane(box, 0, 0), 45.0 + shift_c});
    t.loads.push_back(power_load("chip", on_plane(box, 0, 20), 6.0));
    t.loads.push_back(
        convection_load("fin", on_plane(box, 1, 9), 600.0, 5.0 + shift_c));
    spec.thermal = t;
    return prepare_problem(mesh, spec);
  }

  Eigen::VectorXd density(unsigned seed) const {
    std::mt19937 rng(seed);
    std::uniform_real_distribution<double> u(0.15, 0.95);
    Eigen::VectorXd rho(mesh->element_count());
    for (int e = 0; e < rho.size(); ++e)
      rho[e] = u(rng);
    return rho;
  }
};

const json kSimp = {{"method", "simp"}, {"penalty", 3.0}};
const json kRamp = {{"method", "ramp"}, {"ramp_q", 4.0}};

double max_abs_diff(const Eigen::VectorXd &a, const Eigen::VectorXd &b) {
  REQUIRE(a.size() == b.size());
  return (a - b).cwiseAbs().maxCoeff();
}

// Purely relative comparison (doctest's default adds an absolute 1.0 scale,
// meaningless for SI displacements and joules).
doctest::Approx rel(double value, double tolerance) {
  return doctest::Approx(value).epsilon(tolerance).scale(0.0);
}

} // namespace

TEST_CASE("uniformly heated, axially restrained bar: sigma_xx = -E alpha dT") {
  HeatedBar bar;
  const auto &b = bar.box;
  // Axial ends fixed in x only; y and z symmetry rollers leave the cross
  // section free to expand laterally (uniaxial stress state).
  auto p = prepare_problem(
      bar.mesh,
      bar.spec({{"ends_x0", on_plane(b, 0, 0), {true, false, false}},
                {"ends_xL", on_plane(b, 0, 30), {true, false, false}},
                {"roller_y", on_plane(b, 1, 0), {false, true, false}},
                {"roller_z", on_plane(b, 2, 0), {false, false, true}}}));
  auto phys = make_thermomechanical_physics(p);
  CHECK(phys->kind() == PhysicsKind::Thermomechanical);
  const int E = bar.mesh->element_count();
  const Evaluation ev =
      phys->evaluate(Eigen::VectorXd::Ones(E), false, nullptr);

  const double Ey = 200e9, nu = 0.3, alpha = 12e-6, dT = HeatedBar::kRiseK;
  const double sxx = -Ey * alpha * dT; // -120 MPa
  const Field &T = field(ev, "temperature");
  CHECK((T.values.array() - (293.15 + dT)).abs().maxCoeff() < 1e-9);

  const Field &sig = field(ev, "stress");
  REQUIRE(sig.components == 6);
  for (int e = 0; e < E; ++e) {
    CHECK(sig.values[6 * e] == rel(sxx, 1e-9));
    for (int c = 1; c < 6; ++c)
      CHECK(std::abs(sig.values[6 * e + c]) < 1e-6 * std::abs(sxx));
  }
  const Field &vm = field(ev, "von_mises");
  for (int e = 0; e < E; ++e)
    CHECK(vm.values[e] == rel(-sxx, 1e-9));

  // Lateral expansion (1 + nu) alpha dT, no axial motion.
  const double lat = (1 + nu) * alpha * dT;
  const Field &u = field(ev, "displacement");
  for (int n = 0; n < bar.mesh->node_count(); ++n) {
    const double y = bar.mesh->nodes_m(1, n), z = bar.mesh->nodes_m(2, n);
    CHECK(std::abs(u.values[3 * n]) < 1e-12);
    CHECK(std::abs(u.values[3 * n + 1] - lat * y) < 1e-9 * lat * 0.015);
    CHECK(std::abs(u.values[3 * n + 2] - lat * z) < 1e-9 * lat * 0.015);
  }

  // The x supports carry sigma_xx A each, pushing inward.
  const double A = 0.015 * 0.015;
  const Field &r = field(ev, "reaction");
  double r0 = 0, rL = 0, ry = 0;
  for (int n : on_plane(b, 0, 0))
    r0 += r.values[3 * n];
  for (int n : on_plane(b, 0, 30))
    rL += r.values[3 * n];
  for (int n : on_plane(b, 1, 0))
    ry += r.values[3 * n + 1];
  CHECK(rL == rel(sxx * A, 1e-9));
  CHECK(r0 == rel(-sxx * A, 1e-9));
  CHECK(std::abs(ry) < 1e-6 * std::abs(sxx * A));

  // Compliance u^T K u of the compatible strain (0, lat, lat), and the
  // eigenstrain-subtracted elastic energy 0.5 E (alpha dT)^2 V.
  const double V = 0.03 * A;
  const double lambda = Ey * nu / ((1 + nu) * (1 - 2 * nu));
  const double mu = Ey / (2 * (1 + nu));
  CHECK(ev.objective == rel(4 * lat * lat * (lambda + mu) * V, 1e-9));
  CHECK(ev.objective_unit == "J");
  const json &term = ev.evidence["objective"]["terms"][0];
  CHECK(term["elastic_strain_energy_j"].get<double>() ==
        rel(0.5 * Ey * alpha * dT * alpha * dT * V, 1e-9));
  CHECK(std::abs(term["mechanical_load_work_j"].get<double>()) == 0.0);
  CHECK(term["thermal_load_work_j"].get<double>() == rel(ev.objective, 1e-12));
  CHECK(ev.evidence["coupling"]["mean_temperature_rise_k"].get<double>() ==
        rel(dT, 1e-10));
  CHECK(ev.evidence["elastic"]["cases"][0]["relative_residual"].get<double>() <
        1e-9);
}

TEST_CASE("freely expanding bar: zero stress and alpha L dT elongation") {
  HeatedBar bar;
  const auto &b = bar.box;
  auto single = [&](double x, double y, double z) {
    return nodes_where(b, [=](double px, double py, double pz) {
      return px == x && py == y && pz == z;
    });
  };
  // Statically determinate 3-2-1 supports: no restraint of expansion.
  auto p = prepare_problem(
      bar.mesh, bar.spec({{"origin", single(0, 0, 0), kAll},
                          {"axis_end", single(30, 0, 0), {false, true, true}},
                          {"plane", single(0, 15, 0), {false, false, true}}}));
  auto phys = make_thermomechanical_physics(p);
  const int E = bar.mesh->element_count();
  const Evaluation ev =
      phys->evaluate(Eigen::VectorXd::Ones(E), false, nullptr);

  const double Ey = 200e9, nu = 0.3, alpha = 12e-6, dT = HeatedBar::kRiseK;
  const double a = alpha * dT;
  const Field &u = field(ev, "displacement");
  for (int n = 0; n < bar.mesh->node_count(); ++n)
    for (int d = 0; d < 3; ++d)
      CHECK(std::abs(u.values[3 * n + d] - a * bar.mesh->nodes_m(d, n)) <
            1e-8 * a * 0.03);
  for (int n : on_plane(b, 0, 30))
    CHECK(u.values[3 * n] == rel(alpha * 0.03 * dT, 1e-8));

  const double scale = Ey * a;
  CHECK(field(ev, "stress").values.cwiseAbs().maxCoeff() < 1e-6 * scale);
  CHECK(field(ev, "von_mises").values.maxCoeff() < 1e-6 * scale);
  CHECK(field(ev, "reaction").values.cwiseAbs().maxCoeff() <
        1e-6 * scale * 0.015 * 0.015);

  // Total-load compliance is the thermal-load work 9 K (alpha dT)^2 V (K bulk
  // modulus) although the body is stress free; the elastic energy vanishes.
  const double V = 0.03 * 0.015 * 0.015;
  const double bulk = Ey / (3 * (1 - 2 * nu));
  CHECK(ev.objective == rel(9 * bulk * a * a * V, 1e-9));
  CHECK(ev.evidence["objective"]["terms"][0]["elastic_strain_energy_j"]
            .get<double>() < 1e-12 * ev.objective);
}

TEST_CASE("thermal and mechanical loads superpose at heterogeneous density") {
  const Box b = make_box(5, 3, 3, 4, 3, 5, 0.1, 3);
  auto mesh = build_tet_mesh(b.points, b.tets);
  const std::vector<double> fm =
      spread_force(b, on_plane(b, 0, 20), {30, 0, -300}); // tip load
  auto run = [&](bool heat, bool force) {
    ProblemSpec spec;
    spec.settings = parse_settings({{"physics", "thermomechanical"},
                                    {"method", "ramp"},
                                    {"ramp_q", 3.0},
                                    {"volume_fraction", 1.0}});
    spec.material = steel();
    LoadCaseSpec lc;
    lc.name = "tip";
    lc.fixtures.push_back({"root", on_plane(b, 0, 0), kAll});
    lc.forces_n = force ? fm : std::vector<double>(fm.size(), 0.0);
    spec.load_cases.push_back(lc);
    ThermalSpec t;
    t.reference_temperature_c = 20.0;
    t.fixtures.push_back({"sink", on_plane(b, 0, 0), 20.0});
    if (heat)
      t.loads.push_back(power_load("heater", on_plane(b, 0, 20), 5.0));
    spec.thermal = t;
    auto phys = make_thermomechanical_physics(prepare_problem(mesh, spec));
    Eigen::VectorXd rho(mesh->element_count());
    std::mt19937 rng(11);
    std::uniform_real_distribution<double> u(0.1, 1.0);
    for (int e = 0; e < rho.size(); ++e)
      rho[e] = u(rng);
    return phys->evaluate(rho, false, nullptr);
  };
  const Evaluation th = run(true, false), me = run(false, true),
                   both = run(true, true);
  for (const char *name : {"displacement", "stress", "reaction"}) {
    CAPTURE(name);
    const Eigen::VectorXd &c = field(both, name).values;
    const Eigen::VectorXd sum = field(th, name).values + field(me, name).values;
    CHECK(max_abs_diff(c, sum) < 1e-9 * c.cwiseAbs().maxCoeff());
  }
  CHECK(field(me, "temperature").values.isConstant(293.15, 1e-12));

  // Bilinear compliance with Betti reciprocity f_m . u_th = f_th . u_m.
  const Eigen::VectorXd f =
      Eigen::Map<const Eigen::VectorXd>(fm.data(), static_cast<int>(fm.size()));
  const double fm_uth = f.dot(field(th, "displacement").values);
  auto thermal_work = [](const Evaluation &ev) {
    return ev.evidence["objective"]["terms"][0]["thermal_load_work_j"]
        .get<double>();
  };
  const double fth_um = thermal_work(both) - thermal_work(th);
  CHECK(fth_um == rel(fm_uth, 1e-8));
  CHECK(both.objective == rel(th.objective + me.objective + 2 * fm_uth, 1e-9));
  CHECK(both.evidence["coupling"]["thermal_load_relative_resultant"]
            .get<double>() < 1e-10);
}

TEST_CASE("objective and every constraint gradient match central differences "
          "(SIMP and RAMP)") {
  const Coupled cp;
  for (const json &method : {kSimp, kRamp}) {
    CAPTURE(method.dump());
    auto phys = make_thermomechanical_physics(cp.problem(method));
    const Eigen::VectorXd rho = cp.density(17);
    const int E = static_cast<int>(rho.size());
    const Evaluation base = phys->evaluate(rho, true, nullptr);
    REQUIRE(base.derivatives);
    REQUIRE(base.objective_gradient.size() == E);
    REQUIRE(base.constraints.size() == 5);
    for (const char *name :
         {"max_von_mises@bend", "max_displacement@bend", "max_von_mises@pull",
          "max_displacement@pull", "max_temperature"})
      CHECK_NOTHROW(constraint(base, name));

    std::vector<double> scales;
    for (const Constraint &c : base.constraints) {
      REQUIRE(c.gradient.size() == E);
      scales.push_back(c.gradient.cwiseAbs().maxCoeff());
      REQUIRE(scales.back() > 0);
    }
    const double obj_scale = base.objective_gradient.cwiseAbs().maxCoeff();
    REQUIRE(obj_scale > 0);

    const double h = 1e-5;
    for (int e = 0; e < E; e += 13) {
      CAPTURE(e);
      Eigen::VectorXd r = rho;
      r[e] = rho[e] + h;
      const Evaluation up = phys->evaluate(r, false, nullptr);
      r[e] = rho[e] - h;
      const Evaluation dn = phys->evaluate(r, false, nullptr);
      const double fd = (up.objective - dn.objective) / (2 * h);
      CHECK(std::abs(fd - base.objective_gradient[e]) <=
            1e-5 * obj_scale + 1e-5 * std::abs(fd));
      for (std::size_t i = 0; i < base.constraints.size(); ++i) {
        CAPTURE(base.constraints[i].name);
        const double fdc =
            (up.constraints[i].value - dn.constraints[i].value) / (2 * h);
        CHECK(std::abs(fdc - base.constraints[i].gradient[e]) <=
              2e-5 * scales[i] + 1e-5 * std::abs(fdc));
      }
    }
  }
}

TEST_CASE("shifting the reference and every temperature input is invariant") {
  const Coupled cp;
  const double shift = 37.0;
  auto a = make_thermomechanical_physics(cp.problem(kRamp, 0.0));
  auto b = make_thermomechanical_physics(cp.problem(kRamp, shift));
  const Eigen::VectorXd rho = cp.density(23);
  const Evaluation x = a->evaluate(rho, true, nullptr);
  const Evaluation y = b->evaluate(rho, true, nullptr);

  CHECK(y.objective == rel(x.objective, 1e-10));
  const double gs = x.objective_gradient.cwiseAbs().maxCoeff();
  CHECK(max_abs_diff(x.objective_gradient, y.objective_gradient) < 1e-9 * gs);
  for (const char *name : {"displacement@bend", "displacement@pull",
                           "stress@bend", "stress@pull"}) {
    CAPTURE(name);
    const Eigen::VectorXd &v = field(x, name).values;
    CHECK(max_abs_diff(v, field(y, name).values) <
          1e-9 * v.cwiseAbs().maxCoeff());
  }
  CHECK(max_abs_diff(field(y, "temperature").values,
                     field(x, "temperature").values +
                         Eigen::VectorXd::Constant(
                             field(x, "temperature").values.size(), shift)) <
        1e-9);
  REQUIRE(x.constraints.size() == y.constraints.size());
  for (std::size_t i = 0; i < x.constraints.size(); ++i) {
    const Constraint &cx = x.constraints[i], &cy = y.constraints[i];
    CAPTURE(cx.name);
    REQUIRE(cx.name == cy.name);
    // Compare responses and their density gradients (value = response/limit
    // - 1 uses an absolute kelvin limit for max_temperature).
    const double offset = cx.name == "max_temperature" ? shift : 0.0;
    CHECK(std::abs(cy.response - (cx.response + offset)) <=
          1e-10 * std::abs(cx.response));
    const Eigen::VectorXd dx = cx.gradient * cx.limit,
                          dy = cy.gradient * cy.limit;
    CHECK(max_abs_diff(dx, dy) < 1e-9 * dx.cwiseAbs().maxCoeff());
  }
}

TEST_CASE("factorizations are reused across densities and survive "
          "cancellation; balances are reported") {
  const Coupled cp;
  auto phys = make_thermomechanical_physics(cp.problem(kSimp));
  const Eigen::VectorXd r1 = cp.density(5), r2 = cp.density(6);
  const Evaluation first = phys->evaluate(r1, true, nullptr);

  std::atomic<bool> stop{true};
  CHECK_THROWS_AS(phys->evaluate(r2, true, &stop), EvaluationCancelled);
  stop = false;
  const Evaluation other = phys->evaluate(r2, true, &stop);
  CHECK(std::abs(other.objective - first.objective) > 1e-6 * first.objective);
  const Evaluation again = phys->evaluate(r1, true, &stop);

  auto same = [](const Evaluation &p, const Evaluation &q) {
    CHECK(q.objective == rel(p.objective, 1e-10));
    CHECK(max_abs_diff(p.objective_gradient, q.objective_gradient) <=
          1e-10 * p.objective_gradient.cwiseAbs().maxCoeff());
    REQUIRE(p.constraints.size() == q.constraints.size());
    for (std::size_t i = 0; i < p.constraints.size(); ++i) {
      CHECK(q.constraints[i].value == rel(p.constraints[i].value, 1e-10));
      CHECK(
          max_abs_diff(p.constraints[i].gradient, q.constraints[i].gradient) <=
          1e-10 * p.constraints[i].gradient.cwiseAbs().maxCoeff());
    }
  };
  same(first, again);
  // A fresh evaluator agrees with the reused one.
  same(first, make_thermomechanical_physics(cp.problem(kSimp))
                  ->evaluate(r1, true, nullptr));

  // One symbolic conduction analysis; one numeric factor per evaluation for
  // conduction and for each of the two support sets; none for the cancelled
  // call.
  const json &thermal = again.evidence["thermal"];
  CHECK(thermal["symbolic_analyses"].get<int>() == 1);
  CHECK(thermal["numeric_factorizations"].get<long>() == 3);
  const json &elastic = again.evidence["elastic"];
  CHECK(elastic["support_sets"].get<int>() == 2);
  CHECK(elastic["numeric_factorizations"].get<long>() == 6);

  // Mechanical and thermal balance evidence at the final evaluation.
  for (const json &c : elastic["cases"]) {
    CHECK(c["relative_force_imbalance"].get<double>() < 1e-9);
    CHECK(c["relative_residual"].get<double>() < 1e-9);
  }
  CHECK(thermal["heat_flow_w"]["relative_imbalance"].get<double>() < 1e-9);
  CHECK(again.evidence["coupling"]["thermal_load_relative_resultant"]
            .get<double>() < 1e-10);
  CHECK(field(again, "heat_flux").components == 3);
  CHECK(field(again, "thermal_strain").values.size() ==
        cp.mesh->element_count());
  CHECK(!phys->formulation().empty());
}

TEST_CASE("missing driving force, unrestrained supports and mismatches "
          "refuse") {
  HeatedBar bar;
  const auto &b = bar.box;
  const std::vector<FixtureSpec> restrained = {
      {"ends_x0", on_plane(b, 0, 0), kAll}};

  // alpha = 0: the heated bar's load case would carry no load at all.
  {
    ProblemSpec spec = bar.spec(restrained);
    spec.material.alpha_per_k = 0.0;
    auto p = prepare_problem(bar.mesh, spec);
    CHECK(code_of([&] { make_thermomechanical_physics(p); }) == "zero_load");
  }
  // Temperature equal to the reference and no force: nothing drives it.
  {
    ProblemSpec spec = bar.spec(restrained);
    for (auto &f : spec.thermal->fixtures)
      f.temperature_c = 20.0;
    CHECK(code_of([&] { prepare_problem(bar.mesh, spec); }) == "zero_load");
  }
  // Free-floating body: refused, never pinned arbitrarily.
  {
    ProblemSpec spec =
        bar.spec({{"ends_x0", on_plane(b, 0, 0), {true, false, false}}});
    CHECK(code_of([&] { prepare_problem(bar.mesh, spec); }) ==
          "rigid_body_motion");
  }

  auto p = prepare_problem(bar.mesh, bar.spec(restrained));
  {
    Problem q = *p;
    q.settings.physics = PhysicsKind::Structural;
    CHECK(code_of([&] {
            make_thermomechanical_physics(std::make_shared<const Problem>(q));
          }) == "physics_mismatch");
  }
  {
    Problem q = *p;
    q.structural->cases[0].fixed.values[0] = 1e-4;
    CHECK(code_of([&] {
            make_thermomechanical_physics(std::make_shared<const Problem>(q));
          }) == "invalid_bc");
  }
  {
    // Without conductivity/stiffness floor a void density cannot conduct.
    Problem q = *p;
    q.interpolation.floor = 0.0;
    auto phys =
        make_thermomechanical_physics(std::make_shared<const Problem>(q));
    const int E = bar.mesh->element_count();
    CHECK(code_of([&] {
            phys->evaluate(Eigen::VectorXd::Zero(E), true, nullptr);
          }) == "singular_system");
    const Evaluation ok =
        phys->evaluate(Eigen::VectorXd::Ones(E), false, nullptr);
    CHECK(std::isfinite(ok.objective));
    CHECK(ok.objective > 0);
    CHECK(code_of([&] {
            phys->evaluate(Eigen::VectorXd::Constant(E, 1.5), false, nullptr);
          }) == "invalid_density");
  }
}
