// Topology optimization: design-update methods (OCCT-free, Eigen + JSON).
//
// One entry point, `optimize(problem, physics, options)`, runs a caller-built
// Physics evaluator over a caller-prepared Problem with one of four genuine
// methods (no factory dependency):
//
// * simp / ramp — continuous densities x in [0,1] per design variable,
//   rho = passive(P_beta(H expand(x))): variable ties (expand), density filter
//   H, tanh Heaviside projection P_beta with continuation, passive override.
//   The interpolation (SIMP rho^p or RAMP) lives in the physics through
//   Problem::interpolation. Update: the Method of Moving Asymptotes (Svanberg
//   1987) with per-constraint artificial variables, its separable convex
//   subproblem solved through the concave dual by projected Newton. Every
//   g <= 0 constraint the physics returns plus the phase volume constraint is
//   handled; the objective is scaled by its baseline magnitude and its sense
//   (minimize / maximize) comes from objective_sense().
// * beso — bi-directional evolutionary structural optimization, soft-kill
//   (Huang & Xie): binary variables, element sensitivity numbers (keep benefit
//   per unit volume) filtered by the problem's filter and averaged with the
//   previous iteration's history, evolutionary volume target
//   V_{k+1} = max(V*, V_k (1 - ER)), admission-ratio cap on reintroduced
//   variables. Elements are removed AND reintroduced.
// * level_set — reaction-diffusion level set (Yamada et al. 2010 fictitious
//   interface energy): phi in [-1,1] per design variable evolves by
//   (M/dt + tau L^2 K) phi' = M (phi/dt + kappa F) with K the face-adjacency
//   diffusion operator (factored once), F the normalized ersatz-material
//   topological sensitivity of the volume Lagrangian, rho = H_h(phi) a C2
//   regularized Heaviside. The volume multiplier is solved by safeguarded
//   Newton on the exact dVolume/dlambda (through H'), and at the final volume
//   a reaction-gain backtracking guards against objective increases.
//
// Compatibility (`check_compatibility`, `optimizer_capabilities`): simp/ramp
// support every physics, objective and response limit; beso and level_set
// support structural compliance and thermal compliance without response
// limits (refusal "method_constraint_unsupported" otherwise).
//
// The optimizer owns cancellation (checked before every evaluation; the
// physics also observes the flag), a typed progress callback with restartable
// state (the adapter persists it), history, baseline and the final
// re-evaluation at the final design. It never rescales a design after the
// fact. Report units are SI; the adapter converts.
#pragma once

#include "sim/topopt_manufacturing.h"
#include "sim/topopt_model.h"

#include <Eigen/Dense>
#include <nlohmann/json.hpp>

#include <atomic>
#include <functional>
#include <memory>
#include <optional>
#include <string>
#include <vector>

namespace chudware {
namespace sim {
namespace topology {

// ── Projection / Heaviside primitives
// ───────────────────────────────────────
// Tanh threshold projection (Wang, Lazarov & Sigmund 2011 form):
//   P(r) = (tanh(beta eta) + tanh(beta (r - eta))) /
//          (tanh(beta eta) + tanh(beta (1 - eta)));  beta = 0 -> identity.
struct TanhProjection {
  double beta = 0.0;
  double eta = 0.5;
  double value(double r) const;
  double derivative(double r) const;
};

// C2 regularized Heaviside of half-width h on the level set:
//   H(phi) = 0 (phi <= -h), 1 (phi >= h), else
//   1/2 + 15/16 s - 5/8 s^3 + 3/16 s^5,  s = phi / h;
//   H'(phi) = 15/(16 h) (1 - s^2)^2 inside the band.
struct SmoothHeaviside {
  double half_width = 0.5;
  double value(double phi) const;
  double derivative(double phi) const;
};

// rho = passive(P(H expand(x))) with exact transpose. backward() uses the
// filtered field cached by the most recent forward(). Buffers are reused.
class DensityChain {
public:
  explicit DensityChain(std::shared_ptr<const Problem> problem);
  void set_projection(double beta, double eta);
  const TanhProjection &projection() const { return projection_; }
  void forward(const Eigen::VectorXd &x, Eigen::VectorXd &rho);
  void backward(const Eigen::VectorXd &g_rho, Eigen::VectorXd &g_x);

private:
  std::shared_ptr<const Problem> problem_;
  TanhProjection projection_;
  Eigen::VectorXd raw_, filtered_, work_;
};

// ── Method of Moving Asymptotes
// ─────────────────────────────────────────────
// Subproblem in standard form (z omitted, a_i = 0):
//   min sum_j [p0j/(U_j - x_j) + q0j/(x_j - L_j)] + sum_i (c y_i + d/2 y_i^2)
//   s.t. sum_j [pij/(U_j - x_j) + qij/(x_j - L_j)] + r_i - y_i <= 0,
//        alpha <= x <= beta, y >= 0.
struct MmaSettings {
  double initial_asymptote = 0.5;   // s0: U - L = 2 s0 (xmax - xmin)
  double asymptote_increase = 1.2;  // monotone progress
  double asymptote_decrease = 0.7;  // oscillation
  double constraint_penalty = 1000; // c (constraints are O(1) normalized)
  double constraint_quadratic = 1;  // d
  double regularization = 1e-5;     // raa0 / (xmax - xmin) curvature floor
  double bound_fraction = 0.1;      // alpha/beta keep this share from L/U
  int dual_max_iterations = 200;
  double dual_tolerance = 1e-10; // projected dual gradient infinity norm
};

struct MmaState {
  int iteration = 0; // updates since the last restart
  Eigen::VectorXd previous1, previous2, lower, upper;
  Eigen::VectorXd multipliers; // last dual solution (m)
};

struct MmaDualReport {
  int iterations = 0;
  double projected_gradient = 0;
  bool converged = false;
  double max_artificial = 0; // max y_i (> 0: subproblem had to relax g_i)
};

class Mma {
public:
  Mma() = default;
  // n = xmin.size() variables, m constraints. ModelError "invalid_mma".
  Mma(Eigen::VectorXd xmin, Eigen::VectorXd xmax, int constraints,
      MmaSettings settings = {});
  int variables() const { return static_cast<int>(xmin_.size()); }
  int constraints() const { return m_; }
  // One MMA step at x (inside [xmin,xmax]): x is replaced by the subproblem
  // minimizer. df0 (n), g (m), dg (m x n). move > 0 is an absolute per-
  // variable move limit. ModelError "invalid_mma" on size/finite violations.
  const MmaDualReport &update(Eigen::VectorXd &x, const Eigen::VectorXd &df0,
                              const Eigen::VectorXd &g,
                              const Eigen::MatrixXd &dg, double move);
  // Forget iterate history (asymptotes re-initialize on the next update).
  void restart();
  MmaState state() const;
  void restore(const MmaState &);
  const MmaDualReport &last_dual() const { return report_; }

private:
  double dual_eval(const Eigen::VectorXd &lam, bool with_hessian);

  int m_ = 0;
  MmaSettings settings_;
  Eigen::VectorXd xmin_, xmax_;
  int iteration_ = 0;
  Eigen::VectorXd x1_, x2_, low_, upp_, alpha_, beta_;
  Eigen::VectorXd p0_, q0_, r_;
  Eigen::MatrixXd p_, q_; // m x n
  Eigen::VectorXd lam_, grad_, gcur_, xs_, y_, trial_, dir_, a_, rhs_;
  Eigen::MatrixXd hess_, sub_;
  std::vector<int> free_;
  MmaDualReport report_;
};

// ── Compatibility
// ───────────────────────────────────────────────────────────
struct Compatibility {
  bool supported = true;
  std::string code;    // method_physics_unsupported | method_objective_...
  std::string message; // human reason naming the method and the input
};
Compatibility check_compatibility(const Settings &);
// Machine-readable method x physics x objective x limit x formulation matrix
// for topology_capabilities() (the caller merges physics availability).
nlohmann::json optimizer_capabilities();

// ── Termination, history, restartable state
// ─────────────────────────────────
enum class Termination {
  Converged,        // stationary design, all constraints satisfied
  MaxIterations,    // iteration limit reached first
  Stalled,          // no merit improvement over the stall window / cycling
  LineSearchFailed, // level set: reaction-gain backtracking exhausted
  Infeasible,       // stationary design that violates a constraint
  StuckAtBounds,    // infeasible and every variable pinned at a bound
  Cancelled,        // cancel flag observed; last completed state retained
  Failed            // physics/numerical failure; last completed state kept
};
const char *to_string(Termination);

struct IterationRecord {
  int iteration = 0;
  double objective = 0;        // physical SI value
  double objective_scaled = 0; // sense * objective / baseline scale
  double overhang = 0;         // relaxed overhang measure (0 when inactive)
  double merit = 0;            // scaled objective + penalty + violation
  double volume_fraction = 0;  // achieved phase fraction
  double volume_target = 0;    // current target (evolving for beso/level set)
  std::vector<double> constraints; // values, order = state.constraint_names
  double max_constraint = 0;
  bool feasible = false;
  double change = 0;   // simp/ramp: max |dx|; beso: switched volume fraction;
                       // level set: max |dphi| / 2. Into this iterate.
  double beta = 0;     // projection beta (simp/ramp)
  double grayness = 0; // volume mean of 4 rho (1 - rho) over design elements
  int added = 0, removed = 0; // beso switches / level-set sign changes
  int rejected = 0;           // candidate changes refused (enclosed voids)
  int line_search_trials = 0; // level set
  int enclosed_void_elements = 0;
  double interface_area_m2 = 0; // area of faces between rho>=thr and <thr
  double dual_gradient = 0;     // mma: projected dual gradient at the step
  double seconds = 0;           // wall time of this iteration
};
nlohmann::json to_json(const IterationRecord &);

// Everything needed to continue a run. `design` is the design the next
// iteration starts from: x (simp/ramp/beso, one entry per design variable) or
// phi (level set, one entry per design variable).
struct OptimizerState {
  Method method = Method::Simp;
  PhysicsKind physics = PhysicsKind::Structural;
  int iteration = 0;   // completed (recorded) iterations
  int evaluations = 0; // physics evaluations including line-search trials
  Eigen::VectorXd design;
  // simp / ramp
  MmaState mma;
  double beta = 0;
  int beta_stage_start = 0;
  double last_change = 0; // change of the update that produced `design`
  int stationary_count = 0;
  // beso
  Eigen::VectorXd beso_history; // E history-averaged filtered sensitivities
  std::vector<std::uint64_t> design_hashes; // recent beso designs (cycling)
  int cycle_count = 0;
  // beso / level set
  double volume_target = 0;
  double reaction_gain = 1; // level set backtracking gain kappa
  // common
  bool has_scale = false;
  double objective_scale = 1;
  double baseline_objective = 0;
  double best_merit = 0;
  int best_iteration = 0;
  int rejected_total = 0;
  std::vector<std::string> constraint_names;
  std::vector<IterationRecord> history;
};
nlohmann::json to_json(const OptimizerState &);
// ModelError "invalid_checkpoint" on malformed input.
OptimizerState optimizer_state_from_json(const nlohmann::json &);

// ── Callback and options
// ────────────────────────────────────────────────────
struct Progress {
  const IterationRecord &record;  // the iteration just completed
  const OptimizerState &state;    // live state; design = next start point
  const Eigen::VectorXd &density; // E densities of the recorded design
  const Evaluation &evaluation;   // physics evaluation of that design
  bool checkpoint = false;        // persist `state` now (interval / last)
  bool last = false;              // the run terminates after this call
};
// Exceptions thrown by the callback end the run as Failed ("callback_failed").
using ProgressCallback = std::function<void(const Progress &)>;

struct OptimizerOptions {
  const std::atomic<bool> *cancel = nullptr;
  ProgressCallback progress;
  int checkpoint_interval = 10; // iterations between checkpoint flags; 0 none
  std::optional<OptimizerState> resume; // continue a checkpointed run

  double feasibility_tolerance = 1e-3; // max g accepted as feasible
  int convergence_window = 2;          // consecutive stationary iterations
  int infeasible_window = 10;      // stationary + infeasible before giving up
  int stall_window = 40;           // iterations without merit improvement
  double stall_improvement = 1e-6; // relative merit decrease that counts
  int projection_continuation_interval = 25; // beta doubling interval
  MmaSettings mma;

  double beso_max_addition_ratio = 0.02; // admitted variables / variables
  int beso_convergence_window = 5;       // N in the Huang-Xie criterion

  double level_set_time_step = 0.2;           // pseudo time step dt
  double level_set_band = 0.5;                // Heaviside half-width h in phi
  double level_set_volume_step = 0.02;        // target decrease per iteration
  double level_set_increase_tolerance = 0.01; // accepted relative increase
  int max_line_search = 6;                    // reaction-gain halvings

  OverhangSettings overhang; // build direction/angle are taken from Settings
  int max_enclosure_passes = 8;
};

// ── Result
// ──────────────────────────────────────────────────────────────────
struct OptimizationResult {
  bool ok = false;        // numerical completion (not a certified design)
  bool converged = false; // Termination::Converged and final design feasible
  bool feasible = false;  // final re-evaluated constraints + enclosure rule
  Termination termination = Termination::Failed;
  std::string error_code;            // ModelError code for Failed, else empty
  std::string message;               // human description of the termination
  std::string convergence_criterion; // the exact criterion, with numbers
  std::string update_formulation;    // e.g. "mma(svanberg1987,dual)"
  std::string physics_formulation;   // Physics::formulation()
  std::string interpolation;         // "simp(p=3)", "ramp(q=5)", "linear"
  Method method = Method::Simp;
  PhysicsKind physics = PhysicsKind::Structural;
  DesignPhase phase = DesignPhase::Solid;
  ObjectiveSense sense = ObjectiveSense::Minimize;

  OptimizerState state;      // final design + history (restartable)
  Eigen::VectorXd density;   // E physical densities of the final design
  Eigen::VectorXd level_set; // E element phi (level set only; passive +/-1)

  bool has_baseline = false; // false when resumed or cancelled before eval
  Evaluation baseline;       // evaluation at the initial design
  bool has_final = false;
  bool final_reevaluated = false; // final evaluation is a fresh solve
  Evaluation final_evaluation;    // at `density`; values only when reevaluated
  std::vector<Constraint> final_constraints; // physics + volume, no gradients
  double baseline_objective = 0, final_objective = 0;
  std::string objective_unit;
  double final_overhang = 0;
  double max_violation = 0;    // max(0, max g) at the final design
  double volume_fraction = 0;  // achieved phase fraction
  double volume_limit = 0;     // settings.volume_fraction
  double volume_tolerance = 0; // accepted g slack (beso: one variable volume)
  int iterations = 0, evaluations = 0;
  ManufacturingReport manufacturing;
  nlohmann::json evidence = nlohmann::json::object();
};

// Refuses before any evaluation (ModelError): incompatible method/physics/
// objective/limit ("method_physics_unsupported", "method_objective_
// unsupported", "method_constraint_unsupported"), "physics_mismatch",
// "interpolation_mismatch", "invalid_optimizer_option", "invalid_checkpoint",
// "memory_budget_exceeded". Runtime outcomes (cancel, physics failure,
// non-finite responses, a constraint set that changes between evaluations or
// exceeds 64 entries: "constraint_set_changed" / "too_many_constraints",
// "method_constraint_unsupported" for beso/level_set) return a result with
// the last completed state retained.
OptimizationResult optimize(std::shared_ptr<const Problem> problem,
                            Physics &physics,
                            const OptimizerOptions &options = {});

} // namespace topology
} // namespace sim
} // namespace chudware
