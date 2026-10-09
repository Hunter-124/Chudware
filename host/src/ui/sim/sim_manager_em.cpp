// SimManager EM: em_status probe, EM ribbon commands, waveguide forms, EM runs and the EM panel.
#include "sim_manager_internal.h"
#include "sim_common.h"

#include <cstdio>
#include <filesystem>
#include <algorithm>
#include <array>
#include <cstring>
#include <cctype>

#include "../dragdrop.h"

#include "../file_dialog.h"    // openFileDialog — optional driven-sweep geometry STL
#include "../widgets/ribbon.h" // setRibbonDisabledPrefix — EM tab gating
#include "imgui_internal.h"    // ImGuiWindow — field-level landing scrolls the pane body

namespace ui {

namespace {
// One-shot landing target set by the Setup/Boundaries ribbon commands and
// consumed by drawEmForm: 0 none, 1 Waveguide card (Boundaries), 2 the
// "Port 1 mode" field (Ports), 3 the "Start frequency" field (Frequency
// Range). The landed card then carries an accent wash for kEmFlashS so the
// jump is visible.
enum EmFocus { kEmFocusNone = 0, kEmFocusWaveguide, kEmFocusPorts, kEmFocusRange };
int gEmFocus = kEmFocusNone;
int gEmFlashTarget = kEmFocusNone;
double gEmFlashUntil = 0.0;
constexpr double kEmFlashS = 1.2;
// Stepper page per EmForm (indexed by the enum). A dispatch lands on Run (the
// progress), the completion moves on to Results.
enum EmPage { kEmSetup = 0, kEmRun, kEmResults };
int gEmPage[3] = {kEmSetup, kEmSetup, kEmSetup};
// Session disclosure: the Waveguide card collapses to its one-line summary
// title; "About this analysis" is collapsed by default (standard §2).
bool gEmWgOpen = true;
const char *emAnalysis(int form);
bool gEmAboutOpen = false;
// Render-ready results are rebuilt only after completion or a manager switch.
struct EmResultView {
  const SimManager *owner = nullptr;
  struct Mode {
    std::string name, type, cutoff, error, tooltip;
  };
  std::vector<Mode> modes;
  int form = -1;
  std::vector<float> modeIndex, cutoff;
  std::vector<kit::KeyValueItem> metrics;
};
EmResultView gEmResultView;
bool gEmExecutionOpen = false;
bool gEmModesOpen = false;

const char *emBackendLabel(const std::string &name) {
  if (name == "builtin-fdfd")
    return "Finite-difference eigenmode solver";
  if (name == "analytic-uniform")
    return "Analytic uniform waveguide";
  if (name == "openems")
    return "openEMS full-wave solver";
  return nullptr;
}
std::vector<std::string> gEmBackendLabels[3];
const SimManager *gEmBackendLabelOwner = nullptr;
int gEmBackendLabelForm = -1;
} // namespace

// ---------------------------------------------------------------- EM
// (electromagnetics) gating
//
// The EM ribbon tools use the native em_* worker RPCs.
// em_status is probed ONCE asynchronously. A missing backend disables only Run;
// every analysis still offers editable setup with the native refusal visible.
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
        bool available = false;
        for (int fi = 0; fi < 3; ++fi) {
          em_.backendNames[fi].clear();
          gEmBackendLabels[fi].clear();
        }
        gEmBackendLabelOwner = nullptr;
        if (r.ok && r.result.is_object() && !r.result.contains("code") &&
            r.result.value("ok", false)) {
          emStatusResult_ = r.result;
          const auto solvers = r.result.find("solvers");
          if (solvers != r.result.end() && solvers->is_array())
            for (const auto &solver : *solvers) {
              if (!solver.is_object() || !solver.value("installed", false))
                continue;
              const auto caps = solver.find("capabilities");
              if (caps == solver.end() || !caps->is_array())
                continue;
              for (int fi = 0; fi < 3; ++fi)
                if (std::find(caps->begin(), caps->end(), emAnalysis(fi)) != caps->end()) {
                  em_.backendNames[fi].push_back(solver.value("name", std::string()));
                  available = true;
                }
            }
        }
        emStatus_ = available ? 1 : -1;
        if (emStatus_ < 0)
          ui::setRibbonDisabledPrefix("sim_em_run", "EM solver not installed");
        else
          ui::setRibbonDisabledPrefix("", "");
      },
      5000);
}

bool SimManager::handleEmCommand(AppState &st, const std::string &cmdId) {
  if (emStatus_ == 0)
    probeEmStatus(st);
  if (emStatus_ < 0 && cmdId == "sim_em_run") {
    st.toast(ui::ToastLevel::Warn, "Install an electromagnetics solver before "
                                  "running. Setup remains editable.");
    return true;
  }
  // Setup remains editable offline; native capability checks still gate Run.
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
  // port/boundary editors — the uniform-section backend takes matched modal
  // ports and conducting walls implicitly). Open the form the setting belongs
  // to, switch it to its Setup page and land on the input itself (accent-washed
  // briefly): Ports on the "Port 1 mode" field (Freq Sweep and Time Domain
  // both carry it), Frequency Range on the Freq Sweep "Start frequency" field,
  // Boundaries on the expanded Waveguide card. A landing on the analysis card
  // folds the Waveguide card back so the target is not pushed down by it.
  if (cmdId == "sim_em_ports") {
    if (!emPanelOpen_ || emForm_ == EmForm::Eigenmode)
      openEmForm(st, EmForm::Driven);
    gEmPage[(int)emForm_] = kEmSetup;
    gEmWgOpen = false;
    gEmFocus = kEmFocusPorts;
    return true;
  }
  if (cmdId == "sim_em_range") {
    openEmForm(st, EmForm::Driven);
    gEmPage[(int)emForm_] = kEmSetup;
    gEmWgOpen = false;
    gEmFocus = kEmFocusRange;
    return true;
  }
  if (cmdId == "sim_em_boundary") {
    openEmForm(st, emForm_);
    gEmPage[(int)emForm_] = kEmSetup;
    gEmWgOpen = true;
    gEmFocus = kEmFocusWaveguide;
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
    // The Results page always renders something: the plots, the running
    // spinner, the failure, or an empty state pointing at the footer's Run.
    openEmForm(st, emForm_);
    gEmPage[(int)emForm_] = kEmResults;
    return true;
  }
  return true;
}

namespace {

// ---------------------------------------------------------------- EM panel
// (PropertyManager-style)

// EIA rectangular-waveguide standards (name -> broad/narrow wall in mm),
// mirrored from engine_oracle/sim/em/analytic.py STANDARD_WAVEGUIDES. The order
// here defines em_.stdIdx; keep WR-90 at index 7 (the EmState default).
// "Custom" is stdIdx == -1 (handled in the form).
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
static const char *const kEmModes[] = {"TE10", "TE20", "TE01", "TE11", "TM11"};
static const int kNEmModes = (int)(sizeof(kEmModes) / sizeof(kEmModes[0]));

constexpr const char *kEmStandardDragPayload = "CHUDWARE_EM_STANDARD";
bool gEmPaletteOpen = false;

const char *emAnalysis(int form) {
  static constexpr const char *names[] = {"eigenmode", "driven", "timedomain"};
  return names[form];
}

bool emSupportedKey(const std::string &key, int form) {
  return key == "standard" || key == "waveguide" || key == "solver" ||
      (form == 0 && (key == "n_modes" || key == "resolution" || key == "fields")) ||
      (form != 0 && (key == "length_mm" || key == "mode" || key == "geometry" ||
                     key == "cells_per_wavelength" || key == "max_timesteps" ||
                     key == "end_criteria" || key == "f_start_ghz" ||
                     key == "f_stop_ghz" || key == "n_freq")) ||
      (form == 2 && (key == "f0_ghz" || key == "bw_ghz" || key == "t_max_ns"));
}

bool emStlPathValid(const std::string &path, std::size_t capacity,
                    std::string &error) {
  if (path.empty())
    return true;
  if (path.size() >= capacity || path.find('\0') != std::string::npos) {
    error = "The STL path is too long or contains an embedded NUL.";
    return false;
  }
  std::filesystem::path file(path);
  std::string ext = file.extension().string();
  std::transform(ext.begin(), ext.end(), ext.begin(),
                 [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
  std::error_code ec;
  if (ext != ".stl" || !std::filesystem::is_regular_file(file, ec) || ec) {
    error = "Choose an existing .stl mesh file; the previous geometry is unchanged.";
    return false;
  }
  return true;
}

bool emAcceptStandard(int &index) {
  bool delivered = false;
  if (ImGui::BeginDragDropTarget()) {
    if (const ImGuiPayload *payload =
            ImGui::AcceptDragDropPayload(kEmStandardDragPayload)) {
      if (payload->IsDelivery() && payload->DataSize == sizeof(int)) {
        int candidate = -1;
        std::memcpy(&candidate, payload->Data, sizeof(candidate));
        if (candidate >= 0 && candidate < kNEmStandards) {
          index = candidate;
          delivered = true;
        }
      }
    }
    ImGui::EndDragDropTarget();
  }
  return delivered;
}

// Footer timing for the in-flight em_* call (the RPC reports no progress).
double gEmRunStart = 0.0;
int gEmRunForm = -1; // EmForm (as int) of the in-flight call

// Standard picker rows use the document's display units, without allocating a new list per frame.
const std::vector<std::string> &emStandardItems(bool us) {
  static const std::array<std::vector<std::string>, 2> items = [] {
    std::array<std::vector<std::string>, 2> lists;
    for (int u = 0; u < 2; ++u) {
      auto &v = lists[u];
      v.reserve(kNEmStandards + 1);
      for (const auto &s : kEmStandards)
        v.push_back(std::string(s.name) + "  " + sim::fmtU(s.a_mm, "mm", u != 0) +
                    " \xc3\x97 " + sim::fmtU(s.b_mm, "mm", u != 0));
      v.push_back("Custom cross-section");
    }
    return lists;
  }();
  return items[us ? 1 : 0];
}

// Short waveguide name for card titles and the Run recap: "WR-90",
// "Custom rectangular", "Custom circular" (the dimensions get their own rows).
std::string emWaveguideName(int stdIdx, int shape) {
  if (stdIdx >= 0 && stdIdx < kNEmStandards)
    return kEmStandards[stdIdx].name;
  return shape == 1 ? "Custom circular" : "Custom rectangular";
}

// Inner cross-section, in the document's display units.
std::string emWaveguideDims(int stdIdx, int shape, double a, double b, double r, bool us) {
  if (stdIdx >= 0 && stdIdx < kNEmStandards) {
    a = kEmStandards[stdIdx].a_mm;
    b = kEmStandards[stdIdx].b_mm;
  } else if (shape == 1) {
    return "r " + sim::fmtU(r, "mm", us);
  }
  return sim::fmtU(a, "mm", us) + " \xc3\x97 " + sim::fmtU(b, "mm", us);
}

// Frequency drag aid on a LOG scale: EM ranges span 0.01-300 GHz, so a linear
// track pins every practical value (1-40 GHz) against the left end. The value
// moves live while dragging (same contract as kit::fieldSlider); returns true
// when the value changed this frame. Typed entry stays on the numeric fields.
bool emLogSlider(const char *id, const char *label, double *v, double lo,
                 double hi, const kit::FieldOpts &options) {
  lo = std::max(lo, 1e-6);
  hi = std::max(hi, lo * 1.0001);
  const float width = kit::fieldBegin(id, label, options);
  const ImVec2 valueOrigin = ImGui::GetCursorScreenPos();
  const double before = *v;
  double value = std::clamp(*v, lo, hi);
  // Keep the frequency in a readout above the track. A logarithmic slider thumb can cross
  // every digit in a narrow value column; no text is ever painted beneath it.
  ImGui::AlignTextToFramePadding();
  ImGui::TextUnformatted(kit::fmtValue(value, options.sigFigs).c_str());
  ImGui::SetCursorScreenPos(ImVec2(valueOrigin.x, ImGui::GetCursorScreenPos().y));
  ImGui::SetNextItemWidth(width);
  ImGui::SliderScalar("##log", ImGuiDataType_Double, &value, &lo, &hi, "",
                      ImGuiSliderFlags_Logarithmic | ImGuiSliderFlags_AlwaysClamp |
                          ImGuiSliderFlags_NoInput);
  ImGuiContext &g = *GImGui;  // IMGUI_TEST_ENGINE_ITEM_INFO expands against a local `g`
  (void)g;
  IMGUI_TEST_ENGINE_ITEM_INFO(g.LastItemData.ID, label, g.LastItemData.StatusFlags);
  kit::fieldEnd();
  if (value == std::clamp(before, lo, hi))
    return false;
  *v = value;
  return true;
}
} // namespace

void SimManager::openEmForm(AppState &st, EmForm f) {
  (void)st;
  emForm_ = f; // the in-panel form tabs read emForm_ directly
  emPanelOpen_ = true;
  panelOpen_ = false; // EM takes over the pane; close any study setup panel
}

// Build the {standard:"WR-90"} or {waveguide:{shape,a_mm,b_mm|radius_mm}} spec
// from em_.
nlohmann::json SimManager::emWaveguideSpec() const {
  if (em_.stdIdx >= 0 && em_.stdIdx < kNEmStandards)
    return nlohmann::json{{"standard", kEmStandards[em_.stdIdx].name},
                          {"waveguide", {{"eps_r", em_.epsR}}}};
  nlohmann::json wg;
  if (em_.shape == 1) {
    wg = {{"shape", "circular"}, {"radius_mm", em_.radius_mm},
          {"eps_r", em_.epsR}};
  } else {
    wg = {{"shape", "rectangular"}, {"a_mm", em_.a_mm},
          {"b_mm", em_.b_mm}, {"eps_r", em_.epsR}};
  }
  return nlohmann::json{{"waveguide", wg}};
}

nlohmann::json SimManager::buildEmRequest() const {
  const int fi = static_cast<int>(emForm_);
  nlohmann::json params = nlohmann::json::object();
  const auto &native = em_.nativeParameters[fi];
  if (native.is_object())
    for (auto it = native.begin(); it != native.end(); ++it)
      if (emSupportedKey(it.key(), fi))
        params[it.key()] = it.value();
  params.update(emWaveguideSpec());
  if (!params.contains("solver") || params["solver"] == "" ||
      params["solver"] == "auto") {
    const auto &defaults = sim::jobj(emStatusResult_, "defaults");
    if (defaults.contains(emAnalysis(fi)) && defaults[emAnalysis(fi)].is_string())
      params["solver"] = defaults[emAnalysis(fi)];
  }
  if (emForm_ == EmForm::Eigenmode) {
    params["n_modes"] = em_.nModes;
    params["resolution"] = em_.resolution;
    if (!params.contains("fields"))
      params["fields"] = false;
  } else {
    params["length_mm"] = em_.lengthMm;
    params["mode"] = em_.exactMode.empty()
                         ? kEmModes[em_.modeIdx]
                         : em_.exactMode;
    if (emForm_ == EmForm::Driven) {
      params["f_start_ghz"] = em_.fStartGhz;
      params["f_stop_ghz"] = em_.fStopGhz;
      params["n_freq"] = em_.nFreq;
    } else {
      params["f0_ghz"] = em_.f0Ghz;
      params["bw_ghz"] = em_.bwGhz;
    }
    if (em_.stlPath[0]) {
      if (!params.contains("geometry"))
        params["geometry"] = nlohmann::json::object();
      params["geometry"]["stl_path"] = em_.stlPath;
    } else {
      params.erase("geometry");
    }
  }
  if (params.value("solver", std::string()) == "openems") {
    if (!params.contains("cells_per_wavelength"))
      params["cells_per_wavelength"] = 20;
    if (!params.contains("max_timesteps"))
      params["max_timesteps"] = 200000;
    if (!params.contains("end_criteria"))
      params["end_criteria"] = 1e-4;
    if (params.contains("geometry") && !params["geometry"].contains("priority"))
      params["geometry"]["priority"] = 10;
  }
  return params;
}

nlohmann::json SimManager::activeEmParameters() const {
  const auto &native = em_.nativeParameters[static_cast<int>(emForm_)];
  auto parameters = native.is_object() ? native : nlohmann::json::object();
  parameters.update(buildEmRequest());
  return parameters;
}

void SimManager::invalidateEmResults() {
  gEmResultView.owner = nullptr;
  em_.eigResult = em_.drvResult = em_.tdResult = -1;
  em_.eigModes = nlohmann::json::array();
  em_.drvFreq.clear();
  em_.drvS11.clear();
  em_.drvS21.clear();
  em_.tdTime.clear();
  em_.tdPort1.clear();
  em_.tdPort2.clear();
  for (auto &raw : em_.rawResults)
    raw = nlohmann::json();
}

bool SimManager::configureEmParameters(const nlohmann::json &params,
                                       std::string &error) {
  error.clear();
  auto fail = [&](const std::string &message) {
    error = message;
    return false;
  };
  if (emRunning_)
    return fail("Wait for the EM analysis to finish before changing its inputs.");
  if (!params.is_object())
    return fail("Native parameters must be a JSON object.");
  const int fi = static_cast<int>(emForm_);
  const bool eigen = emForm_ == EmForm::Eigenmode;
  const bool driven = emForm_ == EmForm::Driven;
  for (auto it = params.begin(); it != params.end(); ++it)
    if (!emSupportedKey(it.key(), fi) &&
        (emSupportedKey(it.key(), 0) || emSupportedKey(it.key(), 1) ||
         emSupportedKey(it.key(), 2)))
      return fail("Native parameter " + it.key() + " belongs to another analysis.");
  auto positive = [&](const nlohmann::json &object, const char *key) {
    return !object.contains(key) ||
           (object[key].is_number() &&
            std::isfinite(object[key].get<double>()) &&
            object[key].get<double>() > 0);
  };
  auto integer = [&](const char *key, int lo, int hi) {
    if (!params.contains(key))
      return true;
    return params[key].is_number_integer() &&
           params[key].get<double>() >= lo && params[key].get<double>() <= hi;
  };
  for (const char *key : {"length_mm", "f_start_ghz", "f_stop_ghz",
                          "f0_ghz", "bw_ghz", "t_max_ns", "end_criteria"})
    if (!positive(params, key))
      return fail(std::string(key) + " must be finite and positive.");
  if (!integer("n_modes", 1, 40) || !integer("resolution", 24, 512) ||
      !integer("n_freq", 2, 100001) ||
      !integer("cells_per_wavelength", 10, 200) ||
      !integer("max_timesteps", 1, 10000000))
    return fail("A native integer control is outside its supported range.");
  if (params.contains("end_criteria") && params["end_criteria"].get<double>() >= 1)
    return fail("end_criteria must be below 1.");
  if (params.contains("fields") && !params["fields"].is_boolean())
    return fail("fields must be boolean.");
  if (params.contains("mode") &&
      (!params["mode"].is_string() || params["mode"].get_ref<const std::string &>().empty()))
    return fail("mode must be a nonempty native mode name.");
  int standard = em_.stdIdx;
  if (params.contains("standard")) {
    if (!params["standard"].is_string())
      return fail("standard must be a waveguide name.");
    standard = -1;
    for (int i = 0; i < kNEmStandards; ++i)
      if (params["standard"] == kEmStandards[i].name)
        standard = i;
    if (standard < 0)
      return fail("Choose a listed EIA waveguide standard.");
  }
  const auto wg = params.value("waveguide", nlohmann::json::object());
  if (!wg.is_object())
    return fail("waveguide must be an object.");
  for (auto it = wg.begin(); it != wg.end(); ++it)
    if (it.key() != "shape" && it.key() != "a_mm" && it.key() != "b_mm" &&
        it.key() != "radius_mm" && it.key() != "eps_r")
      return fail("Unsupported waveguide parameter: " + it.key());
  for (const char *key : {"a_mm", "b_mm", "radius_mm", "eps_r"})
    if (!positive(wg, key))
      return fail(std::string("waveguide.") + key + " must be finite and positive.");
  int shape = em_.shape;
  if (wg.contains("shape")) {
    if (wg["shape"] != "rectangular" && wg["shape"] != "circular")
      return fail("waveguide.shape must be rectangular or circular.");
    shape = wg["shape"] == "circular" ? 1 : 0;
    if (!params.contains("standard"))
      standard = -1;
  }
  if (!params.contains("standard") &&
      (wg.contains("a_mm") || wg.contains("b_mm") || wg.contains("radius_mm")))
    standard = -1;
  if (params.contains("standard") &&
      (wg.contains("a_mm") || wg.contains("b_mm") || wg.contains("radius_mm") ||
       wg.contains("shape")))
    return fail("A standard fixes its dimensions; remove standard to define Custom geometry.");
  if ((shape == 0 && wg.contains("radius_mm")) ||
      (shape == 1 && (wg.contains("a_mm") || wg.contains("b_mm"))))
    return fail("Use a_mm/b_mm for rectangular guides, or radius_mm for circular guides.");
  const double a = wg.value("a_mm", em_.a_mm);
  const double b = wg.value("b_mm", em_.b_mm);
  if (standard < 0 && shape == 0 && b > a)
    return fail("Broad wall a_mm must be at least b_mm.");
  if (params.contains("f_start_ghz") || params.contains("f_stop_ghz")) {
    if (params.value("f_stop_ghz", em_.fStopGhz) <=
        params.value("f_start_ghz", em_.fStartGhz))
      return fail("f_stop_ghz must exceed f_start_ghz.");
  }
  if (params.contains("geometry")) {
    const auto &geometry = params["geometry"];
    if (!geometry.is_object() || !geometry.contains("stl_path") ||
        !geometry["stl_path"].is_string())
      return fail("geometry must contain a string stl_path.");
    for (auto it = geometry.begin(); it != geometry.end(); ++it)
      if (it.key() != "stl_path" && it.key() != "priority")
        return fail("Unsupported geometry parameter: " + it.key());
    if (geometry.contains("priority") &&
        (!geometry["priority"].is_number_integer() ||
         geometry["priority"].get<double>() < 0 ||
         geometry["priority"].get<double>() > 1000))
      return fail("geometry.priority must be an integer from 0 to 1000.");
    if (!emStlPathValid(geometry["stl_path"].get<std::string>(),
                        sizeof(em_.stlPath), error))
      return false;
  }
  if (params.contains("solver")) {
    if (!params["solver"].is_string())
      return fail("solver must be a native backend name.");
    const std::string requested = params["solver"].get<std::string>();
    if (requested == "auto" || requested.empty())
      return fail("Select an explicit backend; automatic fallback is not used.");
    bool supported = false;
    for (const auto &solver : sim::jarr(emStatusResult_, "solvers"))
      if (sim::jstr(solver, "name") == requested &&
          sim::jbool(solver, "installed", false)) {
        const auto &caps = sim::jarr(solver, "capabilities");
        supported = std::find(caps.begin(), caps.end(), emAnalysis(fi)) != caps.end();
      }
    if (!supported)
      return fail("This backend is unavailable or does not offer the active analysis.");
  }
  const auto defaults = emStatusResult_.find("defaults");
  std::string effectiveSolver;
  if (params.contains("solver"))
    effectiveSolver = params["solver"].get<std::string>();
  else if (defaults != emStatusResult_.end() && defaults->is_object() &&
           defaults->contains(emAnalysis(fi)) && (*defaults)[emAnalysis(fi)].is_string())
    effectiveSolver = (*defaults)[emAnalysis(fi)].get<std::string>();
  if (effectiveSolver != "openems" &&
      (params.contains("cells_per_wavelength") || params.contains("max_timesteps") ||
       params.contains("end_criteria") ||
       (!eigen && !driven && (params.contains("f_start_ghz") ||
                             params.contains("f_stop_ghz") || params.contains("n_freq")))))
    return fail("Full-wave controls require the openems backend; they are not silently ignored.");
  if (params.contains("t_max_ns") && effectiveSolver != "analytic-uniform")
    return fail("t_max_ns is supported by analytic-uniform only.");
  if (params.contains("geometry") && effectiveSolver != "openems")
    return fail("STL geometry requires the openems backend; analytic uniform sections cannot use it.");
  if (params.value("fields", false) && effectiveSolver != "builtin-fdfd")
    return fail("Field meshes require builtin-fdfd; this backend does not generate them.");
  // Validation is atomic: only native controls accepted above reach the state/request.
  em_.nativeParameters[fi] = params;
  em_.nativeParameters[fi].erase("standard");
  em_.nativeParameters[fi].erase("waveguide");
  em_.stdIdx = standard;
  em_.shape = shape;
  em_.a_mm = a;
  em_.b_mm = b;
  em_.radius_mm = wg.value("radius_mm", em_.radius_mm);
  em_.epsR = wg.value("eps_r", em_.epsR);
  em_.nModes = params.value("n_modes", em_.nModes);
  em_.resolution = params.value("resolution", em_.resolution);
  em_.lengthMm = params.value("length_mm", em_.lengthMm);
  em_.fStartGhz = params.value("f_start_ghz", em_.fStartGhz);
  em_.fStopGhz = params.value("f_stop_ghz", em_.fStopGhz);
  em_.nFreq = params.value("n_freq", em_.nFreq);
  em_.f0Ghz = params.value("f0_ghz", em_.f0Ghz);
  em_.bwGhz = params.value("bw_ghz", em_.bwGhz);
  if (params.contains("mode")) {
    em_.exactMode = params["mode"].get<std::string>();
    for (int i = 0; i < kNEmModes; ++i)
      if (em_.exactMode == kEmModes[i]) {
        em_.modeIdx = i;
        em_.exactMode.clear();
      }
  }
  if (!eigen)
    std::snprintf(em_.stlPath, sizeof(em_.stlPath), "%s",
                  params.contains("geometry")
                      ? params["geometry"]["stl_path"].get_ref<const std::string &>().c_str()
                      : "");
  invalidateEmResults();
  em_.parameterDraftLoaded[fi] = false;
  return true;
}

void SimManager::drawEmParameterEditor() {
  const int fi = static_cast<int>(emForm_);
  const bool expanded = kit::cardBegin(
      "native_parameters", "Advanced parameters", "study_em",
      &em_.parameterEditorOpen);
  kit::itemTooltip("Optional solver settings as JSON. The controls above are the "
                   "normal way to set up a run.");
  if (expanded) {
    kit::dimText("The full solver request as JSON. Apply checks every setting first "
                 "and only then updates the controls above; if anything is invalid, "
                 "nothing changes.");
    kit::dimText("Walls and matched ports are set by the solver and cannot be "
                 "edited here.");
    kit::dimText("Unrecognized top-level settings are saved with the setup but not "
                 "sent to the solver. Unknown waveguide or geometry settings are "
                 "rejected.");
    kit::dimText("Extra settings you can add: fields (eigenmode); t_max_ns "
                 "(analytic time domain); cells_per_wavelength, max_timesteps, "
                 "end_criteria, geometry.priority (openEMS). Time-domain openEMS "
                 "also accepts f_start_ghz, f_stop_ghz and n_freq for spectrum sampling.");
    if (!em_.parameterDraftLoaded[fi]) {
      em_.parameterDraft[fi] = activeEmParameters().dump(2);
      em_.parameterDraftLoaded[fi] = true;
    }
    if (kit::secondaryButton("Load current parameters###load_current")) {
      em_.parameterDraft[fi] = activeEmParameters().dump(2);
      em_.parameterError[fi].clear();
    }
    kit::fieldText("request", "Request JSON", &em_.parameterDraft[fi],
                   kit::FieldOpts().withLines(12));
    if (kit::secondaryButton("Apply parameters###apply")) {
      auto request = nlohmann::json::parse(em_.parameterDraft[fi], nullptr, false);
      if (request.is_discarded())
        em_.parameterError[fi] = "Invalid JSON; the existing setup is unchanged.";
      else
        configureEmParameters(request, em_.parameterError[fi]);
    }
    if (!em_.parameterError[fi].empty())
      kit::inlineStatus(em_.parameterError[fi].c_str(), kit::Tone::Err);
  }
  kit::cardEnd();
}

const char *SimManager::emRunBlocker() const {
  if (emForm_ != EmForm::Eigenmode && emStlCheckedPath_ != em_.stlPath) {
    std::string pathError;
    const bool exists = emStlPathValid(em_.stlPath, sizeof(em_.stlPath), pathError);
    emStlCheckedPath_ = em_.stlPath;
    emStlPathExists_ = exists;
  }

  auto positive = [](double v) { return std::isfinite(v) && v > 0.0; };
  if (em_.stdIdx < 0 &&
      ((em_.shape == 0 && (!positive(em_.a_mm) || !positive(em_.b_mm))) ||
       (em_.shape == 1 && !positive(em_.radius_mm))))
    return "Enter positive waveguide dimensions.";
  if (!positive(em_.epsR))
    return "Enter a positive relative permittivity.";
  if (em_.stdIdx < 0 && em_.shape == 0 && em_.b_mm > em_.a_mm)
    return "Set the broad wall at least as large as the narrow wall.";
  if (emForm_ == EmForm::Eigenmode &&
      (em_.nModes < 1 || em_.nModes > 40 ||
       em_.resolution < 24 || em_.resolution > 512))
    return "Use 1–40 modes and a grid resolution of 24–512.";
  if (emForm_ != EmForm::Eigenmode && !positive(em_.lengthMm))
    return "Enter a positive guide length.";
  if (emForm_ == EmForm::Driven &&
      (!positive(em_.fStartGhz) || !positive(em_.fStopGhz) ||
       em_.fStopGhz <= em_.fStartGhz || em_.nFreq < 2 || em_.nFreq > 100001))
    return "Use a rising frequency range and 2–100,001 points.";
  if (emForm_ == EmForm::TimeDomain &&
      (!positive(em_.f0Ghz) || !positive(em_.bwGhz)))
    return "Enter a positive center frequency and bandwidth.";
  if (emForm_ != EmForm::Eigenmode && em_.stlPath[0] && !emStlPathExists_)
    return "Pick an existing .stl file or clear it.";
  if (emStatus_ < 0)
    return "Install an EM backend before running; setup remains editable.";
  if (emStatus_ == 0)
    return "Wait for the electromagnetics backend capability check.";
  const int fi = static_cast<int>(emForm_);
  const auto &native = em_.nativeParameters[fi];
  const auto defaults = emStatusResult_.find("defaults");
  const nlohmann::json *requested = nullptr;
  if (native.contains("solver"))
    requested = &native["solver"];
  else if (defaults != emStatusResult_.end() && defaults->is_object() &&
           defaults->contains(emAnalysis(fi)))
    requested = &(*defaults)[emAnalysis(fi)];
  if (!requested || !requested->is_string() ||
      requested->get_ref<const std::string &>().empty() || *requested == "auto")
    return "Select an installed analysis backend.";
  bool available = false;
  const auto solvers = emStatusResult_.find("solvers");
  if (solvers != emStatusResult_.end() && solvers->is_array())
    for (const auto &solver : *solvers) {
      if (!solver.is_object() ||
          sim::jstr(solver, "name") != *requested ||
          !sim::jbool(solver, "installed", false))
        continue;
      const auto caps = solver.find("capabilities");
      available = caps != solver.end() && caps->is_array() &&
                  std::find(caps->begin(), caps->end(), emAnalysis(fi)) != caps->end();
    }
  if (!available)
    return "Select an installed backend that supports this analysis.";
  if (em_.stlPath[0] && emForm_ != EmForm::Eigenmode && *requested != "openems")
    return "Select a full-wave backend to use STL geometry.";
  if (*requested == "openems" && em_.stdIdx < 0 && em_.shape == 1)
    return "Choose a rectangular waveguide for openEMS modal ports.";
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
  const nlohmann::json params = buildEmRequest();
  const std::string method = std::string("em_") + emAnalysis(static_cast<int>(emForm_));
  emRunning_ = true;
  gEmRunStart = ImGui::GetTime();
  gEmRunForm = (int)emForm_;
  gEmPage[(int)emForm_] = kEmRun; // progress here, then the results page
  EmForm which = emForm_;
  st.rpc.call(
      method, params,
      [this, which](const core::RpcResult &r) {
        emRunning_ = false;
        gEmResultView.owner = nullptr;
        em_.rawResults[static_cast<int>(which)] = r.result;
        bool ok = r.ok && r.result.is_object() &&
                  !r.result.contains("code") &&
                  r.result.value("ok", true);
        std::string err;
        if (!ok)
          err = sim::jstr(r.result, "hint",
                    sim::jstr(r.result, "error",
                        sim::jstr(r.result, "message",
                            sim::jstr(r.result, "reason",
                                r.err.empty() ? "EM solve failed" : r.err))));
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
            const auto &ports = sim::jobj(r.result, "ports");
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
        // Still watching the progress: move on to the outcome (plots or the
        // failure); a user who navigated elsewhere stays where they are.
        if (gEmPage[(int)which] == kEmRun)
          gEmPage[(int)which] = kEmResults;
      },
      120000);
}

// The active form's Setup page: "About this analysis" (collapsed), the shared
// Waveguide card (collapsed to a short "Waveguide · WR-90" title, expanded on
// demand or by the Boundaries command), then the analysis card. Every input is
// a kit field (label left / value right, unit slot, commit on deactivate); ids
// are literals scoped by the card's field group.
void SimManager::drawEmForm(AppState &st) {
  bool changed = false;
  auto applyStandard = [&](int index) {
    if (index < 0 || index >= kNEmStandards)
      return;
    changed |= em_.stdIdx != index;
    em_.stdIdx = index;
    // Standards select a separate spec; never overwrite retained Custom dimensions.
    gEmWgOpen = true;
  };
  const bool us = unitSys(st).us;
  const auto mm = sim::fieldU("mm", us).withMin(0);
  kit::FieldOpts ghz;
  ghz.withUnit(kit::Unit::GHz).withMin(0);

  // One-shot landing for the Ports/Boundaries/Frequency Range ribbon
  // commands. The pane body is THIS window (fields draw inside card child
  // windows, so the scroll goes to the body explicitly). Boundaries puts the
  // Waveguide card at the top; Ports / Frequency Range keep the analysis
  // card's title in view when the target field (and the row after it) still
  // fits under it, else the field itself leads. The target's card is then
  // washed with the accent for a moment so the jump reads even when nothing
  // scrolled.
  ImGuiWindow *body = ImGui::GetCurrentWindow();
  const double now = ImGui::GetTime();
  auto reveal = [&](float cardTop) {
    const float fieldTop = ImGui::GetCursorScreenPos().y;
    const float need =
        fieldTop + 2.0f * ImGui::GetFrameHeightWithSpacing() - cardTop;
    const float top = need <= body->InnerRect.GetHeight() ? cardTop : fieldTop;
    ImGui::SetScrollFromPosY(body, top - body->Pos.y - kit::rowGap(), 0.0f);
  };
  auto land = [&](int target, float cardTop) {
    if (gEmFocus != target)
      return;
    reveal(cardTop);
    gEmFocus = kEmFocusNone;
    gEmFlashTarget = target;
    gEmFlashUntil = now + kEmFlashS;
  };
  auto tint = [&](int a, int b) -> const float * {
    return (gEmFlashTarget == a || gEmFlashTarget == b) && now < gEmFlashUntil
               ? ui::palette.accent
               : nullptr;
  };
  auto drawReadiness = [&] {
    auto positive = [](double value) { return std::isfinite(value) && value > 0; };
    const bool dimensions = positive(em_.epsR) &&
        (em_.stdIdx >= 0 ||
         (em_.shape == 1 ? positive(em_.radius_mm)
                        : positive(em_.a_mm) && positive(em_.b_mm) && em_.a_mm >= em_.b_mm));
    const bool geometry = dimensions &&
        (emForm_ == EmForm::Eigenmode ||
         (positive(em_.lengthMm) && (!em_.stlPath[0] || emStlPathExists_)));
    kit::inlineStatus(geometry ? "Geometry: dimensions and geometry input are ready."
                              : "Geometry: correct dimensions, guide length or the STL path.",
                       geometry ? kit::Tone::Ok : kit::Tone::Warn);
    if (emForm_ == EmForm::Eigenmode) {
      kit::dimText("Frequency: cutoff frequencies are outputs; no excitation is required.");
    } else {
      const bool frequency = emForm_ == EmForm::Driven
          ? positive(em_.fStartGhz) && positive(em_.fStopGhz) &&
            em_.fStopGhz > em_.fStartGhz && em_.nFreq >= 2 && em_.nFreq <= 100001
          : positive(em_.f0Ghz) && positive(em_.bwGhz);
      kit::inlineStatus(frequency ? "Frequency: excitation and sampling inputs are ready."
                                 : "Frequency: enter positive excitation values and a rising sweep.",
                         frequency ? kit::Tone::Ok : kit::Tone::Warn);
    }
  };

  static const char *const kAbout[3] = {
      "Solves the cutoff frequency of each waveguide mode from the "
      "cross-section alone (2D eigen solve).",
      "Computes S11 and S21 across a frequency sweep for a straight guide "
      "section between two matched modal ports.",
      "Launches a Gaussian-modulated pulse at port 1 and records both port "
      "voltages over time."};
  if (kit::cardBegin("about", "About this analysis", "study_em", &gEmAboutOpen))
    kit::dimText(kAbout[(int)emForm_]);
  kit::cardEnd();

  if (kit::cardBegin("backend", "Analysis backend", "study_em")) {
    kit::FieldGroup group("backend_fields", "Backend");
    const int fi = static_cast<int>(emForm_);
    const auto defaults = emStatusResult_.find("defaults");
    const auto &native = em_.nativeParameters[fi];
    const nlohmann::json *choice = native.contains("solver") ? &native["solver"] : nullptr;
    if (!choice && defaults != emStatusResult_.end() && defaults->is_object() &&
        defaults->contains(emAnalysis(fi)))
      choice = &(*defaults)[emAnalysis(fi)];
    const std::string empty;
    const std::string &current = choice && choice->is_string()
                                    ? choice->get_ref<const std::string &>() : empty;
    const auto &names = em_.backendNames[fi];
    const auto found = std::find(names.begin(), names.end(), current);
    int selected = found == names.end() ? -1 : static_cast<int>(found - names.begin());
    if (gEmBackendLabelOwner != this || gEmBackendLabelForm != fi) {
      auto &labels = gEmBackendLabels[fi];
      labels.clear();
      for (const auto &name : names) {
        const char *label = emBackendLabel(name);
        labels.push_back(label ? label : simStageLabel(name));
      }
      gEmBackendLabelOwner = this;
      gEmBackendLabelForm = fi;
    }
    if (kit::fieldCombo("solver", "Backend", &selected, gEmBackendLabels[fi],
                        kit::FieldOpts().withHint("No installed analysis backend")
                            .withTip(current.c_str()))) {
      if (selected >= 0 && selected < static_cast<int>(names.size())) {
        auto request = activeEmParameters();
        request["solver"] = names[selected];
        if (names[selected] != "openems") {
          request.erase("cells_per_wavelength");
          request.erase("max_timesteps");
          request.erase("end_criteria");
          if (emForm_ == EmForm::TimeDomain) {
            request.erase("f_start_ghz");
            request.erase("f_stop_ghz");
            request.erase("n_freq");
          }
        }
        if (names[selected] != "analytic-uniform")
          request.erase("t_max_ns");
        std::string error;
        if (!configureEmParameters(request, error))
          st.toast(ui::ToastLevel::Warn, error);
      }
    }
    kit::dimText("Installed solvers are checked automatically. Runs use the "
                 "selected backend explicitly; failures do not switch solvers.");
    if (current == "analytic-uniform")
      kit::dimText("Uniform section only: ideal matched ports and conducting "
                   "walls. Eigenmode grid resolution and field output apply "
                   "to the FDFD backend, not this closed-form model.");
  }
  kit::cardEnd();

  if (kit::cardBegin("standard_palette", "Standard waveguide palette", "em_port",
                     &gEmPaletteOpen)) {
    kit::dimText("Click a standard or drag it to Apply waveguide preset. "
                 "Custom dimensions remain available when you switch back.");
    for (int i = 0; i < kNEmStandards; ++i) {
      ImGui::PushID(i);
      if (kit::secondaryButton(kEmStandards[i].name, true, nullptr,
                               ImVec2(ImGui::GetContentRegionAvail().x, 0)))
        applyStandard(i);
      if (!emRunning_ && ImGui::BeginDragDropSource()) {
        ImGui::SetDragDropPayload(kEmStandardDragPayload, &i, sizeof(i));
        kit::dimText(emStandardItems(us)[i].c_str());
        ImGui::EndDragDropSource();
      }
      ImGui::PopID();
    }
  }
  kit::cardEnd();

  const bool isStd = em_.stdIdx >= 0 && em_.stdIdx < kNEmStandards;
  // Short title (the collapsed card must not elide); the dimensions are rows.
  const std::string wgTitle =
      "Waveguide \xc2\xb7 " + emWaveguideName(em_.stdIdx, em_.shape);
  {
    const float cardTop = ImGui::GetCursorScreenPos().y;
    land(kEmFocusWaveguide, cardTop);
  }
  if (kit::cardBegin("waveguide", wgTitle.c_str(), "em_port", &gEmWgOpen,
                     tint(kEmFocusWaveguide, kEmFocusWaveguide))) {
    kit::dimText("Matched modal ports at both ends; conducting walls.");
    kit::FieldGroup g("wg", "Narrow wall b");
    const std::vector<std::string> &items = emStandardItems(us);
    int sel = isStd ? em_.stdIdx : kNEmStandards;
    if (kit::fieldCombo(
            "std", "Standard", &sel, items,
            kit::FieldOpts()
                .withPreview(isStd ? kEmStandards[em_.stdIdx].name : "Custom")
                .withTip("EIA rectangular-waveguide standard (WR-90 is X "
                         "band). Choose Custom for an arbitrary rectangular "
                         "or circular cross-section."))) {
      if (sel >= 0 && sel < kNEmStandards)
        applyStandard(sel);
      else {
        changed |= em_.stdIdx != -1;
        em_.stdIdx = -1;
      }
    }
    kit::secondaryButton("Apply waveguide preset###preset_drop", true, nullptr,
                          ImVec2(ImGui::GetContentRegionAvail().x, 0));
    int dropped = -1;
    if (!emRunning_ && emAcceptStandard(dropped))
      applyStandard(dropped);
    if (em_.stdIdx >= 0 && em_.stdIdx < kNEmStandards) {
      // One value per row so neither dimension is ever elided.
      kit::fieldReadout(
          "dim_a", "Broad wall a",
          sim::fmtU(kEmStandards[em_.stdIdx].a_mm, "mm", us).c_str(),
          kit::FieldOpts().withTip("Inner broad-wall width of the standard "
                                   "guide; sets the TE10 cutoff."));
      kit::fieldReadout(
          "dim_b", "Narrow wall b",
          sim::fmtU(kEmStandards[em_.stdIdx].b_mm, "mm", us).c_str(),
          kit::FieldOpts().withTip("Inner narrow-wall height of the standard "
                                   "guide."));
    } else {
      static const char *const kShapes[] = {"Rectangular", "Circular"};
      int shape = em_.shape == 1 ? 1 : 0;
      if (kit::fieldEnum("shape", "Cross-section", &shape, kShapes, 2)) {
        em_.shape = shape;
        changed = true;
      }
      if (em_.shape == 0) {
        changed |= kit::fieldNumber("a", "Broad wall a", &em_.a_mm,
                         kit::FieldOpts(mm).withTip(
                             "Inner broad-wall width; sets the TE10 cutoff."));
        changed |= kit::fieldNumber("b", "Narrow wall b", &em_.b_mm,
                         kit::FieldOpts(mm).withTip("Inner narrow-wall height."));
      } else {
        changed |= kit::fieldNumber("r", "Radius", &em_.radius_mm,
                         kit::FieldOpts(mm).withTip("Inner radius of the "
                                                    "circular guide."));
      }
    }
    changed |= kit::fieldNumber("eps_r", "Relative permittivity", &em_.epsR,
                                kit::FieldOpts().withMin(0));
    kit::fieldReadout("boundaries", "Boundary model", "Conducting guide walls");
  }
  kit::cardEnd();

  const float anTop = ImGui::GetCursorScreenPos().y;
  const float *anTint = tint(kEmFocusPorts, kEmFocusRange);
  if (emForm_ == EmForm::Eigenmode) {
    if (kit::cardBegin("eigen", "Cutoff modes", "study_em", nullptr, anTint)) {
      kit::FieldGroup g("eig", "Grid resolution");
      changed |= kit::fieldInt("n", "Modes", &em_.nModes,
                    kit::FieldOpts().withRange(1, 40).withTip(
                        "How many cutoff modes to solve, ordered by cutoff "
                        "frequency."));
      changed |= kit::fieldInt("res", "Grid resolution", &em_.resolution,
                    kit::FieldOpts().withRange(24, 512).withTip(
                        "FDFD eigensolver grid cells across the "
                        "cross-section. Higher is more accurate and slower; "
                        "the error against the closed-form cutoff is "
                        "reported per mode."));
    }
    kit::cardEnd();
    if (changed)
      invalidateEmResults();
    drawEmParameterEditor();
    drawReadiness();
    return;
  }

  const bool driven = emForm_ == EmForm::Driven;
  if (kit::cardBegin(driven ? "sweep" : "pulse",
                     driven ? "Frequency sweep" : "Pulse excitation",
                     driven ? "freq_range" : "em_port", nullptr, anTint)) {
    kit::FieldGroup g(driven ? "drv" : "td", "Frequency points");
    changed |= kit::fieldNumber("len", "Guide length", &em_.lengthMm,
                     kit::FieldOpts(mm).withTip(
                         "Straight guide section between the two matched "
                         "modal ports."));
    // The ports are implicit (matched modal ports at both ends); what the user
    // sets is the mode port 1 launches — the Ports command lands here.
    land(kEmFocusPorts, anTop);
    int mi = (em_.modeIdx >= 0 && em_.modeIdx < kNEmModes) ? em_.modeIdx : 0;
    if (kit::fieldEnum("mode", "Port 1 mode", &mi, kEmModes, kNEmModes,
                       kit::FieldOpts().withTip(
                           "Waveguide mode launched at port 1. Both ports are "
                           "matched modal ports; port 2 absorbs the same "
                           "mode.")))
    {
      em_.modeIdx = mi;
      em_.exactMode.clear();
      changed = true;
    }
    if (!em_.exactMode.empty())
      kit::fieldReadout("exact_mode", "Solver mode name", em_.exactMode.c_str());
    if (driven) {
      land(kEmFocusRange, anTop);
      changed |= kit::fieldNumber("f0", "Start frequency", &em_.fStartGhz,
                       kit::FieldOpts(ghz));
      changed |= kit::fieldNumber(
          "f1", "Stop frequency", &em_.fStopGhz,
          kit::FieldOpts(ghz).withIssue(em_.fStopGhz <= em_.fStartGhz
                                            ? "Must be above the start "
                                              "frequency."
                                            : nullptr));
      changed |= kit::fieldInt("nf", "Frequency points", &em_.nFreq,
                    kit::FieldOpts().withRange(2, 100001).withTip(
                        "Samples across the sweep, start and stop included."));
    } else {
      changed |= kit::fieldNumber("f0", "Center frequency", &em_.f0Ghz,
                       kit::FieldOpts(ghz).withTip(
                           "Center of the Gaussian-modulated pulse."));
      changed |= kit::fieldNumber("bw", "Bandwidth", &em_.bwGhz,
                       kit::FieldOpts(ghz).withTip(
                           "Gaussian bandwidth parameter (bw_ghz), not the full width "
                           "of the spectrum."));
    }
    if (driven && std::isfinite(em_.fStartGhz) && std::isfinite(em_.fStopGhz) &&
        std::isfinite(em_.fStartGhz + em_.fStopGhz) &&
        em_.fStopGhz > em_.fStartGhz && em_.fStartGhz > 0) {
      double center = 0.5 * (em_.fStartGhz + em_.fStopGhz);
      double span = em_.fStopGhz - em_.fStartGhz;
      const double oldCenter = center;
      const double oldSpan = span;
      const double half = span * 0.5;
      emLogSlider("center_drag", "Sweep center", &center,
                  std::min(center, half + 0.001), std::max(300.0, center + half),
                  kit::FieldOpts().withUnit(kit::Unit::GHz));
      if (center != oldCenter) {
        em_.fStartGhz = center - half;
        em_.fStopGhz = center + half;
        changed = true;
      }
      emLogSlider("span_drag", "Sweep span", &span, std::min(span, 0.001),
                  std::max(span, 2.0 * (center - 0.0001)),
                  kit::FieldOpts().withUnit(kit::Unit::GHz));
      if (span != oldSpan) {
        em_.fStartGhz = center - span * 0.5;
        em_.fStopGhz = center + span * 0.5;
        changed = true;
      }
      kit::fieldReadout("frequency_step", "Sample interval",
          kit::fmtQuantity((em_.fStopGhz - em_.fStartGhz) /
                           std::max(1, em_.nFreq - 1),
                           kit::unitText(kit::Unit::GHz)).c_str());
    } else if (!driven) {
      changed |= emLogSlider("pulse_center_drag", "Adjust pulse center",
                             &em_.f0Ghz, std::min(0.01, em_.f0Ghz), std::max(300.0, em_.f0Ghz),
                             kit::FieldOpts().withUnit(kit::Unit::GHz));
      changed |= emLogSlider("pulse_bandwidth_drag", "Adjust bandwidth",
                             &em_.bwGhz, std::min(0.01, em_.bwGhz), std::max(300.0, em_.bwGhz),
                             kit::FieldOpts().withUnit(kit::Unit::GHz));
    }
    bool browse = false;
    ImGui::BeginGroup(); // Target the complete path field, not only Browse.
    changed |= kit::fieldPath(
        "stl", "Geometry STL", em_.stlPath, sizeof(em_.stlPath), &browse,
        kit::FieldOpts().withHint("Optional")
            .withTip("Drop an existing .stl file here. Real geometry requires "
                     "an installed full-wave backend selected explicitly.")
            .withIssue(em_.stlPath[0] && !emStlPathExists_
                           ? "An existing .stl mesh file is required." : nullptr));
    ImGui::EndGroup();
    if (!emRunning_ && ImGui::BeginDragDropTarget()) {
      if (const ImGuiPayload *payload =
              ImGui::AcceptDragDropPayload(kFileDragPayload)) {
        if (payload->IsDelivery()) {
          const char *data = static_cast<const char *>(payload->Data);
          const void *nul = payload->DataSize > 0
                                ? std::memchr(data, '\0', payload->DataSize)
                                : nullptr;
          std::string error;
          if (!nul || nul != data + payload->DataSize - 1)
            error = "Invalid file payload; the previous geometry is unchanged.";
          else {
            const std::string path(data, static_cast<std::size_t>(payload->DataSize - 1));
            if (path.empty())
              error = "An empty file drop cannot replace the current geometry.";
            else if (emStlPathValid(path, sizeof(em_.stlPath), error)) {
              std::snprintf(em_.stlPath, sizeof(em_.stlPath), "%s", path.c_str());
              changed = true;
            }
          }
          if (!error.empty())
            st.toast(ui::ToastLevel::Warn, error);
        }
      }
      ImGui::EndDragDropTarget();
    }
    if (browse) {
      const std::string path = ui::openFileDialog("STL mesh (*.stl)\0*.stl\0");
      std::string error;
      if (!path.empty()) {
        if (emStlPathValid(path, sizeof(em_.stlPath), error)) {
          std::snprintf(em_.stlPath, sizeof(em_.stlPath), "%s", path.c_str());
          changed = true;
        } else {
          st.toast(ui::ToastLevel::Warn, error);
        }
      }
    }
  }
  kit::cardEnd();
  if (changed)
    invalidateEmResults();
  drawEmParameterEditor();
  drawReadiness();
}

void SimManager::drawEmPanel(AppState &st) {
  // Per-form labels. Full tab labels match the EM ribbon's Study commands.
  static const char *const kFull[] = {"Eigenmode", "Freq Sweep",
                                      "Time Domain"};
  static const char *const kShort[] = {"Eigen", "Sweep", "Time"};
  static const char *const kTips[] = {
      "Cutoff frequencies of the guide's modes (2D eigen solve).",
      "S11 / S21 against frequency for a guide section (S-parameters).",
      "Port voltages for a Gaussian pulse launched down the guide."};
  // "Run <analysis>" in the ribbon's Title Case (like "Run Modal"); the
  // Results-step re-run is the plain secondary "Re-run" the studies use.
  static const char *const kRun[] = {"Run Eigenmode", "Run Freq Sweep",
                                     "Run Time Domain"};
  static const char *const kStage[] = {"Solving cutoff modes",
                                       "Sweeping S-parameters",
                                       "Propagating the pulse"};
  static const char *const kPages[3][3] = {
      {"eigen_setup", "eigen_run", "eigen_results"},
      {"sweep_setup", "sweep_run", "sweep_results"},
      {"time_setup", "time_run", "time_results"}};

  // Refresh the validation (and its cached STL existence probe) before any
  // field reads emStlPathExists_. Re-derived when the form tabs switch forms.
  const char *formBlocker = nullptr;
  bool runningHere = false;
  int result = -1;
  auto derive = [&] {
    formBlocker = emRunBlocker();
    runningHere = emRunning_ && gEmRunForm == (int)emForm_;
    result = emForm_ == EmForm::Eigenmode ? em_.eigResult
             : emForm_ == EmForm::Driven  ? em_.drvResult
                                          : em_.tdResult;
  };
  derive();

  kit::panelBegin("sim_em", &simSurf());
  const char *status = runningHere   ? "Solving"
                       : result == 1 ? "Solved"
                       : result == 0 ? "Failed"
                                     : nullptr;
  const kit::Tone statusTone = runningHere   ? kit::Tone::Accent
                               : result == 1 ? kit::Tone::Ok
                                             : kit::Tone::Err;
  // No type chip: the title already names the domain and the form tabs name
  // the analysis; the status chip carries the run state.
  if (simPanelHeader(st, "study_em", "Electromagnetics", nullptr, status,
                     statusTone,
                     "Close the EM panel. Inputs and the last results stay "
                     "for this session.")) {
    emPanelOpen_ = false;
    kit::panelEnd();
    return;
  }

  int form = (int)emForm_;
  if (kit::formTabs("forms", kFull, kShort, 3, &form, kTips)) {
    emForm_ = (EmForm)form;
    derive();
  }
  const int fi = (int)emForm_;

  // Setup -> Run -> Results per form, the same track every study shows: Run
  // holds the progress and what is (or was last) solved, Results the outcome.
  const bool solverUp = emStatus_ == 1;
  if (solverUp) {
    const kit::Status outSt = result == 1   ? kit::Status::Done
                              : result == 0 ? kit::Status::Failed
                                            : kit::Status::Pending;
    kit::StepItem steps[3];
    steps[0] = {"Setup", formBlocker ? kit::Status::Retry : kit::Status::Done,
                true, formBlocker ? formBlocker : "Setup is complete."};
    steps[1] = {"Run", runningHere ? kit::Status::Running : outSt, true,
                "Live progress and the settings being solved."};
    steps[2] = {"Results", runningHere ? kit::Status::Pending : outSt,
                result >= 0 || gEmPage[fi] == kEmResults,
                result >= 0 ? "Inspect the results." : "Run the analysis first."};
    int page = gEmPage[fi];
    if (kit::stepper("steps", steps, 3, &page))
      gEmPage[fi] = page;
  }
  const int page = solverUp ? gEmPage[fi] : kEmSetup;

  kit::panelBodyBegin(kPages[fi][page]);
  if (emStatus_ < 0) {
    kit::blockedState("Electromagnetics solver not installed",
                      "No electromagnetics solver is available on this machine.",
                      "Install an EM solver, then restart the simulation service.");
  }
  if (emStatus_ == 0)
    kit::spinner("Checking for the electromagnetics solver\xe2\x80\xa6");
  if (page == kEmSetup) {
    ImGui::BeginDisabled(emRunning_);
    drawEmForm(st);
    ImGui::EndDisabled();
    derive(); // Edits invalidate prior results and readiness in this same frame.
  } else if (page == kEmRun) {
    // The settings this run solves (the footer carries the stage, the
    // progress bar and the elapsed time, the stepper the run state).
    const char *ghzU = kit::unitText(kit::Unit::GHz);
    const int mi =
        (em_.modeIdx >= 0 && em_.modeIdx < kNEmModes) ? em_.modeIdx : 0;
    std::vector<kit::KeyValueItem> rows;
    rows.push_back({"Waveguide", emWaveguideName(em_.stdIdx, em_.shape)});
    rows.push_back({"Inner size",
                    emWaveguideDims(em_.stdIdx, em_.shape, em_.a_mm, em_.b_mm,
                                    em_.radius_mm, unitSys(st).us)});
    if (emForm_ == EmForm::Eigenmode) {
      rows.push_back({"Modes", std::to_string(em_.nModes)});
      rows.push_back({"Grid resolution", std::to_string(em_.resolution)});
    } else {
      rows.push_back({"Guide length", sim::fmtU(em_.lengthMm, "mm", unitSys(st).us)});
      rows.push_back({"Port 1 mode", kEmModes[mi]});
      if (emForm_ == EmForm::Driven) {
        rows.push_back({"Frequency", kit::fmtValue(em_.fStartGhz) +
                                         "\xe2\x80\x93" +
                                         kit::fmtQuantity(em_.fStopGhz, ghzU)});
        rows.push_back({"Points", std::to_string(em_.nFreq)});
        rows.push_back(
            {"Geometry",
             em_.stlPath[0]
                 ? std::filesystem::path(em_.stlPath).filename().string()
                 : std::string("Analytic (no STL)")});
      } else {
        rows.push_back({"Center frequency", kit::fmtQuantity(em_.f0Ghz, ghzU)});
        rows.push_back({"Bandwidth", kit::fmtQuantity(em_.bwGhz, ghzU)});
      }
    }
    kit::keyValueCard("run_settings", "Run settings", "study_em", rows);
    if (!runningHere && result >= 0 &&
        kit::secondaryButton("View results###view_results"))
      gEmPage[fi] = kEmResults;
  } else {
    if (result == 1 &&
        (gEmResultView.owner != this || gEmResultView.form != fi)) {
      auto &view = gEmResultView;
      view = EmResultView{};
      view.owner = this;
      view.form = fi;
      const char *ghz = kit::unitText(kit::Unit::GHz);
      if (emForm_ == EmForm::Eigenmode) {
        for (size_t i = 0; em_.eigModes.is_array() && i < em_.eigModes.size(); ++i) {
          const auto &mode = em_.eigModes[i];
          if (!mode.is_object())
            continue;
          const double cutoff = mode.value("fc_ghz", 0.0);
          view.modeIndex.push_back(static_cast<float>(i + 1));
          view.cutoff.push_back(static_cast<float>(cutoff));
          double error = 0.0;
          const std::string type = mode.value("type", std::string());
          view.modes.push_back({
              mode.value("name", std::string("?")),
              type == "TE" ? "Transverse electric" :
              type == "TM" ? "Transverse magnetic" : simStageLabel(type),
              kit::fmtValue(cutoff),
              finiteNumber(mode, "analytic_err_pct", error)
                  ? kit::fmtValue(error, 3) : std::string("\xe2\x80\x94"),
              "Cutoff wavenumber: " +
                  kit::fmtQuantity(mode.value("kc_per_m", 0.0), "rad/m")});
        }
        if (!view.modes.empty())
          view.metrics.push_back({"Dominant mode",
              view.modes.front().name + " \xc2\xb7 " +
              kit::fmtQuantity(view.cutoff.front(), ghz), kit::Tone::Accent});
        if (view.cutoff.size() >= 2)
          view.metrics.push_back({"Single-mode band",
              kit::fmtValue(view.cutoff[0]) + "\xe2\x80\x93" +
              kit::fmtQuantity(view.cutoff[1], ghz)});
        view.metrics.push_back({"Modes found", std::to_string(view.modes.size())});
      } else if (emForm_ == EmForm::Driven) {
        const auto best = std::min_element(em_.drvS11.begin(), em_.drvS11.end());
        const auto worst = std::min_element(em_.drvS21.begin(), em_.drvS21.end());
        if (best != em_.drvS11.end() &&
            static_cast<size_t>(best - em_.drvS11.begin()) < em_.drvFreq.size())
          view.metrics.push_back({"Best match (S11)",
              kit::fmtQuantity(*best, "dB") + " @ " +
              kit::fmtQuantity(em_.drvFreq[best - em_.drvS11.begin()], ghz)});
        if (worst != em_.drvS21.end())
          view.metrics.push_back({"Lowest transmission (S21)",
                                 kit::fmtQuantity(*worst, "dB")});
        view.metrics.push_back({"Frequency samples", std::to_string(em_.drvFreq.size())});
      } else {
        auto peak = [](const std::vector<float> &values) {
          float p = 0.0f;
          for (float value : values)
            p = std::max(p, std::fabs(value));
          return p;
        };
        const char *volt = kit::unitText(kit::Unit::V);
        view.metrics.push_back({"Port 1 peak", kit::fmtQuantity(peak(em_.tdPort1), volt)});
        view.metrics.push_back({"Port 2 peak", kit::fmtQuantity(peak(em_.tdPort2), volt)});
        view.metrics.push_back({"Time samples", std::to_string(em_.tdTime.size())});
      }
    }
    if (runningHere) {
      kit::spinner("Solving\xe2\x80\xa6 results appear here when the worker "
                   "returns.");
    } else if (result == 0) {
      const std::string &err = emForm_ == EmForm::Eigenmode ? em_.eigError
                               : emForm_ == EmForm::Driven  ? em_.drvError
                                                            : em_.tdError;
      const std::string message =
          err.find_first_of(" \t\n") == std::string::npos ? simStageLabel(err) : err;
      if (kit::blockedState("Solve failed", message.c_str(),
                            "Check the setup, then run again.", "Go to Setup"))
        gEmPage[fi] = kEmSetup;
      kit::itemTooltip(err.c_str());
    } else if (result < 0) {
      // No action here: the footer owns the one Run primary (and, when setup
      // is incomplete, the reason and a "Go to Setup").
      const std::string body =
          std::string("Press ") + kRun[fi] + " below to see " +
          (emForm_ == EmForm::Eigenmode
               ? "the cutoff frequency of each mode of this cross-section."
           : emForm_ == EmForm::Driven
               ? "S11 and S21 across the frequency sweep."
               : "the port-voltage traces of the pulse.");
      kit::emptyState("study_em", "No results yet", body.c_str());
    } else if (emForm_ == EmForm::Eigenmode) {
      if (kit::cardBegin("outcome", "Waveguide modes solved", "study_em")) {
        kit::chipFlowBegin("eig_outcome");
        kit::chipFlowItem(ImGui::GetTextLineHeight());
        kit::statusGlyphInline(kit::Status::Done);
        kit::flowChip("Solved", kit::Tone::Ok, true);
        kit::chipFlowEnd();
        kit::dimText("The lowest cutoff defines the dominant propagating mode.");
      }
      kit::cardEnd();
      kit::keyValueCard("key_metrics", "Key results", "study_em", gEmResultView.metrics);
      kit::PlotSpec cutoff;
      cutoff.x = gEmResultView.modeIndex;
      cutoff.xAxis.title = "Mode order (lowest cutoff first)";
      cutoff.xAxis.integer = true;
      cutoff.xAxis.zero = false;
      cutoff.yAxis.title = "Cutoff frequency";
      cutoff.yAxis.unit = kit::unitText(kit::Unit::GHz);
      cutoff.series.push_back({"Modes", gEmResultView.cutoff});
      cutoff.series.back().points = true;
      kit::linePlot("cutoff_plot", cutoff);
      if (kit::cardBegin("mode_details", "Mode details", "study_em", &gEmModesOpen)) {
        if (kit::tableBegin("modes", {{"Mode"},
                                      {"Type", kit::TableAlign::Left, 0.6f, true},
                                      {"Cutoff (GHz)", kit::TableAlign::Right},
                                      {"Error (%)", kit::TableAlign::Right}})) {
          for (size_t i = 0; i < gEmResultView.modes.size(); ++i) {
            const auto &mode = gEmResultView.modes[i];
            kit::tableRow();
            kit::tableCell(mode.name.c_str(),
                           i == 0 ? kit::Tone::Accent : kit::Tone::Neutral,
                           mode.tooltip.c_str());
            kit::tableCell(mode.type.c_str());
            kit::tableCell(mode.cutoff.c_str());
            kit::tableCell(mode.error.c_str(), kit::Tone::Neutral,
                           "Error against the closed-form analytic cutoff.");
          }
        }
        kit::tableEnd();
      }
      kit::cardEnd();
    } else if (emForm_ == EmForm::Driven) {
      const char *dB = "dB";
      if (kit::cardBegin("outcome", "Frequency sweep solved", "study_em")) {
        kit::chipFlowBegin("drv_outcome");
        kit::chipFlowItem(ImGui::GetTextLineHeight());
        kit::statusGlyphInline(kit::Status::Done);
        kit::flowChip("Solved", kit::Tone::Ok, true);
        kit::chipFlowEnd();
        kit::dimText("Compare reflection at port 1 with transmission to port 2.");
      }
      kit::cardEnd();
      kit::keyValueCard("key_metrics", "Key results", "study_em", gEmResultView.metrics);
      // S11 and S21 live on very different dB scales (a matched section has
      // S11 near the noise floor and S21 near 0), so each gets its own chart.
      kit::PlotSpec s11;
      s11.x = em_.drvFreq;
      s11.xAxis.title = "Frequency";
      s11.xAxis.unit = kit::unitText(kit::Unit::GHz);
      s11.yAxis.title = "Reflection S11";
      s11.yAxis.unit = dB;
      s11.series.push_back({"S11", em_.drvS11});
      s11.emptyText = "Not enough samples to plot.";
      kit::linePlot("s11", s11);
      kit::PlotSpec s21 = s11;
      s21.yAxis.title = "Transmission S21";
      s21.series.clear();
      s21.series.push_back({"S21", em_.drvS21});
      kit::linePlot("s21", s21);
    } else {
      const char *volt = kit::unitText(kit::Unit::V);
      if (kit::cardBegin("outcome", "Pulse propagation solved", "study_em")) {
        kit::chipFlowBegin("td_outcome");
        kit::chipFlowItem(ImGui::GetTextLineHeight());
        kit::statusGlyphInline(kit::Status::Done);
        kit::flowChip("Solved", kit::Tone::Ok, true);
        kit::chipFlowEnd();
        kit::dimText("Compare the launched pulse with the received port voltage.");
      }
      kit::cardEnd();
      kit::keyValueCard("key_metrics", "Key results", "study_em", gEmResultView.metrics);
      kit::PlotSpec p;
      p.x = em_.tdTime;
      p.xAxis.title = "Time";
      p.xAxis.unit = "ns";
      p.yAxis.title = "Port voltage";
      p.yAxis.unit = volt;
      p.series.push_back({"Port 1", em_.tdPort1});
      p.series.push_back({"Port 2", em_.tdPort2});
      p.emptyText = "Not enough samples to plot.";
      kit::linePlot("ports", p);
    }
  }
  if (page == kEmResults && result >= 0 && em_.rawResults[fi].is_object()) {
    if (kit::cardBegin("execution", "Execution details", "study_em", &gEmExecutionOpen)) {
      const auto &raw = em_.rawResults[fi];
      const auto solver = raw.find("solver");
      const std::string empty;
      const std::string &backend = solver != raw.end() && solver->is_string()
                                       ? solver->get_ref<const std::string &>() : empty;
      const char *label = emBackendLabel(backend);
      const std::string human = label ? label : simStageLabel(backend);
      kit::fieldReadout("solver_result", "Backend",
                        backend.empty() ? "Not reported" : human.c_str(),
                        kit::FieldOpts().withTip(backend.c_str()));
      if (raw.contains("solver_fallback"))
        kit::inlineStatus("The solver reported a fallback; copy the result JSON for details.",
                           kit::Tone::Warn);
      if (kit::secondaryButton("Copy result JSON###copy_native_result"))
        ImGui::SetClipboardText(raw.dump(2).c_str());
    }
    kit::cardEnd();
  }
  kit::panelBodyEnd();

  kit::panelFooterBegin();
  kit::ActionSpec a;
  // The footer owns readiness, including missing or still-checking backends.
  const bool rerun = page == kEmResults && result == 1;
  a.primaryLabel = rerun ? "Re-run" : kRun[fi];
  a.primaryAsSecondary = rerun;
  a.blocker = emRunning_ && !runningHere
                  ? "Wait for the other EM analysis to finish."
                  : formBlocker;
  a.primaryEnabled = solverUp && !a.blocker && !emRunning_;
  if (formBlocker && solverUp && page != kEmSetup && !emRunning_)
    a.secondaryLabel = "Go to Setup";
  a.running = runningHere;
  a.stage = kStage[fi];
  a.elapsed = ImGui::GetTime() - gEmRunStart;
  a.progress = -1.0f; // Native worker calls do not report fractional progress.
  a.cancellable = false;
  a.cancelBlocker = "this step runs in the engine and can't be interrupted.";
  switch (kit::actionFooter("run", a)) {
  case kit::FooterAction::Primary:
    runEmForm(st);
    break;
  case kit::FooterAction::Secondary:
    gEmPage[fi] = kEmSetup;
    break;
  default:
    break;
  }
  kit::panelEnd();
}

} // namespace ui
