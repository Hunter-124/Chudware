// Simulation Manager internals: helpers, constants and private nested types shared by the SimManager TUs.
//
// Everything here was file-local to the single sim_manager.cpp; the helpers live in ui::simmgr and
// the using-directive below gives every SimManager TU the same unqualified lookup the old
// anonymous namespace / file statics had. Helpers used by ONE TU stay file-local in that TU.
#pragma once

#include <algorithm>
#include <cctype>
#include <cmath>
#include <cstddef>
#include <cstring>
#include <cstdint>
#include <map>
#include <string>
#include <unordered_set>
#include <utility>
#include <vector>

#include "imgui.h"

#include "../../core/app_state.h"
#include "../../ui_theme.h"
#include "../appearance_common.h" // materialAppearance / kPhysicalMaterials — set_material recolour
#include "../icons.h"
#include "../metrics.h"
#include "../sim_manager.h"
#include "../widgets/kit.h" // kit::dimText — the app-wide dim/secondary prose renderer

namespace ui {

namespace simmgr {}
using namespace simmgr;

namespace simmgr {

inline bool activeDocIsAssembly(const AppState &st) {
  return st.activeTab >= 0 && st.activeTab < (int)st.docTabs.size() &&
         st.docTabs[st.activeTab].kind == AppState::DocTab::TabKind::Assembly;
}

// ---- liquid-glass v2: shared FeatureManager-column surface (host contract
// §H4) ---------------- The Simulation tree body is drawn EMBEDDED inside
// drawLeftPane's tab child (layout.cpp), which already painted ONE glass
// surface for the whole pane under panelId("host_left_glass"). Mirror
// feature_tree.cpp's fmSurf(): fetch that SAME surface (same id + group, every
// frame — the adaptivePanel cache contract) for adaptive, backdrop-tracking
// text + row pills. The rect passed here is moot (layout.cpp paints the id
// first this frame) but supplied for the cache signature.
inline const ui::AdaptiveSurface &simSurf() {
  ImVec2 wp = ImGui::GetWindowPos(), ws = ImGui::GetWindowSize();
  ImRect r(wp, ImVec2(wp.x + ws.x, wp.y + ws.y));
  return ui::adaptivePanel(ui::panelId("host_left_glass"), r,
                           ui::GlassGroup::FeatureManager);
}

// ---- card-surface text pairing ----------------------------------------------------------------
// In-pane content uses the pane's own adaptive ink (simSurf().textPrimary). Only scopes on the
// THEME glass surface (floating popups / the custom-material modal, which do not inherit the
// pane's frosted backdrop) pair against palette.glassBg via glassCardText() / pushCardText().
inline ImVec4 glassCardText() {
  // The popup/modal consumers fill with the OPAQUE theme glassBg
  // (ImGuiCol_PopupBg, ~0.87a dark) regardless of the viewport backdrop — they
  // are NOT the frosted column — so text must always be contrasted against
  // glassBg (a light ink). Do NOT substitute the pane's adaptive (dark) ink
  // here or the popup text goes dark-on-dark = invisible over a light model.
  const float *g = ui::palette.glassBg;
  return ui::maxContrastTextOn(ImVec4(g[0], g[1], g[2], 1.0f));
}
inline void pushCardText() {
  ImVec4 t = glassCardText();
  ImGui::PushStyleColor(ImGuiCol_Text, t);
  t.w = 0.62f;
  ImGui::PushStyleColor(ImGuiCol_TextDisabled, t);
}
inline void popCardText() { ImGui::PopStyleColor(2); }

// Thermal is a flow-family study (it runs the same OpenFOAM convection/CHT
// pipeline as Flow, just always with heat transfer on) — these share STL
// export, the run path, and result rendering.
inline bool isFlowFamily(StudyType t) {
  return t == StudyType::Flow || t == StudyType::Thermal;
}

inline bool supportsLearnedAdvisor(StudyType t) {
  return t == StudyType::Static || t == StudyType::Modal ||
         t == StudyType::ThermalFea || t == StudyType::Buckling;
}
inline const char *studyTypeLabel(StudyType t) {
  switch (t) {
  case StudyType::Modal:
    return "Modal";
  case StudyType::Flow:
    return "Flow";
  case StudyType::Fatigue:
    return "Fatigue";
  case StudyType::Thermal:
    return "Thermal CFD";
  case StudyType::Motion:
    return "Motion";
  case StudyType::ThermalFea:
    return "Thermal FEA";
  case StudyType::Buckling:
    return "Buckling";
  case StudyType::Contact:
    return "Contact";
  case StudyType::FreqResponse:
    return "Vibration";
  case StudyType::Topology:
    return "Topology";
  default:
    return "Static";
  }
}

// Study-type icon name for the atlas (study_* per the icon contract).
// Referencing by string is compile-decoupled; callers guard on IconUV::valid so
// a missing atlas entry degrades to no icon.
inline const char *studyTypeIcon(StudyType t) {
  switch (t) {
  case StudyType::Modal:
    return "study_modal";
  case StudyType::Flow:
    return "study_flow";
  case StudyType::Fatigue:
    return "study_fatigue";
  case StudyType::Thermal:
    return "study_thermal_cfd";
  case StudyType::Motion:
    return "rotate_component"; // no dedicated study_motion sprite; the
                               // rotation-gizmo glyph reads as motion and
                               // is a real atlas icon (picker + study tree)
  case StudyType::ThermalFea:
    return "study_thermal_fea";
  case StudyType::Buckling:
    return "study_buckling";
  case StudyType::Contact:
    return "study_contact";
  case StudyType::FreqResponse:
    return "study_vibration";
  case StudyType::Topology:
    return "study_topology";
  default:
    return "study_static";
  }
}

// THE simulation pane header (standard §2): [icon] elided title (record name only — never
// "Name - Section") [dim type chip] [status chip] ... [Done]. Every takeover pane (study
// editors, EM, Electronics, Firmware, Handoff) calls this inside its kit::panelBegin scope.
// Returns true on the frame "Done" is clicked. `actions` adds header icon buttons left of Done
// (*actionPressed receives the clicked index, else -1).
inline bool simPanelHeader(AppState &st, const char *icon, const char *title,
                           const char *typeChip, const char *statusChip = nullptr,
                           kit::Tone statusTone = kit::Tone::Neutral,
                           const char *doneTip = nullptr,
                           const kit::HeaderAction *actions = nullptr,
                           int actionCount = 0, int *actionPressed = nullptr,
                           const char *statusTip = nullptr) {
  kit::PanelHeaderSpec h;
  h.icon = icon;
  h.title = title;
  // Default study names are "<Type> N": a type chip repeating the title's leading words adds
  // nothing and only costs the title its width.
  const bool chipRepeatsTitle = typeChip && *typeChip && title &&
                                std::strncmp(title, typeChip, std::strlen(typeChip)) == 0;
  h.typeChip = chipRepeatsTitle ? nullptr : typeChip;
  h.statusChip = statusChip;
  h.statusTone = statusTone;
  h.statusTip = statusTip;
  h.doneTip = doneTip && *doneTip
                  ? doneTip
                  : "Close this panel and return to the study list. Edits are saved as you "
                    "make them.";
  h.actions = actions;
  h.actionCount = actionCount;
  h.actionPressed = actionPressed;
  h.titleFont = st.fonts.medium;
  return kit::panelHeader("sim_hdr", h);
}

inline void addMeshParams(nlohmann::json &j, const Study &s, int meshOrder,
                          bool allowAdvisor = false) {
  j["fineness"] = s.meshFineness;
  j["mesh_order"] = meshOrder;
  j["order"] = meshOrder;
  if (s.meshTargetMm > 0.0f)
    j["target_size_mm"] = s.meshTargetMm;
  if (s.meshMinMm > 0.0f)
    j["min_size_mm"] = s.meshMinMm;
  if (allowAdvisor) {
    j["learned_advisor"] = s.learnedAdvisor;
    j["advisor_max_dof"] =
        static_cast<std::uint32_t>(std::max(0, s.advisorMaxDof));
  }
  if (s.adaptEnabled)
    j["adapt"] = {{"max_passes", std::clamp(s.adaptMaxPasses, 1, 6)},
                  {"eta_target", s.adaptEtaTarget}};
}

inline void clearTransientResult(Study &s) {
  s.hasResult = false;
  s.summary.clear();
  s.banner.clear();
  s.resultSummary = nlohmann::json::object();
  s.advResult = nlohmann::json::object();
  s.overlayPath.clear();
  s.freqs.clear();
  s.frSweep.clear();
  s.frAccel.clear();
  s.dragTrace.clear();
  s.dragTraceTotal = 0;
  s.dragTraceStride = 1;
  s.advisor = SimAdvisor{};
  s.dragN = s.liftN = s.sideN = 0.0;
  s.cd = s.cl = s.ld = s.reynolds = 0.0;
  s.frontalMm2 = s.refAreaMm2 = 0.0;
  s.cpMin = s.cpMax = s.vmaxMps = 0.0;
  s.flowFields.clear();
  s.solverNotes.clear();
  s.nNodes = s.nElems = s.nDof = 0;
  s.adaptPasses.clear();
  s.adaptGlobalEta = 0.0;
  s.adaptEtaTargetMet = false;
  s.adaptStopReason.clear();
  // Motion study transient results.
  s.motionHasResult = false;
  s.motionPlaying = false;
  s.motionFrame = 0;
  s.motionLastTick = -1.0;
  s.motionTrajectory = nlohmann::json::array();
  s.motionBodyIds.clear();
  s.motionBodyLabels.clear();
  s.motionDriverUnit.clear();
  s.motionRenderError.clear();
  s.motionRunning = false;
  s.motionInterference = nlohmann::json();
  s.motionResultDrivers = nlohmann::json();
  s.motionCollisionFrames.clear();
}

inline bool finiteNumber(const nlohmann::json &j, const char *key, double &out) {
  auto it = j.find(key);
  if (it == j.end() || !it->is_number())
    return false;
  out = it->get<double>();
  return std::isfinite(out);
}

// Group digits so six- and seven-figure counts read at a glance in a narrow pane.
inline std::string groupedCount(uint64_t n) {
  const std::string d = std::to_string(n);
  std::string out;
  out.reserve(d.size() + d.size() / 3);
  for (size_t i = 0; i < d.size(); ++i) {
    const size_t fromRight = d.size() - i;
    if (i && (fromRight % 3) == 0)
      out.push_back(',');
    out.push_back(d[i]);
  }
  return out;
}

// Uniform keep-every-Nth stride that brings `count` items under `budget`. Purely arithmetic, so
// the same input always yields the same stride: ceil(count / stride) <= budget for every count,
// and stride == 1 whenever the input already fits (no needless decimation of small studies).
inline size_t budgetStrideFor(size_t count, size_t budget) {
  if (count == 0 || budget == 0 || count <= budget)
    return 1;
  return (count + budget - 1) / budget;
}

// Same solid-content policy as the FeatureManager's require-non-empty guard: a loaded
// viewport body wins, otherwise assemblies or non-reference features count. This lets a
// free-free modal study reject an empty/sketch-only document before paying for an export.
inline bool hasSimulationGeometry(const AppState &st) {
  if (!st.docBodyIds.empty())
    return true;
  const auto &tree = st.docMirror.tree();
  if (!tree.is_object())
    return false;
  auto ci = tree.find("components");
  if (ci != tree.end() && ci->is_array() && !ci->empty())
    return true;
  auto fi = tree.find("features");
  if (fi == tree.end() || !fi->is_array())
    return false;
  for (const auto &f : *fi) {
    if (!f.is_object() || f.value("suppressed", false))
      continue;
    const std::string type = f.value("type", std::string());
    if (type.empty() || type == "plane" || type == "datum_plane" ||
        type == "ref_plane" || type == "axis" || type == "datum_axis" ||
        type == "ref_axis" || type == "point" || type == "datum_point" ||
        type == "ref_point" || type == "csys" ||
        type == "coordinate_system" || type == "ref_csys" ||
        type == "sketch" || type == "sketch3d")
      continue;
    return true;
  }
  return false;
}

// Read the active part's material key from DocMirror (doc["material"]["key"]).
// Empty when unset.
inline std::string docMaterialKey(const AppState &st) {
  if (!st.docMirror.hasDoc())
    return {};
  const nlohmann::json &tr = st.docMirror.tree();
  if (!tr.is_object() || !tr.contains("material") ||
      !tr["material"].is_object())
    return {};
  return tr["material"].value("key", std::string());
}

// Re-seed s.materialKey from the part's active material (doc.material.key).
// Empty when the part has no material so canRun still requires an explicit pick
// (which then set_material's the part). Call before run / material UI so the
// study tracks the one shared part material.
inline void resolveStudyMaterialFromDoc(AppState &st, Study &s) {
  if (isFlowFamily(s.type) || s.type == StudyType::Topology ||
      (s.definition.is_object() && s.definition.contains("material") &&
       s.definition["material"].is_object()))
    return; // An explicitly authored native material is study-owned.
  s.materialKey = docMaterialKey(st);
}

// Material and appearance always use the same document ops, whether a normal UI pick is
// queued or canonical Apply waits for acceptance before committing its local study mirror.
inline nlohmann::json partMaterialOps(const AppState &st, const std::string &key) {
  std::vector<uint32_t> bodies = st.docBodyIds;
  if (bodies.empty())
    bodies.push_back(1);
  float col[3];
  float mtl = 0.0f, rgh = 0.5f;
  materialAppearance(key, col, mtl, rgh);
  nlohmann::json ops = nlohmann::json::array();
  ops.push_back({{"op", "set_material"}, {"key", key}});
  for (uint32_t bid : bodies)
    ops.push_back({{"op", "set_appearance"},
                   {"id", "b" + std::to_string(bid)},
                   {"color", {col[0], col[1], col[2]}},
                   {"metallic", mtl},
                   {"roughness", rgh}});
  return ops;
}

inline bool applyPartMaterialNow(AppState &st, Study &s, const std::string &key,
                                 std::string &error) {
  if (!st.docMirror.hasDoc() || !st.workerStarted) {
    error = "Cannot update the part material without an open engine document.";
    return false;
  }
  if (st.docLockedByAgent(st.docMirror.docId())) {
    error = st.agentLockReason(st.docMirror.docId());
    st.toast(ui::ToastLevel::Warn, error);
    return false;
  }
  const auto response = st.docMirror.applyOps(st.worker, partMaterialOps(st, key));
  if (!response.ok) {
    error = "Could not update the part material: " +
            (response.error_message.empty() ? response.error_code : response.error_message);
    return false;
  }
  s.materialKey = key;
  if (!response.aicm_path.empty()) st.loadDocMeshAsync(response.aicm_path);
  st.markActiveTabDirty();
  return true;
}

// Normal material-combo edits stay non-blocking; canonical Apply uses the completed path above.
inline bool applyPartMaterial(AppState &st, Study &s, const std::string &key) {
  if (st.docLockedByAgent(st.docMirror.docId())) {
    st.toast(ui::ToastLevel::Warn, st.agentLockReason(st.docMirror.docId()));
    return false;
  }
  s.materialKey = key;
  if (!st.docMirror.hasDoc() || !st.workerStarted) return true;
  ++st.rebuildsInFlight;
  st.docMirror.applyOpsAsync(
      st.rpc, st.worker, partMaterialOps(st, key),
      [&st](const core::OpsResponse &resp, bool applied) {
        --st.rebuildsInFlight;
        if (!applied)
          return;
        if (resp.ok && !resp.aicm_path.empty()) {
          st.loadDocMeshAsync(resp.aicm_path);
        } else if (!resp.ok) {
          st.addLog(std::string("[err] set_material: ") + resp.error_message);
          if (resp.error_code != "transport")
            st.toast(ui::ToastLevel::Error,
                     std::string("set_material failed: ") +
                         (resp.error_message.empty() ? resp.error_code
                                                     : resp.error_message));
        }
        if (resp.ok)
          st.markActiveTabDirty();
      });
  return true;
}

// ---------------------------------------------------------------- result
// fields

// Named scalar fields a STATIC study's overlay carries (must match
// core/sim/overlay.cpp chunk names). kind: 0 = stress, 1 = length, 2 =
// dimensionless. The host switches between these with NO re-solve by re-loading
// the cached overlay coloured by the chosen chunk name
// (AppState::activeScalarField).
struct ResultField {
  const char *chunk;
  const char *label;
  int kind;
};
inline const ResultField kStaticFields[] = {
    {"von_mises", "von Mises stress", 0},
    // P1 - P3 is the Tresca stress INTENSITY (twice the max shear stress); it is
    // the quantity the Tresca criterion compares with yield, and it is always
    // >= von Mises, so it must never be labelled as a shear stress.
    {"tresca", "Tresca stress intensity (P1 - P3)", 0},
    {"abs_principal", "Max normal stress", 0},
    {"p1", "Max principal stress (P1)", 0},
    {"p3", "Min principal stress (P3)", 0},
    {"max_shear", "Max shear stress", 0},
    {"displacement_mag", "Displacement magnitude", 1},
    {"disp_x", "Displacement X", 1},
    {"disp_y", "Displacement Y", 1},
    {"disp_z", "Displacement Z", 1},
    {"fos", "Factor of safety (von Mises)", 2},
    {"fos_tresca", "Factor of safety (Tresca)", 2},
    {"fos_rankine", "Factor of safety (max normal)", 2},
};
inline const int kNStaticFields =
    (int)(sizeof(kStaticFields) / sizeof(kStaticFields[0]));

// Fields a FATIGUE study's overlay carries (kind 2 = dimensionless).
inline const ResultField kFatigueFields[] = {
    {"fatigue_fos", "Fatigue factor of safety", 2},
    {"life_log10", "Life (log10 cycles)", 2},
    {"von_mises", "Peak von Mises stress", 0},
};
inline const int kNFatigueFields =
    (int)(sizeof(kFatigueFields) / sizeof(kFatigueFields[0]));

// Fields a FLOW study's overlay carries. kind 3 = velocity (m/s,
// unit-agnostic); kind 2 = dimensionless.
inline const ResultField kFlowFields[] = {
    {"pressure_cp", "Pressure coefficient (Cp)", 2},
    {"temperature", "Temperature", 3}, // OpenFOAM thermal studies
    {"velocity", "Velocity", 3},
};
inline const int kNFlowFields = (int)(sizeof(kFlowFields) / sizeof(kFlowFields[0]));

// Fluid presets: {label, density kg/m^3, kinematic viscosity m^2/s}. The last
// entry (Custom) is a sentinel — its density/viscosity come from the study's
// editable fields instead.
struct FluidPreset {
  const char *label;
  float density;
  float visc;
};
inline const FluidPreset kFluids[3] = {
    {"Air (sea level, 15 C)", 1.225f, 1.5e-5f},
    {"Water (fresh, 20 C)", 998.0f, 1.0e-6f},
    {"Custom", 1.225f, 1.5e-5f},
};
inline const int kNFluids = 3;

// Surface-roughness presets: equivalent sand-grain roughness Ks in micrometres.
// Last entry = Custom.
struct RoughPreset {
  const char *label;
  float ks_um;
};
inline const RoughPreset kRoughness[6] = {
    {"Smooth / polished", 0.0f},
    {"Smooth paint / gelcoat", 6.0f},
    {"Bare aluminum / sheet metal", 30.0f},
    {"Galvanized / rough paint", 150.0f},
    {"Cast / rough composite", 500.0f},
    {"Custom (\xc2\xb5m)", 0.0f},
};
inline const int kNRoughness = 6;

// Resolve a study's fluid choice to concrete density + kinematic viscosity
// (SI).
inline void flowFluidProps(const ui::Study &s, float &density, float &visc) {
  int fi = (s.flowFluid < 0 || s.flowFluid >= kNFluids) ? 0 : s.flowFluid;
  if (fi == kNFluids - 1) {
    density = s.flowDensity;
    visc = s.flowViscosity;
  } // Custom
  else {
    density = kFluids[fi].density;
    visc = kFluids[fi].visc;
  }
}
// Resolve a study's roughness choice to an equivalent sand-grain Ks in
// micrometres.
inline float flowRoughnessUm(const ui::Study &s) {
  int ri = (s.flowRoughness < 0 || s.flowRoughness >= kNRoughness)
               ? 0
               : s.flowRoughness;
  return (ri == kNRoughness - 1) ? s.flowRoughnessUm : kRoughness[ri].ks_um;
}

// Display label + unit string + SI->display factor for a named field, honoring
// the active unit system.
inline void fieldMeta(const AppState &st, const std::string &chunk, std::string &label,
               std::string &unit, double &fromSI) {
  SimManager::UnitSys u = SimManager::unitSys(st);
  if (chunk == "mode_amplitude") {
    label = "Mode amplitude";
    unit = "";
    fromSI = 1.0;
    return;
  }
  if (chunk == "temperature") {
    label = "Temperature";
    unit = "\xc2\xb0" "C";
    fromSI = 1.0;
    return;
  }
  auto scan = [&](const ResultField *arr, int n) -> bool {
    for (int i = 0; i < n; ++i)
      if (chunk == arr[i].chunk) {
        label = arr[i].label;
        if (arr[i].kind == 0) {
          unit = u.stress;
          fromSI = u.stressFromMpa;
        } else if (arr[i].kind == 1) {
          unit = u.len;
          fromSI = u.lenFromMm;
        } else if (arr[i].kind == 3) {
          unit = "m/s";
          fromSI = 1.0;
        } // velocity (always SI)
        else {
          unit = "";
          fromSI = 1.0;
        }
        return true;
      }
    return false;
  };
  if (scan(kStaticFields, kNStaticFields))
    return;
  if (scan(kFatigueFields, kNFatigueFields))
    return;
  if (scan(kFlowFields, kNFlowFields))
    return;
  label = chunk;
  unit = "";
  fromSI = 1.0;
}

// ---- fatigue math helpers (Marin endurance correction, reliability, S-N life)
// ----
inline const char *kTheoryKeys[5] = {"goodman", "gerber", "soderberg", "asme",
                              "morrow"};
inline const char *kTheoryNames[5] = {"Goodman", "Gerber", "Soderberg",
                               "ASME-elliptic", "Morrow"};

// Marin surface factor ka = A*Sut^b (capped at 1). 0 ground,1 machined,2
// hot-rolled,3 as-forged.
inline double marin_ka(double Sut, int surface) {
  static const double A[4] = {1.58, 4.51, 57.7, 272.0};
  static const double b[4] = {-0.085, -0.265, -0.718, -0.995};
  int i = (surface < 0 || surface > 3) ? 1 : surface;
  double ka = A[i] * std::pow(Sut, b[i]);
  return ka > 1.0 ? 1.0 : ka;
}
// Marin size factor kb (bending/torsion only; axial = 1). d in mm.
inline double marin_kb(double d_mm, int loadCase) {
  if (loadCase == 1)
    return 1.0; // axial: no size effect
  if (d_mm <= 0)
    d_mm = 7.62;
  if (d_mm < 2.79)
    d_mm = 2.79;
  if (d_mm <= 51.0)
    return std::pow(d_mm / 7.62, -0.107);
  return 1.51 * std::pow(d_mm, -0.157);
}
inline double marin_kc(int loadCase) {
  return loadCase == 1 ? 0.85 : loadCase == 2 ? 0.59 : 1.0;
}
// Marin temperature factor kd (Shigley table, interpolated; C).
inline double marin_kd(double T_c) {
  static const double Ts[7] = {20, 100, 200, 300, 400, 500, 550};
  static const double kd[7] = {1.0, 1.020, 1.025, 0.975, 0.90, 0.825, 0.76};
  if (T_c <= Ts[0])
    return 1.0;
  for (int i = 1; i < 7; ++i)
    if (T_c <= Ts[i]) {
      double t = (T_c - Ts[i - 1]) / (Ts[i] - Ts[i - 1]);
      return kd[i - 1] + t * (kd[i] - kd[i - 1]);
    }
  return kd[6];
}
// Reliability factor ke (Shigley), interpolated by reliability %.
inline double reliability_ke(double Rpct) {
  static const double R[7] = {50, 90, 95, 99, 99.9, 99.99, 99.999};
  static const double ke[7] = {1.000, 0.897, 0.868, 0.814, 0.753, 0.702, 0.659};
  if (Rpct <= R[0])
    return 1.0;
  for (int i = 1; i < 7; ++i)
    if (Rpct <= R[i]) {
      double t = (Rpct - R[i - 1]) / (R[i] - R[i - 1]);
      return ke[i - 1] + t * (ke[i] - ke[i - 1]);
    }
  return ke[6];
}
// Cycles to failure from an equivalent fully-reversed stress (Shigley
// high-cycle S-N). >=1e6 = runout.
inline double sn_life(double sa_eq, double Se, double Sut) {
  if (Se <= 0 || Sut <= 0 || sa_eq <= 0)
    return 1e6;
  if (sa_eq <= Se)
    return 1e6;
  const double f = 0.9, a = (f * Sut) * (f * Sut) / Se,
               b = -(1.0 / 3.0) * std::log10((f * Sut) / Se);
  double N = std::pow(sa_eq / a, 1.0 / b);
  return N < 1 ? 1 : (N > 1e6 ? 1e6 : N);
}
// Native fatigue spectra are tokens; the form uses the corresponding enum indices.
inline int fatigueSpectrumType(const std::string &token) {
  const auto matches = [&](const char* native) {
    return token.size() == std::strlen(native) &&
           std::equal(token.begin(), token.end(), native,
                      [](unsigned char a, unsigned char b) { return std::tolower(a) == b; });
  };
  if (matches("reversed")) return 0;
  if (matches("zero_based")) return 1;
  if (matches("ratio")) return 2;
  return -1;
}

inline void fatigueLoadFactors(const Study &s, float &lmin, float &lmax) {
  switch (s.fatigueLoadType) {
  case 1:
    lmin = 0;
    lmax = 1;
    break; // zero-based
  case 2:
    lmin = s.fatigueRatio;
    lmax = 1;
    break; // ratio R
  case 3:
    lmin = s.fatigueLmin;
    lmax = s.fatigueLmax;
    break; // custom
  default:
    lmin = -1;
    lmax = 1;
    break; // fully reversed
  }
}
// Base (uncorrected) endurance limit Se' for a study: override, else material,
// else 0.5*Sut capped.
inline double seBase(const Study &s, double matSe, double Sut) {
  if (s.fatigueSe > 0)
    return s.fatigueSe;
  if (matSe > 0)
    return matSe;
  return std::min(0.5 * Sut, 700.0);
}

inline bool simOriginActive(const AppState &st, const SimManager *manager,
                     const std::string &docId) {
  return st.simManager == manager && st.docMirror.hasDoc() &&
         st.docMirror.docId() == docId;
}

inline bool simDocOpen(const AppState &st, const std::string &docId) {
  return std::any_of(st.docTabs.begin(), st.docTabs.end(),
                     [&](const AppState::DocTab &tab) {
                       return tab.docId == docId;
                     });
}
inline bool simJsonBool(const nlohmann::json &value, const char *key,
                 bool fallback) {
  const auto it = value.find(key);
  return it == value.end()
             ? fallback
             : (it->is_boolean() ? it->get<bool>() : false);
}

inline std::string simJsonText(const nlohmann::json &value, const char *primary,
                        const char *secondary,
                        const std::string &fallback) {
  auto it = value.find(primary);
  if (it != value.end() && it->is_string())
    return it->get<std::string>();
  it = value.find(secondary);
  if (it != value.end() && it->is_string())
    return it->get<std::string>();
  return fallback;
}

inline std::string simPathSegment(std::string value) {
  for (char &c : value) {
    const unsigned char uc = static_cast<unsigned char>(c);
    if (!std::isalnum(uc) && c != '-' && c != '_')
      c = '_';
  }
  return value.empty() ? std::string("unknown") : value;
}
} // namespace simmgr

struct SimManager::StaticModalPrep {
  struct Load {
    nlohmann::json definition; // canonical row, retained with its selectors through resolution
    std::vector<nlohmann::json> selectors;
    std::vector<nlohmann::json> resolved;
  };

  core::SimClient *sim = nullptr;
  std::uint64_t reservationToken = 0;
  std::string docId;
  std::string studyId;
  std::string requestedStepPath;
  std::string resolvedStepPath;
  nlohmann::json canonical; // immutable merged study at Run, never reconstructed from live UI
  nlohmann::json resolvedMaterial; // FRF mass calculation uses the modal run's material snapshot
  std::string sourceGeometryKey; // study persistence bumps revision without changing geometry
  std::uint64_t sourceMutationGen = 0;
  StudyType type = StudyType::Static;
  int nModes = 6;
  nlohmann::json material = nlohmann::json::object();
  Study meshOptions;
  std::vector<nlohmann::json> fixtureSelectors;
  std::vector<nlohmann::json> fixtureDefinitions;
  std::vector<nlohmann::json> fixtures;
  std::vector<Load> loads;
  nlohmann::json flowParams;
  nlohmann::json fatigue;
  nlohmann::json thermalMaterial;
  nlohmann::json thermalTransient;
  float thermFixTempC = 0.0f;
  // Topology: the canonical study snapshot taken at Run, its normalized material, every
  // unique document selector to resolve (doc_face_points) and the resolutions (same order).
  nlohmann::json topoStudy;
  nlohmann::json topoMaterial;
  std::vector<nlohmann::json> topoSelectors;
  std::vector<nlohmann::json> topoResolved;
  std::string topoGeometryKey; // source geometry digest at Run (checked before launch)
  std::size_t facesRemaining = 0;
};

namespace simmgr {
// Mesh keys a topology study persists, mirrored onto the Study's mesh fields so the shared
// Mesh form / preview edit them (topology meshes are always tet4).
inline void topologyMeshFromJson(Study &s, const nlohmann::json &j) {
  auto num = [&](const char *key, float fallback) {
    const auto it = j.find(key);
    return it != j.end() && it->is_number() ? it->get<float>() : fallback;
  };
  s.meshOrder = 1;
  s.meshFineness = num("mesh_fineness", 0.5f);
  s.meshTargetMm = num("mesh_target_mm", 0.0f);
  s.meshMinMm = num("mesh_min_mm", 0.0f);
}

// Artifact descriptor format, normalized to the sim_topology.h vocabulary for display
// ("aicm_v1" -> "aicm", "stl_binary" -> "stl"); the persisted record is never rewritten.
inline std::string topoArtifactFormat(const nlohmann::json &a) {
  std::string f = a.is_object() && a.contains("format") && a["format"].is_string()
                      ? a["format"].get<std::string>()
                      : std::string();
  if (f.rfind("aicm", 0) == 0)
    return "aicm";
  if (f.rfind("stl", 0) == 0)
    return "stl";
  return f;
}

// ---------------------------------------------------------------- JSON <->
// Study

struct MotionJoint {
  std::string id, label, type, unit;
  double value = 0.0;
};

inline std::vector<MotionJoint> motionJoints(const nlohmann::json &tree) {
  std::vector<MotionJoint> joints;
  const auto mates = tree.find("mates");
  if (mates == tree.end() || !mates->is_array())
    return joints;
  std::map<std::string, std::string> names;
  std::map<std::string, bool> movable;
  const auto components = tree.find("components");
  if (components != tree.end() && components->is_array())
    for (const auto &c : *components)
      if (c.is_object() && !simJsonBool(c, "suppressed", false)) {
        const std::string id = c.value("id", std::string());
        names[id] = c.value("name", std::string());
        movable[id] = !simJsonBool(c, "fixed", false);
      }
  for (const auto &mate : *mates) {
    if (!mate.is_object() || simJsonBool(mate, "suppressed", false))
      continue;
    const std::string type = mate.value("type", std::string());
    const std::string id = mate.value("id", std::string());
    if (id.empty() || (type != "angle" && type != "distance"))
      continue;
    const auto refs = mate.find("refs");
    if (refs == mate.end() || !refs->is_array() || refs->size() < 2 ||
        !(*refs)[0].is_object() || !(*refs)[1].is_object())
      continue;
    const auto a = movable.find((*refs)[0].value("component", std::string()));
    const auto b = movable.find((*refs)[1].value("component", std::string()));
    if (a == movable.end() || b == movable.end() || a == b || (!a->second && !b->second))
      continue;
    MotionJoint joint{id, mate.value("name", std::string()), type,
                      type == "angle" ? "deg" : "mm", 0.0};
    const auto value = mate.find("value");
    bool finiteValue = false;
    if (value != mate.end()) {
      const auto *v = &*value;
      if (v->is_object() && v->contains("value"))
        v = &(*v)["value"];
      if (v->is_number() && std::isfinite(v->get<double>())) {
        joint.value = v->get<double>();
        finiteValue = true;
      }
    }
    if (!finiteValue)
      continue;
    if (joint.label.empty() && mate.contains("refs") && mate["refs"].is_array())
      for (const auto &ref : mate["refs"]) {
        if (!ref.is_object())
          continue;
        const std::string component = ref.value("component", std::string());
        if (component.empty())
          continue;
        const auto name = names.find(component);
        if (!joint.label.empty())
          joint.label += " / ";
        joint.label += name != names.end() && !name->second.empty()
                           ? name->second : component;
      }
    if (joint.label.empty())
      joint.label = "Joint";
    joint.label += " [" + id + "] - " + type + " (" + joint.unit + ")";
    joints.push_back(std::move(joint));
  }
  return joints;
}

inline void seedMotionDriver(Study &s, const std::vector<MotionJoint> &joints) {
  if (s.motionDrivers.size() == 1 && s.motionDrivers[0].mateId.empty() &&
      !joints.empty()) {
    auto &driver = s.motionDrivers[0];
    driver.mateId = joints[0].id;
    if (driver.start == 0.0 && driver.stop == 360.0) {
      driver.start = joints[0].value;
      driver.stop = driver.start + (joints[0].type == "angle" ? 360.0 : 10.0);
    }
  }
}

inline bool motionJointsReady(const Study &s, const nlohmann::json &tree,
                       std::string &blocker) {
  const auto joints = motionJoints(tree);
  if (joints.empty()) {
    blocker = "Add an angle or distance mate in Assembly.";
    return false;
  }
  for (const auto &driver : s.motionDrivers)
    if (std::none_of(joints.begin(), joints.end(), [&](const MotionJoint &joint) {
          return joint.id == driver.mateId;
        })) {
      blocker = "Re-pick a joint: one is missing or suppressed.";
      return false;
    }
  return true;
}

inline nlohmann::json motionDriversJson(const Study &s) {
  auto drivers = nlohmann::json::array();
  for (const auto &driver : s.motionDrivers)
    drivers.push_back({{"mate_id", driver.mateId}, {"start", driver.start},
                      {"stop", driver.stop}});
  return drivers;
}

inline bool sameMotionSource(const nlohmann::json &a, const nlohmann::json &b) {
  if (!a.is_object() || !b.is_object())
    return a == b;
  if (a.size() - a.count("studies") != b.size() - b.count("studies"))
    return false;
  for (auto it = a.begin(); it != a.end(); ++it) {
    if (it.key() == "studies")
      continue;
    const auto other = b.find(it.key());
    if (other == b.end() || *other != it.value())
      return false;
  }
  return true;
}

// Why Motion cannot sample yet (Run shares the viewport's parsed assembly mesh); "" = ready.
// Defined in sim_manager_motion.cpp.
std::string motionGeometryBlocker(AppState &st);

// ================================================================ topology studies
//
// The canonical study JSON (Study::topo) is edited by ui::topology editors; this section owns
// the glue: the async capability probe, the run gate, preparation (one export + one
// doc_face_points per unique selector, reusing beginExportPrep), the native sim_run_topology
// launch through SimClient, the retained run history, artifact checks and actions that must
// stay off the draw path, and the scenario API.

inline std::string topoRunIdOf(const nlohmann::json &run, std::size_t index) {
  return run.is_object() && run.contains("run_id") && run["run_id"].is_string()
             ? run["run_id"].get<std::string>()
             : "#" + std::to_string(index);
}

inline const nlohmann::json *topoFindRun(const nlohmann::json &runs, const std::string &runId) {
  if (!runs.is_array())
    return nullptr;
  for (std::size_t i = 0; i < runs.size(); ++i)
    if (topoRunIdOf(runs[i], i) == runId)
      return &runs[i];
  return nullptr;
}

inline nlohmann::json *topoFindRun(nlohmann::json &runs, const std::string &runId) {
  if (!runs.is_array())
    return nullptr;
  for (std::size_t i = 0; i < runs.size(); ++i)
    if (topoRunIdOf(runs[i], i) == runId)
      return &runs[i];
  return nullptr;
}

// Type-tolerant readers: run records may be authored by other tools (model tools, older
// versions); a wrong-typed member reads as absent instead of throwing mid-frame.
inline const nlohmann::json &topoObj(const nlohmann::json &j, const char *key) {
  static const nlohmann::json kEmpty = nlohmann::json::object();
  if (!j.is_object())
    return kEmpty;
  const auto it = j.find(key);
  return it != j.end() && it->is_object() ? *it : kEmpty;
}
inline std::string topoStr(const nlohmann::json &j, const char *key) {
  if (!j.is_object())
    return {};
  const auto it = j.find(key);
  return it != j.end() && it->is_string() ? it->get<std::string>() : std::string();
}
inline bool topoTrue(const nlohmann::json &j, const char *key) {
  if (!j.is_object())
    return false;
  const auto it = j.find(key);
  return it != j.end() && it->is_boolean() && it->get<bool>();
}

// The overlay artifact of a (decorated) run: {path, arrays} or null.
inline nlohmann::json topoOverlayArtifact(const nlohmann::json &run) {
  const nlohmann::json &result = topoObj(run, "result");
  const nlohmann::json &arts = topoObj(result, "artifacts");
  for (auto it = arts.begin(); it != arts.end(); ++it) {
    const auto &a = it.value();
    if (topoStr(a, "path").empty())
      continue;
    if (topoArtifactFormat(a) != "aicm" && it.key() != "overlay_aicm")
      continue;
    if (topoTrue(a, "missing") || topoTrue(a, "stale"))
      return nullptr;
    return a;
  }
  const std::string direct = topoStr(result, "overlay_aicm");
  if (!direct.empty())
    return {{"path", direct}};
  return nullptr;
}

// ---- indicator geometry (fixture glyphs + load arrows) ----

inline void vecNorm3(float v[3]) {
  float l = std::sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2]);
  if (l > 1e-9f) {
    v[0] /= l;
    v[1] /= l;
    v[2] /= l;
  }
}
inline void perp3(const float d[3], float p[3]) {
  float a[3] = {1, 0, 0};
  if (std::fabs(d[0]) > 0.9f) {
    a[0] = 0;
    a[1] = 1;
    a[2] = 0;
  }
  p[0] = d[1] * a[2] - d[2] * a[1];
  p[1] = d[2] * a[0] - d[0] * a[2];
  p[2] = d[0] * a[1] - d[1] * a[0];
  vecNorm3(p);
}
inline void pushSeg3(std::vector<float> &s, const float a[3], const float b[3]) {
  s.insert(s.end(), {a[0], a[1], a[2], b[0], b[1], b[2]});
}
inline void cross3(const float a[3], const float b[3], float out[3]) {
  out[0] = a[1] * b[2] - a[2] * b[1];
  out[1] = a[2] * b[0] - a[0] * b[2];
  out[2] = a[0] * b[1] - a[1] * b[0];
}
inline void pushTri3(std::vector<float> &t, const float a[3], const float b[3],
              const float c[3]) {
  t.insert(t.end(), {a[0], a[1], a[2], b[0], b[1], b[2], c[0], c[1], c[2]});
}
// Solid 12-segment cone: apex at `tip`, axis pointing tip->base, height h, base
// radius r. Side fan + base cap so the head reads from every angle; the SS
// resolve anti-aliases it.
inline void pushCone3(std::vector<float> &t, const float tip[3], const float axis[3],
               float h, float r) {
  float d[3] = {axis[0], axis[1], axis[2]};
  vecNorm3(d);
  float bc[3] = {tip[0] + d[0] * h, tip[1] + d[1] * h, tip[2] + d[2] * h};
  float p1[3], p2[3];
  perp3(d, p1);
  cross3(d, p1, p2);
  const int kSegs = 12;
  float prev[3] = {bc[0] + p1[0] * r, bc[1] + p1[1] * r, bc[2] + p1[2] * r};
  for (int i = 1; i <= kSegs; ++i) {
    const float a = (float)i / (float)kSegs * 6.2831853f;
    const float ca = std::cos(a), sa = std::sin(a);
    float q[3] = {bc[0] + (p1[0] * ca + p2[0] * sa) * r,
                  bc[1] + (p1[1] * ca + p2[1] * sa) * r,
                  bc[2] + (p1[2] * ca + p2[2] * sa) * r};
    pushTri3(t, tip, prev, q); // side
    pushTri3(t, bc, q, prev);  // base cap
    prev[0] = q[0];
    prev[1] = q[1];
    prev[2] = q[2];
  }
}
// Arrow: shaft tail->tip (tip = tail + dir*len) + a head at the tip. With
// `tris` the head is a SOLID triangulated cone (shaft stops at the cone base so
// it never pokes through the point) and `headLen` can cap the head size;
// without it the legacy 4-line wireframe head is emitted so old segments-only
// callers render unchanged.
inline void pushArrow3(std::vector<float> &s, const float tail[3], const float dir[3],
                float len, std::vector<float> *tris = nullptr,
                float headLen = -1.0f) {
  float d[3] = {dir[0], dir[1], dir[2]};
  vecNorm3(d);
  float tip[3] = {tail[0] + d[0] * len, tail[1] + d[1] * len,
                  tail[2] + d[2] * len};
  const float hl = headLen > 0.0f ? headLen : len * 0.28f;
  const float hw =
      hl * 0.46f; // same head aspect as the old wireframe (0.13/0.28 of len)
  float bk[3] = {tip[0] - d[0] * hl, tip[1] - d[1] * hl, tip[2] - d[2] * hl};
  if (tris) {
    pushSeg3(s, tail, bk);
    float back[3] = {-d[0], -d[1], -d[2]};
    pushCone3(*tris, tip, back, hl, hw);
    return;
  }
  pushSeg3(s, tail, tip);
  float p1[3], p2[3];
  perp3(d, p1);
  cross3(d, p1, p2);
  float c1[3] = {bk[0] + p1[0] * hw, bk[1] + p1[1] * hw, bk[2] + p1[2] * hw};
  float c2[3] = {bk[0] - p1[0] * hw, bk[1] - p1[1] * hw, bk[2] - p1[2] * hw};
  float c3[3] = {bk[0] + p2[0] * hw, bk[1] + p2[1] * hw, bk[2] + p2[2] * hw};
  float c4[3] = {bk[0] - p2[0] * hw, bk[1] - p2[1] * hw, bk[2] - p2[2] * hw};
  pushSeg3(s, tip, c1);
  pushSeg3(s, tip, c2);
  pushSeg3(s, tip, c3);
  pushSeg3(s, tip, c4);
}

// Mirror the native terminal-faceset shape in readiness without rewriting authored geometry
// or metadata. Document selectors remain queries; compounds contain terminal leaves only.
inline bool nativeTerminalFacesetReady(const nlohmann::json &face) {
  if (!face.is_object() || face.contains("facesets")) return false;
  const auto points = face.find("points");
  if (points == face.end() || !points->is_array() || points->empty()) return false;
  const auto vec3 = [](const nlohmann::json &value) {
    if (!value.is_array() || value.size() < 3) return false;
    for (int k = 0; k < 3; ++k)
      if (!value[k].is_number() || !std::isfinite(value[k].get<double>())) return false;
    return true;
  };
  const auto axis = [&](const nlohmann::json &value) {
    if (!vec3(value)) return false;
    double norm2 = 0;
    for (int k = 0; k < 3; ++k) {
      const double component = value[k].get<double>();
      norm2 += component * component;
    }
    return std::isfinite(norm2) && norm2 > 0;
  };
  std::size_t count = 0;
  for (const auto &point : *points) {
    if (point.is_array()) {
      if (!vec3(point)) return false;
      count += 3;
    } else {
      if (!point.is_number() || !std::isfinite(point.get<double>())) return false;
      ++count;
    }
  }
  if (count < 3 || count % 3 != 0) return false;
  for (const char *key : {"tol_mm", "perp_tol_mm"}) {
    const auto value = face.find(key);
    if (value != face.end() && (!value->is_number() ||
        !std::isfinite(value->get<double>()) || value->get<double>() <= 0)) return false;
  }
  const auto normal = face.find("face_normal");
  if (normal != face.end() && !axis(*normal)) return false;
  const auto surface = face.find("surface");
  if (surface == face.end()) return true;
  if (!surface->is_object()) return false;
  const auto type = surface->find("type");
  const auto origin = surface->find("origin");
  if (type == surface->end() || !type->is_string() ||
      origin == surface->end() || !vec3(*origin)) return false;
  const auto positive = [&](const char *key) {
    const auto value = surface->find(key);
    return value != surface->end() && value->is_number() &&
           std::isfinite(value->get<double>()) && value->get<double>() > 0;
  };
  if (*type == "sphere") return positive("radius");
  const auto direction = surface->find("axis");
  if (direction == surface->end() || !axis(*direction)) return false;
  if (*type == "cylinder") return positive("radius");
  if (*type == "cone")
    return positive("semi_angle_rad") && (*surface)["semi_angle_rad"].get<double>() < 1.5707963;
  if (*type == "torus") return positive("major_radius") && positive("minor_radius");
  return false;
}

inline bool nativeBoundaryReady(const nlohmann::json &boundary) {
  if (boundary.is_array()) {
    if (boundary.empty()) return false;
    for (const auto &entry : boundary)
      if (entry.is_array() || !nativeBoundaryReady(entry)) return false;
    return true;
  }
  if (!boundary.is_object())
    return boundary.is_string() && !boundary.get_ref<const std::string&>().empty();
  if (boundary.contains("facesets")) {
    for (const char *key : {"points", "tol_mm", "perp_tol_mm", "face_normal", "surface"})
      if (boundary.contains(key)) return false;
    const auto &children = boundary["facesets"];
    if (!children.is_array() || children.empty()) return false;
    for (const auto &child : children)
      if (!nativeTerminalFacesetReady(child)) return false;
    return true;
  }
  if (boundary.contains("points")) return nativeTerminalFacesetReady(boundary);
  if (boundary.contains("face")) return nativeBoundaryReady(boundary["face"]);
  if (boundary.contains("faces")) return nativeBoundaryReady(boundary["faces"]);
  return !boundary.empty(); // An unresolved document selector, checked by native preparation.
}

inline bool nativeLoadHasFaces(const SimLoad &load) {
  if (!load.faces.empty()) {
    for (const auto &pick : load.faces.picks)
      if (!nativeBoundaryReady(pick.selector)) return false;
    return true;
  }
  if (!load.definition.is_object()) return false;
  const auto faces = load.definition.find("faces");
  if (faces != load.definition.end()) return nativeBoundaryReady(*faces);
  const auto face = load.definition.find("face");
  return face != load.definition.end() && nativeBoundaryReady(*face);
}

inline bool nativeMaterialReady(const Study &s, std::string &blocker) {
  const auto &record = s.definition;
  const int mi = SimManager::materialIndexByKey(s.materialKey);
  const bool authored = record.is_object() && record.contains("material") &&
                        record["material"].is_object();
  const bool proxyMaterial = s.type == StudyType::Contact && record.is_object() &&
                             record.contains("E_mpa") && record.contains("nu");
  if (!authored && !proxyMaterial && mi < 0) {
    blocker = "Pick a material or define native material properties.";
    return false;
  }
  static const nlohmann::json empty = nlohmann::json::object();
  const auto &material = authored ? record["material"] : proxyMaterial ? record : empty;
  const auto *library = !authored && mi >= 0 ? &SimManager::materials()[mi] : nullptr;
  auto number = [](const nlohmann::json &j, const char *key, double fallback) {
    const auto it = j.find(key);
    return it != j.end() && it->is_number() ? it->get<double>() : fallback;
  };
  if (s.type == StudyType::ThermalFea) {
    double k = number(material, "k_w_mk",
                      number(material, "conductivity_w_mk", s.thermK));
    if (record.is_object()) {
      if (!material.contains("k_w_mk") && !material.contains("conductivity_w_mk"))
        k = number(record, "k_w_mk", k);
      k = number(record, "conductivity_w_mk", k);
    }
    const bool transient = s.thermTransient ||
                           (record.is_object() && record.contains("transient"));
    double cp = number(material, "cp_j_kgk", s.thermCp);
    if (record.is_object()) {
      if (!material.contains("cp_j_kgk"))
        cp = number(record, "cp_j_kgk", cp);
      cp = number(record, "specific_heat_j_kgk", cp);
    }
    const double rho = number(material, "rho_kg_m3",
                             1000.0 * number(material, "density_g_cm3",
                                             library ? library->density_g_cm3 : 0.0));
    if (!std::isfinite(k) || k <= 0.0 ||
        (transient && (!std::isfinite(cp) || cp <= 0.0 ||
                       !std::isfinite(rho) || rho <= 0.0))) {
      blocker = "Define positive conductivity, and heat capacity/density for transient heat.";
      return false;
    }
    return true;
  }
  auto elastic = [&](const nlohmann::json &leaf) {
    const double e = number(leaf, "E_mpa", 0.0);
    const double nu = number(leaf, "nu", 1.0);
    return std::isfinite(e) && e > 0.0 && std::isfinite(nu) && nu > -1.0 && nu < 0.5;
  };
  bool valid = elastic(material) ||
               (library && std::isfinite(library->E_mpa) && library->E_mpa > 0.0 &&
                std::isfinite(library->nu) && library->nu > -1.0 && library->nu < 0.5);
  if ((s.type == StudyType::Static || s.type == StudyType::Fatigue) &&
      material.contains("per_body") && material["per_body"].is_object()) {
    valid = !material["per_body"].empty() || material.contains("default");
    for (const auto &leaf : material["per_body"])
      valid = elastic(leaf) && valid;
    if (material.contains("default"))
      valid = elastic(material["default"]) && valid;
  }
  if (!valid) {
    blocker = "Define positive modulus and Poisson ratio between -1 and 0.5.";
    return false;
  }
  if (s.type == StudyType::Modal || s.type == StudyType::FreqResponse) {
    const double density = number(material, "density_g_cm3",
                                  library ? library->density_g_cm3 : 0.0);
    if (!std::isfinite(density) || density <= 0.0) {
      blocker = "Define a positive material density.";
      return false;
    }
  }
  return true;
}

// ---- pre-flight readiness gate ---------------------------------------------------------------
// Returns true only when the values that the selected backend actually consumes are complete and
// finite. run() repeats this gate, so ribbon commands cannot bypass the disabled panel button.
inline bool studyReady(const Study &s, std::string &blocker) {
  auto finite = [](double v) { return std::isfinite(v); };
  if (s.type == StudyType::Topology)
    return true; // gated by SimManager::topologyReady (needs the capability matrix)
  if (s.type == StudyType::Motion) {
    if (s.motionDrivers.empty()) {
      blocker = "Add an angle or distance driver.";
      return false;
    }
    std::unordered_set<std::string> ids;
    bool moving = false;
    for (const auto &driver : s.motionDrivers) {
      if (driver.mateId.empty() || !ids.insert(driver.mateId).second) {
        blocker = "Give each driver its own joint.";
        return false;
      }
      if (!finite(driver.start) || !finite(driver.stop)) {
        blocker = "Enter start and stop for each driver.";
        return false;
      }
      moving = moving || std::fabs(driver.stop - driver.start) >= 1e-9;
    }
    if (!moving) {
      blocker = "Make a driver's start differ from its stop.";
      return false;
    }
    if (!finite(s.motionMinClearance) || s.motionMinClearance < 0.0) {
      blocker = "Set a minimum clearance of 0 mm or more.";
      return false;
    }
    if (s.motionSteps < 2 || s.motionSteps > 120) {
      blocker = "Set between 2 and 120 motion steps.";
      return false;
    }
    return true;
  }
#ifdef __EMSCRIPTEN__
  if (isFlowFamily(s.type) || s.type == StudyType::Contact ||
      s.type == StudyType::FreqResponse) {
    blocker = "Not available in the web version yet.";
    return false;
  }
#endif
  if (isFlowFamily(s.type)) {
    float rho = 0.0f, nu = 0.0f;
    flowFluidProps(s, rho, nu);
    if (!finite(s.flowSpeed) || s.flowSpeed <= 0.0f) {
      blocker = "Set a flow speed above zero.";
      return false;
    }
    if (!finite(s.flowAoA) || !finite(s.flowSideslip) ||
        s.flowDir < 0 || s.flowDir > 5 || s.flowPreset < 0 ||
        s.flowPreset > 3 || s.flowFluid < 0 || s.flowFluid > 2) {
      blocker = "Fix the flow direction, fluid or resolution.";
      return false;
    }
    if (!finite(rho) || !finite(nu) || rho <= 0.0f || nu <= 0.0f) {
      blocker = "Set a positive fluid density and viscosity.";
      return false;
    }
    const float rough = flowRoughnessUm(s);
    if (!finite(rough) || rough < 0.0f || !finite(s.flowRefAreaMm2) ||
        s.flowRefAreaMm2 < 0.0f) {
      blocker = "Set roughness and reference area to 0 or more.";
      return false;
    }
    if (s.type == StudyType::Thermal ||
        (s.flowSolver == 1 && s.flowThermal)) {
      if (!finite(s.flowInletTempC)) {
        blocker = "Enter an inlet temperature.";
        return false;
      }
      if (s.flowThermalType == 1) {
        if (!finite(s.flowSolidKappa) || s.flowSolidKappa <= 0.0f ||
            !finite(s.flowPowerW) || s.flowPowerW <= 0.0f) {
          blocker = "Set a positive solid conductivity and power.";
          return false;
        }
      } else if ((s.flowWallMode == 0 && !finite(s.flowWallTempC)) ||
                 (s.flowWallMode == 1 &&
                  (!finite(s.flowWallFluxWm2) ||
                   std::fabs(s.flowWallFluxWm2) < 1e-12)) ||
                 (s.flowWallMode == 2 &&
                  (!finite(s.flowWallPowerW) || s.flowWallPowerW <= 0.0f))) {
        blocker = "Set a non-zero wall temperature, flux or power.";
        return false;
      }
    }
    return true;
  }

  if (!nativeMaterialReady(s, blocker))
    return false;
  for (const auto &fixture : s.fixtures.picks)
    if (!nativeBoundaryReady(fixture.selector)) {
      blocker = "Fixed faces need valid selectors or non-empty terminal facesets without nesting "
                "or compound-level geometry hints.";
      return false;
    }
  if (s.type == StudyType::Static || s.type == StudyType::Modal ||
      s.type == StudyType::Fatigue || s.type == StudyType::ThermalFea ||
      s.type == StudyType::FreqResponse || s.type == StudyType::Buckling) {
    if (!finite(s.meshFineness) || s.meshFineness < 0.0f ||
        s.meshFineness > 1.0f || !finite(s.meshTargetMm) ||
        s.meshTargetMm < 0.0f || !finite(s.meshMinMm) ||
        s.meshMinMm < 0.0f ||
        (s.meshTargetMm > 0.0f && s.meshMinMm > s.meshTargetMm) ||
        (s.meshOrder != 1 && s.meshOrder != 2)) {
      blocker = "Fix the mesh sizes: minimum exceeds target.";
      return false;
    }
  }

  if (s.type == StudyType::Buckling && s.nModes < 1) {
    blocker = "Set at least one mode.";
    return false;
  }
  if (s.type == StudyType::Contact) {
    if (!finite(s.contactPressureMpa) || s.contactPressureMpa <= 0.0f) {
      blocker = "Set a contact pressure above zero.";
      return false;
    }
    return true;
  }
  if (s.type == StudyType::ThermalFea) {
    if (!finite(s.thermK) || s.thermK <= 0.0f) {
      blocker = "Set a solid conductivity above zero.";
      return false;
    }
    if (!finite(s.thermFixTempC) || !finite(s.thermTInitC)) {
      blocker = "Enter the fixed and initial temperatures.";
      return false;
    }
    if (s.thermTransient &&
        (!finite(s.thermCp) || s.thermCp <= 0.0f ||
         !finite(s.thermTEndS) || s.thermTEndS <= 0.0f ||
         !finite(s.thermDtS) || s.thermDtS < 0.0f)) {
      blocker = "Fix the transient time settings.";
      return false;
    }
    if (s.fixtures.empty() && s.loads.empty()) {
      blocker = "Add a fixed-temp face or heat load.";
      return false;
    }
    for (int i = 0; i < (int)s.loads.size(); ++i) {
      const SimLoad &ld = s.loads[i];
      if (!nativeLoadHasFaces(ld)) {
        blocker = "Pick faces for heat load " + std::to_string(i + 1) + ".";
        return false;
      }
      if (ld.thermalKind == 1) {
        if (!finite(ld.hWm2k) || ld.hWm2k <= 0.0f || !finite(ld.tAmbC)) {
          blocker = "Set convection h and ambient temperature.";
          return false;
        }
      } else if (!finite(ld.fluxWm2) || std::fabs(ld.fluxWm2) < 1e-12) {
        blocker = "Set a non-zero heat flux.";
        return false;
      }
    }
    if (!s.thermTransient && s.fixtures.empty()) {
      bool hasConvection = false;
      for (const auto &ld : s.loads)
        hasConvection = hasConvection || ld.thermalKind == 1;
      if (!hasConvection) {
        blocker = "Add a fixed-temp face or convection load.";
        return false;
      }
    }
    return true;
  }
  if (s.type == StudyType::FreqResponse) {
    if (!finite(s.frBaseG) || s.frBaseG <= 0.0f || !finite(s.frZeta) ||
        s.frZeta <= 0.0f || s.frZeta >= 1.0f) {
      blocker = "Set base input and damping (0 to 1).";
      return false;
    }
    return true; // fixtures remain optional: free-free modal basis is intentional
  }
  if (s.type == StudyType::Modal)
    return true; // intentional free-free exception; no applied load is consumed

  if (s.fixtures.empty()) {
    blocker = "Pick at least one fixed face.";
    return false;
  }
  if (s.loads.empty()) {
    blocker = "Add a force or pressure load.";
    return false;
  }
  bool driving = false;
  for (int i = 0; i < (int)s.loads.size(); ++i) {
    const SimLoad &ld = s.loads[i];
    if (!nativeLoadHasFaces(ld)) {
      blocker = "Pick faces for load " + std::to_string(i + 1) + ".";
      return false;
    }
    if (ld.pressure) {
      if (!finite(ld.pressureMpa)) {
        blocker = "Set a finite pressure.";
        return false;
      }
      driving = driving || ld.pressureMpa != 0.0f;
    } else {
      const double mag2 = (double)ld.vec[0] * ld.vec[0] +
                          (double)ld.vec[1] * ld.vec[1] +
                          (double)ld.vec[2] * ld.vec[2];
      if (!finite(mag2)) {
        blocker = "Give the force a finite vector.";
        return false;
      }
      driving = driving || mag2 > 0.0;
    }
  }
  if (!driving) {
    blocker = "Give at least one load a non-zero force or pressure.";
    return false;
  }
  if (s.type == StudyType::Fatigue) {
    const int mi = SimManager::materialIndexByKey(s.materialKey);
    double matSut = mi >= 0 ? SimManager::materials()[mi].ultimate_mpa : 0.0;
    if (s.definition.is_object() && s.definition.contains("material"))
      finiteNumber(s.definition["material"], "ultimate_mpa", matSut);
    if (s.definition.is_object() && s.definition.contains("fatigue"))
      finiteNumber(s.definition["fatigue"], "ultimate_mpa", matSut);
    if ((!finite(s.fatigueSut) || s.fatigueSut <= 0.0f) &&
        (!finite(matSut) || matSut <= 0.0)) {
      blocker = "Enter an ultimate strength (Sut).";
      return false;
    }
    float lmin = 0.0f, lmax = 0.0f;
    fatigueLoadFactors(s, lmin, lmax);
    if (!finite(lmin) || !finite(lmax) || lmax <= lmin) {
      blocker = "Make the maximum load factor exceed the minimum.";
      return false;
    }
  }
  return true;
}
} // namespace simmgr

} // namespace ui
