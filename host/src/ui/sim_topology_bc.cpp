// sim_topology_bc.cpp — see sim_topology_bc.h.
//
// CHUDWARE_SIM_TOPOLOGY_MODEL_ONLY compiles only the pure study model (wells,
// validation, envelope, run records) so a CPU test can link this TU without
// ImGui/AppState; the editor half is excluded in that build, nothing is
// stubbed.
#include "sim_topology_bc.h"

#include <algorithm>
#include <cerrno>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <functional>
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
  if (!b.is_number_unsigned() && !b.is_number_integer())
    return false;
  if (!t.is_number_unsigned() && !t.is_number_integer())
    return false;
  body = b.get<std::uint32_t>();
  topo = t.get<std::uint32_t>();
  return body != 0 && topo != 0;
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
      if (identity ? (pickOf(have, hb, ht) && hb == b && ht == t)
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
          "Topology optimization meshes are linear tetrahedra; set mesh order "
          "to 1 (tet4).");
  double meshV = 0.0;
  if (member(study, "mesh_target_mm").is_number() &&
      !(finiteAt(study, "mesh_target_mm", meshV) && meshV >= 0.0))
    issue(out, "/mesh_target_mm", Severity::Blocking,
          "Mesh target size must be a finite length >= 0.");

  // Material.
  json mat;
  std::string matErr;
  if (!runMaterial(study, lookup, mat, matErr)) {
    if (physics != "flow")
      issue(out, "/material", Severity::Blocking, matErr);
  } else {
    for (const std::string &k : requiredMaterialKeys(physics))
      if (!mat.contains(k))
        issue(
            out, "/material/" + k, Severity::Blocking,
            pname + " needs the material's " + k +
                " (not recorded by the library entry; enter it in Material).");
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
      const std::string label = entryLabel(e, "Load " + std::to_string(i + 1));
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
                label + " force vector is zero.");
      } else {
        issue(out, ptr + "/type", Severity::Blocking,
              label + " type \"" + type + "\" is not force or pressure.");
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
            pname + " needs at least one force or pressure load.");
    }
    if (!haveCases)
      checkLoads(loads, "/loads");
    if (fixtures == 0)
      issue(out, "/fixtures", Severity::Blocking,
            pname + " needs fixtures that stop rigid-body motion.");
  }

  if (usesThermalBoundary(physics)) {
    const json &th = member(study, "thermal");
    if (!th.is_object()) {
      issue(out, "/thermal", Severity::Blocking,
            pname + " needs thermal boundary conditions.");
    } else {
      double ref = 0.0;
      if (!member(th, "reference_temperature_c").is_null() &&
          !(finiteAt(th, "reference_temperature_c", ref) && ref > kAbsZeroC))
        issue(out, "/thermal/reference_temperature_c", Severity::Blocking,
              "Reference temperature must be above absolute zero.");
      if (physics == "thermomechanical" &&
          member(th, "reference_temperature_c").is_null())
        issue(out, "/thermal/reference_temperature_c", Severity::Blocking,
              "Thermomechanical needs the strain-free reference temperature.");
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
          const std::string label =
              entryLabel(e, "Heat load " + std::to_string(i + 1));
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
                  label + " must be flux, power or convection.");
          }
        }
      if (!constrained && physics != "cht")
        issue(out, "/thermal/fixtures", Severity::Blocking,
              "The temperature field needs a fixed temperature or a convection "
              "boundary.");
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
            pname + " needs fluid properties, inlets and outlets.");
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
                "Conjugate heat transfer needs the fluid conductivity.");
        if (!(finiteAt(fl, "fluid_cp_j_kgk", v) && v > 0.0))
          issue(out, "/flow/fluid_cp_j_kgk", Severity::Blocking,
                "Conjugate heat transfer needs the fluid specific heat.");
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
      const std::string label =
          entryLabel(e, title + " " + std::to_string(i + 1));
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
  return out;
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
  return true;
}

std::string setupKey(const json &study) {
  json s = json::object();
  for (const char *k :
       {"material", "mesh_order", "mesh_fineness", "mesh_target_mm",
        "mesh_min_mm", "fixtures", "loads", "load_cases", "preserve", "exclude",
        "thermal", "flow", "topology"})
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
// Editors (GUI thread)
// ==================================================================================================
#include <cfloat>
#include <cstring>

#include "imgui.h"

#include "../core/app_state.h"
#include "sim_manager.h" // SimManager::unitSys — the Simulate workspace's display units
#include "widgets/kit.h"

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
};

void tip(const char *t) {
  if (t && *t && ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled))
    ImGui::SetTooltip("%s", t);
}

std::string fmtNum(double v) {
  char b[48];
  std::snprintf(b, sizeof(b), "%.6g", v);
  return b;
}

bool parseNumber(const std::string &raw, double &out) {
  std::string s = raw;
  s.erase(0, s.find_first_not_of(" \t"));
  s.erase(s.find_last_not_of(" \t") + 1);
  if (s.empty())
    return false;
  errno = 0;
  char *end = nullptr;
  out = std::strtod(s.c_str(), &end);
  return errno == 0 && end && *end == '\0' && std::isfinite(out);
}

bool isBlank(const char *s) {
  for (; s && *s; ++s)
    if (*s != ' ' && *s != '\t')
      return false;
  return true;
}

// Issues whose key is `pointer` or nested under it.
void issuesFor(const EditCtx &c, const std::string &pointer) {
  for (const Issue &i : c.issues)
    if (!pointer.empty() && i.key == pointer)
      kit::inlineStatus(i.message.c_str(), i.severity == Severity::Blocking
                                               ? kit::Tone::Err
                                               : kit::Tone::Warn);
}

// One numeric text field bound to obj[key] (SI value scaled by `factor` for
// display). Empty input erases the key. `required` only changes the
// placeholder.
void numberField(EditCtx &c, const std::string &id, const char *label,
                 json &obj, const char *key, double factor, const char *unit,
                 const char *help, const char *placeholder = nullptr) {
  ImGui::PushID(id.c_str());
  ImGui::TextUnformatted(label);
  tip(help);
  std::string shown;
  const json &cur = member(obj, key);
  if (c.view.editingId == id)
    shown = c.view.editText;
  else if (cur.is_number())
    shown = fmtNum(cur.get<double>() * factor);
  char buf[64];
  std::snprintf(buf, sizeof(buf), "%s", shown.c_str());
  const float unitW =
      unit && *unit ? ImGui::CalcTextSize(unit).x + 8.0f * ui::uiScale : 0.0f;
  ImGui::SetNextItemWidth(-FLT_MIN - unitW);
  ImGui::InputTextWithHint("##v", placeholder ? placeholder : "required", buf,
                           sizeof(buf), ImGuiInputTextFlags_CharsScientific);
  const bool active = ImGui::IsItemActive();
  const bool done = ImGui::IsItemDeactivatedAfterEdit();
  if (active) {
    c.view.editingId = id;
    c.view.editText = buf;
  }
  if (unit && *unit) {
    ImGui::SameLine();
    ImGui::TextUnformatted(unit);
  }
  if (done) {
    c.view.editingId.clear();
    c.view.fieldErrors.erase(id);
    double v = 0.0;
    if (isBlank(buf)) {
      if (obj.is_object() && obj.contains(key)) {
        obj.erase(key);
        c.result.committed = true;
      }
    } else if (parseNumber(buf, v)) {
      obj[key] = v / factor;
      c.result.committed = true;
    } else {
      c.view.fieldErrors[id] = std::string("\"") + buf + "\" is not a number.";
    }
  } else if (!active && c.view.editingId == id) {
    c.view.editingId.clear();
  }
  const auto err = c.view.fieldErrors.find(id);
  if (err != c.view.fieldErrors.end())
    kit::inlineStatus(err->second.c_str(), kit::Tone::Err);
  ImGui::PopID();
}

// Three-component vector bound to obj[key]; a component committed on an absent
// vector creates the full vector with the other components shown as 0.
void vectorField(EditCtx &c, const std::string &id, const char *label,
                 json &obj, const char *key, double factor, const char *unit,
                 const char *help, const char *altKey = nullptr) {
  ImGui::PushID(id.c_str());
  ImGui::TextUnformatted(label);
  tip(help);
  if (unit && *unit) {
    ImGui::SameLine();
    kit::dimText(unit);
  }
  const char *srcKey = key;
  if (altKey && !(obj.is_object() && obj.contains(key)) && obj.is_object() &&
      obj.contains(altKey))
    srcKey = altKey;
  const json &cur = member(obj, srcKey);
  const bool have = cur.is_array() && cur.size() == 3;
  static const char *const kAxis[3] = {"X", "Y", "Z"};
  const float w = (ImGui::GetContentRegionAvail().x -
                   2.0f * ImGui::GetStyle().ItemSpacing.x) /
                  3.0f;
  for (int k = 0; k < 3; ++k) {
    if (k)
      ImGui::SameLine();
    const std::string cid = id + "/" + std::to_string(k);
    ImGui::PushID(k);
    std::string shown;
    if (c.view.editingId == cid)
      shown = c.view.editText;
    else if (have && cur[k].is_number())
      shown = fmtNum(cur[k].get<double>() * factor);
    char buf[48];
    std::snprintf(buf, sizeof(buf), "%s", shown.c_str());
    ImGui::SetNextItemWidth(w);
    ImGui::InputTextWithHint("##c", kAxis[k], buf, sizeof(buf),
                             ImGuiInputTextFlags_CharsScientific);
    const bool active = ImGui::IsItemActive();
    if (active) {
      c.view.editingId = cid;
      c.view.editText = buf;
    }
    if (ImGui::IsItemDeactivatedAfterEdit()) {
      c.view.editingId.clear();
      c.view.fieldErrors.erase(id);
      double v = 0.0;
      if (parseNumber(buf, v) || isBlank(buf)) {
        json vec = have ? cur : json::array({0.0, 0.0, 0.0});
        vec[k] = isBlank(buf) ? 0.0 : v / factor;
        if (altKey && srcKey == altKey)
          obj.erase(altKey); // persist the shared dialect key
        obj[key] = vec;
        c.result.committed = true;
      } else {
        c.view.fieldErrors[id] =
            std::string("\"") + buf + "\" is not a number.";
      }
    } else if (!active && c.view.editingId == cid) {
      c.view.editingId.clear();
    }
    tip(kAxis[k]);
    ImGui::PopID();
  }
  const auto err = c.view.fieldErrors.find(id);
  if (err != c.view.fieldErrors.end())
    kit::inlineStatus(err->second.c_str(), kit::Tone::Err);
  ImGui::PopID();
}

void textField(EditCtx &c, const std::string &id, const char *label, json &obj,
               const char *key, const char *help) {
  ImGui::PushID(id.c_str());
  ImGui::TextUnformatted(label);
  tip(help);
  std::string shown =
      c.view.editingId == id
          ? c.view.editText
          : (member(obj, key).is_string() ? obj[key].get<std::string>()
                                          : std::string());
  char buf[96];
  std::snprintf(buf, sizeof(buf), "%s", shown.c_str());
  ImGui::SetNextItemWidth(-FLT_MIN);
  ImGui::InputText("##t", buf, sizeof(buf));
  const bool active = ImGui::IsItemActive();
  if (active) {
    c.view.editingId = id;
    c.view.editText = buf;
  }
  if (ImGui::IsItemDeactivatedAfterEdit()) {
    c.view.editingId.clear();
    if (isBlank(buf))
      obj.erase(key);
    else
      obj[key] = std::string(buf);
    c.result.committed = true;
  } else if (!active && c.view.editingId == id) {
    c.view.editingId.clear();
  }
  ImGui::PopID();
}

// The face well: arm toggle + face chips (right-click removes) + Clear.
void wellRow(EditCtx &c, const std::string &pointer, const char *help) {
  json *faces = facesAt(c.study, pointer);
  if (!faces)
    return;
  ImGui::PushID(pointer.c_str());
  const bool armed = c.view.armedPointer == pointer;
  const std::string label =
      armed ? "Selecting faces\xe2\x80\xa6 (click viewport)"
            : (faces->empty()
                   ? std::string("Pick faces")
                   : "Pick faces (" + std::to_string(faces->size()) + ")");
  if (armed)
    ImGui::PushStyleColor(ImGuiCol_Button,
                          kit::toneVec(kit::Tone::Accent, 0.85f));
  if (ImGui::Button(label.c_str(), ImVec2(-FLT_MIN, 0))) {
    c.view.armedPointer = armed ? std::string() : pointer;
    c.result.armChanged = true;
  }
  if (armed)
    ImGui::PopStyleColor();
  tip(armed ? "Click model faces to add them (click a picked face again to "
              "remove it). "
              "Click here to stop."
            : help);
  int removeAt = -1;
  for (int k = 0; k < static_cast<int>(faces->size()); ++k) {
    ImGui::PushID(k);
    const json &sel = (*faces)[k];
    const bool resolvedSet = sel.is_object() && sel.contains("points");
    std::uint32_t b = 0, t = 0;
    const bool picked = pickOf(sel, b, t);
    const std::string chip = resolvedSet
                                 ? "Resolved set " + std::to_string(k + 1)
                                 : "Face " + std::to_string(k + 1);
    ImGui::SmallButton(chip.c_str());
    if (ImGui::IsItemClicked(ImGuiMouseButton_Right))
      removeAt = k;
    tip(resolvedSet
            ? "Pre-resolved point set (agent-authored). Right-click to remove."
        : picked
            ? "Right-click to remove this face."
            : "Face selector without a viewport identity (agent-authored); it "
              "resolves at run time but is not highlighted. Right-click to "
              "remove.");
    if (k % 4 != 3 && k + 1 < static_cast<int>(faces->size()))
      ImGui::SameLine();
    ImGui::PopID();
  }
  if (!faces->empty()) {
    ImGui::SameLine();
    if (ImGui::SmallButton("Clear"))
      removeAt = -2;
    tip("Remove every face from this selection.");
  }
  if (removeAt >= 0) {
    faces->erase(static_cast<std::size_t>(removeAt));
    c.result.committed = true;
  } else if (removeAt == -2) {
    faces->clear();
    c.result.committed = true;
  }
  issuesFor(c, pointer);
  ImGui::PopID();
}

// Remove-entry button. Returns true when clicked.
bool removeButton(const char *what) {
  const bool hit = kit::secondaryButton("Remove");
  tip(what);
  return hit;
}

json &ensureObject(json &parent, const char *key) {
  if (!parent.contains(key) || !parent[key].is_object())
    parent[key] = json::object();
  return parent[key];
}

void armNew(EditCtx &c, const std::string &pointer) {
  if (!pointer.empty()) {
    c.view.armedPointer = pointer;
    c.result.armChanged = true;
  }
  c.result.committed = true;
}

// ---- structural
// -----------------------------------------------------------------------------------
void fixtureList(EditCtx &c, const std::string &listPointer, int loadCase) {
  json *arr = nullptr;
  try {
    const json::json_pointer p(listPointer);
    if (c.study.contains(p) && c.study[p].is_array())
      arr = &c.study[p];
  } catch (...) {
  }
  int removeAt = -1;
  if (arr)
    for (std::size_t i = 0; i < arr->size(); ++i) {
      json &e = (*arr)[i];
      const std::string ptr = listPointer + "/" + std::to_string(i);
      ImGui::PushID(ptr.c_str());
      const std::string title =
          entryLabel(e, "Fixture " + std::to_string(i + 1));
      if (kit::cardBegin("fixture", title.c_str(), "fixture")) {
        if (isBareSelector(e)) {
          kit::dimText(
              "One face selector authored outside the editor (all three "
              "directions fixed).");
          if (kit::secondaryButton("Make editable")) {
            json sel = e;
            e = json{{"faces", json::array({sel})}};
            c.result.committed = true;
          }
          tip("Wrap the selector in an editable fixture (faces + fixed "
              "directions); the "
              "constraint is unchanged.");
        } else {
          wellRow(c, ptr + "/faces",
                  "Arm, then click the faces this fixture holds.");
          ImGui::TextUnformatted("Fixed directions");
          tip("Unchecked directions stay free (roller / slider supports). All "
              "three = "
              "fully clamped.");
          bool on[3] = {true, true, true};
          const json &d = member(e, "dofs");
          if (d.is_array()) {
            on[0] = on[1] = on[2] = false;
            for (const json &k : d)
              if (k.is_number_integer() && k.get<int>() >= 0 &&
                  k.get<int>() <= 2)
                on[k.get<int>()] = true;
          }
          static const char *const kDof[3] = {"X", "Y", "Z"};
          bool changed = false;
          for (int k = 0; k < 3; ++k) {
            if (k)
              ImGui::SameLine();
            changed |= ImGui::Checkbox(kDof[k], &on[k]);
          }
          if (changed) {
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
          issuesFor(c, ptr + "/dofs");
        }
        if (removeButton("Delete this fixture."))
          removeAt = static_cast<int>(i);
      }
      kit::cardEnd();
      ImGui::PopID();
    }
  if (removeAt >= 0) {
    arr->erase(static_cast<std::size_t>(removeAt));
    c.result.committed = true;
    c.view.armedPointer.clear();
    c.result.armChanged = true;
  }
  if (kit::secondaryButton(loadCase >= 0 ? "+ Case fixture" : "+ Fixture"))
    armNew(c, addEntry(c.study, EntryKind::Fixture, loadCase));
  tip(loadCase >= 0 ? "A support active only in this load case (added to the "
                      "shared ones)."
                    : "A support shared by every load case.");
}

void loadList(EditCtx &c, const std::string &listPointer, int loadCase) {
  json *arr = nullptr;
  try {
    const json::json_pointer p(listPointer);
    if (c.study.contains(p) && c.study[p].is_array())
      arr = &c.study[p];
  } catch (...) {
  }
  int removeAt = -1;
  if (arr)
    for (std::size_t i = 0; i < arr->size(); ++i) {
      json &e = (*arr)[i];
      const std::string ptr = listPointer + "/" + std::to_string(i);
      ImGui::PushID(ptr.c_str());
      const bool pressure =
          member(e, "type").is_string() && e["type"] == "pressure";
      const std::string title = entryLabel(
          e, (pressure ? "Pressure " : "Force ") + std::to_string(i + 1));
      if (kit::cardBegin("load", title.c_str(),
                         pressure ? "load_pressure" : "load_force")) {
        wellRow(c, ptr + "/faces",
                "Arm, then click the faces this load acts on.");
        int kind = pressure ? 1 : 0;
        static const char *const kKinds[2] = {"Force", "Pressure"};
        if (kit::segmented("kind", kKinds, 2, &kind)) {
          e["type"] = kind == 1 ? "pressure" : "force";
          c.result.committed = true;
        }
        if (kind == 0) {
          vectorField(c, ptr + "/vec", "Force vector", e, "vec",
                      c.units.forceFromN, c.units.force,
                      "Total force over the selected faces (split by area).",
                      "vector");
          issuesFor(c, ptr + "/vec");
        } else {
          numberField(c, ptr + "/magnitude", "Pressure", e, "magnitude",
                      c.units.pressFromMpa, c.units.press,
                      "Uniform pressure; positive pushes INTO the faces.");
          issuesFor(c, ptr + "/magnitude");
        }
        issuesFor(c, ptr + "/type");
        if (removeButton("Delete this load."))
          removeAt = static_cast<int>(i);
      }
      kit::cardEnd();
      ImGui::PopID();
    }
  if (removeAt >= 0) {
    arr->erase(static_cast<std::size_t>(removeAt));
    c.result.committed = true;
    c.view.armedPointer.clear();
    c.result.armChanged = true;
  }
  if (kit::secondaryButton(loadCase >= 0 ? "+ Force##case" : "+ Force"))
    armNew(c, addEntry(c.study, EntryKind::ForceLoad, loadCase));
  ImGui::SameLine();
  if (kit::secondaryButton(loadCase >= 0 ? "+ Pressure##case" : "+ Pressure"))
    armNew(c, addEntry(c.study, EntryKind::PressureLoad, loadCase));
}

void structuralSection(EditCtx &c, const std::string &physics) {
  kit::sectionHeader("Supports", "fixture");
  fixtureList(c, "/fixtures", -1);
  issuesFor(c, "/fixtures");
  if (physics == "modal") {
    kit::dimText("Modal topology uses supports only; loads do not apply.");
    return;
  }
  json &study = c.study;
  const bool haveCases = study.contains("load_cases") &&
                         study["load_cases"].is_array() &&
                         !study["load_cases"].empty();
  kit::sectionHeader("Loads", "load_force");
  if (!haveCases) {
    loadList(c, "/loads", -1);
    issuesFor(c, "/loads");
    kit::gapRow();
    if (kit::secondaryButton("Use multiple load cases")) {
      json cases = json::array();
      json first = json::object();
      first["name"] = "Case 1";
      first["weight"] = 1.0;
      first["loads"] = study.contains("loads") && study["loads"].is_array()
                           ? study["loads"]
                           : json::array();
      cases.push_back(std::move(first));
      study["load_cases"] = std::move(cases);
      study.erase("loads");
      c.view.loadCase = 0;
      c.view.armedPointer.clear();
      c.result.armChanged = true;
      c.result.committed = true;
    }
    tip("Optimize for several independent load cases with weights. The current "
        "loads "
        "become Case 1 (weight 1, identical to today's single case).");
    return;
  }
  // Shared loads left over next to cases are ambiguous: offer the explicit
  // move.
  if (study.contains("loads") && study["loads"].is_array() &&
      !study["loads"].empty()) {
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
  json &cases = study["load_cases"];
  int removeCase = -1;
  for (std::size_t ci = 0; ci < cases.size(); ++ci) {
    json &cs = cases[ci];
    const std::string base = "/load_cases/" + std::to_string(ci);
    ImGui::PushID(base.c_str());
    const std::string title = entryLabel(cs, "Case " + std::to_string(ci + 1));
    if (kit::cardBegin("case", title.c_str(), "load_force")) {
      textField(c, base + "/name", "Name", cs, "name",
                "Unique load-case name.");
      issuesFor(c, base + "/name");
      numberField(c, base + "/weight", "Weight", cs, "weight", 1.0, "",
                  "Positive weight of this case in the weighted objective.",
                  "solver default 1");
      issuesFor(c, base + "/weight");
      ImGui::TextUnformatted("Case supports (optional)");
      fixtureList(c, base + "/fixtures", static_cast<int>(ci));
      ImGui::TextUnformatted("Case loads");
      loadList(c, base + "/loads", static_cast<int>(ci));
      issuesFor(c, base + "/loads");
      if (cases.size() > 1 && removeButton("Delete this load case."))
        removeCase = static_cast<int>(ci);
    }
    kit::cardEnd();
    ImGui::PopID();
  }
  if (removeCase >= 0) {
    cases.erase(static_cast<std::size_t>(removeCase));
    c.view.armedPointer.clear();
    c.result.armChanged = true;
    c.result.committed = true;
  }
  if (kit::secondaryButton("+ Load case")) {
    json cs = json::object();
    cs["name"] = "Case " + std::to_string(cases.size() + 1);
    cs["loads"] = json::array();
    cases.push_back(std::move(cs));
    c.result.committed = true;
  }
  tip("A new case needs its own loads; weight is optional (solver default 1).");
  if (cases.size() == 1 && kit::secondaryButton("Back to a single case")) {
    json loads = cases[0].contains("loads") ? cases[0]["loads"] : json::array();
    const bool hasCaseFixtures = cases[0].contains("fixtures") &&
                                 cases[0]["fixtures"].is_array() &&
                                 !cases[0]["fixtures"].empty();
    if (hasCaseFixtures) {
      json &fx = study["fixtures"];
      if (!fx.is_array())
        fx = json::array();
      for (const json &f : cases[0]["fixtures"])
        fx.push_back(f);
    }
    study["loads"] = std::move(loads);
    study.erase("load_cases");
    c.view.armedPointer.clear();
    c.result.armChanged = true;
    c.result.committed = true;
  }
}

// ---- thermal
// ----------------------------------------------------------------------------------------
void thermalSection(EditCtx &c, const std::string &physics) {
  json &th = ensureObject(c.study, "thermal");
  kit::sectionHeader("Thermal", "study_thermal");
  numberField(c, "/thermal/reference_temperature_c",
              physics == "thermomechanical"
                  ? "Reference (strain-free) temperature"
                  : "Reference temperature",
              th, "reference_temperature_c", 1.0, "degC",
              "Temperature of zero thermal strain and of unconstrained nodes.",
              physics == "thermomechanical" ? "required" : "optional");
  issuesFor(c, "/thermal/reference_temperature_c");

  kit::sectionHeader("Fixed temperatures", "fixture");
  json &fx = th["fixtures"];
  if (!fx.is_array())
    fx = json::array();
  int removeAt = -1;
  for (std::size_t i = 0; i < fx.size(); ++i) {
    const std::string ptr = "/thermal/fixtures/" + std::to_string(i);
    ImGui::PushID(ptr.c_str());
    const std::string title =
        entryLabel(fx[i], "Fixed temperature " + std::to_string(i + 1));
    if (kit::cardBegin("tfix", title.c_str(), "fixture")) {
      wellRow(c, ptr + "/faces", "Faces held at a fixed temperature.");
      numberField(c, ptr + "/temperature_c", "Temperature", fx[i],
                  "temperature_c", 1.0, "degC",
                  "Prescribed surface temperature.");
      issuesFor(c, ptr + "/temperature_c");
      if (removeButton("Delete this fixed temperature."))
        removeAt = static_cast<int>(i);
    }
    kit::cardEnd();
    ImGui::PopID();
  }
  if (removeAt >= 0) {
    fx.erase(static_cast<std::size_t>(removeAt));
    c.view.armedPointer.clear();
    c.result.armChanged = true;
    c.result.committed = true;
  }
  if (kit::secondaryButton("+ Fixed temperature"))
    armNew(c, addEntry(c.study, EntryKind::ThermalFixture));
  issuesFor(c, "/thermal/fixtures");

  kit::sectionHeader("Heat loads", "study_thermal");
  json &th2 =
      c.study["thermal"]; // addEntry may have rebuilt nothing, but re-read
  json &ld = th2["loads"];
  if (!ld.is_array())
    ld = json::array();
  removeAt = -1;
  static const char *const kKinds[3] = {"Flux", "Power", "Convection"};
  for (std::size_t i = 0; i < ld.size(); ++i) {
    json &e = ld[i];
    const std::string ptr = "/thermal/loads/" + std::to_string(i);
    ImGui::PushID(ptr.c_str());
    const std::string type =
        member(e, "type").is_string() ? e["type"].get<std::string>() : "";
    int kind = type == "power" ? 1 : type == "convection" ? 2 : 0;
    const std::string title =
        entryLabel(e, std::string(kKinds[kind]) + " " + std::to_string(i + 1));
    if (kit::cardBegin("tload", title.c_str(), "study_thermal")) {
      wellRow(c, ptr + "/faces", "Faces this heat boundary acts on.");
      if (kit::segmented("kind", kKinds, 3, &kind)) {
        e["type"] = kind == 1 ? "power" : kind == 2 ? "convection" : "flux";
        c.result.committed = true;
      }
      if (kind == 0) {
        numberField(c, ptr + "/magnitude_w_m2", "Heat flux", e,
                    "magnitude_w_m2", 1.0, "W/m2",
                    "Heat flux into the faces (negative = out).");
        issuesFor(c, ptr + "/magnitude_w_m2");
      } else if (kind == 1) {
        numberField(c, ptr + "/power_w", "Power", e, "power_w", 1.0, "W",
                    "Total heat power over the faces, spread by area.");
        issuesFor(c, ptr + "/power_w");
      } else {
        numberField(c, ptr + "/h_w_m2k", "Film coefficient", e, "h_w_m2k", 1.0,
                    "W/m2K", "Convective heat-transfer coefficient h.");
        issuesFor(c, ptr + "/h_w_m2k");
        numberField(c, ptr + "/ambient_c", "Ambient temperature", e,
                    "ambient_c", 1.0, "degC",
                    "Temperature of the surrounding fluid.");
        issuesFor(c, ptr + "/ambient_c");
      }
      issuesFor(c, ptr + "/type");
      if (removeButton("Delete this heat load."))
        removeAt = static_cast<int>(i);
    }
    kit::cardEnd();
    ImGui::PopID();
  }
  if (removeAt >= 0) {
    ld.erase(static_cast<std::size_t>(removeAt));
    c.view.armedPointer.clear();
    c.result.armChanged = true;
    c.result.committed = true;
  }
  if (kit::secondaryButton("+ Flux"))
    armNew(c, addEntry(c.study, EntryKind::ThermalFlux));
  ImGui::SameLine();
  if (kit::secondaryButton("+ Power"))
    armNew(c, addEntry(c.study, EntryKind::ThermalPower));
  ImGui::SameLine();
  if (kit::secondaryButton("+ Convection"))
    armNew(c, addEntry(c.study, EntryKind::ThermalConvection));
  issuesFor(c, "/thermal/loads");
  issuesFor(c, "/thermal");
}

// ---- flow
// -------------------------------------------------------------------------------------------
void portList(EditCtx &c, const char *key, const char *title, EntryKind kind,
              const std::string &physics) {
  json &fl = ensureObject(c.study, "flow");
  json &arr = fl[key];
  if (!arr.is_array())
    arr = json::array();
  int removeAt = -1;
  for (std::size_t i = 0; i < arr.size(); ++i) {
    json &e = arr[i];
    const std::string ptr =
        std::string("/flow/") + key + "/" + std::to_string(i);
    ImGui::PushID(ptr.c_str());
    const std::string t =
        entryLabel(e, std::string(title) + " " + std::to_string(i + 1));
    if (kit::cardBegin("port", t.c_str(), "study_flow")) {
      wellRow(c, ptr + "/faces", "Faces of this boundary.");
      if (kind == EntryKind::FlowInlet) {
        vectorField(c, ptr + "/velocity_m_s", "Inflow velocity", e,
                    "velocity_m_s", 1.0, "m/s",
                    "Velocity imposed on the inlet faces (must point into the "
                    "domain).");
        issuesFor(c, ptr + "/velocity_m_s");
        if (physics == "cht") {
          numberField(c, ptr + "/temperature_c", "Inlet temperature", e,
                      "temperature_c", 1.0, "degC",
                      "Fluid temperature entering through this inlet.");
          issuesFor(c, ptr + "/temperature_c");
        }
      } else if (kind == EntryKind::FlowOutlet) {
        numberField(c, ptr + "/pressure_pa", "Outlet pressure", e,
                    "pressure_pa", 1.0, "Pa",
                    "Static (gauge) pressure on the outlet faces.");
        issuesFor(c, ptr + "/pressure_pa");
      }
      if (removeButton("Delete this boundary."))
        removeAt = static_cast<int>(i);
    }
    kit::cardEnd();
    ImGui::PopID();
  }
  if (removeAt >= 0) {
    arr.erase(static_cast<std::size_t>(removeAt));
    c.view.armedPointer.clear();
    c.result.armChanged = true;
    c.result.committed = true;
  }
  const std::string add = std::string("+ ") + title;
  if (kit::secondaryButton(add.c_str()))
    armNew(c, addEntry(c.study, kind));
  issuesFor(c, std::string("/flow/") + key);
}

void flowSection(EditCtx &c, const std::string &physics) {
  json &fl = ensureObject(c.study, "flow");
  kit::sectionHeader("Fluid", "study_flow");
  numberField(c, "/flow/viscosity_pa_s", "Dynamic viscosity", fl,
              "viscosity_pa_s", 1.0, "Pa s",
              "Fluid dynamic viscosity mu (water ~1.0e-3, air ~1.8e-5).");
  issuesFor(c, "/flow/viscosity_pa_s");
  numberField(c, "/flow/density_kg_m3", "Density", fl, "density_kg_m3", 1.0,
              "kg/m3", "Fluid density.");
  issuesFor(c, "/flow/density_kg_m3");
  if (physics == "cht") {
    numberField(c, "/flow/fluid_k_w_mk", "Fluid conductivity", fl,
                "fluid_k_w_mk", 1.0, "W/mK",
                "Thermal conductivity of the fluid.");
    issuesFor(c, "/flow/fluid_k_w_mk");
    numberField(c, "/flow/fluid_cp_j_kgk", "Fluid specific heat", fl,
                "fluid_cp_j_kgk", 1.0, "J/kgK",
                "Specific heat capacity of the fluid.");
    issuesFor(c, "/flow/fluid_cp_j_kgk");
  }
  numberField(
      c, "/flow/brinkman_max", "Brinkman maximum", fl, "brinkman_max", 1.0, "",
      "Dimensionless solid impermeability (alpha_max = value * mu / L^2).",
      "solver default");
  issuesFor(c, "/flow/brinkman_max");
  numberField(
      c, "/flow/reference_length_mm", "Reference length", fl,
      "reference_length_mm", c.units.lenFromMm, c.units.len,
      "Length L for the Brinkman scale and the reported Reynolds number.",
      "solver default");
  issuesFor(c, "/flow/reference_length_mm");
  kit::dimText("Laminar (Stokes/Navier-Stokes Brinkman) flow; the reported "
               "Reynolds number "
               "is regime evidence, not a turbulence model.");
  issuesFor(c, "/flow");

  kit::sectionHeader("Inlets", "study_flow");
  portList(c, "inlets", "Inlet", EntryKind::FlowInlet, physics);
  kit::sectionHeader("Outlets", "study_flow");
  portList(c, "outlets", "Outlet", EntryKind::FlowOutlet, physics);
  kit::sectionHeader("Walls (optional labels)", "study_flow");
  kit::dimText("Every exterior face that is not an inlet or outlet is a "
               "no-slip wall. Named "
               "walls only label a subset.");
  portList(c, "walls", "Wall", EntryKind::FlowWall, physics);
}

// ---- regions
// ----------------------------------------------------------------------------------------
void regionList(EditCtx &c, const char *key, bool preserve) {
  json &arr = c.study[key];
  if (!arr.is_array())
    arr = json::array();
  const char *title = preserve ? "Preserve" : "Exclude";
  int removeAt = -1;
  for (std::size_t i = 0; i < arr.size(); ++i) {
    json &e = arr[i];
    const std::string ptr = std::string("/") + key + "/" + std::to_string(i);
    ImGui::PushID(ptr.c_str());
    const bool band = e.contains("faces");
    const std::string t =
        entryLabel(e, std::string(title) + (band ? " band " : " box ") +
                          std::to_string(i + 1));
    if (kit::cardBegin("region", t.c_str(), nullptr)) {
      if (band) {
        wellRow(c, ptr + "/faces",
                preserve ? "Faces whose adjacent material always stays solid."
                         : "Faces whose adjacent material is always removed "
                           "(keep-out).");
        numberField(
            c, ptr + "/thickness_mm", "Band depth", e, "thickness_mm",
            c.units.lenFromMm, c.units.len,
            "Depth below the faces held fixed. Empty uses the study's default "
            "region depth (Manufacturing settings).",
            "study default");
        issuesFor(c, ptr + "/thickness_mm");
      } else {
        vectorField(c, ptr + "/box_min_mm", "Box minimum corner", e,
                    "box_min_mm", c.units.lenFromMm, c.units.len,
                    "Elements whose centroid lies inside the box are fixed.");
        vectorField(c, ptr + "/box_max_mm", "Box maximum corner", e,
                    "box_max_mm", c.units.lenFromMm, c.units.len,
                    "Opposite corner of the box.");
        issuesFor(c, ptr);
      }
      if (removeButton("Delete this region."))
        removeAt = static_cast<int>(i);
    }
    kit::cardEnd();
    ImGui::PopID();
  }
  if (removeAt >= 0) {
    arr.erase(static_cast<std::size_t>(removeAt));
    c.view.armedPointer.clear();
    c.result.armChanged = true;
    c.result.committed = true;
  }
  const std::string addBand = std::string("+ Face band##") + key;
  const std::string addBox = std::string("+ Box##") + key;
  if (kit::secondaryButton(addBand.c_str()))
    armNew(c, addEntry(c.study, preserve ? EntryKind::PreserveFaces
                                         : EntryKind::ExcludeFaces));
  ImGui::SameLine();
  if (kit::secondaryButton(addBox.c_str())) {
    addEntry(c.study,
             preserve ? EntryKind::PreserveBox : EntryKind::ExcludeBox);
    c.result.committed = true;
  }
  issuesFor(c, std::string("/") + key);
}

void regionsSection(EditCtx &c) {
  kit::sectionHeader("Preserve (always solid)", nullptr, nullptr, -1, true);
  kit::dimText(
      "Fixture, load and flow-port faces stay solid (or fluid for ports) "
      "automatically; add bands or boxes for other keep-in regions.");
  regionList(c, "preserve", true);
  kit::sectionHeader("Exclude (keep-out)");
  regionList(c, "exclude", false);
}

// ---- material
// ---------------------------------------------------------------------------------------
struct MatKeyInfo {
  const char *key, *label, *unit, *help;
};
const MatKeyInfo kMatInfo[] = {
    {"E_mpa", "Young's modulus", "MPa", "Elastic modulus of the solid phase."},
    {"nu", "Poisson's ratio", "", "Lateral contraction ratio (-1 < nu < 0.5)."},
    {"yield_mpa", "Yield strength", "MPa",
     "Used for stress reporting / limits."},
    {"density_g_cm3", "Density", "g/cm3",
     "Mass density (mass and modal physics)."},
    {"k_w_mk", "Thermal conductivity", "W/mK",
     "Solid conductivity (thermal physics)."},
    {"alpha_per_k", "Thermal expansion", "1/K",
     "Linear expansion coefficient (thermomechanical)."},
    {"cp_j_kgk", "Specific heat", "J/kgK", "Solid specific heat capacity."},
};

void materialSection(
    EditCtx &c, const MaterialLookup &lookup,
    const std::vector<std::pair<std::string, std::string>> &library,
    const std::string &physics) {
  // Work on a copy so drawing never inserts a null `material` into an authored
  // record.
  json m = c.study.contains("material") ? c.study["material"] : json();
  const bool committedBefore = c.result.committed;
  c.result.committed = false;
  std::string key;
  if (m.is_string())
    key = m.get<std::string>();
  else if (member(m, "key").is_string())
    key = m["key"].get<std::string>();
  std::string preview = "Custom (no library entry)";
  for (const auto &kv : library)
    if (kv.first == key)
      preview = kv.second;
  if (m.is_null())
    preview = "Choose a material";
  kit::sectionHeader("Material", "material", nullptr, -1, true);
  ImGui::SetNextItemWidth(-FLT_MIN);
  if (ImGui::BeginCombo("##topo_mat", preview.c_str())) {
    for (const auto &kv : library) {
      if (ImGui::Selectable(kv.second.c_str(), kv.first == key) &&
          kv.first != key) {
        m = kv.first; // library reference; overrides below convert it to an
                      // object
        c.result.committed = true;
      }
    }
    if (ImGui::Selectable("Custom (no library entry)",
                          m.is_object() && key.empty())) {
      json custom = json::object();
      if (m.is_object())
        for (const MatKeyInfo &mi : kMatInfo)
          copyNumber(m, custom, mi.key);
      m = std::move(custom);
      c.result.committed = true;
    }
    ImGui::EndCombo();
  }
  tip("The study's own material. Library entries supply the constants they "
      "record; "
      "anything the library lacks must be entered below.");
  issuesFor(c, "/material");

  json lib = json::object();
  if (!key.empty() && lookup)
    lookup(key, lib);
  const std::vector<std::string> required = requiredMaterialKeys(physics);
  for (const MatKeyInfo &mi : kMatInfo) {
    const bool req =
        std::find(required.begin(), required.end(), mi.key) != required.end();
    // Editing promotes a key-string material to an object that keeps the
    // reference.
    json view = m.is_object() ? m : json::object();
    const std::string libText =
        lib.contains(mi.key) && lib[mi.key].is_number()
            ? "library " + fmtNum(lib[mi.key].get<double>())
            : (req ? std::string("required") : std::string("not set"));
    const std::string label = std::string(mi.label) + (req ? " *" : "");
    const bool before = c.result.committed;
    c.result.committed = false;
    numberField(c, std::string("/material/") + mi.key, label.c_str(), view,
                mi.key, 1.0, mi.unit, mi.help, libText.c_str());
    if (c.result.committed) {
      if (!m.is_object()) {
        json obj = json::object();
        if (!key.empty())
          obj["key"] = key;
        m = std::move(obj);
      }
      if (view.contains(mi.key))
        m[mi.key] = view[mi.key];
      else
        m.erase(mi.key);
    }
    c.result.committed = c.result.committed || before;
    issuesFor(c, std::string("/material/") + mi.key);
  }
  kit::dimText(
      "* required by the selected physics. A typed value overrides the library "
      "entry for this study only.");
  if (c.result.committed)
    c.study["material"] = m;
  c.result.committed = c.result.committed || committedBefore;
}

} // namespace

BcResult drawBoundaryEditor(
    AppState &st, json &study, BcSection section, BcView &view,
    const MaterialLookup &lookup,
    const std::vector<std::pair<std::string, std::string>> &libraryMaterials,
    const char *lockedReason) {
  const std::vector<Issue> issues = validateBoundaries(study, lookup);
  EditCtx c{st, study, view, issues};
  c.locked = lockedReason != nullptr;
  c.units = SimManager::unitSys(st);
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
        "Open the topology setup and pick the physics.");
  } else {
    kit::dimText(
        ("Boundary conditions for " + physicsLabel(physics) + ".").c_str());
    if (usesStructuralBoundary(physics))
      structuralSection(c, physics);
    if (usesThermalBoundary(physics))
      thermalSection(c, physics);
    if (usesFlowBoundary(physics))
      flowSection(c, physics);
    // Sections authored for another physics are kept verbatim but not sent.
    std::string kept;
    if (!usesStructuralBoundary(physics) &&
        ((study.contains("fixtures") && !study["fixtures"].empty()) ||
         (study.contains("loads") && !study["loads"].empty()) ||
         (study.contains("load_cases") && !study["load_cases"].empty())))
      kept += " supports/loads";
    if (!usesThermalBoundary(physics) && study.contains("thermal") &&
        study["thermal"].is_object() && !study["thermal"].empty())
      kept += " thermal";
    if (!usesFlowBoundary(physics) && study.contains("flow") &&
        study["flow"].is_object() && !study["flow"].empty())
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
      kit::inlineStatus(i.message.c_str(), i.severity == Severity::Blocking
                                               ? kit::Tone::Err
                                               : kit::Tone::Warn);
  ImGui::EndDisabled();
  ImGui::PopID();
  // A removed well cannot stay armed.
  if (!view.armedPointer.empty() && !wellExists(study, view.armedPointer)) {
    view.armedPointer.clear();
    c.result.armChanged = true;
  }
  return c.result;
}

} // namespace topology
} // namespace ui
#endif // CHUDWARE_SIM_TOPOLOGY_MODEL_ONLY
