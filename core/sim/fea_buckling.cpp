#include "sim/fea_buckling.h"
#include "sim/fea_eigen.h"
#include "sim/fea_element.h"
#include "sim/fea_static.h"

#include <Eigen/Dense>
#include <Eigen/SparseCholesky>
#include <algorithm>
#include <array>
#include <cmath>
#include <exception>
#include <iomanip>
#include <sstream>

namespace chudware::sim {
FeaBucklingResult fea_buckling_solve(
    const std::vector<double>& points, const std::vector<int>& tets,
    double E_mpa, double nu, const std::vector<int>& fixed_nodes,
    const std::vector<double>& nodal_forces, int n_modes,
    int nodes_per_elem, double max_load_factor, const SimCancelToken& cancel_token) {
    FeaBucklingResult result;
    auto refuse = [&](const std::string& error) {
        result.ok = false;
        result.n_modes = 0;
        result.load_factors.clear();
        result.mode_shapes.clear();
        result.error = "fea_buckling: " + error;
        return result;
    };
    auto cancelled = [&] {
        return cancel_token && cancel_token->load(std::memory_order_relaxed);
    };
    try {
        if (nodes_per_elem != 4 && nodes_per_elem != 10)
            return refuse("only tet4 and tet10 elements are supported");
        if (n_modes < 1 || n_modes > 100)
            return refuse("n_modes must be between 1 and 100");
        if (!std::isfinite(max_load_factor) || !(max_load_factor > 0.0) ||
            !std::isfinite(1.0/max_load_factor))
            return refuse("max_load_factor must be finite and positive");
        if (fixed_nodes.empty()) return refuse("reference equilibrium requires fixtures");
        if (cancelled()) return refuse("cancelled");
        // This also validates coordinates, connectivity, material, supports and forces,
        // and uses the same CHOLMOD/LDLT solver ladder as a static study.
        const auto reference = fea_static_solve(points, tets, E_mpa, nu, E_mpa,
            fixed_nodes, nodal_forces, nodes_per_elem, {}, cancel_token);
        if (!reference.ok) return refuse("reference static solve failed: " + reference.error);
        if (!reference.conditioning_warning.empty())
            return refuse("reference stiffness is not positive definite: " + reference.conditioning_warning);
        result.n_nodes = reference.n_nodes;
        result.n_dof = reference.n_dof;
        const int ndof = static_cast<int>(points.size());
        std::vector<int> reduced(ndof, -1);
        std::vector<bool> fixed(ndof, false), referenced(result.n_nodes, false);
        for (int node : tets) referenced[node] = true;
        for (int node : fixed_nodes)
            for (int axis = 0; axis < 3; ++axis) fixed[3 * node + axis] = true;
        int nfree = 0;
        for (int d = 0; d < ndof; ++d)
            if (!fixed[d] && referenced[d / 3]) reduced[d] = nfree++;
        if (nfree < n_modes + 2) return refuse("too few free degrees of freedom for requested modes");
        const double mu = E_mpa / (2.0 * (1.0 + nu));
        const double lam = E_mpa * nu / ((1.0 + nu) * (1.0 - 2.0 * nu));
        Eigen::Matrix<double, 6, 6> C = Eigen::Matrix<double, 6, 6>::Zero();
        C.topLeftCorner<3, 3>().setConstant(lam);
        C.diagonal().head<3>().array() += 2.0 * mu;
        C.diagonal().tail<3>().setConstant(mu);
        const int npe = nodes_per_elem, edof = 3 * npe;
        const int nelem = static_cast<int>(tets.size() / npe);
        std::vector<Eigen::Triplet<double>> kt, gt;
        kt.reserve(static_cast<size_t>(nelem) * edof * edof);
        gt.reserve(static_cast<size_t>(nelem) * npe * npe * 3);
        for (int e = 0; e < nelem; ++e) {
            if ((e & 255) == 0 && cancelled()) return refuse("cancelled");
            std::array<Eigen::Vector3d, 10> ep;
            Eigen::Matrix<double, 30, 1> ue = Eigen::Matrix<double, 30, 1>::Zero();
            for (int j = 0; j < npe; ++j) {
                const int node = tets[e * npe + j];
                ep[j] = Eigen::Map<const Eigen::Vector3d>(&points[3 * node]);
                ue.segment<3>(3 * j) = Eigen::Map<const Eigen::Vector3d>(&reference.displacement[3 * node]);
            }
            Eigen::Matrix<double, 30, 30> Ke = Eigen::Matrix<double, 30, 30>::Zero();
            Eigen::Matrix<double, 10, 10> Ge = Eigen::Matrix<double, 10, 10>::Zero();
            const int ng = npe == 10 ? 4 : 1;
            for (int g = 0; g < ng; ++g) {
                Eigen::Matrix<double, 6, 30> B;
                double weight = 0.0;
                if (!fea_element_gauss_b(npe, ep.data(), g, B, weight) ||
                    !std::isfinite(weight) || !(weight > 0.0))
                    return refuse("invalid element quadrature");
                const Eigen::Matrix<double, 6, 1> stress = C * (B * ue);
                Eigen::Matrix3d S;
                S << stress(0), stress(3), stress(5),
                     stress(3), stress(1), stress(4),
                     stress(5), stress(4), stress(2);
                // Initial-stress stiffness: delta_ab * integral(grad Ni . sigma . grad Nj).
                // All three normal and three shear stresses participate, including the
                // nonuniform prestress near fixtures; never substitute an Euler proxy.
                Eigen::Matrix<double, 10, 3> gradients = Eigen::Matrix<double, 10, 3>::Zero();
                for (int j = 0; j < npe; ++j)
                    gradients.row(j) << B(0, 3*j), B(1, 3*j+1), B(2, 3*j+2);
                Ke.noalias() += weight * B.transpose() * C * B;
                Ge.noalias() += weight * gradients * S * gradients.transpose();
                if (!stress.allFinite()) return refuse("non-finite reference stress");
            }
            for (int a = 0; a < edof; ++a) {
                const int ra = reduced[3 * tets[e*npe + a/3] + a%3];
                if (ra < 0) continue;
                for (int b = 0; b < edof; ++b) {
                    const int rb = reduced[3 * tets[e*npe + b/3] + b%3];
                    if (rb < 0) continue;
                    kt.emplace_back(ra, rb, Ke(a,b));
                    if (a%3 == b%3) gt.emplace_back(ra, rb, Ge(a/3,b/3));
                }
            }
        }
        Eigen::SparseMatrix<double> K(nfree,nfree), KG(nfree,nfree);
        K.setFromTriplets(kt.begin(),kt.end());
        KG.setFromTriplets(gt.begin(),gt.end());
        std::vector<Eigen::Triplet<double>>().swap(kt);
        std::vector<Eigen::Triplet<double>>().swap(gt);
        K.makeCompressed(); KG.makeCompressed();
        const double kd = K.norm(), gd = KG.norm();
        if (!(gd > 0.0) || !std::isfinite(kd/gd)) return refuse("zero geometric stiffness");
        // Sylvester inertia gives the count of mu < -epsilon without chasing
        // a cluster of tensile modes near zero. This is a decision on the FULL
        // assembled pencil, not a filter on local stresses or load directions.
        // This is the caller's engineering search ceiling, not a stress filter.
        const double zero_tolerance = 1.0/max_load_factor;
        int solve_modes = 0;
        {
            const Eigen::SparseMatrix<double> shifted = KG + zero_tolerance*K;
            Eigen::SimplicialLDLT<Eigen::SparseMatrix<double>> inertia(shifted);
            if (inertia.info() != Eigen::Success || !inertia.vectorD().allFinite())
                return refuse("geometric pencil inertia factorization failed");
            const int negative = static_cast<int>(
                (inertia.vectorD().array() < 0.0).count());
            if (negative == 0) {
                std::ostringstream reason;
                reason << std::setprecision(17)
                       << "no buckling mode below load factor " << max_load_factor
                       << "; reciprocal eigenvalue cutoff epsilon=" << zero_tolerance;
                return refuse(reason.str());
            }
            solve_modes = std::min(n_modes, negative);
        }
        // Solve the reciprocal pencil K_G phi = mu K phi. Its most negative
        // eigenvalues are the LOWEST positive load factors lambda=-1/mu. Unlike
        // the shifted buckling transform, it stays bounded near a tensile state.
        Eigen::VectorXd values;
        Eigen::MatrixXd vectors;
        if (cancelled()) return refuse("cancelled");
        if (!fea_shift_invert(K, KG, solve_modes, 0.0, true, values, vectors))
            return refuse("buckling eigenproblem did not converge");
        for (int m = 0; m < values.size(); ++m) {
            if (!(values(m) < -zero_tolerance)) continue;
            const double factor = -1.0/values(m);
            if (!std::isfinite(factor)) return refuse("non-finite buckling load factor");
            const Eigen::VectorXd ku = K * vectors.col(m);
            const Eigen::VectorXd gu = KG * vectors.col(m);
            const double residual = (ku + factor*gu).norm() /
                std::max(ku.norm() + factor*gu.norm(), 1e-30);
            if (!std::isfinite(residual) || residual > 1e-5)
                return refuse("buckling eigenpair residual exceeds tolerance");
            const double scale = vectors.col(m).cwiseAbs().maxCoeff();
            if (!(scale > 0.0)) return refuse("zero buckling mode");
            result.load_factors.push_back(factor);
            for (int d = 0; d < ndof; ++d)
                result.mode_shapes.push_back(reduced[d] < 0 ? 0.0 : vectors(reduced[d],m)/scale);
        }
        if (result.load_factors.empty())
            return refuse("no compressive load factor; minimum reciprocal eigenvalue " +
                          std::to_string(values.minCoeff()));
        result.n_modes = static_cast<int>(result.load_factors.size());
        result.ok = true;
        return result;
    } catch (const std::exception& e) {
        return refuse(e.what());
    }
}
}
