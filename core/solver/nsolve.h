// Shared numeric constraint solver core — C++/Eigen port of engine/doc/nsolve.py.
//
// ONE residual-based solver that the 2D sketch solver and the 3D mate solver
// parameterize. The solver knows nothing about sketches or mates: callers
// register *variables* and *residual blocks*. A residual block is a small vector
// function r(x_sub) -> R^m plus its analytic Jacobian J(x_sub) -> R^(m,k), tagged
// with an opaque string id used in diagnostics. Geometry parameterization lives
// entirely in the callers.
//
// Method: damped Newton / Levenberg-Marquardt over an analytic Jacobian, with
// adaptive scale-invariant damping, warm starts, a wall-clock budget for the drag
// path, and QR-rank diagnostics (DOF, redundant vs conflicting constraints).
//
// This is a line-by-line behavioral port of nsolve.py; see that file's module
// docstring for the design rationale. The Python uses scipy.sparse for assembly;
// here we assemble dense (n <= ~600 for v1 sketch/mate sizes, so JtJ is small).
// NO finite differences — every Jacobian is supplied analytically by the caller.
#pragma once

#include <Eigen/Dense>

#include <functional>
#include <optional>
#include <string>
#include <vector>

namespace chudware::solver {

// A residual function maps the sub-vector of its variables to an m-vector.
using ResidualFunc = std::function<Eigen::VectorXd(const Eigen::VectorXd&)>;
// Its Jacobian maps the same sub-vector to a dense (m, k) block, k = #vars.
using JacobianFunc = std::function<Eigen::MatrixXd(const Eigen::VectorXd&)>;

// Outcome of solve().
struct Result {
    Eigen::VectorXd x;
    bool converged = false;
    int iterations = 0;
    double max_residual = 0.0;
    // True iff the solve stopped because its wall-clock time_budget_ms elapsed
    // before converging (the interactive drag fast-path). Distinguishes a budget
    // cutoff from a genuine non-convergence (conflict / singular). x is always the
    // best finite iterate either way.
    bool budget_exceeded = false;
};

// Outcome of analyze() — drives the under/fully/over-defined UI states.
struct Analysis {
    int dof = 0;
    std::vector<std::string> redundant_tags;
    std::vector<std::string> conflicting_tags;
    bool well_posed = true;
    int rank = 0;
    int n_vars = 0;
    int n_residuals = 0;
};

// A registry of variables and tagged residual blocks, plus solve/analyze.
//
// Build a system by calling add_vars and add_residual (or soft_target), then
// solve(...) for positions and analyze(x) for diagnostics. Instances are cheap to
// rebuild; callers typically rebuild on topology change and reuse x as a warm
// start across drags.
class System {
public:
    int n_vars() const { return static_cast<int>(init_.size()); }

    // The registered initial-guess vector (the `sys._init` the Python warm-start
    // path copies as its cold base before overlaying per-entity warm values).
    Eigen::VectorXd init_vector() const {
        Eigen::VectorXd x(static_cast<int>(init_.size()));
        for (size_t i = 0; i < init_.size(); ++i) x(static_cast<int>(i)) = init_[i];
        return x;
    }

    // Allocate n variables; return their contiguous index vector. `init` (length
    // n, or empty for zeros) seeds the initial guess. Indices are stable for the
    // lifetime of the system. The returned vector is what callers hand to
    // add_residual as var_idx (slice it for sub-blocks).
    std::vector<int> add_vars(int n, const std::vector<double>& init = {});

    // Register a residual block. rfunc(x_sub) -> VectorXd(m) and
    // jfunc(x_sub) -> MatrixXd(m, k) where x_sub = x[var_idx], k = var_idx.size().
    // `tag` is an opaque constraint id surfaced by analyze. soft=true marks a
    // drag-aid block (see soft_target): it participates in the least-squares step
    // but is excluded from the convergence test and from analyze diagnostics. The
    // Jacobian must be analytic; its shape is probed once at registration against
    // the current initial guess to catch wiring mistakes early.
    void add_residual(ResidualFunc rfunc, const std::vector<int>& var_idx,
                      JacobianFunc jfunc, std::string tag,
                      double weight = 1.0, bool soft = false);

    // Low-weight residual pulling variable idx toward target (drag helper). The
    // residual is sqrt(weight)*(x_idx - target); weight is small so any real
    // constraint dominates the pull. Register one per dragged coordinate.
    void soft_target(int idx, double target, double weight = 0.01);

    // Remove every soft(-target) block registered so far, leaving the hard
    // constraint blocks — and their registration order — untouched. Lets an
    // interactive drag build the System ONCE and reuse it across mouse-move
    // frames: clear the previous frame's pulls, register this frame's, re-solve.
    // Row offsets / has_soft are re-finalized lazily on the next solve/analyze.
    void clear_soft_targets();

    // Interactive-mode flag. Dense LM steps use LDLT-first with an SVD
    // fallback; damping trials honour the wall-clock deadline, and analyze()
    // attributes redundant rows via a single ColPivQR instead of growing QRs.
    // Large sparse steps use augmented QR in either mode; dense exact steps retain
    // the Python-oracle path.
    void set_interactive(bool on) { interactive_ = on; }
    bool interactive() const { return interactive_; }

    // Per-variable trust region for the LM step. `limits` must be either
    // empty (the default — unlimited, every existing caller bit-identical) or one
    // entry per variable giving the largest |dx_i| a single LM iteration may take;
    // a non-positive entry means that variable is unlimited. When any component of
    // a step exceeds its cap the WHOLE step is scaled down by the smallest ratio, so
    // the descent direction is preserved and only the length is bounded.
    //
    // Why: on the normal equations a near-null direction is amplified by the SQUARE
    // of the Jacobian's condition number, so an inconsistent or barely-resolvable
    // constraint can produce a step of thousands of mm that still lowers the cost by
    // a hair and is therefore accepted. The mate solver caps translation by a
    // fraction of the assembly's size and rotation by a fraction of a turn.
    void set_step_limits(const Eigen::VectorXd& limits) { step_limit_ = limits; }

    // Levenberg-Marquardt solve with adaptive damping. x0 empty uses the
    // registered initial guess; pass the previous solution to warm-start. tol is
    // the convergence threshold on the max absolute residual. time_budget_ms, if
    // set, returns the best-so-far iterate once the wall clock is exceeded. With a
    // soft target present the drag is solved as a constrained problem (minimize the
    // soft objective subject to the hard constraints) via null-space projected
    // descent.
    Result solve(const std::optional<Eigen::VectorXd>& x0 = std::nullopt,
                 double tol = 1e-10, int max_iter = 50,
                 std::optional<double> time_budget_ms = std::nullopt);

    // DOF at configuration x: n_vars minus the numeric rank of the real
    // (non-soft) Jacobian, using the same tolerance as analyze(). Does not
    // evaluate residuals or attribute redundant/conflicting constraints.
    int degrees_of_freedom(const Eigen::VectorXd& x, double tol = 1e-7);

    // DOF / redundancy / conflict analysis at configuration x. Rank via a
    // column-pivoted QR of the dense Jacobian. dof = n_vars - rank. Redundant rows
    // are linearly dependent on earlier-registered rows (chronological attribution
    // — blame the newly added relation). Conflicting = redundant AND not currently
    // satisfiable (residual stays high). Soft-target blocks are excluded.
    Analysis analyze(const Eigen::VectorXd& x, double tol = 1e-7);

    // Dense Jacobian over real (non-soft) blocks at x. Exposed for callers that
    // need the full Jacobian for their own diagnostics (e.g. the sketch solver's
    // SVD-nullspace per-entity status coloring). Rows are stacked in block order.
    Eigen::MatrixXd real_jacobian(const Eigen::VectorXd& x);

private:
    struct Block {
        ResidualFunc rfunc;
        JacobianFunc jfunc;
        std::vector<int> var_idx;
        std::string tag;
        int m = 0;
        double weight = 1.0;
        bool soft = false;
        int row0 = 0;  // global row offset, filled at assembly time
    };

    // A fixed subset of blocks with precomputed row offsets and total row count.
    struct BlockSet {
        std::vector<const Block*> blocks;
        int n_rows = 0;
    };

    void finalize_rows();
    Eigen::VectorXd x0_or_init(const std::optional<Eigen::VectorXd>& x0) const;
    BlockSet make_block_set(bool want_soft) const;  // want_soft=false -> hard only
    Eigen::VectorXd sub(const Eigen::VectorXd& x, const std::vector<int>& idx) const;

    Eigen::VectorXd residual_of(const BlockSet& bs, const Eigen::VectorXd& x) const;
    Eigen::MatrixXd jacobian_of(const BlockSet& bs, const Eigen::VectorXd& x) const;

    // LM minimization of ||r||^2 for the block set. deadline is a steady_clock
    // count in seconds (negative = none). Writes back iterations and max_residual.
    Eigen::VectorXd lm(const Eigen::VectorXd& x, const BlockSet& bs, double tol,
                       int max_iter, double deadline, int& iters_out,
                       double& maxr_out) const;

    Result drag_solve(Eigen::VectorXd x, double tol, int max_iter,
                      double deadline);

    // Min-norm step for the normal equations. Exact path = always SVD (oracle);
    // interactive = LDLT-first + rcond gate, SVD fallback.
    Eigen::VectorXd solve_step(const Eigen::MatrixXd& A,
                               const Eigen::VectorXd& b) const;

    // Scale `step` in place so no component exceeds step_limit_ (no-op when the
    // limit vector is empty or the step already fits). Returns the applied scale.
    double clamp_step(Eigen::VectorXd& step) const;

    std::vector<double> init_;
    std::vector<Block> blocks_;
    int n_rows_ = 0;
    bool has_soft_ = false;
    bool dirty_ = true;
    bool interactive_ = false;
    Eigen::VectorXd step_limit_;  // per-variable trust region; empty = unlimited
};

}  // namespace chudware::solver
