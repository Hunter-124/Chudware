#include "sim/mesher.h"

#include <algorithm>
#include <cmath>
#include <limits>
#include <string>

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

  for (size_t index = 0; index < element_count; ++index) {
    const int *element = &out.tets[index * static_cast<size_t>(stride)];
    for (int node = 0; node < stride; ++node) {
      if (element[node] < 0 || element[node] >= node_count) {
        out.error = "mesh quality: element " + std::to_string(index) +
                    " contains an invalid node index";
        return false;
      }
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
                  " is degenerate, inverted, or has a sign-changing Jacobian";
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

} // namespace detail
} // namespace chudware::sim
