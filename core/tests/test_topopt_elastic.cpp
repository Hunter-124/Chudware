#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include "sim/topopt_elastic.h"

#include <cmath>
#include <functional>
#include <random>
#include <string>
#include <vector>

using namespace chudware::sim::topology;
using nlohmann::json;

namespace {

// Box of nx*ny*nz cells (mm), six Kuhn tets per cell.
struct Box {
  std::vector<double> points;
  std::vector<int> tets;
  int nx, ny, nz;
  double hx, hy, hz;
  int node(int i, int j, int k) const {
    return (k * (ny + 1) + j) * (nx + 1) + i;
  }
  double x(int n) const { return points[3 * n]; }
  double y(int n) const { return points[3 * n + 1]; }
  double z(int n) const { return points[3 * n + 2]; }
  int node_count() const { return static_cast<int>(points.size() / 3); }
};

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

// Interior nodes moved deterministically: a non-symmetric mesh whose
// boundary planes stay planar.
std::vector<double> perturbed(const Box &b, double fraction, unsigned seed) {
  std::vector<double> p = b.points;
  std::mt19937 rng(seed);
  std::uniform_real_distribution<double> u(-fraction, fraction);
  const double h[3] = {b.hx, b.hy, b.hz};
  for (int k = 1; k < b.nz; ++k)
    for (int j = 1; j < b.ny; ++j)
      for (int i = 1; i < b.nx; ++i) {
        const int n = b.node(i, j, k);
        for (int d = 0; d < 3; ++d)
          p[3 * n + d] += u(rng) * h[d];
      }
  return p;
}

std::vector<int>
nodes_where(const Box &b,
            const std::function<bool(double, double, double)> &f) {
  std::vector<int> out;
  for (int n = 0; n < b.node_count(); ++n)
    if (f(b.x(n), b.y(n), b.z(n)))
      out.push_back(n);
  return out;
}

// Consistent nodal forces of a uniform traction with resultant `total` (N)
// over the exterior triangles whose corners all lie in `nodes`.
std::vector<double> face_load(const TetMesh &m, const std::vector<int> &nodes,
                              const Eigen::Vector3d &total) {
  std::vector<double> f(3 * m.node_count(), 0.0);
  const std::vector<int> faces = surface_faces_with_nodes(m, nodes);
  double area = 0;
  for (int t : faces)
    area += m.surface_area_m2[t];
  for (int t : faces)
    for (int k = 0; k < 3; ++k)
      for (int d = 0; d < 3; ++d)
        f[3 * m.surface_nodes(k, t) + d] +=
            total[d] * m.surface_area_m2[t] / (3.0 * area);
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

Eigen::VectorXd random_density(int E, unsigned seed) {
  std::mt19937 rng(seed);
  std::uniform_real_distribution<double> u(0.25, 0.95);
  Eigen::VectorXd r(E);
  for (int e = 0; e < E; ++e)
    r[e] = u(rng);
  return r;
}

const Field &field(const Evaluation &ev, const std::string &name) {
  for (const Field &f : ev.fields)
    if (f.name == name)
      return f;
  FAIL("missing field " << name);
  return ev.fields.front();
}

const Constraint &constraint(const Evaluation &ev, const std::string &name) {
  for (const Constraint &c : ev.constraints)
    if (c.name == name)
      return c;
  FAIL("missing constraint " << name);
  return ev.constraints.front();
}

void check_gradient(const std::function<double(const Eigen::VectorXd &)> &f,
                    const Eigen::VectorXd &rho, const Eigen::VectorXd &grad,
                    const std::vector<int> &elements, double h) {
  const double scale = grad.cwiseAbs().maxCoeff();
  REQUIRE(scale > 0);
  for (int e : elements) {
    Eigen::VectorXd r = rho;
    r[e] += h;
    const double fp = f(r);
    r[e] -= 2 * h;
    const double fm = f(r);
    const double fd = (fp - fm) / (2 * h);
    CAPTURE(e);
    CAPTURE(fd);
    CAPTURE(grad[e]);
    CHECK(std::abs(fd - grad[e]) <= 2e-6 * std::abs(grad[e]) + 1e-7 * scale);
  }
}

// Axial bar 20 x 2 x 3 mm along x: x=0 face carries only u_x, the origin
// u_y/u_z and (0,0,3) u_y (statically determinate), traction F on x=20.
struct AxialBar {
  Box box = make_box(5, 2, 2, 4, 1, 1.5);
  std::shared_ptr<const TetMesh> mesh = build_tet_mesh(box.points, box.tets);
  double F = 1000.0;

  ProblemSpec spec(json extra = json::object()) const {
    json t = {{"physics", "structural"}, {"volume_fraction", 0.9}};
    t.update(extra);
    ProblemSpec s;
    s.settings = parse_settings(t);
    s.material.e_mpa = 200000;
    s.material.nu = 0.3;
    s.fixtures.push_back(
        {"x0",
         nodes_where(box, [](double x, double, double) { return x == 0; }),
         {true, false, false}});
    s.fixtures.push_back({"origin", {box.node(0, 0, 0)}, {false, true, true}});
    s.fixtures.push_back({"zline", {box.node(0, 0, 2)}, {false, true, false}});
    LoadCaseSpec lc;
    lc.name = "pull";
    lc.forces_n = face_load(
        *mesh,
        nodes_where(box, [](double x, double, double) { return x == 20; }),
        Eigen::Vector3d(F, 0, 0));
    s.load_cases.push_back(lc);
    return s;
  }
};

// Cantilever 20 x 10 x 10 mm on a perturbed mesh with two load cases on
// distinct supports: A clamps x=0 and pulls the x=20 face down, B clamps
// x=20 and pushes the x=0 face sideways.
struct TwoCase {
  Box box = make_box(4, 2, 2, 5, 5, 5);
  std::shared_ptr<const TetMesh> mesh =
      build_tet_mesh(perturbed(box, 0.18, 11), box.tets);
  std::vector<int> root =
      nodes_where(box, [](double x, double, double) { return x == 0; });
  std::vector<int> tip =
      nodes_where(box, [](double x, double, double) { return x == 20; });

  LoadCaseSpec case_a(double weight) const {
    LoadCaseSpec lc;
    lc.name = "A";
    lc.weight = weight;
    lc.fixtures.push_back({"root", root, {true, true, true}});
    lc.forces_n = face_load(*mesh, tip, Eigen::Vector3d(0, 0, -100));
    return lc;
  }
  LoadCaseSpec case_b(double weight) const {
    LoadCaseSpec lc;
    lc.name = "B";
    lc.weight = weight;
    lc.fixtures.push_back({"tip", tip, {true, true, true}});
    lc.forces_n = face_load(*mesh, root, Eigen::Vector3d(0, 60, 10));
    return lc;
  }
  ProblemSpec spec(std::vector<LoadCaseSpec> cases,
                   json extra = json::object()) const {
    json t = {{"physics", "structural"}, {"volume_fraction", 0.9}};
    t.update(extra);
    ProblemSpec s;
    s.settings = parse_settings(t);
    s.material.e_mpa = 70000;
    s.material.nu = 0.33;
    s.material.density_g_cm3 = 2.7;
    s.load_cases = std::move(cases);
    return s;
  }
};

} // namespace

TEST_CASE("axial bar matches closed-form elasticity, stress and reactions") {
  AxialBar bar;
  auto problem = prepare_problem(
      bar.mesh,
      bar.spec({{"stress_limit_mpa", 100.0}, {"max_displacement_mm", 0.05}}));
  auto physics = make_structural_physics(problem);
  const int E = bar.mesh->element_count();
  const Eigen::VectorXd rho = Eigen::VectorXd::Ones(E);
  const Evaluation ev = physics->evaluate(rho, true, nullptr);

  const double Em = 200e9, nu = 0.3, L = 20e-3, Ly = 2e-3, Lz = 3e-3;
  const double A = Ly * Lz;
  const double sigma = bar.F / A;
  const double eps = sigma / Em;
  CHECK(ev.objective ==
        doctest::Approx(bar.F * bar.F * L / (Em * A)).epsilon(1e-9).scale(0));
  CHECK(ev.objective_unit == "J");

  // Linear displacement field reproduced at every node.
  const Field &u = field(ev, "displacement");
  for (int n = 0; n < bar.mesh->node_count(); ++n) {
    const Eigen::Vector3d x = bar.mesh->nodes_m.col(n);
    CHECK(u.values[3 * n] ==
          doctest::Approx(eps * x[0]).epsilon(1e-8).scale(1e-9));
    CHECK(u.values[3 * n + 1] ==
          doctest::Approx(-nu * eps * x[1]).epsilon(1e-8).scale(1e-9));
    CHECK(u.values[3 * n + 2] ==
          doctest::Approx(-nu * eps * x[2]).epsilon(1e-8).scale(1e-9));
  }
  const Field &vm = field(ev, "von_mises");
  for (int e = 0; e < E; ++e)
    CHECK(vm.values[e] == doctest::Approx(sigma).epsilon(1e-8));

  // Uniform stress: the KS aggregate equals its closed form.
  const Constraint &s = constraint(ev, "max_von_mises");
  const double lim = 100e6;
  CHECK(s.limit == doctest::Approx(lim));
  CHECK(s.response == doctest::Approx(lim * std::sqrt(std::pow(sigma / lim, 2) +
                                                      std::log(double(E)) /
                                                          kElasticKsParameter))
                          .epsilon(1e-8));
  CHECK(s.response >= sigma);
  CHECK(s.value == doctest::Approx(s.response / lim - 1.0));

  const double dmax =
      std::sqrt(std::pow(eps * L, 2) + std::pow(nu * eps * Ly, 2) +
                std::pow(nu * eps * Lz, 2));
  const Constraint &d = constraint(ev, "max_displacement");
  const double dlim = 0.05e-3;
  CHECK(d.response >= dmax * (1 - 1e-9));
  CHECK(d.response <= dlim *
                          std::sqrt(std::pow(dmax / dlim, 2) +
                                    std::log(double(bar.mesh->node_count())) /
                                        kElasticKsParameter) *
                          (1 + 1e-9));

  // Reactions balance the applied pull.
  const json &c0 = ev.evidence["elastic"]["cases"][0];
  CHECK(c0["reaction_n"][0].get<double>() ==
        doctest::Approx(-bar.F).epsilon(1e-9));
  CHECK(std::abs(c0["reaction_n"][1].get<double>()) < 1e-6);
  CHECK(std::abs(c0["reaction_n"][2].get<double>()) < 1e-6);
  CHECK(c0["relative_residual"].get<double>() < 1e-10);
  // Strain energy is half the external work.
  CHECK(c0["strain_energy_j"].get<double>() ==
        doctest::Approx(0.5 * ev.objective).epsilon(1e-10).scale(0));
}

TEST_CASE("uniform eigenstrain on determinate supports expands freely") {
  AxialBar bar;
  auto problem = prepare_problem(bar.mesh, bar.spec());
  auto model = std::make_shared<ElasticModel>(problem);
  ElasticCases cases(model);
  REQUIRE(cases.system_count() == 1);
  const int E = model->element_count();
  const Eigen::VectorXd rho = random_density(E, 3);
  const double a = 1e-3;
  Eigen::Matrix<double, 6, Eigen::Dynamic> e0 =
      Eigen::Matrix<double, 6, Eigen::Dynamic>::Zero(6, E);
  e0.topRows<3>().setConstant(a);
  Eigen::MatrixXd forces = Eigen::MatrixXd::Zero(model->dof_count(), 1);
  Eigen::VectorXd f = forces.col(0);
  model->add_eigenstrain_force(rho, e0, f);
  forces.col(0) = f;
  cases.solve(rho, forces, nullptr);
  const ElasticCaseState &st = cases.state(0);
  for (int n = 0; n < model->node_count(); ++n)
    for (int d = 0; d < 3; ++d)
      CHECK(st.displacement[3 * n + d] ==
            doctest::Approx(a * model->mesh().nodes_m(d, n))
                .epsilon(1e-8)
                .scale(1e-9));
  CHECK(st.reaction.cwiseAbs().maxCoeff() < 1e-9 * f.cwiseAbs().maxCoeff());
  const StressResponse s =
      stress_response(*model, rho, st.displacement, &e0, 1e8, false);
  CHECK(s.max_value < 1e-8 * 200e9 * a);
}

TEST_CASE("multi-case compliance equals independent weighted solves") {
  TwoCase t;
  auto both = prepare_problem(t.mesh, t.spec({t.case_a(2.0), t.case_b(0.5)}));
  auto only_a = prepare_problem(t.mesh, t.spec({t.case_a(1.0)}));
  auto only_b = prepare_problem(t.mesh, t.spec({t.case_b(1.0)}));
  {
    ElasticCases grouped(std::make_shared<ElasticModel>(both));
    CHECK(grouped.case_count() == 2);
    CHECK(grouped.system_count() == 2); // distinct supports -> two factors
  }
  const int E = t.mesh->element_count();
  const Eigen::VectorXd rho = random_density(E, 7);
  const Evaluation ab =
      make_structural_physics(both)->evaluate(rho, true, nullptr);
  const Evaluation a =
      make_structural_physics(only_a)->evaluate(rho, true, nullptr);
  const Evaluation b =
      make_structural_physics(only_b)->evaluate(rho, true, nullptr);
  CHECK(ab.objective == doctest::Approx(2.0 * a.objective + 0.5 * b.objective)
                            .epsilon(1e-10)
                            .scale(0));
  const Eigen::VectorXd g =
      2.0 * a.objective_gradient + 0.5 * b.objective_gradient;
  CHECK((ab.objective_gradient - g).norm() <= 1e-10 * g.norm());
  // Compliance gradient of a stiffness-only design dependence is <= 0.
  CHECK(ab.objective_gradient.maxCoeff() <= 0.0);

  // Work / energy per case from the returned fields.
  for (const auto &[name, single] :
       std::vector<std::pair<std::string, const Evaluation *>>{{"A", &a},
                                                               {"B", &b}}) {
    const Field &u = field(ab, "displacement@" + name);
    const Field &sed = field(ab, "strain_energy_density@" + name);
    const LoadCaseSpec lc = name == "A" ? t.case_a(1) : t.case_b(1);
    const Eigen::VectorXd force = Eigen::Map<const Eigen::VectorXd>(
        lc.forces_n.data(), lc.forces_n.size());
    const double work = force.dot(u.values);
    CHECK(work == doctest::Approx(single->objective).epsilon(1e-10).scale(0));
    CHECK(sed.values.dot(t.mesh->volume_m3) ==
          doctest::Approx(0.5 * work).epsilon(1e-9).scale(0));
    CHECK((u.values - field(*single, "displacement").values).norm() <=
          1e-10 * u.values.norm());
  }
}

TEST_CASE(
    "objective, stress and displacement gradients match central differences") {
  TwoCase t;
  const int E = t.mesh->element_count();
  const Eigen::VectorXd rho = random_density(E, 19);
  const std::vector<int> probe = {0,     7,           E / 5, E / 3,
                                  E / 2, (2 * E) / 3, E - 9, E - 1};
  for (const char *method : {"simp", "ramp"}) {
    CAPTURE(method);
    auto problem =
        prepare_problem(t.mesh, t.spec({t.case_a(2.0), t.case_b(0.5)},
                                       {{"method", method},
                                        {"stress_limit_mpa", 40.0},
                                        {"max_displacement_mm", 0.02}}));
    auto physics = make_structural_physics(problem);
    const Evaluation ev = physics->evaluate(rho, true, nullptr);
    REQUIRE(ev.constraints.size() == 4);
    check_gradient(
        [&](const Eigen::VectorXd &r) {
          return physics->evaluate(r, false, nullptr).objective;
        },
        rho, ev.objective_gradient, probe, 1e-5);
    for (const Constraint &c : ev.constraints) {
      CAPTURE(c.name);
      REQUIRE(c.gradient.size() == E);
      check_gradient(
          [&](const Eigen::VectorXd &r) {
            return constraint(physics->evaluate(r, false, nullptr), c.name)
                .value;
          },
          rho, c.gradient, probe, 1e-5);
    }
  }
}

TEST_CASE("mass objective with a stress limit") {
  TwoCase t;
  auto problem = prepare_problem(
      t.mesh, t.spec({t.case_a(1.0)},
                     {{"objective", "mass"}, {"stress_limit_mpa", 40.0}}));
  auto physics = make_structural_physics(problem);
  const int E = t.mesh->element_count();
  const Eigen::VectorXd rho = random_density(E, 5);
  const Evaluation ev = physics->evaluate(rho, true, nullptr);
  CHECK(ev.objective_unit == "kg");
  CHECK(ev.objective == doctest::Approx(2700.0 * t.mesh->volume_m3.dot(rho))
                            .epsilon(1e-12)
                            .scale(0));
  CHECK((ev.objective_gradient - 2700.0 * t.mesh->volume_m3).norm() <=
        1e-12 * ev.objective_gradient.norm());
  const Constraint &s = constraint(ev, "max_von_mises");
  check_gradient(
      [&](const Eigen::VectorXd &r) {
        return constraint(physics->evaluate(r, false, nullptr), "max_von_mises")
            .value;
      },
      rho, s.gradient, {1, E / 4, E / 2, E - 2}, 1e-5);
}

TEST_CASE("refusals, cancellation and factor reuse") {
  TwoCase t;
  auto problem =
      prepare_problem(t.mesh, t.spec({t.case_a(1.0), t.case_b(1.0)}));
  auto physics = make_structural_physics(problem);
  const int E = t.mesh->element_count();
  const Eigen::VectorXd rho = random_density(E, 23);

  std::atomic<bool> cancel{true};
  CHECK_THROWS_AS(physics->evaluate(rho, true, &cancel), EvaluationCancelled);
  CHECK(code_of([&] {
          physics->evaluate(Eigen::VectorXd::Ones(E - 1), false, nullptr);
        }) == "invalid_density");

  const Evaluation first = physics->evaluate(rho, false, nullptr);
  const Evaluation second = physics->evaluate(rho, false, nullptr);
  CHECK(first.objective == second.objective);
  // Two support sets, one numeric factorization each per evaluation (the
  // cancelled call refused before assembling).
  CHECK(second.evidence["elastic"]["numeric_factorizations"].get<long>() == 4);
  CHECK(second.evidence["elastic"]["support_sets"].get<int>() == 2);

  ProblemSpec no_density = t.spec(
      {t.case_a(1.0)}, {{"objective", "mass"}, {"stress_limit_mpa", 40.0}});
  no_density.material.density_g_cm3.reset();
  auto massless = prepare_problem(t.mesh, no_density);
  CHECK(code_of([&] { make_structural_physics(massless); }) ==
        "material_missing");

  ProblemSpec modal;
  modal.settings =
      parse_settings({{"physics", "modal"}, {"volume_fraction", 0.9}});
  modal.material.e_mpa = 70000;
  modal.material.nu = 0.33;
  modal.material.density_g_cm3 = 2.7;
  modal.fixtures.push_back({"root", t.root, {true, true, true}});
  auto modal_problem = prepare_problem(t.mesh, modal);
  CHECK(code_of([&] { make_structural_physics(modal_problem); }) ==
        "physics_mismatch");
  CHECK(code_of([&] { make_spectral_physics(problem); }) == "physics_mismatch");
}
