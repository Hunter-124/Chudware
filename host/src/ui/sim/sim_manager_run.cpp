// SimManager run pipeline: solve dispatch, static/modal/thermal/contact/FRF prep, tick, job draining.
#include "sim_manager_internal.h"

#include <algorithm>
#include <cctype>
#include <cstdio>
#include <cstdlib>
#include <initializer_list>
#include <mutex>

#include "../../core/sim_client.h"
#include "engine_backend.h" // engine::daemonMode — server-owned export paths

namespace ui {

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

namespace {
// Only native-consumed keys cross the solver boundary. Ownership, paths, inline meshes and
// measured governor probes always belong to the export/job pipeline, never the editor.
void copyNativeKeys(nlohmann::json &to, const nlohmann::json &from,
                    std::initializer_list<const char *> keys) {
  if (!from.is_object())
    return;
  for (const char *key : keys)
    if (from.contains(key))
      to[key] = from[key];
}

nlohmann::json nativeSetup(nlohmann::json record) {
  for (const char *key : {"id", "name", "result_summary", "results", "result", "runs",
                          "last_job", "last_run", "summary", "provenance", "mesh_quality",
                          "reactions", "advisor"})
    record.erase(key);
  return record;
}

bool nativeSourceCurrent(const AppState &st, const std::string &docId,
                         const std::string &geometryKey, std::uint64_t mutationGen) {
  if (st.docMirror.hasDoc() && st.docMirror.docId() == docId)
    return !(st.rebuildsInFlight > 0 && st.docMirror.mutationGen() != mutationGen) &&
           topology::geometryKey(st.docMirror.tree()) == geometryKey;
  for (const auto &tab : st.docTabs)
    if (tab.docId == docId)
      return !tab.meshCacheInvalidated && tab.session &&
             topology::geometryKey(tab.session->cachedMirror.tree) == geometryKey;
  return false;
}

// Native facesets are terminal geometry, not nested document query objects.
nlohmann::json boundarySelector(const nlohmann::json &entry) {
  if (entry.is_object() && !entry.contains("points") && entry.contains("face"))
    return entry["face"];
  return entry;
}

void applyFacesetSettings(nlohmann::json &face, const nlohmann::json &entry) {
  copyNativeKeys(face, entry,
                 {"tol_mm", "perp_tol_mm", "face_normal", "surface", "temp_c", "dofs"});
}

template <typename Fn>
void forEachBoundaryFaceset(const nlohmann::json &resolved, Fn &&fn) {
  if (resolved.is_object() && resolved.contains("facesets")) {
    for (const auto &face : resolved["facesets"])
      fn(face);
  } else {
    fn(resolved);
  }
}

std::string simExportPath(const std::string &docId,
                          const std::string &studyId,
                          std::uint64_t reservationToken, bool flow) {
  const std::string leaf =
      "sim_" + simPathSegment(docId) + "_" + simPathSegment(studyId) + "_" +
      std::to_string(reservationToken) + (flow ? ".stl" : ".step");
  // Relative paths are intentionally server-owned in daemon/WASM mode.
  return engine::daemonMode() ? leaf : appdataAppDir() + "/cache/" + leaf;
}
} // namespace

// ---------------------------------------------------------------- run (ported
// from SimPanel)


// ---- progress stage labels (THE map; sim_manager.h) ---------------------------------------------
namespace {
// Meshing cell count in a worker progress line ("… 1921/1922 cells, …"); false when absent or
// the stage is not a meshing stage (the native probe block outlives its stage).
bool meshCells(const std::string &stage, const std::string &detail, long long &done,
               long long &total) {
  if (stage.rfind("mesh", 0) != 0)
    return false;
  const std::size_t cells = detail.find(" cells");
  if (cells == std::string::npos)
    return false;
  const std::size_t slash = detail.rfind('/', cells);
  if (slash == std::string::npos || slash == 0)
    return false;
  std::size_t b = slash;
  while (b > 0 && std::isdigit((unsigned char)detail[b - 1]))
    --b;
  if (b == slash || slash + 1 >= cells)
    return false;
  for (std::size_t i = slash + 1; i < cells; ++i)
    if (!std::isdigit((unsigned char)detail[i]))
      return false;
  done = std::strtoll(detail.c_str() + b, nullptr, 10);
  total = std::strtoll(detail.c_str() + slash + 1, nullptr, 10);
  return total > 0 && done >= 0;
}
} // namespace

std::string simStageLabel(const std::string &stage, const std::string &detail) {
  static const struct {
    const char *key, *label;
  } kStages[] = {
      {"voxelize", "Voxelizing"},
      {"mesh", "Meshing"},
      {"mesh.begin", "Meshing"},
      {"mesh_background_probe", "Meshing \xc2\xb7 density probe"},
      {"mesh_background_probe_complete", "Meshing \xc2\xb7 density probe done"},
      {"mesh_probe_skipped", "Meshing"},
      {"mesh_interface_band", "Meshing \xc2\xb7 interface band"},
      {"mesh_interface_band_extract", "Meshing \xc2\xb7 interface band"},
      {"mesh_interface_band_fill", "Meshing \xc2\xb7 filling interface band"},
      {"mesh_interface_band_refine", "Meshing \xc2\xb7 refining interface band"},
      {"mesh_interface_band_quality", "Meshing \xc2\xb7 checking element quality"},
      {"mesh_interface_band_complete", "Meshing \xc2\xb7 interface band done"},
      {"Starting", "Starting the solver"},
      {"setup", "Setting up"},
      {"cache.hit", "Loading the cached result"},
      {"assemble", "Assembling"},
      {"solve", "Solving"},
      {"solve.transient", "Solving \xc2\xb7 time steps"},
      {"solve.complete", "Finishing the solve"},
      {"stream", "Streaming results"},
      {"post", "Post-processing"},
      {"topology.prepare", "Preparing the design domain"},
      {"topology.iteration", "Optimizing"},
      {"topology.reanalyze", "Checking the final design"},
      {"topology.reconstruct", "Rebuilding the geometry"},
      {"topology.artifacts", "Saving results"},
      {"contact.selector_preflight", "Checking contact faces"},
      {"contact.geometry_preflight", "Checking contact geometry"},
      {"contact.geometry_preflight_complete", "Contact geometry checked"},
      {"contact.geometry_refused", "Contact geometry refused"},
      {"contact.normal_cone_preflight", "Checking contact normals"},
      {"contact.interface_classification", "Classifying contact pairs"},
      {"pipe_flow_voxelize", "Voxelizing the pipe"},
      {"pipe_flow_solve", "Solving pipe flow"},
  };
  std::string label;
  for (const auto &k : kStages)
    if (stage == k.key) {
      label = k.label;
      break;
    }
  if (label.empty() && stage.rfind("mesh", 0) == 0) {
    label = "Meshing";
  } else if (label.empty()) {
    // Unknown id: humanize it ("foo_bar.baz" -> "Foo bar baz"), never show the raw token.
    label.reserve(stage.size());
    for (char ch : stage)
      label += (ch == '_' || ch == '.' || ch == '-') ? ' '
                                                     : (char)std::tolower((unsigned char)ch);
    if (!label.empty())
      label[0] = (char)std::toupper((unsigned char)label[0]);
  }
  long long done = 0, total = 0;
  if (meshCells(stage, detail, done, total))
    label = "Meshing \xc2\xb7 " + groupedCount((uint64_t)std::min(done, total)) + " of " +
            groupedCount((uint64_t)total) + " cells";
  return label;
}

float simStageFraction(const std::string &stage, const std::string &detail) {
  long long done = 0, total = 0;
  if (!meshCells(stage, detail, done, total))
    return -1.0f;
  return std::clamp((float)done / (float)total, 0.0f, 1.0f);
}

// Phase text while a run exports/prepares the model, before any solver job exists. The pane
// footer (runUi) and the status line show this SAME wording; once the solver job launches, the
// status bar's job chip and the footer both read the solver's own stage (simStageLabel), and
// this status line is cleared so no third, stale phase lingers.
static const char *const kSimPreparingStage = "Preparing model";
static const char *const kSimPreparingStatus = "Preparing model\xe2\x80\xa6";

// Footer stage line of one progress event: the iteration counter while iterating, else the
// shared stage label (legacy free text when the event names no stage).
static std::string stageDisplay(const core::SimProgress &ev) {
  if (ev.hasIteration) {
    std::string s = "Iteration " + std::to_string(ev.iteration.index);
    if (ev.iteration.maxIterations > 0)
      s += " of " + std::to_string(ev.iteration.maxIterations);
    return s;
  }
  if (ev.stage.empty())
    return ev.text;
  return simStageLabel(ev.stage, ev.text);
}

std::string SimManager::runBlockerFor(AppState &st, const Study &s) const {
  std::string why;
  if (!st.docMirror.hasDoc())
    return "Open a part or assembly.";
  if (!hasSimulationGeometry(st))
    return "Model a solid body to simulate.";
  if (s.type == StudyType::Motion && !activeDocIsAssembly(st))
    return "Open an assembly to run Motion.";
  if (s.type == StudyType::Motion && !motionJointsReady(s, st.docMirror.tree(), why))
    return why;
  if (s.type == StudyType::Motion) {
    why = motionGeometryBlocker(st);
    if (!why.empty())
      return why;
  }
  if (!studyReady(s, why))
    return why;
  if (s.type == StudyType::Topology && !topologyReady(st, s, why))
    return why;
#ifdef __EMSCRIPTEN__
  if (isFlowFamily(s.type) || s.type == StudyType::Contact ||
      s.type == StudyType::FreqResponse)
    return "Not available in the web version yet.";
#endif
  if (s.type != StudyType::Motion && s.type != StudyType::Contact && !st.simClient)
    return "Start the simulation backend.";
  if ((isFlowFamily(s.type) && s.flowSolver == 1) || s.type == StudyType::Thermal) {
    const nlohmann::json flowCaps =
        st.simClient ? st.simClient->caps() : nlohmann::json::object();
    if (!flowCaps.is_object() || !flowCaps.value("openfoam", false))
      return s.type == StudyType::Thermal
                 ? "Thermal CFD needs OpenFOAM, which is not installed."
                 : "Choose Fast (LBM): OpenFOAM is not installed.";
  }
  if (s.type == StudyType::Contact || s.type == StudyType::FreqResponse) {
    if (capsStatus_ == 0)
      return "Checking solver availability\xe2\x80\xa6";
    if (capsStatus_ < 0)
      return "Solver check failed; restart the worker.";
    const char *leg = s.type == StudyType::Contact ? "ccx" : "freq_response";
    const char *flag = s.type == StudyType::Contact ? "found" : "available";
    const std::string fallback = s.type == StudyType::Contact
                                     ? "CalculiX is unavailable."
                                     : "Vibration post-processing is unavailable.";
    const auto it = caps_.find(leg);
    if (it == caps_.end() || !it->is_object() || !it->value(flag, false))
      return it != caps_.end() && it->is_object() ? it->value("hint", fallback) : fallback;
  }
  // Solver availability last: setup problems are the user's to fix first.
  if (running_ || advRunning_ || prepInFlight_ || motionRun_)
    return "Wait: a study in this document is running.";
  if (st.simClient && st.simClient->busy()) {
    auto &job = st.simClient->job();
    if (job.running.load() && !job.ownerDocId.empty() && job.ownerDocId != lastDocId_) {
      std::string other = "another document";
      for (const auto &tab : st.docTabs)
        if (tab.docId == job.ownerDocId && !tab.title.empty()) {
          other = tab.title;
          break;
        }
      return "Wait, or cancel the run in " + other + ".";
    }
    return "Wait: another document is preparing a run.";
  }
  return {};
}

std::string SimManager::runBlocker(const Study &s) const {
  if (!appSt_)
    return "The simulation workspace is still starting.";
  return runBlockerFor(*appSt_, s);
}

SimRunUi SimManager::runUi() const {
  SimRunUi r;
  const double elapsed = runStartTime_ >= 0.0 ? ImGui::GetTime() - runStartTime_ : -1.0;
  core::SimClient *sim = appSt_ ? appSt_->simClient : nullptr;
  if (running_) {
    r.running = true;
    r.studyId = runningStudyId_;
    r.elapsed = elapsed;
    if (prepInFlight_) {
      const bool topoPrep = staticModalPrep_ && staticModalPrep_->type == StudyType::Topology;
      r.stage = topoPrep ? "Preparing design domain" : kSimPreparingStage;
      r.cancellable = topoPrep;
      if (!topoPrep)
        r.cancelBlocker = "Model export and face resolution cannot be interrupted; Cancel "
                          "becomes available once the solver starts.";
      return r;
    }
    const bool jobRunning = sim && sim->job().running.load();
    // Before the first progress event, read the solver's current stage exactly as the status
    // bar's job chip does, so the footer never says "Solving" while the chip says "Starting".
    if (!runStageText_.empty())
      r.stage = runStageText_;
    else if (jobRunning)
      r.stage = simStageLabel(sim->stage(), sim->recentLog());
    else
      r.stage = "Loading the result";
    if (runPct_ > 0)
      r.progress = std::min(1.0f, runPct_ / 100.0f);
    r.cancellable = jobRunning;
    r.cancelRequested = jobRunning && sim->cancelRequested();
    if (!jobRunning)
      r.cancelBlocker = "The solve has finished; loading the result.";
    if (!r.cancelRequested && r.progress >= 0.02f && r.progress < 1.0f &&
        elapsed > 0.0) {
      const auto study = std::find_if(studies_.begin(), studies_.end(),
                                     [&](const Study &s) { return s.id == r.studyId; });
      if (study != studies_.end() && isFlowFamily(study->type)) {
        const double remaining = elapsed * (1.0 - r.progress) / r.progress;
        r.stage += " \xc2\xb7 about " + kit::fmtDuration(remaining) + " remaining";
      }
    }
    return r;
  }
  if (advRunning_) {
    r.running = true;
    r.studyId = advRunningStudyId_;
    r.stage = advStage_.empty() ? std::string("Solving") : advStage_;
    r.elapsed = elapsed;
    r.cancelBlocker = "This worker solve has no cancellation route; it finishes in the "
                      "background.";
    return r;
  }
  if (motionRun_) {
    r.running = true;
    r.elapsed = elapsed;
    r.cancellable = true;
    for (const auto &s : studies_)
      if (s.motionRunning) {
        r.studyId = s.id;
        r.stage = s.summary.empty() ? std::string("Sampling motion") : s.summary;
        return r;
      }
    // Discarded: the native solve still finishes in the background; its result is dropped.
    r.studyId = motionDiscardStudyId_;
    r.stage = "Discarding the pending result";
    r.cancelRequested = true;
  }
  return r;
}

void SimManager::requestCancel(AppState &st) {
  appSt_ = &st;
  const SimRunUi r = runUi();
  if (!r.running || !r.cancellable || r.cancelRequested)
    return;
  if (running_ && prepInFlight_) {
    cancelTopologyPrep(st);
    return;
  }
  if (running_ && st.simClient && st.simClient->job().running.load()) {
    st.simClient->cancel();
    st.status = "Cancelling: waiting for a solver checkpoint";
    st.statusKind = 1;
    return;
  }
  if (motionRun_) {
    for (auto &s : studies_)
      if (s.motionRunning) {
        motionDiscardStudyId_ = s.id;
        invalidateMotion(s);
        st.status = "Motion result discarded; the native solve finishes in the background";
        st.statusKind = 1;
        return;
      }
  }
}

void SimManager::syncStudies(AppState &st) {
  // A study panel needs a valid selection (a refresh or delete may have dropped it).
  if (panelOpen_ && (sel_ < 0 || sel_ >= (int)studies_.size()))
    panelOpen_ = false;
  // Studies mirror the ONE part material (doc.material.key); flow/thermal-CFD never read it and
  // topology owns its own material record.
  for (auto &s : studies_)
    resolveStudyMaterialFromDoc(st, s);
  // Modal solves ignore loads, but opening/running one must not erase authored setup.
}

void SimManager::run(AppState &st, Study &s) {
  appSt_ = &st;
  // Always re-seed material from the part so a FeatureManager / Mass Props
  // change is honored.
  resolveStudyMaterialFromDoc(st, s);
  auto block = [&](const std::string &why) {
    st.addLog("[sim] run blocked: " + why);
    st.toast(ui::ToastLevel::Warn, why);
  };
  if ((s.type == StudyType::Contact || s.type == StudyType::FreqResponse) && capsStatus_ == 0)
    probeCaps(st);
  const std::string why = runBlockerFor(st, s);
  if (!why.empty()) {
    block(why);
    return;
  }
  if (s.type != StudyType::Motion && st.rpc.interactionBusy()) {
    block("The engine is busy with another request; run again in a moment.");
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
  else if (s.type == StudyType::Contact)
    runContact(st, s);
  else if (s.type == StudyType::FreqResponse)
    runFreqResponse(st, s);
  else if (s.type == StudyType::Topology)
    runTopology(st, s);
  else
    runStaticOrModal(st, s);
}

void SimManager::runFatigue(AppState &st, Study &s) {
  if (!st.simClient || !st.docMirror.hasDoc())
    return;
  auto prep = std::make_shared<StaticModalPrep>();
  prep->type = s.type;
  const int mi = materialIndexByKey(s.materialKey);
  double matSut = 0, matSe = 0, matSy = 0;
  if (mi >= 0) {
    const auto &m = materials()[mi];
    matSut = m.ultimate_mpa;
    matSe = m.endurance_mpa;
    matSy = m.yield_mpa;
    prep->material = {{"E_mpa", m.E_mpa},
                      {"nu", m.nu},
                      {"density_g_cm3", m.density_g_cm3},
                      {"yield_mpa", m.yield_mpa},
                      {"ultimate_mpa", m.ultimate_mpa},
                      {"endurance_mpa", m.endurance_mpa}};
  }
  const auto record = studyToJson(s);
  if (record.contains("material") && record["material"].is_object()) {
    prep->material.update(record["material"]);
    finiteNumber(prep->material, "ultimate_mpa", matSut);
    finiteNumber(prep->material, "endurance_mpa", matSe);
    finiteNumber(prep->material, "yield_mpa", matSy);
  }
  double sutUsed = s.fatigueSut > 0 ? s.fatigueSut : matSut;
  if (record.contains("fatigue"))
    finiteNumber(record["fatigue"], "ultimate_mpa", sutUsed);
  if (sutUsed <= 0) {
    st.toast(ui::ToastLevel::Warn, "Set an ultimate strength (Sut) for this "
                                   "material before a fatigue run");
    st.addLog("[sim] fatigue: material has no ultimate strength (Sut)");
    return;
  }
  const double seBaseV = seBase(s, matSe, sutUsed);
  const double ka = marin_ka(sutUsed, s.fatigueSurface),
               kb = marin_kb(s.fatigueSizeMm, s.fatigueLoadCase),
               kc = marin_kc(s.fatigueLoadCase), kd = marin_kd(s.fatigueTempC);
  float lmin, lmax;
  fatigueLoadFactors(s, lmin, lmax);
  prep->fatigue = {{"lmin", lmin},
                   {"lmax", lmax},
                   {"se_corrected_mpa", ka * kb * kc * kd * seBaseV},
                   {"se_base_mpa", seBaseV},
                   {"ultimate_mpa", sutUsed},
                   {"yield_mpa", matSy},
                   {"marin", {{"ka", ka}, {"kb", kb}, {"kc", kc}, {"kd", kd}}}};
  beginExportPrep(st, s, prep);
}

void SimManager::runStaticOrModal(AppState &st, Study &s) {
  if (!st.simClient)
    return;
  if (!st.docMirror.hasDoc()) {
    st.addLog("[sim] no document to analyse");
    return;
  }

  auto prep = std::make_shared<StaticModalPrep>();
  prep->type = s.type;
  prep->nModes = s.nModes;


  const int materialIndex = materialIndexByKey(s.materialKey);
  if (materialIndex >= 0) {
    const auto &material = materials()[materialIndex];
    prep->material = {{"E_mpa", material.E_mpa},
                      {"nu", material.nu},
                      {"density_g_cm3", material.density_g_cm3},
                      {"yield_mpa", material.yield_mpa}};
  }

  beginExportPrep(st, s, prep);
}

void SimManager::beginExportPrep(
    AppState &st, Study &s, const std::shared_ptr<StaticModalPrep> &prep) {
  const std::string docId = st.docMirror.docId();
  if (!isFlowFamily(s.type) && s.type != StudyType::Topology) {
    prep->canonical = studyToJson(s);
    for (const char *key : {"fixtures", "loads"})
      if (prep->canonical.contains(key) && !prep->canonical[key].is_array()) {
        st.toast(ui::ToastLevel::Error, "Simulation boundaries must be arrays.");
        return;
      }
    if (s.type != StudyType::Modal && s.type != StudyType::FreqResponse &&
        prep->canonical.contains("loads"))
      for (const auto &row : prep->canonical["loads"]) {
        if (!row.is_object()) {
          st.toast(ui::ToastLevel::Error, "Every simulation load must be an object.");
          return;
        }
        const auto type = row.find("type");
        if (type != row.end() &&
            (!type->is_string() ||
             (*type != "force" && *type != "pressure" &&
              !(s.type == StudyType::ThermalFea && (*type == "flux" || *type == "convection"))))) {
          st.toast(ui::ToastLevel::Error, "This load type is not supported by the selected solver.");
          return;
        }
      }
  }
  std::uint64_t reservationToken = 0;
  if (prepInFlight_ ||
      !st.simClient->tryReserve(docId, reservationToken)) {
    st.toast(ui::ToastLevel::Info, "A simulation is already in progress");
    return;
  }
  lastDocId_ = docId;
  prep->sim = st.simClient;
  prep->reservationToken = reservationToken;
  prep->docId = docId;
  prep->studyId = s.id;
  prep->requestedStepPath =
      simExportPath(docId, s.id, reservationToken, isFlowFamily(s.type));
  prep->meshOptions.meshFineness = s.meshFineness;
  prep->meshOptions.meshTargetMm = s.meshTargetMm;
  prep->meshOptions.meshMinMm = s.meshMinMm;
  prep->meshOptions.learnedAdvisor = s.learnedAdvisor;
  prep->meshOptions.advisorMaxDof = s.advisorMaxDof;
  prep->meshOptions.adaptEnabled = s.adaptEnabled;
  prep->meshOptions.adaptMaxPasses = s.adaptMaxPasses;
  prep->meshOptions.adaptEtaTarget = s.adaptEtaTarget;
  prep->meshOptions.meshOrder = s.meshOrder;
  if (!isFlowFamily(s.type) && s.type != StudyType::Topology) {
    prep->sourceGeometryKey = topologyGeometryKey(st);
    prep->sourceMutationGen = st.docMirror.mutationGen();
    // Rebuild ONLY from the frozen merged rows: a reordered load carries its own raw keys.
    prep->fixtureSelectors.clear();
    prep->fixtureDefinitions.clear();
    if (prep->canonical.contains("fixtures") && prep->canonical["fixtures"].is_array())
      for (const auto &fixture : prep->canonical["fixtures"]) {
        prep->fixtureSelectors.push_back(boundarySelector(fixture));
        prep->fixtureDefinitions.push_back(fixture);
      }
    prep->fixtures.resize(prep->fixtureSelectors.size());
    prep->loads.clear();
    if (s.type != StudyType::Modal && s.type != StudyType::FreqResponse &&
        prep->canonical.contains("loads") && prep->canonical["loads"].is_array())
      for (const auto &row : prep->canonical["loads"]) {
        StaticModalPrep::Load load;
        load.definition = row;
        if (row.is_object()) {
          const auto faces = row.find("faces");
          if (faces != row.end()) {
            if (faces->is_array())
              for (const auto &face : *faces)
                load.selectors.push_back(boundarySelector(face));
            else
              load.selectors.push_back(boundarySelector(*faces));
          } else if (row.contains("face")) {
            load.selectors.push_back(row["face"]);
          }
        }
        load.resolved.resize(load.selectors.size());
        prep->loads.push_back(std::move(load));
      }
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
  st.status = kSimPreparingStatus;
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
                              "Simulation: export returned an invalid path");
          return;
        }
        prep->resolvedStepPath =
            pathIt == result.result.end()
                ? prep->requestedStepPath
                : pathIt->get<std::string>();
        if (prep->resolvedStepPath.empty()) {
          failStaticModalPrep(st, prep,
                              "Simulation: export returned no path");
          return;
        }

        prep->facesRemaining = prep->fixtureSelectors.size();
        for (const auto &load : prep->loads)
          prep->facesRemaining += load.selectors.size();
        prep->facesRemaining += prep->topoSelectors.size();
        if (prep->facesRemaining == 0) {
          finishStaticModalPrep(st, prep);
          return;
        }

        auto queueFace = [this, &st, prep](const nlohmann::json &selector,
                                          nlohmann::json *destination) {
          if (staticModalPrep_ != prep || !prepInFlight_)
            return;
          nlohmann::json clean = selector;
          if (clean.is_object())
            clean.erase("_pick");
          if (clean.is_object() && clean.contains("facesets")) {
            const auto &sets = clean["facesets"];
            bool valid = sets.is_array() && !sets.empty();
            for (const char *key : {"points", "tol_mm", "perp_tol_mm",
                                   "face_normal", "surface"})
              valid = valid && !clean.contains(key);
            if (valid)
              for (const auto &face : sets)
                valid = valid && face.is_object() && face.contains("points") &&
                        !face.contains("facesets");
            if (!valid) {
              failStaticModalPrep(
                  st, prep, "Compound simulation boundaries need terminal facesets "
                            "without compound-level geometry hints.");
              return;
            }
            *destination = std::move(clean);
            if (--prep->facesRemaining == 0)
              finishStaticModalPrep(st, prep);
            return;
          }
          if (clean.is_object() && clean.contains("points")) {
            *destination = std::move(clean);
            if (--prep->facesRemaining == 0)
              finishStaticModalPrep(st, prep);
            return;
          }
          if (clean.is_object() && clean.value("kind", std::string()) == "face") {
            st.rpc.call(
                "doc_face_points",
                {{"doc_id", prep->docId}, {"selector", std::move(clean)}},
                [this, &st, prep, destination](const core::RpcResult &faceResult) {
                  prepFaceResult(st, prep, faceResult, *destination);
                },
                30000);
            return;
          }

          auto queryFaces = [this, &st, prep, destination](
                                nlohmann::json query, std::string sid = std::string()) {
            if (staticModalPrep_ != prep || !prepInFlight_)
              return;
            st.rpc.call(
                "doc_query", {{"doc_id", prep->docId}, {"query", std::move(query)}},
                [this, &st, prep, destination, sid = std::move(sid)](
                    const core::RpcResult &queryResult) {
                  if (staticModalPrep_ != prep || !prepInFlight_)
                    return;
                  nlohmann::json matches = nlohmann::json::array();
                  if (queryResult.ok && queryResult.result.is_object() &&
                      !queryResult.result.contains("code") &&
                      simJsonBool(queryResult.result, "ok", true) &&
                      queryResult.result.contains("matches") &&
                      queryResult.result["matches"].is_array())
                    for (const auto &match : queryResult.result["matches"]) {
                      if (!match.is_object() || match.value("kind", std::string()) != "face")
                        continue;
                      if (!sid.empty()) {
                        const auto liveSid = match.find("sid");
                        if (liveSid == match.end() || !liveSid->is_string())
                          continue;
                        const auto &value = liveSid->get_ref<const std::string &>();
                        if (sid.size() != value.size() ||
                            !std::equal(sid.begin(), sid.end(), value.begin(),
                                        [](unsigned char a, unsigned char b) {
                                          return std::tolower(a) == std::tolower(b);
                                        }))
                          continue;
                      }
                      matches.push_back(match);
                    }
                  if (matches.empty()) {
                    failStaticModalPrep(
                        st, prep, "Simulation boundary query did not resolve any faces (" +
                                      simJsonText(queryResult.result, "message", "error",
                                                  queryResult.err.empty() ? "no matching face"
                                                                          : queryResult.err) +
                                      ")");
                    return;
                  }
                  if (matches.size() == 1) {
                    st.rpc.call(
                        "doc_face_points",
                        {{"doc_id", prep->docId}, {"selector", std::move(matches[0])}},
                        [this, &st, prep, destination](const core::RpcResult &faceResult) {
                          prepFaceResult(st, prep, faceResult, *destination);
                        },
                        30000);
                    return;
                  }
                  // The outer vectors stay fixed throughout preparation. Allocate all child
                  // slots before scheduling callbacks, then capture only the stable parent
                  // plus index: no pointer/reference can dangle as a query expands.
                  *destination = {{"facesets", nlohmann::json::array()}};
                  auto &sets = (*destination)["facesets"];
                  for (std::size_t i = 0; i < matches.size(); ++i)
                    sets.push_back(nullptr);
                  prep->facesRemaining += matches.size() - 1;
                  for (std::size_t i = 0; i < matches.size(); ++i)
                    st.rpc.call(
                        "doc_face_points",
                        {{"doc_id", prep->docId}, {"selector", std::move(matches[i])}},
                        [this, &st, prep, destination, i](const core::RpcResult &faceResult) {
                          if (staticModalPrep_ != prep || !prepInFlight_)
                            return;
                          prepFaceResult(st, prep, faceResult, (*destination)["facesets"][i]);
                        },
                        30000);
                },
                30000);
          };
          if (clean.is_object()) {
            if (clean.contains("sid") && clean["sid"].is_string() &&
                !clean.contains("of"))
              queryFaces({{"of", "face"}}, clean["sid"].get<std::string>());
            else
              queryFaces(std::move(clean));
          } else if (clean.is_string()) {
            // This method carries no document id and no native RPC prefix. Python only
            // parses the canonical grammar; doc_query/doc_face_points use our own engine.
            st.rpc.call(
                "worker_normalize_face_query", {{"query", std::move(clean)}},
                [this, &st, prep, queryFaces](const core::RpcResult &normalized) {
                  if (staticModalPrep_ != prep || !prepInFlight_)
                    return;
                  if (!normalized.ok || !normalized.result.is_object() ||
                      normalized.result.contains("code") ||
                      !normalized.result.contains("query") ||
                      !normalized.result["query"].is_object()) {
                    failStaticModalPrep(
                        st, prep, "Simulation boundary query could not be read (" +
                                      simJsonText(normalized.result, "message", "error",
                                                  normalized.err.empty() ? "invalid query"
                                                                         : normalized.err) +
                                      ")");
                    return;
                  }
                  queryFaces(normalized.result["query"],
                             normalized.result.value("sid", std::string()));
                },
                30000);
          } else {
            failStaticModalPrep(st, prep,
                                "Simulation boundaries need a face selector or query.");
          }
        };
        for (std::size_t i = 0; i < prep->fixtureSelectors.size(); ++i)
          queueFace(prep->fixtureSelectors[i], &prep->fixtures[i]);
        for (auto &load : prep->loads)
          for (std::size_t i = 0; i < load.selectors.size(); ++i)
            queueFace(load.selectors[i], &load.resolved[i]);
        for (std::size_t i = 0; i < prep->topoSelectors.size(); ++i)
          queueFace(prep->topoSelectors[i], &prep->topoResolved[i]);
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
  const bool topo = prep->type == StudyType::Topology;
  const bool cancelled = topo && message.rfind("Topology run cancelled", 0) == 0;
  if (topo)
    topoRunMeta_ = TopoRunMeta{};
  for (auto &study : studies_) {
    if (study.id != prep->studyId)
      continue;
    if (topo) {
      // Retained runs are untouched; only the transient status reports the refusal.
      study.hasResult = study.topo.contains("runs") && study.topo["runs"].is_array() &&
                        !study.topo["runs"].empty();
      study.topoStatus = message;
      study.topoStatusKind = cancelled ? 1 : 2;
      break;
    }
    study.hasResult = false;
    study.summary = message;
    persist(st, prep->docId, study);
    break;
  }
  st.addLog((topo ? "[topology] " : "[sim] ") + message);
  if (simOriginActive(st, this, prep->docId)) {
    st.status = message;
    st.statusKind = cancelled ? 1 : 2;
    st.toast(cancelled ? ui::ToastLevel::Info : ui::ToastLevel::Error, message);
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
  if (!isFlowFamily(prep->type) && prep->type != StudyType::Topology &&
      !nativeSourceCurrent(st, prep->docId, prep->sourceGeometryKey,
                           prep->sourceMutationGen)) {
    failStaticModalPrep(st, prep,
                        "Simulation stopped: the part changed during preparation. Run again.");
    return;
  }
  if (prep->type == StudyType::Topology) {
    // The design domain was exported at Run; refuse if the part changed while faces resolved
    // (the selections would describe other geometry than the exported STEP).
    if (st.docMirror.hasDoc() && st.docMirror.docId() == prep->docId &&
        !prep->topoGeometryKey.empty() && topologyGeometryKey(st) != prep->topoGeometryKey) {
      failStaticModalPrep(st, prep,
                          "Topology run stopped: the part changed during preparation. Run "
                          "again.");
      return;
    }
    std::map<std::string, nlohmann::json> resolved;
    for (std::size_t i = 0; i < prep->topoSelectors.size(); ++i)
      resolved[topology::selectorKey(prep->topoSelectors[i])] = prep->topoResolved[i];
    nlohmann::json envelope;
    std::string error;
    if (!topology::buildRunEnvelope(prep->topoStudy, resolved, prep->resolvedStepPath,
                                    prep->topoMaterial, envelope, error)) {
      failStaticModalPrep(st, prep, "Topology run refused: " + error);
      return;
    }
    // In-process engine: a per-run directory under the app data dir (unique run id; the
    // surface writer creates files exclusively). In daemon mode the server owns the path.
    if (!engine::daemonMode() && !topoRunMeta_.runId.empty())
      envelope["topology"]["result_dir"] =
          appdataAppDir() + "/topology/" + simPathSegment(prep->docId) + "_" +
          simPathSegment(prep->studyId) + "/" + simPathSegment(topoRunMeta_.runId);
    std::string note;
    const std::uint64_t token = prep->reservationToken;
    const bool launched = prep->sim->launch("sim_run_topology", {{"study", std::move(envelope)}},
                                            note, prep->docId, token);
    prep->reservationToken = 0; // a matching launch consumes the reservation either way
    if (!launched) {
      failStaticModalPrep(st, prep,
                          note.empty() ? std::string("Topology solver failed to start") : note);
      return;
    }
    prepInFlight_ = false;
    staticModalPrep_.reset();
    topoLiveIter_.clear();
    topoLiveObjective_.clear();
    topoLiveVolume_.clear();
    topoLiveChange_.clear();
    for (auto &study : studies_)
      if (study.id == prep->studyId) {
        study.topoStatus = "Optimizing\xe2\x80\xa6";
        study.topoStatusKind = 0;
      }
    st.addLog(note.empty() ? "[topology] run started" : "[topology] " + note);
    if (simOriginActive(st, this, prep->docId))
      st.toast(ui::ToastLevel::Info, "Topology optimization started");
    return;
  }
  if ((prep->type == StudyType::Static || prep->type == StudyType::Buckling) &&
      prep->fixtures.empty()) {
    failStaticModalPrep(st, prep, "Add a fixture before running");
    return;
  }

  nlohmann::json fixtures = nlohmann::json::array();
  for (std::size_t i = 0; i < prep->fixtures.size(); ++i)
    forEachBoundaryFaceset(prep->fixtures[i], [&](const nlohmann::json &resolved) {
      auto face = resolved;
      if (i < prep->fixtureSelectors.size())
        applyFacesetSettings(face, prep->fixtureSelectors[i]);
      if (i < prep->fixtureDefinitions.size())
        applyFacesetSettings(face, prep->fixtureDefinitions[i]);
      fixtures.push_back(std::move(face));
    });
  nlohmann::json study = {{"step_path", prep->resolvedStepPath},
                          {"material", prep->type == StudyType::ThermalFea
                                           ? prep->thermalMaterial : prep->material},
                          {"fixtures", std::move(fixtures)}};
  if (prep->type == StudyType::ThermalFea) {
    for (auto &face : study["fixtures"])
      if (!face.contains("temp_c"))
        face["temp_c"] = prep->thermFixTempC;
    if (!prep->thermalTransient.is_null())
      study["transient"] = prep->thermalTransient;
  }
  if (!isFlowFamily(prep->type))
    addMeshParams(study, prep->meshOptions,
                  (prep->type == StudyType::Static ||
                   prep->type == StudyType::Buckling ||
                   prep->type == StudyType::Fatigue)
                      ? prep->meshOptions.meshOrder : 1,
                  prep->type == StudyType::ThermalFea ||
                      supportsLearnedAdvisor(prep->type));
  if (!isFlowFamily(prep->type)) {
    const auto &record = prep->canonical;
    // GUI aliases supply defaults; explicitly authored native fields are authoritative.
    if (record.contains("material") && record["material"].is_object()) {
      if (record["material"].contains("per_body") &&
          prep->type != StudyType::Static && prep->type != StudyType::Fatigue) {
        failStaticModalPrep(st, prep,
                            "Per-body material assignments are supported by static/fatigue only.");
        return;
      }
      auto material = study["material"];
      material.update(record["material"]);
      study["material"] = std::move(material);
    }
    if (prep->type == StudyType::ThermalFea) {
      const auto authoredMaterial = record.find("material");
      for (const char *key : {"k_w_mk", "cp_j_kgk"})
        if (record.contains(key) &&
            (authoredMaterial == record.end() || !authoredMaterial->is_object() ||
             !authoredMaterial->contains(key)))
          study["material"][key] = record[key];
      if (authoredMaterial != record.end() && authoredMaterial->is_object() &&
          authoredMaterial->contains("conductivity_w_mk") &&
          !authoredMaterial->contains("k_w_mk"))
        study["material"]["k_w_mk"] = (*authoredMaterial)["conductivity_w_mk"];
      if (record.contains("conductivity_w_mk"))
        study["material"]["k_w_mk"] = record["conductivity_w_mk"];
      if (record.contains("specific_heat_j_kgk"))
        study["material"]["cp_j_kgk"] = record["specific_heat_j_kgk"];
      copyNativeKeys(study, record, {"transient", "k_w_mk"});
    }
    copyNativeKeys(study, record,
                   {"fineness", "mesh_order", "order", "target_size_mm", "min_size_mm",
                    "max_size_mm", "learned_advisor", "advisor_max_dof", "adapt",
                    "interfaces", "bond_gap_mm", "contact"});
    // Canonical mesh aliases are used only when the native key was not explicitly authored.
    for (const auto &alias : {std::pair<const char *, const char *>{"mesh_fineness", "fineness"},
                              {"mesh_target_mm", "target_size_mm"},
                              {"mesh_min_mm", "min_size_mm"}})
      if (!record.contains(alias.second) && record.contains(alias.first) &&
          record[alias.first].is_number() && record[alias.first].get<double>() > 0.0)
        study[alias.second] = record[alias.first];
    if (!record.contains("mesh_order") && record.contains("order"))
      study["mesh_order"] = record["order"];
  }

  std::string note;
  bool launched = false;
  const std::uint64_t token = prep->reservationToken;
  if (prep->type == StudyType::Static || prep->type == StudyType::Buckling ||
      prep->type == StudyType::Fatigue || prep->type == StudyType::ThermalFea) {
    nlohmann::json loads = nlohmann::json::array();
    for (const auto &load : prep->loads) {
      nlohmann::json facesets = nlohmann::json::array();
      const auto &row = load.definition;
      for (std::size_t i = 0; i < load.resolved.size(); ++i)
        forEachBoundaryFaceset(load.resolved[i], [&](const nlohmann::json &resolved) {
          auto face = resolved;
          if (i < load.selectors.size())
            applyFacesetSettings(face, load.selectors[i]);
          if (row.contains("faces")) {
            const auto &authored = row["faces"];
            if (authored.is_array() && i < authored.size())
              applyFacesetSettings(face, authored[i]);
            else if (authored.is_object())
              applyFacesetSettings(face, authored);
          }
          facesets.push_back(std::move(face));
        });
      if (prep->type == StudyType::ThermalFea && facesets.empty()) {
        failStaticModalPrep(st, prep,
                            "Every thermal load needs at least one resolved face");
        return;
      }
      nlohmann::json points = facesets.size() == 1
                                  ? std::move(facesets[0])
                                  : nlohmann::json{{"facesets", std::move(facesets)}};
      nlohmann::json item = {{"faces", std::move(points)}};
      if (prep->type == StudyType::ThermalFea) {
        const bool convection =
            row.value("type", std::string()) == "convection" ||
            (row.value("type", std::string()) != "flux" &&
             row.value("thermal_kind", 0) == 1);
        item["type"] = convection ? "convection" : "flux";
        if (convection) {
          item["h"] = row.contains("h") ? row["h"] : row.value("h_w_m2k", nlohmann::json(10.0));
          item["t_amb_c"] = row.contains("t_amb_c")
                                  ? row["t_amb_c"]
                                  : row.value("ambient_c", nlohmann::json(25.0));
        } else {
          item["magnitude"] = row.value("type", std::string()) == "flux" &&
                                      row.contains("magnitude")
                                  ? row["magnitude"] : row.value("flux_w_m2", nlohmann::json(1000.0));
          copyNativeKeys(item, row, {"schedule"});
        }
      } else {
        const bool pressure = row.value("type", std::string()) == "pressure" ||
                              row.contains("pressure");
        item["type"] = pressure ? "pressure" : "force";
        if (pressure)
          item["magnitude"] = row.contains("magnitude")
                                  ? row["magnitude"] : row.value("pressure", nlohmann::json(0.1));
        else
          item["vector"] = row.contains("vector") ? row["vector"]
                              : row.contains("force") ? row["force"]
                                                      : row.value("vec", nlohmann::json::array());
      }
      loads.push_back(std::move(item));
    }
    study["loads"] = std::move(loads);
    if (prep->type == StudyType::Fatigue) {
      study["fatigue"] = prep->fatigue;
      if (prep->canonical.contains("fatigue")) {
        const auto &fatigue = prep->canonical["fatigue"];
        copyNativeKeys(study["fatigue"], fatigue,
                       {"lmin", "lmax", "load_ratio", "se_corrected_mpa", "se_base_mpa",
                        "ultimate_mpa", "yield_mpa"});
        if (fatigue.contains("load_type") && fatigue["load_type"].is_string()) {
          study["fatigue"]["load_type"] = fatigue["load_type"];
          if (!fatigue.contains("lmin") && !fatigue.contains("lmax")) {
            study["fatigue"].erase("lmin");
            study["fatigue"].erase("lmax");
          }
        }
      }
    }
    const char *method = prep->type == StudyType::Buckling
                             ? "sim_run_buckling"
                             : prep->type == StudyType::Fatigue
                                   ? "sim_run_fatigue"
                                   : prep->type == StudyType::ThermalFea
                                         ? "sim_run_thermal"
                                         : (study.contains("adapt") && !study["adapt"].is_null()
                                                ? "sim_run_adaptive" : "sim_run_static");
    nlohmann::json params{{"study", std::move(study)}};
    if (prep->type == StudyType::Buckling) {
      params["n_modes"] = prep->canonical.value("n_modes", nlohmann::json(prep->nModes));
      copyNativeKeys(params, prep->canonical, {"max_load_factor"});
    }
    launched = prep->sim->launch(method, std::move(params), note, prep->docId, token);
  } else if (isFlowFamily(prep->type)) {
    prep->flowParams["stl_path"] = prep->resolvedStepPath;
    launched = prep->sim->launch("sim_run_flow", std::move(prep->flowParams),
                                note, prep->docId, token);
  } else {
    if (prep->type == StudyType::FreqResponse)
      prep->resolvedMaterial = study["material"];
    launched = prep->sim->launch(
        "sim_run_modal",
        {{"study", std::move(study)},
         {"n_modes", prep->canonical.value("n_modes", nlohmann::json(prep->nModes))}},
        note, prep->docId, token);
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
  if (isFlowFamily(prep->type))
    staticModalPrep_.reset();
  st.addLog(note.empty() ? "[sim] simulation started" : note);
  if (simOriginActive(st, this, prep->docId))
    st.toast(ui::ToastLevel::Info,
             note.empty() ? "Simulation started" : note);
}

void SimManager::tick(AppState &st) {
  appSt_ = &st;
  syncStudies(st); // material keys + setup normalisation, kept off the draw path
  if (sel_ >= 0 && sel_ < (int)studies_.size() && isFlowFamily(studies_[sel_].type))
    syncFlowStudy(st, studies_[sel_]);
  syncResultView(st); // keep the viewport in step with the result/model view
                      // (every frame, cheap)
  tickMotion(st);     // advance motion playback frame if playing
  // Footer run clock: one start time per run of this manager (prep, solver job, worker call or
  // motion sample), whichever path launched it.
  {
    const bool active = running_ || advRunning_ || motionRun_ != nullptr;
    if (active && !runWasActive_) {
      runStartTime_ = ImGui::GetTime();
      runPct_ = -1;
      runStageText_.clear();
    }
    if (!active)
      runStartTime_ = -1.0;
    runWasActive_ = active;
  }
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
  // Probe the multiphysics capability doctor (ccx / handoff seams) +
  // fw_status the same way — once, async, only while the Simulate workspace is
  // up.
  if (capsStatus_ == 0 && !capsInflight_ &&
      st.activeWorkspace == AppState::Workspace::Simulate)
    probeCaps(st);
  if (fwStatus_ == 0 && !fwProbeInflight_ &&
      st.activeWorkspace == AppState::Workspace::Simulate)
    probeFwStatus(st);
  drainSimClientJob(st);

  // ---- topology: capability probe, deferred panel actions (file reads / overlay loads /
  // exports stay off the draw path), artifact checks and the live convergence copy.
  const bool simWs = st.activeWorkspace == AppState::Workspace::Simulate;
  bool anyTopology = false;
  for (const auto &study : studies_)
    anyTopology = anyTopology || study.type == StudyType::Topology;
  if (!topoCapsRequested_ && (simWs || anyTopology))
    probeTopologyCaps(st);
  processTopologyActions(st);
  if (sel_ >= 0 && sel_ < (int)studies_.size() &&
      studies_[sel_].type == StudyType::Topology) {
    Study &ts = studies_[sel_];
    syncTopologyViews(ts);
    refreshTopologyRunsView(st, ts);
    const double now = ImGui::GetTime();
    if (topoSetupKeyStudy_ != ts.id || now - topoSetupKeyTime_ > 0.5) {
      topoSetupKeyNow_ = topology::setupKey(studyToJson(ts));
      topoSetupKeyStudy_ = ts.id;
      topoSetupKeyTime_ = now;
    }
    // Entering Results shows the selected (else latest) run's overlay once, silently when
    // that run has no usable overlay (the panel says why).
    const nlohmann::json &runs = ts.topoRunsView;
    if (simWs && node_ == SimNode::Results && ts.overlayPath.empty() && runs.is_array() &&
        !runs.empty()) {
      const std::string runId = topoResultsView_.selectedRunId.empty()
                                    ? topoRunIdOf(runs.back(), runs.size() - 1)
                                    : topoResultsView_.selectedRunId;
      const std::string key = ts.id + "|" + runId;
      if (topoAutoShowKey_ != key) {
        topoAutoShowKey_ = key;
        const nlohmann::json *run = topoFindRun(runs, runId);
        if (run && topoOverlayArtifact(*run).is_object())
          showTopologyRun(st, ts, runId);
      }
    }
  }
  if (st.simClient && running_ && !topoRunMeta_.runId.empty()) {
    const std::uint64_t rev = st.simClient->iterationRevision();
    if (rev != topoIterRevision_) {
      topoIterRevision_ = rev;
      topoLiveIter_.clear();
      topoLiveObjective_.clear();
      topoLiveVolume_.clear();
      topoLiveChange_.clear();
      topoLiveMaxIter_ = 0;
      for (const auto &it : st.simClient->iterationHistory()) {
        topoLiveMaxIter_ = it.maxIterations;
        topoLiveIter_.push_back(it.index);
        // Filter once per history revision, not into a temporary chart buffer every frame.
        if (std::isfinite(it.objective))
          topoLiveObjective_.push_back(it.objective);
        if (std::isfinite(it.volumeFraction))
          topoLiveVolume_.push_back(it.volumeFraction);
        if (std::isfinite(it.designChange))
          topoLiveChange_.push_back(it.designChange);
      }
    }
  }
  syncPaneContext(st); // ribbon study context + study-scoped status line
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
  // The solver job owns the phase now (job chip + footer); drop the prep notice.
  if (uiActive && !prepInFlight_ && job.running.load() && st.status == kSimPreparingStatus)
    st.status.clear();

  {
    std::vector<core::SimProgress> evs;
    {
      std::lock_guard<std::mutex> lk(job.evMtx);
      if (!job.events.empty())
        evs.swap(job.events);
    }
    for (auto &ev : evs) {
      // Footer progress (runUi): the latest percentage and a readable stage line. A native event
      // carries its own fraction (iteration or meshing cells) or none — never the previous
      // stage's percentage.
      if (ev.pct > 0) {
        runPct_ = ev.pct;
      } else if (ev.elapsedS >= 0.0) {
        const float frac = simStageFraction(ev.stage, ev.text);
        runPct_ = frac >= 0.0f ? (int)(frac * 100.0f) : -1;
      }
      {
        std::string stage = stageDisplay(ev);
        if (!stage.empty())
          runStageText_ = std::move(stage);
      }
      // The pane footer and the status bar's job chip show the stage; the left status line is
      // not a third copy: the start notice clears once progress flows.
      if (uiActive && st.status == kSimPreparingStatus)
        st.status.clear();
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
    std::lock_guard<std::mutex> lk(job.mtx);
    if (job.method == "sim_run_pipe_flow" && piping_.flowJobActive()) {
      // The Piping panel's internal-flow run: no FEA study is involved.
      piping_.finishFlowJob(st, job.ok, job.cancelled, job.result, job.error,
                            job.ownerDocId.empty() ? lastDocId_ : job.ownerDocId);
      if (uiActive) {
        st.status = job.ok ? "Internal flow complete"
                           : (job.cancelled ? "Internal flow cancelled" : "Internal flow failed");
        st.statusKind = job.ok ? 0 : (job.cancelled ? 1 : 2);
      }
      return;
    }
    running_ = false;
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
    const auto completedPrep = staticModalPrep_;
    if (completedPrep && completedPrep->docId == ownerDocId &&
        completedPrep->studyId == s.id) {
      if (s.type != StudyType::FreqResponse || !job.ok)
        staticModalPrep_.reset();
      if (!nativeSourceCurrent(st, ownerDocId, completedPrep->sourceGeometryKey,
                               completedPrep->sourceMutationGen) ||
          nativeSetup(studyToJson(s)) != nativeSetup(completedPrep->canonical)) {
        staticModalPrep_.reset();
        clearTransientResult(s);
        s.summary = "Run discarded: the part or study setup changed. Run again.";
        persist(st, ownerDocId, s);
        st.addLog("[sim] " + s.summary);
        return;
      }
    }
    if (s.type == StudyType::Topology) {
      // Completed, refused, infeasible, iteration-limited and cancelled native results are all
      // retained as runs (job.result carries the partial/diagnostic object on !ok).
      finishTopologyRun(st, s, ownerDocId, uiActive);
      return;
    }
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
      if (isThermal)
        s.summary = "Surface " + kit::fmtQuantity(s.cpMin, "\xc2\xb0" "C", 3) + " to " +
                    kit::fmtQuantity(s.cpMax, "\xc2\xb0" "C", 3) + " \xc2\xb7 inlet " +
                    kit::fmtQuantity(s.flowInletTempC, "\xc2\xb0" "C", 3);
      else
        s.summary = "Drag " + kit::fmtQuantity(s.dragN, "N", 3) + " \xc2\xb7 lift " +
                    kit::fmtQuantity(s.liftN, "N", 3) + " \xc2\xb7 Cd " +
                    kit::fmtValue(s.cd, 3) + " \xc2\xb7 Cl " + kit::fmtValue(s.cl, 3);
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
            "The solver did not return a display field. Numerical forces and "
            "coefficients are retained; pressure coloring, streamlines and "
            "velocity slices are unavailable for this run.");
        if (uiActive)
          restoreModelMesh(st);
        resultShown_ = false;
        shownStudyId_.clear();
      } else if (uiActive) {
        st.activeScalarField = isThermal ? "temperature" : "pressure_cp";
        flowShowStreamlines_ = true;
        flowShowSlices_ = false;
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
    } else if (s.type == StudyType::Buckling) {
      double factor = 0.0, critical = 0.0;
      if (!finiteNumber(res, "critical_load_factor", factor) || factor <= 0.0 ||
          !finiteNumber(res, "critical_load_n", critical) || critical <= 0.0 ||
          !res.contains("modes") || !res["modes"].is_array() || res["modes"].empty()) {
        rejectResult("buckling solver returned no finite positive mode");
        return;
      }
      s.nNodes = res.value("n_nodes", 0);
      s.nElems = res.value("n_elems", 0);
      s.nDof = res.value("n_dof", 0);
      s.advResult = nlohmann::json::object();
      for (const char *key : {"modes", "critical_load_factor", "critical_load_n",
                              "reference_load_n", "mesh_quality", "provenance",
                              "vtu_path"})
        if (res.contains(key))
          s.advResult[key] = res[key];
      s.summary = "Lowest load factor " + kit::fmtValue(factor, 3) + " \xc2\xb7 critical load " +
                  kit::fmtQuantityScaled(critical, "N", 4);
      s.hasResult = true;
      st.addLog("[sim] buckling done: " + s.summary);
    } else if (s.type == StudyType::ThermalFea) {
      // Conduction solve (steady or transient): numeric summary only —
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
      copyNativeKeys(adv, res, {"frames", "envelope_max", "provenance", "mesh_quality",
                                 "input_hash", "cache_hit", "solver_notes"});
      s.advResult = adv;
      const char *degC = "\xc2\xb0" "C";
      if (transient)
        s.summary = "Peak " + kit::fmtQuantity(maxTemp, degC, 4) + " at " +
                    kit::fmtQuantity(maxTime, "s", 3) + " \xc2\xb7 final max " +
                    kit::fmtQuantity(finalMax, degC, 4) + " \xc2\xb7 min " +
                    kit::fmtQuantity(minTemp, degC, 4);
      else
        s.summary = "Max " + kit::fmtQuantity(maxTemp, degC, 4) + " \xc2\xb7 min " +
                    kit::fmtQuantity(minTemp, degC, 4);
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
        std::string f;
        for (auto &v : res["freqs_hz"]) {
          s.freqs.push_back((float)v.get<double>());
          if (s.freqs.size() <= 4)
            f += (f.empty() ? "" : ", ") + kit::fmtValue(v.get<double>(), 4);
        }
        if (s.freqs.size() > 4)
          f += ", \xe2\x80\xa6";
        s.summary = s.freqs.empty() ? std::string("No modes found")
                                    : std::to_string(s.freqs.size()) +
                                          (s.freqs.size() == 1 ? " mode at " : " modes at ") + f +
                                          " Hz";
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
        s.summary = std::string(kTheoryNames[m]) + " \xc2\xb7 FoS " +
                    (fos >= 15.0 - 1e-6 ? std::string("15+") : kit::fmtValue(fos, 3)) +
                    " \xc2\xb7 " +
                    (life >= 1e6 ? std::string("infinite life (over 1,000,000 cycles)")
                                 : "life " + kit::fmtValue(life, 3) + " cycles");
      } else if (!s.resultSummary.empty()) {
        const auto &sm = s.resultSummary;
        s.summary = "Max von Mises " +
                    kit::fmtQuantity(sm.value("max_von_mises_mpa", 0.0), "MPa", 3) +
                    " \xc2\xb7 max displacement " +
                    kit::fmtQuantity(sm.value("max_disp_mm", 0.0), "mm", 3) +
                    " \xc2\xb7 min FoS " +
                    (sm.value("min_fos", 0.0) >= 15.0 - 1e-6
                         ? std::string("15+")
                         : kit::fmtValue(sm.value("min_fos", 0.0), 3));
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
      case StudyType::Buckling:
        st.status = "Linear buckling complete";
        break;
      default:
        break;
      }
      if (s.type == StudyType::Static || s.type == StudyType::Modal ||
          s.type == StudyType::Fatigue || s.type == StudyType::ThermalFea ||
          s.type == StudyType::Buckling)
        st.statusKind = adaptUnproven ? 1 : 0;
    }
    // Cache the summary into the document so it reopens with the last-run
    // headline.
    persist(st, ownerDocId, s);
    node_ = SimNode::Results;
  }
}

// ---------------------------------------------------------------- thermal FEA
// run (conduction)

void SimManager::runThermalFea(AppState &st, Study &s) {
  if (!st.simClient || !st.docMirror.hasDoc())
    return;
  if (s.fixtures.picks.empty() && s.loads.empty()) {
    st.toast(ui::ToastLevel::Warn,
             "Add a thermal boundary condition before running");
    return;
  }
  const int mi = materialIndexByKey(s.materialKey);
  auto prep = std::make_shared<StaticModalPrep>();
  prep->type = s.type;
  prep->thermalMaterial = {{"k_w_mk", s.thermK}, {"cp_j_kgk", s.thermCp}};
  if (mi >= 0)
    prep->thermalMaterial["density_g_cm3"] = materials()[mi].density_g_cm3;
  prep->thermFixTempC = s.thermFixTempC;
  if (s.thermTransient && s.thermTEndS > 0) {
    prep->thermalTransient = {{"t_end_s", s.thermTEndS},
                              {"t_init_c", s.thermTInitC}};
    if (s.thermDtS > 0)
      prep->thermalTransient["dt_s"] = s.thermDtS;
  }
  beginExportPrep(st, s, prep);
}

// ---------------------------------------------------------------- ccx proxy
// runs (worker seam)


void SimManager::runContact(AppState &st, Study &s) {
  float lo[3], hi[3];
  if (!modelBbox(st, lo, hi)) {
    st.toast(ui::ToastLevel::Warn,
             "No model geometry to size the contact proxy from");
    return;
  }
  const auto canonical = studyToJson(s);
  const int mi = materialIndexByKey(s.materialKey);
  nlohmann::json params = {{"L_mm", std::max(1.0f, hi[0] - lo[0])},
                           {"b_mm", std::max(1.0f, hi[1] - lo[1])},
                           {"h_mm", std::max(1.0f, hi[2] - lo[2])},
                           {"pressure_mpa", s.contactPressureMpa}};
  if (mi >= 0) {
    params["E_mpa"] = materials()[mi].E_mpa;
    params["nu"] = materials()[mi].nu;
  }
  if (canonical.contains("material") && canonical["material"].is_object())
    copyNativeKeys(params, canonical["material"], {"E_mpa", "nu"});
  copyNativeKeys(params, canonical, {"L_mm", "b_mm", "h_mm", "E_mpa", "nu", "pressure_mpa"});
  const std::string sourceGeometryKey = topologyGeometryKey(st);
  const auto sourceMutationGen = st.docMirror.mutationGen();
  clearTransientResult(s);
  persist(st, s);
  resultShown_ = false;
  shownStudyId_.clear();
  advRunning_ = true;
  advRunningStudyId_ = s.id;
  advStage_ = "Solving contact (CalculiX)";
  s.summary = "Solving (CalculiX)\xe2\x80\xa6";
  const std::string sid = s.id;
  const std::string docId = st.docMirror.docId();
  st.rpc.call(
      "sim_run_contact", params,
      [this, &st, sid, docId, canonical, sourceGeometryKey, sourceMutationGen](const core::RpcResult &r) {
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
          if (!nativeSourceCurrent(st, docId, sourceGeometryKey, sourceMutationGen) ||
              nativeSetup(studyToJson(stu)) != nativeSetup(canonical)) {
            fail("Contact result discarded: the part or study setup changed. Run again.");
            return;
          }
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
          stu.summary = "Applied " + kit::fmtQuantityScaled(applied, "N", 4) +
                        " \xc2\xb7 base reaction " + kit::fmtQuantityScaled(reaction, "N", 4);
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
  const auto prep = staticModalPrep_;
  auto failNow = [&](std::string message) {
    if (staticModalPrep_ == prep)
      staticModalPrep_.reset();
    s.summary = std::move(message);
    persist(st, docId, s);
    st.addLog("[sim] " + s.summary);
    if (simOriginActive(st, this, docId)) {
      st.status = s.summary;
      st.statusKind = 2;
      st.toast(ui::ToastLevel::Error, s.summary);
    }
  };
  if (!prep || prep->docId != docId || prep->studyId != s.id ||
      !nativeSourceCurrent(st, docId, prep->sourceGeometryKey, prep->sourceMutationGen) ||
      nativeSetup(studyToJson(s)) != nativeSetup(prep->canonical)) {
    failNow("Vibration result discarded: the part or study setup changed. Run again.");
    return;
  }
  const auto &record = prep->canonical;
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
  double dens = 0.0;
  finiteNumber(prep->resolvedMaterial, "density_g_cm3", dens);
  if (!record.contains("total_mass_kg") && dens > 0 && !docId.empty()) {
    nlohmann::json r,
        p = {{"doc_id", docId}, {"density_g_cm3", dens}};
    std::string e;
    double massG = 0.0;
    if (st.worker.call("eval_mass", p, r, e, 15000) && r.is_object() &&
        !r.contains("code") && r.value("ok", true) &&
        finiteNumber(r, "mass_g", massG))
      massKg = massG / 1000.0;
  }
  if (!record.contains("total_mass_kg") && massKg <= 0.0) {
    failNow("Vibration: part mass unknown (set a material with density)");
    return;
  }
  double dir[3] = {0, 0, 0};
  int direction = s.frDir;
  double zeta = s.frZeta, baseG = s.frBaseG;
  if (record.contains("adv") && record["adv"].is_object()) {
    const auto &adv = record["adv"];
    if (adv.contains("fr_dir") && adv["fr_dir"].is_number_integer())
      direction = adv["fr_dir"].get<int>();
    finiteNumber(adv, "fr_zeta", zeta);
    finiteNumber(adv, "fr_base_g", baseG);
  }
  dir[direction < 0 || direction > 2 ? 2 : direction] = 1.0;
  nlohmann::json params = {
      {"freqs_hz", modalRes["freqs_hz"]},
      {"mode_shapes", modalRes["mode_shapes"]},
      {"n_nodes", modalRes.value("n_nodes", 0)},
      {"total_mass_kg", massKg},
      {"excitation_dir", {dir[0], dir[1], dir[2]}},
      {"zeta", zeta},
      {"base_accel_g", baseG},
  };
  copyNativeKeys(params, record,
                 {"total_mass_kg", "excitation_dir", "zeta", "base_accel_g",
                  "sweep_hz", "psd_breakpoints", "vibration_spec"});
  advRunning_ = true;
  advRunningStudyId_ = s.id;
  advStage_ = "Post-processing (modal superposition)";
  s.summary = "Post-processing (modal superposition)\xe2\x80\xa6";
  const std::string sid = s.id;
  nlohmann::json modalEvidence = nlohmann::json::object();
  copyNativeKeys(modalEvidence, modalRes, {"provenance", "mesh_quality", "input_hash"});
  st.rpc.call(
      "sim_freq_response", params,
      [this, &st, sid, docId, prep, modalEvidence = std::move(modalEvidence)](const core::RpcResult &r) {
        advRunning_ = false;
        if (staticModalPrep_ == prep)
          staticModalPrep_.reset();
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
          if (!nativeSourceCurrent(st, docId, prep->sourceGeometryKey, prep->sourceMutationGen) ||
              nativeSetup(studyToJson(stu)) != nativeSetup(prep->canonical)) {
            fail("Vibration result discarded: the part or study setup changed. Run again.");
            return;
          }
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
          stu.advResult["modal_evidence"] = modalEvidence;
          stu.advisor = parseAdvisor(r.result);
          stu.summary = "Peak " + kit::fmtValue(peakAccel, 3) + " g at " +
                        kit::fmtQuantity(peakFreq, "Hz", 4) + " \xc2\xb7 " +
                        simJsonText(r.result, "damping_assumption", "damping_assumption", "assumed damping");
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

} // namespace ui
