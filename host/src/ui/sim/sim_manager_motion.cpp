// SimManager motion study: drivers, run/cache/trace, playback, collisions, form/timeline/results.
#include "sim_manager_internal.h"
#include "sim_common.h"

#include <cfloat>
#include <climits>
#include <cstdio>
#include <limits>
#include <memory>

#include "../../render/overlays.h"
#include "../../renderer.h"
#include "../anim.h"
#include "../asm_panels.h"

namespace ui {

namespace {

nlohmann::json motionSource(const nlohmann::json &tree) {
  auto source = tree;
  if (source.is_object())
    source.erase("studies");
  return source;
}

// Joint name without the " [id] - type (unit)" tail motionJoints() appends: Start/Stop show the
// unit, so the combo no longer truncates on that tail. `withType` adds " · Angle"/" · Distance"
// (the combo tells joints apart by it; playback rows carry the type as their key instead).
std::string motionJointBase(const MotionJoint &joint, bool withType) {
  const std::string tail = " [" + joint.id + "] - " + joint.type + " (" + joint.unit + ")";
  std::string name = joint.label;
  if (name.size() >= tail.size() &&
      name.compare(name.size() - tail.size(), tail.size(), tail) == 0)
    name.resize(name.size() - tail.size());
  if (withType)
    name += joint.type == "angle" ? " \xC2\xB7 Angle" : " \xC2\xB7 Distance";
  return name;
}

// Display names for every joint; the mate id is appended only where two names would collide.
std::vector<std::string> motionJointNames(const std::vector<MotionJoint> &joints,
                                          bool withType = true) {
  std::vector<std::string> names;
  names.reserve(joints.size());
  for (const auto &joint : joints)
    names.push_back(motionJointBase(joint, withType));
  std::vector<bool> clash(names.size(), false);
  for (size_t i = 0; i < names.size(); ++i)
    for (size_t j = i + 1; j < names.size(); ++j)
      if (names[i] == names[j])
        clash[i] = clash[j] = true;
  for (size_t i = 0; i < names.size(); ++i)
    if (clash[i])
      names[i] += " (" + joints[i].id + ")";
  return names;
}

constexpr const char *kMotionJointPayload = "CHUDWARE_MOTION_JOINT";
constexpr const char *kMotionDriverPayload = "CHUDWARE_MOTION_DRIVER";

// Keep the origin immutable during a drag. Serializing starts only after ImGui has started
// dragging; changing document/study or editing/reordering the source cannot retarget the payload.
void motionSetupDragSource(const std::string &docId, const Study &study,
                           const std::string &mateId, const char *label,
                           int sourceIndex = -1) {
  if (docId.empty() || study.id.empty() || mateId.empty() ||
      !ImGui::BeginDragDropSource())
    return;
  nlohmann::json data = {{"doc_id", docId}, {"study_id", study.id}, {"mate_id", mateId}};
  if (sourceIndex >= 0) {
    data["source_index"] = static_cast<uint64_t>(sourceIndex);
    data["generation"] = study.motionGeneration;
  }
  const std::string bytes = data.dump();
  ImGui::SetDragDropPayload(sourceIndex >= 0 ? kMotionDriverPayload : kMotionJointPayload,
                           bytes.data(), bytes.size(), ImGuiCond_Once);
  kit::dimText(label);
  ImGui::EndDragDropSource();
}

struct MotionSetupDrop {
  std::string mateId;
  int sourceIndex = -1;
  uint64_t generation = 0;
};

bool motionSetupDropTarget(const std::string &docId, const Study &study,
                           const std::vector<MotionJoint> &joints, MotionSetupDrop &out) {
  if (docId.empty() || study.id.empty() || !ImGui::BeginDragDropTarget())
    return false;
  bool accepted = false;
  for (const char *type : {kMotionJointPayload, kMotionDriverPayload}) {
    const auto *payload = ImGui::AcceptDragDropPayload(type);
    if (!payload || !payload->IsDelivery() || !payload->Data ||
        payload->DataSize <= 0 || payload->DataSize > 65536)
      continue;
    const auto *bytes = static_cast<const char *>(payload->Data);
    const auto data = nlohmann::json::parse(bytes, bytes + payload->DataSize, nullptr, false);
    if (!data.is_object() || !data.contains("doc_id") || !data["doc_id"].is_string() ||
        data["doc_id"].get_ref<const std::string &>() != docId ||
        !data.contains("study_id") || !data["study_id"].is_string() ||
        data["study_id"].get_ref<const std::string &>() != study.id ||
        !data.contains("mate_id") || !data["mate_id"].is_string())
      continue;
    const auto &mateId = data["mate_id"].get_ref<const std::string &>();
    if (mateId.empty() ||
        std::none_of(joints.begin(), joints.end(),
                     [&](const MotionJoint &joint) { return joint.id == mateId; }))
      continue;
    const auto duplicates = std::count_if(
        study.motionDrivers.begin(), study.motionDrivers.end(),
        [&](const MotionDriver &driver) { return driver.mateId == mateId; });
    MotionSetupDrop drop;
    drop.mateId = mateId;
    if (type == kMotionDriverPayload) {
      if (duplicates != 1 || !data.contains("source_index") ||
          !data["source_index"].is_number_unsigned() || !data.contains("generation") ||
          !data["generation"].is_number_unsigned())
        continue;
      const uint64_t index = data["source_index"].get<uint64_t>();
      drop.generation = data["generation"].get<uint64_t>();
      if (index >= study.motionDrivers.size() || index > static_cast<uint64_t>(INT_MAX) ||
          study.motionDrivers[index].mateId != mateId ||
          drop.generation != study.motionGeneration)
        continue;
      drop.sourceIndex = static_cast<int>(index);
    } else if (duplicates != 0) {
      continue;
    }
    out = std::move(drop);
    accepted = true;
    break;
  }
  ImGui::EndDragDropTarget();
  return accepted;
}

bool motionFrameCollision(const nlohmann::json &frame) {
  const auto detail = frame.find("collision_detail");
  return simJsonBool(frame, "interference", false) ||
         (detail != frame.end() && detail->is_array() && !detail->empty());
}

bool motionChecksComplete(const Study &s) {
  const auto &check = s.motionInterference;
  if (!check.is_object() || !simJsonBool(check, "checked", false) ||
      !simJsonBool(check, "complete", false) ||
      !check.contains("frames_checked") || !check["frames_checked"].is_number_integer() ||
      check["frames_checked"].get<int>() != static_cast<int>(s.motionTrajectory.size()) ||
      !check.contains("pair_failures") || !check["pair_failures"].is_array() ||
      !check["pair_failures"].empty())
    return false;
  if (check.contains("geometry_failures") &&
      (!check["geometry_failures"].is_array() || !check["geometry_failures"].empty()))
    return false;
  for (const auto &frame : s.motionTrajectory)
    if (!simJsonBool(frame, "interference_complete", false) ||
        !frame.contains("interference") || !frame["interference"].is_boolean())
      return false;
  return true;
}

// Short outcome label for the result chip.
const char *motionCheckLabel(const Study &s) {
  if (!s.motionCollisionFrames.empty())
    return motionChecksComplete(s) ? "Collision" : "Collision \xC2\xB7 partly checked";
  if (!s.motionCheckInterference)
    return "Interference not checked";
  return motionChecksComplete(s) ? "No interference" : "Check incomplete";
}

// The same outcome as a phrase for the run summary (status bar, toast, Run page "Last run").
std::string motionCheckPhrase(const Study &s) {
  const size_t hits = s.motionCollisionFrames.size();
  if (hits > 0)
    return "Collision at " + std::to_string(hits) +
           (hits == 1 ? " sampled pose" : " sampled poses") +
           (motionChecksComplete(s) ? "" : ", some poses not fully checked");
  if (!s.motionCheckInterference)
    return "Interference not checked";
  return motionChecksComplete(s) ? "No interference at sampled poses"
                                 : "Some poses not fully checked";
}

kit::Tone motionCheckTone(const Study &s) {
  if (!s.motionCollisionFrames.empty())
    return kit::Tone::Err;
  if (!s.motionCheckInterference)
    return kit::Tone::Neutral;
  return motionChecksComplete(s) ? kit::Tone::Ok : kit::Tone::Warn;
}
} // namespace

void SimManager::configureActiveMotion(AppState &st, float start, float stop,
                                       int steps) {
  if (sel_ < 0 || sel_ >= (int)studies_.size())
    return;
  Study &s = studies_[sel_];
  if (s.type != StudyType::Motion || motionRun_ || s.motionRunning)
    return;
  seedMotionDriver(s, motionJoints(st.docMirror.tree()));
  if (s.motionDrivers.empty())
    s.motionDrivers.push_back({});
  s.motionDrivers.front().start = start;
  s.motionDrivers.front().stop = stop;
  invalidateMotion(s);
  s.motionSteps = std::max(2, std::min(steps, 120));
  persist(st, s);
}

void SimManager::restoreMotionHighlight(AppState &st) {
  if (!motionHighlightPose_)
    return;
  st.featureHighlightFaces.clear();
  motionHighlightPose_ = false;
  motionHighlightStudyId_.clear();
  const auto *session = st.activeSession();
  const render::DocMesh *base =
      st.docMirror.hasDoc() && session && session->cachedDocMesh &&
              session->cachedRevision == st.docMirror.revision()
          ? session->cachedDocMesh.get()
          : (st.docMirror.hasDoc() &&
                     motionCacheDocId_ == st.docMirror.docId()
                 ? motionDocMesh_.get() : nullptr);
  if (st.overlays && base) {
    std::string err;
    if (!st.overlays->setDoc(*base, err))
      st.addLog("[motion] restore selection geometry: " + err);
  }
}

// ============================================================ Motion study
//
// Motion studies sample synchronized dimensional assembly mates through the existing native
// simulate_motion RPC. AsyncRpc keeps both activation and solving off the UI thread. Only the
// owning document/study/generation may consume a completion; viewport changes additionally require
// that study's Results view to be active. AICM is world-space, so playback applies
// frameWorld * inverse(cachedBaseWorld), never an independent fixed-world spin.

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

struct SimManager::MotionRun {
  std::string docId, studyId;
  uint64_t generation = 0;
  nlohmann::json source, activation, params;
  std::shared_ptr<const render::DocMesh> baseMesh;
};

void SimManager::invalidateMotion(Study &s) {
  ++s.motionGeneration;
  clearTransientResult(s);
  s.motionSource = nlohmann::json();
  s.summary.clear();
}

Study *SimManager::motionRunStudy(AppState &st, const std::shared_ptr<MotionRun> &run) {
  if (motionRun_ != run)
    return nullptr;
  const nlohmann::json *tree = nullptr;
  for (const auto &tab : st.docTabs)
    if (tab.docId == run->docId && tab.session && tab.session->sim.get() == this) {
      if (simOriginActive(st, this, run->docId))
        tree = &st.docMirror.tree();
      else if (tab.session->cachedMirror.valid())
        tree = &tab.session->cachedMirror.tree;
      break;
    }
  for (auto &study : studies_)
    if (study.id == run->studyId && study.motionGeneration == run->generation) {
      if (tree && sameMotionSource(*tree, run->source))
        return &study;
      invalidateMotion(study);
      study.summary = "Assembly changed or closed during the run. Reopen Motion setup and run again.";
      break;
    }
  motionRun_.reset();
  return nullptr;
}

std::string simmgr::motionGeometryBlocker(AppState &st) {
  // Run shares the viewport's parsed assembly mesh (never parses on Run), so it waits until that
  // mesh is current. At the same revision the parked snapshot is the live tree; only after an
  // edit is the (studies-insensitive) tree comparison needed.
  const auto *session = st.activeSession();
  if (!session)
    return "Open the assembly tab to run Motion.";
  if (!session->cachedDocMesh || !session->cachedMirror.valid())
    return "Wait for the assembly geometry to load.";
  if (session->cachedDocMesh->empty())
    return "Check that the components resolve in the viewport; the assembly has no geometry "
           "to move.";
  if (session->cachedRevision != st.docMirror.revision() &&
      !sameMotionSource(session->cachedMirror.tree, st.docMirror.tree()))
    return "Wait for the assembly geometry to update.";
  return {};
}

void SimManager::runMotion(AppState &st, Study &s) {
  // run() refuses through runBlockerFor (same checks, shown inline in the footer); this guard
  // only keeps a direct call from sampling stale geometry, and still says why.
  const std::string why = !st.docMirror.hasDoc() ? std::string("Open an assembly to run Motion.")
                          : motionRun_ ? std::string("A Motion run is already in progress.")
                                       : motionGeometryBlocker(st);
  if (!why.empty()) {
    s.summary = why;
    st.addLog("[motion] run blocked: " + why);
    return;
  }
  const auto *session = st.activeSession();
  // Persist only inputs. No restore/activation or solver call may block the UI thread.
  invalidateMotion(s);
  persist(st, s);
  auto run = std::make_shared<MotionRun>();
  run->docId = st.docMirror.docId();
  run->studyId = s.id;
  run->generation = s.motionGeneration;
  run->source = motionSource(st.docMirror.tree());
  run->baseMesh = session->cachedDocMesh; // share immutable parsed geometry; never copy/parse on Run
  const auto &first = s.motionDrivers.front();
  run->params = {{"doc_id", run->docId}, {"mate_id", first.mateId},
                 {"start", first.start}, {"stop", first.stop},
                 {"drivers", motionDriversJson(s)}, {"steps", s.motionSteps},
                 {"check_interference", s.motionCheckInterference},
                 {"min_clearance_mm", s.motionMinClearance}};
  // Snapshot the optional native inverse-dynamics object before asynchronous activation.
  // An absent object keeps the existing kinematic-only run; no GUI defaults are injected.
  const auto definition = studyToJson(s);
  const auto motion = definition.find("motion");
  if (motion != definition.end() && motion->is_object() && motion->contains("dynamics"))
    run->params["dynamics"] = (*motion)["dynamics"];
  motionRun_ = run;
  s.motionRunning = true;
  s.summary = "Preparing the assembly snapshot\xE2\x80\xA6";
  st.rpc.call("doc_activate", {{"doc_id", run->docId}},
              [this, &st, run](const core::RpcResult &r) {
    Study *study = motionRunStudy(st, run);
    if (!study)
      return;
    if (!r.ok || !r.result.is_object() || r.result.contains("code") ||
        !r.result.contains("tree") ||
        !sameMotionSource(r.result["tree"], run->source)) {
      core::RpcResult failure;
      failure.err = !r.ok || r.result.contains("code")
                        ? simJsonText(r.result, "message", "error", r.err)
                        : "Assembly snapshot changed. Refresh the document and run Motion again.";
      finishMotionRun(st, run, failure);
      return;
    }
    run->activation = r.result;
    study->summary = "Solving the motion samples\xE2\x80\xA6";
    st.rpc.call("simulate_motion", run->params,
                [this, &st, run](const core::RpcResult &result) {
                  finishMotionRun(st, run, result);
                }, 120000);
  }, 60000);
}

void SimManager::finishMotionRun(AppState &st, const std::shared_ptr<MotionRun> &run,
                                 const core::RpcResult &response) {
  Study *study = motionRunStudy(st, run);
  if (!study)
    return;
  Study &s = *study;
  s.motionRunning = false;
  motionRun_.reset();
  const std::string &docId = run->docId;
  const bool active = simOriginActive(st, this, docId);
  auto failRun = [&](const std::string &message) {
    s.motionPlaying = false;
    s.summary = "Motion failed: " + message;
    st.addLog("[motion] " + s.summary);
    if (active) {
      st.status = s.summary;
      st.statusKind = 2;
      st.toast(ui::ToastLevel::Error, s.summary);
    }
  };

  const auto &result = response.result;
  if (!response.ok || !result.is_object() || result.contains("code")) {
    failRun(simJsonText(result, "message", "error",
                       response.err.empty() ? "motion solver returned no result" : response.err));
    return;
  }
  if (result.contains("dynamics") && result.contains("doc_id") && result["doc_id"].is_string() &&
      result["doc_id"].get_ref<const std::string &>() == docId)
    s.advResult = result["dynamics"];
  if (!simJsonBool(result, "ok", false)) {
    failRun(simJsonText(result, "error", "message", "motion error"));
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
  static const nlohmann::json emptyTrajectory = nlohmann::json::array();
  const auto &trajectory = result.contains("trajectory") ? result["trajectory"] : emptyTrajectory;
  std::string trajectoryError;
  std::unordered_set<std::string> expectedComponents;
  if (!trajectory.is_array() || trajectory.size() < 2)
    trajectoryError = "trajectory must contain at least two frames";
  for (size_t fi = 0; trajectoryError.empty() && fi < trajectory.size(); ++fi) {
    const auto &frameJ = trajectory[fi];
    if (!frameJ.is_object() || !simJsonBool(frameJ, "converged", false) ||
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
  s.motionResultDrivers = result.value("drivers", nlohmann::json::array());
  s.motionInterference = result.value("interference", nlohmann::json());
  s.motionSource = run->source;
  s.motionSourceRevision = active ? st.docMirror.revision() : -1;
  for (size_t i = 0; i < trajectory.size(); ++i)
    if (motionFrameCollision(trajectory[i]))
      s.motionCollisionFrames.push_back(static_cast<int>(i));

  std::string cacheError;
  if (!prepareMotionCache(s, run->activation, docId, run->baseMesh, cacheError)) {
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

  // One human sentence for the Run page "Last run"; the status bar and toast prefix the study
  // kind because they are read outside the pane.
  const size_t nDrivers = s.motionDrivers.size();
  s.summary = std::to_string(nFrames) + " samples \xC2\xB7 " + std::to_string(nDrivers) +
              (nDrivers == 1 ? " driver" : " drivers") + " \xC2\xB7 " + motionCheckPhrase(s);
  st.addLog("[motion] " + s.summary);
  if (active) {
    st.status = "Motion \xC2\xB7 " + s.summary;
    st.statusKind = motionCheckTone(s) == kit::Tone::Err ? 2 :
                    motionCheckTone(s) == kit::Tone::Warn ? 1 : 0;
    st.toast(ui::ToastLevel::Info, st.status);
    if (sel_ >= 0 && sel_ < static_cast<int>(studies_.size()) &&
        studies_[sel_].id == s.id &&
        st.activeWorkspace == AppState::Workspace::Simulate) {
      node_ = SimNode::Results;
      resultShown_ = false; // syncResultView applies frame zero only to this visible study.
      shownStudyId_.clear();
    }
  }
}

bool SimManager::prepareMotionCache(Study &s, const nlohmann::json &activation,
                                    const std::string &docId,
                                    std::shared_ptr<const render::DocMesh> mesh, std::string &err) {
  err.clear();
  // The host already parsed this immutable source revision on its mesh-loader thread. Share it
  // rather than re-reading a large assembly on the UI thread or adopting a background document.
  if (!mesh || mesh->empty()) {
    err = "base geometry is unavailable; wait for the viewport to load and run again";
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

  // Pattern instances may have no tree.components row. A trajectory pose can supply their base
  // transform only if ALL driven dimensions equal the activation's current values at that sample.
  const auto joints = motionJoints(*treeIt);
  for (const auto &frameJ : s.motionTrajectory) {
    bool atBase = !s.motionDrivers.empty();
    for (const auto &driver : s.motionDrivers) {
      const auto joint = std::find_if(joints.begin(), joints.end(),
                                     [&](const MotionJoint &j) { return j.id == driver.mateId; });
      const nlohmann::json *value = nullptr;
      if (frameJ.contains("driver_values") && frameJ["driver_values"].is_object()) {
        const auto v = frameJ["driver_values"].find(driver.mateId);
        if (v != frameJ["driver_values"].end())
          value = &*v;
      } else if (s.motionDrivers.size() == 1 && frameJ.contains("value")) {
        value = &frameJ["value"];
      }
      if (joint == joints.end() || !value || !value->is_number() ||
          !std::isfinite(value->get<double>()) ||
          std::abs(value->get<double>() - joint->value) >
              1e-9 * std::max(1.0, std::abs(joint->value))) {
        atBase = false;
        break;
      }
    }
    if (atBase) {
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
  for (const auto &body : mesh->bodies) {
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

  motionDocMesh_ = std::move(mesh);
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

  if (!motionDocMesh_ || motionDocMesh_->empty() || motionCacheDocId_ != st.docMirror.docId())
    return failPlayback("base geometry is unavailable; run Motion again to refresh it");
  if (s.motionSourceRevision != st.docMirror.revision()) {
    if (!sameMotionSource(s.motionSource, st.docMirror.tree())) {
      invalidateMotion(s);
      return failPlayback("assembly changed; run Motion again");
    }
    s.motionSourceRevision = st.docMirror.revision();
  }
  if (motionHighlightPose_ && st.overlays) {
    std::string err;
    st.clearSelection();
    if (!st.overlays->setDoc(*motionDocMesh_, err))
      return failPlayback("could not restore selection geometry: " + err);
    motionHighlightPose_ = false;
    st.featureHighlightFaces.clear();
    motionHighlightStudyId_.clear();
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
  parts.reserve(motionDocMesh_->bodies.size());
  for (const auto &body : motionDocMesh_->bodies) {
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
    pm.appearance_regions = body.appearance_regions;
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

  ++modelRestoreGeneration_;
  st.meshGen_.fetch_add(1); // an older async model restore cannot replace this playback frame
  st.renderer->parkActiveMeshes();
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
  if (s.type != StudyType::Motion)
    return;
  // Setup normalisation lives here, not in drawMotionForm: a study created before the assembly
  // had a drivable joint gets its first driver seeded (and saved) once one appears.
  if (s.motionDrivers.size() == 1 && s.motionDrivers[0].mateId.empty() &&
      !s.motionRunning && !motionRun_) {
    const auto joints = motionJoints(st.docMirror.tree());
    if (!joints.empty()) {
      seedMotionDriver(s, joints);
      persist(st, s);
    }
  }
  if (!s.motionHasResult || s.motionTrajectory.empty())
    return;
  s.motionFrame = std::clamp(s.motionFrame, 0, (int)s.motionTrajectory.size() - 1);
  if (!s.motionPlaying)
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
  anim::requestFrames();

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
  ImGuiContext &g = *ImGui::GetCurrentContext();
  // The study pane owns the run blocker and footer. This body edits native setup only.
  if (!s.motionRunning && !s.motionHasResult && !s.summary.empty())
    kit::inlineStatus(s.summary.c_str(), kit::Tone::Warn);
  const bool locked = motionRun_ || s.motionRunning;
  const auto joints = motionJoints(st.docMirror.tree());
  if (joints.empty()) {
    const bool assembly = st.activeTab >= 0 && st.activeTab < (int)st.docTabs.size() &&
                          st.docTabs[st.activeTab].kind == AppState::DocTab::TabKind::Assembly;
    if (!assembly) {
      kit::blockedState("No assembly open", "Motion studies drive the mates of an assembly.",
                        "Switch to an assembly tab, or create one with File > New Assembly.");
    } else if (kit::blockedState("No drivable joint",
                                 "This assembly has no active Angle or Distance mate.",
                                 "Add an Angle or Distance mate between the moving component and "
                                 "its reference. A concentric or hinge mate alone does not "
                                 "define a driver.",
                                 locked ? nullptr : "Add a mate")) {
      st.activeWorkspace = AppState::Workspace::Model;
      st.showRibbon = true;
      st.ribbonTab = AppState::RT_Features;
      if (st.asmPanels)
        st.asmPanels->handleCommand(st, "asm_mate");
    }
    return;
  }

  if (locked)
    kit::inlineStatus("Setup is locked while Motion is running.", kit::Tone::Info);
  ImGui::PushID(st.docMirror.docId().c_str());
  ImGui::PushID(s.id.c_str());
  ImGui::BeginDisabled(locked);
  bool changed = false, save = false;
  const std::vector<std::string> jointLabels = motionJointNames(joints);
  const std::vector<std::string> jointShort = motionJointNames(joints, false);
  auto addJoint = [&](const std::string &id, size_t position) {
    const auto joint = std::find_if(joints.begin(), joints.end(),
                                    [&](const MotionJoint &j) { return j.id == id; });
    if (locked || joint == joints.end() ||
        std::any_of(s.motionDrivers.begin(), s.motionDrivers.end(),
                    [&](const MotionDriver &driver) { return driver.mateId == id; }))
      return;
    MotionDriver driver{joint->id, joint->value,
                        joint->value + (joint->type == "angle" ? 360.0 : 10.0)};
    if (s.motionDrivers.size() == 1 && s.motionDrivers.front().mateId.empty())
      s.motionDrivers.front() = std::move(driver);
    else
      s.motionDrivers.insert(s.motionDrivers.begin() + std::min(position, s.motionDrivers.size()),
                             std::move(driver));
    changed = save = true;
  };

  takeFocus("drivers");
  if (kit::cardBegin("available_joints", "Available joints", "mate")) {
    kit::dimText("Click a joint to add a driver, or drag it into the driver list.");
    for (size_t j = 0; j < joints.size(); ++j) {
      const auto &joint = joints[j];
      const bool driven = std::any_of(s.motionDrivers.begin(), s.motionDrivers.end(),
                                      [&](const MotionDriver &d) { return d.mateId == joint.id; });
      const std::string current = sim::fmtU(joint.value, joint.unit.c_str(), unitSys(st).us);
      kit::RowStyle style;
      style.icon = joint.type == "angle" ? "rotate_component" : "move_component";
      style.secondary = driven ? "Already in this study" : "Click or drag to add";
      style.meta = current.c_str();
      style.disabled = driven || locked;
      style.tooltip = joint.label.c_str();
      ImGui::PushID(joint.id.c_str());
      const bool clicked = kit::row("available_joint", jointLabels[j].c_str(), style);
      IMGUI_TEST_ENGINE_ITEM_INFO(ImGui::GetItemID(), jointLabels[j].c_str(),
                                 ImGui::GetCurrentContext()->LastItemData.StatusFlags);
      if (!driven && !locked)
        motionSetupDragSource(st.docMirror.docId(), s, joint.id, jointLabels[j].c_str());
      if (clicked)
        addJoint(joint.id, s.motionDrivers.size());
      ImGui::PopID();
    }
  }
  kit::cardEnd();
  kit::gapRow();

  kit::sectionHeader("Drivers", nullptr, nullptr, static_cast<int>(s.motionDrivers.size()), true);
  kit::dimText("Drag a Move Driver row before another driver, or to the end of the list.");
  static const char *const kUsedReason = "Already driven by another driver in this study.";
  const std::vector<const char *> usedReasons(joints.size(), kUsedReason);
  std::unique_ptr<bool[]> used(new bool[joints.size()]);
  int removeIdx = -1, dropPosition = -1;
  MotionSetupDrop pendingDrop;
  for (int i = 0; i < static_cast<int>(s.motionDrivers.size()); ++i) {
    auto &driver = s.motionDrivers[i];
    int sel = -1;
    for (int j = 0; j < static_cast<int>(joints.size()); ++j) {
      if (joints[j].id == driver.mateId)
        sel = j;
      used[j] = joints[j].id != driver.mateId &&
                std::any_of(s.motionDrivers.begin(), s.motionDrivers.end(),
                            [&](const MotionDriver &d) { return d.mateId == joints[j].id; });
    }
    char title[32], orderTitle[48];
    std::snprintf(title, sizeof(title), "Driver %d", i + 1);
    std::snprintf(orderTitle, sizeof(orderTitle), "Move Driver %d", i + 1);
    const char *icon = sel >= 0 && joints[sel].type == "distance" ? "move_component"
                                                                  : "rotate_component";
    ImGui::PushID(i);
    kit::RowStyle orderStyle;
    orderStyle.icon = icon;
    orderStyle.secondary = sel >= 0 ? jointShort[sel].c_str() : "Choose an eligible joint";
    orderStyle.tooltip = "Drag to reorder. Drop a joint here to insert another driver before this one.";
    orderStyle.disabled = locked;
    kit::row("order", orderTitle, orderStyle);
    IMGUI_TEST_ENGINE_ITEM_INFO(ImGui::GetItemID(), orderTitle,
                               ImGui::GetCurrentContext()->LastItemData.StatusFlags);
    if (!locked) {
      if (sel >= 0)
        motionSetupDragSource(st.docMirror.docId(), s, driver.mateId, orderTitle, i);
      if (motionSetupDropTarget(st.docMirror.docId(), s, joints, pendingDrop))
        dropPosition = i;
    }
    bool removed = false;
    const bool open = s.motionDrivers.size() > 1
                          ? kit::removableCard("driver", title, icon, nullptr,
                                               "Remove this driver", &removed)
                          : kit::cardBegin("driver", title, icon);
    if (open) {
      {
        kit::FieldGroup group("fields", "Joint");
        const char *issue = sel >= 0 ? nullptr
                            : driver.mateId.empty()
                                ? "Choose an Angle or Distance joint."
                                : "This joint is missing, suppressed or no longer dimensional.";
        if (kit::fieldCombo("joint", "Joint", &sel, jointLabels,
                            kit::FieldOpts()
                                .withRequired()
                                .withHint("Choose a joint")
                                .withPreview(sel >= 0 ? jointShort[sel].c_str() : nullptr)
                                .withIssue(issue)
                                .withItemDisabled(used.get(), usedReasons.data())
                                .withTip("Angle or Distance mate this driver sweeps.")) &&
            sel >= 0) {
          const auto &joint = joints[sel];
          driver = {joint.id, joint.value, joint.value + (joint.type == "angle" ? 360.0 : 10.0)};
          changed = save = true;
        }
        const char *unit = sel >= 0 ? joints[sel].unit.c_str() : nullptr;
        if (kit::fieldNumber("start", "Start", &driver.start,
                             sim::fieldU(unit, unitSys(st).us).withTip(
                                 "Exact joint value at the first sample.")))
          changed = save = true;
        if (kit::fieldNumber("stop", "Stop", &driver.stop,
                             sim::fieldU(unit, unitSys(st).us).withTip(
                                 "Exact joint value at the last sample. Equal to Start holds this "
                                 "joint while the other drivers move.")))
          changed = save = true;
      }
      if (kit::secondaryButton("Reverse range###reverse_range", driver.start != driver.stop)) {
        std::swap(driver.start, driver.stop);
        changed = save = true;
      }
      if (kit::secondaryButton("Reset to current###reset_current", sel >= 0,
                               "Choose an eligible joint first.") && sel >= 0) {
        const double current = joints[sel].value;
        if (driver.start != current || driver.stop != current) {
          driver.start = driver.stop = current;
          changed = save = true;
        }
      }
      kit::itemTooltip("Set both endpoints to the assembly's current mate value. This joint will hold.");
      const bool rangeOpen = kit::popoverBegin("range_adjust", "Adjust range");
      if (rangeOpen) {
        // A popup is exclusive: one fixed window of slider bounds survives the whole gesture.
        // These are editing bounds, not new kinematic limits; exact fields remain unrestricted.
        static std::array<double, 2> bounds{};
        if (ImGui::IsWindowAppearing()) {
          const double current = sel >= 0 ? joints[sel].value : driver.start;
          const double pad = sel >= 0 && joints[sel].type == "angle" ? 360.0 : 10.0;
          bounds = {std::max(-1.0e300, std::min({driver.start, driver.stop, current}) - pad),
                    std::min(1.0e300, std::max({driver.start, driver.stop, current}) + pad)};
        }
        kit::dimText("Adjust either endpoint interactively; use Start and Stop for exact values.");
        if (std::isfinite(bounds[0]) && std::isfinite(bounds[1]) && bounds[0] < bounds[1]) {
          kit::FieldGroup group("range_fields", "Start");
          const auto options = sim::fieldU(
              sel >= 0 ? joints[sel].unit.c_str() : nullptr, unitSys(st).us);
          const double beforeStart = driver.start, beforeStop = driver.stop;
          const bool startReleased =
              kit::fieldSlider("adjust_start", "Start", &driver.start, bounds[0], bounds[1], options);
          const bool stopReleased =
              kit::fieldSlider("adjust_stop", "Stop", &driver.stop, bounds[0], bounds[1], options);
          if (driver.start != beforeStart || driver.stop != beforeStop)
            changed = true;
          save |= startReleased || stopReleased;
        } else {
          kit::dimText("Use the exact entries for a range too large for the interactive controls.");
        }
        kit::popoverEnd();
      }
    }
    kit::cardEnd();
    ImGui::PopID();
    if (removed && !locked)
      removeIdx = i;
  }
  kit::RowStyle dropStyle;
  dropStyle.icon = "mate";
  dropStyle.secondary = "Adds it last";
  dropStyle.tooltip = "Drop a joint to add it as a driver, or drop a driver to move it to the end.";
  dropStyle.disabled = locked;
  kit::row("driver_drop", "Drop joint or driver here", dropStyle);
  IMGUI_TEST_ENGINE_ITEM_INFO(ImGui::GetItemID(), "Drop joint or driver here",
                             ImGui::GetCurrentContext()->LastItemData.StatusFlags);
  if (!locked && motionSetupDropTarget(st.docMirror.docId(), s, joints, pendingDrop))
    dropPosition = static_cast<int>(s.motionDrivers.size());

  // Structural mutations are deferred until no card has a reference into the driver vector.
  if (removeIdx >= 0) {
    s.motionDrivers.erase(s.motionDrivers.begin() + removeIdx);
    changed = save = true;
  } else if (dropPosition >= 0) {
    if (pendingDrop.sourceIndex < 0) {
      addJoint(pendingDrop.mateId, static_cast<size_t>(dropPosition));
    } else if (!changed && pendingDrop.generation == s.motionGeneration &&
               pendingDrop.sourceIndex < static_cast<int>(s.motionDrivers.size()) &&
               s.motionDrivers[pendingDrop.sourceIndex].mateId == pendingDrop.mateId) {
      const int destination =
          dropPosition - (pendingDrop.sourceIndex < dropPosition ? 1 : 0);
      if (destination != pendingDrop.sourceIndex) {
        // Rotate whole records; mate identity and double-precision endpoints stay together.
        auto begin = s.motionDrivers.begin();
        if (pendingDrop.sourceIndex < destination)
          std::rotate(begin + pendingDrop.sourceIndex, begin + pendingDrop.sourceIndex + 1,
                      begin + destination + 1);
        else
          std::rotate(begin + destination, begin + pendingDrop.sourceIndex,
                      begin + pendingDrop.sourceIndex + 1);
        changed = save = true;
      }
    }
  }
  kit::gapRow();

  if (kit::cardBegin("sampling", "Sampling", "interference")) {
    kit::FieldGroup group("fields", "Minimum clearance");
    if (kit::fieldInt("samples_exact", "Samples", &s.motionSteps,
                      kit::FieldOpts().withRange(2.0, 120.0).withTip(
                          "Exact number of poses, including both endpoints.")))
      changed = save = true;
    double steps = s.motionSteps;
    const bool released = kit::fieldSlider(
        "samples", "Adjust samples", &steps, 2.0, 120.0,
        kit::FieldOpts().withSigFigs(3).withTip(
            "Interference is checked at sampled poses only, not continuously between them."));
    const int n = std::clamp(static_cast<int>(std::lround(steps)), 2, 120);
    if (n != s.motionSteps) {
      s.motionSteps = n;
      changed = true;
    }
    save |= released;
    if (kit::fieldCheckbox("interference", "Check interference", &s.motionCheckInterference,
                           kit::FieldOpts().withTip(
                               "Test every sampled pose for solid intersections and "
                               "clearance violations.")))
      changed = save = true;
    if (kit::fieldNumber("clearance", "Minimum clearance", &s.motionMinClearance,
                         sim::fieldU("mm", unitSys(st).us)
                             .withMin(0.0)
                             .withTip("Also report separations below this value. Zero checks "
                                      "intersections only.")
                             .withDisabled(!s.motionCheckInterference,
                                           "Turn on Check interference to set a clearance.")))
      changed = save = true;
    kit::dimText("To calculate joint forces and moments, add inverse dynamics in Advanced "
                 "parameters. Set the sweep duration, gravity, component masses, applied loads "
                 "and damping there. Without inverse dynamics, this study checks motion only.");
  }
  kit::cardEnd();
  kit::gapRow();

  // Playback presentation changes persist, but do not change the solved samples.
  if (kit::cardBegin("playback", "Playback", "results")) {
    kit::FieldGroup group("fields", "Loop playback");
    if (kit::fieldNumber("fps", "Frame rate", &s.motionFps,
                         kit::FieldOpts().withUnit("fps").withRange(1.0, 120.0).withSigFigs(3)
                             .withTip("Samples shown per second during playback. Playback never "
                                      "modifies the assembly.")))
      save = true;
    if (kit::fieldCheckbox("loop", "Loop playback", &s.motionLoop,
                           kit::FieldOpts().withTip("Wrap to the first sample after the last.")))
      save = true;
  }
  kit::cardEnd();
  ImGui::EndDisabled();
  ImGui::PopID();
  ImGui::PopID();

  if (changed)
    invalidateMotion(s);
  if (save)
    persist(st, s);
}

// ---------- drawMotionTimeline (transport bar + scrub)
// --------------------------------

void SimManager::drawMotionTimeline(AppState &st, Study &s) {
  if (!s.motionHasResult || s.motionTrajectory.empty())
    return;
  const int nFrames = static_cast<int>(s.motionTrajectory.size());
  const int cur = std::clamp(s.motionFrame, 0, nFrames - 1);
  auto seek = [&](int frame) {
    s.motionPlaying = false;
    s.motionLastTick = -1.0;
    s.motionFrame = std::clamp(frame, 0, nFrames - 1);
    if (applyMotionFrame(st, s, s.motionFrame))
      st.clearSelection();
  };

  ImGui::PushID("transport");
  const float gap = ImGui::GetStyle().ItemInnerSpacing.x;
  if (kit::iconButton("first", "media_first", "First sample", cur > 0))
    seek(0);
  ImGui::SameLine(0, gap);
  if (kit::iconButton("prev", "media_prev", "Previous sample", cur > 0))
    seek(cur - 1);
  ImGui::SameLine(0, gap);
  if (kit::iconButton("play", s.motionPlaying ? "media_pause" : "media_play",
                      s.motionPlaying ? "Pause playback" : "Play the sampled motion",
                      s.motionRenderError.empty(), kit::Tone::Accent)) {
    if (!s.motionPlaying && !s.motionLoop && cur == nFrames - 1)
      seek(0); // replay from the start instead of a no-op at the end
    s.motionPlaying = !s.motionPlaying;
    s.motionLastTick = -1.0;
    st.clearSelection();
  }
  ImGui::SameLine(0, gap);
  if (kit::iconButton("next", "media_next", "Next sample", cur < nFrames - 1))
    seek(cur + 1);
  ImGui::SameLine(0, gap);
  if (kit::iconButton("last", "media_last", "Last sample", cur < nFrames - 1))
    seek(nFrames - 1);
  ImGui::PopID();

  {
    kit::FieldGroup group("scrub", "Sample");
    const std::string ofN = "/ " + std::to_string(nFrames);
    double sample = cur + 1;
    int sig = 1;
    for (int v = nFrames; v >= 10; v /= 10)
      ++sig;
    kit::fieldSlider("sample", "Sample", &sample, 1.0, static_cast<double>(nFrames),
                     kit::FieldOpts().withUnit(ofN.c_str()).withSigFigs(sig).withTip(
                         "Scrub to any sampled pose."));
    const int frame = std::clamp(static_cast<int>(std::lround(sample)) - 1, 0, nFrames - 1);
    if (frame != cur)
      seek(frame);
  }

  // Driver values at the displayed pose (stable mate ids key the rows). The joint name is a
  // wrapping caption and the row key is the short quantity ("Distance 80.87 mm"), so neither
  // elides in the narrow key column.
  const auto &pose = s.motionTrajectory[std::clamp(s.motionFrame, 0, nFrames - 1)];
  const auto values = pose.find("driver_values");
  if (s.motionResultDrivers.is_array() && values != pose.end() && values->is_object()) {
    const auto joints = motionJoints(st.docMirror.tree());
    const auto names = motionJointNames(joints, false);
    for (size_t i = 0; i < s.motionResultDrivers.size(); ++i) {
      const auto &driver = s.motionResultDrivers[i];
      if (!driver.is_object())
        continue;
      const std::string id = driver.value("mate_id", std::string());
      const auto value = values->find(id);
      if (value == values->end() || !value->is_number())
        continue;
      const auto joint = std::find_if(joints.begin(), joints.end(),
                                      [&](const MotionJoint &j) { return j.id == id; });
      const bool known = joint != joints.end();
      const std::string caption =
          known ? names[joint - joints.begin()] : "Driver " + std::to_string(i + 1);
      const char *key = !known                   ? "Value"
                        : joint->type == "angle" ? "Angle"
                                                 : "Distance";
      const std::string text = sim::fmtU(
          value->get<double>(), driver.value("unit", std::string()).c_str(), unitSys(st).us);
      ImGui::PushID(static_cast<int>(i));
      kit::dimText(caption.c_str());
      kit::keyValueRowId("driver", key, text.c_str());
      ImGui::PopID();
    }
  } else if (pose.contains("value") && pose["value"].is_number()) {
    const std::string text = sim::fmtU(pose["value"].get<double>(),
                                      s.motionDriverUnit.c_str(), unitSys(st).us);
    kit::keyValueRowId("driver", "Driver", text.c_str());
  }
}

void SimManager::highlightMotionCollisions(AppState &st, const Study &s,
                                            const nlohmann::json *pair) {
  std::unordered_set<std::string> components;
  auto include = [&](const nlohmann::json &hit) {
    for (const char *key : {"component_a", "component_b"})
      if (hit.contains(key) && hit[key].is_string())
        components.insert(hit[key].get<std::string>());
  };
  if (pair) {
    include(*pair);
  } else {
    const auto &pose = s.motionTrajectory[s.motionFrame];
    if (pose.contains("collision_detail") && pose["collision_detail"].is_array())
      for (const auto &hit : pose["collision_detail"])
        if (hit.is_object())
          include(hit);
  }
  st.clearSelection();
  st.hover = {};
  if (!st.overlays || !motionDocMesh_)
    return;
  render::DocMesh highlights;
  // Reuse the FeatureManager's whole-component face-highlight channel. Do not issue pick-resolve
  // RPCs for a playback pose, whose geometry is intentionally different from the editable model.
  std::unordered_set<std::string> highlighted;
  for (const auto &body : motionDocMesh_->bodies) {
    const auto component = motionComponentByBodyId_.find(body.body_id);
    if (component == motionComponentByBodyId_.end() || !components.count(component->second))
      continue;
    const auto &bodies = s.motionTrajectory[s.motionFrame]["bodies"];
    const auto pose = std::find_if(bodies.begin(), bodies.end(), [&](const nlohmann::json &b) {
      return b.value("component", std::string()) == component->second;
    });
    const auto inverse = motionInverseBaseWorldByComponentId_.find(component->second);
    MotionMat4 world;
    std::string err;
    if (pose == bodies.end() || inverse == motionInverseBaseWorldByComponentId_.end() ||
        !readMotionMatrix((*pose)["transform"], MotionMatrixLayout::ColumnMajor, world, err))
      continue;
    const auto delta = multiplyMotionMatrices(world, inverse->second);
    render::BodyMesh visible;
    visible.body_id = body.body_id;
    visible.n_verts = body.n_verts;
    visible.n_tris = body.n_tris;
    visible.faces = body.faces;
    visible.idx = body.idx;
    visible.pos.resize(body.pos.size());
    for (size_t i = 0; i + 2 < body.pos.size(); i += 3) {
      const double x = body.pos[i], y = body.pos[i + 1], z = body.pos[i + 2];
      visible.pos[i] = static_cast<float>(delta[0] * x + delta[4] * y + delta[8] * z + delta[12]);
      visible.pos[i + 1] = static_cast<float>(delta[1] * x + delta[5] * y + delta[9] * z + delta[13]);
      visible.pos[i + 2] = static_cast<float>(delta[2] * x + delta[6] * y + delta[10] * z + delta[14]);
    }
    highlights.bodies.push_back(std::move(visible));
    for (const auto &face : body.faces)
      st.featureHighlightFaces.push_back((static_cast<uint64_t>(body.body_id) << 32) | face.face_id);
    if (!body.faces.empty())
      highlighted.insert(component->second);
  }
  std::string error;
  if (!st.overlays->setDoc(highlights, error)) {
    st.featureHighlightFaces.clear();
    st.toast(ui::ToastLevel::Warn, "Could not highlight the collision pose: " + error);
  } else {
    motionHighlightPose_ = true;
    motionHighlightStudyId_ = s.id;
  }
  if (highlighted.size() < components.size() || components.empty())
    st.toast(ui::ToastLevel::Warn, "Some collision components have no viewport mapping; see pair details.");
}

// ---------- drawMotionResults (called from drawResultControls)
// -------------------------

void SimManager::drawMotionResults(AppState &st, Study &s) {
  // Preserve native dynamics evidence separately from whether the kinematic trajectory was
  // accepted. Failed force-path balance must never read as a successful dynamic simulation.
  if (s.advResult.is_object() && !s.advResult.empty()) {
    if (kit::cardBegin("dynamics_evidence", "Inverse dynamics", "results")) {
      const bool ok = simJsonBool(s.advResult, "ok", false);
      const bool resolved = simJsonBool(s.advResult, "force_path_resolved", false);
      kit::keyValueRowId("outcome", "Native outcome", ok ? "Solved" : "Not solved",
                        ok ? kit::Tone::Ok : kit::Tone::Warn);
      kit::keyValueRowId("force_path", "Force path", resolved ? "Resolved" : "Unresolved",
                        resolved ? kit::Tone::Ok : kit::Tone::Warn);
      double value = 0.0;
      if (finiteNumber(s.advResult, "duration_s", value))
        kit::keyValueRowId("duration", "Sweep duration",
                          kit::fmtQuantity(value, kit::unitText(kit::Unit::s)).c_str());
      const auto worstForce = s.advResult.find("worst_case");
      if (worstForce != s.advResult.end() &&
          finiteNumber(*worstForce, "force_mag_n", value))
        kit::keyValueRowId("worst_force", "Peak joint reaction",
                          sim::fmtU(value, "N", unitSys(st).us).c_str());
      const auto worstMoment = s.advResult.find("worst_moment_case");
      if (worstMoment != s.advResult.end() &&
          finiteNumber(*worstMoment, "moment_mag_nm", value))
        kit::keyValueRowId("worst_moment", "Peak joint moment",
                          sim::fmtU(value, "N m", unitSys(st).us).c_str());
      const auto warnings = s.advResult.find("warnings");
      if (warnings != s.advResult.end() && warnings->is_array())
        for (const auto &warning : *warnings)
          if (warning.is_string())
            kit::inlineStatus(warning.get_ref<const std::string &>().c_str(), kit::Tone::Warn);
      if (kit::secondaryButton("Copy native dynamics JSON###copy_dynamics"))
        ImGui::SetClipboardText(s.advResult.dump(2).c_str());
      kit::itemTooltip("Includes all native bodies, joints, per-frame reactions, residuals and warnings.");
    }
    kit::cardEnd();
    kit::gapRow();
  }
  if (!s.motionHasResult) {
    // The footer owns the one primary (Run Motion, or its inline blocker); this body only
    // explains and offers navigation back to the inputs.
    auto reviewSetup = [&] {
      const char *label = "Review setup###review_setup";
      const float availW = ImGui::GetContentRegionAvail().x;
      const float w = std::min(availW, ImGui::CalcTextSize(label, nullptr, true).x +
                                           kit::padX() * 4.0f);
      ImGui::SetCursorPosX(ImGui::GetCursorPosX() + std::max(0.0f, (availW - w) * 0.5f));
      if (kit::secondaryButton(label, true, nullptr, ImVec2(w, ImGui::GetFrameHeight()))) {
        node_ = SimNode::Study;
        armedWell_ = -1;
        requestFocus("drivers");
      }
      kit::itemTooltip("Open the drivers and sampling setup.");
    };
    if (s.motionRunning) {
      kit::spinner(s.summary.empty() ? "Solving motion" : s.summary.c_str());
    } else if (!s.summary.empty()) {
      kit::blockedState("Motion results unavailable", s.summary.c_str(),
                        "Check the drivers in Setup, then use Run Motion below.");
      reviewSetup();
    } else {
      kit::emptyState("rotate_component", "No Motion results yet",
                      "Run Motion below to solve the sweep and play it back here.");
      reviewSetup();
    }
    return;
  }
  const int nFrames = static_cast<int>(s.motionTrajectory.size());
  if (nFrames == 0)
    return;
  auto seekCollision = [&](int frame, const nlohmann::json *pair) {
    s.motionPlaying = false;
    s.motionLastTick = -1.0;
    s.motionFrame = std::clamp(frame, 0, nFrames - 1);
    if (applyMotionFrame(st, s, s.motionFrame))
      highlightMotionCollisions(st, s, pair);
  };

  // ---- outcome: status + chips on one wrapping line + one-line verdict ----
  const int nHits = static_cast<int>(s.motionCollisionFrames.size());
  const bool complete = motionChecksComplete(s);
  const kit::Status outcome = nHits > 0                     ? kit::Status::Failed
                              : !s.motionCheckInterference ? kit::Status::Skipped
                              : complete                   ? kit::Status::Done
                                                           : kit::Status::Retry;
  if (kit::cardBegin("outcome", "Outcome", "rotate_component")) {
    // Headline chip first, then the counts; the verdict adds only what the chips cannot say
    // (no number is repeated, and the driver count lives in Setup).
    kit::chipFlowBegin("chips");
    kit::chipFlowItem(ImGui::GetTextLineHeight());
    kit::statusGlyphInline(outcome);
    kit::flowChip(motionCheckLabel(s), motionCheckTone(s), true);
    if (nHits > 0)
      kit::flowValueChip("Colliding poses", std::to_string(nHits).c_str(), kit::Tone::Err);
    kit::flowValueChip("Samples", std::to_string(nFrames).c_str());
    kit::chipFlowEnd();
    const char *verdict =
        nHits > 0                    ? "Step through the colliding poses under Interference."
        : !s.motionCheckInterference ? "Turn on Check interference in Setup to test the poses."
        : complete                   ? "Checked at the sampled poses only, not between them."
                                     : "Some poses could not be fully checked. See Interference.";
    kit::dimText(verdict);
    if (!s.motionRenderError.empty()) {
      const std::string blocked = "Playback blocked: " + s.motionRenderError;
      kit::inlineStatus(blocked.c_str(), kit::Tone::Err);
    }
  }
  kit::cardEnd();
  kit::gapRow();

  // ---- playback: transport + scrub + driver values ----
  if (kit::cardBegin("playback", "Playback", "results"))
    drawMotionTimeline(st, s);
  kit::cardEnd();
  kit::gapRow();

  // ---- interference at the displayed pose ----
  if (kit::cardBegin("interference", "Interference", "interference")) {
    if (s.motionInterference.is_object()) {
      const auto checked = s.motionInterference.find("frames_checked");
      if (checked != s.motionInterference.end() && checked->is_number_integer()) {
        const std::string text =
            std::to_string(checked->get<int>()) + " / " + std::to_string(nFrames) + " samples";
        kit::keyValueRowId("checked", "Fully checked", text.c_str(),
                           checked->get<int>() < nFrames ? kit::Tone::Warn : kit::Tone::Neutral);
      }
    }
    if (nHits > 0) {
      if (kit::secondaryButton("Previous collision")) {
        auto it = std::lower_bound(s.motionCollisionFrames.begin(), s.motionCollisionFrames.end(),
                                   s.motionFrame);
        if (it == s.motionCollisionFrames.begin())
          it = s.motionCollisionFrames.end();
        seekCollision(*--it, nullptr);
      }
      kit::itemTooltip("Jump to the previous colliding pose and highlight its components.");
      ImGui::SameLine(0, ImGui::GetStyle().ItemInnerSpacing.x);
      if (kit::secondaryButton("Next collision")) {
        auto it = std::upper_bound(s.motionCollisionFrames.begin(), s.motionCollisionFrames.end(),
                                   s.motionFrame);
        if (it == s.motionCollisionFrames.end())
          it = s.motionCollisionFrames.begin();
        seekCollision(*it, nullptr);
      }
      kit::itemTooltip("Jump to the next colliding pose and highlight its components.");
    }

    const auto &current = s.motionTrajectory[std::clamp(s.motionFrame, 0, nFrames - 1)];
    const bool hit = motionFrameCollision(current);
    const bool poseComplete = simJsonBool(current, "interference_complete", false) &&
                              current.contains("interference") &&
                              current["interference"].is_boolean();
    kit::inlineStatus(hit                          ? "This pose: collision recorded"
                      : !s.motionCheckInterference ? "This pose: not checked"
                      : poseComplete               ? "This pose: sampled clear"
                                                   : "This pose: incomplete check",
                      hit                          ? kit::Tone::Err
                      : !s.motionCheckInterference ? kit::Tone::Neutral
                      : poseComplete               ? kit::Tone::Ok
                                                   : kit::Tone::Warn);
    const auto details = current.find("collision_detail");
    if (details != current.end() && details->is_array()) {
      for (size_t i = 0; i < details->size(); ++i) {
        const auto &pair = (*details)[i];
        if (!pair.is_object())
          continue;
        const std::string title = simJsonText(pair, "a", "component_a", "Unknown component") +
                                  " / " +
                                  simJsonText(pair, "b", "component_b", "Unknown component");
        std::string secondary;
        if (pair.contains("volume") && pair["volume"].is_number())
          secondary = "Overlap " + kit::fmtQuantity(
              pair["volume"].get<double>() / (unitSys(st).us ? 25.4 * 25.4 * 25.4 : 1.0),
              unitSys(st).us ? "in\xc2\xb3" : kit::unitText(kit::Unit::mm3));
        if (pair.contains("distance") && pair["distance"].is_number())
          secondary += (secondary.empty() ? "" : " \xC2\xB7 ") + std::string("Clearance ") +
                       sim::fmtU(pair["distance"].get<double>(), "mm", unitSys(st).us);
        const std::string warning = simJsonText(pair, "warning", "reason", "");
        const std::string tip =
            "Highlight this pair in the viewport." + (warning.empty() ? "" : "\n" + warning);
        kit::RowStyle style;
        style.icon = "interference";
        style.iconTone = kit::Tone::Err;
        style.secondary = secondary.empty() ? nullptr : secondary.c_str();
        style.meta = warning.empty() ? nullptr : "Warning";
        style.tooltip = tip.c_str();
        ImGui::PushID(static_cast<int>(i));
        if (kit::row("pair", title.c_str(), style))
          seekCollision(s.motionFrame, &pair);
        ImGui::PopID();
      }
    } else if (hit) {
      kit::inlineStatus("Pair detail unavailable; the recorded collision is not a clearance "
                        "result.",
                        kit::Tone::Warn);
    }
    if (s.motionInterference.is_object() && s.motionInterference.contains("pair_failures") &&
        s.motionInterference["pair_failures"].is_array()) {
      for (const auto &failure : s.motionInterference["pair_failures"]) {
        if (!failure.is_object() || !failure.contains("i") || !failure["i"].is_number_integer() ||
            failure["i"].get<int>() != s.motionFrame)
          continue;
        const std::string message =
            simJsonText(failure, "a", "component_a", "Geometry") + " / " +
            simJsonText(failure, "b", "component_b", "unavailable") + ": " +
            simJsonText(failure, "reason", "warning", "Pair could not be checked.");
        kit::inlineStatus(message.c_str(), kit::Tone::Warn);
      }
    }
    if (current.contains("geometry_failures") && current["geometry_failures"].is_array())
      for (const auto &failure : current["geometry_failures"]) {
        const std::string message =
            simJsonText(failure, "name", "component", "Geometry") + ": " +
            simJsonText(failure, "reason", "warning", "Geometry could not be checked.");
        kit::inlineStatus(message.c_str(), kit::Tone::Warn);
      }
  }
  kit::cardEnd();
  kit::gapRow();

  // ---- position trace: |world translation| of one component per sample ----
  // Stable component ids select trajectory rows; display names are labels only.
  const int nBodies = static_cast<int>(s.motionBodyIds.size());
  if (nBodies == 0)
    return;
  if (kit::cardBegin("trace", "Position trace", "plots_view")) {
    {
      kit::FieldGroup group("fields", "Component");
      int body = std::clamp(s.motionPlotBody, 0, nBodies - 1);
      if (kit::fieldCombo("body", "Component", &body, s.motionBodyLabels,
                          kit::FieldOpts().withTip(
                              "Component whose world-position magnitude is plotted.")))
        s.motionPlotBody = body;
    }
    const int body = std::clamp(s.motionPlotBody, 0, nBodies - 1);
    const std::string &component = s.motionBodyIds[body];
    std::vector<double> xs, trace;
    xs.reserve(nFrames);
    trace.reserve(nFrames);
    const sim::Disp positionUnits = sim::disp("mm", unitSys(st).us);
    for (int fi = 0; fi < nFrames; ++fi) {
      const auto &frameJ = s.motionTrajectory[fi];
      double magnitude = std::numeric_limits<double>::quiet_NaN();
      if (frameJ.contains("bodies") && frameJ["bodies"].is_array())
        for (const auto &bodyJ : frameJ["bodies"]) {
          if (bodyJ.value("component", std::string()) != component)
            continue;
          const auto &transform = bodyJ["transform"];
          if (transform.is_array() && transform.size() >= 15 && transform[12].is_number() &&
              transform[13].is_number() && transform[14].is_number()) {
            const double tx = transform[12].get<double>(), ty = transform[13].get<double>(),
                         tz = transform[14].get<double>();
            magnitude = std::sqrt(tx * tx + ty * ty + tz * tz);
          }
          break;
        }
      xs.push_back(fi + 1);
      trace.push_back(sim::toDisplay(magnitude, positionUnits));
    }
    kit::PlotSpec plot;
    plot.x = xs;
    plot.series.push_back({body < (int)s.motionBodyLabels.size()
                               ? s.motionBodyLabels[body].c_str()
                               : component.c_str(),
                           trace});
    plot.xAxis.title = "Sample";
    plot.xAxis.integer = true;
    plot.xAxis.zero = false;
    plot.yAxis.title = "Position |p|";
    plot.yAxis.unit = positionUnits.unit;
    plot.yAxis.zero = false;
    plot.refs.push_back(kit::PlotRef::v(std::clamp(s.motionFrame, 0, nFrames - 1) + 1.0, nullptr,
                                        kit::Tone::Accent, false));
    plot.legend = false;
    plot.emptyText = "Position samples are unavailable for this component.";
    const kit::PlotHover hover = kit::linePlot("trace_plot", plot);
    if (hover.clicked && hover.index >= 0) {
      s.motionPlaying = false;
      s.motionLastTick = -1.0;
      s.motionFrame = std::clamp(hover.index, 0, nFrames - 1);
      if (applyMotionFrame(st, s, s.motionFrame))
        st.clearSelection();
    }
  }
  kit::cardEnd();
}

} // namespace ui
