// SimManager implementation — see sim_manager.h.
#include "sim_manager.h"

#include <algorithm>
#include <array>
#include <cctype>
#include <cfloat>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <map>
#include <memory>
#include <sstream>
#include <utility>
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
#include "layout.h"    // ui::overlayColumnEdge — the study picker flies out beside the column
#include "metrics.h"
#include "property_manager.h" // PropertyManager::active() — mutual-exclusion pick guard
#include "util/aicm_handoff.h" // is_shm_uri — overlay paths are aicm-shm:// handoff URIs, not files
#include "widgets/kit.h" // kit::dimText — the app-wide dim/secondary prose renderer
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
// frosted adaptive pane. Wrapped prose goes through kit::dimText (identical
// 0.62 ink); this pair stays only for single-line row labels, which must not
// wrap inside a fixed-height row pill.
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
static ImU32 simRoleU32(const float c[4], float a = 1.0f) {
  float t[4] = {c[0], c[1], c[2], c[3] * a};
  return Palette::toU32(t);
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

static bool supportsLearnedAdvisor(StudyType t) {
  return t == StudyType::Static || t == StudyType::Modal ||
         t == StudyType::ThermalFea;
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
  ImGui::PushTextWrapPos(ImGui::GetCursorPosX() +
                         ImGui::GetContentRegionAvail().x);
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

static void addMeshParams(nlohmann::json &j, const Study &s, int meshOrder,
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

static void clearTransientResult(Study &s) {
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
}

static bool studyReady(const Study &s, std::string &blocker);

static bool finiteNumber(const nlohmann::json &j, const char *key, double &out) {
  auto it = j.find(key);
  if (it == j.end() || !it->is_number())
    return false;
  out = it->get<double>();
  return std::isfinite(out);
}

// ---- bounded live histories -------------------------------------------------------------------
// The CFD progress stream emits one drag sample per solver iteration and a long OpenFOAM run has
// no iteration ceiling, so appending blindly grows the study forever. Keep at most kDragTraceCap
// samples by uniform power-of-two thinning: a sample is kept when its ARRIVAL index is a multiple
// of the current stride, and when the buffer fills we drop every second retained sample and double
// the stride — which keeps exactly the arrival indices that are multiples of the NEW stride, so the
// series stays uniformly sampled over the whole run rather than truncated to a window. Deterministic
// (no time or randomness) and the honest arrival count is retained in dragTraceTotal.
static constexpr size_t kDragTraceCap = 2048;

static void appendDragSample(Study &s, float v) {
  const uint64_t arrival = s.dragTraceTotal++;
  if (s.dragTraceStride > 1 && (arrival % s.dragTraceStride) != 0)
    return;
  s.dragTrace.push_back(v);
  if (s.dragTrace.size() < kDragTraceCap)
    return;
  size_t w = 0;
  for (size_t r = 0; r < s.dragTrace.size(); r += 2)
    s.dragTrace[w++] = s.dragTrace[r];
  s.dragTrace.resize(w);
  s.dragTraceStride *= 2;
}

// ---- optional core-advisor envelope ------------------------------------------------------------
// Parse the shared "advisor" object off a solver result. Absence is the norm and yields a default
// (present == false) record, which every consumer treats as "no advisor" — identical to the
// pre-advisor behaviour. Every field is read defensively: a partial envelope from an older core
// still parses, with unstated numerics left as NaN.
static SimAdvisor parseAdvisor(const nlohmann::json &res) {
  SimAdvisor a;
  if (!res.is_object())
    return a;
  auto it = res.find("advisor");
  if (it == res.end() || !it->is_object())
    return a;
  const nlohmann::json &j = *it;
  a.present = true;
  auto flag = [&](const char *k, bool dflt) {
    auto f = j.find(k);
    return (f != j.end() && f->is_boolean()) ? f->get<bool>() : dflt;
  };
  auto num = [&](const char *k) {
    double v = 0.0;
    return finiteNumber(j, k, v) ? v : SimAdvisor::kUnset;
  };
  auto str = [&](const char *k) {
    auto f = j.find(k);
    return (f != j.end() && f->is_string()) ? f->get<std::string>() : std::string();
  };
  a.available = flag("available", false);
  a.applied = flag("applied", false);
  a.vetoed = flag("vetoed", false);
  a.budgetRefusal = flag("budget_refusal", false);
  a.mesher = str("mesher");
  a.hRel = num("h_rel");
  {
    auto f = j.find("order");
    if (f != j.end() && f->is_number_integer())
      a.order = f->get<int>();
  }
  {
    auto f = j.find("adapt_passes");
    if (f != j.end() && f->is_number_integer())
      a.adaptPasses = f->get<int>();
  }
  a.etaTarget = num("eta_target");
  a.predictedDof = num("predicted_dof");
  a.predictedMeshMs = num("predicted_mesh_ms");
  a.predictedSolveMs = num("predicted_solve_ms");
  a.failureProb = num("failure_prob");
  a.oodDistance = num("ood_distance");
  a.modelRevision = str("model_revision");
  a.note = str("note");
  return a;
}

// A mesher the advisor may recommend that Chudware's solve path cannot actually run. Chudware
// solves tets; a polyhedral / VEM recommendation is reported as advice that was NOT executed.
static bool advisorMesherIsPolyhedral(const std::string &mesher) {
  std::string m;
  m.reserve(mesher.size());
  for (char c : mesher)
    m.push_back((char)std::tolower((unsigned char)c));
  return m.find("poly") != std::string::npos || m.find("vem") != std::string::npos ||
         m.find("hedral") != std::string::npos;
}

// Group digits so six- and seven-figure counts read at a glance in a narrow pane.
static std::string groupedCount(uint64_t n) {
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
static size_t budgetStrideFor(size_t count, size_t budget) {
  if (count == 0 || budget == 0 || count <= budget)
    return 1;
  return (count + budget - 1) / budget;
}

// Same solid-content policy as the FeatureManager's require-non-empty guard: a loaded
// viewport body wins, otherwise assemblies or non-reference features count. This lets a
// free-free modal study reject an empty/sketch-only document before paying for an export.
static bool hasSimulationGeometry(const AppState &st) {
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

namespace {
bool simOriginActive(const AppState &st, const SimManager *manager,
                     const std::string &docId) {
  return st.simManager == manager && st.docMirror.hasDoc() &&
         st.docMirror.docId() == docId;
}

bool simDocOpen(const AppState &st, const std::string &docId) {
  return std::any_of(st.docTabs.begin(), st.docTabs.end(),
                     [&](const AppState::DocTab &tab) {
                       return tab.docId == docId;
                     });
}

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
bool simJsonBool(const nlohmann::json &value, const char *key,
                 bool fallback) {
  const auto it = value.find(key);
  return it == value.end()
             ? fallback
             : (it->is_boolean() ? it->get<bool>() : false);
}

std::string simJsonText(const nlohmann::json &value, const char *primary,
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

std::string simPathSegment(std::string value) {
  for (char &c : value) {
    const unsigned char uc = static_cast<unsigned char>(c);
    if (!std::isalnum(uc) && c != '-' && c != '_')
      c = '_';
  }
  return value.empty() ? std::string("unknown") : value;
}

std::string staticModalStepPath(const std::string &docId,
                                const std::string &studyId,
                                std::uint64_t reservationToken) {
  const std::string leaf =
      "sim_" + simPathSegment(docId) + "_" + simPathSegment(studyId) + "_" +
      std::to_string(reservationToken) + ".step";
  // Relative paths are intentionally server-owned in daemon/WASM mode.
  return engine::daemonMode() ? leaf : appdataAppDir() + "/cache/" + leaf;
}
} // namespace

struct SimManager::StaticModalPrep {
  struct Load {
    bool pressure = false;
    float vec[3] = {0.0f, 0.0f, -100.0f};
    float pressureMpa = 0.1f;
    std::vector<nlohmann::json> selectors;
    std::vector<nlohmann::json> resolved;
  };

  core::SimClient *sim = nullptr;
  std::uint64_t reservationToken = 0;
  std::string docId;
  std::string studyId;
  std::string requestedStepPath;
  std::string resolvedStepPath;
  StudyType type = StudyType::Static;
  int nModes = 6;
  nlohmann::json material = nlohmann::json::object();
  Study meshOptions;
  std::vector<nlohmann::json> fixtureSelectors;
  std::vector<nlohmann::json> fixtures;
  std::vector<Load> loads;
  std::size_t facesRemaining = 0;
};

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
                      err, 15000) ||
      !res.is_object() || res.contains("code") ||
      !res.value("ok", true)) {
    st.addLog("[sim] sim_study_list failed: " +
              (res.is_object()
                   ? res.value("message", res.value("error", err))
                   : (err.empty() ? "invalid response" : err)));
    return;
  }
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
          // Motion transient result
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
  lastRevision_ = (long)st.docMirror.revision();
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

const char *SimManager::emRunBlocker() const {
  if (emForm_ == EmForm::Driven && emStlCheckedPath_ != em_.stlPath) {
    const bool exists =
        !em_.stlPath[0] || std::filesystem::exists(em_.stlPath);
    emStlCheckedPath_ = em_.stlPath;
    emStlPathExists_ = exists;
  }

  auto positive = [](float v) { return std::isfinite(v) && v > 0.0f; };
  if (em_.stdIdx < 0 &&
      ((em_.shape == 0 && (!positive(em_.a_mm) || !positive(em_.b_mm))) ||
       (em_.shape == 1 && !positive(em_.radius_mm))))
    return "Waveguide dimensions must be finite and positive.";
  if (emForm_ != EmForm::Eigenmode && !positive(em_.lengthMm))
    return "Guide length must be finite and positive.";
  if (emForm_ == EmForm::Driven &&
      (!positive(em_.fStartGhz) || !positive(em_.fStopGhz) ||
       em_.fStopGhz <= em_.fStartGhz || em_.nFreq < 3))
    return "Driven sweep needs positive increasing frequencies and at least 3 points.";
  if (emForm_ == EmForm::TimeDomain &&
      (!positive(em_.f0Ghz) || !positive(em_.bwGhz)))
    return "Time-domain center frequency and bandwidth must be positive.";
  if (emForm_ == EmForm::Driven && em_.stlPath[0] && !emStlPathExists_)
    return "The optional geometry STL path does not exist.";
  return nullptr;
}

// Dispatch the active form's em_* RPC asynchronously (st.rpc — the callback
// lands in a later drain() on the GL thread, safe to touch em_). A single
// in-flight guard blocks re-entry.
void SimManager::runEmForm(AppState &st) {
  if (emRunning_)
    return;
  if (const char *blocker = emRunBlocker()) {
    st.toast(ui::ToastLevel::Warn, blocker);
    return;
  }
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
        bool ok = r.ok && r.result.is_object() &&
                  !r.result.contains("code") &&
                  r.result.value("ok", true);
        std::string err;
        if (!ok)
          err = r.result.is_object()
                    ? r.result.value(
                          "error",
                          r.err.empty() ? std::string("EM solve failed") : r.err)
                    : (r.err.empty() ? "EM solve returned no result" : r.err);
        auto toFinite = [](const nlohmann::json &a,
                           std::vector<float> &out) {
          out.clear();
          if (!a.is_array() || a.empty())
            return false;
          for (const auto &v : a) {
            if (!v.is_number() || !std::isfinite(v.get<double>())) {
              out.clear();
              return false;
            }
            out.push_back((float)v.get<double>());
          }
          return true;
        };
        if (which == EmForm::Eigenmode) {
          const auto modes =
              ok ? r.result.value("modes", nlohmann::json::array())
                 : nlohmann::json::array();
          if (ok && (!modes.is_array() || modes.empty())) {
            ok = false;
            err = "EM eigenmode solve returned no modes";
          }
          if (ok)
            for (const auto &mode : modes) {
              double fc = 0.0, kc = 0.0;
              if (!mode.is_object() || !finiteNumber(mode, "fc_ghz", fc) ||
                  fc <= 0.0 || !finiteNumber(mode, "kc_per_m", kc) ||
                  kc <= 0.0) {
                ok = false;
                err = "EM eigenmode solve returned an invalid mode";
                break;
              }
            }
          em_.eigResult = ok ? 1 : 0;
          em_.eigError = err;
          em_.eigModes = ok ? modes : nlohmann::json::array();
        } else if (which == EmForm::Driven) {
          if (ok) {
            const bool haveFreq =
                toFinite(r.result.value("freq_ghz", nlohmann::json::array()),
                         em_.drvFreq);
            const bool haveS11 =
                toFinite(r.result.value("s11_db", nlohmann::json::array()),
                         em_.drvS11);
            const bool haveS21 =
                toFinite(r.result.value("s21_db", nlohmann::json::array()),
                         em_.drvS21);
            ok = haveFreq && haveS11 && haveS21 &&
                 em_.drvFreq.size() == em_.drvS11.size() &&
                 em_.drvFreq.size() == em_.drvS21.size();
            if (!ok)
              err = "EM driven solve returned empty, mismatched, or non-finite traces";
          }
          em_.drvResult = ok ? 1 : 0;
          em_.drvError = err;
          if (!ok) {
            em_.drvFreq.clear();
            em_.drvS11.clear();
            em_.drvS21.clear();
          }
        } else {
          if (ok) {
            const auto ports =
                r.result.value("ports", nlohmann::json::object());
            const bool haveTime =
                toFinite(r.result.value("t_ns", nlohmann::json::array()),
                         em_.tdTime);
            const bool haveP1 =
                toFinite(ports.value("port1_v", nlohmann::json::array()),
                         em_.tdPort1);
            const bool haveP2 =
                toFinite(ports.value("port2_v", nlohmann::json::array()),
                         em_.tdPort2);
            ok = haveTime && haveP1 && haveP2 &&
                 em_.tdTime.size() == em_.tdPort1.size() &&
                 em_.tdTime.size() == em_.tdPort2.size();
            if (!ok)
              err =
                  "EM time-domain solve returned empty, mismatched, or non-finite traces";
          }
          em_.tdResult = ok ? 1 : 0;
          em_.tdError = err;
          if (!ok) {
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
  // Two-series categorical hues: the orientation-triad blue + the amber status
  // role (the palette's only theme-aware categorical hues) — see the ramp note
  // in drawFatigueGraphs.
  const ImU32 c1 = simRoleU32(ui::palette.axisZ),
              c2 = simRoleU32(ui::palette.statusWarn);

  const auto xRange = std::minmax_element(x.begin(), x.end());
  float xLo = *xRange.first, xHi = *xRange.second;
  if (xHi - xLo < 1e-9f)
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
  const char *emBlocker = emRunBlocker();
  if (emRunning_ || emBlocker) {
    if (emRunning_)
      ImGui::TextDisabled("Solving\xe2\x80\xa6");
    ImGui::BeginDisabled();
    ImGui::Button("Run", ImVec2(-FLT_MIN, 0));
    ImGui::EndDisabled();
  } else if (ImGui::Button("Run", ImVec2(-FLT_MIN, 0))) {
    runEmForm(st);
  }
  if (emBlocker)
    ImGui::TextWrapped("%s", emBlocker);
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
                                   simRoleU32(ui::palette.accent, 0.157f));
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

void SimManager::configureActiveMotion(AppState &st, float start, float stop,
                                       int steps) {
  if (sel_ < 0 || sel_ >= (int)studies_.size())
    return;
  Study &s = studies_[sel_];
  if (s.type != StudyType::Motion)
    return;
  s.motionStart = start;
  s.motionStop = stop;
  s.motionSteps = std::max(2, std::min(steps, 120));
  persist(st, s);
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
  if (!chudware::aicm_handoff::is_shm_uri(s.overlayPath)) {
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
  const bool hadGeometry = !meshPreviewSegs_.empty();
  meshPreviewSegs_.clear();
  meshPreviewStudyId_.clear();
  meshPreviewElemsTotal_ = meshPreviewElemsUsed_ = 0;
  meshPreviewEdgesTotal_ = meshPreviewEdgesShown_ = 0;
  meshPreviewCoarse_ = false;
  if (hadGeometry)
    bumpOverlayGeneration();
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
  // chudware_cpp_docs.
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
          addMeshParams(params, *sp, meshOrder,
                        supportsLearnedAdvisor(sp->type));
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
              s->meshTypeUsed =
                  res.value("mesh_type_used", std::string());
              s->meshNote = res.value("note", std::string());
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
              // Deterministic graduated edge decimation under a FIXED display
              // budget. The old code emitted nothing above 250k elements, so a
              // large study silently blanked its preview with no explanation.
              // Now every mesh yields a bounded wireframe:
              //   Tier A (<= kPreviewBoundaryElems): the exact boundary-face
              //     wireframe, unchanged from before.
              //   Tier B (larger): keep every elemStride'th element and emit
              //     that subset's element edges. Sampling breaks adjacency, so
              //     boundary extraction would return every face of every
              //     sampled element anyway — this is the same coarse wire cloud
              //     for a fraction of the work. Flagged meshPreviewCoarse_ so
              //     the UI never presents it as the true surface mesh.
              // Both tiers then pass through a uniform edge stride so the
              // emitted segment count never exceeds kPreviewSegmentBudget.
              // Every selection is index-strided: same mesh in, identical bytes
              // out, no time/randomness/frame dependence.
              meshPreviewElemsTotal_ = nT > 0 ? (size_t)nT : 0;
              if (nT > 0) {
                const size_t nP = P.size() / 3;
                auto elNode = [&](size_t e, int k) {
                  return T[e * (size_t)stride + (size_t)k].get<int>();
                };
                std::vector<std::pair<int, int>> edges;
                std::unordered_set<uint64_t> seenEdge;
                auto addEdge = [&](int a, int b) {
                  if (a == b || a < 0 || b < 0 || (size_t)a >= nP ||
                      (size_t)b >= nP)
                    return;
                  if (a > b)
                    std::swap(a, b);
                  const uint64_t k = ((uint64_t)(uint32_t)a << 32) | (uint32_t)b;
                  if (!seenEdge.insert(k).second)
                    return;
                  edges.emplace_back(a, b);
                };
                meshPreviewCoarse_ =
                    meshPreviewElemsTotal_ > kPreviewBoundaryElems;
                if (meshPreviewCoarse_) {
                  const size_t elemStride = budgetStrideFor(
                      meshPreviewElemsTotal_, kPreviewSampleElems);
                  static const int tetE[6][2] = {{0, 1}, {0, 2}, {0, 3},
                                                 {1, 2}, {1, 3}, {2, 3}};
                  static const int hexE[12][2] = {
                      {0, 1}, {1, 2}, {2, 3}, {3, 0}, {4, 5}, {5, 6},
                      {6, 7}, {7, 4}, {0, 4}, {1, 5}, {2, 6}, {3, 7}};
                  const int(*el)[2] = (stride == 8) ? hexE : tetE;
                  const int nEl = (stride == 8) ? 12 : 6;
                  size_t used = 0;
                  for (size_t e = 0; e < meshPreviewElemsTotal_;
                       e += elemStride, ++used)
                    for (int k = 0; k < nEl; ++k)
                      addEdge(elNode(e, el[k][0]), elNode(e, el[k][1]));
                  meshPreviewElemsUsed_ = used;
                } else if (stride == 8) {
                  static const int hexF[6][4] = {{0, 1, 2, 3}, {4, 5, 6, 7},
                                                 {0, 1, 5, 4}, {2, 3, 7, 6},
                                                 {0, 3, 7, 4}, {1, 2, 6, 5}};
                  std::map<std::array<int, 4>, int> faceCount;
                  for (size_t e = 0; e < meshPreviewElemsTotal_; ++e)
                    for (int f = 0; f < 6; ++f) {
                      std::array<int, 4> q{
                          elNode(e, hexF[f][0]), elNode(e, hexF[f][1]),
                          elNode(e, hexF[f][2]), elNode(e, hexF[f][3])};
                      std::sort(q.begin(), q.end());
                      faceCount[q]++;
                    }
                  for (const auto &kv : faceCount)
                    if (kv.second == 1) {
                      addEdge(kv.first[0], kv.first[1]);
                      addEdge(kv.first[1], kv.first[2]);
                      addEdge(kv.first[2], kv.first[3]);
                      addEdge(kv.first[0], kv.first[3]);
                    }
                  meshPreviewElemsUsed_ = meshPreviewElemsTotal_;
                } else {
                  static const int fv[4][3] = {
                      {0, 1, 2}, {0, 1, 3}, {0, 2, 3}, {1, 2, 3}};
                  std::map<std::array<int, 3>, int> faceCount;
                  for (size_t e = 0; e < meshPreviewElemsTotal_; ++e)
                    for (int f = 0; f < 4; ++f) {
                      std::array<int, 3> tri{elNode(e, fv[f][0]),
                                             elNode(e, fv[f][1]),
                                             elNode(e, fv[f][2])};
                      std::sort(tri.begin(), tri.end());
                      faceCount[tri]++;
                    }
                  for (const auto &kv : faceCount)
                    if (kv.second == 1) {
                      addEdge(kv.first[0], kv.first[1]);
                      addEdge(kv.first[1], kv.first[2]);
                      addEdge(kv.first[0], kv.first[2]);
                    }
                  meshPreviewElemsUsed_ = meshPreviewElemsTotal_;
                }
                meshPreviewEdgesTotal_ = edges.size();
                const size_t budgetStride =
                    budgetStrideFor(edges.size(), kPreviewSegmentBudget);
                auto pt = [&](int i, float out[3]) {
                  out[0] = (float)P[(size_t)i * 3 + 0].get<double>();
                  out[1] = (float)P[(size_t)i * 3 + 1].get<double>();
                  out[2] = (float)P[(size_t)i * 3 + 2].get<double>();
                };
                meshPreviewSegs_.reserve(
                    6 * ((edges.size() + budgetStride - 1) / budgetStride));
                for (size_t i = 0; i < edges.size(); i += budgetStride) {
                  float pa[3], pb[3];
                  pt(edges[i].first, pa);
                  pt(edges[i].second, pb);
                  meshPreviewSegs_.insert(
                      meshPreviewSegs_.end(),
                      {pa[0], pa[1], pa[2], pb[0], pb[1], pb[2]});
                  ++meshPreviewEdgesShown_;
                }
                if (!meshPreviewSegs_.empty())
                  bumpOverlayGeneration();
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
              if (meshPreviewEdgesShown_ < meshPreviewEdgesTotal_ ||
                  meshPreviewCoarse_)
                msg += " | preview " +
                       groupedCount((uint64_t)meshPreviewEdgesShown_) + " of " +
                       groupedCount((uint64_t)meshPreviewEdgesTotal_) +
                       " edges" +
                       (meshPreviewCoarse_ ? " (coarse LOD)" : " (decimated)");
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

// ---- honest presentation-budget / advisor disclosure --------------------------------------
// Both read the SELECTED study only, so a tab or study switch changes what they report without
// any extra bookkeeping. Empty string means "nothing to disclose"; callers stay silent then.

std::string SimManager::previewLodStatus() const {
  if (sel_ < 0 || sel_ >= (int)studies_.size())
    return {};
  const Study &s = studies_[sel_];
  std::string out;
  if (meshPreviewStudyId_ == s.id && meshPreviewEdgesTotal_ > 0 &&
      (meshPreviewEdgesShown_ < meshPreviewEdgesTotal_ || meshPreviewCoarse_)) {
    out = "Mesh preview: showing " +
          groupedCount((uint64_t)meshPreviewEdgesShown_) + " of " +
          groupedCount((uint64_t)meshPreviewEdgesTotal_) + " edges";
    if (meshPreviewCoarse_)
      out += " from " + groupedCount((uint64_t)meshPreviewElemsUsed_) + " of " +
             groupedCount((uint64_t)meshPreviewElemsTotal_) +
             " elements (coarse LOD)";
  }
  if (flowLinesBuiltFor_ == s.id && flowSegsTotal_ > 0 && flowSegStride_ > 1) {
    if (!out.empty())
      out += "  |  ";
    out += "Flow lines: showing " + groupedCount((uint64_t)flowSegsShown_) +
           " of " + groupedCount((uint64_t)flowSegsTotal_) + " segments (1 in " +
           std::to_string(flowSegStride_) + ")";
  }
  return out;
}

std::string SimManager::advisorStatusLine() const {
  if (sel_ < 0 || sel_ >= (int)studies_.size())
    return {};
  const Study &s = studies_[sel_];
  const SimAdvisor &a = s.advisor;
  if (!a.present)
    return {};
  std::string out = "Advisor: ";
  if (a.budgetRefusal)
    out += "refused (over budget)";
  else if (a.vetoed)
    out += "vetoed";
  else if (a.applied)
    out += "applied";
  else if (a.available)
    out += "available, not applied";
  else
    out += "unavailable";
  // Chudware's solve path is fixed-stride tet-only, so a hybrid/VEM family
  // recommendation is reported as advice rather than claimed as executed.
  if (!a.mesher.empty() && advisorMesherIsPolyhedral(a.mesher))
    out += " | " + a.mesher + " mesher advice NOT executed (tet-only solve)";
  if (SimAdvisor::has(a.oodDistance) && a.oodDistance > 1.0)
    out += " | out of distribution";
  return out;
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
  bool hadGeometry = false;
  for (int b = 0; b < kFlowBands; ++b) {
    hadGeometry = hadGeometry || !flowStreamSegs_[b].empty() ||
                  !flowSliceSegs_[b].empty();
    flowStreamSegs_[b].clear();
    flowSliceSegs_[b].clear();
  }
  flowLinesBuiltFor_.clear();
  flowSegsTotal_ = flowSegsShown_ = 0;
  flowSegStride_ = 1;
  if (hadGeometry)
    bumpOverlayGeneration();
  if (s.overlayPath.empty())
    return;
  // Overlay is normally an aicm-shm:// URI (see applyResultField) —
  // filesystem::exists() would wrongly reject it and drop every streamline.
  // Precheck existence only for real file paths.
  if (!chudware::aicm_handoff::is_shm_uri(s.overlayPath)) {
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
  // BOUNDED AT INGESTION. A fine OpenFOAM overlay can carry millions of
  // streamline/slice segments and every one of them is redrawn each frame, so
  // the cached bands are capped at kFlowSegmentBudget. Count the candidates
  // first, then keep every flowSegStride_'th segment in traversal order — a
  // uniform thinning of the whole field (not a truncation of the first N
  // lines), deterministic for a given overlay. Direction barbs cost two extra
  // segments each, so the line budget reserves room for them up front and the
  // emitted total still lands under kFlowSegmentBudget.
  auto forEachCandidate = [&](const auto &ls, auto &&fn) {
    size_t p = 0;
    for (uint32_t li = 0; li < ls.pts_per_line.size(); ++li) {
      const uint32_t cnt = ls.pts_per_line[li];
      for (uint32_t i = 0; i + 1 < cnt; ++i) {
        const size_t a = p + i, c = p + i + 1;
        if ((c * 3 + 2) >= ls.pts.size())
          break;
        fn(a, c, i);
      }
      p += cnt;
    }
  };
  for (const auto &ls : doc.lines) {
    if (ls.name != "slices" && ls.name != "streamlines")
      continue;
    forEachCandidate(ls, [&](size_t, size_t, uint32_t) { ++flowSegsTotal_; });
  }
  const size_t barbEvery = 6;  // one direction barb every N kept stream segments
  const size_t lineBudget =
      kFlowSegmentBudget * barbEvery / (barbEvery + 2);
  flowSegStride_ = (uint32_t)budgetStrideFor(flowSegsTotal_, lineBudget);
  const float barbLen = 0.018f * mdiag; // barb leg length
  size_t candidate = 0;                 // traversal index across the whole overlay
  for (const auto &ls : doc.lines) {
    const bool isSlice = (ls.name == "slices");
    const bool isStream = (ls.name == "streamlines");
    if (!isSlice && !isStream)
      continue;
    std::vector<float> *dst = isSlice ? flowSliceSegs_ : flowStreamSegs_;
    size_t keptInLine = 0;  // kept segments within the current polyline
    forEachCandidate(ls, [&](size_t a, size_t c, uint32_t i) {
      if (i == 0)
        keptInLine = 0;
      const bool keep = (candidate++ % flowSegStride_) == 0;
      if (!keep)
        return;
      float spd = 0.0f;
      if (ls.has_scalar && c < ls.scalar.size())
        spd = 0.5f * (ls.scalar[a] + ls.scalar[c]);
      const int bi = bandOf(spd);
      const float *pa = &ls.pts[a * 3];
      const float *pc = &ls.pts[c * 3];
      dst[bi].insert(dst[bi].end(),
                     {pa[0], pa[1], pa[2], pc[0], pc[1], pc[2]});
      ++flowSegsShown_;
      // Direction barb on every barbEvery'th KEPT streamline segment. At full
      // resolution (stride 1) this reproduces the previous cadence exactly.
      // Barbs are a decorative direction cue, not field data, so they are drawn
      // and paid for out of the budget but never counted as shown segments —
      // flowSegsShown_ must stay a true 1-in-stride sample of flowSegsTotal_.
      const size_t slot = keptInLine++;
      if (isStream && (slot % barbEvery) == (barbEvery / 2)) {
        float d[3] = {pc[0] - pa[0], pc[1] - pa[1], pc[2] - pa[2]};
        const float dl = std::sqrt(d[0] * d[0] + d[1] * d[1] + d[2] * d[2]);
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
    });
  }
  flowLinesBuiltFor_ = s.id;
  if (flowSegsShown_ > 0)
    bumpOverlayGeneration();
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
  auto block = [&](const std::string &why) {
    st.addLog("[sim] run blocked: " + why);
    st.toast(ui::ToastLevel::Warn, why);
  };
  if (!st.docMirror.hasDoc()) {
    block("Open a part or assembly before running a study.");
    return;
  }
  if (!hasSimulationGeometry(st)) {
    block("The active document has no solid geometry to simulate.");
    return;
  }
  if (s.type == StudyType::Motion &&
      (st.activeTab < 0 || st.activeTab >= (int)st.docTabs.size() ||
       st.docTabs[st.activeTab].kind != AppState::DocTab::TabKind::Assembly)) {
    block("Motion studies require the active document to be an assembly.");
    return;
  }
  std::string readiness;
  if (!studyReady(s, readiness)) {
    block(readiness);
    return;
  }
#ifdef __EMSCRIPTEN__
  if (isFlowFamily(s.type) || s.type == StudyType::Buckling ||
      s.type == StudyType::Contact || s.type == StudyType::FreqResponse) {
    block("not available in the web version yet");
    return;
  }
#endif
  if ((isFlowFamily(s.type) && s.flowSolver == 1) ||
      s.type == StudyType::Thermal) {
    const nlohmann::json flowCaps =
        st.simClient ? st.simClient->caps() : nlohmann::json::object();
    if (!flowCaps.value("openfoam", false)) {
      block("OpenFOAM is not routed by this desktop simulation backend. Use the LBM Flow solver; Thermal CFD is unavailable.");
      return;
    }
  }
  if (s.type == StudyType::Buckling || s.type == StudyType::Contact ||
      s.type == StudyType::FreqResponse) {
    if (capsStatus_ == 0) {
      probeCaps(st);
      block("Checking simulation capabilities; run again when the capability check completes.");
      return;
    }
    if (capsStatus_ < 0) {
      block("The advanced simulation capability probe failed; the worker route is unavailable.");
      return;
    }
    if ((s.type == StudyType::Buckling || s.type == StudyType::Contact) &&
        (!caps_.contains("ccx") || !caps_["ccx"].is_object() ||
         !caps_["ccx"].value("found", false))) {
      block(caps_.contains("ccx") && caps_["ccx"].is_object()
                ? caps_["ccx"].value(
                      "hint", std::string("CalculiX is unavailable."))
                : std::string("CalculiX is unavailable."));
      return;
    }
    if (s.type == StudyType::FreqResponse &&
        (!caps_.contains("freq_response") ||
         !caps_["freq_response"].is_object() ||
         !caps_["freq_response"].value("available", false))) {
      block(caps_.contains("freq_response") &&
                    caps_["freq_response"].is_object()
                ? caps_["freq_response"].value(
                      "hint",
                      std::string(
                          "Vibration post-processing is unavailable."))
                : std::string(
                      "Vibration post-processing is unavailable."));
      return;
    }
  }
  if (s.type != StudyType::Motion && s.type != StudyType::Buckling &&
      s.type != StudyType::Contact && !st.simClient) {
    block("The simulation backend is unavailable.");
    return;
  }
  if ((st.simClient && st.simClient->busy()) || advRunning_ || prepInFlight_) {
    block("A simulation is already in progress.");
    return;
  }
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
        !r.is_object() || r.contains("code") || !r.value("ok", true)) {
      st.addLog("[sim] export STEP failed: " +
                (r.is_object()
                     ? r.value("message", r.value("error", e))
                     : e));
      st.toast(ui::ToastLevel::Error, "Simulation: export failed");
      return;
    }
    lastStepPath_ = r.value("path", stepPath);
  }

  nlohmann::json fixtures = nlohmann::json::array();
  for (const auto &p : s.fixtures.picks) {
    std::string e;
    auto fp = facePointsFor(st, p.selector, e);
    if (!fp.is_null() && fp.contains("points") && fp["points"].is_array() &&
        !fp["points"].empty())
      fixtures.push_back(fp);
  }
  if (fixtures.size() != s.fixtures.picks.size()) {
    st.toast(ui::ToastLevel::Warn,
             "One or more fixture faces no longer resolve; reselect them");
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
    int resolvedFaces = 0;
    for (const auto &p : ld.faces.picks) {
      std::string e;
      auto fp = facePointsFor(st, p.selector, e);
      if (!fp.is_null() && fp.contains("points") && fp["points"].is_array() &&
          !fp["points"].empty()) {
        ++resolvedFaces;
        if (faces.empty())
          faces = fp;
        else
          for (auto &pt : fp["points"])
            faces["points"].push_back(pt);
      }
    }
    if (resolvedFaces != (int)ld.faces.picks.size()) {
      st.toast(ui::ToastLevel::Warn,
               "One or more load faces no longer resolve; reselect them");
      return;
    }
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
    persist(st, s);
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
  if (!st.docMirror.hasDoc()) {
    st.addLog("[sim] no document to analyse");
    return;
  }

  const std::string docId = st.docMirror.docId();
  lastDocId_ = docId;
  std::uint64_t reservationToken = 0;
  if (prepInFlight_ ||
      !st.simClient->tryReserve(docId, reservationToken)) {
    st.toast(ui::ToastLevel::Info, "A simulation is already in progress");
    return;
  }

  auto prep = std::make_shared<StaticModalPrep>();
  prep->sim = st.simClient;
  prep->reservationToken = reservationToken;
  prep->docId = docId;
  prep->studyId = s.id;
  prep->type = s.type;
  prep->nModes = s.nModes;
  prep->requestedStepPath =
      staticModalStepPath(docId, s.id, reservationToken);
  prep->meshOptions.meshFineness = s.meshFineness;
  prep->meshOptions.meshTargetMm = s.meshTargetMm;
  prep->meshOptions.meshMinMm = s.meshMinMm;
  prep->meshOptions.learnedAdvisor = s.learnedAdvisor;
  prep->meshOptions.advisorMaxDof = s.advisorMaxDof;
  prep->meshOptions.adaptEnabled = s.adaptEnabled;
  prep->meshOptions.adaptMaxPasses = s.adaptMaxPasses;
  prep->meshOptions.adaptEtaTarget = s.adaptEtaTarget;
  prep->meshOptions.meshOrder = s.meshOrder;

  for (const auto &pick : s.fixtures.picks)
    prep->fixtureSelectors.push_back(pick.selector);
  prep->fixtures.resize(prep->fixtureSelectors.size());
  if (s.type == StudyType::Static) {
    prep->loads.reserve(s.loads.size());
    for (const auto &load : s.loads) {
      StaticModalPrep::Load snap;
      snap.pressure = load.pressure;
      snap.vec[0] = load.vec[0];
      snap.vec[1] = load.vec[1];
      snap.vec[2] = load.vec[2];
      snap.pressureMpa = load.pressureMpa;
      for (const auto &pick : load.faces.picks)
        snap.selectors.push_back(pick.selector);
      snap.resolved.resize(snap.selectors.size());
      prep->loads.push_back(std::move(snap));
    }
  }

  const int materialIndex = materialIndexByKey(s.materialKey);
  if (materialIndex >= 0) {
    const auto &material = materials()[materialIndex];
    prep->material = {{"E_mpa", material.E_mpa},
                      {"nu", material.nu},
                      {"density_g_cm3", material.density_g_cm3},
                      {"yield_mpa", material.yield_mpa}};
  }

  staticModalPrep_ = prep;
  prepInFlight_ = true;
  clearTransientResult(s);
  persist(st, docId, s);
  clearMeshPreview();
  resultShown_ = false;
  shownStudyId_.clear();
  running_ = true;
  runningStudy_ = sel_;
  runningStudyId_ = s.id;
  st.status = "Sim: preparing\xe2\x80\xa6";
  st.statusKind = 0;

  st.rpc.call(
      "doc_export_step",
      {{"doc_id", docId}, {"path", prep->requestedStepPath}},
      [this, &st, prep](const core::RpcResult &result) {
        if (staticModalPrep_ != prep || !prepInFlight_)
          return;
        if (!simDocOpen(st, prep->docId)) {
          failStaticModalPrep(st, prep,
                              "Simulation cancelled: source document was closed");
          return;
        }
        if (!result.ok || !result.result.is_object() ||
            result.result.contains("code") ||
            !simJsonBool(result.result, "ok", true)) {
          failStaticModalPrep(st, prep, "Simulation: export failed");
          return;
        }
        const auto pathIt = result.result.find("path");
        if (pathIt != result.result.end() && !pathIt->is_string()) {
          failStaticModalPrep(st, prep,
                              "Simulation: export returned an invalid STEP path");
          return;
        }
        prep->resolvedStepPath =
            pathIt == result.result.end()
                ? prep->requestedStepPath
                : pathIt->get<std::string>();
        if (prep->resolvedStepPath.empty()) {
          failStaticModalPrep(st, prep,
                              "Simulation: export returned no STEP path");
          return;
        }
        lastStepPath_ = prep->resolvedStepPath;

        prep->facesRemaining = prep->fixtureSelectors.size();
        for (const auto &load : prep->loads)
          prep->facesRemaining += load.selectors.size();
        if (prep->facesRemaining == 0) {
          finishStaticModalPrep(st, prep);
          return;
        }

        auto queueFace = [this, &st, prep](const nlohmann::json &selector,
                                           nlohmann::json *destination) {
          nlohmann::json clean = selector;
          if (clean.is_object())
            clean.erase("_pick");
          st.rpc.call(
              "doc_face_points",
              {{"doc_id", prep->docId}, {"selector", std::move(clean)}},
              [this, &st, prep, destination](const core::RpcResult &faceResult) {
                prepFaceResult(st, prep, faceResult, *destination);
              },
              30000);
        };
        for (std::size_t i = 0; i < prep->fixtureSelectors.size(); ++i)
          queueFace(prep->fixtureSelectors[i], &prep->fixtures[i]);
        for (auto &load : prep->loads)
          for (std::size_t i = 0; i < load.selectors.size(); ++i)
            queueFace(load.selectors[i], &load.resolved[i]);
      },
      60000);
}

void SimManager::failStaticModalPrep(
    AppState &st, const std::shared_ptr<StaticModalPrep> &prep,
    const std::string &message) {
  if (staticModalPrep_ != prep)
    return;
  if (prep->reservationToken != 0) {
    prep->sim->releaseReservation(prep->reservationToken);
    prep->reservationToken = 0;
  }
  prepInFlight_ = false;
  running_ = false;
  runningStudy_ = -1;
  runningStudyId_.clear();
  for (auto &study : studies_) {
    if (study.id != prep->studyId)
      continue;
    study.hasResult = false;
    study.summary = message;
    persist(st, prep->docId, study);
    break;
  }
  st.addLog("[sim] " + message);
  if (simOriginActive(st, this, prep->docId)) {
    st.status = message;
    st.statusKind = 2;
    st.toast(ui::ToastLevel::Error, message);
  }
  staticModalPrep_.reset();
}

bool SimManager::prepFaceResult(
    AppState &st, const std::shared_ptr<StaticModalPrep> &prep,
    const core::RpcResult &result, nlohmann::json &destination) {
  if (staticModalPrep_ != prep || !prepInFlight_)
    return false;
  if (!result.ok || !result.result.is_object() ||
      result.result.contains("code") ||
      !simJsonBool(result.result, "ok", true) ||
      !result.result.contains("points") ||
      !result.result["points"].is_array() ||
      result.result["points"].empty()) {
    const std::string detail =
        result.result.is_object()
            ? simJsonText(result.result, "message", "error",
                          "face no longer resolves")
            : (result.err.empty() ? std::string("face no longer resolves")
                                  : result.err);
    failStaticModalPrep(
        st, prep,
        "One or more selected faces no longer resolve; reselect them (" +
            detail + ")");
    return false;
  }
  destination = result.result;
  if (--prep->facesRemaining == 0)
    finishStaticModalPrep(st, prep);
  return true;
}

void SimManager::finishStaticModalPrep(
    AppState &st, const std::shared_ptr<StaticModalPrep> &prep) {
  if (staticModalPrep_ != prep || !prepInFlight_)
    return;
  if (!simDocOpen(st, prep->docId)) {
    failStaticModalPrep(st, prep,
                        "Simulation cancelled: source document was closed");
    return;
  }
  if (prep->type == StudyType::Static && prep->fixtures.empty()) {
    failStaticModalPrep(st, prep, "Add a fixture before running");
    return;
  }

  nlohmann::json fixtures = nlohmann::json::array();
  for (const auto &face : prep->fixtures)
    fixtures.push_back(face);
  nlohmann::json study = {{"step_path", prep->resolvedStepPath},
                          {"material", prep->material},
                          {"fixtures", std::move(fixtures)}};
  addMeshParams(study, prep->meshOptions,
                prep->type == StudyType::Static
                    ? prep->meshOptions.meshOrder
                    : 1,
                supportsLearnedAdvisor(prep->type));

  std::string note;
  bool launched = false;
  const std::uint64_t token = prep->reservationToken;
  if (prep->type == StudyType::Static) {
    nlohmann::json loads = nlohmann::json::array();
    for (const auto &load : prep->loads) {
      nlohmann::json points = nlohmann::json::array();
      for (const auto &face : load.resolved) {
        if (points.empty())
          points = face;
        else
          for (const auto &point : face["points"])
            points["points"].push_back(point);
      }
      nlohmann::json item = {{"faces", std::move(points)}};
      if (load.pressure) {
        item["type"] = "pressure";
        item["magnitude"] = load.pressureMpa;
      } else {
        item["type"] = "force";
        item["vector"] = {load.vec[0], load.vec[1], load.vec[2]};
      }
      loads.push_back(std::move(item));
    }
    study["loads"] = std::move(loads);
    const char *method =
        prep->meshOptions.adaptEnabled ? "sim_run_adaptive" : "sim_run_static";
    launched = prep->sim->launch(method, {{"study", study}}, note, prep->docId,
                                 token);
  } else {
    launched = prep->sim->launch(
        "sim_run_modal", {{"study", study}, {"n_modes", prep->nModes}}, note,
        prep->docId, token);
  }
  // A matching launch consumes the reservation on both success and failure.
  prep->reservationToken = 0;
  if (!launched) {
    failStaticModalPrep(
        st, prep,
        note.empty() ? std::string("Simulation failed to start") : note);
    return;
  }

  prepInFlight_ = false;
  staticModalPrep_.reset();
  st.addLog(note.empty() ? "[sim] simulation started" : note);
  if (simOriginActive(st, this, prep->docId))
    st.toast(ui::ToastLevel::Info,
             note.empty() ? "Simulation started" : note);
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
                                  ? std::string("sim_flow.stl")
                                  : (appdataAppDir() + "/cache/sim_flow.stl");
  nlohmann::json r, p = {{"doc_id", st.docMirror.docId()}, {"path", stlPath}};
  std::string e;
  if (!st.worker.call("doc_export_step", p, r, e, 60000) ||
      !r.is_object() || r.contains("code") || !r.value("ok", true)) {
    st.addLog("[sim] export STL failed: " +
              (r.is_object() ? r.value("message", r.value("error", e)) : e));
    st.toast(ui::ToastLevel::Error, "Simulation: STL export failed");
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
    persist(st, s);
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
  // Results is shown again. Keep the per-document motion base cache: a DocSession owns this
  // SimManager, and dropping it here made a later scrub upload an empty assembly.
  resultShown_ = false;
  shownStudyId_.clear();
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
  // don't use the overlay path. Re-apply the current frame on a real view transition because
  // leaving Results restored the editable base mesh while the per-document playback cache stayed.
  const bool motionActive = valid && studies_[sel_].type == StudyType::Motion &&
                            studies_[sel_].motionHasResult && onSimWs &&
                            node_ == SimNode::Results;
  if (motionActive) {
    Study &motion = studies_[sel_];
    if (!resultShown_ || shownStudyId_ != motion.id) {
      applyMotionFrame(st, motion, motion.motionFrame);
      motion.motionLastTick = -1.0;
    }
    resultShown_ = true;
    shownStudyId_ = motion.id;
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
  drainSimClientJob(st);
}

void SimManager::drainSimClientJob(AppState &st) {
  if (!st.simClient)
    return;
  auto *sim = st.simClient;
  auto &job = sim->job();

  // The frame loop ticks only the active manager. Route a shared-client job to
  // the manager owned by its stable document id so tab switches cannot strand
  // progress or a ready completion.
  const bool owns =
      (job.ownerDocId.empty() && running_) ||
      (!job.ownerDocId.empty() && job.ownerDocId == lastDocId_);
  if (!owns) {
    if (!job.ownerDocId.empty()) {
      for (auto &tab : st.docTabs) {
        if (tab.docId != job.ownerDocId || !tab.session ||
            !tab.session->sim || tab.session->sim.get() == this)
          continue;
        tab.session->sim->drainSimClientJob(st);
        return;
      }
    }
    // Keep a completed result parked while its owning tab still exists, but do
    // not let a closed document's orphaned completion block every future run.
    if (job.ready.load()) {
      bool ownerStillOpen = false;
      for (const auto &tab : st.docTabs)
        ownerStillOpen = ownerStillOpen || tab.docId == job.ownerDocId;
      if (!ownerStillOpen) {
        job.ready = false;
        running_ = false;
        runningStudy_ = -1;
        runningStudyId_.clear();
        st.addLog("[sim] discarded completion for a closed document");
      }
    }
    return;
  }
  const bool uiActive =
      job.ownerDocId.empty()
          ? st.simManager == this
          : simOriginActive(st, this, job.ownerDocId);

  {
    std::vector<core::SimProgress> evs;
    {
      std::lock_guard<std::mutex> lk(job.evMtx);
      if (!job.events.empty())
        evs.swap(job.events);
    }
    for (auto &ev : evs) {
      if (uiActive && !ev.text.empty())
        st.status = "Sim: " + ev.text +
                    (ev.pct ? " (" + std::to_string(ev.pct) + "%)" : "");
      if (uiActive)
        st.statusKind = sim->cancelRequested() ? 1 : 0;
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
          appendDragSample(studies_[traceStudy], (float)ev.traceDrag);
      }
    }
  }

  if (job.ready.exchange(false)) {
    running_ = false;
    std::lock_guard<std::mutex> lk(job.mtx);
    const std::string ownerDocId =
        job.ownerDocId.empty() ? lastDocId_ : job.ownerDocId;
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
    runningStudy_ = -1;
    if (si < 0 || si >= (int)studies_.size())
      return;
    Study &s = studies_[si];
    if (!job.ok) {
      if (job.cancelled) {
        s.summary = "Run cancelled";
        if (uiActive) {
          st.status = s.summary;
          st.statusKind = 1;
        }
      } else {
        s.summary = "Run failed: " + job.error;
        if (uiActive) {
          st.status = s.summary;
          st.statusKind = 2;
        }
      }
      st.addLog("[sim] " + s.summary);
      persist(st, ownerDocId, s);
      return;
    }
    const auto &res = job.result;
    s.hasResult = false;
    auto rejectResult = [&](const std::string &why) {
      s.summary = "Run failed: " + why;
      if (uiActive) {
        st.status = s.summary;
        st.statusKind = 2;
      }
      st.addLog("[sim] " + s.summary);
      persist(st, ownerDocId, s);
    };
    clearMeshPreview(); // a fresh result supersedes any setup-time mesh preview
    // Echo whatever the solver reported it did (pressure area, fall-backs, …)
    // for transparency.
    s.solverNotes.clear();
    if (res.contains("solver_notes") && res["solver_notes"].is_array())
      for (const auto &n : res["solver_notes"])
        if (n.is_string())
          s.solverNotes.push_back(n.get<std::string>());
    // Optional shared core-advisor envelope. Absent for every solver route that
    // does not carry one, which leaves s.advisor default-constructed and the
    // whole advisor UI silent — the pre-advisor behaviour, unchanged.
    s.advisor = parseAdvisor(res);
    if (isFlowFamily(s.type)) {
      const bool isThermal =
          res.contains("scalar_fields") && res["scalar_fields"].is_array() &&
          std::find(res["scalar_fields"].begin(), res["scalar_fields"].end(),
                    "temperature") != res["scalar_fields"].end();
      double drag = 0.0, lift = 0.0, side = 0.0, cd = 0.0, cl = 0.0,
             re = 0.0, area = 0.0, cpMin = 0.0, cpMax = 0.0, vmax = 0.0;
      const bool validCommon =
          finiteNumber(res, "reynolds", re) && re > 0.0 &&
          finiteNumber(res, "frontal_area_mm2", area) && area > 0.0 &&
          finiteNumber(res, "cp_min", cpMin) &&
          finiteNumber(res, "cp_max", cpMax) && cpMax >= cpMin &&
          finiteNumber(res, "vmax_mps", vmax) && vmax >= 0.0;
      const bool validForces =
          isThermal || (finiteNumber(res, "drag_n", drag) &&
                        finiteNumber(res, "lift_n", lift) &&
                        finiteNumber(res, "side_n", side) &&
                        finiteNumber(res, "cd", cd) &&
                        finiteNumber(res, "cl", cl));
      const bool diverged =
          res.contains("diverged") && res["diverged"].is_boolean() &&
          res["diverged"].get<bool>();
      const bool converged =
          res.contains("converged") && res["converged"].is_boolean() &&
          res["converged"].get<bool>();
      if (!validCommon || !validForces || diverged || !converged) {
        rejectResult(diverged
                         ? "flow solver reported divergence"
                         : (!converged
                                ? "flow solver did not converge"
                                : "flow solver returned incomplete/non-finite fields"));
        return;
      }
      s.dragN = drag;
      s.liftN = lift;
      s.sideN = side;
      s.cd = cd;
      s.cl = cl;
      s.ld = (std::fabs(cd) > 1e-12) ? cl / cd : 0.0;
      s.reynolds = re;
      s.frontalMm2 = area;
      s.refAreaMm2 = area;
      double refArea = 0.0;
      if (finiteNumber(res, "ref_area_mm2", refArea) && refArea > 0.0)
        s.refAreaMm2 = refArea;
      s.cpMin = cpMin;
      s.cpMax = cpMax;
      s.vmaxMps = vmax;
      s.flowFields.clear();
      if (res.contains("scalar_fields") && res["scalar_fields"].is_array())
        for (const auto &f : res["scalar_fields"])
          if (f.is_string())
            s.flowFields.push_back(f.get<std::string>());
      // Thermal studies don't report drag; show the temperature/heat headline
      // from the banner.
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
      s.banner = res.contains("reynolds_note") &&
                         res["reynolds_note"].is_string()
                     ? res["reynolds_note"].get<std::string>()
                     : std::string();
      if (res.contains("warning") && res["warning"].is_string())
        s.solverNotes.push_back(res["warning"].get<std::string>());
      s.overlayPath.clear();
      if (res.contains("overlay_aicm") && res["overlay_aicm"].is_string())
        s.overlayPath = res["overlay_aicm"].get<std::string>();
      else if (res.contains("lines_aicm") && res["lines_aicm"].is_string())
        s.overlayPath = res["lines_aicm"].get<std::string>();
      if (s.overlayPath.empty()) {
        s.solverNotes.push_back(
            "This solver route returns numeric CFD results only; no pressure "
            "overlay or streamlines are available.");
        if (uiActive)
          restoreModelMesh(st);
        resultShown_ = false;
        shownStudyId_.clear();
      } else if (uiActive) {
        st.activeScalarField = isThermal ? "temperature" : "pressure_cp";
        applyResultField(st, s);
        resultShown_ = true;
        shownStudyId_ = s.id;
        rebuildFlowLines(st, s);
      } else {
        resultShown_ = false;
        shownStudyId_.clear();
      }
      s.hasResult = true;
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
      double maxTemp = 0.0, minTemp = 0.0;
      const bool transient =
          res.contains("transient") && res["transient"].is_boolean() &&
          res["transient"].get<bool>();
      double maxTime = 0.0, finalMax = 0.0;
      if (s.nNodes <= 0 || s.nElems <= 0 ||
          !finiteNumber(s.resultSummary, "max_temp_c", maxTemp) ||
          !finiteNumber(s.resultSummary, "min_temp_c", minTemp) ||
          (transient &&
           (!finiteNumber(s.resultSummary, "max_temp_time_s", maxTime) ||
            !finiteNumber(s.resultSummary, "final_max_temp_c", finalMax)))) {
        rejectResult(
            "thermal solver returned no finite temperature solution");
        return;
      }
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
      char buf[256];
      if (transient)
        std::snprintf(
            buf, sizeof(buf),
            "peak %.1f degC @ %.1fs | final max %.1f degC | min %.1f degC",
            maxTemp, maxTime, finalMax, minTemp);
      else
        std::snprintf(buf, sizeof(buf), "max %.1f degC | min %.1f degC",
                      maxTemp, minTemp);
      s.summary = buf;
      s.hasResult = true;
      st.addLog("[sim] thermal FEA done: " + s.summary);
    } else if (s.type == StudyType::FreqResponse) {
      // Stage 1 (modal eigen solve) landed — hand the mode shapes to the
      // worker's vibration post-processor (modal superposition; async, panel
      // stays live).
      s.nNodes = res.value("n_nodes", 0);
      s.nDof = res.value("n_dof", 0);
      s.freqs.clear();
      if (res.contains("freqs_hz") && res["freqs_hz"].is_array())
        for (const auto &v : res["freqs_hz"])
          if (v.is_number() && std::isfinite(v.get<double>()))
            s.freqs.push_back((float)v.get<double>());
      postFreqResponse(st, s, res, ownerDocId);
      return;
    } else {
      if (s.type == StudyType::Static && res.contains("adapt") &&
          res["adapt"].is_object()) {
        const auto &adapt = res["adapt"];
        s.adaptGlobalEta = adapt.value("global_eta", 0.0);
        s.adaptEtaTargetMet = adapt.value("eta_target_met", false);
        s.adaptStopReason = adapt.value("stop_reason", std::string());
        s.adaptPasses.clear();
        if (adapt.contains("passes") && adapt["passes"].is_array()) {
          for (const auto &p : adapt["passes"]) {
            if (!p.is_object())
              continue;
            Study::AdaptPass row;
            row.pass = p.value("pass", 0);
            row.nNodes = p.value("n_nodes", 0);
            row.nElems = p.value("n_elems", 0);
            row.nDof = p.value("n_dof", 0);
            row.elementSizeMm = p.value("element_size_mm", 0.0);
            row.globalEta = p.value("global_eta", 0.0);
            row.maxVonMisesMpa = p.value("max_von_mises_mpa", 0.0);
            row.maxDispMm = p.value("max_disp_mm", 0.0);
            s.adaptPasses.push_back(row);
          }
        }
      }
      // Mesh / system size for the Results metric table + solver transparency.
      s.nNodes = res.value("n_nodes", 0);
      s.nElems = res.value("n_elems", 0);
      s.nDof = res.value("n_dof", 0);
      s.resultSummary = (res.contains("summary") && res["summary"].is_object())
                            ? res["summary"]
                            : nlohmann::json::object();
      s.overlayPath =
          res.contains("overlay_aicm") && res["overlay_aicm"].is_string()
              ? res["overlay_aicm"].get<std::string>()
              : std::string();
      if (s.nNodes <= 0 || s.nDof <= 0 ||
          (s.type != StudyType::Modal && s.nElems <= 0) ||
          (s.type == StudyType::Modal &&
           (!res.contains("freqs_hz") || !res["freqs_hz"].is_array() ||
            res["freqs_hz"].empty())) ||
          (s.type != StudyType::Modal && s.resultSummary.empty())) {
        rejectResult("structural solver returned an incomplete result");
        return;
      }
      if (s.type == StudyType::Modal) {
        for (const auto &v : res["freqs_hz"]) {
          if (!v.is_number() || !std::isfinite(v.get<double>()) ||
              v.get<double>() < 0.0) {
            rejectResult(
                "modal solver returned a non-finite/negative frequency");
            return;
          }
        }
      } else if (s.type == StudyType::Fatigue) {
        double se = 0.0, sut = 0.0;
        const int method =
            (s.fatigueMethod < 0 || s.fatigueMethod > 4)
                ? 0
                : s.fatigueMethod;
        if (!finiteNumber(s.resultSummary, "se_corrected_mpa", se) ||
            se <= 0.0 || !finiteNumber(s.resultSummary, "sut_mpa", sut) ||
            sut <= 0.0 || !s.resultSummary.contains("theories") ||
            !s.resultSummary["theories"].is_object() ||
            !s.resultSummary["theories"].contains(kTheoryKeys[method]) ||
            !s.resultSummary["theories"][kTheoryKeys[method]].is_object()) {
          rejectResult("fatigue solver returned no finite fatigue audit");
          return;
        }
        double sa = 0.0;
        if (!finiteNumber(
                s.resultSummary["theories"][kTheoryKeys[method]],
                "sa_eq_mpa", sa) ||
            sa <= 0.0) {
          rejectResult(
              "fatigue solver returned a zero or non-finite equivalent stress");
          return;
        }
      } else {
        double vm = 0.0, disp = 0.0, fos = 0.0;
        if (!finiteNumber(s.resultSummary, "max_von_mises_mpa", vm) ||
            vm <= 0.0 ||
            !finiteNumber(s.resultSummary, "max_disp_mm", disp) ||
            disp <= 0.0 || !finiteNumber(s.resultSummary, "min_fos", fos) ||
            fos <= 0.0) {
          rejectResult(
              "static solver returned zero or non-finite response fields");
          return;
        }
      }
      // Numeric result cards are useful even when the routed C++ solver does
      // not package an AICM field overlay. Apply an overlay only when one was
      // actually returned; otherwise disclose the capability gap.
      if (s.overlayPath.empty()) {
        s.solverNotes.push_back(
            "This solver route returned validated numeric results but no "
            "viewport field overlay.");
        resultShown_ = false;
        shownStudyId_.clear();
      } else if (uiActive) {
        st.activeScalarField =
            (s.type == StudyType::Modal)     ? "mode_amplitude"
            : (s.type == StudyType::Fatigue) ? "fatigue_fos"
                                             : "von_mises";
        applyResultField(st, s);
        resultShown_ = true;
        shownStudyId_ = s.id;
      } else {
        resultShown_ = false;
        shownStudyId_.clear();
      }
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
      s.hasResult = true;
      st.addLog("[sim] FEA done: " + s.summary);
    }
    if (uiActive) {
      // An adaptive static run only "completed" if the error target it was given was actually
      // reached. The Results panel reads "Target met: No" straight off these same flags, so a
      // teal "Adaptive static FEA complete" in the status bar would contradict it. Unmet, or an
      // adapt envelope that never reported a target at all, both go out amber.
      bool adaptUnproven = false;
      switch (s.type) {
      case StudyType::Static:
        if (!s.adaptEnabled) {
          st.status = "Static FEA complete";
        } else if (s.adaptEtaTargetMet) {
          st.status = "Adaptive static FEA complete (error target met)";
        } else {
          adaptUnproven = true;
          if (s.adaptPasses.empty())
            st.status = "Adaptive static FEA finished; error target not reported";
          else if (s.adaptStopReason.empty())
            st.status = "Adaptive static FEA stopped; error target not met";
          else
            st.status = "Adaptive static FEA stopped (" + s.adaptStopReason +
                        "); error target not met";
        }
        break;
      case StudyType::Modal:
        st.status = "Modal analysis complete";
        break;
      case StudyType::Fatigue:
        st.status = "Fatigue analysis complete";
        break;
      case StudyType::ThermalFea:
        st.status = "Thermal FEA complete";
        break;
      default:
        break;
      }
      if (s.type == StudyType::Static || s.type == StudyType::Modal ||
          s.type == StudyType::Fatigue || s.type == StudyType::ThermalFea)
        st.statusKind = adaptUnproven ? 1 : 0;
    }
    // Cache the summary into the document so it reopens with the last-run
    // headline.
    persist(st, ownerDocId, s);
    node_ = SimNode::Results;
  }
}

// ---- pre-flight readiness gate ---------------------------------------------------------------
// Returns true only when the values that the selected backend actually consumes are complete and
// finite. run() repeats this gate, so ribbon commands cannot bypass the disabled panel button.
static bool studyReady(const Study &s, std::string &blocker) {
  auto finite = [](double v) { return std::isfinite(v); };
  if (s.type == StudyType::Motion) {
    if (!finite(s.motionStart) || !finite(s.motionStop) ||
        std::fabs(s.motionStop - s.motionStart) < 1e-9) {
      blocker = "Motion needs distinct finite start and stop values.";
      return false;
    }
    if (s.motionSteps < 2 || s.motionSteps > 120) {
      blocker = "Motion steps must be between 2 and 120.";
      return false;
    }
    return true;
  }
#ifdef __EMSCRIPTEN__
  if (isFlowFamily(s.type) || s.type == StudyType::Buckling ||
      s.type == StudyType::Contact || s.type == StudyType::FreqResponse) {
    blocker = "not available in the web version yet";
    return false;
  }
#endif
  if (isFlowFamily(s.type)) {
    float rho = 0.0f, nu = 0.0f;
    flowFluidProps(s, rho, nu);
    if (!finite(s.flowSpeed) || s.flowSpeed <= 0.0f) {
      blocker = "Flow speed must be finite and greater than zero.";
      return false;
    }
    if (!finite(s.flowAoA) || !finite(s.flowSideslip) ||
        s.flowDir < 0 || s.flowDir > 5 || s.flowPreset < 0 ||
        s.flowPreset > 3 || s.flowFluid < 0 || s.flowFluid > 2) {
      blocker = "Flow direction, fluid, preset, and angles must be valid.";
      return false;
    }
    if (!finite(rho) || !finite(nu) || rho <= 0.0f || nu <= 0.0f) {
      blocker = "Fluid density and kinematic viscosity must be finite and positive.";
      return false;
    }
    const float rough = flowRoughnessUm(s);
    if (!finite(rough) || rough < 0.0f || !finite(s.flowRefAreaMm2) ||
        s.flowRefAreaMm2 < 0.0f) {
      blocker = "Roughness and reference area must be finite and non-negative.";
      return false;
    }
    if (s.type == StudyType::Thermal ||
        (s.flowSolver == 1 && s.flowThermal)) {
      if (!finite(s.flowInletTempC)) {
        blocker = "Inlet temperature must be finite.";
        return false;
      }
      if (s.flowThermalType == 1) {
        if (!finite(s.flowSolidKappa) || s.flowSolidKappa <= 0.0f ||
            !finite(s.flowPowerW) || s.flowPowerW <= 0.0f) {
          blocker =
              "Conjugate thermal CFD needs positive conductivity and dissipated power.";
          return false;
        }
      } else if ((s.flowWallMode == 0 && !finite(s.flowWallTempC)) ||
                 (s.flowWallMode == 1 &&
                  (!finite(s.flowWallFluxWm2) ||
                   std::fabs(s.flowWallFluxWm2) < 1e-12)) ||
                 (s.flowWallMode == 2 &&
                  (!finite(s.flowWallPowerW) || s.flowWallPowerW <= 0.0f))) {
        blocker = "Set a finite, non-zero thermal wall excitation.";
        return false;
      }
    }
    return true;
  }

  const int mi = SimManager::materialIndexByKey(s.materialKey);
  if (mi < 0) {
    blocker = s.materialKey.empty()
                  ? "Material required — pick a material before running."
                  : "The part material is not in the simulation material library.";
    return false;
  }
  const auto &mat = SimManager::materials()[mi];
  if (!finite(mat.E_mpa) || mat.E_mpa <= 0.0 || !finite(mat.nu) ||
      mat.nu <= -1.0 || mat.nu >= 0.5) {
    blocker = "Material needs a positive Young's modulus and Poisson ratio below 0.5.";
    return false;
  }
  if ((s.type == StudyType::Modal || s.type == StudyType::FreqResponse ||
       (s.type == StudyType::ThermalFea && s.thermTransient)) &&
      (!finite(mat.density_g_cm3) || mat.density_g_cm3 <= 0.0)) {
    blocker = "This study needs a material with positive density.";
    return false;
  }
  if (s.type == StudyType::Static || s.type == StudyType::Modal ||
      s.type == StudyType::Fatigue || s.type == StudyType::ThermalFea ||
      s.type == StudyType::FreqResponse) {
    if (!finite(s.meshFineness) || s.meshFineness < 0.0f ||
        s.meshFineness > 1.0f || !finite(s.meshTargetMm) ||
        s.meshTargetMm < 0.0f || !finite(s.meshMinMm) ||
        s.meshMinMm < 0.0f ||
        (s.meshTargetMm > 0.0f && s.meshMinMm > s.meshTargetMm) ||
        (s.meshOrder != 1 && s.meshOrder != 2)) {
      blocker =
          "Mesh controls must be finite; minimum size cannot exceed target size.";
      return false;
    }
  }

  if (s.type == StudyType::Buckling) {
    if (!finite(s.bucklingLoadN) || s.bucklingLoadN <= 0.0f) {
      blocker = "Buckling needs a finite positive compressive load.";
      return false;
    }
    return true;
  }
  if (s.type == StudyType::Contact) {
    if (!finite(s.contactPressureMpa) || s.contactPressureMpa <= 0.0f) {
      blocker = "Contact needs a finite positive pressure.";
      return false;
    }
    return true;
  }
  if (s.type == StudyType::ThermalFea) {
    if (!finite(s.thermK) || s.thermK <= 0.0f) {
      blocker = "Thermal FEA needs positive solid conductivity.";
      return false;
    }
    if (!finite(s.thermFixTempC) || !finite(s.thermTInitC)) {
      blocker = "Thermal boundary and initial temperatures must be finite.";
      return false;
    }
    if (s.thermTransient &&
        (!finite(s.thermCp) || s.thermCp <= 0.0f ||
         !finite(s.thermTEndS) || s.thermTEndS <= 0.0f ||
         !finite(s.thermDtS) || s.thermDtS < 0.0f ||
         (s.thermDtS > 0.0f && s.thermDtS > s.thermTEndS))) {
      blocker = "Transient thermal FEA needs positive heat capacity and horizon; dt must be 0 or no greater than the horizon.";
      return false;
    }
    if (s.fixtures.empty() && s.loads.empty()) {
      blocker = "Add a fixed-temperature face or a flux/convection boundary.";
      return false;
    }
    for (int i = 0; i < (int)s.loads.size(); ++i) {
      const SimLoad &ld = s.loads[i];
      if (ld.faces.empty()) {
        blocker = "Thermal load " + std::to_string(i + 1) + " has no faces.";
        return false;
      }
      if (ld.thermalKind == 1) {
        if (!finite(ld.hWm2k) || ld.hWm2k <= 0.0f || !finite(ld.tAmbC)) {
          blocker = "Convection loads need positive h and a finite ambient temperature.";
          return false;
        }
      } else if (!finite(ld.fluxWm2) || std::fabs(ld.fluxWm2) < 1e-12) {
        blocker = "Heat-flux loads must be finite and non-zero.";
        return false;
      }
    }
    if (!s.thermTransient && s.fixtures.empty()) {
      bool hasConvection = false;
      for (const auto &ld : s.loads)
        hasConvection = hasConvection || ld.thermalKind == 1;
      if (!hasConvection) {
        blocker =
            "Steady thermal FEA needs a fixed temperature or convection sink.";
        return false;
      }
    }
    return true;
  }
  if (s.type == StudyType::FreqResponse) {
    if (!finite(s.frBaseG) || s.frBaseG <= 0.0f || !finite(s.frZeta) ||
        s.frZeta <= 0.0f || s.frZeta >= 1.0f) {
      blocker = "Vibration needs positive base acceleration and damping between 0 and 1.";
      return false;
    }
    return true; // fixtures remain optional: free-free modal basis is intentional
  }
  if (s.type == StudyType::Modal)
    return true; // intentional free-free exception; no applied load is consumed

  if (s.fixtures.empty()) {
    blocker = "At least one fixed face is required to constrain the part.";
    return false;
  }
  if (s.loads.empty()) {
    blocker = "Add at least one non-zero force or pressure load.";
    return false;
  }
  for (int i = 0; i < (int)s.loads.size(); ++i) {
    const SimLoad &ld = s.loads[i];
    if (ld.faces.empty()) {
      blocker = "Load " + std::to_string(i + 1) + " has no faces.";
      return false;
    }
    if (ld.pressure) {
      if (!finite(ld.pressureMpa) || ld.pressureMpa <= 0.0f) {
        blocker = "Pressure loads must be finite and greater than zero.";
        return false;
      }
    } else {
      const double mag2 = (double)ld.vec[0] * ld.vec[0] +
                          (double)ld.vec[1] * ld.vec[1] +
                          (double)ld.vec[2] * ld.vec[2];
      if (!finite(mag2) || mag2 <= 1e-18) {
        blocker = "Force loads must have a finite non-zero vector.";
        return false;
      }
    }
  }
  if (s.type == StudyType::Fatigue) {
    const double matSut = mat.ultimate_mpa;
    if ((!finite(s.fatigueSut) || s.fatigueSut <= 0.0f) &&
        (!finite(matSut) || matSut <= 0.0)) {
      blocker = "Fatigue needs ultimate strength (Sut) from the study or material.";
      return false;
    }
    float lmin = 0.0f, lmax = 0.0f;
    fatigueLoadFactors(s, lmin, lmax);
    if (!finite(lmin) || !finite(lmax) || lmax <= lmin) {
      blocker = "Fatigue load factors must be finite with maximum greater than minimum.";
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

  // The body is embedded in a padded child.  ImGui's default wrap edge can
  // still point at the parent window, so bind wrapping to this child's live
  // content width.  This also keeps long study names, empty-state guidance and
  // qualitative warnings inside the rail at narrower window sizes.
  ImGui::PushTextWrapPos(ImGui::GetCursorPosX() +
                         ImGui::GetContentRegionAvail().x);
  struct WrapPop {
    ~WrapPop() { ImGui::PopTextWrapPos(); }
  } wrapPop;

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
  const ImVec2 newStudySize(ImGui::GetContentRegionAvail().x, 0.0f);
  if (ImGui::Button("+ New Study", newStudySize) || shotForcePicker)
    ImGui::OpenPopup("##newStudyPicker");
  simTip("Add a simulation study. The offered kinds follow what you have open: "
         "mechanical "
         "studies for parts/assemblies, motion for mated assemblies, "
         "electronics simulation "
         "and firmware emulation when a schematic / PCB / firmware project is "
         "open.");

  const float S2 = ui::uiScale;
  // ImGui parks a popup at the cursor, i.e. on top of the "+ New Study" button - so the picker
  // covered the very panel it was opened from (in a CHUDWARE_SHOT=simdisc_ecad capture it hid the
  // study list and the empty-state prose, which read as clipped text). Fly it out to the RIGHT of
  // the manager column instead; ImGui still clamps it into the viewport.
  {
    const float colEdge = ui::overlayColumnEdge(0);
    if (colEdge > 0.0f)
      ImGui::SetNextWindowPos(ImVec2(colEdge + 8.0f * S2, ImGui::GetCursorScreenPos().y),
                              ImGuiCond_Appearing);
  }
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
    const nlohmann::json flowCaps =
        st.simClient ? st.simClient->caps() : nlohmann::json::object();
    const bool openfoamMissing =
        !flowCaps.is_object() || !flowCaps.value("openfoam", false);
    const std::string openfoamHint =
        "OpenFOAM is not routed by this desktop backend; thermal CFD is unavailable.";
    const bool ccxMissing =
        capsStatus_ == 1 &&
        (!caps_.contains("ccx") || !caps_["ccx"].is_object() ||
         !caps_["ccx"].value("found", false));
    const std::string ccxHint =
        ccxMissing && caps_.contains("ccx") && caps_["ccx"].is_object()
            ? caps_["ccx"].value("hint",
                                 std::string("CalculiX (ccx) not found."))
            : std::string("CalculiX (ccx) not found.");
    const bool vibrationMissing =
        capsStatus_ == 1 &&
        (!caps_.contains("freq_response") ||
         !caps_["freq_response"].is_object() ||
         !caps_["freq_response"].value("available", false));
    const std::string vibrationHint =
        vibrationMissing && caps_.contains("freq_response") &&
                caps_["freq_response"].is_object()
            ? caps_["freq_response"].value(
                  "hint",
                  std::string("Vibration post-processing is unavailable."))
            : std::string("Vibration post-processing is unavailable.");
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
           "Harmonic base-excitation response (modal superposition).",
           webNoPy || vibrationMissing,
           webNoPy ? webNoPyHint : vibrationHint});
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
                          "External aerodynamics: deterministic numeric LBM "
                          "drag and lift results.",
                          webNoPy, webNoPy ? webNoPyHint : std::string()});
      kEntries.push_back({Kind::Fea, StudyType::Thermal, "Thermal (CFD)",
                          studyTypeIcon(StudyType::Thermal),
                          "Heat transfer via OpenFOAM convection / CHT.",
                          webNoPy || openfoamMissing,
                          webNoPy ? webNoPyHint : openfoamHint});
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
    kit::dimText("New study");
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
      kit::dimText("Open a part, assembly, schematic, PCB or firmware project "
                   "to see the study kinds it unlocks.");
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
      else {
        newStudy(st, chosenType);
        if (sel_ >= 0 && sel_ < (int)studies_.size()) {
          node_ = SimNode::Study;
          loadIdx_ = -1;
          armedWell_ = -1;
          panelOpen_ = true;
        }
      }
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
      const ImVec2 handoffSize(ImGui::GetContentRegionAvail().x, 0.0f);
      if (ImGui::Button("Send board to thermal FEA", handoffSize))
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
    kit::dimText(
        "Open a part or assembly to set up mechanical studies. Electronics "
        "docs unlock circuit simulation and emulation in the picker above.");
    return;
  }
  if (studies_.empty()) {
    // FM chrome convention: Spacing alone separates — no orphaned hairline under the button.
    ImGui::Spacing();
    kit::dimText("No studies yet. Create a study above to add fixtures, "
                 "loads, and run an "
                 "analysis. Studies are saved inside this part file.");
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
    const bool ok = cmName_[0] != '\0' && std::isfinite(cmE_) && cmE_ > 0.0f &&
                    std::isfinite(cmNu_) && cmNu_ > 0.0f && cmNu_ < 0.5f &&
                    std::isfinite(cmDensity_) && cmDensity_ >= 0.0f &&
                    std::isfinite(cmYield_) && cmYield_ >= 0.0f &&
                    std::isfinite(cmUlt_) && cmUlt_ >= 0.0f &&
                    std::isfinite(cmEnd_) && cmEnd_ >= 0.0f;
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
      const std::string key = addCustomMaterial(m);
      if (key.empty()) {
        st.toast(ui::ToastLevel::Error,
                 "Could not write the custom material library");
      } else {
        applyPartMaterial(st, s, key);
        persist(st, s);
        ImGui::CloseCurrentPopup();
      }
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
    char buf[192];
    std::snprintf(buf, sizeof(buf),
                  "E %.0f MPa  -  nu %.2f  -  yield %.0f MPa  -  rho %.2f g/cm3",
                  m.E_mpa, m.nu, m.yield_mpa, m.density_g_cm3);
    kit::dimText(buf);
  } else if (!s.materialKey.empty()) {
    char buf[256];
    std::snprintf(
        buf, sizeof(buf),
        "Part material '%s' is not in the library — pick a library material.",
        s.materialKey.c_str());
    kit::dimText(buf);
  }
}

void SimManager::drawWellRow(AppState &st, const char *label, SimWell &well,
                             int armId, const char *tip) {
  ImGui::PushID(armId);
  const bool armed = (armedWell_ == armId);
  ImGui::TextUnformatted(label);
  simTip(tip);
  // Active selection box: armed = accent border + "Selecting…" hint. Only the
  // armed state overrides the button role; unarmed leaves the style untouched.
  if (armed) {
    float a[4];
    Palette::withAlpha(ui::palette.accent, a, 0.85f);
    ImGui::PushStyleColor(ImGuiCol_Button, simRole(a));
  }
  if (ImGui::Button(armed ? "Selecting faces… (click viewport)" : "Add faces",
                    ImVec2(-FLT_MIN, 0))) {
    const bool nowArmed = !armed;
    armedWell_ = nowArmed ? armId : -1;
    if (nowArmed)
      st.selection
          .clear(); // fresh well — no carryover from a previously-picked face
  }
  if (armed)
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
      "Polyhedral-FEA fills the part with graded tetrahedra. Finer meshes "
      "capture stress concentrations better but take longer.");
  ImGui::Spacing();

  const bool modal = s.type == StudyType::Modal;
  if (modal && !s.loads.empty()) {
    s.loads.clear();
    persist(st, s);
  }

  ImGui::TextUnformatted("Mesh fineness");
  ImGui::SetNextItemWidth(-FLT_MIN);
  const char *fineWord = s.meshFineness < 0.2f   ? "Coarse"
                         : s.meshFineness < 0.4f ? "Coarse-medium"
                         : s.meshFineness < 0.6f ? "Medium"
                         : s.meshFineness < 0.8f ? "Fine-medium"
                                                 : "Fine";
  ImGui::SliderFloat("##fineness", &s.meshFineness, 0.0f, 1.0f, fineWord);
  if (ImGui::IsItemDeactivatedAfterEdit()) {
    persist(st, s);
    clearMeshPreview();
  }
  simTip("Base element size derived from the part's bounding box. Left is "
         "coarse and fast; right is fine and more accurate.");
  ImGui::Spacing();

  ImGui::TextUnformatted("Element order");
  static const char *kOrder[2] = {"Draft - linear (tet4)",
                                  "High quality - quadratic (tet10)"};
  int orderIndex = !modal && s.meshOrder == 2 ? 1 : 0;
  if (modal)
    ImGui::BeginDisabled();
  for (int i = 0; i < 2; ++i)
    if (ImGui::RadioButton(kOrder[i], orderIndex == i) && orderIndex != i) {
      s.meshOrder = i ? 2 : 1;
      persist(st, s);
      clearMeshPreview();
    }
  if (modal)
    ImGui::EndDisabled();
  simTip("Quadratic elements capture bending and curved boundaries more "
         "accurately. Modal studies use linear elements.");
  if (modal)
    ImGui::TextDisabled("Frequency studies always use 1st-order elements.");
  ImGui::Spacing();

  if (ImGui::CollapsingHeader("Advanced")) {
    ImGui::Indent();
    bool edited = false;
    ImGui::TextUnformatted("Element size override (mm, 0 = auto)");
    float target = s.meshTargetMm;
    float minimum = s.meshMinMm;
    ImGui::SetNextItemWidth(-FLT_MIN);
    ImGui::InputFloat("##target_size", &target, 0.0f, 0.0f, "%.4g");
    edited = edited || ImGui::IsItemDeactivatedAfterEdit();
    simTip("Explicit target edge length in millimetres. Overrides fineness.");
    ImGui::SetNextItemWidth(-FLT_MIN);
    ImGui::InputFloat("##min_size", &minimum, 0.0f, 0.0f, "%.4g");
    edited = edited || ImGui::IsItemDeactivatedAfterEdit();
    simTip("Minimum edge length in millimetres for graded refinement.");
    if (edited) {
      s.meshTargetMm = std::max(0.0f, target);
      s.meshMinMm = std::max(0.0f, minimum);
      persist(st, s);
      clearMeshPreview();
    }

    if (supportsLearnedAdvisor(s.type)) {
      ImGui::Spacing();
      if (ImGui::Checkbox("Use learned mesh advisor", &s.learnedAdvisor)) {
        persist(st, s);
        clearMeshPreview();
      }
      simTip(
          "Uses Polyhedral-FEA's optional learned sizing and order advice. "
          "Chudware still enforces resource, mesh-quality, and solver gates.");
      if (s.learnedAdvisor) {
        ImGui::TextUnformatted("Advisor DOF budget (0 = unset)");
        int maximumDof = s.advisorMaxDof;
        ImGui::SetNextItemWidth(-FLT_MIN);
        ImGui::InputInt("##advisormaxdof", &maximumDof, 1000, 10000);
        if (ImGui::IsItemDeactivatedAfterEdit()) {
          s.advisorMaxDof = std::max(0, maximumDof);
          persist(st, s);
          clearMeshPreview();
        }
      }
    }
    ImGui::Unindent();
  }
  ImGui::Spacing();

  if (s.type == StudyType::Static) {
    if (ImGui::Checkbox("Adaptive refinement", &s.adaptEnabled)) {
      persist(st, s);
      clearMeshPreview();
    }
    simTip("Re-meshes and re-solves toward the largest recovered "
           "discretization error until the target or pass limit is reached.");
    if (s.adaptEnabled) {
      ImGui::Indent();
      ImGui::TextUnformatted("Maximum passes");
      ImGui::SetNextItemWidth(-FLT_MIN);
      if (ImGui::SliderInt("##adaptpasses", &s.adaptMaxPasses, 1, 6)) {
        s.adaptMaxPasses = std::clamp(s.adaptMaxPasses, 1, 6);
        persist(st, s);
        clearMeshPreview();
      }
      ImGui::TextUnformatted("Error indicator target");
      ImGui::SetNextItemWidth(-FLT_MIN);
      float etaTarget = s.adaptEtaTarget;
      ImGui::InputFloat("##adaptetatarget", &etaTarget, 0.005f, 0.01f, "%.4g");
      if (ImGui::IsItemDeactivatedAfterEdit()) {
        s.adaptEtaTarget = std::max(0.0f, etaTarget);
        persist(st, s);
        clearMeshPreview();
      }
      ImGui::Unindent();
    }
    ImGui::Spacing();
  }
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
    if (!s.meshTypeUsed.empty())
      ImGui::Text("Mesh type: %s", s.meshTypeUsed.c_str());
    ImGui::TextDisabled("Mesher: Polyhedral-FEA");
    if (!s.meshNote.empty())
      ImGui::TextWrapped("%s", s.meshNote.c_str());
    if (!meshPreviewSegs_.empty() && meshPreviewStudyId_ == s.id) {
      ImGui::TextDisabled("Wireframe shown in the viewport.");
      // Honest LOD disclosure: a big study used to draw nothing at all here.
      // It now always draws something, and always says how much of the real
      // mesh that something is. The solve itself uses the FULL mesh.
      if (meshPreviewEdgesShown_ < meshPreviewEdgesTotal_ ||
          meshPreviewCoarse_) {
        ImGui::PushStyleColor(ImGuiCol_Text, simRole(ui::palette.statusWarn));
        ImGui::TextWrapped(
            "Showing %s of %s edges%s.",
            groupedCount((uint64_t)meshPreviewEdgesShown_).c_str(),
            groupedCount((uint64_t)meshPreviewEdgesTotal_).c_str(),
            meshPreviewCoarse_ ? " (coarse LOD)" : " (display budget)");
        ImGui::PopStyleColor();
        if (meshPreviewCoarse_)
          ImGui::TextDisabled(
              "Coarse LOD samples %s of %s elements, so this is an indicative "
              "wire cloud, not the exact surface mesh.",
              groupedCount((uint64_t)meshPreviewElemsUsed_).c_str(),
              groupedCount((uint64_t)meshPreviewElemsTotal_).c_str());
        ImGui::TextDisabled("The solve uses the full mesh; only the preview is "
                            "decimated.");
      }
    }
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
    char buf[96];
    std::snprintf(buf, sizeof(buf), "ka %.2f  kb %.2f  kc %.2f  kd %.2f", ka,
                  kb, kc, kd);
    kit::dimText(buf);
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
              bandCol = simRoleU32(ui::palette.axisZ, 0.216f);
  // 5-hue categorical ramp for the theory traces. Borrowed from the
  // orientation-triad + CAM roles — the palette's only theme-aware categorical
  // hues — until it gains a dedicated chart-series ramp.
  const float *const tramp[5] = {ui::palette.axisZ, ui::palette.axisY,
                                 ui::palette.statusWarn, ui::palette.camArc,
                                 ui::palette.axisX};
  ImU32 tcol[5], tdim[5];
  for (int i = 0; i < 5; ++i) {
    tcol[i] = simRoleU32(tramp[i]);
    tdim[i] = simRoleU32(tramp[i], 0.47f);
  }

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
  char buf[128];
  std::snprintf(buf, sizeof(buf), "Incoming flow: (%.2f, %.2f, %.2f)", dir[0],
                dir[1], dir[2]);
  kit::dimText(buf);
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
    std::snprintf(buf, sizeof(buf), "Re ~ %.0f  |  rho %.3g kg/m3", Re,
                  density);
    kit::dimText(buf);
  }
  (void)S;
}

// ---- optional core-advisor disclosure
// ----------------------------------------------------- Draws nothing when the
// result carried no advisor object. Recommendations that do not fit Chudware's
// fixed-stride tet solve contract are reported as advice, never as executed
// setup.
static void drawAdvisorDisclosure(AppState &st, const Study &s) {
  const SimAdvisor &a = s.advisor;
  if (!a.present)
    return;
  ImGui::Spacing();
  if (!ImGui::TreeNodeEx("Setup advisor", ImGuiTreeNodeFlags_DefaultOpen))
    return;
  const char *state = a.budgetRefusal ? "Refused - predicted cost over budget"
                      : a.vetoed ? "Vetoed - recommendation rejected by a guard"
                      : a.applied ? "Applied - shaped this run's setup"
                      : a.available
                          ? "Available - not applied to this run"
                          : "Unavailable - no recommendation produced";
  const bool warn = a.budgetRefusal || a.vetoed || !a.available;
  ImGui::PushStyleColor(ImGuiCol_Text, simRole(warn ? ui::palette.statusWarn
                                                    : ui::palette.statusOk));
  ImGui::TextWrapped("%s", state);
  ImGui::PopStyleColor();
  simTip("The advisor only proposes mesh sizing, element order and adaptation. "
         "It never chooses or performs the solve, and this block never reports "
         "solver results.");
  if (!a.mesher.empty()) {
    ImGui::Text("Recommended mesher: %s", a.mesher.c_str());
    if (advisorMesherIsPolyhedral(a.mesher)) {
      ImGui::PushStyleColor(ImGuiCol_Text, simRole(ui::palette.statusWarn));
      ImGui::TextWrapped(
          "Chudware currently solves fixed-stride tetrahedral meshes. This "
          "hybrid/VEM family recommendation was not executed.");
      ImGui::PopStyleColor();
    }
  }
  if (SimAdvisor::has(a.hRel))
    ImGui::Text("Recommended relative size: %.4g", a.hRel);
  if (a.order > 0)
    ImGui::Text("Recommended element order: %d", a.order);
  if (a.adaptPasses >= 0)
    ImGui::Text("Recommended adaptive passes: %d", a.adaptPasses);
  if (SimAdvisor::has(a.etaTarget))
    ImGui::Text("Recommended error target: %.4g", a.etaTarget);
  if (SimAdvisor::has(a.predictedDof))
    ImGui::Text("Predicted DOF: %.0f", a.predictedDof);
  if (SimAdvisor::has(a.predictedMeshMs) ||
      SimAdvisor::has(a.predictedSolveMs)) {
    const std::string meshMs =
        SimAdvisor::has(a.predictedMeshMs)
            ? std::to_string((long long)a.predictedMeshMs) + " ms"
            : std::string("n/a");
    const std::string solveMs =
        SimAdvisor::has(a.predictedSolveMs)
            ? std::to_string((long long)a.predictedSolveMs) + " ms"
            : std::string("n/a");
    ImGui::Text("Predicted time: mesh %s, solve %s", meshMs.c_str(),
                solveMs.c_str());
  }
  if (SimAdvisor::has(a.failureProb)) {
    const bool risky = a.failureProb >= 0.5;
    if (risky)
      ImGui::PushStyleColor(ImGuiCol_Text, simRole(ui::palette.statusWarn));
    ImGui::Text("Predicted failure probability: %.1f%%", a.failureProb * 100.0);
    if (risky)
      ImGui::PopStyleColor();
  }
  if (SimAdvisor::has(a.oodDistance)) {
    const bool ood = a.oodDistance > 1.0;
    if (ood)
      ImGui::PushStyleColor(ImGuiCol_Text, simRole(ui::palette.statusWarn));
    ImGui::Text("Distance from training distribution: %.3g%s", a.oodDistance,
                ood ? " (out of distribution)" : "");
    if (ood)
      ImGui::PopStyleColor();
    simTip("How far this part/setup sits from the cases the advisor was "
           "trained on. Large values mean its predictions are extrapolation.");
  }
  if (!a.modelRevision.empty())
    ImGui::TextDisabled("Advisor model: %s", a.modelRevision.c_str());
  if (!a.note.empty())
    ImGui::TextWrapped("%s", a.note.c_str());
  if (!a.anyDetail())
    ImGui::TextDisabled("No further detail reported.");
  (void)st;
  ImGui::TreePop();
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
    drawMotionResults(st, s);
    return;
  }

  if (!s.hasResult) {
    ImGui::TextWrapped("No results yet. Set up the study and press Run.");
    return;
  }
  const UnitSys u = unitSys(st);
  char b[80];

  // drawForm owns the single scrolling body.  Keeping results in that same
  // child avoids nested height calculations and leaves the pinned Run action
  // outside the scrolling region.

  ImGui::PushFont(st.fonts.medium);
  ImGui::TextUnformatted("Results");
  ImGui::PopFont();
  ImGui::Spacing();

  // --- result-field selector (static + fatigue + spatial flow results expose
  // multiple fields; switching is instant). Numeric-only LBM results must not
  // advertise plot fields that are absent from their result payload.
  const bool hasFlowPlot =
      isFlowFamily(s.type) && !s.flowFields.empty() && !s.overlayPath.empty();
  if (s.type == StudyType::Static || s.type == StudyType::Fatigue ||
      hasFlowPlot) {
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
               (s.type == StudyType::Thermal || s.flowThermal) &&
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
  if (s.type == StudyType::Static && !s.adaptPasses.empty()) {
    ImGui::Spacing();
    ImGui::PushFont(st.fonts.medium);
    ImGui::TextUnformatted("Adaptive convergence");
    ImGui::PopFont();
    if (!s.adaptEtaTargetMet)
      ImGui::PushStyleColor(ImGuiCol_Text,
                            simRole(ui::palette.statusWarn));
    ImGui::Text("Achieved error indicator: %.4g", s.adaptGlobalEta);
    ImGui::Text("Target met: %s", s.adaptEtaTargetMet ? "Yes" : "No");
    ImGui::Text("Passes run: %d", (int)s.adaptPasses.size());
    if (!s.adaptEtaTargetMet) {
      ImGui::TextWrapped(
          "Attention: target not met; treat the stress result as "
          "under-resolved.");
      ImGui::PopStyleColor();
    }
    if (!s.adaptStopReason.empty())
      ImGui::TextDisabled("Stopped: %s", s.adaptStopReason.c_str());
    if (ImGui::BeginTable("##adaptpasses", 3,
                          ImGuiTableFlags_SizingStretchProp |
                              ImGuiTableFlags_BordersInnerH)) {
      ImGui::TableSetupColumn("Pass");
      ImGui::TableSetupColumn("Elements");
      ImGui::TableSetupColumn("Indicator");
      ImGui::TableHeadersRow();
      for (const auto &pass : s.adaptPasses) {
        ImGui::TableNextRow();
        ImGui::TableNextColumn();
        ImGui::Text("%d", pass.pass);
        ImGui::TableNextColumn();
        ImGui::Text("%d", pass.nElems);
        ImGui::TableNextColumn();
        ImGui::Text("%.4g", pass.globalEta);
      }
      ImGui::EndTable();
    }
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
  if (isFlowFamily(s.type) && !s.dragTrace.empty()) {
    ImGui::PlotLines("##drag", s.dragTrace.data(), (int)s.dragTrace.size(), 0,
                     "Drag trace", FLT_MAX, FLT_MAX,
                     ImVec2(-FLT_MIN, 50.0f * ui::uiScale));
    // The live trace is thinned at ingestion so a long run cannot grow it
    // without bound; say so rather than implying every iteration is plotted.
    if (s.dragTraceStride > 1)
      ImGui::TextDisabled("Showing %s of %s iterations (1 in %u).",
                          groupedCount((uint64_t)s.dragTrace.size()).c_str(),
                          groupedCount(s.dragTraceTotal).c_str(),
                          s.dragTraceStride);
  }

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
      for (const auto &n : s.solverNotes) {
        ImGui::Bullet();
        ImGui::SameLine();
        ImGui::TextWrapped("%s", n.c_str());
      }
      ImGui::TreePop();
    }
  }
  drawAdvisorDisclosure(st, s);

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
    if (flowLinesBuiltFor_ == s.id && flowSegStride_ > 1)
      ImGui::TextDisabled(
          "Line overlay: showing %s of %s segments (1 in %u) to stay inside "
          "the display budget.",
          groupedCount((uint64_t)flowSegsShown_).c_str(),
          groupedCount((uint64_t)flowSegsTotal_).c_str(), flowSegStride_);
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
  // Everything above the action belongs to one scrolling region.  The Run
  // action is drawn after EndChild(), so long forms, study lists and result
  // warnings can scroll without ever disappearing behind it.
  const float runButtonHeight = 30.0f * S;
  const float actionReserve =
      runButtonHeight + ImGui::GetStyle().ItemSpacing.y * 4.0f +
      1.0f * S;
  const float bodyHeight =
      std::max(ImGui::GetContentRegionAvail().y - actionReserve,
               ImGui::GetFrameHeight());
  ImGui::BeginChild("##sim_form_scroll", ImVec2(0.0f, bodyHeight),
                    ImGuiChildFlags_None, ImGuiWindowFlags_NoScrollbar);
  ImGui::PushTextWrapPos(ImGui::GetCursorPosX() +
                         ImGui::GetContentRegionAvail().x);

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
        kit::dimText(
            caps_["ccx"]
                .value("hint", std::string("CalculiX (ccx) not found."))
                .c_str());
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
        kit::dimText(
            caps_["ccx"]
                .value("hint", std::string("CalculiX (ccx) not found."))
                .c_str());
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
  ImGui::PopTextWrapPos();
  ui::drawWindowPillScrollbarV(simSurf());
  ImGui::EndChild();

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
    if (ready && (!st.docMirror.hasDoc() || !hasSimulationGeometry(st))) {
      ready = false;
      blocker = "The active document has no solid geometry to simulate.";
    }
    if (ready && s.type == StudyType::Motion &&
        (st.activeTab < 0 || st.activeTab >= (int)st.docTabs.size() ||
         st.docTabs[st.activeTab].kind !=
             AppState::DocTab::TabKind::Assembly)) {
      ready = false;
      blocker = "Motion studies require an active assembly document.";
    }
    if (ready && s.type != StudyType::Motion &&
        s.type != StudyType::Buckling && s.type != StudyType::Contact &&
        !st.simClient) {
      ready = false;
      blocker = "The simulation backend is unavailable.";
    }
    if (ready &&
        ((isFlowFamily(s.type) && s.flowSolver == 1) ||
         s.type == StudyType::Thermal)) {
      const auto flowCaps =
          st.simClient ? st.simClient->caps() : nlohmann::json::object();
      if (!flowCaps.is_object() || !flowCaps.value("openfoam", false)) {
        ready = false;
        blocker =
            "OpenFOAM is not routed by this backend; thermal CFD is unavailable.";
      }
    }
    if (ready && (s.type == StudyType::Buckling ||
                  s.type == StudyType::Contact ||
                  s.type == StudyType::FreqResponse)) {
      if (capsStatus_ <= 0) {
        ready = false;
        blocker = capsStatus_ == 0
                      ? "Checking advanced simulation capabilities."
                      : "Advanced simulation capability probe failed.";
      } else if ((s.type == StudyType::Buckling ||
                  s.type == StudyType::Contact) &&
                 (!caps_.contains("ccx") || !caps_["ccx"].is_object() ||
                  !caps_["ccx"].value("found", false))) {
        ready = false;
        blocker = "CalculiX is unavailable.";
      } else if (s.type == StudyType::FreqResponse &&
                 (!caps_.contains("freq_response") ||
                  !caps_["freq_response"].is_object() ||
                  !caps_["freq_response"].value("available", false))) {
        ready = false;
        blocker = "Vibration post-processing is unavailable.";
      }
    }
    const bool canRun = ready && !busy;
    if (!canRun)
      ImGui::BeginDisabled();
    const ImVec2 runSize(ImGui::GetContentRegionAvail().x, runButtonHeight);
    if (ImGui::Button(busy ? "Running\xe2\x80\xa6" : "Run", runSize))
      run(st, s);
    if (!canRun)
      ImGui::EndDisabled();
    // Tooltip: blocker reason (when unmet) or generic hint (when ready).
    if (!busy && !ready)
      simTip(blocker.c_str());
    else
      simTip("Mesh the part and solve. Results color the model and appear "
             "under Results.");
    if (st.simClient && st.simClient->job().running.load()) {
      if (st.simClient->cancelRequested()) {
        ImGui::BeginDisabled();
        ImGui::SmallButton("Cancel requested");
        ImGui::EndDisabled();
      } else if (ImGui::SmallButton("Cancel")) {
        st.simClient->cancel();
        st.status = "Sim: cancellation requested; waiting for a checkpoint";
        st.statusKind = 1;
      }
      ImGui::TextDisabled(
          "Cancellation is cooperative and takes effect at the next solver "
          "checkpoint.");
    } else if (advRunning_ || prepInFlight_) {
      ImGui::TextDisabled(
          "This preparation/advanced stage has no cancellation route.");
    }
    // Live drag-convergence sparkline and final result summary
    if (!s.dragTrace.empty()) {
      ImGui::PlotLines("##drag", s.dragTrace.data(), (int)s.dragTrace.size(), 0,
                       "Drag trace", FLT_MAX, FLT_MAX,
                       ImVec2(-FLT_MIN, 50.0f * S));
      if (s.dragTraceStride > 1)
        ImGui::TextDisabled("Showing %s of %s iterations (1 in %u).",
                            groupedCount((uint64_t)s.dragTrace.size()).c_str(),
                            groupedCount(s.dragTraceTotal).c_str(),
                            s.dragTraceStride);
    }
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
// trajectory in one shot. The result is cached in the Study and played back on
// the main thread by tickMotion + applyMotionFrame. The cached AICM is already
// world-space, so playback applies frameWorld * inverse(cachedBaseWorld).
//
// Engine-side solve path: worker_router dispatches "simulate_motion" ->
// engine.assemble.motion which calls solve_mates() at each driver step.  No
// simClient / sim worker involved.

using MotionMat4 = std::array<double, 16>;

enum class MotionMatrixLayout { ColumnMajor, RowMajor };

static double motionMatAt(const MotionMat4 &m, int row, int col) {
  return m[static_cast<size_t>(col * 4 + row)];
}

static bool invertMotionMatrix(const MotionMat4 &in, MotionMat4 &out) {
  double aug[4][8] = {};
  for (int r = 0; r < 4; ++r)
    for (int c = 0; c < 4; ++c) {
      const double v = motionMatAt(in, r, c);
      if (!std::isfinite(v))
        return false;
      aug[r][c] = v;
    }
  for (int r = 0; r < 4; ++r)
    aug[r][4 + r] = 1.0;

  constexpr double pivotFloor = 1e-12;
  for (int col = 0; col < 4; ++col) {
    int pivot = col;
    for (int r = col + 1; r < 4; ++r)
      if (std::abs(aug[r][col]) > std::abs(aug[pivot][col]))
        pivot = r;
    if (!std::isfinite(aug[pivot][col]) ||
        std::abs(aug[pivot][col]) <= pivotFloor)
      return false;
    if (pivot != col)
      for (int c = 0; c < 8; ++c)
        std::swap(aug[pivot][c], aug[col][c]);

    const double divisor = aug[col][col];
    for (int c = 0; c < 8; ++c)
      aug[col][c] /= divisor;
    for (int r = 0; r < 4; ++r) {
      if (r == col)
        continue;
      const double factor = aug[r][col];
      for (int c = 0; c < 8; ++c)
        aug[r][c] -= factor * aug[col][c];
    }
  }

  for (int r = 0; r < 4; ++r)
    for (int c = 0; c < 4; ++c) {
      const double v = aug[r][4 + c];
      if (!std::isfinite(v))
        return false;
      out[static_cast<size_t>(c * 4 + r)] = v;
    }
  return true;
}

static bool readMotionMatrix(const nlohmann::json &values,
                             MotionMatrixLayout layout, MotionMat4 &out,
                             std::string &err) {
  if (!values.is_array() || values.size() != 16) {
    err = "transform must contain exactly 16 numbers";
    return false;
  }
  for (int r = 0; r < 4; ++r)
    for (int c = 0; c < 4; ++c) {
      const size_t src = layout == MotionMatrixLayout::ColumnMajor
                             ? static_cast<size_t>(c * 4 + r)
                             : static_cast<size_t>(r * 4 + c);
      if (!values[src].is_number()) {
        err = "transform contains a non-numeric value";
        return false;
      }
      const double v = values[src].get<double>();
      if (!std::isfinite(v)) {
        err = "transform contains a non-finite value";
        return false;
      }
      out[static_cast<size_t>(c * 4 + r)] = v;
    }

  const double affineTol = 1e-6;
  if (std::abs(motionMatAt(out, 3, 0)) > affineTol ||
      std::abs(motionMatAt(out, 3, 1)) > affineTol ||
      std::abs(motionMatAt(out, 3, 2)) > affineTol ||
      std::abs(motionMatAt(out, 3, 3) - 1.0) > affineTol) {
    err = "transform is not an affine world transform";
    return false;
  }
  MotionMat4 inverse;
  if (!invertMotionMatrix(out, inverse)) {
    err = "transform is singular";
    return false;
  }
  return true;
}

static MotionMat4 multiplyMotionMatrices(const MotionMat4 &a,
                                         const MotionMat4 &b) {
  MotionMat4 out{};
  for (int r = 0; r < 4; ++r)
    for (int c = 0; c < 4; ++c)
      for (int k = 0; k < 4; ++k)
        out[static_cast<size_t>(c * 4 + r)] +=
            motionMatAt(a, r, k) * motionMatAt(b, k, c);
  return out;
}

// ---------- runMotion
// -------------------------------------------------------------------

void SimManager::runMotion(AppState &st, Study &s) {
  if (!st.docMirror.hasDoc()) {
    st.toast(ui::ToastLevel::Warn,
             "Open an assembly with mates before running Motion");
    return;
  }
  const std::string docId = st.docMirror.docId();
  clearTransientResult(s);
  motionDocMesh_ = render::DocMesh{};
  motionCacheDocId_.clear();
  motionComponentByBodyId_.clear();
  motionInverseBaseWorldByComponentId_.clear();
  persist(st, s);
  restoreModelMesh(st);

  nlohmann::json params = {
      {"doc_id", docId},
      {"start", s.motionStart},
      {"stop", s.motionStop},
      {"steps", std::max(2, std::min(s.motionSteps, 120))},
      {"check_interference", s.motionCheckInterference},
  };
  if (!s.motionMateId.empty())
    params["mate_id"] = s.motionMateId;

  st.status = "Motion: solving\xe2\x80\xa6";
  st.statusKind = 0;

  auto failRun = [&](const std::string &message) {
    s.motionPlaying = false;
    s.summary = "Motion failed: " + message;
    st.status = s.summary;
    st.statusKind = 2;
    st.addLog("[motion] " + s.summary);
    st.toast(ui::ToastLevel::Error, s.summary);
  };

  nlohmann::json result;
  std::string err;
  const bool ok =
      st.worker.call("simulate_motion", params, result, err, 120000);
  if (!ok || !result.is_object() || result.contains("code")) {
    failRun(result.is_object() && result.contains("message")
                ? result.value("message", err)
                : (err.empty() ? "motion solver returned no result" : err));
    return;
  }
  if (!result.value("ok", false)) {
    failRun(result.value("error", std::string("motion error")));
    return;
  }
  if (!result.contains("doc_id") || !result["doc_id"].is_string() ||
      result["doc_id"].get<std::string>() != docId) {
    failRun("solver result belongs to a different document");
    return;
  }

  // The native worker contract is column-major. Accept a trajectory only when every frame
  // converged, every stable component id is present exactly once, and every transform is finite,
  // affine, and invertible.
  const nlohmann::json trajectory =
      result.value("trajectory", nlohmann::json::array());
  std::string trajectoryError;
  std::unordered_set<std::string> expectedComponents;
  if (!trajectory.is_array() || trajectory.size() < 2)
    trajectoryError = "trajectory must contain at least two frames";
  for (size_t fi = 0; trajectoryError.empty() && fi < trajectory.size(); ++fi) {
    const auto &frameJ = trajectory[fi];
    if (!frameJ.is_object() || !frameJ.value("converged", false) ||
        !frameJ.contains("bodies") || !frameJ["bodies"].is_array() ||
        frameJ["bodies"].empty()) {
      trajectoryError = "frame " + std::to_string(fi) +
                        " is incomplete or did not converge";
      break;
    }
    std::unordered_set<std::string> frameComponents;
    for (const auto &body : frameJ["bodies"]) {
      if (!body.is_object() || !body.contains("component") ||
          !body["component"].is_string()) {
        trajectoryError = "frame " + std::to_string(fi) +
                          " has a body without a stable component id";
        break;
      }
      const std::string component = body["component"].get<std::string>();
      if (component.empty() || !frameComponents.insert(component).second) {
        trajectoryError = "frame " + std::to_string(fi) +
                          " has an empty or duplicate component id";
        break;
      }
      MotionMat4 parsed;
      std::string matrixError;
      if (!body.contains("transform") ||
          !readMotionMatrix(body["transform"],
                            MotionMatrixLayout::ColumnMajor, parsed,
                            matrixError)) {
        trajectoryError = "frame " + std::to_string(fi) + " component '" +
                          component + "': " + matrixError;
        break;
      }
    }
    if (trajectoryError.empty()) {
      if (fi == 0)
        expectedComponents = std::move(frameComponents);
      else if (frameComponents != expectedComponents)
        trajectoryError = "frame " + std::to_string(fi) +
                          " does not contain the same component ids as frame 0";
    }
  }
  if (!trajectoryError.empty()) {
    failRun(trajectoryError);
    return;
  }

  s.motionTrajectory = trajectory;
  s.motionDriverUnit = "deg";
  if (result.contains("driver") && result["driver"].is_object())
    s.motionDriverUnit = result["driver"].value("unit", std::string("deg"));
  if (result.contains("mate_id") && result["mate_id"].is_string())
    s.motionMateId = result["mate_id"].get<std::string>();

  std::string cacheError;
  if (!prepareMotionCache(st, s, cacheError)) {
    s.motionRenderError = cacheError;
    failRun("could not prepare playback geometry: " + cacheError);
    return;
  }

  // Stable ids are lookup keys; display names never participate in the render join.
  s.motionBodyIds.clear();
  s.motionBodyLabels.clear();
  for (const auto &body : s.motionTrajectory[0]["bodies"]) {
    const std::string component = body["component"].get<std::string>();
    std::string label = body.value("name", component);
    if (label.empty())
      label = component;
    if (label != component)
      label += " [" + component + "]";
    s.motionBodyIds.push_back(component);
    s.motionBodyLabels.push_back(std::move(label));
  }
  s.motionPlotBody = 0;
  buildMotionTrace(s);

  s.motionHasResult = true;
  s.hasResult = true;
  s.motionPlaying = false;
  s.motionFrame = 0;
  s.motionLastTick = -1.0;
  const int nFrames = static_cast<int>(s.motionTrajectory.size());

  std::string ifNote;
  if (s.motionCheckInterference && result.contains("interference")) {
    const auto &inf = result["interference"];
    if (inf.value("checked", false)) {
      int col = inf.value("first_collision_step", -1);
      char buf[80];
      if (col >= 0) {
        std::snprintf(buf, sizeof(buf), " | first collision step %d", col);
        ifNote = buf;
      } else {
        ifNote = " | no collisions";
      }
    }
  }
  char hdr[160];
  std::snprintf(
      hdr, sizeof(hdr), "Motion: %d frames, %.1f\xe2\x80\x93%.1f %s%s", nFrames,
      s.motionStart, s.motionStop, s.motionDriverUnit.c_str(), ifNote.c_str());
  s.summary = hdr;
  persist(st, s);

  const bool frameApplied = applyMotionFrame(st, s, 0);
  node_ = SimNode::Results;
  resultShown_ = true;
  shownStudyId_ = s.id;
  st.colormapOn = false;
  if (st.renderer)
    st.renderer->setColormapMode(false);
  st.legendOn = false;
  if (!frameApplied)
    return;

  st.status = "Motion ready \xe2\x80\x94 " + s.summary;
  st.statusKind = ifNote.empty() ? 0 : 1;
  st.addLog("[motion] " + s.summary);
  st.toast(ui::ToastLevel::Info, hdr);
}

bool SimManager::prepareMotionCache(AppState &st, Study &s,
                                    std::string &err) {
  err.clear();
  if (!st.docMirror.hasDoc()) {
    err = "no active document";
    return false;
  }
  const std::string docId = st.docMirror.docId();

  // Keep the tessellation metadata from the same activation response as the AICM path. BODY
  // stores a numeric body id and a display name, while tess.bodies carries the stable component
  // id; joining those two by body_id avoids ambiguous/renamed component labels.
  nlohmann::json activation;
  std::string callError;
  if (!st.worker.call("doc_activate", {{"doc_id", docId}}, activation,
                      callError, 60000) ||
      !activation.is_object() || activation.contains("code")) {
    err = activation.is_object()
              ? activation.value("message", callError)
              : callError;
    if (err.empty())
      err = "doc_activate returned no base mesh";
    return false;
  }
  std::string aicmPath;
  if (!st.docMirror.adoptActivateResponse(docId, activation, aicmPath,
                                          callError) ||
      aicmPath.empty()) {
    err = callError.empty() ? "activation response has no AICM path" : callError;
    return false;
  }

  render::DocMesh parsedMesh;
  std::string parseError;
  if (!render::loadAicmFile(aicmPath, parsedMesh, parseError) ||
      parsedMesh.empty()) {
    err = parseError.empty() ? "base AICM contains no bodies" : parseError;
    return false;
  }

  const auto tessIt = activation.find("tess");
  if (tessIt == activation.end() || !tessIt->is_object() ||
      !tessIt->contains("bodies") || !(*tessIt)["bodies"].is_array()) {
    err = "activation response has no stable body metadata";
    return false;
  }
  std::map<uint32_t, std::string> componentByBodyId;
  for (const auto &meta : (*tessIt)["bodies"]) {
    if (!meta.is_object() || !meta.contains("body_id") ||
        !meta["body_id"].is_number_integer() ||
        !meta.contains("component") || !meta["component"].is_string()) {
      err = "activation body metadata is malformed";
      return false;
    }
    const long long rawBodyId = meta["body_id"].get<long long>();
    const std::string component = meta["component"].get<std::string>();
    if (rawBodyId <= 0 ||
        rawBodyId > static_cast<long long>(UINT32_MAX) || component.empty() ||
        !componentByBodyId
             .emplace(static_cast<uint32_t>(rawBodyId), component)
             .second) {
      err = "activation body metadata has an invalid or duplicate stable id";
      return false;
    }
  }

  std::map<std::string, MotionMat4> baseWorld;
  const auto treeIt = activation.find("tree");
  if (treeIt == activation.end() || !treeIt->is_object() ||
      !treeIt->contains("components") ||
      !(*treeIt)["components"].is_array()) {
    err = "activation response has no assembly component transforms";
    return false;
  }
  for (const auto &componentJ : (*treeIt)["components"]) {
    if (!componentJ.is_object() || componentJ.value("suppressed", false))
      continue;
    const std::string component =
        componentJ.value("id", std::string());
    if (component.empty() || !componentJ.contains("transform"))
      continue;
    MotionMat4 world;
    std::string matrixError;
    if (!readMotionMatrix(componentJ["transform"],
                          MotionMatrixLayout::RowMajor, world,
                          matrixError)) {
      err = "base transform for component '" + component + "': " +
            matrixError;
      return false;
    }
    if (!baseWorld.emplace(component, world).second) {
      err = "assembly has duplicate component id '" + component + "'";
      return false;
    }
  }

  // Pattern-instance bodies have synthetic stable component ids and no row in tree.components.
  // When the requested sweep contains the assembly's current driven value, that solved frame is
  // the exact base pose for those instances.
  double currentDriver = 0.0;
  bool haveCurrentDriver = false;
  if (treeIt->contains("mates") && (*treeIt)["mates"].is_array()) {
    for (const auto &mate : (*treeIt)["mates"]) {
      if (!mate.is_object() ||
          mate.value("id", std::string()) != s.motionMateId ||
          !mate.contains("value"))
        continue;
      const nlohmann::json *value = &mate["value"];
      if (value->is_object() && value->contains("value"))
        value = &(*value)["value"];
      if (value->is_number()) {
        currentDriver = value->get<double>();
        haveCurrentDriver = std::isfinite(currentDriver);
      }
      break;
    }
  }
  if (haveCurrentDriver) {
    for (const auto &frameJ : s.motionTrajectory) {
      if (!frameJ.contains("value") || !frameJ["value"].is_number())
        continue;
      const double frameValue = frameJ["value"].get<double>();
      const double valueTol =
          1e-9 * std::max({1.0, std::abs(currentDriver),
                           std::abs(frameValue)});
      if (!std::isfinite(frameValue) ||
          std::abs(frameValue - currentDriver) > valueTol)
        continue;
      for (const auto &bodyJ : frameJ["bodies"]) {
        const std::string component =
            bodyJ.value("component", std::string());
        if (component.empty() || baseWorld.count(component))
          continue;
        MotionMat4 world;
        std::string matrixError;
        if (!readMotionMatrix(bodyJ["transform"],
                              MotionMatrixLayout::ColumnMajor, world,
                              matrixError)) {
          err = "base transform for component '" + component + "': " +
                matrixError;
          return false;
        }
        baseWorld.emplace(component, world);
      }
      break;
    }
  }

  size_t renderableBodies = 0;
  for (const auto &body : parsedMesh.bodies) {
    if (body.pos.empty())
      continue;
    ++renderableBodies;
    if (body.n_verts == 0 ||
        body.pos.size() != static_cast<size_t>(body.n_verts) * 3 ||
        (!body.nrm.empty() &&
         body.nrm.size() != static_cast<size_t>(body.n_verts) * 3)) {
      err = "base AICM body " + std::to_string(body.body_id) +
            " has inconsistent vertex arrays";
      return false;
    }
    const auto componentIt = componentByBodyId.find(body.body_id);
    if (componentIt == componentByBodyId.end()) {
      err = "base AICM body " + std::to_string(body.body_id) +
            " has no stable component id";
      return false;
    }
    if (!baseWorld.count(componentIt->second)) {
      err = "base transform is unavailable for component '" +
            componentIt->second + "'";
      return false;
    }
  }
  if (renderableBodies == 0) {
    err = "base AICM contains no renderable bodies";
    return false;
  }
  std::map<std::string, MotionMat4> inverseBaseWorld;
  for (const auto &entry : baseWorld) {
    MotionMat4 inverse;
    if (!invertMotionMatrix(entry.second, inverse)) {
      err = "base transform is not invertible for component '" +
            entry.first + "'";
      return false;
    }
    inverseBaseWorld.emplace(entry.first, inverse);
  }

  motionDocMesh_ = std::move(parsedMesh);
  motionCacheDocId_ = docId;
  motionComponentByBodyId_ = std::move(componentByBodyId);
  motionInverseBaseWorldByComponentId_ = std::move(inverseBaseWorld);
  return true;
}

// ---------- buildMotionTrace
// ----------------------------------------------------------- Pick the body
// with the largest world-translation span after each solve.  Assemblies list
// fixed components first, so defaulting to index zero produced a valid but
// constant trace that ImGui could not visibly auto-scale.
void SimManager::buildMotionTrace(Study &s) {
  if (s.motionBodyLabels.size() != s.motionBodyIds.size())
    s.motionBodyLabels = s.motionBodyIds;
  if (s.motionBodyIds.empty()) {
    s.motionPlotBody = 0;
    return;
  }

  int bestBody = 0;
  double bestSpan = -1.0;
  for (int i = 0; i < static_cast<int>(s.motionBodyIds.size()); ++i) {
    double minMagnitude = std::numeric_limits<double>::infinity();
    double maxMagnitude = -std::numeric_limits<double>::infinity();
    for (const auto &frameJ : s.motionTrajectory) {
      if (!frameJ.contains("bodies") || !frameJ["bodies"].is_array())
        continue;
      for (const auto &bodyJ : frameJ["bodies"]) {
        if (bodyJ.value("component", std::string()) != s.motionBodyIds[i])
          continue;
        const auto &transform = bodyJ["transform"];
        if (!transform.is_array() || transform.size() < 15)
          break;
        const double tx = transform[12].get<double>();
        const double ty = transform[13].get<double>();
        const double tz = transform[14].get<double>();
        const double magnitude = std::sqrt(tx * tx + ty * ty + tz * tz);
        minMagnitude = std::min(minMagnitude, magnitude);
        maxMagnitude = std::max(maxMagnitude, magnitude);
        break;
      }
    }
    const double span =
        std::isfinite(minMagnitude) ? maxMagnitude - minMagnitude : -1.0;
    if (span > bestSpan) {
      bestSpan = span;
      bestBody = i;
    }
  }
  s.motionPlotBody = bestBody;
}

// ---------- applyMotionFrame -----------------------------------------------------------
// CPU-transforms each cached world-space body by frameWorld * inverse(cachedBaseWorld), then
// atomically re-uploads via Renderer::setMeshes(). Native simulate_motion transforms are
// column-major 4x4 arrays (OpenGL convention); translation is T[12], T[13], T[14].

bool SimManager::applyMotionFrame(AppState &st, Study &s, int frame) {
  auto failPlayback = [&](const std::string &message) -> bool {
    s.motionPlaying = false;
    const bool firstReport = s.motionRenderError != message;
    s.motionRenderError = message;
    st.status = "Motion playback blocked: " + message;
    st.statusKind = 2;
    if (firstReport) {
      st.addLog("[motion] " + st.status);
      st.toast(ui::ToastLevel::Error, st.status);
    }
    return false;
  };

  if (!st.renderer)
    return failPlayback("renderer is unavailable");
  if (!st.docMirror.hasDoc())
    return failPlayback("the owning document is no longer active");
  if (frame < 0 || frame >= static_cast<int>(s.motionTrajectory.size()))
    return failPlayback("requested frame is outside the trajectory");

  if (motionDocMesh_.empty() ||
      motionCacheDocId_ != st.docMirror.docId()) {
    std::string cacheError;
    if (!prepareMotionCache(st, s, cacheError))
      return failPlayback("could not rebuild the base mesh: " + cacheError);
  }

  const auto &frameJ = s.motionTrajectory[frame];
  if (!frameJ.is_object() || !frameJ.contains("bodies") ||
      !frameJ["bodies"].is_array())
    return failPlayback("trajectory frame has no bodies array");

  std::map<std::string, MotionMat4> frameWorldByComponent;
  for (const auto &bodyJ : frameJ["bodies"]) {
    if (!bodyJ.is_object() || !bodyJ.contains("component") ||
        !bodyJ["component"].is_string() ||
        !bodyJ.contains("transform"))
      return failPlayback("trajectory body has no stable component transform");
    const std::string component =
        bodyJ["component"].get<std::string>();
    MotionMat4 frameWorld;
    std::string matrixError;
    if (component.empty() ||
        !readMotionMatrix(bodyJ["transform"],
                          MotionMatrixLayout::ColumnMajor, frameWorld,
                          matrixError))
      return failPlayback("component '" + component + "': " + matrixError);
    if (!frameWorldByComponent.emplace(component, frameWorld).second)
      return failPlayback("trajectory frame repeats component id '" +
                          component + "'");
  }

  // Build the whole upload before touching the renderer. Any identity, matrix, or vertex failure
  // leaves the last known-good geometry in place instead of partially corrupting the assembly.
  std::vector<Renderer::PartMesh> parts;
  parts.reserve(motionDocMesh_.bodies.size());
  for (const auto &body : motionDocMesh_.bodies) {
    if (body.pos.empty())
      continue;
    const auto componentIt =
        motionComponentByBodyId_.find(body.body_id);
    if (componentIt == motionComponentByBodyId_.end())
      return failPlayback("AICM body " + std::to_string(body.body_id) +
                          " has no stable component id");
    const std::string &component = componentIt->second;
    const auto frameIt = frameWorldByComponent.find(component);
    const auto inverseBaseIt =
        motionInverseBaseWorldByComponentId_.find(component);
    if (frameIt == frameWorldByComponent.end())
      return failPlayback("frame is missing component '" + component + "'");
    if (inverseBaseIt == motionInverseBaseWorldByComponentId_.end())
      return failPlayback("base pose is missing component '" + component + "'");

    const MotionMat4 delta =
        multiplyMotionMatrices(frameIt->second, inverseBaseIt->second);
    MotionMat4 inverseDelta;
    if (!invertMotionMatrix(delta, inverseDelta))
      return failPlayback("relative transform for component '" + component +
                          "' is non-finite or singular");

    const size_t vertexFloats = static_cast<size_t>(body.n_verts) * 3;
    if (body.n_verts == 0 || body.pos.size() != vertexFloats ||
        (!body.nrm.empty() && body.nrm.size() != vertexFloats))
      return failPlayback("AICM vertex arrays are invalid for component '" +
                          component + "'");

    Renderer::PartMesh pm;
    pm.body_id = body.body_id;
    pm.color[0] = body.color[0];
    pm.color[1] = body.color[1];
    pm.color[2] = body.color[2];
    pm.metallic = body.metallic;
    pm.roughness = body.roughness;
    pm.faces.reserve(body.faces.size());
    for (const auto &face : body.faces)
      pm.faces.push_back(
          {face.face_id, face.first_tri, face.tri_count});

    Mesh mesh;
    mesh.indices = body.idx;
    mesh.positions.resize(vertexFloats);
    mesh.normals.resize(vertexFloats, 0.0f);
    for (uint32_t vi = 0; vi < body.n_verts; ++vi) {
      const float *p = &body.pos[static_cast<size_t>(vi) * 3];
      if (!std::isfinite(p[0]) || !std::isfinite(p[1]) ||
          !std::isfinite(p[2]))
        return failPlayback("base vertices are non-finite for component '" +
                            component + "'");
      const double x = delta[0] * p[0] + delta[4] * p[1] +
                       delta[8] * p[2] + delta[12];
      const double y = delta[1] * p[0] + delta[5] * p[1] +
                       delta[9] * p[2] + delta[13];
      const double z = delta[2] * p[0] + delta[6] * p[1] +
                       delta[10] * p[2] + delta[14];
      if (!std::isfinite(x) || !std::isfinite(y) || !std::isfinite(z) ||
          std::abs(x) > FLT_MAX || std::abs(y) > FLT_MAX ||
          std::abs(z) > FLT_MAX)
        return failPlayback("transformed vertices overflow for component '" +
                            component + "'");
      const size_t out = static_cast<size_t>(vi) * 3;
      mesh.positions[out] = static_cast<float>(x);
      mesh.positions[out + 1] = static_cast<float>(y);
      mesh.positions[out + 2] = static_cast<float>(z);

      if (body.nrm.empty()) {
        mesh.normals[out + 1] = 1.0f;
        continue;
      }
      const float *n = &body.nrm[out];
      if (!std::isfinite(n[0]) || !std::isfinite(n[1]) ||
          !std::isfinite(n[2]))
        return failPlayback("base normals are non-finite for component '" +
                            component + "'");
      double nx = inverseDelta[0] * n[0] + inverseDelta[1] * n[1] +
                  inverseDelta[2] * n[2];
      double ny = inverseDelta[4] * n[0] + inverseDelta[5] * n[1] +
                  inverseDelta[6] * n[2];
      double nz = inverseDelta[8] * n[0] + inverseDelta[9] * n[1] +
                  inverseDelta[10] * n[2];
      const double normalLength = std::sqrt(nx * nx + ny * ny + nz * nz);
      if (!std::isfinite(normalLength) || normalLength <= 1e-12)
        return failPlayback("normal transform is invalid for component '" +
                            component + "'");
      nx /= normalLength;
      ny /= normalLength;
      nz /= normalLength;
      mesh.normals[out] = static_cast<float>(nx);
      mesh.normals[out + 1] = static_cast<float>(ny);
      mesh.normals[out + 2] = static_cast<float>(nz);
    }
    pm.mesh = std::move(mesh);
    parts.push_back(std::move(pm));
  }
  if (parts.empty())
    return failPlayback("base mesh contains no renderable bodies");

  st.renderer->setFramingFrozen(true);
  st.renderer->setMeshes(parts);
  st.renderer->setFramingFrozen(false);
  s.motionRenderError.clear();
  return true;
}

// ---------- scripted-shot motion helpers (CHUDWARE_SHOT=motion)
// -------------------------

bool SimManager::motionHasResult() const {
  if (sel_ < 0 || sel_ >= (int)studies_.size())
    return false;
  const Study &s = studies_[sel_];
  return s.type == StudyType::Motion && s.motionHasResult;
}

bool SimManager::flowHasResult() const {
  if (sel_ < 0 || sel_ >= (int)studies_.size())
    return false;
  const Study &s = studies_[sel_];
  return isFlowFamily(s.type) && s.hasResult;
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
  // Playback owns the viewport only while this study's Results node is visible. Otherwise
  // syncResultView has just restored the editable base mesh and no frame may overwrite it.
  if (st.activeWorkspace != AppState::Workspace::Simulate ||
      node_ != SimNode::Results) {
    s.motionLastTick = -1.0;
    return;
  }

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
  if (ImGui::IsItemDeactivatedAfterEdit())
    persist(st, s);
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
  if (!s.motionRenderError.empty()) {
    const auto &pal = ui::palette;
    ImGui::PushStyleColor(
        ImGuiCol_Text,
        ImVec4(pal.statusErr[0], pal.statusErr[1], pal.statusErr[2], 1.0f));
    ImGui::TextWrapped("Playback blocked: %s", s.motionRenderError.c_str());
    ImGui::PopStyleColor();
    ImGui::Spacing();
  }

  drawMotionTimeline(st, s);
  ImGui::Spacing();

  // Per-component position trace (|world translation| vs frame index). Stable component ids
  // select trajectory rows; display names are labels only.
  if (!s.motionBodyIds.empty()) {
    ImGui::PushFont(st.fonts.medium);
    ImGui::TextUnformatted("Position trace");
    ImGui::PopFont();

    const int nBodies = static_cast<int>(s.motionBodyIds.size());
    if (nBodies > 1) {
      const char *preview =
          s.motionPlotBody >= 0 && s.motionPlotBody < nBodies &&
                  s.motionPlotBody <
                      static_cast<int>(s.motionBodyLabels.size())
              ? s.motionBodyLabels[s.motionPlotBody].c_str()
              : "(none)";
      ImGui::SetNextItemWidth(-FLT_MIN);
      if (ImGui::BeginCombo("##motionPlotBody", preview)) {
        for (int i = 0; i < nBodies; ++i) {
          const bool selected = i == s.motionPlotBody;
          const char *label =
              i < static_cast<int>(s.motionBodyLabels.size())
                  ? s.motionBodyLabels[i].c_str()
                  : s.motionBodyIds[i].c_str();
          ImGui::PushID(s.motionBodyIds[i].c_str());
          if (ImGui::Selectable(label, selected))
            s.motionPlotBody = i;
          if (selected)
            ImGui::SetItemDefaultFocus();
          ImGui::PopID();
        }
        ImGui::EndCombo();
      }
      simTip("Component whose world-position magnitude to plot vs frame.");
    }

    const std::string plotComponent =
        s.motionPlotBody >= 0 && s.motionPlotBody < nBodies
            ? s.motionBodyIds[s.motionPlotBody]
            : std::string();
    std::vector<float> trace;
    trace.reserve(nFrames);
    bool completeTrace = !plotComponent.empty();
    for (const auto &frameJ : s.motionTrajectory) {
      bool found = false;
      if (frameJ.contains("bodies") && frameJ["bodies"].is_array()) {
        for (const auto &bodyJ : frameJ["bodies"]) {
          if (bodyJ.value("component", std::string()) != plotComponent)
            continue;
          const auto &transform = bodyJ["transform"];
          if (!transform.is_array() || transform.size() < 15)
            break;
          const double tx = transform[12].get<double>();
          const double ty = transform[13].get<double>();
          const double tz = transform[14].get<double>();
          trace.push_back(static_cast<float>(
              std::sqrt(tx * tx + ty * ty + tz * tz)));
          found = true;
          break;
        }
      }
      if (!found) {
        completeTrace = false;
        break;
      }
    }
    if (completeTrace && !trace.empty()) {
      const auto [minIt, maxIt] =
          std::minmax_element(trace.begin(), trace.end());
      float minPlot = *minIt;
      float maxPlot = *maxIt;
      if (maxPlot - minPlot < 1e-5f) {
        const float pad = std::max(std::fabs(maxPlot) * 0.05f, 1.0f);
        minPlot -= pad;
        maxPlot += pad;
      }
      const float plotH = 60.0f * S;
      ImGui::PlotLines("##motiontrace", trace.data(),
                       static_cast<int>(trace.size()), 0, "|pos| (mm)",
                       minPlot, maxPlot, ImVec2(-FLT_MIN, plotH));
      if (nFrames > 1) {
        ImVec2 rMin = ImGui::GetItemRectMin(), rMax = ImGui::GetItemRectMax();
        float xCur = rMin.x + (rMax.x - rMin.x) *
                                  (static_cast<float>(s.motionFrame) /
                                   static_cast<float>(nFrames - 1));
        ImGui::GetWindowDrawList()->AddLine(
            ImVec2(xCur, rMin.y), ImVec2(xCur, rMax.y),
            simRoleU32(ui::palette.statusWarn, 0.784f), 1.5f * S);
      }
    } else {
      kit::dimText(
          "Position samples are unavailable for the selected component.");
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
  char buf[256];
  std::snprintf(
      buf, sizeof(buf),
      "Provenance: %s \xe2\x80\x94 from %s (aicad.simhandoff/1, id %s)",
      coupling.c_str(), src.c_str(), env.value("id", std::string("?")).c_str());
  kit::dimText(buf);
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
        !r.is_object() || r.contains("code") || !r.value("ok", true)) {
      st.addLog("[sim] export STEP failed: " +
                (r.is_object()
                     ? r.value("message", r.value("error", e))
                     : e));
      st.toast(ui::ToastLevel::Error, "Simulation: export failed");
      return;
    }
    lastStepPath_ = r.value("path", stepPath);
  }

  // Fixed-temperature (Dirichlet) faces. Refuse stale selectors instead of
  // silently solving a different boundary-value problem.
  nlohmann::json fixtures = nlohmann::json::array();
  for (const auto &pck : s.fixtures.picks) {
    std::string e;
    auto fp = facePointsFor(st, pck.selector, e);
    if (fp.is_null() || !fp.contains("points") || fp["points"].empty()) {
      st.toast(ui::ToastLevel::Error,
               "A fixed-temperature face no longer resolves; reselect it");
      st.addLog("[sim] thermal fixture resolution failed: " + e);
      return;
    }
    fp["temp_c"] = s.thermFixTempC;
    fixtures.push_back(fp);
  }
  // Flux / convection loads.
  nlohmann::json loads = nlohmann::json::array();
  for (const auto &ld : s.loads) {
    nlohmann::json faces;
    for (const auto &pck : ld.faces.picks) {
      std::string e;
      auto fp = facePointsFor(st, pck.selector, e);
      if (fp.is_null() || !fp.contains("points") || fp["points"].empty()) {
        st.toast(ui::ToastLevel::Error,
                 "A thermal load face no longer resolves; reselect it");
        st.addLog("[sim] thermal load resolution failed: " + e);
        return;
      }
      if (faces.is_null())
        faces = fp;
      else
        for (auto &pt : fp["points"])
          faces["points"].push_back(pt);
    }
    if (faces.is_null() || faces["points"].empty()) {
      st.toast(ui::ToastLevel::Error,
               "Every thermal load needs at least one resolved face");
      return;
    }
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

  // studyReady guarantees a library-resolved material with a positive density
  // for transient conduction; never substitute an arbitrary material.
  const int mi = materialIndexByKey(s.materialKey);
  if (mi < 0 || (s.thermTransient && materials()[mi].density_g_cm3 <= 0)) {
    st.toast(ui::ToastLevel::Error,
             "Thermal material is missing required density data");
    return;
  }
  const double density = materials()[mi].density_g_cm3;
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
  addMeshParams(study, s, /*order=*/1, /*allowAdvisor=*/true);

  std::string note;
  if (sim->launch("sim_run_thermal", {{"study", study}}, note,
                  st.docMirror.docId())) {
    clearTransientResult(s);
    persist(st, s);
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
  clearTransientResult(s);
  persist(st, s);
  resultShown_ = false;
  shownStudyId_.clear();
  advRunning_ = true;
  s.summary = "Solving (CalculiX)\xe2\x80\xa6";
  const std::string sid = s.id;
  const std::string docId = st.docMirror.docId();
  st.rpc.call(
      "sim_run_buckling", params,
      [this, &st, sid, docId](const core::RpcResult &r) {
        advRunning_ = false;
        for (auto &stu : studies_) {
          if (stu.id != sid)
            continue;
          auto fail = [&](std::string message) {
            stu.hasResult = false;
            stu.summary = std::move(message);
            persist(st, docId, stu);
            st.addLog("[sim] " + stu.summary);
            if (simOriginActive(st, this, docId)) {
              st.status = stu.summary;
              st.statusKind = 2;
              st.toast(ui::ToastLevel::Error, stu.summary);
            }
          };
          if (!r.ok || !r.result.is_object()) {
            fail("Buckling run failed: " +
                 (r.err.empty() ? std::string("worker error") : r.err));
            return;
          }
          if (!simJsonBool(r.result, "ok", false)) {
            fail("Buckling: " +
                 simJsonText(r.result, "error", "reason", "solve failed"));
            return;
          }
          double l0 = 0.0, applied = 0.0;
          if (!r.result.contains("load_factors") ||
              !r.result["load_factors"].is_array() ||
              r.result["load_factors"].empty() ||
              !r.result["load_factors"][0].is_number() ||
              !std::isfinite(
                  l0 = r.result["load_factors"][0].get<double>()) ||
              l0 <= 0.0 ||
              !finiteNumber(r.result, "applied_load_n", applied) ||
              applied <= 0.0) {
            fail(
                "Buckling run failed: solver returned no finite positive mode");
            return;
          }
          stu.advResult = r.result;
          stu.advisor = parseAdvisor(r.result);
          stu.hasResult = true;
          char buf[160];
          std::snprintf(buf, sizeof(buf),
                        "lowest load factor %.3g (critical %.4g N)", l0,
                        l0 * applied);
          stu.summary = buf;
          persist(st, docId, stu);
          node_ = SimNode::Results;
          st.addLog("[sim] buckling done: " + stu.summary);
          if (simOriginActive(st, this, docId)) {
            st.status = "Buckling complete";
            st.statusKind = 0;
          }
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
  clearTransientResult(s);
  persist(st, s);
  resultShown_ = false;
  shownStudyId_.clear();
  advRunning_ = true;
  s.summary = "Solving (CalculiX)\xe2\x80\xa6";
  const std::string sid = s.id;
  const std::string docId = st.docMirror.docId();
  st.rpc.call(
      "sim_run_contact", params,
      [this, &st, sid, docId](const core::RpcResult &r) {
        advRunning_ = false;
        for (auto &stu : studies_) {
          if (stu.id != sid)
            continue;
          auto fail = [&](std::string message) {
            stu.hasResult = false;
            stu.summary = std::move(message);
            persist(st, docId, stu);
            st.addLog("[sim] " + stu.summary);
            if (simOriginActive(st, this, docId)) {
              st.status = stu.summary;
              st.statusKind = 2;
              st.toast(ui::ToastLevel::Error, stu.summary);
            }
          };
          if (!r.ok || !r.result.is_object()) {
            fail("Contact run failed: " +
                 (r.err.empty() ? std::string("worker error") : r.err));
            return;
          }
          if (!simJsonBool(r.result, "ok", false)) {
            fail("Contact: " +
                 simJsonText(r.result, "error", "reason", "solve failed"));
            return;
          }
          double applied = 0.0, reaction = 0.0;
          if (!finiteNumber(r.result, "applied_load_n", applied) ||
              applied <= 0.0 ||
              !finiteNumber(r.result, "base_reaction_n", reaction)) {
            fail(
                "Contact run failed: solver returned no finite reaction audit");
            return;
          }
          stu.advResult = r.result;
          stu.advisor = parseAdvisor(r.result);
          stu.hasResult = true;
          char buf[160];
          std::snprintf(buf, sizeof(buf),
                        "applied %.4g N | base reaction %.4g N", applied,
                        reaction);
          stu.summary = buf;
          persist(st, docId, stu);
          node_ = SimNode::Results;
          st.addLog("[sim] contact done: " + stu.summary);
          if (simOriginActive(st, this, docId)) {
            st.status = "Contact complete";
            st.statusKind = 0;
          }
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
                                  const nlohmann::json &modalRes,
                                  const std::string &docId) {
  s.hasResult = false;
  auto failNow = [&](std::string message) {
    s.summary = std::move(message);
    persist(st, docId, s);
    st.addLog("[sim] " + s.summary);
    if (simOriginActive(st, this, docId)) {
      st.status = s.summary;
      st.statusKind = 2;
      st.toast(ui::ToastLevel::Error, s.summary);
    }
  };
  if (!modalRes.contains("mode_shapes") ||
      !modalRes["mode_shapes"].is_array() || modalRes["mode_shapes"].empty() ||
      !modalRes.contains("freqs_hz") || !modalRes["freqs_hz"].is_array() ||
      modalRes["freqs_hz"].empty()) {
    failNow("Vibration: modal solve returned no usable mode shapes");
    return;
  }
  const int modalNodes = modalRes.value("n_nodes", 0);
  const size_t expectedShapeValues =
      modalNodes > 0
          ? modalRes["freqs_hz"].size() * (size_t)modalNodes * 3u
          : 0u;
  bool finiteModalInput =
      expectedShapeValues > 0 &&
      modalRes["mode_shapes"].size() == expectedShapeValues;
  for (const auto &v : modalRes["freqs_hz"])
    finiteModalInput =
        finiteModalInput && v.is_number() &&
        std::isfinite(v.is_number() ? v.get<double>() : 0.0) &&
        (v.is_number() ? v.get<double>() >= 0.0 : false);
  for (const auto &v : modalRes["mode_shapes"])
    finiteModalInput =
        finiteModalInput && v.is_number() &&
        std::isfinite(v.is_number() ? v.get<double>() : 0.0);
  if (!finiteModalInput) {
    failNow("Vibration: modal basis dimensions or values are invalid");
    return;
  }
  // Part mass from the solid volume at the material density (eval_mass, same
  // source the MCP tool uses). Sync call but sub-second (pure B-rep volume
  // integral).
  double massKg = 0.0;
  int mi = materialIndexByKey(s.materialKey);
  const double dens = (mi >= 0) ? materials()[mi].density_g_cm3 : 0.0;
  if (dens > 0 && !docId.empty()) {
    nlohmann::json r,
        p = {{"doc_id", docId}, {"density_g_cm3", dens}};
    std::string e;
    double massG = 0.0;
    if (st.worker.call("eval_mass", p, r, e, 15000) && r.is_object() &&
        !r.contains("code") && r.value("ok", true) &&
        finiteNumber(r, "mass_g", massG))
      massKg = massG / 1000.0;
  }
  if (massKg <= 0.0) {
    failNow("Vibration: part mass unknown (set a material with density)");
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
      [this, &st, sid, docId](const core::RpcResult &r) {
        advRunning_ = false;
        for (auto &stu : studies_) {
          if (stu.id != sid)
            continue;
          auto fail = [&](std::string message) {
            stu.hasResult = false;
            stu.summary = std::move(message);
            persist(st, docId, stu);
            st.addLog("[sim] " + stu.summary);
            if (simOriginActive(st, this, docId)) {
              st.status = stu.summary;
              st.statusKind = 2;
              st.toast(ui::ToastLevel::Error, stu.summary);
            }
          };
          if (!r.ok || !r.result.is_object() ||
              !simJsonBool(r.result, "ok", false)) {
            fail("Vibration post failed: " +
                 (r.ok && r.result.is_object()
                      ? simJsonText(r.result, "error", "reason", "worker error")
                      : (r.err.empty() ? std::string("worker error")
                                       : r.err)));
            return;
          }
          stu.frSweep.clear();
          stu.frAccel.clear();
          if (!r.result.contains("harmonic") ||
              !r.result["harmonic"].is_object()) {
            fail("Vibration post failed: no harmonic response returned");
            return;
          }
          const auto &hm = r.result["harmonic"];
          const auto sweep =
              hm.value("sweep_hz", nlohmann::json::array());
          const auto accel =
              hm.value("accel_g", nlohmann::json::array());
          if (!sweep.is_array() || !accel.is_array() || sweep.empty() ||
              sweep.size() != accel.size()) {
            fail(
                "Vibration post failed: response arrays are empty or mismatched");
            return;
          }
          for (size_t i = 0; i < sweep.size(); ++i) {
            if (!sweep[i].is_number() || !accel[i].is_number() ||
                !std::isfinite(sweep[i].get<double>()) ||
                !std::isfinite(accel[i].get<double>()) ||
                sweep[i].get<double>() < 0.0 ||
                accel[i].get<double>() < 0.0 ||
                (i > 0 &&
                 sweep[i].get<double>() <= sweep[i - 1].get<double>())) {
              fail("Vibration post failed: response values are invalid or "
                   "frequencies are not increasing");
              return;
            }
            stu.frSweep.push_back((float)sweep[i].get<double>());
            stu.frAccel.push_back((float)accel[i].get<double>());
          }
          double peakAccel = 0.0, peakFreq = 0.0;
          if (!finiteNumber(hm, "peak_accel_g", peakAccel) ||
              !finiteNumber(hm, "peak_freq_hz", peakFreq) ||
              peakAccel < 0.0 || peakFreq < stu.frSweep.front() ||
              peakFreq > stu.frSweep.back()) {
            fail("Vibration post failed: peak response is invalid");
            return;
          }
          stu.advResult = r.result;
          stu.advisor = parseAdvisor(r.result);
          char buf[160];
          std::snprintf(buf, sizeof(buf),
                        "peak %.2f g @ %.1f Hz (zeta %.3g)", peakAccel,
                        peakFreq, stu.frZeta);
          stu.summary = buf;
          stu.hasResult = true;
          persist(st, docId, stu);
          node_ = SimNode::Results;
          st.addLog("[sim] frequency response done: " + stu.summary);
          if (simOriginActive(st, this, docId)) {
            st.status = "Vibration response complete";
            st.statusKind = 0;
          }
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
      double P = 0.0;
      finiteNumber(res, "applied_load_n", P);
      for (int i = 0; i < (int)fac.size(); ++i) {
        if (!fac[i].is_number() || !std::isfinite(fac[i].get<double>()))
          continue;
        const double factor = fac[i].get<double>();
        ImGui::TableNextRow();
        ImGui::TableSetColumnIndex(0);
        ImGui::Text("%d", i + 1);
        ImGui::TableSetColumnIndex(1);
        ImGui::Text("%.4g", factor);
        ImGui::TableSetColumnIndex(2);
        ImGui::Text("%.4g", factor * P);
      }
      ImGui::EndTable();
    }
    kit::dimText(res.value("note", std::string()).c_str());
  } else if (s.type == StudyType::Contact && res.is_object()) {
    ImGui::SeparatorText("Contact conservation check");
    double applied = 0.0, reaction = 0.0;
    const bool haveApplied = finiteNumber(res, "applied_load_n", applied);
    const bool haveReaction =
        finiteNumber(res, "base_reaction_n", reaction);
    if (haveApplied)
      ImGui::Text("Applied load: %.4g N", applied);
    if (haveReaction)
      ImGui::Text("Base reaction: %.4g N", reaction);
    if (!haveApplied || !haveReaction)
      ImGui::TextDisabled("Reaction audit unavailable in this saved result.");
    kit::dimText(res.value("note", std::string()).c_str());
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
      kit::dimText(f.c_str());
    }
    kit::dimText(res.value("damping_assumption", std::string()).c_str());
    kit::dimText(
        "Random (PSD) response needs a qualification-spec PSD — run "
        "run_fea_frequency_response via the copilot with psd_breakpoints.");
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
    kit::dimText(
        "The temperature field colormap overlay is not packaged by the "
        "conduction solver yet — numeric summary + flux audit only.");
  }
  for (const auto &n : s.solverNotes) {
    kit::dimText(n.c_str());
  }
  drawAdvisorDisclosure(st, s);
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
  const std::string &probe = el_.probes[idx];
  const auto results =
      el_.raw.contains("results") && el_.raw["results"].is_object()
          ? el_.raw["results"]
          : nlohmann::json::object();
  const nlohmann::json *x = nullptr;
  const nlohmann::json *y = nullptr;
  bool milliseconds = false, decibels = false;
  if (el_.resultAnalysis == 3 && results.contains("tran") &&
      results["tran"].is_object()) {
    const auto &blk = results["tran"];
    if (blk.contains("x_t") && blk["x_t"].is_array() &&
        blk.contains("traces") && blk["traces"].is_object() &&
        blk["traces"].contains(probe)) {
      x = &blk["x_t"];
      y = &blk["traces"][probe];
      milliseconds = true;
    }
  } else if (el_.resultAnalysis == 1 && results.contains("dc") &&
             results["dc"].is_object()) {
    const auto &blk = results["dc"];
    if (blk.contains("x") && blk["x"].is_array() &&
        blk.contains("traces") && blk["traces"].is_object() &&
        blk["traces"].contains(probe)) {
      x = &blk["x"];
      y = &blk["traces"][probe];
    }
  } else if (el_.resultAnalysis == 2 && results.contains("ac") &&
             results["ac"].is_object()) {
    const auto &blk = results["ac"];
    if (blk.contains("x_f") && blk["x_f"].is_array() &&
        blk.contains("mag") && blk["mag"].is_object() &&
        blk["mag"].contains(probe)) {
      x = &blk["x_f"];
      y = &blk["mag"][probe];
      decibels = true;
    }
  }
  if (!x || !y || !y->is_array() || x->empty() || x->size() != y->size())
    return;
  for (size_t i = 0; i < x->size(); ++i) {
    if (!(*x)[i].is_number() || !(*y)[i].is_number())
      break;
    double xv = (*x)[i].get<double>(), yv = (*y)[i].get<double>();
    if (!std::isfinite(xv) || !std::isfinite(yv) ||
        (decibels && yv < 0.0))
      break;
    el_.xs.push_back((float)(milliseconds ? xv * 1e3 : xv));
    el_.ys.push_back(
        (float)(decibels ? 20.0 * std::log10(std::max(yv, 1e-12)) : yv));
  }
  if (el_.xs.size() != x->size()) {
    el_.xs.clear();
    el_.ys.clear();
  }
}

const char *SimManager::electronicsRunBlocker(bool dispatchText) const {
  if (el_.analysis == 1) {
    const double span = (double)el_.dcStop - el_.dcStart;
    if (!el_.dcSource[0])
      return "DC sweep needs a voltage/current source name.";
    if (!std::isfinite(el_.dcStart) || !std::isfinite(el_.dcStop) ||
        !std::isfinite(el_.dcStep) || el_.dcStep == 0.0f || span == 0.0 ||
        span * el_.dcStep < 0.0)
      return dispatchText
                 ? "DC sweep needs a non-zero step directed from start toward stop."
                 : "DC step must be non-zero and directed from start toward stop.";
    if (std::fabs(span / el_.dcStep) > 1000000.0)
      return "DC sweep exceeds the one-million-point solver limit.";
  } else if (el_.analysis == 2 &&
             (!std::isfinite(el_.acStartHz) ||
              !std::isfinite(el_.acStopHz) || el_.acStartHz <= 0.0f ||
              el_.acStopHz <= el_.acStartHz || el_.acPoints < 2)) {
    return "AC sweep needs positive increasing frequencies and at least 2 points.";
  } else if (el_.analysis == 3 &&
             (!std::isfinite(el_.tranStopMs) ||
              !std::isfinite(el_.tranStepUs) || el_.tranStopMs <= 0.0f ||
              el_.tranStepUs <= 0.0f ||
              el_.tranStepUs * 1e-3f >= el_.tranStopMs)) {
    return "Transient stop time must exceed a positive finite time step.";
  }
  return nullptr;
}

void SimManager::runElectronics(AppState &st) {
  if (elRunning_ || el_.docTab < 0 ||
      el_.docTab >= (int)st.docTabs.size() ||
      st.docTabs[el_.docTab].kind != AppState::DocTab::TabKind::Schematic ||
      st.docTabs[el_.docTab].ecadDocId.empty()) {
    st.toast(ui::ToastLevel::Warn, "Select an open schematic first");
    return;
  }
  if (const char *blocker = electronicsRunBlocker(true)) {
    st.toast(ui::ToastLevel::Warn, blocker);
    return;
  }
  const std::string docId = st.docTabs[el_.docTab].ecadDocId;
  const std::string docTitle = st.docTabs[el_.docTab].displayTitle();
  const int analysis = el_.analysis;
  nlohmann::json analyses = nlohmann::json::array();
  if (analysis == 0)
    analyses.push_back({{"type", "op"}});
  else if (analysis == 1)
    analyses.push_back({{"type", "dc"},
                        {"source", std::string(el_.dcSource)},
                        {"start", el_.dcStart},
                        {"stop", el_.dcStop},
                        {"step", el_.dcStep}});
  else if (analysis == 2)
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
  el_.resultAnalysis = analysis;
  el_.resultDocId = docId;
  el_.resultDocTitle = docTitle;
  el_.error.clear();
  el_.headline.clear();
  el_.raw = nlohmann::json::object();
  el_.probes.clear();
  el_.xs.clear();
  el_.ys.clear();
  st.rpc.call(
      "ecad_simulate", {{"doc_id", docId}, {"study", {{"analyses", analyses}}}},
      [this, docId, docTitle, analysis](const core::RpcResult &r) {
        elRunning_ = false;
        if (el_.resultDocId != docId || el_.resultAnalysis != analysis)
          return;
        // Keep the worker payload for diagnostics even on a failed solve. A populated
        // results object is not evidence of success: SPICE may return its last iterate
        // while explicitly reporting non-convergence.
        if (r.ok && r.result.is_object())
          el_.raw = r.result;
        const bool converged =
            r.ok && r.result.is_object() &&
            r.result.contains("converged") &&
            r.result["converged"].is_boolean() &&
            r.result["converged"].get<bool>();
        if (!r.ok || !r.result.is_object() || r.result.contains("code") ||
            !r.result.value("ok", true) || !converged) {
          el_.result = 0;
          el_.error =
              r.ok && r.result.is_object()
                  ? r.result.value(
                        "error",
                        r.result.value(
                            "message",
                            r.result.value(
                                "hint",
                                std::string("circuit solver did not converge"))))
                  : (r.err.empty() ? "simulation returned no result" : r.err);
          if (el_.error.empty())
            el_.error = converged ? "simulation failed"
                                  : "circuit solver did not converge";
          return;
        }
        const auto results =
            r.result.contains("results") && r.result["results"].is_object()
                ? r.result["results"]
                : nlohmann::json::object();
        static const char *keys[4] = {"op", "dc", "ac", "tran"};
        const char *key = keys[(analysis < 0 || analysis > 3) ? 0 : analysis];
        if (!results.contains(key) || !results[key].is_object() ||
            results[key].empty()) {
          el_.result = 0;
          el_.error =
              std::string("solver returned no ") + key + " analysis payload";
          return;
        }
        el_.raw = r.result;
        el_.result = 1;
        el_.resultDocTitle = docTitle;
        el_.headline = "Converged";
        el_.probes.clear();
        const auto &blk = results[key];
        const char *traceKey =
            analysis == 2 ? "mag" : (analysis == 0 ? nullptr : "traces");
        if (traceKey && blk.contains(traceKey) &&
            blk[traceKey].is_object())
          for (auto it = blk[traceKey].begin(); it != blk[traceKey].end();
               ++it)
            el_.probes.push_back(it.key());
        std::sort(el_.probes.begin(), el_.probes.end());
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
    kit::dimText(
        "No schematic open. SPICE simulation runs on a Schematic document's "
        "netlist \xe2\x80\x94 open the board's schematic tab (a PCB alone has "
        "no simulatable devices).");
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

  static const char *kAn[4] = {"Operating point", "DC sweep", "AC sweep",
                               "Transient"};
  if (el_.analysis < 0 || el_.analysis > 3)
    el_.analysis = 3;
  ImGui::SetNextItemWidth(-FLT_MIN);
  if (ImGui::BeginCombo("##elan", kAn[el_.analysis])) {
    for (int i = 0; i < 4; ++i)
      if (ImGui::Selectable(kAn[i], el_.analysis == i))
        el_.analysis = i;
    ImGui::EndCombo();
  }
  if (el_.analysis == 1) {
    ImGui::SetNextItemWidth(-FLT_MIN);
    ImGui::InputTextWithHint("##eldcsrc", "source name (e.g. V1)",
                             el_.dcSource, sizeof(el_.dcSource));
    ImGui::SetNextItemWidth(-FLT_MIN);
    ImGui::InputFloat("##eldcstart", &el_.dcStart, 0, 0, "start = %.6g");
    ImGui::SetNextItemWidth(-FLT_MIN);
    ImGui::InputFloat("##eldcstop", &el_.dcStop, 0, 0, "stop = %.6g");
    ImGui::SetNextItemWidth(-FLT_MIN);
    ImGui::InputFloat("##eldcstep", &el_.dcStep, 0, 0, "step = %.6g");
  } else if (el_.analysis == 2) {
    ImGui::SetNextItemWidth(-FLT_MIN);
    ImGui::InputFloat("##elfstart", &el_.acStartHz, 0, 0, "f_start = %.0f Hz");
    ImGui::SetNextItemWidth(-FLT_MIN);
    ImGui::InputFloat("##elfstop", &el_.acStopHz, 0, 0, "f_stop = %.0f Hz");
    ImGui::SetNextItemWidth(-FLT_MIN);
    ImGui::InputInt("##elacn", &el_.acPoints, 0, 0);
    simTip("Frequency sample count.");
  } else if (el_.analysis == 3) {
    ImGui::SetNextItemWidth(-FLT_MIN);
    ImGui::InputFloat("##eltstop", &el_.tranStopMs, 0, 0, "t_stop = %.2f ms");
    ImGui::SetNextItemWidth(-FLT_MIN);
    ImGui::InputFloat("##eltstep", &el_.tranStepUs, 0, 0, "t_step = %.1f us");
  }

  const char *elBlocker = electronicsRunBlocker(false);

  ImGui::Spacing();
  if (elRunning_ || elBlocker) {
    ImGui::BeginDisabled();
    ImGui::Button(elRunning_ ? "Running\xe2\x80\xa6" : "Run",
                  ImVec2(-FLT_MIN, 0));
    ImGui::EndDisabled();
  } else if (ImGui::Button("Run", ImVec2(-FLT_MIN, 0))) {
    runElectronics(st);
  }
  if (elBlocker)
    ImGui::TextWrapped("%s", elBlocker);
  simTip("Build the SPICE netlist from the schematic and solve (async). Probes "
         "default to every node voltage + source current.");

  ImGui::Spacing();
  ImGui::Separator();
  const std::string currentDocId = st.docTabs[el_.docTab].ecadDocId;
  const bool resultMatches =
      el_.resultDocId.empty() || el_.resultDocId == currentDocId;
  if (!resultMatches && el_.result >= 0) {
    ImGui::TextWrapped("Last result belongs to %s. Run this schematic to replace it.",
                       el_.resultDocTitle.c_str());
  } else if (el_.result == 0) {
    ImGui::TextWrapped("Failed: %s", el_.error.c_str());
  } else if (el_.result == 1) {
    ImGui::TextUnformatted(el_.headline.c_str());
    if (el_.resultAnalysis >= 0 && el_.resultAnalysis < 4)
      ImGui::TextDisabled("%s \xe2\x80\x94 %s",
                          el_.resultDocTitle.c_str(),
                          kAn[el_.resultAnalysis]);
    const auto results =
        el_.raw.contains("results") && el_.raw["results"].is_object()
            ? el_.raw["results"]
            : nlohmann::json::object();
    if (el_.resultAnalysis == 0 && results.contains("op") &&
        results["op"].is_object()) {
      ImGui::SeparatorText("Operating point");
      for (auto it = results["op"].begin(); it != results["op"].end(); ++it)
        if (it.value().is_number() &&
            std::isfinite(it.value().get<double>()))
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
      if (!el_.xs.empty() && el_.xs.size() == el_.ys.size()) {
        const bool ac = el_.resultAnalysis == 2;
        const bool tran = el_.resultAnalysis == 3;
        drawEmLinePlot("##elplot", el_.xs, &el_.ys,
                       ac ? "mag (dB)" : "V / I", nullptr, nullptr,
                       ac ? "f (Hz)" : (tran ? "t (ms)" : "source"),
                       ac ? "dB" : "value");
      } else {
        ImGui::TextDisabled(
            "Selected trace is empty or contains non-finite samples.");
      }
    }
    const auto convergence =
        el_.raw.contains("convergence") && el_.raw["convergence"].is_object()
            ? el_.raw["convergence"]
            : nlohmann::json::object();
    for (auto it = convergence.begin(); it != convergence.end(); ++it)
      if (it.value().is_boolean() && !it.value().get<bool>())
        ImGui::TextWrapped("Convergence warning: %s did not converge.",
                           it.key().c_str());
    for (const char *key : {"warnings", "log"})
      if (el_.raw.contains(key) && el_.raw[key].is_array())
        for (const auto &line : el_.raw[key])
          if (line.is_string())
            ImGui::TextWrapped("%s", line.get<std::string>().c_str());
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
  if (!std::isfinite(fwsim_.durationMs) || fwsim_.durationMs <= 0.0f ||
      fwsim_.durationMs > 600000.0f) {
    st.toast(ui::ToastLevel::Warn,
             "Emulation duration must be between 0 and 600000 ms");
    return;
  }
  nlohmann::json p{{"project_id", fwsim_.projectIds[fwsim_.projIdx]}};
  const double timeoutS = fwsim_.durationMs * 0.001 + 5.0;
  p["timeout_s"] = timeoutS;
  const int rpcTimeoutMs =
      (int)std::ceil((timeoutS + 5.0) * 1000.0);
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
        if (!r.ok || !r.result.is_object() || r.result.contains("code") ||
            !r.result.value("ok", true)) {
          fwsim_.result = 0;
          fwsim_.error =
              r.ok && r.result.is_object()
                  ? r.result.value(
                        "error",
                        r.result.value("message",
                                       std::string("emulation failed")))
                  : (r.err.empty() ? "emulation returned no result" : r.err);
          return;
        }
        double elapsedMs = 0.0;
        if (!r.result.contains("passed") ||
            !r.result["passed"].is_boolean() ||
            !finiteNumber(r.result, "ms", elapsedMs) || elapsedMs < 0.0) {
          fwsim_.result = 0;
          fwsim_.error =
              "emulation returned an incomplete or non-finite result";
          return;
        }
        const bool passed = r.result["passed"].get<bool>();
        fwsim_.result = passed ? 1 : 0;
        char buf2[96];
        std::snprintf(buf2, sizeof(buf2), "%s  (%.0f ms emulated)",
                      passed ? "PASSED" : "NOT PASSED", elapsedMs);
        fwsim_.headline = buf2;
        if (r.result.contains("assertion_results") &&
            r.result["assertion_results"].is_array())
          for (const auto &a : r.result["assertion_results"]) {
            if (!a.is_object())
              continue;
            const bool assertionPassed =
                a.contains("passed") && a["passed"].is_boolean() &&
                a["passed"].get<bool>();
            std::string line = assertionPassed ? "PASS  " : "FAIL  ";
            if (a.contains("assertion") && a["assertion"].is_string())
              line += a["assertion"].get<std::string>();
            fwsim_.uart.push_back(std::move(line));
          }
        const std::string uart =
            r.result.contains("uart_log") && r.result["uart_log"].is_string()
                ? r.result["uart_log"].get<std::string>()
                : std::string();
        if (!uart.empty()) {
          std::istringstream is(uart);
          std::string ln;
          while (std::getline(is, ln))
            fwsim_.uart.push_back(ln);
        }
      },
      rpcTimeoutMs);
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
    kit::dimText(fwHint_.empty() ? "Emulation backend unavailable \xe2\x80\x94 "
                                   "install Renode (preferred) or "
                                   "qemu-system-arm, then Recheck."
                                 : fwHint_.c_str());
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
    kit::dimText("No firmware projects yet \xe2\x80\x94 create one in the "
                 "Firmware workspace (its New Project rail), then come back.");
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
  ImGui::SetNextItemWidth(-FLT_MIN);
  ImGui::DragFloat("##fwduration", &fwsim_.durationMs, 10.0f, 1.0f,
                   600000.0f, "run = %.0f ms");
  simTip("Requested target emulation time. The backend adds a five-second "
         "startup/shutdown grace period.");
  ImGui::Spacing();
  const bool badDuration =
      !std::isfinite(fwsim_.durationMs) || fwsim_.durationMs <= 0.0f ||
      fwsim_.durationMs > 600000.0f;
  if (fwRunning_ || badDuration) {
    ImGui::BeginDisabled();
    ImGui::Button(fwRunning_ ? "Running\xe2\x80\xa6" : "Run emulation",
                  ImVec2(-FLT_MIN, 0));
    ImGui::EndDisabled();
  } else if (ImGui::Button("Run emulation", ImVec2(-FLT_MIN, 0))) {
    runEmulation(st);
  }
  if (badDuration)
    ImGui::TextWrapped("Duration must be between 0 and 600000 ms.");
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

static bool handoffFiniteNumber(const nlohmann::json &row, const char *key,
                                double *out = nullptr) {
  if (!row.is_object() || !row.contains(key) || !row[key].is_number())
    return false;
  const double value = row[key].get<double>();
  if (!std::isfinite(value))
    return false;
  if (out)
    *out = value;
  return true;
}

static bool usableHeatsinkRow(const nlohmann::json &row) {
  double power = 0.0;
  return row.is_object() && row.value("mapped", false) &&
         row.contains("ref") && row["ref"].is_string() &&
         !row["ref"].get_ref<const std::string &>().empty() &&
         handoffFiniteNumber(row, "power_w", &power) && power > 0.0;
}

static const nlohmann::json *
findHeatsinkRow(const nlohmann::json &result, const std::string &componentRef) {
  if (componentRef.empty() || !result.is_object() ||
      !result.contains("components") || !result["components"].is_array())
    return nullptr;
  for (const auto &row : result["components"])
    if (row.is_object() && row.value("ref", std::string()) == componentRef)
      return &row;
  return nullptr;
}

int SimManager::resolveHandoffDocTab(const AppState &st) const {
  if (ho_.ecadDocId.empty())
    return -1;
  for (int i = 0; i < (int)st.docTabs.size(); ++i)
    if (st.docTabs[i].kind == AppState::DocTab::TabKind::PCB &&
        st.docTabs[i].ecadDocId == ho_.ecadDocId)
      return i;
  return -1;
}

void SimManager::deriveHeatsinkTarget(const std::string &componentRef) {
  ho_.heatsinkComponentRef = componentRef;
  ho_.heatsinkTargetC = 0.0f;
  const nlohmann::json *row = findHeatsinkRow(ho_.result, componentRef);
  if (!row)
    return;
  double target = 0.0;
  if (handoffFiniteNumber(*row, "tj_max_c", &target)) {
    ho_.heatsinkTargetKind = 0;
    ho_.heatsinkTargetC = (float)target;
  } else if (handoffFiniteNumber(*row, "tj_c", &target)) {
    ho_.heatsinkTargetKind = 0;
    ho_.heatsinkTargetC = (float)target;
  } else if (handoffFiniteNumber(*row, "t_case_c", &target)) {
    ho_.heatsinkTargetKind = 1;
    ho_.heatsinkTargetC = (float)target;
  }
}

std::string SimManager::heatsinkSetupBlocker(const AppState &st) const {
  if (resolveHandoffDocTab(st) < 0)
    return "The source board is no longer open. Reopen it and start a new "
           "thermal handoff.";
  if (ho_.running)
    return "Wait for the thermal handoff solve to finish.";
  if (!ho_.heatsinkContextReady || !ho_.result.is_object() ||
      !ho_.result.value("ok", false) || !ho_.result.value("solved", false) ||
      ho_.result.value("doc_id", std::string()) != ho_.ecadDocId)
    return "Run a successful thermal handoff for this board before picking a "
           "heatsink.";
  const nlohmann::json *row =
      findHeatsinkRow(ho_.result, ho_.heatsinkComponentRef);
  if (!row || !usableHeatsinkRow(*row))
    return "Choose a mapped powered component from the thermal result.";
  if (ho_.heatsinkTargetKind < 0 || ho_.heatsinkTargetKind > 1 ||
      !std::isfinite(ho_.heatsinkTargetC))
    return "Enter a finite junction or case temperature target.";
  double ambient = 0.0;
  if (!ho_.result.contains("environment") ||
      !handoffFiniteNumber(ho_.result["environment"], "t_amb_c", &ambient))
    return "The thermal handoff did not provide a finite ambient temperature.";
  if (ho_.heatsinkTargetC <= ambient)
    return "The heatsink target must be above the handoff ambient temperature.";
  return {};
}

void SimManager::openHandoffPanel(AppState &st, int pcbTab) {
  handoffPanelOpen_ = true;
  std::string ecadId;
  std::string title;
  if (pcbTab >= 0 && pcbTab < (int)st.docTabs.size() &&
      st.docTabs[pcbTab].kind == AppState::DocTab::TabKind::PCB) {
    ecadId = st.docTabs[pcbTab].ecadDocId;
    title = st.docTabs[pcbTab].displayTitle();
  }
  if (ecadId != ho_.ecadDocId) {
    ho_.heatsinkComponentRef.clear();
    ho_.heatsinkTargetC = 0.0f;
    ho_.heatsinkContextReady = false;
  }
  ho_.ecadDocId = std::move(ecadId);
  ho_.ecadDocTitle = std::move(title);
  probeCaps(st);
  st.leftPaneTab = AppState::LP_Simulation;
}

void SimManager::runEcadThermalHandoff(AppState &st) {
  const int sourceTab = resolveHandoffDocTab(st);
  if (sourceTab < 0) {
    ho_.error =
        "source board closed or was replaced; start a new handoff from an open "
        "PCB";
    return;
  }
  const std::string ecadId = ho_.ecadDocId;
  ho_.heatsinkContextReady = false;
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
      [this, &st, ecadId](const core::RpcResult &r) {
        ho_.running = false;
        if (ho_.ecadDocId != ecadId)
          return;
        if (!r.ok || !r.result.is_object()) {
          ho_.error = r.err.empty() ? "worker error" : r.err;
          return;
        }
        ho_.result = r.result;
        ho_.heatsinkContextReady =
            r.result.value("ok", false) &&
            r.result.value("solved", false);
        if (!r.result.value("ok", false)) {
          ho_.error = r.result.value("error", std::string("handoff failed"));
        } else if (r.result.value("solved", false)) {
          const nlohmann::json *selected =
              findHeatsinkRow(ho_.result, ho_.heatsinkComponentRef);
          if (!selected || !usableHeatsinkRow(*selected)) {
            selected = nullptr;
            if (ho_.result.contains("components") &&
                ho_.result["components"].is_array())
              for (const auto &row : ho_.result["components"])
                if (usableHeatsinkRow(row)) {
                  selected = &row;
                  break;
                }
          }
          if (selected)
            deriveHeatsinkTarget(
                selected->value("ref", std::string()));
          else {
            ho_.heatsinkComponentRef.clear();
            ho_.heatsinkTargetC = 0.0f;
          }
        }
        st.addLog("[sim] ecad_thermal_handoff: " +
                  r.result.value("hint",
                                 r.result.value("error", std::string("done"))));
      },
      300000);
}

void SimManager::runHeatsinkSelect(AppState &st) {
  const std::string blocker = heatsinkSetupBlocker(st);
  if (!blocker.empty()) {
    st.toast(ui::ToastLevel::Warn, blocker);
    return;
  }
  nlohmann::json params = {
      {"component_ref", ho_.heatsinkComponentRef},
  };
  if (ho_.heatsinkTargetKind == 0)
    params["tj_target_c"] = ho_.heatsinkTargetC;
  else
    params["t_case_target_c"] = ho_.heatsinkTargetC;
  const std::string ecadId = ho_.ecadDocId;
  ho_.heatsinkRunning = true;
  st.rpc.call(
      "heatsink_select", params,
      [this, ecadId](const core::RpcResult &r) {
        ho_.heatsinkRunning = false;
        if (ho_.ecadDocId != ecadId)
          return;
        ho_.heatsink =
            r.ok && r.result.is_object()
                ? r.result
                : nlohmann::json{
                      {"ok", false},
                      {"error", r.err.empty()
                                    ? "heatsink selection returned no result"
                                    : r.err}};
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

  const int sourceTab = resolveHandoffDocTab(st);
  const bool sourceOpen = sourceTab >= 0;
  if (sourceOpen) {
    ImGui::Text("Board: %s", st.docTabs[sourceTab].displayTitle().c_str());
  } else {
    const std::string label =
        !ho_.ecadDocTitle.empty() ? ho_.ecadDocTitle : ho_.ecadDocId;
    ImGui::Text("Board: %s (not open)",
                label.empty() ? "(source unavailable)" : label.c_str());
    kit::dimText(
        "The source board was closed or replaced. Reopen it and start a new "
        "handoff; another tab will never be substituted.");
  }
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
    char buf[256];
    std::snprintf(buf, sizeof(buf),
                  "ECAD \xe2\x86\x92 thermal handoff unavailable: %s",
                  capsStatus_ == 1
                      ? caps_["handoffs"]["ecad_thermal"]
                            .value("hint", std::string("worker seam missing"))
                            .c_str()
                      : "worker unreachable (sim_caps probe failed)");
    kit::dimText(buf);
  } else {
    const bool disabled = ho_.running || !sourceOpen;
    if (disabled)
      ImGui::BeginDisabled();
    if (ImGui::Button(ho_.running ? "Solving\xe2\x80\xa6"
                                  : "Send board to thermal FEA",
                      ImVec2(-FLT_MIN, 0)) &&
        !disabled)
      runEcadThermalHandoff(st);
    if (disabled)
      ImGui::EndDisabled();
  }

  // T-FIN17 forward seam. Selection is bound to a stable component reference
  // from this board's solved handoff; the target remains editable because a
  // part limit is not always present in ECAD metadata.
  const bool handoffReady =
      ho_.heatsinkContextReady && ho_.result.is_object() &&
      ho_.result.value("ok", false) && ho_.result.value("solved", false) &&
      ho_.result.value("doc_id", std::string()) == ho_.ecadDocId;
  const nlohmann::json *selectedRow =
      findHeatsinkRow(ho_.result, ho_.heatsinkComponentRef);
  if (selectedRow && !usableHeatsinkRow(*selectedRow))
    selectedRow = nullptr;
  if (handoffReady && !selectedRow && ho_.result.contains("components") &&
      ho_.result["components"].is_array()) {
    for (const auto &row : ho_.result["components"])
      if (usableHeatsinkRow(row)) {
        deriveHeatsinkTarget(row.value("ref", std::string()));
        selectedRow = &row;
        break;
      }
  }

  ImGui::SeparatorText("Heatsink setup");
  if (handoffReady && selectedRow) {
    ImGui::SetNextItemWidth(-FLT_MIN);
    if (ImGui::BeginCombo("##hscomponent",
                          ho_.heatsinkComponentRef.c_str())) {
      for (const auto &row : ho_.result["components"]) {
        if (!usableHeatsinkRow(row))
          continue;
        const std::string ref = row.value("ref", std::string());
        if (ImGui::Selectable(ref.c_str(),
                              ref == ho_.heatsinkComponentRef))
          deriveHeatsinkTarget(ref);
      }
      ImGui::EndCombo();
    }
    simTip("Mapped powered component to cool. The stable ECAD reference is sent "
           "to the selector.");

    static const char *kTargetKinds[2] = {"Junction target (Tj)",
                                          "Case target (Tcase)"};
    if (ho_.heatsinkTargetKind < 0 || ho_.heatsinkTargetKind > 1)
      ho_.heatsinkTargetKind = 0;
    ImGui::SetNextItemWidth(-FLT_MIN);
    if (ImGui::BeginCombo("##hstargetkind",
                          kTargetKinds[ho_.heatsinkTargetKind])) {
      for (int i = 0; i < 2; ++i)
        if (ImGui::Selectable(kTargetKinds[i],
                              ho_.heatsinkTargetKind == i))
          ho_.heatsinkTargetKind = i;
      ImGui::EndCombo();
    }
    ImGui::SetNextItemWidth(-FLT_MIN);
    ImGui::InputFloat("##hstarget", &ho_.heatsinkTargetC, 0.0f, 0.0f,
                      "target = %.1f C");
    simTip("Seeded from the component Tj limit when available, otherwise from "
           "its solved temperature. Adjust to the design requirement.");
  }

  const bool hsUp =
      capsStatus_ == 1 && caps_.contains("handoffs") &&
      caps_["handoffs"]["heatsink_select"].value("available", false);
  const std::string setupBlocker = heatsinkSetupBlocker(st);
  std::string hsReason;
  if (!hsUp)
    hsReason =
        capsStatus_ == 1
            ? caps_["handoffs"]["heatsink_select"].value(
                  "hint",
                  std::string(
                      "heatsink selection (T-FIN17) is not installed"))
            : "heatsink selection capability is still unavailable";
  else
    hsReason = setupBlocker;
  const bool hsDisabled =
      !hsReason.empty() || ho_.heatsinkRunning;
  if (hsDisabled)
    ImGui::BeginDisabled();
  if (ImGui::Button(ho_.heatsinkRunning ? "Picking\xe2\x80\xa6"
                                        : "Pick heatsink",
                    ImVec2(-FLT_MIN, 0)) &&
      !hsDisabled)
    runHeatsinkSelect(st);
  if (hsDisabled)
    ImGui::EndDisabled();
  simTip(hsReason.c_str());
  if (!hsReason.empty()) {
    kit::dimText(hsReason.c_str());
  }

  ImGui::Spacing();
  ImGui::Separator();
  if (!ho_.error.empty())
    ImGui::TextWrapped("Failed: %s", ho_.error.c_str());
  if (ho_.result.is_object()) {
    const std::string resultDocId =
        ho_.result.value("doc_id", std::string());
    if (!resultDocId.empty() && resultDocId != ho_.ecadDocId) {
      char buf[192];
      std::snprintf(buf, sizeof(buf),
                    "The retained thermal result belongs to ECAD document %s, "
                    "not the currently selected source.",
                    resultDocId.c_str());
      kit::dimText(buf);
    }
  }
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
          kit::dimText(w.get<std::string>().c_str());
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
