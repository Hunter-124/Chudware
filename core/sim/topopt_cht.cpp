// Topology optimization: conjugate heat transfer (CHT) on the shared
// Stokes-Brinkman flow state.
//
// Energy equation (steady, advective form, theta = T - T_ref):
//   C u . grad T - div(k(rho) grad T) = 0 in the domain,
//   T = T_inlet on inlet nodes and T = T_fix on thermal fixtures (Dirichlet),
//   -k dT/dn = -q on flux/power patches (inward q), -k dT/dn = h (T - T_amb)
//   on convection patches, zero diffusive flux on every other boundary
//   (walls and outlets: heat leaves the outlet by advection only).
// u is the incompressible superficial Stokes-Brinkman velocity of the shared
// FlowSystem: the full MINI velocity (P1 + condensed bubble), div u = 0 in
// the weak P1 sense everywhere, porous (nominally solid) elements included.
// C = rho_f c_p,f is the CONSTANT fluid volumetric heat capacity. With
// div u = 0, C u . grad T = div(C u T) only for uniform C; a density-
// dependent capacity would drop T div(C u) and create or destroy enthalpy
// wherever leakage flow crosses a capacity jump. Because the temperature is
// P1, i.e. lies in the pressure space, the discrete continuity equation gives
// int theta div u = 0 exactly, so the advected heat equals the port enthalpy
// flux C int theta u . n (the bubble vanishes on the boundary, so port fluxes
// use the nodal velocity) and the global balance
//   net port enthalpy outflow = flux + power + convection + Dirichlet input
// holds to solver accuracy for any density field and any T_ref.
// Density dependence enters through the conductivity and the solved velocity:
//   k(rho) = k_f + (k_s - k_f) f(rho),  f = SIMP/RAMP shape, rho = 1 solid,
// and alpha(rho) in the flow. Brinkman leakage through nominal solid
// therefore carries heat at the fluid capacity; that heat transport is
// reported (thermal evidence heat_leakage), not suppressed.
// P1 temperature, Galerkin plus consistent SUPG: per element the test
// function gains tau C (ubar . grad w), ubar = element mean of the full
// velocity; because the P1 diffusion term has zero element divergence and the
// boundary sources are not part of the strong residual, the added term is
// tau C^2 V (ubar . grad N_i)(ubar . grad T) exactly (consistent). Its test
// functions sum to zero, so SUPG moves heat but never changes the balance.
//   tau = ((2 C |ubar| / h)^2 + (12 k / h^2)^2)^(-1/2),  h = equal-volume
//   regular-tet edge (TetMesh::element_size_m). The streamline conductivity it
//   adds, tau C^2 |ubar|^2, is reported against k per element.
// Responses: volume-mean temperature (K, objective) and a KS upper bound of
// the nodal peak temperature (beta = 100 / max(|T_limit - T_ref|, 1 K)).
// Gradients: coupled discrete adjoint. K_T^T mu = dJ/dtheta; the flow adjoint
// right-hand side is -(dR_T/dx)^T mu (Galerkin advection and SUPG, including
// d tau / d ubar and the bubble's dependence on velocity and pressure);
// dJ/drho = -mu^T dK_T/drho theta - lambda^T dA/drho x, with dK_T/drho
// through k, tau(k) and the bubble's dependence on alpha(rho).
#include "sim/topopt_flow.h"

#include "util/resource_governor.h"

#include <Eigen/OrderingMethods>
#include <Eigen/SparseLU>

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <limits>
#include <sstream>

namespace chudware {
namespace sim {
namespace topology {

namespace {

[[noreturn]] void fail(const std::string &code, const std::string &message) {
  throw ModelError(code, message);
}

std::string fmt(double v) {
  std::ostringstream s;
  s.precision(6);
  s << v;
  return s.str();
}

void throw_if_cancelled(const std::atomic<bool> *cancel) {
  if (cancel && cancel->load(std::memory_order_relaxed))
    throw EvaluationCancelled();
}

constexpr double kMaxRelativeResidual = 1e-8;
constexpr double kSupgDiffusionConstant = 12.0;

std::uint64_t clamp_bytes(long double b) {
  if (!(b >= 0))
    return 0;
  if (b >= static_cast<long double>(std::numeric_limits<std::uint64_t>::max()))
    return std::numeric_limits<std::uint64_t>::max();
  return static_cast<std::uint64_t>(std::ceil(b));
}

std::shared_ptr<const Problem>
require_cht(const std::shared_ptr<const Problem> &p) {
  if (!p)
    fail("invalid_input", "cht needs a prepared problem");
  if (p->settings.physics != PhysicsKind::Cht)
    fail("physics_mismatch", std::string("the CHT evaluator serves cht, not ") +
                                 to_string(p->settings.physics));
  if (p->settings.objective != ObjectiveKind::MeanTemperature)
    fail("objective_not_supported",
         std::string("cht evaluates mean_temperature, not ") +
             to_string(p->settings.objective));
  if (!p->thermal)
    fail("section_missing", "cht needs a thermal boundary");
  if (!p->flow)
    fail("section_missing", "cht needs a flow boundary");
  if (!p->material.conductivity_w_mk || !(*p->material.conductivity_w_mk > 0))
    fail("material_missing", "cht needs material.k_w_mk > 0");
  if (!p->flow->fluid_conductivity_w_mk ||
      !(*p->flow->fluid_conductivity_w_mk > 0))
    fail("section_missing", "cht needs flow.fluid_k_w_mk > 0");
  if (!p->flow->fluid_specific_heat_j_kgk ||
      !(*p->flow->fluid_specific_heat_j_kgk > 0))
    fail("section_missing", "cht needs flow.fluid_cp_j_kgk > 0");
  return p;
}

class ChtPhysics final : public Physics {
public:
  explicit ChtPhysics(std::shared_ptr<const Problem> problem);
  ~ChtPhysics() override;

  PhysicsKind kind() const override { return PhysicsKind::Cht; }
  std::string formulation() const override;
  Evaluation evaluate(const Eigen::VectorXd &density, bool derivatives,
                      const std::atomic<bool> *cancel) override;

private:
  struct Factor {
    Eigen::SparseLU<Eigen::SparseMatrix<double>, Eigen::COLAMDOrdering<int>> lu;
    bool analyzed = false;
  };
  // omega = tau C^2 with the constant fluid capacity C; tau depends on k(rho)
  // and ubar only.
  struct Supg {
    double tau, omega, domega_dk, domega_dubar_scale;
  };

  void gather_nodal(const Eigen::VectorXd &v, int e,
                    Eigen::Vector4d &out) const;
  Supg supg(int e) const;
  void element_matrix(int e, Eigen::Matrix4d &ke) const;
  void solve_thermal(const std::atomic<bool> *cancel);
  void solve_thermal_adjoint(const Eigen::VectorXd &rhs, Eigen::VectorXd &mu,
                             const std::atomic<bool> *cancel) const;
  void response_gradient(const Eigen::VectorXd &j_theta, double weight,
                         Eigen::VectorXd &grad,
                         const std::atomic<bool> *cancel) const;
  double ks_beta(double limit_k) const;
  Constraint max_temperature_constraint(double limit_k, bool derivatives,
                                        const std::atomic<bool> *cancel) const;
  nlohmann::json thermal_formulation() const;
  nlohmann::json thermal_evidence() const;

  std::shared_ptr<const Problem> problem_;
  FlowSystem flow_;
  const TetMesh &mesh_;
  const ThermalBoundary &thermal_;
  Interpolation interp_;
  int N_ = 0;
  // capacity_fluid_ = rho_f c_p,f, the constant advective capacity.
  double k_solid_ = 0, k_fluid_ = 0, capacity_fluid_ = 0, reference_k_ = 0;
  DofReduction reduction_;
  ReducedPattern pattern_;
  std::vector<int> conv_index_; // E, index into conv_matrix_ or -1
  std::vector<Eigen::Matrix4d, Eigen::aligned_allocator<Eigen::Matrix4d>>
      conv_matrix_;
  Eigen::VectorXd load_; // N: flux q A/3 + convection h theta_amb A/3
  std::vector<std::uint8_t> lift_flag_;
  Eigen::VectorXd mean_weight_; // N, objective weights (sum 1)
  std::uint64_t forecast_bytes_ = 0;

  // Per-solve reusable storage.
  Eigen::SparseMatrix<double> K_;
  std::unique_ptr<Factor> factor_;
  Eigen::VectorXd k_e_, dk_e_;
  Eigen::Matrix3Xd ubar_;
  Eigen::VectorXd rhs_free_, theta_free_, scratch_free_, theta_;
  mutable Eigen::VectorXd adj_rhs_free_, adj_free_, adj_scratch_;
  mutable Eigen::VectorXd mu_full_, j_theta_, flow_rhs_, flow_lambda_;
  bool solved_ = false;
  long numeric_factorizations_ = 0;
  double relative_residual_ = 0;
  mutable double max_adjoint_residual_ = 0;
};

ChtPhysics::ChtPhysics(std::shared_ptr<const Problem> problem)
    : problem_(require_cht(problem)), flow_(std::move(problem)),
      mesh_(*problem_->mesh), thermal_(*problem_->thermal) {
  const TetMesh &m = mesh_;
  const FlowBoundary &fb = *problem_->flow;
  N_ = m.node_count();
  const int E = m.element_count();
  interp_ = problem_->interpolation;
  k_solid_ = *problem_->material.conductivity_w_mk;
  k_fluid_ = *fb.fluid_conductivity_w_mk;
  capacity_fluid_ = fb.density_kg_m3 * *fb.fluid_specific_heat_j_kgk;
  reference_k_ = thermal_.reference_temperature_k;

  Dirichlet fixed = thermal_.fixed;
  fixed.values.array() -= reference_k_;
  reduction_ = make_dof_reduction(N_, fixed);

  const long double Ed = E;
  const long double nf = reduction_.free_size();
  const long double forecast = 4.0L * 16.0L * Ed + 16.0L * 16.0L * Ed +
                               12.0L * 16.0L * Ed +
                               2.0L * 12.0L * FlowSystem::kFactorFillConstant *
                                   std::pow(nf, 4.0L / 3.0L) +
                               8.0L * 24.0L * N_ + 8.0L * 8.0L * Ed;
  forecast_bytes_ = clamp_bytes(forecast);
  {
    std::string why;
    const std::uint64_t total =
        forecast_bytes_ > std::numeric_limits<std::uint64_t>::max() -
                              flow_.forecast_bytes()
            ? std::numeric_limits<std::uint64_t>::max()
            : forecast_bytes_ + flow_.forecast_bytes();
    if (!chudware::governor::admit(total, "topology CHT flow + energy systems",
                                   why))
      fail("memory_budget", why);
  }

  pattern_ = ReducedPattern(nodal_dof_map(m, 1, 0), reduction_);
  K_ = pattern_.pattern();

  conv_index_.assign(E, -1);
  load_.setZero(N_);
  for (const ThermalConvection &c : thermal_.convections) {
    const double theta_amb = c.ambient_k - reference_k_;
    for (int f : c.patch.faces) {
      const int e = m.surface_element[f];
      const int lf = m.surface_local_face[f];
      if (conv_index_[e] < 0) {
        conv_index_[e] = static_cast<int>(conv_matrix_.size());
        conv_matrix_.push_back(Eigen::Matrix4d::Zero());
      }
      Eigen::Matrix4d &M = conv_matrix_[conv_index_[e]];
      const double A = m.surface_area_m2[f];
      for (int i = 0; i < 3; ++i)
        for (int j = 0; j < 3; ++j)
          M(kTetFaces[lf][i], kTetFaces[lf][j]) +=
              c.h_w_m2k * A / 12.0 * (i == j ? 2.0 : 1.0);
      for (int k = 0; k < 3; ++k)
        load_[m.surface_nodes(k, f)] += c.h_w_m2k * theta_amb * A / 3.0;
    }
  }
  for (const ThermalFlux &fx : thermal_.fluxes)
    for (int f : fx.patch.faces)
      for (int k = 0; k < 3; ++k)
        load_[m.surface_nodes(k, f)] +=
            fx.flux_w_m2 * m.surface_area_m2[f] / 3.0;

  lift_flag_.assign(E, 0);
  mean_weight_.setZero(N_);
  for (int e = 0; e < E; ++e)
    for (int k = 0; k < 4; ++k) {
      const int n = m.tets(k, e);
      mean_weight_[n] += m.volume_m3[e] / (4.0 * m.total_volume_m3);
      if (reduction_.full_to_free[n] < 0 && reduction_.prescribed[n] != 0.0)
        lift_flag_[e] = 1;
    }

  factor_ = std::make_unique<Factor>();
  k_e_.resize(E);
  dk_e_.resize(E);
  ubar_.resize(3, E);
  theta_.setZero(N_);
}

ChtPhysics::~ChtPhysics() = default;

std::string ChtPhysics::formulation() const {
  return std::string("tet4 MINI Stokes-Brinkman creeping flow + P1 SUPG "
                     "advection-diffusion, ") +
         (interp_.kind == Interpolation::Kind::Ramp ? "RAMP" : "SIMP") +
         " resistance/conductivity, constant fluid heat capacity on the "
         "superficial velocity, mean temperature, coupled "
         "flow-energy adjoint";
}

void ChtPhysics::gather_nodal(const Eigen::VectorXd &v, int e,
                              Eigen::Vector4d &out) const {
  for (int k = 0; k < 4; ++k)
    out[k] = v[mesh_.tets(k, e)];
}

// tau = (a^2 + b^2)^(-1/2), a = 2 C |ubar| / h, b = 12 k / h^2, omega = tau
// C^2. domega_dubar = domega_dubar_scale * ubar.
ChtPhysics::Supg ChtPhysics::supg(int e) const {
  const double h = mesh_.element_size_m[e];
  const double C = capacity_fluid_, k = k_e_[e];
  const double u2 = ubar_.col(e).squaredNorm();
  const double a2 = 4.0 * C * C * u2 / (h * h);
  const double b = kSupgDiffusionConstant * k / (h * h);
  const double tau = 1.0 / std::sqrt(a2 + b * b);
  const double tau3 = tau * tau * tau;
  Supg s;
  s.tau = tau;
  s.omega = tau * C * C;
  const double dtau_dk = -tau3 * b * kSupgDiffusionConstant / (h * h);
  s.domega_dk = C * C * dtau_dk;
  s.domega_dubar_scale = -C * C * tau3 * 4.0 * C * C / (h * h);
  return s;
}

// Galerkin advection with the full MINI velocity u = sum N_a u_a + b u_b:
//   int N_i C u . grad N_j = C ((V/20)(sum_a u_a + u_i) + int(b N_i) u_b)
//                            . grad N_j,
// plus SUPG with ubar (mean of the full velocity) and diffusion k V G^T G.
void ChtPhysics::element_matrix(int e, Eigen::Matrix4d &ke) const {
  const Eigen::Matrix<double, 3, 4> G = mesh_.element_gradients(e);
  const double V = mesh_.volume_m3[e];
  const double C = capacity_fluid_;
  FlowElementVelocity v;
  flow_.element_velocity(e, v);
  const Eigen::Vector3d U = v.nodal.rowwise().sum();
  const Eigen::Vector3d bubble = FlowSystem::kBubbleLinear * V * v.bubble;
  ke.noalias() = k_e_[e] * V * (G.transpose() * G);
  for (int i = 0; i < 4; ++i) {
    const Eigen::Vector3d w = C * (V / 20.0 * (U + v.nodal.col(i)) + bubble);
    for (int j = 0; j < 4; ++j)
      ke(i, j) += w.dot(G.col(j));
  }
  const Eigen::Vector4d s = G.transpose() * ubar_.col(e);
  ke.noalias() += supg(e).omega * V * (s * s.transpose());
  if (conv_index_[e] >= 0)
    ke += conv_matrix_[conv_index_[e]];
}

void ChtPhysics::solve_thermal(const std::atomic<bool> *cancel) {
  solved_ = false;
  const int E = mesh_.element_count();
  const Eigen::VectorXd &rho = flow_.density();
  FlowElementVelocity v;
  for (int e = 0; e < E; ++e) {
    const double f = interp_.shape(rho[e]);
    const double fp = interp_.shape_derivative(rho[e]);
    k_e_[e] = k_fluid_ + (k_solid_ - k_fluid_) * f;
    dk_e_[e] = (k_solid_ - k_fluid_) * fp;
    flow_.element_velocity(e, v);
    ubar_.col(e) = v.mean;
  }
  ReducedPattern::zero(K_);
  reduction_.restrict_vector(load_, rhs_free_);
  Eigen::Matrix4d ke;
  for (int e = 0; e < E; ++e) {
    if ((e & 4095) == 0)
      throw_if_cancelled(cancel);
    element_matrix(e, ke);
    pattern_.add(K_, e, ke);
    if (lift_flag_[e])
      pattern_.lift(rhs_free_, e, ke);
  }
  throw_if_cancelled(cancel);
  if (chudware::governor::low_memory_now())
    fail("memory_low",
         "host memory is critically low before the CHT energy factorization");
  if (!factor_->analyzed) {
    factor_->lu.analyzePattern(K_);
    factor_->analyzed = true;
  }
  factor_->lu.factorize(K_);
  if (factor_->lu.info() != Eigen::Success)
    fail("singular_system",
         "CHT energy factorization failed: " + factor_->lu.lastErrorMessage());
  ++numeric_factorizations_;
  throw_if_cancelled(cancel);
  theta_free_ = factor_->lu.solve(rhs_free_);
  if (!theta_free_.allFinite())
    fail("singular_system", "CHT energy solve produced non-finite values");
  scratch_free_.noalias() = K_ * theta_free_;
  scratch_free_ -= rhs_free_;
  const double bn = rhs_free_.norm();
  relative_residual_ =
      bn > 0 ? scratch_free_.norm() / bn : scratch_free_.norm();
  if (!(relative_residual_ <= kMaxRelativeResidual))
    fail("solve_inaccurate", "CHT energy relative residual " +
                                 fmt(relative_residual_) + " exceeds " +
                                 fmt(kMaxRelativeResidual));
  reduction_.expand_vector(theta_free_, theta_);
  solved_ = true;
}

void ChtPhysics::solve_thermal_adjoint(const Eigen::VectorXd &rhs,
                                       Eigen::VectorXd &mu,
                                       const std::atomic<bool> *cancel) const {
  throw_if_cancelled(cancel);
  reduction_.restrict_vector(rhs, adj_rhs_free_);
  mu.setZero(N_);
  const double bn = adj_rhs_free_.norm();
  if (bn == 0.0)
    return;
  adj_free_ = factor_->lu.transpose().solve(adj_rhs_free_);
  if (!adj_free_.allFinite())
    fail("singular_system", "CHT energy adjoint produced non-finite values");
  adj_scratch_.noalias() = K_.transpose() * adj_free_;
  adj_scratch_ -= adj_rhs_free_;
  const double rel = adj_scratch_.norm() / bn;
  max_adjoint_residual_ = std::max(max_adjoint_residual_, rel);
  if (!(rel <= kMaxRelativeResidual))
    fail("solve_inaccurate", "CHT energy adjoint relative residual " +
                                 fmt(rel) + " exceeds " +
                                 fmt(kMaxRelativeResidual));
  for (int i = 0; i < reduction_.free_size(); ++i)
    mu[reduction_.free_to_full[i]] = adj_free_[i];
}

// grad += weight * dJ/drho for J(theta) with dJ/dtheta = j_theta (no explicit
// density or flow-state dependence of J).
void ChtPhysics::response_gradient(const Eigen::VectorXd &j_theta,
                                   double weight, Eigen::VectorXd &grad,
                                   const std::atomic<bool> *cancel) const {
  solve_thermal_adjoint(j_theta, mu_full_, cancel);
  const int E = mesh_.element_count();
  const Eigen::VectorXd &rho = flow_.density();
  const int p0 = flow_.pressure_offset();
  flow_rhs_.setZero(4 * N_);
  FlowElementVelocity v;
  Eigen::Vector4d mu, th;
  for (int e = 0; e < E; ++e) {
    if ((e & 4095) == 0)
      throw_if_cancelled(cancel);
    gather_nodal(mu_full_, e, mu);
    if (mu.isZero(0.0))
      continue;
    gather_nodal(theta_, e, th);
    flow_.element_velocity(e, v);
    const Eigen::Matrix<double, 3, 4> G = mesh_.element_gradients(e);
    const double V = mesh_.volume_m3[e];
    const double C = capacity_fluid_;
    const double mb = FlowSystem::kBubbleLinear * V;
    const Eigen::Vector3d ub = ubar_.col(e);
    const Eigen::Vector3d gt = G * th, gm = G * mu;
    const double sum_mu = mu.sum();
    const double ug = ub.dot(gm), ut = ub.dot(gt);
    const Supg s = supg(e);

    // P = mu^T K_e theta. Explicit density part through k and tau(k); the
    // advective capacity is the constant fluid value, so advection depends on
    // density only through the flow (state below, bubble alpha term next).
    double dP =
        dk_e_[e] * V * gm.dot(gt) + s.domega_dk * dk_e_[e] * V * ug * ut;
    // dP/d ubar (SUPG, including d tau / d ubar) and dP/d u_b.
    const Eigen::Vector3d dP_dubar = V * (s.domega_dubar_scale * ug * ut * ub +
                                          s.omega * (gm * ut + gt * ug));
    const Eigen::Vector3d dP_dub =
        C * mb * sum_mu * gt + FlowSystem::kBubbleMean * dP_dubar;
    // The condensed bubble depends on alpha(rho_e) at fixed flow state.
    dP += dP_dub.dot(v.bubble_dalpha) * flow_.alpha_derivative(rho[e]);
    grad[e] -= weight * dP;

    // Flow-state part: nodal velocities (directly and through u_b) and
    // pressures (through u_b).
    for (int a = 0; a < 4; ++a) {
      const Eigen::Vector3d dv = C * V / 20.0 * (sum_mu + mu[a]) * gt +
                                 0.25 * dP_dubar + v.bubble_du * dP_dub;
      flow_rhs_.segment<3>(3 * mesh_.tets(a, e)) -= weight * dv;
      flow_rhs_[p0 + mesh_.tets(a, e)] -=
          weight * dP_dub.dot(v.bubble_dp.col(a));
    }
  }
  flow_.solve_adjoint(flow_rhs_, flow_lambda_, cancel);
  flow_.add_state_gradient(flow_lambda_, 1.0, grad);
}

double ChtPhysics::ks_beta(double limit_k) const {
  return 100.0 / std::max(std::abs(limit_k - reference_k_), 1.0);
}

Constraint
ChtPhysics::max_temperature_constraint(double limit_k, bool derivatives,
                                       const std::atomic<bool> *cancel) const {
  if (!(std::isfinite(limit_k) && limit_k > 0))
    fail("invalid_input", "temperature limit must be finite and > 0 K");
  const double beta = ks_beta(limit_k);
  double peak = -std::numeric_limits<double>::infinity();
  for (int n = 0; n < N_; ++n)
    if (mesh_.node_referenced[n])
      peak = std::max(peak, theta_[n]);
  double sum = 0;
  for (int n = 0; n < N_; ++n)
    if (mesh_.node_referenced[n])
      sum += std::exp(beta * (theta_[n] - peak));
  Constraint c;
  c.name = "max_temperature";
  c.unit = "K";
  c.aggregation = "ks(beta=" + fmt(beta) + "/K)";
  c.limit = limit_k;
  c.response = reference_k_ + peak + std::log(sum) / beta;
  c.value = c.response / limit_k - 1.0;
  if (derivatives) {
    j_theta_.setZero(N_);
    for (int n = 0; n < N_; ++n)
      if (mesh_.node_referenced[n])
        j_theta_[n] = std::exp(beta * (theta_[n] - peak)) / sum;
    c.gradient.setZero(mesh_.element_count());
    response_gradient(j_theta_, 1.0 / limit_k, c.gradient, cancel);
  }
  return c;
}

nlohmann::json ChtPhysics::thermal_formulation() const {
  return {
      {"energy_equation",
       "rho_f c_p,f u . grad T - div(k(rho) grad T) = 0 (steady, advective "
       "form with div u = 0), P1 temperature, unknown theta = T - T_ref"},
      {"conductivity", "k(rho) = k_f + (k_s - k_f) f(rho)"},
      {"capacity",
       "C = rho_f c_p,f constant everywhere (not interpolated): u is the "
       "incompressible superficial Stokes-Brinkman velocity, so only a "
       "uniform capacity keeps C u . grad T = div(C u T) and the steady "
       "energy balance conservative; density acts through k(rho) and the "
       "solved velocity"},
      {"heat_leakage",
       "Brinkman leakage flow through nominally solid elements advects heat "
       "at the fluid capacity; reported in evidence thermal.heat_leakage "
       "alongside flow.leakage"},
      {"advection_velocity",
       "full MINI velocity (P1 + condensed bubble); int theta div u = 0 for "
       "P1 theta, so advected heat equals the port enthalpy flux "
       "rho_f c_p,f int theta u . n dA exactly"},
      {"energy_balance",
       "net port enthalpy outflow = flux/power + convection + Dirichlet "
       "(inlet and fixture) heat input; invariant to T_ref"},
      {"stabilization",
       {{"method", "SUPG (consistent residual-based streamline upwinding)"},
        {"term", "tau C^2 V (ubar . grad N_i)(ubar . grad N_j)"},
        {"tau", "((2 C |ubar| / h)^2 + (12 k / h^2)^2)^(-1/2)"},
        {"ubar", "element mean of the full MINI velocity"},
        {"h", "equal-volume regular-tet edge"},
        {"streamline_conductivity", "tau C^2 |ubar|^2 (W/mK)"}}},
      {"boundary",
       "Dirichlet inlet and fixture temperatures; Neumann flux/power; Robin "
       "convection h (T_amb - T); zero diffusive flux on walls and outlets"},
      {"interpolation",
       {{"kind", interp_.kind == Interpolation::Kind::Ramp ? "ramp" : "simp"},
        {"exponent", interp_.exponent}}},
      {"k_solid_w_mk", k_solid_},
      {"k_fluid_w_mk", k_fluid_},
      {"fluid_volumetric_heat_capacity_j_m3k", capacity_fluid_},
      {"reference_temperature_k", reference_k_},
      {"objective", "volume-mean temperature over the whole domain (K)"},
      {"max_temperature",
       "KS upper bound over referenced nodes, beta = 100 / max(|T_limit - "
       "T_ref|, 1 K); KS - T_peak <= ln(n) / beta"},
      {"gradient",
       "coupled discrete adjoint: K_T^T mu = dJ/dtheta, flow adjoint "
       "A lambda = -(dR_T/dx)^T mu including d tau / d ubar, dJ/drho = "
       "-mu^T dK_T/drho theta - lambda^T dA/drho x"},
  };
}

nlohmann::json ChtPhysics::thermal_evidence() const {
  const TetMesh &m = mesh_;
  const int E = m.element_count();
  const Eigen::VectorXd &rho = flow_.density();
  const double threshold = problem_->settings.density_threshold;
  const double C = capacity_fluid_;
  Eigen::VectorXd r = -load_;
  Eigen::VectorXd supg_nodal = Eigen::VectorXd::Zero(N_);
  double advected = 0, peclet_max = 0, ratio_max = 0, ratio_volume = 0;
  // Heat advected inside nominally solid (rho >= threshold) and fluid
  // elements: int_e C u . grad theta = C V ubar . grad theta (exact for the
  // full MINI velocity), positive where the flow picks heat up.
  double solid_advected = 0, solid_advected_abs = 0, fluid_advected_abs = 0;
  double solid_volume = 0, solid_peclet_max = 0;
  Eigen::Matrix4d ke;
  Eigen::Vector4d th;
  for (int e = 0; e < E; ++e) {
    element_matrix(e, ke);
    gather_nodal(theta_, e, th);
    const Eigen::Vector4d ke_th = ke * th;
    const Eigen::Matrix<double, 3, 4> G = m.element_gradients(e);
    const double V = m.volume_m3[e];
    const Eigen::Vector3d ub = ubar_.col(e);
    const Eigen::Vector3d gt = G * th;
    const Supg s = supg(e);
    const Eigen::Vector4d sg = G.transpose() * ub;
    const Eigen::Vector4d supg_th = s.omega * V * sg * ub.dot(gt);
    for (int k = 0; k < 4; ++k) {
      r[m.tets(k, e)] += ke_th[k];
      supg_nodal[m.tets(k, e)] += supg_th[k];
    }
    const double adv_e = C * V * ub.dot(gt);
    advected += adv_e;
    const double speed = ub.norm();
    const double peclet = C * speed * m.element_size_m[e] / (2.0 * k_e_[e]);
    peclet_max = std::max(peclet_max, peclet);
    if (rho[e] >= threshold) {
      solid_advected += adv_e;
      solid_advected_abs += std::abs(adv_e);
      solid_volume += V;
      solid_peclet_max = std::max(solid_peclet_max, peclet);
    } else {
      fluid_advected_abs += std::abs(adv_e);
    }
    const double ratio = s.omega * speed * speed / k_e_[e];
    ratio_max = std::max(ratio_max, ratio);
    ratio_volume += V * ratio;
  }
  double flux_in = 0;
  nlohmann::json fluxes = nlohmann::json::array();
  for (const ThermalFlux &fx : thermal_.fluxes) {
    const double w = fx.flux_w_m2 * fx.patch.area_m2;
    flux_in += w;
    fluxes.push_back({{"label", fx.patch.label}, {"power_w", w}});
  }
  double conv_in = 0;
  nlohmann::json convections = nlohmann::json::array();
  for (const ThermalConvection &c : thermal_.convections) {
    double w = 0;
    const double theta_amb = c.ambient_k - reference_k_;
    for (int f : c.patch.faces) {
      double ts = 0;
      for (int k = 0; k < 3; ++k)
        ts += theta_[m.surface_nodes(k, f)];
      w += c.h_w_m2k * m.surface_area_m2[f] * (theta_amb - ts / 3.0);
    }
    conv_in += w;
    convections.push_back({{"label", c.patch.label}, {"power_w", w}});
  }
  double dirichlet_in = 0, free_res2 = 0;
  for (int n = 0; n < N_; ++n) {
    if (reduction_.full_to_free[n] < 0)
      dirichlet_in += r[n];
    else
      free_res2 += r[n] * r[n];
  }

  // Port enthalpy C (u . n) theta with the same constant capacity as the
  // advection operator, and mixing-cup temperatures.
  const Eigen::VectorXd &x = flow_.state();
  auto port = [&](const SurfacePatch &patch, double &enthalpy,
                  double &volume_flux, double &flux_theta) {
    for (int f : patch.faces) {
      const Eigen::Vector3d n = m.surface_normal.col(f);
      const double A = m.surface_area_m2[f];
      double wn_sum = 0, th_sum = 0, wth = 0;
      for (int k = 0; k < 3; ++k) {
        const int node = m.surface_nodes(k, f);
        const double wn = x.segment<3>(3 * node).dot(n);
        wn_sum += wn;
        th_sum += theta_[node];
        wth += wn * theta_[node];
      }
      const double un_theta = A / 12.0 * (wth + wn_sum * th_sum);
      enthalpy += C * un_theta;
      volume_flux += A / 3.0 * wn_sum;
      flux_theta += un_theta;
    }
  };
  const FlowBoundary &fb = *problem_->flow;
  double h_in = 0, q_in = 0, qt_in = 0, h_out = 0, q_out = 0, qt_out = 0;
  nlohmann::json inlets = nlohmann::json::array();
  for (const FlowInlet &in : fb.inlets) {
    double h = 0, q = 0, qt = 0;
    port(in.patch, h, q, qt);
    h_in += h;
    q_in += q;
    qt_in += qt;
    inlets.push_back({{"label", in.patch.label},
                      {"enthalpy_flux_w", h},
                      {"mixing_cup_temperature_k",
                       q != 0 ? reference_k_ + qt / q : reference_k_}});
  }
  nlohmann::json outlets = nlohmann::json::array();
  for (const FlowOutlet &out : fb.outlets) {
    double h = 0, q = 0, qt = 0;
    port(out.patch, h, q, qt);
    h_out += h;
    q_out += q;
    qt_out += qt;
    outlets.push_back({{"label", out.patch.label},
                       {"enthalpy_flux_w", h},
                       {"mixing_cup_temperature_k",
                        q != 0 ? reference_k_ + qt / q : reference_k_}});
  }
  const double port_net = h_in + h_out; // inflow faces contribute negatively
  // Physical steady balance: every watt entering through flux/power,
  // convection and prescribed-temperature (inlet, fixture) nodes leaves as
  // port enthalpy. Discretely it differs from zero only by the free-row
  // residual of the energy solve and the weak-continuity residual of the
  // flow solve (advection_port_gap_w).
  const double inputs = flux_in + conv_in + dirichlet_in;
  const double energy_residual = port_net - inputs;
  const double gross = std::abs(flux_in) + std::abs(conv_in) +
                       std::abs(dirichlet_in) + std::abs(port_net);
  double t_min = std::numeric_limits<double>::infinity();
  double t_max = -std::numeric_limits<double>::infinity();
  for (int n = 0; n < N_; ++n)
    if (m.node_referenced[n]) {
      t_min = std::min(t_min, theta_[n]);
      t_max = std::max(t_max, theta_[n]);
    }
  double supg_moved = 0;
  for (int n = 0; n < N_; ++n)
    supg_moved += std::abs(supg_nodal[n]);
  supg_moved *= 0.5;
  return {
      {"solver",
       {{"method", "Eigen SparseLU (COLAMD), unsymmetric SUPG operator, "
                   "transposed factor reused for the adjoint"},
        {"free_unknowns", reduction_.free_size()},
        {"factor_nonzeros",
         static_cast<long>(factor_->lu.nnzL() + factor_->lu.nnzU())},
        {"numeric_factorizations", numeric_factorizations_},
        {"relative_residual", relative_residual_},
        {"max_adjoint_relative_residual", max_adjoint_residual_},
        {"forecast_bytes", forecast_bytes_}}},
      {"temperature_k",
       {{"min", reference_k_ + t_min},
        {"max", reference_k_ + t_max},
        {"volume_mean", reference_k_ + mean_weight_.dot(theta_)}}},
      {"energy",
       {{"flux_input_w", flux_in},
        {"convection_input_w", conv_in},
        {"dirichlet_input_w", dirichlet_in},
        {"total_input_w", inputs},
        {"net_enthalpy_outflow_w", port_net},
        {"global_residual_w", energy_residual},
        {"global_relative_residual", gross > 0 ? energy_residual / gross : 0.0},
        {"advected_heat_w", advected},
        {"advection_port_gap_w", advected - port_net},
        {"free_row_residual_w", std::sqrt(free_res2)},
        {"fluxes", fluxes},
        {"convections", convections}}},
      {"ports",
       {{"inlet_enthalpy_flux_w", h_in},
        {"outlet_enthalpy_flux_w", h_out},
        {"net_enthalpy_outflow_w", port_net},
        {"net_vs_advected_relative",
         advected != 0 ? (port_net - advected) / std::abs(advected) : 0.0},
        {"inlet_volume_flux_m3_s", -q_in},
        {"outlet_volume_flux_m3_s", q_out},
        {"inlets", inlets},
        {"outlets", outlets}}},
      {"numerical_diffusion",
       {{"max_element_peclet", peclet_max},
        {"max_streamline_to_physical_conductivity", ratio_max},
        {"volume_mean_streamline_to_physical_conductivity",
         ratio_volume / m.total_volume_m3},
        {"supg_redistributed_heat_w", supg_moved},
        {"supg_redistributed_relative",
         std::abs(advected) > 0 ? supg_moved / std::abs(advected) : 0.0}}},
      {"heat_leakage",
       {{"density_threshold", threshold},
        {"solid_volume_m3", solid_volume},
        {"solid_advected_heat_w", solid_advected},
        {"solid_advected_gross_w", solid_advected_abs},
        {"solid_advection_fraction",
         solid_advected_abs + fluid_advected_abs > 0
             ? solid_advected_abs / (solid_advected_abs + fluid_advected_abs)
             : 0.0},
        {"solid_max_element_peclet", solid_peclet_max},
        {"capacity", "fluid rho_f c_p,f on the Brinkman leakage velocity"}}},
  };
}

Evaluation ChtPhysics::evaluate(const Eigen::VectorXd &density,
                                bool derivatives,
                                const std::atomic<bool> *cancel) {
  solved_ = false;
  flow_.solve(density, cancel);
  solve_thermal(cancel);
  const TetMesh &m = mesh_;
  const int E = m.element_count();
  Evaluation ev;
  ev.derivatives = derivatives;
  ev.objective_unit = "K";
  ev.objective = reference_k_ + mean_weight_.dot(theta_);
  if (derivatives) {
    ev.objective_gradient.setZero(E);
    response_gradient(mean_weight_, 1.0, ev.objective_gradient, cancel);
  }
  if (problem_->limits.temperature_k)
    ev.constraints.push_back(max_temperature_constraint(
        *problem_->limits.temperature_k, derivatives, cancel));
  if (problem_->limits.pressure_drop_pa)
    ev.constraints.push_back(flow_.pressure_drop_constraint(
        *problem_->limits.pressure_drop_pa, derivatives, cancel));

  Field v;
  v.name = "velocity";
  v.unit = "m/s";
  v.location = FieldLocation::Node;
  v.components = 3;
  flow_.velocity(v.values);
  Field p;
  p.name = "pressure";
  p.unit = "Pa";
  p.location = FieldLocation::Node;
  p.components = 1;
  flow_.pressure(p.values);
  Field t;
  t.name = "temperature";
  t.unit = "K";
  t.location = FieldLocation::Node;
  t.components = 1;
  t.values = theta_.array() + reference_k_;
  Field q;
  q.name = "heat_flux";
  q.unit = "W/m2";
  q.location = FieldLocation::Element;
  q.components = 3;
  q.values.resize(3 * E);
  Eigen::Vector4d th;
  for (int e = 0; e < E; ++e) {
    gather_nodal(theta_, e, th);
    q.values.segment<3>(3 * e) = -k_e_[e] * (m.element_gradients(e) * th);
  }
  ev.fields.push_back(std::move(v));
  ev.fields.push_back(std::move(p));
  ev.fields.push_back(std::move(t));
  ev.fields.push_back(std::move(q));
  ev.evidence = {{"formulation",
                  {{"flow", flow_.formulation_evidence()},
                   {"thermal", thermal_formulation()}}},
                 {"flow", flow_.evidence()},
                 {"thermal", thermal_evidence()}};
  return ev;
}

} // namespace

namespace detail {
std::unique_ptr<Physics> make_cht_physics(std::shared_ptr<const Problem> p) {
  return std::make_unique<ChtPhysics>(std::move(p));
}
} // namespace detail

} // namespace topology
} // namespace sim
} // namespace chudware
