#pragma once

#include <Eigen/Core>

namespace chudware::sim {
// Shared static/buckling strain-displacement operator and physical quadrature weight.
// Coordinates contain nodes_per_elem entries; B uses its leading 3*npe columns.
bool fea_element_gauss_b(int nodes_per_elem, const Eigen::Vector3d* points, int gauss_point,
                         Eigen::Matrix<double, 6, 30>& B, double& weight);
}
