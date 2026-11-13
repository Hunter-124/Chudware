// Supernodal multifrontal sparse Cholesky — see supernodal_llt.h.
//
// analyze():  fill-reducing ordering (AMD on the node graph) -> elimination tree ->
//             postorder -> column counts -> supernodes (with relaxed amalgamation) ->
//             row structures -> a scatter map from A's values into the supernodes.
// factorize(): one dense frontal matrix per supernode, eliminated leaves-first. A
//             front is the supernode's own columns (stored in the factor) plus the
//             update it passes to its parent.
//
// A displacement stiffness is made of full 3x3 node blocks, so its three columns per
// node share one structure. The symbolic phase runs on the node graph in that case:
// a ninth of the pattern, and the ordering cannot split a node.

#include "sim/supernodal_llt.h"

#include <algorithm>
#include <atomic>
#include <cmath>
#include <cstddef>
#include <cstdlib>
#include <cstring>
#include <exception>

#include <Eigen/Dense>
#include <Eigen/OrderingMethods>

#ifdef CHUDWARE_HAVE_OPENMP
#include <omp.h>
#endif

#if defined(__x86_64__) || defined(_M_X64)
#define CHUDWARE_SUPERNODAL_X64 1
#include <immintrin.h>
#if defined(_MSC_VER)
#include <intrin.h>
#define CHUDWARE_AVX2_FN
#else
#define CHUDWARE_AVX2_FN __attribute__((target("avx2,fma")))
#endif
#endif

namespace chudware::sim {
namespace {

// Pivot panel width and trailing-update column chunk. Both are fixed so the
// arithmetic — and therefore the result — is the same on any thread count.
constexpr int kPanel = 96;
constexpr int kChunk = 96;
constexpr int kSolveRows = 512;
// The panel's triangular solve runs in sub-blocks this wide, so that all but a
// kSolveBlock/kPanel share of it is a matrix product.
constexpr int kSolveBlock = 24;
// A front costing more than this (k * f^2) is eliminated on its own with threaded
// dense kernels; cheaper fronts are eliminated side by side, one thread each.
constexpr double kBigFrontWork = 5e7;
// Nested dissection is tried from this many graph nodes, and stops splitting below
// the leaf size.
constexpr int kDissectionMinNodes = 2000;
constexpr int kDissectionLeaf = 96;

using DMap = Eigen::Map<Eigen::MatrixXd, 0, Eigen::OuterStride<>>;
using CMap = Eigen::Map<const Eigen::MatrixXd, 0, Eigen::OuterStride<>>;

bool columns_sorted(int n, const int* ap, const int* ai) {
    for (int c = 0; c < n; ++c)
        for (int p = ap[c] + 1; p < ap[c + 1]; ++p)
            if (ai[p] <= ai[p - 1]) return false;
    return true;
}

// True when the lower triangle is made of complete 3x3 blocks on a node grid of
// three consecutive unknowns: the three columns of a node carry the same rows.
bool has_triple_blocks(int n, const int* ap, const int* ai) {
    if (n == 0 || n % 3 != 0) return false;
    for (int c0 = 0; c0 < n; c0 += 3) {
        int tail[3];
        for (int j = 0; j < 3; ++j) {
            const int col = c0 + j;
            const int end = ap[col + 1];
            int p = static_cast<int>(std::lower_bound(ai + ap[col], ai + end, col) - ai);
            for (int r = col; r < c0 + 3; ++r, ++p)
                if (p >= end || ai[p] != r) return false;
            tail[j] = p;
        }
        const int len = ap[c0 + 1] - tail[0];
        if (len % 3 != 0 || ap[c0 + 2] - tail[1] != len || ap[c0 + 3] - tail[2] != len)
            return false;
        for (int q = 0; q < len; q += 3) {
            const int r = ai[tail[0] + q];
            if (r % 3 != 0 || ai[tail[0] + q + 1] != r + 1 || ai[tail[0] + q + 2] != r + 2)
                return false;
        }
        if (len > 0 &&
            (std::memcmp(ai + tail[0], ai + tail[1], sizeof(int) * len) != 0 ||
             std::memcmp(ai + tail[0], ai + tail[2], sizeof(int) * len) != 0))
            return false;
    }
    return true;
}

// Strict-lower node graph (g) renumbered by `inv`, as per-node lists keyed by the
// larger (`by_high`) or the smaller endpoint, holding the other endpoint.
void renumbered_lists(int nn, const std::vector<int>& gp, const std::vector<int>& gi,
                      const std::vector<int>& inv, bool by_high, std::vector<int>& lp,
                      std::vector<int>& li) {
    lp.assign(static_cast<std::size_t>(nn) + 1, 0);
    for (int c = 0; c < nn; ++c)
        for (int p = gp[c]; p < gp[c + 1]; ++p) {
            const int a = inv[c], b = inv[gi[p]];
            ++lp[static_cast<std::size_t>(by_high ? std::max(a, b) : std::min(a, b)) + 1];
        }
    for (int j = 0; j < nn; ++j) lp[j + 1] += lp[j];
    li.resize(static_cast<std::size_t>(lp[nn]));
    std::vector<int> cur(lp.begin(), lp.end() - 1);
    for (int c = 0; c < nn; ++c)
        for (int p = gp[c]; p < gp[c + 1]; ++p) {
            const int a = inv[c], b = inv[gi[p]];
            const int lo = std::min(a, b), hi = std::max(a, b);
            li[cur[by_high ? hi : lo]++] = by_high ? lo : hi;
        }
}

// Liu's elimination tree with path compression. up = rows above the diagonal.
void elimination_tree(int nn, const std::vector<int>& up_p, const std::vector<int>& up_i,
                      std::vector<int>& parent) {
    parent.assign(nn, -1);
    std::vector<int> ancestor(nn, -1);
    for (int j = 0; j < nn; ++j)
        for (int p = up_p[j]; p < up_p[j + 1]; ++p) {
            int r = up_i[p];
            while (ancestor[r] != -1 && ancestor[r] != j) {
                const int next = ancestor[r];
                ancestor[r] = j;
                r = next;
            }
            if (ancestor[r] == -1) {
                ancestor[r] = j;
                parent[r] = j;
            }
        }
}

std::vector<int> postorder(int nn, const std::vector<int>& parent) {
    std::vector<int> head(nn, -1), next(nn, -1), post, stack;
    post.reserve(nn);
    for (int j = nn - 1; j >= 0; --j)
        if (parent[j] >= 0) {
            next[j] = head[parent[j]];
            head[parent[j]] = j;
        }
    for (int root = 0; root < nn; ++root) {
        if (parent[root] >= 0) continue;
        stack.push_back(root);
        while (!stack.empty()) {
            const int j = stack.back();
            const int c = head[j];
            if (c >= 0) {
                head[j] = next[c];
                stack.push_back(c);
            } else {
                post.push_back(j);
                stack.pop_back();
            }
        }
    }
    return post;
}

// Column counts of the factor (diagonal included) by walking each row's subtree of
// the elimination tree. Returns the factorization work, sum of count^2.
double column_counts(int nn, const std::vector<int>& up_p, const std::vector<int>& up_i,
                     const std::vector<int>& parent, std::vector<int>& count) {
    count.assign(nn, 1);
    std::vector<int> mark(nn, -1);
    for (int i = 0; i < nn; ++i) {
        mark[i] = i;
        for (int p = up_p[i]; p < up_p[i + 1]; ++p)
            for (int j = up_i[p]; mark[j] != i; j = parent[j]) {
                mark[j] = i;
                ++count[j];
            }
    }
    double work = 0.0;
    for (int j = 0; j < nn; ++j) work += static_cast<double>(count[j]) * count[j];
    return work;
}

// Approximate minimum degree on a strict-lower graph given as sorted columns
// (cp/ci). Empty on failure.
std::vector<int> amd_order(int nn, const std::vector<int>& cp, const std::vector<int>& ci) {
    // The diagonal is stored on purpose: Eigen's AMD returns the identity for a
    // pattern without one.
    if (ci.size() + static_cast<std::size_t>(nn) > 2000000000ull) return {};
    Eigen::SparseMatrix<double> g(nn, nn);
    g.resizeNonZeros(static_cast<Eigen::Index>(ci.size()) + nn);
    int* op = g.outerIndexPtr();
    int* ip = g.innerIndexPtr();
    for (int c = 0; c < nn; ++c) {
        op[c] = cp[c] + c;
        ip[op[c]] = c;
        std::copy(ci.begin() + cp[c], ci.begin() + cp[c + 1], ip + op[c] + 1);
    }
    op[nn] = cp[nn] + nn;
    std::fill(g.valuePtr(), g.valuePtr() + g.nonZeros(), 1.0);
    Eigen::PermutationMatrix<Eigen::Dynamic, Eigen::Dynamic, int> perm;
    Eigen::AMDOrdering<int> amd;
    amd(g.selfadjointView<Eigen::Lower>(), perm);
    if (perm.size() != nn) return {};
    return std::vector<int>(perm.indices().data(), perm.indices().data() + nn);
}

// Nested dissection by level-structure bisection (George and Liu's automatic nested
// dissection): split a region at a middle level of a breadth-first search from a
// pseudo-peripheral node, number both halves first and the separator last, recurse.
// Small regions are numbered by minimum degree.
std::vector<int> dissection_order(int nn, const std::vector<int>& gp,
                                  const std::vector<int>& gi) {
    // Full symmetric adjacency.
    std::vector<int> xp(static_cast<std::size_t>(nn) + 1, 0), xi(2 * gi.size());
    for (int c = 0; c < nn; ++c)
        for (int p = gp[c]; p < gp[c + 1]; ++p) {
            ++xp[static_cast<std::size_t>(c) + 1];
            ++xp[static_cast<std::size_t>(gi[p]) + 1];
        }
    for (int j = 0; j < nn; ++j) xp[j + 1] += xp[j];
    {
        std::vector<int> cur(xp.begin(), xp.end() - 1);
        for (int c = 0; c < nn; ++c)
            for (int p = gp[c]; p < gp[c + 1]; ++p) {
                xi[cur[c]++] = gi[p];
                xi[cur[gi[p]]++] = c;
            }
    }

    struct Job {
        std::vector<int> verts;
        int lo;  // the region is numbered into order[lo, lo + verts.size())
    };
    std::vector<int> order(nn), region(nn, 0), seen(nn, -1), level(nn, 0), local(nn, 0);
    std::vector<int> queue, level_start, lp, li, nbrs;
    std::vector<Job> stack;
    {
        Job root;
        root.verts.resize(nn);
        for (int v = 0; v < nn; ++v) root.verts[v] = v;
        root.lo = 0;
        stack.push_back(std::move(root));
    }
    int next_region = 1, stamp = 0;

    // Breadth-first search inside region `rid`; fills queue (visit order), level[]
    // and level_start[]. Returns the number of levels.
    const auto bfs = [&](int start, int rid) {
        ++stamp;
        queue.clear();
        level_start.clear();
        queue.push_back(start);
        seen[start] = stamp;
        level[start] = 0;
        std::size_t head = 0;
        int current = -1;
        while (head < queue.size()) {
            const int v = queue[head];
            if (level[v] != current) {
                current = level[v];
                level_start.push_back(static_cast<int>(head));
            }
            ++head;
            for (int p = xp[v]; p < xp[v + 1]; ++p) {
                const int w = xi[p];
                if (region[w] != rid || seen[w] == stamp) continue;
                seen[w] = stamp;
                level[w] = current + 1;
                queue.push_back(w);
            }
        }
        level_start.push_back(static_cast<int>(queue.size()));
        return static_cast<int>(level_start.size()) - 1;
    };

    const auto number_leaf = [&](const Job& job, int rid) {
        const int cnt = static_cast<int>(job.verts.size());
        for (int i = 0; i < cnt; ++i) local[job.verts[i]] = i;
        lp.assign(static_cast<std::size_t>(cnt) + 1, 0);
        li.clear();
        for (int i = 0; i < cnt; ++i) {
            const int v = job.verts[i];
            nbrs.clear();
            for (int p = xp[v]; p < xp[v + 1]; ++p)
                if (region[xi[p]] == rid && local[xi[p]] > i) nbrs.push_back(local[xi[p]]);
            std::sort(nbrs.begin(), nbrs.end());
            li.insert(li.end(), nbrs.begin(), nbrs.end());
            lp[static_cast<std::size_t>(i) + 1] = static_cast<int>(li.size());
        }
        const std::vector<int> leaf = cnt > 2 ? amd_order(cnt, lp, li) : std::vector<int>();
        for (int i = 0; i < cnt; ++i)
            order[job.lo + i] = job.verts[leaf.empty() ? i : leaf[i]];
    };

    while (!stack.empty()) {
        Job job = std::move(stack.back());
        stack.pop_back();
        const int rid = next_region++;
        const int cnt = static_cast<int>(job.verts.size());
        for (int v : job.verts) region[v] = rid;
        if (cnt <= kDissectionLeaf) {
            number_leaf(job, rid);
            continue;
        }
        int levels = bfs(job.verts[0], rid);
        if (static_cast<int>(queue.size()) < cnt) {
            // Disconnected region: the reached component and the rest share nothing.
            Job reached, rest;
            reached.verts = queue;
            reached.lo = job.lo;
            rest.lo = job.lo + static_cast<int>(queue.size());
            for (int v : job.verts)
                if (seen[v] != stamp) rest.verts.push_back(v);
            stack.push_back(std::move(reached));
            stack.push_back(std::move(rest));
            continue;
        }
        // Walk toward a pseudo-peripheral node: restart from a low-degree node of
        // the last level while that deepens the level structure.
        for (int tries = 0; tries < 4; ++tries) {
            int far = queue[level_start[levels - 1]];
            for (int q = level_start[levels - 1]; q < level_start[levels]; ++q)
                if (xp[queue[q] + 1] - xp[queue[q]] < xp[far + 1] - xp[far]) far = queue[q];
            const int deeper = bfs(far, rid);
            if (deeper <= levels) {
                levels = deeper;
                break;
            }
            levels = deeper;
        }
        if (levels < 3) {
            number_leaf(job, rid);
            continue;
        }
        // The separator level: the thinnest one that leaves at least a quarter of
        // the region on each side, else the most balanced one.
        int cut = -1, balanced = 1, best_side = -1;
        for (int l = 1; l + 1 < levels; ++l) {
            const int below = level_start[l], above = cnt - level_start[l + 1];
            const int side = std::min(below, above);
            const int width = level_start[l + 1] - level_start[l];
            if (side > best_side) {
                best_side = side;
                balanced = l;
            }
            if (side * 4 >= cnt &&
                (cut < 0 || width < level_start[cut + 1] - level_start[cut]))
                cut = l;
        }
        if (cut < 0) cut = balanced;
        // Only the cut-level nodes that touch the next level separate anything; the
        // others stay with the lower half.
        Job lower, upper;
        std::vector<int> separator;
        lower.verts.assign(queue.begin(), queue.begin() + level_start[cut]);
        for (int q = level_start[cut]; q < level_start[cut + 1]; ++q) {
            const int v = queue[q];
            bool touches = false;
            for (int p = xp[v]; p < xp[v + 1] && !touches; ++p)
                touches = region[xi[p]] == rid && level[xi[p]] == cut + 1;
            (touches ? separator : lower.verts).push_back(v);
        }
        upper.verts.assign(queue.begin() + level_start[cut + 1], queue.end());
        lower.lo = job.lo;
        upper.lo = job.lo + static_cast<int>(lower.verts.size());
        int at = upper.lo + static_cast<int>(upper.verts.size());
        for (int v : separator) {
            order[at++] = v;
            region[v] = -1;
        }
        stack.push_back(std::move(lower));
        stack.push_back(std::move(upper));
    }
    return order;
}

// ── Dense product kernel: C -= A * B^T ──────────────────────────────────────────
// C is m x n, A is m x r, B is n x r, all column-major. Every flop of a
// factorization beyond the small diagonal blocks goes through here.

void product_sub_portable(double* c, std::ptrdiff_t ldc, int m, int n, const double* a,
                          std::ptrdiff_t lda, const double* b, std::ptrdiff_t ldb, int r) {
    DMap cm(c, m, n, Eigen::OuterStride<>(ldc));
    CMap am(a, m, r, Eigen::OuterStride<>(lda));
    CMap bm(b, n, r, Eigen::OuterStride<>(ldb));
    cm.noalias() -= am * bm.transpose();
}

#ifdef CHUDWARE_SUPERNODAL_X64
// The portable build targets baseline x86-64, where Eigen's product runs two
// doubles per instruction. This kernel is the same product with AVX2 + FMA — four
// doubles and a fused multiply-add — selected at run time on a CPU that has them.
// It is the standard packed, register-blocked layout: A is packed into strips of 8
// rows and B into strips of 6 columns so the inner loop reads both sequentially and
// keeps an 8 x 6 tile of C in registers.
constexpr int kMR = 8, kNR = 6, kKC = 192, kMC = 96;

bool cpu_has_avx2_fma() {
#if defined(_MSC_VER)
    int info[4];
    __cpuid(info, 0);
    if (info[0] < 7) return false;
    __cpuid(info, 1);
    const bool osxsave = (info[2] & (1 << 27)) != 0;
    const bool avx = (info[2] & (1 << 28)) != 0;
    const bool fma = (info[2] & (1 << 12)) != 0;
    if (!osxsave || !avx || !fma) return false;
    if ((_xgetbv(0) & 6) != 6) return false;  // the OS saves ymm state
    __cpuidex(info, 7, 0);
    return (info[1] & (1 << 5)) != 0;
#else
    return __builtin_cpu_supports("avx2") && __builtin_cpu_supports("fma");
#endif
}

// tile (8 x 6, column-major, stride ldt) -= Ap (8 x kc, packed) * Bp (kc x 6, packed).
CHUDWARE_AVX2_FN void tile_sub_avx2(int kc, const double* ap, const double* bp, double* t,
                                    std::ptrdiff_t ldt) {
    __m256d c00 = _mm256_setzero_pd(), c10 = c00, c01 = c00, c11 = c00, c02 = c00, c12 = c00;
    __m256d c03 = c00, c13 = c00, c04 = c00, c14 = c00, c05 = c00, c15 = c00;
    for (int p = 0; p < kc; ++p, ap += kMR, bp += kNR) {
        const __m256d a0 = _mm256_loadu_pd(ap), a1 = _mm256_loadu_pd(ap + 4);
        __m256d bj = _mm256_broadcast_sd(bp);
        c00 = _mm256_fmadd_pd(a0, bj, c00);
        c10 = _mm256_fmadd_pd(a1, bj, c10);
        bj = _mm256_broadcast_sd(bp + 1);
        c01 = _mm256_fmadd_pd(a0, bj, c01);
        c11 = _mm256_fmadd_pd(a1, bj, c11);
        bj = _mm256_broadcast_sd(bp + 2);
        c02 = _mm256_fmadd_pd(a0, bj, c02);
        c12 = _mm256_fmadd_pd(a1, bj, c12);
        bj = _mm256_broadcast_sd(bp + 3);
        c03 = _mm256_fmadd_pd(a0, bj, c03);
        c13 = _mm256_fmadd_pd(a1, bj, c13);
        bj = _mm256_broadcast_sd(bp + 4);
        c04 = _mm256_fmadd_pd(a0, bj, c04);
        c14 = _mm256_fmadd_pd(a1, bj, c14);
        bj = _mm256_broadcast_sd(bp + 5);
        c05 = _mm256_fmadd_pd(a0, bj, c05);
        c15 = _mm256_fmadd_pd(a1, bj, c15);
    }
    const __m256d lo[6] = {c00, c01, c02, c03, c04, c05};
    const __m256d hi[6] = {c10, c11, c12, c13, c14, c15};
    for (int j = 0; j < kNR; ++j, t += ldt) {
        _mm256_storeu_pd(t, _mm256_sub_pd(_mm256_loadu_pd(t), lo[j]));
        _mm256_storeu_pd(t + 4, _mm256_sub_pd(_mm256_loadu_pd(t + 4), hi[j]));
    }
    _mm256_zeroupper();
}

void product_sub_avx2(double* c, std::ptrdiff_t ldc, int m, int n, const double* a,
                      std::ptrdiff_t lda, const double* b, std::ptrdiff_t ldb, int r) {
    // Packing only pays once the product is big enough to amortize it.
    if (m < 2 * kMR || n < kNR || r < 8) {
        product_sub_portable(c, ldc, m, n, a, lda, b, ldb, r);
        return;
    }
    const int kc_max = std::min(r, kKC), mc_max = std::min(m, kMC);
    const int n_strips = (n + kNR - 1) / kNR, m_strips_max = (mc_max + kMR - 1) / kMR;
    const std::size_t bp_size = static_cast<std::size_t>(n_strips) * kNR * kc_max;
    const std::size_t ap_size = static_cast<std::size_t>(m_strips_max) * kMR * kc_max;
    double* pack = static_cast<double*>(_mm_malloc((bp_size + ap_size) * sizeof(double), 64));
    if (!pack) {
        product_sub_portable(c, ldc, m, n, a, lda, b, ldb, r);
        return;
    }
    double* const bpack = pack;
    double* const apack = pack + bp_size;
    double edge[kMR * kNR];
    for (int pc = 0; pc < r; pc += kKC) {
        const int kc = std::min(kKC, r - pc);
        for (int js = 0; js < n_strips; ++js) {
            double* dst = bpack + static_cast<std::size_t>(js) * kNR * kc;
            const int j0 = js * kNR, nj = std::min(kNR, n - j0);
            for (int p = 0; p < kc; ++p, dst += kNR) {
                const double* col = b + (pc + p) * ldb + j0;
                int j = 0;
                for (; j < nj; ++j) dst[j] = col[j];
                for (; j < kNR; ++j) dst[j] = 0.0;
            }
        }
        for (int ic = 0; ic < m; ic += kMC) {
            const int mc = std::min(kMC, m - ic);
            const int m_strips = (mc + kMR - 1) / kMR;
            for (int is = 0; is < m_strips; ++is) {
                double* dst = apack + static_cast<std::size_t>(is) * kMR * kc;
                const int i0 = ic + is * kMR, ni = std::min(kMR, m - i0);
                for (int p = 0; p < kc; ++p, dst += kMR) {
                    const double* col = a + (pc + p) * lda + i0;
                    int i = 0;
                    for (; i < ni; ++i) dst[i] = col[i];
                    for (; i < kMR; ++i) dst[i] = 0.0;
                }
            }
            for (int js = 0; js < n_strips; ++js) {
                const int j0 = js * kNR, nj = std::min(kNR, n - j0);
                const double* bp = bpack + static_cast<std::size_t>(js) * kNR * kc;
                for (int is = 0; is < m_strips; ++is) {
                    const int i0 = ic + is * kMR, ni = std::min(kMR, m - i0);
                    const double* ap = apack + static_cast<std::size_t>(is) * kMR * kc;
                    double* tile = c + j0 * ldc + i0;
                    if (ni == kMR && nj == kNR) {
                        tile_sub_avx2(kc, ap, bp, tile, ldc);
                    } else {
                        std::memset(edge, 0, sizeof edge);
                        tile_sub_avx2(kc, ap, bp, edge, kMR);
                        for (int j = 0; j < nj; ++j)
                            for (int i = 0; i < ni; ++i) tile[j * ldc + i] += edge[j * kMR + i];
                    }
                }
            }
        }
    }
    _mm_free(pack);
}
#endif  // CHUDWARE_SUPERNODAL_X64

using ProductSub = void (*)(double*, std::ptrdiff_t, int, int, const double*, std::ptrdiff_t,
                            const double*, std::ptrdiff_t, int);

std::atomic<bool> g_portable_kernel{false};

ProductSub product_sub() {
#ifdef CHUDWARE_SUPERNODAL_X64
    static const bool fast = cpu_has_avx2_fma();
    if (fast && !g_portable_kernel.load(std::memory_order_relaxed)) return product_sub_avx2;
#endif
    return product_sub_portable;
}

// C -= P * Ptop^T on the lower trapezoid of C (rows x cols, rows >= cols), where P
// is rows x rank and Ptop its first `cols` rows. Column chunks are independent.
void lower_update(double* c, std::ptrdiff_t ldc, int rows, int cols, const double* p,
                  std::ptrdiff_t ldp, int rank, int threads) {
    const ProductSub product = product_sub();
    const int chunks = (cols + kChunk - 1) / kChunk;
#ifdef CHUDWARE_HAVE_OPENMP
#pragma omp parallel for schedule(dynamic, 1) num_threads(threads) if (threads > 1)
#endif
    for (int cb = 0; cb < chunks; ++cb) {
        const int c0 = cb * kChunk;
        const int nc = std::min(kChunk, cols - c0);
        product(c + c0 + c0 * ldc, ldc, rows - c0, nc, p + c0, ldp, p + c0, ldp, rank);
    }
    (void)threads;
}

}  // namespace

SupernodalLLT::~SupernodalLLT() { std::free(lx_); }

void SupernodalLLT::use_portable_kernel(bool portable) {
    g_portable_kernel.store(portable, std::memory_order_relaxed);
}

void SupernodalLLT::reset() {
    std::free(lx_);
    lx_ = nullptr;
    lx_size_ = 0;
    n_ = nn_ = 0;
    b_ = 1;
    factored_ = false;
}

bool SupernodalLLT::analyze(const Eigen::SparseMatrix<double>& A) {
    reset();
    if (A.rows() != A.cols() || A.rows() == 0 || !A.isCompressed()) return false;
    const int n = static_cast<int>(A.cols());
    const int* ap = A.outerIndexPtr();
    const int* ai = A.innerIndexPtr();
    if (!columns_sorted(n, ap, ai)) return false;
    try {
        const int b = has_triple_blocks(n, ap, ai) ? 3 : 1;
        const int nn = n / b;

        // Strict-lower node graph in the original numbering.
        std::vector<int> gp(static_cast<std::size_t>(nn) + 1, 0), gi;
        for (int pass = 0; pass < 2; ++pass) {
            int64_t at = 0;
            for (int node = 0; node < nn; ++node) {
                const int c = node * b;
                for (int p = ap[c]; p < ap[c + 1]; ++p) {
                    const int r = ai[p];
                    if (r <= c || r % b != 0) continue;
                    if (pass == 1) gi[static_cast<std::size_t>(at)] = r / b;
                    ++at;
                }
                if (at > 2000000000LL) return false;
                gp[static_cast<std::size_t>(node) + 1] = static_cast<int>(at);
            }
            if (pass == 0) gi.resize(static_cast<std::size_t>(at));
        }

        // Fill-reducing ordering. order[k] = original node eliminated k-th. Minimum
        // degree is the better local heuristic and nested dissection the better
        // global one; which wins depends on the shape of the part, so on a mesh
        // large enough to matter both are costed symbolically and the cheaper kept.
        std::vector<int> order = amd_order(nn, gp, gi);
        if (order.empty()) return false;
        std::vector<int> inv(nn), up_p, up_i, parent, count;
        const auto symbolic = [&]() {
            for (int k = 0; k < nn; ++k) inv[order[k]] = k;
            renumbered_lists(nn, gp, gi, inv, true, up_p, up_i);
            elimination_tree(nn, up_p, up_i, parent);
            return column_counts(nn, up_p, up_i, parent, count);
        };
        double work = symbolic();
        if (nn >= kDissectionMinNodes) {
            std::vector<int> amd = order;
            order = dissection_order(nn, gp, gi);
            const double dissected = symbolic();
            if (dissected >= work) {
                order.swap(amd);
            } else {
                work = dissected;
            }
        }

        // Renumber by the elimination tree's postorder so every subtree — and so
        // every supernode — is a contiguous run of columns.
        symbolic();
        {
            const std::vector<int> post = postorder(nn, parent);
            std::vector<int> reordered(nn);
            for (int k = 0; k < nn; ++k) reordered[k] = order[post[k]];
            order.swap(reordered);
        }
        symbolic();

        // Supernodes: column j+1 joins column j's supernode when it is j's parent and
        // has exactly j's structure minus j.
        std::vector<int> sb, se, sc, sn_of(nn);
        for (int j = 0; j < nn; ++j) {
            if (j == 0 || parent[j - 1] != j || count[j - 1] != count[j] + 1) {
                sb.push_back(j);
                se.push_back(j + 1);
                sc.push_back(count[j]);
            } else {
                se.back() = j + 1;
            }
            sn_of[j] = static_cast<int>(sb.size()) - 1;
        }
        const int s0 = static_cast<int>(sb.size());

        // Relaxed amalgamation: fold a supernode into its parent when they are
        // adjacent and the explicit zeros that costs are a small share of the merged
        // block. Wider dense blocks are worth a few stored zeros.
        std::vector<int> merged(s0, -1);
        {
            std::vector<double> zeros(s0, 0.0);
            const auto entries = [](double k, double c) { return k * c - k * (k - 1.0) / 2.0; };
            for (int s = 0; s < s0; ++s) {
                const int pj = parent[se[s] - 1];
                if (pj < 0) continue;
                const int p = sn_of[pj];
                if (se[s] != sb[p]) continue;
                const double ks = se[s] - sb[s], kp = se[p] - sb[p];
                const double cm = ks + sc[p];
                const double em = entries(ks + kp, cm);
                const double z = zeros[s] + zeros[p] + em - entries(ks, sc[s]) - entries(kp, sc[p]);
                const double cols = (ks + kp) * b;
                const double share = z / em;
                const bool fold = cols <= 4 || (cols <= 16 && share < 0.8) ||
                                  (cols <= 48 && share < 0.1) || share < 0.05;
                if (!fold) continue;
                sb[p] = sb[s];
                sc[p] = static_cast<int>(cm);
                zeros[p] = z;
                merged[s] = p;
            }
        }
        std::vector<int> final_id(s0, -1);
        int ns = 0;
        for (int s = 0; s < s0; ++s)
            if (merged[s] < 0) final_id[s] = ns++;
        std::vector<int> sparent(ns, -1), expect_rows(ns), node_sn(nn);
        sfirst_.assign(static_cast<std::size_t>(ns) + 1, 0);
        for (int s = 0; s < s0; ++s) {
            if (merged[s] >= 0) continue;
            const int f = final_id[s];
            sfirst_[f] = sb[s];
            sfirst_[static_cast<std::size_t>(f) + 1] = se[s];
            expect_rows[f] = sc[s] - (se[s] - sb[s]);
            for (int j = sb[s]; j < se[s]; ++j) node_sn[j] = f;
        }
        for (int f = 0; f < ns; ++f) {
            const int pj = parent[sfirst_[static_cast<std::size_t>(f) + 1] - 1];
            if (pj >= 0) sparent[f] = node_sn[pj];
        }

        // Children lists and tree levels (height above the deepest leaf).
        cstart_.assign(static_cast<std::size_t>(ns) + 1, 0);
        for (int f = 0; f < ns; ++f)
            if (sparent[f] >= 0) ++cstart_[static_cast<std::size_t>(sparent[f]) + 1];
        for (int f = 0; f < ns; ++f) cstart_[f + 1] += cstart_[f];
        child_.resize(static_cast<std::size_t>(cstart_[ns]));
        {
            std::vector<int> cur(cstart_.begin(), cstart_.end() - 1);
            std::vector<int> height(ns, 0);
            int levels = 0;
            for (int f = 0; f < ns; ++f) {
                if (sparent[f] >= 0) {
                    child_[cur[sparent[f]]++] = f;
                    height[sparent[f]] = std::max(height[sparent[f]], height[f] + 1);
                }
                levels = std::max(levels, height[f] + 1);
            }
            level_start_.assign(static_cast<std::size_t>(levels) + 1, 0);
            for (int f = 0; f < ns; ++f) ++level_start_[static_cast<std::size_t>(height[f]) + 1];
            for (int l = 0; l < levels; ++l) level_start_[l + 1] += level_start_[l];
            level_order_.resize(ns);
            std::vector<int> lcur(level_start_.begin(), level_start_.end() - 1);
            for (int f = 0; f < ns; ++f) level_order_[lcur[height[f]]++] = f;
        }

        // Row structure below each supernode: its own columns' entries plus what its
        // children pass up.
        std::vector<int> low_p, low_i;
        renumbered_lists(nn, gp, gi, inv, false, low_p, low_i);
        rstart_.assign(static_cast<std::size_t>(ns) + 1, 0);
        rows_.clear();
        {
            std::vector<int> mark(nn, -1), found;
            for (int f = 0; f < ns; ++f) {
                const int end = sfirst_[static_cast<std::size_t>(f) + 1];
                found.clear();
                const auto take = [&](int r) {
                    if (r >= end && mark[r] != f) {
                        mark[r] = f;
                        found.push_back(r);
                    }
                };
                for (int j = sfirst_[f]; j < end; ++j)
                    for (int p = low_p[j]; p < low_p[j + 1]; ++p) take(low_i[p]);
                for (int ci = cstart_[f]; ci < cstart_[f + 1]; ++ci) {
                    const int c = child_[ci];
                    for (int p = rstart_[c]; p < rstart_[c + 1]; ++p) take(rows_[p]);
                }
                // The counts above predicted this structure; a mismatch means the
                // symbolic phase is inconsistent and the numeric one would scribble.
                if (static_cast<int>(found.size()) != expect_rows[f]) return false;
                std::sort(found.begin(), found.end());
                rows_.insert(rows_.end(), found.begin(), found.end());
                if (rows_.size() > 2000000000ull) return false;
                rstart_[static_cast<std::size_t>(f) + 1] = static_cast<int>(rows_.size());
            }
        }

        // Factor layout: one dense (k + m) x k block per supernode.
        lstart_.assign(static_cast<std::size_t>(ns) + 1, 0);
        max_rows_ = 0;
        for (int f = 0; f < ns; ++f) {
            const int64_t k = static_cast<int64_t>(sfirst_[f + 1] - sfirst_[f]) * b;
            const int64_t m = static_cast<int64_t>(rstart_[f + 1] - rstart_[f]) * b;
            if ((k + m) * k > 4000000000LL || k + m > 2000000000LL) return false;
            lstart_[static_cast<std::size_t>(f) + 1] = lstart_[f] + (k + m) * k;
            max_rows_ = std::max(max_rows_, static_cast<int>(m));
        }

        // Where each stored lower-triangle value of A lands in the factor.
        amap_start_.assign(static_cast<std::size_t>(ns) + 1, 0);
        const auto place = [&](int r, int c, int& s, uint32_t& dst) {
            int hi = inv[r / b] * b + r % b, lo = inv[c / b] * b + c % b;
            if (hi < lo) std::swap(hi, lo);
            s = node_sn[lo / b];
            const int first = sfirst_[s] * b;
            const int64_t k = static_cast<int64_t>(sfirst_[s + 1] - sfirst_[s]) * b;
            const int64_t f = k + static_cast<int64_t>(rstart_[s + 1] - rstart_[s]) * b;
            int64_t li;
            if (hi / b < sfirst_[s + 1]) {
                li = hi - first;
            } else {
                const int* rb = rows_.data() + rstart_[s];
                const int* re = rows_.data() + rstart_[s + 1];
                const int* it = std::lower_bound(rb, re, hi / b);
                if (it == re || *it != hi / b) return false;
                li = k + static_cast<int64_t>(it - rb) * b + hi % b;
            }
            dst = static_cast<uint32_t>(li + static_cast<int64_t>(lo - first) * f);
            return true;
        };
        for (int c = 0; c < n; ++c)
            for (int p = static_cast<int>(std::lower_bound(ai + ap[c], ai + ap[c + 1], c) - ai);
                 p < ap[c + 1]; ++p) {
                int s;
                uint32_t dst;
                if (!place(ai[p], c, s, dst)) return false;
                ++amap_start_[static_cast<std::size_t>(s) + 1];
            }
        for (int f = 0; f < ns; ++f) amap_start_[f + 1] += amap_start_[f];
        amap_src_.resize(static_cast<std::size_t>(amap_start_[ns]));
        amap_dst_.resize(static_cast<std::size_t>(amap_start_[ns]));
        {
            std::vector<int64_t> cur(amap_start_.begin(), amap_start_.end() - 1);
            for (int c = 0; c < n; ++c)
                for (int p = static_cast<int>(std::lower_bound(ai + ap[c], ai + ap[c + 1], c) - ai);
                     p < ap[c + 1]; ++p) {
                    int s;
                    uint32_t dst;
                    place(ai[p], c, s, dst);
                    const int64_t at = cur[s]++;
                    amap_src_[static_cast<std::size_t>(at)] = p;
                    amap_dst_[static_cast<std::size_t>(at)] = dst;
                }
        }

        inv_.swap(inv);
        work_ = work * b * b;
        n_ = n;
        b_ = b;
        nn_ = nn;
        pattern_nnz_ = A.nonZeros();
        lx_size_ = lstart_[ns];
        return true;
    } catch (const std::exception&) {
        reset();
        return false;
    }
}

bool SupernodalLLT::process_front(int s, const double* ax, std::vector<double*>& update,
                                  int inner_threads) const {
    const int b = b_;
    const int k = (sfirst_[s + 1] - sfirst_[s]) * b;
    const int m = (rstart_[s + 1] - rstart_[s]) * b;
    const std::ptrdiff_t f = static_cast<std::ptrdiff_t>(k) + m;
    double* blk = lx_ + lstart_[s];
    std::memset(blk, 0, sizeof(double) * static_cast<std::size_t>(f) * k);
    for (int64_t e = amap_start_[s]; e < amap_start_[s + 1]; ++e)
        blk[amap_dst_[static_cast<std::size_t>(e)]] += ax[amap_src_[static_cast<std::size_t>(e)]];

    double* upd = nullptr;
    if (m > 0) {
        upd = static_cast<double*>(std::calloc(static_cast<std::size_t>(m) * m, sizeof(double)));
        if (!upd) return false;
        update[s] = upd;
    }

    // Extend-add: fold each child's update into this front.
    std::vector<int> loc;
    for (int ci = cstart_[s]; ci < cstart_[s + 1]; ++ci) {
        const int c = child_[ci];
        double* cu = update[c];
        if (!cu) continue;
        const int mcn = rstart_[c + 1] - rstart_[c];
        const int mc = mcn * b;
        loc.resize(mc);
        const int* rc = rows_.data() + rstart_[c];
        const int* rs = rows_.data() + rstart_[s];
        const int nrs = rstart_[s + 1] - rstart_[s];
        int pos = 0;
        for (int t = 0; t < mcn; ++t) {
            const int r = rc[t];
            int base;
            if (r < sfirst_[s + 1]) {
                base = (r - sfirst_[s]) * b;
            } else {
                while (pos < nrs && rs[pos] != r) ++pos;
                if (pos == nrs) return false;
                base = k + pos * b;
            }
            for (int d = 0; d < b; ++d) loc[t * b + d] = base + d;
        }
#ifdef CHUDWARE_HAVE_OPENMP
#pragma omp parallel for schedule(static) num_threads(inner_threads) if (inner_threads > 1)
#endif
        for (int tc = 0; tc < mc; ++tc) {
            const int lj = loc[tc];
            const double* src = cu + static_cast<std::size_t>(tc) * mc;
            double* dst = lj < k ? blk + static_cast<std::ptrdiff_t>(lj) * f
                                 : upd + static_cast<std::ptrdiff_t>(lj - k) * m - k;
            for (int tr = tc; tr < mc; ++tr) dst[loc[tr]] += src[tr];
        }
        std::free(cu);
        update[c] = nullptr;
    }

    // Partial Cholesky of the front's first k columns, one panel at a time.
    DMap front(blk, f, k, Eigen::OuterStride<>(f));
    for (int j0 = 0; j0 < k; j0 += kPanel) {
        const int jb = std::min(kPanel, k - j0);
        const int j1 = j0 + jb;
        auto diag = front.block(j0, j0, jb, jb);
        Eigen::LLT<Eigen::Ref<Eigen::MatrixXd>, Eigen::Lower> llt(diag);
        if (llt.info() != Eigen::Success) return false;
        for (int j = 0; j < jb; ++j)
            if (!std::isfinite(diag(j, j))) return false;
        const int below = static_cast<int>(f) - j1;
        if (below == 0) continue;
        // panel = panel * diag^-T, by rows (independent) and in column sub-blocks:
        // solve one sub-block, then eliminate it from the columns to its right.
        const ProductSub product = product_sub();
        const int row_chunks = (below + kSolveRows - 1) / kSolveRows;
#ifdef CHUDWARE_HAVE_OPENMP
#pragma omp parallel for schedule(static) num_threads(inner_threads) if (inner_threads > 1)
#endif
        for (int rcix = 0; rcix < row_chunks; ++rcix) {
            const int r0 = j1 + rcix * kSolveRows;
            const int nr = std::min(kSolveRows, static_cast<int>(f) - r0);
            for (int q0 = 0; q0 < jb; q0 += kSolveBlock) {
                const int qb = std::min(kSolveBlock, jb - q0);
                auto rows = front.block(r0, j0 + q0, nr, qb);
                front.block(j0 + q0, j0 + q0, qb, qb)
                    .triangularView<Eigen::Lower>()
                    .transpose()
                    .solveInPlace<Eigen::OnTheRight>(rows);
                const int rest = jb - q0 - qb;
                if (rest > 0)
                    product(blk + r0 + (j0 + q0 + qb) * f, f, nr, rest, blk + r0 + (j0 + q0) * f,
                            f, blk + (j0 + q0 + qb) + (j0 + q0) * f, f, qb);
            }
        }
        if (j1 < k)
            lower_update(blk + j1 + j1 * f, f, static_cast<int>(f) - j1, k - j1,
                         blk + j1 + j0 * f, f, jb, inner_threads);
    }
    if (m > 0) lower_update(upd, m, m, m, blk + k, f, k, inner_threads);
    return true;
}

bool SupernodalLLT::factorize(const Eigen::SparseMatrix<double>& A, int threads) {
    factored_ = false;
    if (n_ == 0 || A.rows() != n_ || A.cols() != n_ || !A.isCompressed() ||
        A.nonZeros() != pattern_nnz_)
        return false;
    if (!lx_) {
        lx_ = static_cast<double*>(
            std::malloc(sizeof(double) * static_cast<std::size_t>(std::max<int64_t>(lx_size_, 1))));
        if (!lx_) return false;
    }
#ifdef CHUDWARE_HAVE_OPENMP
    if (threads <= 0) threads = omp_get_max_threads();
#else
    threads = 1;
#endif
    threads = std::max(1, threads);

    const int ns = supernodes();
    const double* ax = A.valuePtr();
    std::vector<double*> update(static_cast<std::size_t>(ns), nullptr);
    std::atomic<bool> failed{false};
    const auto is_big = [&](int s) {
        if (threads == 1) return false;
        const double k = static_cast<double>(sfirst_[s + 1] - sfirst_[s]) * b_;
        const double f = k + static_cast<double>(rstart_[s + 1] - rstart_[s]) * b_;
        return k * f * f > kBigFrontWork;
    };
    const int levels = static_cast<int>(level_start_.size()) - 1;
    for (int l = 0; l < levels && !failed.load(std::memory_order_relaxed); ++l) {
        const int* list = level_order_.data() + level_start_[l];
        const int count = level_start_[l + 1] - level_start_[l];
#ifdef CHUDWARE_HAVE_OPENMP
#pragma omp parallel for schedule(dynamic, 1) num_threads(threads) if (threads > 1)
#endif
        for (int q = 0; q < count; ++q) {
            const int s = list[q];
            if (is_big(s) || failed.load(std::memory_order_relaxed)) continue;
            bool good = false;
            try {
                good = process_front(s, ax, update, 1);
            } catch (const std::exception&) {
            }
            if (!good) failed.store(true, std::memory_order_relaxed);
        }
        for (int q = 0; q < count && !failed.load(std::memory_order_relaxed); ++q) {
            const int s = list[q];
            if (!is_big(s)) continue;
            bool good = false;
            try {
                good = process_front(s, ax, update, threads);
            } catch (const std::exception&) {
            }
            if (!good) failed.store(true, std::memory_order_relaxed);
        }
    }
    for (double* u : update) std::free(u);
    factored_ = !failed.load();
    return factored_;
}

bool SupernodalLLT::lower_solve(const double* in, double* out) const {
    if (!factored_) return false;
    const int b = b_;
    const int ns = supernodes();
    Eigen::VectorXd y(n_), tmp(std::max(1, max_rows_));
    for (int node = 0; node < nn_; ++node)
        for (int d = 0; d < b; ++d) y[inv_[node] * b + d] = in[node * b + d];
    for (int s = 0; s < ns; ++s) {
        const int k = (sfirst_[s + 1] - sfirst_[s]) * b;
        const int mn = rstart_[s + 1] - rstart_[s];
        const int m = mn * b;
        const std::ptrdiff_t f = static_cast<std::ptrdiff_t>(k) + m;
        CMap block(lx_ + lstart_[s], f, k, Eigen::OuterStride<>(f));
        Eigen::Map<Eigen::VectorXd> x1(y.data() + static_cast<std::ptrdiff_t>(sfirst_[s]) * b, k);
        block.topRows(k).triangularView<Eigen::Lower>().solveInPlace(x1);
        if (m == 0) continue;
        tmp.head(m).noalias() = block.bottomRows(m) * x1;
        const int* rs = rows_.data() + rstart_[s];
        for (int t = 0; t < mn; ++t)
            for (int d = 0; d < b; ++d) y[rs[t] * b + d] -= tmp[t * b + d];
    }
    std::copy(y.data(), y.data() + n_, out);
    return true;
}

bool SupernodalLLT::upper_solve(const double* in, double* out) const {
    if (!factored_) return false;
    const int b = b_;
    const int ns = supernodes();
    Eigen::VectorXd y = Eigen::Map<const Eigen::VectorXd>(in, n_);
    Eigen::VectorXd tmp(std::max(1, max_rows_));
    for (int s = ns - 1; s >= 0; --s) {
        const int k = (sfirst_[s + 1] - sfirst_[s]) * b;
        const int mn = rstart_[s + 1] - rstart_[s];
        const int m = mn * b;
        const std::ptrdiff_t f = static_cast<std::ptrdiff_t>(k) + m;
        CMap block(lx_ + lstart_[s], f, k, Eigen::OuterStride<>(f));
        Eigen::Map<Eigen::VectorXd> x1(y.data() + static_cast<std::ptrdiff_t>(sfirst_[s]) * b, k);
        if (m > 0) {
            const int* rs = rows_.data() + rstart_[s];
            for (int t = 0; t < mn; ++t)
                for (int d = 0; d < b; ++d) tmp[t * b + d] = y[rs[t] * b + d];
            x1.noalias() -= block.bottomRows(m).transpose() * tmp.head(m);
        }
        block.topRows(k).transpose().triangularView<Eigen::Upper>().solveInPlace(x1);
    }
    for (int node = 0; node < nn_; ++node)
        for (int d = 0; d < b; ++d) out[node * b + d] = y[inv_[node] * b + d];
    return true;
}

bool SupernodalLLT::solve_in_place(double* x) const {
    return lower_solve(x, x) && upper_solve(x, x);
}

uint64_t SupernodalLLT::factor_bytes() const {
    const auto bytes = [](const auto& v) {
        return static_cast<uint64_t>(v.size()) * sizeof(v[0]);
    };
    return static_cast<uint64_t>(lx_size_) * sizeof(double) + bytes(inv_) + bytes(sfirst_) +
           bytes(rstart_) + bytes(rows_) + bytes(lstart_) + bytes(cstart_) + bytes(child_) +
           bytes(level_start_) + bytes(level_order_) + bytes(amap_start_) + bytes(amap_src_) +
           bytes(amap_dst_);
}

}  // namespace chudware::sim
