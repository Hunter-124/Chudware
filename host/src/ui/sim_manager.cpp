// SimManager core: study state/JSON, refresh/persist, study CRUD, command routing, materials, probes.
#include "sim_manager.h"

#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iterator>
#include <sstream>

#include "../bootstrap.h"
#include "../core/sim_client.h"
#include "sim/sim_manager_internal.h"
#include "widgets/ribbon.h" // setRibbonStudyContext — per-study command applicability
#include "sim/sim_interaction.h"

namespace ui {

// Index of the "Motion" per-type tab in ribbon.cpp's kRibbonTabs[] (the Simulate-workspace sim
// tabs are append-only at 13..19, so the index is stable).
static constexpr int kRibbonTabMotion = 18;
// Topology ribbon tab (append-only tab index; app_state RibbonTab::RT_Topology).
static constexpr int kRibbonTabTopology = 21;
static const char *studyTypeKey(StudyType t) {
  switch (t) {
  case StudyType::Modal:
    return "modal";
  case StudyType::Flow:
    return "flow";
  case StudyType::Fatigue:
    return "fatigue";
  case StudyType::Thermal:
    return "thermal";
  case StudyType::Motion:
    return "motion";
  case StudyType::ThermalFea:
    return "thermal_fea";
  case StudyType::Buckling:
    return "buckling";
  case StudyType::Contact:
    return "contact";
  case StudyType::FreqResponse:
    return "freq_response";
  case StudyType::Topology:
    return "topology";
  default:
    return "static";
  }
}
static StudyType studyTypeFromKey(const std::string &k) {
  if (k == "fea_static" || k == "fea_adaptive")
    return StudyType::Static;
  if (k == "fea_modal") return StudyType::Modal;
  if (k == "fea_fatigue") return StudyType::Fatigue;
  if (k == "fea_buckling") return StudyType::Buckling;
  if (k == "fea_contact") return StudyType::Contact;
  if (k == "fea_frequency_response") return StudyType::FreqResponse;
  if (k == "fea_thermal" || k == "fea_thermal_transient") return StudyType::ThermalFea;
  if (k == "cfd_flow" || k == "cfd_openfoam") return StudyType::Flow;
  if (k == "modal")
    return StudyType::Modal;
  if (k == "flow")
    return StudyType::Flow;
  if (k == "fatigue")
    return StudyType::Fatigue;
  if (k == "thermal")
    return StudyType::Thermal;
  if (k == "motion")
    return StudyType::Motion;
  if (k == "thermal_fea")
    return StudyType::ThermalFea;
  if (k == "buckling")
    return StudyType::Buckling;
  if (k == "contact")
    return StudyType::Contact;
  if (k == "freq_response")
    return StudyType::FreqResponse;
  if (k == "topology")
    return StudyType::Topology;
  return StudyType::Static;
}

std::string SimWell::label() const {
  if (picks.empty())
    return "(empty)";
  return std::to_string(picks.size()) +
         (picks.size() == 1 ? " face" : " faces");
}

// ---------------------------------------------------------------- materials
// (singleton)

static std::string customMaterialsPath() {
  return appdataAppDir() + "/custom_materials.json";
}

// Parse a {key:{name,E_mpa,nu,density_g_cm3,yield_mpa}} object file into `out`
// (custom flag tagged).
static void loadMaterialsFile(const std::string &path,
                              std::vector<SimManager::Material> &out,
                              bool custom) {
  std::ifstream f(path, std::ios::binary);
  if (!f)
    return;
  std::stringstream ss;
  ss << f.rdbuf();
  try {
    auto j = nlohmann::json::parse(ss.str());
    if (!j.is_object())
      return;
    for (auto it = j.begin(); it != j.end(); ++it) {
      SimManager::Material m;
      m.key = it.key();
      const auto &v = it.value();
      m.name = v.value("name", it.key());
      m.E_mpa = v.value("E_mpa", 0.0);
      m.nu = v.value("nu", 0.0);
      m.density_g_cm3 = v.value("density_g_cm3", 0.0);
      m.yield_mpa = v.value("yield_mpa", 0.0);
      m.ultimate_mpa = v.value("ultimate_mpa", 0.0);
      m.endurance_mpa = v.value("endurance_mpa", 0.0);
      m.custom = custom;
      // A custom key shadows nothing built-in (custom keys are namespaced
      // "custom_…"); just append.
      out.push_back(std::move(m));
    }
  } catch (...) {
  }
}

// Backing store + loaded flag, so addCustomMaterial can force a rebuild on next
// access.
static std::vector<SimManager::Material> &materialStore() {
  static std::vector<SimManager::Material> m;
  return m;
}
static bool &materialsLoaded() {
  static bool b = false;
  return b;
}

const std::vector<SimManager::Material> &SimManager::materials() {
  auto &mats = materialStore();
  if (!materialsLoaded()) {
    materialsLoaded() = true;
    mats.clear();
    // materials.json moved engine/sim/ -> services/sim/ in the zero-Python
    // cutover; staged payloads may still carry the legacy path, so probe both.
    const std::string base = appDir();
    loadMaterialsFile(base + "/services/sim/materials.json", mats, false);
    if (mats.empty())
      loadMaterialsFile(base + "/engine/sim/materials.json", mats, false);
    loadMaterialsFile(customMaterialsPath(), mats, true);
  }
  return mats;
}

std::string SimManager::addCustomMaterial(const Material &in) {
  // Namespaced, de-duplicated key from the name (custom_<sanitized>); append a
  // counter on collision.
  std::string base = "custom_";
  for (char c : in.name) {
    if ((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9'))
      base += c;
    else if (c >= 'A' && c <= 'Z')
      base += (char)(c - 'A' + 'a');
    else if (c == ' ' || c == '-' || c == '_')
      base += '_';
  }
  if (base == "custom_")
    base = "custom_material";
  // Load the existing custom file (object), ensure a unique key, write it back.
  nlohmann::json j = nlohmann::json::object();
  {
    std::ifstream f(customMaterialsPath(), std::ios::binary);
    if (f) {
      std::stringstream ss;
      ss << f.rdbuf();
      try {
        auto p = nlohmann::json::parse(ss.str());
        if (p.is_object())
          j = p;
      } catch (...) {
      }
    }
  }
  std::string key = base;
  for (int n = 2; j.contains(key); ++n)
    key = base + "_" + std::to_string(n);
  j[key] = {{"name", in.name.empty() ? key : in.name},
            {"E_mpa", in.E_mpa},
            {"nu", in.nu},
            {"density_g_cm3", in.density_g_cm3},
            {"yield_mpa", in.yield_mpa},
            {"ultimate_mpa", in.ultimate_mpa},
            {"endurance_mpa", in.endurance_mpa}};
  // Ensure the directory exists, then verify the write before reporting the
  // material as saved.
  try {
    std::filesystem::create_directories(
        std::filesystem::path(customMaterialsPath()).parent_path());
  } catch (...) {
    return {};
  }
  std::ofstream o(customMaterialsPath(), std::ios::binary | std::ios::trunc);
  if (!o)
    return {};
  o << j.dump(2);
  o.flush();
  if (!o)
    return {};
  materialsLoaded() = false; // force a merge-reload on next materials() access
  return key;
}

int SimManager::materialIndexByKey(const std::string &key) {
  const auto &mats = materials();
  for (int i = 0; i < (int)mats.size(); ++i)
    if (mats[i].key == key)
      return i;
  return -1;
}

SimManager::UnitSys SimManager::unitSys(const AppState &st) {
  // simUnits: 0 follow doc (ips() -> US), 1 force SI, 2 force US. SolidWorks
  // IPS uses psi for sim.
  const bool us = (st.simUnits == 2) || (st.simUnits == 0 && st.ips());
  UnitSys u;
  u.us = us;
  if (us) {
    u.stress = "psi";
    u.stressFromMpa = 145.0377;
    u.len = "in";
    u.lenFromMm = 1.0 / 25.4;
    u.force = "lbf";
    u.forceFromN = 0.2248089;
    u.press = "psi";
    u.pressFromMpa = 145.0377;
  }
  return u; // defaults are already SI (MPa / mm / N)
}

namespace {

void markSimDocDirty(AppState &st, const std::string &docId) {
  for (auto &tab : st.docTabs) {
    if (tab.docId != docId)
      continue;
    tab.dirty = true;
    tab.historyCleared = false;
    if (tab.aiHandoff)
      tab.userAdopted = true;
    return;
  }
}
} // namespace

// ---------------------------------------------------------------- ctor

SimManager::SimManager() {}

// ---------------------------------------------------------------- topology study helpers
static SimSel selFromJson(const nlohmann::json &selector);

namespace {

// Read-only mirror of a topology study's STRUCTURAL wells into Study::fixtures/loads for the
// shared indicator glyphs; the study JSON stays the only persisted source.
void mirrorTopologyWells(Study &s) {
  s.fixtures.picks.clear();
  s.loads.clear();
  const nlohmann::json &mat = s.topo.contains("material") ? s.topo["material"]
                                                          : nlohmann::json();
  s.materialKey = mat.is_string() ? mat.get<std::string>()
                  : (mat.is_object() && mat.contains("key") && mat["key"].is_string())
                      ? mat["key"].get<std::string>()
                      : std::string();
  const std::string physics = topology::studyPhysics(s.topo);
  if (!topology::usesStructuralBoundary(physics))
    return;
  auto addFixtures = [&](const nlohmann::json &arr) {
    if (!arr.is_array())
      return;
    for (const auto &e : arr) {
      if (e.is_object() && e.contains("faces") && e["faces"].is_array()) {
        for (const auto &sel : e["faces"])
          s.fixtures.picks.push_back(selFromJson(sel));
      } else if (e.is_object()) {
        s.fixtures.picks.push_back(selFromJson(e));
      }
    }
  };
  auto addLoads = [&](const nlohmann::json &arr) {
    if (!arr.is_array())
      return;
    for (const auto &e : arr) {
      if (!e.is_object())
        continue;
      SimLoad l;
      l.pressure = e.contains("type") && e["type"] == "pressure";
      const nlohmann::json &v = e.contains("vec") ? e["vec"]
                                : e.contains("vector") ? e["vector"]
                                                       : nlohmann::json();
      if (l.pressure) {
        l.pressureMpa = e.contains("magnitude") && e["magnitude"].is_number()
                            ? e["magnitude"].get<float>()
                            : 0.0f;
      } else {
        for (int k = 0; k < 3; ++k)
          l.vec[k] = v.is_array() && v.size() == 3 && v[k].is_number()
                         ? v[k].get<float>()
                         : 0.0f;
      }
      if (e.contains("faces") && e["faces"].is_array())
        for (const auto &sel : e["faces"])
          l.faces.picks.push_back(selFromJson(sel));
      s.loads.push_back(std::move(l));
    }
  };
  addFixtures(s.topo.value("fixtures", nlohmann::json()));
  if (topology::usesStructuralLoads(physics)) {
    addLoads(s.topo.value("loads", nlohmann::json()));
    const nlohmann::json cases = s.topo.value("load_cases", nlohmann::json());
    if (cases.is_array())
      for (const auto &c : cases) {
        if (!c.is_object())
          continue;
        addFixtures(c.value("fixtures", nlohmann::json()));
        addLoads(c.value("loads", nlohmann::json()));
      }
  }
}
nlohmann::json motionSettingsJson(const Study &s) {
  return {{"drivers", motionDriversJson(s)}, {"steps", s.motionSteps},
          {"loop", s.motionLoop}, {"fps", s.motionFps},
          {"check_interference", s.motionCheckInterference},
          {"min_clearance", s.motionMinClearance}};
}
} // namespace

namespace {
// Apply the typed projection's changes, not its defaults, to the native record. Collections
// replace as whole values; each load owns its own metadata, so indices cannot transfer it.
void applyTypedChanges(nlohmann::json& native, const nlohmann::json& before,
                       const nlohmann::json& after) {
  if (before == after) return;
  if (!before.is_object() || !after.is_object()) { native = after; return; }
  if (!native.is_object()) native = nlohmann::json::object();
  for (auto it = before.begin(); it != before.end(); ++it)
    if (!after.contains(it.key())) native.erase(it.key());
  for (auto it = after.begin(); it != after.end(); ++it) {
    const auto old = before.find(it.key());
    if (old == before.end()) native[it.key()] = it.value();
    else if (*old != it.value()) applyTypedChanges(native[it.key()], *old, it.value());
  }
}
nlohmann::json setupDefinition(nlohmann::json value) {
  for (const char* key : {"id", "type", "name", "result_summary", "results", "result",
                          "runs", "last_job", "last_run", "summary", "provenance",
                          "mesh_quality", "reactions", "advisor", "n_nodes", "n_elems",
                          "n_dof", "freqs_hz"})
    value.erase(key);
  return value;
}
}

nlohmann::json SimManager::studyToJson(const Study& s) {
  auto typed = typedStudyJson(s);
  if (s.type == StudyType::Topology || !s.definition.is_object()) return typed;
  auto native = s.definition;
  applyTypedChanges(native, s.typedBaseline, typed);
  for (const auto& mapping : {std::pair<const char*, const char*>{"mesh_fineness", "fineness"},
                             {"mesh_target_mm", "target_size_mm"}, {"mesh_min_mm", "min_size_mm"},
                             {"mesh_order", "order"}})
    if (native.contains(mapping.second) && s.typedBaseline.contains(mapping.first) &&
        s.typedBaseline[mapping.first] != typed[mapping.first]) {
      if ((std::strcmp(mapping.first, "mesh_target_mm") == 0 ||
           std::strcmp(mapping.first, "mesh_min_mm") == 0) && typed[mapping.first] == 0)
        native.erase(mapping.second);
      else native[mapping.second] = typed[mapping.first];
    }
  if (native.contains("adapt") && native["adapt"].is_object()) {
    if (s.typedBaseline["adapt_enabled"] != typed["adapt_enabled"] && !s.adaptEnabled)
      native.erase("adapt");
    else {
      if (s.typedBaseline["adapt_max_passes"] != typed["adapt_max_passes"])
        native["adapt"]["max_passes"] = typed["adapt_max_passes"];
      if (s.typedBaseline["adapt_eta_target"] != typed["adapt_eta_target"])
        native["adapt"]["eta_target"] = typed["adapt_eta_target"];
    }
  }
  if (s.typedBaseline.contains("adv")) {
    const auto& old = s.typedBaseline["adv"];
    const auto& now = typed["adv"];
    if (old["therm_fix_temp_c"] != now["therm_fix_temp_c"] &&
        native.contains("fixtures") && native["fixtures"].is_array())
      for (auto& fixture : native["fixtures"])
        if (fixture.is_object() && fixture.contains("temp_c"))
          fixture["temp_c"] = now["therm_fix_temp_c"];
    for (const auto& mapping : {std::pair<const char*, const char*>{"contact_pressure_mpa", "pressure_mpa"},
                               {"fr_zeta", "zeta"}, {"fr_base_g", "base_accel_g"}})
      if (native.contains(mapping.second) && old[mapping.first] != now[mapping.first])
        native[mapping.second] = now[mapping.first];
    if (native.contains("excitation_dir") && old["fr_dir"] != now["fr_dir"]) {
      auto axis = nlohmann::json::array({0.0, 0.0, 0.0});
      if (s.frDir >= 0 && s.frDir < 3) axis[s.frDir] = 1.0;
      native["excitation_dir"] = std::move(axis);
    }
    for (const auto& mapping : {std::pair<const char*, const char*>{"therm_k", "k_w_mk"},
                               {"therm_cp", "cp_j_kgk"}}) {
      if (old[mapping.first] != now[mapping.first]) {
        if (native.contains("material") && native["material"].is_object())
          native["material"][mapping.second] = now[mapping.first];
        else native[mapping.second] = now[mapping.first];
        const char* alias = std::strcmp(mapping.first, "therm_k") == 0 ?
                            "conductivity_w_mk" : "specific_heat_j_kgk";
        if (native.contains(alias)) native[alias] = now[mapping.first];
      }
    }
    if (native.contains("transient") && native["transient"].is_object()) {
      if (old["therm_transient"] != now["therm_transient"] && !s.thermTransient)
        native.erase("transient");
      else
        for (const auto& mapping : {std::pair<const char*, const char*>{"therm_t_end_s", "t_end_s"},
                                   {"therm_dt_s", "dt_s"}, {"therm_t_init_c", "t_init_c"}})
          if (old[mapping.first] != now[mapping.first])
            native["transient"][mapping.second] = now[mapping.first];
    }
  }
  if (native.contains("fatigue") && native["fatigue"].is_object() &&
      native["fatigue"].contains("load_ratio") &&
      s.typedBaseline["fatigue"]["ratio"] != typed["fatigue"]["ratio"])
    native["fatigue"]["load_ratio"] = typed["fatigue"]["ratio"];
  if (s.definition.contains("fatigue") && s.definition["fatigue"].is_object() &&
      s.definition["fatigue"].contains("load_type") &&
      s.definition["fatigue"]["load_type"].is_string()) {
    const auto& authored = s.definition["fatigue"];
    auto& fatigue = native["fatigue"];
    if (s.fatigueLoadType >= 0 && s.fatigueLoadType < 3) {
      if (s.typedBaseline["fatigue"]["load_type"] != typed["fatigue"]["load_type"]) {
        static constexpr const char* tokens[] = {"reversed", "zero_based", "ratio"};
        fatigue["load_type"] = tokens[s.fatigueLoadType];
      }
      if (s.fatigueLoadType == 2 &&
          (s.typedBaseline["fatigue"]["ratio"] != typed["fatigue"]["ratio"] ||
           s.typedBaseline["fatigue"]["load_type"] != typed["fatigue"]["load_type"]))
        fatigue["load_ratio"] = s.fatigueRatio;
      if (!authored.contains("lmin") && !authored.contains("lmax")) {
        // These are derived form readouts, not authored native overrides of the spectrum.
        fatigue.erase("lmin");
        fatigue.erase("lmax");
      }
    } else if (s.fatigueLoadType == 3 &&
               (s.typedBaseline["fatigue"]["load_type"] != typed["fatigue"]["load_type"] ||
                s.typedBaseline["fatigue"]["lmin"] != typed["fatigue"]["lmin"] ||
                s.typedBaseline["fatigue"]["lmax"] != typed["fatigue"]["lmax"])) {
      fatigue["load_type"] = authored["load_type"];
      fatigue["lmin"] = typed["fatigue"]["lmin"];
      fatigue["lmax"] = typed["fatigue"]["lmax"];
    }
    if (!authored.contains("ratio")) fatigue.erase("ratio");
  }
  // Stable mate identity owns native per-driver metadata, never the array index.
  if (native.contains("motion") && native["motion"].is_object() &&
      s.definition.contains("motion") && s.definition["motion"].is_object() &&
      s.definition["motion"].contains("drivers") && s.definition["motion"]["drivers"].is_array() &&
      s.typedBaseline.contains("motion") &&
      s.typedBaseline["motion"]["drivers"] != typed["motion"]["drivers"]) {
    auto drivers = nlohmann::json::array();
    for (const auto& driver : typed["motion"]["drivers"]) {
      auto complete = driver;
      for (const auto& prior : s.definition["motion"]["drivers"])
        if (prior.is_object() && prior.value("mate_id", std::string()) ==
                                  driver.value("mate_id", std::string())) {
          complete = prior;
          for (auto it = driver.begin(); it != driver.end(); ++it) complete[it.key()] = it.value();
          break;
        }
      drivers.push_back(std::move(complete));
    }
    native["motion"]["drivers"] = std::move(drivers);
  }
  native["id"] = s.id;
  // Preserve the existing native study-family token, including MCP's fea_* families.
  native["type"] = s.definition.value("type", std::string(studyTypeKey(s.type)));
  native["name"] = s.name;
  if (!s.summary.empty() || native.contains("result_summary")) native["result_summary"] = s.summary;
  return native;
}

nlohmann::json SimManager::typedStudyJson(const Study &s) {
  if (s.type == StudyType::Topology) {
    // Raw ownership: the record goes back verbatim (unknown/agent keys, runs, last_job and
    // other physics' sections included); only identity and the shared Mesh form's keys are
    // refreshed. A mesh key is written only when it was already present or edited away from
    // the form's unset value, so opening the form never injects values.
    nlohmann::json j = s.topo.is_object() ? s.topo : nlohmann::json::object();
    if (!s.id.empty())
      j["id"] = s.id;
    j["type"] = "topology";
    j["name"] = s.name;
    if (!j.contains("mesh_order"))
      j["mesh_order"] = 1; // tet4; an authored other order is kept and blocks the run
    if (j.contains("mesh_fineness") || s.meshFineness != 0.5f)
      j["mesh_fineness"] = s.meshFineness;
    if (s.meshTargetMm > 0.0f)
      j["mesh_target_mm"] = s.meshTargetMm;
    else
      j.erase("mesh_target_mm");
    if (s.meshMinMm > 0.0f)
      j["mesh_min_mm"] = s.meshMinMm;
    else
      j.erase("mesh_min_mm");
    return j;
  }
  nlohmann::json fixtures = nlohmann::json::array();
  for (const auto &p : s.fixtures.picks)
    fixtures.push_back(p.selector);
  nlohmann::json loads = nlohmann::json::array();
  for (const auto &ld : s.loads) {
    nlohmann::json faces = nlohmann::json::array();
    for (const auto &p : ld.faces.picks)
      faces.push_back(p.selector);
    nlohmann::json l = {{"faces", faces}};
    if (ld.pressure) {
      l["type"] = "pressure";
      l["magnitude"] = ld.pressureMpa;
    } else {
      l["type"] = "force";
      l["vec"] = {ld.vec[0], ld.vec[1], ld.vec[2]};
    }
    // ThermalFea persists its heat-load definition alongside (harmless for
    // others).
    l["thermal_kind"] = ld.thermalKind;
    l["flux_w_m2"] = ld.fluxWm2;
    l["h_w_m2k"] = ld.hWm2k;
    l["t_amb_c"] = ld.tAmbC;
    if (ld.definition.is_object()) {
      auto native = ld.definition;
      applyTypedChanges(native, ld.typedBaseline, l);
      const auto edited = [&](const char* key) {
        return l.contains(key) && (!ld.typedBaseline.contains(key) || ld.typedBaseline[key] != l[key]);
      };
      if (edited("type")) {
        if (ld.pressure) {
          native.erase("vector"); native.erase("force"); native.erase("vec");
        } else {
          native.erase("pressure"); native.erase("magnitude");
        }
      }
      if (edited("vec")) {
        if (native.contains("vector")) native["vector"] = l["vec"];
        if (native.contains("force")) native["force"] = l["vec"];
      }
      if (edited("magnitude") && native.contains("pressure")) native["pressure"] = l["magnitude"];
      const std::string kind = ld.definition.value("type", std::string());
      if (kind == "flux" && edited("flux_w_m2")) native["magnitude"] = l["flux_w_m2"];
      if (kind == "convection") {
        if (edited("h_w_m2k")) native["h"] = l["h_w_m2k"];
        if (edited("t_amb_c") && native.contains("ambient_c")) native["ambient_c"] = l["t_amb_c"];
      }
      if ((kind == "flux" || kind == "convection") && edited("thermal_kind"))
        native["type"] = ld.thermalKind == 1 ? "convection" : "flux";
      l = std::move(native);
    }
    loads.push_back(std::move(l));
  }
  float cycleMin = 0.0f, cycleMax = 0.0f;
  fatigueLoadFactors(s, cycleMin, cycleMax);
  return nlohmann::json{
      {"id", s.id},
      {"type", studyTypeKey(s.type)},
      {"name", s.name},
      {"material", s.materialKey},
      {"fixtures", fixtures},
      {"loads", loads},
      {"mesh_fineness", s.meshFineness},
      {"mesh_order", s.meshOrder},
      {"mesh_target_mm", s.meshTargetMm},
      {"mesh_min_mm", s.meshMinMm},
      {"learned_advisor", s.learnedAdvisor},
      {"advisor_max_dof", std::max(0, s.advisorMaxDof)},
      {"adapt_enabled", s.adaptEnabled},
      {"adapt_max_passes", std::clamp(s.adaptMaxPasses, 1, 6)},
      {"adapt_eta_target", s.adaptEtaTarget},
      {"n_modes", s.nModes},
      {"criterion", (int)s.criterion},
      {"flow",
       {{"dir", s.flowDir},
        {"direction", s.flowUseFaceDirection
                          ? nlohmann::json::array({s.flowFaceDirection[0],
                                                  s.flowFaceDirection[1],
                                                  s.flowFaceDirection[2]})
                          : nlohmann::json()},
        {"speed", s.flowSpeed},
        {"preset", s.flowPreset},
        {"fluid", s.flowFluid},
        {"density", s.flowDensity},
        {"viscosity", s.flowViscosity},
        {"aoa", s.flowAoA},
        {"sideslip", s.flowSideslip},
        {"roughness", s.flowRoughness},
        {"roughness_um", s.flowRoughnessUm},
        {"ref_area_mm2", s.flowRefAreaMm2},
        {"solver", s.flowSolver},
        {"thermal", s.flowThermal},
        {"thermal_type", s.flowThermalType},
        {"inlet_temp_c", s.flowInletTempC},
        {"wall_mode", s.flowWallMode},
        {"wall_temp_c", s.flowWallTempC},
        {"wall_flux_wm2", s.flowWallFluxWm2},
        {"wall_power_w", s.flowWallPowerW},
        {"solid_kappa", s.flowSolidKappa},
        {"power_w", s.flowPowerW}}},
      {"fatigue",
       {{"method", s.fatigueMethod},
        {"load_type", s.fatigueLoadType},
        {"ratio", s.fatigueRatio},
        {"lmin", cycleMin},
        {"lmax", cycleMax},
        {"se_mpa", s.fatigueSe},
        {"sut_mpa", s.fatigueSut},
        {"surface", s.fatigueSurface},
        {"load_case", s.fatigueLoadCase},
        {"size_mm", s.fatigueSizeMm},
        {"temp_c", s.fatigueTempC},
        {"reliability", s.fatigueReliability}}},
      {"motion", motionSettingsJson(s)},
      // Advanced study settings (contact proxy, vibration, thermal FEA).
      {"adv",
       {{"contact_pressure_mpa", s.contactPressureMpa},
        {"fr_dir", s.frDir},
        {"fr_zeta", s.frZeta},
        {"fr_base_g", s.frBaseG},
        {"therm_k", s.thermK},
        {"therm_cp", s.thermCp},
        {"therm_fix_temp_c", s.thermFixTempC},
        {"therm_transient", s.thermTransient},
        {"therm_t_end_s", s.thermTEndS},
        {"therm_dt_s", s.thermDtS},
        {"therm_t_init_c", s.thermTInitC}}},
      {"result_summary", s.type == StudyType::Motion ? std::string() : s.summary},
  };
}

// Read one face selector back into a SimSel, recovering the viewport identity
// stashed under "_pick" at capture time so a reopened study's faces still
// highlight.
static SimSel selFromJson(const nlohmann::json &selector) {
  SimSel ss;
  ss.selector = selector;
  if (selector.is_object()) {
    auto it = selector.find("_pick");
    if (it != selector.end() && it->is_object()) {
      ss.pickBody = (uint32_t)it->value("body", 0);
      ss.pickTopo = (uint32_t)it->value("topo", 0);
    }
  }
  return ss;
}

Study SimManager::studyFromJson(const nlohmann::json &j) {
  Study s;
  s.definition = j;
  s.id = j.value("id", std::string());
  s.type = studyTypeFromKey(j.value("type", std::string("static")));
  s.name = j.value("name", std::string("Study"));
  if (s.type == StudyType::Topology) {
    s.topo = j.is_object() ? j : nlohmann::json::object();
    topologyMeshFromJson(s, j);
    mirrorTopologyWells(s);
    s.hasResult = s.topo.contains("runs") && s.topo["runs"].is_array() &&
                  !s.topo["runs"].empty();
    s.typedBaseline = typedStudyJson(s);
    return s;
  }
  s.materialKey = j.contains("material") && j["material"].is_string()
                      ? j["material"].get<std::string>()
                      : (j.contains("material") && j["material"].is_object()
                             ? j["material"].value("key", std::string()) : std::string());
  s.meshFineness = j.value("mesh_fineness", 0.5f);
  s.meshOrder = j.value("mesh_order", 2);
  s.meshTargetMm = j.value("mesh_target_mm", 0.0f);
  s.meshMinMm = j.value("mesh_min_mm", 0.0f);
  s.learnedAdvisor = j.value("learned_advisor", false);
  s.advisorMaxDof = std::max(0, j.value("advisor_max_dof", 0));
  s.adaptEnabled = j.value("adapt_enabled", false);
  s.adaptMaxPasses =
      std::clamp(j.value("adapt_max_passes", 3), 1, 6);
  s.adaptEtaTarget =
      std::max(0.0f, j.value("adapt_eta_target", 0.05f));
  if (j.contains("fineness") && j["fineness"].is_number())
    s.meshFineness = j["fineness"].get<float>();
  if (j.contains("target_size_mm") && j["target_size_mm"].is_number())
    s.meshTargetMm = j["target_size_mm"].get<float>();
  if (j.contains("min_size_mm") && j["min_size_mm"].is_number())
    s.meshMinMm = j["min_size_mm"].get<float>();
  if (!j.contains("mesh_order") && j.contains("order") && j["order"].is_number_integer())
    s.meshOrder = j["order"].get<int>();
  if (j.contains("adapt") && j["adapt"].is_object()) {
    s.adaptEnabled = true;
    s.adaptMaxPasses = j["adapt"].value("max_passes", s.adaptMaxPasses);
    s.adaptEtaTarget = j["adapt"].value("eta_target", s.adaptEtaTarget);
  }
  s.nModes = j.value("n_modes", 6);
  s.criterion = (FailureCriterion)j.value("criterion", 0);
  if (j.contains("flow") && j["flow"].is_object()) {
    const auto &fl = j["flow"];
    s.flowDir = fl.value("dir", 0);
    const auto direction = fl.find("direction");
    if (direction != fl.end() && direction->is_array() && direction->size() == 3) {
      bool valid = true;
      double length2 = 0.0;
      for (int k = 0; k < 3; ++k) {
        double value = 0.0;
        if (!(*direction)[k].is_number())
          valid = false;
        else {
          value = (*direction)[k].get<double>();
          if (!std::isfinite(value)) valid = false;
        }
        s.flowFaceDirection[k] = static_cast<float>(value);
        length2 += value * value;
      }
      if (valid && length2 > 1e-12 && std::isfinite(length2)) {
        const double length = std::sqrt(length2);
        for (float &value : s.flowFaceDirection)
          value = static_cast<float>(value / length);
        s.flowUseFaceDirection = true;
      }
    }
    s.flowSpeed = fl.value("speed", 10.0);
    s.flowPreset = fl.value("preset", 0);
    s.flowFluid = fl.value("fluid", 0);
    s.flowDensity = fl.value("density", 1.225);
    s.flowViscosity = fl.value("viscosity", 1.5e-5);
    s.flowAoA = fl.value("aoa", 0.0);
    s.flowSideslip = fl.value("sideslip", 0.0);
    s.flowRoughness = fl.value("roughness", 0);
    s.flowRoughnessUm = fl.value("roughness_um", 0.0);
    s.flowRefAreaMm2 = fl.value("ref_area_mm2", 0.0);
    s.flowSolver = fl.value("solver", 0);
    s.flowThermal = fl.value("thermal", false);
    s.flowThermalType = fl.value("thermal_type", 0);
    s.flowInletTempC = fl.value("inlet_temp_c", 20.0);
    s.flowWallMode = fl.value("wall_mode", 2);
    s.flowWallTempC = fl.value("wall_temp_c", 80.0);
    s.flowWallFluxWm2 = fl.value("wall_flux_wm2", 0.0);
    s.flowWallPowerW = fl.value("wall_power_w", 10.0);
    s.flowSolidKappa = fl.value("solid_kappa", 200.0);
    s.flowPowerW = fl.value("power_w", 20.0);
  }
  if (j.contains("fatigue") && j["fatigue"].is_object()) {
    const auto &fa = j["fatigue"];
    s.fatigueMethod = fa.value("method", 0);
    const auto spectrum = fa.find("load_type");
    if (spectrum != fa.end() && spectrum->is_string()) {
      s.fatigueLoadType = fatigueSpectrumType(spectrum->get_ref<const std::string&>());
      s.fatigueRatio = fa.value("load_ratio", -1.0f);
      if (fa.contains("lmin") && fa.contains("lmax"))
        s.fatigueLoadType = 3; // Explicit factors take precedence in the native solver.
    } else {
      s.fatigueLoadType = fa.value("load_type", 0);
      s.fatigueRatio = fa.value("ratio", -1.0f);
    }
    if (spectrum == fa.end() && fa.contains("lmin") && fa.contains("lmax"))
      s.fatigueLoadType = 3;
    s.fatigueLmin = fa.value("lmin", -1.0);
    s.fatigueLmax = fa.value("lmax", 1.0);
    s.fatigueSe = fa.value("se_mpa", 0.0);
    s.fatigueSut = fa.value("sut_mpa", 0.0);
    s.fatigueSurface = fa.value("surface", 1);
    s.fatigueLoadCase = fa.value("load_case", 0);
    s.fatigueSizeMm = fa.value("size_mm", 25.0);
    s.fatigueTempC = fa.value("temp_c", 20.0);
    s.fatigueReliability = fa.value("reliability", 50.0);
  }
  if (j.contains("adv") && j["adv"].is_object()) {
    const auto &av = j["adv"];
    s.contactPressureMpa = av.value("contact_pressure_mpa", 1.0f);
    s.frDir = av.value("fr_dir", 2);
    s.frZeta = av.value("fr_zeta", 0.02f);
    s.frBaseG = av.value("fr_base_g", 1.0f);
    s.thermK = av.value("therm_k", 200.0f);
    s.thermCp = av.value("therm_cp", 900.0f);
    s.thermFixTempC = av.value("therm_fix_temp_c", 20.0f);
    s.thermTransient = av.value("therm_transient", false);
    s.thermTEndS = av.value("therm_t_end_s", 60.0f);
    s.thermDtS = av.value("therm_dt_s", 0.0f);
    s.thermTInitC = av.value("therm_t_init_c", 20.0f);
  }
  if (j.contains("pressure_mpa")) s.contactPressureMpa = j["pressure_mpa"].get<float>();
  if (j.contains("zeta")) s.frZeta = j["zeta"].get<float>();
  if (j.contains("base_accel_g")) s.frBaseG = j["base_accel_g"].get<float>();
  if (j.contains("excitation_dir") && j["excitation_dir"].is_array() &&
      j["excitation_dir"].size() == 3)
    for (int axis = 0; axis < 3; ++axis)
      if (j["excitation_dir"][axis].is_number() &&
          std::abs(j["excitation_dir"][axis].get<double>()) == 1.0)
        s.frDir = axis;
  if (j.contains("material") && j["material"].is_object()) {
    s.thermK = j["material"].value("k_w_mk", s.thermK);
    s.thermCp = j["material"].value("cp_j_kgk", s.thermCp);
  }
  if (j.contains("k_w_mk")) s.thermK = j["k_w_mk"].get<float>();
  if (j.contains("conductivity_w_mk")) s.thermK = j["conductivity_w_mk"].get<float>();
  if (j.contains("cp_j_kgk")) s.thermCp = j["cp_j_kgk"].get<float>();
  if (j.contains("specific_heat_j_kgk")) s.thermCp = j["specific_heat_j_kgk"].get<float>();
  if (j.contains("transient") && j["transient"].is_object()) {
    const auto& transient = j["transient"];
    s.thermTransient = true;
    s.thermTEndS = transient.value("t_end_s", s.thermTEndS);
    s.thermDtS = transient.value("dt_s", s.thermDtS);
    s.thermTInitC = transient.value("t_init_c", s.thermTInitC);
  }
  if (j.contains("motion") && j["motion"].is_object()) {
    const auto &mo = j["motion"];
    if (mo.contains("drivers") && mo["drivers"].is_array()) {
      s.motionDrivers.clear();
      for (const auto &driver : mo["drivers"])
        if (driver.is_object())
          s.motionDrivers.push_back({driver.value("mate_id", std::string()),
                                     driver.value("start", 0.0),
                                     driver.value("stop", 360.0)});
    } else {
      // Read pre-multiple-driver studies; all future saves use the shared drivers array.
      s.motionDrivers = {{mo.value("mate_id", std::string()),
                          mo.value("start", 0.0), mo.value("stop", 360.0)}};
    }
    s.motionSteps = mo.value("steps", 24);
    s.motionLoop = mo.value("loop", true);
    s.motionFps = mo.value("fps", 24.0f);
    s.motionCheckInterference = simJsonBool(mo, "check_interference", true);
    s.motionMinClearance = mo.value("min_clearance", 0.0);
  }
  if (s.type != StudyType::Motion) {
    s.summary = j.value("result_summary", std::string());
    // Documents saved before the human summaries: "max 85.2 degC | min 20.1 degC" -> " · ", °C.
    for (const auto &[from, to] : {std::pair<const char *, const char *>{" | ", " \xc2\xb7 "},
                                   {" degC", " \xc2\xb0" "C"},
                                   {" @ ", " at "}})
      for (std::size_t p = s.summary.find(from); p != std::string::npos;
           p = s.summary.find(from, p + std::strlen(to)))
        s.summary.replace(p, std::strlen(from), to);
  }
  if (j.contains("fixtures") && j["fixtures"].is_array())
    for (const auto &sel : j["fixtures"])
      s.fixtures.picks.push_back(selFromJson(sel));
  if (s.type == StudyType::ThermalFea && j.contains("fixtures") && j["fixtures"].is_array())
    for (const auto& fixture : j["fixtures"])
      if (fixture.is_object() && fixture.contains("temp_c") && fixture["temp_c"].is_number()) {
        s.thermFixTempC = fixture["temp_c"].get<float>();
        break;
      }
  if (j.contains("loads") && j["loads"].is_array())
    for (const auto &ld : j["loads"]) {
      SimLoad l;
      if (!ld.is_object()) continue; // Preserve native query forms in definition without retyping.
      const std::string kind = ld.value("type", std::string("force"));
      l.pressure = kind == "pressure" || (ld.contains("pressure") && ld["pressure"].is_number());
      if (l.pressure)
        l.pressureMpa = ld.value("pressure", ld.value("magnitude", 0.1f));
      else {
        const auto vector = ld.contains("vector") ? ld["vector"] :
                            ld.contains("force") ? ld["force"] : ld.value("vec", nlohmann::json());
        if (vector.is_array() && vector.size() == 3)
          for (int k = 0; k < 3; ++k) l.vec[k] = vector[k].get<float>();
      }
      l.thermalKind = kind == "convection" ? 1 : ld.value("thermal_kind", 0);
      l.fluxWm2 = kind == "flux" ? ld.value("magnitude", 1000.0f) : ld.value("flux_w_m2", 1000.0f);
      l.hWm2k = ld.value("h", ld.value("h_w_m2k", 10.0f));
      l.tAmbC = ld.value("t_amb_c", ld.value("ambient_c", 25.0f));
      if (ld.contains("faces") && ld["faces"].is_array())
        for (const auto &sel : ld["faces"])
          l.faces.picks.push_back(selFromJson(sel));
      else if (ld.contains("faces") && ld["faces"].is_object())
        l.faces.picks.push_back(selFromJson(ld["faces"]));
      else if (ld.contains("face"))
        l.faces.picks.push_back(selFromJson(ld["face"]));
      s.loads.push_back(std::move(l));
      auto& saved = s.loads.back();
      saved.definition = ld;
      Study projection;
      projection.loads.push_back(saved);
      projection.loads.back().definition = nlohmann::json();
      saved.typedBaseline = typedStudyJson(projection)["loads"][0];
    }
  s.typedBaseline = typedStudyJson(s);
  return s;
}

nlohmann::json SimManager::activeStudyDefinition() const {
  return sel_ >= 0 && sel_ < static_cast<int>(studies_.size())
             ? studyToJson(studies_[sel_]) : nlohmann::json();
}

namespace {
bool validateParameterTypes(const nlohmann::json& candidate, const nlohmann::json& schema,
                            const std::string& path, std::string& error) {
  if (path == "/fatigue/load_type" && candidate.is_string()) {
    if (fatigueSpectrumType(candidate.get_ref<const std::string&>()) < 0) {
      error = path + " must be reversed, zero_based, ratio, or a GUI spectrum index.";
      return false;
    }
    return true;
  }
  if (schema.is_object()) {
    if (!candidate.is_object()) { error = path + " must be an object."; return false; }
    for (auto it = schema.begin(); it != schema.end(); ++it) {
      if (!candidate.contains(it.key()) || it.key() == "fixtures" || it.key() == "loads" ||
          it.key() == "result_summary" || it.key() == "material") continue;
      if (!validateParameterTypes(candidate[it.key()], it.value(), path + "/" + it.key(), error))
        return false;
    }
    return true;
  }
  bool valid = schema.is_number_integer() ? candidate.is_number_integer()
             : schema.is_number() ? candidate.is_number()
             : schema.is_boolean() ? candidate.is_boolean()
             : schema.is_string() ? candidate.is_string()
             : schema.is_array() ? candidate.is_array() : true;
  if (valid && candidate.is_number()) valid = std::isfinite(candidate.get<double>());
  if (!valid) { error = path + " has the wrong value type."; return false; }
  if (schema.is_array() && !schema.empty())
    for (std::size_t i = 0; i < candidate.size(); ++i)
      if (!validateParameterTypes(candidate[i], schema[0], path + "/" + std::to_string(i), error))
        return false;
  return true;
}
int resizeParameterText(ImGuiInputTextCallbackData* data) {
  if (data->EventFlag == ImGuiInputTextFlags_CallbackResize) {
    auto* text = static_cast<std::string*>(data->UserData);
    text->resize(static_cast<std::size_t>(data->BufTextLen));
    data->Buf = text->data();
  }
  return 0;
}

// Study fields are mostly `float`; widened to double they print as float noise
// ("0.050000000745"). Show such a value as the shortest decimal that reads back
// as the same float. Values a float cannot hold exactly are true doubles: kept.
double tidyFloat(double v) {
  const float f = static_cast<float>(v);
  if (!std::isfinite(v) || static_cast<double>(f) != v) return v;
  char buf[32];
  for (int digits = 1; digits <= 9; ++digits) {
    std::snprintf(buf, sizeof(buf), "%.*g", digits, static_cast<double>(f));
    if (std::strtof(buf, nullptr) == f) return std::strtod(buf, nullptr);
  }
  return v;
}

void tidyFloats(nlohmann::json& j) {
  if (j.is_number_float()) j = tidyFloat(j.get<double>());
  else if (j.is_structured())
    for (auto& child : j) tidyFloats(child);
}

// Undo tidyFloats on an applied draft: a number the user left as displayed gets
// its exact stored value back, so applying an untouched draft changes nothing.
void restoreExactFloats(nlohmann::json& draft, const nlohmann::json& base) {
  if (draft.is_number_float() && base.is_number_float()) {
    if (tidyFloat(base.get<double>()) == draft.get<double>()) draft = base;
  } else if (draft.is_object() && base.is_object()) {
    for (auto it = draft.begin(); it != draft.end(); ++it)
      if (base.contains(it.key())) restoreExactFloats(it.value(), base[it.key()]);
  } else if (draft.is_array() && base.is_array() && draft.size() == base.size()) {
    for (std::size_t i = 0; i < draft.size(); ++i) restoreExactFloats(draft[i], base[i]);
  }
}

// The parameter editor shows a study only the settings its kind uses: the typed
// record carries every kind's block (flow, fatigue, motion, adv...), which reads
// as noise on, say, a Buckling study. Hidden values are kept and restored on apply.
bool parameterKeyShown(StudyType type, const std::string& key) {
  using T = StudyType;
  if (key == "flow") return isFlowFamily(type);
  if (key == "fatigue") return type == T::Fatigue;
  if (key == "motion") return type == T::Motion;
  if (key == "criterion" || key.rfind("adapt_", 0) == 0) return type == T::Static;
  if (key == "n_modes")
    return type == T::Modal || type == T::Buckling || type == T::FreqResponse;
  if (key.rfind("mesh_", 0) == 0 || key == "learned_advisor" || key == "advisor_max_dof")
    return type != T::Motion;
  return true;
}

// Which "adv" sub-keys belong to a study kind (empty prefix: none of them).
const char* advPrefixFor(StudyType type) {
  using T = StudyType;
  switch (type) {
  case T::Contact: return "contact_";
  case T::FreqResponse: return "fr_";
  case T::ThermalFea: return "therm_";
  default: return "";
  }
}

// The study kind a definition belongs to, read exactly as studyFromJson does.
StudyType parameterViewType(const nlohmann::json& definition) {
  return definition.is_object()
             ? studyTypeFromKey(definition.value("type", std::string("static")))
             : StudyType::Static;
}

// Put back everything the editor view left out (and the user therefore never saw),
// so applying the shown subset cannot drop another kind's stored settings.
void restoreHiddenParameters(nlohmann::json& draft, const nlohmann::json& full) {
  if (parameterViewType(full) == StudyType::Topology || !draft.is_object() || !full.is_object())
    return;
  const nlohmann::json view = SimManager::parameterEditorView(full);
  for (auto it = full.begin(); it != full.end(); ++it) {
    if (it.key() == "adv" && it->is_object() && view.contains("adv")) {
      if (!draft.contains("adv") || !draft["adv"].is_object()) continue;
      for (auto sub = it->begin(); sub != it->end(); ++sub)
        if (!view["adv"].contains(sub.key()) && !draft["adv"].contains(sub.key()))
          draft["adv"][sub.key()] = sub.value();
    } else if (!view.contains(it.key()) && !draft.contains(it.key())) {
      draft[it.key()] = it.value();
    }
  }
}
}

nlohmann::json SimManager::parameterEditorView(const nlohmann::json& definition) {
  const StudyType type = parameterViewType(definition);
  nlohmann::json view = definition;
  if (type != StudyType::Topology && view.is_object()) {
    for (auto it = definition.begin(); it != definition.end(); ++it)
      if (!parameterKeyShown(type, it.key())) view.erase(it.key());
    if (view.contains("adv") && view["adv"].is_object()) {
      const std::string prefix = advPrefixFor(type);
      nlohmann::json adv = nlohmann::json::object();
      if (!prefix.empty())
        for (auto it = view["adv"].begin(); it != view["adv"].end(); ++it)
          if (it.key().rfind(prefix, 0) == 0) adv[it.key()] = it.value();
      if (adv.empty()) view.erase("adv");
      else view["adv"] = std::move(adv);
    }
  }
  tidyFloats(view);
  return view;
}

bool SimManager::configureActiveStudy(AppState& st, const nlohmann::json& candidate,
                                      std::string& error) {
  error.clear();
  if (sel_ < 0 || sel_ >= static_cast<int>(studies_.size()) || !st.docMirror.hasDoc()) {
    error = "Select a study in an open document."; return false;
  }
  if (st.rpc.interactionBusy() || runUi().running || motionRun_ || prepInFlight_ || meshPreviewPending_) {
    error = "Wait for the current simulation or engine request before editing setup."; return false;
  }
  if (!candidate.is_object()) { error = "The study definition must be a JSON object."; return false; }
  Study& current = studies_[sel_];
  const auto before = studyToJson(current);
  auto definition = candidate;
  for (const char* key : {"id", "type", "results", "result", "runs", "last_job",
                          "last_run", "result_summary", "summary", "provenance",
                          "mesh_quality", "reactions", "advisor", "n_nodes", "n_elems",
                          "n_dof", "freqs_hz"}) {
    if (candidate.contains(key) && (!before.contains(key) || candidate[key] != before[key])) {
      error = std::string(key) + " is managed by the study or solver and cannot be edited."; return false;
    }
    if (before.contains(key)) definition[key] = before[key];
    else definition.erase(key);
  }
  Study replacement;
  // Both names are native inputs; the solver gives mesh_order precedence. A one-key edit
  // updates its retained counterpart, while an independently conflicting pair is ambiguous.
  for (const char* key : {"mesh_order", "order"})
    if (definition.contains(key) &&
        (!definition[key].is_number_integer() ||
         (definition[key] != 1 && definition[key] != 2))) {
      error = std::string("/") + key + " must be 1 or 2.";
      return false;
    }
  if (definition.contains("mesh_order") && definition.contains("order") &&
      definition["mesh_order"] != definition["order"]) {
    const bool canonicalEdited = !before.contains("mesh_order") ||
                                 definition["mesh_order"] != before["mesh_order"];
    const bool aliasEdited = !before.contains("order") || definition["order"] != before["order"];
    if (canonicalEdited == aliasEdited) {
      error = "/mesh_order and /order conflict; give both the same element order. Current: mesh_order=" +
              (before.contains("mesh_order") ? before["mesh_order"].dump() : "unset") +
              ", order=" + (before.contains("order") ? before["order"].dump() : "unset") +
              "; draft: mesh_order=" + definition["mesh_order"].dump() +
              ", order=" + definition["order"].dump() + ".";
      return false;
    }
    if (canonicalEdited) definition["order"] = definition["mesh_order"];
    else definition["mesh_order"] = definition["order"];
  }
  try {
    if (current.type == StudyType::Topology) {
      auto validated = current.topo;
      if (!topology::applyStudyDefinition(validated, definition, topoCaps_,
                                          topologyMaterialLookup(), error)) return false;
      definition = std::move(validated);
    } else {
      const auto schema = typedStudyJson(Study{});
      if (!validateParameterTypes(definition, schema, "", error)) return false;
      if (definition.contains("fatigue") && definition["fatigue"].is_object()) {
        const auto& fatigue = definition["fatigue"];
        if (!validateParameterTypes(fatigue, nlohmann::json{{"load_ratio", 0.0}},
                                    "/fatigue", error)) return false;
        if (fatigue.contains("load_type") && fatigue["load_type"].is_string() &&
            fatigueSpectrumType(fatigue["load_type"].get_ref<const std::string&>()) == 2 &&
            !fatigue.contains("lmin") && !fatigue.contains("lmax") &&
            (!fatigue.contains("load_ratio") || !fatigue["load_ratio"].is_number() ||
             fatigue["load_ratio"].get<double>() >= 1.0)) {
          error = "/fatigue/load_ratio must be finite and less than 1 for a ratio spectrum.";
          return false;
        }
      }
      if (definition.contains("material") && !definition["material"].is_string() &&
          !definition["material"].is_object() && !definition["material"].is_null()) {
        error = "/material must be a library key or native material object."; return false;
      }
      if (definition.contains("material") && definition["material"].is_object() &&
          !validateParameterTypes(definition["material"],
              nlohmann::json{{"E_mpa", 1.0}, {"nu", 0.3}, {"density_g_cm3", 1.0},
                             {"yield_mpa", 1.0}, {"ultimate_mpa", 1.0},
                             {"endurance_mpa", 1.0}, {"k_w_mk", 1.0}, {"cp_j_kgk", 1.0}},
              "/material", error)) return false;
      if (!validateParameterTypes(definition,
          nlohmann::json{{"pressure_mpa", 1.0}, {"zeta", 0.02}, {"base_accel_g", 1.0},
                         {"conductivity_w_mk", 1.0}, {"specific_heat_j_kgk", 1.0},
                         {"k_w_mk", 1.0}, {"cp_j_kgk", 1.0}, {"max_load_factor", 1.0},
                         {"transient", {{"t_end_s", 1.0}, {"dt_s", 1.0}, {"t_init_c", 1.0},
                                        {"capacitance", "lumped"}}}},
          "", error)) return false;
      for (const char* collection : {"fixtures", "loads"})
        if (definition.contains(collection) && !definition[collection].is_array()) {
          error = std::string("/") + collection + " must be an array."; return false;
        }
      if (definition.contains("loads"))
        for (const auto& load : definition["loads"]) {
          if (!load.is_object()) { error = "/loads entries must be native load objects."; return false; }
          if (!validateParameterTypes(load,
                nlohmann::json{{"type", "force"}, {"magnitude", 0.1}, {"pressure", 0.1},
                               {"thermal_kind", 0}, {"flux_w_m2", 0.0}, {"h", 0.0},
                               {"h_w_m2k", 0.0}, {"t_amb_c", 0.0}, {"ambient_c", 0.0},
                               {"vec", nlohmann::json::array({0.0, 0.0, 0.0})},
                               {"vector", nlohmann::json::array({0.0, 0.0, 0.0})},
                               {"force", nlohmann::json::array({0.0, 0.0, 0.0})}},
                "/loads", error)) return false;
          for (const char* key : {"vec", "vector", "force"})
            if (load.contains(key) && load[key].size() != 3) {
              error = std::string("/loads/") + key + " needs three components."; return false;
            }
          if (load.contains("faces") && !load["faces"].is_array() &&
              !load["faces"].is_object()) {
            error = "/loads/faces must be selectors or a native resolved faceset object."; return false;
          }
        }
    }
    replacement = studyFromJson(definition);
    if (replacement.meshOrder != 1 && replacement.meshOrder != 2) {
      error = "/mesh_order must be 1 or 2."; return false;
    }
    if (replacement.meshFineness < 0 || replacement.meshFineness > 1 ||
        replacement.meshTargetMm < 0 || replacement.meshMinMm < 0) {
      error = "Mesh fineness must be 0..1 and mesh sizes cannot be negative."; return false;
    }
  } catch (const nlohmann::json::exception& e) {
    error = std::string("Invalid study value: ") + e.what(); return false;
  }
  if (definition == before) return true;
  const bool updatePartMaterial = !isFlowFamily(current.type) && current.type != StudyType::Topology &&
      definition.contains("material") && definition["material"].is_string() &&
      (!before.contains("material") || definition["material"] != before["material"]) &&
      replacement.materialKey != docMaterialKey(st);
  if (updatePartMaterial && materialIndexByKey(replacement.materialKey) < 0) {
    error = "/material must name an installed simulation material.";
    return false;
  }
  if (updatePartMaterial && st.docLockedByAgent(st.docMirror.docId())) {
    error = st.agentLockReason(st.docMirror.docId());
    st.toast(ui::ToastLevel::Warn, error);
    return false;
  }
  const bool changed = setupDefinition(definition) != setupDefinition(before);
  if (changed && current.type != StudyType::Topology) {
    for (const char* key : {"results", "result", "result_summary", "summary",
                            "mesh_quality", "reactions", "provenance", "advisor",
                            "n_nodes", "n_elems", "n_dof", "freqs_hz"})
      definition.erase(key);
  }
  nlohmann::json result;
  std::string rpcError;
  if (!st.worker.call("sim_study_edit", {{"doc_id", st.docMirror.docId()},
                     {"study_id", current.id}, {"study", definition}}, result, rpcError, 15000) ||
      !result.is_object() || result.contains("code") || !simJsonBool(result, "ok", true)) {
    error = simJsonText(result, "message", "error", rpcError.empty() ?
                       "Could not save the study definition." : rpcError);
    return false;
  }
  if (updatePartMaterial &&
      !applyPartMaterialNow(st, replacement, replacement.materialKey, error)) {
    // Validation is complete, but the shared part-material write was refused. Do not leave
    // a successfully saved study claiming a material that tick would replace from the part.
    nlohmann::json restored;
    std::string restoreError;
    if (!st.worker.call("sim_study_edit", {{"doc_id", st.docMirror.docId()},
                       {"study_id", current.id}, {"study", before}}, restored, restoreError, 15000) ||
        !restored.is_object() || restored.contains("code") || !simJsonBool(restored, "ok", true)) {
      error += " The previous study definition could not be restored: " +
               simJsonText(restored, "message", "error", restoreError);
      st.addLog("[sim] " + error);
      markStudiesStale();
    }
    markSimDocDirty(st, st.docMirror.docId());
    return false;
  }
  if (!changed) {
    current.name = replacement.name;
    current.definition = definition;
    current.typedBaseline = typedStudyJson(current);
  } else {
    if (current.type == StudyType::Motion) invalidateMotion(current);
    replacement.motionGeneration = current.motionGeneration;
    replacement.definition = definition;
    clearTransientResult(replacement);
    replacement.typedBaseline = typedStudyJson(replacement);
    if (replacement.type == StudyType::Topology)
      replacement.hasResult = definition.contains("runs") && definition["runs"].is_array() &&
                              !definition["runs"].empty();
    current = std::move(replacement);
    clearMeshPreview();
    if (resultShown_) restoreModelMesh(st);
    else clearResultOverlay(st);
    armedWell_ = -1;
    loadIdx_ = -1;
  }
  ++parameterSourceGeneration_;
  markSimDocDirty(st, st.docMirror.docId());
  return true;
}

void SimManager::drawParameterEditor(AppState& st, Study& s) {
  if (parameterDocId_ != st.docMirror.docId() || parameterStudyId_ != s.id) {
    parameterDocId_ = st.docMirror.docId();
    parameterStudyId_ = s.id;
    parameterBase_ = nlohmann::json();
    parameterBaseText_.clear();
    parameterDraft_.clear();
    parameterError_.clear();
    parameterDraftDirty_ = false;
    parameterStale_ = false;
    parameterWasVisible_ = false;
  }
  if (takeFocus("sim_parameters")) parameterEditorOpen_ = true;
  const bool body = kit::cardBegin("sim_parameters", "Advanced parameters", nullptr,
                                    &parameterEditorOpen_);
  if (body) {
    const auto reload = [&](nlohmann::json current) {
      parameterBase_ = std::move(current);
      // The text shows this study kind's settings only, with float noise removed; Apply
      // restores the hidden settings and exact stored values (restoreHiddenParameters /
      // restoreExactFloats), so an untouched draft round-trips unchanged.
      parameterBaseText_ = parameterEditorView(parameterBase_).dump(2);
      parameterDraft_ = parameterBaseText_;
      parameterDraftDirty_ = false;
      parameterError_.clear();
      parameterStale_ = false;
      parameterSeenGeneration_ = parameterSourceGeneration_;
      parameterSeenRevision_ = (long)st.docMirror.revision();
      parameterSeenMaterial_ = s.materialKey;
      parameterSeenSummary_ = s.summary;
      // InputText keeps an active buffer of its own: a clean native refresh must also reload it.
      ++parameterInputGeneration_;
    };
    const bool sourceChanged = !parameterWasVisible_ || parameterBase_.is_null() ||
        parameterSeenGeneration_ != parameterSourceGeneration_ ||
        parameterSeenRevision_ != (long)st.docMirror.revision() ||
        parameterSeenMaterial_ != s.materialKey || parameterSeenSummary_ != s.summary;
    // Non-editing refreshes follow native setup once, not on every rendered frame. User edits
    // alone pin the baseline; dirty drafts survive collapse/reopen and concurrent setup edits.
    if (sourceChanged || (parameterStale_ && !parameterDraftDirty_)) {
      auto current = studyToJson(s);
      parameterStale_ = current != parameterBase_;
      parameterSeenGeneration_ = parameterSourceGeneration_;
      parameterSeenRevision_ = (long)st.docMirror.revision();
      parameterSeenMaterial_ = s.materialKey;
      parameterSeenSummary_ = s.summary;
      if (parameterBase_.is_null() || (!parameterDraftDirty_ && parameterStale_))
        reload(std::move(current));
    }
    kit::dimText("Every setting this study saves, as editable JSON. Units follow the key "
                 "names (for example _mm, _mpa). Settings that only other study types use "
                 "are hidden. Apply checks your edits before saving; the study's ID, type "
                 "and results cannot be changed here.");
    ImGui::SetNextItemWidth(-1.0f);
    ImGui::PushID(parameterInputGeneration_);
    if (ImGui::InputTextMultiline("##sim_parameter_json", parameterDraft_.data(),
                                 parameterDraft_.capacity() + 1,
                                 ImVec2(-1.0f, ImGui::GetFrameHeightWithSpacing() * 10),
                                 ImGuiInputTextFlags_CallbackResize, resizeParameterText, &parameterDraft_))
      parameterDraftDirty_ = parameterDraft_ != parameterBaseText_;
    ImGui::PopID();
    const bool busy = st.rpc.interactionBusy() || runUi().running || motionRun_ || prepInFlight_;
    if (parameterStale_ && parameterDraftDirty_)
      kit::inlineStatus("Setup changed while this draft was open. Revert to reload current parameters.",
                        kit::Tone::Err);
    else if (!parameterError_.empty())
      kit::inlineStatus(parameterError_.c_str(), kit::Tone::Err);
    ImGui::PushID("sim_parameters_apply");
    if (kit::primaryButton("Apply parameters", !busy,
                            "Wait for the current simulation or engine request.")) {
      auto draft = nlohmann::json::parse(parameterDraft_, nullptr, false);
      if (draft.is_discarded()) {
        try { draft = nlohmann::json::parse(parameterDraft_); }
        catch (const nlohmann::json::exception& e) { parameterError_ = e.what(); }
      } else if (studyToJson(s) != parameterBase_) {
        parameterStale_ = true;
        parameterError_.clear();
      } else {
        restoreHiddenParameters(draft, parameterBase_);
        restoreExactFloats(draft, parameterBase_);
        if (configureActiveStudy(st, draft, parameterError_)) reload(studyToJson(s));
      }
    }
    ImGui::PopID();
    ImGui::PushID("sim_parameters_revert");
    if (kit::secondaryButton("Revert"))
      reload(studyToJson(s));
    ImGui::PopID();
  }
  parameterWasVisible_ = body;
  kit::cardEnd();
}

// ---------------------------------------------------------------- refresh /
// mirror

void SimManager::refresh(AppState &st) {
  if (!st.docMirror.hasDoc()) {
    studies_.clear();
    sel_ = -1;
    return;
  }
  if (st.rpc.busy())
    return; // Never wait on the motion solve's serialized worker mutex.
  nlohmann::json res;
  std::string err;
  if (!st.worker.call("sim_study_list", {{"doc_id", st.docMirror.docId()}}, res,
                      err, 15000) ||
      !res.is_object() || res.contains("code") ||
      !res.value("ok", true)) {
    const std::string detail =
        res.is_object() ? res.value("message", res.value("error", err))
                        : (err.empty() ? "invalid response" : err);
    st.addLog("[sim] sim_study_list failed: " + detail);
    listErrorDocId_ = st.docMirror.docId();
    listErrorRevision_ = (long)st.docMirror.revision();
    listError_ = detail;
    return;
  }
  listErrorDocId_.clear();
  listError_.clear();
  // Preserve the transient (non-persisted) result fields across a refresh by
  // id.
  std::vector<Study> prev = std::move(studies_);
  studies_.clear();
  // Piping / vessel / duct / internal-flow records belong to the Piping panel and
  // hydraulic_circuit records to the Hydraulics panel, not the FEA mirror (studyFromJson would
  // misread them as static studies and persist them back as such).
  std::vector<nlohmann::json> pipingRecords;
  std::vector<nlohmann::json> hydraulicRecords;
  if (res.contains("studies") && res["studies"].is_array())
    for (const auto &j : res["studies"]) {
      if (j.is_object() &&
          PipingPanel::isPipingStudyType(j.value("type", std::string()))) {
        pipingRecords.push_back(j);
        continue;
      }
      if (j.is_object() &&
          HydraulicPanel::isHydraulicStudyType(j.value("type", std::string()))) {
        hydraulicRecords.push_back(j);
        continue;
      }
      Study s = studyFromJson(j);
      for (const auto &p : prev)
        if (p.id == s.id) {
          if (setupDefinition(studyToJson(s)) != setupDefinition(studyToJson(p))) {
            s.motionGeneration = p.motionGeneration + 1;
            clearTransientResult(s);
            break;
          }
          s.hasResult = p.hasResult;
          s.banner = p.banner;
          s.dragTrace = p.dragTrace;
          s.dragTraceTotal = p.dragTraceTotal;
          s.dragTraceStride = p.dragTraceStride;
          s.advisor = p.advisor;
          s.resultSummary = p.resultSummary;
          s.advResult = p.advResult;
          s.overlayPath = p.overlayPath;
          s.nNodes = p.nNodes;
          s.nElems = p.nElems;
          s.nDof = p.nDof;
          s.freqs = p.freqs;
          s.frSweep = p.frSweep;
          s.frAccel = p.frAccel;
          s.dragN = p.dragN;
          s.liftN = p.liftN;
          s.sideN = p.sideN;
          s.cd = p.cd;
          s.cl = p.cl;
          s.ld = p.ld;
          s.reynolds = p.reynolds;
          s.frontalMm2 = p.frontalMm2;
          s.refAreaMm2 = p.refAreaMm2;
          s.cpMin = p.cpMin;
          s.cpMax = p.cpMax;
          s.vmaxMps = p.vmaxMps;
          s.flowFields = p.flowFields;
          s.solverNotes = p.solverNotes;
          if (!p.summary.empty())
            s.summary = p.summary;
          if (s.type == StudyType::Topology && p.type == StudyType::Topology) {
            // Runs come from the document; the overlay/status/artifact checks are local.
            s.hasResult = p.hasResult || (s.topo.contains("runs") && s.topo["runs"].is_array() &&
                                          !s.topo["runs"].empty());
            s.topoStatus = p.topoStatus;
            s.topoStatusKind = p.topoStatusKind;
            s.topoFieldLabel = p.topoFieldLabel;
            s.topoFieldUnit = p.topoFieldUnit;
            s.topoFieldFromSI = p.topoFieldFromSI;
            if (s.topo.value("runs", nlohmann::json()) == p.topo.value("runs", nlohmann::json())) {
              s.topoRunsView = p.topoRunsView;
              s.topoRunsCheckKey = p.topoRunsCheckKey;
            }
          }
          // Motion transient result
          if (s.type == StudyType::Motion && motionSettingsJson(s) != motionSettingsJson(p)) {
            s.motionGeneration = p.motionGeneration + 1;
            clearTransientResult(s);
            s.summary = "Setup changed; run Motion again.";
            break;
          }
          s.motionGeneration = p.motionGeneration;
          s.motionSourceRevision = p.motionSourceRevision;
          s.motionRunning = p.motionRunning;
          s.motionSource = p.motionSource;
          s.motionInterference = p.motionInterference;
          s.motionResultDrivers = p.motionResultDrivers;
          s.motionCollisionFrames = p.motionCollisionFrames;
          s.motionHasResult = p.motionHasResult;
          s.motionPlaying = p.motionPlaying;
          s.motionFrame = p.motionFrame;
          s.motionLastTick = p.motionLastTick;
          s.motionTrajectory = p.motionTrajectory;
          s.motionDriverUnit = p.motionDriverUnit;
          s.motionRenderError = p.motionRenderError;
          s.motionBodyIds = p.motionBodyIds;
          s.motionBodyLabels = p.motionBodyLabels;
          s.motionPlotBody = p.motionPlotBody;
          break;
        }
      studies_.push_back(std::move(s));
    }
  lastDocId_ = st.docMirror.docId();
  piping_.setStudies(st, lastDocId_, pipingRecords);
  hydraulic_.setStudies(st, lastDocId_, hydraulicRecords);
  lastRevision_ = (long)st.docMirror.revision();
  ++parameterSourceGeneration_;
  if (sel_ >= (int)studies_.size())
    sel_ = studies_.empty() ? -1 : (int)studies_.size() - 1;
}

void SimManager::persist(AppState &st, Study &s) {
  if (!st.docMirror.hasDoc())
    return;
  persist(st, st.docMirror.docId(), s);
}

void SimManager::persist(AppState &st, const std::string &docId, Study &s) {
  if (s.id.empty() || docId.empty())
    return;
  ++parameterSourceGeneration_;
  if (s.type == StudyType::Topology)
    mirrorTopologyWells(s); // keep the indicator mirror in step with the edited record
  auto definition = studyToJson(s);
  if (s.definition.is_object() &&
      setupDefinition(definition) != setupDefinition(s.definition)) {
    if (s.type == StudyType::Motion) invalidateMotion(s);
    clearTransientResult(s);
    s.meshHasQuality = false;
    clearMeshPreview();
    if (simOriginActive(st, this, docId)) {
      if (resultShown_) restoreModelMesh(st);
      else clearResultOverlay(st);
    }
    // Retained topology runs are immutable evidence; other cached results describe old setup.
    if (s.type != StudyType::Topology) {
      for (const char* key : {"results", "result", "result_summary", "summary",
                              "mesh_quality", "reactions", "provenance", "advisor",
                              "n_nodes", "n_elems", "n_dof", "freqs_hz"})
        definition.erase(key);
    }
    else
      s.hasResult = definition.contains("runs") && definition["runs"].is_array() &&
                    !definition["runs"].empty();
  }
  s.definition = std::move(definition);
  s.typedBaseline = typedStudyJson(s);
  if (s.type == StudyType::Motion || s.type == StudyType::Topology) {
    const std::string studyId = s.id;
    const bool topo = s.type == StudyType::Topology;
    st.rpc.call("sim_study_edit",
                {{"doc_id", docId}, {"study_id", studyId}, {"study", studyToJson(s)}},
                [this, &st, docId, studyId, topo](const core::RpcResult &r) {
      if (!simDocOpen(st, docId))
        return;
      if (!r.ok || !r.result.is_object() || r.result.contains("code") ||
          !simJsonBool(r.result, "ok", true)) {
        st.addLog(std::string(topo ? "[topology]" : "[motion]") + " could not save " +
                  studyId + ": " + simJsonText(r.result, "message", "error", r.err));
        if (simOriginActive(st, this, docId))
          st.toast(ui::ToastLevel::Error,
                   topo ? "Could not save the topology study; retry the edit."
                        : "Could not save Motion setup; retry the edit.");
        return;
      }
      markSimDocDirty(st, docId);
    });
    return;
  }
  nlohmann::json res;
  std::string err;
  nlohmann::json params = {{"doc_id", docId},
                           {"study_id", s.id},
                           {"study", studyToJson(s)}};
  if (!st.worker.call("sim_study_edit", params, res, err, 15000) ||
      !res.is_object() || res.contains("code") ||
      !res.value("ok", true)) {
    st.addLog("[sim] sim_study_edit failed for " + docId + ": " +
              (res.is_object()
                   ? res.value("message", res.value("error", err))
                   : (err.empty() ? "invalid response" : err)));
    if (simOriginActive(st, this, docId))
      st.toast(ui::ToastLevel::Error, "Could not save simulation setup");
    return;
  }
  markSimDocDirty(st, docId);
}

void SimManager::newStudy(AppState &st, StudyType t) {
  if (st.rpc.interactionBusy()) {
    st.toast(ui::ToastLevel::Info, "Wait for the current engine request before adding a study.");
    return;
  }
  if (!st.docMirror.hasDoc()) {
    st.toast(ui::ToastLevel::Warn, "Open a part before creating a study");
    return;
  }
  refresh(st);
  Study s;
  s.type = t;
  if (t == StudyType::Motion)
    seedMotionDriver(s, motionJoints(st.docMirror.tree()));
  if (t == StudyType::Buckling)
    s.nModes = 4;
  // "<Type> N": the lowest N no study of this document already uses ("Topology 1", not the
  // document's ninth study number).
  for (int n = 1;; ++n) {
    s.name = std::string(studyTypeLabel(t)) + " " + std::to_string(n);
    if (std::none_of(studies_.begin(), studies_.end(),
                     [&](const Study &o) { return o.name == s.name; }))
      break;
  }
  // Structural & frequency studies need a material (stiffness/density/yield).
  // Flow/Thermal run the OpenFOAM pipeline and never read materialKey, so leave
  // it unset for them — the tree hides the Material node and nothing in runFlow
  // consumes the record.
  if (!isFlowFamily(t)) {
    // Prefer the part's PHYSICAL material (doc.material.key via set_material).
    // No fallback to a library default — if the part has no material the study
    // starts unset and canRun blocks until the user picks one (which writes
    // set_material on the part).
    resolveStudyMaterialFromDoc(st, s);
  }
  // A Thermal study IS the OpenFOAM heat-transfer pipeline — force the solver +
  // thermal on so the form opens straight into the thermal setup (no LBM, no
  // aero-only fallback).
  if (t == StudyType::Thermal) {
    s.flowSolver = 1;
    s.flowThermal = true;
  }
  if (isFlowFamily(t)) {
    // Persist the actual engine parameters: agents and GUI reopen the same
    // Quick preview rather than deriving a private display-only preset.
    s.definition = typedStudyJson(s);
    s.typedBaseline = s.definition;
    s.definition["flow"]["cells"] = 58;
    s.definition["flow"]["n_steps"] = 667;
  }
  if (t == StudyType::Topology) {
    // A topology study optimizes ONE part's design domain: assemblies (several bodies to
    // couple) and documents without a solid are refused rather than approximated.
    if (activeDocIsAssembly(st)) {
      st.toast(ui::ToastLevel::Warn,
               "Topology optimization runs on a part's design domain. Open the part.");
      return;
    }
    if (!hasSimulationGeometry(st)) {
      st.toast(ui::ToastLevel::Warn,
               "Model a solid design domain before creating a topology study.");
      return;
    }
    s.topo = nlohmann::json::object();
    s.topo["mesh_order"] = 1;
    nlohmann::json config;
    if (topoCaps_.state == topology::Capabilities::State::Ready)
      config = topology::newTopologyConfig(topoCaps_, "structural");
    if (!config.is_object() || config.empty())
      config = {{"physics", "structural"}}; // method/objective chosen once caps arrive
    s.topo["topology"] = std::move(config);
    const std::string partMaterial = docMaterialKey(st);
    if (!partMaterial.empty())
      s.topo["material"] = partMaterial;
    s.meshOrder = 1;
  }
  nlohmann::json res;
  std::string err;
  nlohmann::json params = {{"doc_id", st.docMirror.docId()},
                           {"study", studyToJson(s)}};
  if (!st.worker.call("sim_study_add", params, res, err, 15000) ||
      !res.is_object() || res.contains("code") ||
      !res.value("ok", true)) {
    st.addLog("[sim] sim_study_add failed: " +
              (res.is_object()
                   ? res.value("message", res.value("error", err))
                   : (err.empty() ? "invalid response" : err)));
    st.toast(ui::ToastLevel::Error, "Could not create simulation study");
    return;
  }
  st.markActiveTabDirty();
  refresh(st);
  // Select the newly added study (matched by id).
  std::string newId = res.value("study_id", std::string());
  sel_ = -1;
  for (int i = 0; i < (int)studies_.size(); ++i)
    if (studies_[i].id == newId) {
      sel_ = i;
      break;
    }
  if (sel_ < 0 && !studies_.empty())
    sel_ = (int)studies_.size() - 1;
  node_ = SimNode::Study;
  armedWell_ = -1;
  st.leftPaneTab = AppState::LP_Simulation;
}

void SimManager::deleteStudy(AppState &st, int i) {
  if (i < 0 || i >= (int)studies_.size() || !st.docMirror.hasDoc())
    return;
  std::string id = studies_[i].id;
  if (studies_[i].type == StudyType::Motion) {
    const std::string docId = st.docMirror.docId();
    invalidateMotion(studies_[i]);
    st.rpc.call("sim_study_delete", {{"doc_id", docId}, {"study_id", id}},
                [this, &st, docId, id](const core::RpcResult &r) {
      if (!simDocOpen(st, docId))
        return;
      if (!r.ok || !r.result.is_object() || r.result.contains("code") ||
          !simJsonBool(r.result, "ok", true)) {
        st.addLog("[motion] delete failed: " +
                  simJsonText(r.result, "message", "error", r.err));
        if (simOriginActive(st, this, docId))
          st.toast(ui::ToastLevel::Error, "Could not delete Motion study; retry.");
        return;
      }
      const std::string selectedId = sel_ >= 0 && sel_ < static_cast<int>(studies_.size())
                                         ? studies_[sel_].id : std::string();
      studies_.erase(std::remove_if(studies_.begin(), studies_.end(),
                                    [&](const Study &study) { return study.id == id; }),
                     studies_.end());
      const auto selected = std::find_if(studies_.begin(), studies_.end(),
                                         [&](const Study &study) { return study.id == selectedId; });
      if (selected == studies_.end()) {
        sel_ = studies_.empty() ? -1 : 0;
        node_ = SimNode::Study;
        armedWell_ = -1;
      }
      else {
        sel_ = static_cast<int>(std::distance(studies_.begin(), selected));
      }
      markSimDocDirty(st, docId);
    });
    return;
  }
  if (st.rpc.interactionBusy()) {
    st.toast(ui::ToastLevel::Info, "Wait for the current engine request before deleting this study.");
    return;
  }
  nlohmann::json res;
  std::string err;
  if (!st.worker.call("sim_study_delete",
                      {{"doc_id", st.docMirror.docId()}, {"study_id", id}}, res,
                      err, 15000) ||
      !res.is_object() || res.contains("code") ||
      !res.value("ok", true)) {
    st.addLog("[sim] sim_study_delete failed: " +
              (res.is_object()
                   ? res.value("message", res.value("error", err))
                   : (err.empty() ? "invalid response" : err)));
    st.toast(ui::ToastLevel::Error, "Could not delete simulation study");
    return;
  }
  st.markActiveTabDirty();
  if (resultShown_) restoreModelMesh(st);
  else clearResultOverlay(st);
  armedWell_ = -1;
  refresh(st);
  if (sel_ >= (int)studies_.size())
    sel_ = studies_.empty() ? -1 : (int)studies_.size() - 1;
  node_ = SimNode::Study;
}

// ---------------------------------------------------------------- ribbon
// commands

bool SimManager::handleCommand(AppState &st, const std::string &cmdId) {
  if (cmdId == "sim_firmware_setup" || cmdId == "sim_board_thermal_setup") {
    st.activeWorkspace = AppState::Workspace::Simulate;
    st.leftPaneTab = AppState::LP_Simulation;
    st.revealSimManager = true;
    piping_.close(); hydraulic_.close();
    panelOpen_ = emPanelOpen_ = elPanelOpen_ = fwPanelOpen_ = handoffPanelOpen_ = false;
    armedWell_ = -1;
    if (cmdId == "sim_firmware_setup") openEmulationPanel(st);
    else {
      int pcb = -1;
      for (int i = 0; i < static_cast<int>(st.docTabs.size()); ++i)
        if (st.docTabs[i].kind == AppState::DocTab::TabKind::PCB &&
            !st.docTabs[i].ecadDocId.empty()) { pcb = i; break; }
      openHandoffPanel(st, pcb);
    }
    return true;
  }
  if (cmdId == "sim_circuit_op" || cmdId == "sim_circuit_dc" ||
      cmdId == "sim_circuit_ac" || cmdId == "sim_circuit_tran") {
    if (!elRunning_) {
      st.activeWorkspace = AppState::Workspace::Simulate;
      st.leftPaneTab = AppState::LP_Simulation;
      st.revealSimManager = true;
      piping_.close(); hydraulic_.close();
      panelOpen_ = emPanelOpen_ = fwPanelOpen_ = handoffPanelOpen_ = false;
      armedWell_ = -1;
      el_.analysis = cmdId == "sim_circuit_op" ? 0 : cmdId == "sim_circuit_dc" ? 1 :
                     cmdId == "sim_circuit_ac" ? 2 : 3;
      openElectronicsPanel(st);
    }
    return true;
  }
  if (cmdId == "sim_parameters") {
    if (sel_ < 0 || sel_ >= static_cast<int>(studies_.size())) return true;
    st.activeWorkspace = AppState::Workspace::Simulate;
    st.leftPaneTab = AppState::LP_Simulation;
    st.revealSimManager = true;
    piping_.close(); hydraulic_.close();
    emPanelOpen_ = elPanelOpen_ = fwPanelOpen_ = handoffPanelOpen_ = false;
    panelOpen_ = true;
    node_ = SimNode::Study;
    parameterEditorOpen_ = true;
    requestFocus("sim_parameters");
    return true;
  }
  // Piping tab: the Piping panel takes over the Simulation pane (its records are this doc's
  // piping_network / pipe_flow / vessel / duct_system studies).
  if (cmdId.rfind("sim_piping_", 0) == 0) {
    emPanelOpen_ = false;
    elPanelOpen_ = false;
    fwPanelOpen_ = false;
    handoffPanelOpen_ = false;
    panelOpen_ = false;
    armedWell_ = -1;
    hydraulic_.close();
    refresh(st);
    return piping_.handleCommand(st, cmdId);
  }
  // Hydraulics tab: the Hydraulics panel takes over the pane the same way (its records are this
  // doc's hydraulic_circuit studies).
  if (cmdId.rfind("sim_hyd_", 0) == 0) {
    emPanelOpen_ = false;
    elPanelOpen_ = false;
    fwPanelOpen_ = false;
    handoffPanelOpen_ = false;
    panelOpen_ = false;
    armedWell_ = -1;
    piping_.close();
    refresh(st);
    return hydraulic_.handleCommand(st, cmdId);
  }
  // Any other sim tool means the user left the Piping / Hydraulics panels.
  piping_.close();
  hydraulic_.close();
  // "New Study" on every study ribbon tab: the grouped picker in the Simulation pane. Whatever
  // panel holds the pane yields to the study list so the picker is visible.
  if (cmdId == "sim_new_picker") {
    st.activeWorkspace = AppState::Workspace::Simulate;
    st.leftPaneTab = AppState::LP_Simulation;
    st.revealSimManager = true;
    openNewStudyPicker();
    return true;
  }
  // EM (electromagnetics) tab: every sim_em_* command is gated on the em_*
  // backend probe.
  if (cmdId.rfind("sim_em_", 0) == 0)
    return handleEmCommand(st, cmdId);
  // Any non-EM sim tool means the user left the EM analysis — yield the pane
  // back to the study tree/panel. (The EM panel takes over drawTreeBody first,
  // so without this it stays glued over any study a subsequent study command
  // opens.)
  emPanelOpen_ = false;

  // sim-gui rework: creating a study opens its setup panel (PM-style, left
  // pane) immediately.
  auto newAndOpen = [&](StudyType t) {
    newStudy(st, t);
    if (sel_ >= 0) {
      node_ = SimNode::Study;
      panelOpen_ = true;
      // A fresh study opens on its guided setup; the advanced editor stays folded.
      parameterEditorOpen_ = false;
    }
    return true;
  };
  if (cmdId == "sim_new_static")
    return newAndOpen(StudyType::Static);
  if (cmdId == "sim_new_modal")
    return newAndOpen(StudyType::Modal);
  if (cmdId == "sim_new_flow")
    return newAndOpen(StudyType::Flow);
  if (cmdId == "sim_new_thermal")
    return newAndOpen(StudyType::Thermal);
  if (cmdId == "sim_new_fatigue")
    return newAndOpen(StudyType::Fatigue);
  if (cmdId == "sim_new_motion")
    return newAndOpen(StudyType::Motion);
  if (cmdId == "sim_new_thermal_fea")
    return newAndOpen(StudyType::ThermalFea);
  if (cmdId == "sim_new_buckling")
    return newAndOpen(StudyType::Buckling);
  if (cmdId == "sim_new_contact")
    return newAndOpen(StudyType::Contact);
  if (cmdId == "sim_new_freqresp")
    return newAndOpen(StudyType::FreqResponse);
  if (cmdId == "sim_new_topology") {
    st.activeWorkspace = AppState::Workspace::Simulate;
    st.leftPaneTab = AppState::LP_Simulation;
    st.showRibbon = true;
    st.ribbonTab = kRibbonTabTopology;
    if (topoCaps_.state == topology::Capabilities::State::Unavailable) {
      st.toast(ui::ToastLevel::Warn, topoCaps_.error);
      return true;
    }
    newAndOpen(StudyType::Topology); // before the probe: newStudy refuses while rpc is busy
    if (sel_ >= 0 && sel_ < (int)studies_.size() &&
        studies_[sel_].type == StudyType::Topology)
      syncTopologyViews(studies_[sel_]);
    probeTopologyCaps(st);
    return true;
  }
  // The one obvious way into Motion: Features > Assembly "Motion Study", the Simulation
  // tab's Study group and the assembly empty state all land here. It opens this assembly's
  // saved Motion study (an agent's simulate_motion sweep persists as one) on its playback
  // node in the Simulate workspace, where playback drives the viewport; only when the
  // document has none does it create one, exactly like "New Study" on the Motion tab.
  if (cmdId == "sim_motion_study") {
    if (!activeDocIsAssembly(st)) {
      st.toast(ui::ToastLevel::Warn,
               "Motion studies drive assembly mates. Open an assembly first.");
      return true;
    }
    st.activeWorkspace = AppState::Workspace::Simulate;
    st.leftPaneTab = AppState::LP_Simulation;
    st.revealSimManager = true;
    st.showRibbon = true;
    st.ribbonTab = kRibbonTabMotion;
    refresh(st);
    if (!selectStudyOfType(StudyType::Motion))
      return newAndOpen(StudyType::Motion);
    node_ = SimNode::Results;
    loadIdx_ = -1;
    armedWell_ = -1;
    panelOpen_ = true;
    return true;
  }
  refresh(st);

  // Navigate the study pane to a setup page with the command's card scrolled into view (UI
  // standard decision 5): the editor opens on the Setup step (never a stale Run page).
  auto openSetup = [&](SimNode n, const char *focus) {
    st.leftPaneTab = AppState::LP_Simulation;
    node_ = n;
    if (n == SimNode::Load && sel_ >= 0 && sel_ < (int)studies_.size()) {
      const int nl = (int)studies_[sel_].loads.size();
      loadIdx_ = loadIdx_ >= 0 && loadIdx_ < nl ? loadIdx_ : (nl > 0 ? 0 : -1);
    } else if (n != SimNode::Load) {
      loadIdx_ = -1;
    }
    runPage_ = RunPageView{};
    panelOpen_ = true;
    if (focus)
      requestFocus(focus);
  };
  auto selIs = [&](StudyType t) {
    return sel_ >= 0 && sel_ < (int)studies_.size() && studies_[sel_].type == t;
  };
  auto needStudy = [&](StudyType t) {
    st.leftPaneTab = AppState::LP_Simulation;
    st.toast(ui::ToastLevel::Warn, std::string("Create a ") + studyTypeLabel(t) +
                                       " study first (\"New Study\" on this tab).");
    return true;
  };

  // Per-type setup tools (the per-type ribbon tabs): target the selected study when its kind
  // matches, else the first study of the tab's kind, and open the page that holds the card.
  struct TypedCmd {
    const char *id;
    StudyType t;
    SimNode node;
    const char *focus;
  };
  static const TypedCmd kTypedSetup[] = {
      {"sim_modal_options", StudyType::Modal, SimNode::Study, "modes"},
      {"sim_buckling_options", StudyType::Buckling, SimNode::Study, "modes"},
      {"sim_contact_options", StudyType::Contact, SimNode::Study, "contact"},
      {"sim_freqresp_options", StudyType::FreqResponse, SimNode::Study, "excitation"},
      {"sim_thermal_fea_options", StudyType::ThermalFea, SimNode::Study, "conduction"},
      {"sim_fatigue_setup", StudyType::Fatigue, SimNode::Study, "strength"},
      {"sim_motion_setup", StudyType::Motion, SimNode::Study, "drivers"},
      // Flow-tab tools also serve a selected Thermal CFD study (it shares the flow pages).
      {"sim_fluid", StudyType::Flow, SimNode::Material, "fluid"},
      {"sim_flow_bc", StudyType::Flow, SimNode::Boundaries, "flow"},
      {"sim_flow_res", StudyType::Flow, SimNode::Mesh, "resolution"},
  };
  for (const auto &tc : kTypedSetup) {
    if (cmdId != tc.id)
      continue;
    const bool flowFamilySel = isFlowFamily(tc.t) && selIs(StudyType::Thermal);
    if (!flowFamilySel && !selectStudyOfType(tc.t))
      return needStudy(tc.t);
    armedWell_ = -1;
    openSetup(tc.node, tc.focus);
    return true;
  }
  // "Heat Loads": a Thermal FEA study's heat-load page, else a Thermal CFD study's heat-transfer
  // boundary card.
  if (cmdId == "sim_thermal_bc") {
    armedWell_ = -1;
    if (selIs(StudyType::ThermalFea) ||
        (!selIs(StudyType::Thermal) && selectStudyOfType(StudyType::ThermalFea))) {
      openSetup(SimNode::Load, "load");
      return true;
    }
    if (selectStudyOfType(StudyType::Thermal)) {
      openSetup(SimNode::Boundaries, "heat");
      return true;
    }
    return needStudy(StudyType::ThermalFea);
  }
  // ---- Topology tab tools ----------------------------------------------------------------
  const bool topoCmd = cmdId.rfind("sim_topology_", 0) == 0;
  if (topoCmd) {
    st.leftPaneTab = AppState::LP_Simulation;
    if (!selectStudyOfType(StudyType::Topology))
      return needStudy(StudyType::Topology);
    Study &ts = studies_[sel_];
    syncTopologyViews(ts);
    probeTopologyCaps(st);
    loadIdx_ = -1;
    runPage_ = RunPageView{};
    topoResultsView_.compareMode = false;
    panelOpen_ = true;
    if (cmdId == "sim_topology_setup") {
      node_ = SimNode::Study;
      armedWell_ = -1;
      return true;
    }
    if (cmdId == "sim_topology_boundaries") {
      node_ = SimNode::Boundaries;
      armedWell_ = topoBcView_.armedPointer.empty() ? -1 : kArmTopology;
      topoBcView_.focusPointer = topoBcView_.armedPointer;
      return true;
    }
    if (cmdId == "sim_topology_manufacturing") {
      node_ = SimNode::Regions;
      topoSettingsView_.focusManufacturing = true;
      topoBcView_.focusPointer.clear();
      topoBcView_.armedPointer.clear();
      armedWell_ = -1;
      return true;
    }
    if (cmdId == "sim_topology_compare") {
      node_ = SimNode::Results;
      topoResultsView_.focusCompare = false;
      topoResultsView_.compareMode = true; // < 2 runs: the compare view says so itself
      armedWell_ = -1;
      const nlohmann::json runs = ts.topo.value("runs", nlohmann::json::array());
      if (!runs.is_array() || runs.size() < 2)
        return true;
      auto runId = [&](std::size_t i) {
        const auto &r = runs[i];
        return r.is_object() && r.contains("run_id") && r["run_id"].is_string()
                   ? r["run_id"].get<std::string>()
                   : "#" + std::to_string(i);
      };
      topoResultsView_.selectedRunId = runId(runs.size() - 1);
      topoResultsView_.baselineRunId = runId(runs.size() - 2);
      topoResultsView_.focusCompare = true;
      return true;
    }
    if (cmdId == "sim_topology_preserve" || cmdId == "sim_topology_exclude") {
      if (topologyRunActive()) {
        st.toast(ui::ToastLevel::Info, "Wait for the topology run before editing regions.");
        return true;
      }
      const bool preserve = cmdId == "sim_topology_preserve";
      const std::string ptr = topology::addEntry(
          ts.topo, preserve ? topology::EntryKind::PreserveFaces
                            : topology::EntryKind::ExcludeFaces);
      std::vector<nlohmann::json> picked;
      SimWell scratch;
      captureSelectionInto(st, scratch);
      for (const auto &p : scratch.picks)
        picked.push_back(p.selector);
      topology::appendFaces(ts.topo, ptr, picked);
      persist(st, ts);
      st.selection.clear();
      node_ = SimNode::Regions;
      topoBcView_.armedPointer = ptr;
      topoBcView_.focusPointer = ptr;
      armedWell_ = kArmTopology;
      return true;
    }
    return false;
  }
  // Thermal tab "Heat Load": a heat-flux load on the selected Thermal FEA study, seeded from any
  // pre-selected faces (same capture/arm flow as sim_load_force).
  if (cmdId == "sim_load_heat") {
    if (!selectStudyOfType(StudyType::ThermalFea))
      return needStudy(StudyType::ThermalFea);
    Study &hs = studies_[sel_];
    SimLoad hl;
    hl.thermalKind = 0; // heat flux (default fluxWm2)
    hs.loads.push_back(hl);
    captureSelectionInto(st, hs.loads.back().faces);
    persist(st, hs);
    st.selection.clear();
    loadIdx_ = (int)hs.loads.size() - 1;
    openSetup(SimNode::Load, "load");
    armedWell_ = (int)hs.loads.size();
    return true;
  }
  const bool haveStudy = sel_ >= 0 && sel_ < (int)studies_.size();
  if (!haveStudy) {
    st.leftPaneTab = AppState::LP_Simulation;
    st.toast(ui::ToastLevel::Warn, "Create or select a study first (\"New Study\").");
    return true;
  }
  Study &s = studies_[sel_];
  const bool cfd = isFlowFamily(s.type);
  // Material / Results / Mesh: open the pane on the matching page of the active study. Mesh
  // also MESHES a solid study and shows the element wireframe (a flow study's grid is its
  // Resolution page; it has no FEA mesh to preview).
  if (cmdId == "sim_material" || cmdId == "sim_results" || cmdId == "sim_mesh") {
    armedWell_ = -1;
    if (s.type == StudyType::Motion && cmdId != "sim_results") {
      st.toast(ui::ToastLevel::Info,
               "Motion studies move rigid bodies: no material or mesh. Set the drivers.");
      openSetup(SimNode::Study, "drivers");
      return true;
    }
    if (cmdId == "sim_material") {
      openSetup(SimNode::Material, cfd ? "fluid" : "material");
    } else if (cmdId == "sim_results") {
      topoResultsView_.compareMode = false;
      openSetup(SimNode::Results, nullptr);
    } else {
      openSetup(SimNode::Mesh, cfd ? "resolution" : "mesh");
      const bool busy = st.simClient && st.simClient->busy();
      if (!cfd && !busy)
        previewMesh(st, s); // mesh now + show the wireframe (no solve)
    }
    return true;
  }
  const bool boundaryCmd = cmdId == "sim_fixture" || cmdId == "sim_load_force" ||
                           cmdId == "sim_load_pressure";
  if (s.type == StudyType::Topology && boundaryCmd) {
    st.leftPaneTab = AppState::LP_Simulation;
    syncTopologyViews(s);
    runPage_ = RunPageView{};
    if (topologyRunActive()) {
      st.toast(ui::ToastLevel::Info, "Wait for the topology run before editing its setup.");
      return true;
    }
    const std::string physics = topology::studyPhysics(s.topo);
    std::string ptr;
    if (cmdId == "sim_fixture") {
      ptr = topology::addEntry(s.topo, topology::fixtureKindFor(physics));
    } else {
      if (!topology::usesStructuralLoads(physics)) {
        st.toast(ui::ToastLevel::Warn,
                 physics.empty()
                     ? "Choose the topology physics first (Topology Settings)."
                     : topology::physicsLabel(physics) +
                           " takes no force/pressure loads; set its heat or flow "
                           "boundaries under Boundaries.");
        node_ = physics.empty() ? SimNode::Study : SimNode::Boundaries;
        panelOpen_ = true;
        return true;
      }
      // With load cases, a new load lands in the case selected in the editor (else case 1).
      int loadCase = -1;
      const nlohmann::json &cases = s.topo.contains("load_cases") ? s.topo["load_cases"]
                                                                  : nlohmann::json();
      if (cases.is_array() && !cases.empty())
        loadCase = topoBcView_.loadCase >= 0 && topoBcView_.loadCase < (int)cases.size()
                       ? topoBcView_.loadCase
                       : 0;
      ptr = topology::addEntry(s.topo,
                               cmdId == "sim_load_force" ? topology::EntryKind::ForceLoad
                                                         : topology::EntryKind::PressureLoad,
                               loadCase);
    }
    SimWell scratch;
    captureSelectionInto(st, scratch);
    std::vector<nlohmann::json> picked;
    for (const auto &p : scratch.picks)
      picked.push_back(p.selector);
    topology::appendFaces(s.topo, ptr, picked);
    persist(st, s);
    st.selection.clear();
    node_ = SimNode::Boundaries;
    topoBcView_.armedPointer = ptr;
    topoBcView_.focusPointer = ptr;
    armedWell_ = ptr.empty() ? -1 : kArmTopology;
    panelOpen_ = true;
    return true;
  }
  // Kinds without face fixtures / structural loads: never append setup the run would ignore;
  // say why and open the page that does apply.
  if (boundaryCmd) {
    const bool loadCmd = cmdId != "sim_fixture";
    const char *why = nullptr;
    SimNode n = SimNode::Study;
    const char *focus = nullptr;
    if (cfd) {
      why = "Flow studies take no fixtures or loads; set the flow boundaries instead.";
      n = SimNode::Boundaries;
      focus = s.type == StudyType::Thermal ? "heat" : "flow";
    } else if (s.type == StudyType::Motion) {
      why = "Motion studies drive assembly mates; set the drivers instead.";
      focus = "drivers";
    } else if (s.type == StudyType::Contact) {
      why = "The contact study runs on a two-block proxy; set its contact pressure.";
      focus = "contact";
    } else if (loadCmd && (s.type == StudyType::Modal || s.type == StudyType::FreqResponse)) {
      // Eigen solves ignore loads (runStaticOrModal never sends modal loads).
      why = "Modal studies don't use loads; add fixtures to constrain rigid-body modes.";
      n = SimNode::Fixtures;
      focus = "fixtures";
    } else if (loadCmd && s.type == StudyType::ThermalFea) {
      why = "Thermal FEA takes heat loads (flux or convection); use Heat Load.";
      n = SimNode::Load;
      focus = "load";
    }
    if (why) {
      st.toast(ui::ToastLevel::Warn, why);
      armedWell_ = -1;
      openSetup(n, focus);
      return true;
    }
  }
  // captureSelectionInto adopts any GENUINE pre-selection (faces the user
  // picked before clicking the command) into the new well, then we clear
  // st.selection. The carryover bug came from sim face picks LINGERING in
  // st.selection (the pick observer never removes them) so the next capture
  // scooped the previous well's faces — app.cpp now clears st.selection right
  // after each routed sim pick (see the routeFacePick consumer), so by the time
  // a new command captures, only true pre-selection remains. Arming a well also
  // clears (drawWellRow / tree / +buttons).
  if (cmdId == "sim_fixture") {
    captureSelectionInto(st, s.fixtures);
    persist(st, s);
    st.selection.clear();
    openSetup(SimNode::Fixtures, "fixtures");
    armedWell_ = 0;
    return true;
  }
  if (cmdId == "sim_load_force") {
    // Unit-native default magnitude: US customary -> 50 lbf (=222.411 N);
    // metric -> 100 N. Effective US = forced US (simUnits==2) or doc-follow
    // (simUnits==0) while the doc is IPS.
    const bool usDisp = st.simUnits == 2 || (st.simUnits == 0 && st.ips());
    SimLoad lf;
    if (usDisp)
      lf.vec[2] = -222.411f;
    s.loads.push_back(lf);
    captureSelectionInto(st, s.loads.back().faces);
    persist(st, s);
    st.selection.clear();
    loadIdx_ = (int)s.loads.size() - 1;
    openSetup(SimNode::Load, "load");
    armedWell_ = (int)s.loads.size();
    return true;
  }
  if (cmdId == "sim_load_pressure") {
    const bool usDisp = st.simUnits == 2 || (st.simUnits == 0 && st.ips());
    SimLoad l;
    l.pressure = true;
    if (usDisp)
      l.pressureMpa = 0.689476f; // 100 psi; metric default stays 0.1 MPa
    s.loads.push_back(l);
    captureSelectionInto(st, s.loads.back().faces);
    persist(st, s);
    st.selection.clear();
    loadIdx_ = (int)s.loads.size() - 1;
    openSetup(SimNode::Load, "load");
    armedWell_ = (int)s.loads.size();
    return true;
  }
  if (cmdId == "sim_run") {
    st.leftPaneTab = AppState::LP_Simulation;
    panelOpen_ = true;
    run(st, s);
    // The Run page shows the live progress, or the blocker inline when run() refused.
    runPage_ = RunPageView{s.id, node_, false};
    return true;
  }
  return false;
}

void SimManager::requestFocus(const char *cardId) {
  focusCard_ = cardId ? cardId : "";
  focusFrame_ = ImGui::GetFrameCount();
}

bool SimManager::takeFocus(const char *cardId) {
  // Grace period: the editor may open, and its page lay out, a few frames after the command.
  constexpr int kFocusFrames = 30;
  if (focusCard_.empty() || !cardId)
    return false;
  if (ImGui::GetFrameCount() - focusFrame_ > kFocusFrames) {
    focusCard_.clear();
    return false;
  }
  if (focusCard_ != cardId)
    return false;
  focusCard_.clear();
  ImGui::SetScrollHereY(0.0f);
  return true;
}

void SimManager::syncPaneContext(AppState &st) {
  const bool takeover = emPanelOpen_ || elPanelOpen_ || fwPanelOpen_ || handoffPanelOpen_ ||
                        piping_.isOpen() || hydraulic_.isOpen();
  const bool haveSel = sel_ >= 0 && sel_ < (int)studies_.size();
  const bool simWs = st.activeWorkspace == AppState::Workspace::Simulate;
  // The ribbon greys the study commands that do not apply to the selected kind.
  setRibbonStudyContext(haveSel && !takeover ? studyTypeKey(studies_[sel_].type) : "");
  // The Results page shows the result (or its empty state), never a setup-time mesh wireframe.
  if (node_ == SimNode::Results && !meshPreviewPending_ && !meshPreviewStudyId_.empty())
    clearMeshPreview();

  // Attribute a status-bar change to this manager while one of its runs is (or just was)
  // active: progress lines, completions, failures and cancellations.
  const SimRunUi live = runUi();
  if (st.status != lastStatusSeen_) {
    if (live.running || statusRunActive_) {
      simStatusText_ = st.status;
      simStatusStudyId_ =
          live.running && !live.studyId.empty() ? live.studyId : lastRunStudyId_;
    }
    lastStatusSeen_ = st.status;
  }
  statusRunActive_ = live.running;
  if (live.running && !live.studyId.empty() && live.studyId != lastRunStudyId_)
    lastRunStudyId_ = live.studyId;

  // Scope change (another study, the editor closed, another panel or workspace took the pane):
  // a finished run's line no longer describes what the pane shows.
  static const std::string kNone;
  const std::string &selId = haveSel ? studies_[sel_].id : kNone;
  const int flags = (panelOpen_ ? 1 : 0) | (takeover ? 2 : 0) | (simWs ? 4 : 0);
  if (flags == scopeFlags_ && selId == scopeStudyId_ && lastDocId_ == scopeDocId_)
    return;
  scopeFlags_ = flags;
  scopeStudyId_ = selId;
  scopeDocId_ = lastDocId_;
  if (live.running || simStatusText_.empty() || st.status != simStatusText_)
    return;
  if (selId == simStatusStudyId_ && panelOpen_ && !takeover && simWs)
    return; // the study the line belongs to is still the one on screen
  st.status = "Ready.";
  st.statusKind = 0;
  lastStatusSeen_ = st.status;
  simStatusText_.clear();
}

// sim-gui rework: prefer the current selection when it already matches the
// requested type, else the first study of that type (studies_ mirror order).
// Returns false when none exists.
bool SimManager::selectStudyOfType(StudyType t) {
  if (sel_ >= 0 && sel_ < (int)studies_.size() && studies_[sel_].type == t)
    return true;
  for (int i = 0; i < (int)studies_.size(); ++i) {
    if (studies_[i].type == t) {
      sel_ = i;
      return true;
    }
  }
  return false;
}

void SimManager::configureActive(AppState &st, float density,
                                 const std::string &materialKey) {
  if (sel_ < 0 || sel_ >= (int)studies_.size())
    return;
  if (!materialKey.empty() && st.docLockedByAgent(st.docMirror.docId())) {
    st.toast(ui::ToastLevel::Warn, st.agentLockReason(st.docMirror.docId()));
    return;
  }
  studies_[sel_].meshFineness =
      density; // density and fineness share the higher=finer sense
  if (!materialKey.empty() && !applyPartMaterial(st, studies_[sel_], materialKey))
    return;
  persist(st, studies_[sel_]);
}

bool SimManager::selectStudyById(AppState &st, const std::string &id) {
  for (int i = 0; i < (int)studies_.size(); ++i)
    if (studies_[i].id == id) {
      sel_ = i;
      node_ = SimNode::Study;
      loadIdx_ = -1;
      armedWell_ = -1;
      st.leftPaneTab = AppState::LP_Simulation;
      if (studies_[i].type == StudyType::Topology)
        syncTopologyViews(studies_[i]);
      return true;
    }
  return false;
}

std::string SimManager::activeStudyId() const {
  return sel_ >= 0 && sel_ < (int)studies_.size() ? studies_[sel_].id : std::string();
}

// ============================================================ T-FIN24:
// multiphysics Simulate tab
//
// One Simulate workspace for every study kind. The pieces below add: the
// sim_caps / fw_status capability probes (honest gating), the four new
// study-kind runs (thermal FEA and linear buckling on the core mesh solvers;
// CalculiX contact proxy + modal-superposition frequency response via the
// worker's multiphysics seam), the stateless electronics-SPICE and firmware-
// emulation panels, and the cross-sim handoff panel (aicad.simhandoff/1: ECAD
// board -> thermal FEA + the T-FIN17 heatsink-pick forward seam). All async
// work goes through st.rpc (callbacks drain on the GL thread) or the SimClient
// job — never a sync worker.call in a draw path.

void SimManager::probeCaps(AppState &st) {
  if (capsStatus_ != 0 || capsInflight_)
    return;
  capsInflight_ = true;
  st.rpc.call(
      "sim_caps", nlohmann::json::object(),
      [this](const core::RpcResult &r) {
        capsInflight_ = false;
        if (r.ok && r.result.is_object() && !r.result.contains("code") &&
            r.result.value("ok", true)) {
          caps_ = r.result;
          capsStatus_ = 1;
        } else {
          caps_.clear();
          capsStatus_ = -1;
        }
      },
      8000);
}

void SimManager::probeFwStatus(AppState &st) {
#ifdef __EMSCRIPTEN__
  // fw_status / fw_projects are Python-worker-only; mark unavailable quietly on
  // web.
  (void)st;
  if (fwStatus_ == 0) {
    fwStatus_ = -1;
    fwCanEmulate_ = false;
    fwHint_ = "Firmware emulation is unavailable in the web client";
    fwsim_.projectsLoaded = true;
  }
  return;
#endif
  if (fwStatus_ != 0 || fwProbeInflight_)
    return;
  fwProbeInflight_ = true;
  st.rpc.call(
      "fw_status", nlohmann::json::object(),
      [this](const core::RpcResult &r) {
        fwProbeInflight_ = false;
        if (!r.ok || !r.result.is_object() || r.result.contains("code") ||
            !r.result.value("ok", true)) {
          fwStatus_ = -1;
          fwCanEmulate_ = false;
          fwHint_ = r.result.is_object()
                        ? r.result.value(
                              "error",
                              r.result.value(
                                  "message",
                                  r.err.empty() ? "fw_status unavailable"
                                                : r.err))
                        : (r.err.empty() ? "fw_status unavailable" : r.err);
          return;
        }
        fwStatus_ = 1;
        if (r.result.contains("families") && r.result["families"].is_object()) {
          const auto fam =
              r.result["families"].value("cortex-m", nlohmann::json::object());
          fwCanEmulate_ = fam.value("can_emulate", false);
        }
        if (!fwCanEmulate_)
          fwHint_ = "Emulation unavailable: install Renode (preferred) or "
                    "qemu-system-arm.";
      },
      8000);
  // Project list for the emulation panel + picker disclosure
  // (firmware-project-only case).
  if (!fwsim_.projectsLoaded && !fwsim_.projectsPending) {
    fwsim_.projectsPending = true;
    st.rpc.call(
        "fw_projects", nlohmann::json::object(),
        [this](const core::RpcResult &r) {
          fwsim_.projectsPending = false;
          fwsim_.projectsLoaded = true;
          fwsim_.projectIds.clear();
          fwsim_.projectNames.clear();
          if (r.ok && r.result.is_object() && !r.result.contains("code") &&
              r.result.value("ok", true) &&
              r.result.contains("projects") &&
              r.result["projects"].is_array()) {
            for (const auto &pr : r.result["projects"]) {
              fwsim_.projectIds.push_back(pr.value("id", std::string()));
              std::string nm = pr.value("name", pr.value("id", std::string()));
              const std::string mcu = pr.value("mcu", std::string());
              if (!mcu.empty())
                nm += " (" + mcu + ")";
              fwsim_.projectNames.push_back(std::move(nm));
            }
          }
        },
        8000);
  }
}

bool SimManager::modelBbox(AppState &st, float lo[3], float hi[3]) const {
  lo[0] = lo[1] = lo[2] = 1e30f;
  hi[0] = hi[1] = hi[2] = -1e30f;
  bool any = false;
  for (const auto &kv : st.faceNormalCache) {
    for (int k = 0; k < 3; ++k) {
      lo[k] = std::min(lo[k], kv.second[3 + k]);
      hi[k] = std::max(hi[k], kv.second[3 + k]);
    }
    any = true;
  }
  return any;
}

} // namespace ui
