// Topology optimization: steady heat conduction on the prepared tet4 problem.
//
// Field. theta = T - T_ref (K), T_ref =
// ThermalBoundary::reference_temperature_k. Conductivity k_e = k_solid *
// Interpolation::scale(rho_e). Operator
//   K(rho) = sum_e k_e V_e G_e^T G_e  +  sum_conv h A/12 [2 1 1; 1 2 1; 1 1 2]
// with the convection film term fixed (not design-dependent). Loads: surface
// flux/power q A/3 per corner, convection h (T_amb - T_ref) A/3 per corner.
// Fixed temperatures (fixtures, unreferenced-node pins) are eliminated through
// DofReduction with prescribed theta and lifted into the free right-hand side,
// so nonzero Dirichlet values enter every response and every adjoint exactly.
//
// Responses.
//   thermal compliance C = q^T theta = sum over flux/power patches of
//     integral q (T - T_ref) dA   [W K]: heat-input-weighted temperature rise.
//     It reduces to the classic f^T T when every fixture and ambient sits at
//     T_ref. dC/drho_e = -lambda_e^T dK_e/drho_e theta_e, K_ff lambda = q_f.
//   max temperature: Kreisselmeier-Steinhauser upper bound over referenced
//     nodes, KS = T_peak + ln(sum exp(beta (T_i - T_peak))) / beta >= T_peak,
//     KS - T_peak <= ln(n) / beta; beta = 100 / max(|T_limit - T_ref|, 1 K).
//     The exact raw peak is reported beside it.
//
// The sparse pattern, convection operator and symbolic factorization are built
// once per system; each solve refills values and refactorizes numerically.
#pragma once

#include "sim/topopt_model.h"

#include <Eigen/Dense>
#include <Eigen/Sparse>
#include <nlohmann/json.hpp>

#include <atomic>
#include <memory>
#include <string>
#include <vector>

namespace chudware {
namespace sim {
namespace topology {

// Reusable conduction solve / adjoint / operator-derivative state for any
// problem carrying a ThermalBoundary and solid conductivity (thermal,
// thermomechanical). CHT is refused ("physics_mismatch"): its conductivity
// interpolates solid <-> fluid and couples advection.
//
// Construction refuses: physics_mismatch, section_missing (no thermal
// boundary), material_missing (no k), thermal_unconstrained (a node-connected
// component with neither a fixed referenced node nor a convection face),
// memory_budget (governor admission).
//
// Not thread-safe: one evaluation at a time per instance.
class ThermalSystem {
public:
  explicit ThermalSystem(std::shared_ptr<const Problem> problem);
  ~ThermalSystem();
  ThermalSystem(const ThermalSystem &) = delete;
  ThermalSystem &operator=(const ThermalSystem &) = delete;

  const Problem &problem() const { return *problem_; }
  // Reduction of theta: prescribed = T_fixed - T_ref on constrained nodes.
  const DofReduction &reduction() const { return reduction_; }
  double reference_temperature_k() const { return reference_k_; }
  double solid_conductivity_w_mk() const { return k_solid_; }
  // Element conductivity k_e and dk_e/drho_e at the last solved density.
  double element_conductivity(int e) const;
  double element_conductivity_derivative(int e) const;

  // Assemble K(density), factorize numerically (symbolic analysis reused) and
  // solve for theta. Refuses: invalid_density, singular_system (factorization
  // not positive definite, non-finite solution), solve_inaccurate (relative
  // residual above 1e-8), memory_low (governor emergency). Throws
  // EvaluationCancelled when *cancel is raised. On any throw the system holds
  // no solution (solved() == false).
  void solve(const Eigen::VectorXd &density, const std::atomic<bool> *cancel);
  bool solved() const { return solved_; }

  // Full nodal fields of the last solve (N): theta (K) and T = theta + T_ref.
  const Eigen::VectorXd &temperature_rise() const { return theta_; }
  void temperature(Eigen::VectorXd &out_kelvin) const;

  // Adjoint of the last factorization. rhs: full N vector dJ/dtheta;
  // constrained entries are ignored (theta is prescribed there). lambda: full
  // N, zero on constrained nodes, K_ff lambda_f = rhs_f. Residual-checked.
  void solve_adjoint(const Eigen::VectorXd &rhs, Eigen::VectorXd &lambda,
                     const std::atomic<bool> *cancel) const;

  // Implicit (state) part of a total derivative for J(theta(rho)):
  //   grad_e += -weight * lambda_e^T (dK_e/drho_e) theta_e
  // where lambda solved solve_adjoint(dJ/dtheta). Valid for any Dirichlet
  // values: the lifted K_fp theta_p dependence is included because theta
  // carries the prescribed entries. grad must have E entries.
  void add_state_gradient(const Eigen::VectorXd &lambda, double weight,
                          Eigen::VectorXd &grad) const;

  // Responses of the last solve.
  // Thermal compliance q^T theta (W K) and its density gradient (E entries,
  // overwritten) when gradient != nullptr.
  double thermal_compliance(Eigen::VectorXd *gradient,
                            const std::atomic<bool> *cancel) const;
  // Heat input through flux/power patches, W (sign: + into the body).
  double heat_input_w() const { return heat_input_w_; }
  // Max-temperature constraint "max_temperature" (unit K): response = KS upper
  // bound, value = KS / limit_k - 1, gradient d value / d rho (if derivatives).
  Constraint max_temperature_constraint(double limit_k, bool derivatives,
                                        const std::atomic<bool> *cancel) const;
  // KS sharpness used for a given absolute limit (1/K).
  double max_temperature_beta(double limit_k) const;
  // Exact nodal peak temperature (K) over referenced nodes of the last solve.
  double peak_temperature_k(int *node = nullptr) const;
  int referenced_node_count() const { return referenced_nodes_; }

  // Element heat flux -k_e grad T (W/m^2), 3E entity-major.
  void heat_flux(Eigen::VectorXd &out) const;
  // Last-solve evidence: solver, residuals, factorization counts, temperature
  // range and the per-patch heat-flow balance (W, + into the body):
  // {flux:[{label,source,input_value,area_m2,power_w}],
  //  convection:[{label,h_w_m2k,ambient_k,area_m2,power_w}],
  //  fixtures:[{label,temperature_k,nodes,power_w}], other_fixed_w, net_w,
  //  gross_w, relative_imbalance}.
  nlohmann::json evidence() const;

private:
  struct Factor;
  void assemble(const std::atomic<bool> *cancel);
  void factor_solve(const Eigen::VectorXd &rhs, Eigen::VectorXd &x_free,
                    const char *what, double &relative_residual) const;
  void require_solved(const char *what) const;
  void element_matrix(int e, double conductivity,
                      Eigen::Matrix4d &ke) const; // k V G^T G

  std::shared_ptr<const Problem> problem_;
  const TetMesh &mesh_;
  double k_solid_ = 0;
  double reference_k_ = 0;
  DofReduction reduction_;
  ReducedPattern pattern_;
  Eigen::MatrixXi element_nodes_; // 4 x E

  // Constant design-independent parts (built once).
  Eigen::VectorXd conv_values_;  // convection contribution to K_ff values
  Eigen::VectorXd load_full_;    // flux + convection ambient loads, N
  Eigen::VectorXd compliance_q_; // flux/power loads only, N
  Eigen::VectorXd rhs_const_; // free: load_full_ restricted + convection lift
  std::vector<int> lift_elements_;    // elements with a nonzero prescribed node
  std::vector<int> fixed_elements_;   // elements touching any constrained node
  std::vector<int> fixed_conv_faces_; // convection faces touching a fixed node
  std::vector<int> conv_faces_;       // all convection faces (with repeats)
  std::vector<double> conv_face_h_;   // h per conv_faces_ entry
  std::vector<int> fixture_of_node_;  // N, fixture index or -1
  bool compliance_self_adjoint_ = false; // q_f == rhs_f for every density
  double heat_input_w_ = 0;
  int referenced_nodes_ = 0;

  // Per-solve reusable storage.
  Eigen::SparseMatrix<double> K_;
  std::unique_ptr<Factor> factor_;
  Eigen::VectorXd density_;    // E
  Eigen::VectorXd rhs_free_;   // nf
  Eigen::VectorXd theta_free_; // nf
  Eigen::VectorXd theta_;      // N
  mutable Eigen::VectorXd adjoint_rhs_free_, adjoint_free_, scratch_full_;
  mutable Eigen::VectorXd residual_free_, adjoint_full_;
  bool solved_ = false;
  long numeric_factorizations_ = 0;
  double relative_residual_ = 0;
  mutable double max_adjoint_residual_ = 0;
};

// Thermal-compliance Physics with the optional max-temperature constraint
// (settings.max_temperature_c). Requires physics thermal; refuses zero_load
// when no flux/power patch drives the objective. Returned evaluator owns a
// ThermalSystem; fields: temperature (K, node), heat_flux (W/m2, element, 3).
std::unique_ptr<Physics> make_thermal_physics(std::shared_ptr<const Problem>);

} // namespace topology
} // namespace sim
} // namespace chudware