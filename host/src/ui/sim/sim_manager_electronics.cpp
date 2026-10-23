// SimManager electronics: circuit simulation, firmware emulation, ECAD thermal/heatsink hand-off.
#include "sim_manager_internal.h"

#include <cfloat>
#include <cstdio>
#include <sstream>

#include "sim_common.h" // sim::warningsList — engine warnings as inline statuses
#include "sim_fw_scenario.h"


namespace ui {

namespace {
// Footer timing for the in-flight worker calls (none of them reports
// progress, so the footer shows a marquee + elapsed time).
double gElRunStart = 0.0;
double gFwRunStart = 0.0;
double gHoRunStart = 0.0;
double gHsRunStart = 0.0;
// Collapsible result cards (session-only disclosure state).
bool gElLogOpen = false;
bool gFwUartOpen = true;
// Stepper page per panel. Circuit simulation and firmware emulation follow the
// studies' Setup -> Run -> Results track: a dispatch lands on Run (progress +
// the settings being solved) and the completion moves on to Results. The
// thermal handoff is a send, not an analysis run: Setup -> Results (0 / 1).
enum RunPage { kPageSetup = 0, kPageRun, kPageResults };
int gElPage = kPageSetup;
int gFwPage = kPageSetup;
int gHoPage = 0;

// Electronics analyses: "Run <analysis>" until a result exists, then the
// plain "Re-run" every study uses.
const char *const kElRun[4] = {"Run operating point", "Run DC sweep",
                               "Run AC sweep", "Run transient"};

// A finished call moves a user still watching its progress on to the outcome;
// one who navigated elsewhere stays where they are.
void advanceToResults(int &page) {
  if (page == kPageRun)
    page = kPageResults;
}

// The Setup -> Run -> Results track of the circuit and emulation panels, the
// same one every study shows (Results enables once there is an outcome).
void runStepper(int *page, const char *setupIssue, bool running, int outcome) {
  const kit::Status outSt = outcome == 1   ? kit::Status::Done
                            : outcome == 0 ? kit::Status::Failed
                                           : kit::Status::Pending;
  kit::StepItem steps[3];
  steps[0] = {"Setup", setupIssue ? kit::Status::Retry : kit::Status::Done,
              true, setupIssue ? setupIssue : "Setup is complete."};
  steps[1] = {"Run", running ? kit::Status::Running : outSt, true,
              "Live progress and the settings being solved."};
  steps[2] = {"Results", running ? kit::Status::Pending : outSt,
              outcome >= 0 || *page == kPageResults,
              outcome >= 0 ? "Inspect the results." : "Run the analysis first."};
  kit::stepper("steps", steps, 3, page);
}

kit::Status resultStepStatus(bool running, int result) {
  return running       ? kit::Status::Running
         : result == 1 ? kit::Status::Done
         : result == 0 ? kit::Status::Failed
                       : kit::Status::Pending;
}

// Unit of a SPICE probe name: v(...) is a node voltage, i(...) a branch current.
const char *probeUnit(const std::string &probe) {
  if (!probe.empty() && (probe[0] == 'i' || probe[0] == 'I'))
    return kit::unitText(kit::Unit::A);
  if (!probe.empty() && (probe[0] == 'v' || probe[0] == 'V'))
    return kit::unitText(kit::Unit::V);
  return nullptr;
}

constexpr const char *kCircuitPayload = "CHUDWARE_SIM_CIRCUIT";
constexpr const char *kCircuitComponentPayload = "CHUDWARE_SIM_COMPONENT";
constexpr const char *kPoweredComponentPayload = "CHUDWARE_SIM_POWERED";

void electronicsDragSource(const char *type, const std::string &docId,
                           const char *kind, const std::string &value) {
  if (docId.empty() || value.empty() || !ImGui::BeginDragDropSource())
    return;
  const std::string payload =
      nlohmann::json{{"doc_id", docId}, {"kind", kind}, {"value", value}}.dump();
  ImGui::SetDragDropPayload(type, payload.c_str(), payload.size() + 1);
  ImGui::TextUnformatted(value.c_str());
  ImGui::EndDragDropSource();
}

bool electronicsDrop(const char *type, const std::string &docId,
                     const char *kind, std::string &value) {
  if (docId.empty() || !ImGui::BeginDragDropTarget())
    return false;
  bool accepted = false;
  if (const ImGuiPayload *p = ImGui::AcceptDragDropPayload(type)) {
    if (p->IsDelivery() && p->Data && p->DataSize > 1 &&
        p->DataSize <= 65536 &&
        static_cast<const char *>(p->Data)[p->DataSize - 1] == '\0') {
      const auto data = nlohmann::json::parse(
          static_cast<const char *>(p->Data),
          static_cast<const char *>(p->Data) + p->DataSize - 1, nullptr, false);
      if (data.is_object() && data.contains("doc_id") &&
          data["doc_id"].is_string() && data["doc_id"] == docId &&
          data.contains("kind") && data["kind"].is_string() &&
          data["kind"] == kind && data.contains("value") &&
          data["value"].is_string()) {
        value = data["value"].get<std::string>();
        accepted = !value.empty() && value.find('\0') == std::string::npos;
      }
    }
  }
  ImGui::EndDragDropTarget();
  return accepted;
}

bool nativeObject(const char *text, nlohmann::json &out, std::string &error) {
  out = nlohmann::json::parse(text, nullptr, false);
  error = out.is_object() ? std::string() : "Enter a valid native JSON object.";
  return error.empty();
}

// Native reason tokens are diagnostics, not captions. Keep their exact spelling
// in the result tooltip while using the shared human label convention.
std::string electronicsMessage(const std::string &message) {
  return message.find_first_of(" \t\n") == std::string::npos
             ? simStageLabel(message) : message;
}
} // namespace

// ---------------------------------------------------------------- electronics
// (SPICE) panel

void SimManager::openElectronicsPanel(AppState &st) {
  elPanelOpen_ = true;
  // Preserve the chosen document across panel reopen and tab reordering.
  if (elRunning_)
    return;
  gElPage = kPageSetup;
  if (!el_.docId.empty()) {
    st.leftPaneTab = AppState::LP_Simulation;
    return;
  }
  el_.docTab = -1;
  for (int i = 0; i < (int)st.docTabs.size(); ++i)
    if (st.docTabs[i].kind == AppState::DocTab::TabKind::Schematic &&
        !st.docTabs[i].ecadDocId.empty()) {
      el_.docTab = i;
      el_.docId = st.docTabs[i].ecadDocId;
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

const char *SimManager::electronicsRunBlocker(bool /*dispatchText*/) const {
  if (!el_.simParamsError.empty())
    return "Fix the expression parameters in Advanced (native parameters).";
  if (!std::isfinite(el_.reltol) || el_.reltol <= 0 ||
      !std::isfinite(el_.vntol) || el_.vntol <= 0 ||
      !std::isfinite(el_.gmin) || el_.gmin < 0 ||
      el_.maxiter < 1 || el_.maxiter > 100000 ||
      !std::isfinite(el_.tempC) || el_.tempC <= -273.15)
    return "Set valid tolerances, conductance, iteration limit and temperature.";
  if (el_.analysis == 1) {
    const double span = (double)el_.dcStop - el_.dcStart;
    if (!el_.dcSource[0])
      return "Name a voltage or current source for the DC sweep.";
    if (!std::isfinite(el_.dcStart) || !std::isfinite(el_.dcStop) ||
        !std::isfinite(el_.dcStep) || el_.dcStep == 0.0f || span == 0.0 ||
        span * el_.dcStep < 0.0)
      return "Set a non-zero DC step directed from start toward stop.";
    if (std::fabs(span / el_.dcStep) > 1000000.0)
      return "Reduce the DC sweep to at most one million points.";
  } else if (el_.analysis == 2 &&
             (!std::isfinite(el_.acStartHz) ||
              !std::isfinite(el_.acStopHz) || el_.acStartHz <= 0.0f ||
              el_.acStopHz <= el_.acStartHz || el_.acPoints < 2)) {
    return "Set positive increasing AC frequencies and at least 2 points.";
  } else if (el_.analysis == 3 &&
             (!std::isfinite(el_.tranStopMs) ||
              !std::isfinite(el_.tranStepUs) || el_.tranStopMs <= 0.0f ||
              el_.tranStepUs <= 0.0f ||
              !std::isfinite(el_.tranStartMs) || el_.tranStartMs < 0 ||
              el_.tranStartMs >= el_.tranStopMs ||
              el_.tranStepUs * 1e-3f >= el_.tranStopMs)) {
    return "Set a positive time step and a longer transient stop time.";
  }
  return nullptr;
}

nlohmann::json SimManager::electronicsSetup() const {
  nlohmann::json analysis;
  if (el_.analysis == 0)
    analysis = {{"type", "op"}};
  else if (el_.analysis == 1)
    analysis = {{"type", "dc"}, {"source", std::string(el_.dcSource)},
                {"start", el_.dcStart}, {"stop", el_.dcStop}, {"step", el_.dcStep}};
  else if (el_.analysis == 2)
    analysis = {{"type", "ac"}, {"fstart", el_.acStartHz},
                {"fstop", el_.acStopHz}, {"points", el_.acPoints},
                {"sweep", el_.acScale == 1 ? "oct" : el_.acScale == 2 ? "lin" : "dec"}};
  else
    analysis = {{"type", "tran"}, {"tstep", el_.tranStepUs * 1e-6},
                {"tstop", el_.tranStopMs * 1e-3},
                {"tstart", el_.tranStartMs * 1e-3}, {"uic", el_.tranUic}};
  return {{"doc_id", el_.docId},
          {"study", {{"analyses", nlohmann::json::array({analysis})},
                     {"probes", el_.setupProbes},
                     {"options", {{"reltol", el_.reltol}, {"vntol", el_.vntol},
                                  {"gmin", el_.gmin}, {"maxiter", el_.maxiter},
                                  {"temp", el_.tempC}}}}},
          {"sim_params", nlohmann::json::parse(el_.simParamsJson, nullptr, false)}};
}

void SimManager::runElectronics(AppState &st) {
  if (elRunning_)
    return;
  el_.docTab = -1;
  for (int i = 0; i < (int)st.docTabs.size(); ++i)
    if (st.docTabs[i].kind == AppState::DocTab::TabKind::Schematic &&
        st.docTabs[i].ecadDocId == el_.docId)
      el_.docTab = i;
  if (el_.docTab < 0) {
    return;
  }
  if (const char *blocker = electronicsRunBlocker(true)) {
    st.toast(ui::ToastLevel::Warn, blocker);
    return;
  }
  const nlohmann::json request = electronicsSetup();
  if (!request["sim_params"].is_object()) {
    el_.simParamsError = "Enter a valid native JSON object.";
    st.toast(ui::ToastLevel::Warn, el_.simParamsError);
    return;
  }
  const std::string docId = st.docTabs[el_.docTab].ecadDocId;
  const std::string docTitle = st.docTabs[el_.docTab].displayTitle();
  const int analysis = el_.analysis;
  elRunning_ = true;
  gElRunStart = ImGui::GetTime();
  gElPage = kPageRun;
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
      "ecad_simulate", request,
      [this, docId, docTitle, analysis](const core::RpcResult &r) {
        elRunning_ = false;
        advanceToResults(gElPage);
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
          el_.error = std::string("The solver returned no ") +
                       (analysis == 0 ? "operating-point" :
                        analysis == 1 ? "DC sweep" :
                        analysis == 2 ? "AC sweep" : "transient") +
                       " results.";
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
  static const char *const kAn[4] = {"Operating point", "DC sweep",
                                     "AC sweep", "Transient"};
  static const char *const kAnTips[4] = {
      "DC bias point of every node; no further inputs.",
      "Step one independent source and record every probe.",
      "Small-signal magnitude response across a frequency range.",
      "Time-domain response from t = 0 to the stop time."};

  // Resolve stable identity each frame; closing a tab never substitutes a board.
  std::vector<int> schTabs;
  for (int i = 0; i < (int)st.docTabs.size(); ++i)
    if (st.docTabs[i].kind == AppState::DocTab::TabKind::Schematic &&
        !st.docTabs[i].ecadDocId.empty())
      schTabs.push_back(i);
  if (el_.docId.empty() && !schTabs.empty())
    el_.docId = st.docTabs[schTabs[0]].ecadDocId;
  el_.docTab = -1;
  for (int index : schTabs)
    if (st.docTabs[index].ecadDocId == el_.docId)
      el_.docTab = index;
  if (el_.analysis < 0 || el_.analysis > 3)
    el_.analysis = 3;
  const bool haveSch = el_.docTab >= 0;
  const std::string schTitle =
      haveSch ? st.docTabs[el_.docTab].displayTitle() : std::string();

  kit::panelBegin("sim_el", &simSurf());
  const char *status = elRunning_        ? "Solving"
                       : el_.result == 1 ? "Converged"
                       : el_.result == 0 ? "Failed"
                                         : nullptr;
  const kit::Tone statusTone = elRunning_        ? kit::Tone::Accent
                               : el_.result == 1 ? kit::Tone::Ok
                                                 : kit::Tone::Err;
  if (simPanelHeader(st, "sch_simulate",
                     haveSch ? schTitle.c_str() : "Circuit simulation", "SPICE",
                     status, statusTone,
                     "Close the circuit-simulation panel. Inputs and the last "
                     "result stay for this session.")) {
    elPanelOpen_ = false;
    kit::panelEnd();
    return;
  }

  const char *const elBlocker =
      !haveSch ? "Open a schematic tab to simulate."
               : electronicsRunBlocker(false);
  const bool resultMatches =
      haveSch && (el_.resultDocId.empty() ||
                  el_.resultDocId == st.docTabs[el_.docTab].ecadDocId);
  const int shownResult = resultMatches ? el_.result : -1;
  if (haveSch)
    runStepper(&gElPage, elBlocker, elRunning_, shownResult);
  const int page = haveSch ? gElPage : kPageSetup;

  kit::panelBodyBegin(page == kPageSetup ? "el_setup"
                      : page == kPageRun ? "el_run"
                                         : "el_results");
  if (!haveSch) {
    kit::emptyState("sch_simulate", "No schematic open",
                    "SPICE runs on a Schematic document's netlist. Open the "
                    "board's schematic tab; a PCB alone has no simulatable "
                    "devices.");
    if (!schTabs.empty()) {
      ImGui::BeginDisabled(elRunning_);
      std::vector<std::string> titles;
      for (int tab : schTabs)
        titles.push_back(st.docTabs[tab].displayTitle());
      int selected = -1;
      kit::FieldGroup g("replace_closed_source", "Schematic");
      if (kit::fieldCombo("choose", "Schematic", &selected, titles) &&
          selected >= 0 && selected < (int)schTabs.size()) {
        el_.docId = st.docTabs[schTabs[selected]].ecadDocId;
        el_.docTab = schTabs[selected];
        el_.paletteDocId.clear();
        el_.setupProbes.clear();
        el_.result = -1;
        gElPage = kPageSetup;
      }
      ImGui::EndDisabled();
    }
  } else if (page == kPageSetup) {
    ImGui::BeginDisabled(elRunning_);
    bool changed = false;
    if (kit::cardBegin("circuit", "Circuit", "sch_new")) {
      kit::FieldGroup g("ckt", "Schematic");
      std::vector<std::string> titles;
      int sel = -1;
      for (int i = 0; i < (int)schTabs.size(); ++i) {
        titles.push_back(st.docTabs[schTabs[i]].displayTitle());
        if (schTabs[i] == el_.docTab)
          sel = i;
      }
      if (kit::fieldCombo("sch", "Schematic", &sel, titles,
                          kit::FieldOpts().withTip(
                              "Which open schematic to simulate.")) &&
          sel >= 0 && sel < (int)schTabs.size()) {
        el_.docTab = schTabs[sel];
        el_.docId = st.docTabs[el_.docTab].ecadDocId;
        el_.setupProbes.clear();
        el_.paletteDocId.clear();
        el_.result = -1;
      }
      int an = el_.analysis;
      if (kit::fieldEnum("an", "Analysis", &an, kAn, 4,
                         kit::FieldOpts().withItemTips(kAnTips))) {
        el_.analysis = an;
        changed = true;
      }
      if (el_.analysis == 0)
        kit::dimText(kAnTips[0]);
    }
    kit::cardEnd();
    const auto invalidate = [&] {
      el_.result = -1;
      el_.raw = nlohmann::json::object();
      el_.xs.clear();
      el_.ys.clear();
    };
    const auto addComponentProbes = [&](const nlohmann::json &device) {
      for (const auto &node : sim::jarr(device, "nodes")) {
        if (!node.is_string() || node == "0" || node == "gnd" || node == "GND")
          continue;
        const std::string probe = "V(" + node.get<std::string>() + ")";
        if (std::find(el_.setupProbes.begin(), el_.setupProbes.end(), probe) ==
            el_.setupProbes.end()) {
          el_.setupProbes.push_back(probe);
          invalidate();
        }
      }
    };
    auto refreshPalette = [&] {
      if (el_.palettePending)
        return;
      nlohmann::json simParams;
      if (!nativeObject(el_.simParamsJson, simParams, el_.simParamsError))
        return;
      const std::string docId = el_.docId;
      el_.paletteDocId = docId;
      el_.palettePending = true;
      el_.paletteError.clear();
      el_.circuit = nlohmann::json::object();
      st.rpc.call("ecad_tree", {{"doc_id", docId}},
                  [this, &st, docId, simParams](const core::RpcResult &r) {
        if (el_.docId != docId) {
          el_.palettePending = false;
          return;
        }
        const auto &netlist = sim::jobj(sim::jobj(r.result, "compile"), "netlist");
        if (!r.ok || netlist.empty()) {
          el_.palettePending = false;
          el_.paletteError = "Could not read this schematic's compiled netlist.";
          return;
        }
        st.rpc.call("sim_netlist_to_circuit",
                    {{"netlist", netlist}, {"sim_params", simParams}},
                    [this, docId](const core::RpcResult &converted) {
          el_.palettePending = false;
          if (el_.docId != docId)
            return;
          if (converted.ok)
            el_.circuit = sim::jobj(converted.result, "circuit");
          if (!converted.ok || el_.circuit.empty()) {
            el_.paletteError = simJsonText(converted.result, "message", "error", converted.err);
            if (el_.paletteError.empty())
              el_.paletteError = "Native circuit conversion returned no palette.";
          }
        }, 15000);
      }, 15000);
    };
    if (!elRunning_ && el_.paletteDocId != el_.docId && !el_.palettePending)
      refreshPalette();
    if (kit::cardBegin("circuit_palette", "Schematic palette", "sch_new")) {
      kit::dimText("Click a source to sweep it. Components add their node probes. Copy-drop probes below.");
      if (kit::secondaryButton("Refresh schematic###refresh_palette"))
        refreshPalette();
      if (el_.palettePending)
        kit::spinner("Reading the native circuit...");
      if (!el_.paletteError.empty())
        kit::inlineStatus(el_.paletteError.c_str(), kit::Tone::Warn);
      ImGui::BeginChild("native_palette_items",
                        ImVec2(0, ui::metrics::U(50)),
                        ImGuiChildFlags_None, ImGuiWindowFlags_NoScrollbar);
      for (const auto &device : sim::jarr(el_.circuit, "devices")) {
        const std::string name = sim::jstr(device, "name");
        const std::string kind = sim::jstr(device, "kind");
        if (name.empty())
          continue;
        ImGui::PushID(name.c_str());
        const bool source = kind == "V" || kind == "I";
        if (kit::secondaryButton((name + " · " + kind + "###component").c_str())) {
          if (source) {
            std::snprintf(el_.dcSource, sizeof(el_.dcSource), "%s", name.c_str());
            el_.analysis = 1;
            invalidate();
          } else
            addComponentProbes(device);
        }
        electronicsDragSource(source ? kCircuitPayload : kCircuitComponentPayload,
                              el_.docId, source ? "source" : "component", name);
        ImGui::PopID();
      }
      std::vector<std::string> available;
      for (const auto &node : sim::jarr(el_.circuit, "nodes"))
        if (node.is_string() && node != "0" && node != "gnd" && node != "GND")
          available.push_back("V(" + node.get<std::string>() + ")");
      for (const auto &device : sim::jarr(el_.circuit, "devices"))
        if (sim::jstr(device, "kind") == "V")
          available.push_back("I(" + sim::jstr(device, "name") + ")");
      for (const auto &probe : available) {
        ImGui::PushID(probe.c_str());
        if (kit::secondaryButton((probe + "###probe").c_str()) &&
            std::find(el_.setupProbes.begin(), el_.setupProbes.end(), probe) ==
                el_.setupProbes.end()) {
          el_.setupProbes.push_back(probe);
          invalidate();
        }
        electronicsDragSource(kCircuitPayload, el_.docId, "probe", probe);
        ImGui::PopID();
      }
      if (available.empty() && !el_.palettePending)
        kit::dimText("No native probes available. Manual probe expressions remain editable.");
      ui::drawWindowPillScrollbarV(simSurf());
      ImGui::EndChild();
    }
    kit::cardEnd();
    if (kit::cardBegin("probe_setup", "Probes", "spec_check")) {
      kit::secondaryButton("Copy-drop a native probe here###probe_drop");
      std::string component;
      if (!elRunning_ && electronicsDrop(kCircuitComponentPayload, el_.docId,
                                         "component", component))
        for (const auto &device : sim::jarr(el_.circuit, "devices"))
          if (sim::jstr(device, "name") == component)
            addComponentProbes(device);
      std::string dropped;
      if (!elRunning_ && electronicsDrop(kCircuitPayload, el_.docId, "probe", dropped)) {
        // Delivery must still belong to the selected real circuit.
        bool exists = false;
        for (const auto &node : sim::jarr(el_.circuit, "nodes"))
          if (node.is_string() && dropped == "V(" + node.get<std::string>() + ")")
            exists = true;
        for (const auto &device : sim::jarr(el_.circuit, "devices"))
          if (sim::jstr(device, "kind") == "V" &&
              dropped == "I(" + sim::jstr(device, "name") + ")")
            exists = true;
        if (exists && std::find(el_.setupProbes.begin(), el_.setupProbes.end(),
                                dropped) == el_.setupProbes.end()) {
          el_.setupProbes.push_back(dropped);
          invalidate();
        }
      }
      for (size_t i = 0; i < el_.setupProbes.size();) {
        bool removed = false;
        kit::removableChip(("probe_" + std::to_string(i)).c_str(),
                           el_.setupProbes[i].c_str(), "Remove probe",
                           kit::Tone::Neutral, &removed);
        if (removed) {
          el_.setupProbes.erase(el_.setupProbes.begin() + i);
          invalidate();
        } else
          ++i;
      }
      kit::FieldGroup g("probe_manual", "Expression");
      kit::fieldText("expression", "Expression", el_.manualProbe,
                     sizeof(el_.manualProbe));
      if (kit::secondaryButton("Add expression###add_probe") && el_.manualProbe[0]) {
        const std::string probe = el_.manualProbe;
        if (std::find(el_.setupProbes.begin(), el_.setupProbes.end(), probe) ==
            el_.setupProbes.end()) {
          el_.setupProbes.push_back(probe);
          invalidate();
        }
        el_.manualProbe[0] = '\0';
      }
      if (el_.setupProbes.empty())
        kit::dimText("Empty means native defaults: all nodes and voltage-source currents.");
    }
    kit::cardEnd();

    if (el_.analysis == 1) {
      if (kit::cardBegin("dc", kAn[1], "freq_range")) {
        kit::FieldGroup g("dc", "Source");
        const bool current = el_.dcSource[0] == 'I' || el_.dcSource[0] == 'i';
        kit::FieldOpts val;
        val.withUnit(current ? kit::Unit::A : kit::Unit::V);
        const double span = (double)el_.dcStop - el_.dcStart;
        const char *stepIssue =
            (!std::isfinite(el_.dcStep) || el_.dcStep == 0.0f || span == 0.0 ||
             span * el_.dcStep < 0.0)
                ? "Must be non-zero and point from start toward stop."
            : std::fabs(span / el_.dcStep) > 1000000.0
                ? "Over the one-million-point solver limit."
                : nullptr;
        changed |= kit::fieldText("src", "Source", el_.dcSource, sizeof(el_.dcSource),
                       kit::FieldOpts()
                           .withHint("V1")
                           .withRequired()
                           .withTip("Independent voltage (V\xe2\x80\xa6) or "
                                    "current (I\xe2\x80\xa6) source to sweep.")
                           .withIssue(el_.dcSource[0] ? nullptr
                                                      : "Name the source to "
                                                        "sweep."));
        std::string source;
        if (!elRunning_ && electronicsDrop(kCircuitPayload, el_.docId, "source", source)) {
          for (const auto &device : sim::jarr(el_.circuit, "devices"))
            if (sim::jstr(device, "name") == source &&
                (sim::jstr(device, "kind") == "V" ||
                 sim::jstr(device, "kind") == "I")) {
              std::snprintf(el_.dcSource, sizeof(el_.dcSource), "%s", source.c_str());
              invalidate();
              break;
            }
        }
        changed |= kit::fieldNumber("start", "Start", &el_.dcStart, val);
        changed |= kit::fieldNumber("stop", "Stop", &el_.dcStop, val);
        changed |= kit::fieldNumber("step", "Step", &el_.dcStep,
                         kit::FieldOpts(val).withIssue(stepIssue));
      }
      kit::cardEnd();
    } else if (el_.analysis == 2) {
      if (kit::cardBegin("ac", kAn[2], "freq_range")) {
        kit::FieldGroup g("ac", "Start frequency");
        kit::FieldOpts hz;
        hz.withUnit(kit::Unit::Hz).withMin(1.0e-6);
        static const char *const scales[] = {"Decade", "Octave", "Linear"};
        changed |= kit::fieldEnum("spacing", "Spacing", &el_.acScale, scales, 3);
        changed |= kit::fieldNumber("f0", "Start frequency", &el_.acStartHz, hz);
        changed |= kit::fieldNumber("f1", "Stop frequency", &el_.acStopHz,
                         kit::FieldOpts(hz).withIssue(
                             el_.acStopHz <= el_.acStartHz
                                 ? "Must be above the start frequency."
                                 : nullptr));
        changed |= kit::fieldInt("n", "Points", &el_.acPoints,
                      kit::FieldOpts().withMin(2).withTip(
                          "Samples per decade/octave, or total samples for linear spacing."));
      }
      kit::cardEnd();
    } else if (el_.analysis == 3) {
      if (kit::cardBegin("tran", kAn[3], "freq_range")) {
        kit::FieldGroup g("tran", "Stop time");
        changed |= kit::fieldNumber("stop", "Stop time", &el_.tranStopMs,
                         kit::FieldOpts().withUnit(kit::Unit::ms).withMin(1e-6));
        changed |= kit::fieldNumber(
            "step", "Time step", &el_.tranStepUs,
            kit::FieldOpts()
                .withUnit(kit::Unit::us)
                .withMin(1e-6)
                .withTip("Maximum solver step.")
                .withIssue(el_.tranStepUs * 1e-3f >= el_.tranStopMs
                               ? "Must be shorter than the stop time."
                               : nullptr));
        changed |= kit::fieldNumber("record_start", "Record from", &el_.tranStartMs,
                         kit::FieldOpts().withUnit(kit::Unit::ms).withMin(0));
        changed |= kit::fieldCheckbox("uic", "Use initial conditions", &el_.tranUic);
      }
      kit::cardEnd();
    }
    if (kit::cardBegin("solver_options", "Native solver options", "spec_check")) {
      kit::FieldGroup g("options", "Relative tolerance");
      changed |= kit::fieldNumber("reltol", "Relative tolerance", &el_.reltol);
      changed |= kit::fieldNumber("vntol", "Voltage tolerance", &el_.vntol,
                       kit::FieldOpts().withUnit(kit::Unit::V));
      changed |= kit::fieldNumber("gmin", "Minimum conductance", &el_.gmin);
      changed |= kit::fieldInt("maxiter", "Newton iterations", &el_.maxiter,
                    kit::FieldOpts().withRange(1, 100000));
      changed |= kit::fieldNumber("temp", "Temperature", &el_.tempC,
                       kit::FieldOpts().withUnit(kit::Unit::DegC));
      // CollapsingHeader does not push a scope: the established ##sim_params
      // input ID and its audit label remain exactly the same.
      const bool advanced = ImGui::CollapsingHeader(
          "Advanced (native parameters)###native_parameters");
      kit::itemTooltip("Optional global expression parameters for the native "
                       "circuit solver. Typed settings above are the normal setup "
                       "path; component values and models stay in the schematic. "
                       "Edits take effect directly after JSON validation.");
      if (advanced) {
        kit::dimText("Global expression parameters only. Component values and "
                     "model mappings stay in the schematic.");
        if (ImGui::InputTextMultiline("##sim_params", el_.simParamsJson,
                                      sizeof(el_.simParamsJson),
                                      ImVec2(-FLT_MIN, ui::metrics::U(40)))) {
          nlohmann::json parsed;
          nativeObject(el_.simParamsJson, parsed, el_.simParamsError);
          el_.paletteDocId.clear();
          invalidate();
        }
#ifdef IMGUI_ENABLE_TEST_ENGINE
        {
          ImGuiContext &g = *GImGui;
          IMGUI_TEST_ENGINE_ITEM_INFO(ImGui::GetItemID(), "Native parameters JSON",
                                      g.LastItemData.StatusFlags);
        }
#endif
      }
      if (!el_.simParamsError.empty())
        kit::inlineStatus(el_.simParamsError.c_str(), kit::Tone::Err);
    }
    kit::cardEnd();
    if (changed)
      invalidate();
    ImGui::EndDisabled();
  } else if (page == kPageRun) {
    // The settings this run solves; the footer carries the progress and the
    // stepper the run state.
    std::vector<kit::KeyValueItem> rows;
    rows.push_back({"Schematic", schTitle});
    rows.push_back({"Analysis", kAn[el_.analysis]});
    rows.push_back({"Probes", el_.setupProbes.empty()
                                 ? "Native default nodes and sources"
                                 : std::to_string(el_.setupProbes.size()) + " selected expressions"});
    if (el_.analysis == 1) {
      const char *u = (el_.dcSource[0] == 'I' || el_.dcSource[0] == 'i')
                          ? kit::unitText(kit::Unit::A)
                          : kit::unitText(kit::Unit::V);
      rows.push_back({"Source", el_.dcSource});
      rows.push_back({"Sweep", kit::fmtValue(el_.dcStart) + " to " +
                                   kit::fmtQuantity(el_.dcStop, u)});
      rows.push_back({"Step", kit::fmtQuantity(el_.dcStep, u)});
    } else if (el_.analysis == 2) {
      const char *hz = kit::unitText(kit::Unit::Hz);
      rows.push_back({"Frequency", kit::fmtValue(el_.acStartHz) + " to " +
                                       kit::fmtQuantity(el_.acStopHz, hz)});
      rows.push_back({"Points", std::to_string(el_.acPoints)});
    } else if (el_.analysis == 3) {
      rows.push_back({"Stop time", kit::fmtQuantity(
                                       el_.tranStopMs,
                                       kit::unitText(kit::Unit::ms))});
      rows.push_back({"Time step", kit::fmtQuantity(
                                       el_.tranStepUs,
                                       kit::unitText(kit::Unit::us))});
    }
    kit::keyValueCard("run_settings", "Run settings", "sch_simulate", rows);
    if (!elRunning_ && shownResult >= 0 &&
        kit::secondaryButton("View results###view_results"))
      gElPage = kPageResults;
  } else {
    // ---- Results page: outcome -> metrics/trace -> log.
    const char *const ra =
        el_.resultAnalysis >= 0 && el_.resultAnalysis < 4
            ? kAn[el_.resultAnalysis]
            : "";
    if (elRunning_) {
      kit::spinner("Solving\xe2\x80\xa6 results appear here when the solver "
                   "returns.");
    } else if (shownResult == 0) {
      const std::string message = electronicsMessage(el_.error);
      if (kit::blockedState("Simulation failed", message.c_str(),
                            "Check the schematic's sources and device models, "
                            "then run again.",
                            "Go to Setup"))
        gElPage = kPageSetup;
      kit::itemTooltip(el_.error.c_str());
    } else if (shownResult == 1) {
      if (kit::cardBegin("outcome", "Circuit simulation complete", "sch_simulate")) {
        kit::chipFlowBegin("el_outcome");
        kit::chipFlowItem(ImGui::GetTextLineHeight());
        kit::statusGlyphInline(kit::Status::Done);
        kit::flowChip(el_.headline.c_str(), kit::Tone::Ok, true);
        kit::flowValueChip("Analysis", ra);
        kit::chipFlowEnd();
      }
      kit::cardEnd();
      const nlohmann::json &convergence = sim::jobj(el_.raw, "convergence");
      for (auto it = convergence.begin(); it != convergence.end(); ++it)
        if (it.value().is_boolean() && !it.value().get<bool>()) {
          // Engine block keys ("op", "tran", …) as the analysis names.
          const std::string &k = it.key();
          const char *name = k == "op"     ? kAn[0]
                             : k == "dc"   ? kAn[1]
                             : k == "ac"   ? kAn[2]
                             : k == "tran" ? kAn[3]
                                           : nullptr;
          kit::inlineStatus((std::string(name ? name : "One analysis") +
                             " did not converge.")
                                .c_str(),
                            kit::Tone::Warn);
        }
      sim::warningsList(el_.raw);
      const nlohmann::json &results = sim::jobj(el_.raw, "results");
      if (el_.resultAnalysis == 0) {
        std::vector<kit::MetricRow> rows;
        const nlohmann::json &op = sim::jobj(results, "op");
        for (auto it = op.begin(); it != op.end(); ++it)
          if (it.value().is_number() &&
              std::isfinite(it.value().get<double>())) {
            const char *u = probeUnit(it.key());
            rows.push_back({it.key(), u ? u : "",
                            {{kit::fmtValue(it.value().get<double>())}}});
          }
        if (rows.empty())
          kit::dimText("The solver reported no finite operating-point "
                       "values.");
        else
          kit::metricTable("op", {"Value"}, rows, "Probe");
      } else if (!el_.probes.empty()) {
        {
          kit::FieldGroup g("trace", "Trace");
          int pi = el_.probeIdx;
          if (kit::fieldCombo("probe", "Trace", &pi, el_.probes,
                              kit::FieldOpts().withTip(
                                  "Node voltage or branch current to plot.")))
            selectElTrace(pi);
        }
        const std::string probe =
            el_.probeIdx >= 0 && el_.probeIdx < (int)el_.probes.size()
                ? el_.probes[el_.probeIdx]
                : std::string();
        const bool ac = el_.resultAnalysis == 2;
        const bool tran = el_.resultAnalysis == 3;
        kit::PlotSpec p;
        p.x = el_.xs;
        p.xAxis.title = ac ? "Frequency" : tran ? "Time" : "Source value";
        p.xAxis.unit = ac     ? kit::unitText(kit::Unit::Hz)
                       : tran ? kit::unitText(kit::Unit::ms)
                              : nullptr;
        p.xAxis.log = ac;
        p.yAxis.title = ac ? "Magnitude" : probe.c_str();
        p.yAxis.unit = ac ? "dB" : probeUnit(probe);
        p.series.push_back({probe.c_str(), el_.ys});
        p.emptyText = "The selected trace is empty or contains non-finite "
                      "samples.";
        kit::linePlot("trace_plot", p);
      }
      const nlohmann::json &log = sim::jarr(el_.raw, "log");
      if (!log.empty()) {
        if (kit::cardBegin("log", "Solver log", nullptr, &gElLogOpen))
          for (size_t i = 0; i < log.size(); ++i)
            if (log[i].is_string())
              kit::dimText(log[i].get_ref<const std::string &>().c_str());
        kit::cardEnd();
      }
    } else {
      if (!resultMatches && el_.result >= 0)
        kit::inlineStatus(("The last result belongs to " + el_.resultDocTitle +
                           ". Run this schematic to replace it.")
                              .c_str(),
                          kit::Tone::Info);
      // No action here: the footer owns the one Run primary (and, when the
      // setup is incomplete, the reason and a "Go to Setup").
      kit::emptyState("sch_simulate", "No results yet",
                      (std::string("Press ") + kElRun[el_.analysis] +
                       " below. Probes default to every node voltage and "
                       "source current.")
                          .c_str());
    }
  }
  kit::panelBodyEnd();

  kit::panelFooterBegin();
  kit::ActionSpec a;
  const bool rerun = page == kPageResults && shownResult == 1;
  a.primaryLabel = rerun ? "Re-run" : kElRun[el_.analysis];
  a.primaryAsSecondary = rerun;
  a.blocker = elBlocker;
  a.primaryEnabled = !elBlocker && !elRunning_;
  if (haveSch && elBlocker && page != kPageSetup && !elRunning_)
    a.secondaryLabel = "Go to Setup";
  a.running = elRunning_;
  a.stage = "Solving the netlist";
  a.elapsed = ImGui::GetTime() - gElRunStart;
  a.cancellable = false;
  a.cancelBlocker = "this step runs in the engine and can't be interrupted.";
  switch (kit::actionFooter("run", a)) {
  case kit::FooterAction::Primary:
    runElectronics(st);
    break;
  case kit::FooterAction::Secondary:
    gElPage = kPageSetup;
    break;
  default:
    break;
  }
  kit::panelEnd();
}

// ---------------------------------------------------------------- firmware
// emulation panel

void SimManager::openEmulationPanel(AppState &st) {
  fwPanelOpen_ = true;
  if (!fwRunning_)
    gFwPage = kPageSetup;
  if (!fwsim_.scenarioEditor.initialized && fwsim_.scenario.is_object() &&
      fwsim_.scenario.empty())
    fwsim_.scenario = nullptr;
  probeFwStatus(st);
  st.leftPaneTab = AppState::LP_Simulation;
}

void SimManager::runEmulation(AppState &st) {
  if (fwRunning_ || fwsim_.projIdx < 0 ||
      fwsim_.projIdx >= (int)fwsim_.projectIds.size())
    return;
  if (!std::isfinite(fwsim_.durationMs) || fwsim_.durationMs <= 0.0f ||
      fwsim_.durationMs > 600000.0f) {
    st.toast(ui::ToastLevel::Warn,
             "Emulation duration must be between 0 and 600000 ms");
    return;
  }
  if (!fwsim_.scenarioEditor.rawValid ||
      !sim::validateFwScenario(fwsim_.scenario, fwsim_.scenarioError) ||
      !std::isfinite(fwsim_.timeoutS) || fwsim_.timeoutS < 0.0f ||
      fwsim_.timeoutS > 600.0f) {
    st.toast(ui::ToastLevel::Warn, "Fix the scenario JSON and independent host timeout.");
    return;
  }
  static const char *const backends[] = {"auto", "renode", "qemu", "simavr"};
  const int backend = std::clamp(fwsim_.backend, 0, 3);
  nlohmann::json p{{"project_id", fwsim_.projectIds[fwsim_.projIdx]},
                   {"run_s", fwsim_.durationMs * 0.001},
                   {"backend", backends[backend]},
                   {"scenario", fwsim_.scenario},
                   {"power_profile", fwsim_.powerProfile}};
  if (fwsim_.elfPath[0])
    p["elf_path"] = fwsim_.elfPath;
  if (fwsim_.timeoutS > 0)
    p["timeout_s"] = fwsim_.timeoutS;
  const double timeoutS = fwsim_.timeoutS > 0 ? fwsim_.timeoutS : 30.0;
  const int rpcTimeoutMs = (int)std::ceil((timeoutS + 5.0) * 1000.0);
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
  gFwRunStart = ImGui::GetTime();
  gFwPage = kPageRun;
  fwsim_.result = -1;
  fwsim_.error.clear();
  fwsim_.headline.clear();
  fwsim_.uart.clear();
  fwsim_.assertResults.clear();
  fwsim_.raw = nlohmann::json::object();
  st.rpc.call(
      "fw_sim_run", p,
      [this](const core::RpcResult &r) {
        fwRunning_ = false;
        advanceToResults(gFwPage);
        if (r.result.is_object())
          fwsim_.raw = r.result;
        if (!r.ok || !r.result.is_object() || r.result.contains("code") ||
            !r.result.value("ok", true)) {
          fwsim_.result = 0;
          fwsim_.error = sim::jstr(r.result, "hint",
              sim::jstr(r.result, "error",
                  sim::jstr(r.result, "message",
                      sim::jstr(r.result, "reason",
                          r.err.empty() ? "Emulation failed." : r.err))));
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
        // Native ms measures host wall time, not the requested virtual horizon.
        fwsim_.headline =
            kit::fmtQuantity(elapsedMs, kit::unitText(kit::Unit::ms));
        if (r.result.contains("assertion_results") &&
            r.result["assertion_results"].is_array())
          for (const auto &a : r.result["assertion_results"]) {
            if (!a.is_object())
              continue;
            const bool assertionPassed =
                a.contains("passed") && a["passed"].is_boolean() &&
                a["passed"].get<bool>();
            fwsim_.assertResults.emplace_back(
                a.contains("assertion") && a["assertion"].is_string()
                    ? a["assertion"].get<std::string>()
                    : std::string("(unnamed assertion)"),
                assertionPassed);
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
  // ---- sync: clamp the project pick to the loaded list.
  if (fwsim_.projIdx < 0 || fwsim_.projIdx >= (int)fwsim_.projectIds.size())
    fwsim_.projIdx = 0;
  const bool backendUp = fwStatus_ == 1 && fwCanEmulate_;
  const bool haveProj = backendUp && fwsim_.projectsLoaded &&
                        !fwsim_.projectIds.empty();
  const bool ranToEnd = fwsim_.result == 1 ||
                        (fwsim_.result == 0 && !fwsim_.headline.empty());
  const bool badDuration = !std::isfinite(fwsim_.durationMs) ||
                           fwsim_.durationMs <= 0.0f ||
                           fwsim_.durationMs > 600000.0f;
  const bool badScenario = !fwsim_.scenarioEditor.rawValid ||
      !sim::validateFwScenario(fwsim_.scenario, fwsim_.scenarioError);
  const bool badTimeout = !std::isfinite(fwsim_.timeoutS) ||
                          fwsim_.timeoutS < 0 || fwsim_.timeoutS > 600;
  const char *const fwBlocker =
      fwStatus_ == 0 ? "Wait for the emulation backend check."
      : !backendUp   ? "Install a supported emulator (Renode, QEMU or simavr)."
      : !fwsim_.projectsLoaded ? "Wait for firmware projects to load."
      : fwsim_.projectIds.empty() ? "Create a firmware project first."
      : !haveProj ? "Select a firmware project."
      : badDuration ? "Set a run time of 1 to 600,000 ms."
      : badScenario ? "Fix the scenario inputs."
      : badTimeout ? "Set a host timeout of 0 to 600 seconds (0 uses the default)."
                    : nullptr;
  const int fwOutcome = fwsim_.result == 1                      ? 1
                        : (ranToEnd || !fwsim_.error.empty()) ? 0
                                                              : -1;

  kit::panelBegin("sim_fw", &simSurf());
  const char *status = fwRunning_                  ? "Running"
                       : fwsim_.result == 1        ? "Passed"
                       : ranToEnd                  ? "Not passed"
                       : !fwsim_.error.empty()     ? "Failed"
                                                   : nullptr;
  const kit::Tone statusTone = fwRunning_           ? kit::Tone::Accent
                               : fwsim_.result == 1 ? kit::Tone::Ok
                                                    : kit::Tone::Err;
  if (simPanelHeader(st, "ws_electronics",
                     haveProj ? fwsim_.projectNames[fwsim_.projIdx].c_str()
                              : "Firmware emulation",
                     "Emulation", status, statusTone,
                     "Close the emulation panel. Inputs and the last run stay "
                     "for this session.")) {
    fwPanelOpen_ = false;
    kit::panelEnd();
    return;
  }

  if (haveProj)
    runStepper(&gFwPage, fwBlocker, fwRunning_,
               fwOutcome);
  const int page = haveProj ? gFwPage : kPageSetup;

  kit::panelBodyBegin(page == kPageSetup ? "fw_setup"
                      : page == kPageRun ? "fw_run"
                                         : "fw_results");
  if (fwStatus_ == 0) {
    kit::spinner("Checking the emulation backend\xe2\x80\xa6");
  } else if (!backendUp) {
    // Honest doctor state: the run leg is missing — guidance + Recheck.
    if (kit::blockedState("Emulation backend unavailable",
                          fwHint_.empty()
                              ? "No supported Renode, QEMU or simavr backend was found."
                              : fwHint_.c_str(),
                          "Install a supported emulator for the project's MCU, then recheck.",
                          "Recheck")) {
      fwStatus_ = 0;
      probeFwStatus(st);
    }
  } else if (!fwsim_.projectsLoaded) {
    kit::spinner("Loading firmware projects\xe2\x80\xa6");
  } else if (fwsim_.projectIds.empty()) {
    kit::emptyState("ws_electronics", "No firmware projects",
                    "Create one in the Firmware workspace (New Project), then "
                    "come back to emulate it.");
  }
  if (page == kPageSetup) {
    ImGui::BeginDisabled(fwRunning_);
    bool changed = false;
    if (kit::cardBegin("scenario", "Scenario", "spec_check")) {
      kit::FieldGroup g("scn", "Assertions");
      int pi = fwsim_.projIdx;
      if (kit::fieldCombo("proj", "Project", &pi, fwsim_.projectNames,
                          kit::FieldOpts()
                              .withDisabled(fwsim_.projectIds.empty(),
                                            "Create or load a native firmware project before running.")
                              .withTip(
                              "Firmware project to build (if needed) and "
                              "run.")) &&
          pi >= 0 && pi < (int)fwsim_.projectIds.size()) {
        fwsim_.projIdx = pi;
        changed = true;
      }
      changed |= kit::fieldText("asserts", "Assertions", fwsim_.asserts,
                     sizeof(fwsim_.asserts),
                     kit::FieldOpts()
                         .withHint("uart:hello; gpio:PA5=1")
                         .withTip("';'-separated checks against the emulated "
                                  "run (UART text, GPIO levels)."));
      changed |= kit::fieldNumber("dur", "Run time", &fwsim_.durationMs,
                       kit::FieldOpts()
                           .withUnit(kit::Unit::ms)
                           .withRange(1.0, 600000.0)
                           .withTip("Virtual horizon for Renode; host wall budget is independent."));
      static const char *const backends[] = {"Auto", "Renode", "QEMU", "simavr"};
      changed |= kit::fieldEnum("backend", "Backend", &fwsim_.backend, backends, 4,
                               kit::FieldOpts().withTip(
                                   "Forced backends refuse when unavailable or incompatible."));
      changed |= kit::fieldNumber("timeout", "Host timeout", &fwsim_.timeoutS,
                                  kit::FieldOpts().withUnit(kit::Unit::s)
                                      .withRange(0, 600).withTip(
                                          "0 omits timeout_s and uses the native default (30 s)."));
      changed |= kit::fieldText("elf", "ELF path", fwsim_.elfPath,
                               sizeof(fwsim_.elfPath),
                               kit::FieldOpts().withTip(
                                   "Optional executable override; otherwise use project build discovery."));
      changed |= kit::fieldCheckbox("power", "Power profile", &fwsim_.powerProfile);
    }
    kit::cardEnd();
    changed |= sim::drawFwScenarioEditor(fwsim_.scenarioEditor, fwsim_.scenario,
                                        "simulate_firmware");
    if (changed || !fwsim_.scenarioEditor.rawValid) {
      fwsim_.result = -1;
      fwsim_.headline.clear();
      fwsim_.error.clear();
      fwsim_.raw = nlohmann::json::object();
      fwsim_.uart.clear();
      fwsim_.assertResults.clear();
    }
    ImGui::EndDisabled();
  } else if (page == kPageRun) {
    // The scenario this run emulates; the footer carries the progress and the
    // stepper the run state.
    int checks = 0;
    for (const char *p = fwsim_.asserts; *p;) {
      while (*p == ';' || *p == ' ' || *p == '\t')
        ++p;
      if (!*p)
        break;
      ++checks;
      while (*p && *p != ';')
        ++p;
    }
    std::vector<kit::KeyValueItem> rows;
    rows.push_back({"Project", fwsim_.projectNames[fwsim_.projIdx]});
    rows.push_back({"Assertions",
                    checks == 0   ? std::string("None")
                    : checks == 1 ? std::string("1 check")
                                  : std::to_string(checks) + " checks"});
    rows.push_back({"Run time", kit::fmtQuantity(fwsim_.durationMs,
                                                 kit::unitText(kit::Unit::ms))});
    static const char *const backends[] = {"Auto", "Renode", "QEMU", "simavr"};
    rows.push_back({"Backend", backends[std::clamp(fwsim_.backend, 0, 3)]});
    rows.push_back({"Host timeout", fwsim_.timeoutS > 0
        ? kit::fmtQuantity(fwsim_.timeoutS, kit::unitText(kit::Unit::s))
        : "Native default (30 s)"});
    rows.push_back({"Scenario", fwsim_.scenario.is_null() ? "None"
        : fwsim_.scenario.is_string() ? fwsim_.scenario.get<std::string>()
        : std::to_string(sim::jarr(fwsim_.scenario, "tracks").size()) + " stimulus tracks"});
    kit::keyValueCard("run_settings", "Run settings", "spec_check", rows);
    if (!fwRunning_ && fwOutcome >= 0 &&
        kit::secondaryButton("View results###view_results"))
      gFwPage = kPageResults;
  } else {
    // ---- Results page: outcome -> assertions -> UART log.
    if (fwRunning_) {
      kit::spinner("Emulating\xe2\x80\xa6 results appear here when the run "
                   "ends.");
    } else if (ranToEnd) {
      const bool passed = fwsim_.result == 1;
      if (kit::cardBegin("outcome", "Emulation complete", "spec_check")) {
        kit::chipFlowBegin("fw_outcome");
        kit::chipFlowItem(ImGui::GetTextLineHeight());
        kit::statusGlyphInline(passed ? kit::Status::Done : kit::Status::Failed);
        kit::flowChip(passed ? "Passed" : "Not passed",
                      passed ? kit::Tone::Ok : kit::Tone::Err, true);
        kit::flowValueChip("Host elapsed", fwsim_.headline.c_str());
        kit::chipFlowEnd();
      }
      kit::cardEnd();
      if (!fwsim_.assertResults.empty()) {
        std::vector<kit::KeyValueItem> rows;
        for (const auto &ar : fwsim_.assertResults)
          rows.push_back({ar.first, ar.second ? "Pass" : "Fail",
                          ar.second ? kit::Tone::Ok : kit::Tone::Err});
        kit::keyValueCard("asserts", "Assertions", "spec_check", rows);
      }
    } else if (!fwsim_.error.empty()) {
      const std::string message = electronicsMessage(fwsim_.error);
      if (kit::blockedState("Emulation failed", message.c_str(),
                            "Check that the project builds in the Firmware "
                            "workspace, then run again.",
                            "Go to Setup"))
        gFwPage = kPageSetup;
      kit::itemTooltip(fwsim_.error.c_str());
    } else {
      // No action here: the footer owns the one Run primary (and, when the
      // setup is incomplete, the reason and a "Go to Setup").
      kit::emptyState("ws_electronics", "No run yet",
                      "Press Run emulation below: the selected project is "
                      "built (if needed) and emulated against its scenario "
                      "assertions.");
    }
    if (!fwsim_.uart.empty() && !fwRunning_) {
      if (kit::cardBegin("uart", "UART output", nullptr, &gFwUartOpen)) {
        const float lineH = ImGui::GetTextLineHeightWithSpacing();
        const int shown = (int)std::min<size_t>(fwsim_.uart.size(), 12);
        ImGui::BeginChild("##uart_log", ImVec2(0.0f, lineH * shown),
                          ImGuiChildFlags_None, ImGuiWindowFlags_NoScrollbar);
        if (st.fonts.mono)
          ImGui::PushFont(st.fonts.mono);
        ImGuiListClipper clip;
        clip.Begin((int)fwsim_.uart.size());
        while (clip.Step())
          for (int i = clip.DisplayStart; i < clip.DisplayEnd; ++i)
            ImGui::TextUnformatted(fwsim_.uart[i].c_str());
        if (st.fonts.mono)
          ImGui::PopFont();
        ui::drawWindowPillScrollbarV(simSurf());
        ImGui::EndChild();
      }
      kit::cardEnd();
    }
  }
  if (page == kPageResults && !fwRunning_ && !fwsim_.raw.empty()) {
    sim::warningsList(fwsim_.raw);
    const auto &limitations = sim::jarr(fwsim_.raw, "limitations");
    for (const auto &item : limitations)
      if (item.is_string())
        kit::inlineStatus(item.get_ref<const std::string &>().c_str(), kit::Tone::Warn);
    if (kit::secondaryButton("Copy native result JSON###copy_firmware_result")) {
      const std::string raw = fwsim_.raw.dump(2);
      ImGui::SetClipboardText(raw.c_str());
    }
  }
  kit::panelBodyEnd();

  kit::panelFooterBegin();
  kit::ActionSpec a;
  const bool rerun = page == kPageResults && fwOutcome >= 0;
  a.primaryLabel = rerun ? "Re-run" : "Run emulation";
  a.primaryAsSecondary = rerun;
  a.blocker = fwBlocker;
  a.primaryEnabled = !fwBlocker && !fwRunning_;
  if (haveProj && fwBlocker && page != kPageSetup && !fwRunning_)
    a.secondaryLabel = "Go to Setup";
  a.running = fwRunning_;
  a.stage = "Emulating";
  a.elapsed = ImGui::GetTime() - gFwRunStart;
  a.cancellable = false;
  a.cancelBlocker = "this step runs in the engine and can't be interrupted.";
  switch (kit::actionFooter("run", a)) {
  case kit::FooterAction::Primary:
    runEmulation(st);
    break;
  case kit::FooterAction::Secondary:
    gFwPage = kPageSetup;
    break;
  default:
    break;
  }
  kit::panelEnd();
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
    return "Reopen the source board and start a new thermal handoff.";
  if (ho_.running)
    return "Wait for the thermal handoff solve to finish.";
  if (!ho_.heatsinkContextReady || !ho_.result.is_object() ||
      !ho_.result.value("ok", false) || !ho_.result.value("solved", false) ||
      ho_.result.value("doc_id", std::string()) != ho_.ecadDocId)
    return "Run a successful thermal handoff for this board before picking a "
           "heatsink.";
  if (sim::jstr(sim::jobj(ho_.result, "simhandoff"), "id").empty())
    return "Re-send the board to restore the thermal result's source identity.";
  const nlohmann::json *row =
      findHeatsinkRow(ho_.result, ho_.heatsinkComponentRef);
  if (!row || !usableHeatsinkRow(*row))
    return "Choose a mapped powered component from the thermal result.";
  if (ho_.heatsinkTargetKind < 0 || ho_.heatsinkTargetKind > 1 ||
      !std::isfinite(ho_.heatsinkTargetC))
    return "Enter a finite junction or case temperature target.";
  if (!std::isfinite(ho_.timThicknessMm) || ho_.timThicknessMm < 0 ||
      !std::isfinite(ho_.timConductivityWmK) || ho_.timConductivityWmK <= 0 ||
      ho_.maxCandidates < 1)
    return "Set finite TIM thickness/conductivity and a positive candidate limit.";
  double ambient = 0.0;
  if (!ho_.result.contains("environment") ||
      !handoffFiniteNumber(ho_.result["environment"], "t_amb_c", &ambient))
    return "Re-run the thermal handoff to obtain its ambient temperature.";
  if (ho_.heatsinkTargetC <= ambient)
    return "Set a heatsink target above the ambient temperature.";
  return {};
}

void SimManager::openHandoffPanel(AppState &st, int pcbTab) {
  handoffPanelOpen_ = true;
  if (ho_.running || ho_.heatsinkRunning)
    return;
  gHoPage = 0;
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
    ho_.result = nlohmann::json::object();
    ho_.heatsink = nlohmann::json::object();
  }
  ho_.ecadDocId = std::move(ecadId);
  ho_.ecadDocTitle = std::move(title);
  probeCaps(st);
  st.leftPaneTab = AppState::LP_Simulation;
}

void SimManager::runEcadThermalHandoff(AppState &st) {
  if (ho_.running || ho_.heatsinkRunning)
    return;
  nlohmann::json transient;
  if (ho_.transientJson[0] &&
      !nativeObject(ho_.transientJson, transient, ho_.transientError)) {
    ho_.error = ho_.transientError;
    return;
  }
  const int sourceTab = resolveHandoffDocTab(st);
  if (sourceTab < 0) {
    ho_.error =
        "source board closed or was replaced; start a new handoff from an open "
        "PCB";
    return;
  }
  const std::string ecadId = ho_.ecadDocId;
  ho_.heatsinkContextReady = false;
  static const char *kEnvs[3] = {"still_air", "forced_air", "cold_plate"};
  nlohmann::json params = {
      {"doc_id", ecadId},
      {"environment",
       kEnvs[(ho_.envIdx < 0 || ho_.envIdx > 2) ? 0 : ho_.envIdx]},
      {"include_trace_heating", ho_.traceHeating},
  };
  params["mesh_fineness"] = ho_.meshFineness;
  params["dry_run"] = ho_.dryRun;
  if (ho_.customEnvironment) {
    params["h_w_m2k"] = ho_.filmWm2K;
    params["t_amb_c"] = ho_.ambientC;
  }
  if (ho_.transientJson[0])
    params["transient"] = transient;
  ho_.heatsink = nlohmann::json::object();
  ho_.result = nlohmann::json::object();
  ho_.running = true;
  gHoRunStart = ImGui::GetTime();
  gHoPage = 1;
  ho_.error.clear();
  // Freeze the setup before either async request; reading a board must not stall the UI.
  st.rpc.call("ecad_tree", {{"doc_id", ecadId}},
      [this, &st, ecadId, params](const core::RpcResult &tree) mutable {
        if (ho_.ecadDocId != ecadId) {
          ho_.running = false;
          return;
        }
        const auto &board = sim::jobj(tree.result, "tree");
        if (!tree.ok || tree.result.contains("code") || board.empty()) {
          ho_.running = false;
          ho_.error = tree.err.empty() ? "Could not read the source board." : tree.err;
          return;
        }
        params["board"] = board;
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
          ho_.error = sim::jstr(r.result, "hint",
              sim::jstr(r.result, "error",
                  sim::jstr(r.result, "reason", "Thermal handoff failed.")));
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
      }, 15000);
}

void SimManager::runHeatsinkSelect(AppState &st) {
  if (ho_.running || ho_.heatsinkRunning)
    return;
  const std::string blocker = heatsinkSetupBlocker(st);
  if (!blocker.empty()) {
    st.toast(ui::ToastLevel::Warn, blocker);
    return;
  }
  nlohmann::json params = {
      {"doc_id", ho_.ecadDocId},
      {"handoff_id", sim::jstr(sim::jobj(ho_.result, "simhandoff"), "id")},
      {"component_ref", ho_.heatsinkComponentRef},
      {"tim_t_mm", ho_.timThicknessMm},
      {"tim_k_w_mk", ho_.timConductivityWmK},
      {"include_library", ho_.heatsinkLibrary},
      {"include_parametric", ho_.heatsinkParametric},
      {"max_candidates", ho_.maxCandidates},
  };
  if (ho_.heatsinkTargetKind == 0)
    params["tj_target_c"] = ho_.heatsinkTargetC;
  else
    params["t_case_target_c"] = ho_.heatsinkTargetC;
  const std::string ecadId = ho_.ecadDocId;
  ho_.heatsinkRunning = true;
  gHsRunStart = ImGui::GetTime();
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
  static const char *const kEnvLabels[3] = {"Still air", "Forced air",
                                            "Cold plate"};
  static const char *const kEnvTips[3] = {
      "Natural convection, h = 10 W/(m\xc2\xb2\xc2\xb7K) on the board and "
      "component surfaces.",
      "Fan-driven convection, h = 50 W/(m\xc2\xb2\xc2\xb7K).",
      "Board bottom held at the plate temperature."};
  static const char *const kTargetKinds[2] = {"Junction (Tj)", "Case (Tcase)"};
  const char *degC = kit::unitText(kit::Unit::DegC);

  const int sourceTab = resolveHandoffDocTab(st);
  const bool sourceOpen = sourceTab >= 0;
  const std::string boardTitle =
      sourceOpen ? st.docTabs[sourceTab].displayTitle()
      : !ho_.ecadDocTitle.empty() ? ho_.ecadDocTitle
                                  : std::string("Board thermal handoff");
  const bool seamUp =
      capsStatus_ == 1 &&
      sim::jbool(sim::jobj(sim::jobj(caps_, "handoffs"), "ecad_thermal"),
                 "available", false);
  const bool handoffReady =
      ho_.heatsinkContextReady && ho_.result.is_object() &&
      ho_.result.value("ok", false) && ho_.result.value("solved", false) &&
      ho_.result.value("doc_id", std::string()) == ho_.ecadDocId;

  std::string blocker;
  if (!sourceOpen)
    blocker = "Reopen the source board and start a new handoff.";
  else if (capsStatus_ == 0)
    blocker = "Wait for the board thermal capability check.";
  else if (!seamUp)
    blocker = "Install the board-to-thermal solver integration.";
  else if (!ho_.transientError.empty())
    blocker = "Fix the transient inputs in the thermal setup.";
  else if (ho_.heatsinkRunning)
    blocker = "Wait for heatsink selection to finish.";
  const bool haveResult = ho_.result.is_object() && ho_.result.value("ok", false);
  const int hoOutcome = handoffReady                             ? 1
                        : (!ho_.error.empty() || haveResult) ? 0
                                                             : -1;

  kit::panelBegin("sim_ho", &simSurf());
  const char *status = ho_.running || ho_.heatsinkRunning ? "Solving"
                       : !sourceOpen ? "Board closed"
                       : handoffReady ? "Solved"
                       : !ho_.error.empty() ? "Failed"
                                            : nullptr;
  const kit::Tone statusTone = ho_.running || ho_.heatsinkRunning ? kit::Tone::Accent
                               : !sourceOpen  ? kit::Tone::Warn
                               : handoffReady ? kit::Tone::Ok
                                              : kit::Tone::Err;
  if (simPanelHeader(st, "study_thermal_fea", boardTitle.c_str(),
                     "Thermal handoff", status, statusTone,
                     "Close the handoff panel. The last thermal result stays "
                     "for this session.")) {
    handoffPanelOpen_ = false;
    kit::panelEnd();
    return;
  }

  const kit::StepItem steps[2] = {
      {"Setup",
       !sourceOpen        ? kit::Status::Failed
       : blocker.empty() ? kit::Status::Done
                         : kit::Status::Retry,
       true, blocker.empty() ? nullptr : blocker.c_str()},
      {"Results", resultStepStatus(ho_.running, hoOutcome), true,
       hoOutcome < 0 && !ho_.running ? "Send the board to see results."
                                     : nullptr}};
  kit::stepper("steps", steps, 2, &gHoPage);
  const bool onResults = gHoPage == 1;

  // The pinned footer, shared by both pages (the Setup page returns early).
  auto drawFooter = [&] {
    kit::panelFooterBegin();
    kit::ActionSpec a;
    // On the Results page of a solved board the next step is the heatsink
    // card's primary, so the footer's re-send drops to secondary weight.
    a.primaryLabel =
        handoffReady ? "Re-send board" : "Send board to thermal FEA";
    a.primaryAsSecondary = handoffReady && onResults;
    a.blocker = blocker.empty() ? nullptr : blocker.c_str();
    a.blockerTone = capsStatus_ == 0 ? kit::Tone::Info : kit::Tone::Warn;
    a.primaryEnabled = blocker.empty() && !ho_.running && !ho_.heatsinkRunning &&
                       ho_.transientError.empty();
    a.running = ho_.running || ho_.heatsinkRunning;
    a.stage = ho_.heatsinkRunning ? "Comparing heatsink candidates"
                                  : "Solving board thermal";
    a.elapsed = ImGui::GetTime() - (ho_.heatsinkRunning ? gHsRunStart : gHoRunStart);
    a.cancellable = false;
    a.cancelBlocker = "this step runs in the engine and can't be interrupted.";
    if (kit::actionFooter("send", a) == kit::FooterAction::Primary)
      runEcadThermalHandoff(st);
    kit::panelEnd();
  };

  kit::panelBodyBegin(onResults ? "ho_results" : "ho_setup");
  if (!sourceOpen)
    kit::blockedState("Source board closed",
                      "The board this handoff started from is no longer open; "
                      "another tab is never substituted.",
                      "Reopen the board and start a new handoff from it.");

  if (!onResults) {
    ImGui::BeginDisabled(ho_.running || ho_.heatsinkRunning);
    bool changed = false;
    if (kit::cardBegin("setup", "Thermal setup", "study_thermal_fea")) {
      kit::FieldGroup g("env", "Copper I\xc2\xb2R trace heating");
      int ei = (ho_.envIdx < 0 || ho_.envIdx > 2) ? 0 : ho_.envIdx;
      if (kit::fieldEnum("env", "Environment", &ei, kEnvLabels, 3,
                         kit::FieldOpts().withItemTips(kEnvTips).withTip(
                             "Convection preset applied to the board and "
                             "component surfaces."))) {
        ho_.envIdx = ei;
        changed = true;
      }
      changed |= kit::fieldCheckbox("trace", "Copper I\xc2\xb2R trace heating",
                         &ho_.traceHeating,
                         kit::FieldOpts().withTip(
                             "Adds the PDN I\xc2\xb2R map as distributed "
                             "heat-flux cells (needs a prior PDN run)."));
      changed |= kit::fieldCheckbox("custom_env", "Custom environment",
                                    &ho_.customEnvironment);
      if (ho_.customEnvironment) {
        changed |= kit::fieldNumber("ambient", "Ambient", &ho_.ambientC,
                                    kit::FieldOpts().withUnit(kit::Unit::DegC));
        changed |= kit::fieldNumber("film", "Film coefficient", &ho_.filmWm2K,
                                    kit::FieldOpts().withUnit(kit::Unit::W_m2K));
      }
      changed |= kit::fieldNumber("fineness", "Mesh fineness", &ho_.meshFineness,
                                  kit::FieldOpts().withRange(0.001, 1.0));
      changed |= kit::fieldCheckbox("dry_run", "Inspect without solving", &ho_.dryRun);
      kit::dimText("Optional native transient JSON: t_end_s, dt_s and schedules keyed by component ref. Blank uses native steady setup.");
      if (ImGui::InputTextMultiline("##transient", ho_.transientJson,
                                    sizeof(ho_.transientJson),
                                    ImVec2(-FLT_MIN, ui::metrics::U(32.5f)))) {
        changed = true;
        nlohmann::json parsed;
        if (ho_.transientJson[0])
          nativeObject(ho_.transientJson, parsed, ho_.transientError);
        else
          ho_.transientError.clear();
      }
#ifdef IMGUI_ENABLE_TEST_ENGINE
      {
        ImGuiContext &g = *GImGui;
        IMGUI_TEST_ENGINE_ITEM_INFO(ImGui::GetItemID(), "Native thermal transient JSON",
                                    g.LastItemData.StatusFlags);
      }
#endif
      if (!ho_.transientError.empty())
        kit::inlineStatus(ho_.transientError.c_str(), kit::Tone::Err);
    }
    kit::cardEnd();
    if (changed) {
      ho_.heatsinkContextReady = false;
      ho_.result = nlohmann::json::object();
      ho_.heatsink = nlohmann::json::object();
      ho_.error.clear();
    }
    ImGui::EndDisabled();
    kit::panelBodyEnd();
    drawFooter();
    return;
  }

  // ---- Results page: thermal outcome -> component temperatures -> heatsink
  // pick (the next step once solved) -> its outcome and candidates.
  const std::string resultDocId =
      ho_.result.is_object() ? ho_.result.value("doc_id", std::string())
                             : std::string();
  if (!resultDocId.empty() && resultDocId != ho_.ecadDocId)
    kit::inlineStatus("The retained thermal result belongs to another ECAD "
                      "document, not this board.",
                      kit::Tone::Warn);
  if (ho_.running) {
    kit::spinner("Solving the board\xe2\x80\xa6 results appear here when the "
                 "worker returns.");
  } else if (!ho_.error.empty()) {
    const std::string message = electronicsMessage(ho_.error);
    if (kit::blockedState("Handoff failed", message.c_str(),
                          "Check the board and the setup, then send it again.",
                          "Go to Setup"))
      gHoPage = 0;
    kit::itemTooltip(ho_.error.c_str());
  } else if (!haveResult && sourceOpen) {
    // No action here: the footer owns the one Send primary (and, when the
    // board cannot be sent yet, the reason).
    kit::emptyState("study_thermal_fea", "No thermal result yet",
                    "Press Send board to thermal FEA below: the board is "
                    "fused into one FEA solid with a heat source per powered "
                    "component, then solved.");
  }
  if (!ho_.running && ho_.result.is_object() && ho_.result.value("ok", false)) {
    if (ho_.result.value("solved", false)) {
      const nlohmann::json &env = sim::jobj(ho_.result, "environment");
      kit::chipFlowBegin("ho_outcome");
      kit::chipFlowItem(ImGui::GetTextLineHeight());
      kit::statusGlyphInline(kit::Status::Done);
      kit::flowChip("Solved", kit::Tone::Ok, true);
      kit::flowValueChip(
          "Board max",
          kit::fmtQuantity(sim::jnum(ho_.result, "board_max_temp_c"), degC)
              .c_str());
      if (sim::jhas(env, "t_amb_c"))
        kit::flowValueChip(
            "Ambient", kit::fmtQuantity(sim::jnum(env, "t_amb_c"), degC).c_str());
      kit::chipFlowEnd();
      const nlohmann::json &comps = sim::jarr(ho_.result, "components");
      if (!comps.empty()) {
        if (kit::tableBegin("comps",
                            {{"Component"},
                             {"Case (\xc2\xb0" "C)", kit::TableAlign::Right},
                             {"Junction (\xc2\xb0" "C)",
                              kit::TableAlign::Right}})) {
          for (size_t i = 0; i < comps.size(); ++i) {
            const nlohmann::json &c = comps[i];
            if (!c.is_object())
              continue;
            double tj = 0.0, tjMax = 0.0;
            const bool hasTj = handoffFiniteNumber(c, "tj_c", &tj);
            const bool over = hasTj &&
                              handoffFiniteNumber(c, "tj_max_c", &tjMax) &&
                              tj > tjMax;
            const std::string overTip =
                over ? "Above the part's Tj max of " +
                           kit::fmtQuantity(tjMax, degC) + "."
                     : std::string();
            double tc = 0.0;
            kit::tableRow();
            kit::tableCell(c.value("ref", std::string("?")).c_str());
            kit::tableCell(handoffFiniteNumber(c, "t_case_c", &tc)
                               ? kit::fmtValue(tc, 3).c_str()
                               : "\xe2\x80\x94");
            kit::tableCell(hasTj ? kit::fmtValue(tj, 3).c_str()
                                 : "\xe2\x80\x94",
                           over ? kit::Tone::Err : kit::Tone::Neutral,
                           over ? overTip.c_str() : nullptr);
          }
        }
        kit::tableEnd();
      }
    } else {
      const std::string raw = sim::jstr(ho_.result, "hint",
          sim::jstr(ho_.result, "error_solve", "The board was not solved."));
      kit::inlineStatus(electronicsMessage(raw).c_str(), kit::Tone::Warn);
      kit::itemTooltip(raw.c_str());
    }
    sim::warningsList(ho_.result);
    drawProvenance(ho_.result);
  }

  // ---- heatsink pick (forward seam): bound to a stable component
  // reference from this board's solved handoff; the target stays editable
  // because a part limit is not always present in ECAD metadata.
  const bool hsUp =
      capsStatus_ == 1 &&
      sim::jbool(sim::jobj(sim::jobj(caps_, "handoffs"), "heatsink_select"),
                 "available", false);
  const std::string hsReason =
      hsUp ? heatsinkSetupBlocker(st)
           : capsStatus_ == 0 ? "Wait for the heatsink capability check."
                              : "Install the heatsink selection integration.";
  if (handoffReady && sourceOpen) {
    if (kit::cardBegin("powered_palette", "Mapped powered components", "study_thermal")) {
      ImGui::BeginDisabled(ho_.running || ho_.heatsinkRunning);
      kit::dimText("Click to choose, or copy-drop a component into the heatsink target.");
      for (const auto &row : sim::jarr(ho_.result, "components")) {
        if (!usableHeatsinkRow(row))
          continue;
        const std::string ref = row["ref"].get<std::string>();
        ImGui::PushID(ref.c_str());
        const std::string label = ref + " · " +
            kit::fmtQuantity(row["power_w"].get<double>(), kit::unitText(kit::Unit::W));
        if (kit::secondaryButton((label + "###powered_component").c_str())) {
          deriveHeatsinkTarget(ref);
          ho_.heatsink = nlohmann::json::object();
        }
        electronicsDragSource(kPoweredComponentPayload, ho_.ecadDocId, "component", ref);
        ImGui::PopID();
      }
      ImGui::EndDisabled();
    }
    kit::cardEnd();
  }
  if (kit::cardBegin("heatsink", "Heatsink", "study_thermal")) {
    ImGui::BeginDisabled(ho_.running || ho_.heatsinkRunning);
    bool targetChanged = false;
    if (handoffReady) {
      kit::FieldGroup g("hs", "Target temperature");
      std::vector<std::string> refs;
      int sel = -1;
      for (const auto &row : ho_.result["components"]) {
        if (!usableHeatsinkRow(row))
          continue;
        if (row.value("ref", std::string()) == ho_.heatsinkComponentRef)
          sel = (int)refs.size();
        refs.push_back(row.value("ref", std::string()));
      }
      if (kit::fieldCombo("comp", "Component", &sel, refs,
                          kit::FieldOpts().withTip(
                              "Mapped powered component to cool; its stable "
                              "ECAD reference is sent to the selector.")) &&
          sel >= 0 && sel < (int)refs.size()) {
        deriveHeatsinkTarget(refs[sel]);
        targetChanged = true;
      }
      std::string dropped;
      if (!ho_.running && !ho_.heatsinkRunning &&
          electronicsDrop(kPoweredComponentPayload, ho_.ecadDocId,
                          "component", dropped)) {
        const auto *row = findHeatsinkRow(ho_.result, dropped);
        if (row && usableHeatsinkRow(*row)) {
          deriveHeatsinkTarget(dropped);
          targetChanged = true;
        }
      }
      int kind = (ho_.heatsinkTargetKind == 1) ? 1 : 0;
      if (kit::fieldEnum("kind", "Target", &kind, kTargetKinds, 2)) {
        ho_.heatsinkTargetKind = kind;
        targetChanged = true;
      }
      targetChanged |= kit::fieldNumber("target", "Target temperature", &ho_.heatsinkTargetC,
                       kit::FieldOpts().withUnit(kit::Unit::DegC).withTip(
                           "Seeded from the part's Tj limit when known, else "
                           "from its solved temperature. Set it to the "
                           "design requirement."));
      targetChanged |= kit::fieldNumber("tim_thickness", "TIM thickness",
                                        &ho_.timThicknessMm,
                                        kit::FieldOpts().withUnit(kit::Unit::mm).withMin(0));
      targetChanged |= kit::fieldNumber("tim_conductivity", "TIM conductivity",
                                        &ho_.timConductivityWmK,
                                        kit::FieldOpts().withUnit(kit::Unit::W_mK));
      targetChanged |= kit::fieldCheckbox("library", "Library candidates",
                                          &ho_.heatsinkLibrary);
      targetChanged |= kit::fieldCheckbox("parametric", "Parametric candidates",
                                          &ho_.heatsinkParametric);
      targetChanged |= kit::fieldInt("candidates", "Candidate limit",
                                     &ho_.maxCandidates, kit::FieldOpts().withMin(1));
    } else {
      kit::dimText("After a solved handoff, size a heatsink for one powered "
                   "component of this board.");
    }
    if (targetChanged)
      ho_.heatsink = nlohmann::json::object();
    ImGui::EndDisabled();
    if (!ho_.heatsinkRunning)
      kit::inlineStatus(hsReason.c_str(),
                        handoffReady ? kit::Tone::Warn : kit::Tone::Info);
    if (!ho_.heatsinkRunning &&
        kit::primaryButton("Pick heatsink###pick_heatsink",
                                hsReason.empty(), hsReason.c_str(),
                           ImVec2(-FLT_MIN, 0.0f)))
      runHeatsinkSelect(st);
  }
  kit::cardEnd();

  if (ho_.heatsink.is_object() && !ho_.heatsink.empty() &&
      !ho_.heatsinkRunning) {
    const nlohmann::json &hs = ho_.heatsink;
    if (hs.value("ok", false)) {
      const bool met = hs.value("target_met", false);
      kit::chipFlowBegin("hs_outcome");
      kit::chipFlowItem(ImGui::GetTextLineHeight());
      kit::statusGlyphInline(met ? kit::Status::Done : kit::Status::Retry);
      kit::flowChip(met ? "Target met" : "Target missed",
                    met ? kit::Tone::Ok : kit::Tone::Warn, true);
      kit::flowValueChip("Method", sim::jstr(hs, "mode") == "fea_resolve"
                                       ? "FEA re-solve"
                                       : "Network estimate");
      kit::chipFlowEnd();
      const nlohmann::json &w = sim::jobj(hs, "winner");
      std::vector<kit::KeyValueItem> rows;
      rows.push_back({"Heatsink", sim::jstr(w, "title", sim::jstr(w, "candidate"))});
      rows.push_back({"Source", sim::jstr(w, "source")});
      if (sim::jhas(w, "achieved_c"))
        rows.push_back({"Achieved",
                        kit::fmtQuantity(sim::jnum(w, "achieved_c"), degC),
                        met ? kit::Tone::Ok : kit::Tone::Warn});
      if (sim::jhas(w, "delta_vs_target_c"))
        rows.push_back(
            {"vs target",
             (sim::jnum(w, "delta_vs_target_c") > 0.0 ? "+" : "") +
                 kit::fmtQuantity(sim::jnum(w, "delta_vs_target_c"), degC),
             met ? kit::Tone::Ok : kit::Tone::Warn});
      if (sim::jhas(w, "theta_sa_c_per_w"))
        rows.push_back({"Sink resistance",
                        kit::fmtQuantity(sim::jnum(w, "theta_sa_c_per_w"),
                                         "\xc2\xb0" "C/W")});
      if (sim::jhas(w, "mass_g"))
        rows.push_back({"Mass", kit::fmtQuantity(sim::jnum(w, "mass_g"),
                                                 kit::unitText(kit::Unit::g))});
      if (sim::jhas(w, "cost_usd"))
        rows.push_back({"Cost", "$" + kit::fmtValue(sim::jnum(w, "cost_usd"))});
      kit::keyValueCard("hs_winner", "Heatsink pick", "study_thermal", rows);
      const nlohmann::json &table = sim::jarr(hs, "table");
      if (table.size() > 1) {
        if (kit::tableBegin("hs_cands",
                            {{"Candidate"},
                             {"Achieved (\xc2\xb0" "C)", kit::TableAlign::Right},
                             {"Meets", kit::TableAlign::Right, 0.6f}})) {
          for (size_t i = 0; i < table.size(); ++i) {
            const nlohmann::json &row = table[i];
            const bool ok = sim::jbool(row, "meets_target", false);
            const std::string solve = sim::jstr(row, "solve");
            kit::tableRow();
            kit::tableCell(
                sim::jstr(row, "title", sim::jstr(row, "candidate")).c_str(),
                kit::Tone::Neutral, sim::jstr(row, "candidate").c_str());
            kit::tableCell(sim::jhas(row, "achieved_c")
                               ? kit::fmtValue(sim::jnum(row, "achieved_c"), 3)
                                     .c_str()
                               : "\xe2\x80\x94",
                           kit::Tone::Neutral,
                           solve.empty() ? nullptr : solve.c_str());
            kit::tableCell(ok ? "Yes" : "No",
                           ok ? kit::Tone::Ok : kit::Tone::Neutral);
          }
        }
        kit::tableEnd();
      }
      if (!sim::jstr(hs, "hint").empty())
        kit::dimText(sim::jstr(hs, "hint").c_str());
      sim::warningsList(hs);
    } else {
      const std::string raw = sim::jstr(hs, "hint",
          sim::jstr(hs, "error", sim::jstr(hs, "reason", "Heatsink selection failed.")));
      kit::inlineStatus(electronicsMessage(raw).c_str(), kit::Tone::Err);
      kit::itemTooltip(raw.c_str());
    }
    drawProvenance(hs);
  }
  kit::panelBodyEnd();
  drawFooter();
}

} // namespace ui
