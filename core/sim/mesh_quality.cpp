#include "sim/mesher.h"

#include <functional>
#include <unordered_map>
#include <algorithm>
#include <cmath>
#include <cstdio>
#include <limits>
#include <string>
#include <vector>

namespace chudware::sim {
namespace {

double tet_volume6(const std::vector<double> &p, int a, int b, int c, int d) {
  const double *A = &p[static_cast<size_t>(a) * 3];
  const double *B = &p[static_cast<size_t>(b) * 3];
  const double *C = &p[static_cast<size_t>(c) * 3];
  const double *D = &p[static_cast<size_t>(d) * 3];
  const double ux = B[0] - A[0], uy = B[1] - A[1], uz = B[2] - A[2];
  const double vx = C[0] - A[0], vy = C[1] - A[1], vz = C[2] - A[2];
  const double wx = D[0] - A[0], wy = D[1] - A[1], wz = D[2] - A[2];
  return ux * (vy * wz - vz * wy) - uy * (vx * wz - vz * wx) +
         uz * (vx * wy - vy * wx);
}

double edge_len(const std::vector<double> &p, int a, int b) {
  const double *A = &p[static_cast<size_t>(a) * 3];
  const double *B = &p[static_cast<size_t>(b) * 3];
  const double dx = A[0] - B[0], dy = A[1] - B[1], dz = A[2] - B[2];
  return std::sqrt(dx * dx + dy * dy + dz * dz);
}

double det3(const double j[3][3]) {
  return j[0][0] * (j[1][1] * j[2][2] - j[1][2] * j[2][1]) -
         j[0][1] * (j[1][0] * j[2][2] - j[1][2] * j[2][0]) +
         j[0][2] * (j[1][0] * j[2][1] - j[1][1] * j[2][0]);
}

bool tet10_jacobians_valid(const MeshOut &out, const int *element,
                           double det_tolerance) {
  constexpr double a = 0.5854101966249685;
  constexpr double b = 0.1381966011250105;
  constexpr double kPoints[4][3] = {{b, b, b}, {a, b, b}, {b, a, b}, {b, b, a}};
  constexpr double kGradients[4][3] = {
      {-1.0, -1.0, -1.0}, {1.0, 0.0, 0.0}, {0.0, 1.0, 0.0}, {0.0, 0.0, 1.0}};
  constexpr int kEdges[6][2] = {{0, 1}, {1, 2}, {2, 0}, {3, 0}, {3, 2}, {3, 1}};

  double sign = 0.0;
  for (const auto &point : kPoints) {
    const double barycentric[4] = {1.0 - point[0] - point[1] - point[2],
                                   point[0], point[1], point[2]};
    double shape_gradient[10][3]{};
    for (int corner = 0; corner < 4; ++corner)
      for (int axis = 0; axis < 3; ++axis)
        shape_gradient[corner][axis] =
            (4.0 * barycentric[corner] - 1.0) * kGradients[corner][axis];
    for (int edge = 0; edge < 6; ++edge)
      for (int axis = 0; axis < 3; ++axis)
        shape_gradient[4 + edge][axis] =
            4.0 *
            (barycentric[kEdges[edge][0]] * kGradients[kEdges[edge][1]][axis] +
             barycentric[kEdges[edge][1]] * kGradients[kEdges[edge][0]][axis]);

    double jacobian[3][3]{};
    for (int node = 0; node < 10; ++node)
      for (int xyz = 0; xyz < 3; ++xyz)
        for (int axis = 0; axis < 3; ++axis)
          jacobian[xyz][axis] +=
              out.points[static_cast<size_t>(element[node]) * 3 + xyz] *
              shape_gradient[node][axis];
    const double determinant = det3(jacobian);
    if (!std::isfinite(determinant) || !(std::abs(determinant) > det_tolerance))
      return false;
    if (sign == 0.0)
      sign = determinant;
    else if (determinant * sign <= 0.0)
      return false;
  }
  return true;
}

// Chudware's canonical tet10 mid-edge order (mesher.h) and, per corner face,
// the mid-edge local node sitting on each of its three edges.
constexpr int kTet10Edges[6][2] = {{0, 1}, {1, 2}, {2, 0},
                                   {3, 0}, {3, 2}, {3, 1}};
constexpr int kTetFaceCorners[4][3] = {
    {0, 1, 2}, {0, 1, 3}, {0, 2, 3}, {1, 2, 3}};
constexpr int kTetFaceMids[4][3] = {
    {4, 5, 6},  // (0,1)=4 (1,2)=5 (2,0)=6
    {4, 9, 7},  // (0,1)=4 (1,3)=9 (3,0)=7
    {6, 8, 7},  // (0,2)=6 (2,3)=8 (3,0)=7
    {5, 8, 9},  // (1,2)=5 (2,3)=8 (3,1)=9
};

// Is face `f` of a tet10 a well-formed tri6? This is deliberately the SAME
// criterion the static solver's consistent-load quadrature enforces
// (fea_run.cpp tri6_face_weights): take the surface Jacobian at the face
// centroid as the reference normal, then require a strictly positive Jacobian
// pointing the same way at every point of the 5x5 Gauss-Legendre Duffy rule.
// A projected mid node that has folded the face flips the cross product over
// part of the face and fails the alignment test.
bool tri6_face_unfolded(const std::vector<double> &p, const int *element,
                        int f) {
  const int fn[6] = {element[kTetFaceCorners[f][0]],
                     element[kTetFaceCorners[f][1]],
                     element[kTetFaceCorners[f][2]], element[kTetFaceMids[f][0]],
                     element[kTetFaceMids[f][1]], element[kTetFaceMids[f][2]]};
  auto cross_at = [&](double r, double s, double out_cross[3]) {
    const double L0 = 1.0 - r - s;
    const double dr[6] = {1.0 - 4.0 * L0, 4.0 * r - 1.0, 0.0,
                          4.0 * (L0 - r), 4.0 * s,       -4.0 * s};
    const double ds[6] = {1.0 - 4.0 * L0, 0.0,     4.0 * s - 1.0,
                          -4.0 * r,       4.0 * r, 4.0 * (L0 - s)};
    double xr[3] = {0.0, 0.0, 0.0}, xs[3] = {0.0, 0.0, 0.0};
    for (int i = 0; i < 6; ++i)
      for (int c = 0; c < 3; ++c) {
        const double x = p[static_cast<size_t>(fn[i]) * 3 + c];
        xr[c] += dr[i] * x;
        xs[c] += ds[i] * x;
      }
    out_cross[0] = xr[1] * xs[2] - xr[2] * xs[1];
    out_cross[1] = xr[2] * xs[0] - xr[0] * xs[2];
    out_cross[2] = xr[0] * xs[1] - xr[1] * xs[0];
  };
  double reference[3];
  cross_at(1.0 / 3.0, 1.0 / 3.0, reference);
  const double reference_norm2 = reference[0] * reference[0] +
                                 reference[1] * reference[1] +
                                 reference[2] * reference[2];
  if (!std::isfinite(reference_norm2) || !(reference_norm2 > 0.0))
    return false;
  static constexpr double gx[5] = {0.0469100770306680, 0.2307653449471585, 0.5,
                                   0.7692346550528415, 0.9530899229693320};
  for (int iu = 0; iu < 5; ++iu)
    for (int iv = 0; iv < 5; ++iv) {
      const double r = gx[iu], s = (1.0 - r) * gx[iv];
      double c3[3];
      cross_at(r, s, c3);
      const double norm2 = c3[0] * c3[0] + c3[1] * c3[1] + c3[2] * c3[2];
      const double alignment = c3[0] * reference[0] + c3[1] * reference[1] +
                               c3[2] * reference[2];
      if (!std::isfinite(norm2) || !(norm2 > 0.0) ||
          !std::isfinite(alignment) || !(alignment > 0.0))
        return false;
    }
  return true;
}

// Longest corner-to-corner edge of an element: the length scale its Jacobian
// tolerance is measured against.
double element_scale(const std::vector<double> &p, const int *element) {
  static constexpr int kCornerEdges[6][2] = {{0, 1}, {0, 2}, {0, 3},
                                             {1, 2}, {1, 3}, {2, 3}};
  double scale = 0.0;
  for (const auto &edge : kCornerEdges)
    scale = std::max(
        scale, edge_len(p, element[edge[0]], element[edge[1]]));
  return scale;
}

// Does any mid-edge node actually carry curvature (i.e. was it projected off
// its straight chord)? Interior elements keep exact midpoints, so this skips
// the bulk of a mesh before the 4x25-point face sweep.
bool has_projected_mids(const std::vector<double> &p, const int *element,
                        double scale) {
  const double slack = 1e-9 * std::max(1.0, scale);
  for (int m = 0; m < 6; ++m) {
    const double *A = &p[static_cast<size_t>(element[kTet10Edges[m][0]]) * 3];
    const double *B = &p[static_cast<size_t>(element[kTet10Edges[m][1]]) * 3];
    const double *M = &p[static_cast<size_t>(element[4 + m]) * 3];
    for (int c = 0; c < 3; ++c)
      if (std::abs(M[c] - 0.5 * (A[c] + B[c])) > slack)
        return true;
  }
  return false;
}

// " at (x, y, z) mm" for the corner centroid of an element — a diagnostic
// needs to say WHERE a bad element is, not just its index.
std::string element_location(const std::vector<double> &p, const int *element) {
  double centroid[3] = {0.0, 0.0, 0.0};
  for (int corner = 0; corner < 4; ++corner)
    for (int c = 0; c < 3; ++c)
      centroid[c] += p[static_cast<size_t>(element[corner]) * 3 + c] / 4.0;
  char buffer[96];
  std::snprintf(buffer, sizeof(buffer), " at (%.3f, %.3f, %.3f) mm",
                centroid[0], centroid[1], centroid[2]);
  return std::string(buffer);
}

} // namespace

namespace detail {
void measure_edges(MeshOut &out) {
  std::vector<uint64_t> edges;
  edges.reserve(out.tets.size() / static_cast<std::size_t>(out.nodes_per_elem) * 6);
  for (std::size_t e = 0; e < out.tets.size(); e += out.nodes_per_elem)
    for (int a = 0; a < 4; ++a)
      for (int b = a + 1; b < 4; ++b) {
        const auto lo = static_cast<uint32_t>(std::min(out.tets[e+a], out.tets[e+b]));
        const auto hi = static_cast<uint32_t>(std::max(out.tets[e+a], out.tets[e+b]));
        edges.push_back((static_cast<uint64_t>(lo) << 32) | hi);
      }
  std::sort(edges.begin(), edges.end());
  edges.erase(std::unique(edges.begin(), edges.end()), edges.end());
  std::vector<double> lengths;
  lengths.reserve(edges.size());
  for (const auto edge : edges)
    lengths.push_back(edge_len(out.points, static_cast<int>(edge >> 32),
                               static_cast<int>(edge & 0xffffffffu)));
  if (lengths.empty()) return;
  const auto bounds = std::minmax_element(lengths.begin(), lengths.end());
  out.edge_min_mm = *bounds.first;
  out.edge_max_mm = *bounds.second;
  const auto middle = lengths.begin() + lengths.size()/2;
  std::nth_element(lengths.begin(), middle, lengths.end());
  out.edge_median_mm = *middle;
  if (lengths.size()%2 == 0)
    out.edge_median_mm = 0.5 * (*middle + *std::max_element(lengths.begin(), middle));
}

// THE tet shape metrics (mesher.h TetShape). Both are volume-over-edge-lengths
// forms normalized to 1.0 on a regular tet, and they are ordered:
// `scaled_jacobian >= aspect^-1` and `scaled_jacobian >= 6*sqrt(2)*V/L_max^3`
// (the corner whose three edges are longest carries the smallest ratio, and no
// corner's edge product exceeds L_max^3), which is why the local quality pass
// can drive a single objective and report both floors.
TetShape tet_shape(const double *a, const double *b, const double *c,
                   const double *d) {
  const double *p[4] = {a, b, c, d};
  static constexpr int kEdges[6][2] = {{0, 1}, {0, 2}, {0, 3},
                                       {1, 2}, {1, 3}, {2, 3}};
  static constexpr int kFaces[4][3] = {
      {1, 2, 3}, {0, 2, 3}, {0, 1, 3}, {0, 1, 2}};
  static constexpr int kIncident[4][3] = {
      {1, 2, 3}, {0, 2, 3}, {0, 1, 3}, {0, 1, 2}};
  const auto length = [&p](int i, int j) {
    const double dx = p[i][0] - p[j][0];
    const double dy = p[i][1] - p[j][1];
    const double dz = p[i][2] - p[j][2];
    return std::sqrt(dx * dx + dy * dy + dz * dz);
  };
  const double ux = b[0] - a[0], uy = b[1] - a[1], uz = b[2] - a[2];
  const double vx = c[0] - a[0], vy = c[1] - a[1], vz = c[2] - a[2];
  const double wx = d[0] - a[0], wy = d[1] - a[1], wz = d[2] - a[2];
  const double det6 = std::abs(ux * (vy * wz - vz * wy) -
                               uy * (vx * wz - vz * wx) +
                               uz * (vx * wy - vy * wx));
  TetShape shape{std::numeric_limits<double>::infinity(), 0.0};
  const double volume = det6 / 6.0;
  if (!(volume > 0.0))
    return shape;

  double longest = 0.0;
  for (const auto &edge : kEdges)
    longest = std::max(longest, length(edge[0], edge[1]));
  double max_face = 0.0;
  for (const auto &face : kFaces) {
    const double *A = p[face[0]];
    const double *B = p[face[1]];
    const double *C = p[face[2]];
    const double ax = B[0] - A[0], ay = B[1] - A[1], az = B[2] - A[2];
    const double bx = C[0] - A[0], by = C[1] - A[1], bz = C[2] - A[2];
    const double cx = ay * bz - az * by;
    const double cy = az * bx - ax * bz;
    const double cz = ax * by - ay * bx;
    max_face = std::max(max_face, 0.5 * std::sqrt(cx * cx + cy * cy + cz * cz));
  }
  if (max_face > 0.0 && longest > 0.0) {
    const double shortest_altitude = 3.0 * volume / max_face;
    if (shortest_altitude > 0.0)
      shape.aspect = (longest / shortest_altitude) / 1.224744871391589;
  }

  // Magnitude, not signed: Chudware's tet4 contract carries no orientation
  // (every consumer takes |det|), so a mirrored cell must measure like its
  // twin. Inversion is a SEPARATE verdict and has its own gate — the local
  // quality pass detects folds with polymesh's signed tet_shape_quality.
  double minimum = std::numeric_limits<double>::infinity();
  for (int corner = 0; corner < 4; ++corner) {
    double product = 1.0;
    for (int i = 0; i < 3; ++i)
      product *= length(corner, kIncident[corner][i]);
    if (product > 0.0)
      minimum = std::min(minimum, std::sqrt(2.0) * det6 / product);
  }
  shape.scaled_jacobian = std::isfinite(minimum) ? minimum : 0.0;
  return shape;
}

bool fill_quality(MeshOut &out) {
  const int stride = out.nodes_per_elem == 10 ? 10 : 4;
  if (out.points.empty() || out.points.size() % 3 != 0 || out.tets.empty() ||
      out.tets.size() % static_cast<size_t>(stride) != 0) {
    out.error = "mesh quality: malformed point or element arrays";
    return false;
  }
  for (double coordinate : out.points) {
    if (!std::isfinite(coordinate)) {
      out.error = "mesh quality: non-finite node coordinate";
      return false;
    }
  }

  const size_t element_count = out.tets.size() / static_cast<size_t>(stride);
  const int node_count = static_cast<int>(out.points.size() / 3);
  out.aspect_hist.assign(10, 0);
  double minimum_volume = std::numeric_limits<double>::infinity();
  double maximum_edge_ratio = 0.0;
  double minimum_aspect = std::numeric_limits<double>::infinity();
  double maximum_aspect = 0.0;
  double aspect_sum = 0.0;
  double minimum_jacobian = std::numeric_limits<double>::infinity();
  size_t poor = 0;
  static constexpr int kEdges[6][2] = {{0, 1}, {0, 2}, {0, 3},
                                       {1, 2}, {1, 3}, {2, 3}};
  // Node-adjacency components over the elements, folded into the pass that
  // already validates every index. The metric rides WITH the mesh (the mesh
  // cache included) so no solve route has to re-derive it, and the union rule
  // is the one the static route uses when it decides whether every island
  // reaches a fixture: every node of an element joins that element's set.
  //
  // Nodes NO element references are not components of the domain, and counting
  // them here was a live defect, not pedantry: the hanging-fragment dissolve
  // preserves node numbering by design, so every repaired mesh carries orphan
  // nodes — on a rover caliper-bracket isolate at tet4 h = 3.0 mm this read
  // 57 components for a domain of 4, and 45 for 8 at h = 4.0. Two consumers
  // then read a number that was mostly orphan count: mesh_loaded_model's
  // honest-coarsening rebuild ("came back in more pieces than the shape has
  // solids") fired on every mesh and kept whichever fill happened to have
  // fewer orphans, which is how a COARSER request delivered a FINER mesh
  // (345,716 elements at h = 4.0 against 271,233 at h = 3.0, i.e. a
  // non-monotone memory estimate), and the reported mesh_quality contradicted
  // the static route's own domain count in the same packet. The orphans stay
  // reported by the solve route, which names them in a solver note.
  std::vector<int> parent(static_cast<size_t>(node_count));
  std::vector<char> referenced(static_cast<size_t>(node_count), 0);
  for (int node = 0; node < node_count; ++node)
    parent[static_cast<size_t>(node)] = node;
  auto find = [&parent](int x) {
    while (parent[static_cast<size_t>(x)] != x) {
      parent[static_cast<size_t>(x)] =
          parent[static_cast<size_t>(parent[static_cast<size_t>(x)])];
      x = parent[static_cast<size_t>(x)];
    }
    return x;
  };

  for (size_t index = 0; index < element_count; ++index) {
    const int *element = &out.tets[index * static_cast<size_t>(stride)];
    for (int node = 0; node < stride; ++node) {
      if (element[node] < 0 || element[node] >= node_count) {
        out.error = "mesh quality: element " + std::to_string(index) +
                    " contains an invalid node index";
        return false;
      }
      referenced[static_cast<size_t>(element[node])] = 1;
    }
    for (int node = 1; node < stride; ++node) {
      const int a = find(element[0]);
      const int b = find(element[node]);
      if (a != b)
        parent[static_cast<size_t>(std::max(a, b))] = std::min(a, b);
    }
    for (int a = 0; a < 4; ++a) {
      for (int b = a + 1; b < 4; ++b) {
        if (element[a] == element[b]) {
          out.error = "mesh quality: element " + std::to_string(index) +
                      " has repeated corner nodes";
          return false;
        }
      }
    }

    const double determinant =
        tet_volume6(out.points, element[0], element[1], element[2], element[3]);
    double corner_scale = 0.0;
    for (const auto &edge : kEdges)
      corner_scale =
          std::max(corner_scale,
                   edge_len(out.points, element[edge[0]], element[edge[1]]));
    const double tolerance =
        64.0 * std::numeric_limits<double>::epsilon() *
        std::max(1.0, corner_scale * corner_scale * corner_scale);
    if (!std::isfinite(determinant) || !(std::abs(determinant) > tolerance) ||
        (stride == 10 && !tet10_jacobians_valid(out, element, tolerance))) {
      out.error = "mesh quality: element " + std::to_string(index) +
                  " is degenerate, inverted, or has a sign-changing Jacobian" +
                  element_location(out.points, element);
      return false;
    }

    minimum_volume = std::min(minimum_volume, std::abs(determinant) / 6.0);
    double shortest = std::numeric_limits<double>::infinity();
    double longest = 0.0;
    for (const auto &edge : kEdges) {
      const double length =
          edge_len(out.points, element[edge[0]], element[edge[1]]);
      if (length > 0.0) {
        shortest = std::min(shortest, length);
        longest = std::max(longest, length);
      }
    }
    if (std::isfinite(shortest) && shortest > 0.0)
      maximum_edge_ratio = std::max(maximum_edge_ratio, longest / shortest);

    const TetShape quality =
        tet_shape(&out.points[static_cast<size_t>(element[0]) * 3],
                  &out.points[static_cast<size_t>(element[1]) * 3],
                  &out.points[static_cast<size_t>(element[2]) * 3],
                  &out.points[static_cast<size_t>(element[3]) * 3]);
    if (!std::isfinite(quality.aspect) ||
        !std::isfinite(quality.scaled_jacobian) ||
        !(quality.scaled_jacobian > 0.0)) {
      out.error = "mesh quality: element " + std::to_string(index) +
                  " has invalid quality metrics";
      return false;
    }
    minimum_aspect = std::min(minimum_aspect, quality.aspect);
    maximum_aspect = std::max(maximum_aspect, quality.aspect);
    aspect_sum += quality.aspect;
    minimum_jacobian = std::min(minimum_jacobian, quality.scaled_jacobian);
    if (quality.aspect > kPoorAspect)
      ++poor;
    int bucket =
        static_cast<int>((quality.aspect - 1.0) / (kPoorAspect - 1.0) * 10.0);
    out.aspect_hist[static_cast<size_t>(std::clamp(bucket, 0, 9))]++;
  }

  int components = 0;
  for (int node = 0; node < node_count; ++node)
    if (referenced[static_cast<size_t>(node)] && find(node) == node)
      ++components;
  out.connected_components = components;

  out.min_tet_volume_mm3 = minimum_volume;
  out.max_tet_edge_ratio = maximum_edge_ratio;
  out.aspect_min = minimum_aspect;
  out.aspect_avg = aspect_sum / static_cast<double>(element_count);
  out.aspect_max = maximum_aspect;
  out.jacobian_min = minimum_jacobian;
  out.pct_poor =
      100.0 * static_cast<double>(poor) / static_cast<double>(element_count);
  return true;
}

int repair_folded_quadratic(MeshOut &out) {
  out.quadratic_fallback_elems = 0;
  if (out.nodes_per_elem != 10 || out.points.empty() ||
      out.points.size() % 3 != 0 || out.tets.empty() ||
      out.tets.size() % 10 != 0)
    return 0;
  const size_t element_count = out.tets.size() / 10;
  const int node_count = static_cast<int>(out.points.size() / 3);

  // Straightened elements are stable: a straight mid node sits at the midpoint
  // of its two corners, and those corners are shared verbatim by every element
  // on that edge, so no later pass can move it. The sweep therefore only ever
  // has to revisit elements still carrying projected mids, and converges.
  std::vector<char> straightened(element_count, 0);
  int total = 0;
  for (int pass = 0; pass < 8; ++pass) {
    int fixed_this_pass = 0;
    for (size_t index = 0; index < element_count; ++index) {
      if (straightened[index])
        continue;
      const int *element = &out.tets[index * 10];
      bool indices_valid = true;
      for (int node = 0; node < 10; ++node)
        if (element[node] < 0 || element[node] >= node_count)
          indices_valid = false;
      if (!indices_valid)
        continue;  // fill_quality is the validator; never read out of range
      const double scale = element_scale(out.points, element);
      if (!has_projected_mids(out.points, element, scale))
        continue;
      const double tolerance = 64.0 * std::numeric_limits<double>::epsilon() *
                               std::max(1.0, scale * scale * scale);
      bool folded = !tet10_jacobians_valid(out, element, tolerance);
      for (int f = 0; !folded && f < 4; ++f)
        folded = !tri6_face_unfolded(out.points, element, f);
      if (!folded)
        continue;
      for (int m = 0; m < 6; ++m) {
        const double *A =
            &out.points[static_cast<size_t>(element[kTet10Edges[m][0]]) * 3];
        const double *B =
            &out.points[static_cast<size_t>(element[kTet10Edges[m][1]]) * 3];
        const double a[3] = {A[0], A[1], A[2]};
        const double b[3] = {B[0], B[1], B[2]};
        double *M = &out.points[static_cast<size_t>(element[4 + m]) * 3];
        for (int c = 0; c < 3; ++c)
          M[c] = 0.5 * (a[c] + b[c]);
      }
      straightened[index] = 1;
      ++fixed_this_pass;
      ++total;
    }
    if (fixed_this_pass == 0)
      break;
  }
  out.quadratic_fallback_elems = total;
  return total;
}

// ── Hanging-fragment repair ─────────────────────────────────────────────────
// Judged on CORNERS only: a tet10 mid-edge node is interpolatory and adds no rigid
// mode of its own, so it is never the carrier.
//
// The defect is NOT "a corner used by one element". A tet whose apex is used only by
// itself is perfectly ordinary — hold its other three corners and the apex can only
// move by STRAINING the element, so its 3x3 block is full rank. Measured: a real
// 316466-element wheel mesh has 51 such tips and only 8 of them were pathological.
//
// The defect is a FRAGMENT — a set of elements glued to each other face-to-face —
// that touches the rest of the mesh at two corners or fewer. It can then rotate about
// that node or edge with (near-)zero strain energy, which is a rigid-body mechanism
// inside an otherwise connected mesh: every component/fixture check passes, because
// the fragment does reach a fixture through the node it hangs from. A single element
// is just the smallest such fragment, and taking fragments rather than elements
// matters in practice — on that same wheel mesh the per-element rule cleared the
// exact rigid mode (lambda_min 5.6e-27) but left a two-element flap behind
// (lambda_min 1.1e-13 against lambda_max 2.3e7, i.e. still singular in double
// precision).
//
// Fragments are found by face adjacency (two tets are joined when they share three
// corners), not by node adjacency, precisely because sharing ONE node is the defect
// being looked for.
namespace {

// Face-adjacency fragments, per-fragment count of corners shared with the
// outside, and which fragment IS the body it hangs from.
//
// `is_body` is the asymmetry the rule needs. "Touches the rest of the mesh at
// two corners or fewer" is a symmetric measurement: a 1-element flap glued to
// a 100k-element disc by one corner reads 1 — and so does the DISC, whose only
// corners used outside itself are that flap's. Without this flag the whole
// body was marked a hanging fragment, so `count_lonely_nodes` reported every
// node in the part and `repair_lonely_nodes` tried to dissolve the entire
// mesh (which its own empty-result guard then refused, leaving the study to be
// refused for a mechanism that did not exist). Measured on a rover wheel
// sector at fineness 0.5: the fill hands over 102752 elements in exactly three
// face-connected fragments — the body (102750) and two isolated tets — and the
// study came back "mesh has 22026 node(s) on element fragment(s) that touch
// the rest of the body at two corners or fewer", i.e. all of them.
//
// The body of a node-connected component is the fragment holding a strict
// MAJORITY of that component's elements — a flap is a minority by definition.
// When no fragment has one, there is no body: two bodies fused at a single
// node are two fragments of equal size, neither is a flap of the other, and
// both stay flagged so the mechanism is REPORTED rather than half the model
// deleted (pinned by test_mesh_quality's "refuses to delete the whole mesh").
struct Fragments {
  std::vector<int> root;             // element -> fragment root element
  std::vector<int> outside_corners;  // per element root: corners shared outside
  std::vector<int> element_count;    // per element root: elements in fragment
  std::vector<char> is_body;         // per element root: the body, never a flap
};

Fragments find_fragments(const MeshOut &out, int stride, int node_count,
                         const std::vector<int> &corner_valence) {
  const size_t element_count = out.tets.size() / static_cast<size_t>(stride);
  Fragments f;
  f.root.resize(element_count);
  for (size_t e = 0; e < element_count; ++e)
    f.root[e] = static_cast<int>(e);
  std::function<int(int)> find = [&](int x) {
    while (f.root[static_cast<size_t>(x)] != x) {
      f.root[static_cast<size_t>(x)] =
          f.root[static_cast<size_t>(f.root[static_cast<size_t>(x)])];
      x = f.root[static_cast<size_t>(x)];
    }
    return x;
  };
  auto unite = [&](int a, int b) {
    a = find(a);
    b = find(b);
    if (a != b)
      f.root[static_cast<size_t>(std::max(a, b))] = std::min(a, b);
  };
  // Join elements that share a FACE. Key = the face's three corner ids, sorted.
  static constexpr int kFaces[4][3] = {{0, 1, 2}, {0, 1, 3}, {0, 2, 3}, {1, 2, 3}};
  std::unordered_map<uint64_t, int> face_owner;
  face_owner.reserve(element_count * 4);
  for (size_t e = 0; e < element_count; ++e) {
    const int *element = &out.tets[e * static_cast<size_t>(stride)];
    for (int face = 0; face < 4; ++face) {
      int a = element[kFaces[face][0]], b = element[kFaces[face][1]],
          c = element[kFaces[face][2]];
      if (a < 0 || b < 0 || c < 0 || a >= node_count || b >= node_count ||
          c >= node_count)
        continue;
      if (a > b) std::swap(a, b);
      if (b > c) std::swap(b, c);
      if (a > b) std::swap(a, b);
      const uint64_t key = (static_cast<uint64_t>(a) * 2654435761ull) ^
                           (static_cast<uint64_t>(b) * 40503ull) ^
                           (static_cast<uint64_t>(c) * 2246822519ull);
      auto found = face_owner.find(key);
      if (found == face_owner.end())
        face_owner.emplace(key, static_cast<int>(e));
      else
        unite(found->second, static_cast<int>(e));
    }
  }
  // Per fragment: how many of its corners are ALSO used by an element outside it.
  f.outside_corners.assign(element_count, 0);
  f.element_count.assign(element_count, 0);
  std::unordered_map<int, std::vector<int>> fragment_nodes;
  for (size_t e = 0; e < element_count; ++e) {
    const int r = find(static_cast<int>(e));
    ++f.element_count[static_cast<size_t>(r)];
    const int *element = &out.tets[e * static_cast<size_t>(stride)];
    auto &nodes = fragment_nodes[r];
    for (int c = 0; c < 4; ++c)
      if (element[c] >= 0 && element[c] < node_count) nodes.push_back(element[c]);
  }
  for (auto &entry : fragment_nodes) {
    std::vector<int> &nodes = entry.second;
    std::sort(nodes.begin(), nodes.end());
    int shared = 0;
    for (size_t i = 0; i < nodes.size();) {
      size_t j = i;
      while (j < nodes.size() && nodes[j] == nodes[i]) ++j;
      const int inside = static_cast<int>(j - i);
      if (corner_valence[static_cast<size_t>(nodes[i])] > inside) ++shared;
      i = j;
    }
    f.outside_corners[static_cast<size_t>(entry.first)] = shared;
  }
  // Normalize root ids so callers can index by element without re-finding.
  for (size_t e = 0; e < element_count; ++e)
    f.root[e] = find(static_cast<int>(e));

  // Which fragment is the body: the largest fragment of each NODE-connected
  // component. A fragment is face-connected, hence entirely inside one
  // component, so its root's component labels the whole fragment.
  std::vector<int> body(element_count);
  for (size_t e = 0; e < element_count; ++e)
    body[e] = static_cast<int>(e);
  std::function<int(int)> body_find = [&body](int x) {
    while (body[static_cast<size_t>(x)] != x) {
      body[static_cast<size_t>(x)] =
          body[static_cast<size_t>(body[static_cast<size_t>(x)])];
      x = body[static_cast<size_t>(x)];
    }
    return x;
  };
  std::vector<int> first_at_node(static_cast<size_t>(node_count), -1);
  for (size_t e = 0; e < element_count; ++e) {
    const int *element = &out.tets[e * static_cast<size_t>(stride)];
    for (int c = 0; c < 4; ++c) {
      const int node = element[c];
      if (node < 0 || node >= node_count)
        continue;
      int &first = first_at_node[static_cast<size_t>(node)];
      if (first < 0) {
        first = static_cast<int>(e);
        continue;
      }
      const int a = body_find(first);
      const int b = body_find(static_cast<int>(e));
      if (a != b)
        body[static_cast<size_t>(std::max(a, b))] = std::min(a, b);
    }
  }
  // Per component: its total element count, and its biggest fragment.
  std::unordered_map<int, int> component_elems;
  component_elems.reserve(element_count / 8 + 1);
  for (size_t e = 0; e < element_count; ++e)
    ++component_elems[body_find(static_cast<int>(e))];
  std::unordered_map<int, std::pair<int, int>> biggest; // comp -> (size, root)
  biggest.reserve(component_elems.size());
  for (size_t e = 0; e < element_count; ++e) {
    if (f.root[e] != static_cast<int>(e))
      continue; // one visit per fragment, at its root
    const int size = f.element_count[e];
    const int component = body_find(static_cast<int>(e));
    auto it = biggest.find(component);
    if (it == biggest.end())
      biggest.emplace(component, std::pair<int, int>{size, static_cast<int>(e)});
    else if (size > it->second.first)
      it->second = {size, static_cast<int>(e)};
  }
  f.is_body.assign(element_count, 0);
  for (const auto &entry : biggest) {
    // Strict majority, so a component split into equal halves has no body and
    // neither half can be dissolved as the other's flap.
    if (entry.second.first * 2 > component_elems[entry.first])
      f.is_body[static_cast<size_t>(entry.second.second)] = 1;
  }
  return f;
}

std::vector<int> corner_valences(const MeshOut &out, int stride, int node_count) {
  std::vector<int> valence(static_cast<size_t>(node_count), 0);
  const size_t element_count = out.tets.size() / static_cast<size_t>(stride);
  for (size_t e = 0; e < element_count; ++e) {
    const int *element = &out.tets[e * static_cast<size_t>(stride)];
    for (int c = 0; c < 4; ++c) {
      const int node = element[c];
      if (node >= 0 && node < node_count) ++valence[static_cast<size_t>(node)];
    }
  }
  return valence;
}

// Connected components counted over the nodes an ELEMENT actually references.
// Deliberately not fill_quality's count, which also counts every unreferenced
// node as its own component: the repair below leaves unreferenced nodes behind
// by design (node numbering is preserved), so a count that included them could
// never answer the one question the repair has to ask — did dissolving those
// fragments break the mesh into more PIECES than it had?
int element_component_count(const MeshOut &out, int stride, int node_count) {
  const size_t element_count = out.tets.size() / static_cast<size_t>(stride);
  if (element_count == 0)
    return 0;
  std::vector<int> parent(static_cast<size_t>(node_count));
  for (int node = 0; node < node_count; ++node)
    parent[static_cast<size_t>(node)] = node;
  std::function<int(int)> find = [&parent](int x) {
    while (parent[static_cast<size_t>(x)] != x) {
      parent[static_cast<size_t>(x)] =
          parent[static_cast<size_t>(parent[static_cast<size_t>(x)])];
      x = parent[static_cast<size_t>(x)];
    }
    return x;
  };
  std::vector<char> referenced(static_cast<size_t>(node_count), 0);
  for (size_t e = 0; e < element_count; ++e) {
    const int *element = &out.tets[e * static_cast<size_t>(stride)];
    int anchor = -1;
    for (int k = 0; k < stride; ++k) {
      const int node = element[k];
      if (node < 0 || node >= node_count)
        continue;
      referenced[static_cast<size_t>(node)] = 1;
      if (anchor < 0) {
        anchor = node;
        continue;
      }
      const int a = find(anchor);
      const int b = find(node);
      if (a != b)
        parent[static_cast<size_t>(std::max(a, b))] = std::min(a, b);
    }
  }
  int components = 0;
  for (int node = 0; node < node_count; ++node)
    if (referenced[static_cast<size_t>(node)] && find(node) == node)
      ++components;
  return components;
}

// ── Sub-element debris islands ───────────────────────────────────────────────
// A component that is not the domain, is under a MILLIONTH of the domain's
// volume, and is smaller than ONE average element of this very mesh is fill
// debris: it cannot carry load, it cannot be refined into anything, and it
// costs the whole study a rigid-body refusal. Measured on a rover
// caliper-bracket isolate (body 201 — rods that pass THROUGH each other, so
// their union leaves a 9.8 um knife-edge where one emerges from the next), tet4:
//
//   h = 4.0 mm : 8 components, 7 debris of 9/15/8/1/1/1/2 elements,
//                0.0002-0.058 mm^3 each, 0.106 mm^3 total vs 88526.2 mm^3
//   h = 3.0 mm : 4 components, 3 debris of 1/3/3 elements, 0.0101 mm^3 total
//   h = 2.2 mm : 5 components, 4 debris of 1/2/3/3 elements, 0.0053 mm^3
//
// The hanging-fragment rule above cannot reach them: each debris island is
// internally face-connected and shares NO node with the body, so it reads as a
// separate BODY (outside_corners == 0) and is deliberately spared there —
// that guard exists so a real second body is never deleted. The volume test is
// what tells the two apart, and it is the physics: a millionth of the domain
// contributes a millionth of the stiffness at most, and a component smaller
// than one element of its own mesh was never resolved in the first place.
// Anything bigger stays, and the static route still refuses it BY NAME.
//
// Node numbering is preserved (only the element list shrinks), like every other
// repair here, so caller-side node indices stay valid.
int dissolve_debris_components(MeshOut &out, int stride, int node_count) {
  const size_t element_count = out.tets.size() / static_cast<size_t>(stride);
  if (element_count < 2)
    return 0;
  std::vector<int> parent(static_cast<size_t>(node_count));
  for (int node = 0; node < node_count; ++node)
    parent[static_cast<size_t>(node)] = node;
  std::function<int(int)> find = [&parent](int x) {
    while (parent[static_cast<size_t>(x)] != x) {
      parent[static_cast<size_t>(x)] =
          parent[static_cast<size_t>(parent[static_cast<size_t>(x)])];
      x = parent[static_cast<size_t>(x)];
    }
    return x;
  };
  for (size_t e = 0; e < element_count; ++e) {
    const int *element = &out.tets[e * static_cast<size_t>(stride)];
    for (int k = 1; k < stride; ++k) {
      if (element[0] < 0 || element[0] >= node_count || element[k] < 0 ||
          element[k] >= node_count)
        return 0; // malformed: leave the mesh to the validating pass
      const int a = find(element[0]);
      const int b = find(element[k]);
      if (a != b)
        parent[static_cast<size_t>(std::max(a, b))] = std::min(a, b);
    }
  }
  // Per-component element count and volume, keyed by root.
  std::unordered_map<int, std::pair<int, double>> census;
  census.reserve(16);
  double total_volume = 0.0;
  for (size_t e = 0; e < element_count; ++e) {
    const int *element = &out.tets[e * static_cast<size_t>(stride)];
    const double volume =
        std::abs(tet_volume6(out.points, element[0], element[1], element[2],
                             element[3])) /
        6.0;
    total_volume += volume;
    auto &entry = census[find(element[0])];
    entry.first += 1;
    entry.second += volume;
  }
  if (census.size() < 2 || !(total_volume > 0.0))
    return 0;
  const double mean_element_volume =
      total_volume / static_cast<double>(element_count);
  int domain_root = census.begin()->first;
  double domain_volume = census.begin()->second.second;
  for (const auto &entry : census)
    if (entry.second.second > domain_volume) {
      domain_root = entry.first;
      domain_volume = entry.second.second;
    }
  std::unordered_map<int, char> debris;
  debris.reserve(census.size());
  for (const auto &entry : census) {
    if (entry.first == domain_root)
      continue;
    if (entry.second.second < total_volume * 1e-6 &&
        entry.second.second < mean_element_volume)
      debris.emplace(entry.first, 1);
  }
  if (debris.empty())
    return 0;
  std::vector<int> kept;
  kept.reserve(out.tets.size());
  int dropped = 0;
  for (size_t e = 0; e < element_count; ++e) {
    const int *element = &out.tets[e * static_cast<size_t>(stride)];
    if (debris.count(find(element[0]))) {
      ++dropped;
      out.debris_volume_mm3 +=
          std::abs(tet_volume6(out.points, element[0], element[1], element[2],
                               element[3])) /
          6.0;
      continue;
    }
    kept.insert(kept.end(), element, element + stride);
  }
  out.tets.swap(kept);
  out.debris_components_dissolved += static_cast<int>(debris.size());
  out.debris_elems_dropped += dropped;
  out.n_elems = static_cast<int>(out.tets.size() / static_cast<size_t>(stride));
  return dropped;
}

// What the mesh carries when the hanging-fragment dissolve is refused because
// it would have fragmented the domain (see repair_lonely_nodes).
constexpr const char *kSplitRefusalNote =
    "hanging-fragment dissolve refused: dropping the fragment(s) would have "
    "split the mesh into more connected components than it had, so the mesh is "
    "unchanged and the mechanism is reported instead — re-mesh at a smaller "
    "element size";

// …and when it would have dissolved EVERY element (two bodies fused at a single
// node are two fragments, each hanging off the other by one corner).
constexpr const char *kEmptyRefusalNote =
    "hanging-fragment dissolve refused: every element of the mesh is a hanging "
    "fragment (bodies joined at a node or an edge carry no other load path), so "
    "the mesh is unchanged and the mechanism is reported instead — join the "
    "bodies over a face, add a bonded gap tie, or re-mesh at a smaller element "
    "size";

} // namespace

int count_lonely_nodes(MeshOut &out) {
  const int stride = out.nodes_per_elem == 10 ? 10 : 4;
  out.lonely_nodes_remaining = 0;
  out.single_element_nodes = 0;
  if (out.points.empty() || out.tets.empty() ||
      out.tets.size() % static_cast<size_t>(stride) != 0)
    return 0;
  const int node_count = static_cast<int>(out.points.size() / 3);
  const size_t element_count = out.tets.size() / static_cast<size_t>(stride);
  const std::vector<int> valence = corner_valences(out, stride, node_count);
  for (int node = 0; node < node_count; ++node)
    if (valence[static_cast<size_t>(node)] == 1)
      ++out.single_element_nodes;
  if (element_count < 2)
    return 0;  // a one-element mesh is a domain, not a fragment
  const Fragments fragments = find_fragments(out, stride, node_count, valence);
  std::vector<char> mechanism_node(static_cast<size_t>(node_count), 0);
  for (size_t e = 0; e < element_count; ++e) {
    const int r = fragments.root[e];
    if (fragments.is_body[static_cast<size_t>(r)])
      continue;  // this fragment IS the body — see Fragments::is_body
    // A fragment that shares NO node with anything else is a separate BODY (a
    // connected component), not a flap hanging off one. Two stacked boxes with
    // a 0.05 mm contact gap are exactly that, and before this guard every node
    // of both healthy bodies was reported as a mechanism node and fea_static
    // refused the study. Components are handled by connected_components /
    // unfixed_components / contact + bonded ties, all upstream of this rule.
    // The WHEEL-RADIAL defect this rule exists for is untouched: a hanging
    // fragment shares 1 or 2 corners with its own body by definition, and a
    // body welded to another at a single node still reads 1 and is refused.
    if (fragments.outside_corners[static_cast<size_t>(r)] == 0)
      continue;
    if (fragments.outside_corners[static_cast<size_t>(r)] > 2)
      continue;
    const int *element = &out.tets[e * static_cast<size_t>(stride)];
    for (int c = 0; c < 4; ++c)
      if (element[c] >= 0 && element[c] < node_count)
        mechanism_node[static_cast<size_t>(element[c])] = 1;
  }
  int lonely = 0;
  for (int node = 0; node < node_count; ++node)
    if (mechanism_node[static_cast<size_t>(node)])
      ++lonely;
  out.lonely_nodes_remaining = lonely;
  return lonely;
}

int repair_lonely_nodes(MeshOut &out) {
  const int stride = out.nodes_per_elem == 10 ? 10 : 4;
  out.lonely_nodes_repaired = 0;
  out.hanging_elems_dropped = 0;
  out.dropped_volume_mm3 = 0.0;
  if (out.points.empty() || out.points.size() % 3 != 0 || out.tets.empty() ||
      out.tets.size() % static_cast<size_t>(stride) != 0) {
    count_lonely_nodes(out);
    return 0;
  }
  const int node_count = static_cast<int>(out.points.size() / 3);
  const int lonely_before = count_lonely_nodes(out);
  const int components_before =
      element_component_count(out, stride, node_count);
  // The pre-repair element list, captured lazily at the first drop: a fragment
  // that hangs off TWO corners can be the only thing joining two halves of the
  // mesh, and dissolving it then SPLITS the domain — a defect worse than the
  // mechanism it removes, because each new island is its own rigid-body system.
  std::vector<int> before_drop;

  // Why "drop" rather than "merge the lonely corner onto the opposite face": that
  // corner belongs to the fragment alone, so merging it onto another of the
  // fragment's nodes collapses the same element to zero volume — merge-then-delete
  // and delete are the same operation — and deleting cannot fold a neighbour,
  // because no neighbour uses those corners. The Jacobian precondition is satisfied
  // vacuously: the only elements whose shape changes are the ones that leave.
  // Dropping a fragment can expose another one behind it, so this iterates.
  int total_dropped = 0;
  for (int pass = 0; pass < 16; ++pass) {
    const size_t element_count = out.tets.size() / static_cast<size_t>(stride);
    if (element_count < 2)
      break;
    const std::vector<int> valence = corner_valences(out, stride, node_count);
    const Fragments fragments = find_fragments(out, stride, node_count, valence);
    std::vector<char> drop(element_count, 0);
    size_t dropping = 0;
    for (size_t e = 0; e < element_count; ++e) {
      const int r = fragments.root[e];
      if (fragments.is_body[static_cast<size_t>(r)])
        continue;  // the body is never a flap — see Fragments::is_body
      // Same separate-BODY guard as count_lonely_nodes: dropping every element
      // of a healthy second body is not a repair, it is deleting the part.
      if (fragments.outside_corners[static_cast<size_t>(r)] == 0)
        continue;
      if (fragments.outside_corners[static_cast<size_t>(r)] > 2)
        continue;
      const int *element = &out.tets[e * static_cast<size_t>(stride)];
      bool indices_valid = true;
      for (int c = 0; c < 4; ++c)
        if (element[c] < 0 || element[c] >= node_count) indices_valid = false;
      if (!indices_valid)
        continue;
      drop[e] = 1;
      ++dropping;
      out.dropped_volume_mm3 +=
          std::abs(tet_volume6(out.points, element[0], element[1], element[2],
                               element[3])) /
          6.0;
    }
    if (dropping == 0)
      break;
    // Compact the element list in place; node numbering is left untouched so every
    // caller's node indices (fixtures, loads, exported systems) stay valid.
    std::vector<int> kept;
    kept.reserve(out.tets.size());
    for (size_t e = 0; e < element_count; ++e) {
      if (drop[e])
        continue;
      const int *element = &out.tets[e * static_cast<size_t>(stride)];
      kept.insert(kept.end(), element, element + stride);
    }
    if (total_dropped == 0)
      before_drop = out.tets;  // one copy, only when a repair really happens
    out.tets.swap(kept);
    total_dropped += static_cast<int>(dropping);
  }
  // ── A repair may not delete the domain, nor break it into more pieces ─────
  // Both refusals restore the mesh EXACTLY and leave the mechanism in place, so
  // count_lonely_nodes below still reports it and the solve refuses NAMING it —
  // which is the honest outcome. Deleting the whole mesh, or handing back one
  // in more islands than it came in, replaces a diagnosable mechanism with a
  // mesh nobody can solve or read.
  //
  // Both stay as backstops after Fragments::is_body made the rule asymmetric.
  // The empty case used to be routine — two bodies fused at a single NODE are
  // two face-connected fragments, each hanging off the other by one corner, and
  // the old "the fragment IS the mesh" spared neither — but the body of each
  // component is now protected by construction, so it takes a mesh that is
  // nothing BUT flaps to empty. The split case is a fragment that hangs by two
  // corners and is the only bridge between halves that hang by two corners
  // themselves; today they leave in the same pass rather than surviving as new
  // islands. Any loosening of the corner threshold makes both live again.
  if (total_dropped > 0 &&
      (out.tets.empty() ||
       element_component_count(out, stride, node_count) > components_before)) {
    const char *why = out.tets.empty() ? kEmptyRefusalNote : kSplitRefusalNote;
    out.tets.swap(before_drop);
    out.dropped_volume_mm3 = 0.0;
    total_dropped = 0;
    out.note = out.note.empty() ? std::string(why)
                                : out.note + "; " + std::string(why);
  }
  out.hanging_elems_dropped = total_dropped;
  out.n_elems = static_cast<int>(out.tets.size() / static_cast<size_t>(stride));
  // ── …and the islands the corner rule structurally cannot reach ────────────
  // Runs AFTER the hanging dissolve and its two guards, and keeps its own
  // accounting: this one REMOVES components on purpose, so it must never be
  // inside the guard that refuses a repair for adding them.
  out.debris_components_dissolved = 0;
  out.debris_elems_dropped = 0;
  out.debris_volume_mm3 = 0.0;
  dissolve_debris_components(out, stride, node_count);
  const int lonely_after = count_lonely_nodes(out);
  out.lonely_nodes_repaired = std::max(0, lonely_before - lonely_after);
  return total_dropped;
}

} // namespace detail
} // namespace chudware::sim
