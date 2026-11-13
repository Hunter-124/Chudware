// Supernodal multifrontal sparse Cholesky (A = L L^T) for the SPD systems the FEA
// solvers factor: reduced stiffness, shifted K + |sigma| M, conduction.
//
// A scalar simplicial Cholesky spends a 3D solid's factorization one fill entry at a
// time. This one groups columns with the same structure into dense supernodes and
// runs the elimination as dense partial factorizations of frontal matrices, which is
// where a vectorized, cache-blocked, threaded kernel applies. It needs nothing
// beyond Eigen, so every platform gets it.
//
// Only the lower triangle (row >= column) of the input is read; entries above the
// diagonal are ignored. The input must be compressed with strictly ascending row
// indices per column. analyze() takes the pattern, factorize() the values of a
// matrix with that same pattern, so a loop that changes values only (contact
// active sets, Newton tangents) pays for one ordering.
//
// The factorization is STRICT: a non-positive pivot fails factorize() instead of
// being perturbed, so "factorize succeeded" keeps meaning "numerically SPD".
//
// Results do not depend on the thread count: the elimination order, the blocking
// and every summation order are fixed by analyze().
#pragma once

#include <cstdint>
#include <vector>

#include <Eigen/SparseCore>

namespace chudware::sim {

class SupernodalLLT {
public:
    SupernodalLLT() = default;
    ~SupernodalLLT();
    SupernodalLLT(const SupernodalLLT&) = delete;
    SupernodalLLT& operator=(const SupernodalLLT&) = delete;

    // Ordering + symbolic factorization. False on an unsupported input (not square,
    // not compressed, unsorted rows) or when the structure cannot be allocated.
    bool analyze(const Eigen::SparseMatrix<double>& A);

    // Numeric factorization of a matrix with the analyzed pattern. `threads` <= 0
    // uses the OpenMP default. False on a non-positive or non-finite pivot, a
    // pattern mismatch, or an allocation failure; the factor is then unusable until
    // the next successful factorize().
    bool factorize(const Eigen::SparseMatrix<double>& A, int threads = 0);

    // Solve A x = b in place (x holds b on entry). Requires a successful factorize().
    bool solve_in_place(double* x) const;
    // The two halves of that solve, with P the fill-reducing permutation:
    // lower_solve writes L^-1 P in, upper_solve writes P^T L^-T in. `in` and `out`
    // may be the same array. A generalized eigensolver uses them to reduce
    // K x = lambda M x to standard form.
    bool lower_solve(const double* in, double* out) const;
    bool upper_solve(const double* in, double* out) const;

    bool factored() const { return factored_; }
    int size() const { return n_; }
    // Bytes held by the numeric factor and the symbolic structure.
    uint64_t factor_bytes() const;
    // Stored factor entries (the dense supernode blocks), for reporting.
    uint64_t factor_entries() const { return static_cast<uint64_t>(lx_size_); }
    int supernodes() const { return static_cast<int>(sfirst_.size()) - 1; }
    // Predicted factorization work, sum over columns of (column count)^2: what the
    // ordering was chosen to minimize, and what a caller should budget time on.
    double work() const { return work_; }

    // The dense kernel is AVX2 + FMA on an x86-64 CPU that has them and Eigen's
    // baseline product everywhere else. This forces the baseline one process-wide,
    // so both can be exercised on one machine.
    static void use_portable_kernel(bool portable);

private:
    void reset();
    bool process_front(int s, const double* ax, std::vector<double*>& update,
                       int inner_threads) const;

    int n_ = 0;       // scalar unknowns
    int b_ = 1;       // unknowns per graph node (3 when A is made of full 3x3 blocks)
    int nn_ = 0;      // graph nodes, n_ / b_
    int64_t pattern_nnz_ = 0;

    std::vector<int> inv_;          // original node -> elimination position
    std::vector<int> sfirst_;       // S+1: first node of each supernode
    std::vector<int> rstart_;       // S+1: offsets into rows_
    std::vector<int> rows_;         // row nodes below each supernode, ascending
    std::vector<int64_t> lstart_;   // S+1: offsets of each supernode block in lx_
    std::vector<int> cstart_;       // S+1: offsets into child_
    std::vector<int> child_;        // children of each supernode, ascending
    std::vector<int> level_start_;  // offsets into level_order_
    std::vector<int> level_order_;  // supernodes grouped by height above the leaves
    std::vector<int64_t> amap_start_;  // S+1: offsets into amap_src_/amap_dst_
    std::vector<int> amap_src_;        // value index in A
    std::vector<uint32_t> amap_dst_;   // offset inside the supernode block

    double* lx_ = nullptr;
    int64_t lx_size_ = 0;
    double work_ = 0.0;
    int max_rows_ = 0;  // largest below-supernode row count, in scalar unknowns
    bool factored_ = false;
};

}  // namespace chudware::sim
