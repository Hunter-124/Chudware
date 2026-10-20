// sim_topology_bc.cpp — see sim_topology_bc.h.
//
// CHUDWARE_SIM_TOPOLOGY_MODEL_ONLY compiles only the pure study model (wells,
// validation, envelope, run records) so a CPU test can link this TU without
// ImGui/AppState; the editor half is excluded in that build, nothing is
// stubbed.
#include "sim_topology_bc.h"

#include <algorithm>
#include <cmath>
#include <cctype>
#include <cstdlib>
#include <cstring>
#include <cstdio>
#include <functional>
#include <limits>
#include <set>
#include <utility>

namespace ui {
namespace topology {

using nlohmann::json;

namespace {

constexpr double kAbsZeroC = -273.15;

const char *const kMaterialKeys[] = {"E_mpa",         "nu",     "yield_mpa",
                                     "density_g_cm3", "k_w_mk", "alpha_per_k",
                                     "cp_j_kgk"};

const json &member(const json &j, const char *key) {
  static const json kNull;
  if (!j.is_object())
    return kNull;
  const auto it = j.find(key);
  return it == j.end() ? kNull : *it;
}

bool finiteAt(const json &j, const char *key, double &out) {
  const json &v = member(j, key);
  if (!v.is_number())
    return false;
  out = v.get<double>();
  return std::isfinite(out);
}

bool vec3At(const json &j, const char *key, double out[3]) {
  const json &v = member(j, key);
  if (!v.is_array() || v.size() != 3)
    return false;
  for (int k = 0; k < 3; ++k) {
    if (!v[k].is_number())
      return false;
    out[k] = v[k].get<double>();
    if (!std::isfinite(out[k]))
      return false;
  }
  return true;
}

// Mirror core/sim/fea_run.cpp::validate_faceset_input, not the stricter
// three-component editor fields. Native vectors inspect their first three
// components and native point sets count each nested vector as three scalars.
// Validate without normalizing: canonical resolved data must survive edits.
bool validateResolvedFaceset(const json &fs, const std::string &path,
                             std::string &error) {
  auto reject = [&](const std::string &message) {
    error = path + ": " + message;
    return false;
  };
  auto finiteNumber = [](const json &value) {
    return value.is_number() && std::isfinite(value.get<double>());
  };
  auto finiteVector = [&](const json &value) {
    return value.is_array() && value.size() >= 3 &&
           finiteNumber(value[0]) && finiteNumber(value[1]) &&
           finiteNumber(value[2]);
  };
  auto nonzeroVector = [&](const json &value) {
    if (!finiteVector(value))
      return false;
    double norm2 = 0.0;
    for (int i = 0; i < 3; ++i) {
      const double component = value[i].get<double>();
      norm2 += component * component;
    }
    return std::isfinite(norm2) && norm2 > 0.0;
  };
  const json &points = member(fs, "points");
  if (!points.is_array() || points.empty())
    return reject("faceset requires a non-empty points array.");
  std::size_t flatCount = 0;
  for (const json &point : points) {
    if (point.is_array()) {
      if (!finiteVector(point))
        return reject("faceset points must contain finite 3-vectors.");
      flatCount += 3;
    } else if (finiteNumber(point)) {
      ++flatCount;
    } else {
      return reject("faceset points must contain finite numbers.");
    }
  }
  if (flatCount < 3 || flatCount % 3 != 0)
    return reject("faceset points must contain complete 3-vectors.");
  for (const char *key : {"tol_mm", "perp_tol_mm"}) {
    double value = 0.0;
    if (fs.contains(key) && !(finiteAt(fs, key, value) && value > 0.0))
      return reject(std::string("faceset ") + key +
                    " must be finite and positive.");
  }
  if (fs.contains("face_normal") && !nonzeroVector(fs["face_normal"]))
    return reject("faceset face_normal must be a non-zero finite 3-vector.");
  if (!fs.contains("surface"))
    return true;
  const json &surface = fs["surface"];
  if (!surface.is_object() || !member(surface, "type").is_string())
    return reject("faceset surface requires a string type.");
  if (!finiteVector(member(surface, "origin")))
    return reject("faceset surface requires a finite origin.");
  auto positive = [&](const char *key) {
    double value = 0.0;
    return finiteAt(surface, key, value) && value > 0.0;
  };
  const std::string &type = surface["type"].get_ref<const std::string &>();
  if (type == "sphere") {
    if (!positive("radius"))
      return reject("sphere faceset requires a positive finite radius.");
  } else if (type == "cylinder") {
    if (!positive("radius") || !nonzeroVector(member(surface, "axis")))
      return reject("cylinder faceset requires positive radius and non-zero finite axis.");
  } else if (type == "cone") {
    if (!positive("semi_angle_rad") ||
        surface["semi_angle_rad"].get<double>() >= 1.5707963 ||
        !nonzeroVector(member(surface, "axis")))
      return reject("cone faceset requires a valid semi-angle and non-zero finite axis.");
  } else if (type == "torus") {
    if (!positive("major_radius") || !positive("minor_radius") ||
        !nonzeroVector(member(surface, "axis")))
      return reject("torus faceset requires positive radii and non-zero finite axis.");
  } else {
    return reject("unsupported faceset surface type '" + type + "'.");
  }
  return true;
}

std::string idx(const char *base, std::size_t i) {
  return std::string(base) + "/" + std::to_string(i);
}

bool isBareSelector(const json &entry) {
  return entry.is_object() && !entry.contains("faces") &&
         (entry.contains("kind") || entry.contains("points"));
}

bool pickOf(const json &sel, std::uint32_t &body, std::uint32_t &topo) {
  const json &p = member(sel, "_pick");
  if (!p.is_object())
    return false;
  const json &b = member(p, "body"), &t = member(p, "topo");
  auto validId = [](const json &value) {
    if (!value.is_number_integer())
      return false;
    if (value.is_number_unsigned())
      return value.get<std::uint64_t>() > 0 &&
             value.get<std::uint64_t>() <= std::numeric_limits<std::uint32_t>::max();
    return value.get<std::int64_t>() > 0 &&
           value.get<std::int64_t>() <= std::numeric_limits<std::uint32_t>::max();
  };
  if (!validId(b) || !validId(t))
    return false;
  body = b.get<std::uint32_t>();
  topo = t.get<std::uint32_t>();
  return true;
}

std::uint64_t faceKey(std::uint32_t body, std::uint32_t topo) {
  return (static_cast<std::uint64_t>(body) << 32) | topo;
}

json stripPick(const json &sel) {
  json clean = sel;
  if (clean.is_object())
    clean.erase("_pick");
  return clean;
}

// Visit every well (faces array or bare-selector fixture) with its role and
// activity.
struct WellVisit {
  std::string pointer; // "" for a bare-selector fixture
  WellRole role;
  bool active;
  const json *faces; // the faces array, or the bare selector itself
  bool bare;
};

void visitWells(const json &study,
                const std::function<void(const WellVisit &)> &fn) {
  if (!study.is_object())
    return;
  const std::string physics = studyPhysics(study);
  const bool structural = usesStructuralBoundary(physics);
  const bool loadsOn = usesStructuralLoads(physics);
  const bool thermal = usesThermalBoundary(physics);
  const bool flow = usesFlowBoundary(physics);

  auto list = [&](const json &arr, const std::string &base, WellRole role,
                  bool active, bool allowBare) {
    if (!arr.is_array())
      return;
    for (std::size_t i = 0; i < arr.size(); ++i) {
      const json &e = arr[i];
      if (allowBare && isBareSelector(e)) {
        fn({"", role, active, &e, true});
        continue;
      }
      const json &faces = member(e, "faces");
      if (faces.is_array())
        fn({base + "/" + std::to_string(i) + "/faces", role, active, &faces,
            false});
    }
  };
  list(member(study, "fixtures"), "/fixtures", WellRole::Support, structural,
       true);
  list(member(study, "loads"), "/loads", WellRole::Load, loadsOn, false);
  const json &cases = member(study, "load_cases");
  if (cases.is_array())
    for (std::size_t c = 0; c < cases.size(); ++c) {
      const std::string base = idx("/load_cases", c);
      list(member(cases[c], "fixtures"), base + "/fixtures", WellRole::Support,
           loadsOn, true);
      list(member(cases[c], "loads"), base + "/loads", WellRole::Load, loadsOn,
           false);
    }
  list(member(study, "preserve"), "/preserve", WellRole::Preserve, true, false);
  list(member(study, "exclude"), "/exclude", WellRole::Exclude, true, false);
  const json &plate = member(member(study, "build_plate"), "faces");
  if (plate.is_array())
    fn({"/build_plate/faces", WellRole::Preserve, true, &plate, false});
  const json &th = member(study, "thermal");
  list(member(th, "fixtures"), "/thermal/fixtures", WellRole::Support, thermal,
       false);
  list(member(th, "loads"), "/thermal/loads", WellRole::Load, thermal, false);
  const json &fl = member(study, "flow");
  list(member(fl, "inlets"), "/flow/inlets", WellRole::Port, flow, false);
  list(member(fl, "outlets"), "/flow/outlets", WellRole::Port, flow, false);
  list(member(fl, "walls"), "/flow/walls", WellRole::Wall, flow, false);
}

json *facesAt(json &study, const std::string &pointer) {
  if (pointer.empty())
    return nullptr;
  try {
    const json::json_pointer p(pointer);
    if (!study.contains(p))
      return nullptr;
    json &v = study[p];
    return v.is_array() ? &v : nullptr;
  } catch (...) {
    return nullptr;
  }
}

std::string physicsName(const std::string &p) {
  return p.empty() ? std::string("this physics") : physicsLabel(p);
}

void issue(std::vector<Issue> &out, const std::string &key, Severity sev,
           const std::string &msg) {
  out.push_back({key, sev, msg});
}

std::string entryLabel(const json &entry, const std::string &fallback) {
  const json &l = member(entry, "label");
  if (l.is_string() && !l.get<std::string>().empty())
    return l.get<std::string>();
  const json &n = member(entry, "name");
  if (n.is_string() && !n.get<std::string>().empty())
    return n.get<std::string>();
  return fallback;
}

// The issue line under a field inside its entry's card: the card title
// already names the entry, so the leading name is dropped ("Load 1 needs a
// force vector." -> "Needs a force vector."). Empty when the message does not
// open with the entry's name (then the full message is shown).
std::string localIssueText(const json &study, const std::string &key,
                           const std::string &msg) {
  // The entry is the key up to its deepest array index ("/loads/0/vec").
  long index = -1;
  std::size_t entryEnd = std::string::npos;
  for (std::size_t b = 0; b < key.size();) {
    if (key[b] == '/')
      ++b;
    std::size_t e = key.find('/', b);
    if (e == std::string::npos)
      e = key.size();
    if (e > b && std::all_of(key.begin() + (long)b, key.begin() + (long)e,
                             [](char ch) { return ch >= '0' && ch <= '9'; })) {
      index = std::strtol(key.c_str() + b, nullptr, 10);
      entryEnd = e;
    }
    b = e;
  }
  if (index < 0)
    return {};
  std::string named;
  try {
    named = entryLabel(study.at(json::json_pointer(key.substr(0, entryEnd))), "");
  } catch (...) {
  }
  std::string rest;
  if (!named.empty() && msg.rfind(named + " ", 0) == 0) {
    rest = msg.substr(named.size() + 1);
  } else {
    // Default names end in the 1-based number: "Fixed temperature 1 needs ...".
    const std::string needle = " " + std::to_string(index + 1) + " ";
    const std::size_t at = msg.find(needle);
    if (at == std::string::npos || msg.find('.') < at)
      return {};
    rest = msg.substr(at + needle.size());
  }
  if (rest.empty())
    return {};
  if (rest == "has no faces.")
    return "Pick at least one face.";
  rest[0] = (char)std::toupper((unsigned char)rest[0]);
  return rest;
}

bool facesNonEmpty(const json &entry) {
  const json &f = member(entry, "faces");
  return f.is_array() && !f.empty();
}

// Merge resolved facesets into one (rule shared with services/sim/topology.py
// merge_facesets): points concatenated, tol_mm = max; face_normal kept (with
// perp_tol_mm = max) only when every member carries the identical normal; when
// NO member carries a normal, perp_tol_mm = max is kept and `surface` survives
// only if identical on every member. Mixed normals drop both.
json mergeFacesets(const std::vector<json> &sets) {
  if (sets.size() == 1)
    return sets.front();
  json out = json::object();
  json points = json::array();
  double tol = 0.0, perpTol = 0.0;
  bool anyPerp = false, anyNormal = false, allNormal = true, sameNormal = true;
  bool sameSurface = true;
  const json *normal = nullptr;
  const json *surface = nullptr;
  for (const json &s : sets) {
    const json &pts = member(s, "points");
    if (pts.is_array())
      for (const json &p : pts)
        points.push_back(p);
    double t = 0.0;
    if (finiteAt(s, "tol_mm", t))
      tol = std::max(tol, t);
    const json &n = member(s, "face_normal");
    if (n.is_array()) {
      anyNormal = true;
      if (!normal)
        normal = &n;
      else if (*normal != n)
        sameNormal = false;
    } else {
      allNormal = false;
    }
    const json &sf = member(s, "surface");
    if (!sf.is_object())
      sameSurface = false;
    else if (!surface)
      surface = &sf;
    else if (*surface != sf)
      sameSurface = false;
    double pt = 0.0;
    if (finiteAt(s, "perp_tol_mm", pt)) {
      perpTol = std::max(perpTol, pt);
      anyPerp = true;
    }
  }
  out["points"] = std::move(points);
  if (tol > 0.0)
    out["tol_mm"] = tol;
  if (anyNormal && allNormal && sameNormal) {
    out["face_normal"] = *normal;
    if (anyPerp)
      out["perp_tol_mm"] = perpTol;
  } else if (!anyNormal) {
    if (anyPerp)
      out["perp_tol_mm"] = perpTol;
    if (sameSurface && surface)
      out["surface"] = *surface;
  }
  return out;
}

bool resolveFaces(const json &faces,
                  const std::map<std::string, json> &resolved, json &out,
                  std::string &error, const std::string &what) {
  if (!faces.is_array() || faces.empty()) {
    error = what + " has no faces";
    return false;
  }
  std::vector<json> sets;
  for (const json &sel : faces) {
    if (sel.is_object() && sel.contains("points")) {
      sets.push_back(stripPick(sel));
      continue;
    }
    const auto it = resolved.find(selectorKey(sel));
    if (it == resolved.end()) {
      error = what + ": a face selector was not resolved";
      return false;
    }
    sets.push_back(it->second);
  }
  out = mergeFacesets(sets);
  return true;
}

void copyNumber(const json &from, json &to, const char *key) {
  const json &v = member(from, key);
  if (v.is_number())
    to[key] = v;
}

void copyArray3(const json &from, json &to, const char *key) {
  const json &v = member(from, key);
  if (v.is_array())
    to[key] = v;
}

json sanitizeFinite(const json &v, int depth) {
  if (v.is_number_float()) {
    const double d = v.get<double>();
    return std::isfinite(d) ? v : json(nullptr);
  }
  if (v.is_array()) {
    // Element/node value arrays never belong in a persisted study.
    if (v.size() > 1024)
      return json(nullptr);
    json out = json::array();
    for (const json &e : v)
      out.push_back(sanitizeFinite(e, depth + 1));
    return out;
  }
  if (v.is_object()) {
    json out = json::object();
    for (auto it = v.begin(); it != v.end(); ++it) {
      json c = sanitizeFinite(it.value(), depth + 1);
      if (it.value().is_array() && c.is_null())
        continue; // dropped bulk array
      out[it.key()] = std::move(c);
    }
    return out;
  }
  return v;
}

json fieldDescriptor(const json &f, const std::string &nameHint) {
  json d = json::object();
  d["name"] = f.is_object() && f.contains("name") ? f["name"] : json(nameHint);
  for (const char *k : {"unit", "location", "components", "label"})
    if (f.is_object() && f.contains(k) && !f[k].is_array())
      d[k] = f[k];
  return d;
}

} // namespace

// ---- applicability
// -------------------------------------------------------------------------------
bool usesStructuralBoundary(const std::string &p) {
  return p == "structural" || p == "modal" || p == "buckling" ||
         p == "thermomechanical";
}
bool usesStructuralLoads(const std::string &p) {
  return p == "structural" || p == "buckling" || p == "thermomechanical";
}
bool usesThermalBoundary(const std::string &p) {
  return p == "thermal" || p == "thermomechanical" || p == "cht";
}
bool usesFlowBoundary(const std::string &p) {
  return p == "flow" || p == "cht";
}

std::string studyPhysics(const json &study) {
  const json &p = member(member(study, "topology"), "physics");
  return p.is_string() ? p.get<std::string>() : std::string();
}

// ---- wells
// ------------------------------------------------------------------------------------------
std::vector<WellRef> listWells(const json &study) {
  std::vector<WellRef> out;
  visitWells(study, [&](const WellVisit &w) {
    out.push_back({w.pointer, w.role, w.active});
  });
  return out;
}

void collectFaceKeys(const json &study, WellRole role,
                     std::vector<std::uint64_t> &keys) {
  visitWells(study, [&](const WellVisit &w) {
    if (!w.active || w.role != role)
      return;
    std::uint32_t b = 0, t = 0;
    if (w.bare) {
      if (pickOf(*w.faces, b, t))
        keys.push_back(faceKey(b, t));
      return;
    }
    for (const json &sel : *w.faces)
      if (pickOf(sel, b, t))
        keys.push_back(faceKey(b, t));
  });
}

void collectRegionBoxSegments(const json &study, bool preserve,
                              std::vector<float> &segs) {
  const json &arr = member(study, preserve ? "preserve" : "exclude");
  if (!arr.is_array())
    return;
  for (const json &e : arr) {
    double lo[3], hi[3];
    if (!vec3At(e, "box_min_mm", lo) || !vec3At(e, "box_max_mm", hi))
      continue;
    float c[8][3];
    for (int i = 0; i < 8; ++i)
      for (int k = 0; k < 3; ++k)
        c[i][k] = static_cast<float>(((i >> k) & 1) ? hi[k] : lo[k]);
    static const int kEdges[12][2] = {{0, 1}, {2, 3}, {4, 5}, {6, 7},
                                      {0, 2}, {1, 3}, {4, 6}, {5, 7},
                                      {0, 4}, {1, 5}, {2, 6}, {3, 7}};
    for (const auto &ed : kEdges)
      segs.insert(segs.end(), {c[ed[0]][0], c[ed[0]][1], c[ed[0]][2],
                               c[ed[1]][0], c[ed[1]][1], c[ed[1]][2]});
  }
}

bool wellExists(const json &study, const std::string &pointer) {
  if (pointer.empty() || !study.is_object())
    return false;
  try {
    const json::json_pointer p(pointer);
    return study.contains(p) && study.at(p).is_array();
  } catch (...) {
    return false;
  }
}

int toggleFace(json &study, const std::string &pointer, const json &selector,
               std::uint32_t body, std::uint32_t topo,
               std::size_t *countAfter) {
  json *faces = facesAt(study, pointer);
  if (!faces)
    return 0;
  for (std::size_t i = 0; i < faces->size(); ++i) {
    std::uint32_t b = 0, t = 0;
    if (pickOf((*faces)[i], b, t) && b == body && t == topo) {
      faces->erase(i);
      if (countAfter)
        *countAfter = faces->size();
      return -1;
    }
  }
  json sel = selector;
  sel["_pick"] = {{"body", body}, {"topo", topo}};
  faces->push_back(std::move(sel));
  if (countAfter)
    *countAfter = faces->size();
  return 1;
}

std::size_t appendFaces(json &study, const std::string &pointer,
                        const std::vector<json> &selectors) {
  json *faces = facesAt(study, pointer);
  if (!faces)
    return 0;
  std::size_t added = 0;
  for (const json &sel : selectors) {
    std::uint32_t b = 0, t = 0;
    const bool identity = pickOf(sel, b, t);
    bool dup = false;
    for (const json &have : *faces) {
      std::uint32_t hb = 0, ht = 0;
      const bool haveIdentity = pickOf(have, hb, ht);
      if (identity && haveIdentity ? (hb == b && ht == t)
                                  : selectorKey(have) == selectorKey(sel)) {
        dup = true;
        break;
      }
    }
    if (!dup) {
      faces->push_back(sel);
      ++added;
    }
  }
  return added;
}

EntryKind fixtureKindFor(const std::string &physics) {
  if (physics == "thermal" || physics == "cht")
    return EntryKind::ThermalFixture;
  if (physics == "flow")
    return EntryKind::FlowWall;
  return EntryKind::Fixture;
}

std::string addEntry(json &study, EntryKind kind, int loadCase) {
  if (!study.is_object())
    return {};
  auto push = [&](const std::string &listPointer, json entry) -> std::string {
    const json::json_pointer p(listPointer);
    if (!study.contains(p) || !study[p].is_array())
      study[p] = json::array();
    json &arr = study[p];
    arr.push_back(std::move(entry));
    const std::string base = listPointer + "/" + std::to_string(arr.size() - 1);
    return arr.back().contains("faces") ? base + "/faces" : std::string();
  };
  auto ensureObject = [&](const char *key) {
    if (!study.contains(key) || !study[key].is_object())
      study[key] = json::object();
  };
  std::string casePrefix;
  if (loadCase >= 0) {
    const json &cases = member(study, "load_cases");
    if (!cases.is_array() || loadCase >= static_cast<int>(cases.size()))
      return {};
    casePrefix = idx("/load_cases", static_cast<std::size_t>(loadCase));
  }
  const json faces = {{"faces", json::array()}};
  switch (kind) {
  case EntryKind::Fixture:
    return push(casePrefix + "/fixtures", faces);
  case EntryKind::ForceLoad: {
    json e = faces;
    e["type"] = "force";
    return push(casePrefix + "/loads", e);
  }
  case EntryKind::PressureLoad: {
    json e = faces;
    e["type"] = "pressure";
    return push(casePrefix + "/loads", e);
  }
  case EntryKind::PreserveFaces:
    return push("/preserve", faces);
  case EntryKind::PreserveBox:
    push("/preserve", json::object());
    return {};
  case EntryKind::ExcludeFaces:
    return push("/exclude", faces);
  case EntryKind::ExcludeBox:
    push("/exclude", json::object());
    return {};
  case EntryKind::ThermalFixture:
    ensureObject("thermal");
    return push("/thermal/fixtures", faces);
  case EntryKind::ThermalFlux:
  case EntryKind::ThermalPower:
  case EntryKind::ThermalConvection: {
    ensureObject("thermal");
    json e = faces;
    e["type"] = kind == EntryKind::ThermalFlux    ? "flux"
                : kind == EntryKind::ThermalPower ? "power"
                                                  : "convection";
    return push("/thermal/loads", e);
  }
  case EntryKind::FlowInlet:
    ensureObject("flow");
    return push("/flow/inlets", faces);
  case EntryKind::FlowOutlet:
    ensureObject("flow");
    return push("/flow/outlets", faces);
  case EntryKind::FlowWall:
    ensureObject("flow");
    return push("/flow/walls", faces);
  }
  return {};
}

// ---- material
// -------------------------------------------------------------------------------------
// Plain name of a material constant key, for messages ("k_w_mk" ->
// "thermal conductivity"); never the raw key.
static std::string materialKeyLabel(const std::string &k) {
  if (k == "E_mpa")
    return "Young's modulus";
  if (k == "nu")
    return "Poisson's ratio";
  if (k == "yield_mpa")
    return "yield strength";
  if (k == "density_g_cm3")
    return "density";
  if (k == "k_w_mk")
    return "thermal conductivity";
  if (k == "alpha_per_k")
    return "thermal expansion coefficient";
  if (k == "cp_j_kgk")
    return "specific heat";
  return "material constant";
}

std::vector<std::string> requiredMaterialKeys(const std::string &p) {
  if (p == "structural" || p == "buckling")
    return {"E_mpa", "nu"};
  if (p == "modal")
    return {"E_mpa", "nu", "density_g_cm3"};
  if (p == "thermal" || p == "cht")
    return {"k_w_mk"};
  if (p == "thermomechanical")
    return {"E_mpa", "nu", "k_w_mk", "alpha_per_k"};
  return {};
}

bool runMaterial(const json &study, const MaterialLookup &lookup, json &out,
                 std::string &error) {
  out = json::object();
  const json &m = member(study, "material");
  if (m.is_string()) {
    const std::string key = m.get<std::string>();
    if (key.empty()) {
      error = "No material is assigned.";
      return false;
    }
    json phys;
    if (!lookup || !lookup(key, phys)) {
      error = "Material \"" + key + "\" is not in the material library.";
      return false;
    }
    for (const char *k : kMaterialKeys)
      copyNumber(phys, out, k);
    const json &name = member(phys, "name");
    out["name"] = name.is_string() ? name : json(key);
    return true;
  }
  if (!m.is_object()) {
    error = "No material is assigned.";
    return false;
  }
  const json &key = member(m, "key");
  if (key.is_string() && !key.get<std::string>().empty() && lookup) {
    json phys;
    if (lookup(key.get<std::string>(), phys)) {
      for (const char *k : kMaterialKeys)
        copyNumber(phys, out, k);
      const json &name = member(phys, "name");
      if (name.is_string())
        out["name"] = name;
    }
  }
  for (const char *k : kMaterialKeys)
    copyNumber(m, out, k);
  const json &name = member(m, "name");
  if (name.is_string() && !name.get<std::string>().empty())
    out["name"] = name;
  else if (!out.contains("name") && key.is_string())
    out["name"] = key;
  return true;
}

// ---- validation
// -------------------------------------------------------------------------------------
std::vector<Issue> validateBoundaries(const json &study,
                                      const MaterialLookup &lookup) {
  std::vector<Issue> out;
  const std::string physics = studyPhysics(study);
  if (physics.empty())
    return out; // validateTopology reports the missing physics
  const std::string pname = physicsName(physics);

  // Mesh: tet4 only (never silently changed).
  const json &order = member(study, "mesh_order");
  if (!order.is_null() && !(order.is_number_integer() && order.get<int>() == 1))
    issue(out, "/mesh_order", Severity::Blocking,
          "Set the element order to Linear: topology optimization meshes "
          "linear tetrahedra.");
  double meshV = 0.0;
  for (const char *key : {"mesh_target_mm", "mesh_min_mm"}) {
    const json &size = member(study, key);
    if (!size.is_null() &&
        !(finiteAt(study, key, meshV) && meshV > 0.0))
      issue(out, std::string("/") + key, Severity::Blocking,
            "Mesh sizes must be finite lengths > 0; clear the field for automatic sizing.");
  }
  if (!member(study, "mesh_fineness").is_null() &&
      !(finiteAt(study, "mesh_fineness", meshV) && meshV >= 0.0 && meshV <= 1.0))
    issue(out, "/mesh_fineness", Severity::Blocking,
          "Mesh fineness must be within [0, 1].");

  // Material.
  json mat;
  std::string matErr;
  if (!runMaterial(study, lookup, mat, matErr)) {
    if (physics != "flow")
      issue(out, "/material", Severity::Blocking, matErr);
  } else {
    for (const std::string &k : requiredMaterialKeys(physics))
      if (!mat.contains(k)) {
        Issue i;
        i.key = "/material/" + k;
        i.message = "Enter the " + materialKeyLabel(k) +
                    " in Material (the library entry has none).";
        i.fieldMessage = "Required for " + pname + "; the library entry has none.";
        out.push_back(std::move(i));
      }
    double v = 0.0;
    if (finiteAt(mat, "E_mpa", v) && v <= 0.0)
      issue(out, "/material/E_mpa", Severity::Blocking,
            "Young's modulus must be > 0.");
    if (finiteAt(mat, "nu", v) && !(v > -1.0 && v < 0.5))
      issue(out, "/material/nu", Severity::Blocking,
            "Poisson's ratio must be in (-1, 0.5).");
    if (finiteAt(mat, "k_w_mk", v) && v <= 0.0)
      issue(out, "/material/k_w_mk", Severity::Blocking,
            "Conductivity must be > 0.");
    if (finiteAt(mat, "density_g_cm3", v) && v <= 0.0)
      issue(out, "/material/density_g_cm3", Severity::Blocking,
            "Density must be > 0.");
    for (const char *key : {"yield_mpa", "cp_j_kgk"})
      if (finiteAt(mat, key, v) && v <= 0.0)
        issue(out, std::string("/material/") + key, Severity::Blocking,
              std::string(std::strcmp(key, "yield_mpa") == 0 ? "Yield strength" : "Specific heat") +
                  " must be > 0.");
  }

  auto checkFixtures = [&](const json &arr, const std::string &base,
                           const std::string &what) -> std::size_t {
    std::size_t n = 0;
    if (!arr.is_array())
      return 0;
    for (std::size_t i = 0; i < arr.size(); ++i) {
      const json &e = arr[i];
      const std::string ptr = idx(base.c_str(), i);
      ++n;
      if (isBareSelector(e))
        continue;
      const std::string label =
          entryLabel(e, what + " " + std::to_string(i + 1));
      if (!facesNonEmpty(e))
        issue(out, ptr + "/faces", Severity::Blocking,
              label + " has no faces.");
      const json &d = member(e, "dofs");
      if (!d.is_null()) {
        bool ok = d.is_array() && !d.empty();
        std::set<int> seen;
        if (ok)
          for (const json &c : d)
            ok = ok && c.is_number_integer() && c.get<int>() >= 0 &&
                 c.get<int>() <= 2 && seen.insert(c.get<int>()).second;
        if (!ok)
          issue(out, ptr + "/dofs", Severity::Blocking,
                label + " must fix at least one of X, Y, Z.");
      }
    }
    return n;
  };
  auto checkLoads = [&](const json &arr, const std::string &base) {
    if (!arr.is_array())
      return;
    for (std::size_t i = 0; i < arr.size(); ++i) {
      const json &e = arr[i];
      const std::string ptr = idx(base.c_str(), i);
      // Same name as the load's card ("Force 1", "Pressure 2").
      const bool pressureCard =
          member(e, "type").is_string() && member(e, "type") == "pressure";
      const std::string label = entryLabel(
          e, (pressureCard ? "Pressure " : "Force ") + std::to_string(i + 1));
      if (!facesNonEmpty(e))
        issue(out, ptr + "/faces", Severity::Blocking,
              label + " has no faces.");
      const std::string type = member(e, "type").is_string()
                                   ? member(e, "type").get<std::string>()
                                   : std::string("force");
      if (type == "pressure") {
        double p = 0.0;
        if (!finiteAt(e, "magnitude", p) || p == 0.0)
          issue(out, ptr + "/magnitude", Severity::Blocking,
                label + " needs a non-zero pressure.");
      } else if (type == "force") {
        double v[3];
        if (!vec3At(e, "vec", v) && !vec3At(e, "vector", v))
          issue(out, ptr + "/vec", Severity::Blocking,
                label + " needs a force vector.");
        else if (v[0] == 0.0 && v[1] == 0.0 && v[2] == 0.0)
          issue(out, ptr + "/vec", Severity::Blocking,
                label + " needs a non-zero force.");
      } else {
        issue(out, ptr + "/type", Severity::Blocking,
              label + " is neither a force nor a pressure; delete it and add "
                      "the load again.");
      }
    }
  };

  if (usesStructuralBoundary(physics)) {
    std::size_t fixtures =
        checkFixtures(member(study, "fixtures"), "/fixtures", "Fixture");
    const json &cases = member(study, "load_cases");
    const bool haveCases = cases.is_array() && !cases.empty();
    const json &loads = member(study, "loads");
    const bool haveLoads = loads.is_array() && !loads.empty();
    if (physics == "modal") {
      if (haveCases || haveLoads)
        issue(out, "", Severity::Warning,
              "Modal topology uses fixtures only; the saved loads are kept but "
              "not sent.");
    } else if (haveCases) {
      if (haveLoads)
        issue(out, "/loads", Severity::Blocking,
              "Loads outside the load cases are ambiguous once load cases "
              "exist; move them "
              "into a case.");
      std::set<std::string> names;
      for (std::size_t c = 0; c < cases.size(); ++c) {
        const json &cs = cases[c];
        const std::string base = idx("/load_cases", c);
        const std::string name = member(cs, "name").is_string()
                                     ? member(cs, "name").get<std::string>()
                                     : "";
        if (name.empty())
          issue(out, base + "/name", Severity::Blocking,
                "Load case " + std::to_string(c + 1) + " needs a name.");
        else if (!names.insert(name).second)
          issue(out, base + "/name", Severity::Blocking,
                "Load case name \"" + name + "\" is used twice.");
        double w = 0.0;
        if (!member(cs, "weight").is_null() &&
            !(finiteAt(cs, "weight", w) && w > 0.0))
          issue(out, base + "/weight", Severity::Blocking,
                "Load case " + (name.empty() ? std::to_string(c + 1) : name) +
                    " weight must be finite and > 0.");
        fixtures += checkFixtures(member(cs, "fixtures"), base + "/fixtures",
                                  "Case fixture");
        const json &cl = member(cs, "loads");
        if ((!cl.is_array() || cl.empty()) && physics != "thermomechanical")
          issue(out, base + "/loads", Severity::Blocking,
                "Load case " + (name.empty() ? std::to_string(c + 1) : name) +
                    " has no loads.");
        checkLoads(cl, base + "/loads");
      }
    } else if (!haveLoads && physics != "thermomechanical") {
      issue(out, "/loads", Severity::Blocking,
            "Add at least one force or pressure load.");
    }
    if (!haveCases)
      checkLoads(loads, "/loads");
    if (fixtures == 0)
      issue(out, "/fixtures", Severity::Blocking,
            "Add a fixture that stops rigid-body motion.");
  }

  if (usesThermalBoundary(physics)) {
    const json &th = member(study, "thermal");
    if (!th.is_object()) {
      issue(out, "/thermal", Severity::Blocking,
            "Add thermal boundary conditions.");
    } else {
      double ref = 0.0;
      if (!member(th, "reference_temperature_c").is_null() &&
          !(finiteAt(th, "reference_temperature_c", ref) && ref > kAbsZeroC))
        issue(out, "/thermal/reference_temperature_c", Severity::Blocking,
              "Reference temperature must be above absolute zero.");
      if (physics == "thermomechanical" &&
          member(th, "reference_temperature_c").is_null())
        issue(out, "/thermal/reference_temperature_c", Severity::Blocking,
              "Set the strain-free reference temperature.");
      bool constrained = false, driven = false;
      const json &fx = member(th, "fixtures");
      if (fx.is_array())
        for (std::size_t i = 0; i < fx.size(); ++i) {
          const std::string ptr = idx("/thermal/fixtures", i);
          const std::string label =
              entryLabel(fx[i], "Fixed temperature " + std::to_string(i + 1));
          if (!facesNonEmpty(fx[i]))
            issue(out, ptr + "/faces", Severity::Blocking,
                  label + " has no faces.");
          double t = 0.0;
          if (!(finiteAt(fx[i], "temperature_c", t) && t > kAbsZeroC))
            issue(out, ptr + "/temperature_c", Severity::Blocking,
                  label + " needs a temperature above absolute zero.");
          constrained = true;
        }
      const json &ld = member(th, "loads");
      if (ld.is_array())
        for (std::size_t i = 0; i < ld.size(); ++i) {
          const json &e = ld[i];
          const std::string ptr = idx("/thermal/loads", i);
          const std::string type = member(e, "type").is_string()
                                       ? member(e, "type").get<std::string>()
                                       : "";
          // Same name as the heat load's card ("Flux 1", "Convection 2").
          const std::string label = entryLabel(
              e, std::string(type == "power"        ? "Power "
                             : type == "convection" ? "Convection "
                                                    : "Flux ") +
                     std::to_string(i + 1));
          if (!facesNonEmpty(e))
            issue(out, ptr + "/faces", Severity::Blocking,
                  label + " has no faces.");
          double v = 0.0;
          if (type == "flux") {
            driven = true;
            if (!(finiteAt(e, "magnitude_w_m2", v) && v != 0.0))
              issue(out, ptr + "/magnitude_w_m2", Severity::Blocking,
                    label + " needs a non-zero heat flux.");
          } else if (type == "power") {
            driven = true;
            if (!(finiteAt(e, "power_w", v) && v != 0.0))
              issue(out, ptr + "/power_w", Severity::Blocking,
                    label + " needs a non-zero power.");
          } else if (type == "convection") {
            constrained = true;
            if (!(finiteAt(e, "h_w_m2k", v) && v > 0.0))
              issue(out, ptr + "/h_w_m2k", Severity::Blocking,
                    label + " needs a film coefficient > 0.");
            if (!(finiteAt(e, "ambient_c", v) && v > kAbsZeroC))
              issue(out, ptr + "/ambient_c", Severity::Blocking,
                    label +
                        " needs an ambient temperature above absolute zero.");
          } else {
            issue(out, ptr + "/type", Severity::Blocking,
                  label + " has an unknown type; delete it and add it again "
                          "as a flux, power or convection load.");
          }
        }
      if (!constrained && physics != "cht")
        issue(out, "/thermal/fixtures", Severity::Blocking,
              "Add a fixed temperature or a convection boundary.");
      if (!driven && physics != "thermomechanical")
        issue(out, "/thermal/loads", Severity::Warning,
              "No flux or power input: only fixed-temperature differences "
              "drive heat flow.");
    }
  }

  if (usesFlowBoundary(physics)) {
    const json &fl = member(study, "flow");
    if (!fl.is_object()) {
      issue(out, "/flow", Severity::Blocking,
            "Add the fluid properties, an inlet and an outlet.");
    } else {
      double v = 0.0;
      if (!(finiteAt(fl, "viscosity_pa_s", v) && v > 0.0))
        issue(out, "/flow/viscosity_pa_s", Severity::Blocking,
              "Dynamic viscosity (Pa s) must be > 0.");
      if (!(finiteAt(fl, "density_kg_m3", v) && v > 0.0))
        issue(out, "/flow/density_kg_m3", Severity::Blocking,
              "Fluid density (kg/m3) must be > 0.");
      if (!member(fl, "brinkman_max").is_null() &&
          !(finiteAt(fl, "brinkman_max", v) && v > 0.0))
        issue(out, "/flow/brinkman_max", Severity::Blocking,
              "Brinkman maximum must be > 0.");
      if (!member(fl, "reference_length_mm").is_null() &&
          !(finiteAt(fl, "reference_length_mm", v) && v > 0.0))
        issue(out, "/flow/reference_length_mm", Severity::Blocking,
              "Reference length must be > 0.");
      if (physics == "cht") {
        if (!(finiteAt(fl, "fluid_k_w_mk", v) && v > 0.0))
          issue(out, "/flow/fluid_k_w_mk", Severity::Blocking,
                "Enter the fluid's thermal conductivity.");
        if (!(finiteAt(fl, "fluid_cp_j_kgk", v) && v > 0.0))
          issue(out, "/flow/fluid_cp_j_kgk", Severity::Blocking,
                "Enter the fluid's specific heat.");
      }
      const json &in = member(fl, "inlets");
      if (!in.is_array() || in.empty())
        issue(out, "/flow/inlets", Severity::Blocking,
              "Add at least one velocity inlet.");
      else
        for (std::size_t i = 0; i < in.size(); ++i) {
          const std::string ptr = idx("/flow/inlets", i);
          const std::string label =
              entryLabel(in[i], "Inlet " + std::to_string(i + 1));
          if (!facesNonEmpty(in[i]))
            issue(out, ptr + "/faces", Severity::Blocking,
                  label + " has no faces.");
          double u[3];
          if (!vec3At(in[i], "velocity_m_s", u) ||
              (u[0] == 0.0 && u[1] == 0.0 && u[2] == 0.0))
            issue(out, ptr + "/velocity_m_s", Severity::Blocking,
                  label + " needs a non-zero inflow velocity.");
          double t = 0.0;
          if (physics == "cht" &&
              !(finiteAt(in[i], "temperature_c", t) && t > kAbsZeroC))
            issue(
                out, ptr + "/temperature_c", Severity::Blocking,
                label +
                    " needs an inlet temperature for conjugate heat transfer.");
        }
      const json &outl = member(fl, "outlets");
      if (!outl.is_array() || outl.empty())
        issue(out, "/flow/outlets", Severity::Blocking,
              "Add at least one pressure outlet.");
      else
        for (std::size_t i = 0; i < outl.size(); ++i) {
          const std::string ptr = idx("/flow/outlets", i);
          const std::string label =
              entryLabel(outl[i], "Outlet " + std::to_string(i + 1));
          if (!facesNonEmpty(outl[i]))
            issue(out, ptr + "/faces", Severity::Blocking,
                  label + " has no faces.");
          if (!finiteAt(outl[i], "pressure_pa", v))
            issue(out, ptr + "/pressure_pa", Severity::Blocking,
                  label + " needs an outlet pressure.");
        }
      const json &walls = member(fl, "walls");
      if (walls.is_array())
        for (std::size_t i = 0; i < walls.size(); ++i)
          if (!facesNonEmpty(walls[i]))
            issue(out, idx("/flow/walls", i) + "/faces", Severity::Blocking,
                  "Wall " + std::to_string(i + 1) + " has no faces.");
    }
  }

  // Regions.
  for (const char *which : {"preserve", "exclude"}) {
    const json &arr = member(study, which);
    if (!arr.is_array())
      continue;
    const std::string title =
        std::string(which) == "preserve" ? "Preserve" : "Exclude";
    for (std::size_t i = 0; i < arr.size(); ++i) {
      const json &e = arr[i];
      const std::string ptr = idx((std::string("/") + which).c_str(), i);
      // Same name as the region's card ("Exclude band 1", "Preserve box 2").
      const std::string label = entryLabel(
          e, title + (e.contains("faces") ? " band " : " box ") + std::to_string(i + 1));
      if (e.contains("faces")) {
        if (!facesNonEmpty(e))
          issue(out, ptr + "/faces", Severity::Blocking,
                label + " has no faces.");
        double t = 0.0;
        if (!member(e, "thickness_mm").is_null() &&
            !(finiteAt(e, "thickness_mm", t) && t >= 0.0))
          issue(out, ptr + "/thickness_mm", Severity::Blocking,
                label + " thickness must be a finite length >= 0.");
      } else {
        double lo[3], hi[3];
        if (!vec3At(e, "box_min_mm", lo) || !vec3At(e, "box_max_mm", hi))
          issue(out, ptr, Severity::Blocking,
                label + " box needs finite minimum and maximum corners.");
        else if (!(lo[0] < hi[0] && lo[1] < hi[1] && lo[2] < hi[2]))
          issue(out, ptr, Severity::Blocking,
                label +
                    " box minimum must be below its maximum on every axis.");
      }
    }
  }
  // Fixed-solid vs fixed-void conflicts on the same picked face.
  std::vector<std::uint64_t> pres, excl, bc;
  collectFaceKeys(study, WellRole::Preserve, pres);
  collectFaceKeys(study, WellRole::Exclude, excl);
  collectFaceKeys(study, WellRole::Support, bc);
  collectFaceKeys(study, WellRole::Load, bc);
  collectFaceKeys(study, WellRole::Port, bc);
  const std::set<std::uint64_t> exclSet(excl.begin(), excl.end());
  for (std::uint64_t k : pres)
    if (exclSet.count(k)) {
      issue(out, "/exclude", Severity::Blocking,
            "A face is both preserved and excluded; a region cannot be fixed "
            "solid and "
            "fixed void.");
      break;
    }
  for (std::uint64_t k : bc)
    if (exclSet.count(k)) {
      issue(out, "/exclude", Severity::Blocking,
            "An excluded (keep-out) face carries a fixture, load or port; "
            "boundary "
            "conditions stay solid.");
      break;
    }
  // The issue line under a field sits inside its entry's card, whose title
  // already names the entry: give it the short form.
  for (Issue &i : out)
    if (i.fieldMessage.empty())
      i.fieldMessage = localIssueText(study, i.key, i.message);
  return out;
}
bool applyStudyDefinition(json &study, const json &definition,
                          const Capabilities &caps,
                          const MaterialLookup &lookup, std::string &error) {
  error.clear();
  if (!definition.is_object()) {
    error = "Study definition must be a JSON object.";
    return false;
  }
  json candidate = definition;
  for (const char *key : {"id", "type", "runs", "last_job", "results", "result"}) {
    if (candidate.contains(key) &&
        (!study.contains(key) || candidate[key] != study[key])) {
      error = std::string("/") + key + " is managed by the study, not editable.";
      return false;
    }
    if (study.contains(key))
      candidate[key] = study[key];
  }

  // Only native setup fields are replaceable by omission. Future/agent fields
  // survive an editor that does not know them, including inside retained entries.
  std::set<std::string> native = {
      "name", "material", "mesh_order", "mesh_target_mm", "mesh_fineness",
      "mesh_min_mm", "fixtures", "loads", "load_cases", "preserve", "exclude",
      "thermal", "flow", "topology", "build_plate", "print_process", "method", "physics", "objective",
      "key", "E_mpa", "nu", "yield_mpa", "density_g_cm3", "k_w_mk",
      "alpha_per_k", "cp_j_kgk", "faces", "dofs", "label", "type", "vec",
      "vector", "magnitude", "weight", "box_min_mm", "box_max_mm",
      "thickness_mm", "reference_temperature_c", "temperature_c",
      "magnitude_w_m2", "power_w", "h_w_m2k", "ambient_c", "inlets",
      "outlets", "walls", "velocity_m_s", "pressure_pa", "viscosity_pa_s",
      "density_kg_m3", "fluid_k_w_mk", "fluid_cp_j_kgk", "brinkman_max",
      "reference_length_mm"};
  for (const auto &key : scalarKeys())
    native.insert(key);
  std::function<void(const json &, json &, bool)> keepUnknown =
      [&](const json &old, json &next, bool extension) {
        if (old.is_object() && next.is_object()) {
          for (auto it = old.begin(); it != old.end(); ++it) {
            if (!next.contains(it.key())) {
              if (extension || !native.count(it.key()))
                next[it.key()] = it.value();
            } else if (it.key() != "faces") {
              keepUnknown(it.value(), next[it.key()],
                          extension || !native.count(it.key()));
            }
          }
        } else if (old.is_array() && next.is_array()) {
          // Preserve extension fields only on entries whose native fields still
          // identify the same entry; never attach old metadata after a reorder.
          for (auto &entry : next) {
            if (!entry.is_object())
              continue;
            for (const auto &previous : old) {
              if (!previous.is_object())
                continue;
              bool same = true, identified = false;
              for (const char *key : {"name", "label", "faces", "type",
                                      "box_min_mm", "box_max_mm"}) {
                if (previous.contains(key) || entry.contains(key)) {
                  identified = true;
                  if (member(previous, key) != member(entry, key)) {
                    same = false;
                    break;
                  }
                }
              }
              if (same && identified) {
                keepUnknown(previous, entry, extension);
                break;
              }
            }
          }
        }
      };
  keepUnknown(study, candidate, false);
  const std::string physics = studyPhysics(candidate);
  auto keepInactive = [&](const char *key, bool active) {
    if (!active && study.contains(key)) {
      if (!candidate.contains(key))
        candidate[key] = study[key];
      else
        keepUnknown(study[key], candidate[key], true);
    }
  };
  keepInactive("fixtures", usesStructuralBoundary(physics));
  keepInactive("loads", usesStructuralLoads(physics));
  keepInactive("load_cases", usesStructuralLoads(physics));
  keepInactive("thermal", usesThermalBoundary(physics));
  keepInactive("flow", usesFlowBoundary(physics));

  auto type = [&](const json &object, const char *key, const std::string &path,
                  const auto &valid,
                  const char *expected) {
    if (!object.contains(key) || valid(object[key]))
      return true;
    error = path + "/" + key + " must be " + expected + ".";
    return false;
  };
  auto object = [](const json &v) { return v.is_object(); };
  auto array = [](const json &v) { return v.is_array(); };
  auto text = [](const json &v) { return v.is_string(); };
  auto number = [](const json &v) {
    return v.is_number() && std::isfinite(v.get<double>());
  };
  auto vector = [&](const json &v) {
    return v.is_array() && v.size() == 3 &&
           number(v[0]) && number(v[1]) && number(v[2]);
  };
  if (!type(candidate, "name", "", text, "text") ||
      !type(candidate, "print_process", "", text, "text") ||
      !type(candidate, "topology", "", object, "an object"))
    return false;
  for (const char *key : {"thermal", "flow", "build_plate"})
    if (!type(candidate, key, "", object, "an object"))
      return false;
  for (const char *key : {"fixtures", "loads", "load_cases", "preserve", "exclude"})
    if (!type(candidate, key, "", array, "an array"))
      return false;
  for (const char *key : {"mesh_order", "mesh_target_mm", "mesh_min_mm",
                          "mesh_fineness"})
    if (!type(candidate, key, "", number, "a finite number"))
      return false;
  if (candidate.contains("material") &&
      !candidate["material"].is_string() && !candidate["material"].is_object()) {
    error = "/material must be a library key or a physical-property object.";
    return false;
  }
  if (member(candidate, "material").is_object()) {
    const json &m = candidate["material"];
    for (const char *key : kMaterialKeys)
      if (!type(m, key, "/material", number, "a finite number"))
        return false;
    for (const char *key : {"name", "key"})
      if (!type(m, key, "/material", text, "text"))
        return false;
  }
  // Typed canonical boundary objects prevent malformed inactive sections from
  // being silently retained as if they were usable native configuration.
  std::function<bool(const json &, const std::string &)> boundaryTypes =
      [&](const json &value, const std::string &path) {
        if (value.is_array()) {
          for (std::size_t i = 0; i < value.size(); ++i) {
            if (!value[i].is_object()) {
              error = path + "/" + std::to_string(i) + " must be an object.";
              return false;
            }
            if (!boundaryTypes(value[i], path + "/" + std::to_string(i)))
              return false;
          }
          return true;
        }
        if (!value.is_object())
          return true;
        for (const char *key : {
                 "weight", "magnitude", "thickness_mm", "reference_temperature_c",
                 "temperature_c", "magnitude_w_m2", "power_w", "h_w_m2k",
                 "ambient_c", "pressure_pa", "viscosity_pa_s", "density_kg_m3",
                 "fluid_k_w_mk", "fluid_cp_j_kgk", "brinkman_max",
                 "reference_length_mm"})
          if (!type(value, key, path, number, "a finite number"))
            return false;
        for (const char *key : {"vec", "vector", "box_min_mm", "box_max_mm",
                                "velocity_m_s"})
          if (!type(value, key, path, vector, "three finite numbers"))
            return false;
        for (const char *key : {"type", "name", "label"})
          if (!type(value, key, path, text, "text"))
            return false;
        if (isBareSelector(value) && value.contains("points") &&
            !validateResolvedFaceset(value, path, error))
          return false;
        if (value.contains("faces")) {
          if (!type(value, "faces", path, array, "a selector array"))
            return false;
          for (const auto &selector : value["faces"]) {
            if (!((selector.is_object() && !selector.empty()) ||
                  (selector.is_string() &&
                   !selector.get_ref<const std::string &>().empty()))) {
              error = path + "/faces needs native selectors or resolved point sets.";
              return false;
            }
            if (selector.is_object() && selector.contains("_pick")) {
              std::uint32_t body = 0, topo = 0;
              if (!pickOf(selector, body, topo)) {
                error = path + "/faces/_pick needs valid positive body and face identities.";
                return false;
              }
            }
            if (selector.is_object() && selector.contains("points") &&
                !validateResolvedFaceset(selector, path + "/faces", error))
              return false;
          }
        }
        if (value.contains("dofs")) {
          if (!value["dofs"].is_array() || value["dofs"].empty()) {
            error = path + "/dofs must be a nonempty array of axes 0, 1 or 2.";
            return false;
          }
          std::set<int> seen;
          for (const auto &dof : value["dofs"]) {
            if (!dof.is_number_integer() || dof < 0 || dof > 2 ||
                !seen.insert(dof.get<int>()).second) {
              error = path + "/dofs needs distinct axes 0, 1 or 2.";
              return false;
            }
          }
        }
        for (const char *key : {"fixtures", "loads", "inlets", "outlets", "walls"})
          if (!type(value, key, path, array, "an array") ||
              (value.contains(key) &&
               !boundaryTypes(value[key], path + "/" + key)))
            return false;
        return true;
      };
  for (const char *key : {"fixtures", "loads", "load_cases", "preserve",
                          "exclude", "thermal", "flow", "build_plate"})
    if (candidate.contains(key) &&
        !boundaryTypes(candidate[key], std::string("/") + key))
      return false;

  auto issues = validateTopology(member(candidate, "topology"), caps, candidate);
  for (auto &issue : issues)
    if (!issue.key.empty())
      issue.key = "/topology/" + issue.key;
  const auto boundaries = validateBoundaries(candidate, lookup);
  issues.insert(issues.end(), boundaries.begin(), boundaries.end());
  for (const auto &issue : issues)
    if (issue.severity == Severity::Blocking) {
      error = issue.key.empty() ? issue.message : issue.key + ": " + issue.message;
      return false;
    }
  study = std::move(candidate);
  return true;
}


// ---- envelope
// --------------------------------------------------------------------------------------
std::string selectorKey(const json &selector) {
  return stripPick(selector).dump();
}

std::vector<json> selectorsToResolve(const json &study) {
  std::vector<json> out;
  std::set<std::string> seen;
  visitWells(study, [&](const WellVisit &w) {
    if (!w.active)
      return;
    auto take = [&](const json &sel) {
      if (sel.is_object() && sel.contains("points"))
        return;
      const std::string k = selectorKey(sel);
      if (seen.insert(k).second)
        out.push_back(stripPick(sel));
    };
    if (w.bare)
      take(*w.faces);
    else
      for (const json &sel : *w.faces)
        take(sel);
  });
  return out;
}

bool buildRunEnvelope(const json &study,
                      const std::map<std::string, json> &resolved,
                      const std::string &stepPath, const json &material,
                      json &out, std::string &error) {
  const std::string physics = studyPhysics(study);
  if (physics.empty()) {
    error = "Choose a physics before running.";
    return false;
  }
  out = json::object();
  out["step_path"] = stepPath;
  out["material"] = material;
  out["topology"] = member(study, "topology");
  out["mesh_order"] = 1;
  double v = 0.0;
  if (finiteAt(study, "mesh_fineness", v))
    out["fineness"] = v;
  if (finiteAt(study, "mesh_target_mm", v) && v > 0.0)
    out["target_size_mm"] = v;
  if (finiteAt(study, "mesh_min_mm", v) && v > 0.0)
    out["min_size_mm"] = v;

  auto fixturesOut = [&](const json &arr, const std::string &what,
                         json &dst) -> bool {
    dst = json::array();
    if (!arr.is_array())
      return true;
    for (std::size_t i = 0; i < arr.size(); ++i) {
      const json &e = arr[i];
      const std::string label =
          entryLabel(e, what + " " + std::to_string(i + 1));
      json item = json::object();
      json fs;
      if (isBareSelector(e)) {
        if (!resolveFaces(json::array({e}), resolved, fs, error, label))
          return false;
      } else {
        if (!resolveFaces(member(e, "faces"), resolved, fs, error, label))
          return false;
        if (member(e, "dofs").is_array())
          item["dofs"] = e["dofs"];
      }
      item["faces"] = std::move(fs);
      item["label"] = label;
      dst.push_back(std::move(item));
    }
    return true;
  };
  auto loadsOut = [&](const json &arr, json &dst) -> bool {
    dst = json::array();
    if (!arr.is_array())
      return true;
    for (std::size_t i = 0; i < arr.size(); ++i) {
      const json &e = arr[i];
      const std::string label = entryLabel(e, "Load " + std::to_string(i + 1));
      json item = json::object();
      json fs;
      if (!resolveFaces(member(e, "faces"), resolved, fs, error, label))
        return false;
      item["faces"] = std::move(fs);
      item["label"] = label;
      const std::string type = member(e, "type").is_string()
                                   ? e["type"].get<std::string>()
                                   : "force";
      item["type"] = type;
      if (type == "pressure") {
        copyNumber(e, item, "magnitude");
      } else {
        const json &vec = e.contains("vec") ? e["vec"] : member(e, "vector");
        item["vector"] = vec;
      }
      dst.push_back(std::move(item));
    }
    return true;
  };

  if (usesStructuralBoundary(physics)) {
    json fx;
    if (!fixturesOut(member(study, "fixtures"), "Fixture", fx))
      return false;
    out["fixtures"] = std::move(fx);
  }
  if (usesStructuralLoads(physics)) {
    const json &cases = member(study, "load_cases");
    if (cases.is_array() && !cases.empty()) {
      json oc = json::array();
      for (std::size_t c = 0; c < cases.size(); ++c) {
        const json &cs = cases[c];
        json item = json::object();
        item["name"] = member(cs, "name").is_string()
                           ? cs["name"]
                           : json("Case " + std::to_string(c + 1));
        copyNumber(cs, item, "weight");
        if (member(cs, "fixtures").is_array() && !cs["fixtures"].empty()) {
          json cf;
          if (!fixturesOut(cs["fixtures"], "Case fixture", cf))
            return false;
          item["fixtures"] = std::move(cf);
        }
        json cl;
        if (!loadsOut(member(cs, "loads"), cl))
          return false;
        item["loads"] = std::move(cl);
        oc.push_back(std::move(item));
      }
      out["load_cases"] = std::move(oc);
    } else {
      json ol;
      if (!loadsOut(member(study, "loads"), ol))
        return false;
      out["loads"] = std::move(ol);
    }
  }
  if (usesThermalBoundary(physics)) {
    const json &th = member(study, "thermal");
    json ot = json::object();
    copyNumber(th, ot, "reference_temperature_c");
    json ofx = json::array();
    const json &fx = member(th, "fixtures");
    if (fx.is_array())
      for (std::size_t i = 0; i < fx.size(); ++i) {
        const std::string label =
            entryLabel(fx[i], "Fixed temperature " + std::to_string(i + 1));
        json item = json::object();
        json fs;
        if (!resolveFaces(member(fx[i], "faces"), resolved, fs, error, label))
          return false;
        item["faces"] = std::move(fs);
        item["label"] = label;
        copyNumber(fx[i], item, "temperature_c");
        ofx.push_back(std::move(item));
      }
    ot["fixtures"] = std::move(ofx);
    json oload = json::array();
    const json &ld = member(th, "loads");
    if (ld.is_array())
      for (std::size_t i = 0; i < ld.size(); ++i) {
        const std::string label =
            entryLabel(ld[i], "Heat load " + std::to_string(i + 1));
        json item = json::object();
        json fs;
        if (!resolveFaces(member(ld[i], "faces"), resolved, fs, error, label))
          return false;
        item["faces"] = std::move(fs);
        item["label"] = label;
        if (member(ld[i], "type").is_string())
          item["type"] = ld[i]["type"];
        for (const char *k :
             {"magnitude_w_m2", "power_w", "h_w_m2k", "ambient_c"})
          copyNumber(ld[i], item, k);
        oload.push_back(std::move(item));
      }
    ot["loads"] = std::move(oload);
    out["thermal"] = std::move(ot);
  }
  if (usesFlowBoundary(physics)) {
    const json &fl = member(study, "flow");
    json of = json::object();
    for (const char *k : {"viscosity_pa_s", "density_kg_m3", "brinkman_max",
                          "reference_length_mm"})
      copyNumber(fl, of, k);
    if (physics == "cht")
      for (const char *k : {"fluid_k_w_mk", "fluid_cp_j_kgk"})
        copyNumber(fl, of, k);
    auto ports = [&](const char *key, const char *what, json &dst) -> bool {
      dst = json::array();
      const json &arr = member(fl, key);
      if (!arr.is_array())
        return true;
      for (std::size_t i = 0; i < arr.size(); ++i) {
        const std::string label =
            entryLabel(arr[i], std::string(what) + " " + std::to_string(i + 1));
        json item = json::object();
        json fs;
        if (!resolveFaces(member(arr[i], "faces"), resolved, fs, error, label))
          return false;
        item["faces"] = std::move(fs);
        item["label"] = label;
        copyArray3(arr[i], item, "velocity_m_s");
        copyNumber(arr[i], item, "pressure_pa");
        if (physics == "cht")
          copyNumber(arr[i], item, "temperature_c");
        dst.push_back(std::move(item));
      }
      return true;
    };
    json in, ou, wa;
    if (!ports("inlets", "Inlet", in) || !ports("outlets", "Outlet", ou) ||
        !ports("walls", "Wall", wa))
      return false;
    of["inlets"] = std::move(in);
    of["outlets"] = std::move(ou);
    if (!wa.empty())
      of["walls"] = std::move(wa);
    out["flow"] = std::move(of);
  }
  for (const char *which : {"preserve", "exclude"}) {
    const json &arr = member(study, which);
    if (!arr.is_array() || arr.empty())
      continue;
    json dst = json::array();
    const std::string title =
        std::string(which) == "preserve" ? "Preserve" : "Exclude";
    for (std::size_t i = 0; i < arr.size(); ++i) {
      const json &e = arr[i];
      const std::string label =
          entryLabel(e, title + " " + std::to_string(i + 1));
      json item = json::object();
      item["label"] = label;
      if (e.contains("faces")) {
        json fs;
        if (!resolveFaces(e["faces"], resolved, fs, error, label))
          return false;
        item["faces"] = std::move(fs);
        copyNumber(e, item, "thickness_mm");
      } else {
        copyArray3(e, item, "box_min_mm");
        copyArray3(e, item, "box_max_mm");
      }
      dst.push_back(std::move(item));
    }
    out[which] = std::move(dst);
  }
  const json &plate = member(study, "build_plate");
  if (facesNonEmpty(plate)) {
    json fs;
    if (!resolveFaces(member(plate, "faces"), resolved, fs, error, "Bed face"))
      return false;
    out["build_plate"] = {{"faces", std::move(fs)}};
    const json &label = member(plate, "label");
    if (label.is_string() && !label.get<std::string>().empty())
      out["build_plate"]["label"] = label;
  }
  return true;
}

std::string setupKey(const json &study) {
  json s = json::object();
  for (const char *k :
       {"material", "mesh_order", "mesh_fineness", "mesh_target_mm",
        "mesh_min_mm", "fixtures", "loads", "load_cases", "preserve", "exclude",
        "thermal", "flow", "topology", "build_plate", "print_process"})
    if (study.is_object() && study.contains(k))
      s[k] = study[k];
  // Viewport pick identities are presentation only.
  std::function<void(json &)> strip = [&](json &v) {
    if (v.is_object()) {
      v.erase("_pick");
      for (auto &c : v)
        strip(c);
    } else if (v.is_array()) {
      for (auto &c : v)
        strip(c);
    }
  };
  strip(s);
  // FNV-1a 64: deterministic across builds (the key is persisted in run
  // records).
  std::uint64_t h = 1469598103934665603ull;
  for (unsigned char ch : s.dump()) {
    h ^= ch;
    h *= 1099511628211ull;
  }
  char buf[32];
  std::snprintf(buf, sizeof(buf), "%016llx",
                static_cast<unsigned long long>(h));
  return buf;
}

json compactResult(const json &result) {
  if (!result.is_object())
    return json::object();
  json r = result;
  for (const char *k :
       {"density", "level_set", "design", "x", "nodes", "points", "tets"})
    r.erase(k);
  const json &ov = member(r, "overlay_aicm");
  if (ov.is_string() && ov.get<std::string>().rfind("aicm-shm:", 0) == 0)
    r.erase(
        "overlay_aicm"); // transient handoff segment, not a durable artifact
  // Field descriptors only.
  if (r.contains("fields")) {
    json desc = json::array();
    const json &f = r["fields"];
    if (f.is_array()) {
      for (const json &e : f)
        desc.push_back(fieldDescriptor(e, ""));
    } else if (f.is_object()) {
      for (auto it = f.begin(); it != f.end(); ++it)
        desc.push_back(fieldDescriptor(it.value(), it.key()));
    }
    r["fields"] = std::move(desc);
  }
  // Thin the per-iteration history uniformly (keep the last sample).
  if (r.contains("history") && r["history"].is_object()) {
    json &h = r["history"];
    const json &it = member(h, "iteration");
    const std::size_t n = it.is_array() ? it.size() : 0;
    constexpr std::size_t kMax = 256;
    if (n > kMax) {
      const std::size_t stride = (n + kMax - 1) / kMax;
      for (auto kv = h.begin(); kv != h.end(); ++kv) {
        if (!kv.value().is_array() || kv.value().size() != n)
          continue;
        json thin = json::array();
        for (std::size_t i = 0; i < n; ++i)
          if (i % stride == 0 || i + 1 == n)
            thin.push_back(kv.value()[i]);
        kv.value() = std::move(thin);
      }
      h["thinned_from"] = n;
    }
  }
  return sanitizeFinite(r, 0);
}

} // namespace topology
} // namespace ui

#ifndef CHUDWARE_SIM_TOPOLOGY_MODEL_ONLY
// ==================================================================================================
// Editors (GUI thread) — Simulation UI standard: removable cards per entry,
// kit_form fields bound by JSON pointer (unit slot, required marker, issue
// line), one face-well layout, "+ Add" buttons once per section.
// ==================================================================================================
#include "imgui.h"
#include "imgui_internal.h" // ImGuiWindow: scroll the editor's body to a focused card

#include "../core/app_state.h"
#include "metrics.h"
#include "sim_manager.h" // SimManager::unitSys — the Simulate workspace's display units
#include "viewport_panel.h" // claimViewportHoverFace — face-chip hover highlight
#include "widgets/kit.h"
#include "sim/sim_interaction.h"

namespace ui {
namespace topology {
namespace {

namespace kit = ui::kit;

struct EditCtx {
  AppState &st;
  json &study;
  BcView &view;
  const std::vector<Issue> &issues;
  bool locked = false;
  BcResult result;
  SimManager::UnitSys units;
  ImGuiWindow *body = nullptr; // the scrolling window the editor draws into
  bool focusHandled = false;   // the view's focus was revealed this frame
};

// ---- one-shot scroll focus (BcView::focusPointer) ----------------------------
// True while the pending focus addresses `pointer` itself or a value inside it.
bool focusWithin(const EditCtx &c, const std::string &pointer) {
  const std::string &f = c.view.focusPointer;
  if (f.empty() || c.focusHandled || pointer.empty())
    return false;
  return f == pointer || (f.size() > pointer.size() &&
                          f.compare(0, pointer.size(), pointer) == 0 &&
                          f[pointer.size()] == '/');
}

// Scrolls the editor's body so the screen-space span [top, bottom] shows (its
// top a row gap under the view's top edge); nothing moves when it already
// shows. Marks the focus handled, so an enclosing card ending later (load case
// around its loads) does not override the deeper target.
void revealSpan(EditCtx &c, float top, float bottom) {
  c.focusHandled = true;
  ImGuiWindow *w = c.body;
  if (!w)
    return;
  if (top >= w->InnerRect.Min.y && bottom <= w->InnerRect.Max.y)
    return;
  ImGui::SetScrollFromPosY(w, top - w->Pos.y - kit::rowGap(), 0.0f);
}

// A list-level focus ("/fixtures") lands on the list's section header: call
// right before drawing it.
void focusAnchor(EditCtx &c, const std::string &listPointer) {
  if (c.focusHandled || c.view.focusPointer != listPointer)
    return;
  const float y = ImGui::GetCursorScreenPos().y;
  revealSpan(c, y, y + ImGui::GetFrameHeight() * 3.0f);
}

// After a card that owns `pointers` ended (the card is the last item): reveal
// it when the focus addresses one of them.
void focusCard(EditCtx &c, float top, std::initializer_list<const char *> pointers) {
  for (const char *p : pointers)
    if (focusWithin(c, p)) {
      revealSpan(c, top, ImGui::GetItemRectMax().y);
      return;
    }
}

kit::Tone toneOf(const Issue &i) {
  return i.severity == Severity::Blocking ? kit::Tone::Err : kit::Tone::Warn;
}

// The issue keyed exactly at `pointer` (a field's own issue line), or null.
const Issue *issueAt(const EditCtx &c, const std::string &pointer) {
  if (pointer.empty())
    return nullptr;
  for (const Issue &i : c.issues)
    if (i.key == pointer)
      return &i;
  return nullptr;
}

// `o` plus the pointer's issue (Err when blocking, Warn otherwise).
kit::FieldOpts withIssueAt(const EditCtx &c, const std::string &pointer,
                           kit::FieldOpts o) {
  if (const Issue *i = issueAt(c, pointer))
    o.withIssue(i->fieldMessage.empty() ? i->message.c_str() : i->fieldMessage.c_str(),
                toneOf(*i));
  return o;
}

// Every issue keyed at `pointer` as inline lines (list-level keys).
void issuesFor(const EditCtx &c, const std::string &pointer) {
  if (pointer.empty())
    return;
  for (const Issue &i : c.issues)
    if (i.key == pointer)
      kit::inlineStatus(i.fieldMessage.empty() ? i.message.c_str() : i.fieldMessage.c_str(),
                        toneOf(i));
}

// ---- JSON pointer binding ----------------------------------------------------
// Fields read through these and write only on commit, so drawing never inserts
// keys into an authored study.
const json *jsonAt(const json &root, const std::string &pointer) {
  try {
    const json::json_pointer p(pointer);
    if (root.contains(p))
      return &root.at(p);
  } catch (...) {
  }
  return nullptr;
}

json *arrayAt(json &root, const std::string &pointer) {
  try {
    const json::json_pointer p(pointer);
    if (root.contains(p) && root.at(p).is_array())
      return &root.at(p);
  } catch (...) {
  }
  return nullptr;
}

int countAt(const json &root, const std::string &pointer) {
  const json *a = jsonAt(root, pointer);
  return a && a->is_array() ? static_cast<int>(a->size()) : 0;
}

void setAt(EditCtx &c, const std::string &pointer, json value) {
  try {
    c.study[json::json_pointer(pointer)] = std::move(value);
    c.result.committed = true;
  } catch (...) {
  }
}

void eraseAt(EditCtx &c, const std::string &pointer) {
  try {
    const json::json_pointer p(pointer);
    const json::json_pointer pp = p.parent_pointer();
    json &parent = pp.empty() ? c.study : c.study.at(pp);
    if (parent.is_object() && parent.erase(p.back()) > 0)
      c.result.committed = true;
  } catch (...) {
  }
}

// ---- fields ------------------------------------------------------------------
// Number at `pointer` (stored SI, shown through o's scale). An empty commit
// erases the key; nothing is silently defaulted.
void numberField(EditCtx &c, const char *id, const std::string &pointer,
                 const char *label, kit::FieldOpts o) {
  const json *cur = jsonAt(c.study, pointer);
  bool present = cur && cur->is_number();
  double v = present ? cur->get<double>() : 0.0;
  o.withPresent(&present);
  if (kit::fieldNumber(id, label, &v, withIssueAt(c, pointer, o))) {
    if (present)
      setAt(c, pointer, v);
    else
      eraseAt(c, pointer);
  }
}

// X/Y/Z triple at `pointer`; a first committed cell creates the whole vector
// (other cells 0). `altPointer` is the dialect alias read when `pointer` is
// absent (force "vector"); a commit persists `pointer` and drops the alias.
void vectorField(EditCtx &c, const char *id, const std::string &pointer,
                 const char *label, kit::FieldOpts o,
                 const std::string &altPointer = std::string()) {
  const json *cur = jsonAt(c.study, pointer);
  bool fromAlt = false;
  if (!cur && !altPointer.empty()) {
    cur = jsonAt(c.study, altPointer);
    fromAlt = cur != nullptr;
  }
  bool present = cur && cur->is_array() && cur->size() == 3;
  double v[3] = {0.0, 0.0, 0.0};
  if (present)
    for (int k = 0; k < 3; ++k)
      v[k] = (*cur)[k].is_number() ? (*cur)[k].get<double>() : 0.0;
  o.withPresent(&present);
  if (kit::fieldVec3(id, label, v, withIssueAt(c, pointer, o))) {
    if (fromAlt)
      eraseAt(c, altPointer);
    setAt(c, pointer, json::array({v[0], v[1], v[2]}));
  }
}

// Text at `pointer`; a blank commit erases the key.
void textField(EditCtx &c, const char *id, const std::string &pointer,
               const char *label, const kit::FieldOpts &o) {
  const json *cur = jsonAt(c.study, pointer);
  std::string v = cur && cur->is_string() ? cur->get<std::string>() : std::string();
  if (kit::fieldText(id, label, &v, withIssueAt(c, pointer, o))) {
    if (v.find_first_not_of(" \t") == std::string::npos)
      eraseAt(c, pointer);
    else
      setAt(c, pointer, v);
  }
}

// The face well (standard §4): the SAME controls as the FEA wells
// (faceWellControls: label line with the count, then "Pick faces" /
// "Picking…" and Clear at one frame height, Clear dropping under only when
// the row is too narrow), the picks as removable chips in a wrapping flow
// (hover lights the face in the viewport, × removes it), one dim hint line
// while armed, and the field's issue line.
void wellField(EditCtx &c, const std::string &pointer, const char *help,
               const char *label = "Faces", bool createPlate = false) {
  json *faces = facesAt(c.study, pointer);
  if (!faces && !createPlate)
    return;
  const auto ensureFaces = [&] {
    if (!faces) {
      c.study["build_plate"] = {{"faces", json::array()}};
      faces = facesAt(c.study, pointer);
    }
  };
  const bool armed = c.view.armedPointer == pointer;
  std::size_t n = faces ? faces->size() : 0;
  ImGui::PushID("faces");
  const FaceWellClick click = faceWellControls(label, (int)n, armed, help);
  ImGui::PopID();
  // A real well target, independent of Pick/Clear: delivery copies the face
  // and never toggles an already-present selector off.
  ImGui::PushID("face_drop");
  kit::secondaryButton("Drop a face here", !c.locked && !c.view.docId.empty(),
                       c.locked ? "Setup is locked while running."
                                : "Drag a model face or another face chip here.",
                       ImVec2(ImGui::GetContentRegionAvail().x, 0));
  ui::sim::FaceDrop dropped;
  if (!c.locked && !c.view.docId.empty() &&
      ui::sim::acceptFaceDrop(c.view.docId, dropped)) {
    ensureFaces();
    dropped.selector["_pick"] = {{"body", dropped.body}, {"topo", dropped.topo}};
    if (appendFaces(c.study, pointer, {dropped.selector}))
      c.result.committed = true;
  }
  n = faces ? faces->size() : 0;
  ImGui::PopID();
  const bool toggle = click.toggled;
  const bool clear = click.clear;
  if (toggle) {
    if (!armed)
      ensureFaces();
    c.view.armedPointer = armed ? std::string() : pointer;
    c.result.armChanged = true;
  }

  int removeAt = -1;
  if (n) {
    kit::chipFlowBegin("chips");
    // Chips name the model face itself ("Face 6", as the status bar does), not the list
    // position; the body is added only when this well spans more than one body.
    std::uint32_t firstBody = 0;
    bool multiBody = false;
    for (std::size_t k = 0; k < n; ++k) {
      std::uint32_t b = 0, t = 0;
      if (!pickOf((*faces)[k], b, t))
        continue;
      if (firstBody == 0)
        firstBody = b;
      else if (b != firstBody)
        multiBody = true;
    }
    for (std::size_t k = 0; k < n; ++k) {
      ImGui::PushID(static_cast<int>(k));
      const json &sel = (*faces)[k];
      const bool pointSet = sel.is_object() && sel.contains("points");
      std::uint32_t body = 0, topo = 0;
      const bool picked = pickOf(sel, body, topo);
      std::string text;
      if (pointSet)
        text = "Point set " + std::to_string(k + 1);
      else if (picked)
        text = "Face " + std::to_string(topo) +
               (multiBody ? " of Body " + std::to_string(body) : std::string());
      else
        text = "Face selector " + std::to_string(k + 1);
      bool hovered = false;
      if (kit::removableChip("face", text.c_str(),
                             pointSet ? "Remove this point set."
                                      : "Remove this face.",
                             kit::Tone::Neutral, &hovered))
        removeAt = static_cast<int>(k);
      if (!c.locked && picked && !c.view.docId.empty())
        ui::sim::faceDragSource(c.view.docId, sel, body, topo, text.c_str());
      ImGui::PopID();
      if (!hovered)
        continue;
      if (picked)
        ui::claimViewportHoverFace(body, topo);
      else
        kit::tooltip(pointSet
                         ? "Pre-resolved point set (agent-authored)."
                         : "Face selector without a viewport identity "
                           "(agent-authored); it resolves at run time but "
                           "cannot be highlighted.");
    }
    kit::chipFlowEnd();
  }
  if (armed)
    kit::dimText("Click model faces to add or remove them.");
  if (const Issue *i = issueAt(c, pointer))
    kit::inlineStatus(i->fieldMessage.empty() ? i->message.c_str()
                                              : i->fieldMessage.c_str(),
                      toneOf(*i));
  if (removeAt >= 0) {
    faces->erase(static_cast<std::size_t>(removeAt));
    c.result.committed = true;
  } else if (clear && faces) {
    faces->clear();
    c.result.committed = true;
  }
}

// "+ X" creation buttons of one section, wrapping as a flow. Returns the
// clicked index or -1.
int addButtons(const char *id, const char *const *labels,
               const char *const *tips, int n) {
  const ImGuiStyle &style = ImGui::GetStyle();
  int hit = -1;
  kit::chipFlowBegin(id);
  for (int k = 0; k < n; ++k) {
    kit::chipFlowItem(ImGui::CalcTextSize(labels[k]).x +
                      style.FramePadding.x * 2.0f);
    if (kit::secondaryButton(labels[k]))
      hit = k;
    kit::itemTooltip(tips ? tips[k] : nullptr);
  }
  kit::chipFlowEnd();
  return hit;
}

// A well created in the editor ("+ Fixture") arms and is scrolled into view
// once its card is drawn (the next frame).
void armNew(EditCtx &c, const std::string &pointer) {
  if (!pointer.empty()) {
    c.view.armedPointer = pointer;
    c.view.focusPointer = pointer;
    c.view.focusFrames = 0;
    c.result.armChanged = true;
  }
  c.result.committed = true;
}

// One removable card per element of the array at `listPointer` (PushID index
// scopes each card). Removal is applied after the loop and disarms the well,
// whose pointer would otherwise shift onto a sibling.
struct CardHead {
  std::string title;
  const char *icon = nullptr;
  const char *removeTip = nullptr;
  bool removable = true;
};
void entryCards(
    EditCtx &c, const std::string &listPointer,
    const std::function<CardHead(const json &, std::size_t)> &head,
    const std::function<void(json &, const std::string &, std::size_t)> &body) {
  json *arr = arrayAt(c.study, listPointer);
  if (!arr)
    return;
  int removeAt = -1;
  for (std::size_t i = 0; i < arr->size(); ++i) {
    json &e = (*arr)[i];
    const CardHead h = head(e, i);
    const std::string ptr = idx(listPointer.c_str(), i);
    const float top = ImGui::GetCursorScreenPos().y;
    ImGui::PushID(static_cast<int>(i));
    bool removed = false;
    const bool open =
        h.removable ? kit::removableCard("card", h.title.c_str(), h.icon,
                                         nullptr, h.removeTip, &removed)
                    : kit::cardBegin("card", h.title.c_str(), h.icon);
    if (open)
      body(e, ptr, i);
    kit::cardEnd();
    ImGui::PopID();
    // Nested lists (a load case's loads) end first, so the deepest card wins.
    if (focusWithin(c, ptr))
      revealSpan(c, top, ImGui::GetItemRectMax().y);
    if (removed)
      removeAt = static_cast<int>(i);
  }
  if (removeAt >= 0) {
    arr->erase(static_cast<std::size_t>(removeAt));
    c.view.armedPointer.clear();
    c.result.armChanged = true;
    c.result.committed = true;
  }
}

// ---- structural
// -----------------------------------------------------------------------------------
// X/Y/Z fixed directions as checkboxes (the same control as the symmetry
// axes); all three on erases "dofs" (fully clamped, the shared default).
void dofField(EditCtx &c, json &e, const std::string &ptr) {
  bool on[3] = {true, true, true};
  const json &d = member(e, "dofs");
  if (d.is_array()) {
    on[0] = on[1] = on[2] = false;
    for (const json &k : d)
      if (k.is_number_integer() && k.get<int>() >= 0 && k.get<int>() <= 2)
        on[k.get<int>()] = true;
  }
  static const char *const kTips[3] = {"Hold the faces in X.",
                                       "Hold the faces in Y.",
                                       "Hold the faces in Z."};
  kit::fieldBegin("dofs", "Fixed directions",
                  withIssueAt(c, ptr + "/dofs",
                              kit::FieldOpts().withTip(
                                  "Checked directions are held; the others "
                                  "stay free (roller / slider supports). All "
                                  "three = fully clamped.")));
  const bool changed = axisToggles(on, kTips);
  kit::fieldEnd();
  if (!changed)
    return;
  if (on[0] && on[1] && on[2]) {
    e.erase("dofs");
  } else {
    json dofs = json::array();
    for (int k = 0; k < 3; ++k)
      if (on[k])
        dofs.push_back(k);
    e["dofs"] = dofs; // an empty set is reported as a blocking issue
  }
  c.result.committed = true;
}

void fixtureList(EditCtx &c, const std::string &listPointer, int loadCase) {
  ImGui::PushID(listPointer.c_str());
  entryCards(
      c, listPointer,
      [](const json &e, std::size_t i) {
        return CardHead{entryLabel(e, "Fixture " + std::to_string(i + 1)),
                        "fixture", "Delete this fixture.", true};
      },
      [&](json &e, const std::string &ptr, std::size_t) {
        if (isBareSelector(e)) {
          kit::dimText("One face selector authored outside the editor (all "
                       "three directions fixed).");
          if (kit::secondaryButton("Make editable")) {
            json sel = e;
            e = json{{"faces", json::array({sel})}};
            c.result.committed = true;
          }
          kit::itemTooltip("Wrap the selector in an editable fixture (faces + "
                           "fixed directions); the constraint is unchanged.");
          return;
        }
        kit::FieldGroup g("f", "Fixed directions");
        wellField(c, ptr + "/faces", "The faces this fixture holds.");
        dofField(c, e, ptr);
      });
  static const char *const kAdd[1] = {"+ Fixture"};
  static const char *const kShared[1] = {"A support shared by every load case."};
  static const char *const kCase[1] = {
      "A support active only in this load case (added to the shared ones)."};
  if (addButtons("add", kAdd, loadCase >= 0 ? kCase : kShared, 1) == 0) {
    if (loadCase >= 0)
      c.view.loadCase = loadCase;
    armNew(c, addEntry(c.study, EntryKind::Fixture, loadCase));
  }
  ImGui::PopID();
}

void loadList(EditCtx &c, const std::string &listPointer, int loadCase) {
  // The load type is chosen at creation (+ Force / + Pressure) and fixed, as
  // in FEA studies: the card title and icon carry it, no type switch.
  static const char *const kKindTips[2] = {
      "A total force vector, split over the faces by area.",
      "A uniform pressure normal to the faces; positive pushes into them."};
  ImGui::PushID(listPointer.c_str());
  entryCards(
      c, listPointer,
      [](const json &e, std::size_t i) {
        const bool pressure =
            member(e, "type").is_string() && e["type"] == "pressure";
        return CardHead{
            entryLabel(e, std::string(pressure ? "Pressure " : "Force ") +
                              std::to_string(i + 1)),
            pressure ? "load_pressure" : "load_force", "Delete this load.",
            true};
      },
      [&](json &e, const std::string &ptr, std::size_t) {
        kit::FieldGroup g("f", "Pressure");
        const bool pressure =
            member(e, "type").is_string() && e["type"] == "pressure";
        issuesFor(c, ptr + "/type"); // agent-authored type not force/pressure
        wellField(c, ptr + "/faces", "The faces this load acts on.");
        if (!pressure)
          vectorField(c, "vec", ptr + "/vec", "Force",
                      kit::FieldOpts()
                          .withUnit(kit::unitText(c.units.force))
                          .withScale(c.units.forceFromN)
                          .withRequired()
                          .withTip("Total force over the selected faces "
                                   "(split by area)."),
                      ptr + "/vector");
        else
          numberField(c, "magnitude", ptr + "/magnitude", "Pressure",
                      kit::FieldOpts()
                          .withUnit(kit::unitText(c.units.press))
                          .withScale(c.units.pressFromMpa)
                          .withRequired()
                          .withTip("Uniform pressure; positive pushes INTO "
                                   "the faces."));
      });
  static const char *const kAdd[2] = {"+ Force", "+ Pressure"};
  const int hit = addButtons("add", kAdd, kKindTips, 2);
  if (hit >= 0) {
    if (loadCase >= 0)
      c.view.loadCase = loadCase;
    armNew(c, addEntry(c.study,
                       hit == 0 ? EntryKind::ForceLoad : EntryKind::PressureLoad,
                       loadCase));
  }
  ImGui::PopID();
}

void loadCases(EditCtx &c) {
  json &study = c.study;
  // Shared loads left over next to cases are ambiguous: offer the explicit
  // move.
  if (countAt(study, "/loads") > 0) {
    issuesFor(c, "/loads");
    if (kit::secondaryButton("Move shared loads into Case 1")) {
      json &cl = study["load_cases"][0]["loads"];
      if (!cl.is_array())
        cl = json::array();
      for (const json &l : study["loads"])
        cl.push_back(l);
      study.erase("loads");
      c.result.committed = true;
    }
  }
  const std::size_t caseCount = study["load_cases"].size();
  ImGui::PushID("cases");
  entryCards(
      c, "/load_cases",
      [caseCount](const json &cs, std::size_t i) {
        return CardHead{entryLabel(cs, "Case " + std::to_string(i + 1)),
                        "load_force", "Delete this load case.",
                        caseCount > 1};
      },
      [&](json &, const std::string &base, std::size_t ci) {
        {
          kit::FieldGroup g("f", "Weight");
          textField(c, "name", base + "/name", "Name",
                    kit::FieldOpts().withRequired().withTip(
                        "Unique load-case name."));
          numberField(c, "weight", base + "/weight", "Weight",
                      kit::FieldOpts().withHint("1 (default)").withTip(
                          "Positive weight of this case in the weighted "
                          "objective."));
        }
        focusAnchor(c, base + "/fixtures");
        kit::sectionHeader("Supports in this case", "fixture", "optional",
                           countAt(c.study, base + "/fixtures"));
        fixtureList(c, base + "/fixtures", static_cast<int>(ci));
        focusAnchor(c, base + "/loads");
        kit::sectionHeader("Loads in this case", "load_force", nullptr,
                           countAt(c.study, base + "/loads"));
        loadList(c, base + "/loads", static_cast<int>(ci));
        issuesFor(c, base + "/loads");
      });
  static const char *const kAdd[2] = {"+ Load case", "Back to a single case"};
  static const char *const kTips[2] = {
      "A new case needs its own loads; weight is optional (solver default 1).",
      "Return to one shared set of loads (this case's supports join the "
      "shared ones)."};
  json &cases = study["load_cases"];
  const int hit = addButtons("add", kAdd, kTips, cases.size() == 1 ? 2 : 1);
  if (hit == 0) {
    json cs = json::object();
    cs["name"] = "Case " + std::to_string(cases.size() + 1);
    cs["loads"] = json::array();
    cases.push_back(std::move(cs));
    c.result.committed = true;
  } else if (hit == 1) {
    json loads = cases[0].contains("loads") ? cases[0]["loads"] : json::array();
    if (countAt(cases[0], "/fixtures") > 0) {
      json &fx = study["fixtures"];
      if (!fx.is_array())
        fx = json::array();
      for (const json &f : cases[0]["fixtures"])
        fx.push_back(f);
    }
    study["loads"] = std::move(loads);
    study.erase("load_cases");
    c.view.loadCase = -1;
    c.view.armedPointer.clear();
    c.result.armChanged = true;
    c.result.committed = true;
  }
  ImGui::PopID();
}

void structuralSection(EditCtx &c, const std::string &physics) {
  focusAnchor(c, "/fixtures");
  kit::sectionHeader("Supports", "fixture", nullptr,
                     countAt(c.study, "/fixtures"), true);
  fixtureList(c, "/fixtures", -1);
  issuesFor(c, "/fixtures");
  if (physics == "modal") {
    kit::inlineStatus("Modal topology uses supports only; loads do not apply.",
                      kit::Tone::Info);
    return;
  }
  json &study = c.study;
  if (countAt(study, "/load_cases") > 0) {
    focusAnchor(c, "/load_cases");
    kit::sectionHeader("Load cases", "load_force", nullptr,
                       countAt(study, "/load_cases"));
    loadCases(c);
    return;
  }
  focusAnchor(c, "/loads");
  kit::sectionHeader("Loads", "load_force", nullptr, countAt(study, "/loads"));
  loadList(c, "/loads", -1);
  issuesFor(c, "/loads");
  ImGui::PushID("cases");
  static const char *const kMulti[1] = {"Use multiple load cases"};
  static const char *const kMultiTip[1] = {
      "Optimize for several independent load cases with weights. The current "
      "loads become Case 1 (weight 1, identical to today's single case)."};
  if (addButtons("multi", kMulti, kMultiTip, 1) == 0) {
    json first = json::object();
    first["name"] = "Case 1";
    first["weight"] = 1.0;
    first["loads"] = countAt(study, "/loads") > 0 ? study["loads"] : json::array();
    study["load_cases"] = json::array({std::move(first)});
    study.erase("loads");
    c.view.loadCase = 0;
    c.view.armedPointer.clear();
    c.result.armChanged = true;
    c.result.committed = true;
  }
  ImGui::PopID();
}

// ---- thermal
// ----------------------------------------------------------------------------------------
void thermalSection(EditCtx &c, const std::string &physics) {
  const bool tm = physics == "thermomechanical";
  if (tm)
    kit::gapSection(); // below the structural supports and loads
  const float cardTop = ImGui::GetCursorScreenPos().y;
  if (kit::cardBegin("thermal_ref", "Thermal", "load_heat")) {
    kit::FieldGroup g("ref", "Reference temperature");
    kit::FieldOpts o = kit::FieldOpts()
                           .withUnit(kit::Unit::DegC)
                           .withRequired(tm)
                           .withTip(tm ? "Temperature of zero thermal strain "
                                         "and of unconstrained nodes."
                                       : "Temperature of unconstrained nodes.");
    if (!tm)
      o.withHint("optional");
    numberField(c, "ref_t", "/thermal/reference_temperature_c",
                tm ? "Strain-free temperature" : "Reference temperature", o);
  }
  issuesFor(c, "/thermal");
  kit::cardEnd();
  focusCard(c, cardTop,
            {c.view.focusPointer == "/thermal"
                 ? "/thermal"
                 : "/thermal/reference_temperature_c"});

  focusAnchor(c, "/thermal/fixtures");
  kit::sectionHeader("Fixed temperatures", "fixture", nullptr,
                     countAt(c.study, "/thermal/fixtures"));
  ImGui::PushID("tfix");
  entryCards(
      c, "/thermal/fixtures",
      [](const json &e, std::size_t i) {
        return CardHead{
            entryLabel(e, "Fixed temperature " + std::to_string(i + 1)),
            "fixture", "Delete this fixed temperature.", true};
      },
      [&](json &, const std::string &ptr, std::size_t) {
        kit::FieldGroup g("f", "Temperature");
        wellField(c, ptr + "/faces", "Faces held at a fixed temperature.");
        numberField(c, "t", ptr + "/temperature_c", "Temperature",
                    kit::FieldOpts()
                        .withUnit(kit::Unit::DegC)
                        .withRequired()
                        .withTip("Prescribed surface temperature."));
      });
  static const char *const kAddFix[1] = {"+ Fixed temperature"};
  static const char *const kAddFixTip[1] = {
      "Faces held at a prescribed temperature."};
  if (addButtons("add", kAddFix, kAddFixTip, 1) == 0)
    armNew(c, addEntry(c.study, EntryKind::ThermalFixture));
  issuesFor(c, "/thermal/fixtures");
  ImGui::PopID();

  // The heat-load type is chosen at creation (+ Flux / + Power / + Convection)
  // and fixed: the card title carries it, no type switch.
  static const char *const kKinds[3] = {"Flux", "Power", "Convection"};
  static const char *const kKindTips[3] = {
      "Heat flux per unit area into the faces (negative = out).",
      "Total heat power over the faces, spread by area.",
      "Convection to a surrounding fluid: film coefficient and ambient "
      "temperature."};
  auto kindOf = [](const json &e) {
    const json &t = member(e, "type");
    const std::string type = t.is_string() ? t.get<std::string>() : "";
    return type == "power" ? 1 : type == "convection" ? 2 : 0;
  };
  focusAnchor(c, "/thermal/loads");
  kit::sectionHeader("Heat loads", "load_heat", nullptr,
                     countAt(c.study, "/thermal/loads"));
  ImGui::PushID("tload");
  entryCards(
      c, "/thermal/loads",
      [&](const json &e, std::size_t i) {
        return CardHead{entryLabel(e, std::string(kKinds[kindOf(e)]) + " " +
                                          std::to_string(i + 1)),
                        "load_heat", "Delete this heat load.", true};
      },
      [&](json &e, const std::string &ptr, std::size_t) {
        kit::FieldGroup g("f", "Ambient temperature");
        const int kind = kindOf(e);
        issuesFor(c, ptr + "/type"); // agent-authored type not flux/power/convection
        wellField(c, ptr + "/faces", "Faces this heat boundary acts on.");
        if (kind == 0) {
          numberField(c, "flux", ptr + "/magnitude_w_m2", "Heat flux",
                      kit::FieldOpts()
                          .withUnit(kit::Unit::W_m2)
                          .withRequired()
                          .withTip("Heat flux into the faces (negative = out)."));
        } else if (kind == 1) {
          numberField(c, "power", ptr + "/power_w", "Power",
                      kit::FieldOpts()
                          .withUnit(kit::Unit::W)
                          .withRequired()
                          .withTip("Total heat power over the faces, spread "
                                   "by area."));
        } else {
          numberField(c, "h", ptr + "/h_w_m2k", "Film coefficient",
                      kit::FieldOpts()
                          .withUnit(kit::Unit::W_m2K)
                          .withRequired()
                          .withTip("Convective heat-transfer coefficient h."));
          numberField(c, "ambient", ptr + "/ambient_c", "Ambient temperature",
                      kit::FieldOpts()
                          .withUnit(kit::Unit::DegC)
                          .withRequired()
                          .withTip("Temperature of the surrounding fluid."));
        }
      });
  static const char *const kAdd[3] = {"+ Flux", "+ Power", "+ Convection"};
  const int hit = addButtons("add", kAdd, kKindTips, 3);
  if (hit >= 0)
    armNew(c, addEntry(c.study, hit == 0   ? EntryKind::ThermalFlux
                                : hit == 1 ? EntryKind::ThermalPower
                                           : EntryKind::ThermalConvection));
  issuesFor(c, "/thermal/loads");
  ImGui::PopID();
}

// ---- flow
// -------------------------------------------------------------------------------------------
void portList(EditCtx &c, const char *key, const char *title, EntryKind kind,
              const std::string &physics) {
  const std::string list = std::string("/flow/") + key;
  ImGui::PushID(key);
  entryCards(
      c, list,
      [title](const json &e, std::size_t i) {
        return CardHead{
            entryLabel(e, std::string(title) + " " + std::to_string(i + 1)),
            "flow_bc", "Delete this boundary.", true};
      },
      [&](json &, const std::string &ptr, std::size_t) {
        kit::FieldGroup g("f", "Inlet temperature");
        wellField(c, ptr + "/faces", "Faces of this boundary.");
        if (kind == EntryKind::FlowInlet) {
          vectorField(c, "u", ptr + "/velocity_m_s", "Velocity",
                      kit::FieldOpts()
                          .withUnit(kit::Unit::m_s)
                          .withRequired()
                          .withTip("Velocity imposed on the inlet faces (must "
                                   "point into the domain)."));
          if (physics == "cht")
            numberField(c, "t", ptr + "/temperature_c", "Inlet temperature",
                        kit::FieldOpts()
                            .withUnit(kit::Unit::DegC)
                            .withRequired()
                            .withTip("Fluid temperature entering through "
                                     "this inlet."));
        } else if (kind == EntryKind::FlowOutlet) {
          numberField(c, "p", ptr + "/pressure_pa", "Pressure",
                      kit::FieldOpts()
                          .withUnit(kit::Unit::Pa)
                          .withRequired()
                          .withTip("Static (gauge) pressure on the outlet "
                                   "faces."));
        }
      });
  const std::string add = std::string("+ ") + title;
  const char *labels[1] = {add.c_str()};
  const char *tips[1] = {kind == EntryKind::FlowInlet
                             ? "A velocity inlet."
                         : kind == EntryKind::FlowOutlet
                             ? "A pressure outlet."
                             : "Every exterior face that is not an inlet or "
                               "outlet is already a no-slip wall; a named wall "
                               "only labels a subset."};
  if (addButtons("add", labels, tips, 1) == 0)
    armNew(c, addEntry(c.study, kind));
  issuesFor(c, list);
  ImGui::PopID();
}

void flowSection(EditCtx &c, const std::string &physics) {
  const bool cht = physics == "cht";
  if (cht)
    kit::gapSection(); // below the thermal boundaries
  const float cardTop = ImGui::GetCursorScreenPos().y;
  if (kit::cardBegin("fluid", "Fluid", "fluid")) {
    {
      kit::FieldGroup g("fluid", "Fluid conductivity");
      numberField(c, "mu", "/flow/viscosity_pa_s", "Dynamic viscosity",
                  kit::FieldOpts()
                      .withUnit(kit::Unit::Pa_s)
                      .withRequired()
                      .withTip("Fluid dynamic viscosity (water ~1.0e-3, air "
                               "~1.8e-5)."));
      numberField(c, "rho", "/flow/density_kg_m3", "Density",
                  kit::FieldOpts()
                      .withUnit(kit::Unit::kg_m3)
                      .withRequired()
                      .withTip("Fluid density."));
      if (cht) {
        numberField(c, "k", "/flow/fluid_k_w_mk", "Fluid conductivity",
                    kit::FieldOpts()
                        .withUnit(kit::Unit::W_mK)
                        .withRequired()
                        .withTip("Thermal conductivity of the fluid."));
        numberField(c, "cp", "/flow/fluid_cp_j_kgk", "Fluid specific heat",
                    kit::FieldOpts()
                        .withUnit(kit::Unit::J_kgK)
                        .withRequired()
                        .withTip("Specific heat capacity of the fluid."));
      }
      numberField(c, "brinkman", "/flow/brinkman_max", "Brinkman maximum",
                  kit::FieldOpts()
                      .withHint("solver default")
                      .withTip("Dimensionless solid impermeability (alpha_max "
                               "= value * mu / L^2)."));
      numberField(c, "lref", "/flow/reference_length_mm", "Reference length",
                  kit::FieldOpts()
                      .withUnit(kit::unitText(c.units.len))
                      .withScale(c.units.lenFromMm)
                      .withHint("solver default")
                      .withTip("Length L for the Brinkman scale and the "
                               "reported Reynolds number."));
    }
    kit::dimText("Laminar (Stokes / Navier-Stokes Brinkman) flow; the "
                 "reported Reynolds number is regime evidence, not a "
                 "turbulence model.");
    issuesFor(c, "/flow");
  }
  kit::cardEnd();
  if (c.view.focusPointer == "/flow")
    focusCard(c, cardTop, {"/flow"});
  else
    focusCard(c, cardTop,
              {"/flow/viscosity_pa_s", "/flow/density_kg_m3",
               "/flow/fluid_k_w_mk", "/flow/fluid_cp_j_kgk",
               "/flow/brinkman_max", "/flow/reference_length_mm"});

  focusAnchor(c, "/flow/inlets");
  kit::sectionHeader("Inlets", "flow_bc", nullptr,
                     countAt(c.study, "/flow/inlets"));
  portList(c, "inlets", "Inlet", EntryKind::FlowInlet, physics);
  focusAnchor(c, "/flow/outlets");
  kit::sectionHeader("Outlets", "flow_bc", nullptr,
                     countAt(c.study, "/flow/outlets"));
  portList(c, "outlets", "Outlet", EntryKind::FlowOutlet, physics);
  focusAnchor(c, "/flow/walls");
  kit::sectionHeader("Walls", "flow_bc", "optional",
                     countAt(c.study, "/flow/walls"));
  portList(c, "walls", "Wall", EntryKind::FlowWall, physics);
}

// ---- regions
// ----------------------------------------------------------------------------------------
void regionList(EditCtx &c, const char *key, bool preserve) {
  const std::string list = std::string("/") + key;
  const char *title = preserve ? "Preserve" : "Exclude";
  ImGui::PushID(key);
  entryCards(
      c, list,
      [title, preserve](const json &e, std::size_t i) {
        const bool band = e.is_object() && e.contains("faces");
        return CardHead{entryLabel(e, std::string(title) +
                                          (band ? " band " : " box ") +
                                          std::to_string(i + 1)),
                        preserve ? "combine" : "subtract", "Delete this region.",
                        true};
      },
      [&](json &e, const std::string &ptr, std::size_t) {
        kit::FieldGroup g("f", "Minimum corner");
        const kit::FieldOpts len = kit::FieldOpts()
                                       .withUnit(kit::unitText(c.units.len))
                                       .withScale(c.units.lenFromMm);
        if (e.is_object() && e.contains("faces")) {
          wellField(c, ptr + "/faces",
                    preserve ? "Faces whose adjacent material always stays "
                               "solid."
                             : "Faces whose adjacent material is always "
                               "removed (keep-out).");
          numberField(c, "depth", ptr + "/thickness_mm", "Band depth",
                      kit::FieldOpts(len)
                          .withHint("Auto")
                          .withTip("Depth below the faces held fixed. Empty "
                                   "uses the study's default region depth "
                                   "(Manufacturing settings)."));
        } else {
          vectorField(c, "min", ptr + "/box_min_mm", "Minimum corner",
                      kit::FieldOpts(len).withRequired().withTip(
                          "Elements whose centroid lies inside the box are "
                          "fixed."));
          vectorField(c, "max", ptr + "/box_max_mm", "Maximum corner",
                      kit::FieldOpts(len).withRequired().withTip(
                          "Opposite corner of the box."));
          issuesFor(c, ptr);
        }
      });
  static const char *const kAdd[2] = {"+ Face band", "+ Box"};
  static const char *const kPreserveTips[2] = {
      "Keep a layer of material under picked faces. Fixture, load and "
      "flow-port faces are already kept (solid, or fluid for ports).",
      "Keep every element inside an axis-aligned box."};
  static const char *const kExcludeTips[2] = {
      "Remove a layer of material under picked faces.",
      "Remove every element inside an axis-aligned box."};
  const int hit =
      addButtons("add", kAdd, preserve ? kPreserveTips : kExcludeTips, 2);
  if (hit == 0) {
    armNew(c, addEntry(c.study, preserve ? EntryKind::PreserveFaces
                                         : EntryKind::ExcludeFaces));
  } else if (hit == 1) {
    addEntry(c.study, preserve ? EntryKind::PreserveBox : EntryKind::ExcludeBox);
    // The new box has no well to arm; still bring its card into view.
    const int n = countAt(c.study, list);
    if (n > 0) {
      c.view.focusPointer = idx(list.c_str(), static_cast<std::size_t>(n - 1));
      c.view.focusFrames = 0;
    }
    c.result.committed = true;
  }
  issuesFor(c, list);
  ImGui::PopID();
}

void regionsSection(EditCtx &c) {
  // The step's readiness dot names a region problem: say it on the page too
  // (the offending card may be below the fold) — unless it is the run's first
  // blocker, which the footer already shows.
  const Issue *first = nullptr;
  for (const Issue &i : c.issues)
    if (i.severity == Severity::Blocking && !first)
      first = &i;
  for (const Issue &i : c.issues)
    if (i.severity == Severity::Blocking &&
        (i.key.rfind("/preserve", 0) == 0 || i.key.rfind("/exclude", 0) == 0)) {
      if (&i != first)
        kit::inlineStatus(i.message.c_str(), kit::Tone::Warn);
      break;
    }
  focusAnchor(c, "/preserve");
  kit::sectionHeader("Preserve", "combine", "Always solid",
                     countAt(c.study, "/preserve"), true);
  regionList(c, "preserve", true);
  focusAnchor(c, "/exclude");
  kit::sectionHeader("Exclude", "subtract", "Keep-out",
                     countAt(c.study, "/exclude"));
  regionList(c, "exclude", false);
}

// ---- material
// ---------------------------------------------------------------------------------------
struct MatKeyInfo {
  const char *key, *label;
  kit::Unit unit;
  bool stress; // shown in the study's stress unit (MPa or psi)
  const char *help;
};
const MatKeyInfo kMatInfo[] = {
    {"E_mpa", "Young's modulus", kit::Unit::MPa, true,
     "Elastic modulus of the solid phase."},
    {"nu", "Poisson's ratio", kit::Unit::None, false,
     "Lateral contraction ratio (-1 < nu < 0.5)."},
    {"yield_mpa", "Yield strength", kit::Unit::MPa, true,
     "Used for stress reporting / limits."},
    {"density_g_cm3", "Density", kit::Unit::g_cm3, false,
     "Mass density (mass and modal physics)."},
    {"k_w_mk", "Thermal conductivity", kit::Unit::W_mK, false,
     "Solid conductivity (thermal physics)."},
    {"alpha_per_k", "Thermal expansion", kit::Unit::perK, false,
     "Linear expansion coefficient (thermomechanical)."},
    {"cp_j_kgk", "Specific heat", kit::Unit::J_kgK, false,
     "Solid specific heat capacity."},
};

std::string materialKey(const json &m) {
  if (m.is_string())
    return m.get<std::string>();
  const json &k = member(m, "key");
  return k.is_string() ? k.get<std::string>() : std::string();
}

void materialSection(
    EditCtx &c, const MaterialLookup &lookup,
    const std::vector<std::pair<std::string, std::string>> &library,
    const std::string &physics) {
  // Work on a copy so drawing never inserts a null `material` into an authored
  // record; it is written back only on a commit.
  const json *cur = jsonAt(c.study, "/material");
  json m = cur ? *cur : json();
  std::string key = materialKey(m);
  const std::vector<std::string> required = requiredMaterialKeys(physics);

  std::vector<std::string> names;
  names.reserve(library.size() + 1);
  int sel = -1;
  for (std::size_t i = 0; i < library.size(); ++i) {
    names.push_back(library[i].second);
    if (!key.empty() && library[i].first == key)
      sel = static_cast<int>(i);
  }
  const int customIdx = static_cast<int>(names.size());
  names.emplace_back("Custom (no library entry)");
  if (sel < 0 && m.is_object() && key.empty())
    sel = customIdx;
  std::string preview;
  if (m.is_null())
    preview = "Choose a material";
  else if (sel < 0)
    preview = key + " (not in the library)";

  bool changed = false;
  const float cardTop = ImGui::GetCursorScreenPos().y;
  if (kit::cardBegin("material", "Material", "material")) {
    kit::FieldGroup g("mat", "Thermal conductivity");
    kit::FieldOpts o =
        withIssueAt(c, "/material",
                    kit::FieldOpts()
                        .withRequired(!required.empty())
                        .withTip("The study's own material. Library entries "
                                 "supply the constants they record; anything "
                                 "the library lacks must be entered below."));
    if (!preview.empty())
      o.withPreview(preview.c_str());
    int pick = sel;
    if (kit::fieldCombo("lib", "Material", &pick, names, o) && pick != sel &&
        pick >= 0) {
      if (pick == customIdx) {
        json custom = json::object();
        if (m.is_object())
          for (const MatKeyInfo &mi : kMatInfo)
            copyNumber(m, custom, mi.key);
        m = std::move(custom);
      } else {
        // Library reference; a typed override below promotes it to an object.
        m = library[static_cast<std::size_t>(pick)].first;
      }
      key = materialKey(m);
      changed = true;
    }

    json lib = json::object();
    if (!key.empty() && lookup)
      lookup(key, lib);
    for (const MatKeyInfo &mi : kMatInfo) {
      const bool req =
          std::find(required.begin(), required.end(), mi.key) != required.end();
      const double scale = mi.stress ? c.units.stressFromMpa : 1.0;
      const json &v = member(m, mi.key);
      bool present = v.is_number();
      double val = present ? v.get<double>() : 0.0;
      const json &lv = member(lib, mi.key);
      const std::string hint =
          lv.is_number() ? kit::fmtValue(lv.get<double>() * scale) + " (library)"
          : req          ? std::string()
                         : std::string("optional");
      kit::FieldOpts fo =
          withIssueAt(c, std::string("/material/") + mi.key,
                      kit::FieldOpts()
                          .withUnit(mi.stress ? kit::unitText(c.units.stress)
                                              : kit::unitText(mi.unit))
                          .withScale(scale)
                          .withRequired(req)
                          .withPresent(&present)
                          .withTip(mi.help));
      if (!hint.empty())
        fo.withHint(hint.c_str());
      if (kit::fieldNumber(mi.key, mi.label, &val, fo)) {
        if (!m.is_object()) {
          json obj = json::object();
          if (!key.empty())
            obj["key"] = key;
          m = std::move(obj);
        }
        if (present)
          m[mi.key] = val;
        else
          m.erase(mi.key);
        changed = true;
      }
    }
    kit::dimText("A typed value overrides the library entry for this study "
                 "only.");
  }
  kit::cardEnd();
  focusCard(c, cardTop, {"/material"});
  if (changed) {
    c.study["material"] = m;
    c.result.committed = true;
  }
}

} // namespace

BcResult drawBuildPlateEditor(AppState &st, json &study, BcView &view,
                              const char *lockedReason) {
  const std::vector<Issue> issues;
  EditCtx c{st, study, view, issues};
  c.locked = lockedReason != nullptr;
  ImGui::PushID("build_plate");
  ImGui::BeginDisabled(c.locked);
  wellField(c, "/build_plate/faces",
            "Pick the planar face that rests on the printer bed. Its inward normal sets the build direction.",
            "Bed face", true);
  ImGui::EndDisabled();
  ImGui::PopID();
  const json &faces = member(member(study, "build_plate"), "faces");
  if (faces.is_array() && faces.empty()) {
    if (c.result.committed && view.armedPointer == "/build_plate/faces") {
      view.armedPointer.clear();
      c.result.armChanged = true;
    }
    if (view.armedPointer != "/build_plate/faces")
      study.erase("build_plate");
  }
  view.seenArmed = view.armedPointer;
  return c.result;
}

BcResult drawBoundaryEditor(
    AppState &st, json &study, BcSection section, BcView &view,
    const MaterialLookup &lookup,
    const std::vector<std::pair<std::string, std::string>> &libraryMaterials,
    const char *lockedReason) {
  const std::vector<Issue> issues = validateBoundaries(study, lookup);
  EditCtx c{st, study, view, issues};
  c.locked = lockedReason != nullptr;
  c.units = SimManager::unitSys(st);
  c.body = ImGui::GetCurrentWindow();
  // A well armed from outside the editor (ribbon command, run-gate jump) is
  // brought into view like an explicit focus.
  if (view.armedPointer != view.seenArmed) {
    if (!view.armedPointer.empty() && view.focusPointer.empty()) {
      view.focusPointer = view.armedPointer;
      view.focusFrames = 0;
    }
    view.seenArmed = view.armedPointer;
  }
  const std::string focusAtStart = view.focusPointer;
  ImGui::PushID("topology_bc");
  if (lockedReason)
    kit::inlineStatus(lockedReason, kit::Tone::Warn);
  ImGui::BeginDisabled(c.locked);
  const std::string physics = studyPhysics(study);
  if (section == BcSection::Material) {
    materialSection(c, lookup, libraryMaterials, physics);
  } else if (section == BcSection::Regions) {
    regionsSection(c);
  } else if (physics.empty()) {
    kit::blockedState(
        "Choose a physics first",
        "Boundary conditions depend on the physics being optimized.",
        "Pick the physics on the Settings step.");
  } else {
    if (usesStructuralBoundary(physics))
      structuralSection(c, physics);
    if (usesThermalBoundary(physics))
      thermalSection(c, physics);
    if (usesFlowBoundary(physics))
      flowSection(c, physics);
    // Sections authored for another physics are kept verbatim but not sent.
    auto nonEmpty = [&](const char *pointer) {
      const json *v = jsonAt(study, pointer);
      return v && (v->is_array() || v->is_object()) && !v->empty();
    };
    std::string kept;
    if (!usesStructuralBoundary(physics) &&
        (nonEmpty("/fixtures") || nonEmpty("/loads") || nonEmpty("/load_cases")))
      kept += " supports/loads";
    if (!usesThermalBoundary(physics) && nonEmpty("/thermal"))
      kept += " thermal";
    if (!usesFlowBoundary(physics) && nonEmpty("/flow"))
      kept += " flow";
    if (!kept.empty())
      kit::inlineStatus(("Saved" + kept +
                         " settings are kept but not used by " +
                         physicsLabel(physics) + ".")
                            .c_str(),
                        kit::Tone::Info);
  }
  // Whole-setup issues.
  for (const Issue &i : issues)
    if (i.key.empty())
      kit::inlineStatus(i.message.c_str(), toneOf(i));
  ImGui::EndDisabled();
  ImGui::PopID();
  // A removed well cannot stay armed.
  if (!view.armedPointer.empty() && !wellExists(study, view.armedPointer)) {
    view.armedPointer.clear();
    c.result.armChanged = true;
  }
  // Wells armed by the editor itself are already in view: no refocus.
  view.seenArmed = view.armedPointer;
  // Focus bookkeeping: a revealed target is held one more frame (a new card's
  // first-frame height is provisional); a focus set during this draw (a new
  // entry drawn next frame) waits; anything else unmatched is dropped.
  if (!view.focusPointer.empty()) {
    if (view.focusPointer != focusAtStart) {
      view.focusFrames = 0;
    } else if (!c.focusHandled || ++view.focusFrames >= 2) {
      view.focusPointer.clear();
      view.focusFrames = 0;
    }
  }
  return c.result;
}

} // namespace topology
} // namespace ui
#endif // CHUDWARE_SIM_TOPOLOGY_MODEL_ONLY
