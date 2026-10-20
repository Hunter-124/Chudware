#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include "aicm_loader.h" // host AICM loader (host/src/render on the include path)
#include "sim/topopt_surface.h"

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iterator>
#include <limits>
#include <random>
#include <set>
#include <string>
#include <tuple>
#include <vector>

using namespace chudware::sim::topology;
namespace fs = std::filesystem;

namespace {

// Box of nx*ny*nz cubes (mm), each split into six Kuhn tets around the 0-6
// diagonal.
struct Box {
  std::vector<double> points;
  std::vector<int> tets;
  int nx, ny, nz;
  double hx, hy, hz;
  int node(int i, int j, int k) const {
    return (k * (ny + 1) + j) * (nx + 1) + i;
  }
  int index(int n, int axis) const { // grid index of node n along axis
    const double h = axis == 0 ? hx : axis == 1 ? hy : hz;
    return static_cast<int>(std::llround(points[3 * n + axis] / h));
  }
  double volume_mm3() const { return nx * hx * ny * hy * nz * hz; }
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

std::shared_ptr<const TetMesh> mesh_of(const Box &b) {
  return build_tet_mesh(b.points, b.tets, 4);
}

constexpr double kMm3 = 1e-9; // m^3 per mm^3

// Nodal density linear in the x grid index: d = i / nx (exact rationals).
Eigen::VectorXd ramp_x(const Box &b) {
  const int N = static_cast<int>(b.points.size() / 3);
  Eigen::VectorXd d(N);
  for (int n = 0; n < N; ++n)
    d[n] = static_cast<double>(b.index(n, 0)) / b.nx;
  return d;
}

template <class F> std::string error_code(F &&f) {
  try {
    f();
  } catch (const ModelError &e) {
    return e.code();
  }
  return {};
}

int distinct_positions(const SurfaceMesh &s) {
  std::set<std::tuple<double, double, double>> seen;
  for (int v = 0; v < s.vertex_count(); ++v)
    seen.emplace(s.points_m(0, v), s.points_m(1, v), s.points_m(2, v));
  return static_cast<int>(seen.size());
}

struct TempDir {
  fs::path path;
  TempDir() {
    std::random_device rd;
    for (int i = 0; i < 64 && path.empty(); ++i) {
      const fs::path p = fs::temp_directory_path() /
                         ("chudware_topopt_surface_" + std::to_string(rd()) +
                          "_" + std::to_string(rd()));
      std::error_code ec;
      if (fs::create_directory(p, ec))
        path = p;
    }
    REQUIRE_FALSE(path.empty());
  }
  ~TempDir() {
    std::error_code ec;
    fs::remove_all(path, ec);
  }
  std::string file(const std::string &name) const {
    return (path / name).string();
  }
};

std::vector<unsigned char> read_bytes(const std::string &path) {
  std::ifstream in(path, std::ios::binary);
  return {std::istreambuf_iterator<char>(in), std::istreambuf_iterator<char>()};
}

float f32_at(const std::vector<unsigned char> &b, std::size_t off) {
  std::uint32_t u = 0;
  for (int i = 0; i < 4; ++i)
    u |= static_cast<std::uint32_t>(b[off + i]) << (8 * i);
  float f;
  std::memcpy(&f, &u, 4);
  return f;
}

// Numbers of a VTU DataArray: `name` selects by Name attribute, "" picks the
// Points array.
std::vector<double> vtu_array(const std::string &xml, const std::string &name) {
  std::size_t at =
      name.empty() ? xml.find("<Points>") : xml.find("Name=\"" + name + "\"");
  REQUIRE(at != std::string::npos);
  at = xml.find('>', name.empty() ? xml.find("<DataArray", at) : at);
  const std::size_t end = xml.find("</DataArray>", at);
  std::vector<double> out;
  const char *p = xml.c_str() + at + 1;
  const char *stop = xml.c_str() + end;
  while (p < stop) {
    char *next = nullptr;
    const double v = std::strtod(p, &next);
    if (next == p) {
      ++p;
      continue;
    }
    out.push_back(v);
    p = next;
  }
  return out;
}

long long xml_int_attr(const std::string &xml, const std::string &attr) {
  const std::size_t at = xml.find(attr + "=\"");
  REQUIRE(at != std::string::npos);
  return std::atoll(xml.c_str() + at + attr.size() + 2);
}

bool has_issue(const SurfaceEvidence &ev, const std::string &code) {
  return std::find(ev.issues.begin(), ev.issues.end(), code) != ev.issues.end();
}

// Hand-built surfaces of separately indexed convex shells (metres). Each face
// is wound away from (outward) or toward (inward) its shell's centroid.
struct ShellSoup {
  std::vector<Eigen::Vector3d> pts;
  std::vector<std::array<int, 3>> tris;

  void add_convex(const std::vector<Eigen::Vector3d> &corners,
                  const std::vector<std::array<int, 3>> &faces, bool outward) {
    const int base = static_cast<int>(pts.size());
    Eigen::Vector3d centre = Eigen::Vector3d::Zero();
    for (const Eigen::Vector3d &p : corners)
      centre += p;
    centre /= static_cast<double>(corners.size());
    pts.insert(pts.end(), corners.begin(), corners.end());
    for (std::array<int, 3> f : faces) {
      const Eigen::Vector3d &a = corners[f[0]], &b = corners[f[1]],
                            &c = corners[f[2]];
      const bool away =
          (b - a).cross(c - a).dot((a + b + c) / 3.0 - centre) > 0;
      if (away != outward)
        std::swap(f[1], f[2]);
      tris.push_back({base + f[0], base + f[1], base + f[2]});
    }
  }
  void add_box(const Eigen::Vector3d &lo, const Eigen::Vector3d &hi,
               bool outward) {
    std::vector<Eigen::Vector3d> c;
    for (int k = 0; k < 8; ++k)
      c.emplace_back(k & 1 ? hi.x() : lo.x(), k & 2 ? hi.y() : lo.y(),
                     k & 4 ? hi.z() : lo.z());
    add_convex(c,
               {{0, 1, 3},
                {0, 3, 2},
                {4, 5, 7},
                {4, 7, 6},
                {0, 1, 5},
                {0, 5, 4},
                {2, 3, 7},
                {2, 7, 6},
                {0, 2, 6},
                {0, 6, 4},
                {1, 3, 7},
                {1, 7, 5}},
               outward);
  }
  void add_tet(const Eigen::Vector3d &a, const Eigen::Vector3d &b,
               const Eigen::Vector3d &c, const Eigen::Vector3d &d,
               bool outward) {
    add_convex({a, b, c, d}, {{0, 1, 2}, {0, 1, 3}, {0, 2, 3}, {1, 2, 3}},
               outward);
  }
  void add_mesh(const SurfaceMesh &m) {
    const int base = static_cast<int>(pts.size());
    for (int v = 0; v < m.vertex_count(); ++v)
      pts.push_back(m.points_m.col(v));
    for (int t = 0; t < m.triangle_count(); ++t)
      tris.push_back({base + m.triangles(0, t), base + m.triangles(1, t),
                      base + m.triangles(2, t)});
  }
  SurfaceMesh mesh() const {
    SurfaceMesh s;
    const int V = static_cast<int>(pts.size()),
              T = static_cast<int>(tris.size());
    s.points_m.resize(3, V);
    for (int v = 0; v < V; ++v)
      s.points_m.col(v) = pts[v];
    s.triangles.resize(3, T);
    for (int t = 0; t < T; ++t)
      for (int k = 0; k < 3; ++k)
        s.triangles(k, t) = tris[t][k];
    s.vertex_source.assign(static_cast<std::size_t>(V), SurfaceVertexSource{});
    s.triangle_element = Eigen::VectorXi::Zero(T);
    s.triangle_origin.assign(static_cast<std::size_t>(T),
                             TriangleOrigin::DomainBoundary);
    return s;
  }
};

} // namespace

TEST_CASE("full-solid domain: exact volume and outward winding") {
  const Box box = make_box(3, 2, 2, 4.0, 3.0, 2.5);
  const auto mesh = mesh_of(box);
  const Eigen::VectorXd rho = Eigen::VectorXd::Ones(mesh->element_count());
  const SurfaceResult r = extract_density_surface(*mesh, rho, {});
  const SurfaceEvidence &ev = r.evidence;
  CHECK(ev.state == GeometryState::Valid);
  CHECK(ev.watertight);
  CHECK(ev.manifold);
  CHECK(ev.bodies == 1);
  CHECK(ev.cavities == 0);
  CHECK(ev.isosurface_triangles == 0);
  CHECK(ev.triangles == mesh->surface_count());
  CHECK(ev.signed_volume_m3 ==
        doctest::Approx(box.volume_mm3() * kMm3).epsilon(1e-12));
  const Eigen::Vector3d center(6e-3, 3e-3, 2.5e-3);
  for (int t = 0; t < r.mesh.triangle_count(); ++t) {
    const Eigen::Vector3d a = r.mesh.points_m.col(r.mesh.triangles(0, t));
    const Eigen::Vector3d b = r.mesh.points_m.col(r.mesh.triangles(1, t));
    const Eigen::Vector3d c = r.mesh.points_m.col(r.mesh.triangles(2, t));
    CHECK((b - a).cross(c - a).dot((a + b + c) / 3.0 - center) > 0);
  }

  const ClippedResult clip = clip_density_domain(*mesh, rho, {});
  CHECK(clip.volume.boundary_matches_surface);
  CHECK(static_cast<int>(clip.volume.tet_parent.size()) ==
        mesh->element_count());
  CHECK(clip.volume.volume_m3 ==
        doctest::Approx(box.volume_mm3() * kMm3).epsilon(1e-12));
}

TEST_CASE("planar cut: analytic volume, plane position and cap closure") {
  const Box box = make_box(4, 3, 3, 2.5, 2.0, 2.0); // 10 x 6 x 6 mm
  const auto mesh = mesh_of(box);
  SurfaceOptions o;
  o.threshold = 0.37;
  const SurfaceResult r = extract_nodal_surface(*mesh, ramp_x(box), o);
  CHECK(r.evidence.state == GeometryState::Valid);
  CHECK(r.evidence.watertight);
  CHECK(r.evidence.manifold);
  CHECK(r.evidence.boundary_triangles > 0);
  CHECK(r.evidence.isosurface_triangles > 0);
  CHECK(r.evidence.degenerate_triangles == 0);
  CHECK(r.evidence.signed_volume_m3 ==
        doctest::Approx((1.0 - 0.37) * box.volume_mm3() * kMm3).epsilon(1e-10));
  const double plane_x = 0.37 * 10e-3;
  for (int t = 0; t < r.mesh.triangle_count(); ++t)
    if (r.mesh.triangle_origin[t] == TriangleOrigin::Isosurface)
      for (int k = 0; k < 3; ++k)
        CHECK(r.mesh.points_m(0, r.mesh.triangles(k, t)) ==
              doctest::Approx(plane_x).epsilon(1e-12));

  // The conforming clipped volume has exactly this surface as its boundary and
  // is accepted by the shared mesh builder at the analytic volume.
  const ClippedResult clip = clip_nodal_domain(*mesh, ramp_x(box), o);
  CHECK(clip.volume.boundary_matches_surface);
  CHECK(clip.volume.face_components == 1);
  CHECK(clip.volume.nonconforming_faces == 0);
  const auto rebuilt =
      build_tet_mesh(clip.volume.points_mm, clip.volume.tets, 4);
  CHECK(rebuilt->total_volume_m3 ==
        doctest::Approx((1.0 - 0.37) * box.volume_mm3() * kMm3).epsilon(1e-10));
  CHECK(rebuilt->quality.reoriented_elements == 0);

  // Port/fixture re-resolution: the x = L face survives whole, x = 0 does not.
  std::vector<int> far_face, near_face;
  for (int n = 0; n < mesh->node_count(); ++n) {
    if (box.index(n, 0) == box.nx)
      far_face.push_back(n);
    if (box.index(n, 0) == 0)
      near_face.push_back(n);
  }
  // Not `far`/`near`: <windows.h> (windef.h) defines both as empty macros.
  const RetainedNodeSet far_kept = remap_to_clipped(*mesh, clip.volume, far_face);
  CHECK(far_kept.dropped.empty());
  CHECK(far_kept.nodes.size() == far_face.size());
  const RetainedNodeSet near_kept = remap_to_clipped(*mesh, clip.volume, near_face);
  CHECK(near_kept.nodes.empty());
  CHECK(near_kept.dropped.size() == near_face.size());
  // A y = 0 side patch is clipped: its surviving part keeps area (x >= plane).
  std::vector<int> side;
  for (int n = 0; n < mesh->node_count(); ++n)
    if (box.index(n, 1) == 0)
      side.push_back(n);
  const RetainedNodeSet side_new = remap_to_clipped(*mesh, clip.volume, side);
  double side_area = 0;
  for (int f : surface_faces_with_nodes(*rebuilt, side_new.nodes))
    side_area += rebuilt->surface_area_m2[f];
  CHECK(side_area == doctest::Approx((10.0 - 3.7) * 6.0 * 1e-6).epsilon(1e-10));
}

TEST_CASE(
    "threshold through nodes: welded vertices, no cracks, no zero triangles") {
  const Box box = make_box(4, 2, 2, 1.0, 1.0, 1.0);
  const auto mesh = mesh_of(box);
  SurfaceOptions o;
  o.threshold = 0.5; // exactly the nodal value on the grid plane i = 2
  const SurfaceResult r = extract_nodal_surface(*mesh, ramp_x(box), o);
  int plane_nodes = 0;
  for (int n = 0; n < mesh->node_count(); ++n)
    plane_nodes += box.index(n, 0) == 2;
  CHECK(r.stats.threshold_nodes == plane_nodes);
  CHECK(r.evidence.state == GeometryState::Valid);
  CHECK(r.evidence.watertight);
  CHECK(r.evidence.degenerate_triangles == 0);
  CHECK(r.evidence.min_triangle_area_m2 > 0);
  CHECK(distinct_positions(r.mesh) == r.mesh.vertex_count());
  CHECK(r.evidence.signed_volume_m3 ==
        doctest::Approx(0.5 * box.volume_mm3() * kMm3).epsilon(1e-12));
  for (const SurfaceVertexSource &s : r.mesh.vertex_source)
    CHECK(s.node_b < 0); // every crossing welded to its threshold node
  const ClippedResult clip = clip_nodal_domain(*mesh, ramp_x(box), o);
  CHECK(clip.volume.boundary_matches_surface);
}

TEST_CASE("threshold sheet of zero volume is empty, not a fake surface") {
  const Box box = make_box(4, 2, 2, 1.0, 1.0, 1.0);
  const auto mesh = mesh_of(box);
  Eigen::VectorXd d = Eigen::VectorXd::Zero(mesh->node_count());
  for (int n = 0; n < mesh->node_count(); ++n)
    if (box.index(n, 0) == 2)
      d[n] = 0.5;
  const SurfaceResult r = extract_nodal_surface(*mesh, d, {});
  CHECK(r.evidence.state == GeometryState::Empty);
  CHECK(r.mesh.triangle_count() == 0);
  CHECK(r.stats.cancelled_sheet_triangles > 0);
  CHECK(r.stats.collapsed_polygons > 0);
  TempDir dir;
  CHECK(error_code([&] {
          write_stl(dir.file("empty.stl"), r.mesh, "empty");
        }) == "empty_surface");
  CHECK_FALSE(fs::exists(dir.file("empty.stl")));
}

TEST_CASE("disconnected bodies and enclosed cavities are distinguished") {
  const Box box = make_box(4, 2, 2, 1.0, 1.0, 1.0);
  const auto mesh = mesh_of(box);
  Eigen::VectorXd ends = Eigen::VectorXd::Zero(mesh->node_count());
  for (int n = 0; n < mesh->node_count(); ++n)
    if (box.index(n, 0) == 0 || box.index(n, 0) == 4)
      ends[n] = 1.0;
  const SurfaceResult two = extract_nodal_surface(*mesh, ends, {});
  CHECK(two.evidence.state == GeometryState::Disconnected);
  CHECK(two.evidence.bodies == 2);
  CHECK(two.evidence.watertight);
  CHECK(two.evidence.manifold);
  CHECK(two.evidence.largest_body_volume_fraction == doctest::Approx(0.5));
  // Each end slab reaches half a cell: 0.5 * h * W * H, two of them.
  CHECK(two.evidence.signed_volume_m3 ==
        doctest::Approx(1.0 * 2 * 2 * kMm3).epsilon(1e-12));

  const Box cube = make_box(4, 4, 4, 1.0, 1.0, 1.0);
  const auto cmesh = mesh_of(cube);
  Eigen::VectorXd hollow = Eigen::VectorXd::Ones(cmesh->node_count());
  const int centre = cube.node(2, 2, 2);
  hollow[centre] = 0.0;
  const SurfaceResult cav = extract_nodal_surface(*cmesh, hollow, {});
  CHECK(cav.evidence.state == GeometryState::Valid);
  CHECK(cav.evidence.bodies == 1);
  CHECK(cav.evidence.cavities == 1);
  CHECK(cav.evidence.shells == 2);
  double star = 0; // void corner of each incident tet is that tet scaled by 1/2
  for (int e = 0; e < cmesh->element_count(); ++e)
    for (int a = 0; a < 4; ++a)
      if (cmesh->tets(a, e) == centre)
        star += cmesh->volume_m3[e];
  CHECK(cav.evidence.signed_volume_m3 ==
        doctest::Approx(cube.volume_mm3() * kMm3 - star / 8.0).epsilon(1e-12));
}

TEST_CASE("pinch at a threshold node is reported non-manifold") {
  const Box box = make_box(4, 2, 2, 1.0, 1.0, 1.0);
  const auto mesh = mesh_of(box);
  Eigen::VectorXd d = Eigen::VectorXd::Zero(mesh->node_count());
  d[box.node(1, 1, 1)] = 1.0;
  d[box.node(2, 1, 1)] = 0.5;
  d[box.node(3, 1, 1)] = 1.0;
  const SurfaceResult r = extract_nodal_surface(*mesh, d, {});
  CHECK(r.evidence.watertight);
  CHECK(r.evidence.nonmanifold_vertices == 1);
  CHECK_FALSE(r.evidence.manifold);
  CHECK(r.evidence.state == GeometryState::Unresolved);
  CHECK(r.evidence.shells == 2);
  CHECK(r.evidence.vertex_components == 1);
  bool flagged = false;
  for (const std::string &issue : r.evidence.issues)
    flagged = flagged || issue == "nonmanifold_vertices";
  CHECK(flagged);
}

TEST_CASE("a negative shell crossing the body surface is not a cavity") {
  // Outward cube [-2,2]^3 m and an inward tetrahedron reaching x = 3 m: the
  // centroid of its largest face lies inside the cube, but the shell crosses
  // the cube's x = 2 face and extends outside the body.
  ShellSoup soup;
  soup.add_box({-2, -2, -2}, {2, 2, 2}, true);
  soup.add_tet({0, 0, 0}, {0, 1, 0}, {0, 0, 1}, {3, 0, 0}, false);
  const SurfaceEvidence ev = validate_surface(soup.mesh());
  REQUIRE(ev.watertight);
  REQUIRE(ev.manifold);
  REQUIRE(ev.shells == 2);
  CHECK(ev.state == GeometryState::Unresolved);
  CHECK(ev.cavities == 0);
  CHECK(has_issue(ev, "intersecting_shells"));
  // Reported, not altered: the primitive volumes are summed as given.
  CHECK(ev.signed_volume_m3 == doctest::Approx(64.0 - 0.5).epsilon(1e-12));
}

TEST_CASE("void contact and nesting are classified from the shells") {
  using V3 = Eigen::Vector3d;
  const auto check_one_body = [](const SurfaceEvidence &ev) {
    CHECK(ev.watertight);
    CHECK(ev.manifold);
    CHECK(ev.bodies >= 1);
  };
  SUBCASE("a void strictly inside the body is an enclosed cavity") {
    ShellSoup soup;
    soup.add_box({-2, -2, -2}, {2, 2, 2}, true);
    soup.add_tet({0, 0, 0}, {0, 1, 0}, {0, 0, 1}, {1.999, 0, 0}, false);
    const SurfaceEvidence ev = validate_surface(soup.mesh());
    check_one_body(ev);
    CHECK(ev.state == GeometryState::Valid);
    CHECK(ev.issues.empty());
    CHECK(ev.cavities == 1);
    CHECK(ev.intersecting_shells == 0);
    CHECK(ev.signed_volume_m3 ==
          doctest::Approx(64.0 - 1.999 / 6.0).epsilon(1e-12));
  }
  SUBCASE("a void touching the body surface at one vertex is contact") {
    ShellSoup soup;
    soup.add_box({-2, -2, -2}, {2, 2, 2}, true);
    soup.add_tet({0, 0, 0}, {0, 1, 0}, {0, 0, 1}, {2, 0, 0}, false);
    const SurfaceEvidence ev = validate_surface(soup.mesh());
    check_one_body(ev);
    CHECK(ev.state == GeometryState::Unresolved);
    CHECK(ev.intersecting_shells == 2);
    CHECK(ev.cavities == 0);
    CHECK(has_issue(ev, "intersecting_shells"));
    CHECK(ev.signed_volume_m3 ==
          doctest::Approx(64.0 - 2.0 / 6.0).epsilon(1e-12));
  }
  SUBCASE("a void with a face lying in the body surface is contact") {
    ShellSoup soup;
    soup.add_box({-2, -2, -2}, {2, 2, 2}, true);
    soup.add_tet({2, 0, 0}, {2, 1, 0}, {2, 0, 1}, {1, 0, 0}, false);
    const SurfaceEvidence ev = validate_surface(soup.mesh());
    CHECK(ev.state == GeometryState::Unresolved);
    CHECK(ev.intersecting_shells == 2);
    CHECK(ev.cavities == 0);
  }
  SUBCASE("concave body: corners inside, an edge through the notch") {
    // L-shaped body: the 2 x 2 x 1 mm block without its x,y in [1,2] cell.
    const Box box = make_box(2, 2, 1, 1.0, 1.0, 1.0);
    const auto mesh = mesh_of(box);
    Eigen::VectorXd rho(mesh->element_count());
    for (int e = 0; e < mesh->element_count(); ++e)
      rho[e] = mesh->centroid_m(0, e) > 1e-3 && mesh->centroid_m(1, e) > 1e-3
                   ? 0.0
                   : 1.0;
    const RetainedDomain l = retained_domain(*mesh, rho, 0.5);
    REQUIRE(l.boundary_evidence.state == GeometryState::Valid);
    const double mm = 1e-3;
    const auto with_void = [&](const V3 &a, const V3 &b, const V3 &c,
                               const V3 &d) {
      ShellSoup soup;
      soup.add_mesh(l.boundary);
      soup.add_tet(a * mm, b * mm, c * mm, d * mm, false);
      return validate_surface(soup.mesh());
    };
    const auto tet_volume = [&](const V3 &a, const V3 &b, const V3 &c,
                                const V3 &d) {
      return std::abs((b - a).dot((c - a).cross(d - a))) / 6.0 * kMm3;
    };
    // All four corners lie in the L; the edge (1.9,0.5)-(0.5,1.9) crosses
    // the notch at (1.2,1.2).
    const V3 a(1.9, 0.5, 0.5), b(0.5, 1.9, 0.5), c(0.5, 0.5, 0.3),
        d(0.5, 0.5, 0.7);
    const SurfaceEvidence crossing = with_void(a, b, c, d);
    CHECK(crossing.state == GeometryState::Unresolved);
    CHECK(crossing.intersecting_shells == 2);
    CHECK(crossing.cavities == 0);
    CHECK(crossing.signed_volume_m3 ==
          doctest::Approx(3.0 * kMm3 - tet_volume(a, b, c, d)).epsilon(1e-9));
    // The same void pulled back into the corner cell is enclosed.
    const V3 a2(0.9, 0.2, 0.5), b2(0.2, 0.9, 0.5), c2(0.3, 0.3, 0.3),
        d2(0.3, 0.3, 0.7);
    const SurfaceEvidence inside = with_void(a2, b2, c2, d2);
    CHECK(inside.state == GeometryState::Valid);
    CHECK(inside.cavities == 1);
    CHECK(inside.intersecting_shells == 0);
    CHECK(
        inside.signed_volume_m3 ==
        doctest::Approx(3.0 * kMm3 - tet_volume(a2, b2, c2, d2)).epsilon(1e-9));
  }
  SUBCASE("an island inside a cavity is a second body, not a defect") {
    ShellSoup soup;
    soup.add_box({-2, -2, -2}, {2, 2, 2}, true);
    soup.add_box({-1, -1, -1}, {1, 1, 1}, false);
    soup.add_box({-0.5, -0.5, -0.5}, {0.5, 0.5, 0.5}, true);
    const SurfaceEvidence ev = validate_surface(soup.mesh());
    CHECK(ev.state == GeometryState::Disconnected);
    CHECK(ev.issues.empty());
    CHECK(ev.bodies == 2);
    CHECK(ev.cavities == 1);
    CHECK(ev.signed_volume_m3 ==
          doctest::Approx(64.0 - 8.0 + 1.0).epsilon(1e-12));
  }
  SUBCASE("a body inside solid material is inconsistent nesting") {
    ShellSoup soup;
    soup.add_box({-2, -2, -2}, {2, 2, 2}, true);
    soup.add_box({-1, -1, -1}, {1, 1, 1}, true);
    const SurfaceEvidence ev = validate_surface(soup.mesh());
    CHECK(ev.state == GeometryState::Unresolved);
    CHECK(ev.inconsistent_nesting == 1);
    CHECK(has_issue(ev, "inconsistent_nesting"));
    CHECK(ev.intersecting_shells == 0);
  }
  SUBCASE("a cavity inside a cavity encloses no material") {
    ShellSoup soup;
    soup.add_box({-2, -2, -2}, {2, 2, 2}, true);
    soup.add_box({-1, -1, -1}, {1, 1, 1}, false);
    soup.add_box({-0.5, -0.5, -0.5}, {0.5, 0.5, 0.5}, false);
    const SurfaceEvidence ev = validate_surface(soup.mesh());
    CHECK(ev.state == GeometryState::Unresolved);
    CHECK(ev.cavities == 1);
    CHECK(ev.unenclosed_cavities == 1);
  }
  SUBCASE("a negative shell beside the body encloses nothing") {
    ShellSoup soup;
    soup.add_box({-2, -2, -2}, {2, 2, 2}, true);
    soup.add_box({3, 3, 3}, {4, 4, 4}, false);
    const SurfaceEvidence ev = validate_surface(soup.mesh());
    CHECK(ev.state == GeometryState::Unresolved);
    CHECK(ev.unenclosed_cavities == 1);
    CHECK(ev.cavities == 0);
  }
}

TEST_CASE("a thousand enclosed voids are each classified as cavities") {
  ShellSoup soup;
  soup.add_box({0, 0, 0}, {10, 10, 10}, true);
  const double h = 0.15;
  for (int i = 0; i < 10; ++i)
    for (int j = 0; j < 10; ++j)
      for (int k = 0; k < 10; ++k) {
        const Eigen::Vector3d c(i + 0.5, j + 0.5, k + 0.5);
        soup.add_tet(c + Eigen::Vector3d(-h, -h, -h),
                     c + Eigen::Vector3d(h, -h, -h),
                     c + Eigen::Vector3d(-h, h, -h),
                     c + Eigen::Vector3d(-h, -h, h), false);
      }
  const SurfaceEvidence ev = validate_surface(soup.mesh());
  CHECK(ev.state == GeometryState::Valid);
  CHECK(ev.issues.empty());
  CHECK(ev.shells == 1001);
  CHECK(ev.cavities == 1000);
  const double void_volume = std::pow(2 * h, 3) / 6.0;
  CHECK(ev.signed_volume_m3 ==
        doctest::Approx(1000.0 - 1000 * void_volume).epsilon(1e-12));
}

TEST_CASE("retained whole-element domain and discretization difference") {
  const Box box = make_box(4, 2, 2, 1.0, 1.0, 1.0);
  const auto mesh = mesh_of(box);
  Eigen::VectorXd rho(mesh->element_count());
  for (int e = 0; e < mesh->element_count(); ++e)
    rho[e] = mesh->centroid_m(0, e) < 2e-3 ? 1.0 : 0.1;
  const RetainedDomain r = retained_domain(*mesh, rho, 0.5);
  CHECK(r.elements.size() == 48);
  CHECK(r.volume_m3 == doctest::Approx(8.0 * kMm3).epsilon(1e-12));
  CHECK(r.face_components == 1);
  CHECK(r.boundary_evidence.state == GeometryState::Valid);
  CHECK(r.boundary_evidence.facet_triangles > 0);
  CHECK(r.boundary_evidence.signed_volume_m3 ==
        doctest::Approx(r.volume_m3).epsilon(1e-12));
  const auto rebuilt = build_tet_mesh(r.points_mm, r.tets, 4);
  CHECK(rebuilt->total_volume_m3 ==
        doctest::Approx(r.volume_m3).epsilon(1e-12));

  std::vector<int> left, right;
  for (int n = 0; n < mesh->node_count(); ++n) {
    if (box.index(n, 0) == 0)
      left.push_back(n);
    if (box.index(n, 0) == 4)
      right.push_back(n);
  }
  CHECK(remap_to_retained(r, left).nodes.size() == left.size());
  CHECK(remap_to_retained(r, right).dropped.size() == right.size());

  const SurfaceResult s = extract_density_surface(*mesh, rho, {});
  CHECK(s.evidence.state == GeometryState::Valid);
  const DiscretizationComparison c = compare_discretizations(r, s);
  // Nodes with x <= 1 mm average to 1 and x >= 3 mm to 0.1, so the
  // interpolated boundary lies within one cell layer (4 mm^3) of x = 2 mm.
  CHECK(std::abs(c.difference_m3) <= 4.0 * kMm3);

  // Ties: rho exactly at the threshold is retained.
  const RetainedDomain all = retained_domain(
      *mesh, Eigen::VectorXd::Constant(mesh->element_count(), 0.5), 0.5);
  CHECK(static_cast<int>(all.elements.size()) == mesh->element_count());
}

TEST_CASE("scalar fields interpolate exactly onto the surface") {
  const Box box = make_box(4, 3, 3, 2.5, 2.0, 2.0);
  const auto mesh = mesh_of(box);
  SurfaceOptions o;
  o.threshold = 0.37;
  const SurfaceResult r = extract_nodal_surface(*mesh, ramp_x(box), o);
  const int N = mesh->node_count();
  Eigen::VectorXd f(N), xyz(3 * N);
  for (int n = 0; n < N; ++n) {
    const Eigen::Vector3d p = mesh->nodes_m.col(n);
    f[n] = p.x() + 2 * p.y() + 3 * p.z();
    xyz.segment<3>(3 * n) = p;
  }
  const Eigen::VectorXd fs = interpolate_nodal_field(r.mesh, f, 1);
  const Eigen::VectorXd ps = interpolate_nodal_field(r.mesh, xyz, 3);
  for (int v = 0; v < r.mesh.vertex_count(); ++v) {
    const Eigen::Vector3d p = r.mesh.points_m.col(v);
    CHECK(fs[v] ==
          doctest::Approx(p.x() + 2 * p.y() + 3 * p.z()).epsilon(1e-12));
    CHECK((ps.segment<3>(3 * v) - p).norm() <= 1e-15);
  }
  Eigen::VectorXd ids(mesh->element_count());
  for (int e = 0; e < mesh->element_count(); ++e)
    ids[e] = e;
  const Eigen::VectorXd per_tri = element_field_on_triangles(r.mesh, ids, 1);
  for (int t = 0; t < r.mesh.triangle_count(); ++t)
    CHECK(per_tri[t] == r.mesh.triangle_element[t]);
}

TEST_CASE("STL, AICM and VTU read back with physical counts and millimetres") {
  const Box box = make_box(4, 3, 3, 2.5, 2.0, 2.0); // 10 x 6 x 6 mm
  const auto mesh = mesh_of(box);
  SurfaceOptions o;
  o.threshold = 0.37;
  const SurfaceResult r = extract_nodal_surface(*mesh, ramp_x(box), o);
  TempDir dir;

  // STL: binary, millimetres, the same enclosed volume.
  const std::string stl = dir.file("part.stl");
  const ArtifactFile a = write_stl(stl, r.mesh, "planar cut");
  const std::vector<unsigned char> bytes = read_bytes(stl);
  REQUIRE(bytes.size() ==
          84 + 50 * static_cast<std::size_t>(r.mesh.triangle_count()));
  CHECK(a.bytes == bytes.size());
  CHECK(a.sha256.size() == 64);
  std::uint32_t count = 0;
  for (int i = 0; i < 4; ++i)
    count |= static_cast<std::uint32_t>(bytes[80 + i]) << (8 * i);
  CHECK(static_cast<int>(count) == r.mesh.triangle_count());
  double vol_mm3 = 0, max_x = -1;
  for (std::uint32_t t = 0; t < count; ++t) {
    const std::size_t base = 84 + 50 * static_cast<std::size_t>(t) + 12;
    Eigen::Vector3d p[3];
    for (int c = 0; c < 3; ++c)
      for (int k = 0; k < 3; ++k)
        p[c][k] = f32_at(bytes, base + 12 * c + 4 * k);
    vol_mm3 += p[0].dot(p[1].cross(p[2])) / 6.0;
    for (const auto &q : p)
      max_x = std::max(max_x, q.x());
  }
  CHECK(vol_mm3 ==
        doctest::Approx((1.0 - 0.37) * box.volume_mm3()).epsilon(1e-5));
  CHECK(max_x == doctest::Approx(10.0).epsilon(1e-6));
  // Never overwritten: a second write is refused and the file is untouched.
  CHECK(error_code([&] { write_stl(stl, r.mesh, "again"); }) ==
        "artifact_exists");
  CHECK(read_bytes(stl) == bytes);

  // AICM through the host loader.
  SurfaceScalar density{"density", "1",
                        interpolate_nodal_field(r.mesh, ramp_x(box), 1)};
  const std::string aicm = dir.file("part.aicm");
  write_surface_aicm(aicm, r.mesh, {density}, "Topology");
  render::DocMesh doc;
  std::string err;
  REQUIRE(render::loadAicmFile(aicm, doc, err));
  REQUIRE(doc.bodies.size() == 1);
  CHECK(static_cast<int>(doc.bodies[0].n_tris) == r.mesh.triangle_count());
  CHECK(static_cast<int>(doc.bodies[0].n_verts) == r.mesh.vertex_count());
  float aicm_max_x = -1;
  for (std::size_t i = 0; i < doc.bodies[0].pos.size(); i += 3)
    aicm_max_x = std::max(aicm_max_x, doc.bodies[0].pos[i]);
  CHECK(aicm_max_x == doctest::Approx(10.0f).epsilon(1e-6));
  REQUIRE(doc.scalars.size() == 1);
  CHECK(doc.scalars[0].name == "density");
  CHECK(static_cast<int>(doc.scalars[0].values.size()) ==
        r.mesh.vertex_count());
  for (float v : doc.scalars[0].values)
    CHECK(v >= 0.37f - 1e-6f);
  CHECK(error_code([&] { write_surface_aicm(aicm, r.mesh, {density}, "x"); }) ==
        "artifact_exists");

  // VTU: whole domain, cell density, retained flag and real fields.
  Eigen::VectorXd rho(mesh->element_count());
  for (int e = 0; e < mesh->element_count(); ++e)
    rho[e] = mesh->centroid_m(0, e) < 5e-3 ? 0.9 : 0.2;
  Field temp{"temperature", "K", FieldLocation::Node, 1,
             Eigen::VectorXd(mesh->node_count())};
  for (int n = 0; n < mesh->node_count(); ++n)
    temp.values[n] = 300.0 + 1000.0 * mesh->nodes_m(0, n);
  Field sed{"strain_energy_density", "J/m3", FieldLocation::Element, 1,
            Eigen::VectorXd::LinSpaced(mesh->element_count(), 1.0, 2.0)};
  const std::string vtu = dir.file("result.vtu");
  const ArtifactFile va = write_density_vtu(vtu, *mesh, rho, 0.5, {temp, sed});
  const std::vector<unsigned char> raw = read_bytes(vtu);
  const std::string xml(raw.begin(), raw.end());
  CHECK(xml_int_attr(xml, "NumberOfPoints") == mesh->node_count());
  CHECK(xml_int_attr(xml, "NumberOfCells") == mesh->element_count());
  CHECK(va.cells == mesh->element_count());
  const std::vector<double> pts = vtu_array(xml, "");
  REQUIRE(pts.size() == 3 * static_cast<std::size_t>(mesh->node_count()));
  double vtu_max_x = 0;
  for (std::size_t i = 0; i < pts.size(); i += 3)
    vtu_max_x = std::max(vtu_max_x, pts[i]);
  CHECK(vtu_max_x == doctest::Approx(10.0).epsilon(1e-12));
  const std::vector<double> dens = vtu_array(xml, "density");
  const std::vector<double> kept = vtu_array(xml, "retained");
  const std::vector<double> temps = vtu_array(xml, "temperature");
  const std::vector<double> seds = vtu_array(xml, "strain_energy_density");
  REQUIRE(dens.size() == static_cast<std::size_t>(mesh->element_count()));
  REQUIRE(kept.size() == dens.size());
  REQUIRE(seds.size() == dens.size());
  REQUIRE(temps.size() == static_cast<std::size_t>(mesh->node_count()));
  for (int e = 0; e < mesh->element_count(); ++e) {
    CHECK(dens[e] == rho[e]);
    CHECK(kept[e] == (rho[e] >= 0.5 ? 1.0 : 0.0));
    CHECK(seds[e] == sed.values[e]);
  }
  const std::vector<double> ids = vtu_array(xml, "node_id");
  for (std::size_t i = 0; i < temps.size(); ++i)
    CHECK(temps[i] == temp.values[static_cast<Eigen::Index>(ids[i])]);
  Field reserved{"density", "1", FieldLocation::Element, 1, rho};
  CHECK(error_code([&] {
          write_density_vtu(dir.file("bad.vtu"), *mesh, rho, 0.5, {reserved});
        }) == "invalid_field");
  CHECK_FALSE(fs::exists(dir.file("bad.vtu")));
}

TEST_CASE("AICM refuses triangles that collapse in float32 millimetres") {
  // A valid 0.01 mm cube far from the origin: float32 spacing at 1e6 mm is
  // 0.0625 mm, so all eight corners serialize to the same point.
  Box box = make_box(1, 1, 1, 0.01, 0.01, 0.01);
  for (double &x : box.points)
    x += 1.0e6;
  const auto mesh = mesh_of(box);
  const SurfaceResult r = extract_density_surface(
      *mesh, Eigen::VectorXd::Ones(mesh->element_count()), {});
  REQUIRE(r.evidence.state == GeometryState::Valid);
  REQUIRE(r.mesh.triangle_count() == 12);
  std::set<std::tuple<float, float, float>> f32;
  for (int v = 0; v < r.mesh.vertex_count(); ++v)
    f32.emplace(static_cast<float>(r.mesh.points_m(0, v) * 1e3),
                static_cast<float>(r.mesh.points_m(1, v) * 1e3),
                static_cast<float>(r.mesh.points_m(2, v) * 1e3));
  REQUIRE(f32.size() == 1);

  TempDir dir;
  const std::string aicm = dir.file("far.aicm");
  CHECK(error_code([&] { write_surface_aicm(aicm, r.mesh, {}, "Topology"); }) ==
        "aicm_precision_loss");
  CHECK_FALSE(fs::exists(aicm));
  const std::string stl = dir.file("far.stl");
  CHECK(error_code([&] { write_stl(stl, r.mesh, "far"); }) ==
        "stl_precision_loss");
  CHECK_FALSE(fs::exists(stl));
}

TEST_CASE("AICM keeps far-from-origin triangles that survive float32") {
  // 1 mm cells at 1e6 mm: float32 resolves 0.0625 mm there, so the corners
  // stay distinct and are written without rebasing.
  Box box = make_box(1, 1, 1, 1.0, 1.0, 1.0);
  for (double &x : box.points)
    x += 1.0e6;
  const auto mesh = mesh_of(box);
  const SurfaceResult r = extract_density_surface(
      *mesh, Eigen::VectorXd::Ones(mesh->element_count()), {});
  REQUIRE(r.evidence.state == GeometryState::Valid);
  TempDir dir;
  const std::string aicm = dir.file("far.aicm");
  write_surface_aicm(aicm, r.mesh, {}, "Topology");
  render::DocMesh doc;
  std::string err;
  REQUIRE(render::loadAicmFile(aicm, doc, err));
  REQUIRE(doc.bodies.size() == 1);
  CHECK(static_cast<int>(doc.bodies[0].n_tris) == 12);
  float lo = std::numeric_limits<float>::infinity(), hi = -lo;
  for (float x : doc.bodies[0].pos) {
    lo = std::min(lo, x);
    hi = std::max(hi, x);
  }
  CHECK(lo == 1.0e6f);
  CHECK(hi == 1.0e6f + 1.0f);
}

TEST_CASE("print surface triangle intersections exclude only shared contact") {
  SurfaceMesh s;
  s.points_m.resize(3, 6);
  s.points_m.col(0) << 0, 0, 0;
  s.points_m.col(1) << 2, 0, 0;
  s.points_m.col(2) << 0, 2, 0;
  s.points_m.col(3) << 0.5, 0.5, -1;
  s.points_m.col(4) << 0.5, 0.5, 1;
  s.points_m.col(5) << 1, 1, 0;
  s.triangles.resize(3, 2);
  s.triangles.col(0) << 0, 1, 2;
  s.triangles.col(1) << 3, 4, 5;
  s.vertex_source.resize(6);
  s.triangle_element = Eigen::VectorXi::Zero(2);
  s.triangle_origin.assign(2, TriangleOrigin::Isosurface);
  CHECK(surface_self_intersects(s));
  s.points_m.col(3).x() = 3;
  s.points_m.col(4).x() = 3;
  s.points_m.col(5).x() = 4;
  CHECK_FALSE(surface_self_intersects(s));
  // Sharing a vertex does not exempt a fold crossing the first triangle.
  s.triangles.col(1) << 0, 3, 4;
  s.points_m.col(3) << 1, 1, -1;
  s.points_m.col(4) << 1, 1, 1;
  CHECK(surface_self_intersects(s));
  s.points_m.col(3) << -1, 0, 1;
  s.points_m.col(4) << 0, -1, 1;
  CHECK_FALSE(surface_self_intersects(s));
  // Coplanar contact at the shared vertex is fine; overlap is not.
  s.points_m.col(3) << -1, 0, 0;
  s.points_m.col(4) << 0, -1, 0;
  CHECK_FALSE(surface_self_intersects(s));
  s.points_m.col(3) << 1, 0, 0;
  s.points_m.col(4) << 0, 1, 0;
  CHECK(surface_self_intersects(s));
  s.triangles.col(1) << 1, 0, 3;
  s.points_m.col(3) << 0, -1, 0;
  CHECK_FALSE(surface_self_intersects(s));
  s.points_m.col(3) << 0.5, 0.5, 0;
  CHECK(surface_self_intersects(s));
}

TEST_CASE("print surface pins every cap vertex and validates STL precision") {
  const Box box = make_box(8, 4, 4, 1, 1, 1);
  const auto mesh = mesh_of(box);
  Eigen::VectorXd nodal(mesh->node_count());
  for (int n = 0; n < mesh->node_count(); ++n) {
    const Eigen::Vector3d p = mesh->nodes_m.col(n) * 1e3;
    nodal[n] = std::clamp(0.5 + (p.y() - 1.6 - 0.2 * std::sin(p.x() * 2)) / 4,
                          0.0, 1.0);
  }
  const auto raw = extract_nodal_surface(*mesh, nodal, {});
  REQUIRE(raw.evidence.state == GeometryState::Valid);
  PrintSurfaceEvidence evidence;
  const auto smoothed = smooth_print_surface(raw.mesh, {}, evidence);
  CHECK(evidence.state == "valid");
  CHECK(evidence.iterations_requested == 12);
  CHECK(evidence.iterations_used > 0);
  CHECK(evidence.pinned_vertices > 0);
  CHECK(evidence.smoothed_vertices > 0);
  CHECK(evidence.max_displacement_mm > 0);
  CHECK(evidence.self_intersections_checked);
  CHECK(validate_surface(smoothed).state == GeometryState::Valid);
  CHECK_FALSE(surface_self_intersects(smoothed));
  CHECK(smoothed.triangles == raw.mesh.triangles);
  for (int t = 0; t < raw.mesh.triangle_count(); ++t)
    if (raw.mesh.triangle_origin[t] == TriangleOrigin::DomainBoundary)
      for (int k = 0; k < 3; ++k) {
        const int v = raw.mesh.triangles(k, t);
        CHECK(std::memcmp(raw.mesh.points_m.col(v).data(),
                          smoothed.points_m.col(v).data(), 3 * sizeof(double)) == 0);
      }
  TempDir dir;
  write_stl(dir.file("print.stl"), smoothed, "Print surface");
  SurfaceMesh invalid = raw.mesh;
  invalid.triangles.conservativeResize(3, invalid.triangle_count() - 1);
  invalid.triangle_element.conservativeResize(invalid.triangle_count());
  invalid.triangle_origin.resize(invalid.triangle_count());
  CHECK(error_code([&] { smooth_print_surface(invalid, {}, evidence); }) ==
        "print_surface_invalid");
  CHECK(evidence.state == "refused");
}

TEST_CASE("surface overhang is area weighted and excludes the build plate") {
  const Box box = make_box(2, 2, 2, 1, 1, 1);
  const auto mesh = mesh_of(box);
  const auto s = domain_boundary_surface(*mesh);
  const auto up = measure_surface_overhang(s, Eigen::Vector3d::UnitZ(), 45, 0,
                                          1e-6 * mesh->bbox_max_m.norm());
  CHECK(up.unsupported_area_m2 == 0);
  CHECK(up.vertex_fraction.isZero());
  const auto elevated = measure_surface_overhang(s, Eigen::Vector3d::UnitZ(),
                                                45, -0.001, 1e-9);
  CHECK(elevated.unsupported_area_m2 == doctest::Approx(4e-6));
  CHECK(elevated.total_area_m2 == doctest::Approx(24e-6));
  CHECK(elevated.vertex_fraction.minCoeff() >= 0);
  CHECK(elevated.vertex_fraction.maxCoeff() <= 1);
  TempDir dir;
  const auto path = dir.file("overhang.aicm");
  write_surface_aicm(path, s, {{"overhang", "-", elevated.vertex_fraction}}, "part");
  render::DocMesh doc;
  std::string error;
  REQUIRE(render::loadAicmFile(path, doc, error));
  REQUIRE(doc.scalars.size() == 1);
  CHECK(doc.scalars[0].name == "overhang");
  for (std::size_t v = 0; v < doc.scalars[0].values.size(); ++v)
    CHECK(doc.scalars[0].values[v] ==
          doctest::Approx(elevated.vertex_fraction[v]).epsilon(1e-6));
}

TEST_CASE("print intersection keeps exact shared-plane incidence") {
  SurfaceMesh s;
  s.points_m.resize(3, 5);
  // Near-planar triangles from a marching-tet box reconstruction, in mm.
  // The triangles meet only at vertex 0. Recomputing its signed plane
  // distance introduced a roundoff sign and a spurious crossing interval.
  s.points_m.col(0) << 27.749221801757812, 5.63823938369751, 1.5358493328094482;
  s.points_m.col(1) << 27.36842155456543, 5.923381328582764, 0;
  s.points_m.col(2) << 27.36842155456543, 5.684165000915527, 1.5789045095443726;
  s.points_m.col(3) << 29.473684310913086, 5.635838985443116, 0;
  s.points_m.col(4) << 27.46274757385254, 5.910389423370361, 0;
  s.points_m *= 1e-3;
  s.vertex_source.resize(5);
  s.triangle_element = Eigen::VectorXi::Zero(2);
  s.triangle_origin.assign(2, TriangleOrigin::Isosurface);
  s.triangles.resize(3, 2);
  s.triangles.col(0) << 0, 1, 2;
  s.triangles.col(1) << 3, 4, 0;
  CHECK_FALSE(surface_self_intersects(s));
  for (int rotation = 0; rotation < 3; ++rotation) {
    for (int t = 0; t < 2; ++t) {
      const Eigen::Vector3i ids = s.triangles.col(t);
      s.triangles.col(t) << ids[1], ids[2], ids[0];
    }
    CHECK_FALSE(surface_self_intersects(s));
  }
}

namespace {
SurfaceMesh unchecked_print_iteration(const SurfaceMesh &raw) {
  SurfaceMesh candidate = raw;
  std::vector<std::set<int>> neighbors(raw.vertex_count());
  std::vector<bool> pinned(raw.vertex_count(), false);
  for (int t = 0; t < raw.triangle_count(); ++t)
    for (int k = 0; k < 3; ++k) {
      const int v = raw.triangles(k, t);
      pinned[v] = pinned[v] || raw.triangle_origin[t] != TriangleOrigin::Isosurface;
      neighbors[v].insert(raw.triangles((k + 1) % 3, t));
      neighbors[v].insert(raw.triangles((k + 2) % 3, t));
    }
  for (double factor : {0.5, -0.53}) {
    Eigen::Matrix3Xd next = candidate.points_m;
    for (int v = 0; v < raw.vertex_count(); ++v) {
      if (pinned[v] || neighbors[v].empty())
        continue;
      Eigen::Vector3d mean = Eigen::Vector3d::Zero();
      for (int n : neighbors[v])
        mean += candidate.points_m.col(n);
      mean /= static_cast<double>(neighbors[v].size());
      next.col(v) += factor * (mean - candidate.points_m.col(v));
    }
    candidate.points_m.swap(next);
  }
  return candidate;
}
} // namespace

TEST_CASE("print surface locally rolls back microtriangle folds and their one-ring") {
  const Box box = make_box(4, 4, 4, 1, 1, 1);
  const auto mesh = mesh_of(box);
  Eigen::VectorXd nodal(mesh->node_count());
  for (int n = 0; n < mesh->node_count(); ++n) {
    const Eigen::Vector3d p = mesh->nodes_m.col(n) * 1e3;
    nodal[n] = std::clamp(0.5 + (p.y() - 2 + 0.1 * p.z() - 0.001) / 8,
                          0.0, 1.0);
  }
  const auto raw = extract_nodal_surface(*mesh, nodal, {});
  REQUIRE(raw.evidence.state == GeometryState::Valid);
  CHECK(raw.evidence.min_edge_m < 1e-5);
  CHECK_FALSE(surface_self_intersects(raw.mesh));
  const auto folded = unchecked_print_iteration(raw.mesh);
  std::vector<int> crossing;
  REQUIRE(surface_self_intersects(folded, &crossing));
  REQUIRE_FALSE(crossing.empty());
  CHECK(std::is_sorted(crossing.begin(), crossing.end()));
  CHECK(std::adjacent_find(crossing.begin(), crossing.end()) == crossing.end());
  std::vector<bool> affected(raw.mesh.vertex_count(), false);
  std::vector<bool> boundary(raw.mesh.vertex_count(), false);
  for (int t : crossing)
    for (int k = 0; k < 3; ++k)
      affected[raw.mesh.triangles(k, t)] = true;
  std::vector<bool> one_ring = affected;
  for (int t = 0; t < raw.mesh.triangle_count(); ++t) {
    bool touches = false;
    for (int k = 0; k < 3; ++k) {
      const int v = raw.mesh.triangles(k, t);
      touches = touches || affected[v];
      if (raw.mesh.triangle_origin[t] == TriangleOrigin::DomainBoundary)
        boundary[v] = true;
    }
    if (touches)
      for (int k = 0; k < 3; ++k)
        one_ring[raw.mesh.triangles(k, t)] = true;
  }
  PrintSurfaceOptions options;
  options.iterations = 1;
  PrintSurfaceEvidence evidence;
  const auto result = smooth_print_surface(raw.mesh, options, evidence);
  CHECK(evidence.state == "valid");
  CHECK(evidence.iterations_used == 1);
  CHECK(evidence.rollback_vertices > 0);
  CHECK(evidence.rollback_rounds > 0);
  CHECK(evidence.rollback_rounds <= 8);
  CHECK(evidence.pinned_vertices == std::count(boundary.begin(), boundary.end(), true));
  CHECK(evidence.smoothed_vertices > 0);
  CHECK(evidence.pinned_vertices + evidence.rollback_vertices +
            evidence.smoothed_vertices == raw.mesh.vertex_count());
  CHECK(evidence.max_displacement_mm > 0);
  CHECK(to_json(evidence)["rollback_vertices"] == evidence.rollback_vertices);
  CHECK(to_json(evidence)["rollback_rounds"] == evidence.rollback_rounds);
  CHECK(validate_surface(result).state == GeometryState::Valid);
  CHECK_FALSE(surface_self_intersects(result));
  for (int v = 0; v < raw.mesh.vertex_count(); ++v)
    if (one_ring[v] || boundary[v])
      CHECK(std::memcmp(result.points_m.col(v).data(),
                        raw.mesh.points_m.col(v).data(), 3 * sizeof(double)) == 0);
  TempDir dir;
  write_stl(dir.file("rollback.stl"), result, "Locally rolled-back print surface");
}

