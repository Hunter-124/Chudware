// Topology optimization manufacturing measures. See topopt_manufacturing.h.
#include "sim/topopt_manufacturing.h"

#include "util/resource_governor.h"

#include <algorithm>
#include <cmath>
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

void admit_or_refuse(std::uint64_t bytes, const char *what) {
  std::string why;
  if (!chudware::governor::admit(bytes, what, why))
    fail("memory_budget_exceeded", why);
}

// Uniform bucket grid over element centroids (built once per model).
class CentroidGrid {
public:
  CentroidGrid(const Eigen::Matrix3Xd &points, double cell) {
    const int n = static_cast<int>(points.cols());
    lo_ = points.rowwise().minCoeff();
    const Eigen::Vector3d hi = points.rowwise().maxCoeff();
    cell_ = cell;
    // Keep the grid within a few cells per point so memory stays O(n).
    const double limit = 4.0 * n + 64.0;
    for (;;) {
      double total = 1.0;
      for (int d = 0; d < 3; ++d) {
        dims_[d] =
            static_cast<long long>(std::floor((hi[d] - lo_[d]) / cell_)) + 1;
        total *= static_cast<double>(dims_[d]);
      }
      if (total <= limit)
        break;
      cell_ *= 1.5;
    }
    const long long cells = dims_[0] * dims_[1] * dims_[2];
    offsets_.assign(static_cast<std::size_t>(cells) + 1, 0);
    std::vector<long long> key(n);
    for (int i = 0; i < n; ++i) {
      key[i] = index(points.col(i));
      ++offsets_[static_cast<std::size_t>(key[i]) + 1];
    }
    for (std::size_t c = 1; c < offsets_.size(); ++c)
      offsets_[c] += offsets_[c - 1];
    items_.resize(n);
    std::vector<int> cursor(offsets_.begin(), offsets_.end() - 1);
    for (int i = 0; i < n; ++i)
      items_[cursor[static_cast<std::size_t>(key[i])]++] = i;
  }

  template <class F>
  void for_each_within(const Eigen::Ref<const Eigen::Vector3d> &c, double r,
                       const Eigen::Matrix3Xd &points, F &&f) const {
    long long lo[3], hi[3];
    for (int d = 0; d < 3; ++d) {
      lo[d] = std::max<long long>(
          0, static_cast<long long>(std::floor((c[d] - r - lo_[d]) / cell_)));
      hi[d] = std::min<long long>(
          dims_[d] - 1,
          static_cast<long long>(std::floor((c[d] + r - lo_[d]) / cell_)));
    }
    for (long long i = lo[0]; i <= hi[0]; ++i)
      for (long long j = lo[1]; j <= hi[1]; ++j)
        for (long long k = lo[2]; k <= hi[2]; ++k) {
          const long long cidx = (i * dims_[1] + j) * dims_[2] + k;
          for (int s = offsets_[static_cast<std::size_t>(cidx)];
               s < offsets_[static_cast<std::size_t>(cidx) + 1]; ++s) {
            const int q = items_[s];
            const double dist = (points.col(q) - c).norm();
            if (dist <= r)
              f(q, dist);
          }
        }
  }

private:
  long long index(const Eigen::Ref<const Eigen::Vector3d> &p) const {
    long long ijk[3];
    for (int d = 0; d < 3; ++d)
      ijk[d] = std::clamp<long long>(
          static_cast<long long>(std::floor((p[d] - lo_[d]) / cell_)), 0,
          dims_[d] - 1);
    return (ijk[0] * dims_[1] + ijk[1]) * dims_[2] + ijk[2];
  }

  Eigen::Vector3d lo_;
  double cell_ = 1;
  long long dims_[3] = {1, 1, 1};
  std::vector<int> offsets_;
  std::vector<int> items_;
};

constexpr double kOverhangEps = 1e-6;

} // namespace

// ── OverhangModel
// ───────────────────────────────────────────────────────────
OverhangModel::OverhangModel(const TetMesh &mesh,
                             const OverhangSettings &settings)
    : settings_(settings) {
  const Eigen::Vector3d raw = settings.build_direction;
  if (!(raw.allFinite() && raw.norm() > 0))
    fail("invalid_overhang", "build direction must be finite and non-zero");
  settings_.build_direction = raw.normalized();
  if (!(settings.overhang_angle_deg > 0 && settings.overhang_angle_deg < 90))
    fail("invalid_overhang", "overhang angle " +
                                 fmt(settings.overhang_angle_deg) +
                                 " deg outside (0,90)");
  if (!(std::isfinite(settings.support_radius_factor) &&
        settings.support_radius_factor > 0))
    fail("invalid_overhang", "support radius factor must be > 0");
  if (!(std::isfinite(settings.lateral_allowance) &&
        settings.lateral_allowance >= 0))
    fail("invalid_overhang", "lateral allowance must be >= 0");
  if (!(std::isfinite(settings.power) && settings.power >= 1))
    fail("invalid_overhang", "power-mean exponent must be >= 1");

  const int E = mesh.element_count();
  if (E == 0)
    fail("invalid_overhang", "mesh has no elements");
  element_count_ = E;
  volume_m3_ = mesh.volume_m3;
  total_volume_m3_ = mesh.total_volume_m3;
  const Eigen::Vector3d d = settings_.build_direction;

  double plate = std::numeric_limits<double>::infinity();
  for (int n = 0; n < mesh.node_count(); ++n)
    if (mesh.node_referenced[n])
      plate = std::min(plate, mesh.nodes_m.col(n).dot(d));
  const double diag = (mesh.bbox_max_m - mesh.bbox_min_m).norm();
  const double plate_tol = 1e-6 * std::max(diag, 1e-300);
  plate_.assign(E, 0);
  for (int e = 0; e < E; ++e) {
    double low = std::numeric_limits<double>::infinity();
    for (int a = 0; a < 4; ++a)
      low = std::min(low, mesh.nodes_m.col(mesh.tets(a, e)).dot(d));
    if (low <= plate + plate_tol) {
      plate_[e] = 1;
      ++plate_count_;
    }
  }

  const double tan_a =
      std::tan(settings_.overhang_angle_deg * 3.14159265358979323846 / 180.0);
  const double factor = settings_.support_radius_factor;
  CentroidGrid grid(mesh.centroid_m,
                    factor *
                        std::max(mesh.quality.median_element_size_m, 1e-300));
  auto in_cone = [&](int e, int f, double dist) {
    if (f == e)
      return false;
    const Eigen::Vector3d delta =
        mesh.centroid_m.col(e) - mesh.centroid_m.col(f);
    const double size = mesh.element_size_m[e];
    const double t = delta.dot(d);
    if (!(t > 0.05 * size))
      return false;
    const double lateral = std::sqrt(std::max(0.0, dist * dist - t * t));
    return lateral <= t * tan_a + settings_.lateral_allowance * size;
  };

  // Pass 1: count, then admit the storage before allocating it.
  offsets_.assign(static_cast<std::size_t>(E) + 1, 0);
  for (int e = 0; e < E; ++e) {
    if (plate_[e])
      continue;
    int count = 0;
    grid.for_each_within(mesh.centroid_m.col(e),
                         factor * mesh.element_size_m[e], mesh.centroid_m,
                         [&](int f, double dist) {
                           if (in_cone(e, f, dist))
                             ++count;
                         });
    offsets_[static_cast<std::size_t>(e) + 1] = count;
  }
  std::uint64_t entries = 0;
  for (int e = 0; e < E; ++e)
    entries +=
        static_cast<std::uint64_t>(offsets_[static_cast<std::size_t>(e) + 1]);
  if (entries > static_cast<std::uint64_t>(std::numeric_limits<int>::max()))
    fail("memory_budget_exceeded",
         "overhang support sets exceed the index range; lower "
         "support_radius_factor");
  admit_or_refuse(entries * (sizeof(int) + sizeof(double)) +
                      static_cast<std::uint64_t>(E) * 32ull,
                  "topology overhang support sets");
  for (int e = 0; e < E; ++e)
    offsets_[static_cast<std::size_t>(e) + 1] += offsets_[e];
  indices_.resize(static_cast<std::size_t>(entries));
  weights_.resize(static_cast<std::size_t>(entries));
  for (int e = 0; e < E; ++e) {
    if (plate_[e])
      continue;
    int at = offsets_[e];
    const double radius = factor * mesh.element_size_m[e];
    double sum = 0;
    grid.for_each_within(mesh.centroid_m.col(e), radius, mesh.centroid_m,
                         [&](int f, double dist) {
                           if (!in_cone(e, f, dist))
                             return;
                           const double w = std::max(0.0, radius - dist) *
                                                mesh.volume_m3[f] +
                                            1e-300;
                           indices_[at] = f;
                           weights_[at] = w;
                           sum += w;
                           ++at;
                         });
    for (int s = offsets_[e]; s < at; ++s)
      weights_[s] /= sum;
    if (at == offsets_[e])
      ++coneless_count_;
  }
  mean_.setZero(E);
}

double OverhangModel::measure(const Eigen::VectorXd &rho,
                              Eigen::VectorXd *gradient) {
  if (rho.size() != element_count_)
    fail("invalid_density", "overhang measure needs one density per element");
  const double P = settings_.power;
  const double inv_v = 1.0 / total_volume_m3_;
  if (gradient)
    gradient->setZero(element_count_);
  double total = 0;
  for (int e = 0; e < element_count_; ++e) {
    double s = 1.0;
    if (!plate_[e]) {
      const int b = offsets_[e], t = offsets_[static_cast<std::size_t>(e) + 1];
      if (b == t) {
        s = 0.0;
      } else {
        double m = 0;
        for (int k = b; k < t; ++k)
          m += weights_[k] * std::pow(rho[indices_[k]] + kOverhangEps, P);
        mean_[e] = m;
        s = std::pow(m, 1.0 / P) - kOverhangEps;
      }
    }
    total += volume_m3_[e] * rho[e] * (1.0 - s);
    if (gradient) {
      (*gradient)[e] += volume_m3_[e] * (1.0 - s) * inv_v;
      const int b = offsets_[e], t = offsets_[static_cast<std::size_t>(e) + 1];
      if (!plate_[e] && b != t && rho[e] != 0.0) {
        // ds_e/drho_f = m^(1/P - 1) w_ef (rho_f + eps)^(P - 1)
        const double coef =
            -volume_m3_[e] * rho[e] * inv_v * std::pow(mean_[e], 1.0 / P - 1.0);
        for (int k = b; k < t; ++k)
          (*gradient)[indices_[k]] +=
              coef * weights_[k] *
              std::pow(rho[indices_[k]] + kOverhangEps, P - 1.0);
      }
    }
  }
  return total * inv_v;
}

double OverhangModel::unsupported_fraction(const Eigen::VectorXd &rho,
                                           double threshold) const {
  if (rho.size() != element_count_)
    fail("invalid_density", "overhang check needs one density per element");
  double unsupported = 0;
  for (int e = 0; e < element_count_; ++e) {
    if (rho[e] < threshold || plate_[e])
      continue;
    bool supported = false;
    for (int k = offsets_[e];
         k < offsets_[static_cast<std::size_t>(e) + 1] && !supported; ++k)
      supported = rho[indices_[k]] >= threshold;
    if (!supported)
      unsupported += volume_m3_[e];
  }
  return unsupported / total_volume_m3_;
}

std::string OverhangModel::formulation() const {
  return "unstructured-tet support-cone heuristic: unsupported solid fraction "
         "sum V_e rho_e (1 - s_e) / V with s_e the power mean (P=" +
         fmt(settings_.power) + ") of densities inside the " +
         fmt(settings_.overhang_angle_deg) +
         " deg cone below each element (radius " +
         fmt(settings_.support_radius_factor) +
         " element sizes, lateral "
         "allowance " +
         fmt(settings_.lateral_allowance) +
         " element size); exact gradient; not a support-free guarantee";
}

// ── EnclosedVoidFinder
// ──────────────────────────────────────────────────────
EnclosedVoidFinder::EnclosedVoidFinder(std::shared_ptr<const TetMesh> mesh)
    : mesh_(std::move(mesh)) {
  if (!mesh_)
    fail("invalid_mesh", "enclosed-void finder needs a mesh");
  const int E = mesh_->element_count();
  state_.assign(E, 0);
  void_scratch_.assign(E, 0);
  queue_.reserve(E);
}

const EnclosedVoids &
EnclosedVoidFinder::find(const std::vector<std::uint8_t> &is_void) {
  if (!mesh_)
    fail("invalid_mesh", "enclosed-void finder has no mesh");
  const TetMesh &m = *mesh_;
  const int E = m.element_count();
  if (static_cast<int>(is_void.size()) != E)
    fail("invalid_density", "void mask needs one entry per element");
  result_.void_elements = result_.open_void_elements = 0;
  result_.enclosed_elements = result_.regions = 0;
  result_.enclosed_volume_m3 = 0;
  result_.enclosed.clear();
  queue_.clear();
  for (int e = 0; e < E; ++e) {
    state_[e] = is_void[e] ? 1 : 0;
    if (!is_void[e])
      continue;
    ++result_.void_elements;
    for (int a = 0; a < 4; ++a)
      if (m.face_neighbor(a, e) < 0) {
        state_[e] = 2;
        queue_.push_back(e);
        break;
      }
  }
  for (std::size_t h = 0; h < queue_.size(); ++h) {
    const int e = queue_[h];
    for (int a = 0; a < 4; ++a) {
      const int f = m.face_neighbor(a, e);
      if (f >= 0 && state_[f] == 1) {
        state_[f] = 2;
        queue_.push_back(f);
      }
    }
  }
  result_.open_void_elements = static_cast<int>(queue_.size());
  for (int seed = 0; seed < E; ++seed) {
    if (state_[seed] != 1)
      continue;
    ++result_.regions;
    queue_.clear();
    queue_.push_back(seed);
    state_[seed] = 3;
    for (std::size_t h = 0; h < queue_.size(); ++h) {
      const int e = queue_[h];
      result_.enclosed.push_back(e);
      result_.enclosed_volume_m3 += m.volume_m3[e];
      for (int a = 0; a < 4; ++a) {
        const int f = m.face_neighbor(a, e);
        if (f >= 0 && state_[f] == 1) {
          state_[f] = 3;
          queue_.push_back(f);
        }
      }
    }
  }
  std::sort(result_.enclosed.begin(), result_.enclosed.end());
  result_.enclosed_elements = static_cast<int>(result_.enclosed.size());
  return result_;
}

const EnclosedVoids &EnclosedVoidFinder::find(const Eigen::VectorXd &rho,
                                              double threshold) {
  if (!mesh_)
    fail("invalid_mesh", "enclosed-void finder has no mesh");
  if (rho.size() != mesh_->element_count())
    fail("invalid_density", "density needs one entry per element");
  for (Eigen::Index e = 0; e < rho.size(); ++e)
    void_scratch_[e] = rho[e] < threshold ? 1 : 0;
  return find(void_scratch_);
}

// ── Report
// ──────────────────────────────────────────────────────────────────
nlohmann::json to_json(const ManufacturingReport &r) {
  using nlohmann::json;
  json out;
  if (r.overhang_evaluated) {
    out["overhang"] = {
        {"build_direction",
         {r.build_direction[0], r.build_direction[1], r.build_direction[2]}},
        {"build_direction_source", r.build_direction_source},
        {"overhang_angle_deg", r.overhang_angle_deg},
        {"overhang_penalty", r.overhang_penalty},
        {"in_objective", r.overhang_in_objective},
        {"relaxed_unsupported_fraction", r.overhang_measure},
        {"thresholded_unsupported_volume_fraction",
         r.unsupported_volume_fraction},
        {"plate_elements", r.plate_elements},
        {"coneless_elements", r.coneless_elements},
        {"formulation", r.overhang_formulation},
        {"heuristic", true},
        {"guaranteed_support_free", false}};
    if (r.build_plate) {
      const auto &b = *r.build_plate;
      out["overhang"]["build_plate"] = {
          {"triangles", b.triangles},
          {"area_mm2", b.area_m2 * 1e6},
          {"normal", {b.normal[0], b.normal[1], b.normal[2]}}};
    }
  } else {
    out["overhang"] = nullptr;
  }
  out["enclosed_voids"] = {
      {"required_absent", r.no_enclosed_voids},
      {"enforcement", r.enclosed_void_enforcement},
      {"threshold", r.threshold},
      {"void_elements", r.enclosed.void_elements},
      {"open_void_elements", r.enclosed.open_void_elements},
      {"enclosed_elements", r.enclosed.enclosed_elements},
      {"enclosed_regions", r.enclosed.regions},
      {"enclosed_volume_m3", r.enclosed.enclosed_volume_m3},
      {"rejected_candidate_updates", r.rejected_updates},
      {"method", "tet face-adjacency exterior flood fill of the thresholded "
                 "design; geometry is never filled"}};
  out["min_member"] = {
      {"requested_m", r.min_member_m ? json(*r.min_member_m) : json(nullptr)},
      {"filter_radius_m", r.filter_radius_m},
      {"filter_radius_source", r.filter_radius_source},
      {"regularization", r.min_member_regularization},
      {"guaranteed", false},
      {"note", "minimum-member lower-bound intent via regularization; the "
               "automatic radius filters features finer than approximately "
               "four elements; actual member thickness is not measured"}};
  if (r.min_member_filter_radius_mm) {
    out["min_member"]["min_member_filter_radius_mm"] =
        *r.min_member_filter_radius_mm;
    out["min_member"]["mesh_floor_radius_mm"] = *r.mesh_floor_radius_mm;
  }
  out["ties"] = {{"symmetry_axes",
                  {r.symmetry_axes[0], r.symmetry_axes[1], r.symmetry_axes[2]}},
                 {"extrusion_axis", r.extrusion_axis},
                 {"tied_groups", r.tied_groups},
                 {"max_tie_distance_m", r.max_tie_distance_m}};
  return out;
}

} // namespace topology
} // namespace sim
} // namespace chudware
