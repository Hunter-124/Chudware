// Topology optimization design-update methods. See topopt_optimizer.h.
#include "sim/topopt_optimizer.h"

#include "util/resource_governor.h"

#include <Eigen/SparseCholesky>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstring>
#include <limits>
#include <numeric>
#include <sstream>

namespace chudware {
namespace sim {
namespace topology {

namespace {

using nlohmann::json;
using Clock = std::chrono::steady_clock;

constexpr int kMaxConstraints = 64;
constexpr double kDoubleMax = std::numeric_limits<double>::max();

[[noreturn]] void fail(const std::string &code, const std::string &message) {
  throw ModelError(code, message);
}

std::string fmt(double v) {
  std::ostringstream s;
  s.precision(6);
  s << v;
  return s.str();
}

void admit_or_refuse(std::uint64_t bytes, const char *what) {
  std::string why;
  if (!chudware::governor::admit(bytes, what, why))
    fail("memory_budget_exceeded", why);
}

double seconds_since(Clock::time_point t0) {
  return std::chrono::duration<double>(Clock::now() - t0).count();
}

double face_area(const TetMesh &m, int e, int a) {
  const Eigen::Vector3d p0 = m.nodes_m.col(m.tets(kTetFaces[a][0], e));
  const Eigen::Vector3d p1 = m.nodes_m.col(m.tets(kTetFaces[a][1], e));
  const Eigen::Vector3d p2 = m.nodes_m.col(m.tets(kTetFaces[a][2], e));
  return 0.5 * (p1 - p0).cross(p2 - p0).norm();
}

std::uint64_t hash_design(const Eigen::VectorXd &x) {
  std::uint64_t h = 1469598103934665603ull;
  for (Eigen::Index i = 0; i < x.size(); ++i) {
    h ^= x[i] > 0.5 ? 0x9e3779b97f4a7c15ull : 0x7f4a7c159e3779b9ull;
    h *= 1099511628211ull;
    h ^= static_cast<std::uint64_t>(i);
    h *= 1099511628211ull;
  }
  return h;
}

std::string interpolation_label(const Interpolation &i) {
  if (i.kind == Interpolation::Kind::Ramp)
    return "ramp(q=" + fmt(i.exponent) + ",floor=" + fmt(i.floor) + ")";
  if (i.exponent == 1.0)
    return "linear(floor=" + fmt(i.floor) + ")";
  return "simp(p=" + fmt(i.exponent) + ",floor=" + fmt(i.floor) + ")";
}

} // namespace

// ── Primitives
// ──────────────────────────────────────────────────────────────
double TanhProjection::value(double r) const {
  if (beta <= 0.0)
    return std::clamp(r, 0.0, 1.0);
  const double a = std::tanh(beta * eta), b = std::tanh(beta * (1.0 - eta));
  return std::clamp((a + std::tanh(beta * (r - eta))) / (a + b), 0.0, 1.0);
}

double TanhProjection::derivative(double r) const {
  if (beta <= 0.0)
    return 1.0;
  const double a = std::tanh(beta * eta), b = std::tanh(beta * (1.0 - eta));
  const double t = std::tanh(beta * (r - eta));
  return beta * (1.0 - t * t) / (a + b);
}

double SmoothHeaviside::value(double phi) const {
  const double s = phi / half_width;
  if (s <= -1.0)
    return 0.0;
  if (s >= 1.0)
    return 1.0;
  const double s2 = s * s;
  return 0.5 + s * (15.0 / 16.0 - s2 * (5.0 / 8.0 - s2 * 3.0 / 16.0));
}

double SmoothHeaviside::derivative(double phi) const {
  const double s = phi / half_width;
  if (s <= -1.0 || s >= 1.0)
    return 0.0;
  const double u = 1.0 - s * s;
  return 15.0 / (16.0 * half_width) * u * u;
}

// ── DensityChain
// ────────────────────────────────────────────────────────────
DensityChain::DensityChain(std::shared_ptr<const Problem> problem)
    : problem_(std::move(problem)) {
  if (!problem_ || !problem_->mesh)
    fail("invalid_input", "density chain needs a prepared problem");
  const Eigen::Index E = problem_->mesh->element_count();
  if (problem_->design.element_variable.size() != E ||
      problem_->filter.weights.rows() != E ||
      problem_->filter.weights.cols() != E)
    fail("invalid_input", "design map / filter do not match the mesh");
  raw_.setZero(E);
  filtered_.setZero(E);
  work_.setZero(E);
}

void DensityChain::set_projection(double beta, double eta) {
  if (!(std::isfinite(beta) && beta >= 0.0))
    fail("invalid_input", "projection beta must be finite and >= 0");
  if (!(eta > 0.0 && eta < 1.0))
    fail("invalid_input", "projection threshold must lie in (0,1)");
  projection_.beta = beta;
  projection_.eta = eta;
}

void DensityChain::forward(const Eigen::VectorXd &x, Eigen::VectorXd &rho) {
  const Problem &p = *problem_;
  p.design.expand(x, raw_);
  filtered_.noalias() = p.filter.weights * raw_;
  // The filter mixes tie groups; restore the tie (volume-weighted group mean)
  // so tied elements carry one physical density.
  p.design.project_ties(filtered_);
  const Eigen::Index E = filtered_.size();
  rho.resize(E);
  for (Eigen::Index e = 0; e < E; ++e)
    rho[e] = p.design.element_variable[e] < 0 ? p.design.fixed_density[e]
                                              : projection_.value(filtered_[e]);
}

void DensityChain::backward(const Eigen::VectorXd &g_rho,
                            Eigen::VectorXd &g_x) {
  const Problem &p = *problem_;
  const Eigen::Index E = filtered_.size();
  if (g_rho.size() != E)
    fail("invalid_density", "density gradient size mismatch");
  for (Eigen::Index e = 0; e < E; ++e)
    work_[e] = p.design.element_variable[e] < 0
                   ? 0.0
                   : g_rho[e] * projection_.derivative(filtered_[e]);
  p.design.project_ties_transpose(work_);
  raw_.noalias() = p.filter.weights.transpose() * work_;
  p.design.reduce_gradient(raw_, g_x);
}

// ── MMA
// ─────────────────────────────────────────────────────────────────────
Mma::Mma(Eigen::VectorXd xmin, Eigen::VectorXd xmax, int constraints,
         MmaSettings settings)
    : m_(constraints), settings_(settings), xmin_(std::move(xmin)),
      xmax_(std::move(xmax)) {
  const Eigen::Index n = xmin_.size();
  if (n < 1 || xmax_.size() != n)
    fail("invalid_mma", "bounds must be non-empty and of equal size");
  if (!xmin_.allFinite() || !xmax_.allFinite() ||
      ((xmax_ - xmin_).array() <= 0.0).any())
    fail("invalid_mma", "bounds must be finite with xmax > xmin");
  if (m_ < 0)
    fail("invalid_mma", "constraint count must be >= 0");
  const MmaSettings &s = settings_;
  if (!(s.initial_asymptote > 0 && s.asymptote_increase >= 1 &&
        s.asymptote_decrease > 0 && s.asymptote_decrease <= 1 &&
        s.constraint_penalty > 0 && s.constraint_quadratic > 0 &&
        s.regularization > 0 && s.bound_fraction > 0 && s.bound_fraction < 1 &&
        s.dual_max_iterations >= 1 && s.dual_tolerance > 0))
    fail("invalid_mma", "MMA settings out of range");
  x1_.setZero(n);
  x2_.setZero(n);
  low_.setZero(n);
  upp_.setZero(n);
  alpha_.setZero(n);
  beta_.setZero(n);
  p0_.setZero(n);
  q0_.setZero(n);
  xs_.setZero(n);
  p_.setZero(m_, n);
  q_.setZero(m_, n);
  r_.setZero(m_);
  lam_.setZero(m_);
  grad_.setZero(m_);
  gcur_.setZero(m_);
  y_.setZero(m_);
  trial_.setZero(m_);
  dir_.setZero(m_);
  a_.setZero(m_);
  rhs_.setZero(m_);
  hess_.setZero(m_, m_);
  free_.reserve(static_cast<std::size_t>(m_));
}

void Mma::restart() { iteration_ = 0; }

MmaState Mma::state() const {
  MmaState s;
  s.iteration = iteration_;
  s.previous1 = x1_;
  s.previous2 = x2_;
  s.lower = low_;
  s.upper = upp_;
  s.multipliers = lam_;
  return s;
}

void Mma::restore(const MmaState &s) {
  const Eigen::Index n = xmin_.size();
  if (s.iteration < 0 || s.previous1.size() != n || s.previous2.size() != n ||
      s.lower.size() != n || s.upper.size() != n ||
      s.multipliers.size() != m_ || !s.previous1.allFinite() ||
      !s.previous2.allFinite() || !s.lower.allFinite() ||
      !s.upper.allFinite() || !s.multipliers.allFinite() ||
      (s.multipliers.array() < 0.0).any())
    fail("invalid_checkpoint", "MMA state does not match the problem");
  iteration_ = s.iteration;
  x1_ = s.previous1;
  x2_ = s.previous2;
  low_ = s.lower;
  upp_ = s.upper;
  lam_ = s.multipliers;
}

// Dual function of the MMA subproblem at multipliers lam (>= 0). Writes the
// primal minimizer xs_, the artificial variables y_, the dual gradient grad_
// and (optionally) the dual Hessian hess_ (negative semidefinite).
double Mma::dual_eval(const Eigen::VectorXd &lam, bool with_hessian) {
  const Eigen::Index n = xmin_.size();
  double W = 0.0;
  grad_.setZero();
  if (with_hessian)
    hess_.setZero();
  for (Eigen::Index j = 0; j < n; ++j) {
    double P = p0_[j], Q = q0_[j];
    if (m_ > 0) {
      P += p_.col(j).dot(lam);
      Q += q_.col(j).dot(lam);
    }
    const double sp = std::sqrt(P), sq = std::sqrt(Q);
    double x = (sp * low_[j] + sq * upp_[j]) / (sp + sq);
    const bool interior = x > alpha_[j] && x < beta_[j];
    x = std::clamp(x, alpha_[j], beta_[j]);
    xs_[j] = x;
    const double ux = upp_[j] - x, xl = x - low_[j];
    W += P / ux + Q / xl;
    if (m_ == 0)
      continue;
    grad_.noalias() += p_.col(j) / ux + q_.col(j) / xl;
    if (with_hessian && interior) {
      const double h = 2.0 * P / (ux * ux * ux) + 2.0 * Q / (xl * xl * xl);
      a_.noalias() = p_.col(j) / (ux * ux) - q_.col(j) / (xl * xl);
      for (int r = 0; r < m_; ++r)
        for (int c = 0; c <= r; ++c)
          hess_(r, c) -= a_[r] * a_[c] / h;
    }
  }
  if (m_ == 0)
    return W;
  const double c = settings_.constraint_penalty,
               d = settings_.constraint_quadratic;
  for (int i = 0; i < m_; ++i) {
    const double y = std::max(0.0, (lam[i] - c) / d);
    y_[i] = y;
    W += c * y + 0.5 * d * y * y - lam[i] * y + lam[i] * r_[i];
    grad_[i] += r_[i] - y;
    if (with_hessian && lam[i] > c)
      hess_(i, i) -= 1.0 / d;
  }
  if (with_hessian)
    for (int r = 0; r < m_; ++r)
      for (int cc = r + 1; cc < m_; ++cc)
        hess_(r, cc) = hess_(cc, r);
  return W;
}

const MmaDualReport &Mma::update(Eigen::VectorXd &x, const Eigen::VectorXd &df0,
                                 const Eigen::VectorXd &g,
                                 const Eigen::MatrixXd &dg, double move) {
  const Eigen::Index n = xmin_.size();
  if (x.size() != n || df0.size() != n || g.size() != m_ || dg.rows() != m_ ||
      dg.cols() != n)
    fail("invalid_mma", "MMA update sizes do not match");
  if (!x.allFinite() || !df0.allFinite() || !g.allFinite() || !dg.allFinite())
    fail("invalid_mma", "MMA update received non-finite values");
  if (!(std::isfinite(move) && move > 0))
    fail("invalid_mma", "MMA move limit must be > 0");
  const MmaSettings &s = settings_;
  for (Eigen::Index j = 0; j < n; ++j) {
    x[j] = std::clamp(x[j], xmin_[j], xmax_[j]);
    const double dx = xmax_[j] - xmin_[j];
    if (iteration_ < 2) {
      low_[j] = x[j] - s.initial_asymptote * dx;
      upp_[j] = x[j] + s.initial_asymptote * dx;
    } else {
      const double z = (x[j] - x1_[j]) * (x1_[j] - x2_[j]);
      const double gamma = z < 0.0   ? s.asymptote_decrease
                           : z > 0.0 ? s.asymptote_increase
                                     : 1.0;
      low_[j] = x[j] - gamma * (x1_[j] - low_[j]);
      upp_[j] = x[j] + gamma * (upp_[j] - x1_[j]);
      low_[j] = std::clamp(low_[j], x[j] - 10.0 * dx, x[j] - 0.01 * dx);
      upp_[j] = std::clamp(upp_[j], x[j] + 0.01 * dx, x[j] + 10.0 * dx);
    }
    alpha_[j] = std::max(
        {xmin_[j], low_[j] + s.bound_fraction * (x[j] - low_[j]), x[j] - move});
    beta_[j] = std::min(
        {xmax_[j], upp_[j] - s.bound_fraction * (upp_[j] - x[j]), x[j] + move});
    const double ux = upp_[j] - x[j], xl = x[j] - low_[j];
    const double ux2 = ux * ux, xl2 = xl * xl, reg = s.regularization / dx;
    const double d0 = df0[j];
    p0_[j] =
        ux2 * (1.001 * std::max(d0, 0.0) + 0.001 * std::max(-d0, 0.0) + reg);
    q0_[j] =
        xl2 * (0.001 * std::max(d0, 0.0) + 1.001 * std::max(-d0, 0.0) + reg);
    for (int i = 0; i < m_; ++i) {
      const double di = dg(i, j);
      p_(i, j) =
          ux2 * (1.001 * std::max(di, 0.0) + 0.001 * std::max(-di, 0.0) + reg);
      q_(i, j) =
          xl2 * (0.001 * std::max(di, 0.0) + 1.001 * std::max(-di, 0.0) + reg);
    }
  }
  // r_i = g_i - approximation at x (so the approximation interpolates g).
  for (int i = 0; i < m_; ++i) {
    double sum = 0.0;
    for (Eigen::Index j = 0; j < n; ++j)
      sum += p_(i, j) / (upp_[j] - x[j]) + q_(i, j) / (x[j] - low_[j]);
    r_[i] = g[i] - sum;
  }

  report_ = MmaDualReport{};
  if (lam_.size() != m_ || !lam_.allFinite())
    lam_.setZero(m_);
  lam_ = lam_.cwiseMax(0.0);
  constexpr double kArmijo = 1e-4;
  for (int it = 0; m_ > 0 && it < s.dual_max_iterations; ++it) {
    const double W = dual_eval(lam_, true);
    gcur_ = grad_;
    double pg = 0.0;
    for (int i = 0; i < m_; ++i) {
      const double v = lam_[i] > 0.0 ? gcur_[i] : std::max(gcur_[i], 0.0);
      pg = std::max(pg, std::abs(v));
    }
    report_.iterations = it;
    report_.projected_gradient = pg;
    if (pg <= s.dual_tolerance) {
      report_.converged = true;
      break;
    }
    // Projected Newton (Bertsekas): variables at the bound with an outward
    // gradient are held; the rest take the Newton step of -H (PSD).
    const double eps_b = std::min(1e-8, pg);
    free_.clear();
    dir_.setZero();
    for (int i = 0; i < m_; ++i) {
      if (lam_[i] <= eps_b && gcur_[i] < 0.0)
        dir_[i] = gcur_[i]; // projected gradient drives it onto the bound
      else
        free_.push_back(i);
    }
    const int k = static_cast<int>(free_.size());
    bool newton = false;
    if (k > 0) {
      sub_.resize(k, k);
      rhs_.resize(k);
      double diag = 0.0;
      for (int r = 0; r < k; ++r) {
        rhs_[r] = gcur_[free_[r]];
        for (int c = 0; c < k; ++c)
          sub_(r, c) = -hess_(free_[r], free_[c]);
        diag = std::max(diag, sub_(r, r));
      }
      sub_.diagonal().array() += 1e-12 * (1.0 + diag);
      Eigen::LDLT<Eigen::MatrixXd> ldlt(sub_);
      if (ldlt.info() == Eigen::Success) {
        const Eigen::VectorXd step = ldlt.solve(rhs_);
        if (step.allFinite()) {
          for (int r = 0; r < k; ++r)
            dir_[free_[r]] = step[r];
          newton = gcur_.dot(dir_) > 0.0;
        }
      }
    }
    if (!newton)
      dir_ = gcur_;
    bool accepted = false;
    for (int attempt = 0; attempt < 2 && !accepted; ++attempt) {
      double t = newton ? 1.0 : 1.0 / (1.0 + dir_.cwiseAbs().maxCoeff());
      for (int ls = 0; ls < 60; ++ls, t *= 0.5) {
        trial_ = (lam_ + t * dir_).cwiseMax(0.0);
        const double slope = gcur_.dot(trial_ - lam_);
        if (!(slope > 0.0))
          continue;
        const double Wt = dual_eval(trial_, false);
        if (Wt >= W + kArmijo * slope) {
          accepted = true;
          break;
        }
      }
      if (!accepted && newton) {
        newton = false; // fall back to projected steepest ascent once
        dir_ = gcur_;
      } else if (!accepted) {
        break;
      }
    }
    if (!accepted)
      break; // numerical precision reached
    lam_ = trial_;
  }
  dual_eval(lam_, false);
  if (m_ > 0) {
    report_.max_artificial = y_.maxCoeff();
    if (!report_.converged) {
      double pg = 0.0;
      for (int i = 0; i < m_; ++i) {
        const double v = lam_[i] > 0.0 ? grad_[i] : std::max(grad_[i], 0.0);
        pg = std::max(pg, std::abs(v));
      }
      report_.projected_gradient = pg;
      report_.converged = pg <= s.dual_tolerance;
    }
  } else {
    report_.converged = true;
  }
  x2_ = x1_;
  x1_ = x;
  x = xs_;
  ++iteration_;
  return report_;
}

// ── Compatibility
// ───────────────────────────────────────────────────────────
namespace {

std::vector<std::string> limits_for(PhysicsKind p) {
  switch (p) {
  case PhysicsKind::Structural:
    return {"stress_limit_mpa", "max_displacement_mm"};
  case PhysicsKind::Thermal:
    return {"max_temperature_c"};
  case PhysicsKind::Modal:
    return {"min_frequency_hz"};
  case PhysicsKind::Buckling:
    return {"min_buckling_factor"};
  case PhysicsKind::Flow:
    return {"max_pressure_drop_pa"};
  case PhysicsKind::Thermomechanical:
    return {"stress_limit_mpa", "max_displacement_mm", "max_temperature_c"};
  case PhysicsKind::Cht:
    return {"max_temperature_c", "max_pressure_drop_pa"};
  }
  return {};
}

const char *set_limit(const Settings &s) {
  if (s.stress_limit_mpa)
    return "stress_limit_mpa";
  if (s.max_displacement_mm)
    return "max_displacement_mm";
  if (s.max_temperature_c)
    return "max_temperature_c";
  if (s.min_frequency_hz)
    return "min_frequency_hz";
  if (s.min_buckling_factor)
    return "min_buckling_factor";
  if (s.max_pressure_drop_pa)
    return "max_pressure_drop_pa";
  return nullptr;
}

bool discrete_method(Method m) {
  return m == Method::Beso || m == Method::LevelSet;
}

const std::vector<PhysicsKind> &all_physics() {
  static const std::vector<PhysicsKind> v = {
      PhysicsKind::Structural, PhysicsKind::Thermal,
      PhysicsKind::Modal,      PhysicsKind::Buckling,
      PhysicsKind::Flow,       PhysicsKind::Thermomechanical,
      PhysicsKind::Cht};
  return v;
}

std::string update_formulation(Method m) {
  switch (m) {
  case Method::Simp:
  case Method::Ramp:
    return "mma(svanberg1987; separable convex subproblem, concave dual by "
           "projected newton; artificial variables c=penalty,d=quadratic)";
  case Method::Beso:
    return "beso(huang_xie soft-kill; filtered sensitivity numbers with "
           "two-step history average; evolutionary volume target; admission "
           "ratio cap; binary removal and reintroduction)";
  case Method::LevelSet:
    return "level_set(reaction-diffusion, yamada fictitious interface energy; "
           "phi in [-1,1]; C2 regularized heaviside; implicit diffusion "
           "factored once; volume multiplier by newton on exact dV/dlambda)";
  }
  return "";
}

} // namespace

Compatibility check_compatibility(const Settings &s) {
  Compatibility c;
  if (!discrete_method(s.method))
    return c;
  const std::string method = to_string(s.method);
  if (s.physics != PhysicsKind::Structural &&
      s.physics != PhysicsKind::Thermal) {
    c.supported = false;
    c.code = "method_physics_unsupported";
    c.message = "method " + method +
                " supports physics structural and thermal; physics " +
                to_string(s.physics) + " needs simp or ramp";
    return c;
  }
  if (s.objective != ObjectiveKind::Compliance &&
      s.objective != ObjectiveKind::ThermalCompliance) {
    c.supported = false;
    c.code = "method_objective_unsupported";
    c.message = "method " + method +
                " supports compliance / thermal_compliance objectives; "
                "objective " +
                to_string(s.objective) + " needs simp or ramp";
    return c;
  }
  if (const char *limit = set_limit(s)) {
    c.supported = false;
    c.code = "method_constraint_unsupported";
    c.message = "method " + method +
                " evolves only the volume budget; response limit " + limit +
                " needs simp or ramp";
    return c;
  }
  return c;
}

json optimizer_capabilities() {
  json methods = json::array();
  for (Method m :
       {Method::Simp, Method::Ramp, Method::Beso, Method::LevelSet}) {
    json physics = json::array();
    for (PhysicsKind p : all_physics()) {
      Settings probe;
      probe.method = m;
      probe.physics = p;
      json objectives = json::array();
      for (ObjectiveKind o : objectives_for(p)) {
        probe.objective = o;
        if (check_compatibility(probe).supported)
          objectives.push_back(to_string(o));
      }
      probe.objective = objectives_for(p).front();
      const Compatibility comp = check_compatibility(probe);
      json row = {
          {"physics", to_string(p)},
          {"supported", !objectives.empty()},
          {"objectives", objectives},
          {"limits", discrete_method(m) ? json::array() : json(limits_for(p))},
          {"volume_phase", to_string(design_phase(p))}};
      if (objectives.empty())
        row["reason"] = comp.message;
      physics.push_back(std::move(row));
    }
    json entry = {{"method", to_string(m)},
                  {"update", update_formulation(m)},
                  {"physics", physics}};
    switch (m) {
    case Method::Simp:
    case Method::Ramp:
      entry["interpolation"] =
          m == Method::Simp ? "simp rho^p (penalty)" : "ramp rho/(1+q(1-rho))";
      entry["projection"] = "tanh threshold, beta continuation to "
                            "projection_beta, eta = density_threshold";
      entry["min_member"] = "density filter radius (intent, not guaranteed)";
      entry["overhang"] = "objective penalty, differentiable heuristic";
      entry["no_enclosed_voids"] = "feasibility_check";
      entry["variable_ties"] = true;
      break;
    case Method::Beso:
      entry["interpolation"] = "simp rho^p soft-kill (penalty)";
      entry["projection"] = nullptr;
      entry["min_member"] = "sensitivity filter radius (intent)";
      entry["overhang"] = "sensitivity-number penalty, heuristic";
      entry["no_enclosed_voids"] = "candidate_acceptance";
      entry["variable_ties"] = true;
      break;
    case Method::LevelSet:
      entry["interpolation"] = "linear ersatz material over H(phi)";
      entry["projection"] = nullptr;
      entry["min_member"] = "sensitivity filter radius + reaction-diffusion "
                            "regularization (intent)";
      entry["overhang"] = "reaction-term penalty, heuristic";
      entry["no_enclosed_voids"] = "candidate_acceptance";
      entry["variable_ties"] = true;
      break;
    }
    methods.push_back(std::move(entry));
  }
  json limits = json::object();
  for (const char *key :
       {"stress_limit_mpa", "max_displacement_mm", "max_temperature_c",
        "min_frequency_hz", "min_buckling_factor", "max_pressure_drop_pa"}) {
    json physics = json::array();
    for (PhysicsKind p : all_physics()) {
      const auto l = limits_for(p);
      if (std::find(l.begin(), l.end(), key) != l.end())
        physics.push_back(to_string(p));
    }
    limits[key] = {{"physics", physics},
                   {"methods", json::array({"simp", "ramp"})}};
  }
  return {{"schema", "chudware.topology.optimizer_capabilities/1"},
          {"methods", methods},
          {"limits", limits},
          {"terminations",
           {"converged", "max_iterations", "stalled", "line_search_failed",
            "infeasible", "stuck_at_bounds", "cancelled", "failed"}}};
}

// ── Termination / JSON
// ──────────────────────────────────────────────────────
const char *to_string(Termination t) {
  switch (t) {
  case Termination::Converged:
    return "converged";
  case Termination::MaxIterations:
    return "max_iterations";
  case Termination::Stalled:
    return "stalled";
  case Termination::LineSearchFailed:
    return "line_search_failed";
  case Termination::Infeasible:
    return "infeasible";
  case Termination::StuckAtBounds:
    return "stuck_at_bounds";
  case Termination::Cancelled:
    return "cancelled";
  case Termination::Failed:
    return "failed";
  }
  return "failed";
}

namespace {

json vec_json(const Eigen::VectorXd &v) {
  json a = json::array();
  for (Eigen::Index i = 0; i < v.size(); ++i)
    a.push_back(v[i]);
  return a;
}

Eigen::VectorXd vec_from(const json &j, const char *key) {
  if (!j.contains(key) || !j[key].is_array())
    fail("invalid_checkpoint",
         std::string("checkpoint.") + key + " must be an array of numbers");
  const json &a = j[key];
  Eigen::VectorXd v(static_cast<Eigen::Index>(a.size()));
  for (std::size_t i = 0; i < a.size(); ++i) {
    if (!a[i].is_number())
      fail("invalid_checkpoint",
           std::string("checkpoint.") + key + " must hold numbers");
    v[static_cast<Eigen::Index>(i)] = a[i].get<double>();
  }
  if (!v.allFinite())
    fail("invalid_checkpoint",
         std::string("checkpoint.") + key + " holds non-finite values");
  return v;
}

template <class T> T get_as(const json &j, const char *key) {
  if (!j.contains(key))
    fail("invalid_checkpoint", std::string("checkpoint.") + key + " missing");
  try {
    return j.at(key).get<T>();
  } catch (const std::exception &) {
    fail("invalid_checkpoint",
         std::string("checkpoint.") + key + " has the wrong type");
  }
}

IterationRecord record_from_json(const json &j) {
  IterationRecord r;
  r.iteration = get_as<int>(j, "iteration");
  r.objective = get_as<double>(j, "objective");
  r.objective_scaled = get_as<double>(j, "objective_scaled");
  r.overhang = get_as<double>(j, "overhang");
  r.merit = get_as<double>(j, "merit");
  r.volume_fraction = get_as<double>(j, "volume_fraction");
  r.volume_target = get_as<double>(j, "volume_target");
  r.constraints = get_as<std::vector<double>>(j, "constraints");
  r.max_constraint = get_as<double>(j, "max_constraint");
  r.feasible = get_as<bool>(j, "feasible");
  r.change = get_as<double>(j, "change");
  r.beta = get_as<double>(j, "beta");
  r.grayness = get_as<double>(j, "grayness");
  r.added = get_as<int>(j, "added");
  r.removed = get_as<int>(j, "removed");
  r.rejected = get_as<int>(j, "rejected");
  r.line_search_trials = get_as<int>(j, "line_search_trials");
  r.enclosed_void_elements = get_as<int>(j, "enclosed_void_elements");
  r.interface_area_m2 = get_as<double>(j, "interface_area_m2");
  r.dual_gradient = get_as<double>(j, "dual_gradient");
  r.seconds = get_as<double>(j, "seconds");
  return r;
}

} // namespace

json to_json(const IterationRecord &r) {
  return {{"iteration", r.iteration},
          {"objective", r.objective},
          {"objective_scaled", r.objective_scaled},
          {"overhang", r.overhang},
          {"merit", r.merit},
          {"volume_fraction", r.volume_fraction},
          {"volume_target", r.volume_target},
          {"constraints", r.constraints},
          {"max_constraint", r.max_constraint},
          {"feasible", r.feasible},
          {"change", r.change},
          {"beta", r.beta},
          {"grayness", r.grayness},
          {"added", r.added},
          {"removed", r.removed},
          {"rejected", r.rejected},
          {"line_search_trials", r.line_search_trials},
          {"enclosed_void_elements", r.enclosed_void_elements},
          {"interface_area_m2", r.interface_area_m2},
          {"dual_gradient", r.dual_gradient},
          {"seconds", r.seconds}};
}

json to_json(const OptimizerState &s) {
  json history = json::array();
  for (const auto &r : s.history)
    history.push_back(to_json(r));
  return {{"schema", "chudware.topology.optimizer_state/1"},
          {"method", to_string(s.method)},
          {"physics", to_string(s.physics)},
          {"iteration", s.iteration},
          {"evaluations", s.evaluations},
          {"design", vec_json(s.design)},
          {"mma",
           {{"iteration", s.mma.iteration},
            {"previous1", vec_json(s.mma.previous1)},
            {"previous2", vec_json(s.mma.previous2)},
            {"lower", vec_json(s.mma.lower)},
            {"upper", vec_json(s.mma.upper)},
            {"multipliers", vec_json(s.mma.multipliers)}}},
          {"beta", s.beta},
          {"beta_stage_start", s.beta_stage_start},
          {"last_change", s.last_change},
          {"stationary_count", s.stationary_count},
          {"beso_history", vec_json(s.beso_history)},
          {"design_hashes", s.design_hashes},
          {"cycle_count", s.cycle_count},
          {"volume_target", s.volume_target},
          {"reaction_gain", s.reaction_gain},
          {"has_scale", s.has_scale},
          {"objective_scale", s.objective_scale},
          {"baseline_objective", s.baseline_objective},
          {"best_merit", s.best_merit},
          {"best_iteration", s.best_iteration},
          {"rejected_total", s.rejected_total},
          {"constraint_names", s.constraint_names},
          {"history", history}};
}

OptimizerState optimizer_state_from_json(const json &j) {
  if (!j.is_object())
    fail("invalid_checkpoint", "checkpoint must be an object");
  if (j.value("schema", std::string()) != "chudware.topology.optimizer_state/1")
    fail("invalid_checkpoint", "checkpoint schema is not "
                               "chudware.topology.optimizer_state/1");
  OptimizerState s;
  try {
    s.method = parse_method(get_as<std::string>(j, "method"));
    s.physics = parse_physics(get_as<std::string>(j, "physics"));
  } catch (const ModelError &e) {
    fail("invalid_checkpoint", e.what());
  }
  s.iteration = get_as<int>(j, "iteration");
  s.evaluations = get_as<int>(j, "evaluations");
  s.design = vec_from(j, "design");
  if (!j.contains("mma") || !j["mma"].is_object())
    fail("invalid_checkpoint", "checkpoint.mma must be an object");
  const json &mma = j["mma"];
  s.mma.iteration = get_as<int>(mma, "iteration");
  s.mma.previous1 = vec_from(mma, "previous1");
  s.mma.previous2 = vec_from(mma, "previous2");
  s.mma.lower = vec_from(mma, "lower");
  s.mma.upper = vec_from(mma, "upper");
  s.mma.multipliers = vec_from(mma, "multipliers");
  s.beta = get_as<double>(j, "beta");
  s.beta_stage_start = get_as<int>(j, "beta_stage_start");
  s.last_change = get_as<double>(j, "last_change");
  s.stationary_count = get_as<int>(j, "stationary_count");
  s.beso_history = vec_from(j, "beso_history");
  s.design_hashes = get_as<std::vector<std::uint64_t>>(j, "design_hashes");
  s.cycle_count = get_as<int>(j, "cycle_count");
  s.volume_target = get_as<double>(j, "volume_target");
  s.reaction_gain = get_as<double>(j, "reaction_gain");
  s.has_scale = get_as<bool>(j, "has_scale");
  s.objective_scale = get_as<double>(j, "objective_scale");
  s.baseline_objective = get_as<double>(j, "baseline_objective");
  s.best_merit = get_as<double>(j, "best_merit");
  s.best_iteration = get_as<int>(j, "best_iteration");
  s.rejected_total = get_as<int>(j, "rejected_total");
  s.constraint_names = get_as<std::vector<std::string>>(j, "constraint_names");
  if (!j.contains("history") || !j["history"].is_array())
    fail("invalid_checkpoint", "checkpoint.history must be an array");
  for (const auto &r : j["history"])
    s.history.push_back(record_from_json(r));
  if (s.iteration != static_cast<int>(s.history.size()))
    fail("invalid_checkpoint", "checkpoint iteration count does not match "
                               "its history");
  return s;
}

// ── Runner
// ──────────────────────────────────────────────────────────────────
namespace {

void validate_options(const OptimizerOptions &o) {
  auto need = [](bool ok, const std::string &what) {
    if (!ok)
      fail("invalid_optimizer_option", what);
  };
  need(o.checkpoint_interval >= 0, "checkpoint_interval must be >= 0");
  need(o.feasibility_tolerance > 0 && o.feasibility_tolerance <= 0.1,
       "feasibility_tolerance must lie in (0,0.1]");
  need(o.convergence_window >= 1, "convergence_window must be >= 1");
  need(o.infeasible_window >= o.convergence_window,
       "infeasible_window must be >= convergence_window");
  need(o.stall_window >= 2, "stall_window must be >= 2");
  need(o.stall_improvement >= 0 && std::isfinite(o.stall_improvement),
       "stall_improvement must be finite and >= 0");
  need(o.projection_continuation_interval >= 1,
       "projection_continuation_interval must be >= 1");
  need(o.beso_max_addition_ratio > 0 && o.beso_max_addition_ratio <= 1,
       "beso_max_addition_ratio must lie in (0,1]");
  need(o.beso_convergence_window >= 1, "beso_convergence_window must be >= 1");
  need(o.level_set_time_step > 0 && std::isfinite(o.level_set_time_step),
       "level_set_time_step must be > 0");
  need(o.level_set_band > 0 && o.level_set_band <= 1,
       "level_set_band must lie in (0,1]");
  need(o.level_set_volume_step > 0 && o.level_set_volume_step <= 1,
       "level_set_volume_step must lie in (0,1]");
  need(o.level_set_increase_tolerance >= 0 &&
           std::isfinite(o.level_set_increase_tolerance),
       "level_set_increase_tolerance must be finite and >= 0");
  need(o.max_line_search >= 0 && o.max_line_search <= 30,
       "max_line_search must lie in [0,30]");
  need(o.max_enclosure_passes >= 1 && o.max_enclosure_passes <= 64,
       "max_enclosure_passes must lie in [1,64]");
}

void check_interpolation(const Problem &p) {
  const Settings &s = p.settings;
  const Interpolation &i = p.interpolation;
  bool ok = true;
  switch (s.method) {
  case Method::Simp:
  case Method::Beso:
    ok = i.kind == Interpolation::Kind::Simp && i.exponent == s.penalty;
    break;
  case Method::Ramp:
    ok = i.kind == Interpolation::Kind::Ramp && i.exponent == s.ramp_q;
    break;
  case Method::LevelSet:
    ok = i.kind == Interpolation::Kind::Simp && i.exponent == 1.0;
    break;
  }
  if (!ok)
    fail("interpolation_mismatch",
         std::string("problem interpolation ") + interpolation_label(i) +
             " does not match method " + to_string(s.method) +
             " (interpolation_for(settings))");
}

class Runner {
public:
  Runner(std::shared_ptr<const Problem> problem, Physics &physics,
         const OptimizerOptions &options)
      : pp_(std::move(problem)), P_(*pp_), M_(*P_.mesh), S_(P_.settings),
        phys_(physics), opt_(options), voids_(P_.mesh) {
    E_ = M_.element_count();
    NV_ = P_.design.variable_count;
    sense_ =
        objective_sense(S_.objective) == ObjectiveSense::Minimize ? 1.0 : -1.0;
    phase_ = design_phase(S_.physics);
    threshold_ = S_.density_threshold;
    // Volume constraint: value refreshed per iteration, gradient constant.
    Eigen::VectorXd probe = Eigen::VectorXd::Constant(E_, 0.5);
    vol_ = volume_constraint(P_, probe, true);
    if (S_.build_direction) {
      OverhangSettings os = opt_.overhang;
      os.build_direction = *S_.build_direction;
      os.overhang_angle_deg = S_.overhang_angle_deg;
      overhang_ = OverhangModel(M_, os);
      overhang_on_ = true;
      overhang_w_ = S_.overhang_penalty;
    }
    st_.method = S_.method;
    st_.physics = S_.physics;
    st_.best_merit = kDoubleMax;
    st_.last_change = 1.0;
    if (opt_.resume) {
      st_ = *opt_.resume;
      validate_resume();
    }
    res_.method = S_.method;
    res_.physics = S_.physics;
    res_.phase = phase_;
    res_.sense = objective_sense(S_.objective);
    res_.update_formulation = update_formulation(S_.method);
    res_.physics_formulation = phys_.formulation();
    res_.interpolation = interpolation_label(P_.interpolation);
    res_.volume_limit = S_.volume_fraction;
    res_.termination = Termination::Failed;
  }

  OptimizationResult run() {
    try {
      switch (S_.method) {
      case Method::Simp:
      case Method::Ramp:
        run_density();
        break;
      case Method::Beso:
        run_beso();
        break;
      case Method::LevelSet:
        run_level_set();
        break;
      }
    } catch (const EvaluationCancelled &) {
      set_stop(Termination::Cancelled, "cancelled during iteration " +
                                           std::to_string(st_.iteration) +
                                           "; last completed iterate retained");
    } catch (const ModelError &e) {
      set_stop(Termination::Failed, e.what(), e.code());
    } catch (const std::exception &e) {
      set_stop(Termination::Failed, e.what(), "optimizer_exception");
    }
    finish();
    return std::move(res_);
  }

private:
  // ── shared helpers ────────────────────────────────────────────────────
  void validate_resume() {
    const OptimizerState &s = st_;
    if (s.method != S_.method || s.physics != S_.physics)
      fail("invalid_checkpoint", "checkpoint method/physics differ from the "
                                 "problem settings");
    if (s.design.size() != NV_)
      fail("invalid_checkpoint",
           "checkpoint design has " + std::to_string(s.design.size()) +
               " variables, problem has " + std::to_string(NV_));
    if (s.iteration < 1 || s.iteration != static_cast<int>(s.history.size()) ||
        s.iteration >= S_.max_iterations)
      fail("invalid_checkpoint", "checkpoint iteration count is inconsistent "
                                 "or already at max_iterations");
    if (!s.has_scale || !(s.objective_scale > 0) ||
        !std::isfinite(s.objective_scale))
      fail("invalid_checkpoint", "checkpoint has no objective scale");
    if (s.beso_history.size() != 0 && s.beso_history.size() != E_)
      fail("invalid_checkpoint", "checkpoint beso history size mismatch");
    const double lo = S_.method == Method::LevelSet ? -1.0 : 0.0;
    if (!s.design.allFinite() || (s.design.array() < lo).any() ||
        (s.design.array() > 1.0).any())
      fail("invalid_checkpoint", "checkpoint design outside its bounds");
  }

  void set_stop(Termination t, const std::string &message,
                const std::string &code = "") {
    res_.termination = t;
    res_.message = message;
    res_.error_code = code;
  }

  bool cancel_requested() const {
    return opt_.cancel && opt_.cancel->load(std::memory_order_relaxed);
  }

  Evaluation evaluate(const Eigen::VectorXd &rho, bool derivatives) {
    if (cancel_requested())
      throw EvaluationCancelled();
    ++st_.evaluations;
    Evaluation ev = phys_.evaluate(rho, derivatives, opt_.cancel);
    if (!std::isfinite(ev.objective))
      fail("non_finite_response", "physics returned a non-finite objective");
    if (derivatives && (ev.objective_gradient.size() != E_ ||
                        !ev.objective_gradient.allFinite()))
      fail("missing_gradient", "physics objective gradient is missing or "
                               "non-finite");
    for (const auto &c : ev.constraints) {
      if (!std::isfinite(c.value))
        fail("non_finite_response", "constraint " + c.name + " is non-finite");
      if (derivatives && (c.gradient.size() != E_ || !c.gradient.allFinite()))
        fail("missing_gradient",
             "constraint " + c.name + " gradient is missing or non-finite");
    }
    return ev;
  }

  // Builds cons_ (physics constraints + volume) and checks the set.
  void bind_constraints(const Evaluation &ev) {
    cons_.clear();
    bool physics_volume = false;
    for (const auto &c : ev.constraints) {
      if (c.name == vol_.name)
        physics_volume = true;
      else if (discrete_method(S_.method))
        fail("method_constraint_unsupported",
             std::string("method ") + to_string(S_.method) +
                 " evolves only the volume budget but the physics returned "
                 "constraint " +
                 c.name);
      cons_.push_back(&c);
    }
    if (!physics_volume)
      cons_.push_back(&vol_);
    if (static_cast<int>(cons_.size()) > kMaxConstraints)
      fail("too_many_constraints", std::to_string(cons_.size()) +
                                       " constraints exceed the dense MMA "
                                       "dual bound of " +
                                       std::to_string(kMaxConstraints));
    if (st_.constraint_names.empty()) {
      for (const Constraint *c : cons_)
        st_.constraint_names.push_back(c->name);
      return;
    }
    bool same = st_.constraint_names.size() == cons_.size();
    for (std::size_t i = 0; same && i < cons_.size(); ++i)
      same = st_.constraint_names[i] == cons_[i]->name;
    if (!same)
      fail("constraint_set_changed",
           "the physics constraint set changed between evaluations");
  }

  bool is_volume(const Constraint *c) const { return c->name == vol_.name; }

  void update_volume(const Eigen::VectorXd &rho) {
    vol_.response = phase_fraction(P_, rho);
    vol_.value = vol_.response / vol_.limit - 1.0;
  }

  void init_scale(const Evaluation &ev) {
    if (st_.has_scale)
      return;
    const double f = std::abs(ev.objective);
    st_.objective_scale = f > 1e-300 && std::isfinite(f) ? f : 1.0;
    st_.baseline_objective = ev.objective;
    st_.has_scale = true;
    res_.has_baseline = true;
    res_.baseline = ev;
  }

  double overhang_value(const Eigen::VectorXd &rho, bool gradient) {
    if (!overhang_on_)
      return 0.0;
    return overhang_.measure(rho, gradient ? &oh_grad_ : nullptr);
  }

  // dJ/drho of the scaled objective plus overhang penalty, into g.
  void scaled_objective_gradient(const Evaluation &ev, Eigen::VectorXd &g) {
    g = (sense_ / st_.objective_scale) * ev.objective_gradient;
    if (overhang_on_ && overhang_w_ > 0)
      g.noalias() += overhang_w_ * oh_grad_;
  }

  double scaled(const Evaluation &ev) const {
    return sense_ * ev.objective / st_.objective_scale;
  }

  IterationRecord make_record(const Eigen::VectorXd &rho, const Evaluation &ev,
                              double overhang, double change,
                              double volume_target) {
    IterationRecord r;
    r.iteration = st_.iteration;
    r.objective = ev.objective;
    r.objective_scaled = scaled(ev);
    r.overhang = overhang;
    update_volume(rho);
    r.volume_fraction = vol_.response;
    r.volume_target = volume_target;
    r.constraints.reserve(cons_.size());
    double violation = 0.0, gmax = cons_.empty() ? 0.0 : -kDoubleMax;
    bool feasible = true;
    for (const Constraint *c : cons_) {
      r.constraints.push_back(c->value);
      gmax = std::max(gmax, c->value);
      violation += std::max(0.0, c->value);
      const double allowed =
          opt_.feasibility_tolerance + (is_volume(c) ? volume_slack_ : 0.0);
      if (c->value > allowed)
        feasible = false;
    }
    r.max_constraint = gmax;
    const EnclosedVoids &enc = voids_.find(rho, threshold_);
    r.enclosed_void_elements = enc.enclosed_elements;
    if (S_.no_enclosed_voids && enc.enclosed_elements > 0)
      feasible = false;
    r.feasible = feasible;
    r.merit = r.objective_scaled + overhang_w_ * overhang +
              opt_.mma.constraint_penalty * violation;
    r.change = change;
    double gray = 0.0, dvol = 0.0, area = 0.0;
    for (int e = 0; e < E_; ++e) {
      if (P_.design.element_variable[e] >= 0) {
        gray += M_.volume_m3[e] * 4.0 * rho[e] * (1.0 - rho[e]);
        dvol += M_.volume_m3[e];
      }
      for (int a = 0; a < 4; ++a) {
        const int f = M_.face_neighbor(a, e);
        if (f > e && (rho[e] >= threshold_) != (rho[f] >= threshold_))
          area += face_area(M_, e, a);
      }
    }
    r.grayness = dvol > 0 ? gray / dvol : 0.0;
    r.interface_area_m2 = area;
    return r;
  }

  // Stall bookkeeping on the merit of the record just pushed.
  bool stall_check(const IterationRecord &r) {
    if (r.merit <
        st_.best_merit -
            opt_.stall_improvement * std::max(1.0, std::abs(st_.best_merit))) {
      st_.best_merit = r.merit;
      st_.best_iteration = r.iteration;
    }
    return r.iteration - st_.best_iteration >= opt_.stall_window;
  }

  void reset_stall() {
    st_.best_merit = kDoubleMax;
    st_.best_iteration = st_.iteration;
  }

  void push_record(IterationRecord rec, Clock::time_point t0) {
    rec.seconds = seconds_since(t0);
    st_.history.push_back(std::move(rec));
    ++st_.iteration;
  }

  bool checkpoint_due(bool last) const {
    return last || (opt_.checkpoint_interval > 0 &&
                    st_.iteration % opt_.checkpoint_interval == 0);
  }

  void notify(bool last) {
    if (!opt_.progress)
      return;
    Progress p{st_.history.back(),   st_, rho_eval_, ev_eval_,
               checkpoint_due(last), last};
    try {
      opt_.progress(p);
    } catch (const std::exception &e) {
      fail("callback_failed",
           std::string("progress callback threw: ") + e.what());
    }
  }

  void commit(Eigen::VectorXd &rho, Evaluation &&ev) {
    rho_eval_.swap(rho);
    ev_eval_ = std::move(ev);
    have_eval_ = true;
    // cons_ points into the moved vector's buffer; rebuild to be explicit.
    bind_constraints(ev_eval_);
  }

  // ── simp / ramp ───────────────────────────────────────────────────────
  void run_density() {
    DensityChain chain(pp_);
    const double eta = S_.density_threshold;
    const double beta_target = S_.projection_beta;
    const double tol = S_.change_tolerance;
    res_.volume_tolerance = opt_.feasibility_tolerance;
    Eigen::VectorXd &x = st_.design;
    if (!opt_.resume) {
      st_.beta = beta_target > 0 ? std::min(1.0, beta_target) : 0.0;
      st_.beta_stage_start = 0;
      chain.set_projection(st_.beta, eta);
      initial_density_design(chain, x);
    }
    chain.set_projection(st_.beta, eta);
    criterion_ = "max |x_k - x_{k-1}| < change_tolerance=" + fmt(tol) +
                 " for " + std::to_string(opt_.convergence_window) +
                 " consecutive iterations at the final projection beta=" +
                 fmt(beta_target) +
                 ", all g <= " + fmt(opt_.feasibility_tolerance) +
                 (S_.no_enclosed_voids ? ", no enclosed voids" : "");
    Mma mma;
    bool mma_ready = false;
    Eigen::VectorXd rho, grho, df0, gx, gvec, x_prev;
    Eigen::MatrixXd dg;
    for (;;) {
      const auto t0 = Clock::now();
      chain.forward(x, rho);
      Evaluation ev = evaluate(rho, true);
      bind_constraints(ev);
      init_scale(ev);
      const int m = static_cast<int>(cons_.size());
      if (!mma_ready) {
        admit_or_refuse(static_cast<std::uint64_t>(NV_) * 8ull *
                                static_cast<std::uint64_t>(5 * m + 16) +
                            static_cast<std::uint64_t>(E_) * 64ull,
                        "topology MMA update");
        mma = Mma(Eigen::VectorXd::Zero(NV_), Eigen::VectorXd::Ones(NV_), m,
                  opt_.mma);
        if (opt_.resume && st_.mma.previous1.size() == NV_)
          mma.restore(st_.mma);
        dg.resize(m, NV_);
        gvec.resize(m);
        mma_ready = true;
      }
      update_volume(rho);
      const double O = overhang_value(rho, overhang_w_ > 0);
      scaled_objective_gradient(ev, grho);
      chain.backward(grho, df0);
      for (int i = 0; i < m; ++i) {
        chain.backward(cons_[i]->gradient, gx);
        dg.row(i) = gx.transpose();
        gvec[i] = cons_[i]->value;
      }
      IterationRecord rec =
          make_record(rho, ev, O, st_.last_change, S_.volume_fraction);
      rec.beta = st_.beta;
      rec.dual_gradient = mma.last_dual().projected_gradient;
      const bool feasible = rec.feasible;
      commit(rho, std::move(ev));
      push_record(std::move(rec), t0);

      const bool final_beta = beta_target <= 0 || st_.beta >= beta_target;
      const bool stationary = st_.last_change < tol && final_beta;
      st_.stationary_count = stationary ? st_.stationary_count + 1 : 0;
      const bool stall = stall_check(st_.history.back());
      bool done = true;
      if (st_.stationary_count >= opt_.convergence_window && feasible) {
        set_stop(Termination::Converged,
                 "design change " + fmt(st_.last_change) + " < " + fmt(tol) +
                     " with all constraints satisfied");
      } else if (st_.stationary_count >= opt_.infeasible_window && !feasible) {
        bool pinned = true;
        for (Eigen::Index v = 0; v < x.size() && pinned; ++v)
          pinned = x[v] <= 1e-6 || x[v] >= 1.0 - 1e-6;
        set_stop(
            pinned ? Termination::StuckAtBounds : Termination::Infeasible,
            std::string(pinned ? "every variable is pinned at a bound"
                               : "the design is stationary") +
                " while max g = " + fmt(st_.history.back().max_constraint) +
                (st_.history.back().enclosed_void_elements > 0
                     ? " / enclosed void elements = " +
                           std::to_string(
                               st_.history.back().enclosed_void_elements)
                     : std::string()));
      } else if (stall && final_beta) {
        set_stop(Termination::Stalled, "no merit improvement over " +
                                           std::to_string(opt_.stall_window) +
                                           " iterations");
      } else if (st_.iteration >= S_.max_iterations) {
        set_stop(Termination::MaxIterations,
                 "reached max_iterations=" + std::to_string(S_.max_iterations));
      } else {
        done = false;
      }
      if (done) {
        st_.mma = mma.state();
        notify(true);
        return;
      }

      x_prev = x;
      mma.update(x, df0, gvec, dg, S_.move_limit);
      st_.last_change = (x - x_prev).cwiseAbs().maxCoeff();
      if (beta_target > 0 && st_.beta < beta_target) {
        const bool due = st_.iteration - st_.beta_stage_start >=
                             opt_.projection_continuation_interval ||
                         (st_.last_change < tol && feasible) || stall;
        if (due) {
          st_.beta = std::min(2.0 * st_.beta, beta_target);
          st_.beta_stage_start = st_.iteration;
          chain.set_projection(st_.beta, eta);
          mma.restart();
          st_.stationary_count = 0;
          reset_stall();
        }
      }
      if (checkpoint_due(false))
        st_.mma = mma.state();
      notify(false);
    }
  }

  // Uniform start meeting the volume budget with the initial projection.
  void initial_density_design(DensityChain &chain, Eigen::VectorXd &x) {
    Eigen::VectorXd rho;
    x.resize(NV_);
    auto excess = [&](double t) {
      x.setConstant(t);
      chain.forward(x, rho);
      return phase_fraction(P_, rho) - S_.volume_fraction;
    };
    const bool solid = phase_ == DesignPhase::Solid;
    // Solid: excess increases with t, take the largest feasible t.
    // Fluid: excess decreases with t, take the smallest feasible t.
    double feasible_t = solid ? 1.0 : 0.0;
    if (excess(feasible_t) > 0.0) {
      double lo = 0.0, hi = 1.0;
      for (int it = 0; it < 60; ++it) {
        const double mid = 0.5 * (lo + hi);
        const bool ok = excess(mid) <= 0.0;
        if (solid == ok)
          lo = mid;
        else
          hi = mid;
      }
      feasible_t = solid ? lo : hi;
    }
    x.setConstant(feasible_t);
  }

  // ── BESO ─────────────────────────────────────────────────────────────
  double passive_solid_volume() const {
    double v = 0.0;
    for (int e = 0; e < E_; ++e)
      if (P_.design.element_variable[e] < 0)
        v += M_.volume_m3[e] * P_.design.fixed_density[e];
    return v;
  }

  // Design-variable solid volume that realizes phase fraction T.
  double design_solid_target(double T) const {
    const double solid_fraction = phase_ == DesignPhase::Solid ? T : 1.0 - T;
    return solid_fraction * M_.total_volume_m3 - passive_solid_;
  }

  void run_beso() {
    Eigen::VectorXd &x = st_.design;
    if (!opt_.resume)
      x.setOnes(NV_);
    for (Eigen::Index v = 0; v < x.size(); ++v)
      if (x[v] != 0.0 && x[v] != 1.0)
        fail("invalid_checkpoint", "beso design variables must be 0 or 1");
    const Eigen::VectorXd &Vv = P_.design.variable_volume_m3;
    passive_solid_ = passive_solid_volume();
    volume_slack_ = Vv.maxCoeff() / M_.total_volume_m3 / S_.volume_fraction;
    res_.volume_tolerance = volume_slack_ + opt_.feasibility_tolerance;
    const int N = opt_.beso_convergence_window;
    const double Vstar = S_.volume_fraction;
    const double ER = S_.beso_evolution_rate;
    criterion_ =
        "volume target reached (|V - V*| <= one variable volume) and "
        "|sum_{i<N} f_{k-i} - sum_{i<N} f_{k-N-i}| / |sum_{i<N} f_{k-i}| <= " +
        fmt(S_.change_tolerance) + " with N=" + std::to_string(N) +
        (S_.no_enclosed_voids ? ", no enclosed voids" : "");
    Eigen::VectorXd rho(E_), cand_rho(E_), sens(E_), filt(E_), grho(E_),
        score(NV_);
    std::vector<int> order(static_cast<std::size_t>(NV_));
    std::vector<std::uint8_t> cand(NV_), forced_solid(NV_), forced_void(NV_);
    if (!opt_.resume) {
      P_.design.expand(x, rho);
      st_.volume_target = phase_fraction(P_, rho);
    }
    int last_added = 0, last_removed = 0, last_rejected = 0;
    for (;;) {
      const auto t0 = Clock::now();
      P_.design.expand(x, rho);
      Evaluation ev = evaluate(rho, true);
      bind_constraints(ev);
      init_scale(ev);
      const double O = overhang_value(rho, overhang_w_ > 0);
      scaled_objective_gradient(ev, grho);
      // Sensitivity number: keep benefit per unit volume.
      for (int e = 0; e < E_; ++e)
        sens[e] = -grho[e] / M_.volume_m3[e];
      filt.noalias() = P_.filter.weights * sens;
      if (st_.beso_history.size() != E_)
        st_.beso_history = filt;
      else
        st_.beso_history = 0.5 * (filt + st_.beso_history);
      IterationRecord rec =
          make_record(rho, ev, O, st_.last_change, st_.volume_target);
      rec.added = last_added;
      rec.removed = last_removed;
      rec.rejected = last_rejected;
      const bool feasible = rec.feasible;
      const double frac = rec.volume_fraction;
      commit(rho, std::move(ev));
      push_record(std::move(rec), t0);

      const bool at_target =
          st_.volume_target == Vstar &&
          std::abs(frac - Vstar) <= volume_slack_ * Vstar + 1e-12;
      bool criterion = false;
      const int K = static_cast<int>(st_.history.size());
      if (at_target && K >= 2 * N) {
        bool settled = true;
        double recent = 0.0, earlier = 0.0;
        for (int i = 0; i < N; ++i) {
          const auto &a = st_.history[K - 1 - i];
          const auto &b = st_.history[K - 1 - N - i];
          settled =
              settled && a.volume_target == Vstar && b.volume_target == Vstar;
          recent += a.objective;
          earlier += b.objective;
        }
        criterion = settled && std::abs(recent) > 0 &&
                    std::abs(recent - earlier) / std::abs(recent) <=
                        S_.change_tolerance;
      }
      const std::uint64_t h = hash_design(x);
      const bool repeat =
          std::find(st_.design_hashes.begin(), st_.design_hashes.end(), h) !=
          st_.design_hashes.end();
      st_.cycle_count =
          at_target && repeat && !criterion ? st_.cycle_count + 1 : 0;
      st_.design_hashes.push_back(h);
      if (static_cast<int>(st_.design_hashes.size()) > 2 * N)
        st_.design_hashes.erase(st_.design_hashes.begin());
      bool done = true;
      if (criterion && feasible) {
        set_stop(Termination::Converged,
                 "objective history settled at the target volume");
      } else if (criterion && !feasible) {
        set_stop(Termination::Infeasible,
                 "objective settled but the design violates a constraint "
                 "(enclosed void elements = " +
                     std::to_string(st_.history.back().enclosed_void_elements) +
                     ")");
      } else if (st_.cycle_count >= 2 * N) {
        set_stop(Termination::Stalled,
                 "binary design cycles between previously visited states");
      } else if (st_.iteration >= S_.max_iterations) {
        set_stop(Termination::MaxIterations,
                 "reached max_iterations=" + std::to_string(S_.max_iterations));
      } else {
        done = false;
      }
      if (done) {
        notify(true);
        return;
      }

      // Evolutionary target and candidate selection.
      const double T = st_.volume_target;
      const double T_next = T > Vstar   ? std::max(Vstar, T * (1.0 - ER))
                            : T < Vstar ? std::min(Vstar, T * (1.0 + ER))
                                        : Vstar;
      for (int v = 0; v < NV_; ++v) {
        double s = 0.0;
        for (int k = P_.design.variable_offsets[v];
             k < P_.design.variable_offsets[v + 1]; ++k) {
          const int e = P_.design.variable_elements[k];
          s += M_.volume_m3[e] * st_.beso_history[e];
        }
        score[v] = s / Vv[v];
      }
      std::iota(order.begin(), order.end(), 0);
      std::sort(order.begin(), order.end(), [&](int a, int b) {
        if (score[a] != score[b])
          return score[a] > score[b];
        if (x[a] != x[b])
          return x[a] > x[b]; // ties keep the current state
        return a < b;
      });
      const double D = design_solid_target(T_next);
      const int cap = std::max(
          1, static_cast<int>(std::floor(opt_.beso_max_addition_ratio * NV_)));
      std::fill(forced_solid.begin(), forced_solid.end(), 0);
      std::fill(forced_void.begin(), forced_void.end(), 0);
      int rejected = 0;
      for (int pass = 0; pass < opt_.max_enclosure_passes; ++pass) {
        std::fill(cand.begin(), cand.end(), 0);
        double vol = 0.0;
        for (int v = 0; v < NV_; ++v)
          if (forced_solid[v]) {
            cand[v] = 1;
            vol += Vv[v];
          }
        int added = 0;
        for (int v : order) {
          if (cand[v] || forced_void[v])
            continue;
          if (vol + 0.5 * Vv[v] > D)
            break;
          if (x[v] == 0.0) {
            if (added >= cap)
              continue;
            ++added;
          }
          cand[v] = 1;
          vol += Vv[v];
        }
        if (!S_.no_enclosed_voids)
          break;
        for (int e = 0; e < E_; ++e) {
          const int v = P_.design.element_variable[e];
          cand_rho[e] = v < 0 ? P_.design.fixed_density[e] : cand[v];
        }
        const EnclosedVoids &enc = voids_.find(cand_rho, threshold_);
        if (enc.enclosed_elements == 0)
          break;
        bool changed = false;
        for (int e : enc.enclosed) {
          const int v = P_.design.element_variable[e];
          if (v >= 0 && x[v] == 1.0 && !cand[v] && !forced_solid[v]) {
            forced_solid[v] = 1; // refuse the removal that encloses a void
            ++rejected;
            changed = true;
          }
          for (int a = 0; a < 4; ++a) {
            const int f = M_.face_neighbor(a, e);
            const int w = f >= 0 ? P_.design.element_variable[f] : -1;
            if (w >= 0 && x[w] == 0.0 && cand[w] && !forced_void[w]) {
              forced_void[w] = 1; // refuse the addition that seals it
              ++rejected;
              changed = true;
            }
          }
        }
        if (!changed)
          break;
      }
      int added = 0, removed = 0;
      double switched = 0.0;
      for (int v = 0; v < NV_; ++v) {
        const double nv = cand[v] ? 1.0 : 0.0;
        if (nv != x[v]) {
          (nv > x[v] ? added : removed) += 1;
          switched += Vv[v];
          x[v] = nv;
        }
      }
      last_added = added;
      last_removed = removed;
      last_rejected = rejected;
      st_.rejected_total += rejected;
      st_.last_change = switched / M_.total_volume_m3;
      st_.volume_target = T_next;
      notify(false);
    }
  }

  // ── level set ────────────────────────────────────────────────────────
  void rho_from_phi(const SmoothHeaviside &H, const Eigen::VectorXd &phi,
                    Eigen::VectorXd &rho) const {
    rho.resize(E_);
    for (int e = 0; e < E_; ++e) {
      const int v = P_.design.element_variable[e];
      rho[e] = v < 0 ? P_.design.fixed_density[e] : H.value(phi[v]);
    }
  }

  void run_level_set() {
    const SmoothHeaviside H{opt_.level_set_band};
    const Eigen::VectorXd &Vv = P_.design.variable_volume_m3;
    const double Vt = M_.total_volume_m3;
    const double Vstar = S_.volume_fraction;
    const double tol = S_.change_tolerance;
    passive_solid_ = passive_solid_volume();
    volume_slack_ = 0.0;
    res_.volume_tolerance = opt_.feasibility_tolerance;
    const double Lref = (M_.bbox_max_m - M_.bbox_min_m).maxCoeff();
    const double D = S_.level_set_regularization * Lref * Lref;
    const double dt = opt_.level_set_time_step;
    ls_diffusion_ = D;
    ls_reference_length_ = Lref;
    criterion_ =
        "volume at target, max |phi_k - phi_{k-1}|/2 < " + fmt(tol) +
        " and relative objective change < " + fmt(tol) + " for " +
        std::to_string(opt_.convergence_window) +
        " consecutive iterations, all g <= " + fmt(opt_.feasibility_tolerance) +
        (S_.no_enclosed_voids ? ", no enclosed voids" : "");

    // Operator A = M/dt + D K (face-adjacency two-point diffusion), passive
    // neighbours enter as Dirichlet phi = +1 (solid) / -1 (void).
    std::uint64_t faces = 0;
    for (int e = 0; e < E_; ++e)
      for (int a = 0; a < 4; ++a)
        faces += M_.face_neighbor(a, e) > e ? 1 : 0;
    admit_or_refuse(faces * 4ull * 16ull * 3ull +
                        static_cast<std::uint64_t>(NV_) * 8ull * 40ull,
                    "topology level-set diffusion operator");
    std::vector<Eigen::Triplet<double>> trip;
    trip.reserve(static_cast<std::size_t>(faces) * 4 + NV_);
    Eigen::VectorXd dirichlet = Eigen::VectorXd::Zero(NV_);
    auto passive_phi = [&](int e) {
      return P_.design.fixed_density[e] > 0.5 ? 1.0 : -1.0;
    };
    for (int e = 0; e < E_; ++e)
      for (int a = 0; a < 4; ++a) {
        const int f = M_.face_neighbor(a, e);
        if (f <= e)
          continue;
        const double w = D * face_area(M_, e, a) /
                         (M_.centroid_m.col(e) - M_.centroid_m.col(f)).norm();
        const int ve = P_.design.element_variable[e],
                  vf = P_.design.element_variable[f];
        if (ve >= 0 && vf >= 0) {
          if (ve == vf)
            continue;
          trip.emplace_back(ve, ve, w);
          trip.emplace_back(vf, vf, w);
          trip.emplace_back(ve, vf, -w);
          trip.emplace_back(vf, ve, -w);
        } else if (ve >= 0) {
          trip.emplace_back(ve, ve, w);
          dirichlet[ve] += w * passive_phi(f);
        } else if (vf >= 0) {
          trip.emplace_back(vf, vf, w);
          dirichlet[vf] += w * passive_phi(e);
        }
      }
    for (int v = 0; v < NV_; ++v)
      trip.emplace_back(v, v, Vv[v] / dt);
    Eigen::SparseMatrix<double> A(NV_, NV_);
    A.setFromTriplets(trip.begin(), trip.end());
    trip.clear();
    trip.shrink_to_fit();
    Eigen::SimplicialLDLT<Eigen::SparseMatrix<double>> ldlt;
    ldlt.compute(A);
    if (ldlt.info() != Eigen::Success)
      fail("level_set_factorization",
           "the level-set diffusion operator could not be factored");

    Eigen::VectorXd &phi = st_.design;
    if (!opt_.resume) {
      phi.setOnes(NV_);
      st_.reaction_gain = 1.0;
    }
    Eigen::VectorXd rho(E_), rho_trial(E_), grho(E_), sens(E_), filt(E_),
        s(NV_), rhs(NV_), a(NV_), b(NV_), phi_new(NV_);
    std::vector<std::uint8_t> frozen(NV_);
    rho_from_phi(H, phi, rho);
    if (!opt_.resume)
      st_.volume_target = phase_fraction(P_, rho);
    double b_gain = -1.0; // kappa for which b is current

    // Volume multiplier: phi'(lambda) = clamp(a - lambda b), frozen entries
    // keep phi; Newton on the exact derivative through H', bracketed.
    auto solve_multiplier = [&](double target_design_solid) {
      double lo = kDoubleMax, hi = -kDoubleMax;
      for (int v = 0; v < NV_; ++v) {
        if (frozen[v])
          continue;
        const double bv = std::max(b[v], 1e-300);
        lo = std::min(lo, (a[v] - 1.0) / bv);
        hi = std::max(hi, (a[v] + 1.0) / bv);
      }
      auto volume = [&](double lam, double *deriv) {
        double vol = 0.0, d = 0.0;
        for (int v = 0; v < NV_; ++v) {
          double p;
          if (frozen[v]) {
            p = phi[v];
          } else {
            const double raw = a[v] - lam * b[v];
            p = std::clamp(raw, -1.0, 1.0);
            if (deriv && raw > -1.0 && raw < 1.0)
              d -= Vv[v] * H.derivative(p) * b[v];
          }
          phi_new[v] = p;
          vol += Vv[v] * H.value(p);
        }
        if (deriv)
          *deriv = d;
        return vol;
      };
      if (lo > hi) { // everything frozen
        volume(0.0, nullptr);
        return 0.0;
      }
      if (volume(lo, nullptr) <= target_design_solid)
        return lo;
      if (volume(hi, nullptr) >= target_design_solid)
        return hi;
      double lam = 0.5 * (lo + hi);
      for (int it = 0; it < 200; ++it) {
        double d = 0.0;
        const double f = volume(lam, &d) - target_design_solid;
        if (std::abs(f) <= 1e-12 * Vt)
          return lam;
        if (f > 0)
          lo = lam;
        else
          hi = lam;
        double next = d < 0.0 ? lam - f / d : 0.5 * (lo + hi);
        if (!(next > lo && next < hi))
          next = 0.5 * (lo + hi);
        lam = next;
      }
      volume(lam, nullptr);
      return lam;
    };

    Evaluation ev = evaluate(rho, true);
    bind_constraints(ev);
    init_scale(ev);
    const bool resumed = opt_.resume.has_value();
    bool record_current = !resumed;
    double change = st_.last_change;
    int added = 0, removed = 0, rejected = 0, trials = 0;
    // On resume the start design was already recorded; adopt its evaluation.
    if (!record_current) {
      commit(rho, std::move(ev));
    }
    for (;;) {
      const auto t0 = Clock::now();
      if (record_current) {
        const double O = overhang_value(rho, false);
        IterationRecord rec =
            make_record(rho, ev, O, change, st_.volume_target);
        rec.added = added;
        rec.removed = removed;
        rec.rejected = rejected;
        rec.line_search_trials = trials;
        commit(rho, std::move(ev));
        push_record(std::move(rec), t0);
        const IterationRecord &cur = st_.history.back();
        const bool at_target = st_.volume_target == Vstar;
        double rel = 1.0;
        if (st_.history.size() >= 2) {
          const double prev =
              st_.history[st_.history.size() - 2].objective_scaled;
          rel = std::abs(cur.objective_scaled - prev) /
                std::max(std::abs(cur.objective_scaled), 1e-300);
        }
        const bool stationary = at_target && change < tol && rel < tol;
        st_.stationary_count = stationary ? st_.stationary_count + 1 : 0;
        const bool stall = at_target && stall_check(cur);
        if (!at_target)
          reset_stall();
        bool done = true;
        if (st_.stationary_count >= opt_.convergence_window && cur.feasible) {
          set_stop(Termination::Converged,
                   "level set stationary at the target volume");
        } else if (st_.stationary_count >= opt_.infeasible_window &&
                   !cur.feasible) {
          set_stop(
              Termination::Infeasible,
              "level set stationary while max g = " + fmt(cur.max_constraint) +
                  " / enclosed void elements = " +
                  std::to_string(cur.enclosed_void_elements));
        } else if (stall) {
          set_stop(Termination::Stalled, "no merit improvement over " +
                                             std::to_string(opt_.stall_window) +
                                             " iterations");
        } else if (st_.iteration >= S_.max_iterations) {
          set_stop(Termination::MaxIterations,
                   "reached max_iterations=" +
                       std::to_string(S_.max_iterations));
        } else {
          done = false;
        }
        notify(done);
        if (done)
          return;
      }
      record_current = true;

      // Reaction term: filtered keep-benefit per volume of the Lagrangian
      // objective (ersatz topological sensitivity).
      overhang_value(rho_eval_, overhang_w_ > 0);
      scaled_objective_gradient(ev_eval_, grho);
      for (int e = 0; e < E_; ++e)
        sens[e] = -grho[e] / M_.volume_m3[e];
      filt.noalias() = P_.filter.weights * sens;
      double smax = 0.0;
      for (int v = 0; v < NV_; ++v) {
        double acc = 0.0;
        for (int k = P_.design.variable_offsets[v];
             k < P_.design.variable_offsets[v + 1]; ++k) {
          const int e = P_.design.variable_elements[k];
          acc += M_.volume_m3[e] * filt[e];
        }
        s[v] = acc / Vv[v];
        smax = std::max(smax, std::abs(s[v]));
      }
      if (!(smax > 0))
        smax = 1.0;
      const double T = st_.volume_target;
      const double T_next =
          T > Vstar   ? std::max(Vstar, T - opt_.level_set_volume_step)
          : T < Vstar ? std::min(Vstar, T + opt_.level_set_volume_step)
                      : Vstar;
      const bool guarded = T == Vstar && T_next == Vstar;
      const double J_old = st_.history.back().objective_scaled +
                           overhang_w_ * st_.history.back().overhang;
      double kappa = st_.reaction_gain;
      bool accepted = false;
      Evaluation ev_trial;
      trials = 0;
      rejected = 0;
      for (int attempt = 0; attempt <= opt_.max_line_search; ++attempt) {
        if (cancel_requested())
          throw EvaluationCancelled();
        if (b_gain != kappa) {
          rhs = (kappa / smax) * Vv;
          b = ldlt.solve(rhs);
          b_gain = kappa;
        }
        for (int v = 0; v < NV_; ++v)
          rhs[v] = Vv[v] * (phi[v] / dt + kappa * s[v] / smax) + dirichlet[v];
        a = ldlt.solve(rhs);
        std::fill(frozen.begin(), frozen.end(), 0);
        const double target = design_solid_target(T_next);
        for (int pass = 0; pass < opt_.max_enclosure_passes; ++pass) {
          solve_multiplier(target);
          if (!S_.no_enclosed_voids)
            break;
          rho_from_phi(H, phi_new, rho_trial);
          const EnclosedVoids &enc = voids_.find(rho_trial, threshold_);
          if (enc.enclosed_elements == 0)
            break;
          bool changed = false;
          for (int e : enc.enclosed) {
            const int v = P_.design.element_variable[e];
            if (v >= 0 && !frozen[v] && H.value(phi[v]) >= threshold_ &&
                H.value(phi_new[v]) < threshold_) {
              frozen[v] = 1;
              ++rejected;
              changed = true;
            }
            for (int k = 0; k < 4; ++k) {
              const int f = M_.face_neighbor(k, e);
              const int w = f >= 0 ? P_.design.element_variable[f] : -1;
              if (w >= 0 && !frozen[w] && H.value(phi[w]) < threshold_ &&
                  H.value(phi_new[w]) >= threshold_) {
                frozen[w] = 1;
                ++rejected;
                changed = true;
              }
            }
          }
          if (!changed)
            break;
        }
        rho_from_phi(H, phi_new, rho_trial);
        ++trials;
        ev_trial = evaluate(rho_trial, true);
        if (!guarded) {
          accepted = true;
          break;
        }
        const double O_new = overhang_value(rho_trial, false);
        const double J_new = scaled(ev_trial) + overhang_w_ * O_new;
        if (J_new - J_old <= opt_.level_set_increase_tolerance *
                                 std::max(std::abs(J_old), 1e-300)) {
          accepted = true;
          break;
        }
        kappa *= 0.5;
      }
      st_.rejected_total += rejected;
      if (!accepted) {
        set_stop(Termination::LineSearchFailed,
                 "reaction-gain backtracking exhausted after " +
                     std::to_string(trials) +
                     " trials; last accepted level set retained");
        notify(true);
        return;
      }
      st_.reaction_gain = trials == 1 ? std::min(1.0, 2.0 * kappa) : kappa;
      added = removed = 0;
      change = 0.0;
      for (int v = 0; v < NV_; ++v) {
        change = std::max(change, 0.5 * std::abs(phi_new[v] - phi[v]));
        if (phi[v] < 0.0 && phi_new[v] >= 0.0)
          ++added;
        else if (phi[v] >= 0.0 && phi_new[v] < 0.0)
          ++removed;
      }
      phi.swap(phi_new);
      rho.swap(rho_trial);
      ev = std::move(ev_trial);
      bind_constraints(ev);
      st_.volume_target = T_next;
      st_.last_change = change;
    }
  }

  // ── final ─────────────────────────────────────────────────────────────
  void finish() {
    res_.iterations = st_.iteration;
    res_.convergence_criterion = criterion_;
    if (!have_eval_) {
      res_.ok = false;
      res_.state = std::move(st_);
      res_.evaluations = res_.state.evaluations;
      return;
    }
    const Termination t = res_.termination;
    if (t != Termination::Cancelled && t != Termination::Failed) {
      try {
        res_.final_evaluation = evaluate(rho_eval_, false);
        res_.final_reevaluated = true;
      } catch (const EvaluationCancelled &) {
        set_stop(Termination::Cancelled,
                 "cancelled during the final re-evaluation; last iteration "
                 "evaluation retained");
      } catch (const ModelError &e) {
        set_stop(Termination::Failed,
                 std::string("final re-evaluation failed: ") + e.what(),
                 e.code());
      } catch (const std::exception &e) {
        set_stop(Termination::Failed,
                 std::string("final re-evaluation failed: ") + e.what(),
                 "optimizer_exception");
      }
    }
    if (!res_.final_reevaluated)
      res_.final_evaluation = std::move(ev_eval_);
    res_.has_final = true;
    res_.density = rho_eval_;
    res_.final_objective = res_.final_evaluation.objective;
    res_.objective_unit = res_.final_evaluation.objective_unit;
    res_.baseline_objective = st_.baseline_objective;

    update_volume(rho_eval_);
    res_.volume_fraction = vol_.response;
    bool physics_volume = false;
    bool feasible = true;
    double gmax = 0.0;
    auto take = [&](const Constraint &c) {
      Constraint copy;
      copy.name = c.name;
      copy.unit = c.unit;
      copy.value = c.value;
      copy.response = c.response;
      copy.limit = c.limit;
      copy.aggregation = c.aggregation;
      const double allowed = opt_.feasibility_tolerance +
                             (c.name == vol_.name ? volume_slack_ : 0);
      if (c.value > allowed)
        feasible = false;
      gmax = std::max(gmax, c.value);
      res_.final_constraints.push_back(std::move(copy));
    };
    for (const auto &c : res_.final_evaluation.constraints) {
      physics_volume = physics_volume || c.name == vol_.name;
      take(c);
    }
    if (!physics_volume)
      take(vol_);
    res_.max_violation = gmax;

    ManufacturingReport &mr = res_.manufacturing;
    mr.threshold = threshold_;
    mr.no_enclosed_voids = S_.no_enclosed_voids;
    mr.enclosed = voids_.find(rho_eval_, threshold_);
    mr.enclosed_void_enforcement = !S_.no_enclosed_voids ? "report_only"
                                   : discrete_method(S_.method)
                                       ? "candidate_acceptance"
                                       : "feasibility_check";
    mr.rejected_updates = st_.rejected_total;
    if (S_.no_enclosed_voids && mr.enclosed.enclosed_elements > 0)
      feasible = false;
    if (overhang_on_) {
      mr.overhang_evaluated = true;
      mr.overhang_in_objective = overhang_w_ > 0;
      mr.build_direction = overhang_.settings().build_direction;
      mr.build_direction_source = P_.build_plate ? "build_plate" : "build_direction";
      mr.build_plate = P_.build_plate;
      mr.overhang_angle_deg = S_.overhang_angle_deg;
      mr.overhang_penalty = overhang_w_;
      mr.overhang_measure = overhang_.measure(rho_eval_, nullptr);
      mr.unsupported_volume_fraction =
          overhang_.unsupported_fraction(rho_eval_, threshold_);
      mr.overhang_formulation = overhang_.formulation();
      mr.plate_elements = overhang_.plate_elements();
      mr.coneless_elements = overhang_.coneless_elements();
      res_.final_overhang = mr.overhang_measure;
    }
    mr.min_member_m = P_.min_member_m;
    mr.filter_radius_m = P_.filter_radius_m;
    mr.filter_radius_source = P_.filter_radius_source;
    if (P_.evidence.contains("filter") &&
        P_.evidence["filter"].contains("min_member_filter_radius_mm")) {
      mr.min_member_filter_radius_mm =
          P_.evidence["filter"]["min_member_filter_radius_mm"].get<double>();
      mr.mesh_floor_radius_mm =
          P_.evidence["filter"]["mesh_floor_radius_mm"].get<double>();
    }
    switch (S_.method) {
    case Method::Simp:
    case Method::Ramp:
      mr.min_member_regularization = "density filter (hat, radius " +
                                     fmt(P_.filter.radius_m) +
                                     " m) on the design before projection";
      break;
    case Method::Beso:
      mr.min_member_regularization = "sensitivity-number filter (hat, radius " +
                                     fmt(P_.filter.radius_m) + " m)";
      break;
    case Method::LevelSet:
      mr.min_member_regularization =
          "sensitivity filter (hat, radius " + fmt(P_.filter.radius_m) +
          " m) plus reaction-diffusion regularization tau=" +
          fmt(S_.level_set_regularization);
      break;
    }
    mr.symmetry_axes = S_.symmetry_axes;
    mr.extrusion_axis = S_.extrusion_axis;
    mr.tied_groups = P_.design.tied_groups;
    mr.max_tie_distance_m = P_.design.max_tie_distance_m;

    if (S_.method == Method::LevelSet) {
      res_.level_set.resize(E_);
      for (int e = 0; e < E_; ++e) {
        const int v = P_.design.element_variable[e];
        res_.level_set[e] = v >= 0                             ? st_.design[v]
                            : P_.design.fixed_density[e] > 0.5 ? 1.0
                                                               : -1.0;
      }
    }

    res_.feasible = feasible;
    if (res_.termination == Termination::Converged && !feasible) {
      res_.termination = Termination::Infeasible;
      res_.message += "; the final re-evaluation violates a constraint";
    }
    res_.converged = res_.termination == Termination::Converged;
    res_.ok = res_.termination != Termination::Cancelled &&
              res_.termination != Termination::Failed;

    json ev = {{"volume_phase", to_string(phase_)},
               {"objective_sense", sense_ > 0 ? "minimize" : "maximize"},
               {"objective_scale", st_.objective_scale},
               {"feasibility_tolerance", opt_.feasibility_tolerance},
               {"volume_tolerance", res_.volume_tolerance},
               {"filter_radius_m", P_.filter.radius_m},
               {"design_variables", NV_},
               {"elements", E_},
               {"constraint_names", st_.constraint_names}};
    switch (S_.method) {
    case Method::Simp:
    case Method::Ramp:
      ev["projection"] = {
          {"beta_target", S_.projection_beta},
          {"beta_final", st_.beta},
          {"eta", S_.density_threshold},
          {"continuation_interval", opt_.projection_continuation_interval}};
      ev["move_limit"] = S_.move_limit;
      ev["mma"] = {{"constraint_penalty", opt_.mma.constraint_penalty},
                   {"constraint_quadratic", opt_.mma.constraint_quadratic},
                   {"initial_asymptote", opt_.mma.initial_asymptote}};
      break;
    case Method::Beso:
      ev["beso"] = {{"evolution_rate", S_.beso_evolution_rate},
                    {"max_addition_ratio", opt_.beso_max_addition_ratio},
                    {"history_average", "0.5 (current + previous)"},
                    {"convergence_window", opt_.beso_convergence_window},
                    {"final_volume_target", st_.volume_target}};
      break;
    case Method::LevelSet:
      ev["level_set"] = {
          {"tau", S_.level_set_regularization},
          {"reference_length_m", ls_reference_length_},
          {"diffusion_coefficient_m2", ls_diffusion_},
          {"time_step", opt_.level_set_time_step},
          {"heaviside_half_width", opt_.level_set_band},
          {"volume_step", opt_.level_set_volume_step},
          {"increase_tolerance", opt_.level_set_increase_tolerance},
          {"final_reaction_gain", st_.reaction_gain}};
      break;
    }
    res_.evidence = std::move(ev);
    res_.state = std::move(st_);
    res_.evaluations = res_.state.evaluations;
  }

  std::shared_ptr<const Problem> pp_;
  const Problem &P_;
  const TetMesh &M_;
  const Settings &S_;
  Physics &phys_;
  const OptimizerOptions &opt_;
  int E_ = 0, NV_ = 0;
  double sense_ = 1.0;
  DesignPhase phase_ = DesignPhase::Solid;
  double threshold_ = 0.5;
  double volume_slack_ = 0.0;
  double passive_solid_ = 0.0;
  double ls_diffusion_ = 0.0, ls_reference_length_ = 0.0;
  std::string criterion_;

  bool overhang_on_ = false;
  double overhang_w_ = 0.0;
  OverhangModel overhang_;
  Eigen::VectorXd oh_grad_;
  EnclosedVoidFinder voids_;

  Constraint vol_;
  std::vector<const Constraint *> cons_;

  OptimizerState st_;
  OptimizationResult res_;
  Eigen::VectorXd rho_eval_;
  Evaluation ev_eval_;
  bool have_eval_ = false;
};

} // namespace

OptimizationResult optimize(std::shared_ptr<const Problem> problem,
                            Physics &physics, const OptimizerOptions &options) {
  if (!problem || !problem->mesh)
    fail("invalid_input", "optimize needs a prepared problem");
  const Problem &p = *problem;
  const Settings &s = p.settings;
  validate_options(options);
  const Compatibility c = check_compatibility(s);
  if (!c.supported)
    fail(c.code, c.message);
  if (physics.kind() != s.physics)
    fail("physics_mismatch",
         std::string("physics evaluator is ") + to_string(physics.kind()) +
             " but the problem asks for " + to_string(s.physics));
  check_interpolation(p);
  const int E = p.mesh->element_count();
  if (p.design.variable_count < 1 || p.design.element_variable.size() != E ||
      p.design.variable_volume_m3.size() != p.design.variable_count ||
      p.filter.weights.rows() != E)
    fail("invalid_input", "problem design map / filter are not prepared");
  admit_or_refuse(static_cast<std::uint64_t>(E) * 8ull * 24ull +
                      static_cast<std::uint64_t>(s.max_iterations) * 512ull,
                  "topology optimizer state");
  Runner runner(std::move(problem), physics, options);
  return runner.run();
}

} // namespace topology
} // namespace sim
} // namespace chudware
