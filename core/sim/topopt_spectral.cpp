// Topology optimization: modal frequency and linear buckling evaluators on the
// shared tet4 elasticity (see topopt_elastic.h).
//
// Eigenpairs come from the shared Spectra ladder `fea_shift_invert` (zero
// shift for K phi = lambda M phi, K positive definite because the prepared
// problem is face-connected with every rigid-body mode restrained; zero-shift K
// inverse for the reciprocal buckling pencil K_G phi = mu K phi, lambda =
// -1/mu). Positivity is never decided by a magnitude cutoff relative to the
// stiffness/mass scale: a slender restrained member legitimately has
// lambda_1 ~ 1e-12 trace(K)/trace(M).
//
// Aggregate. a > 0 solves sum_j chi(lambda_j / a) (lambda_j / a)^-P = 1 with
// P = kSpectralSoftMinPower and chi = 1 below 1.25, a C2 quintic decay to 0 at
// 2. It is a C2 symmetric function of the whole spectrum (positively
// homogeneous of degree one), so it is continuous and differentiable through
// repeated, crossing and window-entering eigenvalues, and it never depends on
// how many modes the eigensolver was asked for. lambda_1 N^(-1/P) <= a <=
// lambda_1 for N modes with nonzero weight (N <= 24 keeps chi(lambda_1/a) = 1).
// Every eigenvalue below 2a is computed; completeness is checked by the
// Sylvester inertia of the shifted pencil (LDLT pivot signs) at a count point
// kept away from computed eigenvalues, and the request grows until the counts
// match (a Krylov solver may miss a copy of a repeated eigenvalue).
// Eigenpairs are accepted on their normwise backward error.
//
// Memory per evaluation: one K factorization inside the eigen ladder, one
// indefinite LDLT for counting, and a Lanczos basis of at most
// n_free x max(3*24+1, 60) doubles; no dense n x n matrix.
#include "sim/topopt_elastic.h"

#include "sim/fea_eigen.h"

#include <Eigen/SparseCholesky>

#include <algorithm>
#include <cmath>
#include <limits>
#include <numeric>
#include <sstream>

namespace chudware {
namespace sim {
namespace topology {

namespace {

using Vec6 = Eigen::Matrix<double, 6, 1>;
using Vec12 = Eigen::Matrix<double, 12, 1>;
using SpMat = Eigen::SparseMatrix<double>;

constexpr double kPi = 3.14159265358979323846;
// Reported clusters join eigenvalues closer than this relative gap; inertia
// count points stay at least this far (relative) from every computed
// eigenvalue so no pivot sign decides a near-tie.
constexpr double kClusterGap = 1e-3;
constexpr int kInitialModes = 6;
constexpr int kMaxModes = 24;
// Aggregate window (see file header): full weight for lambda <= kWindowFull a,
// none from kWindowEnd a. kWindowFull >= kMaxModes^(1/P) = 1.2197 keeps the
// lowest mode at full weight, hence a <= lambda_1.
constexpr double kWindowFull = 1.25;
constexpr double kWindowEnd = 2.0;
// Normwise backward error ||A phi - lambda B phi|| /
// ((||A||_inf + |lambda| ||B||_inf) ||phi||) accepted for an eigenpair.
constexpr double kMaxBackwardError = 1e-8;
// Reciprocal buckling eigenvalues mu with |mu| below this fraction of the
// Jacobi-scaled prestress coupling max_ij |KG_ij| / sqrt(K_ii K_jj) are
// roundoff, not compressive modes (load factor beyond 1e9 / scale).
constexpr double kCompressiveTolerance = 1e-9;

[[noreturn]] void fail(const std::string &code, const std::string &message) {
  throw ModelError(code, message);
}

void check_cancel(const std::atomic<bool> *cancel) {
  if (cancel && cancel->load(std::memory_order_relaxed))
    throw EvaluationCancelled();
}

std::string fmt(double v) {
  std::ostringstream s;
  s.precision(8);
  s << v;
  return s.str();
}

// Sylvester inertia counter on one fixed sparse structure: number of pencil
// eigenvalues below c = negative LDLT pivots of (A - c B).
class InertiaCounter {
public:
  void reset(const Eigen::SparseMatrix<double> &structure) {
    work_ = structure;
    analyzed_ = false;
  }
  int count_below(const Eigen::SparseMatrix<double> &A,
                  const Eigen::SparseMatrix<double> &B, double c) {
    if (A.nonZeros() != work_.nonZeros() || B.nonZeros() != work_.nonZeros())
      fail("pattern_mismatch",
           "inertia count operators do not share a pattern");
    for (int attempt = 0; attempt < 3; ++attempt) {
      const double *a = A.valuePtr();
      const double *b = B.valuePtr();
      double *w = work_.valuePtr();
      for (Eigen::Index k = 0; k < work_.nonZeros(); ++k)
        w[k] = a[k] - c * b[k];
      if (!analyzed_) {
        ldlt_.analyzePattern(work_);
        analyzed_ = true;
      }
      ldlt_.factorize(work_);
      ++factorizations_;
      if (ldlt_.info() == Eigen::Success && ldlt_.vectorD().allFinite())
        return static_cast<int>((ldlt_.vectorD().array() < 0.0).count());
      // An exactly zero pivot: c hit an eigenvalue or a pivot cancelled. The
      // boundary sits inside a relative gap, so a tiny shift is harmless.
      c += 1e-9 * std::max(std::abs(c), std::numeric_limits<double>::min());
    }
    fail("eigen_count_failed", "inertia factorization of the shifted pencil "
                               "failed at c=" +
                                   fmt(c));
  }
  long factorizations() const { return factorizations_; }

private:
  Eigen::SparseMatrix<double> work_;
  Eigen::SimplicialLDLT<Eigen::SparseMatrix<double>, Eigen::Lower,
                        Eigen::AMDOrdering<int>>
      ldlt_;
  bool analyzed_ = false;
  long factorizations_ = 0;
};

// Every pencil eigenvalue (modal) or compressive load factor (buckling) below
// `complete_below`, ascending, with its normalized eigenvector.
struct Spectrum {
  std::vector<double> values;
  Eigen::MatrixXd vectors; // free x J, M- (modal) or K- (buckling) normal
  int requested = 0, solves = 0, counts = 0;
  int compressive_total = 0;    // buckling: inertia count of mu < -tol
  bool all_compressive = false; // buckling: every compressive mode computed
  double complete_below = 0;    // inertia-verified (+inf: all_compressive)
  double max_backward_error = 0;
};

std::vector<int> ascending(const Eigen::VectorXd &v) {
  std::vector<int> idx(v.size());
  std::iota(idx.begin(), idx.end(), 0);
  std::sort(idx.begin(), idx.end(), [&](int a, int b) { return v[a] < v[b]; });
  return idx;
}

double max_abs_column_sum(const SpMat &A) {
  double best = 0;
  for (int j = 0; j < A.outerSize(); ++j) {
    double sum = 0;
    for (SpMat::InnerIterator it(A, j); it; ++it)
      sum += std::abs(it.value());
    best = std::max(best, sum);
  }
  return best;
}

// max_ij |G_ij| / sqrt(K_ii K_jj) over G's stored entries: the prestress
// coupling in K's Jacobi metric, off-diagonal entries included (pure shear can
// leave every diagonal entry of K_G exactly zero while K_G is indefinite).
double coupling_scale(const SpMat &G, const SpMat &K) {
  const Eigen::VectorXd d = K.diagonal();
  double best = 0;
  for (int j = 0; j < G.outerSize(); ++j)
    for (SpMat::InnerIterator it(G, j); it; ++it) {
      const double dd = d[it.row()] * d[j];
      if (dd > 0)
        best = std::max(best, std::abs(it.value()) / std::sqrt(dd));
    }
  return best;
}

// Smallest point >= c0 at least kClusterGap (relative) away from every value.
double count_point(const std::vector<double> &ascending_values, double c0) {
  double c = c0;
  for (double v : ascending_values)
    if (std::abs(v - c) <= kClusterGap * c)
      c = v * (1.0 + 2.0 * kClusterGap);
  return c;
}

// chi(s): 1 for s <= kWindowFull, 0 for s >= kWindowEnd, 1 - (10x^3 - 15x^4 +
// 6x^5) between (x the position in the band): C2 at both ends.
double window(double s, double &dchi) {
  dchi = 0;
  if (s <= kWindowFull)
    return 1.0;
  if (s >= kWindowEnd)
    return 0.0;
  const double w = kWindowEnd - kWindowFull, x = (s - kWindowFull) / w;
  dchi = -30.0 * x * x * (1 - x) * (1 - x) / w;
  return 1.0 - x * x * x * (10.0 + x * (-15.0 + 6.0 * x));
}

// a solves sum_j psi(lambda_j / a) = 1, psi(s) = chi(s) s^-P; weights are
// d a / d lambda_j = psi'(s_j) / sum_k psi'(s_k) s_k (sum_j w_j lambda_j = a).
// `values` must hold every eigenvalue below kWindowEnd * a (the caller
// verifies this by inertia); modes beyond carry exactly zero weight.
struct SoftMin {
  double value = 0;
  std::vector<double> weights;
  std::size_t active = 0; // modes with nonzero weight
};
SoftMin soft_min(const std::vector<double> &values) {
  constexpr double P = kSpectralSoftMinPower;
  const double lo = *std::min_element(values.begin(), values.end());
  // Phi(x) = sum_j psi(v_j / (lo x)) - 1 increases strictly in x = a / lo;
  // Phi(1) >= psi(1) - 1 = 0 and Phi(n^(-1/P)) <= 0 bracket the root.
  auto residual = [&](double x, double &slope) {
    double f = -1.0;
    slope = 0.0;
    for (double v : values) {
      const double s = v / (lo * x);
      double dchi;
      const double chi = window(s, dchi);
      if (chi == 0.0)
        continue;
      const double sp = std::pow(s, -P);
      f += chi * sp;
      slope -= (dchi * sp - P * chi * sp / s) * s / x;
    }
    return f;
  };
  const double eps = std::numeric_limits<double>::epsilon();
  double x_lo = std::pow(static_cast<double>(values.size()), -1.0 / P);
  double x_hi = 1.0;
  double sum = 0;
  for (double v : values)
    sum += std::pow(v / lo, -P);
  double x = std::clamp(std::pow(sum, -1.0 / P), x_lo, x_hi);
  for (int it = 0; it < 200; ++it) {
    double slope;
    const double f = residual(x, slope);
    if (f == 0.0)
      break;
    if (f > 0.0)
      x_hi = x;
    else
      x_lo = x;
    double next = x - f / slope;
    if (!(next > x_lo && next < x_hi))
      next = 0.5 * (x_lo + x_hi);
    const bool done =
        std::abs(next - x) <= 4 * eps * x || x_hi - x_lo <= 4 * eps * x_hi;
    x = next;
    if (done)
      break;
  }
  SoftMin out;
  out.value = lo * x;
  std::vector<double> dpsi(values.size());
  double denom = 0;
  for (std::size_t j = 0; j < values.size(); ++j) {
    const double s = values[j] / out.value;
    double dchi;
    const double chi = window(s, dchi);
    const double sp = std::pow(s, -P);
    dpsi[j] = dchi * sp - P * chi * sp / s;
    denom += dpsi[j] * s;
    out.active += chi > 0.0 ? 1 : 0;
  }
  out.weights.reserve(values.size());
  for (double d : dpsi)
    out.weights.push_back(d / denom);
  return out;
}

// Growing lowest-mode request on one pencil. Modal: K phi = lambda M phi
// (B = M, zero shift). Buckling: K_G phi = mu K phi (B = K_G), load factor
// lambda = -1/mu over mu < -tol. `computed` holds the ascending lambdas of the
// last solve and `vecs` their eigenvectors.
struct Ladder {
  const SpMat *K = nullptr, *B = nullptr;
  InertiaCounter *counter = nullptr;
  bool buckling = false;
  double tol = 0;
  int limit = 0, nev = 0;
  Spectrum sp;
  std::vector<double> computed;
  Eigen::MatrixXd vecs;

  void solve(const std::atomic<bool> *cancel) {
    check_cancel(cancel);
    Eigen::VectorXd vals;
    Eigen::MatrixXd v;
    if (!fea_shift_invert(*K, *B, nev, 0.0, buckling, vals, v))
      fail("eigen_solve_failed", std::string(buckling ? "buckling reciprocal"
                                                      : "modal shift-invert") +
                                     " eigensolve did not converge (" +
                                     std::to_string(nev) + " modes)");
    ++sp.solves;
    if (vals.size() < nev || v.cols() < nev || !vals.allFinite())
      fail("eigen_solve_failed",
           "eigensolve returned incomplete or non-finite modes");
    std::vector<int> order;
    for (int i : ascending(vals))
      if (!buckling || vals[i] < -tol)
        order.push_back(i);
    if (static_cast<int>(order.size()) < nev)
      fail("eigen_incomplete",
           "inertia reports " + std::to_string(sp.compressive_total) +
               " compressive modes but the eigensolver returned " +
               std::to_string(order.size()) + " of " + std::to_string(nev));
    // K is positive definite for a prepared (connected, restrained) model, so
    // a non-positive Ritz value is a numerical failure, never a rigid mode.
    if (!buckling && !(vals[order[0]] > 0))
      fail("eigen_solve_failed",
           "eigensolver returned eigenvalue " + fmt(vals[order[0]]) +
               " <= 0 for a restrained model (stiffness not numerically "
               "positive definite)");
    computed.resize(order.size());
    vecs.resize(v.rows(), static_cast<Eigen::Index>(order.size()));
    for (std::size_t j = 0; j < order.size(); ++j) {
      computed[j] = buckling ? -1.0 / vals[order[j]] : vals[order[j]];
      vecs.col(static_cast<Eigen::Index>(j)) = v.col(order[j]);
    }
    sp.requested = nev;
    sp.all_compressive = buckling && nev == sp.compressive_total;
    sp.complete_below = 0; // a new solve is verified afresh
  }

  // True when every lambda below c0 is in `computed`. Otherwise grows the
  // request, re-solves and returns false; refuses rather than drop modes.
  bool verify(double c0, const std::atomic<bool> *cancel) {
    if (sp.all_compressive || c0 <= sp.complete_below)
      return true;
    const double c = count_point(computed, c0);
    check_cancel(cancel);
    const int cnt = buckling ? counter->count_below(*B, *K, -1.0 / c)
                             : counter->count_below(*K, *B, c);
    ++sp.counts;
    const int below =
        static_cast<int>(std::lower_bound(computed.begin(), computed.end(), c) -
                         computed.begin());
    if (cnt == below) {
      sp.complete_below = c;
      return true;
    }
    if (cnt < below)
      fail("eigen_incomplete", "inertia counts " + std::to_string(cnt) +
                                   " eigenvalues below " + fmt(c) + " but " +
                                   std::to_string(below) +
                                   " converged eigenpairs lie there");
    if (nev >= limit)
      fail("eigen_incomplete",
           "the aggregate window below " + fmt(c) + " holds " +
               std::to_string(cnt) + " modes, more than the " +
               std::to_string(limit) +
               "-mode eigen request cap; refusing to drop modes from the "
               "aggregate");
    nev = std::min(limit, std::max(cnt + 2, nev + 2));
    solve(cancel);
    return false;
  }

  // Normalized pairs below complete_below, each accepted on its backward
  // error (the plain relative residual is roundoff-dominated for slender
  // members, where cond(K) reaches 1e13).
  void finish() {
    const std::size_t J =
        sp.all_compressive
            ? computed.size()
            : static_cast<std::size_t>(std::lower_bound(computed.begin(),
                                                        computed.end(),
                                                        sp.complete_below) -
                                       computed.begin());
    if (sp.all_compressive)
      sp.complete_below = std::numeric_limits<double>::infinity();
    const double knorm = max_abs_column_sum(*K), bnorm = max_abs_column_sum(*B);
    const double sign = buckling ? 1.0 : -1.0; // K + lambda K_G | K - lambda M
    sp.values.assign(computed.begin(),
                     computed.begin() + static_cast<std::ptrdiff_t>(J));
    sp.vectors.resize(vecs.rows(), static_cast<Eigen::Index>(J));
    Eigen::VectorXd Kphi, Bphi;
    for (std::size_t j = 0; j < J; ++j) {
      const Eigen::Index col = static_cast<Eigen::Index>(j);
      Eigen::VectorXd phi = vecs.col(col);
      const double lam = computed[j];
      Kphi = *K * phi;
      Bphi = *B * phi;
      const double norm2 = buckling ? phi.dot(Kphi) : phi.dot(Bphi);
      if (!(norm2 > 0))
        fail("eigen_solve_failed", buckling
                                       ? "buckling mode has non-positive energy"
                                       : "mode has non-positive modal mass");
      const double inv = 1.0 / std::sqrt(norm2);
      phi *= inv;
      Kphi *= inv;
      Bphi *= inv;
      const double eta = (Kphi + sign * lam * Bphi).norm() /
                         ((knorm + lam * bnorm) * phi.norm());
      sp.max_backward_error = std::max(sp.max_backward_error, eta);
      if (!(eta <= kMaxBackwardError))
        fail("eigen_solve_failed", "eigenpair backward error " + fmt(eta) +
                                       " exceeds " + fmt(kMaxBackwardError));
      sp.vectors.col(col) = phi;
    }
  }
};

// K phi = lambda M phi: every eigenvalue below the aggregate window end.
Spectrum modal_spectrum(const SpMat &K, const SpMat &M, InertiaCounter &counter,
                        const std::atomic<bool> *cancel) {
  const int n = static_cast<int>(K.rows());
  const int cap = std::min(kMaxModes, n - 1);
  if (cap < 1)
    fail("invalid_bc", "modal problem has fewer than two free DOFs");
  Ladder L;
  L.K = &K;
  L.B = &M;
  L.counter = &counter;
  L.limit = cap;
  L.nev = std::min(kInitialModes, cap);
  L.solve(cancel);
  for (int pass = 0;; ++pass) {
    if (pass > 4 * kMaxModes)
      fail("eigen_incomplete", "modal spectrum did not stabilize");
    if (L.verify(kWindowEnd * soft_min(L.computed).value, cancel))
      break;
  }
  L.finish();
  return L.sp;
}

// Per load case K_G phi = mu K phi; every case's compressive load factors
// below one common window end kWindowEnd * a(all cases). Ladders arrive with
// K, B = K_G and counter set. Cases without compression contribute nothing.
std::vector<Spectrum> buckling_spectra(std::vector<Ladder> &ladders,
                                       const std::atomic<bool> *cancel) {
  for (Ladder &L : ladders) {
    L.buckling = true;
    const int n = static_cast<int>(L.K->rows());
    const int cap = std::min(kMaxModes, n - 1);
    if (cap < 1)
      fail("invalid_bc", "buckling problem has fewer than two free DOFs");
    const double scale = coupling_scale(*L.B, *L.K);
    if (!std::isfinite(scale))
      fail("eigen_solve_failed", "geometric stiffness is not finite");
    if (!(scale > 0))
      continue; // exactly unstressed: no buckling mode at all
    L.tol = kCompressiveTolerance * scale;
    check_cancel(cancel);
    // Total compressive modes first (as fea_buckling does): a tension-only
    // state is refused by inertia without asking a Krylov solver to resolve
    // the large zero cluster of K_G.
    L.sp.compressive_total = L.counter->count_below(*L.B, *L.K, -L.tol);
    ++L.sp.counts;
    if (L.sp.compressive_total == 0)
      continue;
    L.limit = std::min(cap, L.sp.compressive_total);
    L.nev = std::min(kInitialModes, L.limit);
    L.solve(cancel);
  }
  int max_passes = 1;
  for (const Ladder &L : ladders)
    max_passes += L.limit;
  for (int pass = 0;; ++pass) {
    if (pass > max_passes)
      fail("eigen_incomplete", "buckling spectrum did not stabilize");
    std::vector<double> all;
    for (const Ladder &L : ladders)
      all.insert(all.end(), L.computed.begin(), L.computed.end());
    if (all.empty())
      fail("no_compressive_buckling",
           "no load case produces a compressive buckling mode (tension-only "
           "or unstressed prestress); the buckling factor is undefined, not "
           "infinite");
    const double end = kWindowEnd * soft_min(all).value;
    // Modes with mu in (-tol, 0) are not resolved; the window must stay
    // clear of them or a mode could enter it unseen.
    for (const Ladder &L : ladders)
      if (L.tol > 0 && end * L.tol >= 1.0)
        fail("no_compressive_buckling",
             "load-factor window end " + fmt(end) +
                 " reaches the prestress roundoff floor " + fmt(1.0 / L.tol) +
                 "; compression is numerically indistinguishable from none");
    bool complete = true;
    for (Ladder &L : ladders)
      if (L.sp.compressive_total > 0 && !L.verify(end, cancel))
        complete = false;
    if (complete)
      break;
  }
  std::vector<Spectrum> out;
  out.reserve(ladders.size());
  for (Ladder &L : ladders) {
    if (L.sp.compressive_total > 0)
      L.finish();
    out.push_back(std::move(L.sp));
  }
  return out;
}

nlohmann::json clusters_json(const std::vector<double> &v) {
  nlohmann::json out = nlohmann::json::array();
  std::size_t i = 0;
  while (i < v.size()) {
    std::size_t j = i + 1;
    while (j < v.size() && v[j] - v[j - 1] <= kClusterGap * v[j - 1])
      ++j;
    if (j - i > 1)
      out.push_back({{"first_mode", i + 1},
                     {"multiplicity", j - i},
                     {"relative_spread", (v[j - 1] - v[i]) / v[i]}});
    i = j;
  }
  return out;
}

// `count` modes are inertia-complete below the count point; `active` of them
// carry nonzero weight.
std::string soft_min_label(const char *what, std::size_t count,
                           std::size_t active) {
  std::ostringstream s;
  s << "windowed soft-min a: sum_j chi(lambda_j/a) (lambda_j/a)^-"
    << kSpectralSoftMinPower << " = 1, chi = 1 below " << kWindowFull
    << " a with a C2 quintic decay to 0 at " << kWindowEnd << " a; " << active
    << " weighted of " << count << ' ' << what
    << " (every mode below the inertia-verified count point); "
       "lambda_1 N^(-1/P) <= a <= lambda_1; a C2 symmetric function of the "
       "spectrum, continuous and differentiable through repeated, crossing "
       "and window-entering modes and independent of the eigen request";
  return s.str();
}

nlohmann::json finite_or_null(double v) {
  return std::isfinite(v) ? nlohmann::json(v) : nlohmann::json(nullptr);
}

// ── Modal
// ─────────────────────────────────────────────────────────────────────────
class ModalPhysics final : public Physics {
public:
  explicit ModalPhysics(std::shared_ptr<const Problem> problem)
      : problem_(std::move(problem)) {
    if (problem_->settings.objective != ObjectiveKind::Frequency)
      fail("physics_mismatch", "modal topology objective must be frequency");
    if (!problem_->material.density_kg_m3)
      fail("material_missing", "modal topology needs material.density_g_cm3");
    if (!problem_->structural)
      fail("section_missing", "modal topology needs structural fixtures");
    const std::uint64_t n3 = 3ull * problem_->mesh->node_count();
    const std::uint64_t dense =
        n3 * sizeof(double) * (2ull * (3 * kMaxModes + 1) + 2ull * kMaxModes);
    admit_elastic_memory(*problem_, 2, dense, "topology modal evaluator");
    model_ = std::make_shared<ElasticModel>(problem_);
    system_ = std::make_unique<ElasticSystem>(
        model_, problem_->structural->shared_fixed);
    M_ = system_->pattern().pattern();
    counter_.reset(M_);
  }

  PhysicsKind kind() const override { return PhysicsKind::Modal; }

  std::string formulation() const override {
    const Interpolation &ip = problem_->interpolation;
    std::ostringstream s;
    s << "tet4 linear elastodynamics, consistent mass (Du-Olhoff mass "
         "interpolation), "
      << (ip.kind == Interpolation::Kind::Simp ? "SIMP p=" : "RAMP q=")
      << ip.exponent
      << " stiffness; objective sqrt(a)/(2 pi), a the windowed soft-min "
         "(P="
      << kSpectralSoftMinPower
      << ") of the eigenvalues lambda_j over every mode below 2a; "
         "mass-normalized modes, analytic sensitivity sum_j w_j "
         "phi_j^T(K' - lambda_j M')phi_j, w_j = d a / d lambda_j";
    return s.str();
  }

  Evaluation evaluate(const Eigen::VectorXd &rho, bool derivatives,
                      const std::atomic<bool> *cancel) override {
    check_density(*problem_, rho);
    check_cancel(cancel);
    const ElasticModel &model = *model_;
    const double density = *problem_->material.density_kg_m3;
    system_->assemble(rho, cancel);
    assemble_mass(*system_, rho, density, M_);
    check_cancel(cancel);
    const Spectrum sp =
        modal_spectrum(system_->stiffness(), M_, counter_, cancel);
    const SoftMin agg = soft_min(sp.values);
    const int J = static_cast<int>(sp.values.size());
    const int E = model.element_count();
    const double freq = std::sqrt(agg.value) / (2.0 * kPi);

    Evaluation ev;
    ev.derivatives = derivatives;
    ev.objective = freq;
    ev.objective_unit = "Hz";
    std::vector<Eigen::VectorXd> modes(J);
    for (int j = 0; j < J; ++j)
      system_->reduction().expand_vector(sp.vectors.col(j), modes[j]);
    if (derivatives) {
      check_cancel(cancel);
      // d lambda_agg / d rho = sum_j w_j phi_j^T (K' - lambda_j M') phi_j
      Eigen::VectorXd g = Eigen::VectorXd::Zero(E);
      const MassInterpolation mi{model.interpolation().floor};
      const Eigen::MatrixXi &dofs = model.element_dofs();
      for (int j = 0; j < J; ++j) {
        const double w = agg.weights[j];
        if (w == 0.0)
          continue; // beyond the window: exactly zero weight
        model.add_stiffness_contraction(rho, modes[j], modes[j], w, g);
        for (int e = 0; e < E; ++e) {
          Eigen::Vector3d sum = Eigen::Vector3d::Zero();
          double sq = 0;
          for (int a = 0; a < 4; ++a) {
            const Eigen::Vector3d pa(modes[j][dofs(3 * a, e)],
                                     modes[j][dofs(3 * a + 1, e)],
                                     modes[j][dofs(3 * a + 2, e)]);
            sum += pa;
            sq += pa.squaredNorm();
          }
          const double me0 = density * model.mesh().volume_m3[e] / 20.0 *
                             (sq + sum.squaredNorm());
          g[e] -= w * sp.values[j] * mi.derivative(rho[e]) * me0;
        }
      }
      ev.objective_gradient = g / (4.0 * kPi * std::sqrt(agg.value));
    }
    if (problem_->limits.frequency_hz) {
      Constraint k;
      k.name = "min_frequency";
      k.unit = "Hz";
      k.response = freq;
      k.limit = *problem_->limits.frequency_hz;
      k.value = 1.0 - k.response / k.limit;
      k.aggregation = soft_min_label("lowest modes", J, agg.active);
      if (derivatives)
        k.gradient = -ev.objective_gradient / k.limit;
      ev.constraints.push_back(std::move(k));
    }
    nlohmann::json freqs = nlohmann::json::array();
    std::vector<double> hz(J);
    for (int j = 0; j < J; ++j) {
      hz[j] = std::sqrt(sp.values[j]) / (2.0 * kPi);
      freqs.push_back(hz[j]);
      ev.fields.push_back({"mode_shape_" + std::to_string(j + 1), "kg^-1/2",
                           FieldLocation::Node, 3, modes[j]});
    }
    nlohmann::json weights = nlohmann::json::array();
    for (double w : agg.weights)
      weights.push_back(w);
    ev.evidence = {
        {"formulation", formulation()},
        {"frequencies_hz", freqs},
        {"aggregate_frequency_hz", freq},
        {"aggregate_eigenvalue_rad2_s2", agg.value},
        {"aggregation", soft_min_label("lowest modes", J, agg.active)},
        {"aggregate_weights", weights},
        {"clusters", clusters_json(sp.values)},
        {"mode_normalization", "phi^T M phi = 1 (kg^-1/2)"},
        {"mass", "consistent tet4, Du-Olhoff interpolation with floor"},
        {"modes_requested", sp.requested},
        {"eigensolves", sp.solves},
        {"inertia_counts", sp.counts},
        {"complete_below_rad2_s2", sp.complete_below},
        {"window_rad2_s2", {kWindowFull * agg.value, kWindowEnd * agg.value}},
        {"smoothness",
         "C2 in the eigenvalues; nonsmooth only where the material "
         "interpolation is (Du-Olhoff mass is C1 at rho = 0.1)"},
        {"max_eigen_backward_error", sp.max_backward_error},
        {"free_dofs", system_->reduction().free_size()}};
    return ev;
  }

private:
  std::shared_ptr<const Problem> problem_;
  std::shared_ptr<const ElasticModel> model_;
  std::unique_ptr<ElasticSystem> system_;
  Eigen::SparseMatrix<double> M_;
  InertiaCounter counter_;
};

// ── Buckling
// ──────────────────────────────────────────────────────────────────────
class BucklingPhysics final : public Physics {
public:
  explicit BucklingPhysics(std::shared_ptr<const Problem> problem)
      : problem_(std::move(problem)) {
    if (problem_->settings.objective != ObjectiveKind::BucklingFactor)
      fail("physics_mismatch",
           "buckling topology objective must be buckling_factor");
    if (!problem_->structural || problem_->structural->cases.empty())
      fail("section_missing", "buckling topology needs a reference load case");
    for (const StructuralCase &c : problem_->structural->cases)
      if (c.weight != 1.0)
        fail("invalid_bc",
             "load case " + c.name + " weight " + fmt(c.weight) +
                 " does not apply to buckling: the factor is a soft minimum "
                 "over every case's reference load");
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
    // Lanczos basis max(3*24+1, 60) columns twice (ladder + reorthogonal
    // copy), every case's computed and normalized modes, case vectors.
    const std::uint64_t dense =
        n3 * sizeof(double) *
        (2ull * std::max(3 * kMaxModes + 1, 60) + 2ull * kMaxModes * cs.size() +
         6ull * cs.size());
    // Per support set: static factor, eigen-ladder K factor, inertia LDLT.
    admit_elastic_memory(*problem_, 3 * std::max(1, sets), dense,
                         "topology buckling evaluator");
    model_ = std::make_shared<ElasticModel>(problem_);
    cases_ = std::make_unique<ElasticCases>(model_);
    // One K_G per case: the common aggregate window may revisit any case.
    kg_.resize(cases_->case_count());
    for (int c = 0; c < cases_->case_count(); ++c)
      kg_[c] = cases_->system(cases_->case_system(c)).pattern().pattern();
    counters_.reserve(cases_->system_count());
    for (int s = 0; s < cases_->system_count(); ++s) {
      counters_.push_back(std::make_unique<InertiaCounter>());
      counters_.back()->reset(cases_->system(s).pattern().pattern());
    }
  }

  PhysicsKind kind() const override { return PhysicsKind::Buckling; }

  std::string formulation() const override {
    const Interpolation &ip = problem_->interpolation;
    std::ostringstream s;
    s << "tet4 linear buckling (K + lambda K_G(u)) phi = 0 with static "
         "prestress u per load case, K_G from s(rho) C B u, "
      << (ip.kind == Interpolation::Kind::Simp ? "SIMP p=" : "RAMP q=")
      << ip.exponent
      << "; objective a, the windowed soft-min (P=" << kSpectralSoftMinPower
      << ") of every case's positive load factors below 2a; analytic "
         "sensitivity sum_j w_j lambda_j phi_j^T(K' + lambda_j "
         "dK_G/drho)phi_j, "
         "w_j = d a / d lambda_j, with the prestress dependence of K_G by one "
         "adjoint solve per case";
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
    cases_->solve(rho, cases_->reference_forces(), cancel);

    std::vector<Ladder> ladders(C);
    for (int c = 0; c < C; ++c) {
      check_cancel(cancel);
      const int s = cases_->case_system(c);
      const ElasticSystem &sys = cases_->system(s);
      assemble_geometric_stiffness(sys, rho, cases_->state(c).displacement,
                                   kg_[c]);
      ladders[c].K = &sys.stiffness();
      ladders[c].B = &kg_[c];
      ladders[c].counter = counters_[s].get();
    }
    const std::vector<Spectrum> spectra = buckling_spectra(ladders, cancel);
    std::vector<double> all;
    for (const Spectrum &sp : spectra)
      all.insert(all.end(), sp.values.begin(), sp.values.end());
    const SoftMin agg = soft_min(all);

    Evaluation ev;
    ev.derivatives = derivatives;
    ev.objective = agg.value;
    ev.objective_unit = "1";

    std::vector<std::vector<Eigen::VectorXd>> modes(C);
    for (int c = 0; c < C; ++c) {
      const DofReduction &red =
          cases_->system(cases_->case_system(c)).reduction();
      modes[c].resize(spectra[c].values.size());
      for (std::size_t j = 0; j < modes[c].size(); ++j)
        red.expand_vector(spectra[c].vectors.col(j), modes[c][j]);
    }

    if (derivatives) {
      check_cancel(cancel);
      Eigen::VectorXd g = Eigen::VectorXd::Zero(E);
      const Interpolation &ip = model.interpolation();
      const Eigen::Matrix<double, 6, 6> &Cm = model.elasticity();
      const Eigen::MatrixXi &dofs = model.element_dofs();
      std::vector<int> rhs_case;
      for (int c = 0; c < C; ++c)
        if (!spectra[c].values.empty())
          rhs_case.push_back(c);
      adjoint_rhs_.setZero(model.dof_count(),
                           static_cast<Eigen::Index>(rhs_case.size()));
      std::size_t offset = 0;
      for (int c = 0; c < C; ++c) {
        if (spectra[c].values.empty())
          continue;
        const int col = static_cast<int>(
            std::find(rhs_case.begin(), rhs_case.end(), c) - rhs_case.begin());
        const Eigen::VectorXd &u = cases_->state(c).displacement;
        for (std::size_t j = 0; j < modes[c].size(); ++j) {
          const double lam = spectra[c].values[j];
          const double w = agg.weights[offset + j];
          if (w == 0.0)
            continue; // beyond the window: exactly zero weight
          // Explicit stiffness part: w lambda phi^T K' phi.
          model.add_stiffness_contraction(rho, modes[c][j], modes[c][j],
                                          w * lam, g);
          // Geometric part: w lambda^2 d(phi^T K_G phi)/d rho, explicit in
          // the element stress scale plus the adjoint load for u.
          const double wl2 = w * lam * lam;
          Vec12 pe, ue;
          for (int e = 0; e < E; ++e) {
            model.gather(modes[c][j], e, pe);
            model.gather(u, e, ue);
            const Eigen::Matrix<double, 3, 4> G =
                model.mesh().element_gradients(e);
            const Eigen::Map<const Eigen::Matrix<double, 3, 4>> Phi(pe.data());
            const Eigen::Matrix3d H = G * Phi.transpose();
            const Eigen::Matrix3d A = H * H.transpose();
            Vec6 a;
            a << A(0, 0), A(1, 1), A(2, 2), 2 * A(0, 1), 2 * A(1, 2),
                2 * A(0, 2);
            const Eigen::Matrix<double, 6, 12> B = model.strain_matrix(e);
            const double V = model.mesh().volume_m3[e];
            const Vec6 sigma0 = Cm * (B * ue);
            g[e] += wl2 * V * ip.scale_derivative(rho[e]) * sigma0.dot(a);
            const Vec12 gu =
                (wl2 * V * ip.scale(rho[e])) * (B.transpose() * (Cm * a));
            for (int i = 0; i < 12; ++i)
              adjoint_rhs_(dofs(i, e), col) += gu[i];
          }
        }
        offset += modes[c].size();
      }
      check_cancel(cancel);
      cases_->solve_adjoint(adjoint_rhs_, rhs_case, adjoint_);
      for (std::size_t r = 0; r < rhs_case.size(); ++r)
        model.add_stiffness_contraction(rho, adjoint_.col(r),
                                        cases_->state(rhs_case[r]).displacement,
                                        -1.0, g);
      ev.objective_gradient = std::move(g);
    }
    if (problem_->limits.buckling_factor) {
      Constraint k;
      k.name = "min_buckling_factor";
      k.unit = "1";
      k.response = agg.value;
      k.limit = *problem_->limits.buckling_factor;
      k.value = 1.0 - k.response / k.limit;
      k.aggregation = soft_min_label("load factors", all.size(), agg.active);
      if (derivatives)
        k.gradient = -ev.objective_gradient / k.limit;
      ev.constraints.push_back(std::move(k));
    }

    nlohmann::json cases_ev = nlohmann::json::array();
    for (int c = 0; c < C; ++c) {
      const std::string suffix = C > 1 ? "@" + sb.cases[c].name : "";
      ev.fields.push_back({"displacement" + suffix, "m", FieldLocation::Node, 3,
                           cases_->state(c).displacement});
      nlohmann::json factors = nlohmann::json::array();
      for (std::size_t j = 0; j < modes[c].size(); ++j) {
        factors.push_back(spectra[c].values[j]);
        const double scale = modes[c][j].cwiseAbs().maxCoeff();
        ev.fields.push_back(
            {"buckling_mode_" + std::to_string(j + 1) + suffix, "-",
             FieldLocation::Node, 3,
             scale > 0 ? Eigen::VectorXd(modes[c][j] / scale) : modes[c][j]});
      }
      cases_ev.push_back(
          {{"name", sb.cases[c].name},
           {"reference_resultant_n",
            {sb.cases[c].resultant_n[0], sb.cases[c].resultant_n[1],
             sb.cases[c].resultant_n[2]}},
           {"load_factors", factors},
           {"compressive_modes", spectra[c].compressive_total},
           {"no_compressive_mode", spectra[c].compressive_total == 0},
           {"all_compressive_modes_computed", spectra[c].all_compressive},
           {"complete_below_load_factor",
            finite_or_null(spectra[c].complete_below)},
           {"clusters", clusters_json(spectra[c].values)},
           {"modes_requested", spectra[c].requested},
           {"eigensolves", spectra[c].solves},
           {"inertia_counts", spectra[c].counts},
           {"max_eigen_backward_error", spectra[c].max_backward_error}});
    }
    nlohmann::json weights = nlohmann::json::array();
    for (double w : agg.weights)
      weights.push_back(w);
    ev.evidence = {
        {"formulation", formulation()},
        {"aggregate_load_factor", agg.value},
        {"minimum_load_factor", *std::min_element(all.begin(), all.end())},
        {"aggregation", soft_min_label("load factors", all.size(), agg.active)},
        {"aggregate_weights", weights},
        {"window_load_factor",
         {kWindowFull * agg.value, kWindowEnd * agg.value}},
        {"smoothness",
         "C2 in the load factors wherever K, K_G(u) and the "
         "prestress are smooth; refused (no_compressive_buckling) "
         "where the window would reach the unresolved band "
         "|mu| < 1e-9 of the Jacobi-scaled prestress coupling"},
        {"load_factor_meaning", "multiplier of each case's reference load"},
        {"mode_normalization", "phi^T K phi = 1; fields max |.|=1"},
        {"cases", cases_ev},
        {"elastic", cases_->evidence()}};
    return ev;
  }

private:
  std::shared_ptr<const Problem> problem_;
  std::shared_ptr<const ElasticModel> model_;
  std::unique_ptr<ElasticCases> cases_;
  std::vector<Eigen::SparseMatrix<double>> kg_;
  std::vector<std::unique_ptr<InertiaCounter>> counters_;
  Eigen::MatrixXd adjoint_rhs_, adjoint_;
};

} // namespace

std::unique_ptr<Physics>
make_spectral_physics(std::shared_ptr<const Problem> problem) {
  if (!problem)
    fail("invalid_input", "make_spectral_physics needs a prepared problem");
  switch (problem->settings.physics) {
  case PhysicsKind::Modal:
    return std::make_unique<ModalPhysics>(std::move(problem));
  case PhysicsKind::Buckling:
    return std::make_unique<BucklingPhysics>(std::move(problem));
  default:
    fail("physics_mismatch",
         std::string("make_spectral_physics does not apply to physics ") +
             to_string(problem->settings.physics));
  }
}

} // namespace topology
} // namespace sim
} // namespace chudware
