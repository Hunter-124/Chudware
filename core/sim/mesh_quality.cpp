#include "sim/mesher.h"

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

double tri_area(const std::vector<double> &p, int a, int b, int c) {
  const double *A = &p[static_cast<size_t>(a) * 3];
  const double *B = &p[static_cast<size_t>(b) * 3];
  const double *C = &p[static_cast<size_t>(c) * 3];
  const double ux = B[0] - A[0], uy = B[1] - A[1], uz = B[2] - A[2];
  const double vx = C[0] - A[0], vy = C[1] - A[1], vz = C[2] - A[2];
  const double cx = uy * vz - uz * vy;
  const double cy = uz * vx - ux * vz;
  const double cz = ux * vy - uy * vx;
  return 0.5 * std::sqrt(cx * cx + cy * cy + cz * cz);
}

struct TetQuality {
  double aspect;
  double sjac;
};

TetQuality tet_quality(const std::vector<double> &p, int a, int b, int c,
                       int d) {
  const int v[4] = {a, b, c, d};
  static constexpr int kFaces[4][3] = {
      {1, 2, 3}, {0, 2, 3}, {0, 1, 3}, {0, 1, 2}};
  static constexpr int kEdges[6][2] = {{0, 1}, {0, 2}, {0, 3},
                                       {1, 2}, {1, 3}, {2, 3}};
  static constexpr int kIncident[4][3] = {
      {1, 2, 3}, {0, 2, 3}, {0, 1, 3}, {0, 1, 2}};

  const double volume = std::abs(tet_volume6(p, a, b, c, d)) / 6.0;
  TetQuality quality{std::numeric_limits<double>::infinity(), 0.0};
  if (!(volume > 0.0))
    return quality;

  double longest = 0.0;
  for (const auto &edge : kEdges)
    longest = std::max(longest, edge_len(p, v[edge[0]], v[edge[1]]));

  double max_face = 0.0;
  for (const auto &face : kFaces)
    max_face =
        std::max(max_face, tri_area(p, v[face[0]], v[face[1]], v[face[2]]));

  if (max_face > 0.0 && longest > 0.0) {
    const double shortest_altitude = 3.0 * volume / max_face;
    if (shortest_altitude > 0.0)
      quality.aspect = (longest / shortest_altitude) / 1.224744871391589;
  }

  const double det6 = 6.0 * volume;
  double minimum = std::numeric_limits<double>::infinity();
  for (int corner = 0; corner < 4; ++corner) {
    double product = 1.0;
    for (int i = 0; i < 3; ++i)
      product *= edge_len(p, v[corner], v[kIncident[corner][i]]);
    if (product > 0.0)
      minimum = std::min(minimum, std::sqrt(2.0) * det6 / product);
  }
  quality.sjac = std::isfinite(minimum) ? minimum : 0.0;
  return quality;
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
  std::vector<int> parent(static_cast<size_t>(node_count));
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

    const TetQuality quality =
        tet_quality(out.points, element[0], element[1], element[2], element[3]);
    if (!std::isfinite(quality.aspect) || !std::isfinite(quality.sjac) ||
        !(quality.sjac > 0.0)) {
      out.error = "mesh quality: element " + std::to_string(index) +
                  " has invalid quality metrics";
      return false;
    }
    minimum_aspect = std::min(minimum_aspect, quality.aspect);
    maximum_aspect = std::max(maximum_aspect, quality.aspect);
    aspect_sum += quality.aspect;
    minimum_jacobian = std::min(minimum_jacobian, quality.sjac);
    if (quality.aspect > kPoorAspect)
      ++poor;
    int bucket =
        static_cast<int>((quality.aspect - 1.0) / (kPoorAspect - 1.0) * 10.0);
    out.aspect_hist[static_cast<size_t>(std::clamp(bucket, 0, 9))]++;
  }

  // A node reachable from no element is its own component: it carries zero
  // stiffness, so counting it is not pedantry — it is the same singular system
  // a detached island is.
  int components = 0;
  for (int node = 0; node < node_count; ++node)
    if (find(node) == node)
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

} // namespace detail
} // namespace chudware::sim
