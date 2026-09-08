// Modal FEA solver implementation — see fea_modal.h.
//
// Reuses the static solver's tet4 stiffness; adds the CONSISTENT tet4 mass matrix and a
// shift-invert generalized eigensolve. The eigenvalue->frequency conversion + the
// unit-max mode-shape normalization mirror engine/sim/fea_modal.py exactly.

#include "sim/fea_modal.h"
#include "sim/fea_eigen.h"

#include "sim/solver_threads.h"

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdlib>
#include <stdexcept>
#include <string>
#include <thread>

#include <Eigen/Dense>
#include <Eigen/SparseCore>
#include <Eigen/SparseCholesky>
#ifdef CHUDWARE_WITH_CHOLMOD
#include <Eigen/CholmodSupport>  // Eigen::CholmodSupernodalLLT (SuiteSparse BLAS3 Cholesky)
#endif
#ifdef CHUDWARE_HAVE_OPENMP
#include <omp.h>
#endif

#include <Spectra/MatOp/SparseSymMatProd.h>
#include <Spectra/SymGEigsSolver.h>
#include <Spectra/SymGEigsShiftSolver.h>

namespace chudware::sim {

namespace {

constexpr double kPi = 3.14159265358979323846;

#ifdef CHUDWARE_WITH_CHOLMOD
// Spectra shift-invert op backed by CHOLMOD supernodal LLT (P2). Spectra's stock
// SymShiftInvert factorizes (K - sigma*M) with Eigen's scalar single-threaded LDLT —
// the dominant modal cost on real meshes. sigma < 0 here (see the shift note below),
// so K - sigma*M = K + |sigma|*M is strictly SPD and CHOLMOD's LLT applies. Throws on
// factorization failure so the caller can fall back to the stock LDLT op.
class CholmodTriangularFactor
    : public Eigen::CholmodSupernodalLLT<Eigen::SparseMatrix<double>> {
public:
    ~CholmodTriangularFactor() {
        cholmod_free_dense(&solution_, &context());
        cholmod_free_dense(&work_y_, &context());
        cholmod_free_dense(&work_e_, &context());
    }
    void prepare_cholesky() { scratch_.resize(rows()); }
    void lower_triangular_solve(const double* input, double* output) const {
        const auto* permutation = static_cast<const int*>(m_cholmodFactor->Perm);
        for (Eigen::Index i = 0; i < rows(); ++i) scratch_[i] = input[permutation[i]];
        triangular_solve(CHOLMOD_L, scratch_.data(), output);
    }
    void upper_triangular_solve(const double* input, double* output) const {
        triangular_solve(CHOLMOD_Lt, input, scratch_.data());
        const auto* permutation = static_cast<const int*>(m_cholmodFactor->Perm);
        for (Eigen::Index i = 0; i < rows(); ++i) output[permutation[i]] = scratch_[i];
    }
private:
    cholmod_common& context() const {
        return const_cast<CholmodTriangularFactor*>(this)->cholmod();
    }
    void triangular_solve(int system, const double* input, double* output) const {
        cholmod_dense rhs{};
        rhs.nrow = rhs.nzmax = rhs.d = static_cast<size_t>(rows());
        rhs.ncol = 1;
        rhs.x = const_cast<double*>(input);
        rhs.xtype = CHOLMOD_REAL;
        rhs.dtype = CHOLMOD_DOUBLE;
        if (!cholmod_solve2(system, m_cholmodFactor, &rhs, nullptr, &solution_,
                            nullptr, &work_y_, &work_e_, &context()) || !solution_)
            throw std::runtime_error("CHOLMOD triangular solve failed");
        std::copy_n(static_cast<const double*>(solution_->x), rows(), output);
    }
    mutable Eigen::VectorXd scratch_;
    mutable cholmod_dense* solution_ = nullptr;
    mutable cholmod_dense* work_y_ = nullptr;
    mutable cholmod_dense* work_e_ = nullptr;
};

class CholmodShiftInvert {
public:
    using Scalar = double;
    CholmodShiftInvert(const Eigen::SparseMatrix<double>& K,
                       const Eigen::SparseMatrix<double>& M)
        : K_(K), M_(M) {}
    Eigen::Index rows() const { return K_.rows(); }
    Eigen::Index cols() const { return K_.cols(); }
    void set_shift(const double& sigma) {
        if (sigma == 0.0) llt_.compute(K_);
        else {
            Eigen::SparseMatrix<double> A = K_ - sigma * M_;
            llt_.compute(A);
        }
        if (llt_.info() != Eigen::Success)
            throw std::runtime_error("CHOLMOD LLT of (K - sigma*M) failed");
    }
    void perform_op(const double* x_in, double* y_out) const {
        Eigen::Map<const Eigen::VectorXd> x(x_in, K_.rows());
        Eigen::Map<Eigen::VectorXd> y(y_out, K_.rows());
        y = llt_.solve(x);
    }
    void prepare_cholesky() { llt_.prepare_cholesky(); }
    void lower_triangular_solve(const double* input, double* output) const {
        llt_.lower_triangular_solve(input, output);
    }
    void upper_triangular_solve(const double* input, double* output) const {
        llt_.upper_triangular_solve(input, output);
    }

private:
    const Eigen::SparseMatrix<double>& K_;
    const Eigen::SparseMatrix<double>& M_;
    CholmodTriangularFactor llt_;
};
#endif

class LdltShiftInvert {
public:
    using Scalar = double;
    LdltShiftInvert(const Eigen::SparseMatrix<double>& K,
                    const Eigen::SparseMatrix<double>& M) : K_(K), M_(M) {}
    Eigen::Index rows() const { return K_.rows(); }
    Eigen::Index cols() const { return K_.cols(); }
    void set_shift(const double& sigma) {
        if (sigma == 0.0) factor_.compute(K_);
        else {
            Eigen::SparseMatrix<double> A = K_ - sigma * M_;
            factor_.compute(A);
        }
        if (factor_.info() != Eigen::Success || !factor_.vectorD().allFinite() ||
            (factor_.vectorD().array() <= 0.0).any())
            throw std::runtime_error("shifted stiffness is not positive definite");
    }
    void perform_op(const double* input, double* output) const {
        Eigen::Map<const Eigen::VectorXd> x(input, rows());
        Eigen::Map<Eigen::VectorXd> y(output, rows());
        y = factor_.solve(x);
    }
    void prepare_cholesky() {
        inverse_sqrt_diagonal_ = factor_.vectorD().array().sqrt().inverse();
    }
    void lower_triangular_solve(const double* input, double* output) const {
        Eigen::Map<const Eigen::VectorXd> x(input, rows());
        Eigen::Map<Eigen::VectorXd> y(output, rows());
        y = factor_.permutationP() * x;
        factor_.matrixL().solveInPlace(y);
        y.array() *= inverse_sqrt_diagonal_.array();
    }
    void upper_triangular_solve(const double* input, double* output) const {
        Eigen::Map<const Eigen::VectorXd> x(input, rows());
        Eigen::Map<Eigen::VectorXd> y(output, rows());
        y = x.array() * inverse_sqrt_diagonal_.array();
        factor_.matrixU().solveInPlace(y);
        y = factor_.permutationPinv() * y;
    }
private:
    const Eigen::SparseMatrix<double>& K_;
    const Eigen::SparseMatrix<double>& M_;
    Eigen::SimplicialLDLT<Eigen::SparseMatrix<double>> factor_;
    Eigen::VectorXd inverse_sqrt_diagonal_;
};

template <class Op>
bool solve_shifted(Op& op, Spectra::SparseSymMatProd<double>& metric, int modes,
                   double sigma, Eigen::VectorXd& values, Eigen::MatrixXd& vectors) {
    const int ncv = std::min<int>(op.rows(), std::max(3 * modes + 1, 30));
    Spectra::SymGEigsShiftSolver<Op, Spectra::SparseSymMatProd<double>,
                                 Spectra::GEigsMode::ShiftInvert>
        eigs(op, metric, modes, ncv, sigma);
    eigs.init();
    eigs.compute(Spectra::SortRule::LargestMagn,
                 10 * op.rows(), 1e-10, Spectra::SortRule::SmallestAlge);
    if (eigs.info() != Spectra::CompInfo::Successful) return false;
    values = eigs.eigenvalues();
    vectors = eigs.eigenvectors();
    return values.allFinite() && vectors.allFinite();
}


template <class Inverse>
bool solve_buckling_reciprocal(Inverse& inverse, const Eigen::SparseMatrix<double>& K,
                               const Eigen::SparseMatrix<double>& KG, int modes,
                               Eigen::VectorXd& values, Eigen::MatrixXd& vectors) {
    // K_G phi = mu K phi, lambda = -1/mu. The zero-shift K inverse uses the
    // modal factor ladder, but the bounded reciprocal spectrum avoids the
    // lambda/(lambda-sigma) cluster at one in a non-compressive reference state.
    inverse.set_shift(0.0);
    inverse.prepare_cholesky();
    Spectra::SparseSymMatProd<double> geometric(KG);
    const int ncv = std::min<int>(K.rows(), std::max(3 * modes + 1, 60));
    Spectra::SymGEigsSolver<Spectra::SparseSymMatProd<double>, Inverse,
                            Spectra::GEigsMode::Cholesky>
        eigs(geometric, inverse, modes, ncv);
    eigs.init();
    eigs.compute(Spectra::SortRule::SmallestAlge, 1000, 1e-10,
                 Spectra::SortRule::SmallestAlge);
    if (eigs.info() != Spectra::CompInfo::Successful) return false;
    values = eigs.eigenvalues();
    vectors = eigs.eigenvectors();
    return values.allFinite() && vectors.allFinite();
}

void lame(double E, double nu, double& lam, double& mu) {
    lam = E * nu / ((1.0 + nu) * (1.0 - 2.0 * nu));
    mu = E / (2.0 * (1.0 + nu));
}

Eigen::Matrix<double, 6, 6> elasticity(double lam, double mu) {
    Eigen::Matrix<double, 6, 6> C = Eigen::Matrix<double, 6, 6>::Zero();
    C(0, 0) = C(1, 1) = C(2, 2) = lam + 2 * mu;
    C(0, 1) = C(0, 2) = C(1, 0) = C(1, 2) = C(2, 0) = C(2, 1) = lam;
    C(3, 3) = C(4, 4) = C(5, 5) = mu;
    return C;
}

bool tet4_B(const std::array<Eigen::Vector3d, 4>& ep, Eigen::Matrix<double, 6, 12>& B,
            double& vol) {
    Eigen::Matrix4d T;
    for (int j = 0; j < 4; ++j) {
        T(j, 0) = 1.0; T(j, 1) = ep[j].x(); T(j, 2) = ep[j].y(); T(j, 3) = ep[j].z();
    }
    vol = std::abs(T.determinant()) / 6.0;
    if (vol <= 0.0) return false;
    Eigen::Matrix4d Tinv = T.inverse();
    if (!Tinv.allFinite()) { vol = 0.0; return false; }
    Eigen::Matrix<double, 3, 4> dN = Tinv.block<3, 4>(1, 0);
    B.setZero();
    for (int j = 0; j < 4; ++j) {
        int col = j * 3;
        B(0, col + 0) = dN(0, j);
        B(1, col + 1) = dN(1, j);
        B(2, col + 2) = dN(2, j);
        B(3, col + 0) = dN(1, j); B(3, col + 1) = dN(0, j);
        B(4, col + 1) = dN(2, j); B(4, col + 2) = dN(1, j);
        B(5, col + 0) = dN(2, j); B(5, col + 2) = dN(0, j);
    }
    return true;
}

// ── tet10 (quadratic) element helpers ───────────────────────────────────────────────
// Node ordering MIRRORS fea_static.cpp's validated tet10_dN exactly (corners 0-3, then
// edges {0,1},{1,2},{2,0},{3,0},{3,2},{3,1}) so the stiffness here matches the static
// solver and the consistent mass below uses the SAME shape functions.
Eigen::Matrix<double, 10, 3> tet10_dN(double r, double s, double t) {
    const double L0 = 1.0 - r - s - t, L1 = r, L2 = s, L3 = t;
    const Eigen::Vector3d g0(-1.0, -1.0, -1.0), g1(1.0, 0.0, 0.0),
        g2(0.0, 1.0, 0.0), g3(0.0, 0.0, 1.0);
    Eigen::Matrix<double, 10, 3> dN;
    dN.row(0) = (4.0 * L0 - 1.0) * g0.transpose();
    dN.row(1) = (4.0 * L1 - 1.0) * g1.transpose();
    dN.row(2) = (4.0 * L2 - 1.0) * g2.transpose();
    dN.row(3) = (4.0 * L3 - 1.0) * g3.transpose();
    dN.row(4) = (4.0 * (L0 * g1 + L1 * g0)).transpose();  // edge {0,1}
    dN.row(5) = (4.0 * (L1 * g2 + L2 * g1)).transpose();  // edge {1,2}
    dN.row(6) = (4.0 * (L2 * g0 + L0 * g2)).transpose();  // edge {2,0}
    dN.row(7) = (4.0 * (L3 * g0 + L0 * g3)).transpose();  // edge {3,0}
    dN.row(8) = (4.0 * (L3 * g2 + L2 * g3)).transpose();  // edge {3,2}
    dN.row(9) = (4.0 * (L3 * g1 + L1 * g3)).transpose();  // edge {3,1}
    return dN;
}

// 4-point degree-2 tet rule (exact for the tet10 stiffness integrand B^T C B).
struct TetGauss4 {
    double pt[4][3];
    double w[4];
    TetGauss4() {
        const double a = 0.5854101966249685, b = 0.1381966011250105;
        const double P[4][3] = {{a, b, b}, {b, a, b}, {b, b, a}, {b, b, b}};
        for (int g = 0; g < 4; ++g) {
            for (int k = 0; k < 3; ++k) pt[g][k] = P[g][k];
            w[g] = 1.0 / 24.0;
        }
    }
};

bool tet10_B_at(const std::array<Eigen::Vector3d, 10>& ep, double r, double s, double t,
                Eigen::Matrix<double, 6, 30>& B, double& detJ) {
    const Eigen::Matrix<double, 10, 3> dNrst = tet10_dN(r, s, t);
    Eigen::Matrix3d J = Eigen::Matrix3d::Zero();
    for (int n = 0; n < 10; ++n) J += ep[n] * dNrst.row(n);
    detJ = J.determinant();
    if (!(std::abs(detJ) > 0.0)) return false;
    Eigen::Matrix3d Jinv = J.inverse();
    if (!Jinv.allFinite()) return false;
    const Eigen::Matrix<double, 10, 3> dNxyz = dNrst * Jinv;
    B.setZero();
    for (int n = 0; n < 10; ++n) {
        const int col = n * 3;
        const double dx = dNxyz(n, 0), dy = dNxyz(n, 1), dz = dNxyz(n, 2);
        B(0, col + 0) = dx;
        B(1, col + 1) = dy;
        B(2, col + 2) = dz;
        B(3, col + 0) = dy; B(3, col + 1) = dx;
        B(4, col + 1) = dz; B(4, col + 2) = dy;
        B(5, col + 0) = dz; B(5, col + 2) = dx;
    }
    return true;
}


// Consistent tet10 mass for a quadratic isoparametric element. For curved elements det(J)
// varies over the element and cannot be pulled out as one scalar volume multiplier: doing so
// preserves total mass but puts it on the wrong nodes. A 5x5x5 Gauss-Legendre Duffy rule is
// degree-7 exact after the cube-to-tet Jacobian, covering N_i*N_j (degree 4) times detJ
// (degree <=3). Straight elements produce the same closed-form matrix to roundoff.
Eigen::Matrix<double, 10, 10> tet10_mass_consistent(
    double rho, const std::array<Eigen::Vector3d, 10>& ep, const TetGauss4&) {
    static constexpr double x[5] = {
        0.0469100770306680, 0.2307653449471585, 0.5,
        0.7692346550528415, 0.9530899229693320};
    static constexpr double w[5] = {
        0.1184634425280945, 0.2393143352496832, 0.2844444444444444,
        0.2393143352496832, 0.1184634425280945};
    Eigen::Matrix<double, 10, 10> M = Eigen::Matrix<double, 10, 10>::Zero();
    double jacobian_sign = 0.0;
    for (int iu = 0; iu < 5; ++iu)
        for (int iv = 0; iv < 5; ++iv)
            for (int iw = 0; iw < 5; ++iw) {
                const double u = x[iu], v = x[iv], q = x[iw];
                const double r = u;
                const double s = (1.0 - u) * v;
                const double t = (1.0 - u) * (1.0 - v) * q;
                const double L[4] = {1.0 - r - s - t, r, s, t};
                Eigen::Matrix<double, 10, 1> N;
                for (int i = 0; i < 4; ++i) N(i) = L[i] * (2.0 * L[i] - 1.0);
                N(4) = 4.0 * L[0] * L[1];
                N(5) = 4.0 * L[1] * L[2];
                N(6) = 4.0 * L[2] * L[0];
                N(7) = 4.0 * L[3] * L[0];
                N(8) = 4.0 * L[3] * L[2];
                N(9) = 4.0 * L[3] * L[1];
                const Eigen::Matrix<double, 10, 3> dNrst = tet10_dN(r, s, t);
                Eigen::Matrix3d J = Eigen::Matrix3d::Zero();
                for (int n = 0; n < 10; ++n) J += ep[n] * dNrst.row(n);
                const double detJ = J.determinant();
                if (!std::isfinite(detJ) || !(std::abs(detJ) > 0.0) ||
                    (jacobian_sign != 0.0 &&
                     jacobian_sign * detJ <= 0.0))
                    return Eigen::Matrix<double, 10, 10>::Zero();
                jacobian_sign = detJ;
                const double dV = std::abs(detJ) * (1.0 - u) * (1.0 - u) *
                                  (1.0 - v) * w[iu] * w[iv] * w[iw];
                M.noalias() += (rho * dV) * (N * N.transpose());
            }
    return M;
}

}  // namespace

bool fea_shift_invert(const Eigen::SparseMatrix<double>& K,
                      const Eigen::SparseMatrix<double>& M, int n_modes,
                      double sigma, bool buckling,
                      Eigen::VectorXd& values, Eigen::MatrixXd& vectors) {
    cap_solver_threads_for_host();
    Spectra::SparseSymMatProd<double> metric(buckling ? K : M);
    auto solve = [&](auto& op) {
        return buckling
            ? solve_buckling_reciprocal(op, K, M, n_modes, values, vectors)
            : solve_shifted(op, metric, n_modes, sigma, values, vectors);
    };
#ifdef CHUDWARE_WITH_CHOLMOD
    try {
        CholmodShiftInvert op(K, M);
        if (solve(op)) return true;
    } catch (const std::exception&) {}
#endif
    try {
        LdltShiftInvert op(K, M);
        return solve(op);
    } catch (const std::exception&) {
        return false;
    }
}

FeaModalResult fea_modal_solve(const std::vector<double>& points, const std::vector<int>& tets,
                               double E_mpa, double nu, double density_g_cm3,
                               const std::vector<int>& fixed_nodes, int n_modes,
                               int nodes_per_elem, const SimCancelToken& cancel_token) {
    FeaModalResult res;
    const int n_nodes = static_cast<int>(points.size() / 3);
    const int n_dof = n_nodes * 3;
    res.n_nodes = n_nodes;
    res.n_dof = n_dof;
    auto cancelled = [&]() {
        return cancel_token && cancel_token->load(std::memory_order_relaxed);
    };
    auto cancel_result = [&]() {
        res.error = "fea_modal: cancelled";
        return res;
    };
    if (cancelled()) return cancel_result();
    if (n_nodes == 0 || tets.empty()) { res.error = "fea_modal: empty mesh"; return res; }
    if (points.size() % 3 != 0) {
        res.error = "fea_modal: points length must be divisible by 3";
        return res;
    }
    if (nodes_per_elem != 4 && nodes_per_elem != 10) {
        res.error = "fea_modal: nodes_per_elem must be 4 (tet4) or 10 (tet10)";
        return res;
    }
    if (tets.size() % static_cast<size_t>(nodes_per_elem) != 0) {
        res.error = "fea_modal: tets length must be divisible by nodes_per_elem";
        return res;
    }
    if (n_modes <= 0) {
        res.error = "fea_modal: n_modes must be > 0";
        return res;
    }
    if (!std::isfinite(E_mpa) || E_mpa <= 0.0 || !std::isfinite(nu) || nu <= -1.0 ||
        nu >= 0.5 || !std::isfinite(density_g_cm3) || density_g_cm3 <= 0.0) {
        res.error = "fea_modal: material requires finite E>0, -1<nu<0.5 and density>0";
        return res;
    }
    for (double x : points) {
        if (!std::isfinite(x)) {
            res.error = "fea_modal: mesh coordinates must be finite";
            return res;
        }
    }
    for (int nid : tets) {
        if (nid < 0 || nid >= n_nodes) {
            res.error = "fea_modal: element node index out of range";
            return res;
        }
    }
    for (int nid : fixed_nodes) {
        if (nid < 0 || nid >= n_nodes) {
            res.error = "fea_modal: fixed node index out of range";
            return res;
        }
    }

    double lam, mu;
    lame(E_mpa, nu, lam, mu);
    const Eigen::Matrix<double, 6, 6> C = elasticity(lam, mu);
    if (!C.allFinite()) {
        res.error = "fea_modal: material constants overflowed";
        return res;
    }
    const double density = density_g_cm3 * 1e-3;  // g/cm^3 -> g/mm^3

    auto node_pt = [&](int nid) {
        return Eigen::Vector3d(points[nid * 3], points[nid * 3 + 1], points[nid * 3 + 2]);
    };
    const int n_tets = static_cast<int>(tets.size() / nodes_per_elem);

    std::vector<char> fixed(n_dof, 0);
    for (int nid : fixed_nodes)
        if (nid >= 0 && nid < n_nodes)
            for (int k = 0; k < 3; ++k) fixed[nid * 3 + k] = 1;
    std::vector<int> reduced(n_dof, -1);
    int n_free = 0;
    for (int d = 0; d < n_dof; ++d)
        if (!fixed[d]) reduced[d] = n_free++;
    if (n_free < 3) { res.error = "fea_modal: too few free DOFs"; return res; }

    // Reduced free-free K + CONSISTENT M. A consistent mass matrix (vs the old diagonal-
    // lumped one) restores the near-degenerate first bending PAIR on a square cross-section
    // and removes the systematic low-frequency bias a lumped mass introduces on a coarse
    // tet mesh. For tet4 the consistent block for a node pair (a,b) is rho*V/20*(1+delta_ab);
    // for tet10 (2nd-order) both K and M are integrated/derived from the quadratic shape
    // functions — far more accurate in bending, which is the real cure for the square-section
    // split + low reading (Bug N-1).
    std::vector<Eigen::Triplet<double>> trips;
    std::vector<Eigen::Triplet<double>> mtrips;
    const int npe = nodes_per_elem;
    trips.reserve(static_cast<std::size_t>(n_tets) * (npe == 10 ? 900 : 144));
    mtrips.reserve(static_cast<std::size_t>(n_tets) * (npe == 10 ? 300 : 48));
    if (npe == 4) {
        for (int e = 0; e < n_tets; ++e) {
            std::array<Eigen::Vector3d, 4> ep;
            std::array<int, 4> tn;
            for (int j = 0; j < 4; ++j) { tn[j] = tets[e * 4 + j]; ep[j] = node_pt(tn[j]); }
            Eigen::Matrix<double, 6, 12> B;
            double vol;
            if (!tet4_B(ep, B, vol) || !std::isfinite(vol)) {
                res.error = "fea_modal: degenerate element " + std::to_string(e);
                return res;
            }
            Eigen::Matrix<double, 12, 12> Ke = B.transpose() * C * B * vol;
            int dof_map[12];
            for (int j = 0; j < 4; ++j)
                for (int k = 0; k < 3; ++k) dof_map[j * 3 + k] = tn[j] * 3 + k;
            for (int a = 0; a < 12; ++a) {
                int ra = reduced[dof_map[a]];
                if (ra < 0) continue;
                for (int b = 0; b < 12; ++b) {
                    int rb = reduced[dof_map[b]];
                    if (rb < 0) continue;
                    trips.emplace_back(ra, rb, Ke(a, b));
                }
            }
            // Consistent mass: block(a,b) = rho*V/20 * (2 if a==b else 1) on each of the 3
            // translational DOFs (the mass couples only same-direction DOFs).
            const double m0 = density * vol / 20.0;
            for (int a = 0; a < 4; ++a)
                for (int b = 0; b < 4; ++b) {
                    const double coef = m0 * (a == b ? 2.0 : 1.0);
                    for (int k = 0; k < 3; ++k) {
                        int ra = reduced[tn[a] * 3 + k];
                        int rb = reduced[tn[b] * 3 + k];
                        if (ra < 0 || rb < 0) continue;
                        mtrips.emplace_back(ra, rb, coef);
                    }
                }
            if ((e & 255) == 0 && cancelled()) return cancel_result();
        }
    } else {  // tet10
        const TetGauss4 gauss;
        for (int e = 0; e < n_tets; ++e) {
            std::array<Eigen::Vector3d, 10> ep;
            std::array<int, 10> tn;
            for (int j = 0; j < 10; ++j) { tn[j] = tets[e * 10 + j]; ep[j] = node_pt(tn[j]); }
            // Stiffness Ke (30x30): sum_g w_g * detJ_g * B_g^T C B_g.
            Eigen::Matrix<double, 30, 30> Ke = Eigen::Matrix<double, 30, 30>::Zero();
            bool ok = true;
            double jacobian_sign = 0.0;
            for (int g = 0; g < 4; ++g) {
                Eigen::Matrix<double, 6, 30> B;
                double detJ;
                if (!tet10_B_at(ep, gauss.pt[g][0], gauss.pt[g][1],
                                gauss.pt[g][2], B, detJ) ||
                    !std::isfinite(detJ) ||
                    (jacobian_sign != 0.0 &&
                     jacobian_sign * detJ <= 0.0)) {
                    ok = false;
                    break;
                }
                jacobian_sign = detJ;
                // |detJ|: the element connectivity may be negatively oriented (a CW tet);
                // the physical volume is positive, so use the magnitude (mirrors fea_static).
                Ke += (gauss.w[g] * std::abs(detJ)) * (B.transpose() * C * B);
            }
            if (!ok) {
                res.error = "fea_modal: degenerate or inverted element " + std::to_string(e);
                return res;
            }
            // Consistent mapped mass is integrated with a degree-7-exact Duffy
            // rule. Its helper also rejects a non-finite, degenerate, or
            // sign-changing Jacobian anywhere in that denser integration grid.
            const Eigen::Matrix<double, 10, 10> Ms =
                tet10_mass_consistent(density, ep, gauss);
            if (!(Ms.trace() > 0.0) || !Ms.allFinite()) {
                res.error = "fea_modal: invalid mass matrix for element " + std::to_string(e);
                return res;
            }
            int dof_map[30];
            for (int j = 0; j < 10; ++j)
                for (int k = 0; k < 3; ++k) dof_map[j * 3 + k] = tn[j] * 3 + k;
            for (int a = 0; a < 30; ++a) {
                int ra = reduced[dof_map[a]];
                if (ra < 0) continue;
                for (int b = 0; b < 30; ++b) {
                    int rb = reduced[dof_map[b]];
                    if (rb < 0) continue;
                    trips.emplace_back(ra, rb, Ke(a, b));
                }
            }
            for (int a = 0; a < 10; ++a)
                for (int b = 0; b < 10; ++b) {
                    const double coef = Ms(a, b);
                    for (int k = 0; k < 3; ++k) {
                        int ra = reduced[tn[a] * 3 + k];
                        int rb = reduced[tn[b] * 3 + k];
                        if (ra < 0 || rb < 0) continue;
                        mtrips.emplace_back(ra, rb, coef);
                    }
                }
            if ((e & 255) == 0 && cancelled()) return cancel_result();
        }
    }
    Eigen::SparseMatrix<double> Kff(n_free, n_free);
    Kff.setFromTriplets(trips.begin(), trips.end());
    Kff.makeCompressed();
    Eigen::SparseMatrix<double> Mff(n_free, n_free);
    Mff.setFromTriplets(mtrips.begin(), mtrips.end());
    Mff.makeCompressed();
    if (Kff.nonZeros() == 0 || Mff.nonZeros() == 0) {
        res.error = "fea_modal: no valid element stiffness or mass";
        return res;
    }
    if (cancelled()) return cancel_result();

    // Shift-invert generalized eigensolve for the lowest modes. A SMALL NEGATIVE spectral
    // shift sigma makes (K - sigma*M) = (K + |sigma|*M) strictly positive-definite, so the
    // shift-invert factorization is robust even when K is ill-conditioned (a slender tet10
    // beam can hand an exactly-zero shift a non-positive LDLT pivot and fail). Any sigma < 0
    // still targets the smallest-positive eigenvalues (they remain the ones closest to it),
    // and Spectra returns the TRUE generalized eigenvalues, so frequencies are unbiased. We
    // scale |sigma| to the spectrum via the average K/M diagonal ratio (~ the eigenvalue
    // scale) with a tiny coefficient — enough to dominate roundoff, far below the 1st mode.
    int k_req = std::min(n_modes, n_free - 2);
    if (k_req < 1) k_req = 1;

    // FREE-FREE (no fixed DOFs): the zero eigenvalue is 6-FOLD DEGENERATE (3 translations +
    // 3 rotations). Shift-invert Lanczos converges degenerate clusters only when the Krylov
    // subspace is comfortably larger than the cluster; with k_req = n_modes and ncv = 2k+1
    // it routinely converged only PART of the rigid cluster and silently substituted higher
    // elastic modes into the "lowest k" list (observed: n_modes=8 on a slender tet10 beam
    // returned 3 rigid + 5 elastic — the C4 defect, previously papered over caller-side in
    // cad_tools). Fix where it belongs: over-request so the subspace can resolve all 6
    // rigid modes, VERIFY the rigid count, enlarge + retry if short, then truncate the
    // ascending list back to the caller's k_req.
    const bool free_free = fixed_nodes.empty();
    int k_solve = free_free ? std::min(std::max(k_req + 6, 14), n_free - 2) : k_req;
    if (k_solve < k_req) k_solve = k_req;

    double kdiag = 0.0, mdiag = 0.0;
    for (int i = 0; i < n_free; ++i) { kdiag += Kff.coeff(i, i); mdiag += Mff.coeff(i, i); }
    const double evscale = (mdiag > 0.0) ? kdiag / mdiag : 1.0;
    const double sigma = -1e-5 * evscale;
    if (!std::isfinite(kdiag) || !std::isfinite(mdiag) || !(kdiag > 0.0) ||
        !(mdiag > 0.0) || !std::isfinite(evscale) || !(evscale > 0.0)) {
        res.error = "fea_modal: assembled stiffness/mass is non-finite or degenerate";
        return res;
    }

    Eigen::VectorXd evals;
    Eigen::MatrixXd evecs;
    auto eigensolve = [&](int ks) {
        return fea_shift_invert(Kff, Mff, ks, sigma, false, evals, evecs);
    };
    const double rigid_eigenvalue_tol = 1e-8 * evscale;
    auto n_rigid = [&] {
        int n = 0;
        for (int i = 0; i < evals.size(); ++i)
            if (std::abs(evals(i)) <= rigid_eigenvalue_tol) ++n;
        return n;
    };
    if (!eigensolve(k_solve)) {
        res.error = "fea_modal: eigensolve did not converge";
        return res;
    }
    if (cancelled()) return cancel_result();
    if (free_free) {
        for (int attempt = 0; attempt < 2 && n_rigid() < std::min(6, k_solve); ++attempt) {
            const int bigger = std::min(k_solve + 6, n_free - 2);
            if (bigger <= k_solve) break;
            k_solve = bigger;
            if (!eigensolve(k_solve)) break;  // keep the last good result
        }
    }
    if (free_free && n_rigid() < 6) {
        res.error = "fea_modal: eigensolve did not resolve all 6 rigid-body modes";
        return res;
    }
    if (evals.size() < k_req || evecs.cols() < k_req || !evals.allFinite() ||
        !evecs.allFinite()) {
        res.error = "fea_modal: eigensolve returned incomplete or non-finite modes";
        return res;
    }
    const double negative_tol = 1e-9 * evscale;
    for (int i = 0; i < evals.size(); ++i) {
        if (evals(i) < -negative_tol) {
            res.error = "fea_modal: materially negative eigenvalue (unstable/invalid model)";
            return res;
        }
    }
    const int k = std::min(static_cast<int>(evals.size()), k_req);  // truncate over-request

    // omega[rad/s] = sqrt(max(eig,0) * 1e6); freq[Hz] = omega / 2pi. Rank EVERY computed
    // mode ascending, then emit only the k lowest (drops the over-requested tail).
    std::vector<std::pair<double, int>> order;
    order.reserve(static_cast<size_t>(evals.size()));
    for (int i = 0; i < static_cast<int>(evals.size()); ++i) {
        double ev = std::max(evals(i), 0.0);
        double freq = std::sqrt(ev * 1e6) / (2.0 * kPi);
        order.emplace_back(freq, i);
    }
    std::sort(order.begin(), order.end(),
              [](const auto& a, const auto& b) { return a.first < b.first; });

    res.n_modes = k;
    res.freqs_hz.resize(k);
    res.mode_shapes.assign(static_cast<std::size_t>(k) * n_nodes * 3, 0.0);
    for (int m = 0; m < k; ++m) {
        res.freqs_hz[m] = order[m].first;
        int src = order[m].second;
        // Scatter the reduced eigenvector to full DOFs, normalize by unit max |u|.
        std::vector<double> u_full(n_dof, 0.0);
        double scale = 0.0;
        for (int d = 0; d < n_dof; ++d)
            if (reduced[d] >= 0) {
                double v = evecs(reduced[d], src);
                u_full[d] = v;
                scale = std::max(scale, std::abs(v));
            }
        double inv = scale > 0.0 ? 1.0 / scale : 1.0;
        if (!(scale > 0.0) || !std::isfinite(scale)) {
            res.error = "fea_modal: eigenvector normalization failed";
            res.freqs_hz.clear();
            res.mode_shapes.clear();
            res.n_modes = 0;
            return res;
        }
        for (int i = 0; i < n_nodes; ++i)
            for (int c = 0; c < 3; ++c)
                res.mode_shapes[(static_cast<std::size_t>(m) * n_nodes + i) * 3 + c] =
                    u_full[i * 3 + c] * inv;
    }
    res.ok = true;
    return res;
}

}  // namespace chudware::sim
