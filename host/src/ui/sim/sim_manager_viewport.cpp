// SimManager viewport: face picks into wells, BC preview faces, mesh preview, indicator glyphs.
#include "sim_manager_internal.h"
#include "../property_manager.h"
#include "sim_interaction.h"

#include <cstdio>

#include "engine_backend.h" // engine::daemonMode — server-owned export paths

namespace ui {

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
  if (st.rpc.interactionBusy() || runUi().running || motionRun_ || prepInFlight_ || meshPreviewPending_)
    return false;
  if (st.propertyManager && st.propertyManager->active())
    return false; // PM owns picks first
  if (hydraulic_.isOpen())
    return false; // the Hydraulics panel owns the pane and takes no viewport picks
  if (piping_.isOpen())
    return piping_.wantsFacePick(); // the Piping panel owns the pane (and its picks)
  if (armedWell_ < 0)
    return false;
  if (armedWell_ == kArmFlowDirection)
    return sel_ >= 0 && sel_ < (int)studies_.size() &&
           isFlowFamily(studies_[sel_].type) && node_ == SimNode::Boundaries;
  return sel_ >= 0 && sel_ < (int)studies_.size();
}

bool SimManager::routeFacePick(AppState &st, const nlohmann::json &selector,
                               uint32_t body, uint32_t topo) {
  if (!wantsViewportFacePick(st))
    return false;
  if (selector.value("kind", std::string()) != "face")
    return false;
  if (piping_.isOpen())
    return piping_.routeFacePick(st, selector, body, topo);
  Study &s = studies_[sel_];
  if (armedWell_ == kArmFlowDirection) {
    if (!isFlowFamily(s.type) || node_ != SimNode::Boundaries)
      return false;
    const auto key = (static_cast<uint64_t>(body) << 32) | topo;
    const auto normal = st.faceNormalCache.find(key);
    if (normal == st.faceNormalCache.end())
      return false;
    double length2 = 0.0;
    for (int k = 0; k < 3; ++k)
      length2 += static_cast<double>(normal->second[k]) * normal->second[k];
    if (!std::isfinite(length2) || length2 <= 1e-12) {
      st.toast(ui::ToastLevel::Warn, "This face has no usable normal. Pick another face.");
      return true;
    }
    const double length = std::sqrt(length2);
    for (int k = 0; k < 3; ++k)
      s.flowFaceDirection[k] = static_cast<float>(normal->second[k] / length);
    s.flowUseFaceDirection = true;
    s.flowAoA = 0.0f;
    s.flowSideslip = 0.0f;
    armedWell_ = -1;
    persist(st, s);
    st.toast(ui::ToastLevel::Info, "Flow direction set along the face normal.");
    return true;
  }
  if (s.type == StudyType::Topology) {
    // Topology wells live in the canonical study JSON, addressed by the armed pointer.
    if (armedWell_ != kArmTopology || topoBcView_.armedPointer.empty() ||
        topologyRunActive())
      return false;
    std::size_t count = 0;
    const int changed = topology::toggleFace(s.topo, topoBcView_.armedPointer, selector,
                                             body, topo, &count);
    if (changed == 0) {
      topoBcView_.armedPointer.clear();
      armedWell_ = -1;
      return false;
    }
    persist(st, s);
    st.toast(ui::ToastLevel::Info,
             std::string(changed > 0 ? "Added face (" : "Removed face (") +
                 std::to_string(count) + " in this selection)");
    return true;
  }
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

bool SimManager::routeSetupDrop(AppState& st, const std::string& tool,
                                const nlohmann::json& selector, uint32_t body, uint32_t topo) {
  if (st.activeWorkspace != AppState::Workspace::Simulate || st.simManager != this ||
      !st.docMirror.hasDoc() || sel_ < 0 || sel_ >= static_cast<int>(studies_.size()) ||
      (st.propertyManager && st.propertyManager->active()) || st.rpc.interactionBusy() ||
      runUi().running || motionRun_ || prepInFlight_ || meshPreviewPending_ ||
      piping_.isOpen() || hydraulic_.isOpen() ||
      !sim::validFace(selector, body, topo))
    return false;
  Study& s = studies_[sel_];
  const bool thermal = s.type == StudyType::ThermalFea;
  const bool structural = s.type == StudyType::Static || s.type == StudyType::Fatigue ||
                          s.type == StudyType::Buckling;
  const bool fixed = structural || s.type == StudyType::Modal || s.type == StudyType::FreqResponse;
  const bool fixture = (tool == "fixture" && fixed) || (tool == "temperature" && thermal);
  const bool force = tool == "force" && structural;
  const bool pressure = tool == "pressure" && structural;
  const bool flux = tool == "flux" && thermal;
  const bool convection = tool == "convection" && thermal;
  if (!fixture && !force && !pressure && !flux && !convection) return false;
  const auto key = (static_cast<uint64_t>(body) << 32) | topo;
  if (st.faceNormalCache.find(key) == st.faceNormalCache.end()) return false;
  auto captured = selector;
  captured["_pick"] = {{"body", body}, {"topo", topo}};
  const auto contains = [&](const SimWell& well) {
    return std::any_of(well.picks.begin(), well.picks.end(), [&](const SimSel& pick) {
      return (pick.pickBody == body && pick.pickTopo == topo) || pick.selector == captured;
    });
  };
  bool changed = false;
  if (fixture) {
    if (!contains(s.fixtures)) {
      s.fixtures.picks.push_back({captured, body, topo});
      changed = true;
    }
    node_ = SimNode::Fixtures;
    requestFocus("fixtures");
  } else {
    const auto compatible = [&](const SimLoad& load) {
      if (load.definition.is_object() &&
          ((load.definition.contains("faces") && load.definition["faces"].is_object()) ||
           load.definition.contains("face")))
        return false; // Native resolved faceset constraints are edited in Complete parameters.
      return thermal ? load.thermalKind == (convection ? 1 : 0) : load.pressure == pressure;
    };
    int index = -1;
    // Repeated deliveries to the same face and role are copies, never toggles or new loads.
    for (int i = 0; i < static_cast<int>(s.loads.size()); ++i)
      if (compatible(s.loads[i]) && contains(s.loads[i].faces)) { index = i; break; }
    if (index < 0) {
      if (loadIdx_ >= 0 && loadIdx_ < static_cast<int>(s.loads.size()) && compatible(s.loads[loadIdx_]))
        index = loadIdx_;
      else {
        SimLoad load;
        load.pressure = pressure;
        load.thermalKind = convection ? 1 : 0;
        s.loads.push_back(std::move(load));
        index = static_cast<int>(s.loads.size()) - 1;
      }
      s.loads[index].faces.picks.push_back({captured, body, topo});
      changed = true;
    }
    loadIdx_ = index;
    node_ = SimNode::Load;
    requestFocus("load");
  }
  armedWell_ = -1;
  panelOpen_ = true;
  st.leftPaneTab = AppState::LP_Simulation;
  st.revealSimManager = true;
  if (changed) persist(st, s);
  return true;
}

// ---------------------------------------------------------------- previews

bool SimManager::previewActive(AppState &st) const {
  if (st.leftPaneTab != AppState::LP_Simulation)
    return false;
  if (st.colormapOn)
    return false; // don't stack tints over a result colormap
  if (piping_.isOpen() || hydraulic_.isOpen())
    return false; // the Piping / Hydraulics panel owns the pane (Piping overlays its run polylines)
  return sel_ >= 0 && sel_ < (int)studies_.size();
}

void SimManager::collectFixtureFaces(std::vector<uint64_t> &keys) const {
  if (sel_ < 0 || sel_ >= (int)studies_.size())
    return;
  if (studies_[sel_].type == StudyType::Topology) {
    topology::collectFaceKeys(studies_[sel_].topo, topology::WellRole::Support, keys);
    return;
  }
  for (const auto &p : studies_[sel_].fixtures.picks)
    if (p.pickBody && p.pickTopo)
      keys.push_back(((uint64_t)p.pickBody << 32) | (uint64_t)p.pickTopo);
}

void SimManager::collectLoadFaces(std::vector<uint64_t> &keys) const {
  if (sel_ < 0 || sel_ >= (int)studies_.size())
    return;
  if (studies_[sel_].type == StudyType::Topology) {
    topology::collectFaceKeys(studies_[sel_].topo, topology::WellRole::Load, keys);
    return;
  }
  for (const auto &ld : studies_[sel_].loads)
    for (const auto &p : ld.faces.picks)
      if (p.pickBody && p.pickTopo)
        keys.push_back(((uint64_t)p.pickBody << 32) | (uint64_t)p.pickTopo);
}

void SimManager::collectTopologyPreview(std::vector<uint64_t> &preserveFaces,
                                        std::vector<uint64_t> &excludeFaces,
                                        std::vector<uint64_t> &portFaces,
                                        std::vector<float> &preserveBoxSegs,
                                        std::vector<float> &excludeBoxSegs) const {
  if (sel_ < 0 || sel_ >= (int)studies_.size() ||
      studies_[sel_].type != StudyType::Topology)
    return;
  const nlohmann::json &j = studies_[sel_].topo;
  topology::collectFaceKeys(j, topology::WellRole::Preserve, preserveFaces);
  topology::collectFaceKeys(j, topology::WellRole::Exclude, excludeFaces);
  topology::collectFaceKeys(j, topology::WellRole::Port, portFaces);
  topology::collectFaceKeys(j, topology::WellRole::Wall, portFaces);
  topology::collectRegionBoxSegments(j, true, preserveBoxSegs);
  topology::collectRegionBoxSegments(j, false, excludeBoxSegs);
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
  // Export + sim_mesh are long (up to 180s). AsyncRpc keeps the UI
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

namespace {
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

} // namespace ui
