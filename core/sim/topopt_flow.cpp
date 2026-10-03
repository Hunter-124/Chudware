// Topology optimization: MINI Stokes-Brinkman flow state, adjoint and the flow
// (dissipation) Physics evaluator. See topopt_flow.h for the formulation.
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

// FlowSystem::kBubble* are exact integrals of the quartic bubble
// b = 256 L0 L1 L2 L3 over a tet of volume V
// (int L0^a L1^b L2^c L3^d = 3! a! b! c! d! V / (a+b+c+d+3)!):
//   int b        = 256 * 6 / 7!                     V = 32/105 V
//   int b N_a    = 256 * 6 * 2 / 8!                 V = 8/105 V
//   int b^2      = 256^2 * 6 * 16 / 11!             V
//   int |grad b|^2 = 256^2 * 6 * (8 - 4) / 9! V sum_i |grad L_i|^2
// (the last uses sum_i grad L_i = 0 to fold the cross terms).

std::uint64_t clamp_bytes(long double b) {
  if (!(b >= 0))
    return 0;
  if (b >= static_cast<long double>(std::numeric_limits<std::uint64_t>::max()))
    return std::numeric_limits<std::uint64_t>::max();
  return static_cast<std::uint64_t>(std::ceil(b));
}

// Upper bound of |u(x)| over one MINI element with nodal velocities u and
// condensed bubble amplitude ub: the P1 basis has N_a >= 0 with sum N_a = 1
// and the quartic bubble 0 <= b = 256 L0 L1 L2 L3 <= 1 (maximum at the
// centroid), so |sum_a N_a u_a + b ub| <= max_a |u_a| + |ub| everywhere.
double mini_speed_bound(const Eigen::Matrix<double, 3, 4> &u,
                        const Eigen::Vector3d &ub) {
  return u.colwise().norm().maxCoeff() + ub.norm();
}

const Problem &require_flow_problem(const std::shared_ptr<const Problem> &p) {
  if (!p)
    fail("invalid_input", "flow system needs a prepared problem");
  const PhysicsKind k = p->settings.physics;
  if (k != PhysicsKind::Flow && k != PhysicsKind::Cht)
    fail("physics_mismatch", std::string("the Stokes-Brinkman system serves "
                                         "flow and cht, not ") +
                                 to_string(k));
  if (!p->flow)
    fail("section_missing", "the prepared problem has no flow boundary");
  if (!p->mesh)
    fail("invalid_input", "the prepared problem has no mesh");
  return *p;
}

} // namespace

struct FlowSystem::Factor {
  Eigen::SparseLU<Eigen::SparseMatrix<double>, Eigen::COLAMDOrdering<int>> lu;
  bool analyzed = false;
};

FlowSystem::FlowSystem(std::shared_ptr<const Problem> problem)
    : problem_(std::move(problem)), mesh_(*require_flow_problem(problem_).mesh),
      flow_(*problem_->flow) {
  const TetMesh &m = mesh_;
  const int N = m.node_count();
  const int E = m.element_count();
  node_count_ = N;
  mu_ = flow_.viscosity_pa_s;
  alpha_max_ = flow_.alpha_max;
  interp_ = problem_->interpolation;
  if (!(mu_ > 0) || !(alpha_max_ > 0) || !std::isfinite(alpha_max_))
    fail("invalid_bc", "flow needs viscosity > 0 and a finite alpha_max > 0");

  // Cheap admission on the prescribed inflow only; every solved state is
  // checked again against its own speed bound in solve().
  if (!(flow_.reynolds <= kMaxReynolds))
    fail("flow_regime_invalid",
         "prescribed-inlet Reynolds number " + fmt(flow_.reynolds) +
             " (density * mean inlet speed * reference length / viscosity) "
             "exceeds " +
             fmt(kMaxReynolds) +
             ", the creeping-flow limit of this Stokes-Brinkman model; it has "
             "no inertia term and does not represent Navier-Stokes or "
             "turbulent flow");

  // Velocity Dirichlet from preparation; pressure pinned only at nodes no
  // element references (they carry no equation at all).
  std::vector<std::pair<int, double>> entries;
  entries.reserve(static_cast<std::size_t>(flow_.velocity.size()) +
                  m.unreferenced_nodes.size());
  for (int k = 0; k < flow_.velocity.size(); ++k)
    entries.push_back({flow_.velocity.dofs[k], flow_.velocity.values[k]});
  for (int n : m.unreferenced_nodes)
    entries.push_back({3 * N + n, 0.0});
  reduction_ = make_dof_reduction(4 * N, make_dirichlet(4 * N, entries));

  // Structural well-posedness of the ports.
  prescribed_inflow_m3_s_ = 0;
  for (const FlowInlet &in : flow_.inlets) {
    double q = 0;
    for (int f : in.patch.faces) {
      const Eigen::Vector3d n = m.surface_normal.col(f);
      for (int k = 0; k < 3; ++k) {
        const int node = m.surface_nodes(k, f);
        Eigen::Vector3d v;
        for (int c = 0; c < 3; ++c)
          v[c] = reduction_.prescribed[3 * node + c];
        q -= v.dot(n) * m.surface_area_m2[f] / 3.0;
      }
    }
    if (!(q > 1e-12 * in.nominal_flow_m3_s))
      fail("inlet_fully_walled",
           in.patch.label +
               " has no node off the no-slip rim, so its discrete inflow is " +
               fmt(q) + " m^3/s; refine the mesh across the inlet");
    prescribed_inflow_m3_s_ += q;
    inlet_faces_.push_back(in.patch.faces);
  }
  for (const FlowOutlet &out : flow_.outlets) {
    bool any_free = false;
    for (int n : out.patch.nodes)
      for (int c = 0; c < 3; ++c)
        any_free = any_free || reduction_.full_to_free[3 * n + c] >= 0;
    if (!any_free)
      fail("outlet_fully_walled",
           out.patch.label +
               " has every node on the no-slip rim: its pressure traction acts "
               "on no free velocity and the pressure level is undetermined; "
               "refine the mesh across the outlet");
    outlet_faces_.push_back(out.patch.faces);
  }

  // Bounded refusal and governor admission before heavy allocation.
  const long nf = reduction_.free_size();
  if (nf > kMaxUnknowns)
    fail("problem_too_large",
         std::to_string(nf) +
             " free flow unknowns exceed the direct-solver "
             "ceiling of " +
             std::to_string(kMaxUnknowns));
  const long double Ed = E;
  const long double forecast =
      4.0L * 256.0L * Ed    // ReducedPattern slot table
      + 16.0L * 256.0L * Ed // triplet build (transient)
      + 12.0L * 256.0L * Ed // reduced CSC pattern upper bound
      + 2.0L * 12.0L * kFactorFillConstant *
            std::pow(static_cast<long double>(nf), 4.0L / 3.0L) // L + U
      + 8.0L * 20.0L * 4.0L * N; // state/rhs/adjoint vectors
  forecast_bytes_ = clamp_bytes(forecast);
  {
    std::string why;
    if (!chudware::governor::admit(forecast_bytes_,
                                   "topology Stokes-Brinkman system", why))
      fail("memory_budget", why);
  }

  const Eigen::MatrixXi vmap = nodal_dof_map(m, 3, 0);
  const Eigen::MatrixXi pmap = nodal_dof_map(m, 1, 3 * N);
  element_dofs_.resize(16, E);
  element_dofs_.topRows(12) = vmap;
  element_dofs_.bottomRows(4) = pmap;
  pattern_ = ReducedPattern(element_dofs_, reduction_);
  K_ = pattern_.pattern();

  bubble_viscous_.resize(E);
  lift_flag_.assign(E, 0);
  for (int e = 0; e < E; ++e) {
    const Eigen::Matrix<double, 3, 4> G = m.element_gradients(e);
    bubble_viscous_[e] = mu_ * kBubbleStiff * m.volume_m3[e] * G.squaredNorm();
    for (int i = 0; i < 16; ++i) {
      const int d = element_dofs_(i, e);
      if (reduction_.full_to_free[d] < 0 && reduction_.prescribed[d] != 0.0)
        lift_flag_[e] = 1;
    }
  }

  traction_.setZero(4 * N);
  for (const FlowOutlet &out : flow_.outlets)
    for (int f : out.patch.faces) {
      const Eigen::Vector3d load = -out.pressure_pa * m.surface_normal.col(f) *
                                   (m.surface_area_m2[f] / 3.0);
      for (int k = 0; k < 3; ++k)
        traction_.segment<3>(3 * m.surface_nodes(k, f)) += load;
    }

  drop_functional_.setZero(4 * N);
  auto add_mean = [&](const std::vector<std::vector<int>> &ports, double sign) {
    double area = 0;
    for (const auto &faces : ports)
      for (int f : faces)
        area += m.surface_area_m2[f];
    for (const auto &faces : ports)
      for (int f : faces)
        for (int k = 0; k < 3; ++k)
          drop_functional_[3 * N + m.surface_nodes(k, f)] +=
              sign * m.surface_area_m2[f] / (3.0 * area);
  };
  add_mean(inlet_faces_, 1.0);
  add_mean(outlet_faces_, -1.0);

  double h = m.quality.median_element_size_m;
  if (!(h > 0))
    h = std::cbrt(m.total_volume_m3 / std::max(1, E));
  sigma_ = mu_ / h;

  factor_ = std::make_unique<Factor>();
  density_.resize(E);
  alpha_e_.resize(E);
  dalpha_e_.resize(E);
  state_.setZero(4 * N);
}

FlowSystem::~FlowSystem() = default;

double FlowSystem::alpha(double rho) const {
  return alpha_max_ * interp_.shape(rho);
}

double FlowSystem::alpha_derivative(double rho) const {
  return alpha_max_ * interp_.shape_derivative(rho);
}

void FlowSystem::element_matrix(int e, double alpha, Mat16 &S) const {
  const Eigen::Matrix<double, 3, 4> G = mesh_.element_gradients(e);
  const double V = mesh_.volume_m3[e];
  const Eigen::Matrix4d GtG = G.transpose() * G;
  const double mb = kBubbleLinear * V, s = kBubbleMean * V;
  const double beta = bubble_viscous_[e] + alpha * kBubbleMass * V;
  const double c_uu = alpha * alpha * mb * mb / beta;
  const double c_up = alpha * mb * s / beta;
  const double c_pp = s * s / beta;
  S.setZero();
  for (int a = 0; a < 4; ++a)
    for (int b = 0; b < 4; ++b) {
      const double v =
          mu_ * V * GtG(a, b) + alpha * V / 20.0 * (a == b ? 2.0 : 1.0) - c_uu;
      for (int c = 0; c < 3; ++c)
        S(3 * a + c, 3 * b + c) = v;
    }
  for (int a = 0; a < 4; ++a)
    for (int c = 0; c < 3; ++c)
      for (int q = 0; q < 4; ++q) {
        const double v = -0.25 * V * G(c, a) - c_up * G(c, q);
        S(3 * a + c, 12 + q) = v;
        S(12 + q, 3 * a + c) = v;
      }
  for (int q = 0; q < 4; ++q)
    for (int r = 0; r < 4; ++r)
      S(12 + q, 12 + r) = -c_pp * GtG(q, r);
}

void FlowSystem::element_matrix_derivative(int e, double alpha,
                                           Mat16 &dS) const {
  const Eigen::Matrix<double, 3, 4> G = mesh_.element_gradients(e);
  const double V = mesh_.volume_m3[e];
  const Eigen::Matrix4d GtG = G.transpose() * G;
  const double mb = kBubbleLinear * V, s = kBubbleMean * V;
  const double bp = kBubbleMass * V;
  const double beta = bubble_viscous_[e] + alpha * bp;
  const double b2 = beta * beta;
  const double d_uu = mb * mb * (2.0 * alpha / beta - alpha * alpha * bp / b2);
  const double d_up = mb * s * (1.0 / beta - alpha * bp / b2);
  const double d_pp = s * s * bp / b2;
  dS.setZero();
  for (int a = 0; a < 4; ++a)
    for (int b = 0; b < 4; ++b) {
      const double v = V / 20.0 * (a == b ? 2.0 : 1.0) - d_uu;
      for (int c = 0; c < 3; ++c)
        dS(3 * a + c, 3 * b + c) = v;
    }
  for (int a = 0; a < 4; ++a)
    for (int c = 0; c < 3; ++c)
      for (int q = 0; q < 4; ++q) {
        const double v = -d_up * G(c, q);
        dS(3 * a + c, 12 + q) = v;
        dS(12 + q, 3 * a + c) = v;
      }
  for (int q = 0; q < 4; ++q)
    for (int r = 0; r < 4; ++r)
      dS(12 + q, 12 + r) = d_pp * GtG(q, r);
}

void FlowSystem::scale_pressure(Mat16 &S) const {
  S.bottomRows<4>() *= sigma_;
  S.rightCols<4>() *= sigma_;
}

void FlowSystem::gather(const Eigen::VectorXd &full, int e,
                        Eigen::Matrix<double, 16, 1> &xe) const {
  for (int i = 0; i < 16; ++i)
    xe[i] = full[element_dofs_(i, e)];
}

void FlowSystem::solve(const Eigen::VectorXd &density,
                       const std::atomic<bool> *cancel) {
  solved_ = false;
  check_density(*problem_, density);
  throw_if_cancelled(cancel);
  const int E = mesh_.element_count();
  const int N = node_count_;
  density_ = density;
  for (int e = 0; e < E; ++e) {
    alpha_e_[e] = alpha(density[e]);
    dalpha_e_[e] = alpha_derivative(density[e]);
  }
  ReducedPattern::zero(K_);
  reduction_.restrict_vector(traction_, rhs_free_);
  Mat16 S;
  for (int e = 0; e < E; ++e) {
    if ((e & 4095) == 0)
      throw_if_cancelled(cancel);
    element_matrix(e, alpha_e_[e], S);
    scale_pressure(S);
    pattern_.add(K_, e, S);
    if (lift_flag_[e])
      pattern_.lift(rhs_free_, e, S);
  }
  throw_if_cancelled(cancel);
  if (chudware::governor::low_memory_now())
    fail("memory_low", "host memory is critically low before the "
                       "Stokes-Brinkman factorization");
  if (!factor_->analyzed) {
    factor_->lu.analyzePattern(K_);
    factor_->analyzed = true;
  }
  factor_->lu.factorize(K_);
  if (factor_->lu.info() != Eigen::Success)
    fail("singular_system", "Stokes-Brinkman factorization failed: " +
                                factor_->lu.lastErrorMessage());
  ++numeric_factorizations_;
  throw_if_cancelled(cancel);
  x_free_ = factor_->lu.solve(rhs_free_);
  if (!x_free_.allFinite())
    fail("singular_system", "Stokes-Brinkman solve produced non-finite values");
  scratch_free_.noalias() = K_ * x_free_;
  scratch_free_ -= rhs_free_;
  const double bn = rhs_free_.norm();
  relative_residual_ =
      bn > 0 ? scratch_free_.norm() / bn : scratch_free_.norm();
  if (!(relative_residual_ <= kMaxRelativeResidual))
    fail("solve_inaccurate", "Stokes-Brinkman relative residual " +
                                 fmt(relative_residual_) + " exceeds " +
                                 fmt(kMaxRelativeResidual));
  reduction_.expand_vector(x_free_, state_);
  state_.tail(N) *= sigma_;

  // Creeping-flow validity of THIS state, before any response consumes it.
  // The constructor's inlet admission sees only the prescribed inflow; open
  // outlets at different pressures, or constrictions of the design, drive
  // speeds it cannot bound. A refused state is cleared, never clamped.
  double speed_bound = 0;
  int worst = 0;
  for (int e = 0; e < E; ++e) {
    const Eigen::Matrix<double, 3, 4> G = mesh_.element_gradients(e);
    const double V = mesh_.volume_m3[e];
    const double a = alpha_e_[e];
    const double beta = bubble_viscous_[e] + a * kBubbleMass * V;
    Eigen::Matrix<double, 3, 4> u;
    Eigen::Vector4d p;
    for (int k = 0; k < 4; ++k) {
      const int n = mesh_.tets(k, e);
      u.col(k) = state_.segment<3>(3 * n);
      p[k] = state_[3 * N + n];
    }
    const Eigen::Vector3d ub = -(a * kBubbleLinear * V * u.rowwise().sum() +
                                 kBubbleMean * V * (G * p)) /
                               beta;
    const double bound = mini_speed_bound(u, ub);
    if (bound > speed_bound) {
      speed_bound = bound;
      worst = e;
    }
  }
  const double reynolds =
      flow_.density_kg_m3 * speed_bound * flow_.reference_length_m / mu_;
  if (!(reynolds <= kMaxReynolds)) {
    state_.setZero();
    fail("flow_regime_invalid",
         "solved-state Reynolds bound " + fmt(reynolds) +
             " (density * speed bound " + fmt(speed_bound) +
             " m/s * reference length " + fmt(flow_.reference_length_m) +
             " m / viscosity; speed bound max_a |u_a| + |u_b| over the full "
             "MINI velocity including the bubble, reached at element " +
             std::to_string(worst) + ") exceeds " + fmt(kMaxReynolds) +
             ", the creeping-flow limit of this Stokes-Brinkman model; the "
             "prescribed-inlet Reynolds number " +
             fmt(flow_.reynolds) +
             " passed admission, so outlet pressure differences or design "
             "constrictions drive the excess. The model has no inertia term "
             "and does not represent Navier-Stokes or turbulent flow");
  }
  solved_ = true;
}

void FlowSystem::velocity(Eigen::VectorXd &out) const {
  out = state_.head(3 * node_count_);
}

void FlowSystem::pressure(Eigen::VectorXd &out) const {
  out = state_.tail(node_count_);
}

void FlowSystem::element_velocity(int e, FlowElementVelocity &out) const {
  if (!solved_)
    fail("not_solved", "element velocity requested before a successful solve");
  const int N = node_count_;
  const Eigen::Matrix<double, 3, 4> G = mesh_.element_gradients(e);
  const double V = mesh_.volume_m3[e];
  const double a = alpha_e_[e];
  const double mb = kBubbleLinear * V, s = kBubbleMean * V;
  const double c2V = kBubbleMass * V;
  const double beta = bubble_viscous_[e] + a * c2V;
  Eigen::Vector4d p;
  for (int k = 0; k < 4; ++k) {
    const int n = mesh_.tets(k, e);
    out.nodal.col(k) = state_.segment<3>(3 * n);
    p[k] = state_[3 * N + n];
  }
  const Eigen::Vector3d U = out.nodal.rowwise().sum();
  out.bubble = -(a * mb * U + s * (G * p)) / beta;
  out.bubble_du = -a * mb / beta;
  out.bubble_dp = -(s / beta) * G;
  out.bubble_dalpha = -(mb / beta) * U - (c2V / beta) * out.bubble;
  out.mean = 0.25 * U + kBubbleMean * out.bubble;
}

void FlowSystem::solve_adjoint(const Eigen::VectorXd &rhs,
                               Eigen::VectorXd &lambda,
                               const std::atomic<bool> *cancel) const {
  if (!solved_)
    fail("not_solved", "flow adjoint requested before a successful solve");
  const int N = node_count_;
  if (rhs.size() != 4 * N)
    fail("invalid_input", "flow adjoint right-hand side must have 4N entries");
  throw_if_cancelled(cancel);
  const int nf = reduction_.free_size();
  adjoint_rhs_free_.resize(nf);
  for (int i = 0; i < nf; ++i) {
    const int j = reduction_.free_to_full[i];
    adjoint_rhs_free_[i] = j >= 3 * N ? sigma_ * rhs[j] : rhs[j];
  }
  lambda.setZero(4 * N);
  const double bn = adjoint_rhs_free_.norm();
  if (bn == 0.0)
    return;
  adjoint_free_ = factor_->lu.solve(adjoint_rhs_free_);
  if (!adjoint_free_.allFinite())
    fail("singular_system", "flow adjoint produced non-finite values");
  adjoint_scratch_.noalias() = K_ * adjoint_free_;
  adjoint_scratch_ -= adjoint_rhs_free_;
  const double rel = adjoint_scratch_.norm() / bn;
  max_adjoint_residual_ = std::max(max_adjoint_residual_, rel);
  if (!(rel <= kMaxRelativeResidual))
    fail("solve_inaccurate", "flow adjoint relative residual " + fmt(rel) +
                                 " exceeds " + fmt(kMaxRelativeResidual));
  for (int i = 0; i < nf; ++i) {
    const int j = reduction_.free_to_full[i];
    lambda[j] = j >= 3 * N ? sigma_ * adjoint_free_[i] : adjoint_free_[i];
  }
}

void FlowSystem::add_state_gradient(const Eigen::VectorXd &lambda,
                                    double weight,
                                    Eigen::VectorXd &grad) const {
  if (!solved_)
    fail("not_solved", "flow gradient requested before a successful solve");
  const int E = mesh_.element_count();
  if (grad.size() != E || lambda.size() != 4 * node_count_)
    fail("invalid_input", "flow gradient size mismatch");
  Mat16 dS;
  Eigen::Matrix<double, 16, 1> xe, le;
  for (int e = 0; e < E; ++e) {
    if (dalpha_e_[e] == 0.0)
      continue;
    gather(lambda, e, le);
    if (le.isZero(0.0))
      continue;
    gather(state_, e, xe);
    element_matrix_derivative(e, alpha_e_[e], dS);
    grad[e] -= weight * dalpha_e_[e] * le.dot(dS * xe);
  }
}

double FlowSystem::dissipation(Eigen::VectorXd *gradient,
                               const std::atomic<bool> *cancel) const {
  if (!solved_)
    fail("not_solved", "dissipation requested before a successful solve");
  const int N = node_count_;
  const int E = mesh_.element_count();
  const bool deriv = gradient != nullptr;
  if (deriv) {
    dphi_dx_.setZero(4 * N);
    gradient->setZero(E);
  }
  double phi = 0;
  for (int e = 0; e < E; ++e) {
    if ((e & 4095) == 0)
      throw_if_cancelled(cancel);
    const Eigen::Matrix<double, 3, 4> G = mesh_.element_gradients(e);
    const double V = mesh_.volume_m3[e];
    const Eigen::Matrix4d GtG = G.transpose() * G;
    const double a = alpha_e_[e];
    const double mb = kBubbleLinear * V, s = kBubbleMean * V;
    const double c2V = kBubbleMass * V;
    const double beta = bubble_viscous_[e] + a * c2V;
    Eigen::Matrix<double, 3, 4> u;
    Eigen::Vector4d p;
    for (int k = 0; k < 4; ++k) {
      const int n = mesh_.tets(k, e);
      u.col(k) = state_.segment<3>(3 * n);
      p[k] = state_[3 * N + n];
    }
    const Eigen::Vector3d U = u.rowwise().sum();
    const Eigen::Vector3d gp = G * p;
    const Eigen::Vector3d ub = -(a * mb * U + s * gp) / beta;
    Eigen::Matrix<double, 3, 4> Au = mu_ * V * (u * GtG);
    for (int k = 0; k < 4; ++k)
      Au.col(k) += a * V / 20.0 * (u.col(k) + U);
    phi += u.cwiseProduct(Au).sum() + 2.0 * a * mb * U.dot(ub) +
           beta * ub.squaredNorm();
    if (!deriv)
      continue;
    // psi = dPhi/d ub = 2 a mb U + 2 beta ub = -2 s grad p.
    const Eigen::Vector3d psi = -2.0 * s * gp;
    for (int k = 0; k < 4; ++k) {
      const int n = mesh_.tets(k, e);
      dphi_dx_.segment<3>(3 * n) +=
          2.0 * Au.col(k) + 2.0 * a * mb * ub - (a * mb / beta) * psi;
      dphi_dx_[3 * N + n] += -(s / beta) * psi.dot(G.col(k));
    }
    const double uMu = V / 20.0 * (u.squaredNorm() + U.squaredNorm());
    const Eigen::Vector3d dub = -(mb / beta) * U - (c2V / beta) * ub;
    const double dphi_dalpha =
        uMu + 2.0 * mb * U.dot(ub) + c2V * ub.squaredNorm() + psi.dot(dub);
    (*gradient)[e] = dalpha_e_[e] * dphi_dalpha;
  }
  if (deriv) {
    solve_adjoint(dphi_dx_, lambda_, cancel);
    add_state_gradient(lambda_, 1.0, *gradient);
  }
  return phi;
}

double FlowSystem::pressure_drop() const {
  if (!solved_)
    fail("not_solved", "pressure drop requested before a successful solve");
  return drop_functional_.dot(state_);
}

Constraint
FlowSystem::pressure_drop_constraint(double limit_pa, bool derivatives,
                                     const std::atomic<bool> *cancel) const {
  if (!(std::isfinite(limit_pa) && limit_pa > 0))
    fail("invalid_input", "pressure-drop limit must be finite and > 0 Pa");
  Constraint c;
  c.name = "pressure_drop";
  c.unit = "Pa";
  c.aggregation = "exact";
  c.limit = limit_pa;
  c.response = pressure_drop();
  c.value = c.response / limit_pa - 1.0;
  if (derivatives) {
    solve_adjoint(drop_functional_, lambda_, cancel);
    c.gradient.setZero(mesh_.element_count());
    add_state_gradient(lambda_, 1.0 / limit_pa, c.gradient);
  }
  return c;
}

double FlowSystem::port_flux(const std::vector<int> &faces) const {
  double q = 0;
  for (int f : faces) {
    const Eigen::Vector3d n = mesh_.surface_normal.col(f);
    double sum = 0;
    for (int k = 0; k < 3; ++k)
      sum += state_.segment<3>(3 * mesh_.surface_nodes(k, f)).dot(n);
    q += sum * mesh_.surface_area_m2[f] / 3.0;
  }
  return q;
}

nlohmann::json FlowSystem::formulation_evidence() const {
  const bool ramp = interp_.kind == Interpolation::Kind::Ramp;
  return {
      {"model",
       "steady incompressible Stokes-Brinkman (creeping flow, no inertia)"},
      {"weak_form",
       "mu (grad u, grad v) + (alpha(rho) u, v) - (p, div v) = -sum_outlets "
       "p_out (v . n)_outlet; -(q, div u) = 0 (Laplacian viscous form, "
       "do-nothing outlet traction mu du/dn - p n = -p_out n)"},
      {"element",
       "MINI: P1 velocity + quartic bubble 256 L0 L1 L2 L3 per component, P1 "
       "pressure; inf-sup stable, bubble statically condensed per element"},
      {"stabilization",
       {{"added_terms", "none (no tunable parameter)"},
        {"condensed_bubble_pressure_block",
         "-(int b)^2 / beta_e (grad N_q . grad N_q'), beta_e = mu int|grad "
         "b|^2 + alpha_e int b^2"},
        {"condensed_bubble_velocity_terms",
         "-alpha_e^2 (int b N_a)^2 / beta_e and -alpha_e (int b N_a)(int b) / "
         "beta_e grad N_q, from the Brinkman bubble coupling"},
        {"bubble_integrals_per_volume",
         {{"int_b", kBubbleMean},
          {"int_b_Na", kBubbleLinear},
          {"int_b2", kBubbleMass},
          {"int_grad_b2_per_sum_grad_L2", kBubbleStiff}}}}},
      {"boundary_conditions",
       "no-slip on every exterior triangle outside a port (rim nodes "
       "no-slip), prescribed inlet velocity on interior inlet nodes, "
       "traction outlet pressure (natural)"},
      {"resistance",
       {{"law", "alpha(rho) = alpha_max f(rho), rho = 1 solid; alpha(0) = 0"},
        {"interpolation", ramp ? "ramp" : "simp"},
        {"f", ramp ? "rho / (1 + q (1 - rho))" : "rho^p"},
        {"exponent", interp_.exponent},
        {"alpha_max_pa_s_m2", alpha_max_},
        {"brinkman_max", flow_.brinkman_max},
        {"darcy_number_solid", 1.0 / flow_.brinkman_max},
        {"property_floor_applied", false}}},
      {"regime",
       {{"reynolds_input", flow_.reynolds},
        {"reynolds_limit", kMaxReynolds},
        {"reference_length_m", flow_.reference_length_m},
        {"enforcement",
         "flow_regime_invalid when the prescribed-inlet Reynolds number "
         "(density * mean inlet speed * L / mu) exceeds the limit at "
         "construction, and after every solve, before any response, when "
         "the solved-state bound density * max_e(max_a |u_a| + |u_b|) * L / "
         "mu exceeds it (bounds |u(x)| of the full MINI velocity: N_a >= 0, "
         "sum N_a = 1, 0 <= b <= 1), so pressure-driven or constricted flow "
         "cannot bypass it; no speed clamp or scaling, no Navier-Stokes "
         "fallback"},
        {"claims", "creeping flow only; no Navier-Stokes, no turbulence"}}},
      {"dissipation",
       "Phi = int(mu grad u : grad u + alpha |u|^2) dV over the full MINI "
       "velocity (W)"},
      {"pressure_drop",
       "area-mean inlet pressure - area-mean outlet pressure (Pa)"},
      {"gradient",
       "discrete adjoint of the condensed symmetric operator; prescribed "
       "inlet velocities enter through dS/dalpha applied to the full state"},
      {"pressure_scale_pa", sigma_},
  };
}

nlohmann::json FlowSystem::evidence() const {
  if (!solved_)
    fail("not_solved", "flow evidence requested before a successful solve");
  const TetMesh &m = mesh_;
  const int N = node_count_;
  const int E = m.element_count();
  const double threshold = problem_->settings.density_threshold;
  Eigen::VectorXd r = -traction_;
  Mat16 S;
  Eigen::Matrix<double, 16, 1> xe;
  double viscous = 0, brinkman = 0, brinkman_solid = 0;
  double solid_volume = 0, fluid_volume = 0;
  double solid_speed = 0, fluid_speed = 0, max_solid_speed = 0;
  double div2 = 0, grad2 = 0, max_cell_re = 0, speed_bound = 0;
  for (int e = 0; e < E; ++e) {
    const double a = alpha_e_[e];
    element_matrix(e, a, S);
    gather(state_, e, xe);
    const Eigen::Matrix<double, 16, 1> se = S * xe;
    for (int i = 0; i < 16; ++i)
      r[element_dofs_(i, e)] += se[i];
    const Eigen::Matrix<double, 3, 4> G = m.element_gradients(e);
    const double V = m.volume_m3[e];
    const Eigen::Matrix4d GtG = G.transpose() * G;
    const double mb = kBubbleLinear * V, s = kBubbleMean * V;
    const double c2V = kBubbleMass * V;
    const double beta = bubble_viscous_[e] + a * c2V;
    Eigen::Matrix<double, 3, 4> u;
    Eigen::Vector4d p;
    for (int k = 0; k < 4; ++k) {
      u.col(k) = state_.segment<3>(3 * m.tets(k, e));
      p[k] = state_[3 * N + m.tets(k, e)];
    }
    const Eigen::Vector3d U = u.rowwise().sum();
    const Eigen::Vector3d ub = -(a * mb * U + s * (G * p)) / beta;
    viscous += mu_ * V * u.cwiseProduct(u * GtG).sum() +
               bubble_viscous_[e] * ub.squaredNorm();
    const double be = a * (V / 20.0 * (u.squaredNorm() + U.squaredNorm()) +
                           2.0 * mb * U.dot(ub) + c2V * ub.squaredNorm());
    brinkman += be;
    const double speed = (0.25 * U + kBubbleMean * ub).norm();
    const Eigen::Matrix3d gradu = u * G.transpose();
    div2 += V * gradu.trace() * gradu.trace();
    grad2 += V * gradu.squaredNorm();
    max_cell_re = std::max(max_cell_re, flow_.density_kg_m3 * speed *
                                            m.element_size_m[e] / mu_);
    speed_bound = std::max(speed_bound, mini_speed_bound(u, ub));
    if (density_[e] >= threshold) {
      brinkman_solid += be;
      solid_volume += V;
      solid_speed += V * speed;
      max_solid_speed = std::max(max_solid_speed, speed);
    } else {
      fluid_volume += V;
      fluid_speed += V * speed;
    }
  }
  double dirichlet_power = 0, reaction_norm2 = 0, momentum2 = 0;
  double continuity_max = 0;
  for (int j = 0; j < 4 * N; ++j) {
    const bool free = reduction_.full_to_free[j] >= 0;
    if (j < 3 * N) {
      if (free) {
        momentum2 += r[j] * r[j];
      } else {
        dirichlet_power += r[j] * state_[j];
        reaction_norm2 += r[j] * r[j];
      }
    } else if (free) {
      continuity_max = std::max(continuity_max, std::abs(r[j]));
    }
  }
  const double outlet_power = traction_.dot(state_);
  const double phi = viscous + brinkman;
  const double port_power = dirichlet_power + outlet_power;

  double max_speed = 0;
  for (int n = 0; n < N; ++n)
    if (m.node_referenced[n])
      max_speed = std::max(max_speed, state_.segment<3>(3 * n).norm());

  auto mean_pressure = [&](const std::vector<int> &faces) {
    double area = 0, sum = 0;
    for (int f : faces) {
      double ps = 0;
      for (int k = 0; k < 3; ++k)
        ps += state_[3 * N + m.surface_nodes(k, f)];
      sum += ps * m.surface_area_m2[f] / 3.0;
      area += m.surface_area_m2[f];
    }
    return area > 0 ? sum / area : 0.0;
  };
  nlohmann::json inlets = nlohmann::json::array();
  nlohmann::json outlets = nlohmann::json::array();
  double q_in = 0, q_out = 0;
  for (std::size_t i = 0; i < flow_.inlets.size(); ++i) {
    const double q = -port_flux(inlet_faces_[i]);
    q_in += q;
    inlets.push_back(
        {{"label", flow_.inlets[i].patch.label},
         {"inflow_m3_s", q},
         {"nominal_inflow_m3_s", flow_.inlets[i].nominal_flow_m3_s},
         {"mean_pressure_pa", mean_pressure(inlet_faces_[i])}});
  }
  for (std::size_t i = 0; i < flow_.outlets.size(); ++i) {
    const double q = port_flux(outlet_faces_[i]);
    q_out += q;
    outlets.push_back({{"label", flow_.outlets[i].patch.label},
                       {"outflow_m3_s", q},
                       {"prescribed_pressure_pa", flow_.outlets[i].pressure_pa},
                       {"mean_pressure_pa", mean_pressure(outlet_faces_[i])}});
  }
  const double q_wall = port_flux(flow_.wall_faces);
  const double gross_power =
      std::abs(dirichlet_power) + std::abs(outlet_power) + std::abs(phi);

  return {
      {"solver",
       {{"method", "Eigen SparseLU (COLAMD), condensed MINI saddle-point "
                   "system, symbolic analysis reused"},
        {"free_unknowns", reduction_.free_size()},
        {"pattern_nonzeros", static_cast<long>(K_.nonZeros())},
        {"factor_nonzeros",
         static_cast<long>(factor_->lu.nnzL() + factor_->lu.nnzU())},
        {"numeric_factorizations", numeric_factorizations_},
        {"relative_residual", relative_residual_},
        {"max_adjoint_relative_residual", max_adjoint_residual_},
        {"forecast_bytes", forecast_bytes_},
        {"pressure_scale_pa", sigma_}}},
      {"residual",
       {{"momentum_free_relative",
         std::sqrt(momentum2) /
             std::max(std::sqrt(reaction_norm2) + traction_.norm(),
                      std::numeric_limits<double>::min())},
        {"weak_continuity_max_m3_s", continuity_max},
        {"weak_continuity_max_relative",
         continuity_max / prescribed_inflow_m3_s_},
        {"p1_divergence_l2_relative",
         grad2 > 0 ? std::sqrt(div2 / grad2) : 0.0}}},
      {"flux",
       {{"prescribed_inflow_m3_s", prescribed_inflow_m3_s_},
        {"inflow_m3_s", q_in},
        {"outflow_m3_s", q_out},
        {"wall_flux_m3_s", q_wall},
        {"relative_imbalance", (q_in - q_out) / q_in},
        {"inlets", inlets},
        {"outlets", outlets}}},
      {"power",
       {{"dissipation_w", phi},
        {"viscous_w", viscous},
        {"brinkman_w", brinkman},
        {"dirichlet_reaction_power_w", dirichlet_power},
        {"outlet_traction_power_w", outlet_power},
        {"port_power_w", port_power},
        {"relative_imbalance",
         gross_power > 0 ? (phi - port_power) / gross_power : 0.0}}},
      {"pressure_drop_pa", drop_functional_.dot(state_)},
      {"leakage",
       {{"density_threshold", threshold},
        {"solid_volume_m3", solid_volume},
        {"fluid_volume_m3", fluid_volume},
        {"solid_mean_speed_m_s",
         solid_volume > 0 ? solid_speed / solid_volume : 0.0},
        {"fluid_mean_speed_m_s",
         fluid_volume > 0 ? fluid_speed / fluid_volume : 0.0},
        {"solid_max_speed_m_s", max_solid_speed},
        {"solid_to_fluid_speed_ratio",
         fluid_volume > 0 && fluid_speed > 0 && solid_volume > 0
             ? (solid_speed / solid_volume) / (fluid_speed / fluid_volume)
             : 0.0},
        {"brinkman_power_in_solid_w", brinkman_solid},
        {"solid_power_fraction", phi > 0 ? brinkman_solid / phi : 0.0},
        {"darcy_number_solid", 1.0 / flow_.brinkman_max}}},
      {"regime",
       {{"reynolds_limit", kMaxReynolds},
        {"reference_length_m", flow_.reference_length_m},
        {"reynolds_input", flow_.reynolds},
        {"reynolds_input_basis",
         "density * mean prescribed inlet speed * reference length / "
         "viscosity; construction-time admission, blind to pressure-driven "
         "flow between outlets"},
        {"reynolds_solution_bound",
         flow_.density_kg_m3 * speed_bound * flow_.reference_length_m / mu_},
        {"speed_bound_m_s", speed_bound},
        {"reynolds_solution_basis",
         "density * max over elements of (max_a |u_a| + |u_b|) * reference "
         "length / viscosity, an upper bound on |u(x)| of the full MINI "
         "velocity including the bubble; enforced <= reynolds_limit after "
         "every solve"},
        {"max_node_speed_m_s", max_speed},
        {"max_cell_reynolds", max_cell_re},
        {"max_cell_reynolds_basis",
         "density * |element mean velocity| * element size / viscosity; "
         "resolution diagnostic, not the validity check"}}},
  };
}

// ── Flow (dissipation) Physics
// ──────────────────────────────────────────────
namespace {

class FlowPhysics final : public Physics {
public:
  explicit FlowPhysics(std::shared_ptr<const Problem> problem)
      : problem_(require_dissipation(problem)), flow_(std::move(problem)) {}

  static std::shared_ptr<const Problem>
  require_dissipation(const std::shared_ptr<const Problem> &p) {
    if (p->settings.objective != ObjectiveKind::Dissipation)
      fail("objective_not_supported",
           std::string("flow evaluates dissipation, not ") +
               to_string(p->settings.objective));
    return p;
  }

  PhysicsKind kind() const override { return PhysicsKind::Flow; }

  std::string formulation() const override {
    return std::string("tet4 MINI (P1-bubble/P1) Stokes-Brinkman creeping "
                       "flow, ") +
           (problem_->interpolation.kind == Interpolation::Kind::Ramp
                ? "RAMP"
                : "SIMP") +
           " Brinkman resistance, dissipated power, adjoint";
  }

  Evaluation evaluate(const Eigen::VectorXd &density, bool derivatives,
                      const std::atomic<bool> *cancel) override {
    flow_.solve(density, cancel);
    Evaluation ev;
    ev.derivatives = derivatives;
    ev.objective_unit = "W";
    ev.objective = flow_.dissipation(
        derivatives ? &ev.objective_gradient : nullptr, cancel);
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
    ev.fields.push_back(std::move(v));
    ev.fields.push_back(std::move(p));
    ev.evidence = {{"formulation", flow_.formulation_evidence()},
                   {"flow", flow_.evidence()}};
    return ev;
  }

private:
  std::shared_ptr<const Problem> problem_;
  FlowSystem flow_;
};

} // namespace

std::unique_ptr<Physics>
make_flow_physics(std::shared_ptr<const Problem> problem) {
  if (!problem)
    fail("invalid_input", "make_flow_physics needs a prepared problem");
  switch (problem->settings.physics) {
  case PhysicsKind::Flow:
    return std::make_unique<FlowPhysics>(std::move(problem));
  case PhysicsKind::Cht:
    return detail::make_cht_physics(std::move(problem));
  default:
    break;
  }
  fail("physics_mismatch", std::string("make_flow_physics serves flow and "
                                       "cht, not ") +
                               to_string(problem->settings.physics));
}

} // namespace topology
} // namespace sim
} // namespace chudware
