#pragma once

#include <Eigen/Core>
#include <Eigen/SparseCore>

namespace chudware::sim {
// Shared factor ladder: modal returns eigenvalues of K phi=lambda M phi using
// shifted inversion; buckling treats M as K_G and returns the smallest reciprocal
// eigenvalues of K_G phi=mu K phi using the zero-shift K inverse, lambda=-1/mu.
// The factored stiffness must be SPD. False on factorization/convergence failure.
bool fea_shift_invert(const Eigen::SparseMatrix<double>& K,
                      const Eigen::SparseMatrix<double>& M, int n_modes,
                      double sigma, bool buckling,
                      Eigen::VectorXd& eigenvalues, Eigen::MatrixXd& eigenvectors);
}
