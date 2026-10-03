#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include "sim/topopt_manufacturing.h"
#include "sim/topopt_optimizer.h"

#include <algorithm>
#include <atomic>
#include <cmath>
#include <functional>
#include <limits>
#include <memory>
#include <string>
#include <vector>

using namespace chudware::sim::topology;
using nlohmann::json;

namespace {

// Box of nx*ny*nz cubes (mm) split into six equal-volume Kuhn tets around the
// 0-6 diagonal. With mirror_x the upper-x half is the exact mirror image.
struct Box {
  std::vector<double> points;
  std::vector<int> tets;
  int nx, ny, nz;
  int node(int i, int j, int k) const {
    return (k * (ny + 1) + j) * (nx + 1) + i;
  }
};

Box make_box(int nx, int ny, int nz, double h, bool mirror_x = false) {
  Box b{{}, {}, nx, ny, nz};
  for (int k = 0; k <= nz; ++k)
    for (int j = 0; j <= ny; ++j)
      for (int i = 0; i <= nx; ++i) {
        b.points.push_back(i * h);
        b.points.push_back(j * h);
        b.points.push_back(k * h);
      }
  static constexpr int off[8][3] = {{0, 0, 0}, {1, 0, 0}, {1, 1, 0}, {0, 1, 0},
                                    {0, 0, 1}, {1, 0, 1}, {1, 1, 1}, {0, 1, 1}};
  static constexpr int split[6][4] = {{0, 1, 2, 6}, {0, 2, 3, 6}, {0, 3, 7, 6},
                                      {0, 7, 4, 6}, {0, 4, 5, 6}, {0, 5, 1, 6}};
  for (int k = 0; k < nz; ++k)
    for (int j = 0; j < ny; ++j)
      for (int i = 0; i < nx; ++i) {
        const bool mirrored = mirror_x && 2 * i >= nx;
        const int ci = mirrored ? nx - 1 - i : i;
        for (const auto &t : split)
          for (int v : t) {
            int x = ci + off[v][0];
            if (mirrored)
              x = nx - x;
            b.tets.push_back(b.node(x, j + off[v][1], k + off[v][2]));
          }
      }
  return b;
}

std::shared_ptr<Problem>
manual_problem(std::shared_ptr<const TetMesh> mesh, const json &topology,
               double filter_radius_m = 0.0, const TyingSpec &ties = {},
               const std::function<ElementRole(int)> &role = nullptr) {
  auto p = std::make_shared<Problem>();
  p->mesh = mesh;
  p->settings = parse_settings(topology);
  p->interpolation = interpolation_for(p->settings);
  const int E = mesh->element_count();
  p->roles.role.assign(E, ElementRole::Design);
  p->roles.reason.assign(E, 0);
  if (role)
    for (int e = 0; e < E; ++e) {
      p->roles.role[e] = role(e);
      if (p->roles.role[e] == ElementRole::Solid)
        p->roles.reason[e] = kReasonPreserve;
      if (p->roles.role[e] == ElementRole::Void)
        p->roles.reason[e] = kReasonExclude;
    }
  p->roles.recount();
  p->design = build_design_map(*mesh, p->roles, ties);
  p->filter = build_density_filter(*mesh, filter_radius_m);
  p->filter_radius_m = filter_radius_m;
  p->filter_radius_source = "explicit";
  return p;
}

std::string code_of(const std::function<void()> &f) {
  try {
    f();
  } catch (const ModelError &e) {
    return e.code();
  }
  return "";
}

double centroid_mm(const TetMesh &m, int e, int axis) {
  return units::m_to_mm(m.centroid_m(axis, e));
}

// Deterministic spatially irregular coefficients in [1-a, 1+a].
Eigen::VectorXd coefficients(int n, double amplitude, double freq = 1.3) {
  Eigen::VectorXd c(n);
  for (int e = 0; e < n; ++e)
    c[e] = 1.0 + amplitude * std::sin(freq * e + 0.4);
  return c;
}

// f = sum c_e / (rho_e + delta): a separable, convex, decreasing response
// (compliance of independent springs). Optional linear constraints.
class Separable : public Physics {
public:
  Separable(std::shared_ptr<const Problem> p, PhysicsKind kind,
            Eigen::VectorXd c, double delta)
      : p_(std::move(p)), kind_(kind), c_(std::move(c)), delta_(delta) {}
  PhysicsKind kind() const override { return kind_; }
  std::string formulation() const override {
    return "closed form sum c_e/(rho_e+delta)";
  }
  Evaluation evaluate(const Eigen::VectorXd &rho, bool d,
                      const std::atomic<bool> *cancel) override {
    if (cancel && cancel->load())
      throw EvaluationCancelled();
    ++calls;
    check_density(*p_, rho);
    const Eigen::VectorXd &V = p_->mesh->volume_m3;
    const int E = static_cast<int>(rho.size());
    Evaluation ev;
    ev.derivatives = d;
    ev.objective_unit = "J";
    if (d)
      ev.objective_gradient.resize(E);
    for (int e = 0; e < E; ++e) {
      const double s = rho[e] + delta_;
      ev.objective += c_[e] / s;
      if (d)
        ev.objective_gradient[e] = -c_[e] / (s * s);
    }
    if (!region.empty()) {
      Constraint g;
      g.name = "region_solid_fraction";
      g.unit = "1";
      g.aggregation = "exact";
      double vr = 0, solid = 0;
      for (int e : region) {
        vr += V[e];
        solid += V[e] * rho[e];
      }
      g.response = solid / vr;
      g.limit = region_limit;
      g.value = g.response / g.limit - 1.0;
      if (d) {
        g.gradient.setZero(E);
        for (int e : region)
          g.gradient[e] = V[e] / (vr * region_limit);
      }
      ev.constraints.push_back(std::move(g));
    }
    if (min_solid > 0) {
      Constraint g;
      g.name = "min_solid_fraction";
      g.unit = "1";
      g.aggregation = "exact";
      g.response = V.dot(rho) / p_->mesh->total_volume_m3;
      g.limit = min_solid;
      g.value = 1.0 - g.response / g.limit;
      if (d)
        g.gradient = (-1.0 / (p_->mesh->total_volume_m3 * min_solid)) * V;
      ev.constraints.push_back(std::move(g));
    }
    return ev;
  }

  std::vector<int> region;
  double region_limit = 1.0;
  double min_solid = 0.0;
  int calls = 0;

private:
  std::shared_ptr<const Problem> p_;
  PhysicsKind kind_;
  Eigen::VectorXd c_;
  double delta_;
};

// Maximized concave response f = sum w_e sqrt(rho_e + delta).
class Concave : public Physics {
public:
  Concave(std::shared_ptr<const Problem> p, Eigen::VectorXd w, double delta)
      : p_(std::move(p)), w_(std::move(w)), delta_(delta) {}
  PhysicsKind kind() const override { return PhysicsKind::Modal; }
  std::string formulation() const override {
    return "closed form sum w_e sqrt(rho_e+delta)";
  }
  Evaluation evaluate(const Eigen::VectorXd &rho, bool d,
                      const std::atomic<bool> *) override {
    check_density(*p_, rho);
    Evaluation ev;
    ev.derivatives = d;
    if (d)
      ev.objective_gradient.resize(rho.size());
    for (Eigen::Index e = 0; e < rho.size(); ++e) {
      const double s = std::sqrt(rho[e] + delta_);
      ev.objective += w_[e] * s;
      if (d)
        ev.objective_gradient[e] = w_[e] / (2.0 * s);
    }
    return ev;
  }

private:
  std::shared_ptr<const Problem> p_;
  Eigen::VectorXd w_;
  double delta_;
};

// Springs in series, one parallel bundle per group:
// C = sum_g F_g^2 / K_g, K_g = sum_{e in g} scale(rho_e) (problem
// interpolation). dC/drho_e = -F_g^2 scale'(rho_e) / K_g^2.
class SeriesSprings : public Physics {
public:
  SeriesSprings(std::shared_ptr<const Problem> p, std::vector<int> group,
                std::vector<double> force)
      : p_(std::move(p)), group_(std::move(group)), force_(std::move(force)) {}
  PhysicsKind kind() const override { return PhysicsKind::Structural; }
  std::string formulation() const override {
    return "closed form series spring compliance";
  }
  Evaluation evaluate(const Eigen::VectorXd &rho, bool d,
                      const std::atomic<bool> *) override {
    check_density(*p_, rho);
    std::vector<double> K(force_.size(), 0.0);
    for (Eigen::Index e = 0; e < rho.size(); ++e)
      K[group_[e]] += p_->interpolation.scale(rho[e]);
    Evaluation ev;
    ev.derivatives = d;
    for (std::size_t g = 0; g < K.size(); ++g)
      ev.objective += force_[g] * force_[g] / K[g];
    if (d) {
      ev.objective_gradient.resize(rho.size());
      for (Eigen::Index e = 0; e < rho.size(); ++e) {
        const int g = group_[e];
        ev.objective_gradient[e] = -force_[g] * force_[g] *
                                   p_->interpolation.scale_derivative(rho[e]) /
                                   (K[g] * K[g]);
      }
    }
    if (extra_constraint) {
      Constraint c;
      c.name = "unsupported_response";
      c.value = -1.0;
      if (d)
        c.gradient.setZero(rho.size());
      ev.constraints.push_back(std::move(c));
    }
    return ev;
  }
  bool extra_constraint = false;

private:
  std::shared_ptr<const Problem> p_;
  std::vector<int> group_;
  std::vector<double> force_;
};

// Root of a total that decreases in mu: log-bisection.
double solve_decreasing(const std::function<double(double)> &total,
                        double target) {
  double lo = -40, hi = 40;
  for (int it = 0; it < 400; ++it) {
    const double mid = 0.5 * (lo + hi);
    if (total(std::pow(10.0, mid)) > target)
      lo = mid;
    else
      hi = mid;
  }
  return std::pow(10.0, 0.5 * (lo + hi));
}

double clamp01(double v) { return std::min(1.0, std::max(0.0, v)); }

json simp_topology(double vf, json extra = json::object()) {
  json t = {{"physics", "structural"},
            {"method", "simp"},
            {"volume_fraction", vf},
            {"change_tolerance", 1e-4},
            {"max_iterations", 400}};
  t.update(extra);
  return t;
}

OptimizerOptions quiet_options() {
  OptimizerOptions o;
  o.stall_window = 1000;
  return o;
}

} // namespace

TEST_CASE("mma reproduces the closed-form optimum of Svanberg's cantilever") {
  // min 0.0624 sum x  s.t. sum a_j / x_j^3 <= 1, 1 <= x <= 10.
  // KKT: x_j = k a_j^(1/4), k = (sum a_j^(1/4))^(1/3), f* = 0.0624 k^4.
  const double a[5] = {61, 37, 19, 7, 1};
  double root_sum = 0;
  for (double v : a)
    root_sum += std::pow(v, 0.25);
  const double k = std::cbrt(root_sum);
  Eigen::VectorXd x = Eigen::VectorXd::Constant(5, 5.0);
  Mma mma(Eigen::VectorXd::Ones(5), Eigen::VectorXd::Constant(5, 10.0), 1);
  Eigen::VectorXd df0 = Eigen::VectorXd::Constant(5, 0.0624), g(1);
  Eigen::MatrixXd dg(1, 5);
  for (int it = 0; it < 100; ++it) {
    g[0] = -1.0;
    for (int j = 0; j < 5; ++j) {
      g[0] += a[j] / (x[j] * x[j] * x[j]);
      dg(0, j) = -3.0 * a[j] / std::pow(x[j], 4);
    }
    const MmaDualReport &r = mma.update(x, df0, g, dg, 10.0);
    CHECK(r.max_artificial == doctest::Approx(0.0));
  }
  for (int j = 0; j < 5; ++j)
    CHECK(x[j] == doctest::Approx(k * std::pow(a[j], 0.25)).epsilon(1e-3));
  CHECK(0.0624 * x.sum() ==
        doctest::Approx(0.0624 * std::pow(k, 4)).epsilon(1e-4));
  double gx = -1.0;
  for (int j = 0; j < 5; ++j)
    gx += a[j] / (x[j] * x[j] * x[j]);
  CHECK(gx <= 1e-6);
}

TEST_CASE("projection and regularized heaviside derivatives match "
          "central differences") {
  const double h = 1e-6;
  for (double beta : {1.0, 4.0, 16.0})
    for (double eta : {0.3, 0.5, 0.7})
      for (double r : {0.05, 0.31, 0.5, 0.77, 0.95}) {
        TanhProjection P{beta, eta};
        const double fd = (P.value(r + h) - P.value(r - h)) / (2 * h);
        CHECK(P.derivative(r) == doctest::Approx(fd).epsilon(1e-6));
      }
  TanhProjection P{8.0, 0.4};
  CHECK(P.value(0.0) == doctest::Approx(0.0));
  CHECK(P.value(1.0) == doctest::Approx(1.0));
  for (double w : {0.25, 0.5, 1.0}) {
    SmoothHeaviside H{w};
    CHECK(H.value(-w) == 0.0);
    CHECK(H.value(w) == doctest::Approx(1.0));
    CHECK(H.value(0.0) == doctest::Approx(0.5));
    CHECK(H.derivative(-w) == 0.0);
    CHECK(H.derivative(w) == 0.0);
    for (double phi : {-0.9 * w, -0.3 * w, 0.1 * w, 0.6 * w}) {
      const double fd = (H.value(phi + h) - H.value(phi - h)) / (2 * h);
      CHECK(H.derivative(phi) == doctest::Approx(fd).epsilon(1e-6));
    }
  }
}

TEST_CASE("density chain gradient through ties, filter, projection and "
          "passives matches central differences") {
  const Box box = make_box(4, 2, 2, 5.0, /*mirror_x=*/true);
  auto mesh = build_tet_mesh(box.points, box.tets);
  TyingSpec ties;
  ties.mirror = {true, false, false};
  ties.mirror_origin_m = Eigen::Vector3d(0.01, 0, 0);
  auto p = manual_problem(mesh, simp_topology(0.4, {{"projection_beta", 4.0}}),
                          0.006, ties, [&](int e) {
                            const double x = centroid_mm(*mesh, e, 0),
                                         y = centroid_mm(*mesh, e, 1),
                                         z = centroid_mm(*mesh, e, 2);
                            if (x < 2 && y < 5 && z < 5)
                              return ElementRole::Solid;
                            if (x > 5 && x < 10 && y > 5 && z > 5)
                              return ElementRole::Void;
                            return ElementRole::Design;
                          });
  const int E = mesh->element_count(), V = p->design.variable_count;
  REQUIRE(p->design.tied_groups > 0);
  DensityChain chain(p);
  chain.set_projection(4.0, 0.4);
  Eigen::VectorXd x(V), rho, g_x;
  for (int v = 0; v < V; ++v)
    x[v] = 0.5 + 0.4 * std::sin(0.7 * v + 0.2);
  Eigen::VectorXd w(E);
  for (int e = 0; e < E; ++e)
    w[e] = 1.0 + 0.5 * std::cos(e);
  auto functional = [&](const Eigen::VectorXd &xx) {
    Eigen::VectorXd r;
    chain.forward(xx, r);
    return w.dot(r.cwiseProduct(r));
  };
  chain.forward(x, rho);
  // Passive elements keep their fixed density; tied elements share one.
  for (int e = 0; e < E; ++e) {
    if (p->design.element_variable[e] < 0)
      CHECK(rho[e] == p->design.fixed_density[e]);
    for (int f = 0; f < E; ++f)
      if (p->design.element_variable[e] >= 0 &&
          p->design.element_variable[e] == p->design.element_variable[f])
        CHECK(rho[e] == doctest::Approx(rho[f]).epsilon(1e-12));
  }
  const Eigen::VectorXd g_rho = 2.0 * w.cwiseProduct(rho);
  chain.backward(g_rho, g_x);
  const double h = 1e-6;
  for (int v = 0; v < V; ++v) {
    Eigen::VectorXd xp = x, xm = x;
    xp[v] += h;
    xm[v] -= h;
    const double fd = (functional(xp) - functional(xm)) / (2 * h);
    CHECK(g_x[v] == doctest::Approx(fd).epsilon(1e-6).scale(1e-9));
  }
}

TEST_CASE("simp converges to the analytic optimum of a separable volume "
          "constrained problem") {
  const Box box = make_box(4, 2, 2, 5.0);
  auto mesh = build_tet_mesh(box.points, box.tets);
  auto p = manual_problem(mesh, simp_topology(0.4));
  const int E = mesh->element_count();
  const Eigen::VectorXd c = coefficients(E, 0.6);
  const double delta = 0.1;
  Separable phys(p, PhysicsKind::Structural, c, delta);
  const OptimizationResult r = optimize(p, phys, quiet_options());
  REQUIRE(r.ok);
  CHECK(r.termination == Termination::Converged);
  CHECK(r.converged);
  CHECK(r.feasible);
  CHECK(r.final_reevaluated);
  // Equal element volumes: rho_e = clamp(sqrt(c_e / mu) - delta), mean = 0.4.
  const double mu = solve_decreasing(
      [&](double m) {
        double s = 0;
        for (int e = 0; e < E; ++e)
          s += clamp01(std::sqrt(c[e] / m) - delta);
        return s / E;
      },
      0.4);
  double f_star = 0;
  for (int e = 0; e < E; ++e) {
    const double rho = clamp01(std::sqrt(c[e] / mu) - delta);
    f_star += c[e] / (rho + delta);
    CHECK(r.density[e] == doctest::Approx(rho).scale(1.0).epsilon(2e-3));
  }
  CHECK(r.final_objective == doctest::Approx(f_star).epsilon(5e-4));
  CHECK(r.volume_fraction <= 0.4 * (1 + 1e-3));
  CHECK(r.final_objective < r.baseline_objective);
  CHECK(r.state.history.front().volume_fraction ==
        doctest::Approx(0.4).epsilon(1e-9));
}

TEST_CASE("ramp handles two simultaneously active constraints exactly") {
  const Box box = make_box(4, 2, 2, 5.0);
  auto mesh = build_tet_mesh(box.points, box.tets);
  auto p = manual_problem(mesh, {{"physics", "structural"},
                                 {"method", "ramp"},
                                 {"volume_fraction", 0.5},
                                 {"change_tolerance", 1e-4},
                                 {"max_iterations", 400}});
  const int E = mesh->element_count();
  const Eigen::VectorXd c = coefficients(E, 0.5, 2.1);
  const double delta = 0.1;
  Separable phys(p, PhysicsKind::Structural, c, delta);
  std::vector<int> inside, outside;
  for (int e = 0; e < E; ++e)
    (centroid_mm(*mesh, e, 0) > 10 ? inside : outside).push_back(e);
  phys.region = inside;
  phys.region_limit = 0.3;
  const OptimizationResult r = optimize(p, phys, quiet_options());
  REQUIRE(r.ok);
  CHECK(r.converged);
  CHECK(r.feasible);
  REQUIRE(r.final_constraints.size() == 2);
  for (const auto &g : r.final_constraints)
    CHECK(std::abs(g.value) < 2e-3); // both limits active
  // KKT: inside mean 0.3 (multiplier lambda1+lambda2), outside mean 0.7.
  auto reference = [&](const std::vector<int> &set, double mean) {
    const double mu = solve_decreasing(
        [&](double m) {
          double s = 0;
          for (int e : set)
            s += clamp01(std::sqrt(c[e] / m) - delta);
          return s / set.size();
        },
        mean);
    for (int e : set)
      CHECK(r.density[e] ==
            doctest::Approx(clamp01(std::sqrt(c[e] / mu) - delta))
                .scale(1.0)
                .epsilon(2e-3));
  };
  reference(inside, 0.3);
  reference(outside, 0.7);
}

TEST_CASE("maximized objective reaches its analytic optimum") {
  const Box box = make_box(4, 2, 2, 5.0);
  auto mesh = build_tet_mesh(box.points, box.tets);
  auto p = manual_problem(mesh, {{"physics", "modal"},
                                 {"method", "simp"},
                                 {"volume_fraction", 0.4},
                                 {"change_tolerance", 1e-4},
                                 {"max_iterations", 400}});
  const int E = mesh->element_count();
  const Eigen::VectorXd w = coefficients(E, 0.7, 0.9);
  const double delta = 0.05;
  Concave phys(p, w, delta);
  const OptimizationResult r = optimize(p, phys, quiet_options());
  REQUIRE(r.ok);
  CHECK(r.sense == ObjectiveSense::Maximize);
  CHECK(r.converged);
  CHECK(r.final_objective > r.baseline_objective);
  // w / (2 sqrt(rho + delta)) = mu  ->  rho = (w / 2 mu)^2 - delta.
  const double mu = solve_decreasing(
      [&](double m) {
        double s = 0;
        for (int e = 0; e < E; ++e)
          s += clamp01(std::pow(w[e] / (2 * m), 2) - delta);
        return s / E;
      },
      0.4);
  for (int e = 0; e < E; ++e)
    CHECK(r.density[e] ==
          doctest::Approx(clamp01(std::pow(w[e] / (2 * mu), 2) - delta))
              .scale(1.0)
              .epsilon(2e-3));
}

TEST_CASE("symmetry ties and passive regions hold through the optimization") {
  const Box box = make_box(4, 2, 2, 5.0, /*mirror_x=*/true);
  auto mesh = build_tet_mesh(box.points, box.tets);
  TyingSpec ties;
  ties.mirror = {true, false, false};
  ties.mirror_origin_m = Eigen::Vector3d(0.01, 0, 0);
  auto p = manual_problem(mesh, simp_topology(0.4), 0.0, ties, [&](int e) {
    const double x = centroid_mm(*mesh, e, 0), y = centroid_mm(*mesh, e, 1),
                 z = centroid_mm(*mesh, e, 2);
    if (x < 2 && y < 5 && z < 5)
      return ElementRole::Solid;
    if (x > 5 && x < 10 && y > 5 && z > 5)
      return ElementRole::Void;
    return ElementRole::Design;
  });
  const int E = mesh->element_count(), V = p->design.variable_count;
  const Eigen::VectorXd c = coefficients(E, 0.6, 1.7);
  const double delta = 0.1;
  Separable phys(p, PhysicsKind::Structural, c, delta);
  const OptimizationResult r = optimize(p, phys, quiet_options());
  REQUIRE(r.ok);
  CHECK(r.converged);
  for (int e = 0; e < E; ++e)
    if (p->design.element_variable[e] < 0)
      CHECK(r.density[e] == p->design.fixed_density[e]);
  // Tied variable optimum: sum_{e in v} c_e / (x + delta)^2 = mu V_v.
  std::vector<double> csum(V, 0.0), vol(V, 0.0);
  double passive = 0;
  for (int e = 0; e < E; ++e) {
    const int v = p->design.element_variable[e];
    if (v < 0) {
      passive += mesh->volume_m3[e] * p->design.fixed_density[e];
      continue;
    }
    csum[v] += c[e];
    vol[v] += mesh->volume_m3[e];
  }
  const double target = 0.4 * mesh->total_volume_m3 - passive;
  const double mu = solve_decreasing(
      [&](double m) {
        double s = 0;
        for (int v = 0; v < V; ++v)
          s += vol[v] * clamp01(std::sqrt(csum[v] / (m * vol[v])) - delta);
        return s;
      },
      target);
  for (int e = 0; e < E; ++e) {
    const int v = p->design.element_variable[e];
    if (v < 0)
      continue;
    CHECK(r.density[e] ==
          doctest::Approx(clamp01(std::sqrt(csum[v] / (mu * vol[v])) - delta))
              .scale(1.0)
              .epsilon(2e-3));
    for (int k = p->design.variable_offsets[v];
         k < p->design.variable_offsets[v + 1]; ++k)
      CHECK(r.density[p->design.variable_elements[k]] == r.density[e]);
  }
  CHECK(r.volume_fraction <= 0.4 * (1 + 1e-3));
}

TEST_CASE("termination boundaries: iteration limit, infeasibility, cancel") {
  const Box box = make_box(4, 2, 2, 5.0);
  auto mesh = build_tet_mesh(box.points, box.tets);
  const int E = mesh->element_count();
  const Eigen::VectorXd c = coefficients(E, 0.6);

  SUBCASE("iteration limit is never convergence") {
    auto p = manual_problem(mesh, simp_topology(0.4, {{"max_iterations", 3}}));
    Separable phys(p, PhysicsKind::Structural, c, 0.1);
    const OptimizationResult r = optimize(p, phys, quiet_options());
    CHECK(r.ok);
    CHECK(r.termination == Termination::MaxIterations);
    CHECK_FALSE(r.converged);
    CHECK(r.iterations == 3);
    CHECK(r.final_reevaluated);
    CHECK(phys.calls == 4); // three iterations + final re-evaluation
  }

  SUBCASE("contradictory limits end infeasible, not converged") {
    auto p = manual_problem(mesh, simp_topology(0.4));
    Separable phys(p, PhysicsKind::Structural, c, 0.1);
    phys.min_solid = 0.8; // needs solid >= 0.8 while the budget is 0.4
    const OptimizationResult r = optimize(p, phys, quiet_options());
    CHECK(r.ok);
    CHECK_FALSE(r.converged);
    CHECK_FALSE(r.feasible);
    CHECK((r.termination == Termination::Infeasible ||
           r.termination == Termination::StuckAtBounds));
    CHECK(r.max_violation > 0.1);
  }

  SUBCASE("cancel retains the last completed iterate") {
    auto p = manual_problem(mesh, simp_topology(0.4));
    Separable phys(p, PhysicsKind::Structural, c, 0.1);
    std::atomic<bool> cancel{false};
    OptimizerOptions o = quiet_options();
    o.cancel = &cancel;
    o.progress = [&](const Progress &pr) {
      if (pr.record.iteration == 2)
        cancel = true;
    };
    const OptimizationResult r = optimize(p, phys, o);
    CHECK(r.termination == Termination::Cancelled);
    CHECK_FALSE(r.ok);
    CHECK_FALSE(r.converged);
    CHECK(r.iterations == 3);
    CHECK(r.state.history.size() == 3);
    REQUIRE(r.has_final);
    CHECK_FALSE(r.final_reevaluated);
    CHECK(phys.calls == 3);
    CHECK(r.final_objective == r.state.history.back().objective);
    CHECK(phase_fraction(*p, r.density) ==
          doctest::Approx(r.state.history.back().volume_fraction));
  }
}

TEST_CASE("checkpoint state round-trips and resumes the same trajectory") {
  const Box box = make_box(4, 2, 2, 5.0);
  auto mesh = build_tet_mesh(box.points, box.tets);
  const int E = mesh->element_count();
  const Eigen::VectorXd c = coefficients(E, 0.6);
  auto p = manual_problem(mesh, simp_topology(0.4, {{"max_iterations", 12}}));
  Separable full_phys(p, PhysicsKind::Structural, c, 0.1);
  OptimizerOptions o = quiet_options();
  o.checkpoint_interval = 1;
  json saved;
  o.progress = [&](const Progress &pr) {
    if (pr.checkpoint && pr.record.iteration == 4)
      saved = to_json(pr.state);
  };
  const OptimizationResult full = optimize(p, full_phys, o);
  REQUIRE(saved.is_object());
  OptimizerOptions resume = quiet_options();
  resume.resume = optimizer_state_from_json(saved);
  CHECK(resume.resume->iteration == 5);
  Separable resumed_phys(p, PhysicsKind::Structural, c, 0.1);
  const OptimizationResult part = optimize(p, resumed_phys, resume);
  CHECK(part.iterations == full.iterations);
  CHECK(part.termination == full.termination);
  for (int e = 0; e < E; ++e)
    CHECK(part.density[e] == doctest::Approx(full.density[e]).epsilon(1e-9));
  CHECK(code_of([&] {
          json bad = saved;
          bad["design"] = json::array({0.5});
          OptimizerOptions o2;
          o2.resume = optimizer_state_from_json(bad);
          optimize(p, resumed_phys, o2);
        }) == "invalid_checkpoint");
}

TEST_CASE("incompatible method/limit pairs refuse before evaluating") {
  const Box box = make_box(4, 1, 1, 5.0);
  auto mesh = build_tet_mesh(box.points, box.tets);
  const int E = mesh->element_count();
  const Eigen::VectorXd c = coefficients(E, 0.3);
  {
    auto p = manual_problem(mesh, {{"physics", "structural"},
                                   {"method", "beso"},
                                   {"volume_fraction", 0.5},
                                   {"stress_limit_mpa", 100.0}});
    Separable phys(p, PhysicsKind::Structural, c, 0.1);
    CHECK(code_of([&] { optimize(p, phys); }) ==
          "method_constraint_unsupported");
    CHECK(phys.calls == 0);
  }
  {
    auto p = manual_problem(mesh, {{"physics", "flow"},
                                   {"method", "level_set"},
                                   {"volume_fraction", 0.5}});
    Separable phys(p, PhysicsKind::Flow, c, 0.1);
    CHECK(code_of([&] { optimize(p, phys); }) == "method_physics_unsupported");
  }
  {
    auto p = manual_problem(mesh, simp_topology(0.5));
    Separable phys(p, PhysicsKind::Thermal, c, 0.1);
    CHECK(code_of([&] { optimize(p, phys); }) == "physics_mismatch");
  }
  {
    // A beso run whose physics returns a response constraint fails by name
    // instead of ignoring it.
    auto p = manual_problem(mesh, {{"physics", "structural"},
                                   {"method", "beso"},
                                   {"volume_fraction", 0.5}});
    std::vector<int> group(E, 0);
    SeriesSprings phys(p, group, {1.0});
    phys.extra_constraint = true;
    const OptimizationResult r = optimize(p, phys);
    CHECK(r.termination == Termination::Failed);
    CHECK(r.error_code == "method_constraint_unsupported");
    CHECK_FALSE(r.ok);
  }
  for (const char *physics : {"structural", "thermal", "modal", "buckling",
                              "flow", "thermomechanical", "cht"})
    for (const char *method : {"simp", "ramp"}) {
      Settings s;
      s.physics = parse_physics(physics);
      s.method = parse_method(method);
      s.objective = objectives_for(s.physics).front();
      CHECK(check_compatibility(s).supported);
    }
}

TEST_CASE("beso removes and reintroduces material on an evolving budget") {
  // Eight series groups (one 5 mm cube each), group force F_g = 5 + g. A
  // linear interpolation gives void elements a real sensitivity, so a group
  // stripped too far by the evolutionary step wins its material back.
  const Box box = make_box(8, 1, 1, 5.0);
  auto mesh = build_tet_mesh(box.points, box.tets);
  const int E = mesh->element_count();
  auto p = manual_problem(mesh, {{"physics", "structural"},
                                 {"method", "beso"},
                                 {"volume_fraction", 0.5},
                                 {"penalty", 1.0},
                                 {"beso_evolution_rate", 0.1},
                                 {"change_tolerance", 0.01},
                                 {"max_iterations", 120}});
  std::vector<int> group(E);
  std::vector<double> force(8);
  for (int g = 0; g < 8; ++g)
    force[g] = 5.0 + g;
  for (int e = 0; e < E; ++e)
    group[e] = static_cast<int>(std::floor(centroid_mm(*mesh, e, 0) / 5.0));
  SeriesSprings phys(p, group, force);
  std::vector<Eigen::VectorXd> designs{Eigen::VectorXd::Ones(E)};
  OptimizerOptions o;
  o.progress = [&](const Progress &pr) {
    if (!pr.last)
      designs.push_back(pr.state.design);
  };
  const OptimizationResult r = optimize(p, phys, o);
  REQUIRE(r.ok);
  CHECK(r.feasible);
  // Removed then reintroduced: some variable goes 1 -> 0 -> 1.
  bool reintroduced = false;
  for (int v = 0; v < E && !reintroduced; ++v) {
    bool removed = false;
    for (std::size_t k = 1; k < designs.size(); ++k) {
      if (designs[k - 1][v] == 1.0 && designs[k][v] == 0.0)
        removed = true;
      if (removed && designs[k - 1][v] == 0.0 && designs[k][v] == 1.0)
        reintroduced = true;
    }
  }
  CHECK(reintroduced);
  bool any_added = false;
  for (const auto &rec : r.state.history)
    any_added = any_added || rec.added > 0;
  CHECK(any_added);
  // Binary designs only, final volume at target within one element.
  for (Eigen::Index e = 0; e < r.density.size(); ++e)
    CHECK((r.density[e] == 0.0 || r.density[e] == 1.0));
  CHECK(std::abs(r.volume_fraction - 0.5) <= 1.0 / E + 1e-12);
  // Integer optimum of sum F_g^2 / n_g with sum n_g = 24, n_g in [1,6]
  // (greedy marginal allocation is exact for this separable convex problem).
  std::vector<int> n(8, 1);
  for (int k = 8; k < 24; ++k) {
    int best = -1;
    double gain = -1;
    for (int g = 0; g < 8; ++g)
      if (n[g] < 6) {
        const double d = force[g] * force[g] * (1.0 / n[g] - 1.0 / (n[g] + 1));
        if (d > gain) {
          gain = d;
          best = g;
        }
      }
    ++n[best];
  }
  double c_int = 0;
  for (int g = 0; g < 8; ++g)
    c_int += force[g] * force[g] / n[g];
  CHECK(r.final_objective <= 1.1 * c_int);
}

TEST_CASE("level set evolves phi to the volume target and diffusion "
          "regularizes the interface") {
  const Box box = make_box(8, 8, 1, 1.0);
  auto mesh = build_tet_mesh(box.points, box.tets);
  const int E = mesh->element_count();
  Eigen::VectorXd c(E);
  for (int e = 0; e < E; ++e)
    c[e] = 1.0 + 0.9 * std::sin(12.9898 * e + 78.233);
  auto run = [&](double tau) {
    auto p = manual_problem(mesh, {{"physics", "structural"},
                                   {"method", "level_set"},
                                   {"volume_fraction", 0.5},
                                   {"level_set_regularization", tau},
                                   {"max_iterations", 60}});
    Separable phys(p, PhysicsKind::Structural, c, 0.3);
    OptimizerOptions o;
    o.level_set_volume_step = 0.05;
    const OptimizationResult r = optimize(p, phys, o);
    REQUIRE(r.ok);
    REQUIRE(r.level_set.size() == E);
    // phi evolved from the full-solid start and holds the target volume.
    CHECK(r.level_set.minCoeff() < 0.0);
    CHECK(r.level_set.maxCoeff() <= 1.0);
    CHECK(r.level_set.minCoeff() >= -1.0);
    CHECK(std::abs(r.volume_fraction - 0.5) < 1e-6);
    // Densities are the regularized Heaviside of phi.
    SmoothHeaviside H{o.level_set_band};
    for (int e = 0; e < E; ++e)
      CHECK(r.density[e] == H.value(r.level_set[e]));
    // The volume target decreases monotonically to the budget.
    for (std::size_t k = 1; k < r.state.history.size(); ++k)
      CHECK(r.state.history[k].volume_target <=
            r.state.history[k - 1].volume_target);
    CHECK(r.state.history.back().volume_target == 0.5);
    return r.state.history.back().interface_area_m2;
  };
  const double rough = run(0.0);
  const double smooth = run(5e-3);
  CHECK(smooth < rough);
}

TEST_CASE("overhang measure gradient is exact and flags unsupported solid") {
  const Box box = make_box(3, 3, 4, 1.0);
  auto mesh = build_tet_mesh(box.points, box.tets);
  const int E = mesh->element_count();
  OverhangSettings s;
  s.build_direction = Eigen::Vector3d(0.2, 0.1, 1.0);
  OverhangModel model(*mesh, s);
  CHECK(model.plate_elements() > 0);
  Eigen::VectorXd rho(E), grad;
  for (int e = 0; e < E; ++e)
    rho[e] = 0.5 + 0.45 * std::sin(1.1 * e + 0.3);
  model.measure(rho, &grad);
  const double h = 1e-6;
  for (int e = 0; e < E; ++e) {
    Eigen::VectorXd rp = rho, rm = rho;
    rp[e] += h;
    rm[e] -= h;
    const double fp = model.measure(rp, nullptr);
    const double fm = model.measure(rm, nullptr);
    const double fd = (fp - fm) / (2 * h);
    // Subtracting O(1) responses cannot resolve a near-zero derivative to a
    // purely relative tolerance; retain the relative check above that noise
    // floor.
    const double roundoff = 32 * std::numeric_limits<double>::epsilon() *
                            std::max(std::abs(fp), std::abs(fm)) / h;
    CHECK(std::abs(grad[e] - fd) <=
          1e-5 * std::max(std::abs(grad[e]), std::abs(fd)) + roundoff);
  }
  OverhangSettings up;
  up.build_direction = Eigen::Vector3d::UnitZ();
  OverhangModel vertical(*mesh, up);
  Eigen::VectorXd column(E), slab(E);
  for (int e = 0; e < E; ++e) {
    const double x = centroid_mm(*mesh, e, 0), y = centroid_mm(*mesh, e, 1),
                 z = centroid_mm(*mesh, e, 2);
    column[e] = (x > 1 && x < 2 && y > 1 && y < 2) ? 1.0 : 0.0;
    slab[e] = z > 3 ? 1.0 : 0.0;
  }
  CHECK(vertical.unsupported_fraction(column, 0.5) == 0.0);
  CHECK(vertical.unsupported_fraction(slab, 0.5) > 0.0);
  CHECK(vertical.measure(slab, nullptr) > vertical.measure(column, nullptr));
}

TEST_CASE("enclosed voids are found by exterior flood fill") {
  const Box box = make_box(3, 3, 3, 1.0);
  auto mesh = build_tet_mesh(box.points, box.tets);
  const int E = mesh->element_count();
  EnclosedVoidFinder finder(mesh);
  auto cube = [&](int e, int i, int j, int k) {
    return std::floor(centroid_mm(*mesh, e, 0)) == i &&
           std::floor(centroid_mm(*mesh, e, 1)) == j &&
           std::floor(centroid_mm(*mesh, e, 2)) == k;
  };
  std::vector<std::uint8_t> cavity(E, 0), vented(E, 0);
  for (int e = 0; e < E; ++e) {
    cavity[e] = cube(e, 1, 1, 1);
    vented[e] = cube(e, 1, 1, 1) || cube(e, 1, 1, 2);
  }
  const EnclosedVoids a = finder.find(cavity);
  CHECK(a.enclosed_elements == 6);
  CHECK(a.regions == 1);
  CHECK(a.enclosed_volume_m3 == doctest::Approx(1e-9));
  const EnclosedVoids b = finder.find(vented);
  CHECK(b.enclosed_elements == 0);
  CHECK(b.open_void_elements == 12);
}

TEST_CASE("beso refuses removals that would enclose a void") {
  // Uniform series springs over a 3x3x3 block: the centre cube is the
  // cheapest to remove only if enclosure were allowed; with
  // no_enclosed_voids every accepted design keeps all voids vented.
  const Box box = make_box(3, 3, 3, 1.0);
  auto mesh = build_tet_mesh(box.points, box.tets);
  const int E = mesh->element_count();
  auto p = manual_problem(mesh, {{"physics", "structural"},
                                 {"method", "beso"},
                                 {"volume_fraction", 0.9},
                                 {"penalty", 1.0},
                                 {"beso_evolution_rate", 0.05},
                                 {"no_enclosed_voids", true},
                                 {"max_iterations", 30}});
  std::vector<int> group(E);
  std::vector<double> force(2);
  force[0] = 0.1;  // centre cube: least loaded, first to be removed
  force[1] = 10.0; // everything else
  for (int e = 0; e < E; ++e) {
    const bool centre = std::floor(centroid_mm(*mesh, e, 0)) == 1 &&
                        std::floor(centroid_mm(*mesh, e, 1)) == 1 &&
                        std::floor(centroid_mm(*mesh, e, 2)) == 1;
    group[e] = centre ? 0 : 1;
  }
  SeriesSprings phys(p, group, force);
  EnclosedVoidFinder finder(mesh);
  int enclosed_seen = 0;
  OptimizerOptions o;
  o.progress = [&](const Progress &pr) {
    enclosed_seen += finder.find(pr.density, 0.5).enclosed_elements;
  };
  const OptimizationResult r = optimize(p, phys, o);
  REQUIRE(r.ok);
  CHECK(enclosed_seen == 0);
  CHECK(r.manufacturing.enclosed.enclosed_elements == 0);
  CHECK(r.manufacturing.rejected_updates > 0);
  CHECK(r.manufacturing.enclosed_void_enforcement == "candidate_acceptance");
}
