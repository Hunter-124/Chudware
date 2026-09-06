// Linear-static FEA solver implementation — see fea_static.h.
//
// The reduced free-free system is assembled directly into a fixed sparsity pattern
// (fixed DOFs mapped out during assembly, no triplet list), then factored down the
// SPD ladder: CHOLMOD supernodal LLT where SuiteSparse exists, else Eigen
// SimplicialLDLT(AMD), with SparseLU kept only for the non-positive-pivot case that
// needs partial pivoting. von Mises stress is recovered per element from the same
// strain-displacement operator, in chunks, and averaged onto nodes.

#include "sim/fea_static.h"

#include "prof/phases.h"

#include <algorithm>
#include <array>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <cstdlib>
#include <list>
#include <map>
#include <memory>
#include <mutex>
#include <set>
#include <string>
#include <unordered_map>
#include <vector>

#include <thread>

#include <Eigen/Dense>
#include <Eigen/SparseCholesky>
#include <Eigen/SparseCore>
#include <Eigen/SparseLU>

#ifdef CHUDWARE_WITH_CHOLMOD
#include <Eigen/CholmodSupport>  // Eigen::CholmodSupernodalLLT (SuiteSparse BLAS3 Cholesky)
#endif
#ifdef CHUDWARE_HAVE_OPENMP
#include <omp.h>
#endif

#include "util/resource_governor.h"
#include "util/thread_pool.h"

namespace chudware::sim {

namespace {

// Cap the solver's worker threads so a long solve never eats every core — the GUI/render
// threads must keep running (Hunter: "let the GUI continue on at least 1 thread"). Caps both
// CHOLMOD's own OpenMP loops and the OpenMP-based system BLAS (FlexiBLAS/OpenBLAS) it calls.
// Env CHUDWARE_FEA_THREADS overrides; default = governor plan capped at hw-2 / 8.
// Size-adaptive: tiny systems stay serial (thread spawn costs more than the work).
int fea_solver_threads(long n_free) {
    if (const char* e = std::getenv("CHUDWARE_FEA_THREADS")) {
        const int n = std::atoi(e);
        if (n >= 1) return n;
    }
    unsigned hw = std::thread::hardware_concurrency();
    if (hw == 0) hw = 4;
    const int hw_cap = std::max(1, static_cast<int>(hw) - 2);  // always leave 2 cores for GUI/render/IPC
    // Size-adaptive parallelism (measured on tet10 3D elasticity, this box):
    //   * Tiny systems: thread spawn + OpenMP/BLAS setup costs MORE than the work. A ~10k-DOF
    //     factorize is 31 ms on 1 thread but 65 ms on 10 — so serialize the small solves.
    //   * Large systems: the supernodal DGEMM parallelizes, but past ~8 threads it saturates memory
    //     bandwidth and the nested CHOLMOD-OpenMP × OpenBLAS-OpenMP pools oversubscribe — a 106k-DOF
    //     factorize is 1.4 s at 8 threads but 2.3 s at 10. So cap at 8, which also frees more cores
    //     for the live GUI than hw-2 would.
    int want;
    if      (n_free <  20000) want = 1;
    else if (n_free <  60000) want = 4;
    else                      want = 8;
    // Fold through the shared governor/pool so nested solves share one budget.
    const auto plan = chudware::governor::plan_for(0, "fea_solver", want);
    const int governed = std::max(1, std::min({want, plan.workers, hw_cap, 8}));
    chudware::pool::shared().set_max_workers(governed);
    return governed;
}

// Threads for the element loops (assembly + stress recovery) — P1 / F3. These are pure
// per-element compute (B^T C B dense products) with deterministic output slots, so they
// scale near-linearly; unlike the factorization they don't saturate memory bandwidth
// early. Small meshes stay serial (thread spawn costs more than the work). Honours the
// same CHUDWARE_FEA_THREADS override as the solver cap; budget comes from the shared
// governor/pool so nested FEA+CFD cannot oversubscribe.
int fea_assembly_threads(int n_elems) {
#ifndef CHUDWARE_HAVE_OPENMP
    (void)n_elems;
    return 1;
#else
    if (n_elems < 4000) return 1;
    if (const char* e = std::getenv("CHUDWARE_FEA_THREADS")) {
        const int n = std::atoi(e);
        if (n >= 1) return n;
    }
    unsigned hw = std::thread::hardware_concurrency();
    if (hw == 0) hw = 4;
    const int hw_cap = std::max(1, std::min(static_cast<int>(hw) - 2, 8));
    // ~1 KB working set per element as a coarse peak estimate for the assembly pass.
    const uint64_t est = static_cast<uint64_t>(std::max(0, n_elems)) * 1024ull;
    const auto plan = chudware::governor::plan_for(est, "fea_assembly", hw_cap);
    const int n = std::max(1, std::min(plan.workers, hw_cap));
    chudware::pool::shared().set_max_workers(n);
    return n;
#endif
}

// ── Triplet-free, fixed-pattern assembly ────────────────────────────────────────
//
// Every fixed node pins all THREE of its DOFs (that is the only fixture this kernel
// takes), so the reduced free-free system's rows are exactly the DOFs of the FREE
// nodes and its sparsity is a 3x3-BLOCK pattern over free-node adjacency. That lets
// the matrix be built without a triplet list at all: compute the pattern once, then
// scatter element values straight into the compressed arrays.
//
// Why it matters (this is where the memory refusals came from): a triplet list costs
// (3*npe)^2 * 16 bytes PER ELEMENT — 14.4 KB for one tet10 — and setFromTriplets then
// allocated the matrix, an index permutation and a transposed scratch copy ON TOP of
// it. A 100k-element tet10 therefore spent ~1.4 GB on triplets before the
// factorization had allocated a byte. The pattern path pays 12 bytes per STORED
// nonzero once, and duplicate element contributions are summed in place.
//
// The pattern also includes every bond tie and every CONTACT CANDIDATE (active or
// not), which makes it invariant across the active-set loop: an iteration is then a
// numeric refactorization on one cached symbolic analysis, and a released pair simply
// carries the value 0.0.
struct BlockPattern {
    int n_free_nodes = 0;
    std::vector<int> free_node;  // node id -> free-node index, or -1 when fixed
    std::vector<int> start;      // n_free_nodes+1 offsets into col
    std::vector<int> col;        // free-node column ids, ascending within one row

    int blocks() const { return static_cast<int>(col.size()); }
    // Value-array index of entry (i,j) of the 3x3 block at position p of column c.
    // Column-major storage (Eigen's default): column 3c+j owns 3*nb consecutive
    // values, block p occupying [3p, 3p+3). The pattern is symmetric, so a
    // column-major fill of a symmetric matrix and a row-major one coincide.
    inline int64_t value_index(int c, int p, int i, int j) const {
        const int b0 = start[static_cast<std::size_t>(c)];
        const int nb = start[static_cast<std::size_t>(c) + 1] - b0;
        return 9LL * b0 + static_cast<int64_t>(j) * 3 * nb + 3LL * p + i;
    }
    // Position of row-block r within column c. The pair is in the pattern by
    // construction (element cliques, bonds and contact candidates all seeded it), so
    // a miss is a programming error, not a data case: it returns -1 and callers drop
    // the contribution rather than corrupting a neighbouring entry.
    inline int block_pos(int c, int r) const {
        const int b0 = start[static_cast<std::size_t>(c)];
        const int b1 = start[static_cast<std::size_t>(c) + 1];
        const auto first = col.begin() + b0;
        const auto it = std::lower_bound(first, col.begin() + b1, r);
        if (it == col.begin() + b1 || *it != r) return -1;
        return static_cast<int>(it - first);
    }
};

// Free-node adjacency -> block pattern. extra_pairs carries the node pairs that are
// coupled by something other than an element (bond ties, contact candidates).
BlockPattern build_block_pattern(int n_nodes, const std::vector<int>& tets, int stride,
                                 const std::vector<int>& reduced,
                                 const std::vector<std::array<int, 2>>& extra_pairs) {
    BlockPattern bp;
    bp.free_node.assign(static_cast<std::size_t>(n_nodes), -1);
    for (int nid = 0; nid < n_nodes; ++nid)
        if (reduced[static_cast<std::size_t>(nid) * 3] >= 0)
            bp.free_node[static_cast<std::size_t>(nid)] = bp.n_free_nodes++;
    const int nfn = bp.n_free_nodes;
    const std::size_t n_elems = stride > 0 ? tets.size() / static_cast<std::size_t>(stride) : 0;
    // Pass 1: how many adjacency slots each free node needs (duplicates included —
    // they are removed by the sort/unique below, and counting exactly would cost a
    // second hash pass for no memory saving).
    std::vector<int64_t> off(static_cast<std::size_t>(nfn) + 1, 0);
    for (std::size_t e = 0; e < n_elems; ++e) {
        const int* tn = &tets[e * static_cast<std::size_t>(stride)];
        int nf = 0;
        for (int j = 0; j < stride; ++j)
            if (bp.free_node[static_cast<std::size_t>(tn[j])] >= 0) ++nf;
        for (int j = 0; j < stride; ++j) {
            const int r = bp.free_node[static_cast<std::size_t>(tn[j])];
            if (r >= 0) off[static_cast<std::size_t>(r) + 1] += nf;
        }
    }
    for (const auto& p : extra_pairs) {
        const int ra = bp.free_node[static_cast<std::size_t>(p[0])];
        const int rb = bp.free_node[static_cast<std::size_t>(p[1])];
        if (ra >= 0) off[static_cast<std::size_t>(ra) + 1] += (rb >= 0 ? 2 : 1);
        if (rb >= 0) off[static_cast<std::size_t>(rb) + 1] += (ra >= 0 ? 2 : 1);
    }
    for (int r = 0; r < nfn; ++r)
        off[static_cast<std::size_t>(r) + 1] += off[static_cast<std::size_t>(r)];
    std::vector<int> flat(static_cast<std::size_t>(off[static_cast<std::size_t>(nfn)]));
    std::vector<int64_t> cur(off.begin(), off.end() - 1);
    auto push = [&](int r, int c) { flat[static_cast<std::size_t>(cur[static_cast<std::size_t>(r)]++)] = c; };
    for (std::size_t e = 0; e < n_elems; ++e) {
        const int* tn = &tets[e * static_cast<std::size_t>(stride)];
        for (int a = 0; a < stride; ++a) {
            const int r = bp.free_node[static_cast<std::size_t>(tn[a])];
            if (r < 0) continue;
            for (int b = 0; b < stride; ++b) {
                const int c = bp.free_node[static_cast<std::size_t>(tn[b])];
                if (c >= 0) push(r, c);
            }
        }
    }
    for (const auto& p : extra_pairs) {
        const int ra = bp.free_node[static_cast<std::size_t>(p[0])];
        const int rb = bp.free_node[static_cast<std::size_t>(p[1])];
        if (ra >= 0) { push(ra, ra); if (rb >= 0) push(ra, rb); }
        if (rb >= 0) { push(rb, rb); if (ra >= 0) push(rb, ra); }
    }
    // Pass 2: sort/unique each row into the final compact column list.
    bp.start.assign(static_cast<std::size_t>(nfn) + 1, 0);
    bp.col.clear();
    bp.col.reserve(flat.size() / 4 + 1);
    for (int r = 0; r < nfn; ++r) {
        int* b = flat.data() + off[static_cast<std::size_t>(r)];
        int* e2 = flat.data() + cur[static_cast<std::size_t>(r)];
        std::sort(b, e2);
        e2 = std::unique(b, e2);
        bp.start[static_cast<std::size_t>(r)] = static_cast<int>(bp.col.size());
        bp.col.insert(bp.col.end(), b, e2);
    }
    bp.start[static_cast<std::size_t>(nfn)] = static_cast<int>(bp.col.size());
    return bp;
}

// Materialize the pattern as a compressed Eigen matrix with all-zero values. The
// index arrays are written once here; every later assembly (and every contact
// iteration) only touches valuePtr(), so the pattern — and the symbolic
// factorization taken from it — survives untouched.
bool make_pattern_matrix(const BlockPattern& bp, Eigen::SparseMatrix<double>& K,
                         std::string& err) {
    using Idx = Eigen::SparseMatrix<double>::StorageIndex;
    const int64_t blocks = static_cast<int64_t>(bp.col.size());
    const int64_t nnz = 9 * blocks;
    if (nnz > 2000000000LL) {
        err = "fea_static: reduced system needs " + std::to_string(nnz) +
              " stored nonzeros, past the 32-bit sparse index limit — coarsen the mesh "
              "or split the model";
        return false;
    }
    const int n = bp.n_free_nodes * 3;
    K.resize(n, n);
    K.resizeNonZeros(static_cast<Eigen::Index>(nnz));
    Idx* outer = K.outerIndexPtr();
    Idx* inner = K.innerIndexPtr();
    double* val = K.valuePtr();
    for (int c = 0; c < bp.n_free_nodes; ++c) {
        const int b0 = bp.start[static_cast<std::size_t>(c)];
        const int nb = bp.start[static_cast<std::size_t>(c) + 1] - b0;
        for (int j = 0; j < 3; ++j) {
            const int64_t colbase = 9LL * b0 + static_cast<int64_t>(j) * 3 * nb;
            outer[c * 3 + j] = static_cast<Idx>(colbase);
            for (int p = 0; p < nb; ++p) {
                const int r = bp.col[static_cast<std::size_t>(b0 + p)];
                for (int i = 0; i < 3; ++i) {
                    const int64_t idx = colbase + 3LL * p + i;
                    inner[idx] = static_cast<Idx>(r * 3 + i);
                    val[idx] = 0.0;
                }
            }
        }
    }
    outer[n] = static_cast<Idx>(nnz);
    return true;
}

// Accumulate one scalar into the pattern (used by bond ties and contact springs).
// A contribution whose row or column is a FIXED DOF is dropped, which is the correct
// penalty behaviour: the tie grounds the free side.
inline void add_entry(const BlockPattern& bp, double* val, int row_node, int col_node,
                      int i, int j, double v) {
    const int r = bp.free_node[static_cast<std::size_t>(row_node)];
    const int c = bp.free_node[static_cast<std::size_t>(col_node)];
    if (r < 0 || c < 0) return;
    const int p = bp.block_pos(c, r);
    if (p < 0) return;
    val[bp.value_index(c, p, i, j)] += v;
}

// Chunked element assembly: one chunk's element matrices are computed in PARALLEL
// into a bounded scratch buffer, then scattered SERIALLY in element order. Two
// consequences worth the shape: peak scratch is one chunk (~32 MB) instead of one
// dense matrix per element for the whole mesh, and the summation order into each
// matrix entry is element order — independent of thread count, so a solve is
// bit-for-bit reproducible whatever the host's core count.
//
// The public preflight has already rejected every invalid element, so a failing
// element callback here is a defensive path: it aborts the assembly instead of
// silently solving a partial domain.
template <int NPE, class KeFn>
bool assemble_chunked(int n_elems, const std::vector<int>& tets, const BlockPattern& bp,
                      double* val, KeFn&& ke_fn, const SimCancelToken& cancel_token,
                      int nthreads) {
    constexpr int EDOF = NPE * 3;
    constexpr int KE = EDOF * EDOF;
    // ~32 MB of element matrices per chunk (tet10: 4660 elements).
    const int chunk = std::max(1, static_cast<int>((32ull << 20) / (KE * sizeof(double))));
    std::vector<double> buf(static_cast<std::size_t>(std::min(chunk, std::max(1, n_elems))) * KE);
    std::vector<char> good(static_cast<std::size_t>(std::min(chunk, std::max(1, n_elems))), 0);
    for (int c0 = 0; c0 < n_elems; c0 += chunk) {
        if (cancel_token && cancel_token->load(std::memory_order_relaxed)) return false;
        const int c1 = std::min(n_elems, c0 + chunk);
#if defined(CHUDWARE_HAVE_OPENMP)
#pragma omp parallel for schedule(static) num_threads(nthreads) if (nthreads > 1)
#endif
        for (int e = c0; e < c1; ++e) {
            Eigen::Matrix<double, EDOF, EDOF> Ke;
            const bool okay = ke_fn(e, Ke);
            good[static_cast<std::size_t>(e - c0)] = okay ? 1 : 0;
            if (okay) {
                double* dst = &buf[static_cast<std::size_t>(e - c0) * KE];
                Eigen::Map<Eigen::Matrix<double, EDOF, EDOF>> slot(dst);
                slot = Ke;
            }
        }
        for (int e = c0; e < c1; ++e) {
            if (!good[static_cast<std::size_t>(e - c0)]) return false;
            const double* Ke = &buf[static_cast<std::size_t>(e - c0) * KE];
            const int* tn = &tets[static_cast<std::size_t>(e) * NPE];
            for (int la = 0; la < NPE; ++la) {
                const int r = bp.free_node[static_cast<std::size_t>(tn[la])];
                if (r < 0) continue;
                for (int lb = 0; lb < NPE; ++lb) {
                    const int c = bp.free_node[static_cast<std::size_t>(tn[lb])];
                    if (c < 0) continue;
                    const int p = bp.block_pos(c, r);
                    if (p < 0) continue;
                    const int64_t base = bp.value_index(c, p, 0, 0);
                    const int nb = bp.start[static_cast<std::size_t>(c) + 1] -
                                   bp.start[static_cast<std::size_t>(c)];
                    for (int j = 0; j < 3; ++j)
                        for (int i = 0; i < 3; ++i)
                            val[base + static_cast<int64_t>(j) * 3 * nb + i] +=
                                Ke[(la * 3 + i) + (lb * 3 + j) * EDOF];
                }
            }
        }
    }
    return true;
}

void apply_solver_thread_cap(int n) {
#ifdef CHUDWARE_HAVE_OPENMP
    omp_set_num_threads(n);
#endif
    // Belt-and-suspenders for BLAS backends that read the env (FlexiBLAS/OpenBLAS honour these).
    const std::string s = std::to_string(n);
#if defined(_WIN32)
    _putenv_s("OMP_NUM_THREADS", s.c_str());
    _putenv_s("OPENBLAS_NUM_THREADS", s.c_str());
#else
    setenv("OMP_NUM_THREADS", s.c_str(), 1);
    setenv("OPENBLAS_NUM_THREADS", s.c_str(), 1);
#endif
    (void)n;
}

// ── Lightweight, env-gated phase profiler ────────────────────────────────────────
// Set CHUDWARE_FEA_PROFILE=1 to get a per-phase wall-clock + size/memory breakdown of a
// static solve on stderr. Each line is flushed immediately so, if the solve aborts
// (e.g. std::bad_alloc on a too-fine mesh -> SIGABRT), the LAST line printed localizes
// the death — a sampling profiler's report() never runs after an abort, this does.
// Costs nothing when the env var is unset (one getenv per solve).
//
// The SAME mark() boundaries also feed the always-on structured phase registry
// (prof/phases.h) as "solve.<label>" so RPC callers (engine_profile action="phases")
// get the per-phase breakdown without the env var: one clock read + one thread-local
// vector append per mark, so keeping it always-on is free at these coarse boundaries.
struct FeaProf {
    bool on = false;
    std::chrono::steady_clock::time_point t0;
    explicit FeaProf() {
        const char* e = std::getenv("CHUDWARE_FEA_PROFILE");
        on = e && e[0] && e[0] != '0';
        t0 = std::chrono::steady_clock::now();
    }
    // Milliseconds since the previous mark(), then re-anchor. Records the phase into the
    // structured registry always; prints "[fea] <label> = <ms>" only when env-enabled.
    void mark(const char* label) {
        const auto now = std::chrono::steady_clock::now();
        const double ms =
            std::chrono::duration<double, std::milli>(now - t0).count();
        t0 = now;
        chudware::prof::phases::add((std::string("solve.") + label).c_str(), ms);
        if (!on) return;
        std::fprintf(stderr, "[fea] %-22s %10.2f ms\n", label, ms);
        std::fflush(stderr);
    }
    template <typename... A>
    void note(const char* fmt, A... args) {
        if (!on) return;
        std::fprintf(stderr, "[fea] ");
        std::fprintf(stderr, fmt, args...);
        std::fprintf(stderr, "\n");
        std::fflush(stderr);
    }
};

// Lamé parameters (MPa) from E, nu (fea_static.py::_lame_from_material).
void lame(double E, double nu, double& lam, double& mu) {
    lam = E * nu / ((1.0 + nu) * (1.0 - 2.0 * nu));
    mu = E / (2.0 * (1.0 + nu));
}

// 6x6 Voigt isotropic elasticity matrix (_build_elasticity_matrix).
Eigen::Matrix<double, 6, 6> elasticity(double lam, double mu) {
    Eigen::Matrix<double, 6, 6> C = Eigen::Matrix<double, 6, 6>::Zero();
    C(0, 0) = C(1, 1) = C(2, 2) = lam + 2 * mu;
    C(0, 1) = C(0, 2) = C(1, 0) = C(1, 2) = C(2, 0) = C(2, 1) = lam;
    C(3, 3) = C(4, 4) = C(5, 5) = mu;
    return C;
}

// One tet4's strain-displacement matrix B (6x12) and volume (_tet4_stiffness_and_B).
// Returns false (vol=0) for a degenerate element.
bool tet4_B(const std::array<Eigen::Vector3d, 4>& ep, Eigen::Matrix<double, 6, 12>& B,
            double& vol) {
    Eigen::Matrix4d T;
    for (int j = 0; j < 4; ++j) {
        T(j, 0) = 1.0;
        T(j, 1) = ep[j].x();
        T(j, 2) = ep[j].y();
        T(j, 3) = ep[j].z();
    }
    double det = T.determinant();
    vol = std::abs(det) / 6.0;
    if (vol <= 0.0) return false;
    Eigen::Matrix4d Tinv = T.inverse();
    if (!Tinv.allFinite()) { vol = 0.0; return false; }
    // dN[i,j] = d(N_j)/d(x_i) = rows 1..3 of Tinv.
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

// ── Quadratic (10-node) isoparametric tet ─────────────────────────────────────
//
// Natural coords (r,s,t); volume coords L0=1-r-s-t, L1=r, L2=s, L3=t. Node ordering
// matches the gmsh MSH_TET_10 layout (mesher.cpp): corners 0..3, then mid-edge nodes
// on edges {0,1},{1,2},{2,0},{3,0},{3,2},{3,1} -> nodes 4..9.

// Shape-function derivatives dN/d{r,s,t} (10x3) at natural coords (r,s,t).
// dL0/dr=dL0/ds=dL0/dt=-1; dL1/dr=1; dL2/ds=1; dL3/dt=1 (others 0).
Eigen::Matrix<double, 10, 3> tet10_dN(double r, double s, double t) {
    const double L0 = 1.0 - r - s - t, L1 = r, L2 = s, L3 = t;
    // Per-volume-coord gradients in (r,s,t).
    const Eigen::Vector3d g0(-1.0, -1.0, -1.0), g1(1.0, 0.0, 0.0),
        g2(0.0, 1.0, 0.0), g3(0.0, 0.0, 1.0);
    Eigen::Matrix<double, 10, 3> dN;
    // Corner: N = L(2L-1) -> dN = (4L-1) dL.
    dN.row(0) = (4.0 * L0 - 1.0) * g0.transpose();
    dN.row(1) = (4.0 * L1 - 1.0) * g1.transpose();
    dN.row(2) = (4.0 * L2 - 1.0) * g2.transpose();
    dN.row(3) = (4.0 * L3 - 1.0) * g3.transpose();
    // Mid-edge: N = 4 La Lb -> dN = 4 (La dLb + Lb dLa).
    dN.row(4) = (4.0 * (L0 * g1 + L1 * g0)).transpose();  // edge {0,1}
    dN.row(5) = (4.0 * (L1 * g2 + L2 * g1)).transpose();  // edge {1,2}
    dN.row(6) = (4.0 * (L2 * g0 + L0 * g2)).transpose();  // edge {2,0}
    dN.row(7) = (4.0 * (L3 * g0 + L0 * g3)).transpose();  // edge {3,0}
    dN.row(8) = (4.0 * (L3 * g2 + L2 * g3)).transpose();  // edge {3,2}
    dN.row(9) = (4.0 * (L3 * g1 + L1 * g3)).transpose();  // edge {3,1}
    return dN;
}

// 4-point Gauss rule for a tet (degree-2 exact). Points in (r,s,t); each weight 1/24
// so the weights sum to 1/6 (the reference-tet volume in natural coords).
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

// Gauss-point -> nodal extrapolation for the 4-point rule above, used by stress
// recovery. The rule's points sit at the volume coords (a,b,b,b) permuted, and
// a = 1-3b, so for a field that is LINEAR in the volume coords the Gauss values
// relate to the corner values by M = (a-b)P + b*ones (P a permutation). Its inverse
// is (P^T - b*ones)/(a-b), i.e. corner_i = sqrt(5) * (g[GP_AT_CORNER[i]] - b*sum(g)),
// because 1/(a-b) = sqrt(5) and (b/(a-b))/(1 + 4b/(a-b)) = b for these a,b. Corner k
// is the one where L_k = a, which is Gauss point 3,0,1,2 for corners 0,1,2,3.
constexpr int TET10_GP_AT_CORNER[4] = {3, 0, 1, 2};
constexpr double TET10_EXTRAP_SCALE = 2.2360679774997896;  // sqrt(5) = 1/(a-b)
constexpr double TET10_EXTRAP_B = 0.1381966011250105;      // b
// Mid-edge slots 4..9 bisect these corner pairs (tet10_dN's node ordering).
constexpr int TET10_MID_EDGE[6][2] = {{0, 1}, {1, 2}, {2, 0}, {3, 0}, {3, 2}, {3, 1}};

// Build B (6x30) at one Gauss point for a tet10 with corner+mid-edge node coords `ep`
// (10 entries). Returns false if the Jacobian is degenerate; on success sets detJ and
// the spatial shape-function gradients dNxyz (10x3, dNi/d{x,y,z}).
bool tet10_B_at(const std::array<Eigen::Vector3d, 10>& ep, double r, double s, double t,
                Eigen::Matrix<double, 6, 30>& B, double& detJ,
                Eigen::Matrix<double, 10, 3>& dNxyz) {
    const Eigen::Matrix<double, 10, 3> dNrst = tet10_dN(r, s, t);
    // Jacobian J(3x3): J(i,j) = d x_i / d xi_j = sum_n ep[n](i) * dNrst(n,j).
    Eigen::Matrix3d J = Eigen::Matrix3d::Zero();
    for (int n = 0; n < 10; ++n) J += ep[n] * dNrst.row(n);
    detJ = J.determinant();
    if (!(std::abs(detJ) > 0.0)) return false;
    Eigen::Matrix3d Jinv = J.inverse();
    if (!Jinv.allFinite()) return false;
    // dN/d{x,y,z} = dN/d{r,s,t} * Jinv  (row n: grad in natural * Jinv = grad in space).
    dNxyz = dNrst * Jinv;
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

// ── hex8 (8-node trilinear hexahedron) ──────────────────────────────────────────
// Natural-coord node ordering matches the structured grid emitted by mesh_box_hex:
// bottom face (zeta=-1) corners 0,1,2,3 CCW, top face (zeta=+1) corners 4,5,6,7.
constexpr int HEX_NAT[8][3] = {{-1, -1, -1}, {1, -1, -1}, {1, 1, -1}, {-1, 1, -1},
                               {-1, -1, 1},  {1, -1, 1},  {1, 1, 1},  {-1, 1, 1}};

// 2x2x2 Gauss rule (8 points at +-1/sqrt(3), unit weights).
struct HexGauss8 {
    double pt[8][3];
    double w[8];
    HexGauss8() {
        const double g = 0.5773502691896257;  // 1/sqrt(3)
        for (int i = 0; i < 8; ++i) {
            for (int k = 0; k < 3; ++k) pt[i][k] = HEX_NAT[i][k] * g;
            w[i] = 1.0;
        }
    }
};

// Shape-function natural gradients (8x3) at (xi,eta,zeta): dNi/d{xi,eta,zeta}.
Eigen::Matrix<double, 8, 3> hex8_dN(double xi, double eta, double zeta) {
    Eigen::Matrix<double, 8, 3> dN;
    for (int n = 0; n < 8; ++n) {
        const double X = HEX_NAT[n][0], Y = HEX_NAT[n][1], Z = HEX_NAT[n][2];
        dN(n, 0) = 0.125 * X * (1 + Y * eta) * (1 + Z * zeta);
        dN(n, 1) = 0.125 * (1 + X * xi) * Y * (1 + Z * zeta);
        dN(n, 2) = 0.125 * (1 + X * xi) * (1 + Y * eta) * Z;
    }
    return dN;
}

// Build B (6x24) at one Gauss point for a hex8 with corner coords `ep` (8 entries).
// Returns false if the Jacobian is degenerate; on success sets detJ.
bool hex8_B_at(const std::array<Eigen::Vector3d, 8>& ep, double xi, double eta, double zeta,
               Eigen::Matrix<double, 6, 24>& B, double& detJ) {
    const Eigen::Matrix<double, 8, 3> dNrst = hex8_dN(xi, eta, zeta);
    Eigen::Matrix3d J = Eigen::Matrix3d::Zero();
    for (int n = 0; n < 8; ++n) J += ep[n] * dNrst.row(n);
    detJ = J.determinant();
    if (!(std::abs(detJ) > 0.0)) return false;
    Eigen::Matrix3d Jinv = J.inverse();
    if (!Jinv.allFinite()) return false;
    const Eigen::Matrix<double, 8, 3> dNxyz = dNrst * Jinv;
    B.setZero();
    for (int n = 0; n < 8; ++n) {
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

}  // namespace

// ── Prepared-system (factor) cache for load-only repeats ─────────────────────────
// tet10/hex8 only: reuses reduced DOF map + assembled K + numeric factorization when
// mesh / element order / material (E,nu) / fixtures / bond springs match. Loads and
// yield strength do NOT enter the key. tet4 stays on the uncached SparseLU path so
// the Python oracle parity harness remains byte-identical.
namespace fea_factor_cache_detail {

inline uint64_t factor_fnv1a(const void* p, size_t n, uint64_t h = 1469598103934665603ull) {
    const auto* b = static_cast<const unsigned char*>(p);
    for (size_t i = 0; i < n; ++i) { h ^= b[i]; h *= 1099511628211ull; }
    return h;
}
inline uint64_t factor_system_key(const std::vector<double>& points, const std::vector<int>& tets,
                           double E_mpa, double nu, const std::vector<int>& fixed_nodes,
                           int nodes_per_elem, const std::vector<BondSpring>& bond_springs) {
    uint64_t h = factor_fnv1a(points.data(), points.size() * sizeof(double));
    h = factor_fnv1a(tets.data(), tets.size() * sizeof(int), h);
    h = factor_fnv1a(&E_mpa, sizeof E_mpa, h);
    h = factor_fnv1a(&nu, sizeof nu, h);
    h = factor_fnv1a(&nodes_per_elem, sizeof nodes_per_elem, h);
    std::vector<int> fx = fixed_nodes;
    std::sort(fx.begin(), fx.end());
    fx.erase(std::unique(fx.begin(), fx.end()), fx.end());
    h = factor_fnv1a(fx.data(), fx.size() * sizeof(int), h);
    std::vector<BondSpring> bs = bond_springs;
    std::sort(bs.begin(), bs.end(), [](const BondSpring& x, const BondSpring& y) {
        if (x.a != y.a) return x.a < y.a;
        if (x.b != y.b) return x.b < y.b;
        return x.k < y.k;
    });
    for (const BondSpring& s : bs) {
        h = factor_fnv1a(&s.a, sizeof s.a, h);
        h = factor_fnv1a(&s.b, sizeof s.b, h);
        h = factor_fnv1a(&s.k, sizeof s.k, h);
    }
    return h;
}
// One factorization of the reduced SPD stiffness, with the symbolic step (analyze)
// separated from the numeric one (factorize). The split is what lets the contact
// active-set loop pay for ONE ordering + elimination tree and then only refactor:
// its pattern is iteration-invariant by construction (every candidate pair is in the
// pattern), so only the values change.
struct FactorSolver {
    virtual ~FactorSolver() = default;
    virtual bool analyze(const Eigen::SparseMatrix<double>& K) = 0;
    virtual bool factorize(const Eigen::SparseMatrix<double>& K) = 0;
    virtual Eigen::VectorXd solve(const Eigen::VectorXd& ff) = 0;
    virtual bool ok() const = 0;
    // Bytes the retained factor occupies — measured from the factor itself where the
    // backend exposes it, so the factor cache accounts for what it actually holds.
    virtual uint64_t factor_bytes() const = 0;
    virtual const char* label() const = 0;
};

// The reduced stiffness (Dirichlet rows eliminated during assembly) is symmetric
// positive definite, so a Cholesky-family factorization is the right tool:
// SimplicialLDLT stores ONE triangular factor under an AMD fill-reducing permutation,
// where SparseLU stores L and U from COLAMD with partial pivoting it does not need.
// AMDOrdering is spelled out rather than left implicit because the ordering is half
// the memory story on a 3D solid.
//
// This is the DEFAULT only in a build without SuiteSparse, and it is the pure-Eigen
// path CHUDWARE_FEA_SOLVER=ldlt selects. MEASURED on this box (100 mm box, Kuhn 6-tet
// mesh, CHUDWARE_SIM_THREADS=2, one process per point, peak RSS):
//
//   order  n_elems  n_dof    SparseLU        SimplicialLDLT   CholmodSupernodal
//   tet4    93750    52728   20.5 s/1.37 GB  36.5 s/0.60 GB   0.9 s/0.47 GB
//   tet10   20250    89373   OOM > 3 GB      160.8 s/1.52 GB  3.0 s/0.99 GB
//
// So against SparseLU, LDLT is the expected 2.3x memory saving on tet4 — but on a
// quadratic 3D mesh a SCALAR simplicial Cholesky is 53x slower than the supernodal
// one AND uses more memory, because its fill is not amortized into dense supernodes.
// That is why the supernodal LLT leads the ladder where SuiteSparse exists; do not
// re-order these on the "LDLT is lighter than LU" rule of thumb alone, which is true
// and yet not the binding fact here.
struct LdltFactorSolver : FactorSolver {
    Eigen::SimplicialLDLT<Eigen::SparseMatrix<double>, Eigen::Lower, Eigen::AMDOrdering<int>> ldlt;
    bool analyze(const Eigen::SparseMatrix<double>& K) override {
        ldlt.analyzePattern(K);
        return ldlt.info() == Eigen::Success;
    }
    bool factorize(const Eigen::SparseMatrix<double>& K) override {
        ldlt.factorize(K);
        return ldlt.info() == Eigen::Success;
    }
    Eigen::VectorXd solve(const Eigen::VectorXd& ff) override {
        Eigen::VectorXd u = ldlt.solve(ff);
        if (ldlt.info() != Eigen::Success) return {};
        return u;
    }
    bool ok() const override { return ldlt.info() == Eigen::Success; }
    uint64_t factor_bytes() const override {
        if (ldlt.info() != Eigen::Success) return 0;
        const uint64_t nnz =
            static_cast<uint64_t>(ldlt.matrixL().nestedExpression().nonZeros());
        const uint64_t n = static_cast<uint64_t>(ldlt.matrixL().nestedExpression().cols());
        // L values + inner indices + outer starts + the diagonal D and permutation.
        return nnz * (sizeof(double) + sizeof(int)) + n * (2 * sizeof(int) + 2 * sizeof(double));
    }
    const char* label() const override { return "SimplicialLDLT(AMD)"; }
};

// EXPLICIT FALLBACK, taken only when LDLT reports a non-positive pivot (info() !=
// Success on a real SPD system means the reduced matrix is numerically indefinite —
// an under-constrained or near-singular model). Partial pivoting can still carry
// such a system; it just costs both triangular factors.
struct SparseLuFactorSolver : FactorSolver {
    Eigen::SparseLU<Eigen::SparseMatrix<double>> lu;
    bool analyze(const Eigen::SparseMatrix<double>& K) override {
        lu.analyzePattern(K);
        return lu.info() == Eigen::Success;
    }
    bool factorize(const Eigen::SparseMatrix<double>& K) override {
        lu.factorize(K);
        return lu.info() == Eigen::Success;
    }
    Eigen::VectorXd solve(const Eigen::VectorXd& ff) override {
        Eigen::VectorXd u = lu.solve(ff);
        if (lu.info() != Eigen::Success) return {};
        return u;
    }
    bool ok() const override { return lu.info() == Eigen::Success; }
    uint64_t factor_bytes() const override {
        if (lu.info() != Eigen::Success) return 0;
        return (static_cast<uint64_t>(lu.nnzL()) + static_cast<uint64_t>(lu.nnzU())) *
               (sizeof(double) + sizeof(int));
    }
    const char* label() const override { return "SparseLU(COLAMD)"; }
};

#ifdef CHUDWARE_WITH_CHOLMOD
// DEFAULT wherever SuiteSparse is present, on measurement (see the table above the
// LDLT solver): supernodal LLT runs BLAS3 kernels over dense supernodes, which makes
// it 22x faster than SparseLU on tet4 and 53x faster than a scalar SimplicialLDLT on
// tet10 — and, on this box, ALSO the smallest peak of the three, because the dense
// supernodes replace a much larger scalar fill structure. CHUDWARE_FEA_SOLVER=ldlt
// forces the pure-Eigen path; CHUDWARE_FEA_ORDER selects the ordering.
struct CholmodFactorSolver : FactorSolver {
    Eigen::CholmodSupernodalLLT<Eigen::SparseMatrix<double>> chol;
    uint64_t pattern_nnz = 0;
    bool analyze(const Eigen::SparseMatrix<double>& K) override {
        const char* ord = std::getenv("CHUDWARE_FEA_ORDER");
        const int n_free = static_cast<int>(K.rows());
        const std::string o = ord ? ord : (n_free >= 50000 ? "metis" : "default");
        cholmod_common& cc = chol.cholmod();
        if      (o == "metis")  { cc.nmethods = 1; cc.method[0].ordering = CHOLMOD_METIS;  cc.postorder = 1; }
        else if (o == "nesdis") { cc.nmethods = 1; cc.method[0].ordering = CHOLMOD_NESDIS; cc.postorder = 1; }
        else if (o == "amd")    { cc.nmethods = 1; cc.method[0].ordering = CHOLMOD_AMD;    cc.postorder = 1; }
        pattern_nnz = static_cast<uint64_t>(K.nonZeros());
        chol.analyzePattern(K);
        return chol.info() == Eigen::Success;
    }
    bool factorize(const Eigen::SparseMatrix<double>& K) override {
        pattern_nnz = static_cast<uint64_t>(K.nonZeros());
        chol.factorize(K);
        return chol.info() == Eigen::Success;
    }
    Eigen::VectorXd solve(const Eigen::VectorXd& ff) override {
        Eigen::VectorXd u = chol.solve(ff);
        if (chol.info() != Eigen::Success) return {};
        return u;
    }
    bool ok() const override { return chol.info() == Eigen::Success; }
    // CHOLMOD owns its factor internally and Eigen exposes no size for it, so this is
    // an ESTIMATE, not a measurement: supernodal factors on 3D solids run several
    // times the matrix's own nonzero count. Marked as such because the factor cache
    // budgets on it.
    uint64_t factor_bytes() const override {
        return pattern_nnz * 6 * (sizeof(double) + sizeof(int));
    }
    const char* label() const override { return "CholmodSupernodalLLT"; }
};
#endif

// Which factorization to try FIRST: the supernodal LLT when SuiteSparse is compiled
// in, else the pure-Eigen SimplicialLDLT. CHUDWARE_FEA_SOLVER=ldlt|lu|cholmod
// overrides, which is how the comparison table above was measured.
enum class SolverKind { Ldlt, Lu, Cholmod };

inline SolverKind default_solver_kind() {
#ifdef CHUDWARE_WITH_CHOLMOD
    return SolverKind::Cholmod;
#else
    return SolverKind::Ldlt;
#endif
}

inline SolverKind requested_solver_kind() {
    const char* e = std::getenv("CHUDWARE_FEA_SOLVER");
    if (!e || !e[0]) return default_solver_kind();
    const std::string s = e;
    if (s == "lu" || s == "sparselu") return SolverKind::Lu;
    if (s == "ldlt" || s == "simplicial") return SolverKind::Ldlt;
#ifdef CHUDWARE_WITH_CHOLMOD
    if (s == "cholmod" || s == "llt") return SolverKind::Cholmod;
#endif
    return default_solver_kind();
}

inline std::unique_ptr<FactorSolver> make_solver(SolverKind k) {
    switch (k) {
        case SolverKind::Lu:
            return std::unique_ptr<FactorSolver>(new SparseLuFactorSolver());
#ifdef CHUDWARE_WITH_CHOLMOD
        case SolverKind::Cholmod:
            return std::unique_ptr<FactorSolver>(new CholmodFactorSolver());
#endif
        default:
            return std::unique_ptr<FactorSolver>(new LdltFactorSolver());
    }
}

// Analyze + factorize + one solve down the ladder: the requested factorization, then
// (when that was the supernodal LLT, whose strict-SPD test rejects a near-singular
// reduced system) SimplicialLDLT, then SparseLU — whose partial pivoting is the only
// one that can carry a genuinely indefinite reduced matrix. `label` reports which
// factorization actually produced the answer, so the packet never has to guess.
//
// `spd_rejected` is the load-bearing output: the reduced stiffness of a well-posed
// linear-elastic model is SPD BY CONSTRUCTION (a sum of PSD element matrices with the
// Dirichlet rows eliminated), so a strict Cholesky refusing it is not a solver
// preference — it is the model telling us it has a (near-)zero-stiffness mode. The
// caller turns that into a warning the packet carries, because the fallback rungs
// return a FINITE, plausible-looking displacement field for such a system (measured
// on a real mesh: condition number 4e33, a rigid-body mode on three nodes that each
// touch a single tet, equilibrium residual 0.0, max_disp a believable 0.032 mm — while
// an independent SparseLU on the same matrix returned 5.2e10 mm).
inline std::unique_ptr<FactorSolver> factor_and_solve(const Eigen::SparseMatrix<double>& K,
                                                      const Eigen::VectorXd& rhs,
                                                      Eigen::VectorXd& out,
                                                      std::string& label,
                                                      bool& spd_rejected) {
    const SolverKind want = requested_solver_kind();
    spd_rejected = false;
    std::string tried;
    auto try_kind = [&](SolverKind k) -> std::unique_ptr<FactorSolver> {
        auto s = make_solver(k);
        const char* name = s->label();
        if (!s->analyze(K) || !s->factorize(K) ||
            (out = s->solve(rhs), out.size() != K.rows())) {
            if (!tried.empty()) tried += ", ";
            tried += name;  // only FAILED attempts accumulate here
            return nullptr;
        }
        return s;
    };
    if (auto s = try_kind(want)) {
        label = s->label();
        return s;
    }
    // Only a Cholesky-family leader carries the SPD verdict; a SparseLU failure is a
    // plain singular-matrix failure and is reported as such by the caller.
    if (want != SolverKind::Lu) spd_rejected = true;
    // The supernodal LLT's strict-SPD test rejects a near-singular reduced system that
    // the indefinite-tolerant LDLT still factors, so it gets the next rung.
    if (want == SolverKind::Cholmod) {
        if (auto s = try_kind(SolverKind::Ldlt)) {
            label = std::string(s->label()) + " (fallback after " + tried + ")";
            return s;
        }
    }
    if (want == SolverKind::Lu) {
        label.clear();
        return nullptr;
    }
    // Non-positive pivot everywhere above: only partial pivoting is left.
    if (auto s = try_kind(SolverKind::Lu)) {
        label = std::string(s->label()) + " (fallback after " + tried + ")";
        return s;
    }
    label.clear();
    return nullptr;
}

struct PreparedSystem {
    std::vector<int> reduced;
    int n_free = 0;
    int n_dof = 0;
    std::unique_ptr<FactorSolver> solver;
    mutable std::mutex solve_mtx;
    std::size_t nnz = 0;
    uint64_t factor_bytes = 0;  // measured off the factor at store time
    std::string label;
    // A cache hit re-serves the SAME matrix, so it must re-serve its verdict too.
    std::string conditioning_warning;
    size_t bytes() const {
        return reduced.size() * sizeof(int) +
               nnz * (sizeof(double) + sizeof(int)) +  // the retained matrix pattern
               factor_bytes + 4096;
    }
};
struct FactorCacheState {
    std::mutex mtx;
    std::unordered_map<uint64_t, std::shared_ptr<const PreparedSystem>> map;
    std::list<uint64_t> lru;
    uint64_t resident = 0;
    FeaFactorCacheStats stats;
};
inline FactorCacheState& factor_cache_state() {
    static FactorCacheState s;
    return s;
}
// The cache is a CONVENIENCE holding memory a live solve may need, so its ceiling
// tracks the governor's live budget (which itself shrinks as other work takes RAM),
// never a fixed number.
inline uint64_t factor_cache_budget() {
    const uint64_t host = chudware::governor::budget_bytes();
    if (host == UINT64_MAX) return 1024ull * 1024ull * 1024ull;
    return std::max<uint64_t>(128ull * 1024ull * 1024ull, host / 6);
}
// Evict least-recently-used prepared systems until the cache fits `budget` (and
// entirely when the host is under memory pressure). Caller holds cs.mtx.
inline void factor_cache_trim_locked(FactorCacheState& cs, uint64_t budget) {
    if (chudware::governor::low_memory_now()) budget = 0;
    while (!cs.lru.empty() && (cs.resident > budget || cs.lru.size() > 8)) {
        auto it = cs.map.find(cs.lru.back());
        if (it != cs.map.end()) {
            cs.resident -= std::min<uint64_t>(cs.resident,
                                              static_cast<uint64_t>(it->second->bytes()));
            cs.map.erase(it);
            ++cs.stats.evictions;
        }
        cs.lru.pop_back();
    }
    cs.stats.resident_bytes = cs.resident;
}

}  // namespace fea_factor_cache_detail

using fea_factor_cache_detail::factor_and_solve;
using fea_factor_cache_detail::factor_cache_budget;
using fea_factor_cache_detail::factor_cache_state;
using fea_factor_cache_detail::factor_cache_trim_locked;
using fea_factor_cache_detail::factor_system_key;
using fea_factor_cache_detail::FactorSolver;
using fea_factor_cache_detail::make_solver;
using fea_factor_cache_detail::PreparedSystem;
using fea_factor_cache_detail::requested_solver_kind;
using fea_factor_cache_detail::SolverKind;

FeaFactorCacheStats fea_factor_cache_stats() {
    auto& s = factor_cache_state();
    std::lock_guard<std::mutex> lk(s.mtx);
    auto out = s.stats;
    out.resident_bytes = s.resident;
    return out;
}
void fea_factor_cache_reset_stats() {
    auto& s = factor_cache_state();
    std::lock_guard<std::mutex> lk(s.mtx);
    s.stats = FeaFactorCacheStats{};
    s.stats.resident_bytes = s.resident;
}
void fea_factor_cache_clear() {
    auto& s = factor_cache_state();
    std::lock_guard<std::mutex> lk(s.mtx);
    s.map.clear();
    s.lru.clear();
    s.resident = 0;
    s.stats.resident_bytes = 0;
}

namespace {

// One contact candidate, pre-normalized and pre-validated for the active-set loop.
struct ContactPair {
    int a = 0, b = 0;
    double k = 0.0;
    double kt = 0.0;  // tangential stabilization stiffness (k * options.tangential_stabilization)
    double gap0 = 0.0;
    double area = 0.0;
    int group = 0;
    double n[3] = {0.0, 0.0, 1.0};
};


// ── Process-wide heavy-solve gate ────────────────────────────────────────────────
// A direct sparse factorization is the memory peak of the whole engine. Two of them
// running at once on one host do not halve the wall time — they double the peak and
// then thrash, because the governor's budget is per-HOST and plan_for() only GRADES a
// job, it never serializes one (checked: it returns a worker count and a reservation
// and holds no lock). So a heavy solve takes this gate and parallel MCP jobs queue
// instead of competing; a light solve (interactive preview, unit test, the small legs
// of a convergence ladder) never waits behind one.
//
// Re-entrancy: the depth counter makes a nested solve on the same thread pass
// straight through, so an outer gated solve calling into this kernel again cannot
// deadlock against itself.
constexpr int kSolveGateMinFreeDof = 20000;

std::mutex& solve_gate() {
    static std::mutex m;
    return m;
}
thread_local int t_solve_gate_depth = 0;

class SolveGateLock {
public:
    explicit SolveGateLock(long n_free) {
        if (n_free < kSolveGateMinFreeDof || t_solve_gate_depth > 0) return;
        held_ = true;
        ++t_solve_gate_depth;
        if (solve_gate().try_lock()) return;
        const auto t0 = std::chrono::steady_clock::now();
        solve_gate().lock();
        waited_ms_ =
            std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - t0)
                .count();
    }
    ~SolveGateLock() {
        if (!held_) return;
        solve_gate().unlock();
        --t_solve_gate_depth;
    }
    SolveGateLock(const SolveGateLock&) = delete;
    SolveGateLock& operator=(const SolveGateLock&) = delete;
    double waited_ms() const { return waited_ms_; }

private:
    bool held_ = false;
    double waited_ms_ = 0.0;
};

}  // namespace

// The single implementation behind fea_static_solve (contact_springs empty — every
// existing caller, parity path untouched) and fea_static_solve_contact (non-empty,
// which switches the solve into the active-set loop and fills *contact_report).
static FeaStaticResult solve_impl(const std::vector<double>& points,
                                 const std::vector<int>& tets,
                                 double E_mpa, double nu, double yield_mpa,
                                 const std::vector<int>& fixed_nodes,
                                 const std::vector<double>& nodal_forces,
                                 int nodes_per_elem,
                                 const std::vector<BondSpring>& bond_springs,
                                 const std::vector<ContactSpring>& contact_springs,
                                 const FeaContactOptions& contact_options,
                                 FeaContactReport* contact_report,
                                 const SimCancelToken& cancel_token) {
    FeaProf prof;
    FeaStaticResult res;
    const int n_nodes = static_cast<int>(points.size() / 3);
    auto cancelled = [&]() {
        return cancel_token && cancel_token->load(std::memory_order_relaxed);
    };
    auto cancel_result = [&]() {
        res.error = "fea_static: cancelled";
        return res;
    };
    if (cancelled()) return cancel_result();
    const int n_dof = n_nodes * 3;
    res.n_dof = n_dof;
    if (n_nodes == 0 || tets.empty()) {
        res.error = "fea_static: empty mesh";
        return res;
    }
    if (points.size() % 3 != 0) {
        res.error = "fea_static: points length must be divisible by 3";
        return res;
    }
    if (static_cast<int>(nodal_forces.size()) != n_dof) {
        res.error = "fea_static: nodal_forces length must be n_nodes*3";
        return res;
    }
    if (nodes_per_elem != 4 && nodes_per_elem != 10 && nodes_per_elem != 8) {
        res.error = "fea_static: nodes_per_elem must be 4 (tet4), 10 (tet10) or 8 (hex8)";
        return res;
    }
    if (tets.size() % static_cast<size_t>(nodes_per_elem) != 0) {
        res.error = "fea_static: tets length must be divisible by nodes_per_elem";
        return res;
    }
    if (!std::isfinite(E_mpa) || E_mpa <= 0.0 || !std::isfinite(nu) || nu <= -1.0 ||
        nu >= 0.5) {
        res.error = "fea_static: material requires E>0 and -1<nu<0.5";
        return res;
    }
    if (!std::isfinite(yield_mpa) || yield_mpa <= 0.0) {
        res.error = "fea_static: yield strength must be finite and > 0";
        return res;
    }
    for (double x : points) {
        if (!std::isfinite(x)) {
            res.error = "fea_static: mesh coordinates must be finite";
            return res;
        }
    }
    for (double x : nodal_forces) {
        if (!std::isfinite(x)) {
            res.error = "fea_static: nodal forces must be finite";
            return res;
        }
    }
    const bool tet10 = (nodes_per_elem == 10);
    const bool hex8 = (nodes_per_elem == 8);
    const int stride = nodes_per_elem;
    for (int nid : tets) {
        if (nid < 0 || nid >= n_nodes) {
            res.error = "fea_static: tet node index out of range";
            return res;
        }
    }
    for (int nid : fixed_nodes) {
        if (nid < 0 || nid >= n_nodes) {
            res.error = "fea_static: fixed node index out of range";
            return res;
        }
    }
    for (const BondSpring& s : bond_springs) {
        if (s.a < 0 || s.a >= n_nodes || s.b < 0 || s.b >= n_nodes || s.a == s.b ||
            !std::isfinite(s.k) || s.k <= 0.0) {
            res.error = "fea_static: invalid bond spring";
            return res;
        }
    }
    // Contact candidates: validated and normalized up front so the active-set loop below
    // never has to defend itself, and so a bad pair is a refusal rather than a silently
    // dropped contact (a missing pair is a missing load path, i.e. a wrong answer).
    std::vector<ContactPair> pairs;
    const bool contact_mode = !contact_springs.empty();
    if (contact_report) *contact_report = FeaContactReport{};
    if (contact_mode) {
        if (contact_options.max_iterations < 1) {
            res.error = "fea_static: contact max_iterations must be >= 1";
            return res;
        }
        if (!std::isfinite(contact_options.tangential_stabilization) ||
            contact_options.tangential_stabilization < 0.0 ||
            contact_options.tangential_stabilization > 1.0) {
            res.error = "fea_static: contact tangential_stabilization must be in [0, 1]";
            return res;
        }
        pairs.reserve(contact_springs.size());
        for (const ContactSpring& s : contact_springs) {
            const double nx = s.normal[0], ny = s.normal[1], nz = s.normal[2];
            const double len = std::sqrt(nx * nx + ny * ny + nz * nz);
            if (s.a < 0 || s.a >= n_nodes || s.b < 0 || s.b >= n_nodes || s.a == s.b ||
                !std::isfinite(s.k) || s.k <= 0.0 || !std::isfinite(s.gap0_mm) ||
                !std::isfinite(s.area_mm2) || s.area_mm2 < 0.0 || !std::isfinite(len) ||
                len <= 1e-12) {
                res.error = "fea_static: invalid contact spring";
                return res;
            }
            ContactPair p;
            p.a = s.a;
            p.b = s.b;
            p.k = s.k;
            p.kt = s.k * contact_options.tangential_stabilization;
            p.gap0 = s.gap0_mm;
            p.area = s.area_mm2;
            p.group = s.group;
            p.n[0] = nx / len;
            p.n[1] = ny / len;
            p.n[2] = nz / len;
            pairs.push_back(p);
        }
        if (contact_report) {
            contact_report->candidate_pairs = static_cast<int>(pairs.size());
            contact_report->tangential_stabilization =
                contact_options.tangential_stabilization;
        }
    }

    double lam, mu;
    lame(E_mpa, nu, lam, mu);
    const Eigen::Matrix<double, 6, 6> C = elasticity(lam, mu);
    if (!C.allFinite()) {
        res.error = "fea_static: material constants overflowed";
        return res;
    }

    auto node_pt = [&](int nid) {
        return Eigen::Vector3d(points[nid * 3], points[nid * 3 + 1], points[nid * 3 + 2]);
    };
    const int n_tets = static_cast<int>(tets.size() / stride);
    const TetGauss4 gauss;
    // Validate every element before assembly. Silently skipping one bad element changes the
    // domain and can leave a plausible-looking solution on only part of the requested mesh.
    for (int e = 0; e < n_tets; ++e) {
        bool valid = true;
        if (hex8) {
            const HexGauss8 hg;
            std::array<Eigen::Vector3d, 8> ep;
            for (int j = 0; j < 8; ++j) ep[j] = node_pt(tets[e * 8 + j]);
            double sign = 0.0;
            for (int g = 0; g < 8; ++g) {
                Eigen::Matrix<double, 6, 24> B;
                double detJ = 0.0;
                if (!hex8_B_at(ep, hg.pt[g][0], hg.pt[g][1],
                               hg.pt[g][2], B, detJ) ||
                    !std::isfinite(detJ) ||
                    (sign != 0.0 && detJ * sign <= 0.0)) {
                    valid = false;
                    break;
                }
                sign = detJ;
            }
        } else if (tet10) {
            std::array<Eigen::Vector3d, 10> ep;
            for (int j = 0; j < 10; ++j) ep[j] = node_pt(tets[e * 10 + j]);
            double sign = 0.0;
            for (int g = 0; g < 4; ++g) {
                Eigen::Matrix<double, 6, 30> B;
                Eigen::Matrix<double, 10, 3> dNxyz;
                double detJ = 0.0;
                if (!tet10_B_at(ep, gauss.pt[g][0], gauss.pt[g][1], gauss.pt[g][2], B,
                                detJ, dNxyz) ||
                    !std::isfinite(detJ) || (sign != 0.0 && detJ * sign <= 0.0)) {
                    valid = false;
                    break;
                }
                sign = detJ;
            }
        } else {
            std::array<Eigen::Vector3d, 4> ep;
            for (int j = 0; j < 4; ++j) ep[j] = node_pt(tets[e * 4 + j]);
            Eigen::Matrix<double, 6, 12> B;
            double vol = 0.0;
            valid = tet4_B(ep, B, vol) && std::isfinite(vol);
        }
        if (!valid) {
            res.error = "fea_static: degenerate or inverted element " + std::to_string(e);
            return res;
        }
        if ((e & 255) == 0 && cancelled()) return cancel_result();
    }

    // Fixed-DOF mask + reduced (free) DOF numbering: dof -> reduced index, or -1 if fixed.
    // tet10/hex8: try prepared-system cache first (load-only repeats skip assembly+factorize).
    // tet4 is never cached: its studies are the cheap legs of a convergence ladder, where a
    // retained factor would hold memory the NEXT, finer leg needs.
    std::shared_ptr<const PreparedSystem> prepared;
    // Contact bypasses the cache entirely: the active set changes K every iteration, and
    // the cache key cannot see an active set (it would serve a stale contact state).
    const bool use_factor_cache = (tet10 || hex8) && !contact_mode;
    uint64_t sys_key = 0;
    if (use_factor_cache) {
        sys_key = factor_system_key(points, tets, E_mpa, nu, fixed_nodes, nodes_per_elem,
                                    bond_springs);
        auto& cs = factor_cache_state();
        std::lock_guard<std::mutex> lk(cs.mtx);
        auto it = cs.map.find(sys_key);
        if (it != cs.map.end()) {
            prepared = it->second;
            cs.lru.remove(sys_key);
            cs.lru.push_front(sys_key);
            ++cs.stats.hits;
            res.factor_cache_hit = true;
            prof.note("factor-cache HIT (skip assemble/analyze/factorize)");
        } else {
            ++cs.stats.misses;
            prof.note("factor-cache MISS");
        }
        // The governor's budget is recomputed live, so a cache that fitted when it was
        // filled can be over budget now (another study, the GUI, a build). Trim on every
        // lookup, not only on store.
        factor_cache_trim_locked(cs, factor_cache_budget());
    }

    std::vector<int> reduced;
    int n_free = 0;
    Eigen::VectorXd uf;
    bool solved = false;
    // Final active set of the contact loop (empty unless contact_mode) — the reaction
    // and report passes below read it, so it outlives the solve branch.
    std::vector<char> contact_active;

    if (prepared) {
        // ── Cache hit: only rebuild RHS and back-substitute ──────────────────
        reduced = prepared->reduced;
        n_free = prepared->n_free;
        prof.mark("setup+dof-map");
        Eigen::VectorXd ff(n_free);
        for (int d = 0; d < n_dof; ++d)
            if (reduced[d] >= 0) ff(reduced[d]) = nodal_forces[d];
        if (!(ff.squaredNorm() > 0.0)) {
            res.error = "fea_static: no non-zero force acts on a free DOF";
            return res;
        }
        if (cancelled()) return cancel_result();
        prof.mark("build-rhs-only");
        const int nthreads = fea_solver_threads(n_free);
        apply_solver_thread_cap(nthreads);
        {
            std::lock_guard<std::mutex> solve_lock(prepared->solve_mtx);
            uf = prepared->solver->solve(ff);
        }
        if (uf.size() == n_free) {
            solved = true;
            res.solver = prepared->label;
            res.conditioning_warning = prepared->conditioning_warning;
            prof.mark("factor-cache back-substitute");
        } else {
            // Stale/failed factor — fall through to full path.
            prepared.reset();
            res.factor_cache_hit = false;
            res.solver.clear();
            prof.note("factor-cache solve failed; rebuilding");
        }
    }

    if (!prepared) {
        std::vector<char> fixed(n_dof, 0);
        for (int nid : fixed_nodes)
            if (nid >= 0 && nid < n_nodes)
                for (int k = 0; k < 3; ++k) fixed[nid * 3 + k] = 1;
        // A node no element and no spring references has NO stiffness: its rows in the
        // reduced system are exactly zero, which makes the matrix singular however it
        // is factored. Eliminating such a node is exact rather than an approximation —
        // nothing couples it, so it cannot influence any other DOF — and it is
        // necessary in practice: the mesher dissolves hanging elements but must keep
        // node numbering 1:1 with the polymesh mesh the adaptive route refines
        // through, so a repaired mesh can carry a few unreferenced nodes.
        //
        // A LOAD on such a node is a different matter: it is a force applied to
        // nothing, and silently dropping it would quietly change the problem, so that
        // refuses.
        {
            std::vector<char> referenced(static_cast<std::size_t>(n_nodes), 0);
            for (int nid : tets)
                if (nid >= 0 && nid < n_nodes) referenced[static_cast<std::size_t>(nid)] = 1;
            for (const BondSpring& s : bond_springs) {
                if (s.a >= 0 && s.a < n_nodes) referenced[static_cast<std::size_t>(s.a)] = 1;
                if (s.b >= 0 && s.b < n_nodes) referenced[static_cast<std::size_t>(s.b)] = 1;
            }
            for (const ContactPair& cp : pairs) {
                referenced[static_cast<std::size_t>(cp.a)] = 1;
                referenced[static_cast<std::size_t>(cp.b)] = 1;
            }
            for (int i = 0; i < n_nodes; ++i) {
                if (referenced[static_cast<std::size_t>(i)]) continue;
                double load2 = 0.0;
                for (int k = 0; k < 3; ++k) {
                    const double f = nodal_forces[static_cast<std::size_t>(i) * 3 + k];
                    load2 += f * f;
                }
                if (load2 > 0.0) {
                    res.error =
                        "fea_static: node " + std::to_string(i) +
                        " carries an applied load but belongs to no element, bond tie or "
                        "contact pair — the force would act on nothing. Re-resolve the "
                        "load onto the meshed body (a node left behind by mesh repair is "
                        "not part of the domain).";
                    return res;
                }
                for (int k = 0; k < 3; ++k) fixed[static_cast<std::size_t>(i) * 3 + k] = 1;
                ++res.unreferenced_nodes_pinned;
            }
        }
        reduced.assign(n_dof, -1);
        n_free = 0;
        for (int d = 0; d < n_dof; ++d)
            if (!fixed[d]) reduced[d] = n_free++;
        if (n_free == 0) {
            res.error = "fea_static: all DOFs constrained";
            return res;
        }
        if (res.unreferenced_nodes_pinned > 0)
            prof.note("%d node(s) reference no element and were eliminated (zero rows)",
                      res.unreferenced_nodes_pinned);

        prof.mark("setup+dof-map");
        // Assemble the reduced free-free stiffness DIRECTLY into a fixed sparsity
        // pattern (see BlockPattern): no triplet list, so the assembly costs 12 bytes
        // per stored nonzero once instead of 16 bytes per element-DOF PAIR. The
        // pattern carries the element cliques plus every bond tie and every contact
        // CANDIDATE, which makes it invariant across the contact active-set loop.
        std::vector<std::array<int, 2>> extra_pairs;
        extra_pairs.reserve(bond_springs.size() + pairs.size());
        for (const BondSpring& s : bond_springs) {
            if (s.a < 0 || s.a >= n_nodes || s.b < 0 || s.b >= n_nodes || s.a == s.b) continue;
            if (!(std::isfinite(s.k) && s.k > 0.0)) continue;
            extra_pairs.push_back({s.a, s.b});
        }
        for (const ContactPair& cp : pairs) extra_pairs.push_back({cp.a, cp.b});

        const BlockPattern bp =
            build_block_pattern(n_nodes, tets, stride, reduced, extra_pairs);
        prof.note("nodes=%d  n_dof=%d  n_elems=%d  nodes/elem=%d  free_dof=%d", n_nodes, n_dof,
                  n_tets, nodes_per_elem, n_free);
        prof.note("pattern: %d free nodes, %d blocks, nnz=%lld (~%.1f MB, %.0f B/elem)",
                  bp.n_free_nodes, bp.blocks(), 9LL * bp.blocks(),
                  static_cast<double>(9LL * bp.blocks()) * (sizeof(double) + sizeof(int)) /
                      1.048576e6,
                  n_tets > 0 ? static_cast<double>(9LL * bp.blocks()) *
                                   (sizeof(double) + sizeof(int)) / n_tets
                             : 0.0);
        Eigen::SparseMatrix<double> Kff;
        {
            std::string perr;
            if (!make_pattern_matrix(bp, Kff, perr)) {
                res.error = perr;
                return res;
            }
        }
        prof.mark("build-sparsity-pattern");
        if (Kff.nonZeros() == 0) {
            res.error = "fea_static: no valid elements contributed stiffness";
            return res;
        }
        double* kval = Kff.valuePtr();
        const int asm_threads = fea_assembly_threads(n_tets);
        bool asm_ok = true;
        if (hex8) {
            // ── hex8 path (8-node trilinear, 2x2x2 Gauss) ────────────────────────
            const HexGauss8 hg;
            asm_ok = assemble_chunked<8>(n_tets, tets, bp, kval,
                                         [&](int e, Eigen::Matrix<double, 24, 24>& Ke) {
                std::array<Eigen::Vector3d, 8> ep;
                for (int j = 0; j < 8; ++j) ep[j] = node_pt(tets[e * 8 + j]);
                Ke.setZero();
                for (int g = 0; g < 8; ++g) {
                    Eigen::Matrix<double, 6, 24> B;
                    double detJ;
                    if (!hex8_B_at(ep, hg.pt[g][0], hg.pt[g][1], hg.pt[g][2], B, detJ))
                        return false;
                    Ke += B.transpose() * C * B * (std::abs(detJ) * hg.w[g]);
                }
                return true;
            }, cancel_token, asm_threads);
        } else if (!tet10) {
            // ── tet4 path (element math UNCHANGED) ───────────────────────────────
            asm_ok = assemble_chunked<4>(n_tets, tets, bp, kval,
                                         [&](int e, Eigen::Matrix<double, 12, 12>& Ke) {
                std::array<Eigen::Vector3d, 4> ep;
                for (int j = 0; j < 4; ++j) ep[j] = node_pt(tets[e * 4 + j]);
                Eigen::Matrix<double, 6, 12> B;
                double vol;
                if (!tet4_B(ep, B, vol)) return false;
                Ke = B.transpose() * C * B * vol;
                return true;
            }, cancel_token, asm_threads);
        } else {
            // ── tet10 path (quadratic isoparametric, 4-point Gauss) ──────────────
            asm_ok = assemble_chunked<10>(n_tets, tets, bp, kval,
                                          [&](int e, Eigen::Matrix<double, 30, 30>& Ke) {
                std::array<Eigen::Vector3d, 10> ep;
                for (int j = 0; j < 10; ++j) ep[j] = node_pt(tets[e * 10 + j]);
                Ke.setZero();
                for (int g = 0; g < 4; ++g) {
                    Eigen::Matrix<double, 6, 30> B;
                    double detJ;
                    Eigen::Matrix<double, 10, 3> dNxyz;
                    if (!tet10_B_at(ep, gauss.pt[g][0], gauss.pt[g][1], gauss.pt[g][2], B,
                                    detJ, dNxyz))
                        return false;
                    Ke += B.transpose() * C * B * (std::abs(detJ) * gauss.w[g]);
                }
                return true;
            }, cancel_token, asm_threads);
        }
        if (cancelled()) return cancel_result();
        if (!asm_ok) {
            res.error = "fea_static: element assembly failed on a degenerate element";
            return res;
        }
        prof.mark("assemble-elements");
        // Bonded gap ties (asm-fea): penalty-spring stiffness scattered AFTER element
        // assembly — 3 diagonal DOF springs per pair, off-diagonal coupling only when
        // both sides are free. A tie into a fixed node grounds the free side, which is
        // the correct penalty behaviour. Default-empty, so single-part solves are
        // untouched.
        if (!bond_springs.empty()) {
            std::size_t n_applied = 0;
            for (const BondSpring& s : bond_springs) {
                if (s.a < 0 || s.a >= n_nodes || s.b < 0 || s.b >= n_nodes || s.a == s.b) continue;
                if (!(std::isfinite(s.k) && s.k > 0.0)) continue;
                for (int d = 0; d < 3; ++d) {
                    add_entry(bp, kval, s.a, s.a, d, d, s.k);
                    add_entry(bp, kval, s.b, s.b, d, d, s.k);
                    add_entry(bp, kval, s.a, s.b, d, d, -s.k);
                    add_entry(bp, kval, s.b, s.a, d, d, -s.k);
                }
                ++n_applied;
            }
            prof.note("bond springs: %zu applied (of %zu requested)", n_applied,
                      bond_springs.size());
        }
        prof.note("Kff %dx%d  nnz=%lld  (~%.1f MB compressed)", n_free, n_free,
                  static_cast<long long>(Kff.nonZeros()),
                  static_cast<double>(Kff.nonZeros()) * (sizeof(double) + sizeof(int)) / 1.048576e6);
        prof.mark("build-sparse-matrix");

        Eigen::VectorXd ff(n_free);
        for (int d = 0; d < n_dof; ++d)
            if (reduced[d] >= 0) ff(reduced[d]) = nodal_forces[d];
        if (!(ff.squaredNorm() > 0.0)) {
            res.error = "fea_static: no non-zero force acts on a free DOF";
            return res;
        }
        if (cancelled()) return cancel_result();

        // Only ONE heavy factorization runs per process at a time (see SolveGateLock):
        // beyond this point the solve holds the host's solve slot, and how long it
        // waited for it is reported so a queued run is not read as a slow one.
        SolveGateLock gate(n_free);
        res.solve_wait_ms = gate.waited_ms();
        if (gate.waited_ms() > 0.0)
            prof.note("solve gate: waited %.0f ms behind another heavy solve",
                      gate.waited_ms());
        // The reduced stiffness (Dirichlet rows eliminated during assembly) is
        // symmetric positive definite for every element order, so the factorization
        // ladder is Cholesky-first — supernodal LLT, then SimplicialLDLT(AMD) — with
        // SparseLU only for a non-positive pivot. See factor_and_solve / SolverKind.
        if (contact_mode) {
            // ── Unilateral frictionless contact: active-set loop ──────────────
            // Kff (elements + bond ties) is the CACHED part: an iteration only rebuilds
            // the spring block Ks from the current active set and factors Kff + Ks. The
            // element assembly above never runs twice.
            //
            // Per active pair, with n̂ the pair normal and g = gap0 + n̂·(u_b - u_a):
            //   stiffness  k·(n̂ n̂ᵀ) on the (a,b) block, + kt·(I - n̂ n̂ᵀ) stabilization
            //   RHS        +k·gap0·n̂ at a, -k·gap0·n̂ at b   (the constant part of k·g)
            // so the solved pair carries the normal force k·g: negative (compressive)
            // while penetrating, positive (tensile — not allowed) once separated.
            const int n_pairs = static_cast<int>(pairs.size());
            contact_active.assign(static_cast<std::size_t>(n_pairs), 1);

            // Reachability skeleton: node connectivity through ELEMENTS and bond ties,
            // which never changes. Each iteration adds only its active contact pairs.
            std::vector<int> base_parent(static_cast<std::size_t>(n_nodes));
            for (int i = 0; i < n_nodes; ++i) base_parent[static_cast<std::size_t>(i)] = i;
            auto uf_find = [](std::vector<int>& p, int x) {
                while (p[static_cast<std::size_t>(x)] != x) {
                    p[static_cast<std::size_t>(x)] =
                        p[static_cast<std::size_t>(p[static_cast<std::size_t>(x)])];
                    x = p[static_cast<std::size_t>(x)];
                }
                return x;
            };
            auto uf_unite = [&](std::vector<int>& p, int a, int b) {
                a = uf_find(p, a);
                b = uf_find(p, b);
                if (a != b) p[static_cast<std::size_t>(std::max(a, b))] = std::min(a, b);
            };
            std::vector<char> meshed(static_cast<std::size_t>(n_nodes), 0);
            for (int e = 0; e < n_tets; ++e) {
                const int* tn = &tets[static_cast<std::size_t>(e) * stride];
                for (int j = 0; j < stride; ++j) meshed[static_cast<std::size_t>(tn[j])] = 1;
                for (int j = 1; j < stride; ++j) uf_unite(base_parent, tn[0], tn[j]);
            }
            for (const BondSpring& s : bond_springs) uf_unite(base_parent, s.a, s.b);

            // The pattern already carries every CANDIDATE pair, so K's sparsity is
            // invariant across the loop: an iteration restores the element+bond values
            // and adds only the ACTIVE springs. The symbolic analysis (ordering +
            // elimination tree) is therefore paid ONCE and every iteration is a numeric
            // refactorization — the expensive half of a direct solve stops being
            // repeated work.
            const std::size_t knnz = static_cast<std::size_t>(Kff.nonZeros());
            std::vector<double> base_values(kval, kval + knnz);
            auto solver = make_solver(requested_solver_kind());
            const bool symbolic_ok = solver->analyze(Kff);
            prof.mark("contact symbolic analyze");
            std::string solver_label;
            auto solve_iteration = [&](const Eigen::VectorXd& rhs,
                                       Eigen::VectorXd& out) -> bool {
                const int nthreads = fea_solver_threads(n_free);
                apply_solver_thread_cap(nthreads);
                if (symbolic_ok && solver->factorize(Kff)) {
                    out = solver->solve(rhs);
                    if (out.size() == n_free) {
                        solver_label = solver->label();
                        return true;
                    }
                }
                // This active set made the penalty-augmented system indefinite for the
                // leading factorization. Walk the SAME ladder the static path uses, on
                // this iteration only — the cached symbolic analysis above stays valid
                // for the next one, since the pattern never changed. And record the
                // verdict: the penalty-augmented system is SPD too whenever the active
                // set actually constrains every part, so a refusal here says the model
                // is a mechanism at solver precision, not that the solver is fussy.
                for (SolverKind k : {SolverKind::Ldlt, SolverKind::Lu}) {
                    auto alt = make_solver(k);
                    if (!alt->analyze(Kff) || !alt->factorize(Kff)) continue;
                    out = alt->solve(rhs);
                    if (out.size() != n_free) continue;
                    solver_label = std::string(alt->label()) + " (fallback: " +
                                   solver->label() + " reported a non-positive pivot)";
                    res.conditioning_warning =
                        "the strict Cholesky rejected the penalty-augmented contact "
                        "stiffness as not positive definite, so this active set leaves a "
                        "(near-)zero-stiffness mode: the contact set does not fully "
                        "constrain every part, or a sliver element carries the mode. The "
                        "displacements and contact forces reported are not trustworthy.";
                    return true;
                }
                return false;
            };

            std::vector<double> u_iter(static_cast<std::size_t>(n_dof), 0.0);
            std::vector<char> next_active(static_cast<std::size_t>(n_pairs), 0);
            // The set from two iterations back: an active-set loop can CHATTER between
            // two states (a pair on the edge of closing). Detecting the 2-cycle stops at
            // iteration 3 instead of paying max_iterations factorizations for a result
            // that is reported non-converged either way.
            std::vector<char> prev_prev_active;
            std::vector<Eigen::Triplet<double>> spring_trips;
            bool set_stable = false;
            const int max_it = contact_options.max_iterations;
            for (int iter = 1; iter <= max_it; ++iter) {
                if (cancelled()) return cancel_result();
                if (contact_report) contact_report->contact_iterations = iter;
                // (1) Mechanism gate on the CURRENT active set. A part whose contact has
                // fully separated is free to fly off; its "solution" is arbitrary.
                {
                    std::vector<int> parent = base_parent;
                    for (int p = 0; p < n_pairs; ++p)
                        if (contact_active[static_cast<std::size_t>(p)])
                            uf_unite(parent, pairs[static_cast<std::size_t>(p)].a,
                                     pairs[static_cast<std::size_t>(p)].b);
                    std::set<int> anchored;
                    for (int nid : fixed_nodes) anchored.insert(uf_find(parent, nid));
                    std::set<int> floating;
                    for (int i = 0; i < n_nodes; ++i) {
                        if (!meshed[static_cast<std::size_t>(i)]) continue;
                        const int root = uf_find(parent, i);
                        if (!anchored.count(root)) floating.insert(root);
                    }
                    if (!floating.empty()) {
                        int still_active = 0;
                        for (char c : contact_active) still_active += c ? 1 : 0;
                        res.error =
                            "fea_static: contact separated — " +
                            std::to_string(floating.size()) +
                            " mesh component(s) reach no fixture with " +
                            std::to_string(still_active) + " of " +
                            std::to_string(n_pairs) +
                            " candidate contact pair(s) still active (iteration " +
                            std::to_string(iter) +
                            "). The load pulls the parts APART, so nothing holds them: a "
                            "unilateral contact carries compression only. Reverse the load "
                            "into the joint, or fixture the lifting part.";
                        return res;
                    }
                }
                // (2) Spring values for this active set. Restoring the element+bond
                // values first RELEASES every pair: an inactive pair contributes
                // exactly 0.0 stiffness and 0.0 rhs, so a separated pair can never pull
                // its partner back.
                std::memcpy(kval, base_values.data(), knnz * sizeof(double));
                Eigen::VectorXd ffc = ff;
                for (int p = 0; p < n_pairs; ++p) {
                    if (!contact_active[static_cast<std::size_t>(p)]) continue;
                    const ContactPair& cp = pairs[static_cast<std::size_t>(p)];
                    for (int i = 0; i < 3; ++i) {
                        for (int j = 0; j < 3; ++j) {
                            // k·n̂n̂ᵀ + kt·(I - n̂n̂ᵀ)
                            const double kij = (cp.k - cp.kt) * cp.n[i] * cp.n[j] +
                                               (i == j ? cp.kt : 0.0);
                            if (kij == 0.0) continue;
                            add_entry(bp, kval, cp.a, cp.a, i, j, kij);
                            add_entry(bp, kval, cp.b, cp.b, i, j, kij);
                            add_entry(bp, kval, cp.a, cp.b, i, j, -kij);
                            add_entry(bp, kval, cp.b, cp.a, i, j, -kij);
                        }
                        const double gap_force = cp.k * cp.gap0 * cp.n[i];
                        const int rai = reduced[cp.a * 3 + i];
                        const int rbi = reduced[cp.b * 3 + i];
                        if (rai >= 0) ffc(rai) += gap_force;
                        if (rbi >= 0) ffc(rbi) -= gap_force;
                    }
                }
                if (!solve_iteration(ffc, uf)) {
                    res.error =
                        "fea_static: contact iteration " + std::to_string(iter) +
                        " failed to factor the penalty-augmented system (singular): the "
                        "active contact set does not constrain every free part.";
                    return res;
                }
                prof.mark("contact-iteration");
                // (3) Gap per pair from this iterate, then the new active set:
                // penetrating (g < 0) is active, separated (g >= 0) is not.
                for (int d = 0; d < n_dof; ++d)
                    u_iter[static_cast<std::size_t>(d)] =
                        reduced[d] >= 0 ? uf(reduced[d]) : 0.0;
                int n_changed = 0;
                for (int p = 0; p < n_pairs; ++p) {
                    const ContactPair& cp = pairs[static_cast<std::size_t>(p)];
                    double rel = 0.0;
                    for (int i = 0; i < 3; ++i)
                        rel += cp.n[i] * (u_iter[static_cast<std::size_t>(cp.b) * 3 + i] -
                                          u_iter[static_cast<std::size_t>(cp.a) * 3 + i]);
                    const char now = (cp.gap0 + rel) < 0.0 ? 1 : 0;
                    next_active[static_cast<std::size_t>(p)] = now;
                    if (now != contact_active[static_cast<std::size_t>(p)]) ++n_changed;
                }
                prof.note("contact iteration %d: %d pair(s) changed state", iter, n_changed);
                solved = true;  // uf holds this iterate's solution
                if (n_changed == 0) {
                    set_stable = true;
                    break;
                }
                // Out of iterations, or chattering between two sets: keep contact_active
                // as the set that produced `uf`, so the reactions and the per-pair report
                // below describe the same state the displacements came from. converged
                // stays false either way.
                if (iter == max_it) break;
                if (next_active == prev_prev_active) {
                    prof.note("contact: active set is chattering between two states");
                    break;
                }
                prev_prev_active = contact_active;
                contact_active = next_active;
            }
            if (contact_report) contact_report->converged = set_stable;
            res.solver = solver_label;
        } else {
            // ── One factorization path for every element order ────────────────
            // The SPD ladder from factor_and_solve. tet4 used to be pinned to SparseLU
            // for byte-identity with a Python/scipy oracle; that harness no longer
            // exists in the tree, and an LU of an SPD matrix costs both triangular
            // factors under an ordering chosen for pivoting rather than fill — measured
            // at 20.5 s / 1.37 GB where the supernodal Cholesky needs 0.9 s / 0.47 GB
            // on the same 93750-element tet4 system.
            const int nthreads = fea_solver_threads(n_free);
            apply_solver_thread_cap(nthreads);
            std::string label;
            prof.note("solver=%s (%d threads)", make_solver(requested_solver_kind())->label(),
                      nthreads);
            bool spd_rejected = false;
            std::unique_ptr<FactorSolver> built =
                factor_and_solve(Kff, ff, uf, label, spd_rejected);
            prof.mark("analyze+factorize+solve");
            if (!built) {
                res.error = "fea_static: sparse factorization failed (singular system)";
                return res;
            }
            solved = true;
            res.solver = label;
            prof.note("solver used=%s  factor=%.1f MB", label.c_str(),
                      static_cast<double>(built->factor_bytes()) / 1.048576e6);
            // A strict Cholesky refusing this matrix means the MODEL has a
            // (near-)zero-stiffness mode — see factor_and_solve. Say so, and localize
            // it: the usual carrier is an element sharing TWO CORNERS OR FEWER with
            // the rest of the mesh, which can rotate about that node or edge at zero
            // strain energy while every component/fixture check passes (the fragment
            // does reach a fixture, through the node it hangs from). A corner used by
            // only one element is NOT itself the defect — a tet apex nobody else uses
            // is fully constrained by its own element — so the count that matters is
            // the hanging elements, with the tip count kept only as context.
            if (spd_rejected) {
                std::vector<int> corner_valence(static_cast<std::size_t>(n_nodes), 0);
                const int corners = tet10 ? 4 : (hex8 ? 8 : 4);
                for (int e = 0; e < n_tets; ++e)
                    for (int j = 0; j < corners; ++j)
                        ++corner_valence[static_cast<std::size_t>(
                            tets[static_cast<std::size_t>(e) * stride + j])];
                int tips = 0;
                for (int i = 0; i < n_nodes; ++i)
                    if (corner_valence[static_cast<std::size_t>(i)] == 1) ++tips;
                int hanging = 0;
                int first_hanging = -1;
                for (int e = 0; e < n_tets && n_tets > 1; ++e) {
                    int shared = 0;
                    for (int j = 0; j < corners; ++j)
                        if (corner_valence[static_cast<std::size_t>(
                                tets[static_cast<std::size_t>(e) * stride + j])] > 1)
                            ++shared;
                    if (shared > 2) continue;
                    ++hanging;
                    if (first_hanging < 0) first_hanging = e;
                }
                res.conditioning_warning =
                    "the strict Cholesky rejected the reduced stiffness as not positive "
                    "definite. A linear-elastic reduced stiffness is positive definite by "
                    "construction, so this model has a (near-)zero-stiffness mode and the "
                    "displacement/stress/FoS it produced are NOT trustworthy — not even "
                    "with a small equilibrium residual, which stays small precisely in the "
                    "directions the load excites while the soft mode is unconstrained. " +
                    (hanging > 0
                         ? std::to_string(hanging) +
                               " element(s) share two corners or fewer with the rest of "
                               "the mesh (first: element " +
                               std::to_string(first_hanging) +
                               ") and can rotate about that node or edge at zero strain "
                               "energy — that is the mechanism. Re-mesh from geometry (the "
                               "mesher dissolves these) or delete them from an inline mesh."
                         : std::string("No element hangs by two corners or fewer, so look "
                                       "for a near-degenerate sliver, a fragment attached "
                                       "through a single edge of a higher-order element, or "
                                       "a fixture that pins too little to remove the "
                                       "rigid-body modes.")) +
                    " (" + std::to_string(tips) +
                    " corner(s) are used by exactly one element, which is ordinary in "
                    "itself and reported only as context.)";
                prof.note("conditioning: %s", res.conditioning_warning.c_str());
            }
            // Store prepared system for load-only reuse (governor-bounded).
            if (use_factor_cache) {
                auto ps = std::make_shared<PreparedSystem>();
                ps->reduced = reduced;
                ps->n_free = n_free;
                ps->n_dof = n_dof;
                ps->nnz = static_cast<std::size_t>(Kff.nonZeros());
                ps->factor_bytes = built->factor_bytes();
                ps->label = label;
                ps->conditioning_warning = res.conditioning_warning;
                ps->solver = std::move(built);
                const uint64_t need = static_cast<uint64_t>(ps->bytes());
                const uint64_t budget = factor_cache_budget();
                auto& cs = factor_cache_state();
                std::lock_guard<std::mutex> lk(cs.mtx);
                // Make room for this entry, then keep the whole cache inside the live
                // governor budget (which shrinks when the host gets busy).
                while (!cs.lru.empty() && cs.resident + need > budget) {
                    auto bit = cs.map.find(cs.lru.back());
                    if (bit != cs.map.end()) {
                        cs.resident -= std::min<uint64_t>(
                            cs.resident, static_cast<uint64_t>(bit->second->bytes()));
                        cs.map.erase(bit);
                        ++cs.stats.evictions;
                    }
                    cs.lru.pop_back();
                }
                // A factor bigger than the entire budget is not worth holding: the next
                // solve needs that memory more than a possible repeat does.
                if (need <= budget && cs.map.find(sys_key) == cs.map.end()) {
                    cs.map[sys_key] = ps;
                    cs.lru.push_front(sys_key);
                    cs.resident += need;
                    ++cs.stats.stores;
                }
                factor_cache_trim_locked(cs, budget);
            }
        }
        if (cancelled()) return cancel_result();
    }

    if (!solved) {
        res.error = "fea_static: sparse factorization failed (singular system)";
        return res;
    }

    // Scatter back to the full DOF vector (fixed DOFs stay zero).
    std::vector<double> u_full(n_dof, 0.0);
    for (int d = 0; d < n_dof; ++d)
        if (reduced[d] >= 0) u_full[d] = uf(reduced[d]);
    for (double x : u_full) {
        if (!std::isfinite(x)) {
            res.error = "fea_static: sparse solve produced non-finite displacement";
            return res;
        }
    }

    // Displacement + magnitude.
    res.displacement = u_full;
    res.disp_magnitude.assign(n_nodes, 0.0);
    for (int i = 0; i < n_nodes; ++i) {
        double x = u_full[i * 3], y = u_full[i * 3 + 1], z = u_full[i * 3 + 2];
        res.disp_magnitude[i] = std::sqrt(x * x + y * y + z * z);
    }

    // ── Support reactions (equilibrium evidence) ─────────────────────────────
    // R_d = (K u)_d - f_d on each constrained DOF. Only elements incident to a
    // fixed node contribute stiffness there, so K u is re-formed on that boundary
    // layer alone — a fraction of one assembly — instead of retaining the full
    // stiffness matrix (the assembled system is the REDUCED free-free block and
    // has no constrained rows to read). Internal forces self-equilibrate and the
    // free equations are satisfied exactly, so R + F is zero in exact arithmetic:
    // the reported error is the residual of the factorization, not a physical
    // imbalance. Parity-safe — nothing here touches res.displacement/von_mises.
    {
        std::vector<char> fixed_dof(static_cast<std::size_t>(n_dof), 0);
        std::vector<char> fixed_node(static_cast<std::size_t>(n_nodes), 0);
        for (int nid : fixed_nodes) {
            if (nid < 0 || nid >= n_nodes) continue;
            fixed_node[static_cast<std::size_t>(nid)] = 1;
            for (int k = 0; k < 3; ++k) fixed_dof[static_cast<std::size_t>(nid) * 3 + k] = 1;
        }
        std::vector<double> f_int(static_cast<std::size_t>(n_dof), 0.0);
        const HexGauss8 hg;
        for (int e = 0; e < n_tets; ++e) {
            const int* tn = &tets[static_cast<std::size_t>(e) * stride];
            bool touches_fixed = false;
            for (int j = 0; j < stride; ++j)
                touches_fixed = touches_fixed || fixed_node[static_cast<std::size_t>(tn[j])];
            if (!touches_fixed) continue;
            auto scatter = [&](int nodes, const double* fe) {
                for (int j = 0; j < nodes; ++j)
                    for (int k = 0; k < 3; ++k) {
                        const std::size_t d = static_cast<std::size_t>(tn[j]) * 3 + k;
                        if (fixed_dof[d]) f_int[d] += fe[j * 3 + k];
                    }
            };
            if (hex8) {
                std::array<Eigen::Vector3d, 8> ep;
                for (int j = 0; j < 8; ++j) ep[j] = node_pt(tn[j]);
                Eigen::Matrix<double, 24, 1> ue, fe;
                for (int j = 0; j < 8; ++j)
                    for (int k = 0; k < 3; ++k) ue(j * 3 + k) = u_full[tn[j] * 3 + k];
                fe.setZero();
                for (int g = 0; g < 8; ++g) {
                    Eigen::Matrix<double, 6, 24> B;
                    double detJ;
                    if (!hex8_B_at(ep, hg.pt[g][0], hg.pt[g][1], hg.pt[g][2], B, detJ)) continue;
                    fe += B.transpose() * (C * (B * ue)) * (std::abs(detJ) * hg.w[g]);
                }
                scatter(8, fe.data());
            } else if (tet10) {
                std::array<Eigen::Vector3d, 10> ep;
                for (int j = 0; j < 10; ++j) ep[j] = node_pt(tn[j]);
                Eigen::Matrix<double, 30, 1> ue, fe;
                for (int j = 0; j < 10; ++j)
                    for (int k = 0; k < 3; ++k) ue(j * 3 + k) = u_full[tn[j] * 3 + k];
                fe.setZero();
                for (int g = 0; g < 4; ++g) {
                    Eigen::Matrix<double, 6, 30> B;
                    Eigen::Matrix<double, 10, 3> dNxyz;
                    double detJ;
                    if (!tet10_B_at(ep, gauss.pt[g][0], gauss.pt[g][1], gauss.pt[g][2], B,
                                    detJ, dNxyz))
                        continue;
                    fe += B.transpose() * (C * (B * ue)) * (std::abs(detJ) * gauss.w[g]);
                }
                scatter(10, fe.data());
            } else {
                std::array<Eigen::Vector3d, 4> ep;
                for (int j = 0; j < 4; ++j) ep[j] = node_pt(tn[j]);
                Eigen::Matrix<double, 6, 12> B;
                double vol;
                if (!tet4_B(ep, B, vol)) continue;
                Eigen::Matrix<double, 12, 1> ue;
                for (int j = 0; j < 4; ++j)
                    for (int k = 0; k < 3; ++k) ue(j * 3 + k) = u_full[tn[j] * 3 + k];
                const Eigen::Matrix<double, 12, 1> fe =
                    B.transpose() * (C * (B * ue)) * vol;
                scatter(4, fe.data());
            }
        }
        // Penalty ties carry load too: their internal force at a fixed DOF is part
        // of that support's reaction.
        for (const BondSpring& s : bond_springs) {
            if (s.a < 0 || s.a >= n_nodes || s.b < 0 || s.b >= n_nodes || s.a == s.b) continue;
            if (!(std::isfinite(s.k) && s.k > 0.0)) continue;
            for (int d = 0; d < 3; ++d) {
                const std::size_t da = static_cast<std::size_t>(s.a) * 3 + d;
                const std::size_t db = static_cast<std::size_t>(s.b) * 3 + d;
                const double stretch = s.k * (u_full[da] - u_full[db]);
                if (fixed_dof[da]) f_int[da] += stretch;
                if (fixed_dof[db]) f_int[db] -= stretch;
            }
        }
        // Active contact pairs are load-carrying members: their internal force at a
        // fixed DOF belongs to that support's reaction exactly as a bond tie's does.
        // f_int_a = -k·g·n̂ - kt·(I-n̂n̂ᵀ)(u_b-u_a),  f_int_b = -f_int_a, with
        // g = gap0 + n̂·(u_b - u_a) — the constant gap0 term included, which is why the
        // support carries the load rather than the penalty offset.
        for (int p = 0; p < static_cast<int>(pairs.size()); ++p) {
            if (contact_active.empty() || !contact_active[static_cast<std::size_t>(p)])
                continue;
            const ContactPair& cp = pairs[static_cast<std::size_t>(p)];
            double du[3];
            double rel_n = 0.0;
            for (int d = 0; d < 3; ++d) {
                du[d] = u_full[static_cast<std::size_t>(cp.b) * 3 + d] -
                        u_full[static_cast<std::size_t>(cp.a) * 3 + d];
                rel_n += cp.n[d] * du[d];
            }
            const double gap = cp.gap0 + rel_n;
            for (int d = 0; d < 3; ++d) {
                const double tangential = cp.kt * (du[d] - rel_n * cp.n[d]);
                const double force_b = cp.k * gap * cp.n[d] + tangential;
                const std::size_t da = static_cast<std::size_t>(cp.a) * 3 + d;
                const std::size_t db = static_cast<std::size_t>(cp.b) * 3 + d;
                if (fixed_dof[da]) f_int[da] -= force_b;
                if (fixed_dof[db]) f_int[db] += force_b;
            }
        }
        double reaction[3] = {0.0, 0.0, 0.0}, applied[3] = {0.0, 0.0, 0.0};
        double m_reaction[3] = {0.0, 0.0, 0.0}, m_applied[3] = {0.0, 0.0, 0.0};
        auto add_moment = [](double* into, const double* r, const double* f) {
            into[0] += r[1] * f[2] - r[2] * f[1];
            into[1] += r[2] * f[0] - r[0] * f[2];
            into[2] += r[0] * f[1] - r[1] * f[0];
        };
        for (int i = 0; i < n_nodes; ++i) {
            const double* r = &points[static_cast<std::size_t>(i) * 3];
            double node_reaction[3], node_applied[3];
            for (int k = 0; k < 3; ++k) {
                const std::size_t d = static_cast<std::size_t>(i) * 3 + k;
                node_applied[k] = nodal_forces[d];
                node_reaction[k] = fixed_dof[d] ? f_int[d] - nodal_forces[d] : 0.0;
                reaction[k] += node_reaction[k];
                applied[k] += node_applied[k];
            }
            add_moment(m_reaction, r, node_reaction);
            add_moment(m_applied, r, node_applied);
        }
        double imbalance2 = 0.0, applied2 = 0.0;
        for (int k = 0; k < 3; ++k) {
            res.reaction_force_n[static_cast<std::size_t>(k)] = reaction[k];
            res.applied_force_n[static_cast<std::size_t>(k)] = applied[k];
            res.reaction_moment_nmm[static_cast<std::size_t>(k)] = m_reaction[k];
            res.applied_moment_nmm[static_cast<std::size_t>(k)] = m_applied[k];
            const double sum = reaction[k] + applied[k];
            imbalance2 += sum * sum;
            applied2 += applied[k] * applied[k];
        }
        res.equilibrium_error_pct =
            std::sqrt(imbalance2) / std::max(std::sqrt(applied2), 1e-9) * 100.0;
        prof.mark("reactions");
    }

    // ── Contact report: per-body-pair resultant, peak pressure, stabilizer share ──
    // Everything here is measured off the SAME iterate the displacements came from.
    // Compression is positive along n̂ (body A pushing body B away); the peak pressure
    // is the worst single pair's normal force over its own tributary node area, which is
    // a local contact pressure, not a resultant divided by a nominal patch.
    if (contact_mode && contact_report) {
        std::map<int, FeaContactGroup> by_group;
        double sum_normal = 0.0, sum_tangential = 0.0;
        int total_active = 0;
        for (std::size_t p = 0; p < pairs.size(); ++p) {
            const ContactPair& cp = pairs[p];
            auto it = by_group.find(cp.group);
            if (it == by_group.end()) {
                FeaContactGroup fresh;
                fresh.group = cp.group;
                it = by_group.emplace(cp.group, fresh).first;
            }
            FeaContactGroup& g = it->second;
            ++g.candidate_pairs;
            if (contact_active.empty() || !contact_active[p]) continue;
            double du[3];
            double rel_n = 0.0;
            for (int d = 0; d < 3; ++d) {
                du[d] = u_full[static_cast<std::size_t>(cp.b) * 3 + d] -
                        u_full[static_cast<std::size_t>(cp.a) * 3 + d];
                rel_n += cp.n[d] * du[d];
            }
            const double gap = cp.gap0 + rel_n;
            const double compression = -cp.k * gap;  // > 0 while pressed together
            ++g.active_pairs;
            ++total_active;
            for (int d = 0; d < 3; ++d) g.force_n[static_cast<std::size_t>(d)] += compression * cp.n[d];
            double t2 = 0.0;
            for (int d = 0; d < 3; ++d) {
                const double t = cp.kt * (du[d] - rel_n * cp.n[d]);
                t2 += t * t;
            }
            sum_normal += std::abs(compression);
            sum_tangential += std::sqrt(t2);
            if (cp.area > 0.0)
                g.max_contact_pressure_mpa =
                    std::max(g.max_contact_pressure_mpa, compression / cp.area);
            g.max_penetration_mm = std::max(g.max_penetration_mm, -gap);
        }
        contact_report->groups.clear();
        contact_report->groups.reserve(by_group.size());
        for (auto& kv : by_group) {
            FeaContactGroup& g = kv.second;
            g.contact_force_n = std::sqrt(g.force_n[0] * g.force_n[0] +
                                          g.force_n[1] * g.force_n[1] +
                                          g.force_n[2] * g.force_n[2]);
            contact_report->groups.push_back(g);
        }
        contact_report->active_pairs = total_active;
        contact_report->tangential_force_fraction =
            sum_normal > 0.0 ? sum_tangential / sum_normal : 0.0;
    }
    if (cancelled()) return cancel_result();

    // Per-element stress -> node-averaged. The same pass accumulates von Mises and the
    // full symmetric stress tensor (sx,sy,sz,txy,tyz,txz) per node with the SAME
    // vm_count divisor, so every reported field comes from one consistent average.
    //
    // tet4 and hex8 contribute ONE state to each of their nodes (tet4's stress really is
    // constant; hex8 keeps its Gauss-point average). tet10 does NOT, and that was a bug:
    // a quadratic element's stress varies over the element, so collapsing its 4 Gauss
    // points to a constant before the nodal average smooths the field twice and clips
    // every peak. Measured on the V&V plate-with-a-hole (Kirsch SCF 3.0, at h_eff
    // 3.45/2.87/2.59 mm): the element-constant form recovered SCF 1.47/1.76/2.44 where
    // CalculiX C3D10 on the IDENTICAL mesh and loads recovered 2.23/2.58/3.44 — tet10
    // read WORSE than tet4 at the concentration it exists to resolve, while the same
    // solve's far-field stress was right to 6e-4 and its displacements matched ccx to
    // 1e-7. So the 4 Gauss-point tensors are extrapolated to the corner nodes and each
    // mid-edge node takes the mean of its two corners — the C3D10 recovery, see
    // TET10_GP_AT_CORNER. tet4/hex8 recovery is unchanged.
    std::vector<double> vm_accum(n_nodes, 0.0);
    std::vector<int> vm_count(n_nodes, 0);
    std::vector<double> sx_accum(n_nodes, 0.0), sy_accum(n_nodes, 0.0), sz_accum(n_nodes, 0.0);
    std::vector<double> txy_accum(n_nodes, 0.0), tyz_accum(n_nodes, 0.0), txz_accum(n_nodes, 0.0);
    // Recovery runs in CHUNKS: a chunk's element stresses are computed in PARALLEL into
    // a bounded buffer (~4 MB), then scattered SERIALLY in element order. The previous
    // shape materialized one ElemStress for every element in the mesh before scattering
    // any of them — on a 200k-element tet10 that is pure staging held next to the
    // factor, and it scaled with the mesh instead of being bounded. Scattering in
    // element order keeps the summation order (and hence the result) independent of the
    // host's core count.
    struct NodeStress { double vm, sx, sy, sz, txy, tyz, txz; };
    struct ElemStress { NodeStress n[10]; bool ok; };
    const int st_threads = fea_assembly_threads(n_tets);
    const int stress_chunk =
        std::max(1, static_cast<int>((4ull << 20) / sizeof(ElemStress)));
    std::vector<ElemStress> es(
        static_cast<std::size_t>(std::max(1, std::min(n_tets, stress_chunk))), ElemStress{});
    const HexGauss8 hg_stress;
    auto node_state = [](const Eigen::Matrix<double, 6, 1>& s) -> NodeStress {
        const double sx = s(0), sy = s(1), sz = s(2);
        const double txy = s(3), tyz = s(4), txz = s(5);
        const double vm = std::sqrt(0.5 * ((sx - sy) * (sx - sy) + (sy - sz) * (sy - sz) +
                                           (sz - sx) * (sz - sx) +
                                           6.0 * (txy * txy + tyz * tyz + txz * txz)));
        return NodeStress{vm, sx, sy, sz, txy, tyz, txz};
    };
    // One element's recovered stress AT EACH OF ITS NODES, for whichever order this
    // mesh carries.
    auto elem_stress = [&](int e) -> ElemStress {
        ElemStress out{};
        if (hex8) {
            std::array<Eigen::Vector3d, 8> ep;
            std::array<int, 8> tn;
            for (int j = 0; j < 8; ++j) { tn[j] = tets[e * 8 + j]; ep[j] = node_pt(tn[j]); }
            Eigen::Matrix<double, 24, 1> ue;
            for (int j = 0; j < 8; ++j)
                for (int k = 0; k < 3; ++k) ue(j * 3 + k) = u_full[tn[j] * 3 + k];
            Eigen::Matrix<double, 6, 1> stress_avg = Eigen::Matrix<double, 6, 1>::Zero();
            int ngp = 0;
            for (int g = 0; g < 8; ++g) {
                Eigen::Matrix<double, 6, 24> B;
                double detJ;
                if (!hex8_B_at(ep, hg_stress.pt[g][0], hg_stress.pt[g][1], hg_stress.pt[g][2],
                               B, detJ))
                    continue;
                stress_avg += C * (B * ue);
                ++ngp;
            }
            if (ngp == 0) return out;
            stress_avg /= static_cast<double>(ngp);
            const NodeStress st = node_state(stress_avg);
            for (int j = 0; j < 8; ++j) out.n[j] = st;
        } else if (tet10) {
            std::array<Eigen::Vector3d, 10> ep;
            std::array<int, 10> tn;
            for (int j = 0; j < 10; ++j) { tn[j] = tets[e * 10 + j]; ep[j] = node_pt(tn[j]); }
            Eigen::Matrix<double, 30, 1> ue;
            for (int j = 0; j < 10; ++j)
                for (int k = 0; k < 3; ++k) ue(j * 3 + k) = u_full[tn[j] * 3 + k];
            Eigen::Matrix<double, 6, 4> gp = Eigen::Matrix<double, 6, 4>::Zero();
            int ngp = 0;
            for (int g = 0; g < 4; ++g) {
                Eigen::Matrix<double, 6, 30> B;
                double detJ;
                Eigen::Matrix<double, 10, 3> dNxyz;
                if (!tet10_B_at(ep, gauss.pt[g][0], gauss.pt[g][1], gauss.pt[g][2], B, detJ,
                                dNxyz))
                    continue;
                gp.col(ngp++) = C * (B * ue);
            }
            if (ngp == 4) {
                const Eigen::Matrix<double, 6, 1> gsum = gp.rowwise().sum();
                Eigen::Matrix<double, 6, 4> cn;
                for (int i = 0; i < 4; ++i)
                    cn.col(i) = TET10_EXTRAP_SCALE *
                                (gp.col(TET10_GP_AT_CORNER[i]) - TET10_EXTRAP_B * gsum);
                for (int i = 0; i < 4; ++i) out.n[i] = node_state(cn.col(i));
                for (int m = 0; m < 6; ++m)
                    out.n[4 + m] = node_state(
                        0.5 * (cn.col(TET10_MID_EDGE[m][0]) + cn.col(TET10_MID_EDGE[m][1])));
            } else if (ngp > 0) {
                // A degenerate Gauss point leaves the extrapolation ill-posed (the map
                // needs all four): fall back to the mean of the points that did
                // evaluate, element-constant, rather than dropping the element.
                const NodeStress st = node_state(gp.leftCols(ngp).rowwise().sum() /
                                                 static_cast<double>(ngp));
                for (int j = 0; j < 10; ++j) out.n[j] = st;
            } else {
                return out;
            }
        } else {
            std::array<Eigen::Vector3d, 4> ep;
            std::array<int, 4> tn;
            for (int j = 0; j < 4; ++j) { tn[j] = tets[e * 4 + j]; ep[j] = node_pt(tn[j]); }
            Eigen::Matrix<double, 6, 12> B;
            double vol;
            if (!tet4_B(ep, B, vol)) return out;
            Eigen::Matrix<double, 12, 1> ue;
            for (int j = 0; j < 4; ++j)
                for (int k = 0; k < 3; ++k) ue(j * 3 + k) = u_full[tn[j] * 3 + k];
            const NodeStress st = node_state(C * (B * ue));
            for (int j = 0; j < 4; ++j) out.n[j] = st;
        }
        out.ok = true;
        return out;
    };
    for (int c0 = 0; c0 < n_tets; c0 += stress_chunk) {
        const int c1 = std::min(n_tets, c0 + stress_chunk);
#if defined(CHUDWARE_HAVE_OPENMP)
#pragma omp parallel for schedule(static) num_threads(st_threads) if (st_threads > 1)
#endif
        for (int e = c0; e < c1; ++e)
            es[static_cast<std::size_t>(e - c0)] = elem_stress(e);
        for (int e = c0; e < c1; ++e) {
            const ElemStress& s = es[static_cast<std::size_t>(e - c0)];
            if (!s.ok) {
                res.error = "fea_static: stress recovery failed for an element";
                return res;
            }
            for (int j = 0; j < stride; ++j) {
                const NodeStress& v = s.n[j];
                if (!std::isfinite(v.vm) || !std::isfinite(v.sx) || !std::isfinite(v.sy) ||
                    !std::isfinite(v.sz) || !std::isfinite(v.txy) ||
                    !std::isfinite(v.tyz) || !std::isfinite(v.txz)) {
                    res.error = "fea_static: stress recovery failed for an element";
                    return res;
                }
                const int nid = tets[static_cast<std::size_t>(e) * stride + j];
                vm_accum[nid] += v.vm; vm_count[nid] += 1;
                sx_accum[nid] += v.sx;  sy_accum[nid] += v.sy;  sz_accum[nid] += v.sz;
                txy_accum[nid] += v.txy; tyz_accum[nid] += v.tyz; txz_accum[nid] += v.txz;
            }
        }
        if (cancelled()) return cancel_result();
    }
    res.von_mises.assign(n_nodes, 0.0);
    for (int i = 0; i < n_nodes; ++i)
        if (vm_count[i] > 0) res.von_mises[i] = vm_accum[i] / vm_count[i];

    // ── Additive principal/shear/normal-component recovery ───────────────────
    // Average each tensor component (same divisor), build the 3x3 symmetric stress
    // tensor per node, and take its eigenvalues (ascending: p3=eig[0], p1=eig[2]).
    res.principal1.assign(n_nodes, 0.0);
    res.principal3.assign(n_nodes, 0.0);
    res.max_shear.assign(n_nodes, 0.0);
    res.stress_xx.assign(n_nodes, 0.0);
    res.stress_yy.assign(n_nodes, 0.0);
    res.stress_zz.assign(n_nodes, 0.0);
    Eigen::SelfAdjointEigenSolver<Eigen::Matrix3d> eig_solver;
    for (int i = 0; i < n_nodes; ++i) {
        if (vm_count[i] <= 0) continue;  // no incident elements -> leave zeros
        const double inv = 1.0 / static_cast<double>(vm_count[i]);
        const double sx = sx_accum[i] * inv;
        const double sy = sy_accum[i] * inv;
        const double sz = sz_accum[i] * inv;
        const double txy = txy_accum[i] * inv;
        const double tyz = tyz_accum[i] * inv;
        const double txz = txz_accum[i] * inv;
        res.stress_xx[i] = sx;
        res.stress_yy[i] = sy;
        res.stress_zz[i] = sz;
        Eigen::Matrix3d T;
        T << sx, txy, txz,
             txy, sy, tyz,
             txz, tyz, sz;
        if (!T.allFinite()) continue;  // guard NaN/inf -> leave zeros
        eig_solver.computeDirect(T);
        if (eig_solver.info() != Eigen::Success) continue;
        const Eigen::Vector3d ev = eig_solver.eigenvalues();  // ascending
        const double p3 = ev(0), p1 = ev(2);
        if (!std::isfinite(p1) || !std::isfinite(p3)) continue;
        res.principal1[i] = p1;
        res.principal3[i] = p3;
        res.max_shear[i] = (p1 - p3) * 0.5;
    }

    // Stress immediately adjacent to an ideal fully-fixed boundary is a
    // discretization singularity: its peak depends on Poisson ratio and mesh
    // orientation and does not converge to a material-comparable design value.
    // Keep the full nodal fields, but report extrema from nodes outside the
    // two-element fixture patch. If the fixture consumes the whole mesh, fall
    // back to the complete field rather than fabricating a zero-stress result.
    std::vector<char> summary_node(static_cast<size_t>(n_nodes), 1);
    if (!fixed_nodes.empty()) {
        std::vector<char> excluded(static_cast<size_t>(n_nodes), 0);
        for (int node : fixed_nodes) excluded[static_cast<size_t>(node)] = 1;
        // Two element rings put the reported peak beyond the ideal clamp's
        // Poisson-sensitive boundary layer while retaining the full field for
        // callers that explicitly inspect the fixture neighbourhood.
        for (int ring = 0; ring < 2; ++ring) {
            std::vector<char> expanded = excluded;
            for (int e = 0; e < n_tets; ++e) {
                const int* element = &tets[static_cast<size_t>(e) * stride];
                bool touches_excluded = false;
                for (int j = 0; j < stride; ++j)
                    touches_excluded = touches_excluded ||
                                       excluded[static_cast<size_t>(element[j])];
                if (touches_excluded)
                    for (int j = 0; j < stride; ++j)
                        expanded[static_cast<size_t>(element[j])] = 1;
            }
            excluded.swap(expanded);
        }
        for (int i = 0; i < n_nodes; ++i)
            summary_node[static_cast<size_t>(i)] =
                !excluded[static_cast<size_t>(i)];
        if (std::none_of(summary_node.begin(), summary_node.end(),
                         [](char selected) { return selected != 0; }))
            std::fill(summary_node.begin(), summary_node.end(), 1);
    }
    // Summary (trusted max von Mises, global max displacement, factor of safety).
    double max_vm = 0.0, max_disp = 0.0;
    for (int i = 0; i < n_nodes; ++i)
        if (summary_node[static_cast<size_t>(i)])
            max_vm = std::max(max_vm, res.von_mises[static_cast<size_t>(i)]);
    for (double v : res.disp_magnitude) max_disp = std::max(max_disp, v);
    double min_fos = 15.0;
    bool any = false;
    for (int i = 0; i < n_nodes; ++i) {
        if (!summary_node[static_cast<size_t>(i)]) continue;
        const double v = res.von_mises[static_cast<size_t>(i)];
        if (v > 0.0) {
            const double fos = yield_mpa / v;
            if (!any || fos < min_fos) min_fos = fos;
            any = true;
        }
    }
    if (!any) min_fos = 15.0;
    min_fos = std::min(std::max(min_fos, 0.0), 15.0);

    res.max_von_mises_mpa = max_vm;
    res.max_disp_mm = max_disp;
    res.min_fos = min_fos;

    // Export the exclusion as data, not as a footnote: the mask that produced
    // max_von_mises_mpa/min_fos, the count it dropped, and the SAME reduction
    // with no mask. Whoever reads the nodal field (a .vtu, ParaView, a script)
    // sees a peak the summary does not report, and the only honest way to close
    // that gap is to ship the mask beside the field and the raw number beside
    // the reported one. Additive: nothing above is recomputed or altered.
    res.summary_node.assign(static_cast<size_t>(n_nodes), 1);
    res.summary_excluded_nodes = 0;
    for (int i = 0; i < n_nodes; ++i) {
        const bool selected = summary_node[static_cast<size_t>(i)] != 0;
        res.summary_node[static_cast<size_t>(i)] = selected ? uint8_t{1} : uint8_t{0};
        if (!selected) ++res.summary_excluded_nodes;
    }
    double max_vm_raw = 0.0;
    int max_vm_raw_node = -1;
    for (int i = 0; i < n_nodes; ++i) {
        const double v = res.von_mises[static_cast<size_t>(i)];
        if (max_vm_raw_node < 0 || v > max_vm_raw) {
            max_vm_raw = v;
            max_vm_raw_node = i;
        }
    }
    double min_fos_raw = 15.0;
    bool any_raw = false;
    for (int i = 0; i < n_nodes; ++i) {
        const double v = res.von_mises[static_cast<size_t>(i)];
        if (v > 0.0) {
            const double fos = yield_mpa / v;
            if (!any_raw || fos < min_fos_raw) min_fos_raw = fos;
            any_raw = true;
        }
    }
    if (!any_raw) min_fos_raw = 15.0;
    res.max_von_mises_raw_mpa = max_vm_raw;
    res.min_fos_raw = std::min(std::max(min_fos_raw, 0.0), 15.0);
    res.max_von_mises_raw_node = max_vm_raw_node;

    // Additive summary extrema over the per-node principal/shear arrays.
    double max_p1 = 0.0, min_p3 = 0.0, max_sh = 0.0;
    bool first_p1 = true, first_p3 = true;
    for (int i = 0; i < n_nodes; ++i) {
        if (vm_count[i] <= 0) continue;
        if (!summary_node[static_cast<size_t>(i)]) continue;
        const double p1 = res.principal1[i], p3 = res.principal3[i], sh = res.max_shear[i];
        if (first_p1 || p1 > max_p1) { max_p1 = p1; first_p1 = false; }
        if (first_p3 || p3 < min_p3) { min_p3 = p3; first_p3 = false; }
        if (sh > max_sh) max_sh = sh;
    }
    res.max_principal_mpa = first_p1 ? 0.0 : max_p1;
    res.min_principal_mpa = first_p3 ? 0.0 : min_p3;
    res.max_shear_mpa = max_sh;
    res.n_nodes = n_nodes;
    res.n_elems = n_tets;
    res.ok = true;
    prof.mark("stress-recovery");
    return res;
}

FeaStaticResult fea_static_solve(const std::vector<double>& points, const std::vector<int>& tets,
                                 double E_mpa, double nu, double yield_mpa,
                                 const std::vector<int>& fixed_nodes,
                                 const std::vector<double>& nodal_forces, int nodes_per_elem,
                                 const std::vector<BondSpring>& bond_springs,
                                 const SimCancelToken& cancel_token) {
    return solve_impl(points, tets, E_mpa, nu, yield_mpa, fixed_nodes, nodal_forces,
                      nodes_per_elem, bond_springs, {}, FeaContactOptions{}, nullptr,
                      cancel_token);
}

FeaStaticResult fea_static_solve_contact(
    const std::vector<double>& points, const std::vector<int>& tets, double E_mpa,
    double nu, double yield_mpa, const std::vector<int>& fixed_nodes,
    const std::vector<double>& nodal_forces, int nodes_per_elem,
    const std::vector<BondSpring>& bond_springs,
    const std::vector<ContactSpring>& contact_springs, const FeaContactOptions& options,
    FeaContactReport& report, const SimCancelToken& cancel_token) {
    report = FeaContactReport{};
    if (contact_springs.empty()) {
        FeaStaticResult res;
        res.n_dof = static_cast<int>(points.size());
        res.error =
            "fea_static: contact study has no candidate contact pairs — nothing to "
            "solve unilaterally. Raise max_gap_mm, or run the static study instead.";
        return res;
    }
    return solve_impl(points, tets, E_mpa, nu, yield_mpa, fixed_nodes, nodal_forces,
                      nodes_per_elem, bond_springs, contact_springs, options, &report,
                      cancel_token);
}

}  // namespace chudware::sim
