// Topology optimization: tet4 linear elasticity, stress / displacement
// responses and the spectral (modal, linear buckling) evaluators built on it.
//
// Discretization. Constant-strain tet4, isotropic solid elasticity C (Pa, Voigt
// [xx,yy,zz,xy,yz,zx], engineering shear strains). Element stiffness
//   K_e(rho) = s(rho_e) V_e B_e^T C B_e,   s = Problem::interpolation.scale
// (SIMP/RAMP/level-set ersatz through the common Interpolation). Element
// matrices are recomputed from TetMesh::grad_n on demand (no per-element 12x12
// storage). Every support set (identical Dirichlet DOF list) owns ONE reduced
// sparse pattern and ONE Cholesky factor: symbolic analysis once, numeric
// refactorization per density, every load case of that set solved as one
// multi-column right-hand side. Partial-DOF fixtures and case-specific supports
// come from the prepared Dirichlet sets unchanged.
//
// Stress. Element stress of the solid microstructure sigma_s = C (B u - e0)
// (e0: optional element eigenstrain, e.g. thermal), relaxed by the qp-style
// factor eta(rho) = (sqrt(rho+eps) - sqrt(eps)) / (sqrt(1+eps) - sqrt(eps)),
// eps = min_density: sigma_vm,e = eta(rho_e) vm(sigma_s,e). eta(1) = 1, eta(0)
// = 0 and eta' stays finite. The stress constraint aggregates over every
// non-void element with a Kreisselmeier-Steinhauser upper bound of squared
// ratios:
//   r_e = sigma_vm,e / limit,  A = sqrt(KS_P(r_e^2)),  P = 40
//   max r <= A <= sqrt(max r^2 + ln(n)/P)          (conservative)
// Displacement uses the same aggregate over |u_n| / limit at referenced nodes.
// Raw maxima are reported beside every aggregate.
//
// Spectral. Consistent tet4 mass M_e = rho_m V/20 (1 + delta_ab) I3 with the
// Du-Olhoff mass interpolation m(rho) = eps + (1-eps) g(rho), g = rho for
// rho > 0.1 and 6e5 rho^6 - 5e6 rho^7 below (C1 at 0.1), which keeps
// low-density regions from producing spurious local modes. Linear buckling
// solves (K + lambda K_G(u)) phi = 0 with the static prestress u of each load
// case, K_G from the interpolated-material stress s(rho) C B u. Eigenpairs come
// from the shared Spectra ladder `fea_shift_invert`; completeness of the
// computed cluster set is verified by Sylvester inertia counts, never assumed.
// Repeated or clustered eigenvalues are aggregated with the symmetric
// soft-minimum
//   lambda_agg = (sum_j lambda_j^-P)^(-1/P),  P = 16,  lambda_agg <= lambda_1
// which is differentiable through exact multiplicities (no mode switching).
//
// All values SI. Instances are not thread-safe: one evaluation at a time.
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

// ── Memory admission
// ──────────────────────────────────────────────────────────────
// Projected peak of `factorizations` simultaneously held tet4 3-DOF sparse
// factors on this mesh (governor-calibrated fea_peak_bytes per factor) plus
// `dense_bytes` of dense workspace. Refuses ModelError "memory_budget" with the
// governor's explanation BEFORE any heavy allocation.
std::uint64_t elastic_memory_estimate(const Problem &, int factorizations,
                                      std::uint64_t dense_bytes);
void admit_elastic_memory(const Problem &, int factorizations,
                          std::uint64_t dense_bytes, const char *what);

// ── Sparse SPD factor
// ─────────────────────────────────────────────────────────────
// CHOLMOD supernodal LLT when compiled in, else SimplicialLDLT(AMD); a CHOLMOD
// refusal falls back to LDLT once. analyze() on the fixed pattern once;
// factorize() per numeric refill. Refuses "singular_system" when the matrix is
// not positive definite.
class SpdFactor {
public:
  SpdFactor();
  ~SpdFactor();
  SpdFactor(const SpdFactor &) = delete;
  SpdFactor &operator=(const SpdFactor &) = delete;

  void analyze(const Eigen::SparseMatrix<double> &A);
  void factorize(const Eigen::SparseMatrix<double> &A);
  // Column-wise solve; x may not alias b.
  void solve(const Eigen::Ref<const Eigen::MatrixXd> &b,
             Eigen::MatrixXd &x) const;
  bool factorized() const;
  const char *label() const;
  long numeric_factorizations() const;

private:
  struct Impl;
  std::unique_ptr<Impl> impl_;
};

// ── Material response helpers
// ─────────────────────────────────────────────────────
// Von Mises squared of a Voigt stress [sxx,syy,szz,sxy,syz,szx] and its
// gradient.
double von_mises_squared(const Eigen::Matrix<double, 6, 1> &stress);
Eigen::Matrix<double, 6, 1>
von_mises_squared_gradient(const Eigen::Matrix<double, 6, 1> &stress);

// Stress relaxation eta(rho) (see file header); floor = Interpolation::floor.
struct StressRelaxation {
  double floor = 1e-4;
  double value(double rho) const;
  double derivative(double rho) const;
};

// Du-Olhoff mass interpolation with property floor (see file header).
struct MassInterpolation {
  double floor = 1e-4;
  double value(double rho) const;
  double derivative(double rho) const;
};

inline constexpr double kElasticKsParameter = 40.0;   // KS on squared ratios
inline constexpr double kSpectralSoftMinPower = 16.0; // soft-min exponent P

// ── Elastic model (problem constants)
// ─────────────────────────────────────────────
// Requires a structural boundary and E/nu: physics structural, modal,
// buckling or thermomechanical; otherwise "physics_mismatch".
class ElasticModel {
public:
  explicit ElasticModel(std::shared_ptr<const Problem> problem);

  const Problem &problem() const { return *problem_; }
  const std::shared_ptr<const Problem> &problem_ptr() const { return problem_; }
  const TetMesh &mesh() const { return *problem_->mesh; }
  const Interpolation &interpolation() const { return problem_->interpolation; }
  const Eigen::Matrix<double, 6, 6> &elasticity() const { return c_; }
  const Eigen::MatrixXi &element_dofs() const { return dofs_; } // 12 x E
  int element_count() const { return problem_->mesh->element_count(); }
  int node_count() const { return problem_->mesh->node_count(); }
  int dof_count() const { return 3 * node_count(); }

  Eigen::Matrix<double, 6, 12> strain_matrix(int e) const;
  void gather(const Eigen::Ref<const Eigen::VectorXd> &full, int e,
              Eigen::Matrix<double, 12, 1> &ue) const;
  // Engineering strain B u_e of element e.
  Eigen::Matrix<double, 6, 1>
  strain(int e, const Eigen::Ref<const Eigen::VectorXd> &u_full) const;
  // Solid (s = 1) element stiffness V B^T C B.
  void solid_stiffness(int e, Eigen::Matrix<double, 12, 12> &ke) const;

  // grad_e += weight * s'(rho_e) * a_e^T K0_e b_e  (all E elements).
  void add_stiffness_contraction(const Eigen::VectorXd &rho,
                                 const Eigen::Ref<const Eigen::VectorXd> &a,
                                 const Eigen::Ref<const Eigen::VectorXd> &b,
                                 double weight, Eigen::VectorXd &grad) const;
  // out = sum_e s(rho_e) K0_e u_e over ALL full DOFs (constrained rows
  // included): internal force, for reactions and residuals.
  void internal_force(const Eigen::VectorXd &rho,
                      const Eigen::Ref<const Eigen::VectorXd> &u,
                      Eigen::VectorXd &out) const;

  // Eigenstrain load f = sum_e s(rho_e) V B^T C e0_e (added to `force`, full).
  void add_eigenstrain_force(const Eigen::VectorXd &rho,
                             const Eigen::Matrix<double, 6, Eigen::Dynamic> &e0,
                             Eigen::VectorXd &force) const;
  // Adjoint contraction of that load with lambda (full):
  //   grad_rho_e   += weight * s'(rho_e) V (B lambda_e)^T C e0_e
  //   grad_e0(:,e) += weight * s(rho_e)  V C B lambda_e
  // Either output may be null. Thermomechanics chains grad_e0 to temperature.
  void eigenstrain_force_adjoint(
      const Eigen::VectorXd &rho,
      const Eigen::Matrix<double, 6, Eigen::Dynamic> &e0,
      const Eigen::Ref<const Eigen::VectorXd> &lambda, double weight,
      Eigen::VectorXd *grad_rho,
      Eigen::Matrix<double, 6, Eigen::Dynamic> *grad_e0) const;

private:
  std::shared_ptr<const Problem> problem_;
  Eigen::Matrix<double, 6, 6> c_;
  Eigen::MatrixXi dofs_;
};

// ── One support set: reduced pattern + reusable factor
// ────────────────────────────
class ElasticSystem {
public:
  ElasticSystem(std::shared_ptr<const ElasticModel> model,
                const Dirichlet &fixed);
  ElasticSystem(const ElasticSystem &) = delete;
  ElasticSystem &operator=(const ElasticSystem &) = delete;

  const ElasticModel &model() const { return *model_; }
  const DofReduction &reduction() const { return pattern_.reduction(); }
  const ReducedPattern &pattern() const { return pattern_; }
  const Dirichlet &fixed() const { return fixed_; }

  // Refill K_ff = sum s(rho_e) K0_e in place (pattern fixed) and the lift of
  // nonzero prescribed values. check_density is the caller's job.
  void assemble(const Eigen::VectorXd &rho, const std::atomic<bool> *cancel);
  // Numeric factorization of the assembled K_ff (symbolic analysis once).
  void factorize();
  const Eigen::SparseMatrix<double> &stiffness() const { return K_; }
  const SpdFactor &factor() const { return factor_; }
  const Eigen::VectorXd &density() const { return rho_; }

  // Columns of full forces (3N) -> full displacements (prescribed values on
  // constrained DOFs). Residual-checked ("solve_inaccurate" above 1e-8).
  void solve(const Eigen::Ref<const Eigen::MatrixXd> &forces,
             Eigen::MatrixXd &displacements) const;
  // Columns of full dR/du (3N; constrained entries ignored) -> full adjoints,
  // zero on constrained DOFs: K_ff lambda_f = rhs_f. Residual-checked.
  void solve_adjoint(const Eigen::Ref<const Eigen::MatrixXd> &rhs,
                     Eigen::MatrixXd &adjoint) const;
  double max_relative_residual() const { return max_residual_; }

private:
  void solve_free(const Eigen::MatrixXd &rhs_free, Eigen::MatrixXd &x_free,
                  const char *what) const;

  std::shared_ptr<const ElasticModel> model_;
  Dirichlet fixed_;
  ReducedPattern pattern_;
  Eigen::SparseMatrix<double> K_;
  SpdFactor factor_;
  Eigen::VectorXd rho_;
  Eigen::VectorXd lift_free_; // -K_fc u_c, empty when all prescribed are zero
  std::vector<int> lift_elements_;
  bool analyzed_ = false;
  mutable double max_residual_ = 0;
  mutable Eigen::MatrixXd rhs_free_, x_free_, check_free_;
};

// ── Load cases grouped by support set
// ─────────────────────────────────────────────
struct ElasticCaseState {
  Eigen::VectorXd force;        // full 3N, N (as solved)
  Eigen::VectorXd displacement; // full 3N, m
  Eigen::VectorXd reaction; // full 3N: (K u - f) on constrained DOFs, else 0
  double compliance = 0;    // f.u (J); external work done by applied loads
  double strain_energy = 0; // 0.5 u^T K u (J)
  double residual =
      0; // ||(K u - f)_free|| / max(||f||, ||K u||), from elements
};

class ElasticCases {
public:
  // Groups Problem::structural->cases by identical fixed DOF lists (one
  // ElasticSystem each). Requires at least one case ("section_missing").
  explicit ElasticCases(std::shared_ptr<const ElasticModel> model);

  const ElasticModel &model() const { return *model_; }
  int case_count() const { return static_cast<int>(case_system_.size()); }
  int system_count() const { return static_cast<int>(systems_.size()); }
  ElasticSystem &system(int s) { return *systems_[s]; }
  const ElasticSystem &system(int s) const { return *systems_[s]; }
  int case_system(int c) const { return case_system_[c]; }
  const std::vector<int> &system_cases(int s) const { return system_cases_[s]; }
  // Prepared case loads, 3N x C.
  const Eigen::MatrixXd &reference_forces() const { return reference_forces_; }

  // Assemble + factor every support set at rho, then solve case c with load
  // column c of `forces` (3N x C). Fills states, reactions, residuals.
  void solve(const Eigen::VectorXd &rho, const Eigen::MatrixXd &forces,
             const std::atomic<bool> *cancel);
  const ElasticCaseState &state(int c) const { return states_[c]; }
  // Adjoint columns (3N x R); column r is solved with case rhs_case[r]'s
  // support set. One batched solve per support set.
  void solve_adjoint(const Eigen::MatrixXd &rhs,
                     const std::vector<int> &rhs_case,
                     Eigen::MatrixXd &adjoint) const;
  // {solver, support_sets, numeric_factorizations, max_solve_residual,
  //  cases:[{name, system, compliance_j, strain_energy_j, residual,
  //  applied_n[3], reaction_n[3], balance_n}]}
  nlohmann::json evidence() const;

private:
  std::shared_ptr<const ElasticModel> model_;
  std::vector<std::unique_ptr<ElasticSystem>> systems_;
  std::vector<int> case_system_;
  std::vector<std::vector<int>> system_cases_;
  Eigen::MatrixXd reference_forces_;
  std::vector<ElasticCaseState> states_;
  Eigen::VectorXd scratch_;
  mutable Eigen::MatrixXd group_in_, group_out_;
  Eigen::MatrixXd group_force_, group_u_;
};

// ── Responses
// ─────────────────────────────────────────────────────────────────────
struct StressResponse {
  Eigen::VectorXd von_mises;       // E, relaxed eta(rho) vm (Pa)
  Eigen::VectorXd solid_von_mises; // E, vm of C (B u - e0) (Pa)
  double value = 0;       // aggregate A * limit (Pa), >= max relaxed stress
  double upper_bound = 0; // limit * sqrt(max r^2 + ln(n)/P) (Pa)
  double max_value = 0;   // exact max relaxed stress over the set (Pa)
  int max_element = -1;
  int count = 0; // elements aggregated (role != Void)
  // Derivatives of `value` (Pa per unit) when requested:
  Eigen::VectorXd d_displacement; // 3N
  Eigen::VectorXd d_density;      // E, explicit (u held fixed)
  Eigen::Matrix<double, 6, Eigen::Dynamic> d_eigenstrain; // 6 x E if e0 given
};
// Stress aggregate for one displacement state. e0 optional (6 x E).
StressResponse
stress_response(const ElasticModel &, const Eigen::VectorXd &rho,
                const Eigen::Ref<const Eigen::VectorXd> &u,
                const Eigen::Matrix<double, 6, Eigen::Dynamic> *e0,
                double limit_pa, bool derivatives);
std::string stress_aggregation_label(int count);

struct DisplacementResponse {
  double value = 0;       // limit * sqrt(KS_P(|u_n|^2/limit^2)) (m)
  double upper_bound = 0; // limit * sqrt(max r^2 + ln(n)/P) (m)
  double max_value = 0;   // exact max nodal |u| over referenced nodes (m)
  int max_node = -1;
  int count = 0;
  Eigen::VectorXd d_displacement; // 3N, d value / d u
};
DisplacementResponse
displacement_response(const ElasticModel &,
                      const Eigen::Ref<const Eigen::VectorXd> &u,
                      double limit_m, bool derivatives);
std::string displacement_aggregation_label(int count);

// ── Spectral operators on a support set's pattern
// ─────────────────────────────────
// M_ff(rho) (kg), M must carry system.pattern().pattern()'s structure.
void assemble_mass(const ElasticSystem &, const Eigen::VectorXd &rho,
                   double density_kg_m3, Eigen::SparseMatrix<double> &M);
// K_G,ff(rho, u) (N/m) with element stress s(rho_e) C B u_e; same structure.
void assemble_geometric_stiffness(const ElasticSystem &,
                                  const Eigen::VectorXd &rho,
                                  const Eigen::Ref<const Eigen::VectorXd> &u,
                                  Eigen::SparseMatrix<double> &KG);

// Weighted structural compliance (objective compliance) or mass (objective
// mass, needs material density) with optional stress / displacement
// constraints per load case ("max_von_mises[@case]", "max_displacement[@case]",
// cases named only when there are several). Gradients: compliance
// self-adjoint, constraints by one batched adjoint solve per support set.
// Fields: displacement, von_mises (relaxed), strain_energy_density, per case.
// Refuses physics_mismatch (not structural), material_missing,
// memory_budget, singular_system.
std::unique_ptr<Physics>
    make_structural_physics(std::shared_ptr<const Problem>);

// Modal (objective frequency, optional min_frequency_hz) or linear buckling
// (objective buckling_factor, optional min_buckling_factor) evaluator.
// Modal: objective = sqrt(lambda_agg)/(2 pi) Hz over the mass-normalized
// fixed-support spectrum. Buckling: objective = lambda_agg over every load
// case's smallest positive load factors (multipliers of that case's
// reference load); case weights must be 1 (factor is a minimum over cases).
// Refuses physics_mismatch, material_missing, memory_budget, singular_system,
// eigen_solve_failed, eigen_incomplete, no_compressive_buckling (tension or
// no positive load factor), invalid_bc.
std::unique_ptr<Physics> make_spectral_physics(std::shared_ptr<const Problem>);

} // namespace topology
} // namespace sim
} // namespace chudware
