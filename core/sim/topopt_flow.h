// Topology optimization: steady incompressible low-Reynolds Stokes-Brinkman
// flow on the prepared tet4 problem, plus the conjugate heat transfer (CHT)
// evaluator that shares the flow state.
//
// Model (SI). Unknowns u (velocity, P1 + quartic bubble) and p (pressure, P1):
//   find (u, p):  mu (grad u, grad v) + (alpha(rho) u, v) - (p, div v)
//                   = - sum_outlets p_out integral(v . n) dA          (all v)
//                 -(q, div u) = 0                                      (all q)
// Laplacian-form viscous term, so the outlet carries the do-nothing traction
// mu du/dn - p n = -p_out n. No inertia term: the model is creeping (Stokes)
// flow. Both inlet admission and the solved MINI velocity bound must stay at
// or below Re = kMaxReynolds. This is not Navier-Stokes or turbulent flow.
// Walls (every exterior triangle outside a
// port) are no-slip; port rim nodes shared with walls are no-slip; interior
// inlet nodes carry the prescribed inlet velocity (all from
// FlowBoundary::velocity).
//
// Discretization: MINI element (P1 velocity enriched with the quartic bubble
// b = 256 L0 L1 L2 L3 per component, P1 pressure), inf-sup stable without any
// tunable stabilization parameter. The bubble is statically condensed per
// element; with beta_e = mu int|grad b|^2 + alpha_e int b^2 (exact
// polynomial integrals, listed in formulation evidence) the condensed element
// matrix carries the pressure block -(int b)^2 / beta_e g_q . g_q' (the
// consistent MINI pressure stabilization) and alpha-dependent
// velocity/pressure corrections from the Brinkman bubble coupling.
// Pressure rows/columns are scaled by sigma = mu / h_median for conditioning;
// all public states and adjoints are physical.
//
// Resistance. alpha(rho) = alpha_max f(rho), f = Problem::interpolation shape
// (SIMP rho^p or RAMP rho / (1 + q (1 - rho))), so alpha(0) = 0 exactly (fluid
// is Brinkman-free) and alpha(1) = alpha_max = brinkman_max mu / L^2: alpha is
// bounded in [0, alpha_max]. The property floor min_density is not applied to
// the resistance (Stokes is well posed at alpha = 0). rho = 1 is solid.
//
// Responses.
//   dissipation Phi = integral( mu grad u : grad u + alpha |u|^2 ) dV  [W]
//     over the full MINI velocity (bubble included); equals the port power
//     (Dirichlet reaction power + outlet traction power) exactly.
//   pressure drop dp = area-mean p over inlets - area-mean p over outlets [Pa].
// Gradients: discrete adjoint of the condensed symmetric system; the lifted
// prescribed inlet velocity enters every derivative through the element
// operator derivative applied to the full state.
//
// Memory / refusal bounds (published):
//   * Prescribed-inlet Reynolds above kMaxReynolds = 1 refuses at construction.
//     solve() also bounds the full MINI speed by max_a |u_a| + |u_b| in each
//     element and refuses "flow_regime_invalid" above that Reynolds limit,
//     including pressure-driven flow the inlet-only check cannot detect.
//   * More than kMaxUnknowns = 1 500 000 free flow unknowns (4 per node)
//     refuses "problem_too_large" independent of the governor.
//   * Before allocating the pattern/slot table/factor, the storage forecast
//     (slot table 4*256*E B, triplet build 16*256*E B, reduced pattern bound
//     12*256*E B, LU forecast 2 * 12 * kFactorFillConstant * n^(4/3) B with
//     n free unknowns, vectors 640*N B) is admitted by governor::admit ->
//     "memory_budget". CHT admits this flow forecast plus its energy-system
//     forecast (same terms with 4x4 element blocks and n = free temperatures)
//     together. Forecasts are storage bounds, not RSS calibrations; the
//     measured LU nonzeros are reported in evidence.
//   * governor::low_memory_now() before any factorization -> "memory_low".
//   * Structural singularity refuses before factorization:
//     "inlet_fully_walled" (an inlet with every node on the no-slip rim, zero
//     discrete inflow), "outlet_fully_walled" (an outlet whose every node is
//     velocity-constrained: its pressure traction acts on nothing).
//   * Numerical: "singular_system" (LU failure / non-finite state),
//     "solve_inaccurate" (relative residual above 1e-8).
//   * Cooperative cancellation (EvaluationCancelled) during assembly, before
//     and after each factorization and before each adjoint solve.
#pragma once

#include "sim/topopt_model.h"

#include <Eigen/Dense>
#include <Eigen/Sparse>
#include <nlohmann/json.hpp>

#include <atomic>
#include <cstdint>
#include <memory>
#include <string>
#include <vector>

namespace chudware {
namespace sim {
namespace topology {

// MINI velocity of one element at the last solve and the linearization of its
// condensed bubble amplitude
//   u_b = -(alpha_e int(b N_a) sum_a u_a + int(b) grad p) / beta_e.
// The full element velocity is u(x) = sum_a N_a u_a + b(x) u_b.
struct FlowElementVelocity {
  Eigen::Matrix<double, 3, 4> nodal = Eigen::Matrix<double, 3, 4>::Zero();
  Eigen::Vector3d bubble = Eigen::Vector3d::Zero();
  double bubble_du = 0; // d u_b,c / d u_a,c (every corner a, diagonal in c)
  Eigen::Matrix<double, 3, 4> bubble_dp =
      Eigen::Matrix<double, 3, 4>::Zero(); // column q: d u_b / d p_q
  Eigen::Vector3d bubble_dalpha = Eigen::Vector3d::Zero(); // d u_b / d alpha
  // Element mean of the full velocity: sum_a u_a / 4 + kBubbleMean u_b.
  Eigen::Vector3d mean = Eigen::Vector3d::Zero();
};

// Reusable Stokes-Brinkman solve / adjoint / operator-derivative state for a
// problem carrying a FlowBoundary (physics flow or cht). Full DOF numbering
// (4N): velocity 3n+c (node-major), pressure 3N+n.
//
// Construction refuses: physics_mismatch (not flow/cht), section_missing,
// flow_regime_invalid, inlet_fully_walled, outlet_fully_walled,
// problem_too_large, memory_budget.
//
// Not thread-safe: one evaluation at a time per instance.
class FlowSystem {
public:
  static constexpr double kMaxReynolds = 1.0;
  static constexpr long kMaxUnknowns = 1500000;
  static constexpr double kFactorFillConstant = 30.0;
  static constexpr double kMaxRelativeResidual = 1e-8;
  // Exact quartic-bubble integrals per element volume V:
  // int b = kBubbleMean V, int b N_a = kBubbleLinear V, int b^2 = kBubbleMass
  // V, int |grad b|^2 = kBubbleStiff V sum_a |grad N_a|^2.
  static constexpr double kBubbleMean = 32.0 / 105.0;
  static constexpr double kBubbleLinear = 8.0 / 105.0;
  static constexpr double kBubbleMass = 65536.0 * 96.0 / 39916800.0;
  static constexpr double kBubbleStiff = 65536.0 * 24.0 / 362880.0;

  explicit FlowSystem(std::shared_ptr<const Problem> problem);
  ~FlowSystem();
  FlowSystem(const FlowSystem &) = delete;
  FlowSystem &operator=(const FlowSystem &) = delete;

  const Problem &problem() const { return *problem_; }
  int node_count() const { return node_count_; }
  int pressure_offset() const { return 3 * node_count_; }
  const DofReduction &reduction() const { return reduction_; }

  // Brinkman coefficient alpha(rho) = alpha_max f(rho) and d alpha / d rho.
  double alpha(double rho) const;
  double alpha_derivative(double rho) const;
  double alpha_max() const { return alpha_max_; }

  // Assemble the condensed MINI operator at `density`, factorize numerically
  // (symbolic analysis reused) and solve. Refuses invalid_density,
  // singular_system, solve_inaccurate, memory_low and flow_regime_invalid;
  // throws EvaluationCancelled. On any throw the system holds no solution;
  // a solution-based Reynolds refusal also clears the state vector.
  void solve(const Eigen::VectorXd &density, const std::atomic<bool> *cancel);
  bool solved() const { return solved_; }
  const Eigen::VectorXd &density() const { return density_; }
  // Discrete inflow carried by the prescribed inlet velocities (m^3/s).
  double prescribed_inflow_m3_s() const { return prescribed_inflow_m3_s_; }
  // Admitted storage forecast of this system (bytes).
  std::uint64_t forecast_bytes() const { return forecast_bytes_; }

  // Physical full state (4N): velocity (m/s) then pressure (Pa).
  const Eigen::VectorXd &state() const { return state_; }
  void velocity(Eigen::VectorXd &out) const; // 3N
  void pressure(Eigen::VectorXd &out) const; // N
  // MINI velocity of element e at the last solve, with the bubble
  // linearization consumers need for coupled adjoints (CHT advection).
  void element_velocity(int e, FlowElementVelocity &out) const;

  // Adjoint of the last factorization (operator is symmetric). rhs: full 4N
  // dJ/dx; constrained entries ignored. lambda: full 4N, zero on constrained
  // DOFs, A_ff lambda_f = rhs_f. Residual-checked.
  void solve_adjoint(const Eigen::VectorXd &rhs, Eigen::VectorXd &lambda,
                     const std::atomic<bool> *cancel) const;
  // grad_e += -weight * alpha'(rho_e) * lambda_e^T (dS_e/dalpha) x_e, the
  // implicit state part of dJ/drho for lambda from solve_adjoint(dJ/dx).
  // Includes the lifted prescribed (inlet) velocities. grad has E entries.
  void add_state_gradient(const Eigen::VectorXd &lambda, double weight,
                          Eigen::VectorXd &grad) const;

  // Dissipated power Phi (W); gradient (E, overwritten) when non-null.
  double dissipation(Eigen::VectorXd *gradient,
                     const std::atomic<bool> *cancel) const;
  // Area-mean inlet minus area-mean outlet pressure (Pa); linear in state.
  double pressure_drop() const;
  // "pressure_drop" (Pa): response = dp, value = dp / limit - 1.
  Constraint pressure_drop_constraint(double limit_pa, bool derivatives,
                                      const std::atomic<bool> *cancel) const;

  // Exact formulation and stabilization description (stable across calls).
  nlohmann::json formulation_evidence() const;
  // Last-solve evidence: residuals, flux balance per port, weak continuity,
  // P1 divergence, power balance, viscous/Brinkman split, porous leakage,
  // solution Reynolds numbers, factorization statistics.
  nlohmann::json evidence() const;

private:
  struct Factor;
  using Mat16 = Eigen::Matrix<double, 16, 16>;
  void element_matrix(int e, double alpha, Mat16 &S) const;
  void element_matrix_derivative(int e, double alpha, Mat16 &dS) const;
  void gather(const Eigen::VectorXd &full, int e,
              Eigen::Matrix<double, 16, 1> &xe) const;
  void scale_pressure(Mat16 &S) const;
  double port_flux(const std::vector<int> &faces) const;

  std::shared_ptr<const Problem> problem_;
  const TetMesh &mesh_;
  const FlowBoundary &flow_;
  int node_count_ = 0;
  double mu_ = 0;
  double alpha_max_ = 0;
  double sigma_ = 1; // pressure scale (Pa per scaled unit)
  Interpolation interp_;
  DofReduction reduction_;
  ReducedPattern pattern_;
  Eigen::MatrixXi element_dofs_;        // 16 x E
  Eigen::VectorXd bubble_viscous_;      // E: mu * int|grad b|^2
  Eigen::VectorXd traction_;            // 4N outlet traction load
  Eigen::VectorXd drop_functional_;     // 4N, dp = drop_functional_ . x
  std::vector<std::uint8_t> lift_flag_; // E: element touches a nonzero
                                        // prescribed DOF
  std::vector<std::vector<int>> inlet_faces_, outlet_faces_;
  double prescribed_inflow_m3_s_ = 0;
  std::uint64_t forecast_bytes_ = 0;

  // Per-solve reusable storage.
  Eigen::SparseMatrix<double> K_;
  std::unique_ptr<Factor> factor_;
  Eigen::VectorXd density_, alpha_e_, dalpha_e_;
  Eigen::VectorXd rhs_free_, x_free_, scratch_free_, state_;
  mutable Eigen::VectorXd adjoint_rhs_free_, adjoint_free_, adjoint_scratch_;
  mutable Eigen::VectorXd dphi_dx_, lambda_;
  bool solved_ = false;
  long numeric_factorizations_ = 0;
  double relative_residual_ = 0;
  mutable double max_adjoint_residual_ = 0;
};

// Physics evaluator for physics flow (objective dissipation, optional
// pressure_drop limit) or cht (objective mean_temperature, optional
// max_temperature and pressure_drop limits). Any other physics refuses
// "physics_mismatch". The CHT evaluator owns one FlowSystem and a steady P1
// SUPG advection-diffusion system on its full MINI velocity, which is the
// incompressible superficial Stokes-Brinkman velocity (div u = 0 in solid
// and fluid alike). Energy: rho_f c_p,f u . grad T = div(k(rho) grad T) with
// the CONSTANT fluid volumetric heat capacity, so the steady balance is
// conservative for any density (net port enthalpy outflow = boundary heat
// input) and invariant to T_ref; only the conductivity
// k(rho) = k_f + (k_s - k_f) f(rho) interpolates, and density reaches
// advection solely through the solved velocity. Brinkman leakage through
// nominal solid therefore carries heat; evidence thermal.heat_leakage
// reports it beside flow.leakage. See topopt_cht.cpp.
// Fields: velocity (m/s, node, 3), pressure (Pa, node, 1); CHT adds
// temperature (K, node, 1) and heat_flux (W/m2, element, 3).
std::unique_ptr<Physics> make_flow_physics(std::shared_ptr<const Problem>);

namespace detail {
// CHT evaluator construction (topopt_cht.cpp); use make_flow_physics.
std::unique_ptr<Physics> make_cht_physics(std::shared_ptr<const Problem>);
} // namespace detail

} // namespace topology
} // namespace sim
} // namespace chudware
