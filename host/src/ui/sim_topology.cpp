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
     "favours. 0 disables "
     "filtering."},
    {"projection_beta", Group::Regularization, Kind::Real, Unit::None, 0.0,
     false, kInf, false, "Projection sharpness (beta)",
     "Heaviside projection steepness. 0 disables projection."},
    {"density_threshold", Group::Regularization, Kind::Real, Unit::Fraction,
     0.0, true, 1.0, true, "Solid threshold",
     "Density at which the result surface is extracted and the thresholded "
     "design is "
     "re-analysed."},
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
                                    const Capabilities &caps) {
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
  const bool hasDir = topology.contains("build_direction");
  if (topology.contains("overhang_angle_deg") && !hasDir)
    block("build_direction", "Overhang control needs a build direction.");
  const double pen = num(topology, "overhang_penalty");
  if (std::isfinite(pen) && pen > 0.0) {
    if (!hasDir)
      block("build_direction", "Overhang control needs a build direction.");
    if (!topology.contains("overhang_angle_deg"))
      block("overhang_angle_deg",
            "An overhang penalty needs an overhang limit angle.");
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
      o.detail = "The solver reported convergence but terminated with '" +
                 o.termination + "'.";
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
    o.detail = "Stopped at the iteration cap before the convergence criterion "
               "was met.";
    return o;
  }
  if (claimedConverged) {
    o.outcome = Outcome::Converged;
    o.converged = true;
    o.headline = "Converged";
    const std::string crit = str(summary, "convergence_criterion");
    o.detail = crit.empty() ? "Converged with every constraint satisfied."
                            : "Converged: " + crit + ".";
    return o;
  }
  o.outcome = Outcome::NotConverged;
  o.headline = "Not converged";
  if (!why.empty())
    o.detail = why;
  else if (!o.termination.empty())
    o.detail = "Terminated with '" + o.termination +
               "' before meeting the convergence criterion.";
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
  add("objective_final", "Final objective", ub.empty() ? ua : ub,
      objNote.empty(), objNote);
  const std::string fa = runPhase(baselineRun), fb = runPhase(selectedRun);
  add("volume_fraction_final",
      fa == fb ? volumeLabel(fb) + " (final)" : "Final volume fraction", "",
      fa == fb,
      fa == fb ? std::string()
               : "One run budgets solid volume, the other fluid volume.");
  add("mass_final_g", "Final mass", "g", true, "");
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
#include <cfloat>

#include "imgui.h"

#include "metrics.h"
#include "sim_manager.h" // SimManager::unitSys — the Simulate workspace's display units
#include "widgets/kit.h"

namespace ui {
namespace topology {
namespace {

namespace kit = ui::kit;

struct DisplayUnits {
  double len = 1.0, stress = 1.0, pressPa = 1.0;
  std::string lenL = "mm", stressL = "MPa", pressL = "Pa";
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

std::string unitLabel(Unit u, const DisplayUnits &d) {
  switch (u) {
  case Unit::Length:
    return d.lenL;
  case Unit::Stress:
    return d.stressL;
  case Unit::PressurePa:
    return d.pressL;
  case Unit::TempC:
    return "degC";
  case Unit::Hz:
    return "Hz";
  case Unit::Deg:
    return "deg";
  default:
    return {};
  }
}

void hoverTip(const char *tip) {
  if (tip && *tip && ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled))
    ImGui::SetTooltip("%s", tip);
}

void fieldLabel(const std::string &text, const char *tip) {
  ImGui::TextUnformatted(text.c_str());
  hoverTip(tip);
}

std::string trim(const char *s) {
  std::string t = s ? s : "";
  const auto b = t.find_first_not_of(" \t");
  if (b == std::string::npos)
    return {};
  const auto e = t.find_last_not_of(" \t");
  return t.substr(b, e - b + 1);
}

bool parseReal(const std::string &s, double &out) {
  if (s.empty())
    return false;
  errno = 0;
  char *end = nullptr;
  out = std::strtod(s.c_str(), &end);
  return errno == 0 && end == s.c_str() + s.size() && std::isfinite(out);
}

bool parseWhole(const std::string &s, long long &out) {
  if (s.empty())
    return false;
  errno = 0;
  char *end = nullptr;
  out = std::strtoll(s.c_str(), &end, 10);
  return errno == 0 && end == s.c_str() + s.size();
}

void keyIssues(const std::vector<Issue> &issues, const std::string &key) {
  for (const Issue &i : issues)
    if (i.key == key)
      kit::inlineStatus(i.message.c_str(), i.severity == Severity::Blocking
                                               ? kit::Tone::Err
                                               : kit::Tone::Warn);
}

void fieldError(const SettingsView &view, const char *key) {
  const auto it = view.fieldErrors.find(key);
  if (it != view.fieldErrors.end())
    kit::inlineStatus(it->second.c_str(), kit::Tone::Err);
}

// One-shot navigation: puts the current cursor line at the top of the caller's
// scrolling child on the next frame (clamped to its scroll range).
void scrollCursorToTop() {
  ImGui::SetScrollFromPosY(
      ImGui::GetCursorScreenPos().y - ImGui::GetWindowPos().y, 0.0f);
}

std::string defaultHint(const Combination &c, const KeySpec &s, double factor) {
  const json &d = member(c.defaults, s.key);
  if (d.is_number())
    return "solver default " + fmtG(d.get<double>() * factor);
  return "solver default";
}

// Commits a finished text edit. Empty text erases the key; unchanged text never
// commits.
bool commitText(const KeySpec &s, const std::string &text, double factor,
                json &topo, SettingsView &view) {
  view.fieldErrors.erase(s.key);
  const auto it = topo.find(s.key);
  const bool present = it != topo.end();
  if (text.empty()) {
    if (!present)
      return false;
    topo.erase(s.key);
    return true;
  }
  if (s.kind == Kind::Integer) {
    long long v = 0;
    if (!parseWhole(text, v)) {
      view.fieldErrors[s.key] = "Enter a whole number.";
      return false;
    }
    if (present && it->is_number() && it->get<double>() == double(v))
      return false;
    topo[s.key] = v;
    return true;
  }
  double v = 0.0;
  if (!parseReal(text, v)) {
    view.fieldErrors[s.key] = "Enter a number.";
    return false;
  }
  // A focus round trip re-parses the displayed (rounded) text; keep the stored
  // full precision.
  if (present && it->is_number() && fmtG(it->get<double>() * factor) == text)
    return false;
  topo[s.key] = v / factor;
  return true;
}

bool numberField(const KeySpec &s, json &topo, const Combination &c,
                 const std::string &physics, const DisplayUnits &du,
                 SettingsView &view, const std::vector<Issue> &issues) {
  const double factor = unitFactor(s.unit, du);
  std::string label = keyLabel(s, physics, c.volumePhase);
  const std::string unit = unitLabel(s.unit, du);
  if (!unit.empty())
    label += " (" + unit + ")";
  fieldLabel(label, s.tip);

  const bool editing = view.editingKey == s.key;
  std::string text;
  if (editing) {
    text = view.editText;
  } else if (const auto it = topo.find(s.key); it != topo.end()) {
    text = it->is_number() ? fmtG(it->get<double>() * factor) : it->dump();
  }
  char buf[64];
  std::snprintf(buf, sizeof(buf), "%s", text.c_str());
  const std::string hint = defaultHint(c, s, factor);

  ImGui::PushID(s.key);
  ImGui::SetNextItemWidth(-FLT_MIN);
  ImGui::InputTextWithHint("##v", hint.c_str(), buf, sizeof(buf),
                           s.kind == Kind::Integer
                               ? ImGuiInputTextFlags_CharsDecimal
                               : ImGuiInputTextFlags_CharsScientific);
  hoverTip(s.tip);
  bool committed = false;
  if (ImGui::IsItemActive()) {
    view.editingKey = s.key;
    view.editText = buf;
  } else if (ImGui::IsItemDeactivated()) {
    view.editingKey.clear();
    committed = commitText(s, trim(buf), factor, topo, view);
  } else if (editing) {
    view.editingKey
        .clear(); // the field lost focus without a deactivation (hidden)
  }
  ImGui::PopID();
  fieldError(view, s.key);
  keyIssues(issues, s.key);
  return committed;
}

bool boolField(const KeySpec &s, json &topo, const std::vector<Issue> &issues) {
  const auto it = topo.find(s.key);
  bool v = it != topo.end() && it->is_boolean() && it->get<bool>();
  bool committed = false;
  ImGui::PushID(s.key);
  if (ImGui::Checkbox(s.label, &v)) {
    topo[s.key] = v;
    committed = true;
  }
  hoverTip(s.tip);
  ImGui::PopID();
  keyIssues(issues, s.key);
  return committed;
}

const char *const kAxisIds[3] = {"x", "y", "z"};
const char *const kAxisNames[3] = {"X", "Y", "Z"};

bool axisSetField(const KeySpec &s, json &topo,
                  const std::vector<Issue> &issues) {
  fieldLabel(s.label, s.tip);
  bool on[3] = {false, false, false};
  if (const auto it = topo.find(s.key); it != topo.end() && it->is_array())
    for (const json &e : *it)
      for (int i = 0; i < 3; ++i)
        if (e.is_string() && e.get<std::string>() == kAxisIds[i])
          on[i] = true;
  bool changed = false;
  ImGui::PushID(s.key);
  for (int i = 0; i < 3; ++i) {
    if (i)
      ImGui::SameLine();
    changed |= kit::toggleChip(kAxisNames[i], &on[i], kit::Tone::Accent, s.tip);
  }
  ImGui::PopID();
  if (changed) {
    json arr = json::array();
    for (int i = 0; i < 3; ++i)
      if (on[i])
        arr.push_back(kAxisIds[i]);
    if (arr.empty())
      topo.erase(s.key);
    else
      topo[s.key] = std::move(arr);
  }
  keyIssues(issues, s.key);
  return changed;
}

bool axisField(const KeySpec &s, json &topo, const std::vector<Issue> &issues) {
  fieldLabel(s.label, s.tip);
  static const char *const labels[4] = {"None", "X", "Y", "Z"};
  int sel = 0;
  const std::string cur = str(topo, s.key);
  for (int i = 0; i < 3; ++i)
    if (cur == kAxisIds[i])
      sel = i + 1;
  bool changed = false;
  if (kit::segmented(s.key, labels, 4, &sel,
                     ImGui::GetContentRegionAvail().x)) {
    if (sel == 0)
      topo.erase(s.key);
    else
      topo[s.key] = kAxisIds[sel - 1];
    changed = true;
  }
  keyIssues(issues, s.key);
  return changed;
}

bool directionField(const KeySpec &s, json &topo, SettingsView &view,
                    const std::vector<Issue> &issues) {
  fieldLabel(s.label, s.tip);
  static const double kPresets[6][3] = {{1, 0, 0},  {-1, 0, 0}, {0, 1, 0},
                                        {0, -1, 0}, {0, 0, 1},  {0, 0, -1}};
  double cur[3] = {0.0, 0.0, 0.0};
  const auto it = topo.find(s.key);
  bool present = false;
  if (it != topo.end() && it->is_array() && it->size() == 3) {
    present = true;
    for (int i = 0; i < 3; ++i)
      cur[i] = (*it)[i].is_number() ? (*it)[i].get<double>() : 0.0;
  }
  int sel = 0;
  for (int p = 0; p < 6 && present; ++p)
    if (cur[0] == kPresets[p][0] && cur[1] == kPresets[p][1] &&
        cur[2] == kPresets[p][2])
      sel = p + 1;
  const char *labels[7] = {(present && sel == 0) ? "Custom" : "None",
                           "+X",
                           "-X",
                           "+Y",
                           "-Y",
                           "+Z",
                           "-Z"};
  bool committed = false;
  if (kit::segmented(s.key, labels, 7, &sel,
                     ImGui::GetContentRegionAvail().x)) {
    if (sel == 0)
      topo.erase(s.key);
    else
      topo[s.key] = json::array(
          {kPresets[sel - 1][0], kPresets[sel - 1][1], kPresets[sel - 1][2]});
    view.directionEditing = view.directionDirty = false;
    view.fieldErrors.erase(s.key);
    committed = true;
  }

  if (!view.directionEditing)
    for (int i = 0; i < 3; ++i)
      view.direction[i] = cur[i];
  const float spacing = ImGui::GetStyle().ItemInnerSpacing.x;
  const float w = (ImGui::GetContentRegionAvail().x - spacing * 2.0f) / 3.0f;
  bool anyActive = false;
  ImGui::PushID(s.key);
  for (int i = 0; i < 3; ++i) {
    if (i)
      ImGui::SameLine(0.0f, spacing);
    ImGui::PushID(i);
    ImGui::SetNextItemWidth(w);
    if (ImGui::InputDouble("##c", &view.direction[i], 0.0, 0.0, "%.4g"))
      view.directionDirty = true;
    anyActive |= ImGui::IsItemActive();
    ImGui::PopID();
  }
  ImGui::PopID();
  if (anyActive) {
    view.directionEditing = true;
  } else if (view.directionEditing) {
    view.directionEditing = false;
    if (view.directionDirty) {
      view.directionDirty = false;
      const double n = std::sqrt(view.direction[0] * view.direction[0] +
                                 view.direction[1] * view.direction[1] +
                                 view.direction[2] * view.direction[2]);
      if (!(n > 1e-12) || !std::isfinite(n)) {
        view.fieldErrors[s.key] = "The build direction cannot be zero.";
      } else {
        view.fieldErrors.erase(s.key);
        topo[s.key] = json::array({view.direction[0] / n, view.direction[1] / n,
                                   view.direction[2] / n});
        committed = true;
      }
    }
  }
  fieldError(view, s.key);
  keyIssues(issues, s.key);
  return committed;
}

bool drawSpec(const KeySpec &s, json &topo, const Combination &c,
              const std::string &physics, const DisplayUnits &du,
              SettingsView &view, const std::vector<Issue> &issues) {
  switch (s.kind) {
  case Kind::Real:
  case Kind::Integer:
    return numberField(s, topo, c, physics, du, view, issues);
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

// Draws one group's OFFERED keys under a header. Returns committed; `empty`
// reports nothing offered.
bool drawGroup(Group g, const char *title, json &topo, const Combination &c,
               const std::string &physics, const DisplayUnits &du,
               SettingsView &view, const std::vector<Issue> &issues,
               const char *emptyNote) {
  bool any = false;
  for (const KeySpec &s : kSpecs)
    any = any || (s.group == g && c.offersKey(s.key));
  if (!any && !emptyNote)
    return false;
  kit::sectionHeader(title);
  if (!any) {
    kit::dimText(emptyNote);
    return false;
  }
  bool committed = false;
  for (const KeySpec &s : kSpecs) {
    if (s.group != g || !c.offersKey(s.key))
      continue;
    committed |= drawSpec(s, topo, c, physics, du, view, issues);
    kit::gapRow();
  }
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
      view.editingKey.clear();
      view.fieldErrors.clear();
      committed = true;
    }
  };

  kit::sectionHeader("Problem", nullptr, nullptr, -1, true);
  fieldLabel("Physics", "What the optimizer analyses each iteration.");
  ImGui::SetNextItemWidth(-FLT_MIN);
  const std::string physPreview =
      physics.empty() ? "Choose physics" : physicsLabel(physics, &caps);
  if (ImGui::BeginCombo("##topo_physics", physPreview.c_str())) {
    for (const CapabilityEntry &p : caps.physics) {
      // Keep the current method when it supports the new physics, else the
      // first that does.
      std::string target;
      if (caps.supports(method, p.id))
        target = method;
      for (const CapabilityEntry &m : caps.methods)
        if (target.empty() && caps.supports(m.id, p.id))
          target = m.id;
      const std::string label = physicsLabel(p.id, &caps);
      const bool enabled = !target.empty();
      if (ImGui::Selectable(label.c_str(), p.id == physics,
                            enabled ? 0 : ImGuiSelectableFlags_Disabled) &&
          p.id != physics)
        apply(target, p.id);
      const std::string tip =
          enabled ? p.formulation
                  : "No method in this solver supports " + label + ".";
      hoverTip(tip.c_str());
    }
    ImGui::EndCombo();
  }
  keyIssues(issues, "physics");
  kit::gapRow();

  fieldLabel(
      "Method",
      "Optimization family. Unsupported pairs are listed with the reason.");
  ImGui::BeginDisabled(physics.empty());
  ImGui::SetNextItemWidth(-FLT_MIN);
  const std::string methPreview =
      method.empty() ? "Choose method" : methodLabel(method, &caps);
  if (ImGui::BeginCombo("##topo_method", methPreview.c_str())) {
    for (const CapabilityEntry &m : caps.methods) {
      std::string reason;
      const bool enabled = caps.supports(m.id, physics, &reason);
      const std::string label = methodLabel(m.id, &caps);
      if (ImGui::Selectable(label.c_str(), m.id == method,
                            enabled ? 0 : ImGuiSelectableFlags_Disabled) &&
          m.id != method)
        apply(m.id, physics);
      if (enabled) {
        const Combination *c = caps.find(m.id, physics);
        const std::string tip =
            c && !c->formulation.empty() ? c->formulation : m.formulation;
        hoverTip(tip.c_str());
      } else {
        hoverTip(reason.c_str());
      }
    }
    ImGui::EndCombo();
  }
  ImGui::EndDisabled();
  if (physics.empty())
    hoverTip("Choose a physics first.");
  keyIssues(issues, "method");

  // Re-read: a switch above may have changed the pair.
  const std::string p2 = str(topo, "physics"), m2 = str(topo, "method");
  const Combination *combo =
      caps.supports(m2, p2) ? caps.find(m2, p2) : nullptr;
  if (combo) {
    const std::string &f = combo->formulation;
    if (!f.empty())
      kit::dimText(f.c_str());
    if (const CapabilityEntry *pe = caps.physicsEntry(p2))
      if (!pe->validity.empty())
        kit::dimText(pe->validity.c_str());
  }
  kit::gapRow();

  fieldLabel("Objective", nullptr);
  ImGui::BeginDisabled(!combo);
  ImGui::SetNextItemWidth(-FLT_MIN);
  const std::string obj = str(topo, "objective");
  const std::string objPreview =
      obj.empty() ? "Choose objective" : objectiveLabel(obj, p2);
  if (ImGui::BeginCombo("##topo_objective", objPreview.c_str())) {
    if (combo) {
      for (const std::string &o : combo->objectives) {
        if (ImGui::Selectable(objectiveLabel(o, p2).c_str(), o == obj) &&
            o != obj) {
          topo["objective"] = o;
          committed = true;
        }
      }
    }
    ImGui::EndCombo();
  }
  ImGui::EndDisabled();
  if (!combo)
    hoverTip("Choose a supported physics and method first.");
  keyIssues(issues, "objective");
  if (!view.switchNote.empty())
    kit::inlineStatus(view.switchNote.c_str(), kit::Tone::Info);
  return committed;
}

std::string valueText(const json &v) {
  if (v.is_number())
    return fmtG(v.get<double>());
  if (v.is_boolean())
    return v.get<bool>() ? "on" : "off";
  if (v.is_string())
    return v.get<std::string>();
  if (v.is_array()) {
    std::string s;
    for (const json &e : v) {
      if (!s.empty())
        s += ", ";
      s += e.is_number()   ? fmtG(e.get<double>())
           : e.is_string() ? e.get<std::string>()
                           : e.dump();
    }
    return s;
  }
  return v.is_null() ? std::string() : v.dump();
}

} // namespace

bool drawTopologySettings(AppState &st, json &topology,
                          const Capabilities &caps, SettingsView &view,
                          const char *lockedReason) {
  ImGui::PushID("topology_settings");
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
    const bool create =
        kit::blockedState("Topology settings missing",
                          "This study has no topology configuration.",
                          "Create one for linear structural optimization, then "
                          "pick the physics you need.",
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
    ImGui::PopID();
    return committed;
  }

  const DisplayUnits du = displayUnits(st);
  const std::vector<Issue> issues = validateTopology(topology, caps);
  // Window-relative top of the form: the manufacturing focus falls back to it
  // when the chosen pair offers no manufacturing groups (the Problem section
  // explains why).
  const float formTop = ImGui::GetCursorScreenPos().y - ImGui::GetWindowPos().y;
  if (lockedReason)
    kit::inlineStatus(lockedReason, kit::Tone::Warn);
  ImGui::BeginDisabled(lockedReason != nullptr);

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
    committed |= drawGroup(Group::Goal, "Goal", topology, *combo, physics, du,
                           view, issues, nullptr);
    committed |= drawGroup(Group::Limits, "Response limits", topology, *combo,
                           physics, du, view, issues, noLimits.c_str());
    committed |= drawGroup(Group::Convergence, "Convergence", topology, *combo,
                           physics, du, view, issues, nullptr);
    committed |=
        drawGroup(Group::Regularization, "Filter and projection", topology,
                  *combo, physics, du, view, issues, nullptr);
    committed |= drawGroup(Group::Method, methodTitle.c_str(), topology, *combo,
                           physics, du, view, issues, nullptr);
    if (view.focusManufacturing) {
      scrollCursorToTop();
      view.focusManufacturing = false;
    }
    committed |=
        drawGroup(Group::Geometry, "Symmetry and passive regions", topology,
                  *combo, physics, du, view, issues, nullptr);
    bool additive = false;
    for (const KeySpec &s : kSpecs)
      additive =
          additive || (s.group == Group::Additive && combo->offersKey(s.key));
    committed |= drawGroup(Group::Additive, "Additive manufacturing", topology,
                           *combo, physics, du, view, issues, nullptr);
    if (additive)
      kit::dimText("These settings steer the optimizer. Measured member size, "
                   "overhang and "
                   "voids of the result are reported separately; the optimizer "
                   "alone does "
                   "not certify a part as printable.");

    // Owned keys present but not offered: the run is blocked until they are
    // removed.
    bool header = false;
    for (const KeySpec &s : kSpecs) {
      if (!topology.contains(s.key) || combo->offersKey(s.key))
        continue;
      if (!header) {
        kit::sectionHeader("Not offered for this combination");
        header = true;
      }
      const std::string label = keyLabel(s, physics, combo->volumePhase);
      kit::keyValueRow(label.c_str(), valueText(topology[s.key]).c_str(),
                       kit::Tone::Err);
      ImGui::PushID(s.key);
      if (kit::secondaryButton("Remove")) {
        topology.erase(s.key);
        committed = true;
      }
      ImGui::PopID();
    }
  } else if (view.focusManufacturing) {
    ImGui::SetScrollFromPosY(formTop, 0.0f);
    view.focusManufacturing = false;
  }

  // Whole-setup issues (not tied to a drawn field).
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

std::string fmtOpt(double v, const std::string &units = std::string()) {
  if (!std::isfinite(v))
    return "not reported";
  return units.empty() ? fmtG(v) : fmtG(v) + " " + units;
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

// Line plot of one history series; `refY` (finite) draws a dashed reference
// such as the target.
void drawSeriesPlot(const std::vector<double> &x, const std::vector<double> &y,
                    double refY) {
  double xLo = kInf, xHi = -kInf, yLo = kInf, yHi = -kInf;
  size_t pts = 0;
  for (size_t i = 0; i < x.size() && i < y.size(); ++i) {
    if (!std::isfinite(x[i]) || !std::isfinite(y[i]))
      continue;
    xLo = std::min(xLo, x[i]);
    xHi = std::max(xHi, x[i]);
    yLo = std::min(yLo, y[i]);
    yHi = std::max(yHi, y[i]);
    ++pts;
  }
  if (pts < 2) {
    kit::dimText(
        "Fewer than two finite samples were recorded for this series.");
    return;
  }
  if (std::isfinite(refY)) {
    yLo = std::min(yLo, refY);
    yHi = std::max(yHi, refY);
  }
  if (xHi - xLo <= 0.0)
    xHi = xLo + 1.0;
  if (yHi - yLo <= 1e-12 * std::max(1.0, std::fabs(yHi))) {
    yHi += 0.5 * std::max(1e-9, std::fabs(yHi));
    yLo -= 0.5 * std::max(1e-9, std::fabs(yLo));
  }
  const double margin = (yHi - yLo) * 0.08;
  yLo -= margin;
  yHi += margin;

  namespace m = ui::metrics;
  const float w = ImGui::GetContentRegionAvail().x;
  const float h = m::px(160.0f);
  const ImVec2 p0 = ImGui::GetCursorScreenPos();
  ImGui::InvisibleButton("##topo_plot", ImVec2(std::max(w, m::px(40.0f)), h));
  const bool hovered = ImGui::IsItemHovered();
  ImDrawList *dl = ImGui::GetWindowDrawList();
  const ImVec2 a0(p0.x + m::px(52.0f), p0.y + m::px(8.0f));
  const ImVec2 a1(p0.x + w - m::px(10.0f), p0.y + h - m::px(20.0f));
  auto X = [&](double v) {
    return float(a0.x + (v - xLo) / (xHi - xLo) * (a1.x - a0.x));
  };
  auto Y = [&](double v) {
    return float(a1.y - (v - yLo) / (yHi - yLo) * (a1.y - a0.y));
  };
  const ImU32 grid = kit::toneU32(kit::Tone::Neutral, 0.25f);
  const ImU32 axis = kit::toneU32(kit::Tone::Neutral);
  const ImU32 line = kit::toneU32(kit::Tone::Accent);
  dl->PushClipRect(p0, ImVec2(p0.x + w, p0.y + h), true);
  char lb[32];
  for (int g = 0; g <= 4; ++g) {
    const float yy = a0.y + (a1.y - a0.y) * float(g) / 4.0f;
    dl->AddLine(ImVec2(a0.x, yy), ImVec2(a1.x, yy), grid, m::stroke(1.0f));
    std::snprintf(lb, sizeof(lb), "%.4g", yHi - (yHi - yLo) * g / 4.0);
    dl->AddText(ImVec2(p0.x, yy - ImGui::GetTextLineHeight() * 0.5f), axis, lb);
  }
  dl->AddLine(ImVec2(a0.x, a1.y), ImVec2(a1.x, a1.y), axis, m::stroke(1.5f));
  dl->AddLine(a0, ImVec2(a0.x, a1.y), axis, m::stroke(1.5f));
  for (int g = 0; g <= 2; ++g) {
    const double xv = xLo + (xHi - xLo) * g / 2.0;
    std::snprintf(lb, sizeof(lb), "%.4g", xv);
    const float tw = ImGui::CalcTextSize(lb).x;
    dl->AddText(ImVec2(X(xv) - tw * 0.5f * float(g), a1.y + m::px(3.0f)), axis,
                lb);
  }
  if (std::isfinite(refY)) {
    const float ry = Y(refY);
    const float dash = m::px(6.0f);
    for (float xx = a0.x; xx < a1.x; xx += dash * 2.0f)
      dl->AddLine(ImVec2(xx, ry), ImVec2(std::min(xx + dash, a1.x), ry),
                  kit::toneU32(kit::Tone::Warn), m::stroke(1.0f));
  }
  ImVec2 prev;
  bool havePrev = false;
  size_t nearest = 0;
  float bestDx = FLT_MAX;
  const float mx = ImGui::GetIO().MousePos.x;
  for (size_t i = 0; i < x.size() && i < y.size(); ++i) {
    if (!std::isfinite(x[i]) || !std::isfinite(y[i])) {
      havePrev = false;
      continue;
    }
    const ImVec2 p(X(x[i]), Y(y[i]));
    if (havePrev)
      dl->AddLine(prev, p, line, m::stroke(1.75f));
    prev = p;
    havePrev = true;
    if (std::fabs(p.x - mx) < bestDx) {
      bestDx = std::fabs(p.x - mx);
      nearest = i;
    }
  }
  if (hovered && bestDx < FLT_MAX) {
    const ImVec2 p(X(x[nearest]), Y(y[nearest]));
    dl->AddCircleFilled(p, m::px(3.5f), line);
    ImGui::SetTooltip("iteration %.6g: %.6g", x[nearest], y[nearest]);
  }
  dl->PopClipRect();
}

void outcomeCard(const json &result, const OutcomeSummary &o) {
  if (kit::cardBegin("topo_outcome", o.headline.c_str())) {
    kit::statusGlyphInline(outcomeStatus(o.outcome));
    ImGui::SameLine();
    kit::chip(o.computationOk ? "Computation completed" : "Computation failed",
              o.computationOk ? kit::Tone::Ok : kit::Tone::Err, true,
              "Whether the solver finished without an error. A completed "
              "computation is not a "
              "certified design.");
    kit::chip(o.converged ? "Converged" : "Not converged",
              o.converged ? kit::Tone::Ok : kit::Tone::Warn, true,
              "Convergence requires meeting the change criterion with every "
              "constraint "
              "satisfied; an iteration-limit or infeasible stop never counts.");
    if (!o.feasibilityReported)
      kit::chip("Feasibility not reported", kit::Tone::Warn, true);
    else
      kit::chip(o.feasible ? "Feasible" : "Infeasible",
                o.feasible ? kit::Tone::Ok : kit::Tone::Err, true);
    kit::dimText(o.detail.c_str());
    const std::string method = str(result, "method"),
                      physics = str(result, "physics");
    if (!method.empty() || !physics.empty()) {
      const std::string pair =
          methodLabel(method) + " with " + physicsLabel(physics);
      kit::keyValueRow("Study", pair.c_str());
    }
    const std::string formulation = str(result, "formulation");
    if (!formulation.empty())
      kit::keyValueRow("Formulation", formulation.c_str());
    if (!o.termination.empty())
      kit::keyValueRow("Termination", o.termination.c_str());
  }
  kit::cardEnd();
}

void metricsTable(const json &result) {
  const json &s = member(result, "summary");
  const std::string physics = str(result, "physics");
  const std::string objective = str(s, "objective");
  const std::string units = str(s, "objective_units");
  kit::sectionHeader("Baseline vs final");
  if (!s.is_object()) {
    kit::dimText("This run reported no summary metrics.");
    return;
  }
  auto row = [&](const std::string &label, double a, double b,
                 const std::string &u) {
    if (!std::isfinite(a) && !std::isfinite(b))
      return;
    kit::gapRow();
    ImGui::TextWrapped("%s", label.c_str());
    kit::keyValueRow("Baseline", fmtOpt(a, u).c_str());
    kit::keyValueRow("Final", fmtOpt(b, u).c_str());
  };
  row(objective.empty() ? "Objective" : objectiveLabel(objective, physics),
      num(s, "objective_baseline"), num(s, "objective_final"), units);
  row(volumeLabel(resultPhase(result)), num(s, "volume_fraction_baseline"),
      num(s, "volume_fraction_final"), "");
  row("Mass", num(s, "mass_baseline_g"), num(s, "mass_final_g"), "g");
  const double target = num(s, "volume_fraction_target");
  if (std::isfinite(target))
    kit::keyValueRow((volumeLabel(resultPhase(result)) + " target").c_str(),
                     fmtG(target).c_str());
  const double iters = num(s, "iterations");
  if (std::isfinite(iters))
    kit::keyValueRow("Iterations", fmtG(iters).c_str());
  const std::string crit = str(s, "convergence_criterion");
  if (!crit.empty())
    kit::keyValueRow("Convergence criterion", crit.c_str());
  const double change = num(s, "final_change");
  if (std::isfinite(change))
    kit::keyValueRow("Final design change", fmtG(change).c_str());
  const double viol = num(s, "max_constraint_violation");
  if (std::isfinite(viol))
    kit::keyValueRow("Max constraint violation", fmtG(viol).c_str(),
                     viol > 0.0 ? kit::Tone::Err : kit::Tone::Ok);
  const double elapsed = num(s, "elapsed_s");
  if (std::isfinite(elapsed))
    kit::keyValueRow("Run time", kit::fmtDuration(elapsed).c_str());

  // Remaining scalar evidence (residuals, reactions, conservation, Reynolds
  // number, ...).
  static const std::set<std::string> kShown = {"objective",
                                               "objective_units",
                                               "objective_baseline",
                                               "objective_final",
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
  bool header = false;
  for (const auto &[k, v] : s.items()) {
    if (kShown.count(k) || !(v.is_number() || v.is_string() || v.is_boolean()))
      continue;
    if (!header) {
      kit::sectionHeader("Physical evidence");
      header = true;
    }
    kit::keyValueRow(humanize(k).c_str(), valueText(v).c_str());
  }
}

void constraintsTable(const json &result) {
  const json &cons = member(result, "constraints");
  kit::sectionHeader("Constraints", nullptr, nullptr,
                     cons.is_array() ? int(cons.size()) : 0);
  if (!cons.is_array() || cons.empty()) {
    kit::dimText(
        "No constraints besides the volume budget were active for this run.");
    return;
  }
  int idx = 0;
  for (const json &c : cons) {
    std::string name = str(c, "label");
    if (name.empty())
      name = humanize(str(c, "name"));
    const std::string u = str(c, "units");
    const double viol = num(c, "violation");
    bool satisfied = std::isfinite(viol) && viol <= 0.0;
    if (member(c, "satisfied").is_boolean())
      satisfied = boolTrue(c, "satisfied");
    const bool known =
        std::isfinite(viol) || member(c, "satisfied").is_boolean();
    ImGui::PushID(idx++);
    kit::gapRow();
    ImGui::TextWrapped("%s", name.c_str());
    kit::keyValueRow("Limit", fmtOpt(num(c, "limit"), u).c_str());
    kit::keyValueRow("Final", fmtOpt(num(c, "value"), u).c_str());
    if (!known) {
      kit::chip("Not reported", kit::Tone::Warn, true);
    } else {
      const std::string tip =
          std::isfinite(viol)
              ? "Violation g = " + fmtG(viol) + " (g <= 0 is satisfied)"
              : std::string();
      kit::chip(satisfied ? "Satisfied" : "Violated",
                satisfied ? kit::Tone::Ok : kit::Tone::Err, true,
                tip.empty() ? nullptr : tip.c_str());
    }
    ImGui::PopID();
  }
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
  const Series *shown = nullptr;
  for (const Series &s : series)
    if (s.key == view.plotSeries)
      shown = &s;
  if (!shown) {
    shown = &series.front();
    for (const Series &s : series)
      if (s.key == "objective")
        shown = &s;
    view.plotSeries = shown->key;
  }
  ImGui::SetNextItemWidth(-FLT_MIN);
  const std::string phase = resultPhase(result);
  if (ImGui::BeginCombo("##topo_series",
                        seriesLabel(shown->key, phase).c_str())) {
    for (const Series &s : series)
      if (ImGui::Selectable(seriesLabel(s.key, phase).c_str(), &s == shown))
        view.plotSeries = s.key;
    ImGui::EndCombo();
  }
  double ref = kNaN;
  const json &summary = member(result, "summary");
  if (shown->key == "volume_fraction")
    ref = num(summary, "volume_fraction_target");
  if (shown->key == "max_violation")
    ref = 0.0;
  drawSeriesPlot(x, shown->y, ref);
  if (std::isfinite(ref))
    kit::dimText(shown->key == "volume_fraction"
                     ? "Dashed: volume target."
                     : "Dashed: feasibility boundary (0).");
}

void manufacturingSection(const json &result, const json &settings) {
  kit::sectionHeader("Manufacturing");
  const ManufacturingReport rep = manufacturingReport(result, settings);
  if (!rep.rows.empty()) {
    int idx = 0;
    for (const ManufacturingRow &r : rep.rows) {
      ImGui::PushID(idx++);
      kit::gapRow();
      ImGui::TextWrapped("%s", r.label.c_str());
      hoverTip(r.note.c_str());
      auto withUnits = [&](const json &v) {
        if (v.is_null())
          return std::string("-");
        std::string t = valueText(v);
        return (v.is_number() && !r.units.empty()) ? t + " " + r.units : t;
      };
      kit::keyValueRow("Requested", withUnits(r.requested).c_str());
      kit::keyValueRow("Measured", r.measured.is_null()
                                       ? "not measured"
                                       : withUnits(r.measured).c_str());
      const char *note = r.note.empty() ? nullptr : r.note.c_str();
      if (r.status == CheckStatus::Pass)
        kit::chip("Pass", kit::Tone::Ok, true, note);
      else if (r.status == CheckStatus::Fail)
        kit::chip("Fail", kit::Tone::Err, true, note);
      else
        kit::chip("Not measured", kit::Tone::Warn, true, note);
      ImGui::PopID();
    }
  }
  kit::keyValueRow("Surface validated", rep.geometryValidated ? "yes" : "no",
                   rep.geometryValidated ? kit::Tone::Ok : kit::Tone::Warn);
  kit::keyValueRow("DFAM checks",
                   !rep.dfamChecked ? "not run"
                   : rep.dfamPassed ? "passed"
                                    : "failed",
                   !rep.dfamChecked ? kit::Tone::Warn
                   : rep.dfamPassed ? kit::Tone::Ok
                                    : kit::Tone::Err);
  kit::inlineStatus(rep.printReadyReason.c_str(),
                    rep.printReady ? kit::Tone::Ok : kit::Tone::Warn);
}

void compareSection(const json &runs, size_t sel, ResultsView &view) {
  if (view.focusCompare) {
    scrollCursorToTop(); // the ribbon's Compare command lands on this section
    view.focusCompare = false;
  }
  kit::sectionHeader("Compare runs");
  // Baseline defaults to the run before the selected one.
  int base = -1;
  for (size_t i = 0; i < runs.size(); ++i)
    if (i != sel && runIdOf(runs[i], i) == view.baselineRunId)
      base = int(i);
  if (base < 0) {
    base = sel > 0 ? int(sel - 1) : 1;
    view.baselineRunId = runIdOf(runs[size_t(base)], size_t(base));
  }
  fieldLabel("Baseline", "The run the selected run is compared against.");
  ImGui::SetNextItemWidth(-FLT_MIN);
  const std::string preview = runTitle(runs[size_t(base)], size_t(base));
  if (ImGui::BeginCombo("##topo_baseline", preview.c_str())) {
    for (size_t i = 0; i < runs.size(); ++i) {
      if (i == sel)
        continue;
      ImGui::PushID(int(i));
      if (ImGui::Selectable(runTitle(runs[i], i).c_str(), int(i) == base))
        view.baselineRunId = runIdOf(runs[i], i);
      ImGui::PopID();
    }
    ImGui::EndCombo();
  }
  const std::vector<MetricDelta> deltas =
      compareRuns(runs[size_t(base)], runs[sel]);
  if (deltas.empty()) {
    kit::dimText("Neither run reported comparable metrics.");
    return;
  }
  for (const MetricDelta &d : deltas) {
    kit::gapRow();
    ImGui::TextWrapped("%s", d.label.c_str());
    kit::keyValueRow("Baseline", fmtOpt(d.baseline, d.units).c_str());
    kit::keyValueRow("Selected", fmtOpt(d.selected, d.units).c_str());
    if (!d.comparable) {
      kit::keyValueRow("Change", "n/a");
      kit::dimText(d.note.c_str());
      continue;
    }
    std::string change = fmtG(d.selected - d.baseline);
    if (d.selected - d.baseline > 0.0)
      change.insert(change.begin(), '+');
    if (std::fabs(d.baseline) > 0.0) {
      char pct[32];
      std::snprintf(pct, sizeof(pct), " (%+.1f%%)",
                    (d.selected - d.baseline) / std::fabs(d.baseline) * 100.0);
      change += pct;
    }
    kit::keyValueRow("Change", change.c_str());
  }
}

void setupSection(const json &run, const std::string &runId,
                  ResultsAction &act) {
  const json &settings = runSettings(run);
  kit::sectionHeader("Setup of this run");
  if (!settings.is_object()) {
    kit::dimText("This run did not retain its settings snapshot.");
    return;
  }
  const std::string physics = str(settings, "physics");
  kit::keyValueRow("Physics", physicsLabel(physics).c_str());
  kit::keyValueRow("Method", methodLabel(str(settings, "method")).c_str());
  kit::keyValueRow("Objective",
                   objectiveLabel(str(settings, "objective"), physics).c_str());
  for (const KeySpec &s : kSpecs) {
    const auto it = settings.find(s.key);
    if (it == settings.end())
      continue;
    std::string v = valueText(*it);
    if (it->is_number()) {
      if (s.unit == Unit::Length)
        v += " mm";
      else if (s.unit == Unit::Stress)
        v += " MPa";
      else if (s.unit == Unit::PressurePa)
        v += " Pa";
      else if (s.unit == Unit::TempC)
        v += " degC";
      else if (s.unit == Unit::Hz)
        v += " Hz";
      else if (s.unit == Unit::Deg)
        v += " deg";
    }
    kit::keyValueRow(keyLabel(s, physics).c_str(), v.c_str());
  }
  if (kit::secondaryButton("Use these settings")) {
    act.kind = ResultsAction::Kind::ApplyRunSettings;
    act.runId = runId;
  }
  hoverTip("Copy this run's method, physics, objective and scalar settings "
           "into the study. "
           "Face selections and other study data stay as they are.");
}

void artifactsSection(const json &result, const std::string &runId,
                      ResultsAction &act) {
  kit::sectionHeader("Result geometry and export");
  kit::dimText("Results are meshes of the optimized density field (surface and "
               "volume), not "
               "parametric CAD. The source part and this study's setup are "
               "left unchanged; "
               "exporting writes a copy and never edits the model.");
  const json &arts = member(result, "artifacts");
  const bool geometryValid =
      boolTrue(member(result, "manufacturing"), "geometry_valid");
  bool viewable = false;
  std::string viewReason = "This run retained no viewport overlay.";
  if (arts.is_object()) {
    int idx = 0;
    for (const auto &[key, a] : arts.items()) {
      if (!a.is_object())
        continue;
      const std::string format = str(a, "format");
      const bool missing = boolTrue(a, "missing");
      const bool stale = boolTrue(a, "stale");
      if (format == "aicm" || key == "overlay_aicm") {
        viewable = !missing && !stale;
        if (missing)
          viewReason = "The viewport overlay file is missing.";
        if (stale)
          viewReason = "The viewport overlay changed since the run.";
        continue; // a viewport handoff, not an export product
      }
      std::string title = str(a, "label");
      if (title.empty())
        title = humanize(key);
      std::string secondary = format.empty() ? std::string() : format;
      const double bytes = num(a, "bytes");
      if (std::isfinite(bytes) && bytes >= 0.0) {
        if (!secondary.empty())
          secondary += "  ";
        secondary += kit::fmtBytes((unsigned long long)bytes);
      }
      const std::string sha = str(a, "sha256");
      if (sha.size() >= 12) {
        if (!secondary.empty())
          secondary += "  ";
        secondary += "sha256 " + sha.substr(0, 12);
      }
      const std::string path = str(a, "path");
      ImGui::PushID(idx++);
      kit::RowStyle rs;
      rs.secondary = secondary.c_str();
      rs.tooltip = path.c_str();
      // A surface mesh is only offered as usable once the solver validated it
      // (closed, oriented, manifold); the volume field and evidence files are
      // always exportable.
      const bool surface =
          format == "stl" || format == "3mf" || format == "obj";
      const bool unvalidated = surface && !geometryValid;
      rs.disabled = missing || stale || unvalidated;
      rs.actionsW = ui::metrics::px(84.0f);
      ImGui::SetNextItemAllowOverlap();
      kit::row("##art", title.c_str(), rs);
      ImGui::SameLine(ImGui::GetContentRegionAvail().x - rs.actionsW +
                      ImGui::GetCursorPosX() - ImGui::GetStyle().ItemSpacing.x);
      const char *why =
          missing ? "The file is missing on disk."
          : stale ? "The file changed since the run (hash mismatch)."
          : unvalidated
              ? "The surface failed or skipped geometry validation (closed, "
                "manifold, oriented), so it is not offered as a usable part."
              : nullptr;
      if (kit::secondaryButton("Export", why == nullptr, why)) {
        act.kind = ResultsAction::Kind::ExportArtifact;
        act.runId = runId;
        act.artifactKey = key;
      }
      ImGui::PopID();
    }
  }
  if (!str(result, "overlay_aicm").empty())
    viewable = true;
  if (kit::secondaryButton("Show in viewport", viewable, viewReason.c_str())) {
    act.kind = ResultsAction::Kind::ShowRun;
    act.runId = runId;
  }
}

void evidenceSection(const json &result) {
  auto scalars = [](const json &obj, const char *title) {
    if (!obj.is_object())
      return;
    bool header = false;
    for (const auto &[k, v] : obj.items()) {
      if (!(v.is_number() || v.is_string() || v.is_boolean()))
        continue;
      if (!header) {
        kit::sectionHeader(title);
        header = true;
      }
      kit::keyValueRow(humanize(k).c_str(), valueText(v).c_str(),
                       kit::Tone::Neutral, v.is_string());
    }
  };
  scalars(member(result, "mesh_quality"), "Mesh quality");
  scalars(member(result, "provenance"), "Provenance");
}

} // namespace

ResultsAction drawTopologyResults(AppState & /*st*/, const json &runs,
                                  const Capabilities &caps, ResultsView &view) {
  ResultsAction act;
  ImGui::PushID("topology_results");
  if (!runs.is_array() || runs.empty()) {
    kit::emptyState(nullptr, "No topology runs yet",
                    "Run the study to see convergence, constraints and "
                    "manufacturing checks "
                    "here. Every run is kept for comparison.");
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

  kit::sectionHeader("Runs", nullptr, nullptr, int(runs.size()), true);
  for (size_t i = runs.size(); i-- > 0;) { // newest first
    const json &run = runs[i];
    const OutcomeSummary o = classifyResult(runResult(run));
    const std::string id = runIdOf(run, i);
    const std::string title = runTitle(run, i);
    const json &settings = runSettings(run);
    std::string secondary = methodLabel(str(settings, "method"), &caps) +
                            " / " + physicsLabel(runPhysics(run), &caps) +
                            " / " + o.headline;
    const double started = num(run, "started_utc");
    const std::string meta =
        std::isfinite(started) ? kit::fmtAgo(started) : std::string();
    kit::RowStyle rs;
    rs.secondary = secondary.c_str();
    rs.meta = meta.empty() ? nullptr : meta.c_str();
    rs.selected = i == sel;
    rs.iconTone = outcomeTone(o.outcome);
    ImGui::PushID(int(i));
    if (kit::row("##run", title.c_str(), rs) && i != sel) {
      view.selectedRunId = id;
      act.kind = ResultsAction::Kind::ShowRun;
      act.runId = id;
    }
    ImGui::PopID();
  }

  const json &run = runs[sel];
  const json &result = runResult(run);
  const std::string runId = runIdOf(run, sel);
  const OutcomeSummary o = classifyResult(result);
  kit::gapSection();
  outcomeCard(result, o);
  if (result.is_object()) {
    metricsTable(result);
    constraintsTable(result);
    historySection(result, view);
    manufacturingSection(result, runSettings(run));
  }
  if (runs.size() >= 2)
    compareSection(runs, sel, view);
  else
    view.focusCompare =
        false; // nothing to compare; never fire later by surprise
  setupSection(run, runId, act);
  if (result.is_object()) {
    artifactsSection(result, runId, act);
    evidenceSection(result);
  }
  ImGui::PopID();
  return act;
}

} // namespace topology
} // namespace ui
#endif // CHUDWARE_SIM_TOPOLOGY_MODEL_ONLY
