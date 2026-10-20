// Topology optimization common model: mesh precompute, validation, boundary
// resolution, passive masks, design-variable tying and density filtering. See
// topopt_model.h.
#include "sim/topopt_model.h"

#include <Eigen/Eigenvalues>

#include <algorithm>
#include <cmath>
#include <limits>
#include <map>
#include <numeric>
#include <set>
#include <sstream>
#include <utility>

namespace chudware {
namespace sim {
namespace topology {

using nlohmann::json;

ModelError::ModelError(std::string code, const std::string &message)
    : std::runtime_error(code + ": " + message), code_(std::move(code)) {}

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

// ── Uniform grid over a point cloud (neighbor and nearest queries)
// ─────────────────────
class PointGrid {
public:
  PointGrid(const Eigen::Matrix3Xd &pts, double cell) : pts_(pts) {
    const int n = static_cast<int>(pts.cols());
    if (n == 0)
      return;
    lo_ = pts.rowwise().minCoeff();
    const Eigen::Vector3d ext = pts.rowwise().maxCoeff() - lo_;
    cell_ = std::max(cell, ext.maxCoeff() / 1048575.0);
    if (!(cell_ > 0) || !std::isfinite(cell_))
      cell_ = 1.0;
    for (int d = 0; d < 3; ++d)
      dims_[d] = static_cast<std::int64_t>(std::floor(ext[d] / cell_)) + 1;
    std::vector<std::pair<std::uint64_t, int>> entries(n);
    for (int i = 0; i < n; ++i)
      entries[i] = {key(cell_of(pts.col(i))), i};
    std::sort(entries.begin(), entries.end());
    keys_.resize(n);
    order_.resize(n);
    for (int i = 0; i < n; ++i) {
      keys_[i] = entries[i].first;
      order_[i] = entries[i].second;
    }
  }

  // f(index, distance) for every point with |p - q| <= r.
  template <class F>
  void for_each_within(const Eigen::Vector3d &q, double r, F &&f) const {
    const int n = static_cast<int>(pts_.cols());
    if (n == 0)
      return;
    std::array<std::int64_t, 3> a{}, b{};
    double cells = 1.0;
    for (int d = 0; d < 3; ++d) {
      a[d] = clampi(
          static_cast<std::int64_t>(std::floor((q[d] - r - lo_[d]) / cell_)),
          d);
      b[d] = clampi(
          static_cast<std::int64_t>(std::floor((q[d] + r - lo_[d]) / cell_)),
          d);
      cells *= static_cast<double>(b[d] - a[d] + 1);
    }
    if (cells > static_cast<double>(n)) {
      for (int i = 0; i < n; ++i) {
        const double dist = (pts_.col(i) - q).norm();
        if (dist <= r)
          f(i, dist);
      }
      return;
    }
    for (std::int64_t x = a[0]; x <= b[0]; ++x)
      for (std::int64_t y = a[1]; y <= b[1]; ++y)
        for (std::int64_t z = a[2]; z <= b[2]; ++z) {
          const std::uint64_t k = key({x, y, z});
          auto range = std::equal_range(keys_.begin(), keys_.end(), k);
          for (auto it = range.first; it != range.second; ++it) {
            const int i = order_[static_cast<std::size_t>(it - keys_.begin())];
            const double dist = (pts_.col(i) - q).norm();
            if (dist <= r)
              f(i, dist);
          }
        }
  }

  // Nearest point index (-1 when empty); distance in *dist.
  int nearest(const Eigen::Vector3d &q, double *dist) const {
    const int n = static_cast<int>(pts_.cols());
    int best = -1;
    double bd = std::numeric_limits<double>::infinity();
    if (n == 0)
      return -1;
    const double diag =
        std::sqrt(static_cast<double>(
            dims_[0] * dims_[0] + dims_[1] * dims_[1] + dims_[2] * dims_[2])) *
        cell_;
    const double reach = diag + (q - lo_).norm() + cell_;
    for (double r = cell_;; r *= 2.0) {
      for_each_within(q, r, [&](int i, double d) {
        if (d < bd || (d == bd && i < best)) {
          bd = d;
          best = i;
        }
      });
      if (best >= 0 || r > reach)
        break;
    }
    if (dist)
      *dist = bd;
    return best;
  }

private:
  std::int64_t clampi(std::int64_t v, int d) const {
    return std::min<std::int64_t>(std::max<std::int64_t>(v, 0), dims_[d] - 1);
  }
  std::array<std::int64_t, 3> cell_of(const Eigen::Vector3d &p) const {
    std::array<std::int64_t, 3> c{};
    for (int d = 0; d < 3; ++d)
      c[d] = clampi(
          static_cast<std::int64_t>(std::floor((p[d] - lo_[d]) / cell_)), d);
    return c;
  }
  std::uint64_t key(const std::array<std::int64_t, 3> &c) const {
    return (static_cast<std::uint64_t>(c[0]) *
                static_cast<std::uint64_t>(dims_[1]) +
            static_cast<std::uint64_t>(c[1])) *
               static_cast<std::uint64_t>(dims_[2]) +
           static_cast<std::uint64_t>(c[2]);
  }

  const Eigen::Matrix3Xd &pts_;
  Eigen::Vector3d lo_ = Eigen::Vector3d::Zero();
  double cell_ = 1.0;
  std::array<std::int64_t, 3> dims_{1, 1, 1};
  std::vector<std::uint64_t> keys_;
  std::vector<int> order_;
};

struct UnionFind {
  std::vector<int> parent;
  explicit UnionFind(int n) : parent(n) {
    std::iota(parent.begin(), parent.end(), 0);
  }
  int find(int a) {
    while (parent[a] != a) {
      parent[a] = parent[parent[a]];
      a = parent[a];
    }
    return a;
  }
  void unite(int a, int b) {
    a = find(a);
    b = find(b);
    if (a != b)
      parent[std::max(a, b)] = std::min(a, b);
  }
};

Eigen::Vector3d closest_on_triangle(const Eigen::Vector3d &p,
                                    const Eigen::Vector3d &a,
                                    const Eigen::Vector3d &b,
                                    const Eigen::Vector3d &c) {
  const Eigen::Vector3d ab = b - a, ac = c - a, ap = p - a;
  const double d1 = ab.dot(ap), d2 = ac.dot(ap);
  if (d1 <= 0 && d2 <= 0)
    return a;
  const Eigen::Vector3d bp = p - b;
  const double d3 = ab.dot(bp), d4 = ac.dot(bp);
  if (d3 >= 0 && d4 <= d3)
    return b;
  const double vc = d1 * d4 - d3 * d2;
  if (vc <= 0 && d1 >= 0 && d3 <= 0)
    return a + (d1 / (d1 - d3)) * ab;
  const Eigen::Vector3d cp = p - c;
  const double d5 = ab.dot(cp), d6 = ac.dot(cp);
  if (d6 >= 0 && d5 <= d6)
    return c;
  const double vb = d5 * d2 - d1 * d6;
  if (vb <= 0 && d2 >= 0 && d6 <= 0)
    return a + (d2 / (d2 - d6)) * ac;
  const double va = d3 * d6 - d5 * d4;
  if (va <= 0 && (d4 - d3) >= 0 && (d5 - d6) >= 0)
    return b + ((d4 - d3) / ((d4 - d3) + (d5 - d6))) * (c - b);
  const double denom = 1.0 / (va + vb + vc);
  return a + ab * (vb * denom) + ac * (vc * denom);
}

// ── JSON helpers (preparation only; never used inside iterations)
// ─────────────────────
void check_keys(const json &o, std::initializer_list<const char *> known,
                const std::string &ctx) {
  if (!o.is_object())
    fail("invalid_input", ctx + " must be an object");
  for (auto it = o.begin(); it != o.end(); ++it) {
    bool ok = false;
    for (const char *k : known)
      ok = ok || it.key() == k;
    if (!ok)
      fail("unknown_key", ctx + "." + it.key() + " is not a supported key");
  }
}

double as_number(const json &v, const std::string &ctx) {
  if (!v.is_number())
    fail("invalid_input", ctx + " must be a number");
  const double d = v.get<double>();
  if (!std::isfinite(d))
    fail("invalid_input", ctx + " must be finite");
  return d;
}

std::optional<double> opt_number(const json &o, const char *key,
                                 const std::string &ctx) {
  if (!o.contains(key))
    return std::nullopt;
  return as_number(o[key], ctx + "." + key);
}

double req_number(const json &o, const char *key, const std::string &ctx) {
  if (!o.contains(key))
    fail("missing_key", ctx + "." + key + " is required");
  return as_number(o[key], ctx + "." + key);
}

std::string opt_string(const json &o, const char *key, const std::string &ctx) {
  if (!o.contains(key))
    return {};
  if (!o[key].is_string())
    fail("invalid_input", ctx + "." + key + " must be a string");
  return o[key].get<std::string>();
}

Eigen::Vector3d as_vec3(const json &v, const std::string &ctx) {
  if (!v.is_array() || v.size() != 3)
    fail("invalid_input", ctx + " must be [x,y,z]");
  Eigen::Vector3d r;
  for (int d = 0; d < 3; ++d)
    r[d] = as_number(v[d], ctx);
  return r;
}

std::vector<int> as_nodes(const json &v, const std::string &ctx) {
  if (!v.is_array())
    fail("invalid_input", ctx + " must be an array of node indices");
  std::vector<int> out;
  out.reserve(v.size());
  for (const auto &n : v) {
    if (!n.is_number_integer() || n.get<long long>() < 0 ||
        n.get<long long>() > std::numeric_limits<int>::max())
      fail("invalid_input", ctx + " entries must be non-negative integers");
    out.push_back(static_cast<int>(n.get<long long>()));
  }
  return out;
}

int axis_index(const json &v, const std::string &ctx) {
  if (v.is_string()) {
    const std::string s = v.get<std::string>();
    if (s == "x")
      return 0;
    if (s == "y")
      return 1;
    if (s == "z")
      return 2;
  }
  fail("invalid_input", ctx + " must be \"x\", \"y\" or \"z\"");
}

std::array<bool, 3> parse_dofs(const json &o, const std::string &ctx) {
  std::array<bool, 3> dofs{true, true, true};
  if (!o.contains("dofs"))
    return dofs;
  const json &d = o["dofs"];
  if (!d.is_array() || d.empty())
    fail("invalid_bc", ctx + ".dofs must be a non-empty array of 0,1,2");
  dofs = {false, false, false};
  for (const auto &c : d) {
    if (!c.is_number_integer() || c.get<int>() < 0 || c.get<int>() > 2)
      fail("invalid_bc", ctx + ".dofs entries must be 0, 1 or 2");
    if (dofs[c.get<int>()])
      fail("invalid_bc",
           ctx + ".dofs repeats component " + std::to_string(c.get<int>()));
    dofs[c.get<int>()] = true;
  }
  return dofs;
}

std::vector<FixtureSpec> parse_fixtures(const json &arr,
                                        const std::string &ctx) {
  if (!arr.is_array())
    fail("invalid_input", ctx + " must be an array");
  std::vector<FixtureSpec> out;
  for (std::size_t i = 0; i < arr.size(); ++i) {
    const std::string c = ctx + "[" + std::to_string(i) + "]";
    check_keys(arr[i], {"label", "nodes", "dofs"}, c);
    FixtureSpec f;
    f.label = opt_string(arr[i], "label", c);
    if (!arr[i].contains("nodes"))
      fail("missing_key", c + ".nodes is required");
    f.nodes = as_nodes(arr[i]["nodes"], c + ".nodes");
    f.dofs = parse_dofs(arr[i], c);
    out.push_back(std::move(f));
  }
  return out;
}

std::vector<RegionSpec> parse_regions(const json &arr, const std::string &ctx) {
  if (!arr.is_array())
    fail("invalid_input", ctx + " must be an array");
  std::vector<RegionSpec> out;
  for (std::size_t i = 0; i < arr.size(); ++i) {
    const std::string c = ctx + "[" + std::to_string(i) + "]";
    const json &r = arr[i];
    check_keys(
        r, {"label", "nodes", "thickness_mm", "box_min_mm", "box_max_mm"}, c);
    RegionSpec s;
    s.label = opt_string(r, "label", c);
    const bool has_nodes = r.contains("nodes");
    const bool has_box = r.contains("box_min_mm") || r.contains("box_max_mm");
    if (has_nodes == has_box)
      fail("invalid_region",
           c + " needs exactly one of nodes or box_min_mm/box_max_mm");
    if (has_box) {
      if (!r.contains("box_min_mm") || !r.contains("box_max_mm"))
        fail("invalid_region", c + " box needs both box_min_mm and box_max_mm");
      if (r.contains("thickness_mm"))
        fail("invalid_region",
             c + ".thickness_mm applies only to node-set regions");
      s.has_box = true;
      s.box_min_mm = as_vec3(r["box_min_mm"], c + ".box_min_mm");
      s.box_max_mm = as_vec3(r["box_max_mm"], c + ".box_max_mm");
    } else {
      s.nodes = as_nodes(r["nodes"], c + ".nodes");
      s.thickness_mm = opt_number(r, "thickness_mm", c);
    }
    out.push_back(std::move(s));
  }
  return out;
}

} // namespace

// ── Vocabulary
// ──────────────────────────────────────────────────────────────────────
const char *to_string(PhysicsKind p) {
  switch (p) {
  case PhysicsKind::Structural:
    return "structural";
  case PhysicsKind::Thermal:
    return "thermal";
  case PhysicsKind::Modal:
    return "modal";
  case PhysicsKind::Buckling:
    return "buckling";
  case PhysicsKind::Flow:
    return "flow";
  case PhysicsKind::Thermomechanical:
    return "thermomechanical";
  case PhysicsKind::Cht:
    return "cht";
  }
  return "unknown";
}

const char *to_string(Method m) {
  switch (m) {
  case Method::Simp:
    return "simp";
  case Method::Ramp:
    return "ramp";
  case Method::Beso:
    return "beso";
  case Method::LevelSet:
    return "level_set";
  }
  return "unknown";
}

const char *to_string(ObjectiveKind o) {
  switch (o) {
  case ObjectiveKind::Compliance:
    return "compliance";
  case ObjectiveKind::Mass:
    return "mass";
  case ObjectiveKind::ThermalCompliance:
    return "thermal_compliance";
  case ObjectiveKind::Frequency:
    return "frequency";
  case ObjectiveKind::BucklingFactor:
    return "buckling_factor";
  case ObjectiveKind::Dissipation:
    return "dissipation";
  case ObjectiveKind::MeanTemperature:
    return "mean_temperature";
  }
  return "unknown";
}

const char *to_string(DesignPhase p) {
  return p == DesignPhase::Solid ? "solid" : "fluid";
}

PhysicsKind parse_physics(const std::string &s) {
  for (PhysicsKind p :
       {PhysicsKind::Structural, PhysicsKind::Thermal, PhysicsKind::Modal,
        PhysicsKind::Buckling, PhysicsKind::Flow, PhysicsKind::Thermomechanical,
        PhysicsKind::Cht})
    if (s == to_string(p))
      return p;
  fail("unknown_physics", "physics \"" + s +
                              "\" is not one of structural, thermal, modal, "
                              "buckling, flow, thermomechanical, cht");
}

Method parse_method(const std::string &s) {
  for (Method m : {Method::Simp, Method::Ramp, Method::Beso, Method::LevelSet})
    if (s == to_string(m))
      return m;
  fail("unknown_method",
       "method \"" + s + "\" is not one of simp, ramp, beso, level_set");
}

ObjectiveKind parse_objective(const std::string &s) {
  for (ObjectiveKind o :
       {ObjectiveKind::Compliance, ObjectiveKind::Mass,
        ObjectiveKind::ThermalCompliance, ObjectiveKind::Frequency,
        ObjectiveKind::BucklingFactor, ObjectiveKind::Dissipation,
        ObjectiveKind::MeanTemperature})
    if (s == to_string(o))
      return o;
  fail("unknown_objective", "objective \"" + s + "\" is not a known objective");
}

ObjectiveSense objective_sense(ObjectiveKind o) {
  return (o == ObjectiveKind::Frequency || o == ObjectiveKind::BucklingFactor)
             ? ObjectiveSense::Maximize
             : ObjectiveSense::Minimize;
}

DesignPhase design_phase(PhysicsKind p) {
  return (p == PhysicsKind::Flow || p == PhysicsKind::Cht) ? DesignPhase::Fluid
                                                           : DesignPhase::Solid;
}

std::vector<ObjectiveKind> objectives_for(PhysicsKind p) {
  switch (p) {
  case PhysicsKind::Structural:
    return {ObjectiveKind::Compliance, ObjectiveKind::Mass};
  case PhysicsKind::Thermal:
    return {ObjectiveKind::ThermalCompliance};
  case PhysicsKind::Modal:
    return {ObjectiveKind::Frequency};
  case PhysicsKind::Buckling:
    return {ObjectiveKind::BucklingFactor};
  case PhysicsKind::Flow:
    return {ObjectiveKind::Dissipation};
  case PhysicsKind::Thermomechanical:
    return {ObjectiveKind::Compliance};
  case PhysicsKind::Cht:
    return {ObjectiveKind::MeanTemperature};
  }
  return {};
}

bool uses_structural_boundary(PhysicsKind p) {
  return p == PhysicsKind::Structural || p == PhysicsKind::Modal ||
         p == PhysicsKind::Buckling || p == PhysicsKind::Thermomechanical;
}
bool uses_thermal_boundary(PhysicsKind p) {
  return p == PhysicsKind::Thermal || p == PhysicsKind::Thermomechanical ||
         p == PhysicsKind::Cht;
}
bool uses_flow_boundary(PhysicsKind p) {
  return p == PhysicsKind::Flow || p == PhysicsKind::Cht;
}

// ── Interpolation
// ───────────────────────────────────────────────────────────────────
double Interpolation::shape(double rho) const {
  if (kind == Kind::Simp)
    return std::pow(rho, exponent);
  return rho / (1.0 + exponent * (1.0 - rho));
}

double Interpolation::shape_derivative(double rho) const {
  if (kind == Kind::Simp) {
    if (exponent == 1.0)
      return 1.0;
    return exponent * std::pow(rho, exponent - 1.0);
  }
  const double d = 1.0 + exponent * (1.0 - rho);
  return (1.0 + exponent) / (d * d);
}

double Interpolation::scale(double rho) const {
  return floor + (1.0 - floor) * shape(rho);
}
double Interpolation::scale_derivative(double rho) const {
  return (1.0 - floor) * shape_derivative(rho);
}

Interpolation interpolation_for(const Settings &s) {
  Interpolation i;
  i.floor = s.min_density;
  switch (s.method) {
  case Method::Simp:
  case Method::Beso:
    i.kind = Interpolation::Kind::Simp;
    i.exponent = s.penalty;
    break;
  case Method::Ramp:
    i.kind = Interpolation::Kind::Ramp;
    i.exponent = s.ramp_q;
    break;
  case Method::LevelSet:
    i.kind = Interpolation::Kind::Simp;
    i.exponent = 1.0;
    break;
  }
  return i;
}

// ── Settings
// ────────────────────────────────────────────────────────────────────────
Settings parse_settings(const json &t, bool has_build_plate) {
  const std::string ctx = "topology";
  check_keys(t,
             {"physics",
              "method",
              "objective",
              "volume_fraction",
              "max_iterations",
              "change_tolerance",
              "filter_radius_mm",
              "projection_beta",
              "penalty",
              "ramp_q",
              "min_density",
              "move_limit",
              "density_threshold",
              "stress_limit_mpa",
              "max_displacement_mm",
              "max_temperature_c",
              "min_frequency_hz",
              "min_buckling_factor",
              "max_pressure_drop_pa",
              "symmetry_axes",
              "symmetry_origin_mm",
              "extrusion_axis",
              "min_member_mm",
              "build_direction",
              "overhang_angle_deg",
              "overhang_penalty",
              "no_enclosed_voids",
              "preserve_thickness_mm",
              "exclude_thickness_mm",
              "beso_evolution_rate",
              "level_set_regularization",
              "result_dir"},
             ctx);
  Settings s;
  if (!t.contains("physics") || !t["physics"].is_string())
    fail("missing_key", "topology.physics is required (string)");
  s.physics = parse_physics(t["physics"].get<std::string>());
  if (t.contains("method")) {
    if (!t["method"].is_string())
      fail("invalid_input", "topology.method must be a string");
    s.method = parse_method(t["method"].get<std::string>());
  }
  const auto allowed = objectives_for(s.physics);
  s.objective = allowed.front();
  if (t.contains("objective")) {
    if (!t["objective"].is_string())
      fail("invalid_input", "topology.objective must be a string");
    s.objective = parse_objective(t["objective"].get<std::string>());
    if (std::find(allowed.begin(), allowed.end(), s.objective) == allowed.end())
      fail("objective_not_applicable",
           std::string("objective ") + to_string(s.objective) +
               " is not defined for physics " + to_string(s.physics));
  }

  auto range = [&](const char *key, double v, double lo, bool lo_open,
                   double hi, bool hi_open) {
    const bool ok =
        (lo_open ? v > lo : v >= lo) && (hi_open ? v < hi : v <= hi);
    if (!ok)
      fail("out_of_range", ctx + "." + key + "=" + fmt(v) + " outside " +
                               (lo_open ? "(" : "[") + fmt(lo) + "," + fmt(hi) +
                               (hi_open ? ")" : "]"));
    return v;
  };
  const double inf = std::numeric_limits<double>::infinity();
  s.volume_fraction =
      range("volume_fraction", req_number(t, "volume_fraction", ctx), 0, true,
            1, false);
  if (t.contains("max_iterations")) {
    if (!t["max_iterations"].is_number_integer() ||
        t["max_iterations"].get<long long>() < 1 ||
        t["max_iterations"].get<long long>() > 100000)
      fail("out_of_range",
           "topology.max_iterations must be an integer in [1,100000]");
    s.max_iterations = t["max_iterations"].get<int>();
  }
  if (auto v = opt_number(t, "change_tolerance", ctx))
    s.change_tolerance = range("change_tolerance", *v, 0, true, inf, false);
  if (auto v = opt_number(t, "filter_radius_mm", ctx))
    s.filter_radius_mm = range("filter_radius_mm", *v, 0, false, inf, true);
  if (auto v = opt_number(t, "min_density", ctx))
    s.min_density = range("min_density", *v, 0, true, 1, true);
  if (auto v = opt_number(t, "density_threshold", ctx))
    s.density_threshold = range("density_threshold", *v, 0, true, 1, true);
  if (auto v = opt_number(t, "min_member_mm", ctx))
    s.min_member_mm = range("min_member_mm", *v, 0, true, inf, true);
  if (auto v = opt_number(t, "preserve_thickness_mm", ctx))
    s.preserve_thickness_mm =
        range("preserve_thickness_mm", *v, 0, false, inf, true);
  if (auto v = opt_number(t, "exclude_thickness_mm", ctx))
    s.exclude_thickness_mm =
        range("exclude_thickness_mm", *v, 0, false, inf, true);

  auto method_key = [&](const char *key,
                        std::initializer_list<Method> methods) {
    if (!t.contains(key))
      return false;
    for (Method m : methods)
      if (m == s.method)
        return true;
    fail("key_not_applicable",
         ctx + "." + key + " does not apply to method " + to_string(s.method));
  };
  if (method_key("projection_beta", {Method::Simp, Method::Ramp}))
    s.projection_beta =
        range("projection_beta",
              as_number(t["projection_beta"], ctx + ".projection_beta"), 0,
              false, inf, true);
  if (method_key("move_limit", {Method::Simp, Method::Ramp}))
    s.move_limit =
        range("move_limit", as_number(t["move_limit"], ctx + ".move_limit"), 0,
              true, 1, false);
  if (method_key("penalty", {Method::Simp, Method::Beso}))
    s.penalty = range("penalty", as_number(t["penalty"], ctx + ".penalty"), 1,
                      false, inf, true);
  if (method_key("ramp_q", {Method::Ramp}))
    s.ramp_q = range("ramp_q", as_number(t["ramp_q"], ctx + ".ramp_q"), 0,
                     false, inf, true);
  if (method_key("beso_evolution_rate", {Method::Beso}))
    s.beso_evolution_rate =
        range("beso_evolution_rate",
              as_number(t["beso_evolution_rate"], ctx + ".beso_evolution_rate"),
              0, true, 1, true);
  if (method_key("level_set_regularization", {Method::LevelSet}))
    s.level_set_regularization =
        range("level_set_regularization",
              as_number(t["level_set_regularization"],
                        ctx + ".level_set_regularization"),
              0, false, inf, true);

  auto limit = [&](const char *key, std::initializer_list<PhysicsKind> physics,
                   double lo, bool lo_open) -> std::optional<double> {
    if (!t.contains(key))
      return std::nullopt;
    bool ok = false;
    for (PhysicsKind p : physics)
      ok = ok || p == s.physics;
    if (!ok)
      fail("limit_not_applicable", ctx + "." + key +
                                       " does not apply to physics " +
                                       to_string(s.physics));
    return range(key, as_number(t[key], ctx + "." + key), lo, lo_open, inf,
                 true);
  };
  s.stress_limit_mpa =
      limit("stress_limit_mpa",
            {PhysicsKind::Structural, PhysicsKind::Thermomechanical}, 0, true);
  s.max_displacement_mm =
      limit("max_displacement_mm",
            {PhysicsKind::Structural, PhysicsKind::Thermomechanical}, 0, true);
  s.max_temperature_c = limit(
      "max_temperature_c",
      {PhysicsKind::Thermal, PhysicsKind::Thermomechanical, PhysicsKind::Cht},
      -units::kKelvinAtZeroCelsius, true);
  s.min_frequency_hz = limit("min_frequency_hz", {PhysicsKind::Modal}, 0, true);
  s.min_buckling_factor =
      limit("min_buckling_factor", {PhysicsKind::Buckling}, 0, true);
  s.max_pressure_drop_pa = limit(
      "max_pressure_drop_pa", {PhysicsKind::Flow, PhysicsKind::Cht}, 0, true);
  if (s.objective == ObjectiveKind::Mass && !s.stress_limit_mpa &&
      !s.max_displacement_mm)
    fail("mass_requires_limit",
         "objective mass needs stress_limit_mpa or max_displacement_mm");

  if (t.contains("symmetry_axes")) {
    const json &a = t["symmetry_axes"];
    if (!a.is_array())
      fail("invalid_input", "topology.symmetry_axes must be an array");
    for (const auto &v : a) {
      const int ax = axis_index(v, "topology.symmetry_axes[]");
      if (s.symmetry_axes[ax])
        fail("invalid_input", "topology.symmetry_axes repeats an axis");
      s.symmetry_axes[ax] = true;
    }
  }
  if (t.contains("symmetry_origin_mm")) {
    if (!s.symmetry_axes[0] && !s.symmetry_axes[1] && !s.symmetry_axes[2])
      fail("key_not_applicable",
           "topology.symmetry_origin_mm needs symmetry_axes");
    s.symmetry_origin_mm =
        as_vec3(t["symmetry_origin_mm"], "topology.symmetry_origin_mm");
  }
  if (t.contains("extrusion_axis"))
    s.extrusion_axis =
        axis_index(t["extrusion_axis"], "topology.extrusion_axis");
  if (t.contains("build_direction")) {
    Eigen::Vector3d d =
        as_vec3(t["build_direction"], "topology.build_direction");
    if (!(d.norm() > 0))
      fail("out_of_range", "topology.build_direction must be non-zero");
    s.build_direction = d.normalized();
  }
  if (auto v = opt_number(t, "overhang_angle_deg", ctx))
    s.overhang_angle_deg = range("overhang_angle_deg", *v, 0, true, 90, true);
  if (auto v = opt_number(t, "overhang_penalty", ctx))
    s.overhang_penalty = range("overhang_penalty", *v, 0, false, inf, true);
  if (s.overhang_penalty > 0 && !s.build_direction && !has_build_plate)
    fail("build_direction_required",
         "Overhang control needs a build direction or a bed face");
  if (t.contains("no_enclosed_voids")) {
    if (!t["no_enclosed_voids"].is_boolean())
      fail("invalid_input", "topology.no_enclosed_voids must be a boolean");
    s.no_enclosed_voids = t["no_enclosed_voids"].get<bool>();
  }
  s.result_dir = opt_string(t, "result_dir", ctx);
  return s;
}

// ── Material
// ────────────────────────────────────────────────────────────────────────
MaterialInput parse_material(const json &m) {
  const std::string ctx = "material";
  if (!m.is_object())
    fail("invalid_input", "material must be an object");
  for (auto it = m.begin(); it != m.end(); ++it) {
    static const std::set<std::string> known = {
        "E_mpa",  "nu",          "yield_mpa", "density_g_cm3",
        "k_w_mk", "alpha_per_k", "cp_j_kgk",  "name"};
    if (!known.count(it.key()))
      fail("unsupported_material_key",
           "material." + it.key() +
               " is not supported (normalize to E_mpa, nu, "
               "yield_mpa, density_g_cm3, k_w_mk, alpha_per_k, cp_j_kgk)");
  }
  MaterialInput in;
  in.e_mpa = opt_number(m, "E_mpa", ctx);
  in.nu = opt_number(m, "nu", ctx);
  in.yield_mpa = opt_number(m, "yield_mpa", ctx);
  in.density_g_cm3 = opt_number(m, "density_g_cm3", ctx);
  in.k_w_mk = opt_number(m, "k_w_mk", ctx);
  in.alpha_per_k = opt_number(m, "alpha_per_k", ctx);
  in.cp_j_kgk = opt_number(m, "cp_j_kgk", ctx);
  in.name = opt_string(m, "name", ctx);
  return in;
}

Material convert_material(const MaterialInput &in) {
  auto positive = [](const std::optional<double> &v, const char *key) {
    if (v && !(std::isfinite(*v) && *v > 0))
      fail("invalid_material",
           std::string("material.") + key + " must be finite and > 0");
  };
  positive(in.e_mpa, "E_mpa");
  positive(in.yield_mpa, "yield_mpa");
  positive(in.density_g_cm3, "density_g_cm3");
  positive(in.k_w_mk, "k_w_mk");
  positive(in.cp_j_kgk, "cp_j_kgk");
  if (in.nu && !(std::isfinite(*in.nu) && *in.nu > -1.0 && *in.nu < 0.5))
    fail("invalid_material", "material.nu must satisfy -1 < nu < 0.5");
  if (in.alpha_per_k && !std::isfinite(*in.alpha_per_k))
    fail("invalid_material", "material.alpha_per_k must be finite");
  Material m;
  if (in.e_mpa)
    m.youngs_pa = units::mpa_to_pa(*in.e_mpa);
  m.poisson = in.nu;
  if (in.yield_mpa)
    m.yield_pa = units::mpa_to_pa(*in.yield_mpa);
  if (in.density_g_cm3)
    m.density_kg_m3 = units::g_cm3_to_kg_m3(*in.density_g_cm3);
  m.conductivity_w_mk = in.k_w_mk;
  m.expansion_per_k = in.alpha_per_k;
  m.specific_heat_j_kgk = in.cp_j_kgk;
  m.name = in.name;
  return m;
}

void require_material(const Material &m, PhysicsKind p) {
  auto need = [&](const std::optional<double> &v, const char *key) {
    if (!v)
      fail("material_missing",
           std::string("physics ") + to_string(p) + " needs material." + key);
  };
  switch (p) {
  case PhysicsKind::Structural:
  case PhysicsKind::Buckling:
    need(m.youngs_pa, "E_mpa");
    need(m.poisson, "nu");
    break;
  case PhysicsKind::Modal:
    need(m.youngs_pa, "E_mpa");
    need(m.poisson, "nu");
    need(m.density_kg_m3, "density_g_cm3");
    break;
  case PhysicsKind::Thermal:
  case PhysicsKind::Cht:
    need(m.conductivity_w_mk, "k_w_mk");
    break;
  case PhysicsKind::Thermomechanical:
    need(m.youngs_pa, "E_mpa");
    need(m.poisson, "nu");
    need(m.conductivity_w_mk, "k_w_mk");
    need(m.expansion_per_k, "alpha_per_k");
    break;
  case PhysicsKind::Flow:
    break;
  }
}

Eigen::Matrix<double, 6, 6> isotropic_elasticity(double E, double nu) {
  if (!(std::isfinite(E) && E > 0 && std::isfinite(nu) && nu > -1.0 &&
        nu < 0.5))
    fail("invalid_material",
         "isotropic elasticity needs E > 0 and -1 < nu < 0.5");
  const double lambda = E * nu / ((1.0 + nu) * (1.0 - 2.0 * nu));
  const double mu = E / (2.0 * (1.0 + nu));
  Eigen::Matrix<double, 6, 6> C = Eigen::Matrix<double, 6, 6>::Zero();
  C.topLeftCorner<3, 3>().setConstant(lambda);
  for (int i = 0; i < 3; ++i) {
    C(i, i) += 2.0 * mu;
    C(3 + i, 3 + i) = mu;
  }
  return C;
}

// ── Mesh
// ────────────────────────────────────────────────────────────────────────────
std::shared_ptr<const TetMesh>
build_tet_mesh(const std::vector<double> &points_mm,
               const std::vector<int> &connectivity, int nodes_per_elem) {
  if (nodes_per_elem != 4)
    fail("unsupported_element_order", "topology optimization meshes must be "
                                      "linear tet4 (nodes_per_elem=4), got " +
                                          std::to_string(nodes_per_elem));
  if (points_mm.empty() || points_mm.size() % 3 != 0)
    fail("invalid_mesh", "points must be a non-empty flat xyz array");
  if (connectivity.empty() || connectivity.size() % 4 != 0)
    fail("invalid_mesh",
         "tets must be a non-empty flat array of 4 node indices per element");
  const int N = static_cast<int>(points_mm.size() / 3);
  const int E = static_cast<int>(connectivity.size() / 4);
  auto mesh = std::make_shared<TetMesh>();
  TetMesh &m = *mesh;
  m.nodes_m.resize(3, N);
  for (int n = 0; n < N; ++n)
    for (int d = 0; d < 3; ++d) {
      const double v = points_mm[3 * n + d];
      if (!std::isfinite(v))
        fail("invalid_mesh",
             "node " + std::to_string(n) + " has a non-finite coordinate");
      m.nodes_m(d, n) = units::mm_to_m(v);
    }
  m.tets.resize(4, E);
  for (int e = 0; e < E; ++e) {
    for (int a = 0; a < 4; ++a) {
      const int n = connectivity[4 * e + a];
      if (n < 0 || n >= N)
        fail("invalid_mesh", "element " + std::to_string(e) +
                                 " references node " + std::to_string(n) +
                                 " outside [0," + std::to_string(N) + ")");
      m.tets(a, e) = n;
    }
    for (int a = 0; a < 4; ++a)
      for (int b = a + 1; b < 4; ++b)
        if (m.tets(a, e) == m.tets(b, e))
          fail("invalid_mesh", "element " + std::to_string(e) +
                                   " repeats node " +
                                   std::to_string(m.tets(a, e)));
  }
  {
    std::vector<std::pair<std::array<int, 4>, int>> sorted(E);
    for (int e = 0; e < E; ++e) {
      std::array<int, 4> k{m.tets(0, e), m.tets(1, e), m.tets(2, e),
                           m.tets(3, e)};
      std::sort(k.begin(), k.end());
      sorted[e] = {k, e};
    }
    std::sort(sorted.begin(), sorted.end());
    for (int i = 1; i < E; ++i)
      if (sorted[i].first == sorted[i - 1].first)
        fail("duplicate_element",
             "elements " + std::to_string(sorted[i - 1].second) + " and " +
                 std::to_string(sorted[i].second) + " share all four nodes");
  }

  m.grad_n.resize(3, 4 * E);
  m.volume_m3.resize(E);
  m.centroid_m.resize(3, E);
  m.element_size_m.resize(E);
  const double kRegular = 6.0 * std::sqrt(2.0);
  double min_edge = std::numeric_limits<double>::infinity(), max_edge = 0.0,
         quality_sum = 0.0;
  double min_quality = std::numeric_limits<double>::infinity();
  static constexpr int kEdges[6][2] = {{0, 1}, {0, 2}, {0, 3},
                                       {1, 2}, {1, 3}, {2, 3}};
  for (int e = 0; e < E; ++e) {
    auto X = [&](int a) -> Eigen::Vector3d {
      return m.nodes_m.col(m.tets(a, e));
    };
    double l2sum = 0.0, lmax = 0.0;
    for (const auto &ed : kEdges) {
      const double l = (X(ed[1]) - X(ed[0])).norm();
      l2sum += l * l;
      lmax = std::max(lmax, l);
      min_edge = std::min(min_edge, l);
      max_edge = std::max(max_edge, l);
    }
    Eigen::Matrix3d J;
    J.col(0) = X(1) - X(0);
    J.col(1) = X(2) - X(0);
    J.col(2) = X(3) - X(0);
    double det = J.determinant();
    if (!(std::abs(det) > 1e-10 * lmax * lmax * lmax))
      fail("degenerate_element", "element " + std::to_string(e) +
                                     " has (near-)zero volume (6V=" + fmt(det) +
                                     " m^3, longest edge " +
                                     fmt(units::m_to_mm(lmax)) + " mm)");
    if (det < 0) {
      std::swap(m.tets(2, e), m.tets(3, e));
      ++m.quality.reoriented_elements;
      J.col(1) = X(2) - X(0);
      J.col(2) = X(3) - X(0);
      det = J.determinant();
    }
    const Eigen::Matrix3d Jinv = J.inverse();
    Eigen::Matrix<double, 3, 4> G;
    for (int a = 1; a < 4; ++a)
      G.col(a) = Jinv.row(a - 1).transpose();
    G.col(0) = -(G.col(1) + G.col(2) + G.col(3));
    m.grad_n.middleCols<4>(4 * e) = G;
    const double V = det / 6.0;
    m.volume_m3[e] = V;
    m.centroid_m.col(e) = 0.25 * (X(0) + X(1) + X(2) + X(3));
    m.element_size_m[e] = std::cbrt(kRegular * V);
    const double lrms = std::sqrt(l2sum / 6.0);
    const double q = kRegular * V / (lrms * lrms * lrms);
    min_quality = std::min(min_quality, q);
    quality_sum += q;
  }

  // Faces: sort by node triple, pair interior faces, keep exterior, refuse
  // non-manifold.
  m.face_neighbor.setConstant(4, E, -1);
  struct FaceRec {
    std::array<int, 3> key;
    int e;
    int a;
  };
  std::vector<FaceRec> faces;
  faces.reserve(4 * static_cast<std::size_t>(E));
  for (int e = 0; e < E; ++e)
    for (int a = 0; a < 4; ++a) {
      std::array<int, 3> k{m.tets(kTetFaces[a][0], e),
                           m.tets(kTetFaces[a][1], e),
                           m.tets(kTetFaces[a][2], e)};
      std::sort(k.begin(), k.end());
      faces.push_back({k, e, a});
    }
  std::sort(faces.begin(), faces.end(), [](const FaceRec &x, const FaceRec &y) {
    return x.key != y.key ? x.key < y.key
                          : (x.e != y.e ? x.e < y.e : x.a < y.a);
  });
  std::vector<std::pair<int, int>> exterior;
  for (std::size_t i = 0; i < faces.size();) {
    std::size_t j = i + 1;
    while (j < faces.size() && faces[j].key == faces[i].key)
      ++j;
    const std::size_t count = j - i;
    if (count == 1) {
      exterior.push_back({faces[i].e, faces[i].a});
    } else if (count == 2) {
      m.face_neighbor(faces[i].a, faces[i].e) = faces[i + 1].e;
      m.face_neighbor(faces[i + 1].a, faces[i + 1].e) = faces[i].e;
    } else {
      fail("nonmanifold_face", "face (" + std::to_string(faces[i].key[0]) +
                                   "," + std::to_string(faces[i].key[1]) + "," +
                                   std::to_string(faces[i].key[2]) +
                                   ") is shared by " + std::to_string(count) +
                                   " elements");
    }
    i = j;
  }
  const int F = static_cast<int>(exterior.size());
  m.surface_nodes.resize(3, F);
  m.surface_element.resize(F);
  m.surface_local_face.resize(F);
  m.surface_area_m2.resize(F);
  m.surface_normal.resize(3, F);
  m.node_exterior.assign(N, 0);
  for (int f = 0; f < F; ++f) {
    const int e = exterior[f].first, a = exterior[f].second;
    for (int k = 0; k < 3; ++k) {
      m.surface_nodes(k, f) = m.tets(kTetFaces[a][k], e);
      m.node_exterior[m.surface_nodes(k, f)] = 1;
    }
    const Eigen::Vector3d p0 = m.nodes_m.col(m.surface_nodes(0, f));
    const Eigen::Vector3d n =
        (m.nodes_m.col(m.surface_nodes(1, f)) - p0)
            .cross(m.nodes_m.col(m.surface_nodes(2, f)) - p0);
    m.surface_element[f] = e;
    m.surface_local_face[f] = a;
    m.surface_area_m2[f] = 0.5 * n.norm();
    m.surface_normal.col(f) = n.normalized();
  }

  // Node -> element CSR and referenced flags.
  m.node_element_offsets.setZero(N + 1);
  for (int e = 0; e < E; ++e)
    for (int a = 0; a < 4; ++a)
      ++m.node_element_offsets[m.tets(a, e) + 1];
  for (int n = 0; n < N; ++n)
    m.node_element_offsets[n + 1] += m.node_element_offsets[n];
  m.node_element_indices.resize(4 * E);
  {
    std::vector<int> cursor(m.node_element_offsets.data(),
                            m.node_element_offsets.data() + N);
    for (int e = 0; e < E; ++e)
      for (int a = 0; a < 4; ++a)
        m.node_element_indices[cursor[m.tets(a, e)]++] = e;
  }
  m.node_referenced.assign(N, 0);
  for (int n = 0; n < N; ++n) {
    m.node_referenced[n] =
        m.node_element_offsets[n + 1] > m.node_element_offsets[n] ? 1 : 0;
    if (!m.node_referenced[n])
      m.unreferenced_nodes.push_back(n);
  }

  UnionFind uf(E);
  for (int e = 0; e < E; ++e)
    for (int a = 0; a < 4; ++a)
      if (m.face_neighbor(a, e) >= 0)
        uf.unite(e, m.face_neighbor(a, e));
  int components = 0;
  for (int e = 0; e < E; ++e)
    components += uf.find(e) == e ? 1 : 0;

  Eigen::Matrix3Xd used(3, N - static_cast<int>(m.unreferenced_nodes.size()));
  for (int n = 0, k = 0; n < N; ++n)
    if (m.node_referenced[n])
      used.col(k++) = m.nodes_m.col(n);
  m.bbox_min_m = used.rowwise().minCoeff();
  m.bbox_max_m = used.rowwise().maxCoeff();
  m.total_volume_m3 = m.volume_m3.sum();

  MeshQuality &q = m.quality;
  q.min_volume_m3 = m.volume_m3.minCoeff();
  q.max_volume_m3 = m.volume_m3.maxCoeff();
  q.min_shape_quality = min_quality;
  q.mean_shape_quality = quality_sum / E;
  q.min_edge_m = min_edge;
  q.max_edge_m = max_edge;
  std::vector<double> sizes(m.element_size_m.data(),
                            m.element_size_m.data() + E);
  std::nth_element(sizes.begin(), sizes.begin() + E / 2, sizes.end());
  q.median_element_size_m = sizes[E / 2];
  q.unreferenced_nodes = static_cast<int>(m.unreferenced_nodes.size());
  q.components = components;
  return mesh;
}

std::vector<int> surface_faces_with_nodes(const TetMesh &m,
                                          const std::vector<int> &nodes) {
  std::vector<std::uint8_t> mark(m.node_count(), 0);
  for (int n : nodes)
    if (n >= 0 && n < m.node_count())
      mark[n] = 1;
  std::vector<int> out;
  for (int f = 0; f < m.surface_count(); ++f)
    if (mark[m.surface_nodes(0, f)] && mark[m.surface_nodes(1, f)] &&
        mark[m.surface_nodes(2, f)])
      out.push_back(f);
  return out;
}

std::vector<int> elements_touching_nodes(const TetMesh &m,
                                         const std::vector<int> &nodes) {
  std::vector<int> out;
  for (int n : nodes) {
    if (n < 0 || n >= m.node_count())
      continue;
    for (int k = m.node_element_offsets[n]; k < m.node_element_offsets[n + 1];
         ++k)
      out.push_back(m.node_element_indices[k]);
  }
  std::sort(out.begin(), out.end());
  out.erase(std::unique(out.begin(), out.end()), out.end());
  return out;
}

Eigen::Matrix<double, 6, 12>
tet4_strain_matrix(const Eigen::Ref<const Eigen::Matrix<double, 3, 4>> &g) {
  Eigen::Matrix<double, 6, 12> B = Eigen::Matrix<double, 6, 12>::Zero();
  for (int a = 0; a < 4; ++a) {
    const double dx = g(0, a), dy = g(1, a), dz = g(2, a);
    const int c = 3 * a;
    B(0, c) = dx;
    B(1, c + 1) = dy;
    B(2, c + 2) = dz;
    B(3, c) = dy;
    B(3, c + 1) = dx;
    B(4, c + 1) = dz;
    B(4, c + 2) = dy;
    B(5, c) = dz;
    B(5, c + 2) = dx;
  }
  return B;
}

Eigen::MatrixXi nodal_dof_map(const TetMesh &m, int components,
                              int base_offset) {
  if (components < 1)
    fail("invalid_input", "nodal_dof_map needs components >= 1");
  Eigen::MatrixXi map(4 * components, m.element_count());
  for (int e = 0; e < m.element_count(); ++e)
    for (int a = 0; a < 4; ++a)
      for (int c = 0; c < components; ++c)
        map(components * a + c, e) =
            base_offset + components * m.tets(a, e) + c;
  return map;
}

// ── Dirichlet / reduction
// ───────────────────────────────────────────────────────────
Dirichlet make_dirichlet(int full_size,
                         const std::vector<std::pair<int, double>> &entries) {
  std::vector<std::pair<int, double>> sorted = entries;
  for (const auto &[dof, value] : sorted) {
    if (dof < 0 || dof >= full_size)
      fail("invalid_bc", "Dirichlet dof " + std::to_string(dof) +
                             " outside [0," + std::to_string(full_size) + ")");
    if (!std::isfinite(value))
      fail("invalid_bc",
           "Dirichlet value for dof " + std::to_string(dof) + " is not finite");
  }
  std::stable_sort(
      sorted.begin(), sorted.end(),
      [](const auto &x, const auto &y) { return x.first < y.first; });
  std::vector<int> dofs;
  std::vector<double> values;
  for (const auto &[dof, value] : sorted) {
    if (!dofs.empty() && dofs.back() == dof) {
      const double prev = values.back();
      if (std::abs(prev - value) >
          1e-12 * std::max({1.0, std::abs(prev), std::abs(value)}))
        fail("dirichlet_conflict", "dof " + std::to_string(dof) +
                                       " prescribed both " + fmt(prev) +
                                       " and " + fmt(value));
      continue;
    }
    dofs.push_back(dof);
    values.push_back(value);
  }
  Dirichlet d;
  d.dofs = Eigen::Map<const Eigen::VectorXi>(
      dofs.data(), static_cast<Eigen::Index>(dofs.size()));
  d.values = Eigen::Map<const Eigen::VectorXd>(
      values.data(), static_cast<Eigen::Index>(values.size()));
  return d;
}

DofReduction make_dof_reduction(int full_size, const Dirichlet &bc) {
  if (full_size < 0)
    fail("invalid_input", "negative DOF count");
  DofReduction r;
  r.full_size = full_size;
  r.full_to_free.setZero(full_size);
  r.prescribed.setZero(full_size);
  for (int i = 0; i < bc.size(); ++i) {
    const int dof = bc.dofs[i];
    if (dof < 0 || dof >= full_size)
      fail("invalid_bc",
           "Dirichlet dof " + std::to_string(dof) + " out of range");
    if (r.full_to_free[dof] == -1)
      fail("invalid_bc", "Dirichlet dof " + std::to_string(dof) + " repeated");
    r.full_to_free[dof] = -1;
    r.prescribed[dof] = bc.values[i];
  }
  int k = 0;
  for (int i = 0; i < full_size; ++i)
    if (r.full_to_free[i] != -1)
      ++k;
  r.free_to_full.resize(k);
  k = 0;
  for (int i = 0; i < full_size; ++i)
    if (r.full_to_free[i] != -1) {
      r.free_to_full[k] = i;
      r.full_to_free[i] = k++;
    }
  return r;
}

void DofReduction::restrict_vector(const Eigen::VectorXd &full,
                                   Eigen::VectorXd &free) const {
  if (full.size() != full_size)
    fail("invalid_input", "restrict_vector size mismatch");
  free.resize(free_size());
  for (int i = 0; i < free_size(); ++i)
    free[i] = full[free_to_full[i]];
}

void DofReduction::expand_vector(const Eigen::VectorXd &free,
                                 Eigen::VectorXd &full) const {
  if (free.size() != free_size())
    fail("invalid_input", "expand_vector size mismatch");
  full = prescribed;
  for (int i = 0; i < free_size(); ++i)
    full[free_to_full[i]] = free[i];
}

ReducedPattern::ReducedPattern(const Eigen::MatrixXi &element_dofs,
                               const DofReduction &reduction)
    : element_dofs_(element_dofs), reduction_(reduction) {
  const int k = static_cast<int>(element_dofs.rows());
  const int E = static_cast<int>(element_dofs.cols());
  const int nf = reduction.free_size();
  for (int e = 0; e < E; ++e)
    for (int i = 0; i < k; ++i)
      if (element_dofs(i, e) < 0 || element_dofs(i, e) >= reduction.full_size)
        fail("invalid_input", "element DOF " +
                                  std::to_string(element_dofs(i, e)) +
                                  " outside the reduction");
  std::vector<Eigen::Triplet<double>> trip;
  trip.reserve(static_cast<std::size_t>(E) * k * k);
  for (int e = 0; e < E; ++e)
    for (int j = 0; j < k; ++j) {
      const int fj = reduction.full_to_free[element_dofs(j, e)];
      if (fj < 0)
        continue;
      for (int i = 0; i < k; ++i) {
        const int fi = reduction.full_to_free[element_dofs(i, e)];
        if (fi >= 0)
          trip.emplace_back(fi, fj, 1.0);
      }
    }
  pattern_.resize(nf, nf);
  pattern_.setFromTriplets(trip.begin(), trip.end());
  pattern_.makeCompressed();
  std::fill(pattern_.valuePtr(), pattern_.valuePtr() + pattern_.nonZeros(),
            0.0);
  slot_.assign(static_cast<std::size_t>(E) * k * k, -1);
  const int *outer = pattern_.outerIndexPtr();
  const int *inner = pattern_.innerIndexPtr();
  for (int e = 0; e < E; ++e)
    for (int j = 0; j < k; ++j) {
      const int fj = reduction.full_to_free[element_dofs(j, e)];
      if (fj < 0)
        continue;
      for (int i = 0; i < k; ++i) {
        const int fi = reduction.full_to_free[element_dofs(i, e)];
        if (fi < 0)
          continue;
        const int *pos =
            std::lower_bound(inner + outer[fj], inner + outer[fj + 1], fi);
        slot_[(static_cast<std::size_t>(e) * k + j) * k + i] =
            static_cast<int>(pos - inner);
      }
    }
}

void ReducedPattern::zero(Eigen::SparseMatrix<double> &K) {
  std::fill(K.valuePtr(), K.valuePtr() + K.nonZeros(), 0.0);
}

void ReducedPattern::add(Eigen::SparseMatrix<double> &K, int e,
                         const Eigen::Ref<const Eigen::MatrixXd> &ke) const {
  const int k = dofs_per_element();
  if (ke.rows() != k || ke.cols() != k)
    fail("invalid_input", "element matrix size does not match the DOF map");
  if (K.nonZeros() != pattern_.nonZeros() || K.rows() != pattern_.rows())
    fail("pattern_mismatch", "matrix does not carry the reduced pattern");
  double *values = K.valuePtr();
  const int *s = slot_.data() + static_cast<std::size_t>(e) * k * k;
  for (int j = 0; j < k; ++j)
    for (int i = 0; i < k; ++i) {
      const int slot = s[j * k + i];
      if (slot >= 0)
        values[slot] += ke(i, j);
    }
}

void ReducedPattern::lift(Eigen::VectorXd &rhs_free, int e,
                          const Eigen::Ref<const Eigen::MatrixXd> &ke) const {
  const int k = dofs_per_element();
  if (ke.rows() != k || ke.cols() != k)
    fail("invalid_input", "element matrix size does not match the DOF map");
  if (rhs_free.size() != reduction_.free_size())
    fail("invalid_input", "rhs size does not match the reduction");
  for (int j = 0; j < k; ++j) {
    const int dj = element_dofs_(j, e);
    if (reduction_.full_to_free[dj] >= 0)
      continue;
    const double u = reduction_.prescribed[dj];
    if (u == 0.0)
      continue;
    for (int i = 0; i < k; ++i) {
      const int fi = reduction_.full_to_free[element_dofs_(i, e)];
      if (fi >= 0)
        rhs_free[fi] -= ke(i, j) * u;
    }
  }
}

// ── Roles / regions
// ─────────────────────────────────────────────────────────────────
std::vector<std::uint8_t> ElementRoles::solid_mask() const {
  std::vector<std::uint8_t> m(role.size());
  for (std::size_t i = 0; i < role.size(); ++i)
    m[i] = role[i] == ElementRole::Solid;
  return m;
}

std::vector<std::uint8_t> ElementRoles::void_mask() const {
  std::vector<std::uint8_t> m(role.size());
  for (std::size_t i = 0; i < role.size(); ++i)
    m[i] = role[i] == ElementRole::Void;
  return m;
}

void ElementRoles::recount() {
  design_count = solid_count = void_count = 0;
  for (ElementRole r : role) {
    if (r == ElementRole::Design)
      ++design_count;
    else if (r == ElementRole::Solid)
      ++solid_count;
    else
      ++void_count;
  }
}

std::vector<int> region_elements(const TetMesh &m, const RegionSpec &r,
                                 double default_thickness_m) {
  std::vector<int> out;
  if (r.has_box) {
    const Eigen::Vector3d lo = r.box_min_mm * units::kMeterPerMm,
                          hi = r.box_max_mm * units::kMeterPerMm;
    if ((lo.array() > hi.array()).any())
      fail("invalid_region", "region " + r.label + " has box_min > box_max");
    for (int e = 0; e < m.element_count(); ++e) {
      const Eigen::Vector3d c = m.centroid_m.col(e);
      if ((c.array() >= lo.array()).all() && (c.array() <= hi.array()).all())
        out.push_back(e);
    }
    return out;
  }
  for (int n : r.nodes)
    if (n < 0 || n >= m.node_count())
      fail("invalid_region", "region " + r.label + " node " +
                                 std::to_string(n) + " out of range");
  const double t =
      r.thickness_mm ? units::mm_to_m(*r.thickness_mm) : default_thickness_m;
  if (!(std::isfinite(t) && t >= 0))
    fail("invalid_region",
         "region " + r.label + " thickness must be finite and >= 0");
  out = elements_touching_nodes(m, r.nodes);
  if (t > 0) {
    std::vector<std::uint8_t> in(m.element_count(), 0);
    for (int e : out)
      in[e] = 1;
    const std::vector<int> faces = surface_faces_with_nodes(m, r.nodes);
    if (!faces.empty()) {
      Eigen::Matrix3Xd fc(3, faces.size());
      double reach = 0.0;
      for (std::size_t i = 0; i < faces.size(); ++i) {
        const int f = faces[i];
        Eigen::Vector3d c = Eigen::Vector3d::Zero();
        for (int k = 0; k < 3; ++k)
          c += m.nodes_m.col(m.surface_nodes(k, f));
        c /= 3.0;
        fc.col(i) = c;
        for (int k = 0; k < 3; ++k)
          reach = std::max(reach,
                           (m.nodes_m.col(m.surface_nodes(k, f)) - c).norm());
      }
      PointGrid grid(fc, t + reach);
      for (int e = 0; e < m.element_count(); ++e) {
        if (in[e])
          continue;
        const Eigen::Vector3d p = m.centroid_m.col(e);
        bool hit = false;
        grid.for_each_within(p, t + reach, [&](int i, double) {
          if (hit)
            return;
          const int f = faces[i];
          const Eigen::Vector3d q =
              closest_on_triangle(p, m.nodes_m.col(m.surface_nodes(0, f)),
                                  m.nodes_m.col(m.surface_nodes(1, f)),
                                  m.nodes_m.col(m.surface_nodes(2, f)));
          hit = (q - p).norm() <= t;
        });
        if (hit)
          in[e] = 1;
      }
    } else if (!r.nodes.empty()) {
      Eigen::Matrix3Xd pts(3, r.nodes.size());
      for (std::size_t i = 0; i < r.nodes.size(); ++i)
        pts.col(i) = m.nodes_m.col(r.nodes[i]);
      PointGrid grid(pts, t);
      for (int e = 0; e < m.element_count(); ++e) {
        if (in[e])
          continue;
        bool hit = false;
        grid.for_each_within(m.centroid_m.col(e), t,
                             [&](int, double) { hit = true; });
        if (hit)
          in[e] = 1;
      }
    }
    out.clear();
    for (int e = 0; e < m.element_count(); ++e)
      if (in[e])
        out.push_back(e);
  }
  return out;
}

// ── Design map
// ──────────────────────────────────────────────────────────────────────
DesignMap build_design_map(const TetMesh &m, ElementRoles &roles,
                           const TyingSpec &spec) {
  const int E = m.element_count();
  if (static_cast<int>(roles.role.size()) != E ||
      static_cast<int>(roles.reason.size()) != E)
    fail("invalid_input", "element roles do not match the mesh");
  UnionFind uf(E);
  DesignMap dm;
  const bool any_mirror = spec.mirror[0] || spec.mirror[1] || spec.mirror[2];
  if (any_mirror) {
    PointGrid grid(m.centroid_m, m.quality.median_element_size_m);
    for (int axis = 0; axis < 3; ++axis) {
      if (!spec.mirror[axis])
        continue;
      for (int e = 0; e < E; ++e) {
        Eigen::Vector3d img = m.centroid_m.col(e);
        img[axis] = 2.0 * spec.mirror_origin_m[axis] - img[axis];
        double d = 0.0;
        const int f = grid.nearest(img, &d);
        const double tol = std::max(m.element_size_m[e], m.element_size_m[f]);
        if (d > tol)
          fail("symmetry_domain_mismatch",
               std::string("element ") + std::to_string(e) +
                   " has no mirror partner across the " + "xyz"[axis] +
                   " plane at " +
                   fmt(units::m_to_mm(spec.mirror_origin_m[axis])) +
                   " mm (nearest image " + fmt(units::m_to_mm(d)) +
                   " mm away, tolerance " + fmt(units::m_to_mm(tol)) +
                   " mm); the design domain is not symmetric");
        dm.max_tie_distance_m = std::max(dm.max_tie_distance_m, d);
        uf.unite(e, f);
      }
    }
  }
  if (spec.extrusion_axis >= 0) {
    if (spec.extrusion_axis > 2)
      fail("invalid_input", "extrusion axis must be 0, 1 or 2");
    const double cell = spec.extrusion_cell_m > 0
                            ? spec.extrusion_cell_m
                            : m.quality.median_element_size_m;
    if (!(cell > 0) || !std::isfinite(cell))
      fail("invalid_input", "extrusion cell size must be > 0");
    const int u = (spec.extrusion_axis + 1) % 3,
              v = (spec.extrusion_axis + 2) % 3;
    std::map<std::pair<long long, long long>, int> first;
    for (int e = 0; e < E; ++e) {
      const auto key = std::make_pair(
          static_cast<long long>(
              std::floor((m.centroid_m(u, e) - m.bbox_min_m[u]) / cell)),
          static_cast<long long>(
              std::floor((m.centroid_m(v, e) - m.bbox_min_m[v]) / cell)));
      auto it = first.emplace(key, e).first;
      uf.unite(e, it->second);
    }
  }

  std::vector<std::vector<int>> groups(E);
  for (int e = 0; e < E; ++e)
    groups[uf.find(e)].push_back(e);
  dm.element_variable.setConstant(E, -1);
  dm.fixed_density.setZero(E);
  std::vector<int> offsets{0}, members;
  std::vector<double> volumes;
  for (int root = 0; root < E; ++root) {
    const auto &g = groups[root];
    if (g.empty())
      continue;
    int solid = -1, empty = -1;
    for (int e : g) {
      if (roles.role[e] == ElementRole::Solid && solid < 0)
        solid = e;
      if (roles.role[e] == ElementRole::Void && empty < 0)
        empty = e;
    }
    if (solid >= 0 && empty >= 0)
      fail("tie_passive_conflict",
           "symmetry/extrusion ties fixed-solid element " +
               std::to_string(solid) + " to fixed-void element " +
               std::to_string(empty));
    if (solid >= 0 || empty >= 0) {
      const ElementRole r = solid >= 0 ? ElementRole::Solid : ElementRole::Void;
      for (int e : g) {
        if (roles.role[e] != r) {
          roles.role[e] = r;
          roles.reason[e] |= kReasonTied;
        }
        dm.fixed_density[e] = r == ElementRole::Solid ? 1.0 : 0.0;
      }
      continue;
    }
    const int var = static_cast<int>(volumes.size());
    double vol = 0.0;
    for (int e : g) {
      dm.element_variable[e] = var;
      members.push_back(e);
      vol += m.volume_m3[e];
    }
    offsets.push_back(static_cast<int>(members.size()));
    volumes.push_back(vol);
    if (g.size() > 1)
      ++dm.tied_groups;
  }
  dm.variable_count = static_cast<int>(volumes.size());
  dm.variable_offsets = Eigen::Map<const Eigen::VectorXi>(
      offsets.data(), static_cast<Eigen::Index>(offsets.size()));
  dm.variable_elements = Eigen::Map<const Eigen::VectorXi>(
      members.data(), static_cast<Eigen::Index>(members.size()));
  dm.variable_volume_m3 = Eigen::Map<const Eigen::VectorXd>(
      volumes.data(), static_cast<Eigen::Index>(volumes.size()));
  dm.tie_weight.setZero(E);
  for (int v = 0; v < dm.variable_count; ++v)
    for (int k = offsets[v]; k < offsets[v + 1]; ++k)
      dm.tie_weight[members[k]] = m.volume_m3[members[k]] / volumes[v];
  roles.recount();
  return dm;
}

void DesignMap::expand(const Eigen::VectorXd &x, Eigen::VectorXd &rho) const {
  if (x.size() != variable_count)
    fail("invalid_design", "design vector has " + std::to_string(x.size()) +
                               " entries, expected " +
                               std::to_string(variable_count));
  const Eigen::Index E = element_variable.size();
  rho.resize(E);
  for (Eigen::Index e = 0; e < E; ++e) {
    const int v = element_variable[e];
    rho[e] = v >= 0 ? x[v] : fixed_density[e];
  }
}

void DesignMap::reduce_gradient(const Eigen::VectorXd &g_rho,
                                Eigen::VectorXd &g_x) const {
  if (g_rho.size() != element_variable.size())
    fail("invalid_design", "element gradient size mismatch");
  g_x.setZero(variable_count);
  for (Eigen::Index e = 0; e < g_rho.size(); ++e) {
    const int v = element_variable[e];
    if (v >= 0)
      g_x[v] += g_rho[e];
  }
}

void DesignMap::project_ties(Eigen::VectorXd &y) const {
  if (y.size() != element_variable.size())
    fail("invalid_density", "tie projection size mismatch");
  if (tied_groups == 0)
    return;
  for (int v = 0; v < variable_count; ++v) {
    const int begin = variable_offsets[v], end = variable_offsets[v + 1];
    if (end - begin < 2)
      continue;
    double mean = 0.0;
    for (int k = begin; k < end; ++k)
      mean += tie_weight[variable_elements[k]] * y[variable_elements[k]];
    for (int k = begin; k < end; ++k)
      y[variable_elements[k]] = mean;
  }
}

void DesignMap::project_ties_transpose(Eigen::VectorXd &g) const {
  if (g.size() != element_variable.size())
    fail("invalid_density", "tie projection gradient size mismatch");
  if (tied_groups == 0)
    return;
  for (int v = 0; v < variable_count; ++v) {
    const int begin = variable_offsets[v], end = variable_offsets[v + 1];
    if (end - begin < 2)
      continue;
    double sum = 0.0;
    for (int k = begin; k < end; ++k)
      sum += g[variable_elements[k]];
    for (int k = begin; k < end; ++k)
      g[variable_elements[k]] = tie_weight[variable_elements[k]] * sum;
  }
}

// ── Filter
// ──────────────────────────────────────────────────────────────────────────
DensityFilter build_density_filter(const TetMesh &m, double radius_m) {
  if (!(std::isfinite(radius_m) && radius_m >= 0))
    fail("invalid_input", "filter radius must be finite and >= 0");
  const int E = m.element_count();
  DensityFilter f;
  f.radius_m = radius_m;
  f.weights.resize(E, E);
  if (radius_m == 0.0) {
    f.weights.setIdentity();
    f.weights.makeCompressed();
    return f;
  }
  PointGrid grid(m.centroid_m, radius_m);
  std::vector<Eigen::Triplet<double>> trip;
  std::vector<std::pair<int, double>> row;
  for (int e = 0; e < E; ++e) {
    row.clear();
    double sum = 0.0;
    grid.for_each_within(m.centroid_m.col(e), radius_m, [&](int j, double d) {
      const double w = (radius_m - d) * m.volume_m3[j];
      if (w > 0) {
        row.push_back({j, w});
        sum += w;
      }
    });
    for (const auto &[j, w] : row)
      trip.emplace_back(e, j, w / sum);
  }
  f.weights.setFromTriplets(trip.begin(), trip.end());
  f.weights.makeCompressed();
  return f;
}

void DensityFilter::apply(const Eigen::VectorXd &rho,
                          Eigen::VectorXd &out) const {
  if (rho.size() != weights.cols())
    fail("invalid_density", "filter input size mismatch");
  out = weights * rho;
}

void DensityFilter::apply_transpose(const Eigen::VectorXd &g,
                                    Eigen::VectorXd &out) const {
  if (g.size() != weights.rows())
    fail("invalid_density", "filter gradient size mismatch");
  out = weights.transpose() * g;
}

// ── Problem spec parsing
// ────────────────────────────────────────────────────────────
ProblemSpec parse_problem_spec(const json &j) {
  check_keys(j,
             {"topology", "material", "fixtures", "load_cases", "thermal",
              "flow", "preserve", "exclude", "build_plate"},
             "problem");
  ProblemSpec s;
  if (!j.contains("topology"))
    fail("missing_key", "problem.topology is required");
  s.settings = parse_settings(j["topology"], j.contains("build_plate"));
  s.material =
      parse_material(j.contains("material") ? j["material"] : json::object());
  if (j.contains("fixtures"))
    s.fixtures = parse_fixtures(j["fixtures"], "fixtures");
  if (j.contains("load_cases")) {
    const json &arr = j["load_cases"];
    if (!arr.is_array())
      fail("invalid_input", "load_cases must be an array");
    for (std::size_t i = 0; i < arr.size(); ++i) {
      const std::string c = "load_cases[" + std::to_string(i) + "]";
      check_keys(arr[i], {"name", "weight", "fixtures", "forces_n"}, c);
      LoadCaseSpec lc;
      lc.name = opt_string(arr[i], "name", c);
      if (auto w = opt_number(arr[i], "weight", c))
        lc.weight = *w;
      if (arr[i].contains("fixtures"))
        lc.fixtures = parse_fixtures(arr[i]["fixtures"], c + ".fixtures");
      if (!arr[i].contains("forces_n") || !arr[i]["forces_n"].is_array())
        fail("missing_key",
             c + ".forces_n (flat 3*N nodal forces in N) is required");
      for (const auto &v : arr[i]["forces_n"])
        lc.forces_n.push_back(as_number(v, c + ".forces_n[]"));
      s.load_cases.push_back(std::move(lc));
    }
  }
  if (j.contains("thermal")) {
    const json &t = j["thermal"];
    check_keys(t, {"reference_temperature_c", "fixtures", "loads"}, "thermal");
    ThermalSpec ts;
    ts.reference_temperature_c =
        opt_number(t, "reference_temperature_c", "thermal");
    if (t.contains("fixtures")) {
      if (!t["fixtures"].is_array())
        fail("invalid_input", "thermal.fixtures must be an array");
      for (std::size_t i = 0; i < t["fixtures"].size(); ++i) {
        const json &f = t["fixtures"][i];
        const std::string c = "thermal.fixtures[" + std::to_string(i) + "]";
        check_keys(f, {"label", "nodes", "temperature_c"}, c);
        ThermalFixtureSpec fs;
        fs.label = opt_string(f, "label", c);
        if (!f.contains("nodes"))
          fail("missing_key", c + ".nodes is required");
        fs.nodes = as_nodes(f["nodes"], c + ".nodes");
        fs.temperature_c = req_number(f, "temperature_c", c);
        ts.fixtures.push_back(std::move(fs));
      }
    }
    if (t.contains("loads")) {
      if (!t["loads"].is_array())
        fail("invalid_input", "thermal.loads must be an array");
      for (std::size_t i = 0; i < t["loads"].size(); ++i) {
        const json &l = t["loads"][i];
        const std::string c = "thermal.loads[" + std::to_string(i) + "]";
        if (!l.is_object() || !l.contains("type") || !l["type"].is_string())
          fail("missing_key", c + ".type (flux|power|convection) is required");
        const std::string type = l["type"].get<std::string>();
        ThermalLoadSpec ls;
        if (type == "flux") {
          check_keys(l, {"label", "nodes", "type", "magnitude_w_m2"}, c);
          ls.kind = ThermalLoadKind::Flux;
          ls.magnitude_w_m2 = req_number(l, "magnitude_w_m2", c);
        } else if (type == "power") {
          check_keys(l, {"label", "nodes", "type", "power_w"}, c);
          ls.kind = ThermalLoadKind::Power;
          ls.power_w = req_number(l, "power_w", c);
        } else if (type == "convection") {
          check_keys(l, {"label", "nodes", "type", "h_w_m2k", "ambient_c"}, c);
          ls.kind = ThermalLoadKind::Convection;
          ls.h_w_m2k = req_number(l, "h_w_m2k", c);
          ls.ambient_c = req_number(l, "ambient_c", c);
        } else {
          fail("invalid_bc",
               c + ".type \"" + type + "\" is not flux, power or convection");
        }
        ls.label = opt_string(l, "label", c);
        if (!l.contains("nodes"))
          fail("missing_key", c + ".nodes is required");
        ls.nodes = as_nodes(l["nodes"], c + ".nodes");
        ts.loads.push_back(std::move(ls));
      }
    }
    s.thermal = std::move(ts);
  }
  if (j.contains("flow")) {
    const json &f = j["flow"];
    check_keys(f,
               {"viscosity_pa_s", "density_kg_m3", "brinkman_max",
                "reference_length_mm", "fluid_k_w_mk", "fluid_cp_j_kgk",
                "inlets", "outlets", "walls"},
               "flow");
    FlowSpec fs;
    fs.viscosity_pa_s = req_number(f, "viscosity_pa_s", "flow");
    fs.density_kg_m3 = req_number(f, "density_kg_m3", "flow");
    if (auto v = opt_number(f, "brinkman_max", "flow"))
      fs.brinkman_max = *v;
    fs.reference_length_mm = opt_number(f, "reference_length_mm", "flow");
    fs.fluid_k_w_mk = opt_number(f, "fluid_k_w_mk", "flow");
    fs.fluid_cp_j_kgk = opt_number(f, "fluid_cp_j_kgk", "flow");
    auto arr = [&](const char *key) -> const json & {
      static const json empty = json::array();
      if (!f.contains(key))
        return empty;
      if (!f[key].is_array())
        fail("invalid_input", std::string("flow.") + key + " must be an array");
      return f[key];
    };
    const json &inlets = arr("inlets");
    for (std::size_t i = 0; i < inlets.size(); ++i) {
      const std::string c = "flow.inlets[" + std::to_string(i) + "]";
      check_keys(inlets[i], {"label", "nodes", "velocity_m_s", "temperature_c"},
                 c);
      FlowInletSpec in;
      in.label = opt_string(inlets[i], "label", c);
      if (!inlets[i].contains("nodes"))
        fail("missing_key", c + ".nodes is required");
      in.nodes = as_nodes(inlets[i]["nodes"], c + ".nodes");
      if (!inlets[i].contains("velocity_m_s"))
        fail("missing_key", c + ".velocity_m_s is required");
      in.velocity_m_s = as_vec3(inlets[i]["velocity_m_s"], c + ".velocity_m_s");
      in.temperature_c = opt_number(inlets[i], "temperature_c", c);
      fs.inlets.push_back(std::move(in));
    }
    const json &outlets = arr("outlets");
    for (std::size_t i = 0; i < outlets.size(); ++i) {
      const std::string c = "flow.outlets[" + std::to_string(i) + "]";
      check_keys(outlets[i], {"label", "nodes", "pressure_pa"}, c);
      FlowOutletSpec out;
      out.label = opt_string(outlets[i], "label", c);
      if (!outlets[i].contains("nodes"))
        fail("missing_key", c + ".nodes is required");
      out.nodes = as_nodes(outlets[i]["nodes"], c + ".nodes");
      out.pressure_pa = req_number(outlets[i], "pressure_pa", c);
      fs.outlets.push_back(std::move(out));
    }
    const json &walls = arr("walls");
    for (std::size_t i = 0; i < walls.size(); ++i) {
      const std::string c = "flow.walls[" + std::to_string(i) + "]";
      check_keys(walls[i], {"label", "nodes"}, c);
      NodeSet w;
      w.label = opt_string(walls[i], "label", c);
      if (!walls[i].contains("nodes"))
        fail("missing_key", c + ".nodes is required");
      w.nodes = as_nodes(walls[i]["nodes"], c + ".nodes");
      fs.walls.push_back(std::move(w));
    }
    s.flow = std::move(fs);
  }
  if (j.contains("build_plate")) {
    const json &b = j["build_plate"];
    check_keys(b, {"label", "nodes"}, "build_plate");
    if (!b.contains("nodes"))
      fail("missing_key", "build_plate.nodes is required");
    s.build_plate = NodeSet{opt_string(b, "label", "build_plate"),
                           as_nodes(b["nodes"], "build_plate.nodes")};
  }
  if (j.contains("preserve"))
    s.preserve = parse_regions(j["preserve"], "preserve");
  if (j.contains("exclude"))
    s.exclude = parse_regions(j["exclude"], "exclude");
  return s;
}

// ── Problem preparation
// ─────────────────────────────────────────────────────────────
namespace {

std::string name_or(const std::string &label, const std::string &fallback) {
  return label.empty() ? fallback : label;
}

std::vector<int> clean_nodes(const TetMesh &m, const std::vector<int> &in,
                             const std::string &what) {
  if (in.empty())
    fail("invalid_bc", what + " selects no nodes");
  std::vector<int> out = in;
  std::sort(out.begin(), out.end());
  out.erase(std::unique(out.begin(), out.end()), out.end());
  for (int n : out) {
    if (n < 0 || n >= m.node_count())
      fail("invalid_bc", what + " node " + std::to_string(n) + " outside [0," +
                             std::to_string(m.node_count()) + ")");
    if (!m.node_referenced[n])
      fail("invalid_bc",
           what + " node " + std::to_string(n) + " is not used by any element");
  }
  return out;
}

SurfacePatch make_patch(const TetMesh &m, const std::string &label,
                        const std::vector<int> &nodes) {
  SurfacePatch p;
  p.label = label;
  p.nodes = clean_nodes(m, nodes, label);
  p.faces = surface_faces_with_nodes(m, p.nodes);
  if (p.faces.empty())
    fail("invalid_bc", label + " selects no exterior triangle (all three "
                               "corners must be selected)");
  for (int f : p.faces)
    p.area_m2 += m.surface_area_m2[f];
  return p;
}

// Number of rigid-body modes (of 6) not suppressed by the fixed DOFs.
int free_rigid_modes(const TetMesh &m, const Dirichlet &fixed) {
  const Eigen::Vector3d center = 0.5 * (m.bbox_min_m + m.bbox_max_m);
  const double L = std::max((m.bbox_max_m - m.bbox_min_m).norm(), 1e-300);
  Eigen::Matrix<double, 6, 6> G = Eigen::Matrix<double, 6, 6>::Zero();
  for (int i = 0; i < fixed.size(); ++i) {
    const int node = fixed.dofs[i] / 3, c = fixed.dofs[i] % 3;
    if (!m.node_referenced[node])
      continue;
    const Eigen::Vector3d d = (m.nodes_m.col(node) - center) / L;
    Eigen::Matrix<double, 6, 1> r = Eigen::Matrix<double, 6, 1>::Zero();
    r[c] = 1.0;
    for (int k = 0; k < 3; ++k)
      r[3 + k] = Eigen::Vector3d::Unit(k).cross(d)[c];
    G += r * r.transpose();
  }
  Eigen::SelfAdjointEigenSolver<Eigen::Matrix<double, 6, 6>> eig(G);
  const double top = std::max(eig.eigenvalues().maxCoeff(), 0.0);
  int nullity = 0;
  for (int k = 0; k < 6; ++k)
    nullity += eig.eigenvalues()[k] <= 1e-9 * std::max(top, 1e-300) ? 1 : 0;
  return top > 0 ? nullity : 6;
}

void add_fixture_entries(const StructuralFixture &f,
                         std::vector<std::pair<int, double>> &entries) {
  for (int n : f.nodes)
    for (int c = 0; c < 3; ++c)
      if (f.dofs[c])
        entries.push_back({3 * n + c, 0.0});
}

StructuralFixture resolve_fixture(const TetMesh &m, const FixtureSpec &spec,
                                  const std::string &fallback) {
  StructuralFixture f;
  f.label = name_or(spec.label, fallback);
  if (!spec.dofs[0] && !spec.dofs[1] && !spec.dofs[2])
    fail("invalid_bc", f.label + " fixes no DOF");
  f.nodes = clean_nodes(m, spec.nodes, f.label);
  f.dofs = spec.dofs;
  return f;
}

} // namespace

std::shared_ptr<const Problem>
prepare_problem(std::shared_ptr<const TetMesh> mesh_ptr,
                const ProblemSpec &spec) {
  if (!mesh_ptr)
    fail("invalid_mesh", "no mesh");
  const TetMesh &m = *mesh_ptr;
  const int N = m.node_count(), E = m.element_count();
  if (m.quality.components != 1)
    fail("disconnected_domain", "design domain has " +
                                    std::to_string(m.quality.components) +
                                    " face-disconnected components; "
                                    "multi-body/contact is not supported");
  auto P = std::make_shared<Problem>();
  Problem &p = *P;
  p.mesh = mesh_ptr;
  p.settings = spec.settings;
  if (spec.build_plate) {
    const NodeSet &selection = *spec.build_plate;
    const std::string label = name_or(selection.label, "Build plate");
    auto plate_fail = [&](const char *code, const std::string &message) {
      fail(code, label + ": " + message);
    };
    for (int n : selection.nodes)
      if (n < 0 || n >= N)
        plate_fail("build_plate_empty", "selection has no valid boundary face");
    const auto faces = surface_faces_with_nodes(m, selection.nodes);
    if (faces.empty())
      plate_fail("build_plate_empty", "no boundary triangle matched");
    BuildPlate plate;
    plate.triangles = static_cast<int>(faces.size());
    Eigen::Vector3d center = Eigen::Vector3d::Zero();
    for (int f : faces) {
      const double area = m.surface_area_m2[f];
      plate.area_m2 += area;
      plate.normal += area * m.surface_normal.col(f);
      for (int k = 0; k < 3; ++k)
        center += (area / 3.0) * m.nodes_m.col(m.surface_nodes(k, f));
    }
    if (!(plate.normal.norm() > 0))
      plate_fail("build_plate_not_planar", "selected faces are not planar");
    plate.normal.normalize();
    constexpr double cos_two_degrees = 0.9993908270190958;
    for (int f : faces)
      if (m.surface_normal.col(f).dot(plate.normal) < cos_two_degrees)
        plate_fail("build_plate_not_planar",
                   "selected face normals differ by more than 2 degrees");
    const Eigen::Vector3d direction = -plate.normal;
    if (p.settings.build_direction &&
        p.settings.build_direction->dot(direction) < cos_two_degrees)
      plate_fail("build_plate_direction_conflict",
                 "explicit build direction differs from the bed face by more "
                 "than 2 degrees");
    const double height = (center / plate.area_m2).dot(direction);
    double lowest = height;
    for (int n = 0; n < N; ++n)
      if (m.node_referenced[n])
        lowest = std::min(lowest, m.nodes_m.col(n).dot(direction));
    const double tolerance =
        std::max(1e-3 * m.quality.median_element_size_m,
                 1e-6 * (m.bbox_max_m - m.bbox_min_m).norm());
    if (height - lowest > tolerance)
      plate_fail("build_plate_not_lowest",
                 "the part extends " + fmt(units::m_to_mm(height - lowest)) +
                     " mm below the chosen bed face; pick the face that rests "
                     "on the bed");
    p.build_plate = plate;
    p.settings.build_direction = direction;
  }
  const Settings &s = p.settings;
  const PhysicsKind phys = s.physics;
  p.material = convert_material(spec.material);
  require_material(p.material, phys);
  p.interpolation = interpolation_for(s);

  if (s.stress_limit_mpa)
    p.limits.stress_pa = units::mpa_to_pa(*s.stress_limit_mpa);
  if (s.max_displacement_mm)
    p.limits.displacement_m = units::mm_to_m(*s.max_displacement_mm);
  if (s.max_temperature_c)
    p.limits.temperature_k = units::celsius_to_kelvin(*s.max_temperature_c);
  p.limits.frequency_hz = s.min_frequency_hz;
  p.limits.buckling_factor = s.min_buckling_factor;
  p.limits.pressure_drop_pa = s.max_pressure_drop_pa;
  p.preserve_thickness_m = units::mm_to_m(s.preserve_thickness_mm);
  p.exclude_thickness_m = units::mm_to_m(s.exclude_thickness_mm);
  if (s.min_member_mm)
    p.min_member_m = units::mm_to_m(*s.min_member_mm);

  const bool has_structural_input =
      !spec.fixtures.empty() || !spec.load_cases.empty();
  if (has_structural_input && !uses_structural_boundary(phys))
    fail("section_not_applicable",
         std::string("fixtures/load_cases do not apply to physics ") +
             to_string(phys));
  if (spec.thermal && !uses_thermal_boundary(phys))
    fail("section_not_applicable",
         std::string("thermal does not apply to physics ") + to_string(phys));
  if (!spec.thermal && uses_thermal_boundary(phys))
    fail("section_missing", std::string("physics ") + to_string(phys) +
                                " needs a thermal section");
  if (spec.flow && !uses_flow_boundary(phys))
    fail("section_not_applicable",
         std::string("flow does not apply to physics ") + to_string(phys));
  if (!spec.flow && uses_flow_boundary(phys))
    fail("section_missing",
         std::string("physics ") + to_string(phys) + " needs a flow section");

  p.node_ownership.assign(N, 0);
  for (int n = 0; n < N; ++n) {
    if (m.node_exterior[n])
      p.node_ownership[n] |= kNodeExterior;
    if (!m.node_referenced[n])
      p.node_ownership[n] |= kNodeUnreferenced;
  }
  json &ev = p.evidence;
  ev["units"] = "SI (m, Pa, N, W, K)";
  ev["nodes"] = N;
  ev["elements"] = E;
  ev["surface_triangles"] = m.surface_count();
  ev["reoriented_elements"] = m.quality.reoriented_elements;
  ev["unreferenced_nodes_pinned"] = m.quality.unreferenced_nodes;

  // Elements forced solid by boundary patches (structural + conduction
  // physics).
  struct SolidSet {
    std::string label;
    std::vector<int> nodes;
    std::uint8_t reason;
  };
  std::vector<SolidSet> bc_solid_sets;
  const bool bc_thermal_solid =
      phys == PhysicsKind::Thermal || phys == PhysicsKind::Thermomechanical;

  // ── Structural
  if (uses_structural_boundary(phys)) {
    StructuralBoundary sb;
    std::vector<std::pair<int, double>> shared;
    for (int n : m.unreferenced_nodes)
      for (int c = 0; c < 3; ++c)
        shared.push_back({3 * n + c, 0.0});
    for (std::size_t i = 0; i < spec.fixtures.size(); ++i) {
      sb.fixtures.push_back(resolve_fixture(
          m, spec.fixtures[i], "fixture " + std::to_string(i + 1)));
      add_fixture_entries(sb.fixtures.back(), shared);
      bc_solid_sets.push_back({sb.fixtures.back().label,
                               sb.fixtures.back().nodes,
                               kReasonStructuralBoundary});
      for (int n : sb.fixtures.back().nodes)
        p.node_ownership[n] |= kNodeStructuralFixed;
    }
    sb.shared_fixed = make_dirichlet(3 * N, shared);
    if (phys == PhysicsKind::Modal) {
      if (!spec.load_cases.empty())
        fail("section_not_applicable",
             "modal topology uses fixtures only; remove load_cases");
      if (const int k = free_rigid_modes(m, sb.shared_fixed))
        fail("rigid_body_motion",
             std::to_string(k) +
                 " rigid-body mode(s) are not restrained by the fixtures");
    } else {
      if (spec.load_cases.empty())
        fail("section_missing", std::string("physics ") + to_string(phys) +
                                    " needs at least one load case");
      std::set<std::string> names;
      for (std::size_t i = 0; i < spec.load_cases.size(); ++i) {
        const LoadCaseSpec &in = spec.load_cases[i];
        StructuralCase c;
        c.name = name_or(in.name, "case_" + std::to_string(i + 1));
        if (!names.insert(c.name).second)
          fail("invalid_bc", "duplicate load case name " + c.name);
        if (!(std::isfinite(in.weight) && in.weight > 0))
          fail("invalid_bc",
               "load case " + c.name + " weight must be finite and > 0");
        c.weight = in.weight;
        if (static_cast<int>(in.forces_n.size()) != 3 * N)
          fail("invalid_bc",
               "load case " + c.name + " forces_n has " +
                   std::to_string(in.forces_n.size()) +
                   " entries, expected 3*N=" + std::to_string(3 * N));
        c.force_n =
            Eigen::Map<const Eigen::VectorXd>(in.forces_n.data(), 3 * N);
        if (!c.force_n.allFinite())
          fail("invalid_bc", "load case " + c.name + " has non-finite forces");
        std::vector<std::pair<int, double>> entries = shared;
        for (std::size_t k = 0; k < in.fixtures.size(); ++k) {
          c.fixtures.push_back(resolve_fixture(
              m, in.fixtures[k], c.name + " fixture " + std::to_string(k + 1)));
          add_fixture_entries(c.fixtures.back(), entries);
          bc_solid_sets.push_back({c.fixtures.back().label,
                                   c.fixtures.back().nodes,
                                   kReasonStructuralBoundary});
          for (int n : c.fixtures.back().nodes)
            p.node_ownership[n] |= kNodeStructuralFixed;
        }
        c.fixed = make_dirichlet(3 * N, entries);
        std::vector<std::uint8_t> fixed_mask(3 * N, 0);
        for (int k = 0; k < c.fixed.size(); ++k)
          fixed_mask[c.fixed.dofs[k]] = 1;
        for (int n = 0; n < N; ++n) {
          bool loaded = false;
          for (int d = 0; d < 3; ++d) {
            const double f = c.force_n[3 * n + d];
            if (f == 0.0)
              continue;
            loaded = true;
            if (!m.node_referenced[n])
              fail("invalid_bc", "load case " + c.name +
                                     " loads unreferenced node " +
                                     std::to_string(n));
            if (fixed_mask[3 * n + d])
              fail("load_on_fixed_dof",
                   "load case " + c.name + " applies " + fmt(f) +
                       " N to fixed DOF " + std::to_string(d) + " of node " +
                       std::to_string(n) + "; the reaction would absorb it");
            c.resultant_n[d] += f;
          }
          if (loaded) {
            c.load_nodes.push_back(n);
            p.node_ownership[n] |= kNodeStructuralLoad;
          }
        }
        if (c.load_nodes.empty() && phys != PhysicsKind::Thermomechanical)
          fail("zero_load", "load case " + c.name + " applies no force");
        if (!c.load_nodes.empty())
          bc_solid_sets.push_back(
              {c.name + " loads", c.load_nodes, kReasonStructuralBoundary});
        if (const int k = free_rigid_modes(m, c.fixed))
          fail("rigid_body_motion",
               "load case " + c.name + ": " + std::to_string(k) +
                   " rigid-body mode(s) are not restrained by the fixtures");
        sb.cases.push_back(std::move(c));
      }
      for (const auto &c : sb.cases) {
        const Eigen::VectorXi &first = sb.cases.front().fixed.dofs;
        sb.cases_share_constraints = sb.cases_share_constraints &&
                                     c.fixed.dofs.size() == first.size() &&
                                     c.fixed.dofs == first;
      }
    }
    json cases = json::array();
    for (const auto &c : sb.cases)
      cases.push_back(
          {{"name", c.name},
           {"weight", c.weight},
           {"fixed_dofs", c.fixed.size()},
           {"load_nodes", c.load_nodes.size()},
           {"resultant_n",
            {c.resultant_n[0], c.resultant_n[1], c.resultant_n[2]}}});
    ev["structural"] = {
        {"shared_fixed_dofs", sb.shared_fixed.size()},
        {"cases", cases},
        {"cases_share_constraints", sb.cases_share_constraints}};
    p.structural = std::move(sb);
  }

  // ── Thermal (Dirichlet assembled after flow so CHT inlet temperatures merge
  // in)
  std::map<int, std::pair<double, std::string>>
      thermal_fixed; // node -> (K, owner)
  auto fix_temperature = [&](int n, double kelvin, const std::string &owner) {
    auto [it, inserted] =
        thermal_fixed.emplace(n, std::make_pair(kelvin, owner));
    if (!inserted && std::abs(it->second.first - kelvin) > 1e-9)
      fail("thermal_fixture_conflict",
           "node " + std::to_string(n) + " fixed at " +
               fmt(units::kelvin_to_celsius(it->second.first)) + " C by " +
               it->second.second + " and " +
               fmt(units::kelvin_to_celsius(kelvin)) + " C by " + owner);
  };
  if (uses_thermal_boundary(phys)) {
    const ThermalSpec &ts = *spec.thermal;
    ThermalBoundary tb;
    if (ts.reference_temperature_c) {
      if (!(*ts.reference_temperature_c > -units::kKelvinAtZeroCelsius))
        fail("invalid_bc",
             "thermal.reference_temperature_c must be above absolute zero");
      tb.reference_temperature_k =
          units::celsius_to_kelvin(*ts.reference_temperature_c);
      tb.reference_given = true;
    } else if (phys == PhysicsKind::Thermomechanical) {
      fail("section_missing",
           "thermomechanical needs thermal.reference_temperature_c "
           "(strain-free temperature)");
    }
    for (std::size_t i = 0; i < ts.fixtures.size(); ++i) {
      ThermalFixture f;
      f.label = name_or(ts.fixtures[i].label,
                        "thermal fixture " + std::to_string(i + 1));
      if (!(std::isfinite(ts.fixtures[i].temperature_c) &&
            ts.fixtures[i].temperature_c > -units::kKelvinAtZeroCelsius))
        fail("invalid_bc",
             f.label + " temperature must be above absolute zero");
      f.nodes = clean_nodes(m, ts.fixtures[i].nodes, f.label);
      f.temperature_k = units::celsius_to_kelvin(ts.fixtures[i].temperature_c);
      for (int n : f.nodes) {
        fix_temperature(n, f.temperature_k, f.label);
        p.node_ownership[n] |= kNodeThermalFixed;
      }
      if (bc_thermal_solid)
        bc_solid_sets.push_back({f.label, f.nodes, kReasonThermalBoundary});
      tb.fixtures.push_back(std::move(f));
    }
    for (std::size_t i = 0; i < ts.loads.size(); ++i) {
      const ThermalLoadSpec &l = ts.loads[i];
      const std::string label =
          name_or(l.label, "thermal load " + std::to_string(i + 1));
      SurfacePatch patch = make_patch(m, label, l.nodes);
      if (bc_thermal_solid)
        bc_solid_sets.push_back({label, patch.nodes, kReasonThermalBoundary});
      if (l.kind == ThermalLoadKind::Convection) {
        if (!(std::isfinite(l.h_w_m2k) && l.h_w_m2k > 0))
          fail("invalid_bc", label + " h_w_m2k must be > 0");
        if (!(std::isfinite(l.ambient_c) &&
              l.ambient_c > -units::kKelvinAtZeroCelsius))
          fail("invalid_bc", label + " ambient_c must be above absolute zero");
        for (int n : patch.nodes)
          p.node_ownership[n] |= kNodeThermalConvection;
        tb.convections.push_back({std::move(patch), l.h_w_m2k,
                                  units::celsius_to_kelvin(l.ambient_c)});
      } else {
        ThermalFlux fx;
        fx.source = l.kind;
        fx.input_value =
            l.kind == ThermalLoadKind::Flux ? l.magnitude_w_m2 : l.power_w;
        if (!(std::isfinite(fx.input_value) && fx.input_value != 0.0))
          fail("invalid_bc",
               label +
                   (l.kind == ThermalLoadKind::Flux ? " magnitude_w_m2"
                                                    : " power_w") +
                   " must be finite and non-zero");
        fx.flux_w_m2 = l.kind == ThermalLoadKind::Flux
                           ? l.magnitude_w_m2
                           : l.power_w / patch.area_m2;
        tb.total_heat_input_w += fx.flux_w_m2 * patch.area_m2;
        for (int n : patch.nodes)
          p.node_ownership[n] |= kNodeThermalFlux;
        fx.patch = std::move(patch);
        tb.fluxes.push_back(std::move(fx));
      }
    }
    p.thermal = std::move(tb);
  }

  // ── Flow
  if (uses_flow_boundary(phys)) {
    const FlowSpec &fs = *spec.flow;
    FlowBoundary fb;
    if (!(std::isfinite(fs.viscosity_pa_s) && fs.viscosity_pa_s > 0))
      fail("invalid_bc", "flow.viscosity_pa_s must be > 0");
    if (!(std::isfinite(fs.density_kg_m3) && fs.density_kg_m3 > 0))
      fail("invalid_bc", "flow.density_kg_m3 must be > 0");
    if (!(std::isfinite(fs.brinkman_max) && fs.brinkman_max > 0))
      fail("invalid_bc", "flow.brinkman_max must be > 0");
    if (fs.reference_length_mm && !(std::isfinite(*fs.reference_length_mm) &&
                                    *fs.reference_length_mm > 0))
      fail("invalid_bc", "flow.reference_length_mm must be > 0");
    fb.viscosity_pa_s = fs.viscosity_pa_s;
    fb.density_kg_m3 = fs.density_kg_m3;
    fb.brinkman_max = fs.brinkman_max;
    if (phys == PhysicsKind::Cht) {
      if (!fs.fluid_k_w_mk || !(*fs.fluid_k_w_mk > 0))
        fail("section_missing", "cht needs flow.fluid_k_w_mk > 0");
      if (!fs.fluid_cp_j_kgk || !(*fs.fluid_cp_j_kgk > 0))
        fail("section_missing", "cht needs flow.fluid_cp_j_kgk > 0");
      fb.fluid_conductivity_w_mk = fs.fluid_k_w_mk;
      fb.fluid_specific_heat_j_kgk = fs.fluid_cp_j_kgk;
    } else if (fs.fluid_k_w_mk || fs.fluid_cp_j_kgk) {
      fail("key_not_applicable",
           "flow.fluid_k_w_mk/fluid_cp_j_kgk apply only to cht");
    }
    if (fs.inlets.empty())
      fail("section_missing", "flow needs at least one inlet");
    if (fs.outlets.empty())
      fail("section_missing", "flow needs at least one outlet");

    std::vector<int> face_owner(m.surface_count(), -1);
    std::vector<std::string> owners;
    auto claim = [&](const SurfacePatch &patch) {
      const int id = static_cast<int>(owners.size());
      owners.push_back(patch.label);
      for (int f : patch.faces) {
        if (face_owner[f] >= 0)
          fail("port_overlap", patch.label + " and " + owners[face_owner[f]] +
                                   " share exterior triangle " +
                                   std::to_string(f));
        face_owner[f] = id;
      }
    };
    double inlet_area = 0.0, inlet_flow = 0.0;
    for (std::size_t i = 0; i < fs.inlets.size(); ++i) {
      const FlowInletSpec &in = fs.inlets[i];
      FlowInlet fi;
      fi.patch = make_patch(
          m, name_or(in.label, "inlet " + std::to_string(i + 1)), in.nodes);
      claim(fi.patch);
      fi.velocity_m_s = in.velocity_m_s;
      for (int f : fi.patch.faces)
        fi.nominal_flow_m3_s -=
            in.velocity_m_s.dot(m.surface_normal.col(f)) * m.surface_area_m2[f];
      if (!(fi.nominal_flow_m3_s > 0))
        fail("inlet_velocity_outward",
             fi.patch.label +
                 " velocity does not point into the domain (net inflow " +
                 fmt(fi.nominal_flow_m3_s) + " m^3/s)");
      if (in.temperature_c) {
        if (phys != PhysicsKind::Cht)
          fail("key_not_applicable",
               fi.patch.label + " temperature_c applies only to cht");
        if (!(*in.temperature_c > -units::kKelvinAtZeroCelsius))
          fail("invalid_bc",
               fi.patch.label + " temperature_c below absolute zero");
        fi.temperature_k = units::celsius_to_kelvin(*in.temperature_c);
      } else if (phys == PhysicsKind::Cht) {
        fail("section_missing",
             fi.patch.label + " needs temperature_c for cht");
      }
      inlet_area += fi.patch.area_m2;
      inlet_flow += fi.nominal_flow_m3_s;
      for (int n : fi.patch.nodes)
        p.node_ownership[n] |= kNodeFlowInlet;
      fb.inlets.push_back(std::move(fi));
    }
    for (std::size_t i = 0; i < fs.outlets.size(); ++i) {
      FlowOutlet fo;
      fo.patch = make_patch(
          m, name_or(fs.outlets[i].label, "outlet " + std::to_string(i + 1)),
          fs.outlets[i].nodes);
      claim(fo.patch);
      if (!std::isfinite(fs.outlets[i].pressure_pa))
        fail("invalid_bc", fo.patch.label + " pressure_pa must be finite");
      fo.pressure_pa = fs.outlets[i].pressure_pa;
      for (int n : fo.patch.nodes)
        p.node_ownership[n] |= kNodeFlowOutlet;
      fb.outlets.push_back(std::move(fo));
    }
    for (std::size_t i = 0; i < fs.walls.size(); ++i) {
      SurfacePatch w = make_patch(
          m, name_or(fs.walls[i].label, "wall " + std::to_string(i + 1)),
          fs.walls[i].nodes);
      for (int f : w.faces)
        if (face_owner[f] >= 0)
          fail("port_overlap",
               w.label + " overlaps port " + owners[face_owner[f]] +
                   " at exterior triangle " + std::to_string(f));
      fb.walls.push_back(std::move(w));
    }
    std::vector<std::uint8_t> wall_node(N, 0);
    for (int f = 0; f < m.surface_count(); ++f)
      if (face_owner[f] < 0) {
        fb.wall_faces.push_back(f);
        for (int k = 0; k < 3; ++k)
          wall_node[m.surface_nodes(k, f)] = 1;
      }
    std::vector<std::pair<int, double>> vel;
    std::map<int, std::pair<Eigen::Vector3d, std::string>> inlet_node;
    std::vector<std::uint8_t> outlet_node(N, 0);
    for (const auto &o : fb.outlets)
      for (int n : o.patch.nodes)
        outlet_node[n] = 1;
    for (const auto &in : fb.inlets)
      for (int n : in.patch.nodes) {
        if (wall_node[n])
          continue;
        if (outlet_node[n])
          fail("port_overlap", in.patch.label + " and an outlet share node " +
                                   std::to_string(n) +
                                   " with no wall between them");
        auto [it, inserted] = inlet_node.emplace(
            n, std::make_pair(in.velocity_m_s, in.patch.label));
        if (!inserted && (it->second.first - in.velocity_m_s).norm() >
                             1e-12 * std::max(1.0, in.velocity_m_s.norm()))
          fail("port_overlap", in.patch.label + " and " + it->second.second +
                                   " prescribe different velocities at node " +
                                   std::to_string(n));
      }
    std::set<int> rim;
    for (const auto &in : fb.inlets)
      for (int n : in.patch.nodes)
        if (wall_node[n])
          rim.insert(n);
    for (const auto &o : fb.outlets)
      for (int n : o.patch.nodes)
        if (wall_node[n])
          rim.insert(n);
    fb.rim_nodes_no_slip = static_cast<int>(rim.size());
    for (int n = 0; n < N; ++n) {
      if (wall_node[n] || !m.node_referenced[n]) {
        for (int c = 0; c < 3; ++c)
          vel.push_back({3 * n + c, 0.0});
        if (wall_node[n])
          p.node_ownership[n] |= kNodeFlowWall;
      }
    }
    for (const auto &[n, v] : inlet_node)
      for (int c = 0; c < 3; ++c)
        vel.push_back({3 * n + c, v.first[c]});
    fb.velocity = make_dirichlet(3 * N, vel);
    fb.reference_length_m =
        fs.reference_length_mm
            ? units::mm_to_m(*fs.reference_length_mm)
            : std::sqrt(4.0 * inlet_area / 3.14159265358979323846);
    fb.alpha_max = fb.brinkman_max * fb.viscosity_pa_s /
                   (fb.reference_length_m * fb.reference_length_m);
    fb.mean_inlet_speed_m_s = inlet_flow / inlet_area;
    fb.reynolds = fb.density_kg_m3 * fb.mean_inlet_speed_m_s *
                  fb.reference_length_m / fb.viscosity_pa_s;
    if (phys == PhysicsKind::Cht)
      for (const auto &in : fb.inlets)
        for (int n : in.patch.nodes)
          fix_temperature(n, *in.temperature_k, in.patch.label);
    ev["flow"] = {
        {"inlet_area_m2", inlet_area},
        {"nominal_inflow_m3_s", inlet_flow},
        {"mean_inlet_speed_m_s", fb.mean_inlet_speed_m_s},
        {"reference_length_m", fb.reference_length_m},
        {"reference_length_source",
         fs.reference_length_mm ? "explicit" : "equivalent_inlet_diameter"},
        {"reynolds", fb.reynolds},
        {"alpha_max_pa_s_m2", fb.alpha_max},
        {"wall_triangles", fb.wall_faces.size()},
        {"rim_nodes_no_slip", fb.rim_nodes_no_slip},
        {"velocity_dirichlet_dofs", fb.velocity.size()}};
    p.flow = std::move(fb);
  }

  // ── Thermal Dirichlet, well-posedness and drive
  if (p.thermal) {
    ThermalBoundary &tb = *p.thermal;
    std::vector<std::pair<int, double>> entries;
    for (const auto &[n, v] : thermal_fixed)
      entries.push_back({n, v.first});
    for (int n : m.unreferenced_nodes)
      entries.push_back({n, tb.reference_temperature_k});
    tb.fixed = make_dirichlet(N, entries);
    if (thermal_fixed.empty() && tb.convections.empty())
      fail("thermal_unconstrained", "the temperature field needs a fixed "
                                    "temperature or a convection boundary");
    auto all_fixed = [&](const SurfacePatch &patch) {
      for (int n : patch.nodes)
        if (!thermal_fixed.count(n))
          return false;
      return true;
    };
    for (const auto &f : tb.fluxes)
      if (all_fixed(f.patch))
        fail("thermal_load_on_fixed_temperature",
             f.patch.label + " lies entirely on fixed-temperature nodes");
    for (const auto &c : tb.convections)
      if (all_fixed(c.patch))
        fail("thermal_load_on_fixed_temperature",
             c.patch.label + " lies entirely on fixed-temperature nodes");
    if ((phys == PhysicsKind::Thermal || phys == PhysicsKind::Cht) &&
        tb.fluxes.empty())
      fail("zero_load", std::string("physics ") + to_string(phys) +
                            " needs a flux or power heat input");
    bool thermal_drive = !tb.fluxes.empty();
    for (const auto &[n, v] : thermal_fixed)
      thermal_drive = thermal_drive ||
                      std::abs(v.first - tb.reference_temperature_k) > 1e-9;
    for (const auto &c : tb.convections)
      thermal_drive = thermal_drive ||
                      std::abs(c.ambient_k - tb.reference_temperature_k) > 1e-9;
    if (phys == PhysicsKind::Thermomechanical) {
      if (!thermal_drive && p.structural) {
        for (const auto &c : p.structural->cases)
          if (c.load_nodes.empty())
            fail("zero_load", "load case " + c.name +
                                  " has no force and the thermal field equals "
                                  "the reference temperature");
      }
    }
    ev["thermal"] = {{"reference_temperature_k", tb.reference_temperature_k},
                     {"reference_given", tb.reference_given},
                     {"fixed_nodes", tb.fixed.size()},
                     {"fluxes", tb.fluxes.size()},
                     {"convections", tb.convections.size()},
                     {"total_heat_input_w", tb.total_heat_input_w},
                     {"thermal_drive", thermal_drive}};
  }

  // ── Passive roles
  ElementRoles &roles = p.roles;
  roles.role.assign(E, ElementRole::Design);
  roles.reason.assign(E, 0);
  std::vector<std::string> owner(E);
  for (std::size_t i = 0; i < spec.preserve.size(); ++i) {
    RegionSpec r = spec.preserve[i];
    r.label = name_or(r.label, "preserve " + std::to_string(i + 1));
    for (int n : r.nodes)
      (void)clean_nodes(m, {n}, r.label);
    for (int e : region_elements(m, r, p.preserve_thickness_m)) {
      roles.role[e] = ElementRole::Solid;
      roles.reason[e] |= kReasonPreserve;
      owner[e] = r.label;
    }
  }
  for (std::size_t i = 0; i < spec.exclude.size(); ++i) {
    RegionSpec r = spec.exclude[i];
    r.label = name_or(r.label, "exclude " + std::to_string(i + 1));
    for (int n : r.nodes)
      (void)clean_nodes(m, {n}, r.label);
    for (int e : region_elements(m, r, p.exclude_thickness_m)) {
      if (roles.role[e] == ElementRole::Solid)
        fail("passive_conflict", "element " + std::to_string(e) +
                                     " is in both " + owner[e] + " and " +
                                     r.label);
      roles.role[e] = ElementRole::Void;
      roles.reason[e] |= kReasonExclude;
      owner[e] = r.label;
    }
  }
  for (const SolidSet &set : bc_solid_sets) {
    const std::string &label = set.label;
    RegionSpec r;
    r.label = label;
    r.nodes = set.nodes;
    for (int e : region_elements(m, r, p.preserve_thickness_m)) {
      if (roles.role[e] == ElementRole::Void)
        fail("boundary_in_keep_out",
             "boundary " + label + " needs solid element " + std::to_string(e) +
                 " that " + owner[e] + " excludes");
      roles.role[e] = ElementRole::Solid;
      roles.reason[e] |= set.reason;
      if (owner[e].empty())
        owner[e] = label;
    }
  }
  if (p.flow) {
    auto port = [&](const SurfacePatch &patch) {
      for (int e : elements_touching_nodes(m, patch.nodes)) {
        if (roles.role[e] == ElementRole::Solid)
          fail("port_in_preserve",
               "flow port " + patch.label + " needs fluid element " +
                   std::to_string(e) + " that " + owner[e] + " keeps solid");
        roles.role[e] = ElementRole::Void;
        roles.reason[e] |= kReasonFlowPort;
        if (owner[e].empty())
          owner[e] = patch.label;
      }
    };
    for (const auto &in : p.flow->inlets)
      port(in.patch);
    for (const auto &out : p.flow->outlets)
      port(out.patch);
  }
  roles.recount();

  // ── Tying
  TyingSpec tie;
  tie.mirror = s.symmetry_axes;
  tie.mirror_origin_m =
      s.symmetry_origin_mm
          ? Eigen::Vector3d(*s.symmetry_origin_mm * units::kMeterPerMm)
          : Eigen::Vector3d(0.5 * (m.bbox_min_m + m.bbox_max_m));
  tie.extrusion_axis = s.extrusion_axis;
  p.design = build_design_map(m, roles, tie);
  if (p.design.variable_count == 0 || roles.design_count == 0)
    fail("no_design_elements",
         "every element is fixed solid or void; nothing to optimize");

  double passive_phase = 0.0;
  const DesignPhase phase = design_phase(phys);
  for (int e = 0; e < E; ++e)
    if (roles.role[e] ==
        (phase == DesignPhase::Solid ? ElementRole::Solid : ElementRole::Void))
      passive_phase += m.volume_m3[e];
  const double passive_fraction = passive_phase / m.total_volume_m3;
  if (passive_fraction > s.volume_fraction + 1e-12)
    fail("volume_fraction_infeasible",
         std::string("fixed ") + to_string(phase) + " regions already occupy " +
             fmt(passive_fraction) + " of the volume, above volume_fraction " +
             fmt(s.volume_fraction));

  // ── Filter
  if (s.filter_radius_mm) {
    p.filter_radius_m = units::mm_to_m(*s.filter_radius_mm);
    p.filter_radius_source = "explicit";
  } else if (s.min_member_mm) {
    const double intended = 0.5 * units::mm_to_m(*s.min_member_mm);
    const double floor = 2.0 * m.quality.median_element_size_m;
    p.filter_radius_m = std::max(intended, floor);
    p.filter_radius_source =
        floor > intended ? "auto_2x_median_element_size" : "min_member_mm";
  } else {
    p.filter_radius_m = 2.0 * m.quality.median_element_size_m;
    p.filter_radius_source = "auto_2x_median_element_size";
  }
  p.filter = build_density_filter(m, p.filter_radius_m);

  ev["design_phase"] = to_string(phase);
  ev["passive_phase_fraction"] = passive_fraction;
  ev["roles"] = {{"design", roles.design_count},
                 {"solid", roles.solid_count},
                 {"void", roles.void_count}};
  ev["design_variables"] = p.design.variable_count;
  ev["tied_groups"] = p.design.tied_groups;
  ev["max_tie_distance_m"] = p.design.max_tie_distance_m;
  ev["filter"] = {
      {"radius_m", p.filter_radius_m},
      {"source", p.filter_radius_source},
      {"mean_neighbors", static_cast<double>(p.filter.weights.nonZeros()) / E}};
  if (!s.filter_radius_mm && s.min_member_mm) {
    ev["filter"]["min_member_filter_radius_mm"] = 0.5 * *s.min_member_mm;
    ev["filter"]["mesh_floor_radius_mm"] =
        units::m_to_mm(2.0 * m.quality.median_element_size_m);
  }
  ev["interpolation"] = {
      {"kind",
       p.interpolation.kind == Interpolation::Kind::Simp ? "simp" : "ramp"},
      {"exponent", p.interpolation.exponent},
      {"floor", p.interpolation.floor}};
  ev["mesh_quality"] = {
      {"min_shape_quality", m.quality.min_shape_quality},
      {"mean_shape_quality", m.quality.mean_shape_quality},
      {"median_element_size_m", m.quality.median_element_size_m},
      {"min_edge_m", m.quality.min_edge_m},
      {"max_edge_m", m.quality.max_edge_m}};
  return P;
}

// ── Chain helpers
// ───────────────────────────────────────────────────────────────────
void design_to_density(const Problem &p, const Eigen::VectorXd &x,
                       Eigen::VectorXd &rho) {
  Eigen::VectorXd raw;
  p.design.expand(x, raw);
  p.filter.apply(raw, rho);
  p.design.project_ties(rho);
  for (Eigen::Index e = 0; e < rho.size(); ++e)
    if (p.design.element_variable[e] < 0)
      rho[e] = p.design.fixed_density[e];
}

void density_gradient_to_design(const Problem &p, const Eigen::VectorXd &g_rho,
                                Eigen::VectorXd &g_x) {
  if (g_rho.size() != p.mesh->element_count())
    fail("invalid_density", "density gradient size mismatch");
  Eigen::VectorXd g = g_rho;
  for (Eigen::Index e = 0; e < g.size(); ++e)
    if (p.design.element_variable[e] < 0)
      g[e] = 0.0;
  Eigen::VectorXd raw;
  p.design.project_ties_transpose(g);
  p.filter.apply_transpose(g, raw);
  p.design.reduce_gradient(raw, g_x);
}

void check_density(const Problem &p, const Eigen::VectorXd &rho) {
  if (rho.size() != p.mesh->element_count())
    fail("invalid_density", "density has " + std::to_string(rho.size()) +
                                " entries, expected " +
                                std::to_string(p.mesh->element_count()));
  for (Eigen::Index e = 0; e < rho.size(); ++e)
    if (!(std::isfinite(rho[e]) && rho[e] >= 0.0 && rho[e] <= 1.0))
      fail("invalid_density", "element " + std::to_string(e) + " density " +
                                  fmt(rho[e]) + " outside [0,1]");
}

double phase_fraction(const Problem &p, const Eigen::VectorXd &rho) {
  check_density(p, rho);
  const TetMesh &m = *p.mesh;
  const double solid = m.volume_m3.dot(rho) / m.total_volume_m3;
  return design_phase(p.settings.physics) == DesignPhase::Solid ? solid
                                                                : 1.0 - solid;
}

Constraint volume_constraint(const Problem &p, const Eigen::VectorXd &rho,
                             bool derivatives) {
  const DesignPhase phase = design_phase(p.settings.physics);
  Constraint c;
  c.name = std::string(to_string(phase)) + "_volume_fraction";
  c.unit = "1";
  c.aggregation = "exact";
  c.response = phase_fraction(p, rho);
  c.limit = p.settings.volume_fraction;
  c.value = c.response / c.limit - 1.0;
  if (derivatives) {
    const double sign = phase == DesignPhase::Solid ? 1.0 : -1.0;
    c.gradient =
        (sign / (p.mesh->total_volume_m3 * c.limit)) * p.mesh->volume_m3;
  }
  return c;
}

} // namespace topology
} // namespace sim
} // namespace chudware
