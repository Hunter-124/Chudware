// sim_topology.cpp — see sim_topology.h.
//
// CHUDWARE_SIM_TOPOLOGY_MODEL_ONLY compiles ONLY the pure settings/results
// model (the first half of this file), so host/tests/test_sim_topology.cpp
// links this TU standalone — no ImGui, renderer, AppState or SimManager. The
// drawing half is excluded in that build; nothing is stubbed.
#include "sim_topology.h"

#include <algorithm>
#include <cerrno>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <limits>
#include <set>
#include <utility>

namespace ui {
namespace topology {

using nlohmann::json;

namespace {

constexpr double kInf = std::numeric_limits<double>::infinity();
const double kNaN = std::numeric_limits<double>::quiet_NaN();

enum class Kind { Real, Integer, Bool, AxisSet, Axis, Vec3 };
enum class Unit { None, Fraction, Length, Stress, PressurePa, TempC, Hz, Deg };
enum class Group {
  Goal,
  Limits,
  Convergence,
  Regularization,
  Method,
  Geometry,
  Additive
};

// The scalar vocabulary of the `topology{}` object (topology contract, "Public
// study schema"). Ranges are the contract's; `lo`/`hi` are inclusive unless the
// matching *Open flag is set.
struct KeySpec {
  const char *key;
  Group group;
  Kind kind;
  Unit unit;
  double lo;
  bool loOpen;
  double hi;
  bool hiOpen;
  const char *label;
  const char *tip;
};

const KeySpec kSpecs[] = {
    {"volume_fraction", Group::Goal, Kind::Real, Unit::Fraction, 0.0, true, 1.0,
     false, "Volume fraction",
     "Upper bound on the share of the design domain occupied by the budgeted "
     "phase: solid for "
     "structural and thermal physics, fluid for flow and conjugate heat "
     "transfer. Preserved "
     "regions count toward it."},

    {"stress_limit_mpa", Group::Limits, Kind::Real, Unit::Stress, 0.0, true,
     kInf, false, "Max von Mises stress",
     "Aggregated stress constraint on the design (g <= 0)."},
    {"max_displacement_mm", Group::Limits, Kind::Real, Unit::Length, 0.0, true,
     kInf, false, "Max displacement",
     "Upper bound on the displacement magnitude."},
    {"max_temperature_c", Group::Limits, Kind::Real, Unit::TempC, -273.15, true,
     kInf, false, "Max temperature",
     "Upper bound on the temperature in the design."},
    {"min_frequency_hz", Group::Limits, Kind::Real, Unit::Hz, 0.0, true, kInf,
     false, "Min fundamental frequency",
     "Lower bound on the first natural frequency."},
    {"min_buckling_factor", Group::Limits, Kind::Real, Unit::None, 0.0, true,
     kInf, false, "Min buckling load factor",
     "Lower bound on the first linear buckling load factor of the applied "
     "loads."},
    {"max_pressure_drop_pa", Group::Limits, Kind::Real, Unit::PressurePa, 0.0,
     true, kInf, false, "Max pressure drop",
     "Upper bound on the inlet-to-outlet pressure drop."},

    {"max_iterations", Group::Convergence, Kind::Integer, Unit::None, 1.0,
     false, kInf, false, "Max iterations",
     "Hard iteration cap. Stopping here is reported as an iteration-limit "
     "outcome, never as "
     "convergence."},
    {"change_tolerance", Group::Convergence, Kind::Real, Unit::None, 0.0, true,
     kInf, false, "Change tolerance",
     "Converged when the largest design change per iteration falls below this "
     "while every "
     "constraint holds."},
    {"move_limit", Group::Convergence, Kind::Real, Unit::Fraction, 0.0, true,
     1.0, false, "Move limit",
     "Largest per-iteration change of any design variable."},

    {"filter_radius_mm", Group::Regularization, Kind::Real, Unit::Length, 0.0,
     false, kInf, false, "Filter radius",
     "Spatial filter radius; sets the smallest feature scale the optimizer "
     "favors. 0 disables "
     "filtering."},
    {"projection_beta", Group::Regularization, Kind::Real, Unit::None, 0.0,
     false, kInf, false, "Projection sharpness (beta)",
     "Heaviside projection steepness. 0 disables projection."},
    {"density_threshold", Group::Regularization, Kind::Real, Unit::Fraction,
     0.0, true, 1.0, true, "Solid threshold",
     "Density at which the result surface is extracted and the thresholded "
     "design is "
     "re-analyzed."},
    {"min_density", Group::Regularization, Kind::Real, Unit::Fraction, 0.0,
     true, 1.0, true, "Minimum density",
     "Lower bound on the design variables; keeps the void phase non-singular."},

    {"penalty", Group::Method, Kind::Real, Unit::None, 1.0, false, kInf, false,
     "SIMP penalty (p)",
     "Power-law interpolation exponent: property = min + rho^p (max - min)."},
    {"ramp_q", Group::Method, Kind::Real, Unit::None, 0.0, false, kInf, false,
     "RAMP parameter (q)", "Rational interpolation: rho / (1 + q (1 - rho))."},
    {"beso_evolution_rate", Group::Method, Kind::Real, Unit::Fraction, 0.0,
     true, 1.0, true, "Evolution rate",
     "Share of the volume the BESO update may remove or re-add per iteration."},
    {"level_set_regularization", Group::Method, Kind::Real, Unit::None, 0.0,
     false, kInf, false, "Interface regularization",
     "Diffusion weight of the level-set update; higher values give a smoother "
     "interface."},

    {"symmetry_axes", Group::Geometry, Kind::AxisSet, Unit::None, 0.0, false,
     0.0, false, "Symmetry",
     "Tie the design variables so the layout is symmetric about the chosen "
     "axes."},
    {"extrusion_axis", Group::Geometry, Kind::Axis, Unit::None, 0.0, false, 0.0,
     false, "Extrusion",
     "Constant cross-section along this axis (prismatic layout)."},
    {"preserve_thickness_mm", Group::Geometry, Kind::Real, Unit::Length, 0.0,
     false, kInf, false, "Preserved skin depth",
     "Depth kept solid beneath the preserved faces."},
    {"exclude_thickness_mm", Group::Geometry, Kind::Real, Unit::Length, 0.0,
     false, kInf, false, "Keep-out depth",
     "Depth kept empty beneath the excluded faces."},

    {"min_member_mm", Group::Additive, Kind::Real, Unit::Length, 0.0, true,
     kInf, false, "Minimum member (target)",
     "Regularization target, not a guarantee. The member size measured on the "
     "result geometry is "
     "reported separately."},
    {"build_direction", Group::Additive, Kind::Vec3, Unit::None, 0.0, false,
     0.0, false, "Build direction",
     "Printer build direction in part coordinates. Normalized on entry."},
    {"overhang_angle_deg", Group::Additive, Kind::Real, Unit::Deg, 0.0, true,
     90.0, true, "Overhang limit",
     "Largest self-supporting overhang, using the same convention as the DFAM "
     "overhang check."},
    {"overhang_penalty", Group::Additive, Kind::Real, Unit::None, 0.0, false,
     kInf, false, "Overhang penalty",
     "Weight of the differentiable overhang heuristic. It discourages "
     "overhangs but does not "
     "guarantee a support-free part. 0 disables it."},
    {"no_enclosed_voids", Group::Additive, Kind::Bool, Unit::None, 0.0, false,
     0.0, false, "No enclosed voids",
     "Penalize cavities with no path to the outside (trapped powder or "
     "resin)."},
};

const KeySpec *specFor(const std::string &key) {
  for (const KeySpec &s : kSpecs)
    if (key == s.key)
      return &s;
  return nullptr;
}

bool isFlowLike(const std::string &physics) {
  return physics == "flow" || physics == "cht";
}

// The phase topology.volume_fraction budgets: fluid for flow/CHT (a solid cap
// would let a dissipation objective degenerate to all-fluid), solid otherwise.
// rho = 1 is solid everywhere.
std::string volumePhaseFor(const std::string &physics) {
  return isFlowLike(physics) ? "fluid" : "solid";
}

std::string volumeLabel(const std::string &phase) {
  if (phase == "fluid")
    return "Fluid volume fraction";
  if (phase == "solid")
    return "Solid volume fraction";
  return "Volume fraction";
}

// `phase` overrides the physics-derived volume phase when the solver reports
// one.
std::string keyLabel(const KeySpec &s, const std::string &physics,
                     const std::string &phase = std::string()) {
  const std::string key = s.key;
  if (key == "volume_fraction") {
    return volumeLabel(phase.empty() ? volumePhaseFor(physics) : phase);
  } else if (key == "min_density") {
    if (isFlowLike(physics))
      return "Minimum solid fraction";
  } else if (key == "density_threshold") {
    if (isFlowLike(physics))
      return "Solid / fluid threshold";
  }
  return s.label;
}

std::string str(const json &j, const char *key) {
  if (!j.is_object())
    return {};
  const auto it = j.find(key);
  return (it != j.end() && it->is_string()) ? it->get<std::string>()
                                            : std::string();
}

bool boolTrue(const json &j, const char *key) {
  if (!j.is_object())
    return false;
  const auto it = j.find(key);
  return it != j.end() && it->is_boolean() && it->get<bool>();
}

double num(const json &j, const char *key) {
  if (!j.is_object())
    return kNaN;
  const auto it = j.find(key);
  if (it == j.end() || !it->is_number())
    return kNaN;
  const double v = it->get<double>();
  return std::isfinite(v) ? v : kNaN;
}

const json &member(const json &j, const char *key) {
  static const json kNull;
  if (!j.is_object())
    return kNull;
  const auto it = j.find(key);
  return it != j.end() ? *it : kNull;
}

std::vector<std::string> stringList(const json &j) {
  std::vector<std::string> out;
  if (!j.is_array())
    return out;
  for (const json &e : j)
    if (e.is_string())
      out.push_back(e.get<std::string>());
  return out;
}

bool contains(const std::vector<std::string> &v, const std::string &s) {
  return std::find(v.begin(), v.end(), s) != v.end();
}

std::string fmtG(double v) {
  char b[48];
  std::snprintf(b, sizeof(b), "%.6g", v);
  return b;
}

std::string humanize(const std::string &key) {
  std::string s = key;
  for (char &c : s)
    if (c == '_')
      c = ' ';
  if (!s.empty() && s[0] >= 'a' && s[0] <= 'z')
    s[0] = char(s[0] - 'a' + 'A');
  return s;
}

std::string rangeText(const KeySpec &s) {
  std::string out;
  if (std::isfinite(s.lo))
    out += std::string(s.loOpen ? "> " : ">= ") + fmtG(s.lo);
  if (std::isfinite(s.hi)) {
    if (!out.empty())
      out += " and ";
    out += std::string(s.hiOpen ? "< " : "<= ") + fmtG(s.hi);
  }
  return out;
}

bool inRange(const KeySpec &s, double v) {
  if (!std::isfinite(v))
    return false;
  if (std::isfinite(s.lo) && (s.loOpen ? v <= s.lo : v < s.lo))
    return false;
  if (std::isfinite(s.hi) && (s.hiOpen ? v >= s.hi : v > s.hi))
    return false;
  return true;
}

bool isAxis(const std::string &a) { return a == "x" || a == "y" || a == "z"; }

// Contract-range and type check for one present owned key. Returns an empty
// string when valid.
std::string checkValue(const KeySpec &s, const json &v,
                       const std::string &label) {
  switch (s.kind) {
  case Kind::Real:
    if (!v.is_number() || !std::isfinite(v.get<double>()))
      return label + " must be a number.";
    if (!inRange(s, v.get<double>()))
      return label + " must be " + rangeText(s) + ".";
    return {};
  case Kind::Integer: {
    if (!v.is_number())
      return label + " must be a whole number.";
    const double d = v.get<double>();
    if (!std::isfinite(d) || std::floor(d) != d)
      return label + " must be a whole number.";
    if (!inRange(s, d) || d > double(std::numeric_limits<int>::max()))
      return label + " must be " + rangeText(s) + ".";
    return {};
  }
  case Kind::Bool:
    return v.is_boolean() ? std::string() : label + " must be on or off.";
  case Kind::Axis:
    return (v.is_string() && isAxis(v.get<std::string>()))
               ? std::string()
               : label + " must be the X, Y or Z axis.";
  case Kind::AxisSet: {
    if (!v.is_array())
      return label + " must be a set of X, Y, Z axes.";
    std::set<std::string> seen;
    for (const json &e : v) {
      if (!e.is_string() || !isAxis(e.get<std::string>()))
        return label + " must contain only X, Y or Z.";
      if (!seen.insert(e.get<std::string>()).second)
        return label + " lists an axis twice.";
    }
    return {};
  }
  case Kind::Vec3: {
    if (!v.is_array() || v.size() != 3)
      return label + " must be an X, Y, Z vector.";
    double n2 = 0.0;
    for (const json &e : v) {
      if (!e.is_number() || !std::isfinite(e.get<double>()))
        return label + " must be an X, Y, Z vector.";
      n2 += e.get<double>() * e.get<double>();
    }
    if (!(n2 > 1e-24))
      return label + " cannot be the zero vector.";
    return {};
  }
  }
  return {};
}

const json &runResult(const json &run) { return member(run, "result"); }
const json &runSettings(const json &run) { return member(run, "settings"); }

std::string runPhysics(const json &run) {
  std::string p = str(runResult(run), "physics");
  return p.empty() ? str(runSettings(run), "physics") : p;
}

std::string runObjective(const json &run) {
  std::string o = str(member(runResult(run), "summary"), "objective");
  return o.empty() ? str(runSettings(run), "objective") : o;
}

std::string mfgLabel(const std::string &key) {
  if (const KeySpec *s = specFor(key))
    return s->label;
  return humanize(key);
}

std::string mfgUnits(const std::string &key) {
  if (const KeySpec *s = specFor(key)) {
    if (s->unit == Unit::Length)
      return "mm";
    if (s->unit == Unit::Deg)
      return "deg";
  }
  return {};
}

// The phase a run's volume figures refer to: the solver's report, else the
// contract rule.
std::string runPhase(const json &run) {
  const std::string p = str(member(runResult(run), "summary"), "volume_phase");
  return p.empty() ? volumePhaseFor(runPhysics(run)) : p;
}

} // namespace

// ---- capabilities
// --------------------------------------------------------------------------------

bool Combination::offersObjective(const std::string &objective) const {
  return contains(objectives, objective);
}

bool Combination::offersKey(const std::string &key) const {
  return contains(parameters, key) || contains(limits, key);
}

const CapabilityEntry *Capabilities::method(const std::string &id) const {
  for (const CapabilityEntry &e : methods)
    if (e.id == id)
      return &e;
  return nullptr;
}

const CapabilityEntry *Capabilities::physicsEntry(const std::string &id) const {
  for (const CapabilityEntry &e : physics)
    if (e.id == id)
      return &e;
  return nullptr;
}

const Combination *Capabilities::find(const std::string &m,
                                      const std::string &p) const {
  for (const Combination &c : combinations)
    if (c.method == m && c.physics == p)
      return &c;
  return nullptr;
}

bool Capabilities::supports(const std::string &m, const std::string &p,
                            std::string *reason) const {
  if (state != State::Ready) {
    if (reason)
      *reason = state == State::Loading
                    ? "Solver capabilities are still loading."
                    : "Topology solver unavailable: " + error;
    return false;
  }
  if (!method(m)) {
    if (reason)
      *reason = "The solver does not offer the method '" + m + "'.";
    return false;
  }
  if (!physicsEntry(p)) {
    if (reason)
      *reason = "The solver does not offer the physics '" + p + "'.";
    return false;
  }
  const Combination *c = find(m, p);
  if (!c) {
    if (reason)
      *reason = methodLabel(m, this) + " with " + physicsLabel(p, this) +
                " is not in the solver's capability matrix.";
    return false;
  }
  if (!c->supported) {
    if (reason)
      *reason = c->reason;
    return false;
  }
  return true;
}

Capabilities parseCapabilities(const json &caps) {
  Capabilities out;
  if (!caps.is_object())
    return capabilitiesUnavailable("The solver returned no capability matrix.");
  auto entries = [](const json &arr, std::vector<CapabilityEntry> &dst) {
    if (!arr.is_array())
      return;
    for (const json &e : arr) {
      CapabilityEntry ce;
      ce.id = str(e, "id");
      if (ce.id.empty())
        continue;
      ce.label = str(e, "label");
      ce.formulation = str(e, "formulation");
      ce.validity = str(e, "validity");
      dst.push_back(std::move(ce));
    }
  };
  entries(member(caps, "methods"), out.methods);
  entries(member(caps, "physics"), out.physics);
  if (out.methods.empty() || out.physics.empty())
    return capabilitiesUnavailable(
        "The solver's capability matrix lists no methods or physics.");

  out.state =
      Capabilities::State::Ready; // labels below may consult the parsed entries
  const json &combos = member(caps, "combinations");
  if (combos.is_array()) {
    for (const json &e : combos) {
      Combination c;
      c.method = str(e, "method");
      c.physics = str(e, "physics");
      if (c.method.empty() || c.physics.empty())
        continue;
      c.supported = boolTrue(e, "supported");
      c.reason = str(e, "reason");
      c.formulation = str(e, "formulation");
      c.volumePhase = str(e, "volume_phase");
      if (c.volumePhase != "solid" && c.volumePhase != "fluid")
        c.volumePhase = volumePhaseFor(c.physics);
      c.objectives = stringList(member(e, "objectives"));
      c.limits = stringList(member(e, "limits"));
      c.parameters = stringList(member(e, "parameters"));
      if (member(e, "defaults").is_object())
        c.defaults = member(e, "defaults");
      const std::string pair = methodLabel(c.method, &out) + " with " +
                               physicsLabel(c.physics, &out);
      if (c.supported && c.objectives.empty()) {
        c.supported = false;
        c.reason = "The solver lists no objective for " + pair + ".";
      }
      if (!c.supported && c.reason.empty())
        c.reason = "The solver reports " + pair + " as unsupported.";
      out.combinations.push_back(std::move(c));
    }
  }
  return out;
}

Capabilities capabilitiesUnavailable(const std::string &reason) {
  Capabilities c;
  c.state = Capabilities::State::Unavailable;
  c.error = reason;
  return c;
}

// ---- labels
// ------------------------------------------------------------------------------------------

std::string methodLabel(const std::string &id, const Capabilities *caps) {
  if (id == "simp")
    return "SIMP";
  if (id == "ramp")
    return "RAMP";
  if (id == "beso")
    return "BESO";
  if (id == "level_set")
    return "Level set";
  if (caps)
    if (const CapabilityEntry *e = caps->method(id))
      if (!e->label.empty())
        return e->label;
  return id;
}

std::string physicsLabel(const std::string &id, const Capabilities *caps) {
  if (id == "structural")
    return "Linear structural";
  if (id == "thermal")
    return "Steady conduction";
  if (id == "modal")
    return "Natural frequency";
  if (id == "buckling")
    return "Linear buckling";
  if (id == "flow")
    return "Fluid flow";
  if (id == "thermomechanical")
    return "Thermomechanical";
  if (id == "cht")
    return "Conjugate heat transfer";
  if (caps)
    if (const CapabilityEntry *e = caps->physicsEntry(id))
      if (!e->label.empty())
        return e->label;
  return id;
}

std::string objectiveLabel(const std::string &objective,
                           const std::string &physics) {
  if (objective == "compliance")
    return physics == "thermomechanical"
               ? "Minimize thermo-mechanical compliance"
               : "Minimize compliance (maximize stiffness)";
  if (objective == "mass")
    return "Minimize mass under response limits";
  if (objective == "thermal_compliance")
    return "Minimize thermal compliance";
  if (objective == "frequency")
    return "Maximize fundamental frequency";
  if (objective == "buckling_factor")
    return "Maximize buckling load factor";
  if (objective == "dissipation")
    return "Minimize flow energy dissipation";
  if (objective == "mean_temperature")
    return "Minimize mean temperature";
  return humanize(objective);
}

const std::vector<std::string> &scalarKeys() {
  static const std::vector<std::string> keys = [] {
    std::vector<std::string> k;
    for (const KeySpec &s : kSpecs)
      k.emplace_back(s.key);
    return k;
  }();
  return keys;
}

// ---- settings model
// --------------------------------------------------------------------------------

std::vector<Issue> validateTopology(const json &topology,
                                    const Capabilities &caps,
                                    const json &study) {
  std::vector<Issue> issues;
  auto block = [&](std::string key, std::string msg) {
    issues.push_back({std::move(key), Severity::Blocking, std::move(msg)});
  };
  if (!topology.is_object()) {
    block("", "Topology settings are missing.");
    return issues;
  }
  if (caps.state != Capabilities::State::Ready) {
    block("", caps.state == Capabilities::State::Loading
                  ? "Solver capabilities are still loading."
                  : "Topology solver unavailable: " + caps.error);
    return issues;
  }

  const std::string physics = str(topology, "physics");
  const std::string method = str(topology, "method");
  const std::string objective = str(topology, "objective");
  if (physics.empty())
    block("physics", "Choose a physics.");
  else if (!caps.physicsEntry(physics))
    block("physics",
          "The solver does not offer the physics '" + physics + "'.");
  if (method.empty())
    block("method", "Choose an optimization method.");
  else if (!caps.method(method))
    block("method", "The solver does not offer the method '" + method + "'.");

  const Combination *combo = nullptr;
  if (!physics.empty() && !method.empty() && caps.physicsEntry(physics) &&
      caps.method(method)) {
    std::string reason;
    if (caps.supports(method, physics, &reason))
      combo = caps.find(method, physics);
    else
      block("method", reason);
  }
  const std::string pair =
      methodLabel(method, &caps) + " with " + physicsLabel(physics, &caps);

  if (objective.empty())
    block("objective", "Choose an objective.");
  else if (combo && !combo->offersObjective(objective))
    block("objective", objectiveLabel(objective, physics) +
                           " is not offered for " + pair + ".");

  if (combo && combo->offersKey("volume_fraction") &&
      !topology.contains("volume_fraction"))
    block("volume_fraction", "Set the " +
          (combo->volumePhase.empty() ? volumePhaseFor(physics) : combo->volumePhase) +
          " volume fraction (Goal).");

  for (const KeySpec &s : kSpecs) {
    const auto it = topology.find(s.key);
    if (it == topology.end())
      continue;
    const std::string label =
        keyLabel(s, physics, combo ? combo->volumePhase : std::string());
    if (combo && !combo->offersKey(s.key)) {
      std::string message = label;
      message.append(" is not offered for ")
          .append(pair)
          .append("; remove it.");
      block(s.key, std::move(message));
      continue;
    }
    std::string err = checkValue(s, *it, label);
    if (!err.empty())
      block(s.key, std::move(err));
  }

  if (combo && objective == "mass") {
    bool anyLimit = false;
    for (const std::string &l : combo->limits)
      anyLimit = anyLimit || topology.contains(l);
    if (!anyLimit)
      block("objective",
            "Mass minimization needs at least one response limit.");
  }
  const json &plateFaces = member(member(study, "build_plate"), "faces");
  const bool hasDir = topology.contains("build_direction") ||
                      (plateFaces.is_array() && !plateFaces.empty());
  if (topology.contains("overhang_angle_deg") && !hasDir)
    block("build_direction", "Overhang control needs a build direction or a bed face.");
  const double pen = num(topology, "overhang_penalty");
  if (std::isfinite(pen) && pen > 0.0) {
    if (!hasDir)
      block("build_direction", "Overhang control needs a build direction or a bed face.");
  }
  // De-duplicate identical messages (the build-direction rule can fire twice).
  std::vector<Issue> unique;
  for (Issue &i : issues) {
    const bool dup =
        std::any_of(unique.begin(), unique.end(), [&](const Issue &u) {
          return u.key == i.key && u.message == i.message;
        });
    if (!dup)
      unique.push_back(std::move(i));
  }
  return unique;
}

bool hasBlocking(const std::vector<Issue> &issues) {
  return std::any_of(issues.begin(), issues.end(), [](const Issue &i) {
    return i.severity == Severity::Blocking;
  });
}

json newTopologyConfig(const Capabilities &caps, const std::string &physics) {
  for (const CapabilityEntry &m : caps.methods) {
    if (!caps.supports(m.id, physics))
      continue;
    const Combination *c = caps.find(m.id, physics);
    return json{{"physics", physics},
                {"method", m.id},
                {"objective", c->objectives.front()}};
  }
  return json::object();
}

bool applyPrintProcess(json &study, const json &preset, const Capabilities &caps) {
  if (!study.is_object() || !preset.is_object() || str(preset, "id").empty() ||
      !member(preset, "topology").is_object())
    return false;
  const auto settings = study.find("topology");
  if (settings == study.end() || !settings->is_object())
    return false;
  json &topo = *settings;
  const Combination *pair = caps.find(str(topo, "method"), str(topo, "physics"));
  if (!pair || !pair->supported)
    return false;
  for (const auto &[key, value] : preset["topology"].items()) {
    if (!pair->offersKey(key))
      continue;
    if (value.is_null())
      topo.erase(key);
    else
      topo[key] = value;
  }
  study["print_process"] = preset["id"];
  return true;
}

bool mergeDfamResult(json &run, const json &reply, const std::string &error) {
  if (!run.is_object() || !member(run, "result").is_object())
    return false;
  json &mfg = run["result"]["manufacturing"];
  if (!mfg.is_object() || boolTrue(member(mfg, "dfam"), "checked"))
    return false;
  const json &dfam = member(reply, "dfam");
  if (error.empty() && boolTrue(reply, "ok") && boolTrue(dfam, "checked")) {
    mfg["dfam"] = dfam;
    mfg["print_ready_reason"] = str(reply, "print_ready_reason");
  } else {
    std::string why = error.empty() ? str(reply, "error") : error;
    if (why.empty())
      why = "the worker returned no checked result";
    mfg["dfam"] = {{"checked", false}, {"passed", false}, {"status", "unavailable"},
                   {"error", why}};
    mfg["print_ready_reason"] = "DFAM check could not run: " + why;
  }
  mfg["print_ready"] = false;
  return true;
}

std::string surfaceArtifactKey(const json &result) {
  const json &surface = member(member(result, "artifacts"), "surface_print_stl");
  return !str(surface, "path").empty() && !boolTrue(surface, "missing") &&
                 !boolTrue(surface, "stale")
             ? "surface_print_stl" : "surface_stl";
}

SwitchResult selectCombination(json &topology, const Capabilities &caps,
                               const std::string &method,
                               const std::string &physics) {
  SwitchResult r;
  if (!topology.is_object()) {
    r.refusal = "Topology settings are missing.";
    return r;
  }
  if (!caps.supports(method, physics, &r.refusal))
    return r;
  const Combination &c = *caps.find(method, physics);
  r.objectiveBefore = str(topology, "objective");
  topology["method"] = method;
  topology["physics"] = physics;
  if (!c.offersObjective(r.objectiveBefore))
    topology["objective"] = c.objectives.front();
  r.objectiveAfter = str(topology, "objective");
  for (const KeySpec &s : kSpecs) {
    if (topology.contains(s.key) && !c.offersKey(s.key)) {
      topology.erase(s.key);
      r.removedKeys.emplace_back(s.key);
    }
  }
  r.applied = true;
  return r;
}

void applyRunSettings(json &topology, const json &runSettingsSnapshot) {
  if (!runSettingsSnapshot.is_object())
    return;
  if (!topology.is_object())
    topology = json::object();
  auto copyOrErase = [&](const char *key) {
    const auto it = runSettingsSnapshot.find(key);
    if (it != runSettingsSnapshot.end())
      topology[key] = *it;
    else
      topology.erase(key);
  };
  copyOrErase("method");
  copyOrErase("physics");
  copyOrErase("objective");
  for (const KeySpec &s : kSpecs)
    copyOrErase(s.key);
}

// ---- results model
// -----------------------------------------------------------------------------------

std::string geometryKey(const json &tree) {
  if (!tree.is_object())
    return {};
  // FNV-1a over sorted source fields. Skip metadata before serialization: retained
  // studies can be large and must never be copied or hashed into their own key.
  std::uint64_t h = 14695981039346656037ull;
  auto hashField = [&](const std::string &text) {
    for (unsigned char c : text) {
      h ^= c;
      h *= 1099511628211ull;
    }
    h *= 1099511628211ull; // NUL separates the key and JSON value.
  };
  for (auto it = tree.begin(); it != tree.end(); ++it) {
    if (it.key() == "id" || it.key() == "name" || it.key() == "studies")
      continue;
    hashField(it.key());
    hashField(it.value().dump());
  }
  char buf[17];
  std::snprintf(buf, sizeof(buf), "%016llx", static_cast<unsigned long long>(h));
  return buf;
}

namespace {

// The number written right after `key` in `s` (searching from `from`), e.g. "N=" -> 5.
bool numberAfter(const std::string &s, const char *key, double &v,
                 std::size_t from = 0) {
  const std::size_t at = s.find(key, from);
  if (at == std::string::npos)
    return false;
  const char *b = s.c_str() + at + std::strlen(key);
  char *e = nullptr;
  v = std::strtod(b, &e);
  return e != b && std::isfinite(v);
}

std::string iterationsText(double n) {
  const long long k = (long long)n;
  return std::to_string(k) + (k == 1 ? " iteration" : " iterations");
}

} // namespace

std::string terminationLabel(const std::string &termination) {
  if (termination == "converged")
    return "Converged";
  if (termination == "max_iterations")
    return "Iteration limit";
  if (termination == "stalled")
    return "Stalled (no further improvement)";
  if (termination == "line_search_failed")
    return "Line search failed";
  if (termination == "infeasible")
    return "Infeasible design";
  if (termination == "stuck_at_bounds")
    return "Stuck at the density bounds";
  if (termination == "cancelled")
    return "Cancelled";
  if (termination == "failed")
    return "Failed";
  return humanize(termination);
}

std::string formulationLabel(const std::string &formulation) {
  const std::string head = formulation.substr(0, formulation.find_first_of("(;["));
  auto starts = [&](const char *p) { return formulation.rfind(p, 0) == 0; };
  if (starts("mma"))
    return "Method of moving asymptotes (MMA)";
  if (starts("beso"))
    return "Bi-directional evolutionary (BESO)";
  if (starts("level_set"))
    return "Reaction-diffusion level set";
  if (starts("oc"))
    return "Optimality criteria";
  std::string h = head;
  while (!h.empty() && h.back() == ' ')
    h.pop_back();
  return h.empty() ? std::string("Solver formulation") : humanize(h);
}

std::string criterionText(const std::string &crit) {
  if (crit.empty())
    return {};
  const bool voids = crit.find("no enclosed voids") != std::string::npos;
  const bool allG = crit.find("all g <=") != std::string::npos;
  const std::string tail = std::string(allG ? ", with every constraint satisfied" : "") +
                           (voids ? (allG ? " and no enclosed voids" : ", with no enclosed voids")
                                  : "") +
                           ".";
  double tol = 0.0, n = 0.0;
  if (crit.rfind("max |x_k", 0) == 0) { // density methods (SIMP / RAMP)
    const bool haveTol = numberAfter(crit, "change_tolerance=", tol);
    const bool haveN = numberAfter(crit, " for ", n);
    return std::string("The largest design change stayed under ") +
           (haveTol ? fmtG(tol) : std::string("the tolerance")) +
           (haveN ? " for " + iterationsText(n) + " in a row" : std::string()) + tail;
  }
  if (crit.rfind("volume target reached", 0) == 0) { // BESO
    const std::size_t nAt = crit.find(" with N=");
    bool haveTol = false;
    if (nAt != std::string::npos) {
      const std::size_t le = crit.rfind("<= ", nAt);
      haveTol = le != std::string::npos && numberAfter(crit, "<= ", tol, le);
    }
    const bool haveN = numberAfter(crit, "N=", n);
    return std::string("Volume target reached and the objective changed by less than ") +
           (haveTol ? fmtG(tol * 100.0) + "%" : std::string("the tolerance")) +
           (haveN ? " over the last " + iterationsText(n) : std::string()) + tail;
  }
  if (crit.rfind("volume at target", 0) == 0) { // level set
    const bool haveTol = numberAfter(crit, "< ", tol);
    const bool haveN = numberAfter(crit, " for ", n);
    return std::string("Volume at target and the design and objective changes stayed under ") +
           (haveTol ? fmtG(tol) : std::string("the tolerance")) +
           (haveN ? " for " + iterationsText(n) + " in a row" : std::string()) + tail;
  }
  return {};
}

OutcomeSummary classifyResult(const json &result) {
  OutcomeSummary o;
  o.maxViolation = kNaN;
  if (!result.is_object()) {
    o.outcome = Outcome::Failed;
    o.headline = "No result";
    o.detail = "The run returned no result.";
    return o;
  }
  const json &summary = member(result, "summary");
  o.termination = str(result, "termination");
  o.computationOk = boolTrue(result, "ok");
  o.feasibilityReported = member(result, "feasible").is_boolean();
  o.feasible = boolTrue(result, "feasible");
  o.maxViolation = num(summary, "max_constraint_violation");
  if (!std::isfinite(o.maxViolation)) {
    const json &cons = member(result, "constraints");
    if (cons.is_array()) {
      for (const json &c : cons) {
        const double v = num(c, "violation");
        if (std::isfinite(v))
          o.maxViolation =
              std::isfinite(o.maxViolation) ? std::max(o.maxViolation, v) : v;
      }
    }
  }
  std::string why = str(result, "termination_detail");
  if (why.empty())
    why = str(result, "error");
  const bool claimedConverged = boolTrue(result, "converged");
  const std::string violationText =
      std::isfinite(o.maxViolation)
          ? "Max constraint violation " + fmtG(o.maxViolation) + "."
          : std::string();

  if (!o.computationOk) {
    const bool cancelled = o.termination == "cancelled";
    o.outcome = cancelled ? Outcome::Cancelled : Outcome::Failed;
    o.headline = cancelled ? "Run cancelled" : "Computation failed";
    o.detail = !why.empty() ? why
               : cancelled  ? "The run was cancelled before it finished."
                            : "The solver reported a failure without a reason.";
    return o;
  }
  if (o.termination == "cancelled") {
    o.outcome = Outcome::Cancelled;
    o.headline = "Run cancelled (partial result)";
    o.detail =
        why.empty() ? "The design shown is the last completed iteration." : why;
    return o;
  }
  const bool iterLimit = o.termination == "max_iterations";
  const bool infeasible =
      (o.feasibilityReported && !o.feasible) || o.termination == "infeasible";
  const bool otherStop = !o.termination.empty() && o.termination != "converged";

  if (claimedConverged &&
      (iterLimit || infeasible || !o.feasibilityReported || otherStop)) {
    o.outcome = Outcome::Inconsistent;
    o.headline = "Inconsistent result";
    if (infeasible)
      o.detail = "The solver reported convergence for an infeasible design. " +
                 violationText;
    else if (iterLimit)
      o.detail =
          "The solver reported convergence but stopped at the iteration limit.";
    else if (!o.feasibilityReported)
      o.detail =
          "The solver reported convergence without reporting feasibility.";
    else
      o.detail = "The solver reported convergence but stopped with \"" +
                 terminationLabel(o.termination) + "\".";
    return o;
  }
  if (infeasible) {
    o.outcome = Outcome::Infeasible;
    o.headline = "Infeasible design";
    o.detail = violationText.empty() ? "At least one constraint is violated."
                                     : violationText;
    if (iterLimit)
      o.detail += " Stopped at the iteration limit.";
    return o;
  }
  if (iterLimit) {
    o.outcome = Outcome::IterationLimit;
    o.headline = "Iteration limit reached";
    o.detail = "The convergence criterion was not met. Raise the iteration "
               "limit in Setup and run again.";
    return o;
  }
  if (claimedConverged) {
    o.outcome = Outcome::Converged;
    o.converged = true;
    o.headline = "Converged";
    const std::string crit = criterionText(str(summary, "convergence_criterion"));
    o.detail = crit.empty() ? "Every constraint is satisfied." : crit;
    return o;
  }
  o.outcome = Outcome::NotConverged;
  o.headline = "Not converged";
  if (!why.empty())
    o.detail = why;
  else if (!o.termination.empty())
    o.detail = "Stopped (" + terminationLabel(o.termination) +
               ") before meeting the convergence criterion.";
  else
    o.detail = "The convergence criterion was not met.";
  if (!o.feasibilityReported)
    o.detail += " Feasibility was not reported.";
  return o;
}

ManufacturingReport manufacturingReport(const json &result,
                                        const json &settings) {
  ManufacturingReport rep;
  const json &mfg = member(result, "manufacturing");
  std::set<std::string> covered;
  const json &checks = member(mfg, "checks");
  if (checks.is_array()) {
    for (const json &c : checks) {
      ManufacturingRow row;
      row.key = str(c, "id");
      if (row.key.empty())
        continue;
      row.label = str(c, "label");
      if (row.label.empty())
        row.label = mfgLabel(row.key);
      row.units = str(c, "units");
      if (row.units.empty())
        row.units = mfgUnits(row.key);
      row.note = str(c, "note");
      if (c.contains("requested"))
        row.requested = c.at("requested");
      else if (settings.is_object() && settings.contains(row.key))
        row.requested = settings.at(row.key);
      if (c.contains("measured"))
        row.measured = c.at("measured");
      const std::string status = str(c, "status");
      row.status = status == "pass"   ? CheckStatus::Pass
                   : status == "fail" ? CheckStatus::Fail
                                      : CheckStatus::NotMeasured;
      if (row.status == CheckStatus::Pass && row.measured.is_null()) {
        row.status = CheckStatus::NotMeasured;
        if (row.note.empty())
          row.note = "Reported as passing without a measured value.";
      }
      covered.insert(row.key);
      rep.rows.push_back(std::move(row));
    }
  }
  // Requested intent the solver did not measure is listed as such — never as
  // satisfied.
  static const char *const kIntent[] = {"min_member_mm", "overhang_angle_deg",
                                        "no_enclosed_voids", "symmetry_axes",
                                        "extrusion_axis"};
  for (const char *key : kIntent) {
    if (!settings.is_object() || covered.count(key))
      continue;
    const auto it = settings.find(key);
    if (it == settings.end())
      continue;
    if (it->is_boolean() && !it->get<bool>())
      continue;
    if (it->is_array() && it->empty())
      continue;
    ManufacturingRow row;
    row.key = key;
    row.label = mfgLabel(key);
    row.units = mfgUnits(key);
    row.requested = *it;
    row.status = CheckStatus::NotMeasured;
    row.note = std::string(key) == "min_member_mm"
                   ? "Filter/regularization target, not a guaranteed minimum; "
                     "not measured on "
                     "the result geometry."
                   : "Requested in setup; not measured on the result geometry.";
    rep.rows.push_back(std::move(row));
  }

  rep.geometryValidated = boolTrue(mfg, "geometry_valid");
  const json &dfam = member(mfg, "dfam");
  rep.dfamChecked = boolTrue(dfam, "checked");
  rep.dfamPassed = rep.dfamChecked && boolTrue(dfam, "passed");
  const auto failed = std::find_if(
      rep.rows.begin(), rep.rows.end(),
      [](const ManufacturingRow &r) { return r.status == CheckStatus::Fail; });

  if (!mfg.is_object())
    rep.printReadyReason =
        "No manufacturing validation was reported for this run.";
  else if (!rep.geometryValidated)
    rep.printReadyReason =
        "The result surface was not validated as a closed, manifold solid.";
  else if (!str(dfam, "error").empty())
    rep.printReadyReason = "DFAM check could not run: " + str(dfam, "error");
  else if (!str(mfg, "print_ready_reason").empty())
    rep.printReadyReason = str(mfg, "print_ready_reason");
  else if (!rep.dfamChecked)
    rep.printReadyReason =
        "DFAM checks were not run on the result geometry; the optimizer "
        "alone cannot establish printability.";
  else if (!rep.dfamPassed) {
    const std::string s = str(dfam, "summary");
    rep.printReadyReason =
        s.empty() ? "DFAM checks failed." : "DFAM checks failed: " + s;
  } else if (failed != rep.rows.end())
    rep.printReadyReason = failed->label + " failed its manufacturing check.";
  else
    // A topology run never slices its result, and print readiness requires a
    // sliced part whose G-code passed validation. A record's own print_ready
    // flag (old or hand-edited records) cannot stand in for that, so the
    // evidence is reported, never promoted.
    rep.printReadyReason = "Geometry and DFAM checks passed, but the result "
                           "has not been sliced: it is not "
                           "print-ready until the exported part is sliced and "
                           "its G-code passes validation.";
  return rep;
}

std::vector<MetricDelta> compareRuns(const json &baselineRun,
                                     const json &selectedRun) {
  std::vector<MetricDelta> out;
  const json &sa = member(runResult(baselineRun), "summary");
  const json &sb = member(runResult(selectedRun), "summary");
  auto add = [&](const char *key, std::string label, std::string units,
                 bool comparable, std::string note) {
    MetricDelta d;
    d.key = key;
    d.label = std::move(label);
    d.units = std::move(units);
    d.baseline = num(sa, key);
    d.selected = num(sb, key);
    if (!std::isfinite(d.baseline) && !std::isfinite(d.selected))
      return;
    d.comparable =
        comparable && std::isfinite(d.baseline) && std::isfinite(d.selected);
    d.note = std::move(note);
    if (!d.comparable && d.note.empty())
      d.note = "Not reported by both runs.";
    out.push_back(std::move(d));
  };

  const std::string pa = runPhysics(baselineRun), pb = runPhysics(selectedRun);
  const std::string oa = runObjective(baselineRun),
                    ob = runObjective(selectedRun);
  const std::string ua = str(sa, "objective_units"),
                    ub = str(sb, "objective_units");
  std::string objNote;
  if (pa != pb)
    objNote = "Different physics.";
  else if (oa != ob)
    objNote = "Different objectives.";
  else if (ua != ub)
    objNote = "Different objective units.";
  // Final values throughout, so the labels drop the "Final" prefix.
  add("objective_final",
      objNote.empty() && !ob.empty() ? objectiveLabel(ob, pb) : "Objective",
      ub.empty() ? ua : ub, objNote.empty(), objNote);
  const std::string fa = runPhase(baselineRun), fb = runPhase(selectedRun);
  add("volume_fraction_final", fa == fb ? volumeLabel(fb) : "Volume fraction",
      "", fa == fb,
      fa == fb ? std::string()
               : "One run budgets solid volume, the other fluid volume.");
  add("mass_final_g", "Mass", "g", true, "");
  add("max_constraint_violation", "Max constraint violation", "", true, "");
  add("iterations", "Iterations", "", true, "");
  add("elapsed_s", "Run time", "s", true, "");
  return out;
}

} // namespace topology
} // namespace ui

#ifndef CHUDWARE_SIM_TOPOLOGY_MODEL_ONLY
// ================================================================================================
// Drawing. GUI thread only; no RPC, no filesystem access, no AppState mutation.
// ================================================================================================
#include <memory>

#include "imgui.h"
#include "../core/app_state.h"

#include "sim_manager.h" // SimManager::unitSys — the Simulate workspace's display units
#include "widgets/kit.h"

namespace ui {
namespace topology {
namespace {

namespace kit = ui::kit;

struct DisplayUnits {
  double len = 1.0, stress = 1.0, pressPa = 1.0, mass = 1.0;
  std::string lenL = "mm", stressL = "MPa", pressL = "Pa", massL = "g";
};

DisplayUnits displayUnits(const AppState &st) {
  const SimManager::UnitSys u = SimManager::unitSys(st);
  DisplayUnits d;
  d.len = u.lenFromMm;
  d.lenL = u.len;
  d.stress = u.stressFromMpa;
  d.stressL = u.stress;
  if (u.us) { // contract pressure-drop limits are Pa; US display follows the
              // sim's psi
    d.pressPa = u.pressFromMpa * 1e-6;
    d.pressL = u.press;
    d.mass = 1.0 / 453.59237;
    d.massL = "lb";
  }
  return d;
}

double unitFactor(Unit u, const DisplayUnits &d) {
  switch (u) {
  case Unit::Length:
    return d.len;
  case Unit::Stress:
    return d.stress;
  case Unit::PressurePa:
    return d.pressPa;
  default:
    return 1.0;
  }
}

// Canonical display spelling (kit::unitText) of a key's unit in display units.
std::string unitLabel(Unit u, const DisplayUnits &d) {
  switch (u) {
  case Unit::Length:
    return kit::unitText(d.lenL.c_str());
  case Unit::Stress:
    return kit::unitText(d.stressL.c_str());
  case Unit::PressurePa:
    return kit::unitText(d.pressL.c_str());
  case Unit::TempC:
    return kit::unitText(kit::Unit::DegC);
  case Unit::Hz:
    return kit::unitText(kit::Unit::Hz);
  case Unit::Deg:
    return kit::unitText(kit::Unit::deg);
  default:
    return {};
  }
}

// "12.5 mm" / "0.35": a readout with an optional unit.
std::string quantity(double v, const std::string &unit) {
  return unit.empty() ? kit::fmtValue(v) : kit::fmtQuantity(v, unit.c_str());
}

std::string valueText(const json &v) {
  if (v.is_number())
    return kit::fmtValue(v.get<double>());
  if (v.is_boolean())
    return v.get<bool>() ? "On" : "Off";
  auto text = [](const json &e) {
    if (e.is_number())
      return kit::fmtValue(e.get<double>());
    if (e.is_string()) {
      std::string s = e.get<std::string>();
      if (isAxis(s))
        s[0] = char(s[0] - 'a' + 'A');
      return s;
    }
    return e.dump();
  };
  if (v.is_string())
    return text(v);
  if (v.is_array()) {
    std::string s;
    for (const json &e : v) {
      if (!s.empty())
        s += ", ";
      s += text(e);
    }
    return s;
  }
  return v.is_null() ? std::string() : v.dump();
}

// All validation messages of one key folded into one issue line: Err when any
// is blocking, else Warn.
struct FieldIssue {
  std::string text;
  kit::Tone tone = kit::Tone::Err;
};

FieldIssue keyIssue(const std::vector<Issue> &issues, const std::string &key) {
  FieldIssue out;
  bool blocking = false;
  for (const Issue &i : issues) {
    if (i.key != key)
      continue;
    if (!out.text.empty())
      out.text += ' ';
    out.text += i.message;
    blocking = blocking || i.severity == Severity::Blocking;
  }
  out.tone = blocking ? kit::Tone::Err : kit::Tone::Warn;
  return out;
}

kit::FieldOpts &withFieldIssue(kit::FieldOpts &o, const FieldIssue &fi) {
  if (!fi.text.empty())
    o.withIssue(fi.text.c_str(), fi.tone);
  return o;
}

// One-shot navigation: puts the current cursor line at the top of the caller's
// scrolling child on the next frame (clamped to its scroll range).
void scrollCursorToTop() {
  ImGui::SetScrollFromPosY(
      ImGui::GetCursorScreenPos().y - ImGui::GetWindowPos().y, 0.0f);
}

// Placeholder of an empty scalar: the solver default this pair applies, kept
// short enough for the value cell ("0.05", "Off", "Auto"); the field tip says
// in words that an empty field uses it. A default of 0 on a key whose range
// starts at an inclusive 0 is the "disabled" value (no skin depth, no filter,
// no penalty), so it reads "Off".
std::string defaultHint(const Combination &c, const KeySpec &s, double factor) {
  const json &d = member(c.defaults, s.key);
  if (d.is_number()) {
    const double v = d.get<double>();
    if (v == 0.0 && s.lo == 0.0 && !s.loOpen)
      return "Off";
    return kit::fmtValue(v * factor);
  }
  return "Auto";
}

// Owns the labels / tips / disabled reasons of one enum field for the call.
struct EnumItems {
  std::vector<std::string> labels, tips, reasons;
  std::vector<char> off;
  std::unique_ptr<bool[]> disabled;
  std::vector<const char *> l, t, r;

  void add(std::string label, std::string tip = {}, bool enabled = true,
           std::string reason = {}) {
    labels.push_back(std::move(label));
    tips.push_back(std::move(tip));
    reasons.push_back(std::move(reason));
    off.push_back(enabled ? 0 : 1);
  }
  int count() const { return int(labels.size()); }
  // Builds the pointer arrays (after the last add) and wires them into `o`.
  kit::FieldOpts &into(kit::FieldOpts &o) {
    const size_t n = labels.size();
    disabled.reset(new bool[n + 1]);
    l.clear();
    t.clear();
    r.clear();
    bool any = false;
    for (size_t i = 0; i < n; ++i) {
      disabled[i] = off[i] != 0;
      any = any || disabled[i];
      l.push_back(labels[i].c_str());
      t.push_back(tips[i].empty() ? nullptr : tips[i].c_str());
      r.push_back(reasons[i].empty() ? nullptr : reasons[i].c_str());
    }
    o.withItemTips(t.data());
    if (any)
      o.withItemDisabled(disabled.get(), r.data());
    return o;
  }
};

// kit::row with caller-drawn actions in its reserved right lane
// (RowStyle::actionsW). `actions` draws at the lane's left edge, vertically
// centred on the row.
template <class F>
void rowWithActions(const char *id, const char *title, kit::RowStyle rs,
                    float actionsW, F &&actions) {
  rs.actionsW = actionsW;
  ImGui::PushID(id);
  ImGui::SetNextItemAllowOverlap();
  kit::row("row", title, rs);
  const ImVec2 rmin = ImGui::GetItemRectMin(), rmax = ImGui::GetItemRectMax();
  const float h = ImGui::GetFrameHeight();
  ImGui::SetCursorScreenPos(ImVec2(rmax.x - kit::padX() - actionsW,
                                   rmin.y + (rmax.y - rmin.y - h) * 0.5f));
  actions();
  // Resume the layout exactly below the row.
  ImGui::SetCursorScreenPos(ImVec2(rmin.x, rmax.y));
  ImGui::Dummy(ImVec2(0.0f, 0.0f));
  ImGui::PopID();
}

float buttonWidth(const char *label) {
  return ImGui::CalcTextSize(label).x + ImGui::GetStyle().FramePadding.x * 2.0f;
}

// ---- settings fields
// ---------------------------------------------------------------------------
bool numberField(const KeySpec &s, json &topo, const Combination &c,
                 const std::string &physics, const DisplayUnits &du,
                 const std::vector<Issue> &issues) {
  const double factor = unitFactor(s.unit, du);
  const std::string label = keyLabel(s, physics, c.volumePhase);
  const std::string unit = unitLabel(s.unit, du);
  const std::string hint = defaultHint(c, s, factor);
  const FieldIssue fi = keyIssue(issues, s.key);
  const auto it = topo.find(s.key);
  bool present = it != topo.end() && it->is_number();
  kit::FieldOpts o;
  const bool required = std::strcmp(s.key, "volume_fraction") == 0;
  const std::string tip = std::string(s.tip) +
      (required ? " Set the volume budget before running."
                : " Leave empty to use the " +
                  (hint == "Auto" ? std::string("solver's default.")
                                  : "default (" + hint + ")."));
  o.withTip(tip.c_str()).withHint(hint.c_str()).withPresent(&present)
      .withRequired(required);
  if (!unit.empty())
    o.withUnit(unit.c_str());
  withFieldIssue(o, fi);

  if (s.kind == Kind::Integer) {
    const double cur = present ? it->get<double>() : 0.0;
    int v = int(std::clamp(cur, double(std::numeric_limits<int>::min()),
                           double(std::numeric_limits<int>::max())));
    if (!kit::fieldInt(s.key, label.c_str(), &v, o))
      return false;
    if (present)
      topo[s.key] = v;
    else
      topo.erase(s.key);
    return true;
  }
  double v = present ? it->get<double>() : 0.0;
  o.withScale(factor);
  if (!kit::fieldNumber(s.key, label.c_str(), &v, o))
    return false;
  if (present)
    topo[s.key] = v;
  else
    topo.erase(s.key);
  return true;
}

bool boolField(const KeySpec &s, json &topo, const std::vector<Issue> &issues) {
  const auto it = topo.find(s.key);
  bool v = it != topo.end() && it->is_boolean() && it->get<bool>();
  const FieldIssue fi = keyIssue(issues, s.key);
  kit::FieldOpts o;
  o.withTip(s.tip);
  withFieldIssue(o, fi);
  if (!kit::fieldCheckbox(s.key, s.label, &v, o))
    return false;
  topo[s.key] = v;
  return true;
}

const char *const kAxisIds[3] = {"x", "y", "z"};

bool axisSetField(const KeySpec &s, json &topo,
                  const std::vector<Issue> &issues) {
  bool on[3] = {false, false, false};
  if (const auto it = topo.find(s.key); it != topo.end() && it->is_array())
    for (const json &e : *it)
      for (int i = 0; i < 3; ++i)
        if (e.is_string() && e.get<std::string>() == kAxisIds[i])
          on[i] = true;
  const FieldIssue fi = keyIssue(issues, s.key);
  kit::FieldOpts o;
  o.withTip(s.tip);
  withFieldIssue(o, fi);
  static const char *const kTips[3] = {"Symmetric layout about the X axis.",
                                       "Symmetric layout about the Y axis.",
                                       "Symmetric layout about the Z axis."};
  kit::fieldBegin(s.key, s.label, o);
  const bool changed = axisToggles(on, kTips);
  kit::fieldEnd();
  if (!changed)
    return false;
  json arr = json::array();
  for (int i = 0; i < 3; ++i)
    if (on[i])
      arr.push_back(kAxisIds[i]);
  if (arr.empty())
    topo.erase(s.key);
  else
    topo[s.key] = std::move(arr);
  return true;
}

bool axisField(const KeySpec &s, json &topo, const std::vector<Issue> &issues) {
  static const char *const kLabels[4] = {"None", "X", "Y", "Z"};
  int sel = 0;
  const std::string cur = str(topo, s.key);
  for (int i = 0; i < 3; ++i)
    if (cur == kAxisIds[i])
      sel = i + 1;
  const FieldIssue fi = keyIssue(issues, s.key);
  kit::FieldOpts o;
  o.withTip(s.tip);
  withFieldIssue(o, fi);
  if (!kit::fieldEnum(s.key, s.label, &sel, kLabels, 4, o))
    return false;
  if (sel == 0)
    topo.erase(s.key);
  else
    topo[s.key] = kAxisIds[sel - 1];
  return true;
}

// Build direction: a preset combo (None, ±X, ±Y, ±Z, Custom vector) plus the
// X/Y/Z cells while "Custom vector" is chosen. A typed vector is normalized on
// commit; a zero vector is stored as typed and reported by validation.
bool directionField(const KeySpec &s, json &topo, SettingsView &view,
                    const std::vector<Issue> &issues) {
  static const double kPresets[6][3] = {{1, 0, 0},  {-1, 0, 0}, {0, 1, 0},
                                        {0, -1, 0}, {0, 0, 1},  {0, 0, -1}};
  static const char *const kLabels[8] = {"None", "+X", "-X", "+Y",
                                         "-Y",   "+Z", "-Z", "Custom vector"};
  constexpr int kCustom = 7;
  double cur[3] = {0.0, 0.0, 0.0};
  bool present = false;
  if (const auto it = topo.find(s.key);
      it != topo.end() && it->is_array() && it->size() == 3) {
    present = true;
    for (int i = 0; i < 3; ++i)
      cur[i] = (*it)[i].is_number() ? (*it)[i].get<double>() : 0.0;
  }
  int preset = 0;
  for (int p = 0; p < 6 && present; ++p)
    if (cur[0] == kPresets[p][0] && cur[1] == kPresets[p][1] &&
        cur[2] == kPresets[p][2])
      preset = p + 1;
  if (present && preset == 0)
    view.customDirection = true; // an arbitrary stored vector is custom
  int sel = view.customDirection ? kCustom : preset;

  const FieldIssue fi = keyIssue(issues, s.key);
  kit::FieldOpts eo;
  eo.withTip(s.tip).withEnumStyle(kit::EnumStyle::Combo);
  if (sel != kCustom)
    withFieldIssue(eo, fi);
  bool committed = false;
  if (kit::fieldEnum(s.key, s.label, &sel, kLabels, 8, eo)) {
    view.customDirection = sel == kCustom;
    if (sel == 0) {
      committed = present;
      topo.erase(s.key);
    } else if (sel != kCustom) {
      topo[s.key] = json::array(
          {kPresets[sel - 1][0], kPresets[sel - 1][1], kPresets[sel - 1][2]});
      committed = true;
    }
  }
  if (sel != kCustom)
    return committed;

  static const char *const kCells[3] = {"X", "Y", "Z"};
  kit::FieldOpts vo;
  vo.withTip("Direction in part coordinates; normalized when you leave the "
             "cells.")
      .withGroupCommit()
      .withCellHints(kCells)
      .withPresent(&present);
  withFieldIssue(vo, fi);
  if (kit::fieldVec3("build_direction_xyz", "Direction vector", cur, vo)) {
    if (!present) {
      topo.erase(s.key);
    } else {
      const double n =
          std::sqrt(cur[0] * cur[0] + cur[1] * cur[1] + cur[2] * cur[2]);
      const double k = (n > 1e-12 && std::isfinite(n)) ? 1.0 / n : 1.0;
      topo[s.key] = json::array({cur[0] * k, cur[1] * k, cur[2] * k});
    }
    committed = true;
  }
  return committed;
}

bool drawSpec(const KeySpec &s, json &topo, const Combination &c,
              const std::string &physics, const DisplayUnits &du,
              SettingsView &view, const std::vector<Issue> &issues) {
  switch (s.kind) {
  case Kind::Real:
  case Kind::Integer:
    return numberField(s, topo, c, physics, du, issues);
  case Kind::Bool:
    return boolField(s, topo, issues);
  case Kind::AxisSet:
    return axisSetField(s, topo, issues);
  case Kind::Axis:
    return axisField(s, topo, issues);
  case Kind::Vec3:
    return directionField(s, topo, view, issues);
  }
  return false;
}

unsigned groupBit(Group g) {
  switch (g) {
  case Group::Goal:
    return kGroupGoal;
  case Group::Limits:
    return kGroupLimits;
  case Group::Convergence:
    return kGroupConvergence;
  case Group::Regularization:
    return kGroupFilter;
  case Group::Method:
    return kGroupMethod;
  case Group::Geometry:
    return kGroupSymmetry;
  case Group::Additive:
    return kGroupAdditive;
  }
  return 0;
}

bool groupOffered(Group g, const Combination &c) {
  for (const KeySpec &s : kSpecs)
    if (s.group == g && c.offersKey(s.key))
      return true;
  return false;
}

// Draws one group's OFFERED keys in a titled card, as one field group (one
// stacked/inline decision). `emptyNote` (non-null) is shown in the card when
// nothing is offered; otherwise an empty group draws nothing. `note`
// (optional) is a dim line closing the card. Returns committed.
bool drawGroup(Group g, const char *id, const char *title, json &topo,
               const Combination &c, const std::string &physics,
               const DisplayUnits &du, SettingsView &view,
               const std::vector<Issue> &issues, const char *emptyNote,
               const char *note, const json &study,
               const std::function<void()> &additiveControls) {
  const bool any = groupOffered(g, c);
  const bool additive = g == Group::Additive && bool(additiveControls);
  if (!any && !emptyNote && !additive)
    return false;
  kit::gapRow();
  bool committed = false;
  if (kit::cardBegin(id, title)) {
    if (additive)
      additiveControls();
    if (!any && !additive) {
      kit::dimText(emptyNote);
    } else {
      std::string widest;
      float widestW = 0.0f;
      for (const KeySpec &s : kSpecs) {
        if (s.group != g || !c.offersKey(s.key) || s.kind == Kind::Bool)
          continue;
        std::string l = keyLabel(s, physics, c.volumePhase);
        const float w = ImGui::CalcTextSize(l.c_str()).x;
        if (w > widestW) {
          widestW = w;
          widest = std::move(l);
        }
      }
      {
        kit::FieldGroup group("fields", widest.c_str());
        for (const KeySpec &s : kSpecs) {
          if (s.group != g || !c.offersKey(s.key))
            continue;
          const json &faces = member(member(study, "build_plate"), "faces");
          if (std::strcmp(s.key, "build_direction") == 0 &&
              faces.is_array() && !faces.empty()) {
            static const char *const labels[] = {"From bed face"};
            int selected = 0;
            kit::fieldEnum(s.key, s.label, &selected, labels, 1,
                           kit::FieldOpts().withEnumStyle(kit::EnumStyle::Combo)
                               .withDisabled(true, "Clear the bed face to set a direction."));
          } else {
            committed |= drawSpec(s, topo, c, physics, du, view, issues);
          }
        }
      }
      if (note)
        kit::dimText(note);
    }
  }
  kit::cardEnd();
  return committed;
}

std::string switchNote(const SwitchResult &r, const std::string &physics) {
  std::string note;
  if (r.objectiveBefore != r.objectiveAfter && !r.objectiveBefore.empty())
    note += "Objective changed to " +
            objectiveLabel(r.objectiveAfter, physics) + ". ";
  if (!r.removedKeys.empty()) {
    note += "Removed settings this combination does not offer:";
    for (size_t i = 0; i < r.removedKeys.size(); ++i) {
      const KeySpec *s = specFor(r.removedKeys[i]);
      note += (i ? ", " : " ") + (s ? keyLabel(*s, physics) : r.removedKeys[i]);
    }
    note += ".";
  }
  return note;
}

bool problemSection(json &topo, const Capabilities &caps, SettingsView &view,
                    const std::vector<Issue> &issues) {
  bool committed = false;
  const std::string physics = str(topo, "physics");
  const std::string method = str(topo, "method");
  auto apply = [&](const std::string &m, const std::string &p) {
    const SwitchResult r = selectCombination(topo, caps, m, p);
    if (r.applied) {
      view.switchNote = switchNote(r, p);
      view.customDirection = false;
      committed = true;
    }
  };

  // Physics, Method, Objective in one card; the solver's formulation prose
  // lives in a collapsed card below so Objective stays above the fold.
  kit::gapRow();
  kit::cardBegin("problem", "Problem", "settings");
  {
  kit::FieldGroup group("problem", "Objective");

  // Physics. Picking one keeps the current method when it supports the new
  // physics, else the first method that does.
  {
    EnumItems items;
    std::vector<std::string> targets;
    int sel = -1;
    for (const CapabilityEntry &p : caps.physics) {
      std::string target;
      if (caps.supports(method, p.id))
        target = method;
      for (const CapabilityEntry &m : caps.methods)
        if (target.empty() && caps.supports(m.id, p.id))
          target = m.id;
      const std::string label = physicsLabel(p.id, &caps);
      if (p.id == physics)
        sel = items.count();
      items.add(label, p.formulation, !target.empty(),
                "No method in this solver supports " + label + ".");
      targets.push_back(std::move(target));
    }
    const std::string preview =
        sel < 0 && !physics.empty() ? physicsLabel(physics, &caps) : "";
    const FieldIssue fi = keyIssue(issues, "physics");
    kit::FieldOpts o;
    o.withTip("What the optimizer analyses each iteration.")
        .withHint("Choose physics")
        .withRequired()
        .withEnumStyle(kit::EnumStyle::Combo);
    if (!preview.empty())
      o.withPreview(preview.c_str());
    withFieldIssue(o, fi);
    items.into(o);
    const int before = sel;
    if (kit::fieldEnum("physics", "Physics", &sel, items.l.data(),
                       items.count(), o) &&
        sel >= 0 && sel != before && !targets[size_t(sel)].empty())
      apply(targets[size_t(sel)], caps.physics[size_t(sel)].id);
  }

  // Method: unsupported pairs stay listed, disabled with the reason.
  {
    const std::string p = str(topo, "physics");
    const std::string m = str(topo, "method");
    EnumItems items;
    int sel = -1;
    for (const CapabilityEntry &e : caps.methods) {
      std::string reason;
      const bool enabled = caps.supports(e.id, p, &reason);
      std::string tip;
      if (enabled) {
        const Combination *c = caps.find(e.id, p);
        tip = c && !c->formulation.empty() ? c->formulation : e.formulation;
      }
      if (e.id == m)
        sel = items.count();
      items.add(methodLabel(e.id, &caps), std::move(tip), enabled,
                std::move(reason));
    }
    const std::string preview =
        sel < 0 && !m.empty() ? methodLabel(m, &caps) : "";
    const FieldIssue fi = keyIssue(issues, "method");
    kit::FieldOpts o;
    o.withTip("Optimization family. Unsupported pairs are listed with the "
              "reason.")
        .withHint("Choose method")
        .withRequired()
        .withEnumStyle(kit::EnumStyle::Combo)
        .withDisabled(p.empty(), "Choose a physics first.");
    if (!preview.empty())
      o.withPreview(preview.c_str());
    withFieldIssue(o, fi);
    items.into(o);
    const int before = sel;
    if (kit::fieldEnum("method", "Method", &sel, items.l.data(), items.count(),
                       o) &&
        sel >= 0 && sel != before)
      apply(caps.methods[size_t(sel)].id, p);
  }

  // Re-read: a switch above may have changed the pair.
  const std::string p2 = str(topo, "physics"), m2 = str(topo, "method");
  const Combination *combo =
      caps.supports(m2, p2) ? caps.find(m2, p2) : nullptr;

  // Objective: offered by the chosen pair.
  {
    const std::string obj = str(topo, "objective");
    EnumItems items;
    int sel = -1;
    if (combo)
      for (const std::string &o : combo->objectives) {
        if (o == obj)
          sel = items.count();
        items.add(objectiveLabel(o, p2));
      }
    // Keep the selected value compact enough for the pane's value column; the complete
    // objective is wrapped below the group rather than ellipsized inside the combo.
    const std::string preview =
        obj == "compliance" ? "Stiffness" :
        obj == "mass" ? "Mass" :
        obj == "thermal_compliance" ? "Thermal" :
        obj == "frequency" ? "Frequency" :
        obj == "buckling_factor" ? "Buckling" :
        obj == "pressure_drop" ? "Pressure drop" :
        (!obj.empty() ? objectiveLabel(obj, p2) : "");
    const FieldIssue fi = keyIssue(issues, "objective");
    kit::FieldOpts o;
    o.withHint("Choose objective")
        .withRequired()
        .withEnumStyle(kit::EnumStyle::Combo)
        .withDisabled(!combo, "Choose a supported physics and method first.");
    if (!preview.empty())
      o.withPreview(preview.c_str());
    withFieldIssue(o, fi);
    items.into(o);
    const int before = sel;
    if (kit::fieldEnum("objective", "Objective", &sel, items.l.data(),
                       items.count(), o) &&
        combo && sel >= 0 && sel != before) {
      topo["objective"] = combo->objectives[size_t(sel)];
      committed = true;
    }
  }
  } // FieldGroup "problem"
  if (!str(topo, "objective").empty())
    kit::dimText(objectiveLabel(str(topo, "objective"), str(topo, "physics")).c_str());
  if (!view.switchNote.empty())
    kit::inlineStatus(view.switchNote.c_str(), kit::Tone::Info);
  kit::cardEnd();

  const std::string pf = str(topo, "physics"), mf = str(topo, "method");
  const Combination *cf = caps.supports(mf, pf) ? caps.find(mf, pf) : nullptr;
  const CapabilityEntry *pe = cf ? caps.physicsEntry(pf) : nullptr;
  const bool hasFormulation = cf && !cf->formulation.empty();
  const bool hasValidity = pe && !pe->validity.empty();
  if (hasFormulation || hasValidity) {
    kit::gapRow();
    if (kit::cardBegin("formulation", "Formulation", nullptr,
                       &view.formulationOpen)) {
      if (hasFormulation)
        kit::dimText(cf->formulation.c_str());
      if (hasValidity)
        kit::dimText(pe->validity.c_str());
    }
    kit::cardEnd();
  }
  return committed;
}

// Owned keys present but not offered by the pair (filtered to `groups`): the
// run is blocked until they are removed.
bool orphanSection(json &topo, const Combination &combo,
                   const std::string &physics, unsigned groups) {
  bool committed = false;
  bool header = false;
  const float actionsW = ImGui::GetFrameHeight();
  for (const KeySpec &s : kSpecs) {
    if (!(groups & groupBit(s.group)) || !topo.contains(s.key) ||
        combo.offersKey(s.key))
      continue;
    if (!header) {
      kit::sectionHeader("Not offered for this combination");
      kit::inlineStatus("The chosen method and physics do not accept these "
                        "settings. Remove them to run.",
                        kit::Tone::Err);
      header = true;
    }
    const std::string label = keyLabel(s, physics, combo.volumePhase);
    const std::string value = valueText(topo[s.key]);
    kit::RowStyle rs;
    rs.secondary = value.c_str();
    rs.tooltip = s.tip;
    bool remove = false;
    rowWithActions(s.key, label.c_str(), rs, actionsW, [&] {
      remove = kit::iconButton("remove", "delete", "Remove this setting",
                               true, kit::Tone::Err);
    });
    if (remove) {
      topo.erase(s.key);
      committed = true;
    }
  }
  return committed;
}

} // namespace

bool axisToggles(bool on[3], const char *const *tips) {
  static const char *const kNames[3] = {"X", "Y", "Z"};
  static const char *const kIds[3] = {"ax_x", "ax_y", "ax_z"};
  // Kit checkboxes (frame height, so they sit on the field's row exactly like
  // its label): the on/off state reads at a glance, unlike accent chips.
  bool changed = false;
  for (int i = 0; i < 3; ++i) {
    if (i > 0)
      ImGui::SameLine(0.0f, kit::gutter());
    changed |= kit::checkbox(kIds[i], kNames[i], &on[i], tips ? tips[i] : nullptr);
  }
  return changed;
}

bool drawTopologySettings(AppState &st, json &topology,
                          const Capabilities &caps, SettingsView &view,
                          const char *lockedReason, unsigned groups,
                          const json &study,
                          const std::function<void()> &additiveControls) {
  ImGui::PushID("topology_settings");
  const bool problem = (groups & kGroupProblem) != 0;
  const bool mfg = (groups & kGroupsManufacturing) != 0;
  bool committed = false;
  if (caps.state == Capabilities::State::Loading) {
    kit::spinner("Loading solver capabilities");
    ImGui::PopID();
    return false;
  }
  if (caps.state == Capabilities::State::Unavailable) {
    kit::blockedState(
        "Topology solver unavailable", caps.error.c_str(),
        "Saved settings are kept unchanged until the solver is available.");
    view.focusManufacturing =
        false; // nothing to focus; never fire later by surprise
    ImGui::PopID();
    return false;
  }
  if (!topology.is_object()) {
    if (problem) {
      const bool create = kit::blockedState(
          "Topology settings missing",
          "This study has no topology configuration.",
          "Create one for linear structural optimization, then pick the "
          "physics you need.",
          lockedReason ? nullptr : "Create settings");
      if (create) {
        json fresh = newTopologyConfig(caps, "structural");
        if (fresh.empty() && !caps.physics.empty())
          fresh = newTopologyConfig(caps, caps.physics.front().id);
        if (!fresh.empty()) {
          topology = std::move(fresh);
          committed = true;
        }
      }
    } else {
      kit::dimText("Create the topology settings on the Settings step first.");
    }
    view.focusManufacturing = false;
    ImGui::PopID();
    return committed;
  }

  const DisplayUnits du = displayUnits(st);
  const std::vector<Issue> issues = validateTopology(topology, caps, study);
  // Window-relative top of the form: the manufacturing focus falls back to it
  // when the chosen pair offers no manufacturing groups.
  const float formTop = ImGui::GetCursorScreenPos().y - ImGui::GetWindowPos().y;
  if (lockedReason && problem)
    kit::inlineStatus(lockedReason, kit::Tone::Warn);
  ImGui::BeginDisabled(lockedReason != nullptr);

  if (problem)
    committed |= problemSection(topology, caps, view, issues);

  const std::string physics = str(topology, "physics");
  const std::string method = str(topology, "method");
  const Combination *combo =
      caps.supports(method, physics) ? caps.find(method, physics) : nullptr;
  if (combo) {
    const std::string pair =
        methodLabel(method, &caps) + " with " + physicsLabel(physics, &caps);
    const std::string noLimits =
        "No response limits are offered for " + pair + ".";
    const std::string methodTitle = methodLabel(method, &caps) + " parameters";
    auto group = [&](Group g, const char *id, const char *title,
                     const char *emptyNote, const char *note = nullptr) {
      if (groups & groupBit(g)) {
        if (g == Group::Additive && view.focusManufacturing) {
          scrollCursorToTop();
          view.focusManufacturing = false;
        }
        committed |= drawGroup(g, id, title, topology, *combo, physics, du,
                               view, issues, emptyNote, note, study, additiveControls);
      }
    };
    group(Group::Goal, "goal", "Goal", nullptr);
    group(Group::Limits, "limits", "Response limits", noLimits.c_str());
    group(Group::Convergence, "convergence", "Convergence", nullptr);
    group(Group::Regularization, "filter", "Filter and projection", nullptr);
    group(Group::Method, "method_params", methodTitle.c_str(), nullptr);
    if (mfg) {
      const bool symmetry = (groups & kGroupSymmetry) &&
                            groupOffered(Group::Geometry, *combo);
      const bool additive = (groups & kGroupAdditive) &&
                            (groupOffered(Group::Additive, *combo) || additiveControls);
      group(Group::Geometry, "symmetry", "Symmetry and passive regions",
            nullptr);
      group(Group::Additive, "additive", "Additive manufacturing", nullptr,
            "These settings steer the optimizer; the result's member size, "
            "overhang and voids are measured separately, so this alone does "
            "not certify a printable part.");
      if (!problem && !symmetry && !additive) {
        const std::string none =
            pair + " offers no symmetry, extrusion or additive settings.";
        kit::gapRow();
        if (kit::cardBegin("mfg_none", "Manufacturing"))
          kit::dimText(none.c_str());
        kit::cardEnd();
      }
      if (view.focusManufacturing) {
        ImGui::SetScrollFromPosY(formTop, 0.0f);
        view.focusManufacturing = false;
      }
    }
    committed |= orphanSection(topology, *combo, physics, groups);
  } else {
    if (mfg && !problem) {
      kit::gapRow();
      if (kit::cardBegin("mfg_none", "Manufacturing"))
        kit::dimText("Choose a supported physics and method on the Settings "
                     "step; the manufacturing settings it offers appear here.");
      kit::cardEnd();
    }
    if (view.focusManufacturing) {
      ImGui::SetScrollFromPosY(formTop, 0.0f);
      view.focusManufacturing = false;
    }
  }

  // Whole-setup issues (not tied to a drawn field).
  if (problem) {
    bool first = true;
    for (const Issue &i : issues) {
      if (!i.key.empty())
        continue;
      if (first)
        kit::gapSection();
      first = false;
      kit::inlineStatus(i.message.c_str(), i.severity == Severity::Blocking
                                               ? kit::Tone::Err
                                               : kit::Tone::Warn);
    }
  }
  ImGui::EndDisabled();
  ImGui::PopID();
  return committed;
}

// ---- results
// ------------------------------------------------------------------------------------------
namespace {

std::string runIdOf(const json &run, size_t index) {
  const std::string id = str(run, "run_id");
  return id.empty() ? "#" + std::to_string(index) : id;
}

std::string runTitle(const json &run, size_t index) {
  const std::string label = str(run, "label");
  return label.empty() ? "Run " + std::to_string(index + 1) : label;
}

kit::Tone outcomeTone(Outcome o) {
  switch (o) {
  case Outcome::Converged:
    return kit::Tone::Ok;
  case Outcome::Failed:
  case Outcome::Inconsistent:
    return kit::Tone::Err;
  default:
    return kit::Tone::Warn;
  }
}

kit::Status outcomeStatus(Outcome o) {
  switch (o) {
  case Outcome::Converged:
    return kit::Status::Done;
  case Outcome::Failed:
  case Outcome::Inconsistent:
    return kit::Status::Failed;
  case Outcome::Cancelled:
    return kit::Status::Skipped;
  default:
    return kit::Status::Retry;
  }
}

// Objectives the solver maximizes (a higher final value is an improvement).
bool objectiveMaximized(const std::string &objective) {
  return objective == "frequency" || objective == "buckling_factor";
}

// Change cell: dir > 0 higher is better, dir < 0 lower is better, 0 = no
// judgement (neutral tone). Compact for the narrow pane: the percent change
// ("-48.1%") when the baseline is non-zero, else the signed difference — the
// two values sit beside it. A difference below the shown precision (the
// values print identically) is "No change", never "+14.4 µ".
kit::MetricCell changeCell(double from, double to, int dir) {
  if (!std::isfinite(from) || !std::isfinite(to))
    return {"\xe2\x80\x94", kit::Tone::Neutral};
  if (kit::fmtValue(from) == kit::fmtValue(to))
    return {"No change", kit::Tone::Neutral};
  kit::Tone tone = kit::Tone::Neutral;
  const std::string full = kit::fmtDelta(from, to, "", dir > 0, &tone);
  if (dir == 0)
    tone = kit::Tone::Neutral;
  if (from == 0.0)
    return {full, tone};
  const double pct = (to - from) / std::fabs(from) * 100.0;
  return {std::string(pct > 0.0 ? "+" : "-") + kit::fmtValue(std::fabs(pct), 3) +
              "%",
          tone};
}

struct Series {
  std::string key;
  std::vector<double> y;
};

// Numeric history columns of equal length to the iteration column (or the
// longest column).
void historySeries(const json &history, std::vector<double> &x,
                   std::vector<Series> &out) {
  x.clear();
  out.clear();
  if (!history.is_object())
    return;
  auto numeric = [](const json &a, std::vector<double> &v) {
    v.clear();
    if (!a.is_array())
      return false;
    for (const json &e : a)
      v.push_back(e.is_number() ? e.get<double>() : kNaN);
    return !v.empty();
  };
  const bool hasIter = numeric(member(history, "iteration"), x);
  for (const auto &[k, v] : history.items()) {
    if (k == "iteration")
      continue;
    Series s;
    s.key = k;
    if (!numeric(v, s.y))
      continue;
    if (hasIter && s.y.size() != x.size())
      continue;
    out.push_back(std::move(s));
  }
  if (!hasIter && !out.empty()) {
    const size_t n = out.front().y.size();
    out.erase(std::remove_if(out.begin(), out.end(),
                             [n](const Series &s) { return s.y.size() != n; }),
              out.end());
    for (size_t i = 0; i < n; ++i)
      x.push_back(double(i + 1));
  }
}

std::string resultPhase(const json &result) {
  const std::string p = str(member(result, "summary"), "volume_phase");
  return p.empty() ? volumePhaseFor(str(result, "physics")) : p;
}

std::string seriesLabel(const std::string &key, const std::string &phase) {
  if (key == "objective")
    return "Objective";
  if (key == "volume_fraction")
    return volumeLabel(phase);
  if (key == "change")
    return "Design change";
  if (key == "max_violation")
    return "Max constraint violation";
  return humanize(key);
}

// Headline first (standard §5): the card title IS the outcome; the chips add only what the title
// does not say (feasibility, iterations, run time) and the dim line is the one-line verdict or
// next step — never the headline again. Provenance (formulation, stop reason, criterion) lives
// in the collapsed Run details card.
void outcomeCard(const json &result, const OutcomeSummary &o) {
  if (kit::cardBegin("topo_outcome", o.headline.c_str(), "study_topology")) {
    const json &s = member(result, "summary");
    kit::chipFlowBegin("chips");
    kit::chipFlowItem(ImGui::GetTextLineHeight());
    kit::statusGlyphInline(outcomeStatus(o.outcome));
    if (o.outcome != Outcome::Infeasible && o.outcome != Outcome::Failed) {
      if (!o.feasibilityReported)
        kit::flowChip("Feasibility not reported", kit::Tone::Warn, true,
                      "The solver did not say whether every constraint "
                      "holds, so the design is not certified.");
      else
        kit::flowChip(o.feasible ? "Feasible" : "Infeasible",
                      o.feasible ? kit::Tone::Ok : kit::Tone::Err, true,
                      "Whether every active constraint holds at the final "
                      "design.");
    }
    const double iters = num(s, "iterations");
    if (std::isfinite(iters))
      kit::flowValueChip("Iterations", kit::fmtValue(iters).c_str());
    const double elapsed = num(s, "elapsed_s");
    if (std::isfinite(elapsed))
      kit::flowValueChip("Run time", kit::fmtDuration(elapsed).c_str());
    kit::chipFlowEnd();
    if (!o.detail.empty() && o.detail.rfind(o.headline, 0) != 0)
      kit::dimText(o.detail.c_str());
    const json &mfg = member(result, "manufacturing");
    if (o.computationOk && mfg.contains("geometry_valid") &&
        mfg["geometry_valid"].is_boolean() && !boolTrue(mfg, "geometry_valid"))
      kit::inlineStatus("The reconstructed part failed geometry validation. "
                        "Optimization convergence does not establish a usable solid; "
                        "review the manufacturing checks below.",
                        kit::Tone::Err);
  }
  kit::cardEnd();
}

// Key metrics: baseline vs final of the objective, the volume budget and the
// mass (metricTable folds each unit into its metric label). Iterations and
// run time are outcome chips; the rest of the evidence is in Run details.
void metricsSection(const json &result, const DisplayUnits &du) {
  const json &s = member(result, "summary");
  const std::string physics = str(result, "physics");
  const std::string objective = str(s, "objective");
  kit::sectionHeader("Baseline vs final");
  if (!s.is_object()) {
    kit::dimText("This run reported no summary metrics.");
    return;
  }
  std::vector<kit::MetricRow> rows;
  std::vector<std::string> tips;
  tips.reserve(3); // MetricRow::tooltip points into it (never reallocated)
  auto add = [&](std::string label, double a, double b, const std::string &u,
                 int dir, std::string tip = std::string()) {
    if (!std::isfinite(a) && !std::isfinite(b))
      return;
    kit::MetricRow r;
    r.metric = std::move(label);
    r.unit = kit::unitText(u.c_str());
    r.values = {{kit::fmtValue(a)}, {kit::fmtValue(b)}, changeCell(a, b, dir)};
    tips.push_back(std::move(tip));
    r.tooltip = tips.back().empty() ? nullptr : tips.back().c_str();
    rows.push_back(std::move(r));
  };
  add(objective.empty() ? "Objective" : objectiveLabel(objective, physics),
      num(s, "objective_baseline"), num(s, "objective_final"),
      str(s, "objective_units"), objectiveMaximized(objective) ? 1 : -1);
  const double target = num(s, "volume_fraction_target");
  add(volumeLabel(resultPhase(result)), num(s, "volume_fraction_baseline"),
      num(s, "volume_fraction_final"), "", 0,
      std::isfinite(target) ? "Target " + kit::fmtValue(target) + "."
                            : std::string());
  add("Mass", num(s, "mass_baseline_g") * du.mass,
      num(s, "mass_final_g") * du.mass, du.massL, -1);
  if (rows.empty())
    kit::dimText("This run reported no baseline or final values.");
  else
    kit::metricTable("baseline_final", {"Baseline", "Final", "Change"}, rows);
  // A violated constraint is a key fact, not a detail.
  const double viol = num(s, "max_constraint_violation");
  if (std::isfinite(viol) && viol > 0.0)
    kit::keyValueRowId("violation", "Max constraint violation",
                       kit::fmtValue(viol).c_str(), kit::Tone::Err);
}

// Collapsed provenance of the selected run: the method formulation, why it
// stopped, the convergence rule in words (exact formula on hover), the final
// design change and the solver's remaining scalar evidence.
void runDetailsCard(const json &result, const OutcomeSummary &o,
                    ResultsView &view) {
  const json &s = member(result, "summary");
  if (kit::cardBegin("run_details", "Run details", "results",
                     &view.detailsOpen)) {
    const std::string formulation = str(result, "formulation");
    if (!formulation.empty()) {
      kit::keyValueRowId("formulation", "Method",
                         formulationLabel(formulation).c_str());
      kit::itemTooltip(formulation.c_str());
    }
    if (!o.termination.empty())
      kit::keyValueRowId("termination", "Stop reason",
                         terminationLabel(o.termination).c_str());
    const double change = num(s, "final_change");
    if (std::isfinite(change))
      kit::keyValueRowId("final_change", "Final design change",
                         kit::fmtValue(change).c_str());
    const double viol = num(s, "max_constraint_violation");
    if (std::isfinite(viol) && viol <= 0.0)
      kit::keyValueRowId("violation", "Max constraint violation",
                         kit::fmtValue(viol).c_str(), kit::Tone::Ok);
    const std::string rawCrit = str(s, "convergence_criterion");
    if (!rawCrit.empty()) {
      const std::string crit = criterionText(rawCrit);
      const std::string line =
          "Convergence rule: " +
          (crit.empty() ? std::string("the solver's own rule (hover for it).")
                        : crit);
      kit::dimText(line.c_str());
      kit::itemTooltip(rawCrit.c_str());
    }
    // Remaining scalar evidence (residuals, reactions, conservation, Reynolds
    // number, tolerances, ...).
    static const std::set<std::string> kShown = {"objective",
                                                 "objective_units",
                                                 "objective_baseline",
                                                 "objective_final",
                                                 "volume_phase",
                                                 "volume_fraction_target",
                                                 "volume_fraction_baseline",
                                                 "volume_fraction_final",
                                                 "mass_baseline_g",
                                                 "mass_final_g",
                                                 "iterations",
                                                 "max_constraint_violation",
                                                 "convergence_criterion",
                                                 "final_change",
                                                 "elapsed_s"};
    ImGui::PushID("evidence");
    for (const auto &[k, v] : s.items()) {
      if (kShown.count(k) ||
          !(v.is_number() || v.is_string() || v.is_boolean()))
        continue;
      kit::keyValueRowId(k.c_str(), humanize(k).c_str(), valueText(v).c_str());
    }
    ImGui::PopID();
  }
  kit::cardEnd();
}

void constraintsSection(const json &result) {
  const json &cons = member(result, "constraints");
  kit::sectionHeader("Constraints", nullptr, nullptr,
                     cons.is_array() ? int(cons.size()) : 0);
  if (!cons.is_array() || cons.empty()) {
    kit::dimText(
        "No constraints besides the volume budget were active for this run.");
    return;
  }
  std::vector<std::string> tips;
  tips.reserve(cons.size()); // MetricRow::tooltip points into it
  std::vector<kit::MetricRow> rows;
  int satisfiedN = 0, violatedN = 0, unknownN = 0;
  for (const json &c : cons) {
    std::string name = str(c, "label");
    if (name.empty())
      name = humanize(str(c, "name"));
    const double viol = num(c, "violation");
    bool satisfied = std::isfinite(viol) && viol <= 0.0;
    if (member(c, "satisfied").is_boolean())
      satisfied = boolTrue(c, "satisfied");
    const bool known =
        std::isfinite(viol) || member(c, "satisfied").is_boolean();
    kit::MetricCell status;
    if (!known) {
      status = {"Not reported", kit::Tone::Warn};
      ++unknownN;
    } else if (satisfied) {
      status = {"Satisfied", kit::Tone::Ok};
      ++satisfiedN;
    } else {
      status = {"Violated", kit::Tone::Err};
      ++violatedN;
    }
    tips.push_back(std::isfinite(viol)
                       ? "Violation g = " + kit::fmtValue(viol) +
                             " (g <= 0 is satisfied)."
                       : std::string());
    kit::MetricRow r;
    r.metric = std::move(name);
    r.unit = kit::unitText(str(c, "units").c_str());
    r.values = {{kit::fmtValue(num(c, "limit"))},
                {kit::fmtValue(num(c, "value"))},
                std::move(status)};
    r.tooltip = tips.back().empty() ? nullptr : tips.back().c_str();
    rows.push_back(std::move(r));
  }
  kit::chipFlowBegin("constraint_chips");
  if (satisfiedN > 0)
    kit::flowChip((std::to_string(satisfiedN) + " satisfied").c_str(),
                  kit::Tone::Ok, true);
  if (violatedN > 0)
    kit::flowChip((std::to_string(violatedN) + " violated").c_str(),
                  kit::Tone::Err, true);
  if (unknownN > 0)
    kit::flowChip((std::to_string(unknownN) + " not reported").c_str(),
                  kit::Tone::Warn, true);
  kit::chipFlowEnd();
  kit::metricTable("constraints", {"Limit", "Final", "Status"}, rows,
                   "Constraint");
}

void historySection(const json &result, ResultsView &view) {
  kit::sectionHeader("Convergence history");
  std::vector<double> x;
  std::vector<Series> series;
  historySeries(member(result, "history"), x, series);
  if (series.empty()) {
    kit::dimText("This run recorded no iteration history.");
    return;
  }
  int sel = -1;
  for (size_t i = 0; i < series.size(); ++i)
    if (series[i].key == view.plotSeries)
      sel = int(i);
  if (sel < 0) {
    sel = 0;
    for (size_t i = 0; i < series.size(); ++i)
      if (series[i].key == "objective")
        sel = int(i);
    view.plotSeries = series[size_t(sel)].key;
  }
  const std::string phase = resultPhase(result);
  if (series.size() > 1) {
    std::vector<std::string> names;
    names.reserve(series.size());
    for (const Series &s : series)
      names.push_back(seriesLabel(s.key, phase));
    kit::FieldGroup group("history", "Series");
    if (kit::fieldCombo("series", "Series", &sel, names,
                        kit::FieldOpts().withTip(
                            "Which recorded quantity to plot per iteration.")))
      view.plotSeries = series[size_t(sel)].key;
  }
  const Series &shown = series[size_t(sel)];
  const std::string label = seriesLabel(shown.key, phase);
  const json &summary = member(result, "summary");
  const std::string units =
      shown.key == "objective"
          ? std::string(kit::unitText(str(summary, "objective_units").c_str()))
          : std::string();

  kit::PlotSpec p;
  p.x = kit::PlotData(x);
  kit::PlotSeries line;
  line.label = label.c_str();
  line.y = kit::PlotData(shown.y);
  p.series.push_back(line);
  p.xAxis.title = "Iteration";
  p.xAxis.integer = true;
  p.xAxis.zero = false;
  p.yAxis.title = label.c_str();
  p.yAxis.unit = units.empty() ? nullptr : units.c_str();
  p.legend = false;
  p.emptyText = "Fewer than two finite samples were recorded for this series.";
  if (shown.key == "volume_fraction") {
    const double target = num(summary, "volume_fraction_target");
    if (std::isfinite(target))
      p.refs.push_back(kit::PlotRef::h(target, "Target"));
  } else if (shown.key == "max_violation") {
    p.refs.push_back(kit::PlotRef::h(0.0, "Feasible at or below"));
  }
  kit::linePlot("history_plot", p);
}

void manufacturingSection(const json &result, const json &settings) {
  kit::sectionHeader("Manufacturing");
  const ManufacturingReport rep = manufacturingReport(result, settings);
  kit::chipFlowBegin("mfg_chips");
  kit::flowChip(rep.geometryValidated ? "Surface validated"
                                      : "Surface not validated",
                rep.geometryValidated ? kit::Tone::Ok : kit::Tone::Warn, true,
                "Closed, oriented, manifold result surface.");
  kit::flowChip(!rep.dfamChecked  ? "DFAM not run"
                : rep.dfamPassed ? "DFAM passed"
                                 : "DFAM failed",
                !rep.dfamChecked  ? kit::Tone::Warn
                : rep.dfamPassed ? kit::Tone::Ok
                                 : kit::Tone::Err,
                true, "Design-for-additive checks on the result geometry.");
  kit::chipFlowEnd();
  const json &dfamChecks = member(member(member(result, "manufacturing"), "dfam"), "checks");
  if (dfamChecks.is_array() && !dfamChecks.empty()) {
    // Check | Status fits the side pane; the notes of failed and warned checks follow as
    // wrapped lines (every note is also the row tooltip).
    std::vector<kit::MetricRow> rows;
    std::vector<std::pair<std::string, kit::Tone>> findings;
    rows.reserve(dfamChecks.size());
    for (const auto &check : dfamChecks) {
      const std::string status = str(check, "status");
      kit::MetricRow row;
      row.metric = humanize(str(check, "check"));
      const kit::Tone tone = status == "pass"   ? kit::Tone::Ok
                             : status == "fail" ? kit::Tone::Err
                             : status == "warn" ? kit::Tone::Warn
                                                : kit::Tone::Neutral;
      row.values = {{humanize(status), tone}};
      const json &note = member(check, "note");
      if (note.is_string()) {
        row.tooltip = note.get_ref<const std::string &>().c_str();
        if (status == "fail" || status == "warn")
          findings.emplace_back(row.metric + ": " + note.get<std::string>(), tone);
      }
      rows.push_back(std::move(row));
    }
    kit::metricTable("dfam_checks", {"Status"}, rows, "DFAM check");
    for (const auto &[text, tone] : findings)
      kit::inlineStatus(text.c_str(), tone);
  }
  if (!rep.rows.empty()) {
    std::vector<kit::MetricRow> rows;
    rows.reserve(rep.rows.size());
    for (const ManufacturingRow &r : rep.rows) {
      kit::MetricRow m;
      m.metric = r.label;
      m.unit = kit::unitText(r.units.c_str());
      kit::MetricCell check;
      if (r.status == CheckStatus::Pass)
        check = {"Pass", kit::Tone::Ok};
      else if (r.status == CheckStatus::Fail)
        check = {"Fail", kit::Tone::Err};
      else
        check = {"Not measured", kit::Tone::Warn};
      m.values = {{r.requested.is_null() ? "\xe2\x80\x94"
                                         : valueText(r.requested)},
                  {r.measured.is_null() ? "\xe2\x80\x94"
                                        : valueText(r.measured)},
                  std::move(check)};
      m.tooltip = r.note.empty() ? nullptr : r.note.c_str();
      rows.push_back(std::move(m));
    }
    kit::metricTable("mfg_checks", {"Requested", "Measured", "Check"}, rows,
                     "Check");
  }
  kit::inlineStatus(rep.printReadyReason.c_str(),
                    rep.printReady ? kit::Tone::Ok : kit::Tone::Warn);
}

// One setting of a run snapshot as display text ("0.3", "12 mm", "Default").
std::string settingText(const KeySpec &s, const json &settings,
                        const DisplayUnits &du) {
  const auto it = settings.find(s.key);
  if (it == settings.end())
    return "Default";
  return it->is_number() ? quantity(it->get<double>() * unitFactor(s.unit, du),
                                    unitLabel(s.unit, du))
                         : valueText(*it);
}

// The Compare view (ribbon Compare): its own page, not a copy of Results.
// Pick the compared run and its baseline, then see the outcome and final
// metrics side by side (compact change column) and which settings differ.
// Callers guarantee runs.size() >= 2.
void compareView(const json &runs, size_t sel, const DisplayUnits &du,
                 ResultsView &view, ResultsAction &act) {
  if (view.focusCompare) {
    scrollCursorToTop();
    view.focusCompare = false;
  }
  kit::sectionHeader("Compare runs", "plots_view", nullptr, -1, true);
  // Baseline defaults to the run before the compared one.
  int base = -1;
  for (size_t i = 0; i < runs.size(); ++i)
    if (i != sel && runIdOf(runs[i], i) == view.baselineRunId)
      base = int(i);
  if (base < 0) {
    base = sel > 0 ? int(sel - 1) : 1;
    view.baselineRunId = runIdOf(runs[size_t(base)], size_t(base));
  }
  {
    kit::FieldGroup group("compare", "Compared run");
    std::vector<std::string> all;
    all.reserve(runs.size());
    for (size_t i = 0; i < runs.size(); ++i)
      all.push_back(runTitle(runs[i], i));
    int cur = int(sel);
    if (kit::fieldCombo("compared", "Compared run", &cur, all,
                        kit::FieldOpts().withTip(
                            "The run shown in the viewport and compared "
                            "against the baseline.")) &&
        cur >= 0 && size_t(cur) != sel) {
      sel = size_t(cur);
      view.selectedRunId = runIdOf(runs[sel], sel);
      act.kind = ResultsAction::Kind::ShowRun;
      act.runId = view.selectedRunId;
      if (int(sel) == base) { // never compare a run with itself
        base = sel > 0 ? int(sel - 1) : 1;
        view.baselineRunId = runIdOf(runs[size_t(base)], size_t(base));
      }
    }
    std::vector<std::string> names;
    std::vector<size_t> index;
    int pick = -1;
    for (size_t i = 0; i < runs.size(); ++i) {
      if (i == sel)
        continue;
      if (int(i) == base)
        pick = int(names.size());
      names.push_back(runTitle(runs[i], i));
      index.push_back(i);
    }
    if (kit::fieldCombo("baseline", "Baseline", &pick, names,
                        kit::FieldOpts().withTip(
                            "The run the compared run is measured against.")) &&
        pick >= 0) {
      base = int(index[size_t(pick)]);
      view.baselineRunId = runIdOf(runs[size_t(base)], size_t(base));
    }
  }

  const json &baseRun = runs[size_t(base)];
  const json &selRun = runs[sel];
  const OutcomeSummary ob = classifyResult(runResult(baseRun));
  const OutcomeSummary os = classifyResult(runResult(selRun));
  const std::vector<MetricDelta> deltas = compareRuns(baseRun, selRun);
  const bool maximize = objectiveMaximized(runObjective(selRun));
  std::vector<kit::MetricRow> rows;
  rows.reserve(deltas.size() + 1);
  {
    kit::MetricRow r;
    r.metric = "Outcome";
    r.values = {{ob.headline, outcomeTone(ob.outcome)},
                {os.headline, outcomeTone(os.outcome)},
                {""}};
    rows.push_back(std::move(r));
  }
  for (const MetricDelta &d : deltas) {
    int dir = 0;
    if (d.key == "objective_final")
      dir = maximize ? 1 : -1;
    else if (d.key == "mass_final_g" || d.key == "max_constraint_violation" ||
             d.key == "elapsed_s")
      dir = -1;
    kit::MetricRow r;
    r.metric = d.label;
    const bool mass = d.key == "mass_final_g";
    r.unit = mass ? du.massL.c_str() : kit::unitText(d.units.c_str());
    r.values = {{kit::fmtValue(d.baseline * (mass ? du.mass : 1.0))},
                {kit::fmtValue(d.selected * (mass ? du.mass : 1.0))},
                d.comparable ? changeCell(d.baseline, d.selected, dir)
                             : kit::MetricCell{"n/a", kit::Tone::Neutral}};
    r.tooltip = d.note.empty() ? nullptr : d.note.c_str();
    rows.push_back(std::move(r));
  }
  kit::metricTable("compare", {"Baseline", "Compared", "Change"}, rows);

  // Which settings differ: the reason the two runs differ.
  const json &sa = runSettings(baseRun);
  const json &sb = runSettings(selRun);
  kit::sectionHeader("Settings that differ", "settings");
  if (!sa.is_object() || !sb.is_object()) {
    kit::dimText("A run did not retain its settings snapshot.");
  } else {
    const std::string physics = str(sb, "physics");
    std::vector<kit::MetricRow> diff;
    auto addDiff = [&](std::string label, std::string a, std::string b) {
      if (a == b)
        return;
      kit::MetricRow r;
      r.metric = std::move(label);
      r.values = {{std::move(a)}, {std::move(b)}};
      diff.push_back(std::move(r));
    };
    addDiff("Physics", physicsLabel(str(sa, "physics")), physicsLabel(physics));
    addDiff("Method", methodLabel(str(sa, "method")),
            methodLabel(str(sb, "method")));
    addDiff("Objective",
            objectiveLabel(str(sa, "objective"), str(sa, "physics")),
            objectiveLabel(str(sb, "objective"), physics));
    for (const KeySpec &s : kSpecs)
      if (sa.contains(s.key) || sb.contains(s.key))
        addDiff(keyLabel(s, physics), settingText(s, sa, du),
                settingText(s, sb, du));
    if (diff.empty())
      kit::dimText("Both runs used the same settings (the geometry or face "
                   "selections may differ).");
    else
      kit::metricTable("setting_diff", {"Baseline", "Compared"}, diff,
                       "Setting");
  }
  kit::gapRow();
  if (kit::secondaryButton("Back to results"))
    view.compareMode = false;
}

void setupSection(const json &run, const std::string &runId,
                  const DisplayUnits &du, ResultsView &view,
                  ResultsAction &act) {
  const json &settings = runSettings(run);
  if (kit::cardBegin("run_setup", "Setup of this run", "settings",
                     &view.setupOpen)) {
    if (!settings.is_object()) {
      kit::dimText("This run did not retain its settings snapshot.");
    } else {
      const std::string physics = str(settings, "physics");
      kit::keyValueRowId("physics", "Physics", physicsLabel(physics).c_str());
      kit::keyValueRowId("method", "Method",
                         methodLabel(str(settings, "method")).c_str());
      kit::keyValueRowId(
          "objective", "Objective",
          objectiveLabel(str(settings, "objective"), physics).c_str());
      for (const KeySpec &s : kSpecs) {
        const auto it = settings.find(s.key);
        if (it == settings.end())
          continue;
        const std::string v =
            it->is_number()
                ? quantity(it->get<double>() * unitFactor(s.unit, du),
                           unitLabel(s.unit, du))
                : valueText(*it);
        kit::keyValueRowId(s.key, keyLabel(s, physics).c_str(), v.c_str());
      }
      if (kit::secondaryButton("Use these settings")) {
        act.kind = ResultsAction::Kind::ApplyRunSettings;
        act.runId = runId;
      }
      kit::itemTooltip("Copy this run's method, physics, objective and scalar "
                       "settings into the study. Face selections and other "
                       "study data stay as they are.");
    }
  }
  kit::cardEnd();
}

void artifactsSection(AppState &st, const json &result, const std::string &runId,
                      ResultsAction &act) {
  kit::sectionHeader("Result geometry and export");
  kit::dimText("Continue in CAD creates a separate faceted solid for adding or "
               "cutting features. It does not recover sketches or a smooth "
               "parametric feature history. The source part and study stay unchanged.");
  const json &arts = member(result, "artifacts");
  const bool geometryValid =
      boolTrue(member(result, "manufacturing"), "geometry_valid");
  const json &printSurface = member(member(result, "reconstruction"), "print_surface");
  if (printSurface.is_object()) {
    std::string evidence = humanize(str(printSurface, "state"));
    const double iterations = num(printSurface, "iterations_used");
    const double volumeChange = num(printSurface, "volume_change_fraction");
    if (std::isfinite(iterations))
      evidence += " \xc2\xb7 " + kit::fmtValue(iterations) + " smoothing iterations";
    if (std::isfinite(volumeChange))
      evidence += " \xc2\xb7 volume change " + kit::fmtValue(volumeChange * 100.0) + "%";
    kit::keyValueRowId("print_surface", "Print surface", evidence.c_str());
  }
  const std::string surfaceKey = surfaceArtifactKey(result);
  const json &surface = member(arts, surfaceKey.c_str());
  const char *cadBlocker =
      !geometryValid ? "This run's reconstructed surface did not pass geometry validation."
      : str(surface, "path").empty() ? "This run retained no solid surface."
      : boolTrue(surface, "missing") ? "The surface file is missing on disk."
      : boolTrue(surface, "stale") ? "The surface no longer matches this run."
      : st.rpc.interactionBusy() ? "Wait for the current engine request."
                     : nullptr;
  if (kit::primaryButton("Continue in CAD", cadBlocker == nullptr, cadBlocker)) {
    act.kind = ResultsAction::Kind::ContinueInCad;
    act.runId = runId;
    act.artifactKey = surfaceKey;
  }
  bool viewable = false;
  std::string viewReason = "This run retained no viewport overlay.";
  if (arts.is_object()) {
    const float exportW = buttonWidth("Export");
    const auto drawArtifact = [&](const std::string &key, const json &a) {
      if (!a.is_object())
        return;
      const std::string format = str(a, "format");
      const bool missing = boolTrue(a, "missing");
      const bool stale = boolTrue(a, "stale");
      if (format == "aicm" || key == "overlay_aicm") {
        viewable = !missing && !stale;
        if (missing)
          viewReason = "The viewport overlay file is missing.";
        if (stale)
          viewReason = "The viewport overlay changed since the run.";
        return; // a viewport handoff, not an export product
      }
      std::string title = key == "surface_print_stl"
                              ? "Print surface (smoothed)" : str(a, "label");
      if (title.empty())
        title = humanize(key);
      std::string secondary = format;
      const double bytes = num(a, "bytes");
      if (std::isfinite(bytes) && bytes >= 0.0) {
        if (!secondary.empty())
          secondary += " \xc2\xb7 ";
        secondary += kit::fmtBytes((unsigned long long)bytes);
      }
      const std::string sha = str(a, "sha256");
      if (sha.size() >= 12) {
        if (!secondary.empty())
          secondary += " \xc2\xb7 ";
        secondary += "sha256 " + sha.substr(0, 12);
      }
      const std::string path = str(a, "path");
      // A surface mesh is only offered as usable once the solver validated it
      // (closed, oriented, manifold); the volume field and evidence files are
      // always exportable.
      const bool surface =
          format == "stl" || format == "3mf" || format == "obj";
      const bool unvalidated = surface && !geometryValid;
      const char *why =
          missing ? "The file is missing on disk."
          : stale ? "The file changed since the run (hash mismatch)."
          : unvalidated
              ? "The surface failed or skipped geometry validation (closed, "
                "manifold, oriented), so it is not offered as a usable part."
              : nullptr;
      kit::RowStyle rs;
      rs.secondary = secondary.empty() ? nullptr : secondary.c_str();
      rs.tooltip = path.empty() ? nullptr : path.c_str();
      rs.disabled = why != nullptr;
      bool exportIt = false;
      rowWithActions(key.c_str(), title.c_str(), rs, exportW, [&] {
        exportIt = kit::secondaryButton("Export", why == nullptr, why);
      });
      if (exportIt) {
        act.kind = ResultsAction::Kind::ExportArtifact;
        act.runId = runId;
        act.artifactKey = key;
      }
    };
    const auto print = arts.find("surface_print_stl");
    if (print != arts.end())
      drawArtifact(print.key(), print.value());
    for (const auto &[key, artifact] : arts.items())
      if (key != "surface_print_stl")
        drawArtifact(key, artifact);
  }
  if (!str(result, "overlay_aicm").empty())
    viewable = true;
  if (kit::secondaryButton("Show in viewport", viewable, viewReason.c_str())) {
    act.kind = ResultsAction::Kind::ShowRun;
    act.runId = runId;
  }
}

// Collapsed card of a record's scalar members; copy ids derive from the json
// key inside the card's own id scope.
void scalarsCard(const char *id, const char *title, const char *icon,
                 const json &obj, bool *open) {
  if (!obj.is_object())
    return;
  bool any = false;
  for (const auto &[k, v] : obj.items())
    any = any || v.is_number() || v.is_string() || v.is_boolean();
  if (!any)
    return;
  if (kit::cardBegin(id, title, icon, open)) {
    for (const auto &[k, v] : obj.items()) {
      if (!(v.is_number() || v.is_string() || v.is_boolean()))
        continue;
      kit::keyValueRowId(k.c_str(), humanize(k).c_str(), valueText(v).c_str(),
                         kit::Tone::Neutral, v.is_string());
    }
  }
  kit::cardEnd();
}

} // namespace

ResultsAction drawTopologyResults(AppState &st, const json &runs,
                                  const Capabilities &caps, ResultsView &view,
                                  const std::function<void()> &displayControls) {
  ResultsAction act;
  ImGui::PushID("topology_results");
  if (!runs.is_array() || runs.empty()) {
    kit::emptyState("study_topology", "No topology runs yet",
                    "Run the study to see convergence, constraints and "
                    "manufacturing checks here. Every run is kept for "
                    "comparison.");
    ImGui::PopID();
    return act;
  }

  size_t sel = runs.size() - 1;
  bool found = false;
  for (size_t i = 0; i < runs.size(); ++i)
    if (runIdOf(runs[i], i) == view.selectedRunId) {
      sel = i;
      found = true;
    }
  if (!found)
    view.selectedRunId = runIdOf(runs[sel], sel);

  if (view.compareMode && runs.size() >= 2) {
    compareView(runs, sel, displayUnits(st), view, act);
    ImGui::PopID();
    return act;
  }
  view.focusCompare = false; // only the Compare view consumes it

  // Headline first (standard §5): the selected run's outcome and key metrics,
  // then the caller's display controls, then the run list and the details.
  const json &run = runs[sel];
  const json &result = runResult(run);
  const std::string runId = runIdOf(run, sel);
  const OutcomeSummary o = classifyResult(result);
  outcomeCard(result, o);
  if (result.is_object())
    metricsSection(result, displayUnits(st));
  if (displayControls) {
    kit::gapRow();
    displayControls();
  }

  kit::sectionHeader("Runs", nullptr, nullptr, int(runs.size()));
  for (size_t i = runs.size(); i-- > 0;) { // newest first
    const json &r = runs[i];
    const OutcomeSummary ro = classifyResult(runResult(r));
    const std::string id = runIdOf(r, i);
    const std::string title = runTitle(r, i);
    // Recorded labels already name the method and physics ("Run 2 — SIMP,
    // Linear structural"); only a bare "Run N" gets them on the second line.
    const std::string secondary =
        str(r, "label").empty()
            ? methodLabel(str(runSettings(r), "method"), &caps) + " \xc2\xb7 " +
                  physicsLabel(runPhysics(r), &caps) + " \xc2\xb7 " + ro.headline
            : ro.headline;
    const double started = num(r, "started_utc");
    const std::string meta =
        std::isfinite(started) ? kit::fmtAgo(started) : std::string();
    kit::RowStyle rs;
    rs.icon = "study_topology";
    rs.secondary = secondary.c_str();
    rs.meta = meta.empty() ? nullptr : meta.c_str();
    rs.selected = i == sel;
    rs.iconTone = outcomeTone(ro.outcome);
    ImGui::PushID(int(i));
    if (kit::row("run", title.c_str(), rs) && i != sel) {
      view.selectedRunId = id;
      act.kind = ResultsAction::Kind::ShowRun;
      act.runId = id;
    }
    ImGui::PopID();
  }
  if (runs.size() >= 2 && kit::secondaryButton("Compare runs")) {
    view.compareMode = true;
    view.focusCompare = true;
  }
  if (runs.size() >= 2)
    kit::itemTooltip("Put two runs side by side: outcome, final metrics and "
                     "the settings that differ.");

  if (result.is_object()) {
    constraintsSection(result);
    historySection(result, view);
    manufacturingSection(result, runSettings(run));
  }
  if (result.is_object())
    artifactsSection(st, result, runId, act);
  kit::gapSection();
  if (result.is_object())
    runDetailsCard(result, o, view);
  setupSection(run, runId, displayUnits(st), view, act);
  if (result.is_object()) {
    scalarsCard("mesh_quality", "Mesh quality", "mesh",
                member(result, "mesh_quality"), &view.meshOpen);
    scalarsCard("provenance", "Provenance", "history",
                member(result, "provenance"), &view.provenanceOpen);
  }
  ImGui::PopID();
  return act;
}

} // namespace topology
} // namespace ui
#endif // CHUDWARE_SIM_TOPOLOGY_MODEL_ONLY
