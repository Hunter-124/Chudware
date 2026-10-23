// C++/Eigen port of engine/doc/nsolve.py. See nsolve.h for the API contract.
// LM damping and acceptance follow the oracle; large sparse steps use the
// augmented Jacobian to avoid squaring an assembly's condition number.
#include "nsolve.h"

#include <Eigen/Cholesky>
#include <Eigen/QR>
#include <Eigen/SparseCore>
#include <Eigen/SparseQR>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <limits>
#include <stdexcept>

namespace chudware::solver {
namespace {

// Steady-clock seconds, for the drag time budget. Returns a monotonic count.
double now_seconds() {
    using namespace std::chrono;
    return duration<double>(steady_clock::now().time_since_epoch()).count();
}

bool deadline_passed(double deadline) {
    return deadline >= 0.0 && now_seconds() >= deadline;
}

// True iff a wall-clock deadline was set, not converged, and the clock is past it.
bool budget_hit(double deadline, bool converged) {
    return deadline >= 0.0 && !converged && now_seconds() >= deadline;
}

double max_abs(const Eigen::VectorXd& v) {
    return v.size() ? v.cwiseAbs().maxCoeff() : 0.0;
}

// Minimum-norm least squares, with numpy's rcond=None singular-value cutoff.
// Retained for dense steps and rank-deficient sparse fallbacks.
Eigen::VectorXd lstsq(const Eigen::MatrixXd& A, const Eigen::VectorXd& b) {
    Eigen::JacobiSVD<Eigen::MatrixXd> svd(A, Eigen::ComputeThinU | Eigen::ComputeThinV);
    const Eigen::VectorXd& s = svd.singularValues();
    double smax = s.size() ? s(0) : 0.0;
    double cutoff = 2.220446049250313e-16 *
                    static_cast<double>(std::max(A.rows(), A.cols())) * smax;
    Eigen::VectorXd utb = svd.matrixU().transpose() * b;
    Eigen::VectorXd z(s.size());
    for (Eigen::Index i = 0; i < s.size(); ++i)
        z(i) = (s(i) > cutoff) ? (utb(i) / s(i)) : 0.0;
    return svd.matrixV() * z;
}

// Exact-path normal solve (oracle parity): retain numpy.linalg.lstsq's min-norm
// rule after scipy.linalg.solve(assume_a="pos") + caught LinAlgWarning.
// A Cholesky-first diagonal-rcond estimate diverged on coupled mate chains.
// Keep the original Jacobi path for these normal-equation solves.
Eigen::VectorXd solve_pos_exact(const Eigen::MatrixXd& A, const Eigen::VectorXd& b) {
    return lstsq(A, b);
}

// Interactive-path normal solve: LDLT-first for the common well-conditioned
// case, with an rcond gate + SVD fallback when the factor is unusable or ill-conditioned.
// Faster constant factors on drag/edit solves; not used on the oracle parity path.
Eigen::VectorXd solve_pos_interactive(const Eigen::MatrixXd& A, const Eigen::VectorXd& b) {
    Eigen::LDLT<Eigen::MatrixXd> ldlt(A);
    if (ldlt.info() == Eigen::Success) {
        // Reciprocal-condition estimate from the |D| pivots of LDLT (A = P^T L D L^T P).
        // Drop to SVD when the diagonal span is huge (rank-deficient / ill-conditioned).
        const Eigen::VectorXd d = ldlt.vectorD().cwiseAbs();
        const double dmax = d.size() ? d.maxCoeff() : 0.0;
        const double dmin = d.size() ? d.minCoeff() : 0.0;
        const double eps = std::numeric_limits<double>::epsilon();
        // (dmin/dmax) underestimates true rcond; use a slightly loose gate so the
        // common full-rank case stays on the cheap LDLT path.
        const bool well =
            dmax > 0.0 &&
            dmin > eps * dmax * static_cast<double>(std::max<Eigen::Index>(A.rows(), 1));
        if (well) {
            Eigen::VectorXd x = ldlt.solve(b);
            if (x.allFinite()) return x;
        }
    }
    return lstsq(A, b);
}

// Rank of J via column-pivoted QR; threshold relative to the largest pivot.
// Direct port of nsolve._numeric_rank.
int numeric_rank(const Eigen::MatrixXd& J, double tol) {
    if (J.size() == 0) return 0;
    Eigen::ColPivHouseholderQR<Eigen::MatrixXd> qr(J);
    Eigen::VectorXd diag = qr.matrixR().diagonal().cwiseAbs();
    if (diag.size() == 0) return 0;
    const double d0 = diag(0);  // largest pivot (column pivoting)
    const double eps = std::numeric_limits<double>::epsilon();
    double thresh = static_cast<double>(std::max(J.rows(), J.cols())) * eps * d0;
    thresh = std::max(thresh, tol * std::max(d0, 1.0));
    int r = 0;
    for (int i = 0; i < diag.size(); ++i)
        if (diag(i) > thresh) ++r;
    return r;
}

// Row indices of J that are dependent on earlier-registered rows (chronological
// attribution). Direct port of nsolve._dependent_rows — O(m) growing QRs.
// Exact / oracle path. Interactive mode uses dependent_rows_colpiv below.
std::vector<int> dependent_rows(const Eigen::MatrixXd& J, int rank, double tol) {
    const int m = static_cast<int>(J.rows());
    std::vector<int> dep;
    if (rank >= m || m == 0) return dep;
    if (rank <= 0) {
        for (int i = 0; i < m; ++i) dep.push_back(i);
        return dep;
    }
    std::vector<int> basis_rows;
    int cur_rank = 0;
    for (int ri = 0; ri < m; ++ri) {
        if (cur_rank >= rank) {
            for (int k = ri; k < m; ++k) dep.push_back(k);
            break;
        }
        Eigen::MatrixXd candidate(static_cast<int>(basis_rows.size()) + 1, J.cols());
        for (size_t k = 0; k < basis_rows.size(); ++k)
            candidate.row(static_cast<int>(k)) = J.row(basis_rows[k]);
        candidate.row(static_cast<int>(basis_rows.size())) = J.row(ri);
        int new_rank = numeric_rank(candidate, tol);
        if (new_rank > cur_rank) {
            basis_rows.push_back(ri);
            cur_rank = new_rank;
        } else {
            dep.push_back(ri);
        }
    }
    return dep;
}

// Interactive redundant-row attribution: one ColPivQR on J^T so the
// first `rank` pivot columns of J^T are the independent rows of J (in pivot order,
// which is NOT chronological). To keep chronological blame ("later constraint is
// redundant"), walk rows in registration order and mark a row dependent when it is
// NOT among the independent set, once the independent set is saturated — same
// end semantics as dependent_rows for the common case where rank is exact.
//
// Concrete algorithm: QR with column pivoting on J^T (n × m); the first `rank`
// permutation entries are independent row indices of J. Any row not in that set
// is dependent. Slightly coarser than the growing-QR chronological walk when
// several rows enter together, but matches for well-separated constraints and is
// O(one QR) instead of O(m) QRs — what the interactive drag budget needs.
std::vector<int> dependent_rows_colpiv(const Eigen::MatrixXd& J, int rank, double tol) {
    (void)tol;
    const int m = static_cast<int>(J.rows());
    std::vector<int> dep;
    if (rank >= m || m == 0) return dep;
    if (rank <= 0) {
        for (int i = 0; i < m; ++i) dep.push_back(i);
        return dep;
    }
    Eigen::ColPivHouseholderQR<Eigen::MatrixXd> qr(J.transpose());
    const Eigen::PermutationMatrix<Eigen::Dynamic, Eigen::Dynamic> P = qr.colsPermutation();
    std::vector<char> indep(static_cast<size_t>(m), 0);
    const int take = std::min(rank, m);
    for (int i = 0; i < take; ++i) {
        const int ri = static_cast<int>(P.indices()(i));
        if (ri >= 0 && ri < m) indep[static_cast<size_t>(ri)] = 1;
    }
    for (int ri = 0; ri < m; ++ri)
        if (!indep[static_cast<size_t>(ri)]) dep.push_back(ri);
    return dep;
}

}  // namespace

// -- construction -------------------------------------------------------------

std::vector<int> System::add_vars(int n, const std::vector<double>& init) {
    if (n < 0) throw std::invalid_argument("add_vars: n must be >= 0");
    const int start = static_cast<int>(init_.size());
    if (init.empty()) {
        init_.insert(init_.end(), static_cast<size_t>(n), 0.0);
    } else {
        if (static_cast<int>(init.size()) != n)
            throw std::invalid_argument("add_vars: init has wrong length");
        init_.insert(init_.end(), init.begin(), init.end());
    }
    std::vector<int> idx(n);
    for (int i = 0; i < n; ++i) idx[i] = start + i;
    return idx;
}

void System::add_residual(ResidualFunc rfunc, const std::vector<int>& var_idx,
                          JacobianFunc jfunc, std::string tag, double weight,
                          bool soft) {
    for (int i : var_idx)
        if (i < 0 || i >= n_vars())
            throw std::invalid_argument("add_residual: var_idx out of range");
    if (weight <= 0.0) throw std::invalid_argument("add_residual: weight must be > 0");

    // Probe shapes once against the current initial guess.
    Eigen::VectorXd x_sub(static_cast<int>(var_idx.size()));
    for (size_t i = 0; i < var_idx.size(); ++i) x_sub(static_cast<int>(i)) = init_[var_idx[i]];
    Eigen::VectorXd r0 = rfunc(x_sub);
    if (r0.size() == 0) throw std::invalid_argument("add_residual: rfunc returned empty");
    const int m = static_cast<int>(r0.size());
    Eigen::MatrixXd j0 = jfunc(x_sub);
    if (j0.rows() != m || j0.cols() != static_cast<int>(var_idx.size()))
        throw std::invalid_argument("add_residual: jfunc returned wrong shape");

    Block b;
    b.rfunc = std::move(rfunc);
    b.jfunc = std::move(jfunc);
    b.var_idx = var_idx;
    b.tag = std::move(tag);
    b.m = m;
    b.weight = weight;
    b.soft = soft;
    blocks_.push_back(std::move(b));
    dirty_ = true;
}

void System::soft_target(int idx, double target, double weight) {
    if (idx < 0 || idx >= n_vars()) throw std::invalid_argument("soft_target: idx out of range");
    if (weight <= 0.0) throw std::invalid_argument("soft_target: weight must be > 0");
    const double s = std::sqrt(weight);
    const double t = target;
    add_residual(
        [s, t](const Eigen::VectorXd& xs) {
            Eigen::VectorXd r(1);
            r(0) = s * (xs(0) - t);
            return r;
        },
        {idx},
        [s](const Eigen::VectorXd&) {
            Eigen::MatrixXd j(1, 1);
            j(0, 0) = s;
            return j;
        },
        "soft:" + std::to_string(idx), weight, /*soft=*/true);
}

void System::clear_soft_targets() {
    auto it = std::remove_if(blocks_.begin(), blocks_.end(),
                             [](const Block& b) { return b.soft; });
    if (it == blocks_.end()) return;  // nothing soft registered — no-op
    blocks_.erase(it, blocks_.end());
    dirty_ = true;  // finalize_rows() recomputes row offsets + has_soft_ lazily
}

// -- assembly -----------------------------------------------------------------

void System::finalize_rows() {
    int row = 0;
    has_soft_ = false;
    for (auto& b : blocks_) {
        b.row0 = row;
        row += b.m;
        if (b.soft) has_soft_ = true;
    }
    n_rows_ = row;
    dirty_ = false;
}

Eigen::VectorXd System::x0_or_init(const std::optional<Eigen::VectorXd>& x0) const {
    if (!x0) {
        Eigen::VectorXd x(static_cast<int>(init_.size()));
        for (size_t i = 0; i < init_.size(); ++i) x(static_cast<int>(i)) = init_[i];
        return x;
    }
    if (x0->size() != n_vars())
        throw std::invalid_argument("x0 has wrong number of entries");
    return *x0;
}

System::BlockSet System::make_block_set(bool want_soft) const {
    BlockSet bs;
    int row = 0;
    for (const auto& b : blocks_) {
        if (!want_soft && b.soft) continue;
        bs.blocks.push_back(&b);
        row += b.m;
    }
    bs.n_rows = row;
    return bs;
}

Eigen::VectorXd System::sub(const Eigen::VectorXd& x, const std::vector<int>& idx) const {
    Eigen::VectorXd s(static_cast<int>(idx.size()));
    for (size_t i = 0; i < idx.size(); ++i) s(static_cast<int>(i)) = x(idx[i]);
    return s;
}

Eigen::VectorXd System::residual_of(const BlockSet& bs, const Eigen::VectorXd& x) const {
    Eigen::VectorXd r(bs.n_rows);
    int row = 0;
    for (const Block* b : bs.blocks) {
        Eigen::VectorXd rb = b->rfunc(sub(x, b->var_idx));
        r.segment(row, b->m) = rb;
        row += b->m;
    }
    return r;
}

Eigen::MatrixXd System::jacobian_of(const BlockSet& bs, const Eigen::VectorXd& x) const {
    Eigen::MatrixXd J = Eigen::MatrixXd::Zero(bs.n_rows, n_vars());
    int row = 0;
    for (const Block* b : bs.blocks) {
        Eigen::MatrixXd jb = b->jfunc(sub(x, b->var_idx));
        for (size_t c = 0; c < b->var_idx.size(); ++c)
            J.block(row, b->var_idx[c], b->m, 1) = jb.col(static_cast<int>(c));
        row += b->m;
    }
    return J;
}

// -- solve --------------------------------------------------------------------

Eigen::VectorXd System::solve_step(const Eigen::MatrixXd& A,
                                   const Eigen::VectorXd& b) const {
    if (interactive_) return solve_pos_interactive(A, b);
    return solve_pos_exact(A, b);
}

double System::clamp_step(Eigen::VectorXd& step) const {
    if (step_limit_.size() != step.size()) return 1.0;
    double scale = 1.0;
    for (Eigen::Index i = 0; i < step.size(); ++i) {
        const double cap = step_limit_(i);
        const double mag = std::abs(step(i));
        if (cap > 0.0 && mag > cap) scale = std::min(scale, cap / mag);
    }
    if (scale < 1.0) step *= scale;
    return scale;
}

Result System::solve(const std::optional<Eigen::VectorXd>& x0, double tol,
                     int max_iter, std::optional<double> time_budget_ms) {
    if (dirty_) finalize_rows();
    const double t_start = now_seconds();
    const double deadline =
        time_budget_ms ? t_start + (*time_budget_ms) / 1e3 : -1.0;

    Eigen::VectorXd x = x0_or_init(x0);
    if (n_rows_ == 0) {
        Result res;
        res.x = x;
        res.converged = true;
        return res;
    }

    if (!has_soft_) {
        BlockSet bs = make_block_set(/*want_soft=*/false);
        int iters = 0;
        double maxr = 0.0;
        x = lm(x, bs, tol, max_iter, deadline, iters, maxr);
        const bool conv = maxr <= tol;
        Result res;
        res.x = x;
        res.converged = conv;
        res.iterations = iters;
        res.max_residual = maxr;
        res.budget_exceeded = budget_hit(deadline, conv);
        return res;
    }

    return drag_solve(x, tol, max_iter, deadline);
}

Eigen::VectorXd System::lm(const Eigen::VectorXd& x_in, const BlockSet& bs,
                           double tol, int max_iter, double deadline,
                           int& iters_out, double& maxr_out) const {
    Eigen::VectorXd x = x_in;
    if (bs.n_rows == 0) {
        iters_out = 0;
        maxr_out = 0.0;
        return x;
    }
    Eigen::VectorXd r = residual_of(bs, x);
    double cost = r.dot(r);
    Eigen::VectorXd best_x = x;
    double best_maxr = max_abs(r);
    if (best_maxr <= tol) {
        iters_out = 0;
        maxr_out = best_maxr;
        return best_x;
    }

    double lam = -1.0;  // LM damping; initialized from the JtJ scale on iter 1
    double nu = 2.0;    // damping growth factor on rejected steps
    int it = 0;
    Eigen::VectorXd step;
    std::size_t potential_entries = 0;
    for (const Block* block : bs.blocks)
        potential_entries += static_cast<std::size_t>(block->m) * block->var_idx.size();
    const bool sparse_step = n_vars() >= 128 &&
        static_cast<double>(potential_entries) < 0.25 * bs.n_rows * n_vars();
    std::vector<Eigen::Triplet<double>> entries;
    Eigen::SparseMatrix<double> sparse_jacobian;
    Eigen::VectorXd sparse_rhs;
    Eigen::SparseQR<Eigen::SparseMatrix<double>, Eigen::COLAMDOrdering<int>> sparse_qr;
    if (sparse_step) {
        entries.reserve(potential_entries + n_vars());
        sparse_jacobian.resize(bs.n_rows + n_vars(), n_vars());
        sparse_rhs.setZero(bs.n_rows + n_vars());
    }
    for (it = 1; it <= max_iter; ++it) {
        Eigen::MatrixXd JtJ;
        Eigen::VectorXd g, diag;
        if (sparse_step) {
            // Mate blocks touch only their participating components. Do not
            // allocate or multiply a mostly-zero global dense Jacobian.
            entries.clear();
            diag.setZero(n_vars());
            int row = 0;
            for (const Block* block : bs.blocks) {
                const Eigen::MatrixXd jb = block->jfunc(sub(x, block->var_idx));
                for (std::size_t c = 0; c < block->var_idx.size(); ++c) {
                    const int column = block->var_idx[c];
                    // Preserve jacobian_of's last-column semantics for aliases.
                    if (std::find(block->var_idx.begin() + c + 1,
                                  block->var_idx.end(), column) != block->var_idx.end())
                        continue;
                    for (int rj = 0; rj < block->m; ++rj) {
                        const double value = jb(rj, static_cast<Eigen::Index>(c));
                        if (value != 0.0) {
                            entries.emplace_back(row + rj, column, value);
                            diag(column) += value * value;
                        }
                    }
                }
                row += block->m;
            }
            sparse_rhs.head(bs.n_rows) = -r;
        } else {
            Eigen::MatrixXd J = jacobian_of(bs, x);
            JtJ = J.transpose() * J;
            g = J.transpose() * r;
            diag = JtJ.diagonal();
        }
        const std::size_t jacobian_entries = entries.size();
        const double largest_diagonal = diag.maxCoeff();
        diag = diag.cwiseMax(1e-12);  // scale-invariant damping basis
        // FreeCAD-planegcs-style initial damping: normalize the opening step to the
        // problem scale (tau * max(diag(JtJ)), tau = 1e-4). A fixed 1e-4 over-steps
        // when numeric-Jacobian conic/tangent blocks make the normal matrix tiny
        // (O(1e-8)); scaling by max(diag) keeps the first LM step consistently
        // conditioned. Subsequent iterations keep the multiplicatively adapted lam.
        if (lam < 0.0) lam = 1e-4 * std::max(largest_diagonal, 1.0);

        bool accepted = false;
        for (int t = 0; t < 12; ++t) {
            // Honour the wall-clock deadline inside the damping trial
            // loop (interactive only — exact path keeps the outer-only check so
            // iteration counts stay oracle-identical when no budget is set).
            if (interactive_ && deadline_passed(deadline)) {
                iters_out = it;
                maxr_out = best_maxr;
                return best_x;
            }
            if (sparse_step) {
                // [J; sqrt(lambda * diag)] step = [-r; 0] is the same damped
                // least-squares problem, without losing weak rotation directions
                // to the squared spectrum of J^T J.
                entries.resize(jacobian_entries);
                for (int c = 0; c < n_vars(); ++c)
                    entries.emplace_back(bs.n_rows + c, c, std::sqrt(lam * diag(c)));
                sparse_jacobian.setFromTriplets(entries.begin(), entries.end());
                sparse_qr.setPivotThreshold(std::numeric_limits<double>::epsilon() *
                    sparse_jacobian.rows() * std::sqrt((1.0 + lam) * diag.maxCoeff()));
                sparse_qr.compute(sparse_jacobian);
                bool solved = false;
                if (sparse_qr.info() == Eigen::Success && sparse_qr.rank() == n_vars()) {
                    step = sparse_qr.solve(sparse_rhs);
                    solved = step.allFinite();
                }
                if (!solved) {
                    // A rank-deficient QR basic solution is not minimum-norm.
                    // Solve the augmented system itself: forming its normal
                    // matrix squares the spectrum and discards weak constrained
                    // directions merely because another coordinate is free.
                    step = lstsq(Eigen::MatrixXd(sparse_jacobian), sparse_rhs);
                }
            } else {
                Eigen::MatrixXd A = JtJ;
                A.diagonal() += lam * diag;
                step = solve_step(A, -g);
            }
            clamp_step(step);

            Eigen::VectorXd x_new = x + step;
            Eigen::VectorXd r_new = residual_of(bs, x_new);
            double cost_new = r_new.dot(r_new);
            if (cost_new < cost) {
                x = x_new;
                r = r_new;
                cost = cost_new;
                lam = std::max(lam / 10.0, 1e-14);
                nu = 2.0;
                accepted = true;
                double maxr = max_abs(r);
                if (maxr < best_maxr) {
                    best_maxr = maxr;
                    best_x = x;
                }
                break;
            }
            lam *= nu;
            nu *= 2.0;
            if (lam > 1e12) break;
        }

        double maxr = max_abs(r);
        if (maxr <= tol) {
            iters_out = it;
            maxr_out = maxr;
            return x;
        }
        if (deadline_passed(deadline)) {
            iters_out = it;
            maxr_out = best_maxr;
            return best_x;
        }
        if (!accepted) break;  // damping saturated without progress
        // A representable cost reduction is progress even when high damping
        // makes the step tiny. Residual tolerance, rejected trials and the
        // iteration/deadline bounds decide termination, not mixed-unit step size.
    }

    iters_out = it;
    maxr_out = best_maxr;
    return best_x;
}

Result System::drag_solve(Eigen::VectorXd x, double tol, int max_iter,
                          double deadline) {
    BlockSet hard = make_block_set(/*want_soft=*/false);
    const int hard_rows = hard.n_rows;

    // Recover (idx, w, t) for each soft block: r(x_i) = sqrt(w)*(x_i - t).
    std::vector<int> soft_idx;
    std::vector<double> soft_w, soft_t;
    for (const auto& b : blocks_) {
        if (!b.soft) continue;
        soft_idx.push_back(b.var_idx[0]);
        soft_w.push_back(b.weight);
        Eigen::VectorXd zero(1);
        zero(0) = 0.0;
        soft_t.push_back(-b.rfunc(zero)(0) / std::sqrt(b.weight));
    }

    // Per-variable inverse weight for the constrained-LS step.
    const double free_w = 1e-6;
    Eigen::VectorXd wsum = Eigen::VectorXd::Constant(n_vars(), free_w);
    for (size_t i = 0; i < soft_idx.size(); ++i) wsum(soft_idx[i]) += soft_w[i];
    Eigen::VectorXd winv = wsum.cwiseInverse();

    auto soft_cost = [&](const Eigen::VectorXd& xv) {
        double c = 0.0;
        for (size_t i = 0; i < soft_idx.size(); ++i) {
            double d = xv(soft_idx[i]) - soft_t[i];
            c += soft_w[i] * d * d;
        }
        return c;
    };

    // First projection onto the hard manifold; this is the baseline best.
    int total_it = 0, it = 0;
    double hard_maxr = 0.0;
    x = lm(x, hard, tol, 20, deadline, it, hard_maxr);
    total_it += it;
    Eigen::VectorXd best_x = x;
    double best_maxr = hard_maxr;
    double best_cost = soft_cost(x);

    for (int outer = 0; outer < max_iter; ++outer) {
        if (deadline_passed(deadline)) break;
        if (hard_rows == 0) {
            for (size_t i = 0; i < soft_idx.size(); ++i) x(soft_idx[i]) = soft_t[i];
            best_x = x;
            best_maxr = 0.0;
            break;
        }

        // Desired displacement g: move dragged vars toward the cursor; free vars stay.
        Eigen::VectorXd tvec = x;
        for (size_t i = 0; i < soft_idx.size(); ++i) tvec(soft_idx[i]) = soft_t[i];
        Eigen::VectorXd g = tvec - x;
        if (max_abs(g) < 1e-12) break;

        Eigen::MatrixXd Jh = jacobian_of(hard, x);                  // (m, n)
        // KKT-Schur: (Jh W^-1 Jh^T) lam = Jh g ;  s = g - W^-1 Jh^T lam.
        Eigen::MatrixXd M = Jh * winv.asDiagonal() * Jh.transpose();
        Eigen::VectorXd rhs = Jh * g;
        Eigen::MatrixXd Meps = M;
        Meps.diagonal().array() += 1e-12;
        Eigen::VectorXd lam_k;
        {
            Eigen::LDLT<Eigen::MatrixXd> ldlt(Meps);
            if (ldlt.info() == Eigen::Success) {
                lam_k = ldlt.solve(rhs);
                if (!lam_k.allFinite()) lam_k = lstsq(M, rhs);
            } else {
                lam_k = lstsq(M, rhs);
            }
        }
        Eigen::VectorXd s = g - winv.cwiseProduct(Jh.transpose() * lam_k);
        if (!s.allFinite() || max_abs(s) < 1e-12) break;

        // Manifold line search: take the step, re-project, accept only on improvement.
        double f0 = soft_cost(x);
        double alpha = 1.0;
        bool advanced = false;
        for (int ls = 0; ls < 20; ++ls) {
            Eigen::VectorXd x_try = x + alpha * s;
            if (!x_try.allFinite()) {
                alpha *= 0.5;
                continue;
            }
            int it_p = 0;
            double maxr_try = 0.0;
            x_try = lm(x_try, hard, tol, 20, deadline, it_p, maxr_try);
            total_it += it_p;
            double f1 = soft_cost(x_try);
            if (f1 < f0 - 1e-15) {
                x = x_try;
                hard_maxr = maxr_try;
                advanced = true;
                if (maxr_try <= best_maxr && f1 <= best_cost) {
                    best_x = x;
                    best_maxr = maxr_try;
                    best_cost = f1;
                }
                break;
            }
            alpha *= 0.5;
        }
        if (!advanced) break;
    }

    // Final exact projection onto the hard constraints (cheap when warm).
    int it_f = 0;
    x = lm(x, hard, tol, max_iter, deadline, it_f, hard_maxr);
    total_it += it_f;
    if (!x.allFinite() || hard_maxr > best_maxr + 1e-9) {
        x = best_x;
        hard_maxr = best_maxr;
    }
    const bool conv = hard_maxr <= tol;
    Result res;
    res.x = x;
    res.converged = conv;
    res.iterations = total_it;
    res.max_residual = hard_maxr;
    res.budget_exceeded = budget_hit(deadline, conv);
    return res;
}

Eigen::MatrixXd System::real_jacobian(const Eigen::VectorXd& x) {
    if (dirty_) finalize_rows();
    std::vector<const Block*> real;
    int m_total = 0;
    for (const auto& b : blocks_) {
        if (b.soft) continue;
        real.push_back(&b);
        m_total += b.m;
    }
    Eigen::MatrixXd J = Eigen::MatrixXd::Zero(m_total, n_vars());
    int row = 0;
    for (const Block* b : real) {
        Eigen::MatrixXd jb = b->jfunc(sub(x, b->var_idx));
        for (size_t c = 0; c < b->var_idx.size(); ++c)
            J.block(row, b->var_idx[c], b->m, 1) = jb.col(static_cast<int>(c));
        row += b->m;
    }
    return J;
}

// -- diagnostics --------------------------------------------------------------

int System::degrees_of_freedom(const Eigen::VectorXd& x, double tol) {
    if (x.size() != n_vars())
        throw std::invalid_argument("degrees_of_freedom: x has wrong number of entries");
    return n_vars() - numeric_rank(real_jacobian(x), tol);
}

Analysis System::analyze(const Eigen::VectorXd& x, double tol) {
    if (dirty_) finalize_rows();
    if (x.size() != n_vars())
        throw std::invalid_argument("analyze: x has wrong number of entries");

    // Real (non-soft) blocks only.
    std::vector<const Block*> real;
    for (const auto& b : blocks_)
        if (!b.soft) real.push_back(&b);
    const int n = n_vars();
    if (real.empty() || n == 0) {
        Analysis a;
        a.dof = n;
        a.well_posed = true;
        a.n_vars = n;
        return a;
    }

    int m_total = 0;
    for (const Block* b : real) m_total += b->m;
    Eigen::MatrixXd J = Eigen::MatrixXd::Zero(m_total, n);
    Eigen::VectorXd r(m_total);
    std::vector<std::string> row_tag(m_total);
    int row = 0;
    for (const Block* b : real) {
        Eigen::VectorXd xs = sub(x, b->var_idx);
        Eigen::MatrixXd jb = b->jfunc(xs);
        Eigen::VectorXd rb = b->rfunc(xs);
        for (size_t c = 0; c < b->var_idx.size(); ++c)
            J.block(row, b->var_idx[c], b->m, 1) = jb.col(static_cast<int>(c));
        r.segment(row, b->m) = rb;
        for (int k = 0; k < b->m; ++k) row_tag[row + k] = b->tag;
        row += b->m;
    }

    const int rank = numeric_rank(J, tol);
    const int dof = n - rank;
    // Interactive analyze uses a single ColPivQR attribution; the
    // exact path keeps the chronological growing-QR walk for oracle parity.
    std::vector<int> redundant = interactive_ ? dependent_rows_colpiv(J, rank, tol)
                                              : dependent_rows(J, rank, tol);

    std::vector<std::string> redundant_tags, conflicting_tags;
    std::vector<std::string> seen_r, seen_c;
    auto seen = [](const std::vector<std::string>& v, const std::string& s) {
        return std::find(v.begin(), v.end(), s) != v.end();
    };
    for (int ri : redundant) {
        const std::string& tag = row_tag[ri];
        const bool is_conflict = std::abs(r(ri)) > std::max(1e-6, 1e3 * tol);
        if (is_conflict) {
            if (!seen(seen_c, tag)) {
                conflicting_tags.push_back(tag);
                seen_c.push_back(tag);
            }
        } else {
            if (!seen(seen_r, tag)) {
                redundant_tags.push_back(tag);
                seen_r.push_back(tag);
            }
        }
    }
    // A tag flagged as both is a conflict; drop it from warn.
    if (!conflicting_tags.empty()) {
        std::vector<std::string> filtered;
        for (const auto& t : redundant_tags)
            if (!seen(conflicting_tags, t)) filtered.push_back(t);
        redundant_tags.swap(filtered);
    }

    Analysis a;
    a.dof = dof;
    a.redundant_tags = std::move(redundant_tags);
    a.conflicting_tags = std::move(conflicting_tags);
    a.well_posed = a.conflicting_tags.empty();
    a.rank = rank;
    a.n_vars = n;
    a.n_residuals = m_total;
    return a;
}

}  // namespace chudware::solver
