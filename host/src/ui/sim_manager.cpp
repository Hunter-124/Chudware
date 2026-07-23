// SimManager implementation — see sim_manager.h.
#include "sim_manager.h"

#include <algorithm>
#include <array>
#include <cfloat>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <map>
#include <sstream>
#include <thread>
#include <unordered_set>

#include "imgui.h"

#include "../bootstrap.h"
#include "../core/app_state.h"
#include "../core/sim_client.h"
#include "../render/aicm_loader.h" // parse the flow overlay's streamline / slice LINS chunks
#include "../renderer.h"
#include "../ui_theme.h"
#include "appearance_common.h" // materialAppearance / kPhysicalMaterials — set_material recolour
#include "engine_backend.h" // engine::daemonMode — server-owned export paths
#include "feature_tree.h" // fmRowPill/fmTreeIcon/fmSectionDivider — FM-grade row styling
#include "icons.h"
#include "metrics.h"
#include "property_manager.h" // PropertyManager::active() — mutual-exclusion pick guard
#include "util/aicm_handoff.h" // is_shm_uri — overlay paths are aicm-shm:// handoff URIs, not files
#include "widgets/ribbon.h" // setRibbonDisabledPrefix — EM tab gating

namespace ui {

// ---------------------------------------------------------------- small
// helpers

// A hover tooltip on the previous item (SW options are ambiguous — every field
// gets one).
static void simTip(const char *tip) {
  if (tip && *tip && ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled))
    ImGui::SetTooltip("%s", tip);
}

// ---- liquid-glass v2: shared FeatureManager-column surface (host contract
// §H4) ---------------- The Simulation tree body is drawn EMBEDDED inside
// drawLeftPane's tab child (layout.cpp), which already painted ONE glass
// surface for the whole pane under panelId("host_left_glass"). Mirror
// feature_tree.cpp's fmSurf(): fetch that SAME surface (same id + group, every
// frame — the adaptivePanel cache contract) for adaptive, backdrop-tracking
// text + row pills. The rect passed here is moot (layout.cpp paints the id
// first this frame) but supplied for the cache signature.
static const ui::AdaptiveSurface &simSurf() {
  ImVec2 wp = ImGui::GetWindowPos(), ws = ImGui::GetWindowSize();
  ImRect r(wp, ImVec2(wp.x + ws.x, wp.y + ws.y));
  return ui::adaptivePanel(ui::panelId("host_left_glass"), r,
                           ui::GlassGroup::FeatureManager);
}
// Frosted pill behind one embedded row/card at the current cursor (full content
// width), mirroring feature_tree.cpp's fmRowPill. Call immediately BEFORE the
// row's widgets. `rows` extends the pill over multiple frame-heights (a small
// card block); `selected` draws the restrained accent card.
static void simRowPill(bool selected = false, float rows = 1.0f,
                       float rounding = 3.5f) {
  const float rowH = ImGui::GetFrameHeight() * rows;
  ImVec2 rowMin = ImGui::GetCursorScreenPos();
  float availW = ImGui::GetContentRegionAvail().x;
  ImVec2 rowMax(rowMin.x + availW, rowMin.y + rowH);
  ui::glassRowBackground(ImGui::GetWindowDrawList(), rowMin, rowMax, rounding,
                         false, selected, selected ? 0.42f : 0.26f);
}

// v2 checklist-card frame: replaces the old pushGlassChildStyle (theme-static
// dark glassBg fill, which forced a text re-pair) with a transparent
// auto-resizing child fronted by a frosted glass PILL painted behind it — one
// coherent glass family with the pane, no double-frost, and the pane's adaptive
// ink stays correct (no per-card text push). Split the draw list so the pill
// lands behind the child's content; the caller draws content between begin/end
// and reads the returned visibility exactly like BeginChild. Pair begin/end
// unconditionally.
static ImDrawList *s_ckDL = nullptr;
static ImVec2 s_ckP0;
static bool beginCheckCard(const char *id) {
  const float S = ui::uiScale;
  s_ckDL = ImGui::GetWindowDrawList();
  s_ckP0 = ImGui::GetCursorScreenPos();
  s_ckDL->ChannelsSplit(2);
  s_ckDL->ChannelsSetCurrent(1); // content above the pill
  ImGui::PushStyleColor(ImGuiCol_ChildBg,
                        ImVec4(0, 0, 0, 0)); // transparent — pill is the fill
  ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(8.0f * S, 6.0f * S));
  return ImGui::BeginChild(id, ImVec2(0.0f, 0.0f),
                           ImGuiChildFlags_AutoResizeY |
                               ImGuiChildFlags_AlwaysUseWindowPadding);
}
static void endCheckCard() {
  ImGui::EndChild();
  ImGui::PopStyleVar();
  ImGui::PopStyleColor();
  const float w = ImGui::GetItemRectSize().x;
  const ImVec2 p1(s_ckP0.x + w, ImGui::GetItemRectMax().y);
  s_ckDL->ChannelsSetCurrent(0); // pill behind the content
  ui::glassRowBackground(s_ckDL, s_ckP0, p1, 6.0f, false, false, 0.30f);
  s_ckDL->ChannelsMerge();
}

// ---- card-surface text pairing
// -------------------------------------------------------------- v2: the banner
// / checklist cards now fill with the ADAPTIVE frosted surface
// (glassRowBackground pills over the pane's live-backdrop glass — see
// simRowPill), so their text is the pane's own adaptive ink
// (simSurf().textPrimary, dark-on-light over the light viewport). Only the
// scopes still on the THEME glass surface (BeginPopup / the custom-material
// modal — floating popups do not inherit the pane's frosted backdrop) pair
// against palette.glassBg via glassCardText(). So: pushCardText() below is used
// ONLY in those popup scopes now.
static ImVec4 glassCardText() {
  // The popup/modal consumers fill with the OPAQUE theme glassBg
  // (ImGuiCol_PopupBg, ~0.87a dark) regardless of the viewport backdrop — they
  // are NOT the frosted column — so text must always be contrasted against
  // glassBg (a light ink). Do NOT substitute the pane's adaptive (dark) ink
  // here or the popup text goes dark-on-dark = invisible over a light model.
  const float *g = ui::palette.glassBg;
  return ui::maxContrastTextOn(ImVec4(g[0], g[1], g[2], 1.0f));
}
static void pushCardText() {
  ImVec4 t = glassCardText();
  ImGui::PushStyleColor(ImGuiCol_Text, t);
  t.w = 0.62f;
  ImGui::PushStyleColor(ImGuiCol_TextDisabled, t);
}
static void popCardText() { ImGui::PopStyleColor(2); }
// Dim/secondary text for the CURRENT scope: derives from whatever Text color is
// active (adaptive pane ink outside the cards, card ink inside), instead of
// palette.textDim — which only pairs with theme surfaces and washes out on the
// frosted adaptive pane.
static void pushDimText() {
  ImVec4 t = ImGui::GetStyleColorVec4(ImGuiCol_Text);
  t.w *= 0.62f;
  ImGui::PushStyleColor(ImGuiCol_Text, t);
}
static void popDimText() { ImGui::PopStyleColor(1); }
// Semantic status colors as ImVec4 (palette roles carry float[4]).
static ImVec4 simRole(const float c[4]) {
  return ImVec4(c[0], c[1], c[2], c[3]);
}

// Compose the incoming-flow unit vector from a study's base axis + AoA +
// sideslip (defined below; used by both the setup form readout and the viewport
// setup preview).
static void flowIncomingDir(const Study &s, float out[3]);

// Thermal is a flow-family study (it runs the same OpenFOAM convection/CHT
// pipeline as Flow, just always with heat transfer on) — these share STL
// export, the run path, and result rendering.
static bool isFlowFamily(StudyType t) {
  return t == StudyType::Flow || t == StudyType::Thermal;
}

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
  default:
    return "static";
  }
}
static StudyType studyTypeFromKey(const std::string &k) {
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
  return StudyType::Static;
}
static const char *studyTypeLabel(StudyType t) {
  switch (t) {
  case StudyType::Modal:
    return "Frequency";
  case StudyType::Flow:
    return "Flow";
  case StudyType::Fatigue:
    return "Fatigue";
  case StudyType::Thermal:
    return "Thermal";
  case StudyType::Motion:
    return "Motion";
  case StudyType::ThermalFea:
    return "Thermal (FEA)";
  case StudyType::Buckling:
    return "Buckling";
  case StudyType::Contact:
    return "Contact";
  case StudyType::FreqResponse:
    return "Vibration";
  default:
    return "Static";
  }
}

// Study-type icon name for the atlas (study_* per the icon contract).
// Referencing by string is compile-decoupled; callers guard on IconUV::valid so
// a missing atlas entry degrades to no icon.
static const char *studyTypeIcon(StudyType t) {
  switch (t) {
  case StudyType::Modal:
    return "study_modal";
  case StudyType::Flow:
    return "study_flow";
  case StudyType::Fatigue:
    return "study_fatigue";
  case StudyType::Thermal:
    return "study_thermal";
  case StudyType::Motion:
    return "rotate_component"; // no dedicated study_motion sprite; the
                               // rotation-gizmo glyph reads as motion and
                               // is a real atlas icon (picker + study tree)
  case StudyType::ThermalFea:
    return "study_thermal";
  case StudyType::Buckling:
    return "study_static";
  case StudyType::Contact:
    return "study_static";
  case StudyType::FreqResponse:
    return "study_modal";
  default:
    return "study_static";
  }
}

// Frosted-glass backing for a popup, so the "New study" picker floats as part
// of the app-wide glass family (matching the drawing-mode dialogs + section
// tool) instead of ImGui's flat legacy PopupBg. Call once right after
// BeginPopup() succeeds (pair with SetNextWindowBgAlpha(0) BEFORE BeginPopup);
// finish() paints the frosted panel behind the content and merges the split.
struct GlassPopupScope {
  ImDrawList *dl = nullptr;
  const char *id = nullptr;
  void finish() {
    const float s = ui::uiScale;
    ImVec2 p0 = ImGui::GetWindowPos();
    ImVec2 p1(p0.x + ImGui::GetWindowSize().x, p0.y + ImGui::GetWindowSize().y);
    dl->ChannelsSetCurrent(0);
    const ui::AdaptiveSurface &surf =
        ui::adaptivePanel(ui::panelId(id), ImRect(p0, p1), ui::GlassGroup::Hud);
    ui::glassPanel(dl, ImRect(p0, p1), ui::GlassLevel::Regular,
                   ImGui::GetStyle().PopupRounding / s, surf);
    dl->ChannelsMerge();
  }
};
static GlassPopupScope beginGlassPopup(const char *panelId) {
  GlassPopupScope sc;
  sc.dl = ImGui::GetWindowDrawList();
  sc.id = panelId;
  sc.dl->ChannelsSplit(2);
  sc.dl->ChannelsSetCurrent(1);
  return sc;
}

// One-line study-type intent banner on a translucent glass card (type icon +
// description). Part of the UI overhaul's glassmorphism pass
// (ui::glassBackground); falls back to plain text when the icon atlas lacks the
// study_* name. Drawn at the top of the Study overview so each type announces
// what it solves and which inputs apply.
static void drawStudyTypeBanner(StudyType t, const char *desc) {
  const float S = ui::uiScale;
  ImDrawList *dl = ImGui::GetWindowDrawList();
  const ImVec2 pad(9.0f * S, 7.0f * S);
  const ImVec2 p0 = ImGui::GetCursorScreenPos();
  const float w = ImGui::GetContentRegionAvail().x;
  dl->ChannelsSplit(2);
  dl->ChannelsSetCurrent(1); // content sits above the card
  // v2: no local text re-pair — the banner sits on a frosted glass PILL (drawn
  // below) over the pane's adaptive backdrop, so drawTreeBody/drawForm's
  // pane-level adaptive ink is already right.
  ImGui::Dummy(ImVec2(0, pad.y));
  ImGui::Indent(pad.x);
  IconUV iv = getIcon(studyTypeIcon(t), 20);
  if (iv.valid) {
    ImGui::Image(iv.texId, ImVec2(18.0f * S, 18.0f * S), iv.uv0, iv.uv1);
    ImGui::SameLine(0, 7.0f * S);
  }
  ImGui::PushTextWrapPos(p0.x + w - pad.x);
  ImGui::TextWrapped("%s", desc);
  ImGui::PopTextWrapPos();
  ImGui::Unindent(pad.x);
  ImGui::Dummy(ImVec2(0, pad.y));
  const ImVec2 p1(p0.x + w, ImGui::GetCursorScreenPos().y);
  dl->ChannelsSetCurrent(0); // glass card behind the content
  ui::glassRowBackground(dl, p0, p1, 8.0f, false, false,
                         0.30f); // frosted pill (v2)
  dl->ChannelsMerge();
  ImGui::Spacing();
}

// allowHex=false forces tetrahedral (e.g. modal studies have no hex8 mass
// matrix).
static void addMeshParams(nlohmann::json &j, const Study &s, int meshOrder,
                          bool allowHex = true) {
  j["fineness"] = s.meshFineness; // primary control (0=coarse..1=fine)
  const bool hex = allowHex && s.meshHex;
  j["structured"] = hex;
  j["mesh_type"] = hex ? "hex" : "tet";
  j["mesh_order"] = meshOrder;
  j["order"] =
      meshOrder; // direct sim_mesh accepts both; keep it self-describing.
  j["curvature"] = s.meshCurvature;
  j["curv_elems_per_circle"] = s.meshCurvPerCircle;
  if (s.meshTargetMm > 0.0f)
    j["target_size_mm"] = s.meshTargetMm;
  if (s.meshMinMm > 0.0f)
    j["min_size_mm"] = s.meshMinMm;
  j["algorithm3d"] = s.meshAlgo;
  j["optimize"] = s.meshOptimize;
}

static void clearTransientResult(Study &s) {
  s.hasResult = false;
  s.summary.clear();
  s.banner.clear();
  s.resultSummary = nlohmann::json::object();
  s.overlayPath.clear();
  s.freqs.clear();
  s.dragTrace.clear();
  s.dragN = 0.0;
  s.cd = 0.0;
  s.frontalMm2 = 0.0;
  s.solverNotes.clear();
  // Motion study transient results.
  s.motionHasResult = false;
  s.motionPlaying = false;
  s.motionFrame = 0;
  s.motionLastTick = -1.0;
  s.motionTrajectory = nlohmann::json::array();
  s.motionBodyNames.clear();
  s.motionDriverUnit.clear();
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
  // Ensure the directory exists, then write (best-effort).
  try {
    std::filesystem::create_directories(
        std::filesystem::path(customMaterialsPath()).parent_path());
  } catch (...) {
  }
  std::ofstream o(customMaterialsPath(), std::ios::binary | std::ios::trunc);
  if (o)
    o << j.dump(2);
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

// Read the active part's material key from DocMirror (doc["material"]["key"]).
// Empty when unset.
static std::string docMaterialKey(const AppState &st) {
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
static void resolveStudyMaterialFromDoc(AppState &st, Study &s) {
  if (isFlowFamily(s.type))
    return; // Flow/Thermal never consume materialKey
  s.materialKey = docMaterialKey(st);
}

// Write set_material (+ derived set_appearance recolour) to the ACTIVE part via
// DocMirror, same seam the FeatureManager Material menu uses. Also mirrors the
// key onto the study and persists it so the study JSON stays in sync for
// reloads. No-op without a live doc/worker.
static void applyPartMaterial(AppState &st, Study &s, const std::string &key) {
  s.materialKey = key;
  if (!st.docMirror.hasDoc() || !st.workerStarted) {
    // Still persist the study-local mirror so a later open has something.
    return;
  }
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
  ++st.rebuildsInFlight;
  st.docMirror.applyOpsAsync(
      st.rpc, st.worker, ops,
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

// ---------------------------------------------------------------- result
// fields

namespace {
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
const ResultField kStaticFields[] = {
    {"von_mises", "von Mises (distortion energy)", 0},
    {"tresca", "Tresca (max shear stress)", 0},
    {"abs_principal", "Max normal stress", 0},
    {"p1", "1st principal (P1)", 0},
    {"p3", "3rd principal (P3)", 0},
    {"max_shear", "Max shear", 0},
    {"displacement_mag", "Displacement (URES)", 1},
    {"disp_x", "Displacement X", 1},
    {"disp_y", "Displacement Y", 1},
    {"disp_z", "Displacement Z", 1},
    {"fos", "Factor of safety (von Mises)", 2},
    {"fos_tresca", "Factor of safety (Tresca)", 2},
    {"fos_rankine", "Factor of safety (max normal)", 2},
};
const int kNStaticFields =
    (int)(sizeof(kStaticFields) / sizeof(kStaticFields[0]));

// Fields a FATIGUE study's overlay carries (kind 2 = dimensionless).
const ResultField kFatigueFields[] = {
    {"fatigue_fos", "Fatigue factor of safety", 2},
    {"life_log10", "Life (log10 cycles)", 2},
    {"von_mises", "Peak von Mises stress", 0},
};
const int kNFatigueFields =
    (int)(sizeof(kFatigueFields) / sizeof(kFatigueFields[0]));

// Fields a FLOW study's overlay carries. kind 3 = velocity (m/s,
// unit-agnostic); kind 2 = dimensionless.
const ResultField kFlowFields[] = {
    {"pressure_cp", "Pressure coefficient (Cp)", 2},
    {"temperature", "Temperature (degC)", 3}, // OpenFOAM thermal studies
    {"velocity", "Velocity (slices)", 3},
};
const int kNFlowFields = (int)(sizeof(kFlowFields) / sizeof(kFlowFields[0]));

// True if a flow result actually carries `chunk` (from the worker's
// scalar_fields). An empty list means a legacy result with unknown fields —
// show everything rather than hide all.
static bool flowFieldPresent(const std::vector<std::string> &fields,
                             const char *chunk) {
  if (fields.empty())
    return true;
  for (const auto &f : fields)
    if (f == chunk)
      return true;
  return false;
}

// Fluid presets: {label, density kg/m^3, kinematic viscosity m^2/s}. The last
// entry (Custom) is a sentinel — its density/viscosity come from the study's
// editable fields instead.
struct FluidPreset {
  const char *label;
  float density;
  float visc;
};
static const FluidPreset kFluids[3] = {
    {"Air (sea level, 15 C)", 1.225f, 1.5e-5f},
    {"Water (fresh, 20 C)", 998.0f, 1.0e-6f},
    {"Custom", 1.225f, 1.5e-5f},
};
static const int kNFluids = 3;

// Surface-roughness presets: equivalent sand-grain roughness Ks in micrometres.
// Last entry = Custom.
struct RoughPreset {
  const char *label;
  float ks_um;
};
static const RoughPreset kRoughness[6] = {
    {"Smooth / polished", 0.0f},
    {"Smooth paint / gelcoat", 6.0f},
    {"Bare aluminium / sheet metal", 30.0f},
    {"Galvanised / rough paint", 150.0f},
    {"Cast / rough composite", 500.0f},
    {"Custom (um)", 0.0f},
};
static const int kNRoughness = 6;

// Resolve a study's fluid choice to concrete density + kinematic viscosity
// (SI).
static void flowFluidProps(const ui::Study &s, float &density, float &visc) {
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
static float flowRoughnessUm(const ui::Study &s) {
  int ri = (s.flowRoughness < 0 || s.flowRoughness >= kNRoughness)
               ? 0
               : s.flowRoughness;
  return (ri == kNRoughness - 1) ? s.flowRoughnessUm : kRoughness[ri].ks_um;
}

// Display label + unit string + SI->display factor for a named field, honoring
// the active unit system.
void fieldMeta(const AppState &st, const std::string &chunk, std::string &label,
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
    unit = "degC";
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
const char *kTheoryKeys[5] = {"goodman", "gerber", "soderberg", "asme",
                              "morrow"};
const char *kTheoryNames[5] = {"Goodman", "Gerber", "Soderberg",
                               "ASME-elliptic", "Morrow"};

// Marin surface factor ka = A*Sut^b (capped at 1). 0 ground,1 machined,2
// hot-rolled,3 as-forged.
double marin_ka(double Sut, int surface) {
  static const double A[4] = {1.58, 4.51, 57.7, 272.0};
  static const double b[4] = {-0.085, -0.265, -0.718, -0.995};
  int i = (surface < 0 || surface > 3) ? 1 : surface;
  double ka = A[i] * std::pow(Sut, b[i]);
  return ka > 1.0 ? 1.0 : ka;
}
// Marin size factor kb (bending/torsion only; axial = 1). d in mm.
double marin_kb(double d_mm, int loadCase) {
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
double marin_kc(int loadCase) {
  return loadCase == 1 ? 0.85 : loadCase == 2 ? 0.59 : 1.0;
}
// Marin temperature factor kd (Shigley table, interpolated; C).
double marin_kd(double T_c) {
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
double reliability_ke(double Rpct) {
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
double sn_life(double sa_eq, double Se, double Sut) {
  if (Se <= 0 || Sut <= 0 || sa_eq <= 0)
    return 1e6;
  if (sa_eq <= Se)
    return 1e6;
  const double f = 0.9, a = (f * Sut) * (f * Sut) / Se,
               b = -(1.0 / 3.0) * std::log10((f * Sut) / Se);
  double N = std::pow(sa_eq / a, 1.0 / b);
  return N < 1 ? 1 : (N > 1e6 ? 1e6 : N);
}
void fatigueLoadFactors(const Study &s, float &lmin, float &lmax) {
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
double seBase(const Study &s, double matSe, double Sut) {
  if (s.fatigueSe > 0)
    return s.fatigueSe;
  if (matSe > 0)
    return matSe;
  return std::min(0.5 * Sut, 700.0);
}
} // namespace

// ---------------------------------------------------------------- ctor

SimManager::SimManager() {}

// ---------------------------------------------------------------- JSON <->
// Study

nlohmann::json SimManager::studyToJson(const Study &s) {
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
    loads.push_back(l);
  }
  return nlohmann::json{
      {"id", s.id},
      {"type", studyTypeKey(s.type)},
      {"name", s.name},
      {"material", s.materialKey},
      {"fixtures", fixtures},
      {"loads", loads},
      {"mesh_fineness", s.meshFineness},
      {"mesh_hex", s.meshHex},
      {"mesh_curvature", s.meshCurvature},
      {"mesh_curv_per_circle", s.meshCurvPerCircle},
      {"mesh_order", s.meshOrder},
      {"mesh_target_mm", s.meshTargetMm},
      {"mesh_min_mm", s.meshMinMm},
      {"mesh_algo", s.meshAlgo},
      {"mesh_optimize", s.meshOptimize},
      {"n_modes", s.nModes},
      {"criterion", (int)s.criterion},
      {"flow",
       {{"dir", s.flowDir},
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
        {"lmin", s.fatigueLmin},
        {"lmax", s.fatigueLmax},
        {"se_mpa", s.fatigueSe},
        {"sut_mpa", s.fatigueSut},
        {"surface", s.fatigueSurface},
        {"load_case", s.fatigueLoadCase},
        {"size_mm", s.fatigueSizeMm},
        {"temp_c", s.fatigueTempC},
        {"reliability", s.fatigueReliability}}},
      {"motion",
       {{"mate_id", s.motionMateId},
        {"start", s.motionStart},
        {"stop", s.motionStop},
        {"steps", s.motionSteps},
        {"loop", s.motionLoop},
        {"fps", s.motionFps},
        {"check_interference", s.motionCheckInterference}}},
      // T-FIN24 study kinds (buckling/contact proxies, frequency response,
      // thermal FEA).
      {"adv",
       {{"buckling_load_n", s.bucklingLoadN},
        {"contact_pressure_mpa", s.contactPressureMpa},
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
      {"result_summary", s.summary},
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
  s.id = j.value("id", std::string());
  s.type = studyTypeFromKey(j.value("type", std::string("static")));
  s.name = j.value("name", std::string("Study"));
  s.materialKey = j.value("material", std::string());
  // Back-compat: old studies persisted "mesh_density" (same higher=finer sense
  // as fineness).
  s.meshFineness = j.value("mesh_fineness", j.value("mesh_density", 0.5f));
  s.meshHex = j.value("mesh_hex", false);
  s.meshCurvature = j.value("mesh_curvature", true);
  s.meshCurvPerCircle = j.value("mesh_curv_per_circle", 12.0f);
  s.meshOrder = j.value("mesh_order", 2);
  s.meshTargetMm = j.value("mesh_target_mm", 0.0f);
  s.meshMinMm = j.value("mesh_min_mm", 0.0f);
  s.meshAlgo = j.value("mesh_algo", 1);
  s.meshOptimize = j.value("mesh_optimize", true);
  s.nModes = j.value("n_modes", 6);
  s.criterion = (FailureCriterion)j.value("criterion", 0);
  if (j.contains("flow") && j["flow"].is_object()) {
    const auto &fl = j["flow"];
    s.flowDir = fl.value("dir", 0);
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
    s.fatigueLoadType = fa.value("load_type", 0);
    s.fatigueRatio = fa.value("ratio", -1.0);
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
    s.bucklingLoadN = av.value("buckling_load_n", 1000.0f);
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
  if (j.contains("motion") && j["motion"].is_object()) {
    const auto &mo = j["motion"];
    s.motionMateId = mo.value("mate_id", std::string());
    s.motionStart = mo.value("start", 0.0f);
    s.motionStop = mo.value("stop", 360.0f);
    s.motionSteps = mo.value("steps", 24);
    s.motionLoop = mo.value("loop", true);
    s.motionFps = mo.value("fps", 24.0f);
    s.motionCheckInterference = mo.value("check_interference", false);
  }
  s.summary = j.value("result_summary", std::string());
  if (j.contains("fixtures") && j["fixtures"].is_array())
    for (const auto &sel : j["fixtures"])
      s.fixtures.picks.push_back(selFromJson(sel));
  if (j.contains("loads") && j["loads"].is_array())
    for (const auto &ld : j["loads"]) {
      SimLoad l;
      l.pressure = ld.value("type", std::string("force")) == "pressure";
      if (l.pressure)
        l.pressureMpa = ld.value("magnitude", 0.1);
      else if (ld.contains("vec") && ld["vec"].is_array() &&
               ld["vec"].size() == 3)
        for (int k = 0; k < 3; ++k)
          l.vec[k] = ld["vec"][k].get<float>();
      l.thermalKind = ld.value("thermal_kind", 0);
      l.fluxWm2 = ld.value("flux_w_m2", 1000.0f);
      l.hWm2k = ld.value("h_w_m2k", 10.0f);
      l.tAmbC = ld.value("t_amb_c", 25.0f);
      if (ld.contains("faces") && ld["faces"].is_array())
        for (const auto &sel : ld["faces"])
          l.faces.picks.push_back(selFromJson(sel));
      s.loads.push_back(std::move(l));
    }
  return s;
}

// ---------------------------------------------------------------- refresh /
// mirror

void SimManager::refresh(AppState &st) {
  if (!st.docMirror.hasDoc()) {
    studies_.clear();
    sel_ = -1;
    return;
  }
  nlohmann::json res;
  std::string err;
  if (!st.worker.call("sim_study_list", {{"doc_id", st.docMirror.docId()}}, res,
                      err, 15000)) {
    st.addLog("[sim] sim_study_list failed: " + err);
    return;
  }
  if (res.contains("code"))
    return;
  // Preserve the transient (non-persisted) result fields across a refresh by
  // id.
  std::vector<Study> prev = std::move(studies_);
  studies_.clear();
  if (res.contains("studies") && res["studies"].is_array())
    for (const auto &j : res["studies"]) {
      Study s = studyFromJson(j);
      for (const auto &p : prev)
        if (p.id == s.id) {
          s.hasResult = p.hasResult;
          s.banner = p.banner;
          s.dragTrace = p.dragTrace;
          s.resultSummary = p.resultSummary;
          s.overlayPath = p.overlayPath;
          s.nNodes = p.nNodes;
          s.nElems = p.nElems;
          s.nDof = p.nDof;
          s.freqs = p.freqs;
          s.dragN = p.dragN;
          s.cd = p.cd;
          s.frontalMm2 = p.frontalMm2;
          s.solverNotes = p.solverNotes;
          if (!p.summary.empty())
            s.summary = p.summary;
          // Motion transient result
          s.motionHasResult = p.motionHasResult;
          s.motionPlaying = p.motionPlaying;
          s.motionFrame = p.motionFrame;
          s.motionTrajectory = p.motionTrajectory;
          s.motionDriverUnit = p.motionDriverUnit;
          s.motionBodyNames = p.motionBodyNames;
          s.motionPlotBody = p.motionPlotBody;
          break;
        }
      studies_.push_back(std::move(s));
    }
  lastDocId_ = st.docMirror.docId();
  lastRevision_ = (long)st.docMirror.revision();
  if (sel_ >= (int)studies_.size())
    sel_ = studies_.empty() ? -1 : (int)studies_.size() - 1;
}

void SimManager::persist(AppState &st, Study &s) {
  if (s.id.empty() || !st.docMirror.hasDoc())
    return;
  nlohmann::json res;
  std::string err;
  nlohmann::json params = {{"doc_id", st.docMirror.docId()},
                           {"study_id", s.id},
                           {"study", studyToJson(s)}};
  if (!st.worker.call("sim_study_edit", params, res, err, 15000) ||
      res.contains("code")) {
    st.addLog("[sim] sim_study_edit failed: " +
              (res.contains("code") ? res.value("message", err) : err));
    return;
  }
  st.markActiveTabDirty();
}

void SimManager::newStudy(AppState &st, StudyType t) {
  if (!st.docMirror.hasDoc()) {
    st.toast(ui::ToastLevel::Warn, "Open a part before creating a study");
    return;
  }
  refresh(st);
  Study s;
  s.type = t;
  s.name = std::string(studyTypeLabel(t)) + " " +
           std::to_string((int)studies_.size() + 1);
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
  nlohmann::json res;
  std::string err;
  nlohmann::json params = {{"doc_id", st.docMirror.docId()},
                           {"study", studyToJson(s)}};
  if (!st.worker.call("sim_study_add", params, res, err, 15000) ||
      res.contains("code")) {
    st.addLog("[sim] sim_study_add failed: " +
              (res.contains("code") ? res.value("message", err) : err));
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
  nlohmann::json res;
  std::string err;
  if (!st.worker.call("sim_study_delete",
                      {{"doc_id", st.docMirror.docId()}, {"study_id", id}}, res,
                      err, 15000) ||
      res.contains("code")) {
    st.addLog("[sim] sim_study_delete failed: " +
              (res.contains("code") ? res.value("message", err) : err));
    return;
  }
  st.markActiveTabDirty();
  clearResultOverlay(st);
  armedWell_ = -1;
  refresh(st);
  if (sel_ >= (int)studies_.size())
    sel_ = studies_.empty() ? -1 : (int)studies_.size() - 1;
  node_ = SimNode::Study;
}

// ---------------------------------------------------------------- ribbon
// commands

bool SimManager::handleCommand(AppState &st, const std::string &cmdId) {
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
#ifdef __EMSCRIPTEN__
    if (isFlowFamily(t) || t == StudyType::Buckling ||
        t == StudyType::Contact || t == StudyType::FreqResponse) {
      st.toast(ui::ToastLevel::Warn, "not available in the web version yet");
      return true;
    }
#endif
    newStudy(st, t);
    if (sel_ >= 0) {
      node_ = SimNode::Study;
      panelOpen_ = true;
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
  refresh(st);

  // Per-type setup tools (the per-type ribbon tabs): target a study of the
  // tab's type — keep the current selection when it already matches, else the
  // first study of that type. All of them open the study-level setup panel (the
  // Study node hosts the type's form).
  struct TypedCmd {
    const char *id;
    StudyType t;
  };
  static const TypedCmd kTypedSetup[] = {
      {"sim_modal_options", StudyType::Modal},
      {"sim_fluid", StudyType::Flow},
      {"sim_flow_bc", StudyType::Flow},
      {"sim_flow_res", StudyType::Flow},
      {"sim_thermal_bc", StudyType::Thermal},
      {"sim_fatigue_setup", StudyType::Fatigue},
      {"sim_motion_setup", StudyType::Motion},
  };
  for (const auto &tc : kTypedSetup) {
    if (cmdId != tc.id)
      continue;
    st.leftPaneTab = AppState::LP_Simulation;
    if (!selectStudyOfType(tc.t)) {
      st.toast(ui::ToastLevel::Warn,
               std::string("Create a ") + studyTypeLabel(tc.t) +
                   " study first (\"New Study\" on this tab).");
      return true;
    }
    node_ = SimNode::Study;
    loadIdx_ = -1;
    armedWell_ = -1;
    panelOpen_ = true;
    return true;
  }
  const bool haveStudy = sel_ >= 0 && sel_ < (int)studies_.size();
  // Material / Results / Mesh: open the pane on the matching sub-node of the
  // active study. Mesh also actually MESHES the part and shows the element
  // wireframe (the toolbar button used to be a no-op — it now does what "Mesh"
  // implies).
  if (cmdId == "sim_material" || cmdId == "sim_results" ||
      cmdId == "sim_mesh") {
    st.leftPaneTab = AppState::LP_Simulation;
    if (!haveStudy) {
      st.addLog("[sim] create a study first");
      return true;
    }
    Study &s = studies_[sel_];
    armedWell_ = -1;
    if (cmdId == "sim_material")
      node_ = SimNode::Material;
    else if (cmdId == "sim_results")
      node_ = SimNode::Results;
    else {
      node_ = SimNode::Mesh;
      const bool busy = st.simClient && st.simClient->busy();
      if (!busy)
        previewMesh(st, s); // mesh now + show the wireframe (no solve)
    }
    panelOpen_ = true; // tool invoked -> its setup panel opens (PM-style)
    return true;
  }
  if (!haveStudy) {
    st.leftPaneTab = AppState::LP_Simulation;
    st.addLog("[sim] create a study first (Simulation tab > New "
              "Static/Frequency/Flow)");
    return true;
  }
  Study &s = studies_[sel_];
  // Frequency (modal) eigenvalue solves ignore loads entirely — a load added
  // here would be silently discarded on run (runStaticOrModal never sends modal
  // loads). Block the load commands with a teaching toast so the user learns to
  // constrain rigid-body modes with fixtures instead.
  if ((cmdId == "sim_load_force" || cmdId == "sim_load_pressure") &&
      s.type == StudyType::Modal) {
    st.toast(ui::ToastLevel::Warn, "Frequency studies don't use loads — add "
                                   "fixtures to constrain rigid-body modes.");
    return true;
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
    node_ = SimNode::Fixtures;
    armedWell_ = 0;
    st.leftPaneTab = AppState::LP_Simulation;
    panelOpen_ = true;
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
    node_ = SimNode::Load;
    loadIdx_ = (int)s.loads.size() - 1;
    armedWell_ = (int)s.loads.size();
    st.leftPaneTab = AppState::LP_Simulation;
    panelOpen_ = true;
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
    node_ = SimNode::Load;
    loadIdx_ = (int)s.loads.size() - 1;
    armedWell_ = (int)s.loads.size();
    st.leftPaneTab = AppState::LP_Simulation;
    panelOpen_ = true;
    return true;
  }
  if (cmdId == "sim_run") {
    st.leftPaneTab = AppState::LP_Simulation;
    panelOpen_ = true; // show the run progress / result summary panel
    run(st, s);
    return true;
  }
  return false;
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

// ---------------------------------------------------------------- EM
// (electromagnetics) gating
//
// The EM ribbon tab's tools call em_* worker RPCs owned by a parallel backend
// agent. Until that backend lands (or on installs without the solver),
// em_status is probed ONCE asynchronously; a missing backend greys the whole
// sim_em_* command set with an honest "EM solver not installed" tooltip
// (ui::setRibbonDisabledPrefix) — no fake buttons, no silent no-ops.
void SimManager::probeEmStatus(AppState &st) {
  if (emStatus_ != 0 || emProbeInflight_)
    return;
  emProbeInflight_ = true;
  st.rpc.call(
      "em_status", nlohmann::json::object(),
      [this](const core::RpcResult &r) {
        emProbeInflight_ = false;
        // Protocol-2 method errors arrive ok=true with result.code set — an
        // unknown method / missing solver both count as "not installed".
        const bool available =
            r.ok && !r.result.contains("code") &&
            r.result.value("installed", r.result.value("available", true));
        emStatus_ = available ? 1 : -1;
        if (available)
          emStatusResult_ = r.result;
        if (emStatus_ < 0)
          ui::setRibbonDisabledPrefix("sim_em_", "EM solver not installed");
        else
          ui::setRibbonDisabledPrefix("", "");
      },
      5000);
}

bool SimManager::handleEmCommand(AppState &st, const std::string &cmdId) {
  if (emStatus_ == 0) {
    probeEmStatus(st);
    st.toast(ui::ToastLevel::Info,
             "Checking for the electromagnetics solver\xe2\x80\xa6");
    return true;
  }
  if (emStatus_ < 0) {
    st.toast(ui::ToastLevel::Warn, "Electromagnetics solver not installed "
                                   "\xe2\x80\x94 the em_* backend is not "
                                   "available on this machine.");
    return true;
  }
  // Backend present — route each sim_em_* ribbon command to the EM panel
  // (PM-style, takes over the left pane). The three analysis forms open on
  // their own command; the Setup/Run/Results commands act on the currently-open
  // form (mirrors how the study tools target the active study).
  st.leftPaneTab = AppState::LP_Simulation;
  if (cmdId == "sim_em_eigen") {
    openEmForm(st, EmForm::Eigenmode);
    return true;
  }
  if (cmdId == "sim_em_sweep") {
    openEmForm(st, EmForm::Driven);
    return true;
  }
  if (cmdId == "sim_em_time") {
    openEmForm(st, EmForm::TimeDomain);
    return true;
  }

  // Setup helpers fold into the relevant form (there are no separate
  // port/boundary editors — the uniform-section backend takes ports/boundaries
  // implicitly as matched modal ports). Open the form the setting belongs to so
  // the user lands on the right inputs.
  if (cmdId == "sim_em_ports" || cmdId == "sim_em_boundary") {
    if (!emPanelOpen_)
      openEmForm(st, EmForm::Driven);
    else
      emPanelOpen_ = true;
    st.toast(ui::ToastLevel::Info, "EM uses matched modal ports on the guide "
                                   "cross-section \xe2\x80\x94 set the "
                                   "waveguide, length and mode in the form.");
    return true;
  }
  if (cmdId == "sim_em_range") {
    // Frequency range lives on the driven (sweep) form.
    openEmForm(st, EmForm::Driven);
    return true;
  }
  if (cmdId == "sim_em_run") {
    if (!emPanelOpen_) {
      st.toast(
          ui::ToastLevel::Warn,
          "Open an EM analysis first (Eigenmode, Freq Sweep or Time Domain).");
      return true;
    }
    runEmForm(st);
    return true;
  }
  if (cmdId == "sim_em_results") {
    if (!emPanelOpen_)
      openEmForm(st, emForm_);
    else
      emPanelOpen_ = true;
    return true;
  }
  return true;
}

// ---------------------------------------------------------------- EM panel
// (PropertyManager-style)

// EIA rectangular-waveguide standards (name -> broad/narrow wall in mm),
// mirrored from engine_oracle/sim/em/analytic.py STANDARD_WAVEGUIDES. The order
// here defines em_.stdIdx; keep WR-90 at index 7 (the EmState default).
// "Custom" is stdIdx == -1 (handled in the form).
namespace {
struct EmStandard {
  const char *name;
  float a_mm;
  float b_mm;
};
static const EmStandard kEmStandards[] = {
    {"WR-2300", 584.20f, 292.10f},  {"WR-650", 165.10f, 82.55f},
    {"WR-430", 109.22f, 54.61f},    {"WR-284", 72.136f, 34.036f},
    {"WR-187", 47.5488f, 22.1488f}, {"WR-137", 34.8488f, 15.7988f},
    {"WR-112", 28.4988f, 12.6238f}, {"WR-90", 22.86f, 10.16f},
    {"WR-75", 19.05f, 9.525f},      {"WR-62", 15.7988f, 7.8994f},
    {"WR-42", 10.668f, 4.318f},     {"WR-28", 7.112f, 3.556f},
    {"WR-15", 3.7592f, 1.8796f},    {"WR-10", 2.54f, 1.27f},
};
static const int kNEmStandards =
    (int)(sizeof(kEmStandards) / sizeof(kEmStandards[0]));
static const char *kEmModes[] = {"TE10", "TE20", "TE01", "TE11", "TM11"};
static const int kNEmModes = (int)(sizeof(kEmModes) / sizeof(kEmModes[0]));
} // namespace

void SimManager::openEmForm(AppState &st, EmForm f) {
  (void)st;
  emForm_ = f;
  emTabSync_ =
      true; // force the in-panel tab bar to this form on the next draw only
  emPanelOpen_ = true;
  panelOpen_ = false; // EM takes over the pane; close any study setup panel
}

// Build the {standard:"WR-90"} or {waveguide:{shape,a_mm,b_mm|radius_mm}} spec
// from em_.
nlohmann::json SimManager::emWaveguideSpec() const {
  if (em_.stdIdx >= 0 && em_.stdIdx < kNEmStandards)
    return nlohmann::json{{"standard", kEmStandards[em_.stdIdx].name}};
  nlohmann::json wg;
  if (em_.shape == 1) {
    wg = {{"shape", "circular"}, {"radius_mm", em_.radius_mm}};
  } else {
    wg = {{"shape", "rect"}, {"a_mm", em_.a_mm}, {"b_mm", em_.b_mm}};
  }
  return nlohmann::json{{"waveguide", wg}};
}

// Dispatch the active form's em_* RPC asynchronously (st.rpc — the callback
// lands in a later drain() on the GL thread, safe to touch em_). A single
// in-flight guard blocks re-entry.
void SimManager::runEmForm(AppState &st) {
  if (emRunning_)
    return;
  nlohmann::json params = emWaveguideSpec();
  std::string method;
  switch (emForm_) {
  case EmForm::Eigenmode:
    method = "em_eigenmode";
    params["n_modes"] = em_.nModes;
    params["resolution"] = em_.resolution;
    break;
  case EmForm::Driven:
    method = "em_driven";
    params["length_mm"] = em_.lengthMm;
    params["mode"] =
        kEmModes[(em_.modeIdx >= 0 && em_.modeIdx < kNEmModes) ? em_.modeIdx
                                                               : 0];
    params["f_start_ghz"] = em_.fStartGhz;
    params["f_stop_ghz"] = em_.fStopGhz;
    params["n_freq"] = em_.nFreq;
    if (em_.stlPath[0])
      params["geometry"] =
          nlohmann::json{{"stl_path", std::string(em_.stlPath)}};
    break;
  case EmForm::TimeDomain:
    method = "em_timedomain";
    params["length_mm"] = em_.lengthMm;
    params["mode"] =
        kEmModes[(em_.modeIdx >= 0 && em_.modeIdx < kNEmModes) ? em_.modeIdx
                                                               : 0];
    params["f0_ghz"] = em_.f0Ghz;
    params["bw_ghz"] = em_.bwGhz;
    break;
  }
  emRunning_ = true;
  EmForm which = emForm_;
  st.rpc.call(
      method, params,
      [this, which](const core::RpcResult &r) {
        emRunning_ = false;
        const bool ok =
            r.ok && !r.result.contains("code") && r.result.value("ok", true);
        std::string err;
        if (!ok) {
          err = r.result.value(
              "error", r.err.empty() ? std::string("EM solve failed") : r.err);
        }
        // Coerce a JSON number array to float, clamping non-finite entries (a
        // matched-port S11 can come back as +/-inf/NaN from some adapters) so
        // the auto-ranged plot stays sane.
        auto toF = [](const nlohmann::json &a, std::vector<float> &out) {
          out.clear();
          if (!a.is_array())
            return;
          for (const auto &v : a) {
            float f = v.is_number() ? (float)v.get<double>() : 0.0f;
            if (!std::isfinite(f))
              f = (f < 0 || (v.is_number() && v.get<double>() < 0)) ? -300.0f
                                                                    : 300.0f;
            out.push_back(f);
          }
        };
        if (which == EmForm::Eigenmode) {
          em_.eigResult = ok ? 1 : 0;
          em_.eigError = err;
          em_.eigModes = ok ? r.result.value("modes", nlohmann::json::array())
                            : nlohmann::json::array();
        } else if (which == EmForm::Driven) {
          em_.drvResult = ok ? 1 : 0;
          em_.drvError = err;
          if (ok) {
            toF(r.result.value("freq_ghz", nlohmann::json::array()),
                em_.drvFreq);
            toF(r.result.value("s11_db", nlohmann::json::array()), em_.drvS11);
            toF(r.result.value("s21_db", nlohmann::json::array()), em_.drvS21);
          } else {
            em_.drvFreq.clear();
            em_.drvS11.clear();
            em_.drvS21.clear();
          }
        } else {
          em_.tdResult = ok ? 1 : 0;
          em_.tdError = err;
          if (ok) {
            toF(r.result.value("t_ns", nlohmann::json::array()), em_.tdTime);
            const auto &ports =
                r.result.value("ports", nlohmann::json::object());
            toF(ports.value("port1_v", nlohmann::json::array()), em_.tdPort1);
            toF(ports.value("port2_v", nlohmann::json::array()), em_.tdPort2);
          } else {
            em_.tdTime.clear();
            em_.tdPort1.clear();
            em_.tdPort2.clear();
          }
        }
      },
      120000);
}

// Generic two-series line plot (ImDrawList — there is no ImPlot in host/; this
// mirrors the drawFatigueGraphs plotting idiom). x drives the horizontal axis;
// y1/y2 are optional series drawn in the two accent hues, auto-ranged to the
// combined min/max with a small margin.
void SimManager::drawEmLinePlot(const char *id, const std::vector<float> &x,
                                const std::vector<float> *y1, const char *l1,
                                const std::vector<float> *y2, const char *l2,
                                const char *xlabel, const char *ylabel) {
  const float S = ui::uiScale;
  if (x.size() < 2) {
    ImGui::TextDisabled("Not enough samples to plot.");
    return;
  }
  ImDrawList *dl = ImGui::GetWindowDrawList();
  const ImVec4 inkV = ImGui::GetStyleColorVec4(ImGuiCol_Text);
  auto ink = [&](float a) {
    return ImGui::ColorConvertFloat4ToU32(
        ImVec4(inkV.x, inkV.y, inkV.z, inkV.w * a));
  };
  const ImU32 axisCol = ink(0.62f), txtCol = ink(1.0f), gridCol = ink(0.20f);
  const ImU32 c1 = IM_COL32(90, 170, 250, 255),
              c2 = IM_COL32(240, 140, 60, 255);

  float xLo = x.front(), xHi = x.back();
  if (xHi <= xLo)
    xHi = xLo + 1.0f;
  float yLo = 1e30f, yHi = -1e30f;
  auto span = [&](const std::vector<float> *y) {
    if (!y)
      return;
    for (float v : *y) {
      if (v < yLo)
        yLo = v;
      if (v > yHi)
        yHi = v;
    }
  };
  span(y1);
  span(y2);
  if (yLo > yHi) {
    yLo = 0;
    yHi = 1;
  }
  if (yHi - yLo < 1e-6f) {
    yHi += 0.5f;
    yLo -= 0.5f;
  }
  const float ymarg = (yHi - yLo) * 0.08f;
  yLo -= ymarg;
  yHi += ymarg;

  const float w = ImGui::GetContentRegionAvail().x, h = 170.0f * S;
  const ImVec2 p0 = ImGui::GetCursorScreenPos();
  ImGui::Dummy(ImVec2(w, h));
  dl->PushClipRect(p0, ImVec2(p0.x + w, p0.y + h), true);
  const float padL = 48 * S, padB = 22 * S, padT = 8 * S, padR = 12 * S;
  const ImVec2 a0(p0.x + padL, p0.y + padT),
      a1(p0.x + w - padR, p0.y + h - padB);
  auto X = [&](float v) {
    return a0.x + (v - xLo) / (xHi - xLo) * (a1.x - a0.x);
  };
  auto Y = [&](float v) {
    return a1.y - (v - yLo) / (yHi - yLo) * (a1.y - a0.y);
  };
  // grid + axes
  for (int g = 0; g <= 4; ++g) {
    float yy = a0.y + (a1.y - a0.y) * g / 4.0f;
    dl->AddLine(ImVec2(a0.x, yy), ImVec2(a1.x, yy), gridCol, 1.0f);
    char lb[24];
    std::snprintf(lb, sizeof(lb), "%.4g", yHi - (yHi - yLo) * g / 4.0f);
    dl->AddText(ImVec2(p0.x + 2 * S, yy - 6 * S), axisCol, lb);
  }
  dl->AddLine(ImVec2(a0.x, a1.y), ImVec2(a1.x, a1.y), axisCol, 1.5f);
  dl->AddLine(ImVec2(a0.x, a0.y), ImVec2(a0.x, a1.y), axisCol, 1.5f);
  // x tick labels (start / mid / end)
  for (int g = 0; g <= 2; ++g) {
    float xv = xLo + (xHi - xLo) * g / 2.0f;
    char lb[24];
    std::snprintf(lb, sizeof(lb), "%.4g", xv);
    dl->AddText(ImVec2(X(xv) - (g == 2   ? 24 * S
                                : g == 1 ? 12 * S
                                         : 0),
                       a1.y + 4 * S),
                axisCol, lb);
  }
  auto plot = [&](const std::vector<float> *y, ImU32 col) {
    if (!y || y->empty())
      return;
    int n = (int)std::min(x.size(), y->size());
    ImVec2 prev(X(x[0]), Y((*y)[0]));
    for (int i = 1; i < n; ++i) {
      ImVec2 cur(X(x[i]), Y((*y)[i]));
      dl->AddLine(prev, cur, col, 2.0f);
      prev = cur;
    }
  };
  plot(y1, c1);
  plot(y2, c2);
  if (xlabel)
    dl->AddText(ImVec2(a1.x - 30 * S, a1.y + 4 * S), txtCol, xlabel);
  if (ylabel)
    dl->AddText(ImVec2(p0.x + 2 * S, p0.y + 2 * S), txtCol, ylabel);
  dl->PopClipRect();
  // legend row
  if (l1 && y1) {
    ImGui::PushStyleColor(ImGuiCol_Text, c1);
    ImGui::TextUnformatted(l1);
    ImGui::PopStyleColor();
  }
  if (l2 && y2) {
    if (l1 && y1)
      ImGui::SameLine(0, 16 * S);
    ImGui::PushStyleColor(ImGuiCol_Text, c2);
    ImGui::TextUnformatted(l2);
    ImGui::PopStyleColor();
  }
  (void)id;
}

void SimManager::drawEmForm(AppState &st) {
  (void)st;
  const float S = ui::uiScale;
  // ---- shared waveguide cross-section selector ----
  ImGui::TextUnformatted("Waveguide");
  ImGui::SetNextItemWidth(-FLT_MIN);
  const char *stdLabel = (em_.stdIdx >= 0 && em_.stdIdx < kNEmStandards)
                             ? kEmStandards[em_.stdIdx].name
                             : "Custom cross-section";
  if (ImGui::BeginCombo("##emstd", stdLabel)) {
    for (int i = 0; i < kNEmStandards; ++i) {
      char lb[48];
      std::snprintf(lb, sizeof(lb), "%s  (%.3g x %.3g mm)",
                    kEmStandards[i].name, kEmStandards[i].a_mm,
                    kEmStandards[i].b_mm);
      if (ImGui::Selectable(lb, em_.stdIdx == i)) {
        em_.stdIdx = i;
        em_.a_mm = kEmStandards[i].a_mm;
        em_.b_mm = kEmStandards[i].b_mm;
      }
    }
    if (ImGui::Selectable("Custom cross-section", em_.stdIdx < 0))
      em_.stdIdx = -1;
    ImGui::EndCombo();
  }
  simTip("EIA rectangular-waveguide standard (WR-90 is X-band). Choose Custom "
         "for an arbitrary "
         "rectangular or circular cross-section.");
  if (em_.stdIdx < 0) {
    static const char *kShapes[2] = {"Rectangular", "Circular"};
    ImGui::TextUnformatted("Cross-section");
    ImGui::SetNextItemWidth(-FLT_MIN);
    if (ImGui::BeginCombo("##emshape", kShapes[em_.shape ? 1 : 0])) {
      for (int i = 0; i < 2; ++i)
        if (ImGui::Selectable(kShapes[i], em_.shape == i))
          em_.shape = i;
      ImGui::EndCombo();
    }
    if (em_.shape == 0) {
      ImGui::TextUnformatted("Broad wall a (mm)");
      ImGui::SetNextItemWidth(-FLT_MIN);
      ImGui::DragFloat("##ema", &em_.a_mm, 0.1f, 0.1f, 2000.0f, "%.3f mm");
      ImGui::TextUnformatted("Narrow wall b (mm)");
      ImGui::SetNextItemWidth(-FLT_MIN);
      ImGui::DragFloat("##emb", &em_.b_mm, 0.1f, 0.1f, 2000.0f, "%.3f mm");
    } else {
      ImGui::TextUnformatted("Radius (mm)");
      ImGui::SetNextItemWidth(-FLT_MIN);
      ImGui::DragFloat("##emr", &em_.radius_mm, 0.1f, 0.1f, 2000.0f, "%.3f mm");
    }
  }
  ImGui::Spacing();

  // ---- per-form inputs ----
  if (emForm_ == EmForm::Eigenmode) {
    ImGui::SeparatorText("Eigenmode");
    ImGui::TextUnformatted("Number of modes");
    ImGui::SetNextItemWidth(-FLT_MIN);
    ImGui::SliderInt("##emn", &em_.nModes, 1, 20);
    simTip("How many cutoff modes to solve, ordered by cutoff frequency.");
    ImGui::TextUnformatted("Resolution (grid)");
    ImGui::SetNextItemWidth(-FLT_MIN);
    ImGui::SliderInt("##emres", &em_.resolution, 16, 256);
    simTip("FDFD eigensolver grid resolution across the cross-section. Higher "
           "= more accurate, "
           "slower. The analytic error vs the closed-form cutoff is reported "
           "per mode.");
  } else {
    // Driven + time domain share the guide length + excitation mode.
    ImGui::SeparatorText(emForm_ == EmForm::Driven ? "Driven (S-parameters)"
                                                   : "Time domain");
    ImGui::TextUnformatted("Length (mm)");
    ImGui::SetNextItemWidth(-FLT_MIN);
    ImGui::DragFloat("##emlen", &em_.lengthMm, 0.5f, 0.1f, 5000.0f, "%.2f mm");
    simTip(
        "Straight guide section length between the two matched modal ports.");
    ImGui::TextUnformatted("Excitation mode");
    ImGui::SetNextItemWidth(-FLT_MIN);
    int mi = (em_.modeIdx >= 0 && em_.modeIdx < kNEmModes) ? em_.modeIdx : 0;
    if (ImGui::BeginCombo("##emmode", kEmModes[mi])) {
      for (int i = 0; i < kNEmModes; ++i)
        if (ImGui::Selectable(kEmModes[i], mi == i))
          em_.modeIdx = i;
      ImGui::EndCombo();
    }
    if (emForm_ == EmForm::Driven) {
      ImGui::TextUnformatted("Start frequency (GHz)");
      ImGui::SetNextItemWidth(-FLT_MIN);
      ImGui::DragFloat("##emf0", &em_.fStartGhz, 0.05f, 0.01f, 300.0f,
                       "%.3f GHz");
      ImGui::TextUnformatted("Stop frequency (GHz)");
      ImGui::SetNextItemWidth(-FLT_MIN);
      ImGui::DragFloat("##emf1", &em_.fStopGhz, 0.05f, 0.01f, 300.0f,
                       "%.3f GHz");
      ImGui::TextUnformatted("Frequency points");
      ImGui::SetNextItemWidth(-FLT_MIN);
      ImGui::SliderInt("##emnf", &em_.nFreq, 3, 1001);
      ImGui::TextUnformatted("Geometry STL (optional)");
      ImGui::SetNextItemWidth(-FLT_MIN);
      ImGui::InputText("##emstl", em_.stlPath, sizeof(em_.stlPath));
      simTip("Optional STL path for a full-wave (openEMS) solve over real "
             "geometry. Leave empty "
             "for the fast analytic uniform-section S-parameters.");
    } else { // TimeDomain
      ImGui::TextUnformatted("Center frequency f0 (GHz)");
      ImGui::SetNextItemWidth(-FLT_MIN);
      ImGui::DragFloat("##emtf0", &em_.f0Ghz, 0.05f, 0.01f, 300.0f, "%.3f GHz");
      ImGui::TextUnformatted("Bandwidth (GHz)");
      ImGui::SetNextItemWidth(-FLT_MIN);
      ImGui::DragFloat("##embw", &em_.bwGhz, 0.05f, 0.01f, 300.0f, "%.3f GHz");
      simTip("Gaussian-modulated excitation: center frequency and bandwidth of "
             "the pulse.");
    }
  }
  (void)S;
}

void SimManager::drawEmPanel(AppState &st) {
  const float S = ui::uiScale;
  // ---- header (own — EM has no Study, so drawPanelHeader(Study&) doesn't
  // apply) ----
  const char *formName = emForm_ == EmForm::Eigenmode ? "Eigenmode"
                         : emForm_ == EmForm::Driven
                             ? "Freq Sweep (S-parameters)"
                             : "Time Domain";
  IconUV iv = getIcon(emForm_ == EmForm::Eigenmode ? "study_modal"
                      : emForm_ == EmForm::Driven  ? "study_flow"
                                                   : "study_static",
                      20);
  if (iv.valid) {
    ImGui::Image(iv.texId, ImVec2(16.0f * S, 16.0f * S), iv.uv0, iv.uv1);
    ImGui::SameLine(0, 6.0f * S);
  }
  ImGui::PushFont(st.fonts.medium);
  ImGui::TextUnformatted(
      (std::string("Electromagnetics \xe2\x80\x94 ") + formName).c_str());
  ImGui::PopFont();
  const float btnW = 40.0f * S;
  const float rightX = ImGui::GetWindowContentRegionMax().x - btnW;
  if (rightX > ImGui::GetCursorPosX())
    ImGui::SameLine(rightX);
  else
    ImGui::SameLine();
  if (ImGui::Button("OK", ImVec2(btnW, 0))) {
    emPanelOpen_ = false;
    return;
  }
  simTip("Close the EM panel and return to the study tree.");
  ImGui::Separator();
  ImGui::Spacing();

  // ---- analysis switcher (three tabs, so all forms are reachable without the
  // ribbon) ----
  if (ImGui::BeginTabBar("##emtabs")) {
    struct T {
      const char *label;
      EmForm f;
    };
    static const T tabs[] = {{"Eigenmode", EmForm::Eigenmode},
                             {"Freq Sweep", EmForm::Driven},
                             {"Time Domain", EmForm::TimeDomain}};
    for (const auto &t : tabs) {
      // Only force-select on the frame right after a ribbon command changed
      // emForm_; otherwise the tab bar's own click drives emForm_ (SetSelected
      // every frame would fight the user).
      ImGuiTabItemFlags tf =
          (emTabSync_ && emForm_ == t.f) ? ImGuiTabItemFlags_SetSelected : 0;
      if (ImGui::BeginTabItem(t.label, nullptr, tf)) {
        emForm_ = t.f;
        ImGui::EndTabItem();
      }
    }
    emTabSync_ = false;
    ImGui::EndTabBar();
  }
  ImGui::Spacing();

  // ---- inputs ----
  drawEmForm(st);

  // ---- run ----
  ImGui::Spacing();
  ImGui::Separator();
  if (emRunning_) {
    ImGui::TextDisabled("Solving\xe2\x80\xa6");
    ImGui::BeginDisabled();
    ImGui::Button("Run", ImVec2(-FLT_MIN, 0));
    ImGui::EndDisabled();
  } else {
    if (ImGui::Button("Run", ImVec2(-FLT_MIN, 0)))
      runEmForm(st);
  }
  simTip("Run the electromagnetics solve for this analysis (async — the panel "
         "stays responsive).");

  // ---- results ----
  ImGui::Spacing();
  if (emForm_ == EmForm::Eigenmode) {
    if (em_.eigResult == 0) {
      ImGui::PushStyleColor(ImGuiCol_Text,
                            Palette::toU32(ui::palette.statusErr));
      ImGui::TextWrapped("%s", em_.eigError.c_str());
      ImGui::PopStyleColor();
    } else if (em_.eigResult == 1) {
      ImGui::SeparatorText("Modes");
      if (em_.eigModes.empty()) {
        ImGui::TextDisabled("No modes returned.");
      } else if (ImGui::BeginTable("##emmodes", 5,
                                   ImGuiTableFlags_Borders |
                                       ImGuiTableFlags_RowBg |
                                       ImGuiTableFlags_SizingStretchProp)) {
        ImGui::TableSetupColumn("Mode");
        ImGui::TableSetupColumn("Type");
        ImGui::TableSetupColumn("fc (GHz)");
        ImGui::TableSetupColumn("kc (1/m)");
        ImGui::TableSetupColumn("err %");
        ImGui::TableHeadersRow();
        int row = 0;
        for (const auto &m : em_.eigModes) {
          ImGui::TableNextRow();
          // The first (lowest-fc) mode is the dominant mode — highlight the
          // whole row.
          if (row == 0)
            ImGui::TableSetBgColor(ImGuiTableBgTarget_RowBg0,
                                   IM_COL32(90, 170, 250, 40));
          ImGui::TableNextColumn();
          std::string nm = m.value("name", std::string("?"));
          if (row == 0)
            nm += "  (dominant)";
          ImGui::TextUnformatted(nm.c_str());
          ImGui::TableNextColumn();
          ImGui::TextUnformatted(m.value("type", std::string("")).c_str());
          ImGui::TableNextColumn();
          ImGui::Text("%.4f", m.value("fc_ghz", 0.0));
          ImGui::TableNextColumn();
          ImGui::Text("%.2f", m.value("kc_per_m", 0.0));
          ImGui::TableNextColumn();
          if (m.contains("analytic_err_pct"))
            ImGui::Text("%.3f", m.value("analytic_err_pct", 0.0));
          else
            ImGui::TextDisabled("-");
          ++row;
        }
        ImGui::EndTable();
      }
    } else {
      ImGui::TextDisabled("Run to compute the cutoff-mode table.");
    }
  } else if (emForm_ == EmForm::Driven) {
    if (em_.drvResult == 0) {
      ImGui::PushStyleColor(ImGuiCol_Text,
                            Palette::toU32(ui::palette.statusErr));
      ImGui::TextWrapped("%s", em_.drvError.c_str());
      ImGui::PopStyleColor();
    } else if (em_.drvResult == 1) {
      // S11 and S21 live on very different dB scales (a matched section has S11
      // near the noise floor and S21 near 0), so a shared Y axis would flatten
      // one. Plot them stacked, each auto-ranged.
      ImGui::SeparatorText("Return loss S11 (dB)");
      drawEmLinePlot("##ems11", em_.drvFreq, &em_.drvS11, "S11 (dB)", nullptr,
                     nullptr, "GHz", "dB");
      ImGui::SeparatorText("Insertion loss S21 (dB)");
      drawEmLinePlot("##ems21", em_.drvFreq, &em_.drvS21, "S21 (dB)", nullptr,
                     nullptr, "GHz", "dB");
    } else {
      ImGui::TextDisabled("Run to compute S11 / S21 vs frequency.");
    }
  } else { // TimeDomain
    if (em_.tdResult == 0) {
      ImGui::PushStyleColor(ImGuiCol_Text,
                            Palette::toU32(ui::palette.statusErr));
      ImGui::TextWrapped("%s", em_.tdError.c_str());
      ImGui::PopStyleColor();
    } else if (em_.tdResult == 1) {
      ImGui::SeparatorText("Port voltages");
      drawEmLinePlot("##emtd", em_.tdTime, &em_.tdPort1, "Port 1 (V)",
                     &em_.tdPort2, "Port 2 (V)", "ns", "V");
    } else {
      ImGui::TextDisabled("Run to compute the port-voltage traces.");
    }
  }
}

void SimManager::configureActive(AppState &st, float density,
                                 const std::string &materialKey) {
  if (sel_ < 0 || sel_ >= (int)studies_.size())
    return;
  studies_[sel_].meshFineness =
      density; // density and fineness share the higher=finer sense
  if (!materialKey.empty())
    applyPartMaterial(st, studies_[sel_], materialKey);
  persist(st, studies_[sel_]);
}

void SimManager::configureActiveFlow(AppState &st, int fluid, float aoaDeg,
                                     float sideslipDeg, int preset) {
  if (sel_ < 0 || sel_ >= (int)studies_.size())
    return;
  Study &s = studies_[sel_];
  s.flowFluid = fluid;
  if (fluid >= 0 && fluid < kNFluids - 1) {
    s.flowDensity = kFluids[fluid].density;
    s.flowViscosity = kFluids[fluid].visc;
  }
  s.flowAoA = aoaDeg;
  s.flowSideslip = sideslipDeg;
  s.flowPreset = preset;
  persist(st, s);
}

// ---------------------------------------------------------------- pick capture

void SimManager::captureSelectionInto(AppState &st, SimWell &well) {
  for (const auto &it : st.selection.items()) {
    if (it.selector.empty())
      continue;
    try {
      auto sel = nlohmann::json::parse(it.selector);
      if (sel.value("kind", std::string()) != "face")
        continue;
      // Stash the viewport identity so the face stays highlightable after
      // save/reopen.
      sel["_pick"] = {{"body", it.hit.body_id}, {"topo", it.hit.topo_id}};
      SimSel ss;
      ss.selector = sel;
      ss.pickBody = it.hit.body_id;
      ss.pickTopo = it.hit.topo_id;
      // De-dup by (body,topo).
      bool dup = false;
      for (const auto &p : well.picks)
        if (p.pickBody == ss.pickBody && p.pickTopo == ss.pickTopo) {
          dup = true;
          break;
        }
      if (!dup)
        well.picks.push_back(std::move(ss));
    } catch (...) {
    }
  }
}

// ---------------------------------------------------------------- viewport
// pick mux

bool SimManager::wantsViewportFacePick(AppState &st) const {
  if (st.leftPaneTab != AppState::LP_Simulation)
    return false;
  if (st.propertyManager && st.propertyManager->active())
    return false; // PM owns picks first
  if (armedWell_ < 0)
    return false;
  return sel_ >= 0 && sel_ < (int)studies_.size();
}

bool SimManager::routeFacePick(AppState &st, const nlohmann::json &selector,
                               uint32_t body, uint32_t topo) {
  if (!wantsViewportFacePick(st))
    return false;
  if (selector.value("kind", std::string()) != "face")
    return false;
  Study &s = studies_[sel_];
  nlohmann::json sel = selector;
  sel["_pick"] = {{"body", body}, {"topo", topo}};
  SimSel ss;
  ss.selector = sel;
  ss.pickBody = body;
  ss.pickTopo = topo;
  SimWell *well = nullptr;
  std::string wlabel = "selection";
  if (armedWell_ == 0) {
    well = &s.fixtures;
    wlabel = "fixture";
  } else if (armedWell_ - 1 < (int)s.loads.size()) {
    well = &s.loads[armedWell_ - 1].faces;
    wlabel = s.loads[armedWell_ - 1].pressure ? "pressure load" : "force load";
  }
  if (!well)
    return false;
  for (const auto &p : well->picks)
    if (p.pickBody == body &&
        p.pickTopo == topo) { // already present — toggle it OFF (deselect)
      well->picks.erase(std::remove_if(well->picks.begin(), well->picks.end(),
                                       [&](const SimSel &q) {
                                         return q.pickBody == body &&
                                                q.pickTopo == topo;
                                       }),
                        well->picks.end());
      persist(st, s);
      st.toast(ui::ToastLevel::Info,
               "Removed face from " + wlabel + " (" +
                   std::to_string((int)well->picks.size()) + " left)");
      return true;
    }
  well->picks.push_back(std::move(ss));
  persist(st, s);
  // Feedback so the user knows the click landed — fixture/load selection was
  // silent before.
  st.toast(ui::ToastLevel::Info, "Added face to " + wlabel + " (" +
                                     std::to_string((int)well->picks.size()) +
                                     " total)");
  return true;
}

// ---------------------------------------------------------------- previews

bool SimManager::previewActive(AppState &st) const {
  if (st.leftPaneTab != AppState::LP_Simulation)
    return false;
  if (st.colormapOn)
    return false; // don't stack tints over a result colormap
  return sel_ >= 0 && sel_ < (int)studies_.size();
}

void SimManager::collectFixtureFaces(std::vector<uint64_t> &keys) const {
  if (sel_ < 0 || sel_ >= (int)studies_.size())
    return;
  for (const auto &p : studies_[sel_].fixtures.picks)
    if (p.pickBody && p.pickTopo)
      keys.push_back(((uint64_t)p.pickBody << 32) | (uint64_t)p.pickTopo);
}

void SimManager::collectLoadFaces(std::vector<uint64_t> &keys) const {
  if (sel_ < 0 || sel_ >= (int)studies_.size())
    return;
  for (const auto &ld : studies_[sel_].loads)
    for (const auto &p : ld.faces.picks)
      if (p.pickBody && p.pickTopo)
        keys.push_back(((uint64_t)p.pickBody << 32) | (uint64_t)p.pickTopo);
}

// ---------------------------------------------------------------- result field
// reload / mesh preview

void SimManager::applyResultField(AppState &st, Study &s) {
  if (s.overlayPath.empty() || !st.renderer)
    return;
  // The overlay is normally an aicm-shm:// handoff URI (published to /dev/shm
  // by the engine and resolved through aicm_handoff::map inside loadAicmFile) —
  // NOT a filesystem path. Only a fallback temp .aicm is a real file.
  // std::filesystem::exists() on a shm URI is always false, so guarding every
  // overlay with it silently dropped the whole result (colormap + deform) on
  // every solve. Precheck existence ONLY for genuine file paths; shm URIs go
  // straight to the loader, which reports a clean error if the segment was
  // reclaimed.
  if (!aicad::aicm_handoff::is_shm_uri(s.overlayPath)) {
    std::error_code ec;
    if (!std::filesystem::exists(s.overlayPath, ec)) {
      st.addLog("[sim] overlay file missing (re-run to regenerate): " +
                s.overlayPath);
      return;
    }
  }
  // Seed display state BEFORE the mesh load: commitDocMeshFromParsed reads
  // activeScalarField at commit, and setMeshes re-applies a standing nonzero
  // deformScale_.
  st.colormapOn = true;
  st.renderer->setColormapMode(true);
  // Prefer a standing auto scale so async commit (WASM) lands deformed without
  // a second pass.
  if (st.renderer && st.dispScaleHint > 0)
    st.renderer->setDeformScale(st.dispScaleHint);
#ifdef __EMSCRIPTEN__
  // Main/GL thread must not sync-fetch AICM (wedges ASYNCIFY). Loader pthread +
  // drain commits.
  pendingOverlayStudyId_ = s.id;
  st.loadDocMeshAsync(s.overlayPath, /*renderOnly=*/false);
#else
  std::string err;
  // FULL (not renderOnly) load: re-attaches the chosen named scalar to the mesh
  // so the colormap tracks the selected field. The renderOnly path drops
  // scalar/disp, so it can't switch fields.
  if (!st.loadDocMesh(s.overlayPath, err, false)) {
    if (!err.empty())
      st.addLog("[sim] overlay reload failed: " + err);
    return;
  }
  finishResultOverlay(st, s);
#endif
}

void SimManager::finishResultOverlay(AppState &st, Study &s) {
  if (!st.renderer)
    return;
  st.colormapOn = true;
  st.renderer->setColormapMode(true);
  st.legendOn = st.docMeshHasScalar;
  // Show the deformed shape at the auto (visible) scale EVERY time the result
  // is (re)applied — not only on a fresh solve. clearResultOverlay() zeroes the
  // renderer's deformScale_ whenever the overlay is hidden (tab/workspace
  // switch, node change), and setMeshes re-applies only a *nonzero* standing
  // scale — so without this a re-shown result would load correctly colored but
  // perfectly FLAT. This is the single apply path, so the fresh-solve
  // completion no longer sets it separately.
  if (st.docMeshHasDisp && st.renderer)
    st.renderer->setDeformScale(st.dispScaleHint > 0 ? st.dispScaleHint : 1.0f);
  st.addLog(std::string("[sim] result view applied: deformed=") +
            (st.docMeshHasDisp ? "yes" : "no") +
            " scale=" + std::to_string(st.dispScaleHint));
  std::string label, unit;
  double fromSI = 1.0;
  fieldMeta(st, st.activeScalarField, label, unit, fromSI);
  st.legendTitle = label;
  st.legendUnits = unit;
  float vmin = 0, vmax = 1;
  if (st.renderer->scalarRange(vmin, vmax)) {
    // Legend min/max are display labels only — the colormap maps the raw SI
    // values, so scaling the labels into the active unit system is purely
    // cosmetic and keeps the gradient correct.
    st.legendMin = (float)(vmin * fromSI);
    st.legendMax = (float)(vmax * fromSI);
  }
  (void)s;
}

void SimManager::clearMeshPreview() {
  meshPreviewSegs_.clear();
  meshPreviewStudyId_.clear();
}

void SimManager::previewMesh(AppState &st, Study &s) {
  if (!st.docMirror.hasDoc())
    return;
  if (meshPreviewPending_) {
    st.toast(ui::ToastLevel::Info, "Mesh preview already in progress…");
    return;
  }
  // T-PERF4: export + sim_mesh are long (up to 180s). AsyncRpc keeps the UI
  // live. Daemon/WASM: empty path → hostd picks a server temp under
  // aicad_cpp_docs.
  const std::string stepPath =
      engine::daemonMode() ? std::string()
                           : (appdataAppDir() + "/cache/sim_mesh_preview.step");
  const std::string studyId = s.id;
  const int meshOrder = s.meshOrder;
  meshPreviewPending_ = true;
  st.toast(ui::ToastLevel::Info, "Meshing… (UI stays interactive)");
  ++st.rebuildsInFlight;
  nlohmann::json exportParams = {{"doc_id", st.docMirror.docId()},
                                 {"path", stepPath}};
  st.rpc.call(
      "doc_export_step", std::move(exportParams),
      [this, &st, studyId, meshOrder, stepPath](const core::RpcResult &r) {
        if (!r.ok || (r.result.is_object() && r.result.contains("code"))) {
          --st.rebuildsInFlight;
          meshPreviewPending_ = false;
          st.toast(ui::ToastLevel::Error, "Mesh preview: export failed");
          return;
        }
        std::string path = r.result.value("path", stepPath);
        nlohmann::json params = {{"step_path", path}};
        Study *sp = nullptr;
        for (auto &ss : studies_)
          if (ss.id == studyId) {
            sp = &ss;
            break;
          }
        if (sp)
          addMeshParams(params, *sp, meshOrder);
        else
          params["order"] = meshOrder;
        st.rpc.call(
            "sim_mesh", std::move(params),
            [this, &st, studyId, meshOrder](const core::RpcResult &r2) {
              --st.rebuildsInFlight;
              meshPreviewPending_ = false;
              if (!r2.ok) {
                st.toast(ui::ToastLevel::Error, "Mesh preview: " + r2.err);
                st.addLog("[sim] sim_mesh failed: " + r2.err);
                return;
              }
              const auto &res = r2.result;
              if (res.contains("code") || !res.value("ok", true)) {
                const std::string msg =
                    res.contains("code") ? res.value("message", std::string())
                    : res.contains("error") ? res.value("error", std::string())
                                            : std::string("meshing failed");
                st.toast(ui::ToastLevel::Error,
                         msg.empty() ? "Mesh preview: meshing failed" : msg);
                st.addLog("[sim] sim_mesh failed: " + msg);
                return;
              }
              Study *s = nullptr;
              for (auto &ss : studies_)
                if (ss.id == studyId) {
                  s = &ss;
                  break;
                }
              if (!s)
                return;
              s->nNodes = res.value("n_nodes", 0);
              s->nElems = res.value("n_elems", 0);
              const double minVol = res.value("min_tet_volume_mm3", 0.0);
              const double edgeRatio = res.value("max_tet_edge_ratio", 0.0);
              s->meshAspectMin = (float)res.value("aspect_min", 0.0);
              s->meshAspectAvg = (float)res.value("aspect_avg", 0.0);
              s->meshAspectMax = (float)res.value("aspect_max", 0.0);
              s->meshJacobianMin = (float)res.value("jacobian_min", 0.0);
              s->meshPctPoor = (float)res.value("pct_poor", 0.0);
              s->meshAspectHist.clear();
              if (res.contains("aspect_hist") && res["aspect_hist"].is_array())
                for (const auto &b : res["aspect_hist"])
                  s->meshAspectHist.push_back((float)b.get<int>());
              s->meshHasQuality = s->nElems > 0;
              clearMeshPreview();
              meshPreviewStudyId_ = s->id;
              if (!res.contains("points") || !res.contains("tets") ||
                  !res["points"].is_array() || !res["tets"].is_array()) {
                st.toast(ui::ToastLevel::Info,
                         "Mesh: " + std::to_string(s->nNodes) + " nodes, " +
                             std::to_string(s->nElems) + " elements");
                return;
              }
              const auto &P = res["points"];
              const auto &T = res["tets"];
              const int stride =
                  res.value("nodes_per_elem", meshOrder == 2 ? 10 : 4);
              const int nT =
                  (stride > 0) ? (int)(T.size() / (size_t)stride) : 0;
              if (nT > 0 && nT <= 250000) {
                auto elNode = [&](int e, int k) {
                  return T[(size_t)e * (size_t)stride + k].get<int>();
                };
                std::unordered_set<uint64_t> seenEdge;
                auto pt = [&](int i, float out[3]) {
                  out[0] = (float)P[(size_t)i * 3 + 0].get<double>();
                  out[1] = (float)P[(size_t)i * 3 + 1].get<double>();
                  out[2] = (float)P[(size_t)i * 3 + 2].get<double>();
                };
                auto edge = [&](int a, int b) {
                  if (a > b)
                    std::swap(a, b);
                  uint64_t k = ((uint64_t)(uint32_t)a << 32) | (uint32_t)b;
                  if (!seenEdge.insert(k).second)
                    return;
                  float pa[3], pb[3];
                  pt(a, pa);
                  pt(b, pb);
                  meshPreviewSegs_.insert(
                      meshPreviewSegs_.end(),
                      {pa[0], pa[1], pa[2], pb[0], pb[1], pb[2]});
                };
                if (stride == 8) {
                  static const int hexF[6][4] = {{0, 1, 2, 3}, {4, 5, 6, 7},
                                                 {0, 1, 5, 4}, {2, 3, 7, 6},
                                                 {0, 3, 7, 4}, {1, 2, 6, 5}};
                  std::map<std::array<int, 4>, int> faceCount;
                  for (int e = 0; e < nT; ++e)
                    for (int f = 0; f < 6; ++f) {
                      std::array<int, 4> q{
                          elNode(e, hexF[f][0]), elNode(e, hexF[f][1]),
                          elNode(e, hexF[f][2]), elNode(e, hexF[f][3])};
                      std::sort(q.begin(), q.end());
                      faceCount[q]++;
                    }
                  for (const auto &kv : faceCount)
                    if (kv.second == 1) {
                      edge(kv.first[0], kv.first[1]);
                      edge(kv.first[1], kv.first[2]);
                      edge(kv.first[2], kv.first[3]);
                      edge(kv.first[0], kv.first[3]);
                    }
                } else {
                  static const int fv[4][3] = {
                      {0, 1, 2}, {0, 1, 3}, {0, 2, 3}, {1, 2, 3}};
                  std::map<std::array<int, 3>, int> faceCount;
                  for (int e = 0; e < nT; ++e)
                    for (int f = 0; f < 4; ++f) {
                      std::array<int, 3> tri{elNode(e, fv[f][0]),
                                             elNode(e, fv[f][1]),
                                             elNode(e, fv[f][2])};
                      std::sort(tri.begin(), tri.end());
                      faceCount[tri]++;
                    }
                  for (const auto &kv : faceCount)
                    if (kv.second == 1) {
                      edge(kv.first[0], kv.first[1]);
                      edge(kv.first[1], kv.first[2]);
                      edge(kv.first[0], kv.first[2]);
                    }
                }
              }
              std::string msg = "Mesh: " + std::to_string(s->nNodes) +
                                " nodes, " + std::to_string(s->nElems) +
                                " elements";
              if (edgeRatio > 0.0) {
                char q[80];
                std::snprintf(q, sizeof(q), " | worst edge ratio %.2f",
                              edgeRatio);
                msg += q;
              }
              if (minVol > 0.0) {
                char q[80];
                std::snprintf(q, sizeof(q), " | min volume %.3g mm3", minVol);
                st.addLog(std::string("[sim] mesh quality: ") + q);
              }
              st.toast(ui::ToastLevel::Info, msg);
            },
            180000);
      },
      60000);
}

bool SimManager::hasMeshPreview(AppState &st) const {
  if (st.leftPaneTab != AppState::LP_Simulation)
    return false;
  if (sel_ < 0 || sel_ >= (int)studies_.size())
    return false;
  return meshPreviewStudyId_ == studies_[sel_].id && !meshPreviewSegs_.empty();
}

// ---- indicator geometry (fixture glyphs + load arrows) ----

namespace {
void vecNorm3(float v[3]) {
  float l = std::sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2]);
  if (l > 1e-9f) {
    v[0] /= l;
    v[1] /= l;
    v[2] /= l;
  }
}
void perp3(const float d[3], float p[3]) {
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
void pushSeg3(std::vector<float> &s, const float a[3], const float b[3]) {
  s.insert(s.end(), {a[0], a[1], a[2], b[0], b[1], b[2]});
}
void cross3(const float a[3], const float b[3], float out[3]) {
  out[0] = a[1] * b[2] - a[2] * b[1];
  out[1] = a[2] * b[0] - a[0] * b[2];
  out[2] = a[0] * b[1] - a[1] * b[0];
}
void pushTri3(std::vector<float> &t, const float a[3], const float b[3],
              const float c[3]) {
  t.insert(t.end(), {a[0], a[1], a[2], b[0], b[1], b[2], c[0], c[1], c[2]});
}
// Solid 12-segment cone: apex at `tip`, axis pointing tip->base, height h, base
// radius r. Side fan + base cap so the head reads from every angle; the SS
// resolve anti-aliases it.
void pushCone3(std::vector<float> &t, const float tip[3], const float axis[3],
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
void pushArrow3(std::vector<float> &s, const float tail[3], const float dir[3],
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
// 3-axis cross glyph (legacy fixture "ground/clamp" marker) of half-length r at
// c.
void pushCross3(std::vector<float> &s, const float c[3], float r) {
  for (int axis = 0; axis < 3; ++axis) {
    float a[3] = {c[0], c[1], c[2]}, b[3] = {c[0], c[1], c[2]};
    a[axis] -= r;
    b[axis] += r;
    pushSeg3(s, a, b);
  }
}
// A short "ground bar + diagonal hatch" anchor laid in the plane spanned by
// (u,w), centred at p, half-width r, the bar running along u and the hatch
// ticks leaning toward -w. This is the classic SolidWorks restraint tick that
// says "this direction is pinned to ground".
void pushGroundHatch(std::vector<float> &s, const float p[3], const float u[3],
                     const float w[3], float r) {
  float b0[3], b1[3];
  for (int k = 0; k < 3; ++k) {
    b0[k] = p[k] - u[k] * r;
    b1[k] = p[k] + u[k] * r;
  }
  pushSeg3(s, b0, b1);
  for (int i = -2; i <= 2; ++i) {
    float t0[3], t1[3];
    for (int k = 0; k < 3; ++k) {
      t0[k] = p[k] + u[k] * (0.42f * r * i);
      t1[k] = t0[k] - (u[k] * 0.30f + w[k] * 0.42f) * r;
    }
    pushSeg3(s, t0, t1);
  }
}
// A small moment/rotation restraint marker: a ~270-degree arc in the plane
// (u,w) about centre c with radius r, capped by a tiny double tick at the open
// end so it reads as a "no rotation about this axis" bracket rather than a full
// circle. Cheap: 10 chord segments + 2 ticks.
void pushMomentArc(std::vector<float> &s, const float c[3], const float u[3],
                   const float w[3], float r) {
  const int kN = 10;
  const float a0 = 0.35f, a1 = 0.35f + 4.9f; // ~281 deg sweep, leaves a gap
  float prev[3];
  for (int i = 0; i <= kN; ++i) {
    const float a = a0 + (a1 - a0) * (float)i / (float)kN;
    const float ca = std::cos(a) * r, sa = std::sin(a) * r;
    float q[3] = {c[0] + u[0] * ca + w[0] * sa, c[1] + u[1] * ca + w[1] * sa,
                  c[2] + u[2] * ca + w[2] * sa};
    if (i > 0)
      pushSeg3(s, prev, q);
    if (i ==
        kN) { // stop-ticks at the arc end so it reads as a restraint bracket
      float t1[3], t2[3];
      for (int k = 0; k < 3; ++k) {
        t1[k] = q[k] - (u[k] * std::cos(a1) + w[k] * std::sin(a1)) * 0.4f * r;
        t2[k] = q[k] + (u[k] * -std::sin(a1) + w[k] * std::cos(a1)) * 0.4f * r;
      }
      pushSeg3(s, q, t1);
      pushSeg3(s, q, t2);
    }
    prev[0] = q[0];
    prev[1] = q[1];
    prev[2] = q[2];
  }
}
// Fixture "fixed geometry" glyph at a point on a face — a full 6-DOF restraint
// symbol so a "Fixed" support visually says it locks all three translations AND
// all three rotations (moments):
//   * 3 solid ground-anchored pin-cones along the local orthonormal axes (X/Y/Z
//   translations)
//   * a ground hatch under each pin (the SolidWorks "pinned to ground" tick)
//   * a small moment arc about each of the 3 axes (rotational restraint)
// `n` is the face outward normal; the two in-plane axes are derived from it so
// the hatches sit flat on the surface. Falls back to the legacy 3-axis cross
// when no triangle batch is supplied.
void pushFixture3(std::vector<float> &s, std::vector<float> *tris,
                  const float c[3], const float n[3], float r) {
  if (!tris) {
    pushCross3(s, c, r);
    return;
  }
  // Local orthonormal frame: e2 = outward normal, e0/e1 span the face plane.
  float e2[3] = {n[0], n[1], n[2]};
  vecNorm3(e2);
  float e0[3], e1[3];
  perp3(e2, e0);
  cross3(e2, e0, e1);
  const float *ax[3] = {e0, e1, e2};
  const float pinLen = 1.05f * r, pinRad = 0.34f * r;
  for (int a = 0; a < 3; ++a) {
    const float *d = ax[a];
    // Translation pin: solid cone pointing back toward the anchor along +axis
    // (both directions for the two in-plane axes so the restraint reads
    // symmetric; normal points outward only).
    int dirs = (a == 2) ? 1 : 2;
    for (int sgn = 0; sgn < dirs; ++sgn) {
      float dd[3] = {d[0], d[1], d[2]};
      if (sgn == 1) {
        dd[0] = -dd[0];
        dd[1] = -dd[1];
        dd[2] = -dd[2];
      }
      float tip[3] = {c[0] + dd[0] * 0.15f * r, c[1] + dd[1] * 0.15f * r,
                      c[2] + dd[2] * 0.15f * r};
      pushCone3(*tris, tip, dd, pinLen,
                pinRad); // tip inboard, base out along axis
    }
    // Ground hatch anchoring this axis, drawn in the plane perpendicular to the
    // axis.
    const float *u = ax[(a + 1) % 3];
    const float *w = ax[(a + 2) % 3];
    float base[3] = {c[0] + d[0] * (pinLen + 0.15f * r),
                     c[1] + d[1] * (pinLen + 0.15f * r),
                     c[2] + d[2] * (pinLen + 0.15f * r)};
    pushGroundHatch(s, base, u, w, 0.6f * r);
  }
  // Moment (rotational) restraint: one small arc about each axis, drawn in the
  // plane it rotates in.
  for (int a = 0; a < 3; ++a) {
    const float *u = ax[(a + 1) % 3];
    const float *w = ax[(a + 2) % 3];
    pushMomentArc(s, c, u, w, 0.55f * r);
  }
}
// ---- Thermal-FEA boundary-condition glyphs (issue #24)
// -------------------------------------------- A thermal study's "fixture"
// faces are a *fixed-temperature* (Dirichlet) BC, not a mechanical restraint,
// and its "loads" are heat flux or convection — so they get their own symbols
// instead of the 6-DOF pin cluster / force arrows. All three build the local
// face frame the same way (e2 = out normal, e0/e1 span the face plane).
static void faceFrame(const float n[3], float e0[3], float e1[3], float e2[3]) {
  e2[0] = n[0];
  e2[1] = n[1];
  e2[2] = n[2];
  vecNorm3(e2);
  perp3(e2, e0);
  cross3(e2, e0, e1);
}
// Fixed-temperature marker: an octagon ring flat on the face + a bold "=" (two
// parallel bars) at its centre — reads as "held constant" (isothermal), the
// thermal analogue of the ground hatch.
void pushIsoTemp(std::vector<float> &s, const float c[3], const float n[3],
                 float r) {
  float e0[3], e1[3], e2[3];
  faceFrame(n, e0, e1, e2);
  const int kN = 8;
  float prev[3];
  for (int i = 0; i <= kN; ++i) {
    const float a = 6.2831853f * (float)i / (float)kN;
    const float ca = std::cos(a) * r, sa = std::sin(a) * r;
    float q[3] = {c[0] + e0[0] * ca + e1[0] * sa,
                  c[1] + e0[1] * ca + e1[1] * sa,
                  c[2] + e0[2] * ca + e1[2] * sa};
    if (i > 0)
      pushSeg3(s, prev, q);
    prev[0] = q[0];
    prev[1] = q[1];
    prev[2] = q[2];
  }
  for (int b = -1; b <= 1; b += 2) { // the "=" bars
    float off = 0.30f * r * (float)b;
    float p0[3], p1[3];
    for (int k = 0; k < 3; ++k) {
      p0[k] = c[k] + e1[k] * off - e0[k] * 0.42f * r;
      p1[k] = c[k] + e1[k] * off + e0[k] * 0.42f * r;
    }
    pushSeg3(s, p0, p1);
  }
}
// Convection marker: three stacked "≈" waves standing off the surface along the
// outward normal, so a convective/film BC reads as heat wafting away from the
// face (distinct from a straight flux arrow).
void pushConvection(std::vector<float> &s, const float c[3], const float n[3],
                    float r) {
  float e0[3], e1[3], e2[3];
  faceFrame(n, e0, e1, e2);
  const int kSeg = 12;
  for (int row = 0; row < 3; ++row) {
    const float h =
        (0.25f + 0.55f * (float)row) * r; // stand-off along the normal
    float prev[3];
    for (int i = 0; i <= kSeg; ++i) {
      const float t = (float)i / (float)kSeg; // 0..1 across the face
      const float u = (t - 0.5f) * 1.7f * r;  // span along e0
      const float wob = std::sin(t * 6.2831853f) * 0.16f * r; // wave along e1
      float q[3];
      for (int k = 0; k < 3; ++k)
        q[k] = c[k] + e0[k] * u + e1[k] * wob + e2[k] * h;
      if (i > 0)
        pushSeg3(s, prev, q);
      prev[0] = q[0];
      prev[1] = q[1];
      prev[2] = q[2];
    }
  }
}
} // namespace

void SimManager::collectIndicatorSegments(
    AppState &st, std::vector<float> &loadSegs, std::vector<float> &fixtureSegs,
    std::vector<float> *loadTris, std::vector<float> *fixtureTris) const {
  if (sel_ < 0 || sel_ >= (int)studies_.size())
    return;
  const Study &s = studies_[sel_];
  // Model scale from the face-centroid extent (faceNormalCache holds {n,c} per
  // face).
  float lo[3] = {1e30f, 1e30f, 1e30f}, hi[3] = {-1e30f, -1e30f, -1e30f};
  bool any = false;
  for (const auto &kv : st.faceNormalCache) {
    const auto &f = kv.second;
    for (int k = 0; k < 3; ++k) {
      lo[k] = std::min(lo[k], f[3 + k]);
      hi[k] = std::max(hi[k], f[3 + k]);
    }
    any = true;
  }
  float diag = 100.0f;
  if (any) {
    float dx = hi[0] - lo[0], dy = hi[1] - lo[1], dz = hi[2] - lo[2];
    diag = std::sqrt(dx * dx + dy * dy + dz * dz);
  }
  if (diag < 1e-3f)
    diag = 100.0f;
  const float L = 0.14f * diag;
  auto faceCN = [&](uint32_t body, uint32_t topo, float c[3],
                    float n[3]) -> bool {
    uint64_t key = ((uint64_t)body << 32) | topo;
    auto it = st.faceNormalCache.find(key);
    if (it == st.faceNormalCache.end())
      return false;
    n[0] = it->second[0];
    n[1] = it->second[1];
    n[2] = it->second[2];
    c[0] = it->second[3];
    c[1] = it->second[4];
    c[2] = it->second[5];
    return true;
  };
  // Distribute up to `grid`x`grid` sample points across a face so a
  // fixture/load repeats its symbol across the surface (SolidWorks-style)
  // instead of a single glyph at the centroid. Points live on the face plane
  // (through centroid c, normal n) and are spread over the face's world AABB,
  // then pulled 55% toward the centroid so they stay inside
  // curved/circular/L-shaped faces we can't clip exactly. Missing bbox key ->
  // just the centroid (mirrors faceCN's guard). `spacing` returns the world gap
  // between adjacent cells so callers can shrink the glyph to avoid overlap.
  auto faceGrid = [&](uint32_t body, uint32_t topo, const float c[3],
                      const float n[3], int grid,
                      std::vector<std::array<float, 3>> &out, float &spacing) {
    out.clear();
    spacing = 0.0f;
    float e0[3], e1[3], nn[3] = {n[0], n[1], n[2]};
    vecNorm3(nn);
    perp3(nn, e0);
    cross3(nn, e0, e1);
    uint64_t key = ((uint64_t)body << 32) | topo;
    auto bit = st.faceBBoxCache.find(key);
    if (bit == st.faceBBoxCache.end() || grid <= 1) {
      out.push_back({c[0], c[1], c[2]});
      return;
    }
    const auto &bb = bit->second; // {minX,minY,minZ,maxX,maxY,maxZ}
    // In-plane half extents = projected bbox size onto each in-plane axis,
    // shrunk toward centroid.
    float hu = 0.0f, hw = 0.0f;
    for (int k = 0; k < 3; ++k) {
      float d = (bb[3 + k] - bb[k]);
      hu += std::fabs(e0[k]) * d;
      hw += std::fabs(e1[k]) * d;
    }
    hu *= 0.55f * 0.5f;
    hw *= 0.55f * 0.5f;
    spacing = std::min(2.0f * hu, 2.0f * hw) / (float)std::max(1, grid - 1);
    for (int j = 0; j < grid; ++j) {
      float tv =
          (grid == 1) ? 0.0f : (2.0f * (float)j / (float)(grid - 1) - 1.0f);
      for (int i = 0; i < grid; ++i) {
        float su =
            (grid == 1) ? 0.0f : (2.0f * (float)i / (float)(grid - 1) - 1.0f);
        out.push_back({c[0] + e0[0] * su * hu + e1[0] * tv * hw,
                       c[1] + e0[1] * su * hu + e1[1] * tv * hw,
                       c[2] + e0[2] * su * hu + e1[2] * tv * hw});
      }
    }
  };
  // ThermalFea reinterprets the wells: fixture faces are a fixed-temperature
  // (Dirichlet) BC and loads are heat flux / convection — so both get thermal
  // glyphs instead of mechanical ones.
  const bool thermalFea = (s.type == StudyType::ThermalFea);
  for (const auto &p : s.fixtures.picks) {
    if (!p.pickBody || !p.pickTopo)
      continue;
    float c[3], n[3];
    if (!faceCN(p.pickBody, p.pickTopo, c, n))
      continue;
    std::vector<std::array<float, 3>> pts;
    float spacing = 0.0f;
    faceGrid(p.pickBody, p.pickTopo, c, n, 3, pts, spacing);
    // Glyph radius: scaled to model size but capped to ~40% of the cell gap so
    // neighbours in the grid stay distinct instead of merging into a blob.
    float gr = L * 0.30f;
    if (spacing > 1e-4f)
      gr = std::min(gr, 0.40f * spacing);
    for (const auto &pt : pts) {
      if (thermalFea)
        pushIsoTemp(fixtureSegs, pt.data(), n, gr);
      else
        pushFixture3(fixtureSegs, fixtureTris, pt.data(), n, gr);
    }
  }
  // Fatigue with fully-reversed loading swings +/- the load — draw a second,
  // opposite arrow so the alternating nature reads on the model (a
  // double-headed indicator).
  const bool reversed =
      (s.type == StudyType::Fatigue && s.fatigueLoadType == 0);
  // Issue #22: scale each load's arrow length by its magnitude relative to the
  // largest load of the SAME kind (force vs pressure normalized separately —
  // different units, not comparable). A lone load is unchanged (ratio 1.0); the
  // smallest of several floors at 0.4*L so it stays legible. Head length tracks
  // len (pushArrow3 default), so heads scale too.
  float maxForce = 0.0f, maxPress = 0.0f;
  for (const auto &ld : s.loads) {
    if (ld.pressure)
      maxPress = std::max(maxPress, std::fabs(ld.pressureMpa));
    else
      maxForce = std::max(maxForce, std::sqrt(ld.vec[0] * ld.vec[0] +
                                              ld.vec[1] * ld.vec[1] +
                                              ld.vec[2] * ld.vec[2]));
  }
  for (const auto &ld : s.loads) {
    // ThermalFea loads: heat flux (arrows normal to the face, into the part) or
    // convection (the stacked-wave film glyph). No magnitude normalization —
    // thermal units aren't the force scale.
    if (thermalFea) {
      for (const auto &p : ld.faces.picks) {
        if (!p.pickBody || !p.pickTopo)
          continue;
        float c[3], n[3];
        if (!faceCN(p.pickBody, p.pickTopo, c, n))
          continue;
        std::vector<std::array<float, 3>> pts;
        float spacing = 0.0f;
        faceGrid(p.pickBody, p.pickTopo, c, n, 3, pts, spacing);
        float gr = L * 0.30f;
        if (spacing > 1e-4f)
          gr = std::min(gr, 0.40f * spacing);
        for (const auto &ap : pts) {
          if (ld.thermalKind == 1) {
            pushConvection(loadSegs, ap.data(), n, gr);
          } else { // heat flux into the surface
            float dir[3] = {-n[0], -n[1], -n[2]};
            float tail[3] = {ap[0] - dir[0] * L * 0.7f,
                             ap[1] - dir[1] * L * 0.7f,
                             ap[2] - dir[2] * L * 0.7f};
            pushArrow3(loadSegs, tail, dir, L * 0.7f, loadTris);
          }
        }
      }
      continue;
    }
    float scale = 1.0f;
    if (ld.pressure) {
      if (maxPress > 1e-9f)
        scale = 0.4f + 0.6f * std::fabs(ld.pressureMpa) / maxPress;
    } else {
      float m = std::sqrt(ld.vec[0] * ld.vec[0] + ld.vec[1] * ld.vec[1] +
                          ld.vec[2] * ld.vec[2]);
      if (maxForce > 1e-9f)
        scale = 0.4f + 0.6f * m / maxForce;
    }
    const float Ld = L * scale;
    for (const auto &p : ld.faces.picks) {
      if (!p.pickBody || !p.pickTopo)
        continue;
      float c[3], n[3];
      if (!faceCN(p.pickBody, p.pickTopo, c, n))
        continue;
      // Distribute the load's arrow across the face (SolidWorks-style field of
      // arrows) so the load reads clearly instead of a single stalk at the
      // centroid. loadTris is always passed so every arrowhead is a SOLID 3D
      // cone, never the wireframe fallback.
      std::vector<std::array<float, 3>> pts;
      float spacing = 0.0f;
      faceGrid(p.pickBody, p.pickTopo, c, n, 3, pts, spacing);
      for (const auto &ap : pts) {
        const float *a = ap.data();
        if (ld.pressure) {
          // Pressure pushes INTO the face along -normal; arrow tip lands on the
          // surface.
          float dir[3] = {-n[0], -n[1], -n[2]};
          float tail[3] = {a[0] - dir[0] * Ld, a[1] - dir[1] * Ld,
                           a[2] - dir[2] * Ld};
          pushArrow3(loadSegs, tail, dir, Ld, loadTris);
          if (reversed) { // opposite (suction) half-cycle
            float rd[3] = {n[0], n[1], n[2]};
            float rt[3] = {a[0] - rd[0] * Ld, a[1] - rd[1] * Ld,
                           a[2] - rd[2] * Ld};
            pushArrow3(loadSegs, rt, rd, Ld, loadTris);
          }
        } else {
          float dir[3] = {ld.vec[0], ld.vec[1], ld.vec[2]};
          float mag =
              std::sqrt(dir[0] * dir[0] + dir[1] * dir[1] + dir[2] * dir[2]);
          if (mag < 1e-6f)
            continue;
          // force arrow emanates from the face along the vector
          pushArrow3(loadSegs, a, dir, Ld, loadTris);
          if (reversed) {
            float rd[3] = {-dir[0], -dir[1], -dir[2]};
            pushArrow3(loadSegs, a, rd, Ld, loadTris);
          }
        }
      }
    }
  }
}

// ---------------------------------------------------------------- flow setup
// preview + result lines

namespace {
// 8-stop turbo-ish ramp (slow -> fast) for speed-banded flow lines.
static const float kFlowBandCol[8][4] = {
    {0.20f, 0.10f, 0.55f, 0.85f}, // indigo (near-stagnant)
    {0.13f, 0.32f, 0.78f, 0.85f}, // blue
    {0.10f, 0.58f, 0.85f, 0.88f}, // cyan
    {0.13f, 0.76f, 0.62f, 0.90f}, // teal
    {0.46f, 0.84f, 0.27f, 0.92f}, // green
    {0.88f, 0.85f, 0.18f, 0.95f}, // yellow
    {0.97f, 0.56f, 0.13f, 0.97f}, // orange
    {0.92f, 0.23f, 0.14f, 1.00f}, // red (fastest)
};
// Push the 12 edges of an oriented box (centre c, orthonormal axes u/v/w,
// half-extents hu/hv/hw).
void pushBox(std::vector<float> &s, const float c[3], const float u[3],
             const float v[3], const float w[3], float hu, float hv, float hw) {
  float cor[8][3];
  int n = 0;
  for (int su = -1; su <= 1; su += 2)
    for (int sv = -1; sv <= 1; sv += 2)
      for (int sw = -1; sw <= 1; sw += 2) {
        cor[n][0] = c[0] + su * hu * u[0] + sv * hv * v[0] + sw * hw * w[0];
        cor[n][1] = c[1] + su * hu * u[1] + sv * hv * v[1] + sw * hw * w[1];
        cor[n][2] = c[2] + su * hu * u[2] + sv * hv * v[2] + sw * hw * w[2];
        ++n;
      }
  // bit pattern: bit0=w, bit1=v, bit2=u. Connect corners differing in exactly
  // one bit.
  static const int edges[12][2] = {{0, 1}, {2, 3}, {4, 5}, {6, 7},
                                   {0, 2}, {1, 3}, {4, 6}, {5, 7},
                                   {0, 4}, {1, 5}, {2, 6}, {3, 7}};
  for (auto &e : edges)
    pushSeg3(s, cor[e[0]], cor[e[1]]);
}
} // namespace

bool SimManager::flowSetupActive(AppState &st) const {
  if (st.leftPaneTab != AppState::LP_Simulation)
    return false;
  if (st.colormapOn)
    return false; // a result colormap is up — the setup overlay would muddy it
  if (node_ == SimNode::Results)
    return false; // never stack the setup tunnel over a result view
  if (sel_ < 0 || sel_ >= (int)studies_.size())
    return false;
  return isFlowFamily(studies_[sel_].type);
}

void SimManager::collectFlowSetup(AppState &st, std::vector<float> &arrowSegs,
                                  std::vector<float> &domainSegs,
                                  std::vector<float> &inletSegs,
                                  std::vector<float> *arrowTris) const {
  if (sel_ < 0 || sel_ >= (int)studies_.size())
    return;
  const Study &s = studies_[sel_];
  // Model bbox / centre / scale from the face-centroid cache (same source as
  // the FEA indicators).
  float lo[3] = {1e30f, 1e30f, 1e30f}, hi[3] = {-1e30f, -1e30f, -1e30f};
  bool any = false;
  for (const auto &kv : st.faceNormalCache) {
    for (int k = 0; k < 3; ++k) {
      lo[k] = std::min(lo[k], kv.second[3 + k]);
      hi[k] = std::max(hi[k], kv.second[3 + k]);
    }
    any = true;
  }
  if (!any) {
    lo[0] = lo[1] = lo[2] = -50;
    hi[0] = hi[1] = hi[2] = 50;
  }
  float ctr[3] = {0.5f * (lo[0] + hi[0]), 0.5f * (lo[1] + hi[1]),
                  0.5f * (lo[2] + hi[2])};
  float diag = std::sqrt((hi[0] - lo[0]) * (hi[0] - lo[0]) +
                         (hi[1] - lo[1]) * (hi[1] - lo[1]) +
                         (hi[2] - lo[2]) * (hi[2] - lo[2]));
  if (diag < 1e-3f)
    diag = 100.0f;
  const float r = 0.5f * diag; // model "radius"

  // Flow frame: f = incoming dir, right = up x f (fallback +Y), up = f x right.
  float f[3];
  flowIncomingDir(s, f);
  vecNorm3(f);
  float up[3] = {0, 0, 1};
  float rgt[3] = {up[1] * f[2] - up[2] * f[1], up[2] * f[0] - up[0] * f[2],
                  up[0] * f[1] - up[1] * f[0]};
  if (std::sqrt(rgt[0] * rgt[0] + rgt[1] * rgt[1] + rgt[2] * rgt[2]) < 1e-4f) {
    rgt[0] = 0;
    rgt[1] = 1;
    rgt[2] = 0;
  }
  vecNorm3(rgt);
  float upv[3] = {f[1] * rgt[2] - f[2] * rgt[1], f[2] * rgt[0] - f[0] * rgt[2],
                  f[0] * rgt[1] - f[1] * rgt[0]};
  vecNorm3(upv);

  // Wind-tunnel domain (matches the solver's 1x up / 3x down / 1x lateral
  // padding, roughly): a box 6r long along the flow, 3r square in
  // cross-section, with the model ~1/4 in from the inlet.
  const float halfLen = 3.0f * r, halfCross = 1.6f * r;
  const float shift =
      1.0f *
      r; // push the box centre downstream so the inlet sits ahead of the part
  float boxC[3] = {ctr[0] + f[0] * shift, ctr[1] + f[1] * shift,
                   ctr[2] + f[2] * shift};
  pushBox(domainSegs, boxC, f, rgt, upv, halfLen, halfCross, halfCross);

  // Inlet plane: the upstream face rectangle + a cross, at the box's leading
  // face.
  float inC[3] = {boxC[0] - f[0] * halfLen, boxC[1] - f[1] * halfLen,
                  boxC[2] - f[2] * halfLen};
  float a1[3], a2[3], a3[3], a4[3];
  for (int k = 0; k < 3; ++k) {
    a1[k] = inC[k] + rgt[k] * halfCross + upv[k] * halfCross;
    a2[k] = inC[k] - rgt[k] * halfCross + upv[k] * halfCross;
    a3[k] = inC[k] - rgt[k] * halfCross - upv[k] * halfCross;
    a4[k] = inC[k] + rgt[k] * halfCross - upv[k] * halfCross;
  }
  pushSeg3(inletSegs, a1, a2);
  pushSeg3(inletSegs, a2, a3);
  pushSeg3(inletSegs, a3, a4);
  pushSeg3(inletSegs, a4, a1);
  pushSeg3(inletSegs, a1, a3);
  pushSeg3(inletSegs, a2, a4);

  // Outlet plane (issue #24/#25): the downstream box face got no marker, so it
  // was unclear where flow exits. Stamp the same rectangle + diagonal cross at
  // the trailing face centre. It shares the inletSegs batch (drawn in the
  // flow-inlet colour) but sits 6r downstream of the inlet, so the two read as
  // clearly separate planes rather than one glyph.
  float outC[3] = {boxC[0] + f[0] * halfLen, boxC[1] + f[1] * halfLen,
                   boxC[2] + f[2] * halfLen};
  float o1[3], o2[3], o3[3], o4[3];
  for (int k = 0; k < 3; ++k) {
    o1[k] = outC[k] + rgt[k] * halfCross + upv[k] * halfCross;
    o2[k] = outC[k] - rgt[k] * halfCross + upv[k] * halfCross;
    o3[k] = outC[k] - rgt[k] * halfCross - upv[k] * halfCross;
    o4[k] = outC[k] + rgt[k] * halfCross - upv[k] * halfCross;
  }
  pushSeg3(inletSegs, o1, o2);
  pushSeg3(inletSegs, o2, o3);
  pushSeg3(inletSegs, o3, o4);
  pushSeg3(inletSegs, o4, o1);
  pushSeg3(inletSegs, o1, o3);
  pushSeg3(inletSegs, o2, o4);

  // Flow arrows: a 3x3 grid announcing the incoming stream, kept fully UPSTREAM
  // of the inlet face (tips just short of it) and much shorter than the
  // old 1.2r spears that pierced the model. Heads are capped so the glyphs
  // never shout over the part being analysed. Issue #25: the arrow length now
  // tracks the inlet speed — a fast stream reads visibly longer than a slow one
  // — normalized around a nominal ~20 m/s and clamped so it stays readable.
  const float spd = std::max(0.0f, (float)s.flowSpeed);
  const float aLen = (0.32f + 0.50f * std::min(spd / 20.0f, 1.4f)) * r;
  const float headLen = std::min(0.28f * aLen, 0.08f * r);
  for (int iu = -1; iu <= 1; ++iu)
    for (int iv = -1; iv <= 1; ++iv) {
      float tail[3];
      for (int k = 0; k < 3; ++k)
        tail[k] = inC[k] - f[k] * 0.6f * r + rgt[k] * (0.7f * halfCross * iu) +
                  upv[k] * (0.7f * halfCross * iv);
      pushArrow3(arrowSegs, tail, f, aLen, arrowTris, headLen);
    }
}

bool SimManager::flowResultLinesActive(AppState &st) const {
  if (st.leftPaneTab != AppState::LP_Simulation)
    return false;
  if (sel_ < 0 || sel_ >= (int)studies_.size())
    return false;
  const Study &s = studies_[sel_];
  return isFlowFamily(s.type) && node_ == SimNode::Results && s.hasResult;
}

void SimManager::rebuildFlowLines(AppState &st, Study &s) {
  for (int b = 0; b < kFlowBands; ++b) {
    flowStreamSegs_[b].clear();
    flowSliceSegs_[b].clear();
  }
  flowLinesBuiltFor_.clear();
  if (s.overlayPath.empty())
    return;
  // Overlay is normally an aicm-shm:// URI (see applyResultField) —
  // filesystem::exists() would wrongly reject it and drop every streamline.
  // Precheck existence only for real file paths.
  if (!aicad::aicm_handoff::is_shm_uri(s.overlayPath)) {
    std::error_code ec;
    if (!std::filesystem::exists(s.overlayPath, ec))
      return;
  }
  render::DocMesh doc;
  std::string err;
  if (!render::loadAicmFile(s.overlayPath, doc, err))
    return;
  double vmax = (s.vmaxMps > 1e-6)
                    ? s.vmaxMps
                    : ((s.flowSpeed * 1.5 > 1e-6) ? s.flowSpeed * 1.5 : 1.0);
  flowLinesVmax_ = vmax;
  auto bandOf = [&](float spd) -> int {
    int bi = (int)((spd / (float)vmax) * kFlowBands);
    return bi < 0 ? 0 : (bi >= kFlowBands ? kFlowBands - 1 : bi);
  };
  // Issue #26: streamlines carry NO flow-direction cue. We stamp small
  // wireframe arrow "barbs" along each streamline (every kArrowEvery segments)
  // pointing the local flow direction, pushed into the SAME per-band buffer so
  // they inherit the band colour and the show-streamlines toggle, and are drawn
  // by the existing per-band drawSegments caller (viewport_panel). They are
  // wireframe (two barb segs) rather than solid cones because the results draw
  // path only draws segments per band — filled cones would need a per-band
  // drawTriangles call the read-only caller doesn't make. Barb size is a small
  // fraction of the model diagonal (NOT the local segment length, which can be
  // near-zero) so heads stay legible and uniform.
  float lo[3] = {1e30f, 1e30f, 1e30f}, hi[3] = {-1e30f, -1e30f, -1e30f};
  bool anyF = false;
  for (const auto &kv : st.faceNormalCache) {
    for (int k = 0; k < 3; ++k) {
      lo[k] = std::min(lo[k], kv.second[3 + k]);
      hi[k] = std::max(hi[k], kv.second[3 + k]);
    }
    anyF = true;
  }
  float mdiag = 100.0f;
  if (anyF) {
    mdiag = std::sqrt((hi[0] - lo[0]) * (hi[0] - lo[0]) +
                      (hi[1] - lo[1]) * (hi[1] - lo[1]) +
                      (hi[2] - lo[2]) * (hi[2] - lo[2]));
    if (mdiag < 1e-3f)
      mdiag = 100.0f;
  }
  const float barbLen = 0.018f * mdiag; // barb leg length
  const int kArrowEvery =
      6; // one arrowhead every N segments along a streamline
  for (const auto &ls : doc.lines) {
    const bool isSlice = (ls.name == "slices");
    const bool isStream = (ls.name == "streamlines");
    if (!isSlice && !isStream)
      continue;
    std::vector<float> *dst = isSlice ? flowSliceSegs_ : flowStreamSegs_;
    size_t p = 0;
    for (uint32_t li = 0; li < ls.pts_per_line.size(); ++li) {
      uint32_t cnt = ls.pts_per_line[li];
      for (uint32_t i = 0; i + 1 < cnt; ++i) {
        size_t a = p + i, c = p + i + 1;
        if ((c * 3 + 2) >= ls.pts.size())
          break;
        float spd = 0.0f;
        if (ls.has_scalar && c < ls.scalar.size())
          spd = 0.5f * (ls.scalar[a] + ls.scalar[c]);
        int bi = bandOf(spd);
        const float *pa = &ls.pts[a * 3];
        const float *pc = &ls.pts[c * 3];
        dst[bi].insert(dst[bi].end(),
                       {pa[0], pa[1], pa[2], pc[0], pc[1], pc[2]});
        // Sample a direction barb sparsely along streamlines only.
        if (isStream && (i % kArrowEvery) == (uint32_t)(kArrowEvery / 2)) {
          float d[3] = {pc[0] - pa[0], pc[1] - pa[1], pc[2] - pa[2]};
          float dl = std::sqrt(d[0] * d[0] + d[1] * d[1] + d[2] * d[2]);
          if (dl > 1e-6f) {
            d[0] /= dl;
            d[1] /= dl;
            d[2] /= dl;
            float perp[3];
            perp3(d, perp); // any unit vector normal to d
            // Barbs sweep back from the tip (pc) at ~30 deg on either side.
            for (int sgn = -1; sgn <= 1; sgn += 2) {
              float b[3] = {
                  pc[0] - d[0] * barbLen + perp[0] * barbLen * 0.5f * sgn,
                  pc[1] - d[1] * barbLen + perp[1] * barbLen * 0.5f * sgn,
                  pc[2] - d[2] * barbLen + perp[2] * barbLen * 0.5f * sgn};
              dst[bi].insert(dst[bi].end(),
                             {pc[0], pc[1], pc[2], b[0], b[1], b[2]});
            }
          }
        }
      }
      p += cnt;
    }
  }
  flowLinesBuiltFor_ = s.id;
}

void SimManager::collectFlowResultLines(AppState &st,
                                        std::vector<FlowLineBatch> &out) {
  if (sel_ < 0 || sel_ >= (int)studies_.size())
    return;
  Study &s = studies_[sel_];
  if (flowLinesBuiltFor_ != s.id)
    rebuildFlowLines(st, s);
  for (int b = 0; b < kFlowBands; ++b) {
    if (flowShowStreamlines_ && !flowStreamSegs_[b].empty()) {
      FlowLineBatch fb;
      fb.segs = &flowStreamSegs_[b];
      for (int k = 0; k < 4; ++k)
        fb.color[k] = kFlowBandCol[b][k];
      out.push_back(fb);
    }
    if (flowShowSlices_ && !flowSliceSegs_[b].empty()) {
      FlowLineBatch fb;
      fb.segs = &flowSliceSegs_[b];
      for (int k = 0; k < 4; ++k)
        fb.color[k] = kFlowBandCol[b][k];
      fb.color[3] *= 0.55f; // slices a touch fainter than streamlines
      out.push_back(fb);
    }
  }
}

// ---------------------------------------------------------------- run (ported
// from SimPanel)

nlohmann::json SimManager::facePointsFor(AppState &st,
                                         const nlohmann::json &selector,
                                         std::string &err) {
  nlohmann::json result;
  // Strip the host-only "_pick" hint before sending to the engine.
  nlohmann::json sel = selector;
  if (sel.is_object())
    sel.erase("_pick");
  nlohmann::json params = {{"doc_id", st.docMirror.docId()}, {"selector", sel}};
  if (!st.worker.call("doc_face_points", params, result, err, 30000))
    return {};
  if (result.contains("code")) {
    err = result.value("message", std::string("face_points error"));
    return {};
  }
  return result;
}

void SimManager::run(AppState &st, Study &s) {
  // Always re-seed material from the part so a FeatureManager / Mass Props
  // change is honored.
  resolveStudyMaterialFromDoc(st, s);
#ifdef __EMSCRIPTEN__
  if (isFlowFamily(s.type) || s.type == StudyType::Buckling ||
      s.type == StudyType::Contact || s.type == StudyType::FreqResponse) {
    st.toast(ui::ToastLevel::Warn, "not available in the web version yet");
    return;
  }
#endif
  if (s.type == StudyType::Motion)
    runMotion(st, s);
  else if (isFlowFamily(s.type))
    runFlow(st, s);
  else if (s.type == StudyType::Fatigue)
    runFatigue(st, s);
  else if (s.type == StudyType::ThermalFea)
    runThermalFea(st, s);
  else if (s.type == StudyType::Buckling)
    runBuckling(st, s);
  else if (s.type == StudyType::Contact)
    runContact(st, s);
  else if (s.type == StudyType::FreqResponse)
    runFreqResponse(st, s);
  else
    runStaticOrModal(st, s);
}

void SimManager::runFatigue(AppState &st, Study &s) {
  if (!st.simClient)
    return;
  auto *sim = st.simClient;
  if (!st.docMirror.hasDoc()) {
    st.addLog("[sim] no document to analyse");
    return;
  }

  const std::string stepPath =
      engine::daemonMode() ? std::string()
                           : (appdataAppDir() + "/cache/sim_fatigue.step");
  {
    nlohmann::json r,
        p = {{"doc_id", st.docMirror.docId()}, {"path", stepPath}};
    std::string e;
    if (!st.worker.call("doc_export_step", p, r, e, 60000) ||
        r.contains("code")) {
      st.addLog("[sim] export STEP failed: " +
                (r.contains("code") ? r.value("message", e) : e));
      st.toast(ui::ToastLevel::Error, "Simulation: export failed");
      return;
    }
    lastStepPath_ = r.value("path", stepPath);
  }

  nlohmann::json fixtures = nlohmann::json::array();
  for (const auto &p : s.fixtures.picks) {
    std::string e;
    auto fp = facePointsFor(st, p.selector, e);
    if (!fp.is_null() && fp.contains("points"))
      fixtures.push_back(fp);
  }
  if (fixtures.empty()) {
    st.addLog("[sim] add at least one fixture face before running");
    st.toast(ui::ToastLevel::Warn, "Add a fixture before running");
    return;
  }

  int mi = materialIndexByKey(s.materialKey);
  double matSut = 0, matSe = 0, matSy = 0;
  nlohmann::json material = nlohmann::json::object();
  if (mi >= 0) {
    const auto &m = materials()[mi];
    matSut = m.ultimate_mpa;
    matSe = m.endurance_mpa;
    matSy = m.yield_mpa;
    material = {{"E_mpa", m.E_mpa},
                {"nu", m.nu},
                {"density_g_cm3", m.density_g_cm3},
                {"yield_mpa", m.yield_mpa},
                {"ultimate_mpa", m.ultimate_mpa},
                {"endurance_mpa", m.endurance_mpa}};
  }
  const double sutUsed = s.fatigueSut > 0 ? s.fatigueSut : matSut;
  if (sutUsed <= 0) {
    st.toast(ui::ToastLevel::Warn, "Set an ultimate strength (Sut) for this "
                                   "material before a fatigue run");
    st.addLog("[sim] fatigue: material has no ultimate strength (Sut)");
    return;
  }
  // Marin-corrected endurance limit (host owns the correction; engine just uses
  // Se_corrected).
  const double seBaseV = seBase(s, matSe, sutUsed);
  const double ka = marin_ka(sutUsed, s.fatigueSurface),
               kb = marin_kb(s.fatigueSizeMm, s.fatigueLoadCase),
               kc = marin_kc(s.fatigueLoadCase), kd = marin_kd(s.fatigueTempC);
  const double seCorr = ka * kb * kc * kd * seBaseV;
  float lmin, lmax;
  fatigueLoadFactors(s, lmin, lmax);

  nlohmann::json loads = nlohmann::json::array();
  for (const auto &ld : s.loads) {
    nlohmann::json faces = nlohmann::json::array();
    for (const auto &p : ld.faces.picks) {
      std::string e;
      auto fp = facePointsFor(st, p.selector, e);
      if (!fp.is_null() && fp.contains("points")) {
        if (faces.empty())
          faces = fp;
        else
          for (auto &pt : fp["points"])
            faces["points"].push_back(pt);
      }
    }
    if (faces.empty())
      continue;
    nlohmann::json l = {{"faces", faces}};
    if (ld.pressure) {
      l["type"] = "pressure";
      l["magnitude"] = ld.pressureMpa;
    } else {
      l["type"] = "force";
      l["vector"] = {ld.vec[0], ld.vec[1], ld.vec[2]};
    }
    loads.push_back(l);
  }

  nlohmann::json study = {
      {"step_path", lastStepPath_},
      {"material", material},
      {"fixtures", fixtures},
      {"loads", loads},
      {"fatigue",
       {{"lmin", lmin},
        {"lmax", lmax},
        {"se_corrected_mpa", seCorr},
        {"se_base_mpa", seBaseV},
        {"ultimate_mpa", sutUsed},
        {"yield_mpa", matSy},
        {"marin", {{"ka", ka}, {"kb", kb}, {"kc", kc}, {"kd", kd}}}}},
  };
  addMeshParams(study, s, s.meshOrder); // 2nd-order elements keep the fatigue
                                        // stress field accurate.
  std::string note;
  if (sim->launch("sim_run_fatigue", {{"study", study}}, note,
                  st.docMirror.docId())) {
    clearTransientResult(s);
    clearMeshPreview();
    resultShown_ = false;
    shownStudyId_.clear();
    running_ = true;
    runningStudy_ = sel_;
    runningStudyId_ = s.id;
  }
  st.addLog(note);
}

void SimManager::runStaticOrModal(AppState &st, Study &s) {
  if (!st.simClient)
    return;
  auto *sim = st.simClient;
  if (!st.docMirror.hasDoc()) {
    st.addLog("[sim] no document to analyse");
    return;
  }
  if (prepInFlight_ || sim->busy()) {
    st.toast(ui::ToastLevel::Info, "A simulation is already in progress");
    return;
  }

  // Snapshot study inputs on the UI thread, then run export + face_points +
  // launch off-thread so the GL/main thread never blocks on 60s doc_export_step
  // / N× doc_face_points (WASM-critical).
  const std::string docId = st.docMirror.docId();
  const std::string studyId = s.id;
  const StudyType stype = s.type;
  const int nModes = s.nModes;
  const int meshOrder = s.meshOrder;
  const std::string materialKey = s.materialKey;
  const bool meshHex = s.meshHex;
  const float meshFineness = s.meshFineness;
  const bool meshCurvature = s.meshCurvature;
  const float meshCurvPerCircle = s.meshCurvPerCircle;
  const float meshTargetMm = s.meshTargetMm;
  const float meshMinMm = s.meshMinMm;
  const int meshAlgo = s.meshAlgo;
  const bool meshOptimize = s.meshOptimize;

  struct FacePick {
    nlohmann::json selector;
  };
  struct LoadSnap {
    bool pressure = false;
    float vec[3] = {0, 0, -100};
    float pressureMpa = 0.1f;
    std::vector<FacePick> faces;
  };
  std::vector<FacePick> fixPicks;
  for (const auto &p : s.fixtures.picks)
    fixPicks.push_back({p.selector});
  std::vector<LoadSnap> loadSnaps;
  if (stype == StudyType::Static) {
    for (const auto &ld : s.loads) {
      LoadSnap ls;
      ls.pressure = ld.pressure;
      ls.vec[0] = ld.vec[0];
      ls.vec[1] = ld.vec[1];
      ls.vec[2] = ld.vec[2];
      ls.pressureMpa = ld.pressureMpa;
      for (const auto &p : ld.faces.picks)
        ls.faces.push_back({p.selector});
      loadSnaps.push_back(std::move(ls));
    }
  }

  nlohmann::json material = nlohmann::json::object();
  int mi = materialIndexByKey(materialKey);
  if (mi >= 0) {
    const auto &m = materials()[mi];
    material = {{"E_mpa", m.E_mpa},
                {"nu", m.nu},
                {"density_g_cm3", m.density_g_cm3},
                {"yield_mpa", m.yield_mpa}};
  }

  // Mark busy immediately so the Run button disables while prep runs.
  prepInFlight_ = true;
  clearTransientResult(s);
  clearMeshPreview();
  resultShown_ = false;
  shownStudyId_.clear();
  running_ = true;
  runningStudy_ = sel_;
  runningStudyId_ = s.id;
  st.status = "Sim: preparing\xe2\x80\xa6";
  st.statusKind = 0;

  // Capture mesh-option fields the addMeshParams helper would read from Study.
  Study meshOpts;
  meshOpts.meshHex = meshHex;
  meshOpts.meshFineness = meshFineness;
  meshOpts.meshCurvature = meshCurvature;
  meshOpts.meshCurvPerCircle = meshCurvPerCircle;
  meshOpts.meshTargetMm = meshTargetMm;
  meshOpts.meshMinMm = meshMinMm;
  meshOpts.meshAlgo = meshAlgo;
  meshOpts.meshOptimize = meshOptimize;
  meshOpts.meshOrder = meshOrder;

  std::thread([this, &st, sim, docId, studyId, stype, nModes, material,
               fixPicks, loadSnaps, meshOpts]() {
    auto failPrep = [this, &st](const std::string &msg) {
      prepInFlight_ = false;
      running_ = false;
      st.toast(ui::ToastLevel::Error, msg);
    };

    // Empty path under daemon/WASM → server temp; desktop non-daemon keeps
    // appdata cache path.
    const std::string stepPath =
        engine::daemonMode() ? std::string()
                             : (appdataAppDir() + "/cache/sim_study.step");
    nlohmann::json r, p = {{"doc_id", docId}, {"path", stepPath}};
    std::string e;
    if (!st.worker.call("doc_export_step", p, r, e, 60000) ||
        r.contains("code")) {
      failPrep("Simulation: export failed");
      return;
    }
    const std::string resolved = r.value("path", stepPath);
    lastStepPath_ = resolved;

    auto facePts = [&](const nlohmann::json &selector,
                       std::string &err) -> nlohmann::json {
      nlohmann::json result;
      nlohmann::json sel = selector;
      if (sel.is_object())
        sel.erase("_pick");
      nlohmann::json params = {{"doc_id", docId}, {"selector", sel}};
      if (!st.worker.call("doc_face_points", params, result, err, 30000))
        return {};
      if (result.contains("code")) {
        err = result.value("message", std::string("face_points error"));
        return {};
      }
      return result;
    };

    nlohmann::json fixtures = nlohmann::json::array();
    for (const auto &fpick : fixPicks) {
      std::string fe;
      auto fp = facePts(fpick.selector, fe);
      if (!fp.is_null() && fp.contains("points"))
        fixtures.push_back(fp);
    }
    if (fixtures.empty()) {
      failPrep("Add a fixture before running");
      return;
    }

    nlohmann::json study = {
        {"step_path", resolved},
        {"material", material},
        {"fixtures", fixtures},
    };
    Study meshStudy = meshOpts; // non-const for addMeshParams
    addMeshParams(study, meshStudy,
                  stype == StudyType::Static ? meshOpts.meshOrder : 1,
                  /*allowHex=*/stype == StudyType::Static);

    std::string note;
    bool launched = false;
    if (stype == StudyType::Static) {
      nlohmann::json loads = nlohmann::json::array();
      for (const auto &ld : loadSnaps) {
        nlohmann::json faces = nlohmann::json::array();
        for (const auto &pck : ld.faces) {
          std::string fe;
          auto fp = facePts(pck.selector, fe);
          if (!fp.is_null() && fp.contains("points")) {
            if (faces.empty())
              faces = fp;
            else
              for (auto &pt : fp["points"])
                faces["points"].push_back(pt);
          }
        }
        if (faces.empty())
          continue;
        nlohmann::json l = {{"faces", faces}};
        if (ld.pressure) {
          l["type"] = "pressure";
          l["magnitude"] = ld.pressureMpa;
        } else {
          l["type"] = "force";
          l["vector"] = {ld.vec[0], ld.vec[1], ld.vec[2]};
        }
        loads.push_back(l);
      }
      study["loads"] = loads;
      launched = sim->launch("sim_run_static", {{"study", study}}, note, docId);
    } else {
      launched =
          sim->launch("sim_run_modal", {{"study", study}, {"n_modes", nModes}},
                      note, docId);
    }
    prepInFlight_ = false;
    if (!launched) {
      running_ = false;
      st.toast(ui::ToastLevel::Error,
               note.empty() ? std::string("Simulation failed to start") : note);
    } else {
      // Prefer toast over addLog from a worker thread (addLog is not locked).
      st.toast(ui::ToastLevel::Info,
               note.empty() ? "Simulation started" : note);
    }
    (void)studyId;
  }).detach();
}

void SimManager::runFlow(AppState &st, Study &s) {
  if (!st.simClient)
    return;
  auto *sim = st.simClient;
  if (!st.docMirror.hasDoc()) {
    st.addLog("[sim] no document to analyse");
    return;
  }
  const std::string stlPath = engine::daemonMode()
                                  ? std::string(".stl")
                                  : (appdataAppDir() + "/cache/sim_flow.stl");
  nlohmann::json r, p = {{"doc_id", st.docMirror.docId()}, {"path", stlPath}};
  std::string e;
  if (!st.worker.call("doc_export_step", p, r, e, 60000) ||
      r.contains("code")) {
    st.addLog("[sim] export STL failed: " +
              (r.contains("code") ? r.value("message", e) : e));
    return;
  }
  lastStepPath_ = r.value("path", stlPath);
  static const int dirs[6][3] = {{1, 0, 0},  {-1, 0, 0}, {0, 1, 0},
                                 {0, -1, 0}, {0, 0, 1},  {0, 0, -1}};
  static const char *presets[4] = {"coarse", "medium", "fine", "xfine"};
  int di = (s.flowDir < 0 || s.flowDir > 5) ? 0 : s.flowDir;
  int pi = (s.flowPreset < 0 || s.flowPreset > 3) ? 0 : s.flowPreset;
  float density, visc;
  flowFluidProps(s, density, visc);
  const char *fluidName = (s.flowFluid == 1)   ? "water"
                          : (s.flowFluid == 2) ? "custom"
                                               : "air";
  nlohmann::json params = {
      {"stl_path", lastStepPath_},
      {"direction", {dirs[di][0], dirs[di][1], dirs[di][2]}},
      {"speed_mps", s.flowSpeed},
      {"preset", presets[pi]},
      {"aoa_deg", s.flowAoA},
      {"sideslip_deg", s.flowSideslip},
      {"fluid",
       {{"name", fluidName},
        {"density_kgm3", density},
        {"kinematic_visc_m2s", visc}}},
      {"roughness_um", flowRoughnessUm(s)},
      {"ref_area_mm2", s.flowRefAreaMm2},
      {"solver", s.flowSolver == 1 ? "openfoam" : "lbm"},
  };
  // Thermal (heat-transfer) study — OpenFOAM only.
  if (s.flowSolver == 1 && s.flowThermal) {
    if (s.flowThermalType == 1) {
      // Conjugate heat transfer: solid part conducts an internal power
      // dissipation.
      params["thermal"] = {
          {"enabled", true},
          {"mode", "conjugate"},
          {"inlet_temp_c", s.flowInletTempC},
          {"solid_kappa", s.flowSolidKappa},
          {"power_w", s.flowPowerW},
      };
    } else {
      // Fluid heat transfer: wall thermal BC on the whole part surface.
      const char *wallMode = (s.flowWallMode == 0)   ? "temp"
                             : (s.flowWallMode == 1) ? "flux"
                                                     : "power";
      params["thermal"] = {
          {"enabled", true},
          {"mode", "fluid"},
          {"inlet_temp_c", s.flowInletTempC},
          {"wall_mode", wallMode},
          {"wall_temp_c", s.flowWallTempC},
          {"wall_flux_w_m2", s.flowWallFluxWm2},
          {"wall_power_w", s.flowWallPowerW},
      };
    }
  }
  std::string note;
  if (sim->launch("sim_run_flow", params, note, st.docMirror.docId())) {
    clearTransientResult(s);
    clearMeshPreview();
    resultShown_ = false;
    shownStudyId_.clear();
    running_ = true;
    runningStudy_ = sel_;
    runningStudyId_ = s.id;
  }
  st.addLog(note);
}

// ---------------------------------------------------------------- tick (drain,
// owner-gated)

void SimManager::clearResultOverlay(AppState &st) {
  st.colormapOn = false;
  st.legendOn = false;
  st.docMeshHasScalar = false;
  st.docMeshHasDisp = false;
  clearMeshPreview();
  pendingOverlayStudyId_.clear();
  if (st.renderer) {
    st.renderer->setColormapMode(false);
    st.renderer->setDeformScale(0.0f);
  }
  // The overlay is no longer on the renderer; syncResultView re-applies it if
  // Results is still up.
  resultShown_ = false;
  shownStudyId_.clear();
  // Release cached motion tessellation (populated in runMotion).
  motionDocMesh_ = render::DocMesh{};
}

void SimManager::restoreModelMesh(AppState &st) {
  clearResultOverlay(st);
  pendingOverlayStudyId_.clear();
  if (!st.docMirror.hasDoc())
    return;
  // The result overlay REPLACED the doc mesh with the FEA surface (no pickable
  // faces). Re-tessellate the current document so the editable, face-bearing
  // model mesh comes back. activate() fires the mirror listener; suppress the
  // tab-dirty marker around it (restoring a view must not "modify" the
  // document), exactly like switchToTab does.
  const bool prevSuppress = st.suppressTabDirty;
  st.suppressTabDirty = true;
  std::string aicm, err;
  if (st.docMirror.activate(st.worker, st.docMirror.docId(), aicm, err) &&
      !aicm.empty()) {
#ifdef __EMSCRIPTEN__
    st.loadDocMeshAsync(aicm);
#else
    std::string le;
    if (!st.loadDocMesh(aicm, le) && !le.empty())
      st.addLog("[sim] restore model mesh: " + le);
#endif
  } else if (!err.empty()) {
    st.addLog("[sim] restore model mesh failed: " + err);
  }
  st.suppressTabDirty = prevSuppress;
}

void SimManager::syncResultView(AppState &st) {
  // Show the result overlay ONLY while the user is in the Simulate WORKSPACE
  // and the selected study's Results node is up. Any other focus — a different
  // sub-node, a different study, or switching to the Model / Manufacture /
  // Drawing / Electronics workspace — restores the editable model so the part
  // can be edited (the FEA result mesh has no pickable faces).
  //
  // NB: gate on activeWorkspace, NOT leftPaneTab. leftPaneTab==LP_Simulation
  // persists after the user clicks away to another workspace tab (title_bar
  // only flips activeWorkspace), so keying off it left the deformed overlay
  // stuck on the Modeling viewport. activeWorkspace is what the workspace tabs
  // actually set, so it hides/restores correctly on every workspace switch.
  const bool valid = sel_ >= 0 && sel_ < (int)studies_.size();
  const bool onSimWs = st.activeWorkspace == AppState::Workspace::Simulate;

  // Motion studies manage the viewport via applyMotionFrame (setMeshes); they
  // don't use the overlay path and must not trigger restoreModelMesh while
  // their Results node is active.
  const bool motionActive = valid && studies_[sel_].type == StudyType::Motion &&
                            studies_[sel_].motionHasResult && onSimWs &&
                            node_ == SimNode::Results;
  if (motionActive) {
    resultShown_ = true;
    shownStudyId_ = studies_[sel_].id;
    return;
  }

  const bool wantShow = valid && onSimWs && node_ == SimNode::Results &&
                        studies_[sel_].hasResult &&
                        !studies_[sel_].overlayPath.empty();
  if (wantShow) {
    if (!resultShown_ || shownStudyId_ != studies_[sel_].id) {
      applyResultField(st, studies_[sel_]);
      resultShown_ = true;
      shownStudyId_ = studies_[sel_].id;
    }
  } else if (resultShown_) {
    restoreModelMesh(st);
    resultShown_ = false;
    shownStudyId_.clear();
  }
}

void SimManager::tick(AppState &st) {
  syncResultView(st); // keep the viewport in step with the result/model view
                      // (every frame, cheap)
  tickMotion(st);     // advance motion playback frame if playing
  // WASM: finish legend/deform once loadDocMeshAsync has committed the overlay
  // mesh.
  if (!pendingOverlayStudyId_.empty() &&
      (st.docMeshHasScalar || st.docMeshHasDisp)) {
    for (auto &ss : studies_) {
      if (ss.id != pendingOverlayStudyId_)
        continue;
      finishResultOverlay(st, ss);
      pendingOverlayStudyId_.clear();
      break;
    }
  }
  // EM gating: probe the em_* backend ONCE when the Simulate workspace is
  // active (async — the callback lands in a later st.rpc.drain(); no per-frame
  // or synchronous worker traffic).
  if (emStatus_ == 0 && !emProbeInflight_ &&
      st.activeWorkspace == AppState::Workspace::Simulate)
    probeEmStatus(st);
  // T-FIN24: probe the multiphysics capability doctor (ccx / handoff seams) +
  // fw_status the same way — once, async, only while the Simulate workspace is
  // up.
  if (capsStatus_ == 0 && !capsInflight_ &&
      st.activeWorkspace == AppState::Workspace::Simulate)
    probeCaps(st);
  if (fwStatus_ == 0 && !fwProbeInflight_ &&
      st.activeWorkspace == AppState::Workspace::Simulate)
    probeFwStatus(st);
  if (!st.simClient)
    return;
  auto *sim = st.simClient;
  auto &job = sim->job();

  // Only consume events/completion for jobs THIS document launched.
  const bool owns =
      (job.ownerDocId.empty() && running_) ||
      (st.docMirror.hasDoc() && job.ownerDocId == st.docMirror.docId());
  if (!owns)
    return;

  {
    std::vector<core::SimProgress> evs;
    {
      std::lock_guard<std::mutex> lk(job.evMtx);
      if (!job.events.empty())
        evs.swap(job.events);
    }
    for (auto &ev : evs) {
      if (!ev.text.empty())
        st.status = "Sim: " + ev.text +
                    (ev.pct ? " (" + std::to_string(ev.pct) + "%)" : "");
      st.statusKind = 0;
      if (ev.hasTrace) {
        int traceStudy = -1;
        if (!runningStudyId_.empty()) {
          for (int i = 0; i < (int)studies_.size(); ++i)
            if (studies_[i].id == runningStudyId_) {
              traceStudy = i;
              break;
            }
        }
        if (traceStudy < 0 && runningStudy_ >= 0 &&
            runningStudy_ < (int)studies_.size())
          traceStudy = runningStudy_;
        if (traceStudy >= 0)
          studies_[traceStudy].dragTrace.push_back((float)ev.traceDrag);
      }
    }
  }

  if (job.ready.exchange(false)) {
    running_ = false;
    std::lock_guard<std::mutex> lk(job.mtx);
    int si = -1;
    if (!runningStudyId_.empty()) {
      for (int i = 0; i < (int)studies_.size(); ++i)
        if (studies_[i].id == runningStudyId_) {
          si = i;
          break;
        }
    }
    if (si < 0)
      si = runningStudy_;
    runningStudyId_.clear();
    if (si < 0 || si >= (int)studies_.size())
      return;
    Study &s = studies_[si];
    if (!job.ok) {
      s.summary = "Run failed: " + job.error;
      st.status = s.summary;
      st.statusKind = 2;
      st.addLog("[sim] " + s.summary);
      return;
    }
    const auto &res = job.result;
    s.hasResult = true;
    clearMeshPreview(); // a fresh result supersedes any setup-time mesh preview
    // Echo whatever the solver reported it did (pressure area, fall-backs, …)
    // for transparency.
    s.solverNotes.clear();
    if (res.contains("solver_notes") && res["solver_notes"].is_array())
      for (const auto &n : res["solver_notes"])
        if (n.is_string())
          s.solverNotes.push_back(n.get<std::string>());
    if (isFlowFamily(s.type)) {
      s.dragN = res.value("drag_n", 0.0);
      s.liftN = res.value("lift_n", 0.0);
      s.sideN = res.value("side_n", 0.0);
      s.cd = res.value("cd", 0.0);
      s.cl = res.value("cl", 0.0);
      s.ld = res.value("ld", 0.0);
      s.reynolds = res.value("reynolds", 0.0);
      s.frontalMm2 = res.value("frontal_area_mm2", 0.0);
      s.refAreaMm2 = res.value("ref_area_mm2", s.frontalMm2);
      s.cpMin = res.value("cp_min", -1.0);
      s.cpMax = res.value("cp_max", 1.0);
      s.vmaxMps = res.value("vmax_mps", s.flowSpeed * 1.5);
      s.flowFields.clear();
      if (res.contains("scalar_fields") && res["scalar_fields"].is_array())
        for (const auto &f : res["scalar_fields"])
          if (f.is_string())
            s.flowFields.push_back(f.get<std::string>());
      // Thermal studies don't report drag; show the temperature/heat headline
      // from the banner.
      bool isThermal = false;
      for (const auto &f : s.flowFields)
        if (f == "temperature")
          isThermal = true;
      char buf[256];
      if (isThermal)
        std::snprintf(buf, sizeof(buf),
                      "Surface %.0f-%.0f degC | inlet %.0f degC", s.cpMin,
                      s.cpMax, s.flowInletTempC);
      else
        std::snprintf(buf, sizeof(buf),
                      "Drag %.3g N | Lift %.3g N | Cd %.3f | Cl %.3f", s.dragN,
                      s.liftN, s.cd, s.cl);
      s.summary = buf;
      s.banner = res.value("reynolds_note", std::string());
      // The overlay carries the MODEL (Cp-coloured) + streamline/slice lines,
      // so the part stays visible — flow now rides the same overlay/colormap
      // path as FEA instead of replacing the model with a body-less streamline
      // file.
      s.overlayPath =
          res.value("overlay_aicm", res.value("lines_aicm", std::string()));
      st.activeScalarField = isThermal ? "temperature" : "pressure_cp";
      applyResultField(st, s);
      resultShown_ = true;
      shownStudyId_ = s.id;
      rebuildFlowLines(st, s);
      st.addLog("[sim] flow done: " + s.summary);
    } else if (s.type == StudyType::ThermalFea) {
      // Conduction solve (steady or T-FIN18 transient): numeric summary only —
      // the core returns the nodal temperature field but packages no display
      // overlay (honest gap; the summary + flux audit are shown in Results).
      s.nNodes = res.value("n_nodes", 0);
      s.nElems = res.value("n_elems", 0);
      s.nDof = res.value("n_dof", 0);
      s.resultSummary = (res.contains("summary") && res["summary"].is_object())
                            ? res["summary"]
                            : nlohmann::json::object();
      nlohmann::json adv = nlohmann::json::object();
      adv["summary"] = s.resultSummary;
      if (res.contains("flux_summary"))
        adv["flux_summary"] = res["flux_summary"];
      if (res.contains("n_steps"))
        adv["n_steps"] = res["n_steps"];
      if (res.contains("transient"))
        adv["transient"] = res["transient"];
      if (res.contains("simhandoff"))
        adv["simhandoff"] = res["simhandoff"];
      s.advResult = adv;
      const auto &sm = s.resultSummary;
      char buf[256];
      if (res.value("transient", false))
        std::snprintf(
            buf, sizeof(buf),
            "peak %.1f degC @ %.1fs | final max %.1f degC | min %.1f degC",
            sm.value("max_temp_c", 0.0), sm.value("max_temp_time_s", 0.0),
            sm.value("final_max_temp_c", sm.value("max_temp_c", 0.0)),
            sm.value("min_temp_c", 0.0));
      else
        std::snprintf(buf, sizeof(buf), "max %.1f degC | min %.1f degC",
                      sm.value("max_temp_c", 0.0), sm.value("min_temp_c", 0.0));
      s.summary = buf;
      st.addLog("[sim] thermal FEA done: " + s.summary);
    } else if (s.type == StudyType::FreqResponse) {
      // Stage 1 (modal eigen solve) landed — hand the mode shapes to the
      // worker's vibration post-processor (modal superposition; async, panel
      // stays live).
      s.nNodes = res.value("n_nodes", 0);
      s.nDof = res.value("n_dof", 0);
      s.freqs.clear();
      if (res.contains("freqs_hz") && res["freqs_hz"].is_array())
        for (auto &v : res["freqs_hz"])
          s.freqs.push_back((float)v.get<double>());
      postFreqResponse(st, s, res);
    } else {
      // Mesh / system size for the Results metric table + solver transparency.
      s.nNodes = res.value("n_nodes", 0);
      s.nElems = res.value("n_elems", 0);
      s.nDof = res.value("n_dof", 0);
      s.resultSummary = (res.contains("summary") && res["summary"].is_object())
                            ? res["summary"]
                            : nlohmann::json::object();
      s.overlayPath = res.value("overlay_aicm", std::string());
      // Default the colormapped field for a fresh result + load it (one shared
      // apply path).
      st.activeScalarField = (s.type == StudyType::Modal)     ? "mode_amplitude"
                             : (s.type == StudyType::Fatigue) ? "fatigue_fos"
                                                              : "von_mises";
      applyResultField(
          st, s); // loads the overlay AND applies the auto deform scale
      // syncResultView ran at the top of this tick (old state); mark the
      // overlay shown for THIS study so it isn't redundantly reloaded next
      // frame. node_ is set to Results below.
      resultShown_ = true;
      shownStudyId_ = s.id;
      s.freqs.clear();
      if (s.type == StudyType::Modal && res.contains("freqs_hz") &&
          res["freqs_hz"].is_array()) {
        std::string f = "Frequencies (Hz):";
        for (auto &v : res["freqs_hz"]) {
          s.freqs.push_back((float)v.get<double>());
          char b[32];
          std::snprintf(b, sizeof(b), " %.1f", v.get<double>());
          f += b;
        }
        s.summary = f;
      } else if (s.type == StudyType::Fatigue && !s.resultSummary.empty()) {
        const auto &sm = s.resultSummary;
        const double SeC = sm.value("se_corrected_mpa", 0.0),
                     Sut = sm.value("sut_mpa", 0.0);
        const double SeD = reliability_ke(s.fatigueReliability) * SeC;
        const int m =
            (s.fatigueMethod < 0 || s.fatigueMethod > 4) ? 0 : s.fatigueMethod;
        double saeq = 0.0;
        if (sm.contains("theories") && sm["theories"].contains(kTheoryKeys[m]))
          saeq = sm["theories"][kTheoryKeys[m]].value("sa_eq_mpa", 0.0);
        const double fos = (saeq > 1e-9) ? SeD / saeq : 15.0;
        const double life = sn_life(saeq, SeD, Sut);
        char buf[256];
        if (life >= 1e6)
          std::snprintf(buf, sizeof(buf),
                        "%s FoS %.2f | life: infinite (>1e6 cycles)",
                        kTheoryNames[m], fos);
        else
          std::snprintf(buf, sizeof(buf), "%s FoS %.2f | life %.3g cycles",
                        kTheoryNames[m], fos, life);
        s.summary = buf;
      } else if (!s.resultSummary.empty()) {
        const auto &sm = s.resultSummary;
        char buf[256];
        std::snprintf(buf, sizeof(buf),
                      "max vM %.2f MPa | max disp %.4f mm | min FoS %.2f",
                      sm.value("max_von_mises_mpa", 0.0),
                      sm.value("max_disp_mm", 0.0), sm.value("min_fos", 0.0));
        s.summary = buf;
      }
      st.addLog("[sim] FEA done: " + s.summary);
    }
    // Cache the summary into the document so it reopens with the last-run
    // headline.
    persist(st, s);
    node_ = SimNode::Results;
  }
}

// ---- pre-flight readiness gate
// ------------------------------------------------- Returns true when the study
// is ready to run; populates `blocker` with the first unmet prerequisite when
// returning false. Flow / Thermal are always ready (no material or fixtures
// required by the solver — the flow pipeline handles them).
static bool studyReady(const Study &s, std::string &blocker) {
  if (s.type == StudyType::Motion)
    return true; // Motion only needs a doc + optional mate-id
#ifdef __EMSCRIPTEN__
  // Python-worker-only legs (flow/LBM/OpenFOAM, CalculiX proxies, vibration
  // post) are not available in the browser client yet — gate honestly rather
  // than failing mid-run.
  if (isFlowFamily(s.type) || s.type == StudyType::Buckling ||
      s.type == StudyType::Contact || s.type == StudyType::FreqResponse) {
    blocker = "not available in the web version yet";
    return false;
  }
#endif
  if (isFlowFamily(s.type))
    return true;
  // T-FIN24 kinds: the ccx proxies need only a material (E, nu — dims come from
  // the part bbox); thermal FEA needs at least one boundary condition;
  // frequency response mirrors modal (fixtures optional — free-free is
  // allowed).
  if (s.type == StudyType::Buckling || s.type == StudyType::Contact) {
    if (s.materialKey.empty()) {
      blocker = "Material required — the proxy solve reads E and nu from it.";
      return false;
    }
    return true;
  }
  if (s.type == StudyType::ThermalFea) {
    if (s.fixtures.empty() && s.loads.empty()) {
      blocker = "Add at least one thermal BC: a fixed-temperature face "
                "(Fixtures) or a "
                "flux/convection load.";
      return false;
    }
    return true;
  }
  if (s.type == StudyType::FreqResponse) {
    if (s.materialKey.empty()) {
      blocker = "Material required — mass and stiffness come from it.";
      return false;
    }
    return true;
  }
  if (s.materialKey.empty()) {
    blocker = "Material required — pick a material before running.";
    return false;
  }
  if (s.type != StudyType::Modal && s.fixtures.empty()) {
    blocker = "At least one fixed face is required to constrain the part.";
    return false;
  }
  if (s.type == StudyType::Fatigue) {
    int mi = SimManager::materialIndexByKey(s.materialKey);
    double matSut = (mi >= 0) ? SimManager::materials()[mi].ultimate_mpa : 0.0;
    if (s.fatigueSut <= 0.0f && matSut <= 0.0) {
      blocker = "Fatigue study needs ultimate strength (Sut) \xe2\x80\x94 set "
                "it in Fatigue parameters "
                "or choose a material with Sut data.";
      return false;
    }
  }
  return true;
}

// ============================================================ tree + form
// (drawTreeBody)

static const char *kPresetLabels[4] = {"Coarse  (~23 s)", "Medium  (~2 min)",
                                       "Fine  (~16 min)",
                                       "Extra fine  (~73 min)"};
static const char *kDirLabels[6] = {"+X", "-X", "+Y", "-Y", "+Z", "-Z"};

// A small status dot (teal ok / amber attention). Mirrors the feature tree's
// badge vocabulary.
static void simBadge(bool ok, const char *tip) {
  const auto &pal = ui::palette;
  ImGui::SameLine();
  ImGui::PushStyleColor(ImGuiCol_Text,
                        ok ? ImVec4(pal.statusOk[0], pal.statusOk[1],
                                    pal.statusOk[2], pal.statusOk[3])
                           : ImVec4(pal.statusWarn[0], pal.statusWarn[1],
                                    pal.statusWarn[2], pal.statusWarn[3]));
  ImGui::TextUnformatted(ok ? u8"•" : "?");
  ImGui::PopStyleColor();
  if (tip && *tip)
    simTip(tip);
}

void SimManager::drawTreeBody(AppState &st) {
  // Keep the mirror in step with the active document (cheap: re-pull only on a
  // doc/revision change).
  if (st.docMirror.hasDoc() && (lastDocId_ != st.docMirror.docId() ||
                                lastRevision_ != (long)st.docMirror.revision()))
    refresh(st);

  const float S = ui::uiScale;

  // v2: the Simulation tree body is embedded in the FeatureManager column,
  // whose glass surface layout.cpp already painted; push its ADAPTIVE text as
  // the pane-level ink for the whole body (mirrors feature_tree.cpp) so pane
  // text + row pills + banner/checklist cards all track the live viewport.
  // Popups (BeginPopup / modal) are floating — they re-pair via pushCardText().
  // Only pushed over a live backdrop; on chrome the theme Text role already
  // reads on glassBg.
  const bool paneInk = ui::viewportBackdrop().valid();
  if (paneInk)
    ImGui::PushStyleColor(ImGuiCol_Text, simSurf().textPrimary);
  struct PaneInkPop {
    bool on;
    ~PaneInkPop() {
      if (on)
        ImGui::PopStyleColor();
    }
  } paneInkPop{paneInk};

  // Compact vertical rhythm (match FeatureManager): tighten ItemSpacing.y only
  // so the study tree/rows don't read "double spaced". RAII so it unwinds on
  // the body's early returns too.
  ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing,
                      ImVec2(ImGui::GetStyle().ItemSpacing.x, 4.0f * S));
  struct SpacingPop {
    ~SpacingPop() { ImGui::PopStyleVar(); }
  } spacingPop;

  // ---- EM (electromagnetics) panel takes over the pane when open. EM is
  // standalone (no doc Study), so it must be handled BEFORE the study-panel
  // block below (which requires a valid sel_).
  if (emPanelOpen_) {
    drawEmPanel(st);
    return;
  }
  // T-FIN24 stateless panels (electronics SPICE / firmware emulation /
  // cross-sim handoff): same PM-style takeover as the EM panel.
  if (elPanelOpen_) {
    drawElectronicsPanel(st);
    return;
  }
  if (fwPanelOpen_) {
    drawEmulationPanel(st);
    return;
  }
  if (handoffPanelOpen_) {
    drawHandoffPanel(st);
    return;
  }

  // ---- sim-gui rework: tool-invoked setup panel (PropertyManager flow)
  // -------------------- When a ribbon tool was invoked (or a tree node
  // double-clicked) the matching setup editor TAKES OVER the pane body —
  // exactly how a modeling command's PM form replaces the feature tree. Closing
  // (OK / X) returns to the study tree. No more always-open numbered
  // accordions.
  if (panelOpen_ && (sel_ < 0 || sel_ >= (int)studies_.size()))
    panelOpen_ = false;
  if (panelOpen_) {
    Study &s = studies_[sel_];
    if (!drawPanelHeader(st, s)) {
      panelOpen_ = false;
      armedWell_ = -1;
    } else {
      drawForm(st, s);
    }
    return;
  }

  // ---- Single "+ New Study" entry point: opens a picker popup for the study
  // types. The per-type ribbon tabs' "New Study" buttons route to the same
  // newStudy path.
  if (ImGui::Button("+ New Study", ImVec2(-FLT_MIN, 0)) || shotForcePicker)
    ImGui::OpenPopup("##newStudyPicker");
  simTip("Add a simulation study. The offered kinds follow what you have open: "
         "mechanical "
         "studies for parts/assemblies, motion for mated assemblies, "
         "electronics simulation "
         "and firmware emulation when a schematic / PCB / firmware project is "
         "open.");

  const float S2 = ui::uiScale;
  ImGui::SetNextWindowBgAlpha(
      0.0f); // suppress the flat PopupBg — the glass panel replaces it
  ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                      ImVec2(10.0f * S2, 10.0f * S2));
  ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing, ImVec2(8.0f * S2, 3.0f * S2));
  if (ImGui::BeginPopup("##newStudyPicker")) {
    GlassPopupScope glass = beginGlassPopup("sim_new_study_popup");
    pushCardText(); // re-pair the adaptive ink for the frosted popup surface
    ui::fmRowStripeReset(); // refresh the FM palette + restart the row stripe
                            // phase

    // A study row creates an in-part study (newStudy) or routes into a
    // stateless panel (EM / electronics SPICE / firmware emulation). The
    // offered set is PROGRESSIVELY DISCLOSED by the open doc types (T-SIM10
    // spec / T-FIN24): mechanical kinds need a Part/Assembly, motion needs an
    // assembly, electronics + emulation appear only with a Schematic / PCB /
    // firmware project open. Kinds whose engine leg is missing render dimmed
    // with the doctor's guidance (fw_status pattern) — never dead buttons.
    enum class Kind { Fea, Em, El, Fw };
    struct SEntry {
      Kind kind;
      StudyType type;
      const char *name;
      const char *icon;
      std::string desc;
      bool disabled;
      std::string reason;
    };
    bool hasSch = false, hasPcb = false, hasAsm = false;
    for (const auto &t : st.docTabs) {
      if (t.kind == AppState::DocTab::TabKind::Schematic)
        hasSch = true;
      if (t.kind == AppState::DocTab::TabKind::PCB)
        hasPcb = true;
      if (t.kind == AppState::DocTab::TabKind::Assembly)
        hasAsm = true;
    }
    const bool mechDoc = st.docMirror.hasDoc();
    const bool ccxMissing = capsStatus_ == 1 && caps_.contains("ccx") &&
                            !caps_["ccx"].value("found", false);
    const std::string ccxHint =
        ccxMissing ? caps_["ccx"].value(
                         "hint", std::string("CalculiX (ccx) not found."))
                   : std::string();
#ifdef __EMSCRIPTEN__
    const bool webNoPy = true;
    const std::string webNoPyHint = "not available in the web version yet";
#else
    const bool webNoPy = false;
    const std::string webNoPyHint;
#endif
    std::vector<SEntry> kEntries;
    if (mechDoc) {
      kEntries.push_back({Kind::Fea, StudyType::Static, "Static",
                          studyTypeIcon(StudyType::Static),
                          "Linear stress, displacement and factor of safety.",
                          false, ""});
      kEntries.push_back({Kind::Fea, StudyType::Fatigue, "Fatigue",
                          studyTypeIcon(StudyType::Fatigue),
                          "Cyclic-load life (Goodman / Gerber / Soderberg).",
                          false, ""});
      kEntries.push_back({Kind::Fea, StudyType::Modal, "Frequency",
                          studyTypeIcon(StudyType::Modal),
                          "Natural frequencies and mode shapes.", false, ""});
      kEntries.push_back(
          {Kind::Fea, StudyType::FreqResponse, "Vibration",
           studyTypeIcon(StudyType::FreqResponse),
           "Harmonic base-excitation response (modal superposition).", webNoPy,
           webNoPy ? webNoPyHint : std::string()});
      kEntries.push_back({Kind::Fea, StudyType::Buckling, "Buckling",
                          studyTypeIcon(StudyType::Buckling),
                          "Critical load factors (CalculiX box proxy).",
                          webNoPy || ccxMissing,
                          webNoPy ? webNoPyHint : ccxHint});
      kEntries.push_back({Kind::Fea, StudyType::Contact, "Contact",
                          studyTypeIcon(StudyType::Contact),
                          "Nonlinear contact conservation check (CalculiX).",
                          webNoPy || ccxMissing,
                          webNoPy ? webNoPyHint : ccxHint});
      kEntries.push_back({Kind::Fea, StudyType::Flow, "Flow",
                          studyTypeIcon(StudyType::Flow),
                          "External aerodynamics: drag, lift, streamlines. "
                          "Fast (LBM) or accurate "
                          "(OpenFOAM).",
                          webNoPy, webNoPy ? webNoPyHint : std::string()});
      kEntries.push_back({Kind::Fea, StudyType::Thermal, "Thermal (CFD)",
                          studyTypeIcon(StudyType::Thermal),
                          "Heat transfer via OpenFOAM convection / CHT.",
                          webNoPy, webNoPy ? webNoPyHint : std::string()});
      kEntries.push_back({Kind::Fea, StudyType::ThermalFea, "Thermal (FEA)",
                          studyTypeIcon(StudyType::ThermalFea),
                          "Conduction on the part: fixed temps, flux, "
                          "convection; steady or transient.",
                          false, ""});
    }
    if (hasAsm)
      kEntries.push_back({Kind::Fea, StudyType::Motion, "Motion",
                          studyTypeIcon(StudyType::Motion),
                          "Drive a dimensional mate, play back the trajectory.",
                          false, ""});
    if (mechDoc)
      kEntries.push_back(
          {Kind::Em, StudyType::Static, "Electromagnetic", "cat_electronics",
           "Full-wave RF: eigenmode, S-parameters, time domain.", emStatus_ < 0,
           "EM solver not installed on this machine."});
    // Electronics-sim + emulation: disclosed ONLY with electronics docs /
    // firmware projects open (T-SIM10 rule 2).
    const bool fwProjects = fwsim_.projectsLoaded && !fwsim_.projectIds.empty();
    if (hasSch || hasPcb) {
      kEntries.push_back(
          {Kind::El, StudyType::Static, "Electronics", "cat_electronics",
           "Circuit simulation (SPICE): operating point, AC sweep, transient.",
           !hasSch,
           "SPICE runs on a Schematic — open the board's schematic tab."});
    }
    if (hasSch || hasPcb || fwProjects) {
      kEntries.push_back(
          {Kind::Fw, StudyType::Static, "Firmware emulation", "cat_electronics",
           "Run the firmware in an emulator (Renode / QEMU) with scenario "
           "assertions.",
           fwStatus_ == 1 && !fwCanEmulate_,
           fwHint_.empty()
               ? std::string("Emulation backend unavailable — install "
                             "Renode or qemu-system-arm.")
               : fwHint_});
    }

    // Title.
    ImGui::PushFont(st.fonts.medium);
    pushDimText();
    ImGui::TextUnformatted("New study");
    popDimText();
    ImGui::PopFont();
    ImGui::Dummy(ImVec2(0, 2.0f * S2));

    const float iconSz = 18.0f * S2;
    // Description column starts past the WIDEST name so no name (e.g.
    // "Electromagnetic") clips into its description.
    ImGui::PushFont(st.fonts.medium);
    float maxNameW = 0.0f;
    for (const auto &e : kEntries)
      maxNameW = std::max(maxNameW, ImGui::CalcTextSize(e.name).x);
    if (kEntries.empty()) {
      pushDimText();
      ImGui::TextWrapped(
          "Open a part, assembly, schematic, PCB or firmware project to "
          "see the study kinds it unlocks.");
      popDimText();
    }
    ImGui::PopFont();
    const float descOffset = iconSz + 8.0f * S2 + maxNameW + 14.0f * S2;
    Kind chosenKind = Kind::Fea;
    StudyType chosenType = StudyType::Static;
    bool picked = false;
    for (const auto &e : kEntries) {
      ImGui::PushID(e.name);
      ui::FmRowRect r = ui::fmRowPill(false);
      const float startX = ImGui::GetCursorPosX();
      ImGui::AlignTextToFramePadding();
      IconUV iv = getIcon(e.icon, 20);
      if (iv.valid)
        ImGui::Image(iv.texId, ImVec2(iconSz, iconSz), iv.uv0, iv.uv1);
      else
        ImGui::Dummy(ImVec2(iconSz, iconSz));
      ImGui::SameLine(0, 8.0f * S2);
      ImGui::PushFont(st.fonts.medium);
      ImGui::TextUnformatted(e.name);
      ImGui::PopFont();
      ImGui::SameLine(0, 0);
      ImGui::SetCursorPosX(startX + descOffset);
      pushDimText();
      ImGui::TextUnformatted(e.desc.c_str());
      popDimText();
      // Full-row hit target as the LAST item so fmRowPillEnd's hover paint (and
      // the click) cover the entire row, not just the description text under
      // the cursor.
      ImGui::SetCursorScreenPos(r.mn);
      const bool hit = ImGui::InvisibleButton(
          "##row", ImVec2(r.mx.x - r.mn.x, r.mx.y - r.mn.y));
      ui::fmRowPillEnd(r, false);
      if (e.disabled) {
        // Honest gating: the row stays visible but dimmed; hovering explains
        // the missing engine leg (doctor guidance), and the click is swallowed.
        if (ImGui::IsItemHovered())
          ImGui::SetTooltip("%s", e.reason.c_str());
      } else if (hit) {
        chosenKind = e.kind;
        chosenType = e.type;
        picked = true;
      }
      ImGui::PopID();
    }

    popCardText();
    glass.finish();
    // Act AFTER the loop so a routed EM command (which mutates panel state)
    // doesn't run mid-draw.
    if (picked) {
      if (chosenKind == Kind::Em)
        handleEmCommand(st, "sim_em_eigen");
      else if (chosenKind == Kind::El)
        openElectronicsPanel(st);
      else if (chosenKind == Kind::Fw)
        openEmulationPanel(st);
      else
        newStudy(st, chosenType);
      ImGui::CloseCurrentPopup();
    }
    ImGui::EndPopup();
  }
  ImGui::PopStyleVar(2);
  ImGui::Spacing();

  // ---- cross-sim handoffs (aicad.simhandoff/1 seam, T-FIN24)
  // ------------------------- One-click flows between domains, disclosed by the
  // open doc types. Drawn ABOVE the study tree so a board-only session still
  // reaches them.
  {
    int pcbTab = -1;
    for (int i = 0; i < (int)st.docTabs.size(); ++i)
      if (st.docTabs[i].kind == AppState::DocTab::TabKind::PCB &&
          !st.docTabs[i].ecadDocId.empty()) {
        pcbTab = i;
        break;
      }
    if (pcbTab >= 0) {
      ui::fmSectionDivider("Cross-sim handoffs");
      if (ImGui::Button("Send board to thermal FEA", ImVec2(-FLT_MIN, 0)))
        openHandoffPanel(st, pcbTab);
      simTip("ECAD -> thermal handoff (aicad.simhandoff/1): fuses the routed "
             "board into "
             "one FEA solid, applies per-component heat sources from the "
             "boardphys power "
             "metadata and environment convection, and solves the core "
             "conduction study.");
    }
  }

  if (!st.docMirror.hasDoc()) {
    pushDimText();
    ImGui::TextWrapped(
        "Open a part or assembly to set up mechanical studies. Electronics "
        "docs unlock circuit simulation and emulation in the picker above.");
    popDimText();
    return;
  }
  if (studies_.empty()) {
    ImGui::Separator();
    pushDimText(); // pane surface — dim the ADAPTIVE ink, not the theme textDim
                   // role
    ImGui::TextWrapped("No studies yet. Create a study above to add fixtures, "
                       "loads, and run an "
                       "analysis. Studies are saved inside this part file.");
    popDimText();
    return;
  }

  // ---- study tree (FM-grade rows; single-click selects, double-click opens
  // the editor) ----
  ui::fmRowStripeReset(); // restart stripe phase + refresh the FM palette for
                          // this pane body
  ui::fmSectionDivider("Studies");
  for (int i = 0; i < (int)studies_.size(); ++i)
    drawStudyRow(st, i);
}

// PM-style setup-panel header: study-type icon + "<study> - <section>" title
// and a right-aligned OK button (the sim setup forms persist edits immediately,
// so OK simply closes — same visual language as the PropertyManager's command
// header). Returns false once the user closes it.
bool SimManager::drawPanelHeader(AppState &st, Study &s) {
  const float S = ui::uiScale;
  bool keepOpen = true;

  const char *section = "Setup";
  switch (node_) {
  case SimNode::Material:
    section = "Material";
    break;
  case SimNode::Fixtures:
    section = "Fixtures";
    break;
  case SimNode::Load:
    section = "Loads";
    break;
  case SimNode::Mesh:
    section = "Mesh";
    break;
  case SimNode::Results:
    section = (s.type == StudyType::Motion) ? "Playback" : "Results";
    break;
  case SimNode::Study:
    switch (s.type) {
    case StudyType::Flow:
      section = "Flow Setup";
      break;
    case StudyType::Thermal:
      section = "Thermal Setup";
      break;
    case StudyType::Fatigue:
      section = "Loading / S-N";
      break;
    case StudyType::Motion:
      section = "Driver / Time";
      break;
    default:
      section = "Study";
      break;
    }
    break;
  }

  IconUV iv = getIcon(studyTypeIcon(s.type), 20);
  if (iv.valid) {
    ImGui::Image(iv.texId, ImVec2(16.0f * S, 16.0f * S), iv.uv0, iv.uv1);
    ImGui::SameLine(0, 6.0f * S);
  }
  ImGui::PushFont(st.fonts.medium);
  ImGui::TextUnformatted((s.name + " \xe2\x80\x94 " + section).c_str());
  ImGui::PopFont();

  // Right-aligned OK (close) button on the same row.
  const float btnW = 40.0f * S;
  const float rightX = ImGui::GetWindowContentRegionMax().x - btnW;
  if (rightX > ImGui::GetCursorPosX())
    ImGui::SameLine(rightX);
  else
    ImGui::SameLine();
  if (ImGui::Button("OK", ImVec2(btnW, 0)))
    keepOpen = false;
  simTip("Close this setup panel and return to the study tree. Edits are saved "
         "as you make them.");

  ImGui::Separator();
  ImGui::Spacing();
  return keepOpen;
}

void SimManager::drawStudyRow(AppState &st, int i) {
  Study &s = studies_[i];
  resolveStudyMaterialFromDoc(st, s);
  ImGui::PushID(i);
  const bool selStudy = (sel_ == i);

  // sim-gui rework (Hunter red-pen): FM-grade rows via the exported
  // feature_tree.h helpers — glass row pills, soft-blue selection card,
  // accent-outline hover, crisp icons at FM sizing. Single-click SELECTS
  // (drives fixture/load previews); double-click opens the setup panel (same
  // gesture as "Edit Feature" on the modeling tree). Pills must balance: one
  // End per Pill.
  ImGuiTreeNodeFlags flags =
      ImGuiTreeNodeFlags_OpenOnArrow | ImGuiTreeNodeFlags_SpanFullWidth |
      ImGuiTreeNodeFlags_DefaultOpen | ImGuiTreeNodeFlags_FramePadding;
  // NB: do NOT set ImGuiTreeNodeFlags_Selected. fmRowPill pushes
  // HeaderHovered/HeaderActive transparent but not ImGuiCol_Header, so a
  // Selected node paints ImGui's default header rect (its own frame geometry,
  // not the pill's GetFrameHeight()+content-avail rect) on top of the glass
  // card — a mis-sized/overflowing blue fill. Selection is shown by
  // fmRowPill(selected) alone, exactly like featureRow() and the origin/plane
  // rows in feature_tree.cpp.

  ui::FmRowRect studyRect = ui::fmRowPill(selStudy && node_ == SimNode::Study);
  ui::fmTreeIcon(studyTypeIcon(s.type), "[S]");
  ImGui::SameLine(0, 4.0f * ui::uiScale);
  std::string title = s.name + "  (" + studyTypeLabel(s.type) + ")";
  bool open = ImGui::TreeNodeEx("##study", flags, "%s", title.c_str());
  const bool studyDbl = ImGui::IsItemHovered() &&
                        ImGui::IsMouseDoubleClicked(ImGuiMouseButton_Left);
  if (ImGui::IsItemClicked() && !ImGui::IsItemToggledOpen()) {
    sel_ = i;
    node_ = SimNode::Study;
    armedWell_ = -1;
  }
  ui::fmRowPillEnd(studyRect, selStudy && node_ == SimNode::Study);
  if (studyDbl) {
    sel_ = i;
    node_ = SimNode::Study;
    armedWell_ = -1;
    panelOpen_ = true;
  }
  // Right-click: delete (deferred so we never mutate studies_ mid-row-draw).
  bool wantDelete = false;
  if (ImGui::BeginPopupContextItem("##studyctx")) {
    if (ImGui::MenuItem("Open setup")) {
      sel_ = i;
      node_ = SimNode::Study;
      panelOpen_ = true;
    }
    if (ImGui::MenuItem("Delete study"))
      wantDelete = true;
    ImGui::EndPopup();
  }

  if (open) {
    // One FM-grade leaf row: icon painted inside the leaf-arrow slot + label +
    // status badge. Single-click selects the node (previews); double-click
    // opens its setup panel and (for Fixtures / Loads) arms the face-pick well.
    auto child = [&](const char *icon, const std::string &label, SimNode n,
                     bool ok, const char *tip, int loadI = -1) {
      const bool selected =
          selStudy && node_ == n && (n != SimNode::Load || loadIdx_ == loadI);
      ui::FmRowRect rr = ui::fmRowPill(selected);
      ImGuiTreeNodeFlags lf =
          ImGuiTreeNodeFlags_Leaf | ImGuiTreeNodeFlags_NoTreePushOnOpen |
          ImGuiTreeNodeFlags_SpanFullWidth | ImGuiTreeNodeFlags_FramePadding;
      // No ImGuiTreeNodeFlags_Selected — the selection card is drawn by
      // fmRowPill(selected); the Selected flag would paint ImGui's mis-sized
      // default header rect over it (see the parent study row above and
      // featureRow()/plane rows in feature_tree.cpp).
      ImVec2 rowPos = ImGui::GetCursorScreenPos();
      ImGui::TreeNodeEx(
          (label + "##n" + std::to_string((int)n) + "_" + std::to_string(loadI))
              .c_str(),
          lf);
      ui::fmTreeIconInArrowSlot(rowPos, icon, "");
      const bool clicked = ImGui::IsItemClicked();
      const bool dbl = ImGui::IsItemHovered() &&
                       ImGui::IsMouseDoubleClicked(ImGuiMouseButton_Left);
      ui::fmRowPillEnd(rr, selected);
      if (clicked || dbl) {
        sel_ = i;
        node_ = n;
        loadIdx_ = loadI;
        if (dbl) {
          // Open the editor; arm the pick well for the selector-driven nodes.
          int newArm = (n == SimNode::Fixtures)
                           ? 0
                           : (n == SimNode::Load ? loadI + 1 : -1);
          if (newArm != armedWell_)
            st.selection.clear(); // drop stale picks
          armedWell_ = newArm;
          panelOpen_ = true;
        } else {
          armedWell_ = -1; // selection only: preview, no pick capture
        }
      }
      simBadge(ok, tip);
    };

    const std::string matName = [&] {
      int mi = materialIndexByKey(s.materialKey);
      if (mi >= 0)
        return materials()[mi].name;
      if (!s.materialKey.empty())
        return materialDisplayName(s.materialKey);
      return std::string("(not set)");
    }();
    // Material drives stiffness/density/yield for structural & frequency
    // studies. Flow/Thermal run the OpenFOAM pipeline (flowSolidKappa is set
    // directly in the form) and never read materialKey, so the node is hidden
    // for them — re-add here if a CHT material feature lands.
    if (!isFlowFamily(s.type))
      child("material", "Material: " + matName, SimNode::Material,
            !s.materialKey.empty(),
            s.materialKey.empty()
                ? "No material assigned — pick one before running."
                : "");
    // Thermal has no Material/Fixtures/Loads/Mesh nodes; give the tree a
    // meaningful entry point back to the thermal boundary-condition form (drawn
    // by the Study overview / drawFlowForm).
    if (s.type == StudyType::Thermal)
      child("study_thermal", "Heat source / wall BC", SimNode::Study, true,
            "Configure the thermal boundary condition in the study form.");
    // T-FIN24 kinds: type-level setup entry points. The ccx proxies have no
    // per-face setup (they solve a box proxy sized to the part), so
    // fixtures/loads/mesh nodes are hidden for them below.
    if (s.type == StudyType::Buckling)
      child(
          "study_static", "Proxy load / modes", SimNode::Study, true,
          "Compressive load and mode count for the eigenvalue buckling solve.");
    if (s.type == StudyType::Contact)
      child("study_static", "Contact pressure", SimNode::Study, true,
            "Pressure on the two-block contact proxy.");
    if (s.type == StudyType::FreqResponse)
      child("study_modal", "Excitation / damping", SimNode::Study, true,
            "Base-excitation axis, amplitude and modal damping.");
    if (s.type == StudyType::ThermalFea)
      child("study_thermal", "Conductivity / transient", SimNode::Study, true,
            "Solid conductivity and the optional transient time settings.");

    const bool proxyKind =
        (s.type == StudyType::Buckling || s.type == StudyType::Contact);
    if (!isFlowFamily(s.type) && !proxyKind) {
      bool hasFix = !s.fixtures.empty();
      child("fixture",
            (s.type == StudyType::ThermalFea ? "Fixed temp (" : "Fixtures (") +
                s.fixtures.label() + ")",
            SimNode::Fixtures, hasFix,
            hasFix ? ""
                   : (s.type == StudyType::Modal ||
                              s.type == StudyType::FreqResponse
                          ? "No fixtures — the modal basis will be free-free."
                      : s.type == StudyType::ThermalFea
                          ? "Optional: faces held at a fixed temperature."
                          : "A static study needs at least one fixed face."));
      // Loads (each its own row). Frequency (modal) eigenvalue solves ignore
      // loads entirely — they never reach runStaticOrModal's load array — so
      // the load nodes are hidden for modal studies. Fixtures + Mesh stay
      // (fixtures constrain rigid-body modes).
      if (s.type != StudyType::Modal && s.type != StudyType::FreqResponse) {
        for (int li = 0; li < (int)s.loads.size(); ++li) {
          const SimLoad &ld = s.loads[li];
          std::string lbl =
              s.type == StudyType::ThermalFea
                  ? std::string(ld.thermalKind == 1 ? "Convection "
                                                    : "Heat flux ") +
                        std::to_string(li + 1) + " (" + ld.faces.label() + ")"
                  : (ld.pressure ? "Pressure " : "Force ") +
                        std::to_string(li + 1) + " (" + ld.faces.label() + ")";
          child(ld.pressure ? "load_pressure" : "load_force", lbl,
                SimNode::Load, !ld.faces.empty(),
                ld.faces.empty() ? "This load has no faces selected." : "", li);
        }
      }
      child("mesh", "Mesh", SimNode::Mesh, true, "Mesh density for the solve.");
    }
    child("results", s.hasResult ? "Results" : "Results (not run)",
          SimNode::Results, s.hasResult,
          s.hasResult ? "" : "Run the study to generate results.");
    ImGui::TreePop();
  }
  ImGui::PopID();
  if (wantDelete)
    deleteStudy(st, i);
}

void SimManager::drawCustomMaterialPopup(AppState &st, Study &s) {
  if (ImGui::BeginPopupModal("New material", nullptr,
                             ImGuiWindowFlags_AlwaysAutoResize)) {
    const float S = ui::uiScale;
    pushCardText(); // modal fills with theme glassBg — re-pair the inherited
                    // adaptive text
    ImGui::TextUnformatted("Define a custom material");
    ImGui::TextDisabled("Saved to your library; available in every study.");
    ImGui::Separator();
    // Label-above + fixed-width fields (the file's form convention); bounded
    // physical quantities are drags/sliders so out-of-range values are
    // unreachable, not just flagged.
    auto label = [](const char *l) { ImGui::TextUnformatted(l); };
    label("Name");
    ImGui::SetNextItemWidth(240.0f * S);
    ImGui::InputText("##cmname", cmName_, sizeof(cmName_));
    label("Young's modulus E (MPa)");
    ImGui::SetNextItemWidth(240.0f * S);
    ImGui::DragFloat("##cme", &cmE_, 100.0f, 1.0f, 1.0e6f, "%.0f MPa");
    simTip("Elastic stiffness. Steel ~200000 MPa, aluminum ~69000 MPa, most "
           "plastics 1500-3500 MPa.");
    label("Poisson ratio");
    ImGui::SetNextItemWidth(240.0f * S);
    ImGui::SliderFloat("##cmnu", &cmNu_, 0.0f, 0.499f, "%.3f");
    simTip("Lateral contraction ratio, 0-0.5. Metals ~0.3, rubber approaches "
           "0.5.");
    label("Density (g/cm^3)");
    ImGui::SetNextItemWidth(240.0f * S);
    ImGui::DragFloat("##cmrho", &cmDensity_, 0.01f, 0.0f, 25.0f, "%.3f g/cm3");
    simTip("Mass density — used by frequency (modal) studies.");
    label("Yield strength (MPa)");
    ImGui::SetNextItemWidth(240.0f * S);
    ImGui::DragFloat("##cmyield", &cmYield_, 1.0f, 0.0f, 1.0e5f, "%.0f MPa");
    simTip("Stress at which the material yields — drives the factor-of-safety "
           "result.");
    label("Ultimate strength Sut (MPa)");
    ImGui::SetNextItemWidth(240.0f * S);
    ImGui::DragFloat("##cmult", &cmUlt_, 1.0f, 0.0f, 1.0e5f, "%.0f MPa");
    simTip("Ultimate tensile strength — used by fatigue (Goodman / Gerber).");
    label("Endurance limit Se (MPa)");
    ImGui::SetNextItemWidth(240.0f * S);
    ImGui::DragFloat("##cmend", &cmEnd_, 1.0f, 0.0f, 1.0e5f, "%.0f MPa");
    simTip("Fully-corrected fatigue / endurance limit. ~0.5x Sut for steels; "
           "lower for "
           "aluminum/non-ferrous. Leave as a fraction of Sut if unsure.");
    ImGui::Separator();
    const bool ok =
        cmName_[0] != '\0' && cmE_ > 0.0f && cmNu_ > 0.0f && cmNu_ < 0.5f;
    if (!ok)
      ImGui::BeginDisabled();
    if (ImGui::Button("Add", ImVec2(120.0f * S, 0))) {
      Material m;
      m.name = cmName_;
      m.E_mpa = cmE_;
      m.nu = cmNu_;
      m.density_g_cm3 = cmDensity_;
      m.yield_mpa = cmYield_;
      m.ultimate_mpa = cmUlt_;
      m.endurance_mpa = cmEnd_;
      // Custom material becomes the part's active material (set_material) too.
      applyPartMaterial(st, s, addCustomMaterial(m));
      persist(st, s);
      ImGui::CloseCurrentPopup();
    }
    if (!ok)
      ImGui::EndDisabled();
    ImGui::SameLine();
    if (ImGui::Button("Cancel", ImVec2(120.0f * S, 0)))
      ImGui::CloseCurrentPopup();
    popCardText();
    ImGui::EndPopup();
  }
}

void SimManager::drawMaterialCombo(AppState &st, Study &s) {
  // Live-resolve from the part so FeatureManager / Mass Props material changes
  // show here.
  resolveStudyMaterialFromDoc(st, s);
  const float S = ui::uiScale;
  const float fh =
      ImGui::GetFrameHeight(); // square +-button matches the combo height
  const auto &mats = materials();
  int mi = materialIndexByKey(s.materialKey);
  const char *cur = mi >= 0 ? mats[mi].name.c_str()
                            : (!s.materialKey.empty() ? s.materialKey.c_str()
                                                      : "(select material)");
  ImGui::SetNextItemWidth(-(fh + 6.0f * S));
  if (ImGui::BeginCombo("##material", cur)) {
    bool customHeader = false;
    for (int i = 0; i < (int)mats.size(); ++i) {
      if (mats[i].custom && !customHeader) {
        ImGui::Separator();
        ImGui::TextDisabled("Custom materials");
        customHeader = true;
      }
      if (ImGui::Selectable(mats[i].name.c_str(), mi == i)) {
        // Write set_material on the part (one shared material) and mirror onto
        // the study.
        applyPartMaterial(st, s, mats[i].key);
        persist(st, s);
      }
      if (mi == i)
        ImGui::SetItemDefaultFocus();
    }
    ImGui::EndCombo();
  }
  simTip("Material defines stiffness (E), Poisson ratio, density and yield "
         "strength used for the "
         "stress / factor-of-safety solve. Changing it updates the part's "
         "active material "
         "(FeatureManager Material row + Mass Properties).");
  ImGui::SameLine(0, 4.0f * S);
  if (ImGui::Button("+##newmat", ImVec2(fh, fh))) {
    // Seed the editor from the current pick (or sensible aluminum defaults) and
    // open.
    if (mi >= 0) {
      std::snprintf(cmName_, sizeof(cmName_), "%s",
                    (mats[mi].name + " (copy)").c_str());
      cmE_ = (float)mats[mi].E_mpa;
      cmNu_ = (float)mats[mi].nu;
      cmDensity_ = (float)mats[mi].density_g_cm3;
      cmYield_ = (float)mats[mi].yield_mpa;
      cmUlt_ = (float)mats[mi].ultimate_mpa;
      cmEnd_ = (float)mats[mi].endurance_mpa;
    }
    ImGui::OpenPopup("New material");
  }
  simTip("Add a custom material (saved to your library).");
  drawCustomMaterialPopup(st, s);
  // Transparency: show the resolved properties of the selected material inline.
  if (mi >= 0) {
    const auto &m = mats[mi];
    pushDimText();
    ImGui::TextWrapped(
        "E %.0f MPa  -  nu %.2f  -  yield %.0f MPa  -  rho %.2f g/cm3", m.E_mpa,
        m.nu, m.yield_mpa, m.density_g_cm3);
    popDimText();
  } else if (!s.materialKey.empty()) {
    pushDimText();
    ImGui::TextWrapped(
        "Part material '%s' is not in the library — pick a library material.",
        s.materialKey.c_str());
    popDimText();
  }
}

void SimManager::drawWellRow(AppState &st, const char *label, SimWell &well,
                             int armId, const char *tip) {
  ImGui::PushID(armId);
  const bool armed = (armedWell_ == armId);
  ImGui::TextUnformatted(label);
  simTip(tip);
  // Active selection box: armed = accent border + "Selecting…" hint.
  ImGui::PushStyleColor(ImGuiCol_Button,
                        armed ? ImVec4(ui::palette.accent[0],
                                       ui::palette.accent[1],
                                       ui::palette.accent[2], 0.85f)
                              : ImGui::GetStyleColorVec4(ImGuiCol_Button));
  if (ImGui::Button(armed ? "Selecting faces… (click viewport)" : "Add faces",
                    ImVec2(-FLT_MIN, 0))) {
    const bool nowArmed = !armed;
    armedWell_ = nowArmed ? armId : -1;
    if (nowArmed)
      st.selection
          .clear(); // fresh well — no carryover from a previously-picked face
  }
  ImGui::PopStyleColor();
  simTip(
      armed
          ? "Click faces in the viewport to add them. Click here again to stop."
          : "Arm this field, then click model faces in the viewport.");
  // Chips: each picked face, right-click to remove.
  if (!well.picks.empty()) {
    for (int k = 0; k < (int)well.picks.size(); ++k) {
      ImGui::PushID(k);
      ImGui::SmallButton(("Face " + std::to_string(k + 1)).c_str());
      if (ImGui::IsItemClicked(ImGuiMouseButton_Right)) {
        well.picks.erase(well.picks.begin() + k);
        persist(st, studies_[sel_]);
        ImGui::PopID();
        break;
      }
      simTip("Right-click to remove this face.");
      if (k % 4 != 3 && k + 1 < (int)well.picks.size())
        ImGui::SameLine();
      ImGui::PopID();
    }
    ImGui::SameLine();
    if (ImGui::SmallButton("Clear")) {
      well.picks.clear();
      persist(st, studies_[sel_]);
    }
    simTip("Remove all faces from this field.");
  }
  ImGui::PopID();
}

void SimManager::drawFixturesForm(AppState &st, Study &s) {
  ImGui::PushFont(st.fonts.medium);
  ImGui::TextUnformatted(s.type == StudyType::Modal ? "Fixtures (optional)"
                                                    : "Fixtures");
  ImGui::PopFont();
  ImGui::TextWrapped("Faces that are clamped (zero displacement).");
  ImGui::Spacing();
  drawWellRow(st, "Fixed faces", s.fixtures, 0,
              "Faces held rigidly in place. The reaction to your loads is "
              "carried here.");
}

void SimManager::drawLoadsForm(AppState &st, Study &s) {
  ImGui::PushFont(st.fonts.medium);
  ImGui::TextUnformatted("Loads");
  ImGui::PopFont();
  const float S = ui::uiScale;
  for (int li = 0; li < (int)s.loads.size(); ++li) {
    SimLoad &ld = s.loads[li];
    ImGui::PushID(li);
    ImGui::Separator();
    std::string hdr =
        (ld.pressure ? "Pressure " : "Force ") + std::to_string(li + 1);
    ImGui::TextUnformatted(hdr.c_str());
    ImGui::SameLine(ImGui::GetContentRegionAvail().x - 22.0f * S);
    if (ImGui::SmallButton("X")) {
      s.loads.erase(s.loads.begin() + li);
      persist(st, s);
      ImGui::PopID();
      break;
    }
    simTip("Remove this load.");
    drawWellRow(st, "Faces", ld.faces, li + 1, "Faces the load is applied to.");
    if (ImGui::Checkbox("Pressure", &ld.pressure))
      persist(st, s);
    simTip("On: a uniform pressure normal to the faces. Off: a total force "
           "vector distributed "
           "over the faces.");
    // Magnitude in the active display units (stored internally in SI — MPa /
    // N). The unit goes on its OWN line and the input label is hidden ("##"),
    // so the value boxes get the full pane width and never run off the right
    // edge in the narrow pane.
    UnitSys u = unitSys(st);
    if (ld.pressure) {
      ImGui::Text("Magnitude (into faces)");
      float disp = ld.pressureMpa * (float)u.pressFromMpa;
      ImGui::SetNextItemWidth(-FLT_MIN);
      char pfmt[24];
      std::snprintf(pfmt, sizeof(pfmt), "%%.4g %s", u.press); // unit in the box
      if (ImGui::InputFloat("##pmag", &disp, 0, 0, pfmt)) {
      }
      if (ImGui::IsItemDeactivatedAfterEdit()) {
        ld.pressureMpa = disp / (float)u.pressFromMpa;
        persist(st, s);
      }
      simTip("Pressure magnitude pushing into the faces.");
    } else {
      ImGui::Text("Force (world X / Y / Z)");
      float disp[3] = {ld.vec[0] * (float)u.forceFromN,
                       ld.vec[1] * (float)u.forceFromN,
                       ld.vec[2] * (float)u.forceFromN};
      ImGui::SetNextItemWidth(-FLT_MIN);
      char ffmt[24];
      std::snprintf(ffmt, sizeof(ffmt), "%%.4g %s",
                    u.force); // unit in each box
      if (ImGui::InputFloat3("##fvec", disp, ffmt)) {
      }
      if (ImGui::IsItemDeactivatedAfterEdit()) {
        for (int k = 0; k < 3; ++k)
          ld.vec[k] = disp[k] / (float)u.forceFromN;
        persist(st, s);
      }
      simTip("Total force along world X/Y/Z, spread evenly over the selected "
             "faces.");
    }
    ImGui::PopID();
  }
  ImGui::Spacing();
  if (ImGui::Button("+ Force")) {
    s.loads.push_back(SimLoad{});
    persist(st, s);
    st.selection.clear();
    node_ = SimNode::Load;
    loadIdx_ = (int)s.loads.size() - 1;
    armedWell_ = (int)s.loads.size();
  }
  simTip("Add a force load (newtons) on a set of faces.");
  ImGui::SameLine();
  if (ImGui::Button("+ Pressure")) {
    SimLoad l;
    l.pressure = true;
    s.loads.push_back(l);
    persist(st, s);
    st.selection.clear();
    node_ = SimNode::Load;
    loadIdx_ = (int)s.loads.size() - 1;
    armedWell_ = (int)s.loads.size();
  }
  simTip("Add a pressure load (MPa) on a set of faces.");
}

void SimManager::drawMeshForm(AppState &st, Study &s) {
  ImGui::PushFont(st.fonts.medium);
  ImGui::TextUnformatted("Mesh");
  ImGui::PopFont();
  ImGui::TextWrapped(
      "The mesh the solve runs on. Finer captures stress concentrations "
      "better but takes longer.");
  ImGui::Spacing();

  // --- Mesh type: tetrahedral (general) vs structured hexahedral (prismatic)
  // ------
  const bool modal = (s.type == StudyType::Modal);
  // Data hygiene: a modal study should carry no loads (the eigenvalue solve
  // ignores them and the tree hides them). If a study accumulated loads before
  // becoming modal — e.g. copied from a static template — drop them once so
  // future JSON readers don't see inert load entries.
  if (modal && !s.loads.empty()) {
    s.loads.clear();
    persist(st, s);
  }
  ImGui::TextUnformatted("Mesh type");
  // Binary choice — radio rows read instantly and skip the combo popup in the
  // narrow pane.
  static const char *kType[2] = {"Tetrahedral (any shape)",
                                 "Hexahedral (box / prismatic only)"};
  if (modal)
    ImGui::BeginDisabled(); // modal has no hex8 mass matrix
  int ti = (s.meshHex && !modal) ? 1 : 0;
  for (int i = 0; i < 2; ++i)
    if (ImGui::RadioButton(kType[i], ti == i) && ti != i) {
      s.meshHex = (i == 1);
      persist(st, s);
      clearMeshPreview();
    }
  if (modal)
    ImGui::EndDisabled();
  simTip("Tetrahedral fills any geometry but its element pattern is irregular "
         "(normal for "
         "unstructured meshing). Hexahedral lays down a perfectly regular, "
         "symmetric grid — "
         "but only works on a box / rectangular prism (other shapes fall back "
         "with a note). "
         "Frequency studies are tetrahedral only.");
  ImGui::Spacing();
  const bool hexMesh = s.meshHex && !modal;

  // --- Mesh fineness (primary control)
  // -------------------------------------------
  ImGui::TextUnformatted("Mesh fineness");
  ImGui::SetNextItemWidth(-FLT_MIN);
  // Show a Coarse..Fine word instead of a bare number so the control reads like
  // SW.
  const char *fineWord = s.meshFineness < 0.2f   ? "Coarse"
                         : s.meshFineness < 0.4f ? "Coarse-medium"
                         : s.meshFineness < 0.6f ? "Medium"
                         : s.meshFineness < 0.8f ? "Fine-medium"
                                                 : "Fine";
  if (ImGui::SliderFloat("##fineness", &s.meshFineness, 0.0f, 1.0f, fineWord)) {
  }
  if (ImGui::IsItemDeactivatedAfterEdit()) {
    persist(st, s);
    clearMeshPreview();
  }
  simTip("Base element size, derived from the part's overall size. Left = "
         "coarse/fast, "
         "right = fine/slow & more accurate.");
  ImGui::Spacing();

  if (hexMesh) {
    // Structured hex: only fineness (grid density) applies; the tet-only
    // controls (curvature/order/optimize/algorithm) are hidden.
    ImGui::TextDisabled(
        "Regular hex8 grid. Element size from fineness; the part must be a\n"
        "box / rectangular prism (otherwise switch to Tetrahedral).");
    ImGui::Spacing();
  } else {

    // --- Curvature-adaptive refinement
    // ---------------------------------------------
    if (ImGui::Checkbox("Refine curved features (holes, fillets)",
                        &s.meshCurvature)) {
      persist(st, s);
      clearMeshPreview();
    }
    simTip("Automatically uses smaller elements around curvature so stress "
           "concentrations "
           "at holes and fillets are resolved. Strongly recommended for "
           "accurate FEA.");
    ImGui::Spacing();

    // --- Element order
    // ------------------------------------------------------------- Linear
    // (tet4) shear-locks and makes bending look straight; quadratic (tet10)
    // captures it. Static defaults to High quality; modal always runs
    // 1st-order.
    ImGui::TextUnformatted("Element order");
    static const char *kOrder[2] = {"Draft - linear (1st order, fast)",
                                    "High quality - quadratic (2nd order)"};
    // Modal always runs 1st-order linear tets (runStaticOrModal forces
    // order=1); show + lock Draft.
    int oi = (!modal && s.meshOrder == 2) ? 1 : 0;
    if (modal)
      ImGui::BeginDisabled();
    for (int i = 0; i < 2; ++i)
      if (ImGui::RadioButton(kOrder[i], oi == i) && oi != i) {
        s.meshOrder = i ? 2 : 1;
        persist(st, s);
        clearMeshPreview();
      }
    if (modal)
      ImGui::EndDisabled();
    simTip("2nd-order (quadratic) elements capture bending and curved geometry "
           "far more "
           "accurately. Linear elements are overly stiff (shear-locking) and "
           "make a bent "
           "part look almost straight. Use High quality for structural "
           "accuracy; Draft for a "
           "fast first look. (Frequency studies always use 1st-order.)");
    if (modal)
      ImGui::TextDisabled("Frequency studies always use 1st-order elements.");
    ImGui::Spacing();

    // --- Optimize
    // ------------------------------------------------------------------
    if (modal)
      ImGui::BeginDisabled();
    if (ImGui::Checkbox("Optimize element quality (Netgen)", &s.meshOptimize)) {
      persist(st, s);
      clearMeshPreview();
    }
    if (modal)
      ImGui::EndDisabled();
    simTip("Run Netgen's surface + volume optimizers after generation to "
           "improve poorly-shaped "
           "tetrahedra. Recommended; adds a little meshing time.");
    if (modal)
      ImGui::TextDisabled(
          "Mesh quality settings are fixed for frequency studies.");
    ImGui::Spacing();

    // --- Advanced (collapsed)
    // ------------------------------------------------------
    if (ImGui::CollapsingHeader("Advanced")) {
      ImGui::Indent();
      bool edited = false;
      if (s.meshCurvature) {
        ImGui::TextUnformatted("Curvature refinement (elements per circle)");
        ImGui::SetNextItemWidth(-FLT_MIN);
        if (ImGui::SliderFloat("##curvper", &s.meshCurvPerCircle, 6.0f, 24.0f,
                               "%.0f")) {
        }
        edited = edited || ImGui::IsItemDeactivatedAfterEdit();
        simTip("Target number of elements around a full circle of curvature. "
               "Higher = "
               "rounder holes/fillets but more elements.");
        ImGui::Spacing();
      }
      ImGui::TextUnformatted("Element size override (mm, 0 = auto)");
      float target = s.meshTargetMm, minSize = s.meshMinMm;
      ImGui::SetNextItemWidth(-FLT_MIN);
      if (ImGui::InputFloat("##target_size", &target, 0.0f, 0.0f, "%.4g")) {
      }
      edited = edited || ImGui::IsItemDeactivatedAfterEdit();
      simTip(
          "Explicit target element size in mm. Overrides fineness when > 0.");
      ImGui::SetNextItemWidth(-FLT_MIN);
      if (ImGui::InputFloat("##min_size", &minSize, 0.0f, 0.0f, "%.4g")) {
      }
      edited = edited || ImGui::IsItemDeactivatedAfterEdit();
      simTip("Minimum element size in mm (floor for curvature refinement). 0 = "
             "auto.");
      if (edited) {
        s.meshTargetMm = std::max(0.0f, target);
        s.meshMinMm = std::max(0.0f, minSize);
        persist(st, s);
        clearMeshPreview();
      }
      ImGui::Spacing();
      ImGui::TextUnformatted("3D algorithm");
      static const char *kAlgo[2] = {"Delaunay (robust)",
                                     "HXT (fast, parallel)"};
      int ai = (s.meshAlgo == 10) ? 1 : 0;
      ImGui::SetNextItemWidth(-FLT_MIN);
      if (ImGui::BeginCombo("##meshalgo", kAlgo[ai])) {
        for (int i = 0; i < 2; ++i)
          if (ImGui::Selectable(kAlgo[i], ai == i)) {
            s.meshAlgo = i ? 10 : 1;
            persist(st, s);
            clearMeshPreview();
          }
        ImGui::EndCombo();
      }
      simTip("Tetrahedral meshing algorithm. Delaunay is the robust default; "
             "HXT is faster "
             "and multi-threaded for large parts.");
      ImGui::Unindent();
    }
    ImGui::Spacing();
  } // end !hexMesh tet-only controls

  bool busy = st.simClient && st.simClient->busy();
  if (busy)
    ImGui::BeginDisabled();
  if (ImGui::Button("Preview mesh", ImVec2(-FLT_MIN, 28.0f * ui::uiScale)))
    previewMesh(st, s);
  if (busy)
    ImGui::EndDisabled();
  simTip("Mesh the part now and show the element wireframe + node/element "
         "counts + quality "
         "in the viewport, without running the solve.");

  if (s.nNodes > 0 || s.nElems > 0) {
    ImGui::Spacing();
    ImGui::Text("Nodes: %d", s.nNodes);
    ImGui::Text("Elements: %d", s.nElems);
    if (!meshPreviewSegs_.empty() && meshPreviewStudyId_ == s.id)
      ImGui::TextDisabled("Wireframe shown in the viewport.");
  }

  // --- Mesh quality readout
  // ------------------------------------------------------
  if (s.meshHasQuality) {
    ImGui::Spacing();
    ImGui::PushFont(st.fonts.medium);
    ImGui::TextUnformatted("Mesh quality");
    ImGui::PopFont();
    ImGui::Text("Aspect ratio: %.2f avg, %.2f worst", s.meshAspectAvg,
                s.meshAspectMax);
    simTip("Element aspect ratio (1.0 = ideal). Elements above 10 are flagged "
           "as poor; a "
           "few are normal, many indicate the mesh needs refining where the "
           "geometry is thin.");
    ImGui::Text("Min Jacobian: %.3f", s.meshJacobianMin);
    simTip("Worst scaled Jacobian (1.0 = ideal, <= 0 = inverted/invalid "
           "element).");
    // Color the poor-element percentage like SW: green clean, amber some, red
    // many.
    ImVec4 poorCol = s.meshPctPoor < 1.0f   ? simRole(ui::palette.statusOk)
                     : s.meshPctPoor < 5.0f ? simRole(ui::palette.statusWarn)
                                            : simRole(ui::palette.statusErr);
    ImGui::TextColored(poorCol, "Poor elements: %.1f%%", s.meshPctPoor);
    if (s.meshAspectHist.size() == 10) {
      ImGui::PlotHistogram("##aspecthist", s.meshAspectHist.data(), 10, 0,
                           "aspect 1 -> 10", 0.0f, FLT_MAX,
                           ImVec2(-FLT_MIN, 40.0f * ui::uiScale));
      simTip("Distribution of element aspect ratios from best (left, ~1) to "
             "poor (right, >=10).");
    }
  }
}

void SimManager::drawFatigueForm(AppState &st, Study &s) {
  const float S = ui::uiScale;
  UnitSys u = unitSys(st);
  resolveStudyMaterialFromDoc(st, s);
  const int mati = materialIndexByKey(s.materialKey);
  const double matSut = mati >= 0 ? materials()[mati].ultimate_mpa : 0.0;
  const double matSe = mati >= 0 ? materials()[mati].endurance_mpa : 0.0;

  // ---- Cyclic loading: define the stress cycle as load factors x the applied
  // load ----
  ImGui::SeparatorText("Cyclic loading");
  static const char *kLT[4] = {"Fully reversed  (R = -1)",
                               "Zero to max  (R = 0)", "Repeated  (custom R)",
                               "Custom min/max factor"};
  int lt =
      (s.fatigueLoadType < 0 || s.fatigueLoadType > 3) ? 0 : s.fatigueLoadType;
  ImGui::SetNextItemWidth(-FLT_MIN);
  if (ImGui::BeginCombo("##flt", kLT[lt])) {
    for (int i = 0; i < 4; ++i)
      if (ImGui::Selectable(kLT[i], lt == i)) {
        s.fatigueLoadType = i;
        persist(st, s);
      }
    ImGui::EndCombo();
  }
  simTip("How the applied load cycles. The result stress from your loads is "
         "the PEAK; this sets how "
         "it swings. Fully reversed = +/- peak (worst case). Zero to max = "
         "0..peak. Repeated = a "
         "min/max ratio R. Custom = explicit min & max multipliers of the "
         "applied load.");
  if (s.fatigueLoadType == 2) {
    ImGui::TextUnformatted("Load ratio R = min / max");
    ImGui::SetNextItemWidth(-FLT_MIN);
    if (ImGui::SliderFloat("##fR", &s.fatigueRatio, -1.0f, 0.95f, "%.2f")) {
    }
    if (ImGui::IsItemDeactivatedAfterEdit())
      persist(st, s);
    simTip(
        "-1 = fully reversed, 0 = zero-based, approaching 1 = nearly static.");
  } else if (s.fatigueLoadType == 3) {
    ImGui::TextUnformatted("Min / max load factor (x applied)");
    float mm[2] = {s.fatigueLmin, s.fatigueLmax};
    ImGui::SetNextItemWidth(-FLT_MIN);
    if (ImGui::DragFloat2("##fmm", mm, 0.01f, 0.0f, 0.0f, "%.2f x")) {
    }
    if (ImGui::IsItemDeactivatedAfterEdit()) {
      s.fatigueLmin = std::min(mm[0], mm[1]);
      s.fatigueLmax = std::max(mm[0], mm[1]);
      persist(st, s);
    }
    simTip("The cycle runs between (min factor) and (max factor) times the "
           "applied load. "
           "Values are sorted min/max on commit.");
  }
  // Readout + a tiny cycle sketch so the amplitude/mean are obvious.
  float lmin, lmax;
  fatigueLoadFactors(s, lmin, lmax);
  const float amp = (lmax - lmin) * 0.5f, mean = (lmax + lmin) * 0.5f;
  const float R = (std::fabs(lmax) > 1e-6f) ? (lmin / lmax) : -1.0f;
  ImGui::TextDisabled(
      "Amplitude %.2fx | mean %.2fx | R %.2f  (x the applied-load stress)", amp,
      mean, R);
  {
    ImDrawList *dl = ImGui::GetWindowDrawList();
    const float w = ImGui::GetContentRegionAvail().x, h = 46.0f * S;
    const ImVec2 p0 = ImGui::GetCursorScreenPos();
    ImGui::Dummy(ImVec2(w, h));
    const float midY = p0.y + h * 0.5f, x0 = p0.x + 24 * S,
                x1 = p0.x + w - 6 * S;
    const float span =
        std::max(1.0f, std::max(std::fabs(lmax), std::fabs(lmin)));
    auto Y = [&](float v) { return midY - (v / span) * (h * 0.42f); };
    // Chart ink derives from the scope's paired text color (card ink here) so
    // the sketch stays readable on the light theme's near-white card too — no
    // dark-theme-only literals.
    const ImVec4 inkV = ImGui::GetStyleColorVec4(ImGuiCol_Text);
    auto ink = [&](float a) {
      return ImGui::ColorConvertFloat4ToU32(
          ImVec4(inkV.x, inkV.y, inkV.z, inkV.w * a));
    };
    float warnA[4];
    Palette::withAlpha(ui::palette.statusWarn, warnA, 0.75f);
    dl->AddLine(ImVec2(x0, Y(0)), ImVec2(x1, Y(0)), ink(0.45f), 1.0f); // zero
    dl->AddLine(ImVec2(x0, Y(mean)), ImVec2(x1, Y(mean)), Palette::toU32(warnA),
                1.0f); // mean
    ImU32 col = IM_COL32(90, 170, 250,
                         255); // waveform trace hue (same family as the graphs)
    ImVec2 prev(x0, Y(mean));
    for (int i = 1; i <= 60; ++i) {
      float t = (float)i / 60.0f;
      float v = mean + amp * std::sin(t * 6.2831853f * 1.5f);
      ImVec2 cur(x0 + t * (x1 - x0), Y(v));
      dl->AddLine(prev, cur, col, 2.0f);
      prev = cur;
    }
    dl->AddText(ImVec2(p0.x, Y(lmax) - 6 * S), ink(1.0f), "max");
    dl->AddText(ImVec2(p0.x, Y(lmin) - 6 * S), ink(1.0f), "min");
  }

  // ---- Strength: Sut + Se' (overrides; default from the material) ----
  ImGui::SeparatorText("Strength");
  if (matSut > 0)
    ImGui::TextDisabled("Material: Sut %.0f %s | Se' %.0f %s",
                        matSut * u.stressFromMpa, u.stress,
                        matSe * u.stressFromMpa, u.stress);
  else {
    ImGui::PushStyleColor(ImGuiCol_Text, simRole(ui::palette.statusWarn));
    ImGui::TextWrapped("This material has no ultimate strength — set Sut (and "
                       "Se') below, or pick a "
                       "material that carries fatigue data.");
    ImGui::PopStyleColor();
  }
  auto strengthField = [&](const char *label, float &field, double matDefault,
                           const char *tip) {
    float disp =
        (field > 0 ? field : (float)matDefault) * (float)u.stressFromMpa;
    ImGui::TextUnformatted(label);
    ImGui::SetNextItemWidth(-FLT_MIN);
    std::string id = std::string("##") + label;
    char sfmt[24];
    std::snprintf(sfmt, sizeof(sfmt), "%%.4g %s", u.stress); // unit in the box
    if (ImGui::InputFloat(id.c_str(), &disp, 0, 0, sfmt)) {
    }
    if (ImGui::IsItemDeactivatedAfterEdit()) {
      field = disp / (float)u.stressFromMpa;
      persist(st, s);
    }
    simTip(tip);
  };
  strengthField((std::string("Ultimate Sut (") + u.stress + ")").c_str(),
                s.fatigueSut, matSut, "Ultimate tensile strength.");
  strengthField((std::string("Endurance Se' (") + u.stress + ")").c_str(),
                s.fatigueSe, matSe,
                "Uncorrected (test-specimen) endurance limit. Marin factors "
                "below correct it.");

  // ---- Endurance correction (Marin factors) ----
  ImGui::SeparatorText("Endurance correction (Marin)");
  auto combo = [&](const char *lbl, const char *id, const char **items, int n,
                   int &val, const char *tip) {
    int v = (val < 0 || val >= n) ? 0 : val;
    ImGui::TextUnformatted(lbl);
    ImGui::SetNextItemWidth(-FLT_MIN);
    if (ImGui::BeginCombo(id, items[v])) {
      for (int i = 0; i < n; ++i)
        if (ImGui::Selectable(items[i], v == i)) {
          val = i;
          persist(st, s);
        }
      ImGui::EndCombo();
    }
    simTip(tip);
  };
  static const char *kSurf[4] = {"Ground", "Machined / cold-drawn",
                                 "Hot-rolled", "As-forged"};
  static const char *kCase[3] = {"Bending", "Axial", "Torsion"};
  combo("Surface finish", "##fsurf", kSurf, 4, s.fatigueSurface,
        "Surface factor ka.");
  combo("Loading type", "##fcase", kCase, 3, s.fatigueLoadCase,
        "Load factor kc + size factor kb (axial has no size effect).");
  // Bounded drags: the kb size correlation is valid ~2..250 mm, kd roughly
  // -50..500 C — soft ranges make the correlation limits discoverable instead
  // of silently extrapolating.
  ImGui::TextUnformatted("Section size");
  ImGui::SetNextItemWidth(-FLT_MIN);
  if (ImGui::DragFloat("##fsize", &s.fatigueSizeMm, 0.5f, 1.0f, 250.0f,
                       "%.1f mm")) {
  }
  if (ImGui::IsItemDeactivatedAfterEdit())
    persist(st, s);
  simTip("Effective diameter for the size factor kb (use the section "
         "thickness/diameter).");
  ImGui::TextUnformatted("Temperature");
  ImGui::SetNextItemWidth(-FLT_MIN);
  if (ImGui::DragFloat("##ftemp", &s.fatigueTempC, 1.0f, -50.0f, 500.0f,
                       "%.0f C")) {
  }
  if (ImGui::IsItemDeactivatedAfterEdit())
    persist(st, s);
  simTip("Operating temperature for the temperature factor kd.");
  ImGui::TextUnformatted("Design reliability (%)");
  ImGui::SetNextItemWidth(-FLT_MIN);
  if (ImGui::SliderFloat("##frel", &s.fatigueReliability, 50.0f, 99.99f,
                         "%.2f")) {
  }
  if (ImGui::IsItemDeactivatedAfterEdit())
    persist(st, s);
  simTip("Confidence level for the headline life/FoS — higher reliability "
         "lowers the allowable "
         "endurance and shortens predicted life. The Results graph shows the "
         "full band.");

  // Computed corrected endurance limit + factors (transparency).
  const double Sut = s.fatigueSut > 0 ? s.fatigueSut : matSut;
  if (Sut > 0) {
    const double Seb = seBase(s, matSe, Sut);
    const double ka = marin_ka(Sut, s.fatigueSurface),
                 kb = marin_kb(s.fatigueSizeMm, s.fatigueLoadCase),
                 kc = marin_kc(s.fatigueLoadCase),
                 kd = marin_kd(s.fatigueTempC);
    const double SeC = ka * kb * kc * kd * Seb;
    ImGui::Spacing();
    pushDimText();
    ImGui::TextWrapped("ka %.2f  kb %.2f  kc %.2f  kd %.2f", ka, kb, kc, kd);
    popDimText();
    ImGui::Text("Corrected Se = %.0f %s", SeC * u.stressFromMpa, u.stress);
    simTip("Se = ka.kb.kc.kd . Se'. This is the endurance limit used for life "
           "+ factor of safety "
           "(reliability is applied on top, in Results).");
  }
}

// Full fatigue results: all mean-stress theories, FoS + life per theory, a
// reliability (confidence) slider that re-bands life live (no re-solve), a
// per-theory life-band chart, the constant-life (Haigh) diagram with every
// theory's line, and an S-N curve with a reliability band.
void SimManager::drawFatigueGraphs(AppState &st, Study &s) {
  if (s.resultSummary.empty()) {
    ImGui::TextDisabled("No fatigue results.");
    return;
  }
  const auto &sm = s.resultSummary;
  const float S = ui::uiScale;
  const UnitSys u = unitSys(st);
  const double SeC = sm.value(
      "se_corrected_mpa", 0.0); // Marin-corrected endurance (no reliability)
  const double Sut = sm.value("sut_mpa", 0.0), Sy = sm.value("sy_mpa", 0.0);
  const double sa = sm.value("crit_sigma_a", 0.0),
               smean = sm.value("crit_sigma_m", 0.0);
  if (Sut <= 0 || SeC <= 0 || !sm.contains("theories")) {
    ImGui::TextDisabled(
        "No fatigue strength data — set Sut / Se in the study.");
    return;
  }
  auto saEq = [&](int t) -> double {
    const char *k = kTheoryKeys[t];
    return sm["theories"].contains(k)
               ? sm["theories"][k].value("sa_eq_mpa", 0.0)
               : 0.0;
  };
  const int m =
      (s.fatigueMethod < 0 || s.fatigueMethod > 4) ? 0 : s.fatigueMethod;

  // ---- reliability (confidence): live re-bands life with no re-solve ----
  ImGui::SeparatorText("Confidence");
  ImGui::TextUnformatted("Design reliability (%)");
  ImGui::SetNextItemWidth(-FLT_MIN);
  if (ImGui::SliderFloat("##relres", &s.fatigueReliability, 50.0f, 99.99f,
                         "%.2f")) {
  }
  if (ImGui::IsItemDeactivatedAfterEdit())
    persist(st, s);
  simTip("Higher reliability uses a lower allowable endurance (ke) -> more "
         "conservative life. The "
         "band spans 50% (optimistic) to 99.9% (conservative).");
  const double SeD = reliability_ke(s.fatigueReliability) * SeC;
  const double SeLo = reliability_ke(99.9) * SeC;  // conservative endurance
  const double SeOpt = reliability_ke(50.0) * SeC; // optimistic endurance
  ImGui::TextDisabled("Corrected Se %.0f %s  ->  at %.1f%%: %.0f %s",
                      SeC * u.stressFromMpa, u.stress, s.fatigueReliability,
                      SeD * u.stressFromMpa, u.stress);

  // ---- per-theory table ----
  if (ImGui::BeginTable("##ftheory", 4,
                        ImGuiTableFlags_SizingStretchProp |
                            ImGuiTableFlags_BordersInnerH)) {
    ImGui::TableSetupColumn("Theory");
    ImGui::TableSetupColumn("Sa,eq");
    ImGui::TableSetupColumn("FoS");
    ImGui::TableSetupColumn("Life");
    ImGui::TableHeadersRow();
    for (int t = 0; t < 5; ++t) {
      double se = saEq(t);
      double fos = se > 1e-9 ? SeD / se : 15.0;
      double life = sn_life(se, SeD, Sut);
      ImGui::TableNextRow();
      ImGui::TableNextColumn();
      if (t == m)
        ImGui::TextUnformatted(kTheoryNames[t]);
      else
        ImGui::TextDisabled("%s", kTheoryNames[t]);
      char c[40];
      ImGui::TableNextColumn();
      std::snprintf(c, sizeof(c), "%.3g", se * u.stressFromMpa);
      ImGui::TextUnformatted(c);
      ImGui::TableNextColumn();
      if (fos < 1.0)
        ImGui::PushStyleColor(ImGuiCol_Text, simRole(ui::palette.statusErr));
      std::snprintf(c, sizeof(c), "%.2f", fos);
      ImGui::TextUnformatted(c);
      if (fos < 1.0)
        ImGui::PopStyleColor();
      ImGui::TableNextColumn();
      if (life >= 1e6)
        ImGui::TextUnformatted("inf");
      else {
        std::snprintf(c, sizeof(c), "%.2e", life);
        ImGui::TextUnformatted(c);
      }
    }
    ImGui::EndTable();
  }
  ImGui::TextUnformatted("Plotted / headline theory");
  ImGui::SetNextItemWidth(-FLT_MIN);
  if (ImGui::BeginCombo("##ftheorysel", kTheoryNames[m])) {
    for (int i = 0; i < 5; ++i)
      if (ImGui::Selectable(kTheoryNames[i], m == i)) {
        s.fatigueMethod = i;
        persist(st, s);
      }
    ImGui::EndCombo();
  }

  ImDrawList *dl = ImGui::GetWindowDrawList();
  // Axis / label / grid ink from the scope's paired text color (pane adaptive
  // ink here) and the semantic accents from palette roles — the old dark-theme
  // IM_COL32 light-greys were unreadable on the light theme. The 5 theory trace
  // hues stay as named constants (a fixed series ramp).
  const ImVec4 inkV = ImGui::GetStyleColorVec4(ImGuiCol_Text);
  auto ink = [&](float a) {
    return ImGui::ColorConvertFloat4ToU32(
        ImVec4(inkV.x, inkV.y, inkV.z, inkV.w * a));
  };
  const ImU32 axisCol = ink(0.62f), txtCol = ink(1.0f), gridCol = ink(0.26f);
  float warnA[4];
  Palette::withAlpha(ui::palette.statusWarn, warnA, 0.8f);
  const ImU32 ptCol = Palette::toU32(ui::palette.statusErr);
  const ImU32 yieldCol = Palette::toU32(warnA),
              bandCol = IM_COL32(90, 170, 250, 55);
  const ImU32 tcol[5] = {
      IM_COL32(90, 170, 250, 255), IM_COL32(120, 210, 120, 255),
      IM_COL32(240, 140, 60, 255), IM_COL32(200, 120, 230, 255),
      IM_COL32(230, 90, 90, 255)};
  const ImU32 tdim[5] = {
      IM_COL32(90, 170, 250, 120), IM_COL32(120, 210, 120, 120),
      IM_COL32(240, 140, 60, 120), IM_COL32(200, 120, 230, 120),
      IM_COL32(230, 90, 90, 120)};

  // ---- life-estimate band per theory (low / median / high cycles) ----
  ImGui::SeparatorText("Life estimate (low / median / high)");
  {
    const float w = ImGui::GetContentRegionAvail().x, rowH = 18.0f * S,
                h = 5 * rowH + 22 * S;
    const ImVec2 p0 = ImGui::GetCursorScreenPos();
    ImGui::Dummy(ImVec2(w, h));
    dl->PushClipRect(p0, ImVec2(p0.x + w, p0.y + h),
                     true); // never let a bar/dot escape the box
    const float padL = 72 * S, padR = 16 * S;
    const ImVec2 a0(p0.x + padL, p0.y), a1(p0.x + w - padR, p0.y + 5 * rowH);
    const double xlo = 0.0, xhi = 6.0;
    auto X = [&](double logN) {
      return a0.x + (float)((logN - xlo) / (xhi - xlo)) * (a1.x - a0.x);
    };
    for (int e = 0; e <= 6; ++e) {
      float x = X(e);
      dl->AddLine(ImVec2(x, a0.y), ImVec2(x, a1.y), gridCol, 1.0f);
      char lb[12];
      std::snprintf(lb, sizeof(lb), "1e%d", e);
      dl->AddText(ImVec2(x - 7 * S, a1.y + 2 * S), txtCol, lb);
    }
    auto lg = [](double N) {
      return std::log10(std::min(std::max(N, 1.0), 1e6));
    };
    for (int t = 0; t < 5; ++t) {
      double se = saEq(t);
      double lHi = sn_life(se, SeOpt, Sut), lMed = sn_life(se, SeD, Sut),
             lLo = sn_life(se, SeLo, Sut);
      float y = p0.y + t * rowH + rowH * 0.5f;
      dl->AddText(ImVec2(p0.x, y - 6 * S), (t == m) ? txtCol : axisCol,
                  kTheoryNames[t]);
      float xLo = X(lg(lLo)), xHi = X(lg(lHi)), xMed = X(lg(lMed));
      dl->AddLine(ImVec2(xLo, y), ImVec2(xHi, y), (t == m) ? tcol[t] : tdim[t],
                  3.0f * S);
      dl->AddCircleFilled(ImVec2(xMed, y), 3.5f * S,
                          ink(0.95f)); // median (design) dot
      if (lHi >= 1e6)
        dl->AddText(ImVec2(xHi + 3 * S, y - 6 * S), txtCol, "inf");
    }
    dl->PopClipRect();
  }
  ImGui::TextDisabled("Bar = 50%%..99.9%% reliability; dot = design (%.1f%%).",
                      s.fatigueReliability);

  // ---------- Constant-life (Haigh) diagram with ALL theory lines ----------
  ImGui::SeparatorText("Constant-life (Haigh) diagram");
  {
    const float w = ImGui::GetContentRegionAvail().x, h = 160.0f * S;
    const ImVec2 p0 = ImGui::GetCursorScreenPos();
    ImGui::Dummy(ImVec2(w, h));
    dl->PushClipRect(p0, ImVec2(p0.x + w, p0.y + h), true);
    const float padL = 40 * S, padB = 22 * S, padT = 8 * S, padR = 10 * S;
    const ImVec2 a0(p0.x + padL, p0.y + padT),
        a1(p0.x + w - padR, p0.y + h - padB);
    // Axes span the data: extend Sm to cover an operating mean stress beyond
    // Sut so the point never falls off the right; Sa axis already covers
    // max(SeD, sa).
    double xMax = Sut;
    if (smean * 1.1 > xMax)
      xMax = smean * 1.1;
    if (xMax <= 0)
      xMax = 1.0;
    const double yMax =
        (std::max(SeD, sa) * 1.2 > 0) ? std::max(SeD, sa) * 1.2 : SeD;
    const double SyL = Sy > 0 ? Sy : Sut;
    auto clampX = [&](float x) { return std::min(std::max(x, a0.x), a1.x); };
    auto clampY = [&](float y) { return std::min(std::max(y, a0.y), a1.y); };
    auto X = [&](double v) { return a0.x + (float)(v / xMax) * (a1.x - a0.x); };
    auto Y = [&](double v) { return a1.y - (float)(v / yMax) * (a1.y - a0.y); };
    dl->AddLine(ImVec2(a0.x, a1.y), ImVec2(a1.x, a1.y), axisCol, 1.5f);
    dl->AddLine(ImVec2(a0.x, a0.y), ImVec2(a0.x, a1.y), axisCol, 1.5f);
    for (int t = 0; t < 5; ++t) {
      ImVec2 prev(X(0), Y(SeD));
      for (int i = 1; i <= 40; ++i) {
        double smv = xMax * i / 40.0, frac;
        switch (t) {
        case 1:
          frac = 1 - (smv / Sut) * (smv / Sut);
          break; // gerber
        case 2:
          frac = 1 - smv / SyL;
          break; // soderberg
        case 3: {
          double r = smv / SyL;
          frac = std::sqrt(std::max(1 - r * r, 0.0));
        } break; // asme
        case 4:
          frac = 1 - smv / (Sut + 345.0);
          break; // morrow
        default:
          frac = 1 - smv / Sut;
          break; // goodman
        }
        ImVec2 cur(X(smv), Y(SeD * std::max(frac, 0.0)));
        dl->AddLine(prev, cur, (t == m) ? tcol[t] : tdim[t],
                    (t == m) ? 2.2f : 1.2f);
        prev = cur;
      }
    }
    if (Sy > 0)
      dl->AddLine(ImVec2(X(0), Y(Sy)), ImVec2(X(Sy), Y(0)), yieldCol,
                  1.0f); // Langer
    const ImVec2 op(clampX(X(smean)), clampY(Y(sa)));
    dl->AddLine(ImVec2(X(0), Y(0)), op, ink(0.5f), 1.0f);
    dl->AddCircleFilled(op, 4.0f * S, ptCol);
    char lab[48];
    dl->AddText(ImVec2(p0.x + 2 * S, p0.y + 2 * S), txtCol, "Sa");
    dl->AddText(ImVec2(a1.x - 16 * S, a1.y + 4 * S), txtCol, "Sm");
    std::snprintf(lab, sizeof(lab), "Sut %.0f", Sut * u.stressFromMpa);
    dl->AddText(ImVec2(clampX(X(Sut)) - 28 * S, a1.y + 4 * S), txtCol, lab);
    dl->PopClipRect();
  }
  ImGui::TextDisabled(
      "Lines = each theory at %.1f%%; point = critical (Sm %.3g, Sa %.3g %s).",
      s.fatigueReliability, smean * u.stressFromMpa, sa * u.stressFromMpa,
      u.stress);

  // ---------- S-N curve with reliability band ----------
  ImGui::SeparatorText("S-N curve");
  {
    const float w = ImGui::GetContentRegionAvail().x, h = 150.0f * S;
    const ImVec2 p0 = ImGui::GetCursorScreenPos();
    ImGui::Dummy(ImVec2(w, h));
    dl->PushClipRect(p0, ImVec2(p0.x + w, p0.y + h), true);
    const float padL = 44 * S, padB = 22 * S, padT = 8 * S, padR = 10 * S;
    const ImVec2 a0(p0.x + padL, p0.y + padT),
        a1(p0.x + w - padR, p0.y + h - padB);
    // Cover low-cycle life: if the design point lands below 1e3 cycles, extend
    // the axis down to 1e1 so the operating dot stays on-graph instead of
    // running off the left.
    const double f = 0.9, s1k = f * Sut, xhi = 7.0, yMax = s1k * 1.1;
    const double seM = saEq(m), lifeM = sn_life(seM, SeD, Sut);
    const double logM = std::log10(std::min(std::max(lifeM, 1.0), 1e7));
    const double xlo = (logM < 3.0) ? 1.0 : 3.0;
    const int eLo = (int)xlo;
    auto X = [&](double logN) {
      return a0.x + (float)((logN - xlo) / (xhi - xlo)) * (a1.x - a0.x);
    };
    auto Y = [&](double v) { return a1.y - (float)(v / yMax) * (a1.y - a0.y); };
    auto clampX = [&](float x) { return std::min(std::max(x, a0.x), a1.x); };
    auto clampY = [&](float y) { return std::min(std::max(y, a0.y), a1.y); };
    dl->AddLine(ImVec2(a0.x, a1.y), ImVec2(a1.x, a1.y), axisCol, 1.5f);
    dl->AddLine(ImVec2(a0.x, a0.y), ImVec2(a0.x, a1.y), axisCol, 1.5f);
    for (int e = eLo; e <= 7; ++e) {
      float x = X(e);
      dl->AddLine(ImVec2(x, a0.y), ImVec2(x, a1.y), gridCol, 1.0f);
      char lb[12];
      std::snprintf(lb, sizeof(lb), "1e%d", e);
      dl->AddText(ImVec2(x - 8 * S, a1.y + 4 * S), txtCol, lb);
    }
    // reliability band between optimistic (50%) and conservative (99.9%) S-N
    // lines
    dl->AddQuadFilled(ImVec2(X(3), Y(s1k)), ImVec2(X(6), Y(SeOpt)),
                      ImVec2(X(6), Y(SeLo)), ImVec2(X(3), Y(s1k)), bandCol);
    dl->AddLine(ImVec2(X(3), Y(s1k)), ImVec2(X(6), Y(SeD)), tcol[m],
                2.0f); // design line
    dl->AddLine(ImVec2(X(6), Y(SeD)), ImVec2(X(7), Y(SeD)), tcol[m], 2.0f);
    float okA[4];
    Palette::withAlpha(ui::palette.statusOk, okA, 0.45f);
    dl->AddLine(ImVec2(a0.x, Y(SeD)), ImVec2(a1.x, Y(SeD)), Palette::toU32(okA),
                1.0f);
    const ImVec2 op(clampX(X(logM)), clampY(Y(std::min(seM, yMax))));
    dl->AddCircleFilled(op, 4.0f * S, ptCol);
    char lab[48];
    dl->AddText(ImVec2(p0.x + 2 * S, p0.y + 2 * S), txtCol, "S");
    dl->AddText(ImVec2(a1.x - 60 * S, a1.y + 4 * S), txtCol, "life (cycles)");
    std::snprintf(lab, sizeof(lab), "Se %.0f", SeD * u.stressFromMpa);
    dl->AddText(ImVec2(p0.x + 2 * S, Y(SeD) - 6 * S), txtCol, lab);
    dl->PopClipRect();
  }
  double seH = saEq(m), lifeH = sn_life(seH, SeD, Sut);
  if (lifeH >= 1e6)
    ImGui::TextDisabled("%s life at %.1f%%: infinite (runout).",
                        kTheoryNames[m], s.fatigueReliability);
  else
    ImGui::TextDisabled("%s life at %.1f%%: %.3g cycles.", kTheoryNames[m],
                        s.fatigueReliability, lifeH);
}

// Compose the incoming-flow unit vector from a base axis + AoA + sideslip (Z-up
// world). Mirrors the solver's construction so the setup preview arrow and the
// readout match what gets solved.
static void flowIncomingDir(const Study &s, float out[3]) {
  static const float kAxis[6][3] = {{1, 0, 0},  {-1, 0, 0}, {0, 1, 0},
                                    {0, -1, 0}, {0, 0, 1},  {0, 0, -1}};
  int di = (s.flowDir < 0 || s.flowDir > 5) ? 0 : s.flowDir;
  float f0[3] = {kAxis[di][0], kAxis[di][1], kAxis[di][2]};
  // side0 = up x f0 (up = +Z); fall back to +Y if f0 is (anti)parallel to up.
  float up[3] = {0, 0, 1};
  float side0[3] = {up[1] * f0[2] - up[2] * f0[1],
                    up[2] * f0[0] - up[0] * f0[2],
                    up[0] * f0[1] - up[1] * f0[0]};
  float sl = std::sqrt(side0[0] * side0[0] + side0[1] * side0[1] +
                       side0[2] * side0[2]);
  if (sl < 1e-5f) {
    side0[0] = 0;
    side0[1] = 1;
    side0[2] = 0;
  } else {
    for (int k = 0; k < 3; ++k)
      side0[k] /= sl;
  }
  // up0 = f0 x side0
  float up0[3] = {f0[1] * side0[2] - f0[2] * side0[1],
                  f0[2] * side0[0] - f0[0] * side0[2],
                  f0[0] * side0[1] - f0[1] * side0[0]};
  const float a = s.flowAoA * 3.14159265f / 180.0f;      // pitch toward up0
  const float b = s.flowSideslip * 3.14159265f / 180.0f; // yaw toward side0
  float ca = std::cos(a), saa = std::sin(a), cb = std::cos(b), sb = std::sin(b);
  for (int k = 0; k < 3; ++k)
    out[k] = ca * cb * f0[k] + ca * sb * side0[k] + saa * up0[k];
  float l = std::sqrt(out[0] * out[0] + out[1] * out[1] + out[2] * out[2]);
  if (l > 1e-9f)
    for (int k = 0; k < 3; ++k)
      out[k] /= l;
}

void SimManager::drawFlowForm(AppState &st, Study &s) {
  const float S = ui::uiScale;
  // ---- incoming flow: base axis + angle of attack + sideslip ----
  ImGui::TextUnformatted("Flow direction (base axis)");
  ImGui::SetNextItemWidth(-FLT_MIN);
  if (ImGui::BeginCombo(
          "##dir",
          kDirLabels[(s.flowDir < 0 || s.flowDir > 5) ? 0 : s.flowDir])) {
    for (int i = 0; i < 6; ++i)
      if (ImGui::Selectable(kDirLabels[i], s.flowDir == i)) {
        s.flowDir = i;
        persist(st, s);
      }
    ImGui::EndCombo();
  }
  simTip("World axis the fluid flows toward. Angle of attack and sideslip tilt "
         "the stream off this "
         "axis, so you can sweep a fuselage or hull without re-orienting the "
         "part.");

  ImGui::TextUnformatted("Angle of attack");
  ImGui::SetNextItemWidth(-FLT_MIN);
  if (ImGui::SliderFloat("##aoa", &s.flowAoA, -30.0f, 30.0f, "%.1f deg")) {
  }
  if (ImGui::IsItemDeactivatedAfterEdit())
    persist(st, s);
  simTip("Pitch of the incoming stream relative to the base axis (about the "
         "side axis). Positive "
         "tilts the flow toward world +Z.");
  ImGui::TextUnformatted("Sideslip");
  ImGui::SetNextItemWidth(-FLT_MIN);
  if (ImGui::SliderFloat("##beta", &s.flowSideslip, -30.0f, 30.0f,
                         "%.1f deg")) {
  }
  if (ImGui::IsItemDeactivatedAfterEdit())
    persist(st, s);
  simTip("Yaw of the incoming stream relative to the base axis (about the up "
         "axis).");

  ImGui::Spacing();
  // ---- free-stream speed ----
  ImGui::TextUnformatted("Speed");
  ImGui::SetNextItemWidth(-FLT_MIN);
  if (ImGui::InputFloat("##speed", &s.flowSpeed, 0, 0, "%.2f m/s")) {
  }
  if (ImGui::IsItemDeactivatedAfterEdit())
    persist(st, s);
  simTip("Free-stream speed in metres per second.");

  // ---- fluid medium ----
  ImGui::TextUnformatted("Fluid");
  int fi = (s.flowFluid < 0 || s.flowFluid >= kNFluids) ? 0 : s.flowFluid;
  ImGui::SetNextItemWidth(-FLT_MIN);
  if (ImGui::BeginCombo("##fluid", kFluids[fi].label)) {
    for (int i = 0; i < kNFluids; ++i)
      if (ImGui::Selectable(kFluids[i].label, fi == i)) {
        s.flowFluid = i;
        if (i != kNFluids - 1) {
          s.flowDensity = kFluids[i].density;
          s.flowViscosity = kFluids[i].visc;
        }
        persist(st, s);
      }
    ImGui::EndCombo();
  }
  simTip("Working fluid. Water has ~815x the density and ~15x the Reynolds "
         "number of air at the same "
         "speed/size — pick it for hull / hydrodynamic studies.");
  if (s.flowFluid == kNFluids - 1) { // Custom
    ImGui::TextUnformatted("Density (kg/m3)");
    ImGui::SetNextItemWidth(-FLT_MIN);
    if (ImGui::InputFloat("##rho", &s.flowDensity, 0, 0, "%.3f")) {
    }
    if (ImGui::IsItemDeactivatedAfterEdit())
      persist(st, s);
    ImGui::TextUnformatted("Kinematic viscosity (m2/s)");
    ImGui::SetNextItemWidth(-FLT_MIN);
    if (ImGui::InputFloat("##nu", &s.flowViscosity, 0, 0, "%.2e")) {
    }
    if (ImGui::IsItemDeactivatedAfterEdit())
      persist(st, s);
  }

  // ---- surface roughness ----
  ImGui::TextUnformatted("Surface roughness");
  int ri = (s.flowRoughness < 0 || s.flowRoughness >= kNRoughness)
               ? 0
               : s.flowRoughness;
  ImGui::SetNextItemWidth(-FLT_MIN);
  if (ImGui::BeginCombo("##rough", kRoughness[ri].label)) {
    for (int i = 0; i < kNRoughness; ++i)
      if (ImGui::Selectable(kRoughness[i].label, ri == i)) {
        s.flowRoughness = i;
        persist(st, s);
      }
    ImGui::EndCombo();
  }
  simTip("Equivalent sand-grain roughness of the wetted surface. Rougher skin "
         "raises the friction "
         "drag — significant for hulls and large surfaces (qualitative "
         "correction).");
  if (s.flowRoughness == kNRoughness - 1) { // Custom
    ImGui::TextUnformatted("Roughness Ks (um)");
    ImGui::SetNextItemWidth(-FLT_MIN);
    if (ImGui::InputFloat("##ks", &s.flowRoughnessUm, 0, 0, "%.1f")) {
    }
    if (ImGui::IsItemDeactivatedAfterEdit())
      persist(st, s);
  }

  // ---- reference area for the coefficients ----
  ImGui::TextUnformatted("Reference area (mm2, 0 = auto frontal)");
  ImGui::SetNextItemWidth(-FLT_MIN);
  if (ImGui::InputFloat("##refarea", &s.flowRefAreaMm2, 0, 0, "%.0f")) {
  }
  if (ImGui::IsItemDeactivatedAfterEdit())
    persist(st, s);
  simTip("Area used to non-dimensionalise Cd and Cl. Leave 0 to use the "
         "projected frontal area; set "
         "the planform (wing) or wetted area to match a published coefficient "
         "convention.");

  ImGui::Spacing();
  // ---- resolution / runtime ----
  ImGui::TextUnformatted("Resolution");
  ImGui::SetNextItemWidth(-FLT_MIN);
  if (ImGui::BeginCombo("##preset",
                        kPresetLabels[(s.flowPreset < 0 || s.flowPreset > 3)
                                          ? 0
                                          : s.flowPreset])) {
    for (int i = 0; i < 4; ++i)
      if (ImGui::Selectable(kPresetLabels[i], s.flowPreset == i)) {
        s.flowPreset = i;
        persist(st, s);
      }
    ImGui::EndCombo();
  }
  simTip("Voxel resolution / run time. Thin walls need a finer preset or they "
         "leak flow.");

  // A dedicated Thermal study IS the OpenFOAM heat-transfer pipeline — it
  // always runs OpenFOAM with thermal on, so it skips the LBM/OpenFOAM selector
  // and the heat-transfer toggle.
  const bool thermalStudy = (s.type == StudyType::Thermal);

  bool ofAvail = false;
  std::string ofVer;
  if (st.simClient) {
    nlohmann::json caps = st.simClient->caps();
    if (caps.is_object()) {
      ofAvail = caps.value("openfoam", false);
      ofVer = caps.value("openfoam_version", std::string());
    }
  }

  if (!thermalStudy) {
    // ---- CFD engine: LBM (fast) vs OpenFOAM RANS (accurate, only if
    // provisioned) ----
    ImGui::Spacing();
    ImGui::TextUnformatted("Solver");
    // Fall back to LBM if OpenFOAM was selected but is no longer available.
    if (!ofAvail && s.flowSolver == 1) {
      s.flowSolver = 0;
      persist(st, s);
    }
    static const char *kSolverLabels[2] = {"LBM (fast)",
                                           "OpenFOAM RANS (accurate)"};
    int svi = (s.flowSolver == 1 && ofAvail) ? 1 : 0;
    ImGui::SetNextItemWidth(-FLT_MIN);
    if (ImGui::BeginCombo("##solver", kSolverLabels[svi])) {
      if (ImGui::Selectable(kSolverLabels[0], svi == 0)) {
        s.flowSolver = 0;
        persist(st, s);
      }
      if (ofAvail) {
        if (ImGui::Selectable(kSolverLabels[1], svi == 1)) {
          s.flowSolver = 1;
          persist(st, s);
        }
      } else {
        ImGui::BeginDisabled();
        ImGui::Selectable("OpenFOAM RANS (not provisioned)", false);
        ImGui::EndDisabled();
      }
      ImGui::EndCombo();
    }
    if (ofAvail) {
      std::string tip = "LBM: fast lattice-Boltzmann for quick comparisons. "
                        "OpenFOAM: steady RANS "
                        "(k-omega SST) with boundary-layer meshing — slower "
                        "but trustworthy Cd/Cl "
                        "for engineering decisions. Detected: " +
                        ofVer + ".";
      simTip(tip.c_str());
    } else {
      simTip(
          "LBM lattice-Boltzmann solver. OpenFOAM RANS is unavailable on this "
          "machine — "
          "provision it with `python -m tools.provision_openfoam --install`.");
    }
  } else {
    // Thermal study: force the OpenFOAM thermal pipeline; warn if it isn't
    // provisioned.
    s.flowSolver = 1;
    s.flowThermal = true;
    if (!ofAvail) {
      ImGui::Spacing();
      ImGui::PushStyleColor(ImGuiCol_Text, simRole(ui::palette.statusWarn));
      ImGui::TextWrapped("Thermal studies need OpenFOAM, which isn't detected. "
                         "Provision it with "
                         "`python -m tools.provision_openfoam --install`.");
      ImGui::PopStyleColor();
    }
  }

  // ---- thermal / heat transfer ----
  // Flow study: an opt-in "Heat transfer" toggle (OpenFOAM only). Thermal
  // study: always on.
  bool showThermal = thermalStudy ? ofAvail : (s.flowSolver == 1 && ofAvail);
  if (!thermalStudy && s.flowSolver == 1 && ofAvail) {
    ImGui::Spacing();
    if (ImGui::Checkbox("Heat transfer (thermal)", &s.flowThermal))
      persist(st, s);
    simTip("Solve the energy equation (buoyantSimpleFoam) for a heated part: "
           "surface temperature, "
           "the thermal plume, and total wall heat flow. Applied to the whole "
           "part surface.");
  }
  if (showThermal && (thermalStudy || s.flowThermal)) {
    {
      ImGui::Indent(10.0f * S);
      ImGui::TextUnformatted("Thermal model");
      static const char *kThermTypes[2] = {"Fluid (surface heat)",
                                           "Conjugate (solid + fluid)"};
      int tt = (s.flowThermalType == 1) ? 1 : 0;
      ImGui::SetNextItemWidth(-FLT_MIN);
      if (ImGui::BeginCombo("##thermtype", kThermTypes[tt])) {
        for (int i = 0; i < 2; ++i)
          if (ImGui::Selectable(kThermTypes[i], tt == i)) {
            s.flowThermalType = i;
            persist(st, s);
          }
        ImGui::EndCombo();
      }
      simTip("Fluid: a thermal boundary condition on the part surface heats "
             "the air. "
             "Conjugate (CHT): the solid part conducts heat from an internal "
             "power dissipation "
             "and the air cools it — the right model for electronics / "
             "heat-sink cooling. "
             "CHT meshes a solid region and takes a few minutes.");

      ImGui::TextUnformatted("Inlet/ambient temp (degC)");
      ImGui::SetNextItemWidth(-FLT_MIN);
      if (ImGui::InputFloat("##inletT", &s.flowInletTempC, 0, 0, "%.1f")) {
      }
      if (ImGui::IsItemDeactivatedAfterEdit())
        persist(st, s);

      if (s.flowThermalType == 1) {
        ImGui::TextUnformatted("Solid conductivity (W/mK)");
        ImGui::SetNextItemWidth(-FLT_MIN);
        if (ImGui::InputFloat("##kappa", &s.flowSolidKappa, 0, 0, "%.1f")) {
        }
        if (ImGui::IsItemDeactivatedAfterEdit())
          persist(st, s);
        simTip("Thermal conductivity of the part material. ~200 aluminium, ~50 "
               "steel, ~0.2 plastic.");
        ImGui::TextUnformatted("Dissipated power (W)");
        ImGui::SetNextItemWidth(-FLT_MIN);
        if (ImGui::InputFloat("##chtP", &s.flowPowerW, 0, 0, "%.2f")) {
        }
        if (ImGui::IsItemDeactivatedAfterEdit())
          persist(st, s);
        simTip("Total heat generated inside the solid part (e.g. a component's "
               "power draw).");
      } else {
        ImGui::TextUnformatted("Wall condition");
        static const char *kWallModes[3] = {
            "Fixed temperature", "Heat flux (W/m2)", "Total power (W)"};
        int wm =
            (s.flowWallMode < 0 || s.flowWallMode > 2) ? 2 : s.flowWallMode;
        ImGui::SetNextItemWidth(-FLT_MIN);
        if (ImGui::BeginCombo("##wallmode", kWallModes[wm])) {
          for (int i = 0; i < 3; ++i)
            if (ImGui::Selectable(kWallModes[i], wm == i)) {
              s.flowWallMode = i;
              persist(st, s);
            }
          ImGui::EndCombo();
        }
        simTip("How the part heats the fluid: hold the wall at a fixed "
               "temperature, drive a uniform "
               "heat flux, or dissipate a fixed total power across the whole "
               "surface.");
        if (wm == 0) {
          ImGui::TextUnformatted("Wall temperature (degC)");
          ImGui::SetNextItemWidth(-FLT_MIN);
          if (ImGui::InputFloat("##wallT", &s.flowWallTempC, 0, 0, "%.1f")) {
          }
          if (ImGui::IsItemDeactivatedAfterEdit())
            persist(st, s);
        } else if (wm == 1) {
          ImGui::TextUnformatted("Heat flux (W/m2)");
          ImGui::SetNextItemWidth(-FLT_MIN);
          if (ImGui::InputFloat("##wallQ", &s.flowWallFluxWm2, 0, 0, "%.1f")) {
          }
          if (ImGui::IsItemDeactivatedAfterEdit())
            persist(st, s);
        } else {
          ImGui::TextUnformatted("Total power (W)");
          ImGui::SetNextItemWidth(-FLT_MIN);
          if (ImGui::InputFloat("##wallP", &s.flowWallPowerW, 0, 0, "%.2f")) {
          }
          if (ImGui::IsItemDeactivatedAfterEdit())
            persist(st, s);
        }
      }
      ImGui::Unindent(10.0f * S);
    }
  }

  // ---- live setup readout: resolved stream vector + Reynolds estimate ----
  ImGui::Spacing();
  float dir[3];
  flowIncomingDir(s, dir);
  float density, visc;
  flowFluidProps(s, density, visc);
  pushDimText();
  ImGui::Text("Incoming flow: (%.2f, %.2f, %.2f)", dir[0], dir[1], dir[2]);
  // Reynolds estimate off the model's bounding diagonal (a stand-in for the
  // characteristic length).
  float lo[3] = {1e30f, 1e30f, 1e30f}, hi[3] = {-1e30f, -1e30f, -1e30f};
  bool any = false;
  for (const auto &kv : st.faceNormalCache) {
    for (int k = 0; k < 3; ++k) {
      lo[k] = std::min(lo[k], kv.second[3 + k]);
      hi[k] = std::max(hi[k], kv.second[3 + k]);
    }
    any = true;
  }
  if (any && visc > 0) {
    float Lc = std::max({hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2]}) *
               1e-3f; // mm -> m
    double Re = (double)s.flowSpeed * Lc / visc;
    ImGui::Text("Re ~ %.0f  |  rho %.3g kg/m3", Re, density);
  }
  popDimText();
  (void)S;
}

void SimManager::drawResultControls(AppState &st, Study &s) {
  // T-FIN24 kinds render numeric/plot results (no viewport overlay).
  if (s.type == StudyType::Buckling || s.type == StudyType::Contact ||
      s.type == StudyType::FreqResponse || s.type == StudyType::ThermalFea) {
    drawAdvancedResults(st, s);
    return;
  }
  // Motion results: timeline + trace plot — no colormap, no metric table.
  if (s.type == StudyType::Motion) {
    float rH = ImGui::GetContentRegionAvail().y - 50.0f * ui::uiScale;
    ImGui::BeginChild("##motionresults",
                      ImVec2(0, std::max(rH, 80.0f * ui::uiScale)), false);
    drawMotionResults(st, s);
    ImGui::EndChild();
    return;
  }

  if (!s.hasResult) {
    ImGui::TextWrapped("No results yet. Set up the study and press Run.");
    return;
  }
  const UnitSys u = unitSys(st);
  char b[80];

  // Scroll so a long metric list never clips at the bottom of the narrow pane.
  // Reserve ~50px for the Run button that drawForm places after the switch.
  float rH = ImGui::GetContentRegionAvail().y - 50.0f * ui::uiScale;
  ImGui::BeginChild("##simresults",
                    ImVec2(0, std::max(rH, 80.0f * ui::uiScale)), false);

  ImGui::PushFont(st.fonts.medium);
  ImGui::TextUnformatted("Results");
  ImGui::PopFont();
  ImGui::Spacing();

  // --- result-field selector (static + fatigue + flow expose multiple fields;
  // switching is instant) ---
  if (s.type == StudyType::Static || s.type == StudyType::Fatigue ||
      isFlowFamily(s.type)) {
    const ResultField *arr = (s.type == StudyType::Static)    ? kStaticFields
                             : (s.type == StudyType::Fatigue) ? kFatigueFields
                                                              : kFlowFields;
    const int n = (s.type == StudyType::Static)    ? kNStaticFields
                  : (s.type == StudyType::Fatigue) ? kNFatigueFields
                                                   : kNFlowFields;
    ImGui::TextUnformatted("Plot");
    // For flow/thermal, only offer fields the result actually carries (aero ->
    // pressure_cp/ velocity; thermal/CHT -> temperature/velocity). FEA studies
    // expose their full field set.
    auto avail = [&](int i) {
      return !isFlowFamily(s.type) ||
             flowFieldPresent(s.flowFields, arr[i].chunk);
    };
    int curIdx = 0;
    for (int i = 0; i < n; ++i)
      if (st.activeScalarField == arr[i].chunk)
        curIdx = i;
    if (!avail(curIdx))
      for (int i = 0; i < n; ++i)
        if (avail(i)) {
          curIdx = i;
          break;
        }
    ImGui::SetNextItemWidth(-FLT_MIN);
    if (ImGui::BeginCombo("##field", arr[curIdx].label)) {
      for (int i = 0; i < n; ++i) {
        if (!avail(i))
          continue;
        if (ImGui::Selectable(arr[i].label, i == curIdx)) {
          st.activeScalarField = arr[i].chunk;
          applyResultField(st, s);
          // Velocity reads on the slice planes; pressure reads on the body. The
          // body carries only the Cp scalar, so for the velocity view drop the
          // body colormap (it would otherwise fall back to Cp) and let the
          // speed-colored slices carry the field.
          if (isFlowFamily(s.type)) {
            if (st.activeScalarField == "velocity") {
              flowShowSlices_ = true;
              st.colormapOn = false;
              st.legendOn = false;
              if (st.renderer)
                st.renderer->setColormapMode(false);
            } else {
              flowShowSlices_ = false;
            }
            rebuildFlowLines(st, s);
          }
        }
      }
      ImGui::EndCombo();
    }
    simTip("Which result field colors the model. Switching is instant — the "
           "same result is "
           "re-colored, no re-solve.");
    ImGui::Spacing();
  }

  // --- metric table ---
  auto row = [&](const char *k, const std::string &v, bool warn = false) {
    ImGui::TableNextRow();
    ImGui::TableNextColumn();
    ImGui::TextUnformatted(k);
    ImGui::TableNextColumn();
    if (warn)
      ImGui::PushStyleColor(ImGuiCol_Text, simRole(ui::palette.statusErr));
    ImGui::TextUnformatted(v.c_str());
    if (warn)
      ImGui::PopStyleColor();
  };
  auto stressV = [&](const char *key) {
    double mpa = s.resultSummary.value(key, 0.0);
    std::snprintf(b, sizeof(b), "%.4g %s", mpa * u.stressFromMpa, u.stress);
    return std::string(b);
  };
  // Fatigue renders its own rich multi-theory view (drawFatigueGraphs) — skip
  // the generic table.
  if (s.type != StudyType::Fatigue &&
      ImGui::BeginTable("##restab", 2,
                        ImGuiTableFlags_SizingStretchProp |
                            ImGuiTableFlags_BordersInnerH)) {
    if (s.type == StudyType::Modal) {
      for (size_t i = 0; i < s.freqs.size(); ++i) {
        std::snprintf(b, sizeof(b), "%.2f Hz", s.freqs[i]);
        row(("Mode " + std::to_string(i + 1)).c_str(), b);
      }
      if (s.freqs.empty())
        row("Frequencies", "(none)");
    } else if (isFlowFamily(s.type) &&
               flowFieldPresent(s.flowFields, "temperature")) {
      // Thermal / CHT result: temperatures (degC), not forces. cpMin/cpMax
      // carry the surface (or solid) temperature range; the banner adds inlet +
      // total heat flow.
      std::snprintf(b, sizeof(b), "%.1f degC", s.flowInletTempC);
      row("Inlet / ambient", b);
      const char *lbl =
          (s.flowThermalType == 1) ? "Part temp (min)" : "Surface temp (min)";
      std::snprintf(b, sizeof(b), "%.1f degC", s.cpMin);
      row(lbl, b);
      std::snprintf(b, sizeof(b), "%.1f degC", s.cpMax);
      row((s.flowThermalType == 1) ? "Part temp (max)" : "Surface temp (max)",
          b);
      std::snprintf(b, sizeof(b), "%.0f", s.reynolds);
      row("Reynolds number", b);
    } else if (isFlowFamily(s.type)) {
      std::snprintf(b, sizeof(b), "%.4g N", s.dragN);
      row("Drag", b);
      std::snprintf(b, sizeof(b), "%.4g N", s.liftN);
      row("Lift", b);
      if (std::fabs(s.sideN) > 1e-9) {
        std::snprintf(b, sizeof(b), "%.4g N", s.sideN);
        row("Side force", b);
      }
      std::snprintf(b, sizeof(b), "%.4f", s.cd);
      row("Drag coefficient Cd", b);
      std::snprintf(b, sizeof(b), "%.4f", s.cl);
      row("Lift coefficient Cl", b);
      std::snprintf(b, sizeof(b), "%.2f", s.ld);
      row("Lift / drag (L/D)", b);
      std::snprintf(b, sizeof(b), "%.0f", s.reynolds);
      row("Reynolds number", b);
      std::snprintf(b, sizeof(b), "%.0f mm^2",
                    s.refAreaMm2 > 0 ? s.refAreaMm2 : s.frontalMm2);
      row(s.flowRefAreaMm2 > 0 ? "Reference area" : "Frontal area", b);
    } else {
      const auto &sm = s.resultSummary;
      row("Max von Mises", stressV("max_von_mises_mpa"));
      if (sm.contains("max_tresca_mpa"))
        row("Max shear stress (Tresca)", stressV("max_tresca_mpa"));
      else if (sm.contains("max_shear_mpa"))
        row("Max shear", stressV("max_shear_mpa"));
      if (sm.contains("max_principal_mpa"))
        row("Max principal (P1)", stressV("max_principal_mpa"));
      if (sm.contains("min_principal_mpa"))
        row("Min principal (P3)", stressV("min_principal_mpa"));
      double mm = sm.value("max_disp_mm", 0.0);
      std::snprintf(b, sizeof(b), "%.4g %s", mm * u.lenFromMm, u.len);
      row("Max displacement", b);
      // Headline FoS per the selected failure criterion (fall back to von Mises
      // if the engine build in use doesn't yet carry the criterion-specific
      // keys).
      const char *fosKey =
          (s.criterion == FailureCriterion::Tresca)      ? "min_fos_tresca"
          : (s.criterion == FailureCriterion::MaxNormal) ? "min_fos_rankine"
                                                         : "min_fos";
      const char *critName =
          (s.criterion == FailureCriterion::Tresca)      ? "Tresca"
          : (s.criterion == FailureCriterion::MaxNormal) ? "max normal"
                                                         : "von Mises";
      double fos = sm.value(fosKey, sm.value("min_fos", 0.0));
      std::snprintf(b, sizeof(b), "%.2f", fos);
      row((std::string("Min FoS (") + critName + ")").c_str(), b,
          fos > 0.0 && fos < 1.0);
    }
    ImGui::EndTable();
  }
  // Yield warning for static/modal (fatigue shows per-theory FoS in its own
  // table).
  if (s.type == StudyType::Static || s.type == StudyType::Modal) {
    if (s.resultSummary.value("min_fos", 99.0) < 1.0) {
      ImGui::PushStyleColor(ImGuiCol_Text, simRole(ui::palette.statusErr));
      ImGui::TextWrapped(
          "Factor of safety below 1 — the material yields under this load.");
      ImGui::PopStyleColor();
    }
  }
  if (s.type == StudyType::Fatigue)
    drawFatigueGraphs(st, s);
  if (!s.banner.empty()) {
    ImGui::PushStyleColor(ImGuiCol_Text, simRole(ui::palette.statusWarn));
    ImGui::TextWrapped("Qualitative result: %s", s.banner.c_str());
    ImGui::PopStyleColor();
  }
  if (isFlowFamily(s.type) && !s.dragTrace.empty())
    ImGui::PlotLines("##drag", s.dragTrace.data(), (int)s.dragTrace.size(), 0,
                     "Drag trace", FLT_MAX, FLT_MAX,
                     ImVec2(-FLT_MIN, 50.0f * ui::uiScale));

  // --- transparency: what the solver actually did ---
  if (s.nNodes > 0 || s.nElems > 0 || s.nDof > 0 || !s.solverNotes.empty()) {
    ImGui::Spacing();
    if (ImGui::TreeNodeEx("Mesh & solver", ImGuiTreeNodeFlags_DefaultOpen)) {
      if (s.nNodes > 0)
        ImGui::Text("Nodes: %d", s.nNodes);
      if (s.nElems > 0)
        ImGui::Text("Elements: %d", s.nElems);
      if (s.nDof > 0)
        ImGui::Text("Equations (DOF): %d", s.nDof);
      for (const auto &n : s.solverNotes)
        ImGui::BulletText("%s", n.c_str());
      ImGui::TreePop();
    }
  }

  // --- flow visualization controls: colormap + streamline / slice line
  // overlays ---
  if (isFlowFamily(s.type)) {
    ImGui::Spacing();
    ImGui::Separator();
    if (ImGui::Checkbox("Show colormap", &st.colormapOn)) {
      if (st.renderer)
        st.renderer->setColormapMode(st.colormapOn);
      st.legendOn = st.colormapOn && st.docMeshHasScalar;
    }
    simTip("Color the model by the selected plot field (Cp / temperature, or "
           "velocity on the slices).");
    if (ImGui::Checkbox("Show streamlines", &flowShowStreamlines_))
      rebuildFlowLines(st, s);
    simTip("Trace the flow as speed-colored streamlines over the model.");
    ImGui::SameLine();
    if (ImGui::Checkbox("Show slices", &flowShowSlices_))
      rebuildFlowLines(st, s);
    simTip("Velocity cross-section planes (mid-X / mid-Y / mid-Z) drawn as a "
           "speed-colored grid.");
  }

  // --- visualization controls (stacked so nothing clips in the narrow pane)
  // ---
  if (!isFlowFamily(s.type)) {
    ImGui::Spacing();
    ImGui::Separator();
    if (ImGui::Checkbox("Show colormap", &st.colormapOn)) {
      if (st.renderer)
        st.renderer->setColormapMode(st.colormapOn);
      st.legendOn = st.colormapOn && st.docMeshHasScalar;
    }
    simTip("Color the model by the selected result field.");
    if (st.docMeshHasDisp && st.renderer) {
      ImGui::Spacing();
      const float autoS = st.dispScaleHint > 0 ? st.dispScaleHint : 1.0f;
      ImGui::Text("Deformation scale  (auto = %.3gx)", autoS);
      float scale = st.renderer->deformScale();
      float maxScale = autoS * 2.0f;
      ImGui::SetNextItemWidth(-FLT_MIN);
      if (ImGui::SliderFloat("##deform", &scale, 0.0f, maxScale, "%.3gx"))
        st.renderer->setDeformScale(scale);
      simTip("Exaggerate the deformed shape. Auto picks a visible scale; 1x is "
             "the real "
             "(true-scale) displacement; Off shows the undeformed model.");
      // Auto = the suggested visible exaggeration; 1x = real displacement; Off
      // = undeformed.
      if (ImGui::SmallButton("Auto"))
        st.renderer->setDeformScale(autoS);
      ImGui::SameLine();
      if (ImGui::SmallButton("1x (true)"))
        st.renderer->setDeformScale(1.0f);
      ImGui::SameLine();
      if (ImGui::SmallButton("Off"))
        st.renderer->setDeformScale(0.0f);
    }
  }

  ImGui::EndChild();
}

// ---- Stepwise setup checklist (all structural sub-nodes)
// ----------------------- Replaces the scattered Study-overview inputs with
// numbered, glass-carded sections. Called for every structural SimNode
// (Study/Material/Fixtures/Load/Mesh); SetNextItemOpen auto-expands the
// matching section when a tree leaf is selected. Flow/Thermal short-circuit to
// the flow form (no material / fixture steps apply there).
void SimManager::drawStudyChecklist(AppState &st, Study &s) {
  const float S = ui::uiScale;
  const auto &pal = ui::palette;

  // ---------- Motion: banner + motion form, no structural steps ----------
  if (s.type == StudyType::Motion) {
    drawMotionForm(st, s);
    return;
  }

  // ---------- Flow / Thermal: banner + flow form, no structural steps
  // ----------
  if (isFlowFamily(s.type)) {
    drawStudyTypeBanner(
        s.type, s.type == StudyType::Thermal
                    ? "Steady-state heat transfer (OpenFOAM): convection with "
                      "wall / heat-source "
                      "boundary conditions. Structural material is not used."
                    : "External flow / wind-tunnel (CFD): drag, lift and "
                      "pressure from the velocity "
                      "and fluid. Structural material is not used.");
    drawFlowForm(st, s);
    return;
  }

  // Study-type intent banner
  if (s.type == StudyType::Modal)
    drawStudyTypeBanner(
        s.type, "Natural frequencies and mode shapes. Loads are not applicable "
                "\xe2\x80\x94 fixtures (optional) constrain rigid-body modes.");
  else if (s.type == StudyType::Static)
    drawStudyTypeBanner(
        s.type, "Linear static stress under applied loads. Set material, "
                "fixtures, loads and mesh, then run for stress, displacement "
                "and factor of safety.");
  else // Fatigue
    drawStudyTypeBanner(
        s.type, "Fatigue life under cyclic loading: the static stress state is "
                "scaled over the load cycle (Goodman / Gerber / Soderberg).");

  // Helper: draw a small filled-circle badge to the right of the
  // most-recently-drawn item. green=set, amber=required+missing, dim=optional.
  auto drawBadge = [&](bool isSet, bool required) {
    ImU32 col;
    if (isSet) {
      col = pal.toU32(pal.statusOk);
    } else if (required) {
      col = pal.toU32(pal.statusWarn);
    } else {
      float d[4];
      Palette::withAlpha(pal.textDim, d, 0.55f);
      col = pal.toU32(d);
    }
    ImVec2 rmin = ImGui::GetItemRectMin();
    ImVec2 rmax = ImGui::GetItemRectMax();
    float cx = rmax.x - 14.0f * S;
    float cy = (rmin.y + rmax.y) * 0.5f;
    ImGui::GetWindowDrawList()->AddCircleFilled(ImVec2(cx, cy), 5.0f * S, col);
  };

  bool matSet = !s.materialKey.empty();
  bool fixSet = !s.fixtures.empty();
  bool matReq = true; // material always required for structural
  bool fixReq = (s.type != StudyType::Modal); // modal can run free-free

  // ---- Step 1: Material ----
  // Open when this leaf is selected in the tree, or on the Study overview root.
  if (node_ == SimNode::Material || node_ == SimNode::Study)
    ImGui::SetNextItemOpen(true, node_ == SimNode::Study ? ImGuiCond_Once
                                                         : ImGuiCond_Always);
  bool matOpen = ImGui::CollapsingHeader("1  Material");
  drawBadge(matSet, matReq);
  if (ImGui::IsItemHovered())
    ImGui::SetTooltip("Material defines stiffness (E), Poisson ratio, density "
                      "and yield strength\n"
                      "used in the stress solve and factor-of-safety result.\n"
                      "Required for all structural and fatigue studies.");
  if (matOpen) {
    if (beginCheckCard(
            "##ckmat")) { // v2 frosted card (pane ink; no theme re-pair)
      drawMaterialCombo(st, s);
      // Static: failure criterion lives logically with the material choice
      if (s.type == StudyType::Static) {
        ImGui::Spacing();
        ImGui::SeparatorText("Failure criterion");
        static const char *kCrit[3] = {"Max von Mises (distortion energy)",
                                       "Max shear stress (Tresca)",
                                       "Max normal stress (Rankine)"};
        int ci = (int)s.criterion;
        if (ci < 0 || ci > 2)
          ci = 0;
        ImGui::SetNextItemWidth(-FLT_MIN);
        if (ImGui::BeginCombo("##crit2", kCrit[ci])) {
          for (int i = 0; i < 3; ++i)
            if (ImGui::Selectable(kCrit[i], ci == i)) {
              s.criterion = (FailureCriterion)i;
              persist(st, s);
            }
          ImGui::EndCombo();
        }
        simTip("Theory for the headline factor of safety. Ductile metals: von "
               "Mises or "
               "Tresca (more conservative). Brittle: max normal stress.");
      }
      // Modal: mode count lives with material (drives the mass matrix order)
      if (s.type == StudyType::Modal) {
        ImGui::Spacing();
        ImGui::SeparatorText("Modes");
        ImGui::SetNextItemWidth(-FLT_MIN);
        if (ImGui::SliderInt("##modes2", &s.nModes, 1, 12)) {
        }
        if (ImGui::IsItemDeactivatedAfterEdit())
          persist(st, s);
        simTip("How many natural frequencies / mode shapes to solve for.");
      }
    }
    endCheckCard();
  }
  ImGui::Spacing();

  // ---- Step 2: Fixtures ----
  if (node_ == SimNode::Fixtures)
    ImGui::SetNextItemOpen(true, ImGuiCond_Always);
  bool fixOpen = ImGui::CollapsingHeader("2  Fixtures");
  drawBadge(fixSet, fixReq);
  if (ImGui::IsItemHovered()) {
    if (s.type == StudyType::Modal)
      ImGui::SetTooltip("Fixtures (optional for frequency studies): constrains "
                        "rigid-body modes.\n"
                        "A free-free modal solve runs without fixtures.");
    else
      ImGui::SetTooltip("Fixtures constrain the part so it cannot translate or "
                        "rotate as a rigid body.\n"
                        "Required for static and fatigue studies \xe2\x80\x94 "
                        "at least one fixed face.");
  }
  if (fixOpen) {
    if (beginCheckCard("##ckfix")) {
      drawFixturesForm(st, s);
    }
    endCheckCard();
  }
  ImGui::Spacing();

  // ---- Step 3: Loads (hidden for modal) ----
  if (s.type != StudyType::Modal) {
    if (node_ == SimNode::Load)
      ImGui::SetNextItemOpen(true, ImGuiCond_Always);
    bool loadOpen = ImGui::CollapsingHeader("3  Loads");
    drawBadge(!s.loads.empty(), false); // optional: neutral when empty
    if (ImGui::IsItemHovered())
      ImGui::SetTooltip(
          "Loads (optional): forces or pressures applied to model faces.\n"
          "A study without loads produces zero stress \xe2\x80\x94 add at "
          "least one load\n"
          "to get a meaningful structural result.");
    if (loadOpen) {
      if (beginCheckCard("##ckloads")) {
        drawLoadsForm(st, s);
      }
      endCheckCard();
    }
    ImGui::Spacing();
  }

  // ---- Step 4: Mesh ----
  if (node_ == SimNode::Mesh)
    ImGui::SetNextItemOpen(true, ImGuiCond_Always);
  bool meshOpen = ImGui::CollapsingHeader("4  Mesh");
  drawBadge(true, false); // mesh always has sane defaults — neutral/green
  if (ImGui::IsItemHovered())
    ImGui::SetTooltip(
        "Mesh: controls element size and type.\n"
        "Finer elements capture stress concentrations (holes, fillets) better\n"
        "but increase solve time. The defaults are a good starting point.");
  if (meshOpen) {
    if (beginCheckCard("##ckmesh")) {
      drawMeshForm(st, s);
    }
    endCheckCard();
  }

  // ---- Step 5: Fatigue parameters (fatigue studies only) ----
  if (s.type == StudyType::Fatigue) {
    ImGui::Spacing();
    bool fatigueOpen = ImGui::CollapsingHeader("5  Fatigue parameters");
    drawBadge(true, false);
    if (ImGui::IsItemHovered())
      ImGui::SetTooltip(
          "Cyclic loading definition, Marin endurance corrections,\n"
          "and strength overrides (Sut / Se') for the fatigue solve.");
    if (fatigueOpen) {
      if (beginCheckCard("##ckfatigue")) {
        drawFatigueForm(st, s);
      }
      endCheckCard();
    }
  }
}

void SimManager::drawForm(AppState &st, Study &s) {
  const float S = ui::uiScale;
  // Keep study materialKey in lockstep with the part's active material.
  resolveStudyMaterialFromDoc(st, s);

  // Header: study name (editable).
  ImGui::PushFont(st.fonts.medium);
  ImGui::TextUnformatted(s.name.c_str());
  ImGui::PopFont();
  ImGui::SameLine();
  ImGui::TextDisabled("(%s)", studyTypeLabel(s.type));

  // Units selector — drives how loads + results are shown/entered. The solver
  // always works in SI; this is display/input only. "Follow document" tracks
  // the part's MMGS/IPS setting (so an IPS part shows stress in psi, lengths in
  // inches, forces in lbf by default).
  {
    static const char *kUnitItems[3] = {"Units: follow document",
                                        "Units: SI (MPa - N - mm)",
                                        "Units: US (psi - lbf - in)"};
    int ui_ = (st.simUnits < 0 || st.simUnits > 2) ? 0 : st.simUnits;
    ImGui::SetNextItemWidth(-FLT_MIN);
    if (ImGui::BeginCombo("##simunits", kUnitItems[ui_])) {
      for (int i = 0; i < 3; ++i)
        if (ImGui::Selectable(kUnitItems[i], ui_ == i)) {
          st.simUnits = i;
          // Re-label the legend in the new units if a result is on screen
          // (cheap; no reload).
          if (s.hasResult && st.colormapOn && st.renderer) {
            std::string label, unit;
            double fromSI = 1.0;
            fieldMeta(st, st.activeScalarField, label, unit, fromSI);
            st.legendTitle = label;
            st.legendUnits = unit;
            float vmin = 0, vmax = 1;
            if (st.renderer->scalarRange(vmin, vmax)) {
              st.legendMin = (float)(vmin * fromSI);
              st.legendMax = (float)(vmax * fromSI);
            }
          }
        }
      ImGui::EndCombo();
    }
    simTip("Display + input units for loads and results. The solver always "
           "computes in SI.");
  }

  ImGui::Spacing();

  // sim-gui rework: each panel shows ONLY the invoked tool's editor
  // (PropertyManager flow) — the numbered always-open accordion stack
  // (drawStudyChecklist) is retired. The Study node hosts the type-level setup
  // (flow/thermal BCs, fatigue loading, motion driver, or the static/modal
  // study options); every other node maps 1:1 to its ribbon tool.
  switch (node_) {
  case SimNode::Results:
    drawResultControls(st, s);
    break;
  case SimNode::Material:
    drawMaterialCombo(st, s);
    if (s.type == StudyType::Static) {
      // Failure criterion lives logically with the material choice.
      ImGui::Spacing();
      ImGui::SeparatorText("Failure criterion");
      static const char *kCrit[3] = {"Max von Mises (distortion energy)",
                                     "Max shear stress (Tresca)",
                                     "Max normal stress (Rankine)"};
      int ci = (int)s.criterion;
      if (ci < 0 || ci > 2)
        ci = 0;
      ImGui::SetNextItemWidth(-FLT_MIN);
      if (ImGui::BeginCombo("##critp", kCrit[ci])) {
        for (int i = 0; i < 3; ++i)
          if (ImGui::Selectable(kCrit[i], ci == i)) {
            s.criterion = (FailureCriterion)i;
            persist(st, s);
          }
        ImGui::EndCombo();
      }
      simTip("Theory for the headline factor of safety. Ductile metals: von "
             "Mises or "
             "Tresca (more conservative). Brittle: max normal stress.");
    }
    break;
  case SimNode::Fixtures:
    drawFixturesForm(st, s);
    if (s.type == StudyType::ThermalFea) {
      ImGui::Spacing();
      ImGui::SeparatorText("Fixed temperature");
      ImGui::SetNextItemWidth(-FLT_MIN);
      if (ImGui::InputFloat("##thermfixt", &s.thermFixTempC, 0, 0,
                            "T = %.1f degC")) {
      }
      if (ImGui::IsItemDeactivatedAfterEdit())
        persist(st, s);
      simTip("Dirichlet temperature held on the fixture faces (thermal FEA: "
             "fixtures "
             "are fixed-TEMPERATURE faces, not structural clamps).");
    }
    break;
  case SimNode::Load:
    drawLoadsForm(st, s);
    if (s.type == StudyType::ThermalFea && loadIdx_ >= 0 &&
        loadIdx_ < (int)s.loads.size()) {
      SimLoad &ld = s.loads[loadIdx_];
      ImGui::Spacing();
      ImGui::SeparatorText("Thermal load");
      static const char *kTk[2] = {"Heat flux (W/m2)", "Convection (h, T_amb)"};
      int tk = (ld.thermalKind == 1) ? 1 : 0;
      ImGui::SetNextItemWidth(-FLT_MIN);
      if (ImGui::BeginCombo("##thermloadkind", kTk[tk])) {
        for (int i = 0; i < 2; ++i)
          if (ImGui::Selectable(kTk[i], tk == i)) {
            ld.thermalKind = i;
            persist(st, s);
          }
        ImGui::EndCombo();
      }
      simTip(
          "For a thermal FEA study this load's faces carry heat flux in, or "
          "shed "
          "heat by convection (the force/pressure fields above are ignored).");
      if (ld.thermalKind == 0) {
        ImGui::SetNextItemWidth(-FLT_MIN);
        if (ImGui::InputFloat("##thermflux", &ld.fluxWm2, 0, 0,
                              "q = %.0f W/m2")) {
        }
        if (ImGui::IsItemDeactivatedAfterEdit())
          persist(st, s);
      } else {
        ImGui::SetNextItemWidth(-FLT_MIN);
        if (ImGui::InputFloat("##thermh", &ld.hWm2k, 0, 0,
                              "h = %.1f W/(m2 K)")) {
        }
        if (ImGui::IsItemDeactivatedAfterEdit())
          persist(st, s);
        ImGui::SetNextItemWidth(-FLT_MIN);
        if (ImGui::InputFloat("##thermtamb", &ld.tAmbC, 0, 0,
                              "T_amb = %.1f degC")) {
        }
        if (ImGui::IsItemDeactivatedAfterEdit())
          persist(st, s);
      }
    }
    break;
  case SimNode::Mesh:
    drawMeshForm(st, s);
    break;
  case SimNode::Study:
  default:
    if (s.type == StudyType::Motion) {
      drawMotionForm(st, s);
    } else if (isFlowFamily(s.type)) {
      drawStudyTypeBanner(
          s.type, s.type == StudyType::Thermal
                      ? "Steady-state heat transfer (OpenFOAM): convection "
                        "with wall / heat-source "
                        "boundary conditions. Structural material is not used."
                      : "External flow / wind-tunnel (CFD): drag, lift and "
                        "pressure from the "
                        "velocity and fluid. Structural material is not used.");
      drawFlowForm(st, s);
    } else if (s.type == StudyType::Fatigue) {
      drawStudyTypeBanner(s.type,
                          "Fatigue life under cyclic loading: the static "
                          "stress state is scaled over "
                          "the load cycle (Goodman / Gerber / Soderberg).");
      drawFatigueForm(st, s);
    } else if (s.type == StudyType::Modal) {
      drawStudyTypeBanner(s.type,
                          "Natural frequencies and mode shapes. Loads are not "
                          "applicable \xe2\x80\x94 "
                          "fixtures (optional) constrain rigid-body modes.");
      ImGui::SeparatorText("Modes");
      ImGui::SetNextItemWidth(-FLT_MIN);
      if (ImGui::SliderInt("##modesp", &s.nModes, 1, 12)) {
      }
      if (ImGui::IsItemDeactivatedAfterEdit())
        persist(st, s);
      simTip("How many natural frequencies / mode shapes to solve for.");
    } else if (s.type == StudyType::ThermalFea) {
      drawStudyTypeBanner(
          s.type,
          "Heat conduction on the part itself (core FEA solver): "
          "fixed-temperature "
          "faces (Fixtures) + flux / convection loads. Toggle Transient for a "
          "time-domain solve (backward Euler).");
      ImGui::SeparatorText("Solid properties");
      ImGui::SetNextItemWidth(-FLT_MIN);
      if (ImGui::InputFloat("##thermk", &s.thermK, 0, 0, "k = %.1f W/(m K)")) {
      }
      if (ImGui::IsItemDeactivatedAfterEdit())
        persist(st, s);
      simTip("Thermal conductivity of the solid. ~200 aluminium, ~50 steel, "
             "~400 copper, "
             "~0.3 FR4/plastics.");
      ImGui::SeparatorText("Transient");
      if (ImGui::Checkbox("Transient (time-domain)", &s.thermTransient))
        persist(st, s);
      simTip("Off = steady state. On = integrate C dT/dt + K T = f(t) to the "
             "horizon "
             "below (T-FIN18).");
      if (s.thermTransient) {
        ImGui::SetNextItemWidth(-FLT_MIN);
        if (ImGui::InputFloat("##thermtend", &s.thermTEndS, 0, 0,
                              "t_end = %.1f s")) {
        }
        if (ImGui::IsItemDeactivatedAfterEdit())
          persist(st, s);
        ImGui::SetNextItemWidth(-FLT_MIN);
        if (ImGui::InputFloat("##thermdt", &s.thermDtS, 0, 0,
                              "dt = %.2f s (0 = auto)")) {
        }
        if (ImGui::IsItemDeactivatedAfterEdit())
          persist(st, s);
        ImGui::SetNextItemWidth(-FLT_MIN);
        if (ImGui::InputFloat("##thermtinit", &s.thermTInitC, 0, 0,
                              "T_init = %.1f degC")) {
        }
        if (ImGui::IsItemDeactivatedAfterEdit())
          persist(st, s);
        ImGui::SetNextItemWidth(-FLT_MIN);
        if (ImGui::InputFloat("##thermcp", &s.thermCp, 0, 0,
                              "cp = %.0f J/(kg K)")) {
        }
        if (ImGui::IsItemDeactivatedAfterEdit())
          persist(st, s);
        simTip("Specific heat (transient only). ~900 aluminium, ~490 steel.");
      }
    } else if (s.type == StudyType::Buckling) {
      drawStudyTypeBanner(
          s.type,
          "Linear (eigenvalue) buckling via CalculiX: critical load factors "
          "for "
          "slender members. Solves a BOX PROXY sized to the part bounding box "
          "(fixed-free column) — an Euler-grade estimate, honest for "
          "strut-like parts.");
      if (capsStatus_ == 1 && caps_.contains("ccx") &&
          !caps_["ccx"].value("found", false)) {
        pushDimText();
        ImGui::TextWrapped(
            "%s", caps_["ccx"]
                      .value("hint", std::string("CalculiX (ccx) not found."))
                      .c_str());
        popDimText();
      }
      ImGui::SeparatorText("Compressive load");
      ImGui::SetNextItemWidth(-FLT_MIN);
      if (ImGui::InputFloat("##buckload", &s.bucklingLoadN, 0, 0,
                            "P = %.0f N")) {
      }
      if (ImGui::IsItemDeactivatedAfterEdit())
        persist(st, s);
      simTip("Axial compressive load the factors scale: critical load = P x "
             "lambda. "
             "lambda < 1 means the part buckles below this load.");
      ImGui::SetNextItemWidth(-FLT_MIN);
      if (ImGui::SliderInt("##buckmodes", &s.nModes, 1, 8, "%d modes")) {
      }
      if (ImGui::IsItemDeactivatedAfterEdit())
        persist(st, s);
    } else if (s.type == StudyType::Contact) {
      drawStudyTypeBanner(
          s.type,
          "Nonlinear contact via CalculiX (*STATIC, NLGEOM + *CONTACT PAIR): a "
          "two-"
          "block proxy sized to the part bounding box, verifying the interface "
          "transmits the applied load into the support (reaction = applied).");
      if (capsStatus_ == 1 && caps_.contains("ccx") &&
          !caps_["ccx"].value("found", false)) {
        pushDimText();
        ImGui::TextWrapped(
            "%s", caps_["ccx"]
                      .value("hint", std::string("CalculiX (ccx) not found."))
                      .c_str());
        popDimText();
      }
      ImGui::SeparatorText("Contact pressure");
      ImGui::SetNextItemWidth(-FLT_MIN);
      if (ImGui::InputFloat("##ctpress", &s.contactPressureMpa, 0, 0,
                            "p = %.2f MPa")) {
      }
      if (ImGui::IsItemDeactivatedAfterEdit())
        persist(st, s);
      simTip("Downward pressure on the upper block's top face.");
    } else if (s.type == StudyType::FreqResponse) {
      drawStudyTypeBanner(
          s.type,
          "Harmonic base-excitation response by modal superposition: each mode "
          "becomes "
          "an SDOF oscillator; responses combine by SRSS. Random (PSD) "
          "response is "
          "available via the copilot's run_fea_frequency_response tool.");
      ImGui::SeparatorText("Excitation");
      static const char *kAxes[3] = {"X axis", "Y axis", "Z axis"};
      int ax = (s.frDir < 0 || s.frDir > 2) ? 2 : s.frDir;
      ImGui::SetNextItemWidth(-FLT_MIN);
      if (ImGui::BeginCombo("##fraxis", kAxes[ax])) {
        for (int i = 0; i < 3; ++i)
          if (ImGui::Selectable(kAxes[i], ax == i)) {
            s.frDir = i;
            persist(st, s);
          }
        ImGui::EndCombo();
      }
      simTip("Base-excitation direction (the shaker axis).");
      ImGui::SetNextItemWidth(-FLT_MIN);
      if (ImGui::InputFloat("##frbaseg", &s.frBaseG, 0, 0,
                            "base input = %.2f g")) {
      }
      if (ImGui::IsItemDeactivatedAfterEdit())
        persist(st, s);
      ImGui::SetNextItemWidth(-FLT_MIN);
      if (ImGui::InputFloat("##frzeta", &s.frZeta, 0, 0,
                            "damping zeta = %.3f")) {
      }
      if (ImGui::IsItemDeactivatedAfterEdit())
        persist(st, s);
      simTip("Modal damping ratio (assumption, echoed on the result). 0.02 = Q "
             "of 25.");
      ImGui::SetNextItemWidth(-FLT_MIN);
      if (ImGui::SliderInt("##frmodes", &s.nModes, 1, 12, "%d modes")) {
      }
      if (ImGui::IsItemDeactivatedAfterEdit())
        persist(st, s);
    } else { // Static
      drawStudyTypeBanner(s.type, "Linear static stress under applied loads. "
                                  "Use the ribbon's Material / "
                                  "Fixture / Force / Pressure / Mesh tools (or "
                                  "double-click a tree node) to "
                                  "set the study up, then Run.");
    }
    break;
  }

  // ---- Gated Run button (always at the bottom, visible on every sub-node)
  // -------- Disabled when a solve is running OR prerequisites are unmet
  // (studyReady gate). A glass tooltip explains the first unmet prerequisite so
  // the user knows what to fix.
  {
    ImGui::Spacing();
    ImGui::Separator();
    ImGui::Spacing();
    bool busy =
        (st.simClient && st.simClient->busy()) || advRunning_ || prepInFlight_;
    std::string blocker;
    bool ready = studyReady(s, blocker);
    const bool canRun = ready && !busy;
    if (!canRun)
      ImGui::BeginDisabled();
    if (ImGui::Button(busy ? "Running\xe2\x80\xa6" : "Run",
                      ImVec2(-FLT_MIN, 30.0f * S)))
      run(st, s);
    if (!canRun)
      ImGui::EndDisabled();
    // Tooltip: blocker reason (when unmet) or generic hint (when ready).
    if (!busy && !ready)
      simTip(blocker.c_str());
    else
      simTip("Mesh the part and solve. Results color the model and appear "
             "under Results.");
    if (busy && st.simClient) {
      ImGui::SameLine();
      if (ImGui::SmallButton("Cancel"))
        st.simClient->cancel();
    }
    // Live drag-convergence sparkline and final result summary
    if (!s.dragTrace.empty())
      ImGui::PlotLines("##drag", s.dragTrace.data(), (int)s.dragTrace.size(), 0,
                       "Drag trace", FLT_MAX, FLT_MAX,
                       ImVec2(-FLT_MIN, 50.0f * S));
    if (s.hasResult && node_ != SimNode::Results) {
      ImGui::Spacing();
      ImGui::TextWrapped("%s", s.summary.c_str());
    }
  }
}

// ============================================================ Motion study
// (T-SIM3/T-SIM4)
//
// Motion studies drive ONE dimensional assembly mate across [start, stop] in
// `steps` samples. simulate_motion is a synchronous worker RPC (same path as
// sim_mesh / doc_export_step) that returns the full per-body world-transform
// trajectory in one shot.  The result is cached in the Study and played back on
// the main thread by tickMotion + applyMotionFrame, which CPU- transforms the
// base-pose vertex positions and re-uploads via Renderer::setMeshes().
//
// Engine-side solve path: worker_router dispatches "simulate_motion" ->
// engine.assemble.motion which calls solve_mates() at each driver step.  No
// simClient / sim worker involved.

// ---------- runMotion
// -------------------------------------------------------------------

void SimManager::runMotion(AppState &st, Study &s) {
  if (!st.docMirror.hasDoc()) {
    st.toast(ui::ToastLevel::Warn,
             "Open an assembly with mates before running Motion");
    return;
  }

  // Build RPC params.
  nlohmann::json params = {
      {"doc_id", st.docMirror.docId()},
      {"start", s.motionStart},
      {"stop", s.motionStop},
      {"steps", std::max(2, std::min(s.motionSteps, 120))},
      {"check_interference", s.motionCheckInterference},
  };
  if (!s.motionMateId.empty())
    params["mate_id"] = s.motionMateId;

  st.status = "Motion: solving\xe2\x80\xa6";
  st.statusKind = 0;

  nlohmann::json result;
  std::string err;
  const bool ok =
      st.worker.call("simulate_motion", params, result, err, 120000);
  if (!ok || result.contains("code")) {
    const std::string msg =
        result.contains("message") ? result.value("message", err) : err;
    st.addLog("[motion] simulate_motion failed: " + msg);
    st.toast(ui::ToastLevel::Error, msg.empty() ? "Motion solve failed" : msg);
    s.summary = "Motion failed: " + msg;
    return;
  }
  if (!result.value("ok", false)) {
    const std::string msg = result.value("error", std::string("motion error"));
    st.addLog("[motion] simulate_motion error: " + msg);
    st.toast(ui::ToastLevel::Error, msg);
    s.summary = "Motion failed: " + msg;
    return;
  }

  // Parse trajectory.
  s.motionTrajectory = result.value("trajectory", nlohmann::json::array());
  s.motionDriverUnit = "deg";
  if (result.contains("driver") && result["driver"].is_object())
    s.motionDriverUnit = result["driver"].value("unit", std::string("deg"));
  if (result.contains("mate_id") && result["mate_id"].is_string())
    s.motionMateId = result["mate_id"].get<std::string>();

  const int nFrames = (int)s.motionTrajectory.size();
  if (nFrames == 0) {
    st.toast(ui::ToastLevel::Warn, "Motion: trajectory returned no frames");
    s.summary = "Motion: no trajectory frames";
    return;
  }

  // Cache the base AICM tessellation so applyMotionFrame can re-upload
  // transformed geometry.
  motionDocMesh_ = render::DocMesh{};
  {
    std::string aicm, le;
    if (!st.docMirror.activate(st.worker, st.docMirror.docId(), aicm, le) ||
        aicm.empty()) {
      st.addLog("[motion] could not get AICM for base mesh: " + le);
    } else {
      std::string pe;
      if (!render::loadAicmFile(aicm, motionDocMesh_, pe) && !pe.empty())
        st.addLog("[motion] AICM parse warning: " + pe);
    }
  }

  // Build body-name order from frame 0.
  s.motionBodyNames.clear();
  if (!s.motionTrajectory.empty() && s.motionTrajectory[0].contains("bodies")) {
    for (const auto &b : s.motionTrajectory[0]["bodies"])
      if (b.contains("component") && b["component"].is_string())
        s.motionBodyNames.push_back(b["component"].get<std::string>());
  }
  s.motionPlotBody = 0;

  buildMotionTrace(s);

  s.motionHasResult = true;
  s.hasResult = true;
  s.motionPlaying = false;
  s.motionFrame = 0;
  s.motionLastTick = -1.0;

  // Interference note.
  std::string ifNote;
  if (s.motionCheckInterference && result.contains("interference")) {
    const auto &inf = result["interference"];
    if (inf.value("checked", false)) {
      int col = inf.value("first_collision_step", -1);
      char buf[80];
      if (col >= 0) {
        std::snprintf(buf, sizeof(buf), " | first collision step %d", col);
        ifNote = buf;
      } else
        ifNote = " | no collisions";
    }
  }
  char hdr[160];
  std::snprintf(
      hdr, sizeof(hdr), "Motion: %d frames, %.1f\xe2\x80\x93%.1f %s%s", nFrames,
      s.motionStart, s.motionStop, s.motionDriverUnit.c_str(), ifNote.c_str());
  s.summary = hdr;

  applyMotionFrame(st, s, 0);

  node_ = SimNode::Results;
  resultShown_ = true;
  shownStudyId_ = s.id;
  st.colormapOn = false;
  if (st.renderer)
    st.renderer->setColormapMode(false);
  st.legendOn = false;

  st.status = "Motion ready — " + s.summary;
  st.addLog("[motion] " + s.summary);
  st.toast(ui::ToastLevel::Info, hdr);
}

// ---------- buildMotionTrace
// ----------------------------------------------------------- Validates the
// trajectory parse; per-body traces are derived on demand in drawMotionResults.

void SimManager::buildMotionTrace(Study &s) {
  // Validate that each frame's bodies array is parseable: this surfaces JSON
  // shape errors once at run time rather than silently in the draw loop.
  for (const auto &frame : s.motionTrajectory) {
    if (!frame.contains("bodies") || !frame["bodies"].is_array())
      continue;
    for (const auto &b : frame["bodies"]) {
      if (!b.contains("transform") || !b["transform"].is_array())
        continue;
      if (b["transform"].size() < 16)
        continue;
      // One read to prove the values parse without throwing.
      (void)b["transform"][0].get<double>();
    }
  }
  // motionBodyNames is already set by runMotion before this call.
}

// ---------- applyMotionFrame
// ----------------------------------------------------------- CPU-transforms
// the cached base-pose vertex positions by each body's world transform from the
// trajectory, then re-uploads via Renderer::setMeshes(). Transform convention
// from simulate_motion: each body["transform"] is a row-major 4×4 float list
// where row i, col j = T[i*4+j].  Translation = T[3], T[7], T[11].

void SimManager::applyMotionFrame(AppState &st, Study &s, int frame) {
  if (!st.renderer)
    return;
  if (frame < 0 || frame >= (int)s.motionTrajectory.size())
    return;
  const auto &frameJ = s.motionTrajectory[frame];
  if (!frameJ.contains("bodies") || !frameJ["bodies"].is_array())
    return;

  // Build name -> 4×4 row-major transform map.
  struct Mat4r {
    float m[16];
  };
  std::map<std::string, Mat4r> bodyXf;
  for (const auto &b : frameJ["bodies"]) {
    if (!b.contains("component") || !b.contains("transform"))
      continue;
    const auto &xf = b["transform"];
    if (!xf.is_array() || xf.size() < 16)
      continue;
    Mat4r M;
    for (int i = 0; i < 16; ++i)
      M.m[i] = (float)xf[i].get<double>();
    bodyXf[b["component"].get<std::string>()] = M;
  }

  // Build PartMesh list: transform each body's vertices; unknowns stay at
  // identity.
  std::vector<Renderer::PartMesh> parts;
  parts.reserve(motionDocMesh_.bodies.size());

  for (const auto &body : motionDocMesh_.bodies) {
    Renderer::PartMesh pm;
    pm.body_id = body.body_id;
    pm.color[0] = body.color[0];
    pm.color[1] = body.color[1];
    pm.color[2] = body.color[2];
    pm.metallic = body.metallic;
    pm.roughness = body.roughness;

    for (const auto &f : body.faces)
      pm.faces.push_back({f.face_id, f.first_tri, f.tri_count});

    const uint32_t nv = body.n_verts;
    Mesh mesh;
    mesh.indices = body.idx;
    mesh.positions.resize(nv * 3);
    mesh.normals.resize(nv * 3, 0.0f);

    auto it = bodyXf.find(body.name);
    if (it != bodyXf.end()) {
      const float *M = it->second.m;
      for (uint32_t vi = 0; vi < nv; ++vi) {
        const float *p = &body.pos[vi * 3];
        mesh.positions[vi * 3 + 0] =
            M[0] * p[0] + M[1] * p[1] + M[2] * p[2] + M[3];
        mesh.positions[vi * 3 + 1] =
            M[4] * p[0] + M[5] * p[1] + M[6] * p[2] + M[7];
        mesh.positions[vi * 3 + 2] =
            M[8] * p[0] + M[9] * p[1] + M[10] * p[2] + M[11];
        if (!body.nrm.empty()) {
          const float *n = &body.nrm[vi * 3];
          mesh.normals[vi * 3 + 0] = M[0] * n[0] + M[1] * n[1] + M[2] * n[2];
          mesh.normals[vi * 3 + 1] = M[4] * n[0] + M[5] * n[1] + M[6] * n[2];
          mesh.normals[vi * 3 + 2] = M[8] * n[0] + M[9] * n[1] + M[10] * n[2];
        } else {
          mesh.normals[vi * 3 + 1] = 1.0f; // default up normal
        }
      }
    } else {
      // No transform for this body — copy as-is.
      for (uint32_t vi = 0; vi < nv; ++vi) {
        const float *p = &body.pos[vi * 3];
        mesh.positions[vi * 3 + 0] = p[0];
        mesh.positions[vi * 3 + 1] = p[1];
        mesh.positions[vi * 3 + 2] = p[2];
        if (!body.nrm.empty()) {
          mesh.normals[vi * 3 + 0] = body.nrm[vi * 3];
          mesh.normals[vi * 3 + 1] = body.nrm[vi * 3 + 1];
          mesh.normals[vi * 3 + 2] = body.nrm[vi * 3 + 2];
        } else {
          mesh.normals[vi * 3 + 1] = 1.0f;
        }
      }
    }
    pm.mesh = std::move(mesh);
    parts.push_back(std::move(pm));
  }

  // Freeze camera framing so the view doesn't jump on each frame advance.
  st.renderer->setFramingFrozen(true);
  st.renderer->setMeshes(parts);
  st.renderer->setFramingFrozen(false);
}

// ---------- scripted-shot motion helpers (AI_CAD_SHOT=motion)
// -------------------------

bool SimManager::motionHasResult() const {
  if (sel_ < 0 || sel_ >= (int)studies_.size())
    return false;
  const Study &s = studies_[sel_];
  return s.type == StudyType::Motion && s.motionHasResult;
}

int SimManager::motionFrameCount() const {
  if (sel_ < 0 || sel_ >= (int)studies_.size())
    return 0;
  const Study &s = studies_[sel_];
  if (s.type != StudyType::Motion || !s.motionHasResult)
    return 0;
  return (int)s.motionTrajectory.size();
}

void SimManager::applyMotionFramePublic(AppState &st, int frame) {
  if (sel_ < 0 || sel_ >= (int)studies_.size())
    return;
  Study &s = studies_[sel_];
  if (s.type != StudyType::Motion || !s.motionHasResult)
    return;
  s.motionFrame = frame;
  applyMotionFrame(st, s, frame);
}

// ---------- tickMotion
// -----------------------------------------------------------------

void SimManager::tickMotion(AppState &st) {
  if (sel_ < 0 || sel_ >= (int)studies_.size())
    return;
  Study &s = studies_[sel_];
  if (s.type != StudyType::Motion || !s.motionHasResult || !s.motionPlaying)
    return;

  const int nFrames = (int)s.motionTrajectory.size();
  if (nFrames <= 1)
    return;

  const double now = ImGui::GetTime();
  if (s.motionLastTick < 0.0) {
    s.motionLastTick = now;
    return;
  }
  const double elapsed = now - s.motionLastTick;
  const double frameDur = 1.0 / std::max(1.0f, s.motionFps);
  if (elapsed < frameDur)
    return;

  s.motionLastTick = now;
  s.motionFrame++;
  if (s.motionFrame >= nFrames) {
    if (s.motionLoop)
      s.motionFrame = 0;
    else {
      s.motionFrame = nFrames - 1;
      s.motionPlaying = false;
      return;
    }
  }
  applyMotionFrame(st, s, s.motionFrame);
}

// ---------- drawMotionForm (setup pane)
// ------------------------------------------------

void SimManager::drawMotionForm(AppState &st, Study &s) {
  const float S = ui::uiScale;

  drawStudyTypeBanner(s.type, "Motion study: drives one dimensional assembly "
                              "mate across a range, re-solves "
                              "the mate graph at each step, and plays back the "
                              "per-body trajectory in the viewport.");

  ImGui::Spacing();
  ImGui::PushFont(st.fonts.medium);
  ImGui::TextUnformatted("Driven mate");
  ImGui::PopFont();

  ImGui::SetNextItemWidth(-FLT_MIN);
  char mateIdBuf[128];
  std::strncpy(mateIdBuf, s.motionMateId.c_str(), sizeof(mateIdBuf) - 1);
  mateIdBuf[sizeof(mateIdBuf) - 1] = '\0';
  if (ImGui::InputText("##motionMateId", mateIdBuf, sizeof(mateIdBuf)))
    s.motionMateId = mateIdBuf;
  simTip("Mate ID to drive (e.g. \"m3\"). Leave blank to auto-pick the single "
         "drivable mate.");

  ImGui::Spacing();
  ImGui::PushFont(st.fonts.medium);
  ImGui::TextUnformatted("Driver range");
  ImGui::PopFont();

  ImGui::SetNextItemWidth(100.0f * S);
  if (ImGui::DragFloat("Start##ms", &s.motionStart, 1.0f, -720.0f, 720.0f,
                       "%.1f"))
    persist(st, s);
  simTip("Driver start value (deg for angle mates, mm for distance mates).");
  ImGui::SameLine(0, 12.0f * S);
  ImGui::SetNextItemWidth(100.0f * S);
  if (ImGui::DragFloat("Stop##ms", &s.motionStop, 1.0f, -720.0f, 720.0f,
                       "%.1f"))
    persist(st, s);
  simTip("Driver stop value.");

  int steps = s.motionSteps;
  ImGui::SetNextItemWidth(-FLT_MIN);
  if (ImGui::SliderInt("Steps", &steps, 2, 120)) {
    s.motionSteps = steps;
    persist(st, s);
  }
  simTip("Number of frames in the trajectory (2..120).");

  ImGui::Spacing();
  ImGui::PushFont(st.fonts.medium);
  ImGui::TextUnformatted("Playback");
  ImGui::PopFont();

  ImGui::SetNextItemWidth(80.0f * S);
  if (ImGui::DragFloat("FPS##ms", &s.motionFps, 0.5f, 1.0f, 120.0f, "%.0f"))
    persist(st, s);
  simTip("Playback speed in frames per second.");
  ImGui::SameLine(0, 16.0f * S);
  if (ImGui::Checkbox("Loop##ms", &s.motionLoop))
    persist(st, s);
  simTip("Wrap playback back to frame 0 after the last frame.");

  ImGui::Spacing();
  if (ImGui::Checkbox("Check interference at each step",
                      &s.motionCheckInterference))
    persist(st, s);
  simTip("Test for body-body intersection at each trajectory step (slower "
         "solve).");
}

// ---------- drawMotionTimeline (transport bar + scrub)
// --------------------------------

void SimManager::drawMotionTimeline(AppState &st, Study &s) {
  if (!s.motionHasResult)
    return;
  const int nFrames = (int)s.motionTrajectory.size();
  if (nFrames == 0)
    return;
  const float S = ui::uiScale;
  const auto &pal = ui::palette;

  ImGui::Spacing();
  const float bW = 28.0f * S, bH = 24.0f * S;

  if (ImGui::Button("|<##mt", ImVec2(bW, bH))) {
    s.motionPlaying = false;
    s.motionFrame = 0;
    applyMotionFrame(st, s, 0);
  }
  simTip("Rewind to first frame.");
  ImGui::SameLine(0, 2.0f * S);
  if (ImGui::Button("<<##mt", ImVec2(bW, bH))) {
    s.motionPlaying = false;
    s.motionFrame = std::max(0, s.motionFrame - 1);
    applyMotionFrame(st, s, s.motionFrame);
  }
  simTip("Previous frame.");
  ImGui::SameLine(0, 2.0f * S);
  if (ImGui::Button(s.motionPlaying ? "||##mt" : ">##mt", ImVec2(bW, bH))) {
    s.motionPlaying = !s.motionPlaying;
    s.motionLastTick = -1.0;
  }
  simTip(s.motionPlaying ? "Pause" : "Play");
  ImGui::SameLine(0, 2.0f * S);
  if (ImGui::Button(">>##mt", ImVec2(bW, bH))) {
    s.motionPlaying = false;
    s.motionFrame = std::min(nFrames - 1, s.motionFrame + 1);
    applyMotionFrame(st, s, s.motionFrame);
  }
  simTip("Next frame.");
  ImGui::SameLine(0, 2.0f * S);
  if (ImGui::Button(">|##mt", ImVec2(bW, bH))) {
    s.motionPlaying = false;
    s.motionFrame = nFrames - 1;
    applyMotionFrame(st, s, s.motionFrame);
  }
  simTip("Jump to last frame.");

  // Driver value label.
  float driverVal = s.motionStart;
  if (s.motionFrame < nFrames) {
    const auto &fj = s.motionTrajectory[s.motionFrame];
    if (fj.contains("value") && fj["value"].is_number())
      driverVal = (float)fj["value"].get<double>();
  }
  char info[64];
  std::snprintf(info, sizeof(info), "  %d/%d  %.1f %s", s.motionFrame + 1,
                nFrames, driverVal, s.motionDriverUnit.c_str());
  ImGui::SameLine(0, 6.0f * S);
  ImGui::TextUnformatted(info);

  // Scrub slider.
  int frame = s.motionFrame;
  ImGui::SetNextItemWidth(-FLT_MIN);
  if (ImGui::SliderInt("##motionScrub", &frame, 0, nFrames - 1, "")) {
    s.motionFrame = frame;
    s.motionPlaying = false;
    applyMotionFrame(st, s, frame);
  }
  simTip("Drag to scrub to any frame.");

  // Per-frame status badges.
  if (s.motionFrame < nFrames) {
    const auto &fj = s.motionTrajectory[s.motionFrame];
    if (fj.contains("converged") && !fj["converged"].get<bool>()) {
      ImGui::PushStyleColor(ImGuiCol_Text,
                            ImVec4(pal.statusWarn[0], pal.statusWarn[1],
                                   pal.statusWarn[2], 1.0f));
      ImGui::TextUnformatted("  ! Solver did not converge at this frame");
      ImGui::PopStyleColor();
    }
    if (fj.contains("interference") && fj["interference"].get<bool>()) {
      ImGui::PushStyleColor(
          ImGuiCol_Text,
          ImVec4(pal.statusErr[0], pal.statusErr[1], pal.statusErr[2], 1.0f));
      ImGui::TextUnformatted("  ! Interference at this frame");
      ImGui::PopStyleColor();
    }
  }
}

// ---------- drawMotionResults (called from drawResultControls)
// -------------------------

void SimManager::drawMotionResults(AppState &st, Study &s) {
  if (!s.motionHasResult) {
    ImGui::TextWrapped("Run the motion study to generate a trajectory.");
    return;
  }
  const float S = ui::uiScale;
  const int nFrames = (int)s.motionTrajectory.size();

  ImGui::TextWrapped("%s", s.summary.c_str());
  ImGui::Spacing();

  drawMotionTimeline(st, s);
  ImGui::Spacing();

  // Per-body position trace (|translation| vs frame index).
  if (!s.motionBodyNames.empty()) {
    ImGui::PushFont(st.fonts.medium);
    ImGui::TextUnformatted("Position trace");
    ImGui::PopFont();

    const int nBodies = (int)s.motionBodyNames.size();
    if (nBodies > 1) {
      ImGui::SetNextItemWidth(-FLT_MIN);
      if (ImGui::BeginCombo("##motionPlotBody",
                            s.motionPlotBody < nBodies
                                ? s.motionBodyNames[s.motionPlotBody].c_str()
                                : "(none)")) {
        for (int i = 0; i < nBodies; ++i) {
          bool sel2 = (i == s.motionPlotBody);
          if (ImGui::Selectable(s.motionBodyNames[i].c_str(), sel2))
            s.motionPlotBody = i;
          if (sel2)
            ImGui::SetItemDefaultFocus();
        }
        ImGui::EndCombo();
      }
      simTip("Body whose position magnitude to plot vs frame.");
    }

    const std::string &plotBodyName = (s.motionPlotBody < nBodies)
                                          ? s.motionBodyNames[s.motionPlotBody]
                                          : std::string();
    std::vector<float> trace;
    trace.reserve(nFrames);
    for (const auto &frame : s.motionTrajectory) {
      float mag = 0.0f;
      if (frame.contains("bodies")) {
        for (const auto &b : frame["bodies"]) {
          if (!b.contains("component") ||
              b["component"].get<std::string>() != plotBodyName)
            continue;
          if (!b.contains("transform") || b["transform"].size() < 16)
            break;
          float tx = (float)b["transform"][3].get<double>();
          float ty = (float)b["transform"][7].get<double>();
          float tz = (float)b["transform"][11].get<double>();
          mag = std::sqrt(tx * tx + ty * ty + tz * tz);
          break;
        }
      }
      trace.push_back(mag);
    }
    if (!trace.empty()) {
      const float plotH = 60.0f * S;
      ImGui::PlotLines("##motiontrace", trace.data(), (int)trace.size(), 0,
                       "|pos| (mm)", FLT_MAX, FLT_MAX, ImVec2(-FLT_MIN, plotH));
      // Vertical cursor at current frame.
      if (nFrames > 1) {
        ImVec2 rMin = ImGui::GetItemRectMin(), rMax = ImGui::GetItemRectMax();
        float xCur = rMin.x + (rMax.x - rMin.x) *
                                  ((float)s.motionFrame / (float)(nFrames - 1));
        ImGui::GetWindowDrawList()->AddLine(
            ImVec2(xCur, rMin.y), ImVec2(xCur, rMax.y),
            IM_COL32(255, 200, 80, 200), 1.5f * S);
      }
    }
  }

  // ---- cross-sim handoff: motion -> static FEA (aicad.simhandoff/1)
  // ---------------- Wired behind the sim_caps endpoint-availability check
  // (T-FIN24 honest gating): the worst-frame joint-reaction chaining lives in
  // the MCP motion_fea_handoff tool; until its worker mirror lands the button
  // stays dimmed with the doctor's guidance.
  {
    ImGui::Spacing();
    ImGui::SeparatorText("Handoff");
    const bool up = capsStatus_ == 1 && caps_.contains("handoffs") &&
                    caps_["handoffs"].contains("motion_fea") &&
                    caps_["handoffs"]["motion_fea"].value("available", false);
    if (!up)
      ImGui::BeginDisabled();
    ImGui::Button("Send reactions to static FEA", ImVec2(-FLT_MIN, 0));
    if (!up)
      ImGui::EndDisabled();
    if (!up && ImGui::IsItemHovered())
      ImGui::SetTooltip(
          "%s",
          capsStatus_ == 1 && caps_.contains("handoffs") &&
                  caps_["handoffs"].contains("motion_fea")
              ? caps_["handoffs"]["motion_fea"]
                    .value("hint",
                           std::string("motion->FEA worker seam pending"))
                    .c_str()
              : "motion->FEA handoff: run motion_fea_handoff via the copilot "
                "(worker mirror pending)");
    drawProvenance(s.advResult);
  }
}

// ============================================================ T-FIN24:
// multiphysics Simulate tab
//
// One Simulate workspace for every study kind. The pieces below add: the
// sim_caps / fw_status capability probes (honest gating), the four new
// study-kind runs (thermal FEA on the core conduction solver; CalculiX
// buckling/contact box proxies + modal-superposition frequency response via the
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
        if (r.ok && r.result.is_object() && !r.result.contains("code")) {
          caps_ = r.result;
          capsStatus_ = 1;
        } else {
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
        if (!r.ok || !r.result.is_object() || r.result.contains("code")) {
          fwStatus_ = -1;
          fwHint_ = r.err.empty() ? "fw_status unavailable" : r.err;
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
          if (r.ok && r.result.is_object() && r.result.contains("projects") &&
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

void SimManager::drawProvenance(const nlohmann::json &result) {
  if (!result.is_object() || !result.contains("simhandoff") ||
      !result["simhandoff"].is_object())
    return;
  const auto &env = result["simhandoff"];
  const std::string coupling = env.value("coupling", std::string("?"));
  std::string src = "?";
  if (env.contains("source") && env["source"].is_object())
    src = env["source"].value("domain", std::string("?")) + "/" +
          env["source"].value("tool", std::string("?"));
  pushDimText();
  ImGui::TextWrapped(
      "Provenance: %s \xe2\x80\x94 from %s (aicad.simhandoff/1, id %s)",
      coupling.c_str(), src.c_str(), env.value("id", std::string("?")).c_str());
  popDimText();
}

// ---------------------------------------------------------------- thermal FEA
// run (conduction)

void SimManager::runThermalFea(AppState &st, Study &s) {
  if (!st.simClient)
    return;
  auto *sim = st.simClient;
  if (!st.docMirror.hasDoc()) {
    st.addLog("[sim] no document to analyse");
    return;
  }

  const std::string stepPath =
      engine::daemonMode() ? std::string()
                           : (appdataAppDir() + "/cache/sim_thermal.step");
  {
    nlohmann::json r,
        p = {{"doc_id", st.docMirror.docId()}, {"path", stepPath}};
    std::string e;
    if (!st.worker.call("doc_export_step", p, r, e, 60000) ||
        r.contains("code")) {
      st.addLog("[sim] export STEP failed: " +
                (r.contains("code") ? r.value("message", e) : e));
      st.toast(ui::ToastLevel::Error, "Simulation: export failed");
      return;
    }
    lastStepPath_ = r.value("path", stepPath);
  }

  // Fixed-temperature (Dirichlet) faces.
  nlohmann::json fixtures = nlohmann::json::array();
  for (const auto &pck : s.fixtures.picks) {
    std::string e;
    auto fp = facePointsFor(st, pck.selector, e);
    if (!fp.is_null() && fp.contains("points")) {
      fp["temp_c"] = s.thermFixTempC;
      fixtures.push_back(fp);
    }
  }
  // Flux / convection loads.
  nlohmann::json loads = nlohmann::json::array();
  for (const auto &ld : s.loads) {
    nlohmann::json faces;
    for (const auto &pck : ld.faces.picks) {
      std::string e;
      auto fp = facePointsFor(st, pck.selector, e);
      if (!fp.is_null() && fp.contains("points")) {
        if (faces.is_null())
          faces = fp;
        else
          for (auto &pt : fp["points"])
            faces["points"].push_back(pt);
      }
    }
    if (faces.is_null())
      continue;
    nlohmann::json l = {{"faces", faces}};
    if (ld.thermalKind == 1) {
      l["type"] = "convection";
      l["h"] = ld.hWm2k;
      l["t_amb_c"] = ld.tAmbC;
    } else {
      l["type"] = "flux";
      l["magnitude"] = ld.fluxWm2;
    }
    loads.push_back(l);
  }
  if (fixtures.empty() && loads.empty()) {
    st.toast(ui::ToastLevel::Warn,
             "Add a thermal boundary condition before running");
    return;
  }

  // Material: conductivity from the study; density from the picked material
  // (transient mass).
  double density = 2.7;
  int mi = materialIndexByKey(s.materialKey);
  if (mi >= 0 && materials()[mi].density_g_cm3 > 0)
    density = materials()[mi].density_g_cm3;
  nlohmann::json study = {
      {"step_path", lastStepPath_},
      {"material",
       {{"k_w_mk", s.thermK},
        {"cp_j_kgk", s.thermCp},
        {"density_g_cm3", density}}},
      {"fixtures", fixtures},
      {"loads", loads},
  };
  if (s.thermTransient && s.thermTEndS > 0) {
    nlohmann::json tr = {{"t_end_s", s.thermTEndS},
                         {"t_init_c", s.thermTInitC}};
    if (s.thermDtS > 0)
      tr["dt_s"] = s.thermDtS;
    study["transient"] = tr;
  }
  addMeshParams(study, s, /*order=*/1, /*allowHex=*/false);

  std::string note;
  if (sim->launch("sim_run_thermal", {{"study", study}}, note,
                  st.docMirror.docId())) {
    clearTransientResult(s);
    clearMeshPreview();
    resultShown_ = false;
    shownStudyId_.clear();
    running_ = true;
    runningStudy_ = sel_;
    runningStudyId_ = s.id;
  }
  st.addLog(note);
}

// ---------------------------------------------------------------- ccx proxy
// runs (worker seam)

void SimManager::runBuckling(AppState &st, Study &s) {
  float lo[3], hi[3];
  if (!modelBbox(st, lo, hi)) {
    st.toast(ui::ToastLevel::Warn,
             "No model geometry to size the buckling proxy from");
    return;
  }
  int mi = materialIndexByKey(s.materialKey);
  if (mi < 0) {
    st.toast(ui::ToastLevel::Warn, "Pick a material first");
    return;
  }
  const auto &m = materials()[mi];
  // Column axis = the longest bbox extent; the two others are the section.
  float d[3] = {hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2]};
  int ax = (d[1] > d[0]) ? 1 : 0;
  if (d[2] > d[ax])
    ax = 2;
  const float L = std::max(1.0f, d[ax]);
  const float b = std::max(1.0f, d[(ax + 1) % 3]);
  const float h = std::max(1.0f, d[(ax + 2) % 3]);
  nlohmann::json params = {{"L_mm", L},          {"b_mm", b},
                           {"h_mm", h},          {"E_mpa", m.E_mpa},
                           {"nu", m.nu},         {"load_n", s.bucklingLoadN},
                           {"n_modes", s.nModes}};
  advRunning_ = true;
  s.summary = "Solving (CalculiX)\xe2\x80\xa6";
  const std::string sid = s.id;
  st.rpc.call(
      "sim_run_buckling", params,
      [this, &st, sid](const core::RpcResult &r) {
        advRunning_ = false;
        for (auto &stu : studies_) {
          if (stu.id != sid)
            continue;
          if (!r.ok || !r.result.is_object()) {
            stu.summary = "Buckling run failed: " +
                          (r.err.empty() ? "worker error" : r.err);
            st.addLog("[sim] " + stu.summary);
            return;
          }
          stu.advResult = r.result;
          if (!r.result.value("ok", false)) {
            stu.summary =
                "Buckling: " +
                r.result.value(
                    "error",
                    r.result.value("reason", std::string("solve failed")));
            st.addLog("[sim] " + stu.summary);
            return;
          }
          stu.hasResult = true;
          double l0 = 0.0;
          if (r.result.contains("load_factors") &&
              r.result["load_factors"].is_array() &&
              !r.result["load_factors"].empty())
            l0 = r.result["load_factors"][0].get<double>();
          char buf[160];
          std::snprintf(buf, sizeof(buf),
                        "lowest load factor %.3g (critical %.4g N)", l0,
                        l0 * r.result.value("applied_load_n", 0.0));
          stu.summary = buf;
          node_ = SimNode::Results;
          st.addLog("[sim] buckling done: " + stu.summary);
          return;
        }
      },
      120000);
}

void SimManager::runContact(AppState &st, Study &s) {
  float lo[3], hi[3];
  if (!modelBbox(st, lo, hi)) {
    st.toast(ui::ToastLevel::Warn,
             "No model geometry to size the contact proxy from");
    return;
  }
  int mi = materialIndexByKey(s.materialKey);
  if (mi < 0) {
    st.toast(ui::ToastLevel::Warn, "Pick a material first");
    return;
  }
  const auto &m = materials()[mi];
  nlohmann::json params = {{"L_mm", std::max(1.0f, hi[0] - lo[0])},
                           {"b_mm", std::max(1.0f, hi[1] - lo[1])},
                           {"h_mm", std::max(1.0f, hi[2] - lo[2])},
                           {"E_mpa", m.E_mpa},
                           {"nu", m.nu},
                           {"pressure_mpa", s.contactPressureMpa}};
  advRunning_ = true;
  s.summary = "Solving (CalculiX)\xe2\x80\xa6";
  const std::string sid = s.id;
  st.rpc.call(
      "sim_run_contact", params,
      [this, &st, sid](const core::RpcResult &r) {
        advRunning_ = false;
        for (auto &stu : studies_) {
          if (stu.id != sid)
            continue;
          if (!r.ok || !r.result.is_object()) {
            stu.summary = "Contact run failed: " +
                          (r.err.empty() ? "worker error" : r.err);
            st.addLog("[sim] " + stu.summary);
            return;
          }
          stu.advResult = r.result;
          if (!r.result.value("ok", false)) {
            stu.summary =
                "Contact: " +
                r.result.value(
                    "error",
                    r.result.value("reason", std::string("solve failed")));
            st.addLog("[sim] " + stu.summary);
            return;
          }
          stu.hasResult = true;
          char buf[160];
          std::snprintf(buf, sizeof(buf),
                        "applied %.4g N | base reaction %.4g N",
                        r.result.value("applied_load_n", 0.0),
                        r.result.value("base_reaction_n", 0.0));
          stu.summary = buf;
          node_ = SimNode::Results;
          st.addLog("[sim] contact done: " + stu.summary);
          return;
        }
      },
      120000);
}

// ---------------------------------------------------------------- frequency
// response

void SimManager::runFreqResponse(AppState &st, Study &s) {
  // Stage 1: the ordinary modal eigen solve on the SimClient
  // (runStaticOrModal's non- static branch launches sim_run_modal). Stage 2
  // (the modal-superposition post) fires from tick's completion handler ->
  // postFreqResponse.
  runStaticOrModal(st, s);
}

void SimManager::postFreqResponse(AppState &st, Study &s,
                                  const nlohmann::json &modalRes) {
  if (!modalRes.contains("mode_shapes") || !modalRes.contains("freqs_hz")) {
    s.summary = "Vibration: modal solve returned no mode shapes";
    st.addLog("[sim] " + s.summary);
    return;
  }
  // Part mass from the solid volume at the material density (eval_mass, same
  // source the MCP tool uses). Sync call but sub-second (pure B-rep volume
  // integral).
  double massKg = 0.0;
  int mi = materialIndexByKey(s.materialKey);
  const double dens = (mi >= 0) ? materials()[mi].density_g_cm3 : 0.0;
  if (dens > 0 && st.docMirror.hasDoc()) {
    nlohmann::json r,
        p = {{"doc_id", st.docMirror.docId()}, {"density_g_cm3", dens}};
    std::string e;
    if (st.worker.call("eval_mass", p, r, e, 15000) && !r.contains("code"))
      massKg = r.value("mass_g", 0.0) / 1000.0;
  }
  if (massKg <= 0.0) {
    s.summary = "Vibration: part mass unknown (set a material with density)";
    st.addLog("[sim] " + s.summary);
    return;
  }
  double dir[3] = {0, 0, 0};
  dir[(s.frDir < 0 || s.frDir > 2) ? 2 : s.frDir] = 1.0;
  nlohmann::json params = {
      {"freqs_hz", modalRes["freqs_hz"]},
      {"mode_shapes", modalRes["mode_shapes"]},
      {"n_nodes", modalRes.value("n_nodes", 0)},
      {"total_mass_kg", massKg},
      {"excitation_dir", {dir[0], dir[1], dir[2]}},
      {"zeta", s.frZeta},
      {"base_accel_g", s.frBaseG},
  };
  advRunning_ = true;
  s.summary = "Post-processing (modal superposition)\xe2\x80\xa6";
  const std::string sid = s.id;
  st.rpc.call(
      "sim_freq_response", params,
      [this, &st, sid](const core::RpcResult &r) {
        advRunning_ = false;
        for (auto &stu : studies_) {
          if (stu.id != sid)
            continue;
          if (!r.ok || !r.result.is_object() || !r.result.value("ok", false)) {
            stu.summary =
                "Vibration post failed: " +
                (r.ok ? r.result.value("error", std::string("worker error"))
                      : r.err);
            st.addLog("[sim] " + stu.summary);
            return;
          }
          stu.advResult = r.result;
          stu.frSweep.clear();
          stu.frAccel.clear();
          if (r.result.contains("harmonic") &&
              r.result["harmonic"].is_object()) {
            const auto &hm = r.result["harmonic"];
            if (hm.contains("sweep_hz") && hm["sweep_hz"].is_array())
              for (const auto &v : hm["sweep_hz"])
                stu.frSweep.push_back((float)v.get<double>());
            if (hm.contains("accel_g") && hm["accel_g"].is_array())
              for (const auto &v : hm["accel_g"])
                stu.frAccel.push_back((float)v.get<double>());
            char buf[160];
            std::snprintf(
                buf, sizeof(buf), "peak %.2f g @ %.1f Hz (zeta %.3g)",
                hm.value("peak_accel_g", 0.0), hm.value("peak_freq_hz", 0.0),
                r.result.value("damping_assumption", std::string()).empty()
                    ? 0.02
                    : 0.0);
            std::snprintf(buf, sizeof(buf), "peak %.2f g @ %.1f Hz",
                          hm.value("peak_accel_g", 0.0),
                          hm.value("peak_freq_hz", 0.0));
            stu.summary = buf;
          }
          stu.hasResult = true;
          node_ = SimNode::Results;
          st.addLog("[sim] frequency response done: " + stu.summary);
          return;
        }
      },
      60000);
}

// ---------------------------------------------------------------- advanced
// results block

void SimManager::drawAdvancedResults(AppState &st, Study &s) {
  (void)st;
  if (!s.hasResult) {
    if (advRunning_ || running_)
      ImGui::TextDisabled("Solving\xe2\x80\xa6");
    else
      ImGui::TextDisabled("Run the study to generate results.");
    return;
  }
  ImGui::TextWrapped("%s", s.summary.c_str());
  ImGui::Spacing();
  const auto &res = s.advResult;
  if (s.type == StudyType::Buckling && res.is_object()) {
    ImGui::SeparatorText("Buckling load factors");
    if (res.contains("load_factors") && res["load_factors"].is_array() &&
        ImGui::BeginTable("##bucktab", 3, ImGuiTableFlags_SizingStretchProp)) {
      ImGui::TableSetupColumn("Mode");
      ImGui::TableSetupColumn("Factor");
      ImGui::TableSetupColumn("Critical load (N)");
      ImGui::TableHeadersRow();
      const auto &fac = res["load_factors"];
      const double P = res.value("applied_load_n", 0.0);
      for (int i = 0; i < (int)fac.size(); ++i) {
        ImGui::TableNextRow();
        ImGui::TableSetColumnIndex(0);
        ImGui::Text("%d", i + 1);
        ImGui::TableSetColumnIndex(1);
        ImGui::Text("%.4g", fac[i].get<double>());
        ImGui::TableSetColumnIndex(2);
        ImGui::Text("%.4g", fac[i].get<double>() * P);
      }
      ImGui::EndTable();
    }
    pushDimText();
    ImGui::TextWrapped("%s", res.value("note", std::string()).c_str());
    popDimText();
  } else if (s.type == StudyType::Contact && res.is_object()) {
    ImGui::SeparatorText("Contact conservation check");
    ImGui::Text("Applied load: %.4g N", res.value("applied_load_n", 0.0));
    ImGui::Text("Base reaction: %.4g N", res.value("base_reaction_n", 0.0));
    pushDimText();
    ImGui::TextWrapped("%s", res.value("note", std::string()).c_str());
    popDimText();
  } else if (s.type == StudyType::FreqResponse && res.is_object()) {
    ImGui::SeparatorText("Harmonic response");
    if (!s.frSweep.empty() && s.frSweep.size() == s.frAccel.size())
      drawEmLinePlot("##frplot", s.frSweep, &s.frAccel, "accel (g)", nullptr,
                     nullptr, "f (Hz)", "response (g)");
    if (!s.freqs.empty()) {
      std::string f = "Modes (Hz):";
      for (float v : s.freqs) {
        char b[24];
        std::snprintf(b, sizeof(b), " %.1f", v);
        f += b;
      }
      pushDimText();
      ImGui::TextWrapped("%s", f.c_str());
      popDimText();
    }
    pushDimText();
    ImGui::TextWrapped("%s",
                       res.value("damping_assumption", std::string()).c_str());
    ImGui::TextWrapped(
        "Random (PSD) response needs a qualification-spec PSD — run "
        "run_fea_frequency_response via the copilot with psd_breakpoints.");
    popDimText();
  } else if (s.type == StudyType::ThermalFea && res.is_object()) {
    ImGui::SeparatorText(s.thermTransient ? "Transient conduction"
                                          : "Steady conduction");
    const auto sm = res.value("summary", nlohmann::json::object());
    ImGui::Text("Max temp: %.1f degC", sm.value("max_temp_c", 0.0));
    if (sm.contains("final_max_temp_c"))
      ImGui::Text("Final max: %.1f degC", sm.value("final_max_temp_c", 0.0));
    ImGui::Text("Min temp: %.1f degC", sm.value("min_temp_c", 0.0));
    if (res.contains("n_steps"))
      ImGui::Text("Time steps: %d", res.value("n_steps", 0));
    ImGui::Text("Nodes %d | elements %d", s.nNodes, s.nElems);
    pushDimText();
    ImGui::TextWrapped(
        "The temperature field colormap overlay is not packaged by the "
        "conduction solver yet — numeric summary + flux audit only.");
    popDimText();
  }
  for (const auto &n : s.solverNotes) {
    pushDimText();
    ImGui::TextWrapped("%s", n.c_str());
    popDimText();
  }
  drawProvenance(res);
}

// ---------------------------------------------------------------- electronics
// (SPICE) panel

void SimManager::openElectronicsPanel(AppState &st) {
  elPanelOpen_ = true;
  // Default to the first open Schematic tab.
  el_.docTab = -1;
  for (int i = 0; i < (int)st.docTabs.size(); ++i)
    if (st.docTabs[i].kind == AppState::DocTab::TabKind::Schematic &&
        !st.docTabs[i].ecadDocId.empty()) {
      el_.docTab = i;
      break;
    }
  st.leftPaneTab = AppState::LP_Simulation;
}

void SimManager::selectElTrace(int idx) {
  el_.probeIdx = idx;
  el_.xs.clear();
  el_.ys.clear();
  if (idx < 0 || idx >= (int)el_.probes.size())
    return;
  const std::string &pr = el_.probes[idx];
  const auto results = el_.raw.value("results", nlohmann::json::object());
  if (results.contains("tran") && results["tran"].is_object()) {
    const auto &tr = results["tran"];
    if (tr.contains("x_t") && tr["x_t"].is_array() && tr.contains("traces") &&
        tr["traces"].contains(pr)) {
      for (const auto &v : tr["x_t"])
        el_.xs.push_back((float)(v.get<double>() * 1e3));
      for (const auto &v : tr["traces"][pr])
        el_.ys.push_back((float)v.get<double>());
    }
  } else if (results.contains("ac") && results["ac"].is_object()) {
    const auto &ac = results["ac"];
    if (ac.contains("x_f") && ac["x_f"].is_array() && ac.contains("mag") &&
        ac["mag"].contains(pr)) {
      for (const auto &v : ac["x_f"])
        el_.xs.push_back((float)v.get<double>());
      for (const auto &v : ac["mag"][pr]) {
        double m = v.is_number() ? v.get<double>() : 0.0;
        el_.ys.push_back((float)(20.0 * std::log10(std::max(m, 1e-12))));
      }
    }
  }
}

void SimManager::runElectronics(AppState &st) {
  if (el_.docTab < 0 || el_.docTab >= (int)st.docTabs.size())
    return;
  const std::string docId = st.docTabs[el_.docTab].ecadDocId;
  if (docId.empty())
    return;
  nlohmann::json analyses = nlohmann::json::array();
  if (el_.analysis == 0)
    analyses.push_back({{"type", "op"}});
  else if (el_.analysis == 2)
    analyses.push_back({{"type", "ac"},
                        {"fstart", el_.acStartHz},
                        {"fstop", el_.acStopHz},
                        {"n", el_.acPoints}});
  else
    analyses.push_back({{"type", "tran"},
                        {"tstep", el_.tranStepUs * 1e-6},
                        {"tstop", el_.tranStopMs * 1e-3}});
  elRunning_ = true;
  el_.result = -1;
  el_.error.clear();
  el_.headline.clear();
  st.rpc.call(
      "ecad_simulate", {{"doc_id", docId}, {"study", {{"analyses", analyses}}}},
      [this](const core::RpcResult &r) {
        elRunning_ = false;
        if (!r.ok || !r.result.is_object() || r.result.contains("code")) {
          el_.result = 0;
          el_.error =
              r.ok ? r.result.value("message", std::string("simulation failed"))
                   : r.err;
          return;
        }
        if (!r.result.value("ok", true)) {
          el_.result = 0;
          el_.error = r.result.value("error", std::string("simulation failed"));
          return;
        }
        el_.raw = r.result;
        el_.result = 1;
        el_.headline = r.result.value("converged", false)
                           ? "Converged"
                           : "Completed (convergence warnings)";
        // Collect the probe names from the analysis payload.
        el_.probes.clear();
        const auto results =
            r.result.value("results", nlohmann::json::object());
        const char *keys[2] = {"tran", "ac"};
        for (const char *k : keys) {
          if (!results.contains(k) || !results[k].is_object())
            continue;
          const auto &blk = results[k];
          const char *tk = (std::string(k) == "tran") ? "traces" : "mag";
          if (blk.contains(tk) && blk[tk].is_object())
            for (auto it = blk[tk].begin(); it != blk[tk].end(); ++it)
              el_.probes.push_back(it.key());
        }
        selectElTrace(el_.probes.empty() ? -1 : 0);
      },
      60000);
}

void SimManager::drawElectronicsPanel(AppState &st) {
  const float S = ui::uiScale;
  IconUV iv = getIcon("cat_electronics", 20);
  if (iv.valid) {
    ImGui::Image(iv.texId, ImVec2(16.0f * S, 16.0f * S), iv.uv0, iv.uv1);
    ImGui::SameLine(0, 6.0f * S);
  }
  ImGui::PushFont(st.fonts.medium);
  ImGui::TextUnformatted("Electronics \xe2\x80\x94 circuit simulation");
  ImGui::PopFont();
  const float btnW = 40.0f * S;
  const float rightX = ImGui::GetWindowContentRegionMax().x - btnW;
  if (rightX > ImGui::GetCursorPosX())
    ImGui::SameLine(rightX);
  else
    ImGui::SameLine();
  if (ImGui::Button("OK", ImVec2(btnW, 0))) {
    elPanelOpen_ = false;
    return;
  }
  ImGui::Separator();
  ImGui::Spacing();

  // Schematic selector (SPICE runs on the schematic netlist).
  std::vector<int> schTabs;
  for (int i = 0; i < (int)st.docTabs.size(); ++i)
    if (st.docTabs[i].kind == AppState::DocTab::TabKind::Schematic &&
        !st.docTabs[i].ecadDocId.empty())
      schTabs.push_back(i);
  if (schTabs.empty()) {
    pushDimText();
    ImGui::TextWrapped(
        "No schematic open. SPICE simulation runs on a Schematic document's "
        "netlist \xe2\x80\x94 open the board's schematic tab (a PCB alone has "
        "no simulatable devices).");
    popDimText();
    return;
  }
  if (el_.docTab < 0 ||
      std::find(schTabs.begin(), schTabs.end(), el_.docTab) == schTabs.end())
    el_.docTab = schTabs[0];
  ImGui::SetNextItemWidth(-FLT_MIN);
  if (ImGui::BeginCombo("##elsch",
                        st.docTabs[el_.docTab].displayTitle().c_str())) {
    for (int i : schTabs)
      if (ImGui::Selectable(st.docTabs[i].displayTitle().c_str(),
                            el_.docTab == i))
        el_.docTab = i;
    ImGui::EndCombo();
  }
  simTip("Which open schematic to simulate.");

  static const char *kAn[3] = {"Operating point (DC)", "AC sweep", "Transient"};
  int ai = (el_.analysis == 0) ? 0 : (el_.analysis == 2) ? 1 : 2;
  ImGui::SetNextItemWidth(-FLT_MIN);
  if (ImGui::BeginCombo("##elan", kAn[ai])) {
    if (ImGui::Selectable(kAn[0], ai == 0))
      el_.analysis = 0;
    if (ImGui::Selectable(kAn[1], ai == 1))
      el_.analysis = 2;
    if (ImGui::Selectable(kAn[2], ai == 2))
      el_.analysis = 3;
    ImGui::EndCombo();
  }
  if (el_.analysis == 3) {
    ImGui::SetNextItemWidth(-FLT_MIN);
    ImGui::InputFloat("##eltstop", &el_.tranStopMs, 0, 0, "t_stop = %.2f ms");
    ImGui::SetNextItemWidth(-FLT_MIN);
    ImGui::InputFloat("##eltstep", &el_.tranStepUs, 0, 0, "t_step = %.1f us");
  } else if (el_.analysis == 2) {
    ImGui::SetNextItemWidth(-FLT_MIN);
    ImGui::InputFloat("##elfstart", &el_.acStartHz, 0, 0, "f_start = %.0f Hz");
    ImGui::SetNextItemWidth(-FLT_MIN);
    ImGui::InputFloat("##elfstop", &el_.acStopHz, 0, 0, "f_stop = %.0f Hz");
  }

  ImGui::Spacing();
  if (elRunning_) {
    ImGui::BeginDisabled();
    ImGui::Button("Running\xe2\x80\xa6", ImVec2(-FLT_MIN, 0));
    ImGui::EndDisabled();
  } else if (ImGui::Button("Run", ImVec2(-FLT_MIN, 0))) {
    runElectronics(st);
  }
  simTip("Build the SPICE netlist from the schematic and solve (async). Probes "
         "default to "
         "every node voltage + source current.");

  ImGui::Spacing();
  ImGui::Separator();
  if (el_.result == 0) {
    ImGui::TextWrapped("Failed: %s", el_.error.c_str());
  } else if (el_.result == 1) {
    ImGui::TextUnformatted(el_.headline.c_str());
    const auto results = el_.raw.value("results", nlohmann::json::object());
    if (el_.analysis == 0 && results.contains("op") &&
        results["op"].is_object()) {
      ImGui::SeparatorText("Operating point");
      for (auto it = results["op"].begin(); it != results["op"].end(); ++it)
        if (it.value().is_number())
          ImGui::Text("%s = %.6g", it.key().c_str(), it.value().get<double>());
    } else if (!el_.probes.empty()) {
      ImGui::SetNextItemWidth(-FLT_MIN);
      if (ImGui::BeginCombo("##elprobe",
                            el_.probeIdx >= 0 &&
                                    el_.probeIdx < (int)el_.probes.size()
                                ? el_.probes[el_.probeIdx].c_str()
                                : "(probe)")) {
        for (int i = 0; i < (int)el_.probes.size(); ++i)
          if (ImGui::Selectable(el_.probes[i].c_str(), el_.probeIdx == i))
            selectElTrace(i);
        ImGui::EndCombo();
      }
      if (!el_.xs.empty() && el_.xs.size() == el_.ys.size())
        drawEmLinePlot("##elplot", el_.xs, &el_.ys,
                       el_.analysis == 2 ? "mag (dB)" : "V / I", nullptr,
                       nullptr, el_.analysis == 2 ? "f (Hz)" : "t (ms)",
                       el_.analysis == 2 ? "dB" : "value");
    }
  } else {
    ImGui::TextDisabled("Run to simulate the schematic.");
  }
}

// ---------------------------------------------------------------- firmware
// emulation panel

void SimManager::openEmulationPanel(AppState &st) {
  fwPanelOpen_ = true;
  probeFwStatus(st);
  st.leftPaneTab = AppState::LP_Simulation;
}

void SimManager::runEmulation(AppState &st) {
  if (fwsim_.projIdx < 0 || fwsim_.projIdx >= (int)fwsim_.projectIds.size())
    return;
  nlohmann::json p{{"project_id", fwsim_.projectIds[fwsim_.projIdx]}};
  nlohmann::json asserts = nlohmann::json::array();
  std::string buf(fwsim_.asserts);
  size_t start = 0;
  while (start <= buf.size()) {
    size_t end = buf.find(';', start);
    std::string tok = buf.substr(
        start, end == std::string::npos ? std::string::npos : end - start);
    const size_t a = tok.find_first_not_of(" \t");
    const size_t b = tok.find_last_not_of(" \t");
    if (a != std::string::npos)
      asserts.push_back(tok.substr(a, b - a + 1));
    if (end == std::string::npos)
      break;
    start = end + 1;
  }
  if (!asserts.empty())
    p["assertions"] = asserts;
  fwRunning_ = true;
  fwsim_.result = -1;
  fwsim_.error.clear();
  fwsim_.headline.clear();
  fwsim_.uart.clear();
  st.rpc.call(
      "fw_sim_run", p,
      [this](const core::RpcResult &r) {
        fwRunning_ = false;
        if (!r.ok || !r.result.is_object() || r.result.contains("code")) {
          fwsim_.result = 0;
          fwsim_.error =
              r.ok ? r.result.value("message", std::string("emulation failed"))
                   : r.err;
          return;
        }
        fwsim_.result = r.result.value("passed", false) ? 1 : 0;
        char buf2[96];
        std::snprintf(buf2, sizeof(buf2), "%s  (%.0f ms emulated)",
                      r.result.value("passed", false) ? "PASSED" : "NOT PASSED",
                      r.result.value("ms", 0.0));
        fwsim_.headline = buf2;
        if (r.result.contains("assertion_results") &&
            r.result["assertion_results"].is_array())
          for (const auto &a : r.result["assertion_results"]) {
            if (!a.is_object())
              continue;
            std::string line = a.value("passed", false) ? "PASS  " : "FAIL  ";
            line += a.value("assertion", std::string());
            fwsim_.uart.push_back(std::move(line));
          }
        const std::string uart = r.result.value("uart_log", std::string());
        if (!uart.empty()) {
          std::istringstream is(uart);
          std::string ln;
          while (std::getline(is, ln))
            fwsim_.uart.push_back(ln);
        }
      },
      120000);
}

void SimManager::drawEmulationPanel(AppState &st) {
  const float S = ui::uiScale;
  ImGui::PushFont(st.fonts.medium);
  ImGui::TextUnformatted("Firmware \xe2\x80\x94 emulation");
  ImGui::PopFont();
  const float btnW = 40.0f * S;
  const float rightX = ImGui::GetWindowContentRegionMax().x - btnW;
  if (rightX > ImGui::GetCursorPosX())
    ImGui::SameLine(rightX);
  else
    ImGui::SameLine();
  if (ImGui::Button("OK", ImVec2(btnW, 0))) {
    fwPanelOpen_ = false;
    return;
  }
  ImGui::Separator();
  ImGui::Spacing();

  if (fwStatus_ == 0) {
    ImGui::TextDisabled("Checking fw_status\xe2\x80\xa6");
    return;
  }
  if (fwStatus_ < 0 || !fwCanEmulate_) {
    // Honest doctor state: the run leg is missing — show the guidance, no dead
    // buttons.
    pushDimText();
    ImGui::TextWrapped("%s", fwHint_.empty()
                                 ? "Emulation backend unavailable \xe2\x80\x94 "
                                   "install Renode (preferred) or "
                                   "qemu-system-arm, then Recheck."
                                 : fwHint_.c_str());
    popDimText();
    if (ImGui::SmallButton("Recheck")) {
      fwStatus_ = 0;
      probeFwStatus(st);
    }
    return;
  }
  if (!fwsim_.projectsLoaded) {
    ImGui::TextDisabled("Loading projects\xe2\x80\xa6");
    return;
  }
  if (fwsim_.projectIds.empty()) {
    pushDimText();
    ImGui::TextWrapped(
        "No firmware projects yet \xe2\x80\x94 create one in the Firmware "
        "workspace (its New Project rail), then come back.");
    popDimText();
    return;
  }
  if (fwsim_.projIdx >= (int)fwsim_.projectIds.size())
    fwsim_.projIdx = 0;
  ImGui::SetNextItemWidth(-FLT_MIN);
  if (ImGui::BeginCombo("##fwproj",
                        fwsim_.projectNames[fwsim_.projIdx].c_str())) {
    for (int i = 0; i < (int)fwsim_.projectIds.size(); ++i)
      if (ImGui::Selectable(fwsim_.projectNames[i].c_str(),
                            fwsim_.projIdx == i))
        fwsim_.projIdx = i;
    ImGui::EndCombo();
  }
  ImGui::SetNextItemWidth(-FLT_MIN);
  ImGui::InputTextWithHint(
      "##fwasserts", "assertions, ';'-separated  e.g. uart:hello; gpio:PA5=1",
      fwsim_.asserts, sizeof(fwsim_.asserts));
  simTip("Scenario assertions checked against the emulated run (UART text, "
         "GPIO levels).");
  ImGui::Spacing();
  if (fwRunning_) {
    ImGui::BeginDisabled();
    ImGui::Button("Running\xe2\x80\xa6", ImVec2(-FLT_MIN, 0));
    ImGui::EndDisabled();
  } else if (ImGui::Button("Run emulation", ImVec2(-FLT_MIN, 0))) {
    runEmulation(st);
  }
  simTip(
      "Build (if needed) and run the firmware in Renode/QEMU with the scenario "
      "assertions (async).");

  ImGui::Spacing();
  ImGui::Separator();
  if (fwsim_.result == 1)
    ImGui::TextUnformatted(fwsim_.headline.c_str());
  else if (fwsim_.result == 0 && !fwsim_.headline.empty())
    ImGui::TextUnformatted(fwsim_.headline.c_str());
  if (!fwsim_.error.empty())
    ImGui::TextWrapped("Failed: %s", fwsim_.error.c_str());
  if (!fwsim_.uart.empty()) {
    ImGui::SeparatorText("Output");
    ImGui::BeginChild("##fwuart", ImVec2(0, 160.0f * S),
                      ImGuiChildFlags_Border);
    for (const auto &l : fwsim_.uart)
      ImGui::TextUnformatted(l.c_str());
    ImGui::EndChild();
  }
  if (fwsim_.result < 0 && fwsim_.error.empty() && !fwRunning_)
    ImGui::TextDisabled("Run to emulate the selected project.");
}

// ---------------------------------------------------------------- cross-sim
// handoff panel

void SimManager::openHandoffPanel(AppState &st, int pcbTab) {
  handoffPanelOpen_ = true;
  ho_.docTab = pcbTab;
  probeCaps(st);
  st.leftPaneTab = AppState::LP_Simulation;
}

void SimManager::runEcadThermalHandoff(AppState &st) {
  if (ho_.docTab < 0 || ho_.docTab >= (int)st.docTabs.size())
    return;
  const std::string ecadId = st.docTabs[ho_.docTab].ecadDocId;
  if (ecadId.empty())
    return;
  // Fetch the full board document (ecad_tree carries the doc verbatim) — sync
  // but light (the board JSON is already resident in the worker's session).
  nlohmann::json tree, boardDoc;
  std::string e;
  if (!st.worker.call("ecad_tree", {{"doc_id", ecadId}}, tree, e, 15000) ||
      tree.contains("code")) {
    ho_.error = "ecad_tree failed: " +
                (tree.contains("code") ? tree.value("message", e) : e);
    return;
  }
  boardDoc = tree.value("tree", nlohmann::json::object());
  static const char *kEnvs[3] = {"still_air", "forced_air", "cold_plate"};
  nlohmann::json params = {
      {"board", boardDoc},
      {"doc_id", ecadId},
      {"environment",
       kEnvs[(ho_.envIdx < 0 || ho_.envIdx > 2) ? 0 : ho_.envIdx]},
      {"include_trace_heating", ho_.traceHeating},
  };
  ho_.running = true;
  ho_.error.clear();
  st.rpc.call(
      "ecad_thermal_handoff", params,
      [this, &st](const core::RpcResult &r) {
        ho_.running = false;
        if (!r.ok || !r.result.is_object()) {
          ho_.error = r.err.empty() ? "worker error" : r.err;
          return;
        }
        ho_.result = r.result;
        if (!r.result.value("ok", false))
          ho_.error = r.result.value("error", std::string("handoff failed"));
        st.addLog("[sim] ecad_thermal_handoff: " +
                  r.result.value("hint",
                                 r.result.value("error", std::string("done"))));
      },
      300000);
}

void SimManager::runHeatsinkSelect(AppState &st) {
  ho_.heatsinkRunning = true;
  st.rpc.call(
      "heatsink_select", nlohmann::json::object(),
      [this](const core::RpcResult &r) {
        ho_.heatsinkRunning = false;
        ho_.heatsink =
            r.ok ? r.result : nlohmann::json{{"ok", false}, {"error", r.err}};
      },
      300000);
}

void SimManager::drawHandoffPanel(AppState &st) {
  const float S = ui::uiScale;
  ImGui::PushFont(st.fonts.medium);
  ImGui::TextUnformatted("Board \xe2\x86\x92 thermal FEA handoff");
  ImGui::PopFont();
  const float btnW = 40.0f * S;
  const float rightX = ImGui::GetWindowContentRegionMax().x - btnW;
  if (rightX > ImGui::GetCursorPosX())
    ImGui::SameLine(rightX);
  else
    ImGui::SameLine();
  if (ImGui::Button("OK", ImVec2(btnW, 0))) {
    handoffPanelOpen_ = false;
    return;
  }
  ImGui::Separator();
  ImGui::Spacing();

  if (ho_.docTab >= 0 && ho_.docTab < (int)st.docTabs.size())
    ImGui::Text("Board: %s", st.docTabs[ho_.docTab].displayTitle().c_str());
  static const char *kEnvLabels[3] = {"Still air (h=10)", "Forced air (h=50)",
                                      "Cold plate (bottom held)"};
  int ei = (ho_.envIdx < 0 || ho_.envIdx > 2) ? 0 : ho_.envIdx;
  ImGui::SetNextItemWidth(-FLT_MIN);
  if (ImGui::BeginCombo("##hoenv", kEnvLabels[ei])) {
    for (int i = 0; i < 3; ++i)
      if (ImGui::Selectable(kEnvLabels[i], ei == i))
        ho_.envIdx = i;
    ImGui::EndCombo();
  }
  simTip(
      "Convection environment preset applied to the board/component surfaces.");
  ImGui::Checkbox("Include copper I2R trace heating", &ho_.traceHeating);
  simTip("Adds the PDN I2R map as distributed flux cells (needs a prior "
         "ecad_run_pdn).");

  ImGui::Spacing();
  const bool seamUp =
      capsStatus_ == 1 && caps_.contains("handoffs") &&
      caps_["handoffs"]["ecad_thermal"].value("available", false);
  if (capsStatus_ == 0) {
    ImGui::TextDisabled("Checking the handoff seam\xe2\x80\xa6");
  } else if (!seamUp) {
    pushDimText();
    ImGui::TextWrapped(
        "ECAD \xe2\x86\x92 thermal handoff unavailable: %s",
        capsStatus_ == 1
            ? caps_["handoffs"]["ecad_thermal"]
                  .value("hint", std::string("worker seam missing"))
                  .c_str()
            : "worker unreachable (sim_caps probe failed)");
    popDimText();
  } else if (ho_.running) {
    ImGui::BeginDisabled();
    ImGui::Button("Solving\xe2\x80\xa6", ImVec2(-FLT_MIN, 0));
    ImGui::EndDisabled();
  } else if (ImGui::Button("Send board to thermal FEA", ImVec2(-FLT_MIN, 0))) {
    runEcadThermalHandoff(st);
  }

  // T-FIN17 forward seam: the button exists behind an endpoint-availability
  // check and lights up the moment the heatsink selection loop lands in the
  // worker.
  const bool hsUp =
      capsStatus_ == 1 && caps_.contains("handoffs") &&
      caps_["handoffs"]["heatsink_select"].value("available", false);
  if (!hsUp)
    ImGui::BeginDisabled();
  if (ImGui::Button(ho_.heatsinkRunning ? "Picking\xe2\x80\xa6"
                                        : "Pick heatsink",
                    ImVec2(-FLT_MIN, 0)) &&
      hsUp && !ho_.heatsinkRunning)
    runHeatsinkSelect(st);
  if (!hsUp)
    ImGui::EndDisabled();
  if (!hsUp && ImGui::IsItemHovered())
    ImGui::SetTooltip(
        "%s",
        capsStatus_ == 1
            ? caps_["handoffs"]["heatsink_select"]
                  .value("hint",
                         std::string(
                             "heatsink selection (T-FIN17) not installed yet"))
                  .c_str()
            : "heatsink selection (T-FIN17) not installed yet");

  ImGui::Spacing();
  ImGui::Separator();
  if (!ho_.error.empty())
    ImGui::TextWrapped("Failed: %s", ho_.error.c_str());
  if (ho_.result.is_object() && ho_.result.value("ok", false)) {
    if (ho_.result.value("solved", false)) {
      ImGui::SeparatorText("Component temperatures");
      ImGui::Text("Board max: %.1f degC",
                  ho_.result.value("board_max_temp_c", 0.0));
      if (ho_.result.contains("components") &&
          ho_.result["components"].is_array() &&
          ImGui::BeginTable("##hocomp", 3, ImGuiTableFlags_SizingStretchProp)) {
        ImGui::TableSetupColumn("Ref");
        ImGui::TableSetupColumn("Tcase (C)");
        ImGui::TableSetupColumn("Tj (C)");
        ImGui::TableHeadersRow();
        for (const auto &c : ho_.result["components"]) {
          if (!c.is_object())
            continue;
          ImGui::TableNextRow();
          ImGui::TableSetColumnIndex(0);
          ImGui::TextUnformatted(c.value("ref", std::string("?")).c_str());
          ImGui::TableSetColumnIndex(1);
          if (c.contains("t_case_c") && c["t_case_c"].is_number())
            ImGui::Text("%.1f", c["t_case_c"].get<double>());
          else
            ImGui::TextDisabled("-");
          ImGui::TableSetColumnIndex(2);
          if (c.contains("tj_c") && c["tj_c"].is_number())
            ImGui::Text("%.1f", c["tj_c"].get<double>());
          else
            ImGui::TextDisabled("-");
        }
        ImGui::EndTable();
      }
    } else {
      ImGui::TextWrapped(
          "%s", ho_.result
                    .value("hint", ho_.result.value("error_solve",
                                                    std::string("not solved")))
                    .c_str());
    }
    if (ho_.result.contains("warnings") && ho_.result["warnings"].is_array()) {
      for (const auto &w : ho_.result["warnings"])
        if (w.is_string()) {
          pushDimText();
          ImGui::TextWrapped("%s", w.get<std::string>().c_str());
          popDimText();
        }
    }
    drawProvenance(ho_.result);
  }
  if (ho_.heatsink.is_object() && !ho_.heatsink.empty()) {
    ImGui::SeparatorText("Heatsink pick");
    if (ho_.heatsink.value("ok", false))
      ImGui::TextWrapped(
          "%s", ho_.heatsink.value("hint", ho_.heatsink.dump(2)).c_str());
    else
      ImGui::TextWrapped(
          "%s",
          ho_.heatsink.value("error", std::string("heatsink selection failed"))
              .c_str());
    drawProvenance(ho_.heatsink);
  }
}

} // namespace ui
