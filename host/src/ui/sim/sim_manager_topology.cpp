// SimManager topology glue: config, capability probe, run/finish/export, run history, form/results.
#include "sim_manager_internal.h"
#include "sim_common.h" // sim::countNoun

#include <atomic>
#include <cctype>
#include <cstring>
#include <chrono>
#include <cstdlib>
#include <ctime>
#include <filesystem>
#include <fstream>
#include <thread>

#include "../../bootstrap.h" // appDir — services/sim/topology_print_processes.json
#include "../../core/sim_client.h"
#include "../file_dialog.h" // saveFileDialog / afterFileWritten — topology artifact export

namespace ui {

namespace {

struct TopologyPrintProcesses {
  std::vector<nlohmann::json> entries;
  std::vector<std::string> labels, tips;
};

const TopologyPrintProcesses &topologyPrintProcesses() {
  static const TopologyPrintProcesses data = [] {
    TopologyPrintProcesses out;
    std::ifstream file(appDir() + "/services/sim/topology_print_processes.json");
    if (!file)
      return out;
    const auto root = nlohmann::json::parse(file, nullptr, false);
    if (!root.is_object() || !root.contains("processes") || !root["processes"].is_array())
      return out;
    for (const auto &preset : root["processes"]) {
      if (topoStr(preset, "id").empty() || topoStr(preset, "label").empty() ||
          !preset.contains("topology") || !preset["topology"].is_object())
        continue;
      out.entries.push_back(preset);
      out.labels.push_back(topoStr(preset, "label"));
      std::string tip = topoStr(preset, "description");
      for (const char *key : {"process", "nozzle_mm", "line_width_mm", "layer_height_mm"})
        if (preset.contains(key))
          tip += "\n" + std::string(key) + ": " + preset[key].dump();
      for (const auto &[key, value] : preset["topology"].items())
        tip += "\n" + key + ": " + (value.is_null() ? "Not set" : value.dump());
      out.tips.push_back(std::move(tip));
    }
    return out;
  }();
  return data;
}

// configureActiveTopology merge: objects merge recursively, everything else replaces, null
// erases.
void mergeTopologyPatch(nlohmann::json &dst, const nlohmann::json &patch) {
  for (auto it = patch.begin(); it != patch.end(); ++it) {
    if (it.value().is_null()) {
      dst.erase(it.key());
    } else if (it.value().is_object() && dst.contains(it.key()) &&
               dst[it.key()].is_object()) {
      mergeTopologyPatch(dst[it.key()], it.value());
    } else {
      dst[it.key()] = it.value();
    }
  }
}

double unixNow() {
  return std::chrono::duration<double>(
             std::chrono::system_clock::now().time_since_epoch())
      .count();
}

// Field descriptors the overlay can colour by: the AICM artifact's own scalar arrays when it
// lists them, else the result's field descriptors.
std::vector<nlohmann::json> topoOverlayFields(const nlohmann::json &run,
                                              const nlohmann::json &overlay) {
  std::vector<nlohmann::json> out;
  auto take = [&](const nlohmann::json &arr) {
    if (!arr.is_array())
      return;
    for (const auto &f : arr) {
      if (topoStr(f, "name").empty())
        continue;
      const auto comps = f.find("components");
      if (comps != f.end() && !(comps->is_number_integer() && comps->get<int>() == 1))
        continue; // vector fields are not a scalar colormap
      out.push_back(f);
    }
  };
  if (overlay.is_object() && overlay.contains("arrays"))
    take(overlay["arrays"]);
  const nlohmann::json &result = topoObj(run, "result");
  if (out.empty() && result.contains("fields"))
    take(result["fields"]);
  return out;
}

// Display label of an overlay field: the app's shared field names first (static / flow tables,
// so "von_mises" reads "von Mises stress" exactly as in static studies), then the descriptor's
// own label, then the humanized chunk name, with proper nouns restored ("von Mises", "Tresca",
// "Reynolds") — never a raw "Von mises".
std::string topoFieldLabel(const std::string &name, const std::string &given) {
  if (name == "overhang")
    return "Needs support";
  for (int i = 0; i < kNStaticFields; ++i)
    if (name == kStaticFields[i].chunk)
      return kStaticFields[i].label;
  for (int i = 0; i < kNFlowFields; ++i)
    if (name == kFlowFields[i].chunk)
      return kFlowFields[i].label;
  std::string label = given.empty() ? name : given;
  for (char &c : label)
    if (c == '_')
      c = ' ';
  std::string lower = label;
  for (char &c : lower)
    c = (char)std::tolower((unsigned char)c);
  static const struct {
    const char *lower, *proper;
  } kNouns[] = {{"von mises", "von Mises"}, {"tresca", "Tresca"}, {"reynolds", "Reynolds"}};
  for (const auto &n : kNouns)
    for (std::size_t at = lower.find(n.lower); at != std::string::npos;
         at = lower.find(n.lower, at + 1))
      label.replace(at, std::strlen(n.proper), n.proper);
  // Sentence case, except a label that opens with the lower-case proper noun ("von Mises ...").
  if (!label.empty() && lower.rfind("von mises", 0) != 0)
    label[0] = (char)std::toupper((unsigned char)label[0]);
  return label;
}

void topoFieldMeta(const AppState &st, const nlohmann::json &desc, std::string &label,
                   std::string &unit, double &fromSI) {
  const SimManager::UnitSys u = SimManager::unitSys(st);
  const std::string name = topoStr(desc, "name");
  const std::string raw = topoStr(desc, "unit");
  label = topoFieldLabel(name, topoStr(desc, "label"));
  fromSI = 1.0;
  unit = raw;
  if (name == "density") {
    label = "Density (solid fraction)";
    unit.clear();
  } else if (name == "overhang") {
    unit.clear();
  } else if (raw == "Pa") {
    unit = u.stress;
    fromSI = u.stressFromMpa * 1e-6;
  } else if (raw == "m") {
    unit = u.len;
    fromSI = u.lenFromMm * 1e3;
  }
}
} // namespace

struct SimManager::TopoExport {
  std::atomic<bool> done{false};
  bool ok = false;
  std::string src, dst, err;
  long long expectBytes = -1;
};

bool SimManager::configureActiveTopology(AppState &st, const nlohmann::json &patch) {
  if (sel_ < 0 || sel_ >= (int)studies_.size() || !patch.is_object())
    return false;
  Study &s = studies_[sel_];
  if (s.type != StudyType::Topology || topologyRunActive())
    return false;
  nlohmann::json p = patch;
  p.erase("id");
  p.erase("type");
  if (p.contains("name") && p["name"].is_string() && !p["name"].get<std::string>().empty())
    s.name = p["name"].get<std::string>();
  p.erase("name");
  if (!s.topo.is_object())
    s.topo = nlohmann::json::object();
  mergeTopologyPatch(s.topo, p);
  topologyMeshFromJson(s, s.topo);
  s.topoRunsCheckKey.clear();
  persist(st, s);
  return true;
}

bool SimManager::applyActiveTopologyPrintProcess(AppState &st, const std::string &id) {
  if (sel_ < 0 || sel_ >= (int)studies_.size() ||
      studies_[sel_].type != StudyType::Topology || topologyRunActive())
    return false;
  for (const auto &preset : topologyPrintProcesses().entries) {
    if (topoStr(preset, "id") != id)
      continue;
    Study &s = studies_[sel_];
    if (!topology::applyPrintProcess(s.topo, preset, topoCaps_))
      return false;
    persist(st, s);
    return true;
  }
  return false;
}

nlohmann::json SimManager::activeTopologyStudy() const {
  if (sel_ < 0 || sel_ >= (int)studies_.size() ||
      studies_[sel_].type != StudyType::Topology)
    return nullptr;
  return studyToJson(studies_[sel_]);
}

nlohmann::json SimManager::activeTopologyResult() const {
  if (sel_ < 0 || sel_ >= (int)studies_.size() ||
      studies_[sel_].type != StudyType::Topology)
    return nullptr;
  const nlohmann::json &t = studies_[sel_].topo;
  if (!t.contains("runs") || !t["runs"].is_array() || t["runs"].empty())
    return nullptr;
  return t["runs"].back();
}

int SimManager::topologyRunCount() const {
  if (sel_ < 0 || sel_ >= (int)studies_.size() ||
      studies_[sel_].type != StudyType::Topology)
    return 0;
  const nlohmann::json &t = studies_[sel_].topo;
  return t.contains("runs") && t["runs"].is_array() ? (int)t["runs"].size() : 0;
}

bool SimManager::topologyRunActive() const {
  if (prepInFlight_ && staticModalPrep_ && staticModalPrep_->type == StudyType::Topology)
    return true;
  return running_ && !topoRunMeta_.runId.empty();
}

void SimManager::probeTopologyCaps(AppState &st, bool retry) {
  if (topoCapsInflight_ || (topoCapsRequested_ && !retry))
    return;
  topoCapsRequested_ = true;
  topoCapsInflight_ = true;
  topoCaps_ = topology::Capabilities{}; // Loading
  st.rpc.call("topology_capabilities", nlohmann::json::object(),
              [this, &st](const core::RpcResult &r) {
    topoCapsInflight_ = false;
    if (!r.ok || !r.result.is_object() || r.result.contains("code") ||
        !simJsonBool(r.result, "ok", true)) {
      const std::string why = simJsonText(r.result, "message", "error",
                                          r.err.empty() ? std::string("no reply") : r.err);
      topoCaps_ = topology::capabilitiesUnavailable(
          "The topology solver is not available in this engine (" + why + ").");
      st.addLog("[topology] capability probe failed: " + why);
      return;
    }
    const nlohmann::json &matrix =
        r.result.contains("capabilities") && r.result["capabilities"].is_object()
            ? r.result["capabilities"]
            : r.result;
    topoCaps_ = topology::parseCapabilities(matrix);
  }, 30000);
}

void SimManager::syncTopologyViews(const Study &s) {
  if (topoViewStudyId_ == s.id)
    return;
  topoViewStudyId_ = s.id;
  topoSettingsView_ = topology::SettingsView{};
  topoResultsView_ = topology::ResultsView{};
  topoBcView_ = topology::BcView{};
  topoAutoShowKey_.clear();
  if (armedWell_ == kArmTopology)
    armedWell_ = -1;
}

topology::MaterialLookup SimManager::topologyMaterialLookup() const {
  return [](const std::string &key, nlohmann::json &phys) {
    const int i = materialIndexByKey(key);
    if (i < 0)
      return false;
    const Material &m = materials()[i];
    phys = nlohmann::json::object();
    phys["name"] = m.name;
    // Only constants the library actually records; absent ones stay absent.
    if (m.E_mpa > 0.0) {
      phys["E_mpa"] = m.E_mpa;
      phys["nu"] = m.nu;
    }
    if (m.density_g_cm3 > 0.0)
      phys["density_g_cm3"] = m.density_g_cm3;
    if (m.yield_mpa > 0.0)
      phys["yield_mpa"] = m.yield_mpa;
    return true;
  };
}

std::string SimManager::topologyGeometryKey(AppState &st) {
  if (!st.docMirror.hasDoc())
    return {};
  const long rev = (long)st.docMirror.revision();
  if (topoGeomDocId_ == st.docMirror.docId() && topoGeomRevision_ == rev)
    return topoGeomKey_;
  // Runtime identity changes on reopen, but unchanged source geometry stays current.
  topoGeomKey_ = topology::geometryKey(st.docMirror.tree());
  topoGeomDocId_ = st.docMirror.docId();
  topoGeomRevision_ = rev;
  return topoGeomKey_;
}

bool SimManager::topologyReady(AppState &st, const Study &s, std::string &blocker) const {
  if (activeDocIsAssembly(st)) {
    blocker = "Topology optimization runs on a part document, not an assembly.";
    return false;
  }
  if (topoCaps_.state == topology::Capabilities::State::Loading) {
    blocker = "Waiting for the topology solver's capability matrix.";
    return false;
  }
  if (topoCaps_.state == topology::Capabilities::State::Unavailable) {
    blocker = topoCaps_.error;
    return false;
  }
  const nlohmann::json topo = s.topo.value("topology", nlohmann::json());
  if (!topo.is_object()) {
    blocker = "Create the topology settings first (Setup).";
    return false;
  }
  const std::string method = topo.value("method", std::string());
  const std::string physics = topo.value("physics", std::string());
  std::string reason;
  if (!topoCaps_.supports(method, physics, &reason)) {
    blocker = method.empty() || physics.empty()
                  ? "Choose the physics and method in Setup."
                  : (reason.empty() ? "This method/physics pair is not supported." : reason);
    return false;
  }
  for (const auto &i : topology::validateTopology(topo, topoCaps_, s.topo))
    if (i.severity == topology::Severity::Blocking) {
      blocker = i.message;
      return false;
    }
  for (const auto &i : topology::validateBoundaries(s.topo, topologyMaterialLookup()))
    if (i.severity == topology::Severity::Blocking) {
      blocker = i.message;
      return false;
    }
  return true;
}

void SimManager::runTopology(AppState &st, Study &s) {
  if (!st.simClient || !st.docMirror.hasDoc())
    return;
  std::string blocker;
  if (!topologyReady(st, s, blocker)) {
    st.toast(ui::ToastLevel::Warn, blocker);
    return;
  }
  auto prep = std::make_shared<StaticModalPrep>();
  prep->type = StudyType::Topology;
  prep->topoStudy = studyToJson(s);
  const std::string physics = topology::studyPhysics(prep->topoStudy);
  std::string err;
  if (!topology::runMaterial(prep->topoStudy, topologyMaterialLookup(), prep->topoMaterial,
                             err)) {
    if (physics != "flow") { // flow optimizes the fluid channel; no solid constants used
      st.toast(ui::ToastLevel::Warn, err);
      return;
    }
    prep->topoMaterial = nlohmann::json::object();
  }
  prep->topoSelectors = topology::selectorsToResolve(prep->topoStudy);
  prep->topoResolved.resize(prep->topoSelectors.size());
  prep->topoGeometryKey = topologyGeometryKey(st);

  // Run identity (unique per study; the result directory and record key use it).
  const double now = unixNow();
  const std::time_t tt = (std::time_t)now;
  std::tm tmv{};
#ifdef _WIN32
  gmtime_s(&tmv, &tt);
#else
  gmtime_r(&tt, &tmv);
#endif
  char stamp[32];
  std::strftime(stamp, sizeof(stamp), "%Y%m%dT%H%M%SZ", &tmv);
  const std::string setup = topology::setupKey(prep->topoStudy);
  TopoRunMeta meta;
  meta.studyId = s.id;
  meta.runId = std::string("run-") + stamp + "-" + setup.substr(setup.size() - 4);
  const nlohmann::json runs = s.topo.value("runs", nlohmann::json::array());
  for (std::size_t i = 0; runs.is_array() && i < runs.size(); ++i)
    if (topoRunIdOf(runs[i], i) == meta.runId)
      meta.runId += "-" + std::to_string(runs.size());
  const nlohmann::json settings = prep->topoStudy.value("topology", nlohmann::json::object());
  meta.label = "Run " + std::to_string((runs.is_array() ? runs.size() : 0) + 1) + " \xe2\x80\x94 " +
               topology::methodLabel(settings.value("method", std::string()), &topoCaps_) +
               ", " + topology::physicsLabel(physics, &topoCaps_);
  meta.settings = settings;
  meta.setupKey = setup;
  meta.geometryKey = prep->topoGeometryKey;
  meta.printProcess = topoStr(prep->topoStudy, "print_process");
  meta.startedUtc = now;
  topoRunMeta_ = meta;
  topoBcView_.armedPointer.clear();
  if (armedWell_ == kArmTopology)
    armedWell_ = -1;

  beginExportPrep(st, s, prep);
  if (staticModalPrep_ != prep) { // reservation refused: nothing started
    topoRunMeta_ = TopoRunMeta{};
    return;
  }
  // beginExportPrep clears the transient result; the retained runs still exist.
  s.hasResult = runs.is_array() && !runs.empty();
  s.topoStatus = prep->topoSelectors.empty()
                     ? std::string("Preparing the design domain\xe2\x80\xa6")
                     : "Preparing the design domain and " +
                           sim::countNoun((long long)prep->topoSelectors.size(),
                                          "face selection") +
                           "\xe2\x80\xa6";
  s.topoStatusKind = 0;
}

void SimManager::cancelTopologyPrep(AppState &st) {
  if (!prepInFlight_ || !staticModalPrep_ || staticModalPrep_->type != StudyType::Topology)
    return;
  auto prep = staticModalPrep_;
  failStaticModalPrep(st, prep, "Topology run cancelled during preparation.");
}

void SimManager::finishTopologyRun(AppState &st, Study &s, const std::string &ownerDocId,
                                   bool uiActive) {
  auto &job = st.simClient->job();
  const nlohmann::json &res = job.result;
  TopoRunMeta meta = topoRunMeta_;
  topoRunMeta_ = TopoRunMeta{};
  const bool haveResult =
      res.is_object() && (res.contains("termination") || res.contains("summary") ||
                           res.contains("ok"));
  if (!haveResult) {
    // User text names what happened; the engine's error code stays in the log only.
    s.topoStatus = job.cancelled ? "Run cancelled before the solver reported a result."
                                 : "Run failed: " + (job.error.empty()
                                                         ? std::string("the solver returned no result.")
                                                         : job.error);
    s.topoStatusKind = job.cancelled ? 1 : 2;
    st.addLog("[topology] " + s.topoStatus +
              (job.errorCode.empty() ? std::string() : " (" + job.errorCode + ")"));
    if (uiActive) {
      st.status = job.cancelled ? "Topology: run cancelled" : "Topology: run failed";
      st.statusKind = s.topoStatusKind;
    }
    return;
  }
  if (meta.runId.empty() || meta.studyId != s.id) {
    // The launching run's identity was lost (should not happen: runs are serialized).
    meta.runId = "run-" + std::to_string((long long)unixNow());
    meta.settings = s.topo.value("topology", nlohmann::json::object());
  }
  nlohmann::json result = topology::compactResult(res);
  if (!job.ok) {
    if (!result.contains("ok"))
      result["ok"] = false;
    if (!result.contains("error") && !job.error.empty())
      result["error"] = job.error;
  }
  nlohmann::json record = {
      {"run_id", meta.runId},
      {"label", meta.label},
      {"started_utc", meta.startedUtc},
      {"finished_utc", unixNow()},
      {"settings", meta.settings},
      {"source", {{"geometry_key", meta.geometryKey}, {"setup_key", meta.setupKey}}},
      {"result", std::move(result)}};
  if (!s.topo.contains("runs") || !s.topo["runs"].is_array())
    s.topo["runs"] = nlohmann::json::array();
  s.topo["runs"].push_back(std::move(record));
  s.hasResult = true;
  s.topoRunsCheckKey.clear();
  const topology::OutcomeSummary o = topology::classifyResult(s.topo["runs"].back()["result"]);
  // Headline alone unless the detail adds something (never "Converged — Converged: …").
  const bool detailAdds = !o.detail.empty() && o.detail.rfind(o.headline, 0) != 0;
  s.topoStatus = o.headline + (detailAdds ? " \xe2\x80\x94 " + o.detail : std::string());
  s.topoStatusKind = o.outcome == topology::Outcome::Converged ? 0
                     : o.outcome == topology::Outcome::Failed ? 2
                                                               : 1;
  s.summary = o.headline;
  st.addLog("[topology] " + meta.runId + ": " + s.topoStatus);
  persist(st, ownerDocId, s);
  checkTopologyDfam(st, s, ownerDocId, meta.runId, meta.printProcess);
  if (uiActive) {
    st.status = "Topology: " + o.headline;
    st.statusKind = s.topoStatusKind;
    if (sel_ >= 0 && sel_ < (int)studies_.size() && studies_[sel_].id == s.id) {
      syncTopologyViews(s);
      topoResultsView_.selectedRunId = meta.runId;
      node_ = SimNode::Results;
      topoActions_.push_back({s.id, {topology::ResultsAction::Kind::ShowRun, meta.runId, ""}});
    }
  }
}

void SimManager::checkTopologyDfam(AppState &st, Study &s, const std::string &ownerDocId,
                                    const std::string &runId,
                                    const std::string &printProcess) {
  auto *run = topoFindRun(s.topo["runs"], runId);
  if (!run)
    return;
  const auto &result = topoObj(*run, "result");
  const auto &mfg = topoObj(result, "manufacturing");
  if (!topoTrue(mfg, "geometry_valid") || topoTrue(topoObj(mfg, "dfam"), "checked"))
    return;
  const std::string runDir = topoStr(result, "result_dir");
  if (runDir.empty()) {
    if (topology::mergeDfamResult(*run, {}, "the run has no result directory")) {
      s.topoRunsCheckKey.clear();
      persist(st, ownerDocId, s);
    }
    return;
  }
  nlohmann::json params = {{"run_dir", runDir}};
  if (!printProcess.empty())
    params["print_process"] = printProcess;
  const std::string studyId = s.id;
  st.rpc.call("topology_dfam", std::move(params),
              [&st, ownerDocId, studyId, runId](const core::RpcResult &reply) {
    SimManager *owner = st.docMirror.hasDoc() && st.docMirror.docId() == ownerDocId
                            ? st.simManager : nullptr;
    if (!owner)
      for (auto &tab : st.docTabs)
        if (tab.docId == ownerDocId && tab.session && tab.session->sim)
          owner = tab.session->sim.get();
    if (!owner)
      return;
    for (auto &study : owner->studies_) {
      if (study.type != StudyType::Topology || study.id != studyId ||
          !study.topo.contains("runs"))
        continue;
      auto *record = topoFindRun(study.topo["runs"], runId);
      if (!record)
        return;
      const std::string error = reply.ok && topoTrue(reply.result, "ok")
                                    ? std::string()
                                    : simJsonText(reply.result, "error", "message", reply.err);
      if (topology::mergeDfamResult(*record, reply.result, error)) {
        study.topoRunsCheckKey.clear();
        owner->persist(st, ownerDocId, study);
      }
      return;
    }
  }, 120000);
}

void SimManager::refreshTopologyRunsView(AppState &st, Study &s, bool force) {
  const nlohmann::json runs = s.topo.value("runs", nlohmann::json::array());
  const std::string geom = topologyGeometryKey(st);
  const std::string key = std::to_string(runs.is_array() ? runs.size() : 0) + "|" + geom +
                          "|" +
                          (runs.is_array() && !runs.empty() ? topoRunIdOf(runs.back(), runs.size() - 1)
                                                            : std::string());
  if (!force && key == s.topoRunsCheckKey)
    return;
  s.topoRunsCheckKey = key;
  nlohmann::json view = runs.is_array() ? runs : nlohmann::json::array();
  for (auto &run : view) {
    if (!run.is_object())
      continue;
    const std::string runGeom = topoStr(topoObj(run, "source"), "geometry_key");
    // A run made on other source geometry no longer describes the part: its artifacts are
    // stale, never presented as current. A run recorded without the GUI's geometry digest
    // (model-tool runs) cannot be checked; it is flagged unverified, not silently current.
    const bool sourceStale = !runGeom.empty() && (geom.empty() || runGeom != geom);
    if (runGeom.empty())
      run["source_unverified"] = true;
    if (!run.contains("result") || !run["result"].is_object())
      continue;
    nlohmann::json &result = run["result"];
    if (result.contains("artifacts") && result["artifacts"].is_object())
      for (auto it = result["artifacts"].begin(); it != result["artifacts"].end(); ++it) {
        nlohmann::json &a = it.value();
        if (!a.is_object())
          continue;
        const std::string fmt = topoArtifactFormat(a);
        if (!fmt.empty())
          a["format"] = fmt;
        const std::string path = topoStr(a, "path");
        std::error_code ec;
        const bool exists = !path.empty() && std::filesystem::is_regular_file(path, ec);
        a["missing"] = !exists;
        bool stale = sourceStale;
        if (exists && a.contains("bytes") && a["bytes"].is_number_integer()) {
          const auto size = std::filesystem::file_size(path, ec);
          stale = stale || ec || (long long)size != a["bytes"].get<long long>();
        }
        a["stale"] = stale;
      }
    if (result.contains("overlay_aicm") && result["overlay_aicm"].is_string()) {
      std::error_code ec;
      const std::string path = result["overlay_aicm"].get<std::string>();
      if (sourceStale || !std::filesystem::is_regular_file(path, ec))
        result.erase("overlay_aicm");
    }
    if (sourceStale)
      run["source_stale"] = true;
  }
  s.topoRunsView = std::move(view);
}

bool SimManager::showTopologyRun(AppState &st, Study &s, const std::string &runId) {
  refreshTopologyRunsView(st, s);
  const nlohmann::json *run = topoFindRun(s.topoRunsView, runId);
  if (!run) {
    st.toast(ui::ToastLevel::Warn, "That run is no longer in this study.");
    return false;
  }
  const nlohmann::json overlay = topoOverlayArtifact(*run);
  if (!overlay.is_object()) {
    st.toast(ui::ToastLevel::Warn,
             run->value("source_stale", false)
                 ? "This run was made on different geometry; run the study again to view it."
                 : "This run has no viewport overlay file (missing or not produced).");
    return false;
  }
  const std::vector<nlohmann::json> fields = topoOverlayFields(*run, overlay);
  const nlohmann::json *chosen = nullptr;
  for (const auto &f : fields)
    if (f["name"] == st.activeScalarField)
      chosen = &f;
  for (const auto &f : fields)
    if (!chosen && f["name"] == "density")
      chosen = &f;
  if (!chosen && !fields.empty())
    chosen = &fields.front();
  if (chosen) {
    st.activeScalarField = (*chosen)["name"].get<std::string>();
    topoFieldMeta(st, *chosen, s.topoFieldLabel, s.topoFieldUnit, s.topoFieldFromSI);
  } else {
    st.activeScalarField = "density";
    s.topoFieldLabel = "Density (solid fraction)";
    s.topoFieldUnit.clear();
    s.topoFieldFromSI = 1.0;
  }
  s.overlayPath = overlay["path"].get<std::string>();
  s.hasResult = true;
  topoResultsView_.selectedRunId = runId;
  // syncResultView applies it on the next tick while Results is the active node.
  resultShown_ = false;
  shownStudyId_.clear();
  return true;
}

void SimManager::exportTopologyArtifact(AppState &st, Study &s, const std::string &runId,
                                        const std::string &artifactKey) {
  if (topoExport_) {
    st.toast(ui::ToastLevel::Info, "An export is already being written.");
    return;
  }
  refreshTopologyRunsView(st, s, true);
  const nlohmann::json *run = topoFindRun(s.topoRunsView, runId);
  const nlohmann::json result =
      run ? topoObj(*run, "result") : nlohmann::json::object();
  const nlohmann::json arts = topoObj(result, "artifacts");
  if (!run || !arts.is_object() || !arts.contains(artifactKey)) {
    st.toast(ui::ToastLevel::Warn, "That result file is not recorded for this run.");
    return;
  }
  const nlohmann::json a = arts[artifactKey];
  if (!topoTrue(a, "missing") && topoStr(a, "path").empty()) {
    st.toast(ui::ToastLevel::Warn, "That result file has no recorded path.");
    return;
  }
  if (topoTrue(a, "missing")) {
    st.toast(ui::ToastLevel::Warn, "The result file is missing on disk; run the study again.");
    return;
  }
  if (topoTrue(a, "stale")) {
    st.toast(ui::ToastLevel::Warn,
             "The result file no longer matches this run or the current part; run again.");
    return;
  }
  const std::string fmt = topoArtifactFormat(a);
  const bool surface = fmt == "stl" || fmt == "3mf" || fmt == "obj";
  const bool geometryValid = topoTrue(topoObj(result, "manufacturing"), "geometry_valid");
  if (surface && !geometryValid) {
    st.toast(ui::ToastLevel::Warn,
             "The reconstructed surface did not pass geometry validation; it is not exported.");
    return;
  }
  const std::string src = a.value("path", std::string());
  std::string ext = std::filesystem::path(src).extension().string();
  if (!ext.empty() && ext[0] == '.')
    ext.erase(0, 1);
  if (ext.empty())
    ext = fmt.empty() ? "dat" : fmt;
  const std::string base = simPathSegment(s.name) + "_" + simPathSegment(runId) + "_" +
                           simPathSegment(artifactKey);
  std::string dst;
  if (std::getenv("CHUDWARE_NO_FILE_DIALOGS")) {
    dst = (std::filesystem::current_path() / (base + "." + ext)).string();
  } else {
    const std::string upper = [&] {
      std::string u = ext;
      for (char &c : u)
        c = (char)std::toupper((unsigned char)c);
      return u;
    }();
    std::string filter = upper + " file (*." + ext + ")";
    filter.push_back('\0');
    filter += "*." + ext;
    filter.push_back('\0');
    dst = ui::saveFileDialog(filter.c_str(), ext.c_str(), (base + "." + ext).c_str());
    if (dst.empty())
      return; // cancelled
  }
  std::error_code ec;
  if (std::filesystem::equivalent(src, dst, ec)) {
    st.toast(ui::ToastLevel::Warn, "Choose a destination other than the run's own file.");
    return;
  }
  auto job = std::make_shared<TopoExport>();
  job->src = src;
  job->dst = dst;
  job->expectBytes = a.contains("bytes") && a["bytes"].is_number_integer()
                         ? a["bytes"].get<long long>()
                         : -1;
  topoExport_ = job;
  st.toast(ui::ToastLevel::Info, "Exporting " + std::filesystem::path(dst).filename().string() +
                                     "\xe2\x80\xa6");
  // The copy runs off the GUI thread; tick() reports completion. The run's own file is never
  // touched (copy source only).
  std::thread([job] {
    std::error_code err;
    std::filesystem::copy_file(job->src, job->dst,
                               std::filesystem::copy_options::overwrite_existing, err);
    if (err) {
      job->err = err.message();
    } else {
      const auto size = std::filesystem::file_size(job->dst, err);
      if (err)
        job->err = err.message();
      else if (job->expectBytes >= 0 && (long long)size != job->expectBytes)
        job->err = "copied size differs from the recorded artifact size";
      else
        job->ok = true;
    }
    job->done = true;
  }).detach();
}

void SimManager::continueTopologyInCad(AppState &st, Study &s, const std::string &runId) {
  if (st.rpc.interactionBusy() || topologyRunActive()) {
    st.toast(ui::ToastLevel::Info, "Wait for the current engine request or topology run.");
    return;
  }
  refreshTopologyRunsView(st, s, true);
  const nlohmann::json *run = topoFindRun(s.topoRunsView, runId);
  if (!run) {
    st.toast(ui::ToastLevel::Warn, "That run is no longer in this study.");
    return;
  }
  const nlohmann::json &result = topoObj(*run, "result");
  const nlohmann::json &surface = topoObj(topoObj(result, "artifacts"),
                                         topology::surfaceArtifactKey(result).c_str());
  const std::string path = topoStr(surface, "path");
  if (!topoTrue(topoObj(result, "manufacturing"), "geometry_valid") ||
      path.empty() || topoTrue(surface, "missing") || topoTrue(surface, "stale")) {
    st.toast(ui::ToastLevel::Warn,
             "This run has no current, validated surface to continue in CAD.");
    return;
  }
  st.toast(ui::ToastLevel::Info, "Creating an editable solid from the optimized surface...");
  ++st.rebuildsInFlight;
  st.rpc.call("import_file", {{"path", path}, {"as_solid", true}},
              [&st](const core::RpcResult &r) {
    if (!r.ok || !r.result.is_object() || r.result.contains("code") ||
        !simJsonBool(r.result, "ok", true)) {
      --st.rebuildsInFlight;
      st.toast(ui::ToastLevel::Error, "Could not create the optimized CAD part: " +
          simJsonText(r.result, "message", "error", r.err));
      return;
    }
    const auto &rebuild = topoObj(r.result, "rebuild");
    if (rebuild.contains("feature_status") && rebuild["feature_status"].is_array())
      for (const auto &f : rebuild["feature_status"])
        if (!simJsonBool(f, "ok", false)) {
          --st.rebuildsInFlight;
          st.toast(ui::ToastLevel::Error, "Could not create the optimized CAD part: " +
              simJsonText(f, "error", "message", "solid reconstruction failed"));
          return;
        }
    const std::string doc = topoStr(r.result, "doc_id");
    if (doc.empty()) {
      --st.rebuildsInFlight;
      st.toast(ui::ToastLevel::Error, "Solid import returned no document.");
      return;
    }
    st.rpc.call("doc_activate", {{"doc_id", doc}},
                [&st, doc](const core::RpcResult &activated) {
      --st.rebuildsInFlight;
      std::string aicm, err;
      const bool suppress = st.suppressTabDirty;
      st.suppressTabDirty = true;
      const bool ok = activated.ok && activated.result.is_object() &&
          st.docMirror.adoptActivateResponse(doc, activated.result, aicm, err);
      st.suppressTabDirty = suppress;
      if (!ok) {
        st.toast(ui::ToastLevel::Error, "Created the optimized part, but could not open it: " +
            (activated.ok ? err : activated.err));
        return;
      }
      if (st.simManager)
        st.simManager->clearResultOverlay(st);
      st.ensureActiveTab();
      st.clearViewportForNewDoc();
      st.applyDocDrivenVisibility(st.docMirror.tree());
      st.markActiveTabDirty();
      if (!aicm.empty()) {
        st.fitViewOnNextMeshCommit = true;
        st.loadDocMeshAsync(aicm);
      }
      st.activeWorkspace = AppState::Workspace::Model;
      st.leftPaneTab = AppState::LP_FeatureManager;
      st.showRibbon = true;
      st.ribbonTab = AppState::RT_Features;
      st.addLog("[topology] continued in CAD as " + doc);
      st.toast(ui::ToastLevel::Success,
               "Optimized solid opened in CAD. Add features and save it as a new .step+ part.");
    }, core::kLongEngineCallTimeoutMs);
  }, core::kLongEngineCallTimeoutMs);
}

void SimManager::processTopologyActions(AppState &st) {
  if (topoExport_ && topoExport_->done.load()) {
    auto job = std::move(topoExport_);
    if (job->ok) {
      st.toast(ui::ToastLevel::Info, "Exported " + job->dst);
      st.addLog("[topology] exported " + job->src + " -> " + job->dst);
      ui::afterFileWritten(job->dst);
    } else {
      st.toast(ui::ToastLevel::Error, "Export failed: " + job->err);
      st.addLog("[topology] export failed (" + job->dst + "): " + job->err);
    }
  }
  if (topoActions_.empty())
    return;
  std::vector<TopoPendingAction> actions;
  actions.swap(topoActions_);
  for (const auto &pa : actions) {
    Study *s = nullptr;
    for (auto &study : studies_)
      if (study.id == pa.studyId && study.type == StudyType::Topology)
        s = &study;
    if (!s)
      continue;
    switch (pa.action.kind) {
    case topology::ResultsAction::Kind::ShowRun:
      showTopologyRun(st, *s, pa.action.runId);
      break;
    case topology::ResultsAction::Kind::ExportArtifact:
      exportTopologyArtifact(st, *s, pa.action.runId, pa.action.artifactKey);
      break;
    case topology::ResultsAction::Kind::ContinueInCad:
      continueTopologyInCad(st, *s, pa.action.runId);
      break;
    case topology::ResultsAction::Kind::ApplyRunSettings: {
      if (topologyRunActive()) {
        st.toast(ui::ToastLevel::Info, "Wait for the topology run to finish.");
        break;
      }
      const nlohmann::json runs = s->topo.value("runs", nlohmann::json::array());
      const nlohmann::json *run = topoFindRun(runs, pa.action.runId);
      if (!run)
        break;
      const nlohmann::json settings = run->value("settings", nlohmann::json::object());
      nlohmann::json &topo = s->topo["topology"];
      if (!topo.is_object())
        topo = nlohmann::json::object();
      topology::applyRunSettings(topo, settings);
      persist(st, *s);
      const std::string label = topoStr(*run, "label");
      st.toast(ui::ToastLevel::Info, "Settings restored from " +
                                         (label.empty() ? std::string("the selected run") : label) +
                                         ".");
      break;
    }
    case topology::ResultsAction::Kind::None:
      break;
    }
  }
}

void SimManager::drawTopologyForm(AppState &st, Study &s) {
  syncTopologyViews(s);
  topoResultsView_.compareMode = false; // Compare is a Results view; any setup page leaves it
  const bool locked = topologyRunActive() && (topoRunMeta_.studyId == s.id ||
                                              (staticModalPrep_ && staticModalPrep_->studyId == s.id));
  const char *lockedReason =
      locked ? "A run owns this study; edits unlock when it finishes or is cancelled."
             : nullptr;
  const bool capsDown = topoCaps_.state == topology::Capabilities::State::Unavailable;
  if (node_ == SimNode::Study) {
    if (capsDown) {
      if (kit::blockedState("Topology solver unavailable", topoCaps_.error.c_str(),
                            "Saved settings are kept unchanged until the solver is available.",
                            "Check again"))
        probeTopologyCaps(st, true);
      return;
    }
    // Never insert a null `topology` key into an agent record just by drawing the form.
    nlohmann::json scratch = s.topo.contains("topology") ? s.topo["topology"] : nlohmann::json();
    if (topology::drawTopologySettings(st, scratch, topoCaps_, topoSettingsView_, lockedReason,
                                       topology::kGroupsSolver, s.topo)) {
      s.topo["topology"] = std::move(scratch);
      persist(st, s);
    }
    return;
  }
  if (capsDown) {
    kit::inlineStatus(topoCaps_.error.c_str(), kit::Tone::Err);
    if (kit::secondaryButton("Check the topology solver again"))
      probeTopologyCaps(st, true);
  }
  const topology::BcSection section = node_ == SimNode::Material   ? topology::BcSection::Material
                                      : node_ == SimNode::Regions  ? topology::BcSection::Regions
                                                                   : topology::BcSection::Boundaries;
  std::vector<std::pair<std::string, std::string>> library;
  for (const auto &m : materials())
    library.emplace_back(m.key, m.name + (m.custom ? " (custom)" : ""));
  topoBcView_.docId = st.docMirror.docId();
  const topology::BcResult r = topology::drawBoundaryEditor(
      st, s.topo, section, topoBcView_, topologyMaterialLookup(), library, lockedReason);
  if (r.committed)
    persist(st, s);
  if (r.armChanged)
    st.selection.clear(); // a freshly armed well starts without stale picks
  armedWell_ = topoBcView_.armedPointer.empty() ? -1 : kArmTopology;
  if (node_ == SimNode::Regions && !capsDown) {
    kit::gapSection();
    nlohmann::json absentSettings;
    nlohmann::json &settings = s.topo.contains("topology") ? s.topo["topology"] : absentSettings;
    bool manufacturingCommit = false;
    const auto additiveControls = [&] {
      const auto &presets = topologyPrintProcesses();
      if (!presets.entries.empty()) {
        int &selected = topoSettingsView_.printProcessIndex;
        if (selected < 0 || selected >= (int)presets.entries.size()) {
          selected = 0;
          for (std::size_t i = 0; i < presets.entries.size(); ++i)
            if (topoStr(presets.entries[i], "id") == topoStr(s.topo, "print_process"))
              selected = (int)i;
        }
        {
          kit::FieldGroup group("print_process", "Print process");
          kit::fieldCombo("preset", "Print process", &selected, presets.labels,
                         kit::FieldOpts().withTip(presets.tips[selected].c_str()));
        }
        if (kit::secondaryButton("Apply"))
          manufacturingCommit |= topology::applyPrintProcess(s.topo, presets.entries[selected],
                                                               topoCaps_);
        kit::itemTooltip(presets.tips[selected].c_str());
        for (std::size_t i = 0; i < presets.entries.size(); ++i)
          if (topoStr(presets.entries[i], "id") == topoStr(s.topo, "print_process"))
            kit::chip(presets.labels[i].c_str(), kit::Tone::Accent);
      }
      const auto plate = topology::drawBuildPlateEditor(st, s.topo, topoBcView_, lockedReason);
      manufacturingCommit |= plate.committed;
      if (plate.armChanged)
        st.selection.clear();
      armedWell_ = topoBcView_.armedPointer.empty() ? -1 : kArmTopology;
    };
    const bool scalarCommit = topology::drawTopologySettings(
        st, settings, topoCaps_, topoSettingsView_, lockedReason,
        topology::kGroupsManufacturing, s.topo, additiveControls);
    if (scalarCommit || manufacturingCommit) {
      persist(st, s);
    }
  }
}

void SimManager::drawTopologyResults(AppState &st, Study &s) {
  syncTopologyViews(s);
  const nlohmann::json &runs = s.topoRunsView;
  if (topoResultsView_.compareMode && (!runs.is_array() || runs.size() < 2)) {
    // Compare is its own view, never a copy of Results: with fewer than two runs it says what
    // it needs. The footer's Run / Re-run stays the one primary action.
    const bool none = !runs.is_array() || runs.empty();
    if (kit::emptyState("plots_view", "Compare needs two runs",
                        none ? "Run the study, change a setting, then run it again. Compare "
                               "puts the two runs side by side: objective, volume, mass and "
                               "constraints."
                             : "This study has one run. Change a setting (for example the "
                               "volume target or the method), then press Re-run optimization "
                               "below. Compare then puts the two runs side by side.",
                        "Back to results"))
      topoResultsView_.compareMode = false;
    return;
  }
  if (!runs.is_array() || runs.empty()) {
    // Same vocabulary as drawResultsEmpty: a spinner while this study runs; blocked → name the
    // problem and jump to (and scroll to) the setup entry that fixes it; else say how to run.
    // The footer's "Run optimization" is the pane's one primary action.
    const SimRunUi ru = runUi();
    if (ru.running && ru.studyId == s.id) {
      kit::spinner(ru.stage.empty() ? "Optimizing\xe2\x80\xa6" : ru.stage.c_str());
      return;
    }
    const std::string blocker = runBlockerFor(st, s);
    if (blocker.empty()) {
      kit::emptyState("study_topology", "No results yet",
                      "Run the study to see results: press Run optimization below. Every run is "
                      "kept here for comparison and export.");
      return;
    }
    const std::vector<SetupStep> steps = setupSteps(st, s);
    const SetupStep *fix = nullptr;
    for (const SetupStep &step : steps)
      if (step.status == 2 && !fix) // 2 = needs attention (SetupStep::status)
        fix = &step;
    const std::string action = fix ? std::string("Go to ") + fix->label : std::string();
    if (kit::blockedState("Can't run yet", blocker.c_str(),
                          fix ? "Fix the setup, then run the study to see results."
                              : "Run the study to see results once this clears.",
                          fix ? action.c_str() : nullptr) &&
        fix) {
      node_ = fix->node;
      runPage_ = RunPageView{};
      // Scroll the editor to the first blocking entry of that step (BcView focus).
      auto under = [](const std::string &k, const char *p) { return k.rfind(p, 0) == 0; };
      for (const auto &is : topology::validateBoundaries(s.topo, topologyMaterialLookup())) {
        if (is.severity != topology::Severity::Blocking || is.key.empty())
          continue;
        const bool mat = under(is.key, "/material");
        const bool reg = under(is.key, "/preserve") || under(is.key, "/exclude");
        const bool mesh = under(is.key, "/mesh");
        const bool match = fix->node == SimNode::Material  ? mat
                           : fix->node == SimNode::Regions ? reg
                           : fix->node == SimNode::Boundaries
                               ? !mat && !reg && !mesh
                               : false;
        if (match) {
          topoBcView_.focusPointer = is.key;
          topoBcView_.focusFrames = 0;
          break;
        }
      }
    }
    return;
  }
  // Viewport field selector (exact chunks of that AICM file): drawn by the results panel after
  // the outcome and key metrics (standard §5 order).
  auto displayControls = [&]() {
    if (!resultShown_ || shownStudyId_ != s.id)
      return;
    const nlohmann::json *run = topoFindRun(runs, topoResultsView_.selectedRunId);
    if (!run)
      return;
    const nlohmann::json overlay = topoOverlayArtifact(*run);
    const std::vector<nlohmann::json> fields =
        overlay.is_object() ? topoOverlayFields(*run, overlay) : std::vector<nlohmann::json>{};
    if (fields.size() <= 1)
      return;
    // One entry per field: the plain label (legend title) and the combo text with unit.
    std::vector<std::string> names, plain, labels, units;
    std::vector<double> froms;
    int sel = -1;
    for (const auto &f : fields) {
      std::string label, unit;
      double from = 1.0;
      topoFieldMeta(st, f, label, unit, from);
      if (topoStr(f, "name") == st.activeScalarField)
        sel = (int)names.size();
      names.push_back(topoStr(f, "name"));
      labels.push_back(unit.empty() ? label : label + " (" + kit::unitText(unit.c_str()) + ")");
      plain.push_back(std::move(label));
      units.push_back(std::move(unit));
      froms.push_back(from);
    }
    kit::FieldGroup group("topo_field", "Viewport field");
    kit::FieldOpts o;
    o.withTip("Scalar the viewport overlay is coloured by.")
        .withHint(s.topoFieldLabel.empty() ? "Choose a field" : s.topoFieldLabel.c_str());
    if (kit::fieldCombo("field", "Viewport field", &sel, labels, o) && sel >= 0 &&
        names[(size_t)sel] != st.activeScalarField) {
      const size_t k = (size_t)sel;
      st.activeScalarField = names[k];
      s.topoFieldLabel = plain[k];
      s.topoFieldUnit = units[k];
      s.topoFieldFromSI = froms[k];
      resultShown_ = false; // syncResultView reloads the overlay in tick
      shownStudyId_.clear();
    }
  };
  const nlohmann::json &last = runs.back();
  if (topoTrue(last, "source_stale"))
    kit::inlineStatus("The part changed since the latest run: its results describe the old "
                      "geometry and are marked stale. Run again.",
                      kit::Tone::Warn);
  else if (topoTrue(last, "source_unverified"))
    kit::inlineStatus("The latest run was recorded without this editor's geometry digest "
                      "(e.g. by a model tool); it cannot be checked against the current part.",
                      kit::Tone::Info);
  else if (!topoSetupKeyNow_.empty() && topoSetupKeyStudy_ == s.id &&
           topoStr(topoObj(last, "source"), "setup_key") !=
               topoSetupKeyNow_)
    kit::inlineStatus("The setup was edited after the latest run; run again to see its effect.",
                      kit::Tone::Info);
  const topology::ResultsAction a =
      topology::drawTopologyResults(st, runs, topoCaps_, topoResultsView_, displayControls);
  if (a.kind != topology::ResultsAction::Kind::None)
    topoActions_.push_back({s.id, a});
}

void SimManager::drawTopologyLiveStatus(AppState &st, Study &s) {
  if (s.type != StudyType::Topology)
    return;
  ImGui::PushID("topo_live");
  const bool ownPrep = prepInFlight_ && staticModalPrep_ &&
                       staticModalPrep_->type == StudyType::Topology &&
                       staticModalPrep_->studyId == s.id;
  const bool ownSolve = running_ && topoRunMeta_.studyId == s.id && !ownPrep;
  // One place per message: progress lives in the footer, a finished run's headline in the Run
  // page's Last run card and the Results outcome card. Shown here only: a warning while this
  // study's run is in flight (cancellation requested) or an error (kind 2), which the Last run
  // card leaves out.
  const bool show = !s.topoStatus.empty() &&
                    (s.topoStatusKind == 2 || ((ownPrep || ownSolve) && s.topoStatusKind == 1));
  if (show)
    kit::inlineStatus(s.topoStatus.c_str(),
                      s.topoStatusKind == 2 ? kit::Tone::Err : kit::Tone::Warn);
  if (ownSolve && st.simClient &&
      (topoLiveObjective_.size() >= 2 || topoLiveVolume_.size() >= 2 ||
       topoLiveChange_.size() >= 2)) {
    kit::sectionHeader("Live convergence");
    // Sparklines over the iterations reported so far (sample index on x); the caption carries
    // the latest value.
    auto spark = [](const char *id, const std::vector<double> &values, const char *caption) {
      if (values.size() < 2)
        return;
      kit::PlotSpec p;
      p.mode = kit::PlotMode::Sparkline;
      kit::PlotSeries line;
      line.label = caption;
      line.y = kit::PlotData(values);
      p.series.push_back(line);
      p.caption = caption;
      p.captionLast = true;
      kit::linePlot(id, p);
    };
    spark("objective", topoLiveObjective_, "Objective");
    spark("volume", topoLiveVolume_, "Volume fraction");
    spark("change", topoLiveChange_, "Design change");
  }
  ImGui::PopID();
}

void SimManager::drawTopologyRunArea(AppState &st, Study &s) {
  const bool ownPrep = prepInFlight_ && staticModalPrep_ &&
                       staticModalPrep_->type == StudyType::Topology &&
                       staticModalPrep_->studyId == s.id;
  const bool ownSolve = running_ && topoRunMeta_.studyId == s.id && !ownPrep;
  const bool otherBusy = !ownPrep && !ownSolve &&
                         ((st.simClient && st.simClient->busy()) || prepInFlight_ ||
                          advRunning_ || motionRun_ || st.rpc.interactionBusy());
  std::string blocker;
  bool ready = st.docMirror.hasDoc() && hasSimulationGeometry(st);
  if (!ready)
    blocker = "The active document has no solid geometry to optimize.";
  else if (!st.simClient) {
    ready = false;
    blocker = "The simulation backend is unavailable.";
  } else
    ready = topologyReady(st, s, blocker);
  if (otherBusy)
    blocker = "Another simulation is running or preparing.";

  // "Re-run" only once a run exists: a Results step with nothing to show offers the plain primary.
  const bool hasRuns =
      s.topo.contains("runs") && s.topo["runs"].is_array() && !s.topo["runs"].empty();
  const bool rerun = node_ == SimNode::Results && hasRuns;
  // A Results step with no runs already explains the blocker in its blocked state; the footer
  // does not repeat it (Compare's empty state does not name it, so the footer keeps it there).
  const bool bodyNamesBlocker =
      node_ == SimNode::Results && !hasRuns && !topoResultsView_.compareMode && !otherBusy;
  kit::ActionSpec a;
  a.primaryLabel = rerun ? "Re-run optimization" : "Run optimization";
  a.primaryAsSecondary = rerun;
  a.primaryEnabled = ready && !otherBusy;
  a.blocker = a.primaryEnabled || blocker.empty() || bodyNamesBlocker ? nullptr : blocker.c_str();
  std::string stage;
  if (ownPrep) {
    a.running = true;
    stage = "Preparing the design domain";
  } else if (ownSolve && st.simClient) {
    a.running = true;
    // Shared stage vocabulary (footer + status bar): never a raw engine stage id. The iteration
    // counter shows while optimizing; the final reanalysis / reconstruction / save stages name
    // themselves instead of a frozen "Iteration N of N".
    const std::string raw = st.simClient->stage();
    const bool postIter = raw.rfind("topology.", 0) == 0 && raw != "topology.iteration" &&
                          raw != "topology.prepare";
    if (!topoLiveIter_.empty() && !postIter) {
      const int maxIt = topoLiveMaxIter_;
      const int it = (int)topoLiveIter_.back();
      stage = "Iteration " + std::to_string(it) +
              (maxIt > 0 ? " of " + std::to_string(maxIt) : std::string());
      if (maxIt > 0)
        a.progress = std::clamp((float)it / (float)maxIt, 0.0f, 1.0f);
    } else if (!raw.empty()) {
      const std::string log = st.simClient->recentLog();
      stage = simStageLabel(raw, log);
      a.progress = simStageFraction(raw, log);
    }
    if (stage.empty())
      stage = "Optimizing";
    a.elapsed = st.simClient->elapsedSeconds();
    a.cancelRequested = st.simClient->cancelRequested();
  }
  a.stage = stage.empty() ? nullptr : stage.c_str();
  switch (kit::actionFooter("topo_run", a)) {
  case kit::FooterAction::Primary:
    armedWell_ = -1;
    run(st, s);
    runPage_ = RunPageView{s.id, node_, false}; // same as every study: show the Run step
    break;
  case kit::FooterAction::Cancel:
    if (ownPrep) {
      cancelTopologyPrep(st);
    } else if (st.simClient) {
      st.simClient->cancel();
      s.topoStatus = "Cancellation requested; the optimizer stops at its next checkpoint.";
      s.topoStatusKind = 1;
      st.status = "Topology: cancellation requested";
      st.statusKind = 1;
    }
    break;
  default:
    break;
  }
}

} // namespace ui
