// SupernodalLLT (sim/supernodal_llt.h): the in-tree sparse Cholesky that leads the
// FEA solver ladder in a build without SuiteSparse.
//
// Every case is checked against Eigen's SimplicialLDLT on the same matrix, so a
// wrong ordering, a wrong scatter map or a wrong extend-add shows up as a solution
// mismatch rather than as a plausible-looking residual.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <array>
#include <map>
#include <random>
#include <utility>
#include <vector>

#include <Eigen/SparseCholesky>
#include <Eigen/SparseCore>

#include "sim/supernodal_llt.h"

using chudware::sim::SupernodalLLT;

namespace {

using SpMat = Eigen::SparseMatrix<double>;

// A random SPD "stiffness" on a Kuhn tet10 box mesh with `b` unknowns per node:
// each element contributes G G^T + 0.5 I over its nodes, so the pattern is exactly
// a finite-element one (full b x b node blocks when b == 3).
SpMat tet10_box(int nx, int ny, int nz, int b, unsigned seed) {
    const auto vid = [&](int i, int j, int k) { return (i * (ny + 1) + j) * (nz + 1) + k; };
    int nn = (nx + 1) * (ny + 1) * (nz + 1);
    std::map<std::pair<int, int>, int> mid;
    std::vector<std::array<int, 10>> elems;
    const int perms[6][3] = {{0, 1, 2}, {0, 2, 1}, {1, 0, 2}, {1, 2, 0}, {2, 0, 1}, {2, 1, 0}};
    for (int i = 0; i < nx; ++i)
        for (int j = 0; j < ny; ++j)
            for (int k = 0; k < nz; ++k)
                for (const auto& p : perms) {
                    int c[3] = {i, j, k};
                    int v[4];
                    v[0] = vid(c[0], c[1], c[2]);
                    for (int s = 0; s < 3; ++s) {
                        ++c[p[s]];
                        v[s + 1] = vid(c[0], c[1], c[2]);
                    }
                    std::array<int, 10> e{};
                    for (int a = 0; a < 4; ++a) e[a] = v[a];
                    int q = 4;
                    for (int a = 0; a < 4; ++a)
                        for (int d = a + 1; d < 4; ++d) {
                            const auto key = std::minmax(v[a], v[d]);
                            auto it = mid.find(key);
                            if (it == mid.end()) it = mid.emplace(key, nn++).first;
                            e[q++] = it->second;
                        }
                    elems.push_back(e);
                }
    std::mt19937 rng(seed);
    std::normal_distribution<double> nd;
    const int ed = 10 * b;
    Eigen::MatrixXd G(ed, ed);
    std::vector<Eigen::Triplet<double>> trips;
    for (const auto& e : elems) {
        for (int a = 0; a < ed * ed; ++a) G.data()[a] = nd(rng);
        const Eigen::MatrixXd Ke = G * G.transpose() + 0.5 * Eigen::MatrixXd::Identity(ed, ed);
        for (int a = 0; a < 10; ++a)
            for (int c = 0; c < 10; ++c)
                for (int i = 0; i < b; ++i)
                    for (int j = 0; j < b; ++j)
                        trips.emplace_back(e[a] * b + i, e[c] * b + j, Ke(a * b + i, c * b + j));
    }
    SpMat A(nn * b, nn * b);
    A.setFromTriplets(trips.begin(), trips.end());
    A.makeCompressed();
    return A;
}

Eigen::VectorXd reference_solve(const SpMat& A, const Eigen::VectorXd& rhs) {
    Eigen::SimplicialLDLT<SpMat> ldlt(A);
    REQUIRE(ldlt.info() == Eigen::Success);
    return ldlt.solve(rhs);
}

Eigen::VectorXd supernodal_solve(SupernodalLLT& llt, const Eigen::VectorXd& rhs) {
    Eigen::VectorXd x = rhs;
    REQUIRE(llt.solve_in_place(x.data()));
    return x;
}

double rel(const Eigen::VectorXd& a, const Eigen::VectorXd& b) {
    return (a - b).norm() / b.norm();
}

}  // namespace

TEST_CASE("matches LDLT on node-blocked (3 DOF/node) and scalar finite-element patterns") {
    for (int b : {1, 3}) {
        CAPTURE(b);
        // 8x4x4 cells is large enough (> 2000 graph nodes for b == 1) to exercise
        // the nested-dissection candidate as well as minimum degree.
        const SpMat A = tet10_box(8, 4, 4, b, 7u + static_cast<unsigned>(b));
        const Eigen::VectorXd rhs = Eigen::VectorXd::Random(A.rows());
        SupernodalLLT llt;
        REQUIRE(llt.analyze(A));
        REQUIRE(llt.factorize(A));
        CHECK(llt.size() == A.rows());
        CHECK(llt.supernodes() > 1);
        CHECK(llt.work() > 0.0);
        const Eigen::VectorXd x = supernodal_solve(llt, rhs);
        CHECK((A * x - rhs).norm() / rhs.norm() < 1e-12);
        CHECK(rel(x, reference_solve(A, rhs)) < 1e-10);
    }
}

TEST_CASE("only the lower triangle is read") {
    const SpMat full = tet10_box(3, 2, 2, 3, 11u);
    const SpMat lower = full.triangularView<Eigen::Lower>();
    const Eigen::VectorXd rhs = Eigen::VectorXd::Random(full.rows());
    SupernodalLLT a, b;
    REQUIRE(a.analyze(full));
    REQUIRE(a.factorize(full));
    REQUIRE(b.analyze(lower));
    REQUIRE(b.factorize(lower));
    CHECK(rel(supernodal_solve(b, rhs), supernodal_solve(a, rhs)) < 1e-13);
}

TEST_CASE("refactorization reuses the symbolic analysis and is bit-reproducible") {
    SpMat A = tet10_box(4, 3, 3, 3, 3u);
    const Eigen::VectorXd rhs = Eigen::VectorXd::Random(A.rows());
    SupernodalLLT llt;
    REQUIRE(llt.analyze(A));
    REQUIRE(llt.factorize(A, 1));
    const Eigen::VectorXd serial = supernodal_solve(llt, rhs);
    REQUIRE(llt.factorize(A, 4));
    CHECK(supernodal_solve(llt, rhs) == serial);  // thread count changes nothing

    // New values on the same pattern (a contact iteration / Newton tangent).
    A *= 2.0;
    REQUIRE(llt.factorize(A));
    CHECK(rel(supernodal_solve(llt, rhs), 0.5 * serial) < 1e-12);
}

TEST_CASE("the AVX2 and portable dense kernels agree") {
    const SpMat A = tet10_box(6, 3, 3, 3, 5u);
    const Eigen::VectorXd rhs = Eigen::VectorXd::Random(A.rows());
    SupernodalLLT llt;
    REQUIRE(llt.analyze(A));
    REQUIRE(llt.factorize(A));
    const Eigen::VectorXd fast = supernodal_solve(llt, rhs);
    SupernodalLLT::use_portable_kernel(true);
    const bool refactored = llt.factorize(A);
    SupernodalLLT::use_portable_kernel(false);
    REQUIRE(refactored);
    CHECK(rel(supernodal_solve(llt, rhs), fast) < 1e-12);
}

TEST_CASE("lower_solve then upper_solve is the full solve") {
    const SpMat A = tet10_box(3, 3, 2, 3, 9u);
    const Eigen::VectorXd rhs = Eigen::VectorXd::Random(A.rows());
    SupernodalLLT llt;
    REQUIRE(llt.analyze(A));
    REQUIRE(llt.factorize(A));
    Eigen::VectorXd y(A.rows()), x(A.rows());
    REQUIRE(llt.lower_solve(rhs.data(), y.data()));
    // ||L^-1 P b||^2 == b^T A^-1 b: the halves really are a Cholesky factor's.
    const Eigen::VectorXd full = supernodal_solve(llt, rhs);
    CHECK(std::abs(y.squaredNorm() - rhs.dot(full)) < 1e-10 * rhs.dot(full));
    REQUIRE(llt.upper_solve(y.data(), x.data()));
    CHECK(rel(x, full) < 1e-13);
}

TEST_CASE("disconnected components and a diagonal matrix factor") {
    // Two separate blocks: the ordering must cover every component.
    const SpMat one = tet10_box(2, 2, 2, 3, 1u);
    const Eigen::Index n = one.rows();
    std::vector<Eigen::Triplet<double>> trips;
    for (int k = 0; k < one.outerSize(); ++k)
        for (SpMat::InnerIterator it(one, k); it; ++it) {
            trips.emplace_back(it.row(), it.col(), it.value());
            trips.emplace_back(it.row() + n, it.col() + n, 2.0 * it.value());
        }
    SpMat two(2 * n, 2 * n);
    two.setFromTriplets(trips.begin(), trips.end());
    const Eigen::VectorXd rhs = Eigen::VectorXd::Random(two.rows());
    SupernodalLLT llt;
    REQUIRE(llt.analyze(two));
    REQUIRE(llt.factorize(two));
    CHECK(rel(supernodal_solve(llt, rhs), reference_solve(two, rhs)) < 1e-10);

    SpMat diag(5, 5);
    for (int i = 0; i < 5; ++i) diag.insert(i, i) = i + 1.0;
    diag.makeCompressed();
    SupernodalLLT d;
    REQUIRE(d.analyze(diag));
    REQUIRE(d.factorize(diag));
    Eigen::VectorXd ones = Eigen::VectorXd::Ones(5);
    REQUIRE(d.solve_in_place(ones.data()));
    for (int i = 0; i < 5; ++i) CHECK(ones[i] == doctest::Approx(1.0 / (i + 1.0)));
}

TEST_CASE("strict: an indefinite or non-finite matrix is refused, not perturbed") {
    SpMat A = tet10_box(3, 2, 2, 3, 13u);
    SupernodalLLT llt;
    REQUIRE(llt.analyze(A));
    A.coeffRef(A.rows() / 2, A.rows() / 2) = -1e6;
    CHECK_FALSE(llt.factorize(A));
    CHECK_FALSE(llt.factored());
    Eigen::VectorXd x = Eigen::VectorXd::Ones(A.rows());
    CHECK_FALSE(llt.solve_in_place(x.data()));

    A.coeffRef(A.rows() / 2, A.rows() / 2) = std::numeric_limits<double>::quiet_NaN();
    CHECK_FALSE(llt.factorize(A));
}

TEST_CASE("unsupported inputs are rejected up front") {
    SupernodalLLT llt;
    SpMat rect(4, 3);
    rect.insert(0, 0) = 1.0;
    rect.makeCompressed();
    CHECK_FALSE(llt.analyze(rect));

    SpMat uncompressed(3, 3);
    uncompressed.reserve(Eigen::VectorXi::Constant(3, 2));
    for (int i = 0; i < 3; ++i) uncompressed.insert(i, i) = 1.0;
    CHECK_FALSE(llt.analyze(uncompressed));

    // A matrix whose pattern differs from the analyzed one cannot be factored.
    const SpMat A = tet10_box(2, 2, 2, 3, 2u);
    REQUIRE(llt.analyze(A));
    SpMat diag(A.rows(), A.cols());
    diag.setIdentity();
    diag.makeCompressed();
    CHECK_FALSE(llt.factorize(diag));
}
