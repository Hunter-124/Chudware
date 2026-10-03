// Prepared-problem seam gates (sim/fea_prepare.h).
//
// prepare_fea_mesh, resolve_boundary_selection, resolve_mechanical_fixtures and
// resolve_mechanical_load_case are what an iterating solver (topology
// optimization) consumes INSTEAD of a study route, so every number a physics
// evaluator will trust is pinned here against closed forms on small inline tet
// meshes — OCCT-free and mesher-free:
//   - exact face areas, outward normals and consistent ∫N dA / ∫N n dA weights
//     (tet4 and straight-sided tet10);
//   - load resultants AND moments equal to the analytic patch integrals, with
//   no
//     force leaking onto a node off the loaded face;
//   - partial-DOF roller fixtures: which combinations leave rigid-body modes
//     free (refused) and which suppress all six;
//   - a planar selection with a face normal does not bleed through a thin plate
//     to the opposite surface, nor onto interior or orphan nodes;
//   - one conforming domain is accepted; disconnected/touching bodies, hanging
//     fragments, contact/interface/bond-gap declarations, malformed arrays and
//     all-zero load cases are refused.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include "sim/fea_prepare.h"

#include <algorithm>
#include <array>
#include <cmath>
#include <limits>
#include <map>
#include <memory>
#include <string>
#include <utility>
#include <vector>

using chudware::sim::BoundaryFacet;
using chudware::sim::BoundarySelection;
using chudware::sim::MechanicalFixtureSet;
using chudware::sim::MechanicalLoadVector;
using chudware::sim::PreparedMesh;
using chudware::sim::PrepareMeshOptions;
using nlohmann::json;
using Vec3 = std::array<double, 3>;

namespace {

constexpr double kTol = 1e-11;

// nx*ny*nz cubes of hx*hy*hz mm at `origin`, each split into six Kuhn tets
// around its 0-6 diagonal (conforming across cells; windings deliberately
// mixed).
struct Box {
  std::vector<double> points;
  std::vector<int> tets;
  int nx = 0, ny = 0, nz = 0;
  double lx = 0, ly = 0, lz = 0;
  int node(int i, int j, int k) const {
    return (k * (ny + 1) + j) * (nx + 1) + i;
  }
  std::size_t n_nodes() const { return points.size() / 3; }
};

Box make_box(int nx, int ny, int nz, double hx, double hy, double hz,
             Vec3 origin = {0.0, 0.0, 0.0}) {
  Box b;
  b.nx = nx;
  b.ny = ny;
  b.nz = nz;
  b.lx = nx * hx;
  b.ly = ny * hy;
  b.lz = nz * hz;
  for (int k = 0; k <= nz; ++k)
    for (int j = 0; j <= ny; ++j)
      for (int i = 0; i <= nx; ++i) {
        b.points.push_back(origin[0] + i * hx);
        b.points.push_back(origin[1] + j * hy);
        b.points.push_back(origin[2] + k * hz);
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

// Straight-sided tet10 from a tet4 mesh: shared mid-edge nodes appended after
// the corners, local order {0,1}=4 {1,2}=5 {2,0}=6 {3,0}=7 {3,2}=8 {3,1}=9.
struct Tet10 {
  std::vector<double> points;
  std::vector<int> tets;
  std::size_t corner_count = 0;
};

Tet10 to_tet10(const Box &b) {
  Tet10 out;
  out.points = b.points;
  out.corner_count = b.n_nodes();
  std::map<std::pair<int, int>, int> mids;
  auto mid = [&](int a, int c) {
    const auto key = std::minmax(a, c);
    const auto it = mids.find(key);
    if (it != mids.end())
      return it->second;
    const int id = static_cast<int>(out.points.size() / 3);
    for (int d = 0; d < 3; ++d)
      out.points.push_back(0.5 *
                           (b.points[static_cast<std::size_t>(a) * 3 + d] +
                            b.points[static_cast<std::size_t>(c) * 3 + d]));
    mids.emplace(key, id);
    return id;
  };
  static constexpr int edges[6][2] = {{0, 1}, {1, 2}, {2, 0},
                                      {3, 0}, {3, 2}, {3, 1}};
  for (std::size_t e = 0; e < b.tets.size(); e += 4) {
    const int *t = &b.tets[e];
    for (int i = 0; i < 4; ++i)
      out.tets.push_back(t[i]);
    for (const auto &edge : edges)
      out.tets.push_back(mid(t[edge[0]], t[edge[1]]));
  }
  return out;
}

json inline_study(const std::vector<double> &points,
                  const std::vector<int> &tets, int nodes_per_elem = 4) {
  return json{
      {"points", points}, {"tets", tets}, {"nodes_per_elem", nodes_per_elem}};
}

json box_study(const Box &b) { return inline_study(b.points, b.tets); }

// Concatenate two boxes WITHOUT sharing any node ids.
json two_body_study(const Box &a, const Box &b) {
  std::vector<double> points = a.points;
  points.insert(points.end(), b.points.begin(), b.points.end());
  std::vector<int> tets = a.tets;
  const int shift = static_cast<int>(a.n_nodes());
  for (int id : b.tets)
    tets.push_back(id + shift);
  return inline_study(points, tets);
}

std::shared_ptr<const PreparedMesh> prepare(const json &study) {
  std::string error = "stale";
  auto mesh = chudware::sim::prepare_fea_mesh(study, error);
  REQUIRE_MESSAGE(mesh, error);
  CHECK(error.empty());
  return mesh;
}

void check_prepare_refused(const json &study,
                           const PrepareMeshOptions &options = {}) {
  std::string error;
  auto mesh = chudware::sim::prepare_fea_mesh(study, error, options);
  CHECK_FALSE(mesh);
  CHECK_FALSE(error.empty());
}

// The planar face `axis == value` of the box b, as doc_face_points emits it:
// the face's corner samples, an in-plane reach covering the whole face, its
// unit outward normal and a perpendicular tolerance below one element layer.
json plane_faceset(const Box &b, int axis, double value,
                   double perp_tol = 0.05) {
  const double extent[3] = {b.lx, b.ly, b.lz};
  const int u = (axis + 1) % 3, v = (axis + 2) % 3;
  json points = json::array();
  for (double su : {0.0, extent[u]})
    for (double sv : {0.0, extent[v]}) {
      double p[3];
      p[axis] = value;
      p[u] = su;
      p[v] = sv;
      points.push_back({p[0], p[1], p[2]});
    }
  json normal = {0.0, 0.0, 0.0};
  normal[static_cast<std::size_t>(axis)] = value > 0.0 ? 1.0 : -1.0;
  return json{{"points", points},
              {"tol_mm", std::hypot(extent[u], extent[v])},
              {"face_normal", normal},
              {"perp_tol_mm", perp_tol}};
}

// Pure-Euclidean point picks (no face normal) at the given coordinates.
json point_faceset(const std::vector<Vec3> &samples, double tol = 0.01) {
  json points = json::array();
  for (const auto &p : samples)
    points.push_back({p[0], p[1], p[2]});
  return json{{"points", points}, {"tol_mm", tol}};
}

Vec3 coord(const std::vector<double> &points, int node) {
  const std::size_t i = static_cast<std::size_t>(node) * 3;
  return {points[i], points[i + 1], points[i + 2]};
}

Vec3 cross(const Vec3 &a, const Vec3 &b) {
  return {a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2],
          a[0] * b[1] - a[1] * b[0]};
}

Vec3 moment_about_origin(const std::vector<double> &points,
                         const std::vector<double> &forces) {
  Vec3 m{0.0, 0.0, 0.0};
  for (std::size_t n = 0; n < points.size() / 3; ++n) {
    const Vec3 f{forces[n * 3], forces[n * 3 + 1], forces[n * 3 + 2]};
    const Vec3 r = coord(points, static_cast<int>(n));
    const Vec3 rf = cross(r, f);
    for (int c = 0; c < 3; ++c)
      m[c] += rf[c];
  }
  return m;
}

void check_vec(const Vec3 &actual, const Vec3 &expected, double tol = kTol) {
  for (int c = 0; c < 3; ++c) {
    INFO("component " << c << ": " << actual[c] << " vs " << expected[c]);
    CHECK(std::abs(actual[c] - expected[c]) <=
          tol * (1.0 + std::abs(expected[c])));
  }
}

// Facet-level invariants every consumer relies on.
void check_flat_facets(const BoundarySelection &sel, const Vec3 &outward) {
  double area = 0.0;
  for (const BoundaryFacet &f : sel.facets) {
    check_vec(f.normal, outward);
    double w = 0.0;
    Vec3 nw{0.0, 0.0, 0.0};
    for (int i = 0; i < f.node_count; ++i) {
      CHECK(std::binary_search(sel.nodes.begin(), sel.nodes.end(),
                               f.nodes[static_cast<std::size_t>(i)]));
      w += f.weights[i];
      for (int c = 0; c < 3; ++c)
        nw[c] += f.normal_weights[i][c];
    }
    CHECK(w == doctest::Approx(f.area_mm2).epsilon(1e-12));
    check_vec(nw, {outward[0] * f.area_mm2, outward[1] * f.area_mm2,
                   outward[2] * f.area_mm2});
    area += f.area_mm2;
  }
  CHECK(area == doctest::Approx(sel.area_mm2).epsilon(1e-12));
  double node_area = 0.0;
  for (double a : sel.node_area_mm2)
    node_area += a;
  CHECK(node_area == doctest::Approx(sel.area_mm2).epsilon(1e-12));
}

} // namespace

// ── prepare_fea_mesh ─────────────────────────────────────────────────────────

TEST_CASE("prepare: one conforming tet4 box is a single referenced domain") {
  const Box b = make_box(3, 2, 2, 1.0, 1.5, 0.5);
  const auto mesh = prepare(box_study(b));
  CHECK(mesh->n_nodes() == b.n_nodes());
  CHECK(mesh->n_elems() == static_cast<std::size_t>(6 * 3 * 2 * 2));
  CHECK(mesh->connected_components == 1);
  CHECK(mesh->unreferenced_nodes == 0);
  CHECK(std::all_of(mesh->referenced.begin(), mesh->referenced.end(),
                    [](std::uint8_t r) { return r == 1; }));
}

TEST_CASE("prepare: an orphan node is eliminated, never selected or loaded") {
  Box b = make_box(1, 1, 1, 1.0, 1.0, 1.0);
  // Lies exactly ON the top face plane, inside the face: a pure geometric
  // match.
  b.points.insert(b.points.end(), {0.5, 0.5, 1.0});
  const int orphan = static_cast<int>(b.n_nodes()) - 1;
  const auto mesh = prepare(box_study(b));
  CHECK(mesh->connected_components == 1);
  CHECK(mesh->unreferenced_nodes == 1);
  CHECK(mesh->referenced[static_cast<std::size_t>(orphan)] == 0);

  const json top = plane_faceset(b, 2, 1.0);
  BoundarySelection sel;
  std::string error;
  REQUIRE_MESSAGE(
      chudware::sim::resolve_boundary_selection(top, *mesh, "top", sel, error),
      error);
  CHECK(sel.nodes == std::vector<int>{b.node(0, 0, 1), b.node(1, 0, 1),
                                      b.node(0, 1, 1), b.node(1, 1, 1)});
  CHECK(sel.area_mm2 == doctest::Approx(1.0).epsilon(1e-12));

  MechanicalFixtureSet fixtures;
  REQUIRE_MESSAGE(chudware::sim::resolve_mechanical_fixtures(
                      json::array({top}), *mesh, fixtures, error),
                  error);
  CHECK(fixtures.fixed_dof[static_cast<std::size_t>(orphan) * 3 + 2] == 0);
  CHECK(fixtures.fixed_nodes == sel.nodes);

  MechanicalLoadVector loads;
  REQUIRE_MESSAGE(chudware::sim::resolve_mechanical_load_case(
                      json::array({{{"faces", top},
                                    {"type", "force"},
                                    {"vector", {0.0, 0.0, -8.0}}}}),
                      *mesh, loads, error),
                  error);
  for (int c = 0; c < 3; ++c)
    CHECK(loads.forces[static_cast<std::size_t>(orphan) * 3 + c] == 0.0);
  for (int n : sel.nodes)
    CHECK(loads.forces[static_cast<std::size_t>(n) * 3 + 2] ==
          doctest::Approx(-2.0));
}

TEST_CASE("prepare: disconnected and merely touching bodies are refused") {
  const Box a = make_box(1, 1, 1, 1.0, 1.0, 1.0);
  SUBCASE("separated by a gap") {
    const Box b = make_box(1, 1, 1, 1.0, 1.0, 1.0, {2.0, 0.0, 0.0});
    check_prepare_refused(two_body_study(a, b));
  }
  SUBCASE("coincident faces without shared nodes are not silently glued") {
    const Box b = make_box(1, 1, 1, 1.0, 1.0, 1.0, {1.0, 0.0, 0.0});
    check_prepare_refused(two_body_study(a, b));
  }
  SUBCASE("three islands are refused") {
    const Box b = make_box(1, 1, 1, 1.0, 1.0, 1.0, {3.0, 0.0, 0.0});
    const Box c = make_box(1, 1, 1, 1.0, 1.0, 1.0, {6.0, 0.0, 0.0});
    json study = two_body_study(a, b);
    std::vector<double> points = study["points"].get<std::vector<double>>();
    std::vector<int> tets = study["tets"].get<std::vector<int>>();
    const int shift = static_cast<int>(points.size() / 3);
    points.insert(points.end(), c.points.begin(), c.points.end());
    for (int id : c.tets)
      tets.push_back(id + shift);
    check_prepare_refused(inline_study(points, tets));
  }
}

TEST_CASE(
    "prepare: a fragment hanging from one corner is a refused mechanism") {
  Box b = make_box(1, 1, 1, 1.0, 1.0, 1.0);
  const int hinge = b.node(1, 1, 1);
  const int first = static_cast<int>(b.n_nodes());
  b.points.insert(b.points.end(),
                  {2.0, 1.0, 1.0, 1.0, 2.0, 1.0, 1.0, 1.0, 2.0});
  b.tets.insert(b.tets.end(), {hinge, first, first + 1, first + 2});
  // Node-connected (one component), yet the flap rotates about its hinge
  // freely.
  check_prepare_refused(box_study(b));
}

TEST_CASE("prepare: contact, interfaces and bond gaps are refused, not "
          "approximated") {
  const Box b = make_box(1, 1, 1, 1.0, 1.0, 1.0);
  PrepareMeshOptions topology;
  topology.context = "topology";
  for (const json &block :
       {json{{"contact", json::object()}},
        json{{"contact", {{"enabled", true}}}},
        json{{"interfaces", json::object()}}, json{{"bond_gap_mm", 0.05}}}) {
    json study = box_study(b);
    study.update(block);
    INFO(block.dump());
    check_prepare_refused(study, topology);
  }
  // An explicitly disabled contact block and a zero bond gap declare nothing.
  for (const json &block :
       {json{{"contact", {{"enabled", false}}}}, json{{"bond_gap_mm", 0.0}}}) {
    json study = box_study(b);
    study.update(block);
    INFO(block.dump());
    CHECK(prepare(study)->connected_components == 1);
  }
}

TEST_CASE("prepare: malformed inline arrays are refused before any traversal") {
  const Box b = make_box(1, 1, 1, 1.0, 1.0, 1.0);
  check_prepare_refused(json::object());
  check_prepare_refused(json::array());
  {
    json study = box_study(b);
    study["points"].push_back(0.0);
    check_prepare_refused(study);
  }
  {
    json study = box_study(b);
    study["points"][4] = std::numeric_limits<double>::quiet_NaN();
    check_prepare_refused(study);
  }
  {
    json study = box_study(b);
    study["nodes_per_elem"] = 6;
    check_prepare_refused(study);
  }
  {
    json study = box_study(b);
    study["nodes_per_elem"] = "4";
    check_prepare_refused(study);
  }
  {
    json study = box_study(b);
    study["tets"].push_back(0);
    check_prepare_refused(study);
  }
  {
    json study = box_study(b);
    study["tets"][3] = static_cast<int>(b.n_nodes());
    check_prepare_refused(study);
  }
  {
    json study = box_study(b);
    study["tets"][0] = -1;
    check_prepare_refused(study);
  }
}

// ── resolve_boundary_selection ───────────────────────────────────────────────

TEST_CASE("selection: tet4 faces carry exact area and outward normals") {
  const Box b = make_box(3, 2, 2, 1.0, 1.5, 0.5); // 3 x 3 x 1 mm
  const auto mesh = prepare(box_study(b));
  struct Face {
    int axis;
    double value;
    Vec3 outward;
    double area;
    int cells;
  };
  const Face faces[] = {
      {2, b.lz, {0, 0, 1}, b.lx * b.ly, b.nx * b.ny},
      {2, 0.0, {0, 0, -1}, b.lx * b.ly, b.nx * b.ny},
      {0, b.lx, {1, 0, 0}, b.ly * b.lz, b.ny * b.nz},
      {0, 0.0, {-1, 0, 0}, b.ly * b.lz, b.ny * b.nz},
      {1, b.ly, {0, 1, 0}, b.lx * b.lz, b.nx * b.nz},
      {1, 0.0, {0, -1, 0}, b.lx * b.lz, b.nx * b.nz},
  };
  for (const Face &face : faces) {
    INFO("axis " << face.axis << " = " << face.value);
    BoundarySelection sel;
    std::string error;
    REQUIRE_MESSAGE(
        chudware::sim::resolve_boundary_selection(
            plane_faceset(b, face.axis, face.value), *mesh, "face", sel, error),
        error);
    CHECK(sel.area_mm2 == doctest::Approx(face.area).epsilon(1e-12));
    CHECK(sel.facets.size() == static_cast<std::size_t>(2 * face.cells));
    CHECK(std::is_sorted(sel.nodes.begin(), sel.nodes.end()));
    for (int n : sel.nodes)
      CHECK(coord(b.points, n)[static_cast<std::size_t>(face.axis)] ==
            face.value);
    for (const BoundaryFacet &f : sel.facets) {
      CHECK(f.node_count == 3);
      CHECK(f.element >= 0);
      CHECK(f.face >= 0);
      CHECK(f.face < 4);
    }
    check_flat_facets(sel, face.outward);
  }
}

TEST_CASE("selection: a thin-plate face never bleeds to the opposite surface") {
  // 4 x 3 x 0.5 mm plate, one element through the thickness: every node is on
  // the skin, and the face's in-plane reach (5 mm) is ten times the thickness.
  const Box b = make_box(4, 3, 1, 1.0, 1.0, 0.5);
  const auto mesh = prepare(box_study(b));
  const json top = plane_faceset(b, 2, b.lz, 0.1);
  BoundarySelection sel;
  std::string error;
  REQUIRE_MESSAGE(
      chudware::sim::resolve_boundary_selection(top, *mesh, "top", sel, error),
      error);
  CHECK(sel.nodes.size() == static_cast<std::size_t>((b.nx + 1) * (b.ny + 1)));
  for (int n : sel.nodes)
    CHECK(coord(b.points, n)[2] == b.lz);
  CHECK(sel.area_mm2 == doctest::Approx(12.0).epsilon(1e-12));
  check_flat_facets(sel, {0.0, 0.0, 1.0});

  // Control: the same reach WITHOUT the face normal is a Euclidean ball that
  // swallows the whole skin — top, bottom and all four sides (31 mm²).
  json legacy = top;
  legacy.erase("face_normal");
  legacy.erase("perp_tol_mm");
  BoundarySelection everything;
  REQUIRE_MESSAGE(chudware::sim::resolve_boundary_selection(
                      legacy, *mesh, "legacy", everything, error),
                  error);
  CHECK(everything.nodes.size() == b.n_nodes());
  CHECK(everything.area_mm2 ==
        doctest::Approx(2 * 12.0 + 2 * 2.0 + 2 * 1.5).epsilon(1e-12));

  // The normal-filtered pressure deposits exactly −p·A·ẑ, only on top nodes.
  MechanicalLoadVector loads;
  REQUIRE_MESSAGE(
      chudware::sim::resolve_mechanical_load_case(
          json::array(
              {{{"faces", top}, {"type", "pressure"}, {"magnitude", 2.0}}}),
          *mesh, loads, error),
      error);
  check_vec(loads.resultant_n, {0.0, 0.0, -24.0});
  check_vec(moment_about_origin(mesh->points, loads.forces),
            cross({2.0, 1.5, 0.5}, {0.0, 0.0, -24.0}));
  for (int n = 0; n < static_cast<int>(b.n_nodes()); ++n)
    if (coord(b.points, n)[2] != b.lz)
      for (int c = 0; c < 3; ++c)
        CHECK(loads.forces[static_cast<std::size_t>(n) * 3 + c] == 0.0);
}

TEST_CASE(
    "selection: interior nodes and edge/point picks hold no traction face") {
  const Box b = make_box(2, 2, 2, 1.0, 1.0, 1.0);
  const auto mesh = prepare(box_study(b));
  BoundarySelection sel;
  std::string error;

  // The only interior node: geometrically matched, but not on the skin.
  CHECK_FALSE(chudware::sim::resolve_boundary_selection(
      point_faceset({{1.0, 1.0, 1.0}}), *mesh, "core", sel, error));
  CHECK_FALSE(error.empty());

  // A straight edge: a valid nodal (Dirichlet) set with no complete facet.
  REQUIRE_MESSAGE(
      chudware::sim::resolve_boundary_selection(
          point_faceset({{0.0, 0.0, 0.0}, {0.0, 1.0, 0.0}, {0.0, 2.0, 0.0}}),
          *mesh, "edge", sel, error),
      error);
  CHECK(sel.nodes ==
        std::vector<int>{b.node(0, 0, 0), b.node(0, 1, 0), b.node(0, 2, 0)});
  CHECK(sel.facets.empty());
  CHECK(sel.area_mm2 == 0.0);
  CHECK(sel.node_area_mm2 == std::vector<double>(3, 0.0));

  // A single corner.
  REQUIRE_MESSAGE(
      chudware::sim::resolve_boundary_selection(
          point_faceset({{2.0, 2.0, 2.0}}), *mesh, "corner", sel, error),
      error);
  CHECK(sel.nodes == std::vector<int>{b.node(2, 2, 2)});
  CHECK(sel.facets.empty());

  // Malformed facesets.
  CHECK_FALSE(chudware::sim::resolve_boundary_selection(json::array(), *mesh,
                                                        "bad", sel, error));
  CHECK_FALSE(error.empty());
  CHECK_FALSE(chudware::sim::resolve_boundary_selection(
      json{{"tol_mm", 1.0}}, *mesh, "bad", sel, error));
  CHECK_FALSE(error.empty());
  json no_reach = point_faceset({{0.0, 0.0, 0.0}});
  no_reach["tol_mm"] = 0.0;
  CHECK_FALSE(chudware::sim::resolve_boundary_selection(no_reach, *mesh, "bad",
                                                        sel, error));
  CHECK_FALSE(error.empty());
}

TEST_CASE("selection: straight tet10 faces integrate to mid-side tributaries") {
  const Box b = make_box(2, 1, 1, 1.0, 2.0, 1.5); // 2 x 2 x 1.5 mm
  const Tet10 t = to_tet10(b);
  const auto mesh = prepare(inline_study(t.points, t.tets, 10));
  CHECK(mesh->connected_components == 1);

  const json top = plane_faceset(b, 2, b.lz);
  BoundarySelection sel;
  std::string error;
  REQUIRE_MESSAGE(
      chudware::sim::resolve_boundary_selection(top, *mesh, "top", sel, error),
      error);
  const double area = b.lx * b.ly;
  CHECK(sel.area_mm2 == doctest::Approx(area).epsilon(1e-12));
  CHECK(sel.facets.size() == static_cast<std::size_t>(2 * b.nx * b.ny));
  double mid_sum = 0.0;
  for (std::size_t i = 0; i < sel.nodes.size(); ++i) {
    CHECK(coord(t.points, sel.nodes[i])[2] == doctest::Approx(b.lz));
    if (static_cast<std::size_t>(sel.nodes[i]) < t.corner_count) {
      // ∫ N_corner dA = 0 on a straight quadratic triangle.
      CHECK(std::abs(sel.node_area_mm2[i]) < 1e-12);
    } else {
      CHECK(sel.node_area_mm2[i] > 0.0);
      mid_sum += sel.node_area_mm2[i];
    }
  }
  CHECK(mid_sum == doctest::Approx(area).epsilon(1e-12));
  for (const BoundaryFacet &f : sel.facets) {
    CHECK(f.node_count == 6);
    check_vec(f.normal, {0.0, 0.0, 1.0});
    for (int i = 0; i < 3; ++i)
      CHECK(std::abs(f.weights[i]) < 1e-12);
    for (int i = 3; i < 6; ++i)
      CHECK(f.weights[i] == doctest::Approx(f.area_mm2 / 3.0).epsilon(1e-12));
  }
  check_flat_facets(sel, {0.0, 0.0, 1.0});

  // Work-consistent force: corners carry nothing, the resultant is F, and the
  // line of action passes through the face centroid.
  const Vec3 force{3.0, -1.0, -12.0};
  MechanicalLoadVector loads;
  REQUIRE_MESSAGE(
      chudware::sim::resolve_mechanical_load_case(
          json::array({{{"faces", top}, {"type", "force"}, {"vector", force}}}),
          *mesh, loads, error),
      error);
  for (std::size_t n = 0; n < t.corner_count; ++n)
    for (int c = 0; c < 3; ++c)
      CHECK(std::abs(loads.forces[n * 3 + c]) < 1e-12);
  check_vec(loads.resultant_n, force);
  const Vec3 centroid{b.lx / 2, b.ly / 2, b.lz};
  check_vec(moment_about_origin(mesh->points, loads.forces),
            cross(centroid, force), 1e-10);

  // Consistent pressure: −p·A·n̂ through the centroid.
  REQUIRE_MESSAGE(
      chudware::sim::resolve_mechanical_load_case(
          json::array(
              {{{"faces", top}, {"type", "pressure"}, {"magnitude", 0.5}}}),
          *mesh, loads, error),
      error);
  const Vec3 resultant{0.0, 0.0, -0.5 * area};
  check_vec(loads.resultant_n, resultant);
  check_vec(moment_about_origin(mesh->points, loads.forces),
            cross(centroid, resultant), 1e-10);
}

// ── resolve_mechanical_fixtures ──────────────────────────────────────────────

TEST_CASE("fixtures: roller sets free or suppress rigid-body modes exactly") {
  const Box b = make_box(2, 2, 1, 1.0, 1.0, 1.0); // 2 x 2 x 1 mm
  const auto mesh = prepare(box_study(b));
  auto roller = [&](int axis, double value, int dof) {
    json f = plane_faceset(b, axis, value);
    f["dofs"] = json::array({dof});
    return f;
  };
  MechanicalFixtureSet out;
  std::string error;

  // Bottom on z-rollers: Tz, Rx, Ry held; Tx, Ty, Rz free.
  CHECK_FALSE(chudware::sim::resolve_mechanical_fixtures(
      json::array({roller(2, 0.0, 2)}), *mesh, out, error));
  CHECK_FALSE(error.empty());

  // + x-rollers on x=0: Tx and Rz held; Ty still free.
  CHECK_FALSE(chudware::sim::resolve_mechanical_fixtures(
      json::array({roller(2, 0.0, 2), roller(0, 0.0, 0)}), *mesh, out, error,
      "topology"));
  CHECK_FALSE(error.empty());

  // + y-rollers on y=0: the classic 3-2-1 roller support holds all six.
  json rollers =
      json::array({roller(2, 0.0, 2), roller(0, 0.0, 0), roller(1, 0.0, 1)});
  REQUIRE_MESSAGE(
      chudware::sim::resolve_mechanical_fixtures(rollers, *mesh, out, error),
      error);
  CHECK(error.empty());
  REQUIRE(out.fixed_dof.size() == b.n_nodes() * 3);
  std::size_t expected_count = 0;
  std::vector<int> constrained;
  for (int n = 0; n < static_cast<int>(b.n_nodes()); ++n) {
    const Vec3 p = coord(b.points, n);
    int count = 0;
    for (int d = 0; d < 3; ++d) {
      const bool fixed = p[static_cast<std::size_t>(d)] == 0.0;
      CHECK(out.fixed_dof[static_cast<std::size_t>(n) * 3 + d] ==
            (fixed ? 1 : 0));
      count += fixed ? 1 : 0;
    }
    expected_count += static_cast<std::size_t>(count);
    if (count > 0)
      constrained.push_back(n);
  }
  CHECK(out.fixed_dof_count == expected_count);
  CHECK(out.fixed_dof_count == 9u + 6u + 6u);
  CHECK(out.constrained_nodes == constrained);
  CHECK(out.fixed_nodes == std::vector<int>{b.node(0, 0, 0)});
  REQUIRE(out.selections.size() == 3u);
  CHECK(out.selections[0].nodes.size() == 9u);
  CHECK(out.selections[1].nodes.size() == 6u);
  CHECK(out.selections[2].nodes.size() == 6u);
}

TEST_CASE(
    "fixtures: point and line clamps leave modes free; a face clamp does not") {
  const Box b = make_box(2, 2, 1, 1.0, 1.0, 1.0);
  const auto mesh = prepare(box_study(b));
  MechanicalFixtureSet out;
  std::string error;
  const json point = point_faceset({{0.0, 0.0, 0.0}});
  const json line =
      point_faceset({{0.0, 0.0, 0.0}, {0.0, 1.0, 0.0}, {0.0, 2.0, 0.0}});

  CHECK_FALSE(chudware::sim::resolve_mechanical_fixtures(json::array({point}),
                                                         *mesh, out, error));
  CHECK_FALSE(error.empty());

  // Rotation about the clamped line itself survives.
  CHECK_FALSE(chudware::sim::resolve_mechanical_fixtures(json::array({line}),
                                                         *mesh, out, error));
  CHECK_FALSE(error.empty());

  // One off-line point pins that rotation.
  REQUIRE_MESSAGE(chudware::sim::resolve_mechanical_fixtures(
                      json::array({line, point_faceset({{2.0, 0.0, 0.0}})}),
                      *mesh, out, error),
                  error);
  CHECK(out.fixed_nodes == std::vector<int>{b.node(0, 0, 0), b.node(2, 0, 0),
                                            b.node(0, 1, 0), b.node(0, 2, 0)});
  CHECK(out.fixed_dof_count == 12u);

  // Full bottom clamp: every bottom node fixed in x, y, z and nothing else.
  REQUIRE_MESSAGE(
      chudware::sim::resolve_mechanical_fixtures(
          json::array({plane_faceset(b, 2, 0.0)}), *mesh, out, error),
      error);
  std::vector<int> bottom;
  for (int j = 0; j <= b.ny; ++j)
    for (int i = 0; i <= b.nx; ++i)
      bottom.push_back(b.node(i, j, 0));
  CHECK(out.fixed_nodes == bottom);
  CHECK(out.constrained_nodes == bottom);
  CHECK(out.fixed_dof_count == 27u);
}

TEST_CASE("fixtures: malformed lists and dofs are refused") {
  const Box b = make_box(1, 1, 1, 1.0, 1.0, 1.0);
  const auto mesh = prepare(box_study(b));
  MechanicalFixtureSet out;
  std::string error;
  auto refuse = [&](const json &fixtures) {
    CHECK_FALSE(chudware::sim::resolve_mechanical_fixtures(fixtures, *mesh, out,
                                                           error));
    CHECK_FALSE(error.empty());
  };
  refuse(json::array());
  refuse(json::object());
  refuse(json::array({42}));
  refuse(json::array({json{{"tol_mm", 1.0}}}));
  json bottom = plane_faceset(b, 2, 0.0);
  for (const json &dofs :
       {json::array(), json::array({3}), json::array({0, 0}), json::array({-1}),
        json::array({0.5}), json::array({0, 1, 2, 0}), json("x")}) {
    INFO(dofs.dump());
    json f = bottom;
    f["dofs"] = dofs;
    refuse(json::array({f}));
  }
  refuse(json::array({point_faceset({{5.0, 5.0, 5.0}})}));
}

// ── resolve_mechanical_load_case ─────────────────────────────────────────────

TEST_CASE(
    "loads: combined force and pressure match analytic resultant and moment") {
  const Box b = make_box(2, 2, 2, 2.0, 1.0, 1.0); // 4 x 2 x 2 mm
  const auto mesh = prepare(box_study(b));
  const json top = plane_faceset(b, 2, b.lz);
  const json end = plane_faceset(b, 0, b.lx);
  const Vec3 force{10.0, 0.0, -30.0};
  const double pressure = 1.5; // MPa, inward on the x = 4 face (4 mm²)
  json loads = json::array(
      {{{"faces", top}, {"type", "force"}, {"vector", force}},
       {{"faces", end}, {"type", "pressure"}, {"magnitude", pressure}}});
  MechanicalLoadVector out;
  std::string error;
  REQUIRE_MESSAGE(
      chudware::sim::resolve_mechanical_load_case(loads, *mesh, out, error),
      error);
  REQUIRE(out.forces.size() == b.n_nodes() * 3);

  const Vec3 push{-pressure * 4.0, 0.0, 0.0};
  check_vec(out.resultant_n, {force[0] + push[0], force[1], force[2]});
  Vec3 sum{0.0, 0.0, 0.0};
  for (std::size_t i = 0; i < out.forces.size(); ++i)
    sum[i % 3] += out.forces[i];
  check_vec(sum, out.resultant_n);

  // Equal split over the 9 top nodes acts at their mean (the face centroid);
  // the consistent pressure acts at the end-face centroid.
  const Vec3 m_force = cross({2.0, 1.0, 2.0}, force);
  const Vec3 m_push = cross({4.0, 1.0, 1.0}, push);
  check_vec(
      moment_about_origin(mesh->points, out.forces),
      {m_force[0] + m_push[0], m_force[1] + m_push[1], m_force[2] + m_push[2]});

  // Only nodes on the two loaded faces carry load.
  for (int n = 0; n < static_cast<int>(b.n_nodes()); ++n) {
    const Vec3 p = coord(b.points, n);
    if (p[2] == b.lz || p[0] == b.lx)
      continue;
    for (int c = 0; c < 3; ++c)
      CHECK(out.forces[static_cast<std::size_t>(n) * 3 + c] == 0.0);
  }
  REQUIRE(out.selections.size() == 2u);
  CHECK(out.selections[0].nodes.size() == 9u);
  CHECK(out.selections[1].nodes.size() == 9u);

  // The pressure alone deposits −p · (∫N dA) · n̂ on exactly the nodes and
  // tributary areas resolve_boundary_selection reports for the same face.
  BoundarySelection sel;
  REQUIRE_MESSAGE(
      chudware::sim::resolve_boundary_selection(end, *mesh, "end", sel, error),
      error);
  MechanicalLoadVector alone;
  REQUIRE_MESSAGE(chudware::sim::resolve_mechanical_load_case(
                      json::array({loads[1]}), *mesh, alone, error),
                  error);
  CHECK(alone.selections[0].nodes == sel.nodes);
  for (std::size_t i = 0; i < sel.nodes.size(); ++i) {
    const std::size_t n = static_cast<std::size_t>(sel.nodes[i]);
    CHECK(alone.forces[n * 3] ==
          doctest::Approx(-pressure * sel.node_area_mm2[i]));
    CHECK(alone.forces[n * 3 + 1] == 0.0);
    CHECK(alone.forces[n * 3 + 2] == 0.0);
  }
}

TEST_CASE("loads: zero, unresolvable and faceless loads are refused") {
  const Box b = make_box(2, 2, 1, 1.0, 1.0, 1.0);
  const auto mesh = prepare(box_study(b));
  const json top = plane_faceset(b, 2, b.lz);
  const json edge =
      point_faceset({{0.0, 0.0, 0.0}, {0.0, 1.0, 0.0}, {0.0, 2.0, 0.0}});
  const json nowhere = point_faceset({{9.0, 9.0, 9.0}});
  MechanicalLoadVector out;
  std::string error;
  auto refuse = [&](const json &loads) {
    CHECK_FALSE(
        chudware::sim::resolve_mechanical_load_case(loads, *mesh, out, error));
    CHECK_FALSE(error.empty());
    CHECK(out.forces.empty());
    CHECK(out.selections.empty());
  };

  refuse(json::array());
  refuse(json::array(
      {{{"faces", top}, {"type", "force"}, {"vector", {0.0, 0.0, 0.0}}}}));
  refuse(json::array(
      {{{"faces", top}, {"type", "pressure"}, {"magnitude", 0.0}},
       {{"faces", top}, {"type", "force"}, {"vector", {0, 0, 0}}}}));
  refuse(json::array(
      {{{"faces", top}, {"type", "moment"}, {"vector", {1, 0, 0}}}}));
  refuse(
      json::array({{{"faces", top}, {"type", "force"}, {"vector", {1, 0}}}}));
  refuse(json::array({{{"type", "force"}, {"vector", {1, 0, 0}}}}));
  refuse(json::array(
      {{{"faces", nowhere}, {"type", "force"}, {"vector", {0.0, 0.0, -1.0}}}}));
  // Three collinear skin nodes hold no complete facet: a pressure needs one.
  refuse(json::array(
      {{{"faces", edge}, {"type", "pressure"}, {"magnitude", 1.0}}}));

  // A force on that same edge is valid: equal nodal split, resultant exact.
  REQUIRE_MESSAGE(chudware::sim::resolve_mechanical_load_case(
                      json::array({{{"faces", edge},
                                    {"type", "force"},
                                    {"vector", {0.0, 6.0, 0.0}}}}),
                      *mesh, out, error),
                  error);
  check_vec(out.resultant_n, {0.0, 6.0, 0.0});
  for (int n : {b.node(0, 0, 0), b.node(0, 1, 0), b.node(0, 2, 0)})
    CHECK(out.forces[static_cast<std::size_t>(n) * 3 + 1] ==
          doctest::Approx(2.0));

  // A ZERO load is a no-op even when its faces resolve to nothing; the case
  // keeps only the real load.
  REQUIRE_MESSAGE(
      chudware::sim::resolve_mechanical_load_case(
          json::array(
              {{{"faces", nowhere}, {"type", "pressure"}, {"magnitude", 0.0}},
               {{"faces", top},
                {"type", "force"},
                {"vector", {0.0, 0.0, -9.0}}}}),
          *mesh, out, error, "topology"),
      error);
  check_vec(out.resultant_n, {0.0, 0.0, -9.0});
  REQUIRE(out.selections.size() == 1u);
  CHECK(out.selections[0].nodes.size() ==
        static_cast<std::size_t>((b.nx + 1) * (b.ny + 1)));
  for (int n : out.selections[0].nodes)
    CHECK(coord(b.points, n)[2] == b.lz);
}
