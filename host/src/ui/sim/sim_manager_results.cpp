// SimManager results: result overlays, view sync/restore, result controls, advisor, provenance.
#include "sim_manager_internal.h"

#include <filesystem>

#include "../../renderer.h"
#include "util/aicm_handoff.h" // is_shm_uri — overlay paths are aicm-shm:// handoff URIs, not files

namespace ui {

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

namespace {

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
} // namespace

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
  st.loadDocMeshAsync(s.overlayPath, /*renderOnly=*/false, /*cacheAsModel=*/false);
#else
  std::string err;
  // Full scalar/displacement load, but never replace the editable model's CPU/GPU cache.
  if (!st.loadDocMesh(s.overlayPath, err, /*renderOnly=*/false, /*cacheAsModel=*/false)) {
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
  if (s.type == StudyType::Topology && !s.topoFieldLabel.empty()) {
    // Topology overlays name their own chunks and units (run artifact descriptors).
    label = s.topoFieldLabel;
    unit = s.topoFieldUnit;
    fromSI = s.topoFieldFromSI;
  }
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
}

std::string SimManager::advisorStatusLine() const {
  if (sel_ < 0 || sel_ >= (int)studies_.size())
    return {};
  const Study &s = studies_[sel_];
  const SimAdvisor &a = s.advisor;
  if (!a.present)
    return {};
  std::string out = "Setup advisor ";
  if (a.budgetRefusal)
    out += "declined (over budget)";
  else if (a.vetoed)
    out += "overruled";
  else if (a.applied)
    out += "applied";
  else if (a.available)
    out += "available, not applied";
  else
    out += "unavailable";
  // Chudware's solve path is fixed-stride tet-only, so a hybrid/VEM family
  // recommendation is reported as advice rather than claimed as executed.
  if (!a.mesher.empty() && advisorMesherIsPolyhedral(a.mesher))
    out += " \xc2\xb7 polyhedral mesh advice not used (tetrahedra only)";
  if (SimAdvisor::has(a.oodDistance) && a.oodDistance > 1.0)
    out += " \xc2\xb7 unusual model for the advisor";
  return out;
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
  // Close the logical result view; restoreModelMesh also restores editable geometry/picking.
  // Document switches install the entering model themselves. Keep the per-document motion
  // base cache so a later scrub cannot upload an empty assembly.
  resultShown_ = false;
  shownStudyId_.clear();
}

void SimManager::restoreModelMesh(AppState &st) {
  restoreMotionHighlight(st);
  clearResultOverlay(st);
  if (!st.docMirror.hasDoc())
    return;
  st.clearSelection();
  const auto generation = ++modelRestoreGeneration_;
  const std::string docId = st.docMirror.docId();
  const int revision = st.docMirror.revision();
  auto *session = st.activeSession();
  // Restore picking and geometry together. A result mesh has no editable B-rep face IDs.
  if (session && session->cachedRevision == revision &&
      session->cachedMirror.docId == docId && session->cachedDocMesh) {
    std::string err;
    if (st.bindCachedDocMesh(*session, err))
      return;
  }
  st.rpc.call("doc_activate", {{"doc_id", docId}},
              [this, &st, docId, revision, generation](const core::RpcResult &r) {
    if (generation != modelRestoreGeneration_ || !simOriginActive(st, this, docId) ||
        st.docMirror.revision() != revision || resultShown_)
      return;
    if (!r.ok || !r.result.is_object() || r.result.contains("code")) {
      st.addLog("[sim] restore model mesh failed: " +
                simJsonText(r.result, "message", "error", r.err));
      return;
    }
    const bool previous = st.suppressTabDirty;
    st.suppressTabDirty = true;
    std::string aicm, err;
    const bool adopted = st.docMirror.adoptActivateResponse(docId, r.result, aicm, err);
    st.suppressTabDirty = previous;
    if (adopted && !aicm.empty())
      st.loadDocMeshAsync(aicm);
    else
      st.addLog("[sim] restore model mesh failed: " + err);
  }, 60000);
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
  if (motionHighlightPose_ &&
      (!valid || studies_[sel_].id != motionHighlightStudyId_ ||
       !onSimWs || node_ != SimNode::Results))
    restoreMotionHighlight(st);
  if (valid && studies_[sel_].type == StudyType::Motion &&
      studies_[sel_].motionHasResult &&
      studies_[sel_].motionSourceRevision != st.docMirror.revision()) {
    auto &motion = studies_[sel_];
    if (!sameMotionSource(motion.motionSource, st.docMirror.tree())) {
      invalidateMotion(motion);
      motion.summary = "Assembly changed. Run Motion again before playing or checking clearance.";
    }
    motion.motionSourceRevision = st.docMirror.revision();
  }

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

namespace {

// Collapsible kit card whose open state lives in the enclosing window's ImGui storage (collapsed
// by default unless `defaultOpen`), so a disclosure needs no SimManager member. Always pair with
// kit::cardEnd().
bool disclosureCardBegin(const char *id, const char *title,
                         const char *icon = nullptr, bool defaultOpen = false) {
  ImGuiStorage *store = ImGui::GetStateStorage();
  ImGui::PushID(id);
  const ImGuiID key = ImGui::GetID("disclosure_open");
  ImGui::PopID();
  bool open = store->GetBool(key, defaultOpen);
  const bool body = kit::cardBegin(id, title, icon, &open);
  store->SetBool(key, open);
  return body;
}

// One chip of the headline outcome card: a plain status chip (empty value) or a key/value chip.
struct OutcomeChip {
  std::string label;
  std::string value;
  kit::Tone tone = kit::Tone::Neutral;
  const char *tip = nullptr;
};

// Results headline (standard §5): status glyph + chips on ONE wrapping line + a one-line verdict.
void drawOutcomeCard(kit::Status status, const std::vector<OutcomeChip> &chips,
                     const std::string &verdict) {
  if (kit::cardBegin("outcome")) {
    kit::chipFlowBegin("outcome_chips");
    kit::chipFlowItem(ImGui::GetTextLineHeight());
    kit::statusGlyphInline(status);
    for (const OutcomeChip &c : chips) {
      if (c.value.empty())
        kit::flowChip(c.label.c_str(), c.tone, c.tone != kit::Tone::Neutral,
                      c.tip);
      else
        kit::flowValueChip(c.label.c_str(), c.value.c_str(), c.tone, c.tip);
    }
    kit::chipFlowEnd();
    if (!verdict.empty())
      ImGui::TextWrapped("%s", verdict.c_str());
  }
  kit::cardEnd();
}

void addElementsChip(std::vector<OutcomeChip> &chips, const Study &s) {
  if (s.nElems > 0)
    chips.push_back({groupedCount((uint64_t)s.nElems) + " elements", "",
                     kit::Tone::Neutral, "Elements in the solved mesh."});
}

// Collapsed "Mesh & solver" disclosure: what the solver actually ran (transparency).
void drawMeshSolverCard(const Study &s) {
  if (s.nNodes <= 0 && s.nElems <= 0 && s.nDof <= 0 && s.solverNotes.empty())
    return;
  if (disclosureCardBegin("mesh_solver", "Mesh & solver", "mesh")) {
    if (s.nNodes > 0)
      kit::keyValueRowId("nodes", "Nodes",
                         groupedCount((uint64_t)s.nNodes).c_str());
    if (s.nElems > 0)
      kit::keyValueRowId("elements", "Elements",
                         groupedCount((uint64_t)s.nElems).c_str());
    if (s.nDof > 0)
      kit::keyValueRowId("dof", "Equations (DOF)",
                         groupedCount((uint64_t)s.nDof).c_str());
    for (const auto &n : s.solverNotes)
      kit::dimText(n.c_str());
  }
  kit::cardEnd();
}

std::string advisorMs(double ms) {
  return SimAdvisor::has(ms) ? kit::fmtDuration(ms / 1000.0) : std::string("n/a");
}

// ---- optional core-advisor disclosure
// Draws nothing when the result carried no advisor object. Recommendations that do not fit
// Chudware's fixed-stride tet solve contract are reported as advice, never as executed setup.
void drawAdvisorDisclosure(const Study &s) {
  const SimAdvisor &a = s.advisor;
  if (!a.present)
    return;
  if (disclosureCardBegin("advisor", "Setup advisor", "mesh_auto")) {
    const char *state = a.budgetRefusal ? "Refused: predicted cost over budget"
                        : a.vetoed      ? "Vetoed: rejected by a guard"
                        : a.applied     ? "Applied to this run's setup"
                        : a.available   ? "Available, not applied"
                                        : "No recommendation produced";
    const bool warn = a.budgetRefusal || a.vetoed || !a.available;
    kit::keyValueRowId("state", "Status", state,
                       warn ? kit::Tone::Warn : kit::Tone::Ok);
    kit::itemTooltip(
        "The advisor only proposes mesh sizing, element order and adaptation. "
        "It never chooses or performs the solve, and this card never reports "
        "solver results.");
    if (!a.mesher.empty()) {
      kit::keyValueRowId("mesher", "Recommended mesher", a.mesher.c_str());
      if (advisorMesherIsPolyhedral(a.mesher))
        kit::inlineStatus("Chudware solves fixed-stride tetrahedral meshes; "
                          "this hybrid/VEM recommendation was not executed.",
                          kit::Tone::Warn);
    }
    if (SimAdvisor::has(a.hRel))
      kit::keyValueRowId("hrel", "Relative element size",
                         kit::fmtValue(a.hRel, 3).c_str());
    if (a.order > 0)
      kit::keyValueRowId("order", "Element order",
                         a.order == 1   ? "Linear"
                         : a.order == 2 ? "Quadratic"
                                        : std::to_string(a.order).c_str());
    if (a.adaptPasses >= 0)
      kit::keyValueRowId("passes", "Adaptive passes",
                         std::to_string(a.adaptPasses).c_str());
    if (SimAdvisor::has(a.etaTarget))
      kit::keyValueRowId("eta", "Error target",
                         kit::fmtValue(a.etaTarget, 3).c_str());
    if (SimAdvisor::has(a.predictedDof))
      kit::keyValueRowId(
          "dof", "Predicted DOF",
          groupedCount((uint64_t)std::max(0.0, a.predictedDof)).c_str());
    if (SimAdvisor::has(a.predictedMeshMs))
      kit::keyValueRowId("mesh_t", "Predicted mesh time",
                         advisorMs(a.predictedMeshMs).c_str());
    if (SimAdvisor::has(a.predictedSolveMs))
      kit::keyValueRowId("solve_t", "Predicted solve time",
                         advisorMs(a.predictedSolveMs).c_str());
    if (SimAdvisor::has(a.failureProb))
      kit::keyValueRowId(
          "fail", "Predicted failure probability",
          kit::fmtQuantity(a.failureProb * 100.0,
                           kit::unitText(kit::Unit::Percent), 3)
              .c_str(),
          a.failureProb >= 0.5 ? kit::Tone::Warn : kit::Tone::Neutral);
    if (SimAdvisor::has(a.oodDistance)) {
      const bool ood = a.oodDistance > 1.0;
      const std::string v =
          kit::fmtValue(a.oodDistance, 3) + (ood ? " (out of distribution)" : "");
      kit::keyValueRowId("ood", "Distance from training data", v.c_str(),
                         ood ? kit::Tone::Warn : kit::Tone::Neutral);
      kit::itemTooltip("How far this part/setup sits from the cases the advisor "
                       "was trained on. Large values mean its predictions are "
                       "extrapolation.");
    }
    if (!a.modelRevision.empty())
      kit::keyValueRowId("model", "Advisor model", a.modelRevision.c_str(),
                         kit::Tone::Neutral, true);
    if (!a.note.empty())
      kit::dimText(a.note.c_str());
    if (!a.anyDetail())
      kit::dimText("No further detail reported.");
  }
  kit::cardEnd();
}

// Flow-family colormap toggle side effects shared by both result families.
void applyColormapToggle(AppState &st) {
  if (st.renderer)
    st.renderer->setColormapMode(st.colormapOn);
  st.legendOn = st.colormapOn && st.docMeshHasScalar;
}

} // namespace

// Results step with no result. The pinned footer owns the actions (primary "Run <type>", plus
// the blocker and "Go to <step>" when setup blocks the run), so the body says only what will
// appear here — never the blocker again (one place per message).
void SimManager::drawResultsEmpty(AppState &st, Study &s) {
  (void)st;
  // This study is solving: the footer carries progress + Cancel; the body says so.
  const SimRunUi ru = runUi();
  if (ru.running && ru.studyId == s.id) {
    kit::spinner(ru.stage.empty() ? "Solving\xe2\x80\xa6" : ru.stage.c_str());
    return;
  }
  const char *body = runBlocker(s).empty() ? "Run the study to see results."
                                           : "Results appear here once the study runs.";
  kit::emptyState(studyTypeIcon(s.type), "No results yet", body);
}

void SimManager::drawResultControls(AppState &st, Study &s) {
  if (s.type == StudyType::Topology) {
    drawTopologyResults(st, s);
    return;
  }
  // Multiphysics kinds render numeric/plot results (no viewport overlay).
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
    drawResultsEmpty(st, s);
    return;
  }
  // drawForm owns the single scrolling body; results stay in it so the pinned
  // footer action remains outside the scroll region.
  const UnitSys u = unitSys(st);
  const char *stressU = kit::unitText(u.stress);
  const char *lenU = kit::unitText(u.len);
  const char *degC = kit::unitText(kit::Unit::DegC);
  const bool flow = isFlowFamily(s.type);
  const bool thermalFlow =
      flow && (s.type == StudyType::Thermal || s.flowThermal) &&
      flowFieldPresent(s.flowFields, "temperature");
  const nlohmann::json &sm = s.resultSummary;
  auto num = [&](const char *key, double def) {
    double v = def;
    return (sm.is_object() && finiteNumber(sm, key, v)) ? v : def;
  };

  // ---- static: headline factor of safety per the selected failure criterion
  // (fall back to von Mises if the engine build lacks the criterion keys).
  const bool isStatic = s.type == StudyType::Static;
  const char *fosKey =
      (s.criterion == FailureCriterion::Tresca)      ? "min_fos_tresca"
      : (s.criterion == FailureCriterion::MaxNormal) ? "min_fos_rankine"
                                                     : "min_fos";
  const char *critName =
      (s.criterion == FailureCriterion::Tresca)      ? "Tresca"
      : (s.criterion == FailureCriterion::MaxNormal) ? "max normal"
                                                     : "von Mises";
  const double fos = isStatic ? num(fosKey, num("min_fos", 0.0)) : 0.0;
  const bool fosFail = isStatic && fos > 0.0 && fos < 1.0;
  // von Mises FoS < 1 <=> the peak von Mises stress exceeds the material yield.
  const bool vmExceeds = isStatic && num("min_fos", 99.0) < 1.0;
  const bool adaptShort =
      isStatic && !s.adaptPasses.empty() && !s.adaptEtaTargetMet;
  const double vmMpa = isStatic ? num("max_von_mises_mpa", 0.0) : 0.0;
  // The solver caps every factor of safety at 15 (fea_run clamp_fos); a value
  // at the cap means "15 or more", never a computed 15.
  constexpr double kFosCap = 15.0;
  const bool fosAtCap = fos >= kFosCap - 1e-6;
  const std::string fosTip =
      std::string("Minimum factor of safety (") + critName + " criterion)." +
      (fosAtCap ? " The solver reports factors of safety up to 15; this part "
                  "is at least that safe everywhere."
                : "");
  // Same masking as the peak chip: the ring of elements around ideal fixtures
  // is left out of the headline numbers (clamp singularity). When the full
  // nodal peak there is clearly higher, the chip says so in its label.
  const double vmRawMpa = isStatic ? num("max_von_mises_raw_mpa", 0.0) : 0.0;
  const bool peakMasked = vmRawMpa > vmMpa * 1.05;
  // Flow context for the verdict line: the inputs that frame the numbers
  // (speed, Reynolds), which the chips and the metric card do not repeat.
  std::string flowContext;
  if (flow) {
    flowContext = "Steady flow at " +
                  kit::fmtQuantity(s.flowSpeed, kit::unitText(kit::Unit::m_s));
    if (s.reynolds > 0.0)
      flowContext += ", Reynolds ~" + kit::fmtValue(s.reynolds, 3);
    flowContext += ".";
  }

  // ---- 1. headline outcome: each number appears ONCE — in a chip here, or as
  // a row of the key-metrics card below, never both. The verdict line carries
  // words (holds / yields, flow context), not the chip values again.
  {
    kit::Status status = kit::Status::Done;
    std::vector<OutcomeChip> chips;
    std::string verdict;
    std::string fatigueTip;
    if (isStatic) {
      status = fosFail ? kit::Status::Failed
               : adaptShort ? kit::Status::Retry
                            : kit::Status::Done;
      if (fos > 0.0) {
        chips.push_back({"Min FoS",
                         fosAtCap ? std::string("15+") : kit::fmtValue(fos, 3),
                         fosFail ? kit::Tone::Err : kit::Tone::Ok, fosTip.c_str()});
        verdict = std::string(fosFail ? "Yields under this load ("
                                      : "Holds under this load (") +
                  critName + " criterion).";
      }
      chips.push_back({peakMasked ? "Peak stress (away from fixtures)"
                                  : "Peak stress",
                       kit::fmtQuantity(vmMpa * u.stressFromMpa, stressU),
                       vmExceeds ? kit::Tone::Warn : kit::Tone::Neutral,
                       peakMasked
                           ? "Highest von Mises stress, leaving out the two "
                             "element rings around fixed faces: an ideal clamp "
                             "makes the stress there depend on the mesh. The "
                             "color legend and 'Peak at fixtures' include those "
                             "nodes, so they read higher."
                           : "Highest von Mises stress in the part."});
      if (!s.adaptPasses.empty())
        chips.push_back({adaptShort ? "Under-resolved" : "Converged", "",
                         adaptShort ? kit::Tone::Warn : kit::Tone::Ok,
                         "Adaptive mesh refinement against its error target."});
    } else if (s.type == StudyType::Modal) {
      if (s.freqs.empty()) {
        status = kit::Status::Retry;
        verdict = "The solver reported no natural frequencies.";
      } else {
        chips.push_back({"Modes", std::to_string(s.freqs.size())});
        chips.push_back({"First mode", kit::fmtQuantity(s.freqs[0], "Hz")});
      }
    } else if (s.type == StudyType::Fatigue && sm.is_object() &&
               sm.contains("theories") && sm["theories"].is_object()) {
      // Headline theory's safety factor and life at the design reliability
      // (the same evaluation as the per-theory table below).
      const int m =
          (s.fatigueMethod < 0 || s.fatigueMethod > 4) ? 0 : s.fatigueMethod;
      const double SeD =
          reliability_ke(s.fatigueReliability) * num("se_corrected_mpa", 0.0);
      const double Sut = num("sut_mpa", 0.0);
      double saeq = 0.0;
      const auto &th = sm["theories"];
      if (th.contains(kTheoryKeys[m]) && th[kTheoryKeys[m]].is_object())
        finiteNumber(th[kTheoryKeys[m]], "sa_eq_mpa", saeq);
      if (SeD > 0.0 && Sut > 0.0) {
        const double ffos = saeq > 1e-9 ? SeD / saeq : 15.0;
        const double life = sn_life(saeq, SeD, Sut);
        const bool infinite = life >= 1e6;
        status = ffos < 1.0 ? kit::Status::Failed : kit::Status::Done;
        fatigueTip = std::string("Fatigue factor of safety (") +
                     kTheoryNames[m] + " mean-stress theory).";
        chips.push_back({"Fatigue FoS", kit::fmtValue(ffos, 3),
                         ffos < 1.0 ? kit::Tone::Err : kit::Tone::Ok,
                         fatigueTip.c_str()});
        chips.push_back({"Life",
                         infinite ? std::string("Infinite")
                                  : kit::fmtValue(life, 3) + " cycles",
                         infinite ? kit::Tone::Ok : kit::Tone::Neutral,
                         infinite ? "Runout: the alternating stress is below the "
                                    "endurance limit."
                                  : "Cycles to failure from the S-N curve."});
        verdict = std::string(infinite ? "Infinite life" : "Finite life") +
                  " at " + kit::fmtQuantity(s.fatigueReliability, "%") +
                  " reliability (" + kTheoryNames[m] + ").";
      }
    } else if (thermalFlow) {
      status = s.banner.empty() ? kit::Status::Done : kit::Status::Retry;
      chips.push_back({"Max temp", kit::fmtQuantity(s.cpMax, degC)});
      chips.push_back({"Inlet", kit::fmtQuantity(s.flowInletTempC, degC)});
      verdict = flowContext;
    } else if (flow) {
      status = s.banner.empty() ? kit::Status::Done : kit::Status::Retry;
      chips.push_back({"Drag", kit::fmtQuantity(s.dragN, "N")});
      chips.push_back({"Cd", kit::fmtValue(s.cd, 3), kit::Tone::Neutral,
                       "Drag coefficient."});
      chips.push_back({"L/D", kit::fmtValue(s.ld, 3), kit::Tone::Neutral,
                       "Lift-to-drag ratio."});
      verdict = flowContext;
    }
    if (flow && !s.banner.empty())
      chips.push_back({"Qualitative", "", kit::Tone::Warn,
                       "Indicative result: see the note below."});
    addElementsChip(chips, s);
    drawOutcomeCard(status, chips, verdict);
  }

  // ---- 2. warnings, inline under the headline (a yielding part is already
  // the verdict + red chip; only a von Mises exceedance under another
  // criterion needs its own line)
  if (vmExceeds && !fosFail)
    kit::inlineStatus("Peak von Mises stress exceeds the material yield "
                      "strength.",
                      kit::Tone::Warn);
  if (adaptShort)
    kit::inlineStatus("Adaptive refinement missed its error target: treat the "
                      "stresses as under-resolved.",
                      kit::Tone::Warn);
  if (!s.banner.empty())
    kit::inlineStatus(("Qualitative result: " + s.banner).c_str(),
                      kit::Tone::Warn);
  if (flow && s.overlayPath.empty())
    kit::inlineStatus(
        "This run returned numerical results without a display field. "
        "Pressure coloring, streamlines and velocity slices are unavailable; "
        "the original model is shown. Numerical results remain available below.",
        kit::Tone::Warn);

  // ---- 3. key metrics, right under the headline: the numbers the chips do
  // not carry (fatigue renders its own multi-theory view below)
  if (isStatic) {
    auto stress = [&](const char *key) {
      return kit::fmtQuantity(num(key, 0.0) * u.stressFromMpa, stressU);
    };
    // A key/value row with a hover explanation (keyValueRowId has no tip).
    auto rowWithTip = [](const char *id, const char *label,
                         const std::string &value, const char *tip) {
      const ImVec2 rowMin = ImGui::GetCursorScreenPos();
      const float rowW = ImGui::GetContentRegionAvail().x;
      kit::keyValueRowId(id, label, value.c_str());
      const ImVec2 rowMax(rowMin.x + rowW, ImGui::GetCursorScreenPos().y);
      if (ImGui::IsWindowHovered() && ImGui::IsMouseHoveringRect(rowMin, rowMax))
        kit::tooltip(tip);
    };
    // The headline peak leaves out two element rings around ideal fixtures (a
    // clamp singularity); the nodal colormap does not. Show the nodal peak too
    // so the legend maximum never silently contradicts the headline.
    if (kit::cardBegin("metrics", "Key results", "results")) {
      if (peakMasked)
        rowWithTip("fixtures", "Peak at fixtures",
                   stress("max_von_mises_raw_mpa"),
                   "Highest nodal von Mises stress, including the nodes beside "
                   "fixed faces; the color legend shows it. The peak stress "
                   "chip leaves out two element rings there, where an ideal "
                   "clamp makes the stress depend on the mesh.");
      // Shear: tau_max = (P1 - P3) / 2. The solver's max_tresca_mpa is the
      // Tresca stress INTENSITY P1 - P3 (2 x tau_max, always >= von Mises), so
      // it is shown under that name and never passed off as a shear stress.
      if (sm.contains("max_shear_mpa"))
        rowWithTip("shear", "Max shear stress", stress("max_shear_mpa"),
                   "Largest (P1 - P3) / 2, away from fixed faces like the "
                   "peak stress chip.");
      if (sm.contains("max_tresca_mpa"))
        rowWithTip("tresca", "Tresca stress intensity", stress("max_tresca_mpa"),
                   "Largest P1 - P3 (twice the max shear stress), including "
                   "the nodes beside fixed faces. The Tresca criterion compares "
                   "it with yield strength; it is always at least the von Mises "
                   "stress at the same point.");
      if (sm.contains("max_principal_mpa"))
        kit::keyValueRowId("p1", "Max principal (P1)",
                           stress("max_principal_mpa").c_str());
      if (sm.contains("min_principal_mpa"))
        kit::keyValueRowId("p3", "Min principal (P3)",
                           stress("min_principal_mpa").c_str());
      kit::keyValueRowId(
          "disp", "Max displacement",
          kit::fmtQuantity(num("max_disp_mm", 0.0) * u.lenFromMm, lenU).c_str());
    }
    kit::cardEnd();
  } else if (s.type == StudyType::Modal) {
    std::vector<kit::KeyValueItem> rows;
    for (size_t i = 0; i < s.freqs.size(); ++i)
      rows.push_back({"Mode " + std::to_string(i + 1),
                      kit::fmtQuantity(s.freqs[i], "Hz")});
    kit::keyValueCard("metrics", "Natural frequencies", "study_modal", rows);
  } else if (thermalFlow) {
    // Thermal / CHT result: cpMin/cpMax carry the surface (or solid)
    // temperature range; max and inlet are headline chips.
    const bool part = s.flowThermalType == 1;
    kit::keyValueCard("metrics", "Temperatures", "study_thermal",
                      {{part ? "Part temp (min)" : "Surface temp (min)",
                        kit::fmtQuantity(s.cpMin, degC)}});
  } else if (flow) {
    // Drag, Cd and L/D are headline chips; Reynolds is in the verdict line.
    std::vector<kit::KeyValueItem> rows = {{"Lift", kit::fmtQuantity(s.liftN, "N")}};
    if (std::fabs(s.sideN) > 1e-9)
      rows.push_back({"Side force", kit::fmtQuantity(s.sideN, "N")});
    rows.push_back({"Lift coefficient (Cl)", kit::fmtValue(s.cl, 3)});
    rows.push_back({s.flowRefAreaMm2 > 0 ? "Reference area" : "Frontal area",
                    kit::fmtQuantity(s.refAreaMm2 > 0 ? s.refAreaMm2 : s.frontalMm2,
                                     kit::unitText(kit::Unit::mm2))});
    kit::keyValueCard("metrics", "Forces & coefficients", "study_flow", rows);
  }

  // ---- 4. viewport: field selector + display toggles (after the key metrics;
  // collapsible, open until the user folds it away)
  {
    const bool hasFlowPlot = flow && !s.overlayPath.empty();
    const bool hasFields =
        s.type == StudyType::Static || s.type == StudyType::Fatigue || hasFlowPlot;
    const bool hasDeform = !flow && st.docMeshHasDisp && st.renderer;
    if (disclosureCardBegin("view", "Viewport", "display_shaded", true)) {
      kit::FieldGroup group("view_fields", "Viewport field");
      if (hasFields) {
        const ResultField *arr = (s.type == StudyType::Static)    ? kStaticFields
                                 : (s.type == StudyType::Fatigue) ? kFatigueFields
                                                                  : kFlowFields;
        const int n = (s.type == StudyType::Static)    ? kNStaticFields
                      : (s.type == StudyType::Fatigue) ? kNFatigueFields
                                                       : kNFlowFields;
        // Flow/thermal results only offer the fields they carry (aero ->
        // pressure_cp/velocity; thermal/CHT -> temperature/velocity); FEA
        // studies expose their full field set.
        std::vector<int> idx;
        std::vector<const char *> labels;
        for (int i = 0; i < n; ++i)
          if (!flow || flowFieldPresent(s.flowFields, arr[i].chunk)) {
            idx.push_back(i);
            labels.push_back(arr[i].label);
          }
        int cur = 0;
        for (int k = 0; k < (int)idx.size(); ++k)
          if (st.activeScalarField == arr[idx[k]].chunk)
            cur = k;
        if (!labels.empty() &&
            kit::fieldEnum("field", "Viewport field", &cur, labels.data(),
                           (int)labels.size(),
                           kit::FieldOpts().withTip(
                               "Which result field colors the model. Switching "
                               "is instant: the same result is re-colored, no "
                               "re-solve."))) {
          st.activeScalarField = arr[idx[cur]].chunk;
          applyResultField(st, s);
          // Velocity reads on the slice planes; pressure reads on the body. The
          // body carries only the Cp scalar, so for the velocity view drop the
          // body colormap (it would otherwise fall back to Cp) and let the
          // speed-colored slices carry the field.
          if (flow) {
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
      if (kit::fieldCheckbox(
              "colormap", "Show colormap", &st.colormapOn,
              kit::FieldOpts()
                  .withDisabled(flow && !hasFlowPlot, "This run has no display field.")
                  .withTip(
                  flow ? "Color the model by the selected field (Cp or "
                         "temperature; velocity shows on the slices)."
                       : "Color the model by the selected result field.")))
        applyColormapToggle(st);
      if (flow) {
        if (kit::fieldCheckbox("streamlines", "Show streamlines",
                               &flowShowStreamlines_,
                               kit::FieldOpts()
                                   .withDisabled(!hasFlowPlot, "This run has no streamlines.")
                                   .withTip(
                                   "Trace the flow as speed-colored streamlines "
                                   "over the model.")))
          rebuildFlowLines(st, s);
        if (kit::fieldCheckbox("slices", "Show velocity slices", &flowShowSlices_,
                               kit::FieldOpts()
                                   .withDisabled(!hasFlowPlot, "This run has no velocity slices.")
                                   .withTip(
                                   "Velocity cross-section planes (mid-X / mid-Y "
                                   "/ mid-Z) drawn as a speed-colored grid.")))
          rebuildFlowLines(st, s);
      }
      if (hasDeform) {
        // Deformation: the presets are the primary control (Auto = the suggested
        // visible exaggeration; 1x = real displacement; Off = undeformed); the
        // slider below sets a custom exaggeration. Its range always contains
        // every preset and the current scale, so drawing it never clamps
        // (mutates) the view.
        const float autoS = st.dispScaleHint > 0 ? st.dispScaleHint : 1.0f;
        const std::string tip =
            "How the deformed shape is drawn. Auto (" + kit::fmtValue(autoS, 3) +
            "\xc3\x97) picks a visible scale; 1\xc3\x97 is the true-scale "
            "displacement; Off shows the undeformed model.";
        static const char *const kPresets[] = {"Auto", "1\xc3\x97", "Off"};
        static const char *const kPresetTips[] = {
            "Suggested visible exaggeration.", "True-scale displacement.",
            "Undeformed model."};
        const float cur = st.renderer->deformScale();
        int preset = cur == autoS ? 0 : cur == 1.0f ? 1 : cur == 0.0f ? 2 : -1;
        const float w = kit::fieldBegin("deform_preset", "Deformation",
                                        kit::FieldOpts().withTip(tip.c_str()));
        if (kit::segmented("presets", kPresets, 3, &preset, w, kPresetTips))
          st.renderer->setDeformScale(preset == 0   ? autoS
                                      : preset == 1 ? 1.0f
                                                    : 0.0f);
        kit::fieldEnd();
        float scale = st.renderer->deformScale();
        const float maxScale = std::max({autoS * 2.0f, 1.0f, scale});
        kit::fieldSlider("deform", "Exaggeration", &scale, 0.0f, maxScale,
                         kit::FieldOpts()
                             .withUnit("\xc3\x97")
                             .withSigFigs(3)
                             .withTip("Drag for a custom exaggeration of the "
                                      "deformed shape (0 = undeformed)."));
        if (scale != st.renderer->deformScale())
          st.renderer->setDeformScale(scale);
      }
      if (flow && flowLinesBuiltFor_ == s.id && flowSegStride_ > 1) {
        const std::string note =
            "Line overlay shows " + groupedCount((uint64_t)flowSegsShown_) +
            " of " + groupedCount((uint64_t)flowSegsTotal_) + " segments (1 in " +
            std::to_string(flowSegStride_) + ") to stay inside the display budget.";
        kit::dimText(note.c_str());
      }
    }
    kit::cardEnd();
  }

  // ---- 5. details: adaptive convergence history
  if (isStatic && !s.adaptPasses.empty()) {
    if (kit::cardBegin("adaptive", "Adaptive convergence")) {
      kit::keyValueRowId("eta", "Achieved error indicator",
                         kit::fmtValue(s.adaptGlobalEta, 3).c_str(),
                         adaptShort ? kit::Tone::Warn : kit::Tone::Neutral);
      if (!s.adaptStopReason.empty()) {
        // Engine stop tokens -> words; the engine's detail (after ':') goes in
        // the tooltip, never into the row.
        const std::string &why = s.adaptStopReason;
        const char *label = why.rfind("eta_target_met", 0) == 0 ? "Error target met"
                            : why.rfind("pass_budget_exhausted", 0) == 0
                                ? "Pass limit reached"
                            : why.rfind("refinement_exhausted", 0) == 0
                                ? "Mesh could not refine further"
                                : "Stopped early";
        const ImVec2 rowMin = ImGui::GetCursorScreenPos();
        const float rowW = ImGui::GetContentRegionAvail().x;
        kit::keyValueRowId("stop", "Stopped because", label);
        const ImVec2 rowMax(rowMin.x + rowW, ImGui::GetCursorScreenPos().y);
        const size_t colon = why.find(':');
        if (colon != std::string::npos && colon + 1 < why.size() &&
            ImGui::IsWindowHovered() && ImGui::IsMouseHoveringRect(rowMin, rowMax)) {
          const size_t from = why.find_first_not_of(' ', colon + 1);
          kit::tooltip(why.substr(from == std::string::npos ? colon + 1 : from).c_str());
        }
      }
      std::vector<kit::MetricRow> rows;
      rows.reserve(s.adaptPasses.size());
      for (const auto &pass : s.adaptPasses)
        rows.push_back({std::to_string(pass.pass), "",
                        {{groupedCount((uint64_t)std::max(0, pass.nElems))},
                         {kit::fmtValue(pass.globalEta, 3)}}});
      kit::metricTable("passes", {"Elements", "Indicator"}, rows, "Pass");
    }
    kit::cardEnd();
  }

  // ---- 6. plots
  if (s.type == StudyType::Fatigue)
    drawFatigueGraphs(st, s);
  if (flow && !s.dragTrace.empty()) {
    kit::PlotSpec p;
    p.mode = kit::PlotMode::Sparkline;
    p.caption = "Drag trace";
    p.series.push_back({"Drag", s.dragTrace});
    kit::linePlot("drag_trace", p);
    // The live trace is thinned at ingestion so a long run cannot grow it
    // without bound; say so rather than implying every iteration is plotted.
    if (s.dragTraceStride > 1) {
      const std::string note =
          "Showing " + groupedCount((uint64_t)s.dragTrace.size()) + " of " +
          groupedCount(s.dragTraceTotal) + " iterations (1 in " +
          std::to_string(s.dragTraceStride) + ").";
      kit::dimText(note.c_str());
    }
  }

  // ---- 7. provenance, collapsed
  drawMeshSolverCard(s);
  drawAdvisorDisclosure(s);
}

void SimManager::drawProvenance(const nlohmann::json &result) {
  if (!result.is_object() || !result.contains("simhandoff") ||
      !result["simhandoff"].is_object())
    return;
  const auto &env = result["simhandoff"];
  std::string src = "?";
  if (env.contains("source") && env["source"].is_object())
    src = env["source"].value("domain", std::string("?")) + "/" +
          env["source"].value("tool", std::string("?"));
  // Callers may show several envelopes in one pane (handoff result + heatsink
  // pick): scope by the payload's address, which is a stable member.
  ImGui::PushID(&result);
  if (disclosureCardBegin("provenance", "Provenance")) {
    kit::keyValueRowId("coupling", "Coupling",
                       env.value("coupling", std::string("?")).c_str());
    kit::keyValueRowId("source", "Source", src.c_str());
    kit::keyValueRowId("schema", "Envelope", "aicad.simhandoff/1");
    kit::keyValueRowId("id", "Envelope id",
                       env.value("id", std::string("?")).c_str(),
                       kit::Tone::Neutral, true);
  }
  kit::cardEnd();
  ImGui::PopID();
}

// ---------------------------------------------------------------- advanced
// results block

void SimManager::drawAdvancedResults(AppState &st, Study &s) {
  if (!s.hasResult) {
    if (advRunning_ && advRunningStudyId_ == s.id)
      kit::spinner(advStage_.empty() ? "Solving\xe2\x80\xa6" : advStage_.c_str());
    else
      drawResultsEmpty(st, s);
    return;
  }
  const UnitSys u = unitSys(st);
  const char *forceU = kit::unitText(u.force);
  const char *degC = kit::unitText(kit::Unit::DegC);
  const nlohmann::json &res = s.advResult;
  const bool haveRes = res.is_object();

  // ---- headline outcome (numbers once: chips here, the rest in the cards
  // below; the verdict is words, never the raw engine summary string)
  {
    kit::Status status = kit::Status::Done;
    std::vector<OutcomeChip> chips;
    std::string verdict;
    if (s.type == StudyType::Buckling && haveRes && res.contains("modes") &&
        res["modes"].is_array() && !res["modes"].empty()) {
      double f1 = 0.0;
      if (finiteNumber(res["modes"][0], "load_factor", f1)) {
        // Critical load = factor x applied load: a factor below 1 buckles
        // before the applied load is reached.
        const bool buckles = f1 > 0.0 && f1 < 1.0;
        status = buckles ? kit::Status::Failed : kit::Status::Done;
        chips.push_back({"First load factor", kit::fmtValue(f1, 3),
                         buckles ? kit::Tone::Err : kit::Tone::Ok,
                         "Critical load = load factor \xc3\x97 applied load."});
        verdict = buckles ? "Buckles before the applied load is reached."
                          : "Stable at the applied load.";
      }
    } else if (s.type == StudyType::Contact && haveRes) {
      double applied = 0.0, reaction = 0.0;
      const bool haveApplied = finiteNumber(res, "applied_load_n", applied);
      const bool haveReaction = finiteNumber(res, "base_reaction_n", reaction);
      if (haveApplied)
        chips.push_back({"Applied", kit::fmtQuantity(applied * u.forceFromN, forceU)});
      if (haveReaction)
        chips.push_back({"Reaction", kit::fmtQuantity(reaction * u.forceFromN, forceU)});
      if (haveApplied && haveReaction && std::fabs(applied) > 0.0) {
        const double off = (std::fabs(reaction) / std::fabs(applied) - 1.0) * 100.0;
        verdict = std::fabs(off) <= 2.0
                      ? std::string("The base reaction balances the applied load.")
                      : "The base reaction differs from the applied load by " +
                            kit::fmtValue(std::fabs(off), 3) + " %.";
        if (std::fabs(off) > 2.0)
          status = kit::Status::Retry;
      }
    } else if (s.type == StudyType::FreqResponse && !s.frAccel.empty()) {
      const auto peakIt = std::max_element(s.frAccel.begin(), s.frAccel.end());
      chips.push_back({"Peak response",
                       kit::fmtQuantity(*peakIt, kit::unitText(kit::Unit::gAccel))});
      const size_t at = (size_t)(peakIt - s.frAccel.begin());
      if (at < s.frSweep.size())
        verdict = "Peak response at " +
                  kit::fmtQuantity(s.frSweep[at], kit::unitText(kit::Unit::Hz)) + ".";
    } else if (s.type == StudyType::ThermalFea && haveRes) {
      const auto sm = res.value("summary", nlohmann::json::object());
      double tmax = 0.0;
      if (sm.is_object() && finiteNumber(sm, "max_temp_c", tmax))
        chips.push_back({"Max temp", kit::fmtQuantity(tmax, degC)});
    }
    addElementsChip(chips, s);
    drawOutcomeCard(status, chips, verdict);
  }

  if (s.type == StudyType::Buckling && haveRes) {
    if (res.contains("modes") && res["modes"].is_array()) {
      const auto &modes = res["modes"];
      double P = 0.0;
      finiteNumber(res, "reference_load_n", P);
      // Shape shares go in the mode-name tooltip; build every string first so
      // the rows' tooltip pointers stay valid through the table call.
      std::vector<std::string> tips(modes.size());
      for (size_t i = 0; i < modes.size(); ++i)
        if (modes[i].contains("participation") &&
            modes[i]["participation"].is_object()) {
          const auto &p = modes[i]["participation"];
          tips[i] = "Directional shape share: X " +
                    kit::fmtValue(p.value("x", 0.0) * 100.0, 3) + "%, Y " +
                    kit::fmtValue(p.value("y", 0.0) * 100.0, 3) + "%, Z " +
                    kit::fmtValue(p.value("z", 0.0) * 100.0, 3) + "%.";
        }
      std::vector<kit::MetricRow> rows;
      for (size_t i = 0; i < modes.size(); ++i) {
        double factor = 0.0;
        if (!finiteNumber(modes[i], "load_factor", factor))
          continue;
        kit::MetricRow r;
        r.metric = "Mode " + std::to_string(i + 1);
        r.values = {{kit::fmtValue(factor, 4),
                     factor > 0.0 && factor < 1.0 ? kit::Tone::Err
                                                  : kit::Tone::Neutral},
                    {kit::fmtQuantity(factor * P * u.forceFromN, forceU)}};
        r.tooltip = tips[i].empty() ? nullptr : tips[i].c_str();
        rows.push_back(std::move(r));
      }
      if (kit::cardBegin("buckling", "Buckling load factors", "study_buckling")) {
        kit::metricTable("modes", {"Load factor", "Critical load"}, rows, "Mode");
        kit::dimText("Hover a mode for its directional shape shares (not "
                     "effective modal mass).");
      }
      kit::cardEnd();
    }
  } else if (s.type == StudyType::Contact && haveRes) {
    // Applied load and base reaction are the headline chips; the verdict
    // states the balance.
    double v = 0.0;
    if (!finiteNumber(res, "applied_load_n", v) ||
        !finiteNumber(res, "base_reaction_n", v))
      kit::inlineStatus("Reaction audit unavailable in this saved result.",
                        kit::Tone::Info);
    kit::dimText(res.value("note", std::string()).c_str());
  } else if (s.type == StudyType::FreqResponse && haveRes) {
    if (!s.frSweep.empty() && s.frSweep.size() == s.frAccel.size()) {
      kit::PlotSpec p;
      p.x = s.frSweep;
      p.xAxis.title = "Frequency";
      p.xAxis.unit = kit::unitText(kit::Unit::Hz);
      p.yAxis.title = "Response";
      p.yAxis.unit = kit::unitText(kit::Unit::gAccel);
      p.series.push_back({"Acceleration", s.frAccel});
      kit::linePlot("fr_response", p);
    }
    if (!s.freqs.empty()) {
      std::vector<kit::KeyValueItem> rows;
      for (size_t i = 0; i < s.freqs.size(); ++i)
        rows.push_back({"Mode " + std::to_string(i + 1),
                        kit::fmtQuantity(s.freqs[i], "Hz")});
      kit::keyValueCard("modes", "Natural frequencies", "study_vibration", rows);
    }
    kit::dimText(res.value("damping_assumption", std::string()).c_str());
    kit::dimText("Random (PSD) response needs a qualification-spec PSD: ask "
                 "the copilot for a random-vibration response with your PSD "
                 "breakpoints.");
  } else if (s.type == StudyType::ThermalFea && haveRes) {
    const auto sm = res.value("summary", nlohmann::json::object());
    std::vector<kit::KeyValueItem> rows;
    double v = 0.0;
    // Max temperature is the headline chip.
    if (sm.is_object() && finiteNumber(sm, "final_max_temp_c", v))
      rows.push_back({"Final max temperature", kit::fmtQuantity(v, degC)});
    if (sm.is_object() && finiteNumber(sm, "min_temp_c", v))
      rows.push_back({"Min temperature", kit::fmtQuantity(v, degC)});
    if (res.contains("n_steps") && res["n_steps"].is_number())
      rows.push_back({"Time steps", std::to_string(res["n_steps"].get<long long>())});
    kit::keyValueCard("thermal",
                      s.thermTransient ? "Transient conduction" : "Steady conduction",
                      "study_thermal_fea", rows);
    kit::inlineStatus("No temperature colormap yet: the conduction solver "
                      "reports a numeric summary and flux audit only.",
                      kit::Tone::Info);
  }
  drawMeshSolverCard(s);
  drawAdvisorDisclosure(s);
  drawProvenance(res);
}

} // namespace ui
