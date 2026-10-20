#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include "sim/topopt_model.h"

#include <Eigen/SparseCholesky>

#include <algorithm>
#include <array>
#include <cmath>
#include <functional>
#include <random>
#include <string>
#include <vector>

using namespace chudware::sim::topology;
using nlohmann::json;

namespace {

// Box of nx*ny*nz cubes (mm), each split into six Kuhn tets around the 0-6
// diagonal. With mirror_x the cubes of the upper x half are exact mirror images
// of the lower half, so half of all tets arrive negatively oriented.
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
             bool mirror_x = false) {
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

std::string code_of(const std::function<void()> &f) {
  try {
    f();
  } catch (const ModelError &e) {
    return e.code();
  }
  return "";
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

RegionSpec box_region(const std::string &label, std::array<double, 3> lo,
                      std::array<double, 3> hi) {
  RegionSpec r;
  r.label = label;
  r.has_box = true;
  r.box_min_mm = Eigen::Vector3d(lo[0], lo[1], lo[2]);
  r.box_max_mm = Eigen::Vector3d(hi[0], hi[1], hi[2]);
  return r;
}

// Cantilever: 4x2x2 box of 5 mm cubes, x=0 fixed, -100 N z spread over the x=20
// face.
struct Cantilever {
  Box box = make_box(4, 2, 2, 5, 5, 5);
  std::shared_ptr<const TetMesh> mesh = build_tet_mesh(box.points, box.tets);
  std::vector<int> root =
      nodes_where(box, [](double x, double, double) { return x == 0; });
  std::vector<int> tip =
      nodes_where(box, [](double x, double, double) { return x == 20; });

  ProblemSpec spec(json topology = json::object()) const {
    json t = {{"physics", "structural"}, {"volume_fraction", 0.5}};
    t.update(topology);
    ProblemSpec s;
    s.settings = parse_settings(t);
    s.material.e_mpa = 200000;
    s.material.nu = 0.3;
    s.fixtures.push_back({"root", root, {true, true, true}});
    LoadCaseSpec lc;
    lc.name = "tip";
    lc.forces_n.assign(3 * mesh->node_count(), 0.0);
    for (int n : tip)
      lc.forces_n[3 * n + 2] = -100.0 / tip.size();
    s.load_cases.push_back(lc);
    return s;
  }
};

// Channel along x: inlet at x=0, outlet at x=20, 2x2 mm cells in y/z.
struct Channel {
  Box box = make_box(4, 2, 2, 5, 1, 1);
  std::shared_ptr<const TetMesh> mesh = build_tet_mesh(box.points, box.tets);
  std::vector<int> inlet =
      nodes_where(box, [](double x, double, double) { return x == 0; });
  std::vector<int> outlet =
      nodes_where(box, [](double x, double, double) { return x == 20; });

  ProblemSpec spec(const char *physics = "flow") const {
    ProblemSpec s;
    s.settings =
        parse_settings({{"physics", physics}, {"volume_fraction", 0.8}});
    FlowSpec f;
    f.viscosity_pa_s = 1e-3;
    f.density_kg_m3 = 1000;
    f.brinkman_max = 1e4;
    f.reference_length_mm = 2.0;
    f.inlets.push_back({"in", inlet, Eigen::Vector3d(1, 0, 0), std::nullopt});
    f.outlets.push_back({"out", outlet, 5.0});
    s.flow = f;
    return s;
  }
};

} // namespace

TEST_CASE("tet4 gradients, volume and orientation") {
  const std::vector<double> pts = {0, 0, 0, 2, 0, 0, 0, 3, 0, 0, 0, 4};
  auto pos = build_tet_mesh(pts, {0, 1, 2, 3});
  auto neg = build_tet_mesh(pts, {0, 1, 3, 2});
  CHECK(pos->quality.reoriented_elements == 0);
  CHECK(neg->quality.reoriented_elements == 1);
  const double V = 2.0 * 3.0 * 4.0 / 6.0 * 1e-9; // mm^3 -> m^3
  CHECK(pos->volume_m3[0] == doctest::Approx(V).epsilon(1e-12));
  CHECK(neg->volume_m3[0] == doctest::Approx(V).epsilon(1e-12));
  for (const auto *m : {pos.get(), neg.get()}) {
    const Eigen::Matrix<double, 3, 4> G = m->element_gradients(0);
    CHECK(G.rowwise().sum().norm() < 1e-9);
    Eigen::Matrix3d I = Eigen::Matrix3d::Zero();
    for (int a = 0; a < 4; ++a)
      I += m->nodes_m.col(m->tets(a, 0)) * G.col(a).transpose();
    CHECK((I - Eigen::Matrix3d::Identity()).norm() < 1e-12);
    // Per-node gradients do not depend on the input node order.
    for (int a = 0; a < 4; ++a)
      for (int b = 0; b < 4; ++b)
        if (pos->tets(a, 0) == m->tets(b, 0))
          CHECK((pos->element_gradients(0).col(a) - G.col(b)).norm() < 1e-9);
    CHECK(m->surface_count() == 4);
    for (int f = 0; f < 4; ++f) {
      Eigen::Vector3d c = Eigen::Vector3d::Zero();
      for (int k = 0; k < 3; ++k)
        c += m->nodes_m.col(m->surface_nodes(k, f)) / 3.0;
      CHECK(m->surface_normal.col(f).dot(c - m->centroid_m.col(0)) > 0);
    }
  }
  CHECK(
      pos->centroid_m.col(0).isApprox(Eigen::Vector3d(0.5e-3, 0.75e-3, 1e-3)));
}

TEST_CASE("affine maps transform volume and gradients exactly") {
  const Box b = make_box(2, 2, 2, 3, 4, 5);
  auto ref = build_tet_mesh(b.points, b.tets);
  std::mt19937 rng(7);
  std::uniform_real_distribution<double> u(
      -0.2, 0.2); // diagonally dominant: det(A) > 0
  for (double flip : {1.0, -1.0}) {
    Eigen::Matrix3d A = Eigen::Matrix3d::Identity();
    for (int i = 0; i < 3; ++i)
      for (int j = 0; j < 3; ++j)
        A(i, j) += u(rng);
    A.row(0) *= flip;
    const Eigen::Vector3d t(11, -7, 3);
    std::vector<double> pts(b.points.size());
    for (std::size_t n = 0; n < pts.size() / 3; ++n) {
      const Eigen::Vector3d x =
          A * Eigen::Vector3d(b.points[3 * n], b.points[3 * n + 1],
                              b.points[3 * n + 2]) +
          t;
      for (int d = 0; d < 3; ++d)
        pts[3 * n + d] = x[d];
    }
    auto m = build_tet_mesh(pts, b.tets);
    CHECK(m->quality.reoriented_elements ==
          (flip < 0 ? m->element_count() : 0));
    const double det = std::abs(A.determinant());
    const Eigen::Matrix3d AinvT = A.inverse().transpose();
    CHECK(ref->total_volume_m3 ==
          doctest::Approx(6.0 * 8.0 * 10.0 * 1e-9).epsilon(1e-12));
    CHECK(m->total_volume_m3 ==
          doctest::Approx(det * ref->total_volume_m3).epsilon(1e-10));
    for (int e = 0; e < m->element_count(); ++e) {
      CHECK(m->volume_m3[e] ==
            doctest::Approx(det * ref->volume_m3[e]).epsilon(1e-10));
      CHECK(m->centroid_m.col(e).isApprox(A * ref->centroid_m.col(e) + t * 1e-3,
                                          1e-12));
      for (int a = 0; a < 4; ++a)
        for (int c = 0; c < 4; ++c)
          if (m->tets(a, e) == ref->tets(c, e))
            CHECK((m->element_gradients(e).col(a) -
                   AinvT * ref->element_gradients(e).col(c))
                      .norm() <
                  1e-9 * ref->element_gradients(e).col(c).norm() + 1e-9);
      // A linear nodal field is differentiated exactly.
      const Eigen::Vector3d g(0.3, -1.2, 2.5);
      Eigen::Vector3d grad = Eigen::Vector3d::Zero();
      for (int a = 0; a < 4; ++a)
        grad += (g.dot(m->nodes_m.col(m->tets(a, e))) + 4.0) *
                m->element_gradients(e).col(a);
      CHECK((grad - g).norm() < 1e-9);
    }
  }
}

TEST_CASE("closed surface, divergence theorem and adjacency") {
  const Box b = make_box(3, 2, 2, 2, 3, 4);
  auto m = build_tet_mesh(b.points, b.tets);
  const double Lx = 6e-3, Ly = 6e-3, Lz = 8e-3;
  CHECK(m->total_volume_m3 == doctest::Approx(Lx * Ly * Lz).epsilon(1e-12));
  CHECK(m->surface_area_m2.sum() ==
        doctest::Approx(2 * (Lx * Ly + Ly * Lz + Lz * Lx)).epsilon(1e-12));
  Eigen::Vector3d closure = Eigen::Vector3d::Zero();
  double flux = 0.0;
  for (int f = 0; f < m->surface_count(); ++f) {
    closure += m->surface_normal.col(f) * m->surface_area_m2[f];
    Eigen::Vector3d c = Eigen::Vector3d::Zero();
    for (int k = 0; k < 3; ++k)
      c += m->nodes_m.col(m->surface_nodes(k, f)) / 3.0;
    flux += c.dot(m->surface_normal.col(f)) * m->surface_area_m2[f] / 3.0;
  }
  CHECK(closure.norm() < 1e-15);
  CHECK(flux == doctest::Approx(m->total_volume_m3).epsilon(1e-10));
  int interior = 0;
  for (int e = 0; e < m->element_count(); ++e)
    for (int a = 0; a < 4; ++a) {
      const int nb = m->face_neighbor(a, e);
      if (nb < 0)
        continue;
      ++interior;
      bool back = false;
      for (int k = 0; k < 4; ++k)
        back = back || m->face_neighbor(k, nb) == e;
      CHECK(back);
    }
  CHECK(interior + m->surface_count() == 4 * m->element_count());
  CHECK(m->quality.components == 1);
  CHECK(m->node_element_offsets[m->node_count()] == 4 * m->element_count());
  // Nodes resolve to exactly the exterior triangles they span.
  const auto x0 =
      nodes_where(b, [](double x, double, double) { return x == 0; });
  double area = 0;
  for (int f : surface_faces_with_nodes(*m, x0))
    area += m->surface_area_m2[f];
  CHECK(area == doctest::Approx(Ly * Lz).epsilon(1e-12));
}

TEST_CASE("invalid and singular geometry refuses") {
  const std::vector<double> tet = {0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1};
  CHECK(code_of([&] {
          build_tet_mesh(tet, {0, 1, 2, 3, 0, 0, 0, 0, 0, 0}, 10);
        }) == "unsupported_element_order");
  CHECK(code_of([&] {
          build_tet_mesh({0, 0, 0, 1, 0, 0, 0, 1, 0, 1, 1, 0}, {0, 1, 2, 3});
        }) == "degenerate_element");
  CHECK(code_of([&] {
          build_tet_mesh({0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1e-11},
                         {0, 1, 2, 3});
        }) == "degenerate_element");
  std::vector<double> nan = tet;
  nan[4] = std::nan("");
  CHECK(code_of([&] { build_tet_mesh(nan, {0, 1, 2, 3}); }) == "invalid_mesh");
  CHECK(code_of([&] { build_tet_mesh(tet, {0, 1, 2, 4}); }) == "invalid_mesh");
  CHECK(code_of([&] { build_tet_mesh(tet, {0, 1, 2, 2}); }) == "invalid_mesh");
  CHECK(code_of([&] { build_tet_mesh(tet, {0, 1, 2}); }) == "invalid_mesh");
  CHECK(code_of([&] { build_tet_mesh({}, {}); }) == "invalid_mesh");
  CHECK(code_of([&] { build_tet_mesh(tet, {0, 1, 2, 3, 3, 2, 1, 0}); }) ==
        "duplicate_element");
  const std::vector<double> fan = {0, 0, 0, 1, 0, 0,  0,   1,   0,
                                   0, 0, 1, 0, 0, -1, 0.2, 0.2, 2};
  CHECK(code_of([&] {
          build_tet_mesh(fan, {0, 1, 2, 3, 0, 1, 2, 4, 0, 1, 2, 5});
        }) == "nonmanifold_face");
  // Two separate tets: the mesh builds, a problem refuses.
  std::vector<double> two = tet;
  for (double v : tet)
    two.push_back(v + 5);
  auto m = build_tet_mesh(two, {0, 1, 2, 3, 4, 5, 6, 7});
  CHECK(m->quality.components == 2);
  ProblemSpec s;
  s.settings =
      parse_settings({{"physics", "structural"}, {"volume_fraction", 0.5}});
  CHECK(code_of([&] { prepare_problem(m, s); }) == "disconnected_domain");
  // An unreferenced node is reported and pinned, never silently free.
  std::vector<double> extra = tet;
  for (double v : {9.0, 9.0, 9.0})
    extra.push_back(v);
  auto u = build_tet_mesh(extra, {0, 1, 2, 3});
  CHECK(u->unreferenced_nodes == std::vector<int>{4});
  CHECK(u->node_referenced[4] == 0);
}

TEST_CASE("material units and interpolation") {
  MaterialInput in;
  in.e_mpa = 200000;
  in.nu = 0.3;
  in.yield_mpa = 250;
  in.density_g_cm3 = 7.85;
  in.k_w_mk = 45;
  in.alpha_per_k = 1.2e-5;
  in.cp_j_kgk = 470;
  const Material m = convert_material(in);
  CHECK(*m.youngs_pa == doctest::Approx(2e11));
  CHECK(*m.yield_pa == doctest::Approx(2.5e8));
  CHECK(*m.density_kg_m3 == doctest::Approx(7850));
  CHECK(*m.conductivity_w_mk == 45);
  CHECK(units::celsius_to_kelvin(20) == doctest::Approx(293.15));
  CHECK(units::mm3_to_m3(1e9) == doctest::Approx(1.0));
  CHECK(units::pa_to_mpa(units::mpa_to_pa(3.5)) == doctest::Approx(3.5));
  MaterialInput bad = in;
  bad.nu = 0.5;
  CHECK(code_of([&] { convert_material(bad); }) == "invalid_material");
  Material thermal_only;
  thermal_only.conductivity_w_mk = 1.0;
  CHECK(code_of([&] {
          require_material(thermal_only, PhysicsKind::Structural);
        }) == "material_missing");
  CHECK(code_of([&] {
          require_material(m, PhysicsKind::Thermomechanical);
        }).empty());
  CHECK(code_of([&] {
          parse_material({{"E_mpa", 1}, {"per_body", json::array()}});
        }) == "unsupported_material_key");

  const auto C = isotropic_elasticity(2e11, 0.3);
  // Uniaxial stress recovers E: invert C and read 1/S11.
  CHECK(1.0 / C.inverse()(0, 0) == doctest::Approx(2e11));
  CHECK(C(3, 3) == doctest::Approx(2e11 / 2.6));

  for (Interpolation ip :
       {Interpolation{Interpolation::Kind::Simp, 3.0, 1e-3},
        Interpolation{Interpolation::Kind::Ramp, 8.0, 1e-3}}) {
    CHECK(ip.shape(0.0) == 0.0);
    CHECK(ip.shape(1.0) == doctest::Approx(1.0));
    CHECK(ip.scale(0.0) == doctest::Approx(1e-3));
    CHECK(ip.scale(1.0) == doctest::Approx(1.0));
    for (double r : {0.1, 0.37, 0.8}) {
      const double h = 1e-6;
      CHECK(ip.scale_derivative(r) ==
            doctest::Approx((ip.scale(r + h) - ip.scale(r - h)) / (2 * h))
                .epsilon(1e-6));
    }
  }
  const Interpolation ramp0{Interpolation::Kind::Ramp, 8.0, 0.0};
  CHECK(ramp0.shape_derivative(0.0) == doctest::Approx(1.0 / 9.0));
}

TEST_CASE("strain matrix kinematics") {
  auto m =
      build_tet_mesh({0, 0, 0, 1, 0, 0, 0, 1, 0, 0.2, 0.3, 1}, {0, 1, 2, 3});
  const auto B = tet4_strain_matrix(m->element_gradients(0));
  Eigen::Matrix<double, 12, 1> rot, stretch;
  const Eigen::Vector3d w(0.1, -0.2, 0.3);
  for (int a = 0; a < 4; ++a) {
    const Eigen::Vector3d x = m->nodes_m.col(m->tets(a, 0));
    rot.segment<3>(3 * a) = w.cross(x) + Eigen::Vector3d(1, 2, 3);
    stretch.segment<3>(3 * a) = Eigen::Vector3d(0.01 * x[0], 0, 0.02 * x[1]);
  }
  CHECK((B * rot).norm() < 1e-12);
  const Eigen::Matrix<double, 6, 1> e = B * stretch;
  CHECK(e[0] == doctest::Approx(0.01));
  CHECK(e[4] == doctest::Approx(0.02)); // gamma_yz = du_z/dy
  CHECK(std::abs(e[1]) + std::abs(e[2]) + std::abs(e[3]) + std::abs(e[5]) <
        1e-12);
}

TEST_CASE(
    "reduced pattern with nonzero Dirichlet reproduces a linear temperature") {
  const Box b = make_box(4, 3, 2, 2.5, 2, 3);
  auto m = build_tet_mesh(b.points, b.tets);
  std::vector<std::pair<int, double>> bc;
  for (int n : nodes_where(b, [](double x, double, double) { return x == 0; }))
    bc.push_back({n, 300.0});
  for (int n : nodes_where(b, [](double x, double, double) { return x == 10; }))
    bc.push_back({n, 400.0});
  const Dirichlet d = make_dirichlet(m->node_count(), bc);
  const DofReduction red = make_dof_reduction(m->node_count(), d);
  CHECK(red.free_size() + d.size() == m->node_count());
  const Eigen::MatrixXi map = nodal_dof_map(*m, 1);
  ReducedPattern pat(map, red);
  Eigen::SparseMatrix<double> K = pat.pattern();
  Eigen::VectorXd rhs = Eigen::VectorXd::Zero(red.free_size());
  std::vector<Eigen::Triplet<double>> trip;
  for (int e = 0; e < m->element_count(); ++e) {
    const Eigen::Matrix<double, 3, 4> G = m->element_gradients(e);
    const Eigen::Matrix4d ke = 45.0 * m->volume_m3[e] * G.transpose() * G;
    pat.add(K, e, ke);
    pat.lift(rhs, e, ke);
    for (int i = 0; i < 4; ++i)
      for (int j = 0; j < 4; ++j) {
        const int fi = red.full_to_free[map(i, e)],
                  fj = red.full_to_free[map(j, e)];
        if (fi >= 0 && fj >= 0)
          trip.emplace_back(fi, fj, ke(i, j));
      }
  }
  Eigen::SparseMatrix<double> Kref(red.free_size(), red.free_size());
  Kref.setFromTriplets(trip.begin(), trip.end());
  CHECK((Eigen::MatrixXd(K) - Eigen::MatrixXd(Kref)).norm() <
        1e-12 * Eigen::MatrixXd(Kref).norm());
  Eigen::SimplicialLDLT<Eigen::SparseMatrix<double>> solver(K);
  REQUIRE(solver.info() == Eigen::Success);
  Eigen::VectorXd T;
  red.expand_vector(solver.solve(rhs), T);
  for (int n = 0; n < m->node_count(); ++n)
    CHECK(T[n] == doctest::Approx(300.0 + 100.0 * m->nodes_m(0, n) / 10e-3)
                      .epsilon(1e-9));
  // Re-assembly in place keeps the pattern (no re-analysis needed).
  ReducedPattern::zero(K);
  CHECK(K.nonZeros() == pat.pattern().nonZeros());
  CHECK(code_of([&] { make_dirichlet(4, {{1, 2.0}, {1, 3.0}}); }) ==
        "dirichlet_conflict");
  CHECK(make_dirichlet(4, {{2, 1.0}, {1, 3.0}, {2, 1.0}}).size() == 2);
}

TEST_CASE("density filter preserves constants and is its own transpose pair") {
  const Box b = make_box(5, 3, 2, 2, 2, 2);
  auto m = build_tet_mesh(b.points, b.tets);
  const int E = m->element_count();
  for (double r : {0.0, 1.5e-3, 4e-3}) {
    const DensityFilter f = build_density_filter(*m, r);
    Eigen::VectorXd out;
    f.apply(Eigen::VectorXd::Constant(E, 0.37), out);
    CHECK((out.array() - 0.37).abs().maxCoeff() < 1e-14);
    std::mt19937 rng(3);
    std::uniform_real_distribution<double> u(0, 1);
    Eigen::VectorXd a(E), g(E), Ha, Htg;
    for (int e = 0; e < E; ++e) {
      a[e] = u(rng);
      g[e] = u(rng) - 0.5;
    }
    f.apply(a, Ha);
    f.apply_transpose(g, Htg);
    CHECK(Ha.dot(g) == doctest::Approx(a.dot(Htg)).epsilon(1e-13));
    CHECK(Ha.minCoeff() >= a.minCoeff() - 1e-15);
    CHECK(Ha.maxCoeff() <= a.maxCoeff() + 1e-15);
    if (r == 0.0)
      CHECK((Ha - a).norm() == 0.0);
    else
      CHECK(f.weights.nonZeros() > E);
  }
}

TEST_CASE(
    "problem preparation converts units once and owns explicit boundaries") {
  Cantilever c;
  auto spec = c.spec({{"filter_radius_mm", 3.0}, {"stress_limit_mpa", 120.0}});
  auto p = prepare_problem(c.mesh, spec);
  CHECK(*p->material.youngs_pa == doctest::Approx(2e11));
  CHECK(p->filter_radius_m == doctest::Approx(3e-3));
  CHECK(p->filter_radius_source == "explicit");
  CHECK(*p->limits.stress_pa == doctest::Approx(1.2e8));
  REQUIRE(p->structural);
  const StructuralCase &lc = p->structural->cases.at(0);
  CHECK(lc.resultant_n.isApprox(Eigen::Vector3d(0, 0, -100)));
  CHECK(lc.fixed.size() == 3 * static_cast<int>(c.root.size()));
  CHECK((lc.fixed.values.array() == 0).all());
  CHECK(!p->thermal);
  CHECK(!p->flow);
  for (int n : c.root)
    CHECK((p->node_ownership[n] & kNodeStructuralFixed));
  for (int n : c.tip)
    CHECK((p->node_ownership[n] & kNodeStructuralLoad));
  // Boundary-touching elements are fixed solid; the rest is design.
  for (int e : elements_touching_nodes(*c.mesh, c.root)) {
    CHECK(p->roles.role[e] == ElementRole::Solid);
    CHECK((p->roles.reason[e] & kReasonStructuralBoundary));
  }
  CHECK(p->roles.design_count > 0);
  CHECK(p->design.variable_count == p->roles.design_count);
  CHECK(p->evidence["design_phase"] == "solid");

  // Multi-case with case-specific fixtures.
  auto two = c.spec();
  LoadCaseSpec side = two.load_cases[0];
  side.name = "side";
  side.weight = 2.0;
  for (double &f : side.forces_n)
    f = 0;
  for (int n : c.tip)
    side.forces_n[3 * n + 1] = 10.0;
  side.fixtures.push_back({"tip y", {c.tip.front()}, {false, false, true}});
  two.load_cases.push_back(side);
  auto p2 = prepare_problem(c.mesh, two);
  CHECK(p2->structural->cases.size() == 2);
  CHECK(!p2->structural->cases_share_constraints);
  CHECK(p2->structural->cases[1].weight == 2.0);
}

TEST_CASE("boundary and passive conflicts refuse") {
  Cantilever c;
  auto s = c.spec();
  s.load_cases[0].forces_n[3 * c.root.front()] = 5.0;
  CHECK(code_of([&] { prepare_problem(c.mesh, s); }) == "load_on_fixed_dof");

  s = c.spec();
  s.fixtures[0].nodes = {c.root.front()};
  CHECK(code_of([&] { prepare_problem(c.mesh, s); }) == "rigid_body_motion");

  s = c.spec();
  s.fixtures[0].dofs = {true, true, false};
  CHECK(code_of([&] { prepare_problem(c.mesh, s); }) == "rigid_body_motion");

  s = c.spec();
  for (double &f : s.load_cases[0].forces_n)
    f = 0;
  CHECK(code_of([&] { prepare_problem(c.mesh, s); }) == "zero_load");

  s = c.spec();
  s.preserve.push_back(box_region("keep", {9, -1, -1}, {16, 11, 11}));
  s.exclude.push_back(box_region("hole", {12, -1, -1}, {20, 11, 11}));
  CHECK(code_of([&] { prepare_problem(c.mesh, s); }) == "passive_conflict");

  s = c.spec();
  s.exclude.push_back(box_region("root hole", {-1, -1, -1}, {3, 11, 11}));
  CHECK(code_of([&] { prepare_problem(c.mesh, s); }) == "boundary_in_keep_out");

  s = c.spec({{"volume_fraction", 0.1}});
  s.preserve.push_back(box_region("block", {-1, -1, -1}, {12, 11, 11}));
  CHECK(code_of([&] { prepare_problem(c.mesh, s); }) ==
        "volume_fraction_infeasible");

  s = c.spec();
  s.preserve.push_back(box_region("all", {-1, -1, -1}, {21, 11, 11}));
  s.settings.volume_fraction = 1.0;
  CHECK(code_of([&] { prepare_problem(c.mesh, s); }) == "no_design_elements");

  s = c.spec();
  s.flow = Channel().spec().flow;
  CHECK(code_of([&] { prepare_problem(c.mesh, s); }) ==
        "section_not_applicable");

  s = c.spec();
  s.fixtures[0].nodes.push_back(c.mesh->node_count());
  CHECK(code_of([&] { prepare_problem(c.mesh, s); }) == "invalid_bc");

  // Node-set preserve with thickness grows by distance to the selected surface.
  // Root and tip boundary columns plus the 8 mm pad fix 2/3 of the volume as
  // solid, so the budget must exceed that for the problem to be feasible.
  s = c.spec({{"volume_fraction", 0.8}});
  RegionSpec pad_region;
  pad_region.label = "tip pad";
  pad_region.nodes = c.tip;
  pad_region.thickness_mm = 8.0;
  s.preserve.push_back(pad_region);
  auto p = prepare_problem(c.mesh, s);
  int pad = 0;
  for (int e = 0; e < c.mesh->element_count(); ++e)
    if (p->roles.reason[e] & kReasonPreserve) {
      ++pad;
      CHECK(c.mesh->centroid_m(0, e) >= 20e-3 - 8e-3 - 1e-12);
    }
  CHECK(pad > static_cast<int>(elements_touching_nodes(*c.mesh, c.tip).size()));
}

TEST_CASE("settings validation is explicit") {
  CHECK(code_of([] {
          parse_settings({{"physics", "structural"},
                          {"volume_fraction", 0.5},
                          {"bogus", 1}});
        }) == "unknown_key");
  CHECK(code_of([] { parse_settings({{"physics", "structural"}}); }) ==
        "missing_key");
  CHECK(code_of([] {
          parse_settings({{"physics", "structural"}, {"volume_fraction", 1.5}});
        }) == "out_of_range");
  CHECK(code_of([] {
          parse_settings({{"physics", "structural"},
                          {"volume_fraction", 0.5},
                          {"ramp_q", 3}});
        }) == "key_not_applicable");
  CHECK(code_of([] {
          parse_settings({{"physics", "thermal"},
                          {"volume_fraction", 0.5},
                          {"stress_limit_mpa", 3}});
        }) == "limit_not_applicable");
  CHECK(code_of([] {
          parse_settings({{"physics", "flow"},
                          {"volume_fraction", 0.5},
                          {"objective", "compliance"}});
        }) == "objective_not_applicable");
  CHECK(code_of([] {
          parse_settings({{"physics", "structural"},
                          {"volume_fraction", 0.5},
                          {"objective", "mass"}});
        }) == "mass_requires_limit");
  CHECK(code_of([] {
          parse_settings({{"physics", "plasma"}, {"volume_fraction", 0.5}});
        }) == "unknown_physics");
  CHECK(code_of([] {
          parse_settings({{"physics", "structural"},
                          {"volume_fraction", 0.5},
                          {"overhang_penalty", 1}});
        }) == "build_direction_required");
  const Settings s = parse_settings({{"physics", "modal"},
                                     {"method", "ramp"},
                                     {"volume_fraction", 0.4},
                                     {"ramp_q", 6},
                                     {"symmetry_axes", {"x", "z"}},
                                     {"extrusion_axis", "y"},
                                     {"build_direction", {0, 0, 2}}});
  CHECK(s.objective == ObjectiveKind::Frequency);
  CHECK(objective_sense(s.objective) == ObjectiveSense::Maximize);
  CHECK((s.symmetry_axes == std::array<bool, 3>{true, false, true}));
  CHECK(s.extrusion_axis == 1);
  CHECK(s.build_direction->isApprox(Eigen::Vector3d(0, 0, 1)));
  const Interpolation ip = interpolation_for(s);
  CHECK(ip.kind == Interpolation::Kind::Ramp);
  CHECK(ip.exponent == 6);
  CHECK(interpolation_for(parse_settings({{"physics", "thermal"},
                                          {"method", "level_set"},
                                          {"volume_fraction", 0.4}}))
            .exponent == 1.0);
}

TEST_CASE("normalized JSON parses into the same prepared problem") {
  Cantilever c;
  json forces = json::array();
  for (int i = 0; i < 3 * c.mesh->node_count(); ++i)
    forces.push_back(0.0);
  for (int n : c.tip)
    forces[3 * n + 2] = -100.0 / c.tip.size();
  const json j = {
      {"topology", {{"physics", "structural"}, {"volume_fraction", 0.5}}},
      {"material", {{"E_mpa", 200000}, {"nu", 0.3}}},
      {"fixtures", {{{"label", "root"}, {"nodes", c.root}}}},
      {"load_cases", {{{"name", "tip"}, {"forces_n", forces}}}},
      {"preserve",
       {{{"label", "pad"}, {"nodes", c.tip}, {"thickness_mm", 2.0}}}}};
  auto p = prepare_problem(c.mesh, parse_problem_spec(j));
  CHECK(p->structural->cases[0].resultant_n.isApprox(
      Eigen::Vector3d(0, 0, -100)));
  CHECK(p->roles.solid_count > 0);
  CHECK(code_of([&] {
          json bad = j;
          bad["extra"] = 1;
          parse_problem_spec(bad);
        }) == "unknown_key");
  CHECK(code_of([&] {
          json bad = j;
          bad["preserve"][0]["box_min_mm"] = {0, 0, 0};
          parse_problem_spec(bad);
        }) == "invalid_region");
}

TEST_CASE("thermal boundaries convert and validate") {
  Cantilever c;
  ProblemSpec s;
  s.settings =
      parse_settings({{"physics", "thermal"}, {"volume_fraction", 0.6}});
  s.material.k_w_mk = 200;
  ThermalSpec t;
  t.fixtures.push_back({"sink", c.root, 25.0});
  ThermalLoadSpec power;
  power.label = "chip";
  power.nodes = c.tip;
  power.kind = ThermalLoadKind::Power;
  power.power_w = 10.0;
  t.loads.push_back(power);
  s.thermal = t;
  auto p = prepare_problem(c.mesh, s);
  REQUIRE(p->thermal);
  const ThermalBoundary &tb = *p->thermal;
  CHECK(tb.fluxes[0].patch.area_m2 == doctest::Approx(100e-6));
  CHECK(tb.fluxes[0].flux_w_m2 == doctest::Approx(10.0 / 100e-6));
  CHECK(tb.total_heat_input_w == doctest::Approx(10.0));
  CHECK(tb.fixed.size() == static_cast<int>(c.root.size()));
  CHECK((tb.fixed.values.array() - 298.15).abs().maxCoeff() < 1e-12);
  CHECK(!p->structural);
  for (int e : elements_touching_nodes(*c.mesh, c.tip))
    CHECK((p->roles.reason[e] & kReasonThermalBoundary));

  auto conflict = s;
  conflict.thermal->fixtures.push_back({"hot", {c.root.front()}, 80.0});
  CHECK(code_of([&] { prepare_problem(c.mesh, conflict); }) ==
        "thermal_fixture_conflict");
  auto loose = s;
  loose.thermal->fixtures.clear();
  CHECK(code_of([&] { prepare_problem(c.mesh, loose); }) ==
        "thermal_unconstrained");
  auto on_fixed = s;
  on_fixed.thermal->loads[0].nodes = c.root;
  CHECK(code_of([&] { prepare_problem(c.mesh, on_fixed); }) ==
        "thermal_load_on_fixed_temperature");
  auto nodes_only = s;
  nodes_only.thermal->loads[0].nodes = {c.tip.front()};
  CHECK(code_of([&] { prepare_problem(c.mesh, nodes_only); }) == "invalid_bc");
  auto no_section = s;
  no_section.thermal.reset();
  CHECK(code_of([&] { prepare_problem(c.mesh, no_section); }) ==
        "section_missing");

  // Thermomechanical requires the strain-free reference temperature explicitly.
  auto tm = c.spec();
  tm.settings = parse_settings(
      {{"physics", "thermomechanical"}, {"volume_fraction", 0.5}});
  tm.material.k_w_mk = 45;
  tm.material.alpha_per_k = 1.2e-5;
  tm.thermal = t;
  CHECK(code_of([&] { prepare_problem(c.mesh, tm); }) == "section_missing");
  tm.thermal->reference_temperature_c = 20.0;
  auto ptm = prepare_problem(c.mesh, tm);
  CHECK(ptm->thermal->reference_temperature_k == doctest::Approx(293.15));
  CHECK(ptm->structural);
}

TEST_CASE("flow and CHT boundaries: fluid budget, ports, walls and regime") {
  Channel ch;
  auto p = prepare_problem(ch.mesh, ch.spec());
  REQUIRE(p->flow);
  const FlowBoundary &fb = *p->flow;
  CHECK(design_phase(PhysicsKind::Flow) == DesignPhase::Fluid);
  CHECK(fb.inlets[0].nominal_flow_m3_s == doctest::Approx(4e-6));
  CHECK(fb.mean_inlet_speed_m_s == doctest::Approx(1.0));
  CHECK(fb.reference_length_m == doctest::Approx(2e-3));
  CHECK(fb.reynolds == doctest::Approx(1000 * 1.0 * 2e-3 / 1e-3));
  CHECK(fb.alpha_max == doctest::Approx(1e4 * 1e-3 / 4e-6));
  CHECK(fb.rim_nodes_no_slip == 16);
  // The one interior inlet node carries the inlet velocity; rims are no-slip.
  const int centre = ch.box.node(0, 1, 1);
  for (int k = 0; k < fb.velocity.size(); ++k) {
    const int dof = fb.velocity.dofs[k];
    if (dof == 3 * centre)
      CHECK(fb.velocity.values[k] == 1.0);
    else if (dof == 3 * ch.box.node(0, 0, 0))
      CHECK(fb.velocity.values[k] == 0.0);
  }
  for (int e : elements_touching_nodes(*ch.mesh, ch.inlet)) {
    CHECK(p->roles.role[e] == ElementRole::Void);
    CHECK((p->roles.reason[e] & kReasonFlowPort));
  }
  for (int n : ch.inlet)
    CHECK((p->node_ownership[n] & kNodeFlowInlet));

  // Fluid budget: rho = 1 solid, volume fraction counts fluid.
  const int E = ch.mesh->element_count();
  Eigen::VectorXd rho = Eigen::VectorXd::Constant(E, 0.25);
  CHECK(phase_fraction(*p, rho) == doctest::Approx(0.75));
  const Constraint vc = volume_constraint(*p, rho, true);
  CHECK(vc.name == "fluid_volume_fraction");
  CHECK(vc.value == doctest::Approx(0.75 / 0.8 - 1.0));
  const double h = 1e-3;
  Eigen::VectorXd r2 = rho;
  r2[3] += h;
  CHECK(vc.gradient[3] ==
        doctest::Approx((volume_constraint(*p, r2, false).value - vc.value) / h)
            .epsilon(1e-8));

  auto out = ch.spec();
  out.flow->inlets[0].velocity_m_s = Eigen::Vector3d(-1, 0, 0);
  CHECK(code_of([&] { prepare_problem(ch.mesh, out); }) ==
        "inlet_velocity_outward");
  auto overlap = ch.spec();
  overlap.flow->outlets[0].nodes = ch.inlet;
  CHECK(code_of([&] { prepare_problem(ch.mesh, overlap); }) == "port_overlap");
  auto wall = ch.spec();
  wall.flow->walls.push_back({"bad wall", ch.outlet});
  CHECK(code_of([&] { prepare_problem(ch.mesh, wall); }) == "port_overlap");
  auto keep = ch.spec();
  keep.preserve.push_back(box_region("keep", {-1, -1, -1}, {3, 3, 3}));
  CHECK(code_of([&] { prepare_problem(ch.mesh, keep); }) == "port_in_preserve");
  auto temp = ch.spec();
  temp.flow->inlets[0].temperature_c = 20.0;
  CHECK(code_of([&] { prepare_problem(ch.mesh, temp); }) ==
        "key_not_applicable");

  // CHT couples inlet temperature into the thermal Dirichlet set.
  auto cht = ch.spec("cht");
  cht.material.k_w_mk = 200;
  cht.flow->fluid_k_w_mk = 0.6;
  cht.flow->fluid_cp_j_kgk = 4180;
  ThermalSpec t;
  ThermalLoadSpec flux;
  flux.label = "heater";
  flux.nodes =
      nodes_where(ch.box, [](double, double, double z) { return z == 0; });
  flux.kind = ThermalLoadKind::Flux;
  flux.magnitude_w_m2 = 1e4;
  t.loads.push_back(flux);
  cht.thermal = t;
  CHECK(code_of([&] { prepare_problem(ch.mesh, cht); }) ==
        "section_missing"); // inlet temperature
  cht.flow->inlets[0].temperature_c = 20.0;
  auto pc = prepare_problem(ch.mesh, cht);
  REQUIRE(pc->thermal);
  CHECK(pc->thermal->fixed.size() == static_cast<int>(ch.inlet.size()));
  CHECK((pc->thermal->fixed.values.array() - 293.15).abs().maxCoeff() < 1e-12);
  CHECK(pc->thermal->total_heat_input_w == doctest::Approx(1e4 * 20e-3 * 2e-3));
}

TEST_CASE("symmetry and extrusion tying invariants") {
  const Box b = make_box(4, 2, 2, 2, 2, 2, true);
  auto m = build_tet_mesh(b.points, b.tets);
  CHECK(m->quality.reoriented_elements == m->element_count() / 2);
  const int E = m->element_count();

  ElementRoles roles;
  roles.role.assign(E, ElementRole::Design);
  roles.reason.assign(E, 0);
  TyingSpec mirror;
  mirror.mirror = {true, false, false};
  mirror.mirror_origin_m = Eigen::Vector3d(4e-3, 2e-3, 2e-3);
  DesignMap dm = build_design_map(*m, roles, mirror);
  CHECK(dm.variable_count == E / 2);
  CHECK(dm.max_tie_distance_m < 1e-12);
  CHECK(dm.tied_groups == E / 2);
  Eigen::VectorXd x(dm.variable_count), rho;
  for (int v = 0; v < dm.variable_count; ++v)
    x[v] = std::sin(1.0 + v);
  dm.expand(x, rho);
  for (int e = 0; e < E; ++e)
    for (int f = 0; f < E; ++f) {
      Eigen::Vector3d img = m->centroid_m.col(e);
      img[0] = 8e-3 - img[0];
      if ((img - m->centroid_m.col(f)).norm() < 1e-12)
        CHECK(rho[e] == rho[f]);
    }
  Eigen::VectorXd g(E), gx;
  for (int e = 0; e < E; ++e)
    g[e] = std::cos(0.3 * e);
  dm.reduce_gradient(g, gx);
  CHECK(rho.dot(g) == doctest::Approx(x.dot(gx)).epsilon(1e-13));
  CHECK(dm.variable_volume_m3.sum() ==
        doctest::Approx(m->total_volume_m3).epsilon(1e-12));

  // Extrusion along z: every column of a cell shares one variable.
  ElementRoles r2;
  r2.role.assign(E, ElementRole::Design);
  r2.reason.assign(E, 0);
  TyingSpec ext;
  ext.extrusion_axis = 2;
  ext.extrusion_cell_m = 2e-3;
  DesignMap de = build_design_map(*m, r2, ext);
  CHECK(de.variable_count == 4 * 2);
  for (int e = 0; e < E; ++e)
    for (int f = 0; f < E; ++f)
      if (std::floor(m->centroid_m(0, e) / 2e-3) ==
              std::floor(m->centroid_m(0, f) / 2e-3) &&
          std::floor(m->centroid_m(1, e) / 2e-3) ==
              std::floor(m->centroid_m(1, f) / 2e-3))
        CHECK(de.element_variable[e] == de.element_variable[f]);

  // Passive solid propagates to its mirror image; solid tied to void refuses.
  ElementRoles r3;
  r3.role.assign(E, ElementRole::Design);
  r3.reason.assign(E, 0);
  r3.role[0] = ElementRole::Solid;
  DesignMap dp = build_design_map(*m, r3, mirror);
  CHECK(r3.solid_count == 2);
  CHECK(dp.variable_count == E / 2 - 1);
  int tied = 0;
  for (int e = 0; e < E; ++e)
    tied += (r3.reason[e] & kReasonTied) ? 1 : 0;
  CHECK(tied == 1);
  ElementRoles r4 = r3;
  r4.role.assign(E, ElementRole::Design);
  r4.role[0] = ElementRole::Solid;
  Eigen::Vector3d img = m->centroid_m.col(0);
  img[0] = 8e-3 - img[0];
  for (int f = 0; f < E; ++f)
    if ((m->centroid_m.col(f) - img).norm() < 1e-12)
      r4.role[f] = ElementRole::Void;
  CHECK(code_of([&] { build_design_map(*m, r4, mirror); }) ==
        "tie_passive_conflict");

  // A mirror plane that does not bisect the domain refuses.
  ElementRoles r5;
  r5.role.assign(E, ElementRole::Design);
  r5.reason.assign(E, 0);
  TyingSpec off = mirror;
  off.mirror_origin_m[0] = 1e-3;
  CHECK(code_of([&] { build_design_map(*m, r5, off); }) ==
        "symmetry_domain_mismatch");
}

TEST_CASE(
    "design chain is affine with an exact transpose and respects passives") {
  const Box b = make_box(4, 2, 4, 5, 5, 5, true);
  auto mesh = build_tet_mesh(b.points, b.tets);
  ProblemSpec s;
  s.settings = parse_settings({{"physics", "structural"},
                               {"volume_fraction", 0.5},
                               {"symmetry_axes", {"x"}},
                               {"filter_radius_mm", 7.0}});
  s.material.e_mpa = 1000;
  s.material.nu = 0.3;
  const auto root =
      nodes_where(b, [](double, double, double z) { return z == 0; });
  const auto top = nodes_where(
      b, [](double x, double, double z) { return z == 20 && x == 10; });
  s.fixtures.push_back({"base", root, {true, true, true}});
  LoadCaseSpec lc;
  lc.forces_n.assign(3 * mesh->node_count(), 0.0);
  for (int n : top)
    lc.forces_n[3 * n + 2] = -1.0;
  s.load_cases.push_back(lc);
  auto p = prepare_problem(mesh, s);
  const int V = p->design.variable_count, E = mesh->element_count();
  REQUIRE(V > 0);
  std::mt19937 rng(11);
  std::uniform_real_distribution<double> u(0, 1);
  Eigen::VectorXd x(V), g(E), rho, rho0, gx;
  for (int v = 0; v < V; ++v)
    x[v] = u(rng);
  for (int e = 0; e < E; ++e)
    g[e] = u(rng) - 0.5;
  design_to_density(*p, x, rho);
  design_to_density(*p, Eigen::VectorXd::Zero(V), rho0);
  density_gradient_to_design(*p, g, gx);
  CHECK((rho - rho0).dot(g) == doctest::Approx(x.dot(gx)).epsilon(1e-12));
  for (int e = 0; e < E; ++e)
    if (p->design.element_variable[e] < 0)
      CHECK(rho[e] == p->design.fixed_density[e]);
  design_to_density(*p, Eigen::VectorXd::Constant(V, 0.6), rho);
  for (int e = 0; e < E; ++e)
    if (p->design.element_variable[e] >= 0)
      CHECK(rho[e] <= 1.0 + 1e-15);
  check_density(*p, rho);
  // Volume accounting uses geometric density only (floor never enters).
  const double solid = mesh->volume_m3.dot(rho) / mesh->total_volume_m3;
  CHECK(phase_fraction(*p, rho) == doctest::Approx(solid));
  CHECK(volume_constraint(*p, rho, false).name == "solid_volume_fraction");
  Eigen::VectorXd bad = rho;
  bad[0] = 1.2;
  CHECK(code_of([&] { check_density(*p, bad); }) == "invalid_density");
  CHECK(code_of([&] {
          design_to_density(*p, Eigen::VectorXd::Zero(V + 1), rho);
        }) == "invalid_design");
}

namespace {

// Three equal-volume tets (mm). Extrusion along x with the default cell
// (median element size, ~1.782 mm) ties elements 0 and 2 (y/z centroid bin
// (0,0)) while element 1 sits alone in bin (1,0). With R = 1.05 mm element 1
// reaches element 0 (1.031 mm) but not element 2 (1.061 mm), so filtering
// alone gives the two tied elements different physical densities.
std::shared_ptr<Problem> tie_neighborhood_problem() {
  auto mesh =
      build_tet_mesh({0, 0, 0, 0, 4, 0, 0, 0, 1, 1, 0, 0, 1, 4, 0, 1, 0, 1},
                     {0, 1, 2, 3, 1, 2, 3, 4, 2, 3, 4, 5});
  auto p = std::make_shared<Problem>();
  p->mesh = mesh;
  p->roles.role.assign(3, ElementRole::Design);
  p->roles.reason.assign(3, 0);
  TyingSpec tie;
  tie.extrusion_axis = 0;
  p->design = build_design_map(*mesh, p->roles, tie);
  p->filter_radius_m = 1.05e-3;
  p->filter_radius_source = "explicit";
  p->filter = build_density_filter(*mesh, p->filter_radius_m);
  return p;
}

} // namespace

TEST_CASE("filtered physical densities stay tied on an asymmetric "
          "extrusion neighborhood") {
  const auto p = tie_neighborhood_problem();
  const TetMesh &m = *p->mesh;
  REQUIRE(m.element_count() == 3);
  REQUIRE(p->design.variable_count == 2);
  REQUIRE(p->design.element_variable[0] == p->design.element_variable[2]);
  REQUIRE(p->design.element_variable[0] != p->design.element_variable[1]);
  REQUIRE(p->filter.weights.coeff(0, 1) > 0.0);
  REQUIRE(p->filter.weights.coeff(2, 1) == 0.0);
  const int tied = p->design.element_variable[0];
  const int lone = p->design.element_variable[1];

  Eigen::VectorXd x(2), raw, filtered, rho;
  x[tied] = 0.2;
  x[lone] = 0.8;
  p->design.expand(x, raw);
  p->filter.apply(raw, filtered);
  REQUIRE(filtered[0] > filtered[2] + 1e-3);
  design_to_density(*p, x, rho);
  CHECK(rho[0] == doctest::Approx(rho[2]).epsilon(1e-14));
  const double v0 = m.volume_m3[0], v2 = m.volume_m3[2];
  CHECK(rho[0] ==
        doctest::Approx((v0 * filtered[0] + v2 * filtered[2]) / (v0 + v2))
            .epsilon(1e-14));
  CHECK(rho[1] == doctest::Approx(filtered[1]).epsilon(1e-14));
  // The tie average keeps the filtered material volume.
  CHECK(m.volume_m3.dot(rho) / m.total_volume_m3 ==
        doctest::Approx(m.volume_m3.dot(filtered) / m.total_volume_m3)
            .epsilon(1e-14));

  std::mt19937 rng(5);
  std::uniform_real_distribution<double> u(0, 1);
  for (int trial = 0; trial < 8; ++trial) {
    x << u(rng), u(rng);
    design_to_density(*p, x, rho);
    CHECK(rho[0] == doctest::Approx(rho[2]).epsilon(1e-14));
  }

  // Nonlinear asymmetric response J = sum c_e rho_e^3 + rho_0 rho_1: the
  // reverse chain matches central differences of the forward chain.
  const Eigen::Vector3d c(1.0, 2.0, 3.5);
  auto response = [&](const Eigen::VectorXd &xv) {
    Eigen::VectorXd r;
    design_to_density(*p, xv, r);
    return (c.array() * r.array().cube()).sum() + r[0] * r[1];
  };
  x << 0.3, 0.7;
  design_to_density(*p, x, rho);
  Eigen::VectorXd g_rho = (3.0 * c.array() * rho.array().square()).matrix();
  Eigen::VectorXd g_x;
  g_rho[0] += rho[1];
  g_rho[1] += rho[0];
  density_gradient_to_design(*p, g_rho, g_x);
  REQUIRE(g_x.size() == 2);
  const double h = 1e-6;
  for (int v = 0; v < 2; ++v) {
    Eigen::VectorXd xp = x, xm = x;
    xp[v] += h;
    xm[v] -= h;
    const double fd = (response(xp) - response(xm)) / (2 * h);
    CHECK(g_x[v] == doctest::Approx(fd).epsilon(1e-8));
  }
}

namespace {

// 4x3x3 box, 2 mm x/y cells mirrored about x = 4 mm, z layers 1, 2 and 3 mm
// thick: the tets of one extrusion column have three different volumes.
Box graded_box() {
  Box b = make_box(4, 3, 3, 2, 2, 1, true);
  static constexpr double layer_z[4] = {0, 1, 3, 6};
  for (std::size_t n = 0; 3 * n < b.points.size(); ++n)
    b.points[3 * n + 2] = layer_z[std::lround(b.points[3 * n + 2])];
  return b;
}

// Tanh threshold projection (Wang, Lazarov & Sigmund 2011) and derivative.
struct Heaviside {
  double beta = 4.0, eta = 0.5;
  double den() const {
    return std::tanh(beta * eta) + std::tanh(beta * (1 - eta));
  }
  double value(double r) const {
    return (std::tanh(beta * eta) + std::tanh(beta * (r - eta))) / den();
  }
  double derivative(double r) const {
    const double t = std::tanh(beta * (r - eta));
    return beta * (1 - t * t) / den();
  }
};

} // namespace

TEST_CASE("tie projection is a volume-weighted volume-preserving projection") {
  const Box b = graded_box();
  auto m = build_tet_mesh(b.points, b.tets);
  const int E = m->element_count();
  ElementRoles roles;
  roles.role.assign(E, ElementRole::Design);
  roles.reason.assign(E, 0);
  TyingSpec ext;
  ext.extrusion_axis = 2;
  ext.extrusion_cell_m = 2e-3;
  const DesignMap dm = build_design_map(*m, roles, ext);
  REQUIRE(dm.variable_count == 4 * 3);
  REQUIRE(dm.tie_weight.size() == E);

  // Each group's weights are V_e / V_v and sum to one; the groups are unequal.
  double ratio = 0.0;
  for (int v = 0; v < dm.variable_count; ++v) {
    double sum = 0.0, lo = 1e300, hi = 0.0;
    for (int k = dm.variable_offsets[v]; k < dm.variable_offsets[v + 1]; ++k) {
      const int e = dm.variable_elements[k];
      CHECK(dm.tie_weight[e] ==
            doctest::Approx(m->volume_m3[e] / dm.variable_volume_m3[v])
                .epsilon(1e-14));
      sum += dm.tie_weight[e];
      lo = std::min(lo, m->volume_m3[e]);
      hi = std::max(hi, m->volume_m3[e]);
    }
    CHECK(sum == doctest::Approx(1.0).epsilon(1e-14));
    ratio = std::max(ratio, hi / lo);
  }
  REQUIRE(ratio > 2.5);

  std::mt19937 rng(23);
  std::uniform_real_distribution<double> u(0, 1);
  Eigen::VectorXd y(E), g(E);
  for (int e = 0; e < E; ++e) {
    y[e] = u(rng);
    g[e] = u(rng) - 0.5;
  }
  Eigen::VectorXd ty = y;
  dm.project_ties(ty);
  // Volume-weighted (not arithmetic) mean per group, exact volume, bounds.
  double mean_gap = 0.0;
  for (int v = 0; v < dm.variable_count; ++v) {
    double vol = 0.0, arith = 0.0, lo = 1.0, hi = 0.0;
    const int begin = dm.variable_offsets[v], end = dm.variable_offsets[v + 1];
    for (int k = begin; k < end; ++k) {
      const int e = dm.variable_elements[k];
      vol += m->volume_m3[e] * y[e];
      arith += y[e];
      lo = std::min(lo, y[e]);
      hi = std::max(hi, y[e]);
    }
    vol /= dm.variable_volume_m3[v];
    arith /= end - begin;
    mean_gap = std::max(mean_gap, std::abs(vol - arith));
    for (int k = begin; k < end; ++k) {
      const int e = dm.variable_elements[k];
      CHECK(ty[e] == doctest::Approx(vol).epsilon(1e-13));
      CHECK(ty[e] >= lo - 1e-15);
      CHECK(ty[e] <= hi + 1e-15);
    }
  }
  CHECK(mean_gap > 1e-3);
  CHECK(
      m->volume_m3.dot(ty) / m->total_volume_m3 ==
      doctest::Approx(m->volume_m3.dot(y) / m->total_volume_m3).epsilon(1e-12));
  // Idempotent, and the transpose is exact.
  Eigen::VectorXd tty = ty;
  dm.project_ties(tty);
  CHECK((tty - ty).cwiseAbs().maxCoeff() < 1e-14);
  Eigen::VectorXd tg = g;
  dm.project_ties_transpose(tg);
  CHECK(ty.dot(g) == doctest::Approx(y.dot(tg)).epsilon(1e-13));
  // The volume functional's gradient is a fixed point of T^T.
  Eigen::VectorXd tv = m->volume_m3;
  dm.project_ties_transpose(tv);
  CHECK(((tv - m->volume_m3).cwiseAbs().maxCoeff()) <
        1e-14 * m->volume_m3.maxCoeff());

  CHECK(code_of([&] {
          Eigen::VectorXd bad(E + 1);
          dm.project_ties(bad);
        }) == "invalid_density");
  CHECK(code_of([&] {
          Eigen::VectorXd bad(E - 1);
          dm.project_ties_transpose(bad);
        }) == "invalid_density");

  // Without ties both operators are the identity.
  ElementRoles r2;
  r2.role.assign(E, ElementRole::Design);
  r2.reason.assign(E, 0);
  const DesignMap untied = build_design_map(*m, r2, TyingSpec{});
  REQUIRE(untied.tied_groups == 0);
  Eigen::VectorXd same = y;
  untied.project_ties(same);
  CHECK((same.array() == y.array()).all());
  untied.project_ties_transpose(same);
  CHECK((same.array() == y.array()).all());
}

TEST_CASE("mirror and extrusion ties survive filter and Heaviside projection "
          "with exact gradients and fixed passives") {
  const Box b = graded_box();
  auto mesh = build_tet_mesh(b.points, b.tets);
  const int E = mesh->element_count();
  auto p = std::make_shared<Problem>();
  p->mesh = mesh;
  p->roles.role.assign(E, ElementRole::Design);
  p->roles.reason.assign(E, 0);
  p->roles.role[0] = ElementRole::Solid; // cube (0,0,0) -> column (0,0)
  p->roles.role[((0 * 3 + 1) * 4 + 1) * 6] =
      ElementRole::Void; // cube (1,1,0) -> column (1,1)
  TyingSpec tie;
  tie.mirror = {true, false, false};
  tie.mirror_origin_m = Eigen::Vector3d(4e-3, 0, 0);
  tie.extrusion_axis = 2;
  tie.extrusion_cell_m = 2e-3;
  p->design = build_design_map(*mesh, p->roles, tie);
  p->filter_radius_m = 2.5e-3;
  p->filter_radius_source = "explicit";
  p->filter = build_density_filter(*mesh, p->filter_radius_m);
  const DesignMap &dm = p->design;
  // 12 columns, mirrored in pairs -> 6 groups; two became passive.
  REQUIRE(dm.variable_count == 4);
  REQUIRE(p->roles.solid_count == 36);
  REQUIRE(p->roles.void_count == 36);
  const int V = dm.variable_count;

  const Heaviside heaviside;
  Eigen::VectorXd raw, filtered, rho;
  // rho = passive(P(T H expand(x))); `filtered` keeps T H expand(x).
  auto chain = [&](const Eigen::VectorXd &x, Eigen::VectorXd &out) {
    dm.expand(x, raw);
    p->filter.apply(raw, filtered);
    dm.project_ties(filtered);
    out.resize(E);
    for (int e = 0; e < E; ++e)
      out[e] = dm.element_variable[e] < 0 ? dm.fixed_density[e]
                                          : heaviside.value(filtered[e]);
  };
  auto group_spread = [&](const Eigen::VectorXd &f) {
    double spread = 0.0;
    for (int v = 0; v < V; ++v)
      for (int k = dm.variable_offsets[v]; k < dm.variable_offsets[v + 1]; ++k)
        spread = std::max(
            spread, std::abs(f[dm.variable_elements[k]] -
                             f[dm.variable_elements[dm.variable_offsets[v]]]));
    return spread;
  };

  std::mt19937 rng(41);
  std::uniform_real_distribution<double> u(0.05, 0.95);
  Eigen::VectorXd x(V);
  for (int v = 0; v < V; ++v)
    x[v] = u(rng);
  // The filter alone mixes groups; T restores them, and P keeps them.
  dm.expand(x, raw);
  Eigen::VectorXd untied;
  p->filter.apply(raw, untied);
  REQUIRE(group_spread(untied) > 1e-3);
  chain(x, rho);
  CHECK(group_spread(filtered) < 1e-14);
  CHECK(group_spread(rho) < 1e-14);
  Eigen::VectorXd again = rho;
  dm.project_ties(again);
  CHECK((again - rho).cwiseAbs().maxCoeff() < 1e-14);
  // T keeps the filtered design-region volume.
  double v_untied = 0.0, v_tied = 0.0;
  for (int e = 0; e < E; ++e)
    if (dm.element_variable[e] >= 0) {
      v_untied += mesh->volume_m3[e] * untied[e];
      v_tied += mesh->volume_m3[e] * filtered[e];
    }
  CHECK(v_tied / mesh->total_volume_m3 ==
        doctest::Approx(v_untied / mesh->total_volume_m3).epsilon(1e-12));

  // design_to_density is the same chain without P.
  Eigen::VectorXd plain;
  design_to_density(*p, x, plain);
  for (int e = 0; e < E; ++e)
    CHECK(plain[e] ==
          (dm.element_variable[e] < 0 ? dm.fixed_density[e] : filtered[e]));

  // Passives stay fixed for any design, including the bounds.
  for (const double c : {0.0, 1.0}) {
    chain(Eigen::VectorXd::Constant(V, c), rho);
    design_to_density(*p, Eigen::VectorXd::Constant(V, c), plain);
    for (int e = 0; e < E; ++e)
      if (p->roles.role[e] != ElementRole::Design) {
        CHECK(rho[e] == (p->roles.role[e] == ElementRole::Solid ? 1.0 : 0.0));
        CHECK(plain[e] == rho[e]);
      }
  }

  // Reverse chain: zero passives, P', T^T, H^T, reduce; against central
  // differences of J = sum_e c_e rho_e^3 with asymmetric c.
  Eigen::VectorXd c(E);
  for (int e = 0; e < E; ++e)
    c[e] = 1.0 + 0.5 * std::sin(0.7 * e + 0.3);
  auto response = [&](const Eigen::VectorXd &xv) {
    Eigen::VectorXd r;
    chain(xv, r);
    return (c.array() * r.array().cube()).sum();
  };
  auto response_plain = [&](const Eigen::VectorXd &xv) {
    Eigen::VectorXd r;
    design_to_density(*p, xv, r);
    return (c.array() * r.array().cube()).sum();
  };
  chain(x, rho);
  Eigen::VectorXd g = (3.0 * c.array() * rho.array().square()).matrix();
  for (int e = 0; e < E; ++e)
    g[e] = dm.element_variable[e] < 0
               ? 0.0
               : g[e] * heaviside.derivative(filtered[e]);
  dm.project_ties_transpose(g);
  Eigen::VectorXd gh, gx;
  p->filter.apply_transpose(g, gh);
  dm.reduce_gradient(gh, gx);

  design_to_density(*p, x, plain);
  Eigen::VectorXd gp = (3.0 * c.array() * plain.array().square()).matrix(),
                  gx_plain;
  density_gradient_to_design(*p, gp, gx_plain);

  const double h = 1e-6;
  for (int v = 0; v < V; ++v) {
    Eigen::VectorXd xp = x, xm = x;
    xp[v] += h;
    xm[v] -= h;
    CHECK(
        gx[v] ==
        doctest::Approx((response(xp) - response(xm)) / (2 * h)).epsilon(1e-6));
    CHECK(gx_plain[v] ==
          doctest::Approx((response_plain(xp) - response_plain(xm)) / (2 * h))
              .epsilon(1e-6));
  }
}

TEST_CASE("minimum member filter respects the two-element mesh radius floor") {
  Cantilever c;
  const auto p = prepare_problem(c.mesh, c.spec({{"min_member_mm", 1.2}}));
  const double floor = 2.0 * c.mesh->quality.median_element_size_m;
  CHECK(p->filter_radius_m == doctest::Approx(floor));
  CHECK(p->filter_radius_source == "auto_2x_median_element_size");
  CHECK(p->evidence["filter"]["min_member_filter_radius_mm"].get<double>() ==
        doctest::Approx(0.6));
  CHECK(p->evidence["filter"]["mesh_floor_radius_mm"].get<double>() ==
        doctest::Approx(floor * 1e3));
  const auto large = prepare_problem(c.mesh, c.spec({{"min_member_mm", 80.0}}));
  CHECK(large->filter_radius_m == doctest::Approx(0.04));
  CHECK(large->filter_radius_source == "min_member_mm");
  const auto explicit_radius = prepare_problem(
      c.mesh, c.spec({{"min_member_mm", 1.2}, {"filter_radius_mm", 0.2}}));
  CHECK(explicit_radius->filter_radius_m == doctest::Approx(0.0002));
  CHECK(explicit_radius->filter_radius_source == "explicit");
}
