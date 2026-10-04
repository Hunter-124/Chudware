#include "sim/topopt_thermal.h"

#include "sim/solver_threads.h"
#include "util/resource_governor.h"

#include <Eigen/SparseCholesky>
#ifdef CHUDWARE_WITH_CHOLMOD
#include <Eigen/CholmodSupport>
#endif

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <limits>
#include <numeric>
#include <string>
#include <utility>

namespace chudware {
namespace sim {
namespace topology {

namespace {

constexpr double kSolveTolerance = 1e-8; // relative residual ceiling
constexpr double kKsSharpness = 100.0;   // beta * temperature scale
constexpr int kCancelStride = 4096;      // elements between cancel polls

[[noreturn]] void fail(const std::string &code, const std::string &message) {
  throw ModelError(code, message);
}

void check_cancel(const std::atomic<bool> *cancel) {
  if (cancel && cancel->load(std::memory_order_relaxed))
    throw EvaluationCancelled();
}

std::string fmt(double v) {
  char buf[48];
  std::snprintf(buf, sizeof buf, "%.6g", v);
  return buf;
}

const std::shared_ptr<const Problem> &
require_problem(const std::shared_ptr<const Problem> &p) {
  if (!p || !p->mesh)
    fail("invalid_input",
         "thermal system needs a prepared problem with a mesh");
  return p;
}

int find_root(std::vector<int> &parent, int n) {
  while (parent[n] != n) {
    parent[n] = parent[parent[n]];
    n = parent[n];
  }
  return n;
}

// Convection film matrix h A/12 [2 1 1; 1 2 1; 1 1 2] scattered onto the
// owning tet's local indices (the face is the tet face opposite local node a).
void film_matrix(const TetMesh &m, int face, double h, Eigen::Matrix4d &hs) {
  hs.setZero();
  const int a = m.surface_local_face[face];
  const double c = h * m.surface_area_m2[face] / 12.0;
  for (int i = 0; i < 3; ++i)
    for (int j = 0; j < 3; ++j)
      hs(kTetFaces[a][i], kTetFaces[a][j]) = i == j ? 2.0 * c : c;
}

} // namespace

// ── Factor: numeric SPD factorization over the fixed reduced pattern
struct ThermalSystem::Factor {
#ifdef CHUDWARE_WITH_CHOLMOD
  Eigen::CholmodSupernodalLLT<Eigen::SparseMatrix<double>, Eigen::Lower> llt;
  static constexpr const char *kName = "cholmod_supernodal_llt";
#else
  Eigen::SimplicialLLT<Eigen::SparseMatrix<double>, Eigen::Lower> llt;
  static constexpr const char *kName = "eigen_simplicial_llt";
#endif
  bool analyzed = false;
};

ThermalSystem::ThermalSystem(std::shared_ptr<const Problem> problem)
    : problem_(require_problem(problem)), mesh_(*problem_->mesh) {
  const Problem &p = *problem_;
  const PhysicsKind phys = p.settings.physics;
  if (phys != PhysicsKind::Thermal && phys != PhysicsKind::Thermomechanical)
    fail("physics_mismatch",
         std::string("solid conduction system does not model physics ") +
             to_string(phys));
  if (!p.thermal)
    fail("section_missing", "thermal system needs a prepared thermal boundary");
  if (!p.material.conductivity_w_mk || !(*p.material.conductivity_w_mk > 0) ||
      !std::isfinite(*p.material.conductivity_w_mk))
    fail("material_missing", "thermal system needs a positive k_w_mk");
  const ThermalBoundary &tb = *p.thermal;
  const int N = mesh_.node_count();
  const int E = mesh_.element_count();
  k_solid_ = *p.material.conductivity_w_mk;
  reference_k_ = tb.reference_temperature_k;

  {
    std::string why;
    if (!chudware::governor::admit_fea(static_cast<std::uint64_t>(E), 4, 1,
                                       "topology thermal system", why))
      fail("memory_budget", why);
  }

  // Dirichlet in theta = T - T_ref.
  if (tb.fixed.dofs.size() != tb.fixed.values.size())
    fail("invalid_bc", "thermal Dirichlet dofs/values size mismatch");
  std::vector<std::pair<int, double>> entries;
  entries.reserve(static_cast<std::size_t>(tb.fixed.size()));
  for (int i = 0; i < tb.fixed.size(); ++i)
    entries.emplace_back(tb.fixed.dofs[i], tb.fixed.values[i] - reference_k_);
  reduction_ = make_dof_reduction(N, make_dirichlet(N, entries));

  // Well-posedness: every node-connected component of the conduction graph
  // needs a fixed referenced node or a convection face. The floor-scaled
  // conductivity keeps each element's operator positive on its gradients.
  {
    std::vector<int> parent(N);
    std::iota(parent.begin(), parent.end(), 0);
    for (int e = 0; e < E; ++e) {
      const int r0 = find_root(parent, mesh_.tets(0, e));
      for (int a = 1; a < 4; ++a) {
        const int ra = find_root(parent, mesh_.tets(a, e));
        if (ra != r0)
          parent[ra] = r0;
      }
    }
    std::vector<std::uint8_t> anchored(N, 0);
    for (int i = 0; i < tb.fixed.size(); ++i) {
      const int n = tb.fixed.dofs[i];
      if (mesh_.node_referenced[n])
        anchored[find_root(parent, n)] = 1;
    }
    for (const ThermalConvection &c : tb.convections)
      for (int f : c.patch.faces)
        anchored[find_root(parent, mesh_.surface_nodes(0, f))] = 1;
    int floating = 0;
    std::vector<std::uint8_t> seen(N, 0);
    for (int n = 0; n < N; ++n) {
      if (!mesh_.node_referenced[n])
        continue;
      const int r = find_root(parent, n);
      if (!seen[r]) {
        seen[r] = 1;
        floating += anchored[r] ? 0 : 1;
      }
    }
    if (floating > 0)
      fail("thermal_unconstrained",
           std::to_string(floating) +
               " conduction component(s) have neither a fixed temperature nor "
               "a convection boundary; the steady temperature is undefined");
  }

  element_nodes_ = nodal_dof_map(mesh_, 1);
  pattern_ = ReducedPattern(element_nodes_, reduction_);
  K_ = pattern_.pattern();
  const int nf = reduction_.free_size();

  // Surface loads and the fixed convection operator.
  load_full_ = Eigen::VectorXd::Zero(N);
  compliance_q_ = Eigen::VectorXd::Zero(N);
  heat_input_w_ = 0;
  for (const ThermalFlux &fx : tb.fluxes)
    for (int f : fx.patch.faces) {
      const double share = fx.flux_w_m2 * mesh_.surface_area_m2[f] / 3.0;
      for (int i = 0; i < 3; ++i) {
        load_full_[mesh_.surface_nodes(i, f)] += share;
        compliance_q_[mesh_.surface_nodes(i, f)] += share;
      }
      heat_input_w_ += 3.0 * share;
    }
  ReducedPattern::zero(K_);
  Eigen::Matrix4d hs;
  for (const ThermalConvection &c : tb.convections) {
    if (!(c.h_w_m2k > 0) || !std::isfinite(c.h_w_m2k))
      fail("invalid_bc", c.patch.label + " h_w_m2k must be > 0");
    const double ambient = c.ambient_k - reference_k_;
    for (int f : c.patch.faces) {
      film_matrix(mesh_, f, c.h_w_m2k, hs);
      pattern_.add(K_, mesh_.surface_element[f], hs);
      const double share = c.h_w_m2k * ambient * mesh_.surface_area_m2[f] / 3.0;
      for (int i = 0; i < 3; ++i)
        load_full_[mesh_.surface_nodes(i, f)] += share;
      conv_faces_.push_back(f);
      conv_face_h_.push_back(c.h_w_m2k);
    }
  }
  conv_values_ =
      Eigen::Map<const Eigen::VectorXd>(K_.valuePtr(), K_.nonZeros());
  reduction_.restrict_vector(load_full_, rhs_const_);
  for (std::size_t i = 0; i < conv_faces_.size(); ++i) {
    const int f = conv_faces_[i];
    film_matrix(mesh_, f, conv_face_h_[i], hs);
    pattern_.lift(rhs_const_, mesh_.surface_element[f], hs);
    bool touches = false;
    for (int j = 0; j < 3; ++j)
      touches =
          touches || reduction_.full_to_free[mesh_.surface_nodes(j, f)] < 0;
    if (touches)
      fixed_conv_faces_.push_back(static_cast<int>(i));
  }

  for (int e = 0; e < E; ++e) {
    bool fixed = false, nonzero = false;
    for (int a = 0; a < 4; ++a) {
      const int n = mesh_.tets(a, e);
      if (reduction_.full_to_free[n] < 0) {
        fixed = true;
        nonzero = nonzero || reduction_.prescribed[n] != 0.0;
      }
    }
    if (fixed)
      fixed_elements_.push_back(e);
    if (nonzero)
      lift_elements_.push_back(e);
  }

  fixture_of_node_.assign(N, -1);
  for (std::size_t i = 0; i < tb.fixtures.size(); ++i)
    for (int n : tb.fixtures[i].nodes)
      if (n >= 0 && n < N && reduction_.full_to_free[n] < 0 &&
          fixture_of_node_[n] < 0)
        fixture_of_node_[n] = static_cast<int>(i);

  // q_f equals the free right-hand side for every density iff nothing is
  // lifted (all prescribed theta zero) and every ambient sits at T_ref.
  bool self_adjoint = (reduction_.prescribed.array() == 0.0).all();
  for (const ThermalConvection &c : tb.convections)
    self_adjoint = self_adjoint && c.ambient_k == reference_k_;
  compliance_self_adjoint_ = self_adjoint;

  referenced_nodes_ = 0;
  for (int n = 0; n < N; ++n)
    referenced_nodes_ += mesh_.node_referenced[n] ? 1 : 0;

  factor_ = std::make_unique<Factor>();
  if (nf > 0) {
#ifdef CHUDWARE_WITH_CHOLMOD
    cap_solver_threads_for_host();
#endif
    factor_->llt.analyzePattern(K_);
    if (factor_->llt.info() != Eigen::Success)
      fail("singular_system", "symbolic analysis of the conduction operator "
                              "failed");
    factor_->analyzed = true;
  }

  density_.resize(E);
  rhs_free_.resize(nf);
  theta_free_.resize(nf);
  theta_ = Eigen::VectorXd::Zero(N);
  adjoint_rhs_free_.resize(nf);
  adjoint_free_.resize(nf);
  residual_free_.resize(nf);
  scratch_full_.resize(N);
  adjoint_full_.resize(N);
}

ThermalSystem::~ThermalSystem() = default;

double ThermalSystem::element_conductivity(int e) const {
  return k_solid_ * problem_->interpolation.scale(density_[e]);
}

double ThermalSystem::element_conductivity_derivative(int e) const {
  return k_solid_ * problem_->interpolation.scale_derivative(density_[e]);
}

void ThermalSystem::element_matrix(int e, double conductivity,
                                   Eigen::Matrix4d &ke) const {
  const auto G = mesh_.element_gradients(e);
  ke.noalias() = (conductivity * mesh_.volume_m3[e]) * (G.transpose() * G);
}

void ThermalSystem::assemble(const std::atomic<bool> *cancel) {
  std::copy(conv_values_.data(), conv_values_.data() + conv_values_.size(),
            K_.valuePtr());
  const int E = mesh_.element_count();
  Eigen::Matrix4d ke;
  for (int e = 0; e < E; ++e) {
    if (e % kCancelStride == 0)
      check_cancel(cancel);
    element_matrix(e, element_conductivity(e), ke);
    pattern_.add(K_, e, ke);
  }
  rhs_free_ = rhs_const_;
  for (int e : lift_elements_) {
    element_matrix(e, element_conductivity(e), ke);
    pattern_.lift(rhs_free_, e, ke);
  }
}

void ThermalSystem::factor_solve(const Eigen::VectorXd &rhs,
                                 Eigen::VectorXd &x_free, const char *what,
                                 double &relative_residual) const {
  x_free = factor_->llt.solve(rhs);
  if (factor_->llt.info() != Eigen::Success || !x_free.allFinite())
    fail("singular_system", std::string("conduction ") + what +
                                " solve produced no finite "
                                "solution");
  residual_free_.noalias() = K_ * x_free;
  residual_free_ -= rhs;
  const double rn = residual_free_.norm();
  const double bn = rhs.norm();
  relative_residual =
      bn > 0 ? rn / bn
             : (rn == 0 ? 0.0 : std::numeric_limits<double>::infinity());
  if (!(relative_residual <= kSolveTolerance))
    fail("solve_inaccurate",
         std::string("conduction ") + what + " solve relative residual " +
             fmt(relative_residual) + " exceeds " + fmt(kSolveTolerance));
}

void ThermalSystem::solve(const Eigen::VectorXd &density,
                          const std::atomic<bool> *cancel) {
  solved_ = false;
  check_density(*problem_, density);
  check_cancel(cancel);
  if (chudware::governor::low_memory_now())
    fail("memory_low", "host memory is critically low; thermal solve stopped "
                       "before refactorization");
  density_ = density;
  assemble(cancel);
  check_cancel(cancel);
  if (reduction_.free_size() > 0) {
#ifdef CHUDWARE_WITH_CHOLMOD
    cap_solver_threads_for_host();
#endif
    factor_->llt.factorize(K_);
    ++numeric_factorizations_;
    if (factor_->llt.info() != Eigen::Success)
      fail("singular_system",
           "conduction operator is not positive definite at this density "
           "(zero-conductance region detached from every fixed temperature "
           "and convection boundary)");
    factor_solve(rhs_free_, theta_free_, "temperature", relative_residual_);
  } else {
    relative_residual_ = 0;
  }
  reduction_.expand_vector(theta_free_, theta_);
  max_adjoint_residual_ = 0;
  solved_ = true;
}

void ThermalSystem::require_solved(const char *what) const {
  if (!solved_)
    fail("invalid_state",
         std::string(what) + " needs a successful ThermalSystem::solve first");
}

void ThermalSystem::temperature(Eigen::VectorXd &out_kelvin) const {
  require_solved("temperature");
  out_kelvin = theta_.array() + reference_k_;
}

void ThermalSystem::solve_adjoint(const Eigen::VectorXd &rhs,
                                  Eigen::VectorXd &lambda,
                                  const std::atomic<bool> *cancel) const {
  require_solved("adjoint");
  const int N = mesh_.node_count();
  if (rhs.size() != N)
    fail("invalid_input", "adjoint right-hand side must have one entry per "
                          "node");
  check_cancel(cancel);
  reduction_.restrict_vector(rhs, adjoint_rhs_free_);
  if (reduction_.free_size() > 0) {
    double rel = 0;
    factor_solve(adjoint_rhs_free_, adjoint_free_, "adjoint", rel);
    max_adjoint_residual_ = std::max(max_adjoint_residual_, rel);
  }
  lambda.setZero(N); // after restrict: rhs may alias lambda
  for (int i = 0; i < reduction_.free_size(); ++i)
    lambda[reduction_.free_to_full[i]] = adjoint_free_[i];
}

void ThermalSystem::add_state_gradient(const Eigen::VectorXd &lambda,
                                       double weight,
                                       Eigen::VectorXd &grad) const {
  require_solved("state gradient");
  const int E = mesh_.element_count();
  if (lambda.size() != mesh_.node_count() || grad.size() != E)
    fail("invalid_input", "state gradient needs N adjoint and E gradient "
                          "entries");
  Eigen::Vector4d le, te;
  for (int e = 0; e < E; ++e) {
    const double dk = element_conductivity_derivative(e);
    if (dk == 0.0)
      continue;
    for (int a = 0; a < 4; ++a) {
      le[a] = lambda[mesh_.tets(a, e)];
      te[a] = theta_[mesh_.tets(a, e)];
    }
    const auto G = mesh_.element_gradients(e);
    const Eigen::Vector3d gl = G * le, gt = G * te;
    grad[e] -= weight * dk * mesh_.volume_m3[e] * gl.dot(gt);
  }
}

double
ThermalSystem::thermal_compliance(Eigen::VectorXd *gradient,
                                  const std::atomic<bool> *cancel) const {
  require_solved("thermal compliance");
  const double c = compliance_q_.dot(theta_);
  if (gradient) {
    gradient->setZero(mesh_.element_count());
    if (compliance_self_adjoint_) {
      // K_ff theta_f = q_f exactly and theta_p = 0: the adjoint is theta.
      add_state_gradient(theta_, 1.0, *gradient);
    } else {
      solve_adjoint(compliance_q_, adjoint_full_, cancel);
      add_state_gradient(adjoint_full_, 1.0, *gradient);
    }
  }
  return c;
}

double ThermalSystem::max_temperature_beta(double limit_k) const {
  return kKsSharpness / std::max(std::abs(limit_k - reference_k_), 1.0);
}

double ThermalSystem::peak_temperature_k(int *node) const {
  require_solved("peak temperature");
  double peak = -std::numeric_limits<double>::infinity();
  int at = -1;
  for (int n = 0; n < mesh_.node_count(); ++n)
    if (mesh_.node_referenced[n] && theta_[n] > peak) {
      peak = theta_[n];
      at = n;
    }
  if (node)
    *node = at;
  return peak + reference_k_;
}

Constraint ThermalSystem::max_temperature_constraint(
    double limit_k, bool derivatives, const std::atomic<bool> *cancel) const {
  require_solved("max temperature");
  if (!(limit_k > 0) || !std::isfinite(limit_k))
    fail("invalid_input", "max temperature limit must be a positive kelvin "
                          "value");
  const int N = mesh_.node_count();
  const double beta = max_temperature_beta(limit_k);
  // Shift by the peak node's own theta and add the log term to the peak's
  // kelvin value: the peak term is then exactly exp(0) = 1, so log(sum) >= 0
  // and KS >= the nodal peak holds bit-for-bit. Re-deriving the peak as
  // (peak_k - ref) + ref can round one ulp below it (seen on MSVC).
  int peak_node = -1;
  const double peak_k = peak_temperature_k(&peak_node);
  const double peak = peak_node >= 0 ? theta_[peak_node] : peak_k - reference_k_;
  double sum = 0;
  for (int n = 0; n < N; ++n)
    if (mesh_.node_referenced[n])
      sum += std::exp(beta * (theta_[n] - peak));
  const double ks = peak_k + std::log(sum) / beta;

  Constraint c;
  c.name = "max_temperature";
  c.unit = "K";
  c.response = ks;
  c.limit = limit_k;
  c.value = ks / limit_k - 1.0;
  c.aggregation = "ks_upper(beta=" + fmt(beta) +
                  "/K, nodes=" + std::to_string(referenced_nodes_) + ")";
  if (derivatives) {
    for (int n = 0; n < N; ++n)
      scratch_full_[n] = mesh_.node_referenced[n]
                             ? std::exp(beta * (theta_[n] - peak)) / sum
                             : 0.0;
    solve_adjoint(scratch_full_, adjoint_full_, cancel);
    c.gradient = Eigen::VectorXd::Zero(mesh_.element_count());
    add_state_gradient(adjoint_full_, 1.0 / limit_k, c.gradient);
  }
  return c;
}

void ThermalSystem::heat_flux(Eigen::VectorXd &out) const {
  require_solved("heat flux");
  const int E = mesh_.element_count();
  out.resize(3 * E);
  Eigen::Vector4d te;
  for (int e = 0; e < E; ++e) {
    for (int a = 0; a < 4; ++a)
      te[a] = theta_[mesh_.tets(a, e)];
    out.segment<3>(3 * e) =
        -element_conductivity(e) * (mesh_.element_gradients(e) * te);
  }
}

nlohmann::json ThermalSystem::evidence() const {
  require_solved("thermal evidence");
  using nlohmann::json;
  const ThermalBoundary &tb = *problem_->thermal;
  const int N = mesh_.node_count();

  // Support heat flow r = (K theta - f) on constrained nodes (+ into body).
  Eigen::VectorXd &r = scratch_full_;
  r.setZero();
  Eigen::Matrix4d ke;
  Eigen::Vector4d te;
  for (int e : fixed_elements_) {
    element_matrix(e, element_conductivity(e), ke);
    for (int a = 0; a < 4; ++a)
      te[a] = theta_[mesh_.tets(a, e)];
    const Eigen::Vector4d q = ke * te;
    for (int a = 0; a < 4; ++a)
      if (reduction_.full_to_free[mesh_.tets(a, e)] < 0)
        r[mesh_.tets(a, e)] += q[a];
  }
  for (int i : fixed_conv_faces_) {
    const int f = conv_faces_[i];
    const int e = mesh_.surface_element[f];
    film_matrix(mesh_, f, conv_face_h_[i], ke);
    for (int a = 0; a < 4; ++a)
      te[a] = theta_[mesh_.tets(a, e)];
    const Eigen::Vector4d q = ke * te;
    for (int a = 0; a < 4; ++a)
      if (reduction_.full_to_free[mesh_.tets(a, e)] < 0)
        r[mesh_.tets(a, e)] += q[a];
  }

  double net = 0, gross = 0;
  json flux = json::array();
  for (const ThermalFlux &fx : tb.fluxes) {
    double area = 0;
    for (int f : fx.patch.faces)
      area += mesh_.surface_area_m2[f];
    const double power = fx.flux_w_m2 * area;
    net += power;
    gross += std::abs(power);
    flux.push_back(
        {{"label", fx.patch.label},
         {"source", fx.source == ThermalLoadKind::Power ? "power" : "flux"},
         {"input_value", fx.input_value},
         {"flux_w_m2", fx.flux_w_m2},
         {"area_m2", area},
         {"power_w", power}});
  }
  json conv = json::array();
  for (const ThermalConvection &c : tb.convections) {
    double area = 0, power = 0;
    for (int f : c.patch.faces) {
      const double A = mesh_.surface_area_m2[f];
      double mean = 0;
      for (int i = 0; i < 3; ++i)
        mean += theta_[mesh_.surface_nodes(i, f)];
      mean /= 3.0;
      area += A;
      power += c.h_w_m2k * A * (c.ambient_k - reference_k_ - mean);
    }
    net += power;
    gross += std::abs(power);
    conv.push_back({{"label", c.patch.label},
                    {"h_w_m2k", c.h_w_m2k},
                    {"ambient_k", c.ambient_k},
                    {"area_m2", area},
                    {"power_w", power}});
  }
  std::vector<double> fixture_power(tb.fixtures.size(), 0.0);
  std::vector<int> fixture_nodes(tb.fixtures.size(), 0);
  double other = 0;
  for (int i = 0; i < reduction_.full_size; ++i) {
    if (reduction_.full_to_free[i] >= 0)
      continue;
    const double support = r[i] - load_full_[i];
    const int fx = fixture_of_node_[i];
    if (fx >= 0) {
      fixture_power[fx] += support;
      ++fixture_nodes[fx];
    } else {
      other += support;
    }
  }
  json fixtures = json::array();
  for (std::size_t i = 0; i < tb.fixtures.size(); ++i) {
    net += fixture_power[i];
    gross += std::abs(fixture_power[i]);
    fixtures.push_back({{"label", tb.fixtures[i].label},
                        {"temperature_k", tb.fixtures[i].temperature_k},
                        {"nodes", fixture_nodes[i]},
                        {"power_w", fixture_power[i]}});
  }
  net += other;
  gross += std::abs(other);

  double tmin = std::numeric_limits<double>::infinity();
  for (int n = 0; n < N; ++n)
    if (mesh_.node_referenced[n])
      tmin = std::min(tmin, theta_[n]);
  int peak_node = -1;
  const double peak = peak_temperature_k(&peak_node);

  return {
      {"solver", Factor::kName},
      {"free_dofs", reduction_.free_size()},
      {"fixed_dofs", reduction_.full_size - reduction_.free_size()},
      {"pattern_nonzeros", K_.nonZeros()},
      {"symbolic_analyses", factor_->analyzed ? 1 : 0},
      {"numeric_factorizations", numeric_factorizations_},
      {"relative_residual", relative_residual_},
      {"adjoint_relative_residual_max", max_adjoint_residual_},
      {"reference_temperature_k", reference_k_},
      {"temperature_k",
       {{"min", tmin + reference_k_}, {"max", peak}, {"max_node", peak_node}}},
      {"heat_flow_w",
       {{"sign", "positive into the body"},
        {"flux", std::move(flux)},
        {"convection", std::move(conv)},
        {"fixtures", std::move(fixtures)},
        {"other_fixed_w", other},
        {"net_w", net},
        {"gross_w", gross},
        {"relative_imbalance", gross > 0 ? std::abs(net) / gross : 0.0}}}};
}

// ── Thermal-compliance Physics
namespace {

class ThermalPhysics final : public Physics {
public:
  explicit ThermalPhysics(std::shared_ptr<const Problem> problem)
      : problem_(problem), system_(std::move(problem)) {
    const Interpolation &ip = problem_->interpolation;
    formulation_ =
        std::string(
            "tet4 steady conduction, k = k_s (eps + (1 - eps) f(rho)), ") +
        (ip.kind == Interpolation::Kind::Simp
             ? "simp f = rho^" + fmt(ip.exponent)
             : "ramp f = rho/(1+" + fmt(ip.exponent) + "(1-rho))") +
        ", eps = " + fmt(ip.floor) +
        "; thermal compliance = heat-input-weighted temperature rise over "
        "T_ref (W*K); fixed convection film; lifted Dirichlet; adjoint "
        "gradient";
    if (problem_->limits.temperature_k)
      formulation_ += "; max temperature by KS upper bound, adjoint gradient";
  }

  PhysicsKind kind() const override { return PhysicsKind::Thermal; }
  std::string formulation() const override { return formulation_; }

  Evaluation evaluate(const Eigen::VectorXd &density, bool derivatives,
                      const std::atomic<bool> *cancel) override {
    Evaluation ev;
    ev.derivatives = derivatives;
    system_.solve(density, cancel);
    ev.objective = system_.thermal_compliance(
        derivatives ? &ev.objective_gradient : nullptr, cancel);
    ev.objective_unit = "W*K";

    nlohmann::json limit_evidence = nullptr;
    if (problem_->limits.temperature_k) {
      const double limit = *problem_->limits.temperature_k;
      Constraint c =
          system_.max_temperature_constraint(limit, derivatives, cancel);
      int node = -1;
      const double peak = system_.peak_temperature_k(&node);
      const double beta = system_.max_temperature_beta(limit);
      limit_evidence = {
          {"limit_k", limit},
          {"ks_k", c.response},
          {"raw_peak_k", peak},
          {"raw_peak_node", node},
          {"raw_value", peak / limit - 1.0},
          {"beta_per_k", beta},
          {"ks_minus_peak_k", c.response - peak},
          {"ks_gap_bound_k",
           std::log(static_cast<double>(system_.referenced_node_count())) /
               beta}};
      ev.constraints.push_back(std::move(c));
    }
    check_cancel(cancel);

    Field temperature;
    temperature.name = "temperature";
    temperature.unit = "K";
    temperature.location = FieldLocation::Node;
    temperature.components = 1;
    system_.temperature(temperature.values);
    ev.fields.push_back(std::move(temperature));
    Field flux;
    flux.name = "heat_flux";
    flux.unit = "W/m2";
    flux.location = FieldLocation::Element;
    flux.components = 3;
    system_.heat_flux(flux.values);
    ev.fields.push_back(std::move(flux));

    ev.evidence = system_.evidence();
    const double q = system_.heat_input_w();
    ev.evidence["objective"] = {
        {"name", "thermal_compliance"},
        {"definition", "sum over flux/power patches of integral q (T - T_ref) "
                       "dA"},
        {"unit", "W*K"},
        {"value", ev.objective},
        {"heat_input_w", q},
        {"mean_heated_temperature_rise_k",
         q != 0 ? nlohmann::json(ev.objective / q) : nlohmann::json(nullptr)}};
    if (!limit_evidence.is_null())
      ev.evidence["max_temperature"] = std::move(limit_evidence);
    return ev;
  }

private:
  std::shared_ptr<const Problem> problem_;
  ThermalSystem system_;
  std::string formulation_;
};

} // namespace

std::unique_ptr<Physics>
make_thermal_physics(std::shared_ptr<const Problem> problem) {
  require_problem(problem);
  if (problem->settings.physics != PhysicsKind::Thermal)
    fail("physics_mismatch",
         std::string("thermal compliance physics needs physics thermal, got ") +
             to_string(problem->settings.physics));
  if (problem->settings.objective != ObjectiveKind::ThermalCompliance)
    fail("objective_not_supported",
         std::string("thermal physics optimizes thermal_compliance, got ") +
             to_string(problem->settings.objective));
  if (!problem->thermal)
    fail("section_missing",
         "thermal physics needs a prepared thermal boundary");
  bool driven = false;
  for (const ThermalFlux &fx : problem->thermal->fluxes)
    driven = driven || (!fx.patch.faces.empty() && fx.flux_w_m2 != 0.0);
  if (!driven)
    fail("zero_load", "thermal compliance needs a non-zero flux or power heat "
                      "input on at least one surface");
  return std::make_unique<ThermalPhysics>(std::move(problem));
}

} // namespace topology
} // namespace sim
} // namespace chudware
