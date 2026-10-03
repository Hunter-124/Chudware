// Topology optimization: tet4 linear elasticity (see topopt_elastic.h).
#include "sim/topopt_elastic.h"

#include "sim/solver_threads.h"
#include "util/resource_governor.h"

#include <Eigen/SparseCholesky>
#ifdef CHUDWARE_WITH_CHOLMOD
#include <Eigen/CholmodSupport>
#endif

#include <algorithm>
#include <cmath>
#include <limits>
#include <sstream>

namespace chudware {
namespace sim {
namespace topology {

namespace {

using Vec6 = Eigen::Matrix<double, 6, 1>;
using Vec12 = Eigen::Matrix<double, 12, 1>;
using Mat12 = Eigen::Matrix<double, 12, 12>;
using Mat6X = Eigen::Matrix<double, 6, Eigen::Dynamic>;

// Normwise backward error accepted for a direct solve: a backward-stable
// Cholesky lands near n * machine epsilon; anything this large means the
// factor no longer represents the operator.
constexpr double kMaxBackwardError = 1e-9;

[[noreturn]] void fail(const std::string &code, const std::string &message) {
  throw ModelError(code, message);
}

void check_cancel(const std::atomic<bool> *cancel) {
  if (cancel && cancel->load(std::memory_order_relaxed))
    throw EvaluationCancelled();
}

std::string fmt(double v) {
  std::ostringstream s;
  s.precision(6);
  s << v;
  return s.str();
}

nlohmann::json vec3_json(const Eigen::Vector3d &v) {
  return nlohmann::json::array({v[0], v[1], v[2]});
}

// Max absolute column sum of a sparse matrix (operator 1-norm).
double sparse_norm1(const Eigen::SparseMatrix<double> &A) {
  double best = 0;
  for (int j = 0; j < A.outerSize(); ++j) {
    double s = 0;
    for (Eigen::SparseMatrix<double>::InnerIterator it(A, j); it; ++it)
      s += std::abs(it.value());
    best = std::max(best, s);
  }
  return best;
}

// KS upper bound of squared ratios: returns m = max r2, KS and fills weights
// (softmax, sum 1) over the entries flagged `use`.
struct KsResult {
  double max_r2 = 0, ks = 0;
  int count = 0, argmax = -1;
};
KsResult ks_squared(const Eigen::VectorXd &r2, const std::vector<char> &use,
                    double P, Eigen::VectorXd *weights) {
  KsResult k;
  k.max_r2 = -std::numeric_limits<double>::infinity();
  for (Eigen::Index i = 0; i < r2.size(); ++i) {
    if (!use[i])
      continue;
    ++k.count;
    if (r2[i] > k.max_r2) {
      k.max_r2 = r2[i];
      k.argmax = static_cast<int>(i);
    }
  }
  if (k.count == 0)
    return k;
  double sum = 0;
  if (weights)
    weights->setZero(r2.size());
  for (Eigen::Index i = 0; i < r2.size(); ++i) {
    if (!use[i])
      continue;
    const double w = std::exp(P * (r2[i] - k.max_r2));
    sum += w;
    if (weights)
      (*weights)[i] = w;
  }
  k.ks = k.max_r2 + std::log(sum) / P;
  if (weights)
    *weights /= sum;
  return k;
}

} // namespace

// ── Memory admission
// ──────────────────────────────────────────────────────────────
std::uint64_t elastic_memory_estimate(const Problem &p, int factorizations,
                                      std::uint64_t dense_bytes) {
  const std::uint64_t per = governor::fea_peak_bytes(
      static_cast<std::uint64_t>(p.mesh->element_count()), 4, 3);
  const long double total =
      static_cast<long double>(per) * std::max(1, factorizations) +
      static_cast<long double>(dense_bytes);
  const long double cap =
      static_cast<long double>(std::numeric_limits<std::uint64_t>::max());
  return total >= cap ? std::numeric_limits<std::uint64_t>::max()
                      : static_cast<std::uint64_t>(total);
}

void admit_elastic_memory(const Problem &p, int factorizations,
                          std::uint64_t dense_bytes, const char *what) {
  const std::uint64_t estimate =
      elastic_memory_estimate(p, factorizations, dense_bytes);
  std::string why;
  if (!governor::admit(estimate, what, why))
    fail("memory_budget", why);
}

// ── SpdFactor
// ─────────────────────────────────────────────────────────────────────
struct SpdFactor::Impl {
  using Ldlt = Eigen::SimplicialLDLT<Eigen::SparseMatrix<double>, Eigen::Lower,
                                     Eigen::AMDOrdering<int>>;
#ifdef CHUDWARE_WITH_CHOLMOD
  std::unique_ptr<Eigen::CholmodSupernodalLLT<Eigen::SparseMatrix<double>>>
      chol;
#endif
  std::unique_ptr<Ldlt> ldlt;
  bool use_chol = false;
  bool ok = false;
  long numeric = 0;

  void analyze_ldlt(const Eigen::SparseMatrix<double> &A) {
    ldlt = std::make_unique<Ldlt>();
    ldlt->analyzePattern(A);
    if (ldlt->info() != Eigen::Success)
      fail("singular_system", "sparse LDLT symbolic analysis failed");
  }
};

SpdFactor::SpdFactor() : impl_(std::make_unique<Impl>()) {}
SpdFactor::~SpdFactor() = default;

void SpdFactor::analyze(const Eigen::SparseMatrix<double> &A) {
  cap_solver_threads_for_host();
  impl_->ok = false;
  impl_->use_chol = false;
#ifdef CHUDWARE_WITH_CHOLMOD
  impl_->chol = std::make_unique<
      Eigen::CholmodSupernodalLLT<Eigen::SparseMatrix<double>>>();
  impl_->chol->analyzePattern(A);
  if (impl_->chol->info() == Eigen::Success) {
    impl_->use_chol = true;
    return;
  }
  impl_->chol.reset();
#endif
  impl_->analyze_ldlt(A);
}

void SpdFactor::factorize(const Eigen::SparseMatrix<double> &A) {
  impl_->ok = false;
#ifdef CHUDWARE_WITH_CHOLMOD
  if (impl_->use_chol) {
    impl_->chol->factorize(A);
    ++impl_->numeric;
    if (impl_->chol->info() == Eigen::Success) {
      impl_->ok = true;
      return;
    }
    // One explicit fallback: the pure-Eigen LDLT decides positive
    // definiteness by its pivots.
    impl_->chol.reset();
    impl_->use_chol = false;
    impl_->analyze_ldlt(A);
  }
#endif
  if (!impl_->ldlt)
    fail("singular_system", "factorize called before analyze");
  impl_->ldlt->factorize(A);
  ++impl_->numeric;
  if (impl_->ldlt->info() != Eigen::Success ||
      !impl_->ldlt->vectorD().allFinite() ||
      (impl_->ldlt->vectorD().array() <= 0.0).any())
    fail("singular_system",
         "stiffness is not positive definite (insufficient supports or a "
         "disconnected loaded region)");
  impl_->ok = true;
}

void SpdFactor::solve(const Eigen::Ref<const Eigen::MatrixXd> &b,
                      Eigen::MatrixXd &x) const {
  if (!impl_->ok)
    fail("singular_system", "solve without a valid factorization");
#ifdef CHUDWARE_WITH_CHOLMOD
  if (impl_->use_chol) {
    x = impl_->chol->solve(b);
    if (impl_->chol->info() != Eigen::Success)
      fail("singular_system", "CHOLMOD solve failed");
    return;
  }
#endif
  x = impl_->ldlt->solve(b);
  if (impl_->ldlt->info() != Eigen::Success)
    fail("singular_system", "LDLT solve failed");
}

bool SpdFactor::factorized() const { return impl_->ok; }

const char *SpdFactor::label() const {
  return impl_->use_chol ? "CholmodSupernodalLLT" : "SimplicialLDLT(AMD)";
}

long SpdFactor::numeric_factorizations() const { return impl_->numeric; }

// ── Material helpers
// ──────────────────────────────────────────────────────────────
double von_mises_squared(const Vec6 &s) {
  const double a = s[0] - s[1], b = s[1] - s[2], c = s[2] - s[0];
  return 0.5 * (a * a + b * b + c * c) +
         3.0 * (s[3] * s[3] + s[4] * s[4] + s[5] * s[5]);
}

Vec6 von_mises_squared_gradient(const Vec6 &s) {
  Vec6 g;
  g << 2 * s[0] - s[1] - s[2], 2 * s[1] - s[0] - s[2], 2 * s[2] - s[0] - s[1],
      6 * s[3], 6 * s[4], 6 * s[5];
  return g;
}

double StressRelaxation::value(double rho) const {
  const double a = std::sqrt(floor);
  return (std::sqrt(rho + floor) - a) / (std::sqrt(1.0 + floor) - a);
}

double StressRelaxation::derivative(double rho) const {
  const double a = std::sqrt(floor);
  return 0.5 / std::sqrt(rho + floor) / (std::sqrt(1.0 + floor) - a);
}

double MassInterpolation::value(double rho) const {
  const double g =
      rho > 0.1 ? rho : 6e5 * std::pow(rho, 6) - 5e6 * std::pow(rho, 7);
  return floor + (1.0 - floor) * g;
}

double MassInterpolation::derivative(double rho) const {
  const double g =
      rho > 0.1 ? 1.0 : 3.6e6 * std::pow(rho, 5) - 3.5e7 * std::pow(rho, 6);
  return (1.0 - floor) * g;
}

// ── ElasticModel
// ──────────────────────────────────────────────────────────────────
ElasticModel::ElasticModel(std::shared_ptr<const Problem> problem)
    : problem_(std::move(problem)) {
  if (!problem_ || !problem_->mesh)
    fail("invalid_input", "elastic model needs a prepared problem");
  if (!uses_structural_boundary(problem_->settings.physics) ||
      !problem_->structural)
    fail("physics_mismatch",
         std::string("tet4 elasticity does not apply to physics ") +
             to_string(problem_->settings.physics));
  if (!problem_->material.youngs_pa || !problem_->material.poisson)
    fail("material_missing", "elasticity needs material.E_mpa and material.nu");
  c_ = isotropic_elasticity(*problem_->material.youngs_pa,
                            *problem_->material.poisson);
  dofs_ = nodal_dof_map(*problem_->mesh, 3);
}

Eigen::Matrix<double, 6, 12> ElasticModel::strain_matrix(int e) const {
  const Eigen::Matrix<double, 3, 4> G = mesh().element_gradients(e);
  return tet4_strain_matrix(G);
}

void ElasticModel::gather(const Eigen::Ref<const Eigen::VectorXd> &full, int e,
                          Vec12 &ue) const {
  for (int i = 0; i < 12; ++i)
    ue[i] = full[dofs_(i, e)];
}

Vec6 ElasticModel::strain(int e,
                          const Eigen::Ref<const Eigen::VectorXd> &u) const {
  Vec12 ue;
  gather(u, e, ue);
  return strain_matrix(e) * ue;
}

void ElasticModel::solid_stiffness(int e, Mat12 &ke) const {
  const Eigen::Matrix<double, 6, 12> B = strain_matrix(e);
  ke.noalias() = mesh().volume_m3[e] * (B.transpose() * (c_ * B));
}

void ElasticModel::add_stiffness_contraction(
    const Eigen::VectorXd &rho, const Eigen::Ref<const Eigen::VectorXd> &a,
    const Eigen::Ref<const Eigen::VectorXd> &b, double weight,
    Eigen::VectorXd &grad) const {
  const Interpolation &ip = interpolation();
  const int E = element_count();
  Vec12 ae, be;
  for (int e = 0; e < E; ++e) {
    const double ds = ip.scale_derivative(rho[e]);
    if (ds == 0.0)
      continue;
    const Eigen::Matrix<double, 6, 12> B = strain_matrix(e);
    gather(a, e, ae);
    gather(b, e, be);
    const Vec6 ea = B * ae, eb = B * be;
    grad[e] += weight * ds * mesh().volume_m3[e] * ea.dot(c_ * eb);
  }
}

void ElasticModel::internal_force(const Eigen::VectorXd &rho,
                                  const Eigen::Ref<const Eigen::VectorXd> &u,
                                  Eigen::VectorXd &out) const {
  const Interpolation &ip = interpolation();
  out.setZero(dof_count());
  Vec12 ue;
  for (int e = 0; e < element_count(); ++e) {
    const Eigen::Matrix<double, 6, 12> B = strain_matrix(e);
    gather(u, e, ue);
    const Vec12 fe = (ip.scale(rho[e]) * mesh().volume_m3[e]) *
                     (B.transpose() * (c_ * (B * ue)));
    for (int i = 0; i < 12; ++i)
      out[dofs_(i, e)] += fe[i];
  }
}

void ElasticModel::add_eigenstrain_force(const Eigen::VectorXd &rho,
                                         const Mat6X &e0,
                                         Eigen::VectorXd &force) const {
  if (e0.cols() != element_count() || force.size() != dof_count())
    fail("invalid_input", "eigenstrain force size mismatch");
  const Interpolation &ip = interpolation();
  for (int e = 0; e < element_count(); ++e) {
    const Eigen::Matrix<double, 6, 12> B = strain_matrix(e);
    const Vec12 fe = (ip.scale(rho[e]) * mesh().volume_m3[e]) *
                     (B.transpose() * (c_ * e0.col(e)));
    for (int i = 0; i < 12; ++i)
      force[dofs_(i, e)] += fe[i];
  }
}

void ElasticModel::eigenstrain_force_adjoint(
    const Eigen::VectorXd &rho, const Mat6X &e0,
    const Eigen::Ref<const Eigen::VectorXd> &lambda, double weight,
    Eigen::VectorXd *grad_rho, Mat6X *grad_e0) const {
  if (e0.cols() != element_count())
    fail("invalid_input", "eigenstrain size mismatch");
  if (grad_e0 && grad_e0->cols() != element_count())
    fail("invalid_input", "eigenstrain gradient must be 6 x E");
  const Interpolation &ip = interpolation();
  Vec12 le;
  for (int e = 0; e < element_count(); ++e) {
    gather(lambda, e, le);
    const Vec6 cbl = c_ * (strain_matrix(e) * le);
    const double V = mesh().volume_m3[e];
    if (grad_rho)
      (*grad_rho)[e] +=
          weight * ip.scale_derivative(rho[e]) * V * cbl.dot(e0.col(e));
    if (grad_e0)
      grad_e0->col(e) += (weight * ip.scale(rho[e]) * V) * cbl;
  }
}

// ── ElasticSystem
// ─────────────────────────────────────────────────────────────────
ElasticSystem::ElasticSystem(std::shared_ptr<const ElasticModel> model,
                             const Dirichlet &fixed)
    : model_(std::move(model)), fixed_(fixed),
      pattern_(model_->element_dofs(),
               make_dof_reduction(model_->dof_count(), fixed)) {
  const DofReduction &red = pattern_.reduction();
  if (red.free_size() == 0)
    fail("invalid_bc", "every structural DOF is fixed");
  K_ = pattern_.pattern();
  const Eigen::MatrixXi &dofs = model_->element_dofs();
  for (int e = 0; e < model_->element_count(); ++e)
    for (int i = 0; i < 12; ++i) {
      const int d = dofs(i, e);
      if (red.full_to_free[d] < 0 && red.prescribed[d] != 0.0) {
        lift_elements_.push_back(e);
        break;
      }
    }
}

void ElasticSystem::assemble(const Eigen::VectorXd &rho,
                             const std::atomic<bool> *cancel) {
  if (rho.size() != model_->element_count())
    fail("invalid_density", "density size does not match the mesh");
  rho_ = rho;
  ReducedPattern::zero(K_);
  const Interpolation &ip = model_->interpolation();
  Mat12 ke;
  for (int e = 0; e < model_->element_count(); ++e) {
    if ((e & 4095) == 0)
      check_cancel(cancel);
    model_->solid_stiffness(e, ke);
    ke *= ip.scale(rho[e]);
    pattern_.add(K_, e, ke);
  }
  if (lift_elements_.empty()) {
    lift_free_.resize(0);
    return;
  }
  lift_free_.setZero(reduction().free_size());
  for (int e : lift_elements_) {
    model_->solid_stiffness(e, ke);
    ke *= ip.scale(rho[e]);
    pattern_.lift(lift_free_, e, ke);
  }
}

void ElasticSystem::factorize() {
  if (rho_.size() != model_->element_count())
    fail("singular_system", "factorize before assemble");
  if (!analyzed_) {
    factor_.analyze(K_);
    analyzed_ = true;
  }
  factor_.factorize(K_);
}

void ElasticSystem::solve_free(const Eigen::MatrixXd &rhs, Eigen::MatrixXd &x,
                               const char *what) const {
  factor_.solve(rhs, x);
  if (!x.allFinite())
    fail("singular_system", std::string(what) + " solve is not finite");
  check_free_.noalias() = K_ * x;
  check_free_ -= rhs;
  const double knorm = sparse_norm1(K_);
  for (Eigen::Index c = 0; c < x.cols(); ++c) {
    const double denom = knorm * x.col(c).lpNorm<1>() + rhs.col(c).lpNorm<1>();
    if (denom == 0.0)
      continue;
    const double backward = check_free_.col(c).lpNorm<1>() / denom;
    max_residual_ = std::max(max_residual_, backward);
    if (!(backward <= kMaxBackwardError))
      fail("solve_inaccurate", std::string(what) + " solve backward error " +
                                   fmt(backward) + " exceeds " +
                                   fmt(kMaxBackwardError));
  }
}

void ElasticSystem::solve(const Eigen::Ref<const Eigen::MatrixXd> &forces,
                          Eigen::MatrixXd &u) const {
  const DofReduction &red = reduction();
  if (forces.rows() != red.full_size)
    fail("invalid_input", "force columns must have 3N rows");
  const int nf = red.free_size();
  rhs_free_.resize(nf, forces.cols());
  for (Eigen::Index c = 0; c < forces.cols(); ++c)
    for (int i = 0; i < nf; ++i)
      rhs_free_(i, c) = forces(red.free_to_full[i], c) +
                        (lift_free_.size() ? lift_free_[i] : 0.0);
  solve_free(rhs_free_, x_free_, "static");
  u.resize(red.full_size, forces.cols());
  for (Eigen::Index c = 0; c < forces.cols(); ++c) {
    u.col(c) = red.prescribed;
    for (int i = 0; i < nf; ++i)
      u(red.free_to_full[i], c) = x_free_(i, c);
  }
}

void ElasticSystem::solve_adjoint(const Eigen::Ref<const Eigen::MatrixXd> &rhs,
                                  Eigen::MatrixXd &adjoint) const {
  const DofReduction &red = reduction();
  if (rhs.rows() != red.full_size)
    fail("invalid_input", "adjoint columns must have 3N rows");
  const int nf = red.free_size();
  rhs_free_.resize(nf, rhs.cols());
  for (Eigen::Index c = 0; c < rhs.cols(); ++c)
    for (int i = 0; i < nf; ++i)
      rhs_free_(i, c) = rhs(red.free_to_full[i], c);
  solve_free(rhs_free_, x_free_, "adjoint");
  adjoint.setZero(red.full_size, rhs.cols());
  for (Eigen::Index c = 0; c < rhs.cols(); ++c)
    for (int i = 0; i < nf; ++i)
      adjoint(red.free_to_full[i], c) = x_free_(i, c);
}

// ── ElasticCases
// ──────────────────────────────────────────────────────────────────
ElasticCases::ElasticCases(std::shared_ptr<const ElasticModel> model)
    : model_(std::move(model)) {
  const StructuralBoundary &sb = *model_->problem().structural;
  if (sb.cases.empty())
    fail("section_missing", "elasticity needs at least one load case");
  const int C = static_cast<int>(sb.cases.size());
  const int n3 = model_->dof_count();
  reference_forces_.resize(n3, C);
  case_system_.assign(C, -1);
  for (int c = 0; c < C; ++c) {
    const StructuralCase &sc = sb.cases[c];
    if (sc.force_n.size() != n3)
      fail("invalid_bc", "load case " + sc.name + " force size mismatch");
    reference_forces_.col(c) = sc.force_n;
    for (int s = 0; s < system_count(); ++s) {
      const Dirichlet &f = systems_[s]->fixed();
      if (f.dofs.size() == sc.fixed.dofs.size() && f.dofs == sc.fixed.dofs &&
          f.values == sc.fixed.values) {
        case_system_[c] = s;
        break;
      }
    }
    if (case_system_[c] < 0) {
      systems_.push_back(std::make_unique<ElasticSystem>(model_, sc.fixed));
      system_cases_.emplace_back();
      case_system_[c] = system_count() - 1;
    }
    system_cases_[case_system_[c]].push_back(c);
  }
  states_.resize(C);
}

void ElasticCases::solve(const Eigen::VectorXd &rho,
                         const Eigen::MatrixXd &forces,
                         const std::atomic<bool> *cancel) {
  const int n3 = model_->dof_count();
  if (forces.rows() != n3 || forces.cols() != case_count())
    fail("invalid_input", "forces must be 3N x cases");
  for (int s = 0; s < system_count(); ++s) {
    check_cancel(cancel);
    ElasticSystem &sys = *systems_[s];
    sys.assemble(rho, cancel);
    check_cancel(cancel);
    sys.factorize();
    check_cancel(cancel);
    const std::vector<int> &cs = system_cases_[s];
    group_force_.resize(n3, static_cast<Eigen::Index>(cs.size()));
    for (std::size_t j = 0; j < cs.size(); ++j)
      group_force_.col(j) = forces.col(cs[j]);
    sys.solve(group_force_, group_u_);
    const DofReduction &red = sys.reduction();
    for (std::size_t j = 0; j < cs.size(); ++j) {
      ElasticCaseState &st = states_[cs[j]];
      st.force = group_force_.col(j);
      st.displacement = group_u_.col(j);
      model_->internal_force(rho, st.displacement, scratch_);
      st.reaction.setZero(n3);
      double free_res = 0, free_ku = 0;
      for (int d = 0; d < n3; ++d) {
        const double r = scratch_[d] - st.force[d];
        if (red.full_to_free[d] < 0)
          st.reaction[d] = r;
        else {
          free_res += r * r;
          free_ku += scratch_[d] * scratch_[d];
        }
      }
      const double scale = std::max({st.force.norm(), std::sqrt(free_ku),
                                     std::numeric_limits<double>::min()});
      st.residual = std::sqrt(free_res) / scale;
      st.compliance = st.force.dot(st.displacement);
      st.strain_energy = 0.5 * st.displacement.dot(scratch_);
    }
  }
}

void ElasticCases::solve_adjoint(const Eigen::MatrixXd &rhs,
                                 const std::vector<int> &rhs_case,
                                 Eigen::MatrixXd &adjoint) const {
  const int n3 = model_->dof_count();
  if (rhs.rows() != n3 ||
      rhs.cols() != static_cast<Eigen::Index>(rhs_case.size()))
    fail("invalid_input", "adjoint rhs must be 3N x R with R case indices");
  adjoint.resize(n3, rhs.cols());
  std::vector<int> cols;
  for (int s = 0; s < system_count(); ++s) {
    cols.clear();
    for (std::size_t r = 0; r < rhs_case.size(); ++r) {
      if (rhs_case[r] < 0 || rhs_case[r] >= case_count())
        fail("invalid_input", "adjoint case index out of range");
      if (case_system_[rhs_case[r]] == s)
        cols.push_back(static_cast<int>(r));
    }
    if (cols.empty())
      continue;
    group_in_.resize(n3, static_cast<Eigen::Index>(cols.size()));
    for (std::size_t j = 0; j < cols.size(); ++j)
      group_in_.col(j) = rhs.col(cols[j]);
    systems_[s]->solve_adjoint(group_in_, group_out_);
    for (std::size_t j = 0; j < cols.size(); ++j)
      adjoint.col(cols[j]) = group_out_.col(j);
  }
}

nlohmann::json ElasticCases::evidence() const {
  const StructuralBoundary &sb = *model_->problem().structural;
  nlohmann::json cases = nlohmann::json::array();
  double max_residual = 0;
  long factorizations = 0;
  for (int s = 0; s < system_count(); ++s) {
    max_residual = std::max(max_residual, systems_[s]->max_relative_residual());
    factorizations += systems_[s]->factor().numeric_factorizations();
  }
  for (int c = 0; c < case_count(); ++c) {
    const ElasticCaseState &st = states_[c];
    Eigen::Vector3d applied = Eigen::Vector3d::Zero(),
                    reaction = Eigen::Vector3d::Zero();
    for (int n = 0; n < model_->node_count(); ++n)
      for (int d = 0; d < 3; ++d) {
        if (st.force.size())
          applied[d] += st.force[3 * n + d];
        if (st.reaction.size())
          reaction[d] += st.reaction[3 * n + d];
      }
    const double scale = std::max(applied.norm(), reaction.norm());
    cases.push_back({{"name", sb.cases[c].name},
                     {"support_set", case_system_[c]},
                     {"fixed_dofs", sb.cases[c].fixed.size()},
                     {"compliance_j", st.compliance},
                     {"strain_energy_j", st.strain_energy},
                     {"relative_residual", st.residual},
                     {"applied_n", vec3_json(applied)},
                     {"reaction_n", vec3_json(reaction)},
                     {"relative_force_imbalance",
                      scale > 0 ? (applied + reaction).norm() / scale : 0.0}});
  }
  return {{"solver", system_count() ? systems_[0]->factor().label() : ""},
          {"support_sets", system_count()},
          {"numeric_factorizations", factorizations},
          {"max_solve_backward_error", max_residual},
          {"cases", cases}};
}

// ── Responses
// ─────────────────────────────────────────────────────────────────────
StressResponse stress_response(const ElasticModel &model,
                               const Eigen::VectorXd &rho,
                               const Eigen::Ref<const Eigen::VectorXd> &u,
                               const Mat6X *e0, double limit,
                               bool derivatives) {
  if (!(std::isfinite(limit) && limit > 0))
    fail("invalid_input", "stress limit must be finite and > 0");
  const int E = model.element_count();
  if (e0 && e0->cols() != E)
    fail("invalid_input", "eigenstrain must be 6 x E");
  const Problem &p = model.problem();
  const StressRelaxation relax{model.interpolation().floor};
  const Eigen::Matrix<double, 6, 6> &C = model.elasticity();
  StressResponse out;
  out.von_mises.resize(E);
  out.solid_von_mises.resize(E);
  Mat6X sigma(6, E);
  Eigen::VectorXd r2(E);
  std::vector<char> use(E, 0);
  const double inv_l2 = 1.0 / (limit * limit);
  for (int e = 0; e < E; ++e) {
    Vec6 eps = model.strain(e, u);
    if (e0)
      eps -= e0->col(e);
    sigma.col(e) = C * eps;
    const double vm2 = von_mises_squared(sigma.col(e));
    const double eta = relax.value(rho[e]);
    out.solid_von_mises[e] = std::sqrt(vm2);
    out.von_mises[e] = eta * out.solid_von_mises[e];
    r2[e] = eta * eta * vm2 * inv_l2;
    use[e] = p.roles.role[e] != ElementRole::Void;
  }
  Eigen::VectorXd w;
  const KsResult k =
      ks_squared(r2, use, kElasticKsParameter, derivatives ? &w : nullptr);
  if (k.count == 0)
    fail("invalid_bc", "stress constraint has no non-void element");
  const double A = std::sqrt(k.ks);
  out.count = k.count;
  out.value = limit * A;
  out.max_value = limit * std::sqrt(k.max_r2);
  out.max_element = k.argmax;
  out.upper_bound =
      limit * std::sqrt(k.max_r2 + std::log(static_cast<double>(k.count)) /
                                       kElasticKsParameter);
  if (!derivatives)
    return out;
  out.d_displacement.setZero(model.dof_count());
  out.d_density.setZero(E);
  if (e0)
    out.d_eigenstrain.setZero(6, E);
  const Eigen::MatrixXi &dofs = model.element_dofs();
  for (int e = 0; e < E; ++e) {
    if (!use[e] || w[e] == 0.0)
      continue;
    const double coef = limit * w[e] / (2.0 * A); // d value / d r2_e
    const double eta = relax.value(rho[e]);
    const Vec6 s = sigma.col(e);
    const double vm2 = von_mises_squared(s);
    out.d_density[e] =
        coef * 2.0 * eta * relax.derivative(rho[e]) * vm2 * inv_l2;
    const Vec6 g_sigma =
        (coef * eta * eta * inv_l2) * von_mises_squared_gradient(s);
    const Vec6 g_strain = C * g_sigma; // d/d eps (C symmetric)
    const Vec12 g_u = model.strain_matrix(e).transpose() * g_strain;
    for (int i = 0; i < 12; ++i)
      out.d_displacement[dofs(i, e)] += g_u[i];
    if (e0)
      out.d_eigenstrain.col(e) = -g_strain;
  }
  return out;
}

std::string stress_aggregation_label(int count) {
  std::ostringstream s;
  s << "ks_sqrt(P=" << kElasticKsParameter
    << ") of squared qp-relaxed von Mises ratio r=eta(rho)*vm(C(Bu-e0))/limit,"
       " eta=(sqrt(rho+eps)-sqrt(eps))/(sqrt(1+eps)-sqrt(eps)), over "
    << count
    << " non-void elements; max r <= value/limit <= sqrt(max r^2 + ln(n)/P)";
  return s.str();
}

DisplacementResponse
displacement_response(const ElasticModel &model,
                      const Eigen::Ref<const Eigen::VectorXd> &u, double limit,
                      bool derivatives) {
  if (!(std::isfinite(limit) && limit > 0))
    fail("invalid_input", "displacement limit must be finite and > 0");
  const TetMesh &m = model.mesh();
  const int N = m.node_count();
  Eigen::VectorXd r2(N);
  std::vector<char> use(N, 0);
  const double inv_l2 = 1.0 / (limit * limit);
  for (int n = 0; n < N; ++n) {
    r2[n] = u.segment<3>(3 * n).squaredNorm() * inv_l2;
    use[n] = m.node_referenced[n] != 0;
  }
  Eigen::VectorXd w;
  const KsResult k =
      ks_squared(r2, use, kElasticKsParameter, derivatives ? &w : nullptr);
  if (k.count == 0)
    fail("invalid_bc", "displacement constraint has no referenced node");
  DisplacementResponse out;
  const double A = std::sqrt(k.ks);
  out.count = k.count;
  out.value = limit * A;
  out.max_value = limit * std::sqrt(k.max_r2);
  out.max_node = k.argmax;
  out.upper_bound =
      limit * std::sqrt(k.max_r2 + std::log(static_cast<double>(k.count)) /
                                       kElasticKsParameter);
  if (!derivatives)
    return out;
  out.d_displacement.setZero(3 * N);
  for (int n = 0; n < N; ++n) {
    if (!use[n] || w[n] == 0.0)
      continue;
    const double coef = limit * w[n] / (2.0 * A);
    out.d_displacement.segment<3>(3 * n) =
        (coef * 2.0 * inv_l2) * u.segment<3>(3 * n);
  }
  return out;
}

std::string displacement_aggregation_label(int count) {
  std::ostringstream s;
  s << "ks_sqrt(P=" << kElasticKsParameter
    << ") of squared nodal displacement magnitude ratio r=|u_n|/limit over "
    << count
    << " referenced nodes; max r <= value/limit <= sqrt(max r^2 + ln(n)/P)";
  return s.str();
}

// ── Spectral operators
// ────────────────────────────────────────────────────────────
void assemble_mass(const ElasticSystem &sys, const Eigen::VectorXd &rho,
                   double density, Eigen::SparseMatrix<double> &M) {
  const ElasticModel &model = sys.model();
  if (rho.size() != model.element_count())
    fail("invalid_density", "density size does not match the mesh");
  const MassInterpolation mi{model.interpolation().floor};
  ReducedPattern::zero(M);
  Mat12 me;
  for (int e = 0; e < model.element_count(); ++e) {
    const double m =
        density * model.mesh().volume_m3[e] * mi.value(rho[e]) / 20.0;
    me.setZero();
    for (int a = 0; a < 4; ++a)
      for (int b = 0; b < 4; ++b)
        for (int c = 0; c < 3; ++c)
          me(3 * a + c, 3 * b + c) = (a == b ? 2.0 : 1.0) * m;
    sys.pattern().add(M, e, me);
  }
}

void assemble_geometric_stiffness(const ElasticSystem &sys,
                                  const Eigen::VectorXd &rho,
                                  const Eigen::Ref<const Eigen::VectorXd> &u,
                                  Eigen::SparseMatrix<double> &KG) {
  const ElasticModel &model = sys.model();
  if (rho.size() != model.element_count())
    fail("invalid_density", "density size does not match the mesh");
  const Interpolation &ip = model.interpolation();
  ReducedPattern::zero(KG);
  Mat12 kg;
  for (int e = 0; e < model.element_count(); ++e) {
    const Vec6 s = ip.scale(rho[e]) * (model.elasticity() * model.strain(e, u));
    Eigen::Matrix3d S;
    S << s[0], s[3], s[5], s[3], s[1], s[4], s[5], s[4], s[2];
    const Eigen::Matrix<double, 3, 4> G = model.mesh().element_gradients(e);
    const Eigen::Matrix4d H =
        model.mesh().volume_m3[e] * (G.transpose() * S * G);
    kg.setZero();
    for (int a = 0; a < 4; ++a)
      for (int b = 0; b < 4; ++b)
        for (int c = 0; c < 3; ++c)
          kg(3 * a + c, 3 * b + c) = H(a, b);
    sys.pattern().add(KG, e, kg);
  }
}

// ── Structural physics
// ────────────────────────────────────────────────────────────
namespace {

class StructuralPhysics final : public Physics {
public:
  explicit StructuralPhysics(std::shared_ptr<const Problem> problem)
      : problem_(std::move(problem)) {
    if (!problem_ || problem_->settings.physics != PhysicsKind::Structural)
      fail("physics_mismatch", "make_structural_physics needs physics "
                               "structural");
    const ObjectiveKind obj = problem_->settings.objective;
    if (obj != ObjectiveKind::Compliance && obj != ObjectiveKind::Mass)
      fail("physics_mismatch", std::string("structural objective ") +
                                   to_string(obj) + " is not implemented");
    if (obj == ObjectiveKind::Mass && !problem_->material.density_kg_m3)
      fail("material_missing", "objective mass needs material.density_g_cm3");
    if (!problem_->structural)
      fail("section_missing", "structural boundary missing");
    // Support sets = distinct fixed DOF lists; one factor each.
    const auto &cs = problem_->structural->cases;
    int sets = 0;
    for (std::size_t c = 0; c < cs.size(); ++c) {
      bool seen = false;
      for (std::size_t d = 0; d < c && !seen; ++d)
        seen = cs[d].fixed.dofs.size() == cs[c].fixed.dofs.size() &&
               cs[d].fixed.dofs == cs[c].fixed.dofs &&
               cs[d].fixed.values == cs[c].fixed.values;
      sets += seen ? 0 : 1;
    }
    const std::uint64_t n3 = 3ull * problem_->mesh->node_count();
    // forces, displacements, reactions, adjoint rhs/solution (2 per case).
    const std::uint64_t dense = n3 * sizeof(double) * cs.size() * 8ull;
    admit_elastic_memory(*problem_, std::max(1, sets), dense,
                         "topology structural evaluator");
    model_ = std::make_shared<ElasticModel>(problem_);
    cases_ = std::make_unique<ElasticCases>(model_);
  }

  PhysicsKind kind() const override { return PhysicsKind::Structural; }

  std::string formulation() const override {
    const Interpolation &ip = problem_->interpolation;
    std::ostringstream s;
    s << "tet4 linear elasticity (isotropic, small strain), "
      << (ip.kind == Interpolation::Kind::Simp ? "SIMP p=" : "RAMP q=")
      << ip.exponent << " stiffness floor " << ip.floor << ", "
      << (problem_->settings.objective == ObjectiveKind::Mass
              ? "mass objective (rho_m sum V rho)"
              : "weighted multi-case compliance f.u, self-adjoint gradient")
      << "; " << cases_->case_count() << " load case(s) on "
      << cases_->system_count()
      << " support set(s), one Cholesky factor per set";
    if (problem_->limits.stress_pa)
      s << "; stress: KS(P=" << kElasticKsParameter
        << ") of squared qp-relaxed von Mises ratio, adjoint";
    if (problem_->limits.displacement_m)
      s << "; displacement: KS(P=" << kElasticKsParameter
        << ") of squared nodal |u| ratio, adjoint";
    return s.str();
  }

  Evaluation evaluate(const Eigen::VectorXd &rho, bool derivatives,
                      const std::atomic<bool> *cancel) override {
    check_density(*problem_, rho);
    check_cancel(cancel);
    const ElasticModel &model = *model_;
    const StructuralBoundary &sb = *problem_->structural;
    const int C = cases_->case_count();
    const int E = model.element_count();
    const TetMesh &mesh = model.mesh();
    cases_->solve(rho, cases_->reference_forces(), cancel);
    check_cancel(cancel);

    Evaluation ev;
    ev.derivatives = derivatives;
    if (derivatives)
      ev.objective_gradient.setZero(E);
    nlohmann::json obj_ev = nlohmann::json::array();
    if (problem_->settings.objective == ObjectiveKind::Compliance) {
      ev.objective_unit = "J";
      for (int c = 0; c < C; ++c) {
        const ElasticCaseState &st = cases_->state(c);
        ev.objective += sb.cases[c].weight * st.compliance;
        obj_ev.push_back({{"case", sb.cases[c].name},
                          {"weight", sb.cases[c].weight},
                          {"compliance_j", st.compliance}});
        if (derivatives)
          model.add_stiffness_contraction(rho, st.displacement, st.displacement,
                                          -sb.cases[c].weight,
                                          ev.objective_gradient);
      }
    } else {
      const double dens = *problem_->material.density_kg_m3;
      ev.objective_unit = "kg";
      ev.objective = dens * mesh.volume_m3.dot(rho);
      if (derivatives)
        ev.objective_gradient = dens * mesh.volume_m3;
      obj_ev.push_back({{"density_kg_m3", dens}});
    }

    const std::optional<double> stress_limit = problem_->limits.stress_pa;
    const std::optional<double> disp_limit = problem_->limits.displacement_m;
    std::vector<StressResponse> stress(C);
    std::vector<DisplacementResponse> disp(C);
    const double field_stress_scale =
        stress_limit
            ? *stress_limit
            : (problem_->material.yield_pa ? *problem_->material.yield_pa
                                           : *problem_->material.youngs_pa);
    for (int c = 0; c < C; ++c) {
      check_cancel(cancel);
      const ElasticCaseState &st = cases_->state(c);
      stress[c] = stress_response(model, rho, st.displacement, nullptr,
                                  field_stress_scale,
                                  derivatives && stress_limit.has_value());
      if (disp_limit)
        disp[c] = displacement_response(model, st.displacement, *disp_limit,
                                        derivatives);
    }

    // One batched adjoint per support set: K lambda = d response / d u.
    if (derivatives && (stress_limit || disp_limit)) {
      const int per = (stress_limit ? 1 : 0) + (disp_limit ? 1 : 0);
      adjoint_rhs_.resize(model.dof_count(), C * per);
      adjoint_case_.assign(C * per, 0);
      int col = 0;
      for (int c = 0; c < C; ++c) {
        if (stress_limit) {
          adjoint_rhs_.col(col) = stress[c].d_displacement;
          adjoint_case_[col++] = c;
        }
        if (disp_limit) {
          adjoint_rhs_.col(col) = disp[c].d_displacement;
          adjoint_case_[col++] = c;
        }
      }
      check_cancel(cancel);
      cases_->solve_adjoint(adjoint_rhs_, adjoint_case_, adjoint_);
    }

    nlohmann::json resp_ev = nlohmann::json::array();
    int col = 0;
    for (int c = 0; c < C; ++c) {
      const std::string suffix = C > 1 ? "@" + sb.cases[c].name : "";
      const ElasticCaseState &st = cases_->state(c);
      if (stress_limit) {
        Constraint k;
        k.name = "max_von_mises" + suffix;
        k.unit = "Pa";
        k.response = stress[c].value;
        k.limit = *stress_limit;
        k.value = k.response / k.limit - 1.0;
        k.aggregation = stress_aggregation_label(stress[c].count);
        if (derivatives) {
          k.gradient = stress[c].d_density;
          model.add_stiffness_contraction(rho, adjoint_.col(col),
                                          st.displacement, -1.0, k.gradient);
          k.gradient /= k.limit;
          ++col;
        }
        resp_ev.push_back(
            {{"constraint", k.name},
             {"aggregate_pa", stress[c].value},
             {"raw_max_pa", stress[c].max_value},
             {"raw_max_element", stress[c].max_element},
             {"aggregate_upper_bound_pa", stress[c].upper_bound}});
        ev.constraints.push_back(std::move(k));
      }
      if (disp_limit) {
        Constraint k;
        k.name = "max_displacement" + suffix;
        k.unit = "m";
        k.response = disp[c].value;
        k.limit = *disp_limit;
        k.value = k.response / k.limit - 1.0;
        k.aggregation = displacement_aggregation_label(disp[c].count);
        if (derivatives) {
          k.gradient.setZero(E);
          model.add_stiffness_contraction(rho, adjoint_.col(col),
                                          st.displacement, -1.0, k.gradient);
          k.gradient /= k.limit;
          ++col;
        }
        resp_ev.push_back({{"constraint", k.name},
                           {"aggregate_m", disp[c].value},
                           {"raw_max_m", disp[c].max_value},
                           {"raw_max_node", disp[c].max_node},
                           {"aggregate_upper_bound_m", disp[c].upper_bound}});
        ev.constraints.push_back(std::move(k));
      }
    }

    // Fields of every case.
    const Interpolation &ip = problem_->interpolation;
    nlohmann::json stress_ev = nlohmann::json::array();
    for (int c = 0; c < C; ++c) {
      const std::string suffix = C > 1 ? "@" + sb.cases[c].name : "";
      const ElasticCaseState &st = cases_->state(c);
      Field u{"displacement" + suffix, "m", FieldLocation::Node, 3,
              st.displacement};
      Field vm{"von_mises" + suffix, "Pa", FieldLocation::Element, 1,
               stress[c].von_mises};
      Field sed{"strain_energy_density" + suffix, "J/m3",
                FieldLocation::Element, 1, Eigen::VectorXd(E)};
      for (int e = 0; e < E; ++e) {
        const Vec6 eps = model.strain(e, st.displacement);
        sed.values[e] =
            0.5 * ip.scale(rho[e]) * eps.dot(model.elasticity() * eps);
      }
      double solid_max = 0;
      for (int e = 0; e < E; ++e)
        if (rho[e] >= problem_->settings.density_threshold)
          solid_max = std::max(solid_max, stress[c].solid_von_mises[e]);
      stress_ev.push_back(
          {{"case", sb.cases[c].name},
           {"max_relaxed_von_mises_pa", stress[c].max_value},
           {"max_relaxed_element", stress[c].max_element},
           {"max_solid_von_mises_above_threshold_pa", solid_max}});
      ev.fields.push_back(std::move(u));
      ev.fields.push_back(std::move(vm));
      ev.fields.push_back(std::move(sed));
    }

    ev.evidence = {{"formulation", formulation()},
                   {"objective_terms", obj_ev},
                   {"elastic", cases_->evidence()},
                   {"stress",
                    {{"measure", "eta(rho)*von_mises(C B u), qp-relaxed"},
                     {"cases", stress_ev}}},
                   {"responses", resp_ev}};
    return ev;
  }

private:
  std::shared_ptr<const Problem> problem_;
  std::shared_ptr<const ElasticModel> model_;
  std::unique_ptr<ElasticCases> cases_;
  Eigen::MatrixXd adjoint_rhs_, adjoint_;
  std::vector<int> adjoint_case_;
};

} // namespace

std::unique_ptr<Physics>
make_structural_physics(std::shared_ptr<const Problem> problem) {
  return std::make_unique<StructuralPhysics>(std::move(problem));
}

} // namespace topology
} // namespace sim
} // namespace chudware
