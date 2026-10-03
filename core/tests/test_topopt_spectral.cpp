#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include "sim/topopt_elastic.h"

#include <array>
#include <cmath>
#include <functional>
#include <map>
#include <random>
#include <string>
#include <vector>

using namespace chudware::sim::topology;
using nlohmann::json;

namespace {

constexpr double kPi = 3.14159265358979323846;

struct Mesh {
  std::vector<double> points;
  std::vector<int> tets;
  int node_count() const { return static_cast<int>(points.size() / 3); }
  double coord(int n, int d) const { return points[3 * n + d]; }
};

// Kuhn box (six tets per cell); interior nodes optionally perturbed.
Mesh make_box(int nx, int ny, int nz, double h, double perturb = 0,
              unsigned seed = 1) {
  Mesh m;
  auto node = [&](int i, int j, int k) {
    return (k * (ny + 1) + j) * (nx + 1) + i;
  };
  std::mt19937 rng(seed);
  std::uniform_real_distribution<double> u(-perturb, perturb);
  for (int k = 0; k <= nz; ++k)
    for (int j = 0; j <= ny; ++j)
      for (int i = 0; i <= nx; ++i) {
        const bool interior =
            i > 0 && i < nx && j > 0 && j < ny && k > 0 && k < nz;
        for (double c : {i * h, j * h, k * h})
          m.points.push_back(c + (interior ? u(rng) * h : 0.0));
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
            m.tets.push_back(node(i + off[v][0], j + off[v][1], k + off[v][2]));
  return m;
}

// Cube-symmetric subdivision: each cell is split into 24 tets (corner edge,
// face center, cell center), so an n x n x nz column is invariant under the
// square's dihedral group and its two bending modes are exactly repeated.
Mesh make_symmetric_column(int n, int nz, double h) {
  Mesh m;
  std::map<std::array<int, 3>, int> ids;
  auto id = [&](int X, int Y, int Z) {
    const std::array<int, 3> key{X, Y, Z};
    auto it = ids.find(key);
    if (it != ids.end())
      return it->second;
    const int v = m.node_count();
    ids.emplace(key, v);
    for (int c : key)
      m.points.push_back(0.5 * h * c);
    return v;
  };
  static constexpr int quad[4][2] = {{0, 0}, {1, 0}, {1, 1}, {0, 1}};
  for (int k = 0; k < nz; ++k)
    for (int j = 0; j < n; ++j)
      for (int i = 0; i < n; ++i) {
        const int base[3] = {2 * i, 2 * j, 2 * k};
        const int center = id(base[0] + 1, base[1] + 1, base[2] + 1);
        for (int a = 0; a < 3; ++a)
          for (int side = 0; side < 2; ++side) {
            const int b = (a + 1) % 3, c = (a + 2) % 3;
            int fc[3];
            fc[a] = base[a] + 2 * side;
            fc[b] = base[b] + 1;
            fc[c] = base[c] + 1;
            const int face = id(fc[0], fc[1], fc[2]);
            int corner[4];
            for (int q = 0; q < 4; ++q) {
              int p[3];
              p[a] = base[a] + 2 * side;
              p[b] = base[b] + 2 * quad[q][0];
              p[c] = base[c] + 2 * quad[q][1];
              corner[q] = id(p[0], p[1], p[2]);
            }
            for (int q = 0; q < 4; ++q)
              for (int v : {corner[q], corner[(q + 1) % 4], face, center})
                m.tets.push_back(v);
          }
      }
  return m;
}

std::vector<int>
nodes_where(const Mesh &m,
            const std::function<bool(double, double, double)> &f) {
  std::vector<int> out;
  for (int n = 0; n < m.node_count(); ++n)
    if (f(m.coord(n, 0), m.coord(n, 1), m.coord(n, 2)))
      out.push_back(n);
  return out;
}

Mesh scaled(Mesh m, double s) {
  for (double &c : m.points)
    c *= s;
  return m;
}

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
  std::uniform_real_distribution<double> u(0.3, 0.95);
  Eigen::VectorXd r(E);
  for (int e = 0; e < E; ++e)
    r[e] = u(rng);
  return r;
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
    CHECK(std::abs(fd - grad[e]) <= 1e-5 * std::abs(grad[e]) + 1e-6 * scale);
  }
}

std::vector<double> numbers(const json &a) {
  std::vector<double> v;
  for (const auto &x : a)
    v.push_back(x.get<double>());
  return v;
}

std::shared_ptr<const Problem> modal_problem(const Mesh &mesh, double e_mpa,
                                             json extra = json::object()) {
  auto tm = build_tet_mesh(mesh.points, mesh.tets);
  json t = {{"physics", "modal"}, {"volume_fraction", 0.9}};
  t.update(extra);
  ProblemSpec s;
  s.settings = parse_settings(t);
  s.material.e_mpa = e_mpa;
  s.material.nu = 0.3;
  s.material.density_g_cm3 = 2.7;
  s.fixtures.push_back(
      {"base",
       nodes_where(mesh, [](double, double, double z) { return z == 0; }),
       {true, true, true}});
  return prepare_problem(tm, s);
}

// Column along z (axis length nz*h): base face carries u_z only, the origin
// u_x/u_y and the base corner on +x carries u_y; the top face takes a uniform
// load `fz` (negative = compression).
std::shared_ptr<const Problem> column_problem(const Mesh &mesh, double fz,
                                              json extra = json::object()) {
  auto tm = build_tet_mesh(mesh.points, mesh.tets);
  double top = 0, xmax = 0;
  for (int n = 0; n < mesh.node_count(); ++n) {
    top = std::max(top, mesh.coord(n, 2));
    xmax = std::max(xmax, mesh.coord(n, 0));
  }
  json t = {{"physics", "buckling"}, {"volume_fraction", 0.9}};
  t.update(extra);
  ProblemSpec s;
  s.settings = parse_settings(t);
  s.material.e_mpa = 200000;
  s.material.nu = 0.3;
  s.fixtures.push_back(
      {"base",
       nodes_where(mesh, [](double, double, double z) { return z == 0; }),
       {false, false, true}});
  s.fixtures.push_back(
      {"origin",
       nodes_where(mesh, [](double x, double y,
                            double z) { return x == 0 && y == 0 && z == 0; }),
       {true, true, false}});
  s.fixtures.push_back({"xline",
                        nodes_where(mesh,
                                    [&](double x, double y, double z) {
                                      return x == xmax && y == 0 && z == 0;
                                    }),
                        {false, true, false}});
  LoadCaseSpec lc;
  lc.name = "axial";
  lc.forces_n = face_load(
      *tm,
      nodes_where(mesh, [&](double, double, double z) { return z == top; }),
      Eigen::Vector3d(0, 0, fz));
  s.load_cases.push_back(lc);
  return prepare_problem(tm, s);
}

// Elements whose centroid lies in column cell (i, j) of a box with cell h.
std::vector<int> cell_column(const Mesh &m, double h, int i, int j) {
  std::vector<int> out;
  const int E = static_cast<int>(m.tets.size() / 4);
  for (int e = 0; e < E; ++e) {
    double cx = 0, cy = 0;
    for (int a = 0; a < 4; ++a) {
      cx += 0.25 * m.coord(m.tets[4 * e + a], 0);
      cy += 0.25 * m.coord(m.tets[4 * e + a], 1);
    }
    if (static_cast<int>(std::floor(cx / h)) == i &&
        static_cast<int>(std::floor(cy / h)) == j)
      out.push_back(e);
  }
  return out;
}

// Four corner pillars (solid) in a near-void 4 x 4 box; pillar A at density t.
// Pillars B, C, D stay solid, so t sweeps A's modes across the others and
// compresses or spreads the low spectrum.
struct PillarPath {
  Eigen::VectorXd base; // t = 1 design
  Eigen::VectorXd dir;  // d rho / d t (pillar A indicator)
  Eigen::VectorXd at(double t) const { return base + (t - 1.0) * dir; }
};
PillarPath pillar_path(const Mesh &m, double h) {
  const int E = static_cast<int>(m.tets.size() / 4);
  PillarPath p;
  p.base = Eigen::VectorXd::Constant(E, 0.01);
  p.dir = Eigen::VectorXd::Zero(E);
  const int cells[4][2] = {{0, 0}, {3, 0}, {0, 3}, {3, 3}};
  for (int q = 0; q < 4; ++q)
    for (int e : cell_column(m, h, cells[q][0], cells[q][1])) {
      p.base[e] = 1.0;
      if (q == 0)
        p.dir[e] = 1.0;
    }
  return p;
}

// Along rho(t), bisects every change of the eigen request (`signature`) to a
// 1e-10 bracket and requires the objective and every constraint to be
// continuous there and its central difference straddling the change to match
// the analytic directional derivative. Returns the number of changes found.
int check_request_changes(
    Physics &physics, const PillarPath &path,
    const std::function<std::string(const Evaluation &)> &signature, double t0,
    double t1, int samples) {
  auto eval = [&](double t, bool d) {
    return physics.evaluate(path.at(t), d, nullptr);
  };
  int changes = 0;
  double prev_t = t0;
  std::string prev = signature(eval(t0, false));
  for (int i = 1; i <= samples; ++i) {
    const double t = t0 + (t1 - t0) * i / samples;
    const std::string cur = signature(eval(t, false));
    if (cur != prev) {
      double a = prev_t, b = t;
      const std::string sa = prev;
      while (b - a > 1e-10) {
        const double m = 0.5 * (a + b);
        if (signature(eval(m, false)) == sa)
          a = m;
        else
          b = m;
      }
      ++changes;
      const Evaluation ea = eval(a, false), eb = eval(b, false);
      CAPTURE(a);
      CAPTURE(sa);
      CAPTURE(signature(eb));
      CAPTURE(ea.objective);
      CAPTURE(eb.objective);
      REQUIRE(signature(ea) != signature(eb));
      const double mid = 0.5 * (a + b);
      const Evaluation em = eval(mid, true);
      const double slope = em.objective_gradient.dot(path.dir);
      CHECK(std::abs(eb.objective - ea.objective) <=
            1e-9 * std::abs(ea.objective) + 2 * std::abs(slope) * (b - a));
      REQUIRE(ea.constraints.size() == eb.constraints.size());
      for (std::size_t k = 0; k < ea.constraints.size(); ++k)
        CHECK(std::abs(eb.constraints[k].value - ea.constraints[k].value) <=
              1e-9 * std::abs(ea.constraints[k].response /
                              ea.constraints[k].limit) +
                  2 * std::abs(em.constraints[k].gradient.dot(path.dir)) *
                      (b - a));
      // The central difference straddles the request change.
      const double h = 1e-5;
      const Evaluation ep = eval(mid + h, false), en = eval(mid - h, false);
      const double fd = (ep.objective - en.objective) / (2 * h);
      CAPTURE(fd);
      CAPTURE(slope);
      CHECK(std::abs(fd - slope) <=
            1e-5 * std::abs(slope) + 1e-9 * std::abs(em.objective));
      for (std::size_t k = 0; k < em.constraints.size(); ++k) {
        const double cfd =
            (ep.constraints[k].value - en.constraints[k].value) / (2 * h);
        const double cslope = em.constraints[k].gradient.dot(path.dir);
        CHECK(std::abs(cfd - cslope) <= 1e-5 * std::abs(cslope) + 1e-9);
      }
    }
    prev = cur;
    prev_t = t;
  }
  return changes;
}

} // namespace

TEST_CASE("modal frequencies follow exact scaling laws and modes are "
          "mass-normalized") {
  const Mesh mesh = make_box(2, 3, 5, 4.0, 0.15, 3);
  auto problem = modal_problem(mesh, 70000);
  auto physics = make_spectral_physics(problem);
  const int E = problem->mesh->element_count();
  const Eigen::VectorXd ones = Eigen::VectorXd::Ones(E);
  const Evaluation solid = physics->evaluate(ones, false, nullptr);
  CHECK(solid.objective_unit == "Hz");
  const std::vector<double> f1 = numbers(solid.evidence["frequencies_hz"]);
  REQUIRE(f1.size() >= 2);
  CHECK(solid.objective <= f1[0] * (1 + 1e-12)); // soft-min is conservative

  // Uniform density r scales every eigenvalue by s(r)/m(r).
  const double r = 0.5;
  const double ratio =
      std::sqrt(problem->interpolation.scale(r) /
                MassInterpolation{problem->interpolation.floor}.value(r));
  const Evaluation half =
      physics->evaluate(Eigen::VectorXd::Constant(E, r), false, nullptr);
  const std::vector<double> fr = numbers(half.evidence["frequencies_hz"]);
  REQUIRE(fr.size() >= 2);
  for (int j = 0; j < 2; ++j)
    CHECK(fr[j] == doctest::Approx(ratio * f1[j]).epsilon(1e-8).scale(0));
  CHECK(half.objective ==
        doctest::Approx(ratio * solid.objective).epsilon(1e-8).scale(0));

  // Geometric similarity: twice the size, half the frequency. E x4: twice.
  const Evaluation big =
      make_spectral_physics(modal_problem(scaled(mesh, 2.0), 70000))
          ->evaluate(ones, false, nullptr);
  CHECK(big.objective ==
        doctest::Approx(0.5 * solid.objective).epsilon(1e-8).scale(0));
  const Evaluation stiff = make_spectral_physics(modal_problem(mesh, 280000))
                               ->evaluate(ones, false, nullptr);
  CHECK(stiff.objective ==
        doctest::Approx(2.0 * solid.objective).epsilon(1e-8).scale(0));

  // Mass-normalized, K-orthogonal mode fields.
  auto model = std::make_shared<ElasticModel>(problem);
  ElasticSystem sys(model, problem->structural->shared_fixed);
  sys.assemble(ones, nullptr);
  Eigen::SparseMatrix<double> M = sys.pattern().pattern();
  assemble_mass(sys, ones, 2700.0, M);
  const int J = static_cast<int>(f1.size());
  Eigen::MatrixXd Phi(sys.reduction().free_size(), J);
  for (int j = 0; j < J; ++j) {
    const std::string name = "mode_shape_" + std::to_string(j + 1);
    const Field *fld = nullptr;
    for (const Field &f : solid.fields)
      if (f.name == name)
        fld = &f;
    REQUIRE(fld != nullptr);
    Eigen::VectorXd free;
    sys.reduction().restrict_vector(fld->values, free);
    Phi.col(j) = free;
  }
  const Eigen::MatrixXd PMP = Phi.transpose() * (M * Phi);
  const Eigen::MatrixXd PKP = Phi.transpose() * (sys.stiffness() * Phi);
  CHECK((PMP - Eigen::MatrixXd::Identity(J, J)).cwiseAbs().maxCoeff() < 1e-8);
  for (int j = 0; j < J; ++j) {
    const double omega2 = std::pow(2 * kPi * f1[j], 2);
    CHECK(PKP(j, j) == doctest::Approx(omega2).epsilon(1e-8).scale(0));
    for (int k = 0; k < J; ++k)
      if (k != j)
        CHECK(std::abs(PKP(j, k)) < 1e-8 * omega2);
  }
}

TEST_CASE("modal objective and frequency constraint gradients match central "
          "differences") {
  const Mesh mesh = make_box(2, 3, 5, 4.0, 0.15, 9);
  auto problem = modal_problem(mesh, 70000, {{"min_frequency_hz", 5000.0}});
  auto physics = make_spectral_physics(problem);
  const int E = problem->mesh->element_count();
  const Eigen::VectorXd rho = random_density(E, 13);
  const Evaluation ev = physics->evaluate(rho, true, nullptr);
  REQUIRE(ev.objective_gradient.size() == E);
  REQUIRE(ev.constraints.size() == 1);
  const std::vector<int> probe = {0, E / 6, E / 3, E / 2, (3 * E) / 4, E - 1};
  check_gradient(
      [&](const Eigen::VectorXd &r) {
        return physics->evaluate(r, false, nullptr).objective;
      },
      rho, ev.objective_gradient, probe, 1e-5);
  check_gradient(
      [&](const Eigen::VectorXd &r) {
        return physics->evaluate(r, false, nullptr).constraints[0].value;
      },
      rho, ev.constraints[0].gradient, probe, 1e-5);
}

TEST_CASE("repeated bending pair is aggregated smoothly and symmetrically") {
  const Mesh mesh = make_symmetric_column(2, 4, 5.0);
  auto problem = modal_problem(mesh, 70000);
  auto physics = make_spectral_physics(problem);
  const TetMesh &tm = *problem->mesh;
  const int E = tm.element_count();
  const Eigen::VectorXd rho = Eigen::VectorXd::Constant(E, 0.6);
  const Evaluation ev = physics->evaluate(rho, true, nullptr);
  const std::vector<double> f = numbers(ev.evidence["frequencies_hz"]);
  REQUIRE(f.size() >= 2);
  CHECK(std::abs(f[1] - f[0]) <= 1e-8 * f[0]); // exact multiplicity two
  REQUIRE(ev.evidence["clusters"].size() >= 1);
  CHECK(ev.evidence["clusters"][0]["multiplicity"].get<int>() == 2);
  // The pair shares the soft-min weight: no single eigenvector is selected.
  const std::vector<double> w = numbers(ev.evidence["aggregate_weights"]);
  CHECK(w[0] == doctest::Approx(w[1]).epsilon(1e-6));

  // Differentiable at the multiplicity: central differences agree.
  const std::vector<int> probe = {E / 7, E / 3, E / 2, E - 5};
  check_gradient(
      [&](const Eigen::VectorXd &r) {
        return physics->evaluate(r, false, nullptr).objective;
      },
      rho, ev.objective_gradient, probe, 1e-5);

  // The aggregate is a symmetric function of the pair, so its gradient is
  // invariant under the x <-> y mirror of the column.
  const double gmax = ev.objective_gradient.cwiseAbs().maxCoeff();
  int checked = 0;
  for (int e = 0; e < E; e += 5) {
    const Eigen::Vector3d c = tm.centroid_m.col(e);
    const Eigen::Vector3d mirror(c[1], c[0], c[2]);
    for (int g = 0; g < E; ++g)
      if ((tm.centroid_m.col(g) - mirror).norm() < 1e-9) {
        CHECK(std::abs(ev.objective_gradient[e] - ev.objective_gradient[g]) <=
              1e-6 * gmax);
        ++checked;
        break;
      }
  }
  CHECK(checked > 10);
}

TEST_CASE(
    "buckling factor scales inversely with load and quadratically with size") {
  const Mesh mesh = make_box(2, 2, 8, 2.0);
  const int E = static_cast<int>(mesh.tets.size() / 4);
  const Eigen::VectorXd ones = Eigen::VectorXd::Ones(E);
  const Evaluation p1 = make_spectral_physics(column_problem(mesh, -1000))
                            ->evaluate(ones, false, nullptr);
  const Evaluation p2 = make_spectral_physics(column_problem(mesh, -2000))
                            ->evaluate(ones, false, nullptr);
  CHECK(p1.objective_unit == "1");
  CHECK(p1.objective > 0);
  CHECK(p2.objective ==
        doctest::Approx(0.5 * p1.objective).epsilon(1e-8).scale(0));
  const double min1 = p1.evidence["minimum_load_factor"].get<double>();
  const double min2 = p2.evidence["minimum_load_factor"].get<double>();
  CHECK(min2 == doctest::Approx(0.5 * min1).epsilon(1e-8).scale(0));
  CHECK(p1.objective <= min1 * (1 + 1e-12));
  // Same load on a twice-larger column: stiffness x2, prestress / 4.
  const Evaluation big =
      make_spectral_physics(column_problem(scaled(mesh, 2.0), -1000))
          ->evaluate(ones, false, nullptr);
  CHECK(big.objective ==
        doctest::Approx(4.0 * p1.objective).epsilon(1e-8).scale(0));
}

TEST_CASE("tension has no buckling factor") {
  const Mesh mesh = make_box(2, 2, 8, 2.0);
  auto physics = make_spectral_physics(column_problem(mesh, +1000));
  const int E = static_cast<int>(mesh.tets.size() / 4);
  CHECK(code_of([&] {
          physics->evaluate(Eigen::VectorXd::Ones(E), false, nullptr);
        }) == "no_compressive_buckling");
}

TEST_CASE("buckling gradient includes the prestress adjoint chain") {
  const Mesh mesh = make_box(2, 2, 6, 2.0, 0.15, 21);
  auto problem = column_problem(mesh, -1000, {{"min_buckling_factor", 50.0}});
  auto physics = make_spectral_physics(problem);
  const int E = problem->mesh->element_count();
  const Eigen::VectorXd rho = random_density(E, 31);
  const Evaluation ev = physics->evaluate(rho, true, nullptr);
  REQUIRE(ev.objective_gradient.size() == E);
  REQUIRE(ev.constraints.size() == 1);
  const std::vector<int> probe = {0, E / 5, E / 3, E / 2, (4 * E) / 5, E - 1};
  check_gradient(
      [&](const Eigen::VectorXd &r) {
        return physics->evaluate(r, false, nullptr).objective;
      },
      rho, ev.objective_gradient, probe, 1e-5);
  check_gradient(
      [&](const Eigen::VectorXd &r) {
        return physics->evaluate(r, false, nullptr).constraints[0].value;
      },
      rho, ev.constraints[0].gradient, probe, 1e-5);
}

TEST_CASE("spectral refusals and cancellation") {
  const Mesh mesh = make_box(2, 3, 4, 4.0);
  ProblemSpec s;
  s.settings = parse_settings({{"physics", "modal"}, {"volume_fraction", 0.9}});
  s.material.e_mpa = 70000;
  s.material.nu = 0.3;
  s.fixtures.push_back(
      {"base",
       nodes_where(mesh, [](double, double, double z) { return z == 0; }),
       {true, true, true}});
  // prepare_problem itself requires density for modal.
  CHECK(code_of([&] {
          prepare_problem(build_tet_mesh(mesh.points, mesh.tets), s);
        }) == "material_missing");

  auto problem = modal_problem(mesh, 70000);
  auto physics = make_spectral_physics(problem);
  const int E = problem->mesh->element_count();
  std::atomic<bool> cancel{true};
  CHECK_THROWS_AS(physics->evaluate(Eigen::VectorXd::Ones(E), true, &cancel),
                  EvaluationCancelled);
  CHECK(code_of([&] {
          physics->evaluate(Eigen::VectorXd::Constant(E, 1.5), false, nullptr);
        }) == "invalid_density");
  CHECK(code_of([&] { make_structural_physics(problem); }) ==
        "physics_mismatch");
}

TEST_CASE("shear prestress buckles although every geometric-stiffness diagonal "
          "is zero") {
  // Four face-connected tets (mm). Only node 2's x DOF moves: the first tet
  // carries pure shear sigma_xy = 6 MPa, the others are unstrained, and the
  // last tet touches no boundary node, so it stays a design element.
  Mesh mesh;
  mesh.points = {0, 0, 0,  1, 0, 0,  0, 1, 0,  0, 0,
                 1, 0, -1, 0, 1, -1, 1, 0, -2, 1};
  mesh.tets = {0, 1, 2, 3, 0, 1, 3, 4, 1, 3, 4, 5, 3, 4, 5, 6};
  ProblemSpec s;
  s.settings =
      parse_settings({{"physics", "buckling"}, {"volume_fraction", 1.0}});
  s.material.e_mpa = 200000;
  s.material.nu = 0.3;
  s.fixtures.push_back({"origin", {0}, {true, true, true}});
  s.fixtures.push_back({"roller", {1}, {false, true, true}});
  s.fixtures.push_back({"pin", {2}, {false, false, true}});
  LoadCaseSpec lc;
  lc.name = "shear";
  lc.forces_n.assign(3 * mesh.node_count(), 0.0);
  lc.forces_n[3 * 2 + 0] = 1.0;
  s.load_cases.push_back(lc);
  auto problem = prepare_problem(build_tet_mesh(mesh.points, mesh.tets), s);
  const int E = problem->mesh->element_count();
  const Eigen::VectorXd ones = Eigen::VectorXd::Ones(E);

  // Independent reference: dense pencil (K_G, K) of the static prestress.
  auto model = std::make_shared<ElasticModel>(problem);
  ElasticCases cases(model);
  cases.solve(ones, cases.reference_forces(), nullptr);
  const ElasticSystem &sys = cases.system(0);
  Eigen::SparseMatrix<double> KG = sys.pattern().pattern();
  assemble_geometric_stiffness(sys, ones, cases.state(0).displacement, KG);
  const Eigen::MatrixXd Kd(sys.stiffness()), Gd(KG);
  const double offdiag = Gd.cwiseAbs().maxCoeff();
  CHECK(offdiag ==
        doctest::Approx(1000.0).epsilon(1e-9)); // V sigma / h^2 (N/m)
  CHECK(Gd.diagonal().cwiseAbs().maxCoeff() <= 1e-12 * offdiag);
  Eigen::GeneralizedSelfAdjointEigenSolver<Eigen::MatrixXd> pencil(Gd, Kd);
  REQUIRE(pencil.info() == Eigen::Success);
  const double mu_min = pencil.eigenvalues().minCoeff();
  REQUIRE(mu_min < 0); // indefinite K_G: a compressive direction exists
  const double lambda_ref = -1.0 / mu_min;

  auto physics = make_spectral_physics(problem);
  Evaluation ev;
  REQUIRE(code_of([&] { ev = physics->evaluate(ones, true, nullptr); }) == "");
  CHECK(ev.evidence["minimum_load_factor"].get<double>() ==
        doctest::Approx(lambda_ref).epsilon(1e-8).scale(0));
  CHECK(ev.objective > 0);
  CHECK(ev.objective <= lambda_ref * (1 + 1e-10));

  const Eigen::VectorXd rho = random_density(E, 5);
  const Evaluation g = physics->evaluate(rho, true, nullptr);
  check_gradient(
      [&](const Eigen::VectorXd &r) {
        return physics->evaluate(r, false, nullptr).objective;
      },
      rho, g.objective_gradient, {0, 1, 2, 3}, 1e-5);
}

TEST_CASE("clamped slender beam keeps its low positive bending frequency") {
  // 1 mm square, 1 m cantilever: lambda_1 is ~1e-12 of trace(K)/trace(M), a
  // genuine bending mode of a restrained model, not a rigid-body mode.
  const double h = 1e-3, L = 1.0, E_pa = 7e10, dens = 2700;
  const Mesh mesh = make_box(1, 1, 1000, 1.0);
  auto problem = modal_problem(mesh, 70000);
  auto physics = make_spectral_physics(problem);
  const int E = problem->mesh->element_count();
  const Eigen::VectorXd ones = Eigen::VectorXd::Ones(E);
  Evaluation ev;
  REQUIRE(code_of([&] { ev = physics->evaluate(ones, false, nullptr); }) == "");
  const std::vector<double> f = numbers(ev.evidence["frequencies_hz"]);
  REQUIRE(!f.empty());
  const double lambda1 = std::pow(2 * kPi * f[0], 2);
  // Conforming tet4 with consistent mass is a Rayleigh-Ritz upper bound of
  // the continuum, itself Euler-Bernoulli to O((h/L)^2).
  const double eb = std::pow(1.875104068711961, 4) * E_pa * (h * h / 12.0) /
                    (dens * std::pow(L, 4));
  CHECK(lambda1 >= 0.99 * eb);
  // Admissible nodal trial u_x = z^2, u_z = -2 (x - h/2) z bounds it above.
  CHECK(lambda1 <= 8.654 * (E_pa / dens) * h * h / std::pow(L, 4));
  CHECK(ev.objective > 0);
  CHECK(ev.objective <= f[0] * (1 + 1e-12));

  // The reported mode is a genuine eigenpair of the assembled pencil.
  auto model = std::make_shared<ElasticModel>(problem);
  ElasticSystem sys(model, problem->structural->shared_fixed);
  sys.assemble(ones, nullptr);
  Eigen::SparseMatrix<double> M = sys.pattern().pattern();
  assemble_mass(sys, ones, dens, M);
  const Field *mode = nullptr;
  for (const Field &fld : ev.fields)
    if (fld.name == "mode_shape_1")
      mode = &fld;
  REQUIRE(mode != nullptr);
  Eigen::VectorXd phi;
  sys.reduction().restrict_vector(mode->values, phi);
  const Eigen::VectorXd Mphi = M * phi;
  CHECK(phi.dot(Mphi) == doctest::Approx(1.0).epsilon(1e-8));
  // Normwise backward error: the Rayleigh quotient of a computed vector is
  // roundoff-dominated here (cond(K) ~ 1e13), the backward error is not.
  const Eigen::SparseMatrix<double> &K = sys.stiffness();
  double knorm = 0;
  for (int j = 0; j < K.outerSize(); ++j) {
    double col = 0;
    for (Eigen::SparseMatrix<double>::InnerIterator it(K, j); it; ++it)
      col += std::abs(it.value());
    knorm = std::max(knorm, col);
  }
  const Eigen::VectorXd r = K * phi - lambda1 * Mphi;
  CHECK(r.norm() <= 1e-10 * knorm * phi.norm());
}

TEST_CASE("modal objective is continuous and differentiable across eigen "
          "request changes") {
  const double h = 4.0;
  const Mesh mesh = make_box(4, 4, 3, h);
  auto problem = modal_problem(mesh, 70000, {{"min_frequency_hz", 5000.0}});
  auto physics = make_spectral_physics(problem);
  const PillarPath path = pillar_path(mesh, h);
  const int changes = check_request_changes(
      *physics, path,
      [](const Evaluation &ev) {
        return ev.evidence["modes_requested"].dump();
      },
      0.70, 1.0, 15);
  CHECK(changes >= 1);
}

TEST_CASE("buckling objective is continuous and differentiable across eigen "
          "request changes") {
  const double h = 4.0;
  const Mesh mesh = make_box(4, 4, 6, h);
  auto tm = build_tet_mesh(mesh.points, mesh.tets);
  ProblemSpec s;
  s.settings = parse_settings({{"physics", "buckling"},
                               {"volume_fraction", 0.9},
                               {"min_buckling_factor", 40.0}});
  s.material.e_mpa = 200000;
  s.material.nu = 0.3;
  s.fixtures.push_back(
      {"base",
       nodes_where(mesh, [](double, double, double z) { return z == 0; }),
       {true, true, true}});
  // -1000 N on each pillar top, split over its four corner nodes.
  LoadCaseSpec lc;
  lc.name = "pillars";
  lc.forces_n.assign(3 * mesh.node_count(), 0.0);
  const int cells[4][2] = {{0, 0}, {3, 0}, {0, 3}, {3, 3}};
  for (const auto &c : cells)
    for (int n : nodes_where(mesh, [&](double x, double y, double z) {
           return z == 6 * h && x >= c[0] * h && x <= (c[0] + 1) * h &&
                  y >= c[1] * h && y <= (c[1] + 1) * h;
         }))
      lc.forces_n[3 * n + 2] = -250.0;
  s.load_cases.push_back(lc);
  auto problem = prepare_problem(tm, s);
  auto physics = make_spectral_physics(problem);
  const PillarPath path = pillar_path(mesh, h);
  const int changes = check_request_changes(
      *physics, path,
      [](const Evaluation &ev) {
        std::string sig;
        for (const auto &c : ev.evidence["cases"])
          sig += c["modes_requested"].dump() + ";";
        return sig;
      },
      0.70, 1.0, 15);
  CHECK(changes >= 1);
}
