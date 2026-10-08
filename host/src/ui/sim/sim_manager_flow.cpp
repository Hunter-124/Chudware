// SimManager flow (CFD): flow setup/result line overlays, flow run dispatch and the flow form.
#include "sim_manager_internal.h"

#include <algorithm>
#include <cmath>
#include <cstring>
#include <limits>
#include <filesystem>
#include <string>
#include <vector>

#include "../../core/sim_client.h"
#include "util/aicm_handoff.h" // is_shm_uri — overlay paths are aicm-shm:// handoff URIs, not files

namespace ui {

// Compose the incoming-flow unit vector from a study's base axis + AoA +
// sideslip (defined below; used by both the setup form readout and the viewport
// setup preview).
static void flowIncomingDir(const Study &s, float out[3]);

namespace {
struct FlowResolution {
  int cells;
  int steps;
};
// Standard matches FlowParams / run_cfd_flow. Quick reduces each grid axis
// to about 60% and the iteration budget to one third; Accurate refines both.
constexpr FlowResolution kFlowFidelity[] = {{58, 667}, {96, 2000}, {144, 3000}};

FlowResolution flowResolution(const Study &s) {
  static constexpr FlowResolution legacy[] = {
      {48, 1500}, {64, 3000}, {96, 6000}, {128, 10000}};
  FlowResolution r = legacy[std::clamp(s.flowPreset, 0, 3)];
  const auto flow = s.definition.find("flow");
  const auto &native = flow != s.definition.end() && flow->is_object()
                           ? *flow : s.definition;
  auto parameter = [&](const char *key, int fallback) {
    const auto value = native.find(key);
    if (value != native.end())
      return value->is_number_integer() ? value->get<int>() : fallback;
    const auto root = s.definition.find(key);
    if (root != s.definition.end() && root->is_number_integer())
      return root->get<int>();
    return fallback;
  };
  r.cells = parameter("cells", r.cells);
  r.steps = parameter("n_steps", r.steps);
  return r;
}
} // namespace

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

namespace {

// ---------------------------------------------------------------- flow setup
// preview + result lines

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
// Push a rectangle outline (centre c, in-plane axes u/v, half-extents hu/hv):
// the inlet / outlet plane marker. Outline only — no fill, no diagonals.
void pushPlaneMarker(std::vector<float> &s, const float c[3], const float u[3],
                     const float v[3], float hu, float hv) {
  float q[4][3];
  for (int k = 0; k < 3; ++k) {
    q[0][k] = c[k] + u[k] * hu + v[k] * hv;
    q[1][k] = c[k] - u[k] * hu + v[k] * hv;
    q[2][k] = c[k] - u[k] * hu - v[k] * hv;
    q[3][k] = c[k] + u[k] * hu - v[k] * hv;
  }
  for (int i = 0; i < 4; ++i)
    pushSeg3(s, q[i], q[(i + 1) % 4]);
}

// World bounds of the loaded model: the per-body vertex boxes (true extents),
// else the face-centroid cache. False when no geometry is loaded — the setup
// preview then draws nothing rather than a made-up box.
bool flowModelBounds(const AppState &st, float lo[3], float hi[3]) {
  for (int k = 0; k < 3; ++k) {
    lo[k] = 1e30f;
    hi[k] = -1e30f;
  }
  bool any = false;
  for (const auto &kv : st.bodyBBoxCache) {
    if (kv.second[0] > kv.second[3])
      continue; // empty body
    for (int k = 0; k < 3; ++k) {
      lo[k] = std::min(lo[k], kv.second[k]);
      hi[k] = std::max(hi[k], kv.second[3 + k]);
    }
    any = true;
  }
  if (any)
    return true;
  for (const auto &kv : st.faceNormalCache) {
    for (int k = 0; k < 3; ++k) {
      lo[k] = std::min(lo[k], kv.second[3 + k]);
      hi[k] = std::max(hi[k], kv.second[3 + k]);
    }
    any = true;
  }
  return any;
}
} // namespace

bool SimManager::flowSetupActive(AppState &st) const {
  if (hydraulic_.isOpen() || piping_.isOpen())
    return false; // Industrial panels draw through the simulation scene seam.
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
                                  std::vector<float> &inletSegs,
                                  std::vector<float> *arrowTris) const {
  if (sel_ < 0 || sel_ >= (int)studies_.size())
    return;
  const Study &s = studies_[sel_];
  // Model bbox / centre / scale (no geometry loaded -> no preview).
  float lo[3], hi[3];
  if (!flowModelBounds(st, lo, hi))
    return;
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

  // The solver's wind-tunnel domain (several model lengths each way) is far
  // larger than the framed model, so the preview only annotates the model:
  // inlet / outlet plane OUTLINES just clear of the part along the stream and
  // sized to its cross-section, plus a short 3x3 arrow grid entering the inlet
  // plane. Everything stays within ~1.2 model radii of the centre, i.e. inside
  // the camera's fit of the model, so nothing runs off into the scissor.
  const float half[3] = {0.5f * (hi[0] - lo[0]), 0.5f * (hi[1] - lo[1]),
                         0.5f * (hi[2] - lo[2])};
  auto extentAlong = [&](const float a[3]) {
    return std::fabs(a[0]) * half[0] + std::fabs(a[1]) * half[1] +
           std::fabs(a[2]) * half[2];
  };
  const float gap = 0.06f * r, margin = 0.04f * r;
  const float planeDist = extentAlong(f) + gap;
  const float hRgt = std::max(extentAlong(rgt) + margin, 0.12f * r);
  const float hUp = std::max(extentAlong(upv) + margin, 0.12f * r);

  // Inlet plane ahead of the part, outlet plane behind it (same batch, drawn in
  // the flow-inlet colour): together they say where flow enters and leaves.
  float inC[3], outC[3];
  for (int k = 0; k < 3; ++k) {
    inC[k] = ctr[k] - f[k] * planeDist;
    outC[k] = ctr[k] + f[k] * planeDist;
  }
  pushPlaneMarker(inletSegs, inC, rgt, upv, hRgt, hUp);
  pushPlaneMarker(inletSegs, outC, rgt, upv, hRgt, hUp);

  // Flow arrows: a 3x3 grid whose tips touch the inlet plane, so they never
  // pierce the part. The length tracks the inlet speed (a fast stream reads a
  // little longer than a slow one) around a nominal ~20 m/s, clamped short.
  const float spd = std::max(0.0f, (float)s.flowSpeed);
  const float aLen = (0.08f + 0.07f * std::min(spd / 20.0f, 1.4f)) * r;
  const float headLen = std::min(0.35f * aLen, 0.05f * r);
  for (int iu = -1; iu <= 1; ++iu)
    for (int iv = -1; iv <= 1; ++iv) {
      float tail[3];
      for (int k = 0; k < 3; ++k)
        tail[k] = inC[k] - f[k] * aLen + rgt[k] * (0.6f * hRgt * iu) +
                  upv[k] * (0.6f * hUp * iv);
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

void SimManager::runFlow(AppState &st, Study &s) {
  if (!st.simClient || !st.docMirror.hasDoc())
    return;
  auto prep = std::make_shared<StaticModalPrep>();
  prep->type = s.type;
  // Completion compares the frozen setup/source against the live document for
  // every study family. Flow skips structural face preparation, so freeze its
  // identity here too; otherwise a valid flow run is discarded as stale.
  prep->canonical = studyToJson(s);
  prep->sourceGeometryKey = topologyGeometryKey(st);
  prep->sourceMutationGen = st.docMirror.mutationGen();
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
  prep->flowParams = {
      {"stl_path", ""},
      {"direction", s.flowUseFaceDirection
                        ? nlohmann::json::array({s.flowFaceDirection[0],
                                                s.flowFaceDirection[1],
                                                s.flowFaceDirection[2]})
                        : nlohmann::json::array({dirs[di][0], dirs[di][1], dirs[di][2]})},
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
      {"overlay", true},
  };
  // Thermal (heat-transfer) study — OpenFOAM only.
  if (s.flowSolver == 1 && s.flowThermal) {
    if (s.flowThermalType == 1) {
      // Conjugate heat transfer: solid part conducts an internal power
      // dissipation.
      prep->flowParams["thermal"] = {
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
      prep->flowParams["thermal"] = {
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
  // Native LBM resolution overrides are retained by the canonical editor rather
  // than represented by the grid preset. Forward them without changing derived
  // geometry/identity or pretending unknown retained keys are solver options.
  const auto flow = s.definition.find("flow");
  const nlohmann::json &native = flow != s.definition.end() && flow->is_object()
                                     ? *flow
                                     : s.definition;
  for (const char *key : {"cells", "n_steps"}) {
    const auto value = native.find(key);
    if (value != native.end()) {
      prep->flowParams[key] = *value;
    } else {
      const auto rootValue = s.definition.find(key);
      if (rootValue != s.definition.end())
        prep->flowParams[key] = *rootValue;
    }
  }
  beginExportPrep(st, s, prep);
}

// ============================================================ setup form

namespace {

// OpenFOAM availability as reported by the flow backend's capability probe.
// `known` is false until the probe has answered (caps() returns {} while a job
// holds the backend), so an unanswered probe never reads as "unavailable".
struct FlowCaps {
  bool known = false;
  bool openfoam = false;
  std::string version;
};

FlowCaps flowCaps(AppState &st) {
  FlowCaps fc;
  if (!st.simClient)
    return fc;
  const nlohmann::json caps = st.simClient->caps();
  if (!caps.is_object() || !caps.contains("openfoam"))
    return fc;
  fc.known = true;
  fc.openfoam = caps.value("openfoam", false);
  fc.version = caps.value("openfoam_version", std::string());
  return fc;
}

// Unit-vector component for display: float noise below the shown precision
// reads as 0, never as an engineering-prefixed "-43.7 n".
std::string fmtDirComponent(float v) {
  return kit::fmtValue(std::fabs(v) < 5e-4f ? 0.0 : (double)v, 3);
}

// Preset gestures copy only one explicitly chosen setup property. Freeze origin
// ownership at drag start so document/study navigation cannot retarget the payload.
constexpr const char *kFlowPresetPayload = "CHUDWARE_FLOW_PRESET";

void flowPresetSource(const std::string &docId, const Study &s,
                      const char *kind, int index, const char *label) {
  if (docId.empty() || s.id.empty() || !ImGui::BeginDragDropSource())
    return;
  const std::string data = nlohmann::json({{"doc", docId}, {"study", s.id},
                                         {"kind", kind}, {"index", index}}).dump();
  ImGui::SetDragDropPayload(kFlowPresetPayload, data.c_str(), data.size() + 1,
                           ImGuiCond_Once);
  kit::dimText(label);
  ImGui::EndDragDropSource();
}

bool flowPresetDrop(const std::string &docId, const Study &s,
                    const char *kind, int count, int &index) {
  if (!ImGui::BeginDragDropTarget())
    return false;
  bool accepted = false;
  if (const ImGuiPayload *p = ImGui::AcceptDragDropPayload(kFlowPresetPayload)) {
    if (p->IsDelivery() && p->Data && p->DataSize > 1 && p->DataSize <= 4096) {
      const char *data = static_cast<const char *>(p->Data);
      if (data[p->DataSize - 1] == '\0' &&
          std::strlen(data) == static_cast<std::size_t>(p->DataSize - 1)) {
        const auto value = nlohmann::json::parse(data, nullptr, false);
        if (value.is_object() && value.contains("doc") &&
            value["doc"].is_string() && value["doc"] == docId &&
            !docId.empty() && value.contains("study") &&
            value["study"].is_string() && value["study"] == s.id &&
            !s.id.empty() && value.contains("kind") &&
            value["kind"].is_string() && value["kind"] == kind &&
            value.contains("index") && value["index"].is_number_integer()) {
          const auto candidate = value["index"].get<std::int64_t>();
          if (candidate >= 0 && candidate < count) {
            index = static_cast<int>(candidate);
            accepted = true;
          }
        }
      }
    }
  }
  ImGui::EndDragDropTarget();
  return accepted;
}

// Mechanical drag input in the kit's field layout; exact entries remain separate
// kit number fields. Live values drive the existing viewport preview, while only
// release commits a study edit. No slider-derived speed ceiling is imposed.
bool flowAdjust(const char *id, const char *label, float *value, float step,
                const kit::FieldOpts &options = kit::FieldOpts()) {
  ImGuiContext &g = *GImGui;
  const float width = kit::fieldBegin(id, label, options);
  ImGui::SetNextItemWidth(width);
  const float lo = static_cast<float>(std::max(
      options.minV, -static_cast<double>(std::numeric_limits<float>::max())));
  const float hi = static_cast<float>(std::min(
      options.maxV, static_cast<double>(std::numeric_limits<float>::max())));
  const ImGuiID itemId = ImGui::GetID("##adjust");
  ImGuiStorage *storage = ImGui::GetStateStorage();
  const float before = *value;
  ImGui::DragFloat("##adjust", value, step, lo, hi, "%.4g",
                   ImGuiSliderFlags_AlwaysClamp | ImGuiSliderFlags_NoInput);
  IMGUI_TEST_ENGINE_ITEM_INFO(GImGui->LastItemData.ID, label,
                              GImGui->LastItemData.StatusFlags);
  if (ImGui::IsItemActivated())
    storage->SetFloat(itemId, before);
  const bool cancelled = ImGui::IsItemActive() &&
                         ImGui::IsKeyPressed(ImGuiKey_Escape);
  if (cancelled)
    *value = storage->GetFloat(itemId, before);
  const bool committed = !cancelled && ImGui::IsItemDeactivatedAfterEdit() &&
                         *value != storage->GetFloat(itemId, before);
  kit::fieldEnd();
  return committed;
}

} // namespace

// Thermal CFD uses the OpenFOAM heat-transfer pipeline. Capability checks never
// rewrite a saved solver choice: unavailable studies stay editable and Run is
// refused by the shared run blocker instead of silently becoming another solve.
void SimManager::syncFlowStudy(AppState &st, Study &s) {
  (void)st;
  if (s.type == StudyType::Thermal) {
    s.flowSolver = 1;
    s.flowThermal = true;
  }
}

// Compose the incoming-flow unit vector from a base axis + AoA + sideslip (Z-up
// world). Mirrors the solver's construction so the setup preview arrow and the
// readout match what gets solved.
static void flowIncomingDir(const Study &s, float out[3]) {
  static const float kAxis[6][3] = {{1, 0, 0},  {-1, 0, 0}, {0, 1, 0},
                                    {0, -1, 0}, {0, 0, 1},  {0, 0, -1}};
  int di = (s.flowDir < 0 || s.flowDir > 5) ? 0 : s.flowDir;
  float f0[3] = {kAxis[di][0], kAxis[di][1], kAxis[di][2]};
  if (s.flowUseFaceDirection)
    std::copy(s.flowFaceDirection, s.flowFaceDirection + 3, f0);
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

// Wind-tunnel / thermal-CFD setup, one page per setup sub-chip (node_ picks it;
// the stepper chips and the Flow-tab ribbon commands navigate between them):
//   SimNode::Boundaries -> Boundaries: Flow (axis, AoA, sideslip, speed + the
//                          resolved stream / Reynolds chips), Reference, Heat
//   SimNode::Mesh       -> Resolution: Grid + Solver
//   anything else       -> Fluid: Fluid + Surface (the Study node lands here)
// Every edit commits through the kit field and persists; the solver/thermal
// normalisation lives in syncFlowStudy, never here.
void SimManager::drawFlowForm(AppState &st, Study &s) {
  static_assert(kNFluids == 3, "fluid labels below assume Air, Water, Custom");
  static_assert(kNRoughness == 6, "roughness labels below assume 5 presets + Custom");
  using kit::FieldOpts;
  using kit::Unit;

  const bool thermalStudy = (s.type == StudyType::Thermal);
  const FlowCaps fc = flowCaps(st);
  const bool ofAvail = fc.known && fc.openfoam;

  bool changed = false;
  int fidelityEdit = -1;
  const std::string docId = st.docMirror.hasDoc() ? st.docMirror.docId() : "";
  ImGui::PushID(docId.c_str());
  ImGui::PushID(s.id.c_str());
  if ((thermalStudy || s.flowSolver == 1) && fc.known && !fc.openfoam)
    kit::inlineStatus("OpenFOAM is unavailable. Setup remains editable; running "
                      "this configuration is blocked.", kit::Tone::Warn);

  // ======================================================== Fluid page
  auto fluidPage = [&]() {
    // ---- Fluid: preset (read-only properties) or custom density + viscosity --
    takeFocus("fluid");
    if (kit::cardBegin("fluid", "Fluid", "fluid")) {
      kit::FieldGroup g("fields", "Kinematic viscosity");
      static const char *const kFluidLabels[3] = {"Air", "Water", "Custom"};
      const std::string airTip =
          "Air at sea level, 15\xc2\xb0" "C: density " +
          kit::fmtQuantity(kFluids[0].density, kit::unitText(Unit::kg_m3)) +
          ", kinematic viscosity " +
          kit::fmtQuantity(kFluids[0].visc * 1e6, kit::unitText(Unit::cSt)) + ".";
      const std::string waterTip =
          "Fresh water at 20\xc2\xb0" "C: density " +
          kit::fmtQuantity(kFluids[1].density, kit::unitText(Unit::kg_m3)) +
          ", kinematic viscosity " +
          kit::fmtQuantity(kFluids[1].visc * 1e6, kit::unitText(Unit::cSt)) +
          ". About 815 times the density and 15 times the Reynolds number of "
          "air at the same speed and size: pick it for hull / hydrodynamic "
          "studies.";
      const char *const fluidTips[3] = {
          airTip.c_str(), waterTip.c_str(),
          "Enter the density and kinematic viscosity of your fluid."};
      int fi = (s.flowFluid < 0 || s.flowFluid >= kNFluids) ? 0 : s.flowFluid;
      bool applyPreset = false;
      const bool fluidHelpers = kit::popoverBegin("fluid_helpers", "Fluid presets",
                                                  "fluid");
      if (fluidHelpers) {
        kit::dimText("Click to apply, or drag to Drop fluid preset.");
        for (int i = 0; i < kNFluids - 1; ++i) {
          ImGui::PushID(i);
          if (kit::secondaryButton(kFluidLabels[i])) {
            fi = i;
            applyPreset = true;
          }
          flowPresetSource(docId, s, "fluid", i, kFluidLabels[i]);
          kit::itemTooltip(fluidTips[i]);
          ImGui::PopID();
        }
        kit::popoverEnd();
      }
      kit::secondaryButton("Drop fluid preset###fluid_drop", true, nullptr,
                           ImVec2(ImGui::GetContentRegionAvail().x, 0));
      applyPreset |= flowPresetDrop(docId, s, "fluid", kNFluids - 1, fi);
      if (kit::fieldEnum("medium", "Medium", &fi, kFluidLabels, kNFluids,
                         FieldOpts().withItemTips(fluidTips).withTip(
                             "Working fluid around the part.")) || applyPreset) {
        s.flowFluid = fi;
        // Preset selection changes the working medium, not the retained Custom
        // recipe. Custom density/viscosity survive a round trip through Air/Water.
        changed = true;
      }
      static const char *const kViscTip =
          "Dynamic viscosity divided by density, in centistokes (1 cSt = "
          "1 mm\xc2\xb2/s). Air ~15, water ~1.";
      // One boxed field shape for every medium: a preset shows its library
      // values in the same number fields as Custom (never bare readouts), and
      // editing either value switches the medium to Custom.
      const bool custom = fi == kNFluids - 1;
      float density = custom ? s.flowDensity : kFluids[fi].density;
      float visc = custom ? s.flowViscosity : kFluids[fi].visc;
      const std::string presetTip =
          std::string(kFluidLabels[fi]) +
          " preset value. Editing it switches Medium to Custom.";
      bool edited = false;
      if (kit::fieldNumber(
              "density", "Density", &density,
              FieldOpts()
                  .withUnit(Unit::kg_m3)
                  .withMin(0.0)
                  .withRequired(custom)
                  .withTip(custom ? "Mass density of the fluid." : presetTip.c_str())
                  .withIssue(density > 0.0f ? nullptr
                                            : "Density must be greater than zero.")))
        edited = true;
      if (kit::fieldNumber(
              "viscosity", "Kinematic viscosity", &visc,
              FieldOpts()
                  .withUnit(Unit::cSt)
                  .withScale(1e6)
                  .withMin(0.0)
                  .withRequired(custom)
                  .withTip(custom ? kViscTip : presetTip.c_str())
                  .withIssue(visc > 0.0f ? nullptr
                                         : "Kinematic viscosity must be greater "
                                           "than zero.")))
        edited = true;
      if (edited) {
        s.flowFluid = kNFluids - 1;
        s.flowDensity = density;
        s.flowViscosity = visc;
        changed = true;
      }
      if (!custom) {
        const std::string from =
            std::string("Values from the ") + kFluidLabels[fi] + " preset.";
        kit::dimText(from.c_str());
      }
    }
    kit::cardEnd();

    // ---- Surface: roughness preset, or custom equivalent sand-grain Ks ----
    if (kit::cardBegin("surface", "Surface", "appr_paint")) {
      kit::FieldGroup g("fields", "Roughness height");
      const char *roughLabels[kNRoughness];
      std::string roughTipText[kNRoughness];
      const char *roughTips[kNRoughness];
      for (int i = 0; i < kNRoughness; ++i) {
        const bool custom = (i == kNRoughness - 1);
        roughLabels[i] = custom ? "Custom" : kRoughness[i].label;
        roughTipText[i] =
            custom ? std::string("Enter your own equivalent sand-grain roughness.")
                   : "Ks " + kit::fmtQuantity(kRoughness[i].ks_um,
                                              kit::unitText(Unit::um));
        roughTips[i] = roughTipText[i].c_str();
      }
      int ri = (s.flowRoughness < 0 || s.flowRoughness >= kNRoughness)
                   ? 0
                   : s.flowRoughness;
      bool applyPreset = false;
      const bool surfaceHelpers = kit::popoverBegin("surface_helpers",
                                                    "Surface presets",
                                                    "appr_paint");
      if (surfaceHelpers) {
        kit::dimText("Click to apply, or drag to Drop roughness preset.");
        for (int i = 0; i < kNRoughness - 1; ++i) {
          ImGui::PushID(i);
          kit::RowStyle row;
          row.meta = roughTipText[i].c_str();
          if (kit::row("preset", roughLabels[i], row)) {
            ri = i;
            applyPreset = true;
          }
          flowPresetSource(docId, s, "roughness", i, roughLabels[i]);
          ImGui::PopID();
        }
        kit::popoverEnd();
      }
      kit::secondaryButton("Drop roughness preset###roughness_drop", true, nullptr,
                           ImVec2(ImGui::GetContentRegionAvail().x, 0));
      applyPreset |= flowPresetDrop(docId, s, "roughness", kNRoughness - 1, ri);
      if (kit::fieldEnum("roughness", "Roughness", &ri, roughLabels, kNRoughness,
                         FieldOpts().withItemTips(roughTips).withTip(
                             "Equivalent sand-grain roughness of the wetted "
                             "surface. Rougher skin raises the friction drag: "
                             "significant for hulls and large surfaces "
                             "(qualitative correction).")) || applyPreset) {
        s.flowRoughness = ri;
        changed = true;
      }
      // Same shape as the fluid card: presets show their Ks in the number
      // field; editing it switches Roughness to Custom.
      const bool customKs = ri == kNRoughness - 1;
      float ks = customKs ? s.flowRoughnessUm : kRoughness[ri].ks_um;
      const std::string ksTip =
          customKs ? std::string("Equivalent sand-grain roughness height.")
                   : std::string(roughLabels[ri]) +
                         " preset value. Editing it switches Roughness to Custom.";
      if (kit::fieldNumber("ks", "Roughness height", &ks,
                           FieldOpts()
                               .withUnit(Unit::um)
                               .withMin(0.0)
                               .withTip(ksTip.c_str()))) {
        s.flowRoughness = kNRoughness - 1;
        s.flowRoughnessUm = ks;
        changed = true;
      }
    }
    kit::cardEnd();
  };

  // ===================================================== Boundaries page
  auto boundariesPage = [&]() {
    // ---- Flow: base axis + angle of attack + sideslip + free-stream speed ----
    // Saved thermal fields remain accessible without an installed backend.
    const bool heatCard = thermalStudy || s.flowSolver == 1 || s.flowThermal;
    if (!heatCard)
      takeFocus("heat");
    takeFocus("flow");
    if (kit::cardBegin("flow", "Flow", "flow_arrow")) {
      const bool pickingDirection = armedWell_ == kArmFlowDirection;
      const bool canPick = !runUi().running && !st.rpc.interactionBusy() &&
                           !meshPreviewPending_;
      if (kit::secondaryButton(
              pickingDirection ? "Cancel face pick###pick_inlet"
                               : "Pick inlet face###pick_inlet",
              canPick, canPick ? nullptr : "Wait for the active operation to finish.",
              ImVec2(ImGui::GetContentRegionAvail().x, 0))) {
        armedWell_ = pickingDirection ? -1 : kArmFlowDirection;
      }
      kit::itemTooltip("Click a model face to flow along its normal. "
                       "Resets angle of attack and sideslip to zero.");
      if (armedWell_ == kArmFlowDirection)
        kit::inlineStatus("Click an inlet face in the viewport.", kit::Tone::Info);
      if (s.flowUseFaceDirection)
        kit::dimText("Base direction comes from the picked face normal. "
                     "Choose an axis below to replace it.");
      {
        kit::FieldGroup g("fields", "Angle of attack");
        static const char *const kDirLabels[6] = {"+X", "-X", "+Y",
                                                  "-Y", "+Z", "-Z"};
        int dir = s.flowUseFaceDirection ? -1
                     : ((s.flowDir < 0 || s.flowDir > 5) ? 0 : s.flowDir);
        kit::dimText("Choose a base axis; angles remain unchanged.");
        for (int i = 0; i < 6; ++i) {
          if (i % 3 != 0)
            ImGui::SameLine();
          ImGui::PushID(i);
          bool selected = dir == i;
          if (kit::toggleChip(kDirLabels[i], &selected)) {
            if (dir != i) {
              dir = i;
              s.flowDir = dir;
              s.flowUseFaceDirection = false;
              armedWell_ = -1;
              changed = true;
            }
          }
          {
            ImGuiContext &g = *GImGui;
            IMGUI_TEST_ENGINE_ITEM_INFO(g.LastItemData.ID, kDirLabels[i],
                                        g.LastItemData.StatusFlags);
          }
          flowPresetSource(docId, s, "direction", i, kDirLabels[i]);
          ImGui::PopID();
        }
        kit::secondaryButton("Drop direction preset###direction_drop", true,
                             nullptr, ImVec2(ImGui::GetContentRegionAvail().x, 0));
        if (flowPresetDrop(docId, s, "direction", 6, dir) &&
            (dir != s.flowDir || s.flowUseFaceDirection)) {
          s.flowDir = dir;
          s.flowUseFaceDirection = false;
          armedWell_ = -1;
          changed = true;
        }
        if (kit::fieldEnum("dir", "Direction", &dir, kDirLabels, 6,
                           FieldOpts().withHint("Picked face normal").withTip(
                               "World axis the fluid flows toward. Angle of "
                               "attack and sideslip tilt the stream off this "
                               "axis, so you can sweep a fuselage or hull "
                               "without re-orienting the part."))) {
          s.flowDir = dir;
          s.flowUseFaceDirection = false;
          armedWell_ = -1;
          changed = true;
        }
        changed |= flowAdjust("adjust_aoa", "Adjust AoA", &s.flowAoA, 0.1f,
                              FieldOpts().withUnit(Unit::deg).withRange(-30, 30)
                                  .withTip("Drag to pitch the stream; release "
                                           "to save. Exact entry below."));
        if (kit::fieldNumber("aoa", "Angle of attack", &s.flowAoA,
                             FieldOpts()
                                 .withUnit(Unit::deg)
                                 .withRange(-30.0, 30.0)
                                 .withTip("Pitch of the incoming stream "
                                          "relative to the base axis (about "
                                          "the side axis). Positive tilts the "
                                          "flow toward world +Z. Range -30 to "
                                          "30\xc2\xb0.")))
          changed = true;
        changed |= flowAdjust("adjust_sideslip", "Adjust sideslip",
                              &s.flowSideslip, 0.1f,
                              FieldOpts().withUnit(Unit::deg).withRange(-30, 30)
                                  .withTip("Drag to yaw the stream; release "
                                           "to save. Exact entry below."));
        if (kit::fieldNumber("sideslip", "Sideslip", &s.flowSideslip,
                             FieldOpts()
                                 .withUnit(Unit::deg)
                                 .withRange(-30.0, 30.0)
                                 .withTip("Yaw of the incoming stream relative "
                                          "to the base axis (about the up "
                                          "axis). Range -30 to 30\xc2\xb0.")))
          changed = true;
        changed |= flowAdjust("adjust_speed", "Adjust speed", &s.flowSpeed, 0.1f,
                              FieldOpts().withUnit(Unit::m_s).withMin(0)
                                  .withTip("Drag to change speed without a "
                                           "preset ceiling. Exact entry below."));
        if (kit::fieldNumber(
                "speed", "Speed", &s.flowSpeed,
                FieldOpts()
                    .withUnit(Unit::m_s)
                    .withMin(0.0)
                    .withRequired()
                    .withTip("Free-stream speed of the fluid.")
                    .withIssue(s.flowSpeed > 0.0f
                                   ? nullptr
                                   : "Speed must be greater than zero.")))
          changed = true;
      }
      // Derived readouts as one compact chip line: the resolved stream vector
      // and a Reynolds estimate off the model's largest bounding-box extent.
      float dir[3];
      flowIncomingDir(s, dir);
      float density = 0.0f, visc = 0.0f;
      flowFluidProps(s, density, visc);
      const std::string stream = "X " + fmtDirComponent(dir[0]) +
                                 " \xc2\xb7 Y " + fmtDirComponent(dir[1]) +
                                 " \xc2\xb7 Z " + fmtDirComponent(dir[2]);
      // Reynolds is shown only when it can be estimated (geometry loaded, speed
      // and viscosity set) — never as a bare dash.
      std::string re;
      float lo[3], hi[3];
      if (flowModelBounds(st, lo, hi) && visc > 0.0f && s.flowSpeed > 0.0f) {
        const double Lc = std::max({hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2]}) *
                          1e-3; // mm -> m
        if (Lc > 0.0)
          re = "~" + kit::fmtValue((double)s.flowSpeed * Lc / visc, 3);
      }
      kit::chipFlowBegin("derived");
      kit::flowValueChip("Stream", stream.c_str(), kit::Tone::Neutral,
                         "Resolved incoming-flow direction: the base axis "
                         "tilted by angle of attack and sideslip.");
      const std::string speed = kit::fmtQuantity(s.flowSpeed,
                                                 kit::unitText(Unit::m_s));
      kit::flowValueChip("Speed", speed.c_str());
      if (!re.empty())
        kit::flowValueChip("Reynolds", re.c_str(), kit::Tone::Neutral,
                           "Reynolds number estimate from the free-stream speed, "
                           "the fluid viscosity and the model's largest extent.");
      kit::chipFlowEnd();
    }
    kit::cardEnd();

    // ---- Reference: coefficient normalisation area ----
    if (kit::cardBegin("reference", "Reference", "measure")) {
      kit::FieldGroup g("fields", "Reference area");
      bool refSet = s.flowRefAreaMm2 > 0.0f;
      if (kit::fieldNumber(
              "area", "Reference area", &s.flowRefAreaMm2,
              FieldOpts()
                  .withUnit(Unit::mm2)
                  .withMin(0.0)
                  .withPresent(&refSet)
                  .withHint("Auto")
                  .withTip("Area used to non-dimensionalise Cd and Cl. Auto "
                           "(empty) uses the model's projected frontal area; "
                           "set the planform (wing) or wetted area to match a "
                           "published coefficient convention."))) {
        if (!refSet)
          s.flowRefAreaMm2 = 0.0f;
        changed = true;
      }
    }
    kit::cardEnd();

    // ---- Heat transfer: opt-in for Flow + OpenFOAM; always on for Thermal --
    if (!heatCard)
      return;
    takeFocus("heat");
    if (kit::cardBegin("heat", "Heat transfer", "load_heat")) {
      kit::FieldGroup g("fields", "Solid conductivity");
      if (!thermalStudy &&
          kit::fieldCheckbox("enabled", "Solve heat transfer", &s.flowThermal,
                             FieldOpts().withTip(
                                 "Solve the energy equation "
                                 "(buoyantSimpleFoam) for a heated part: "
                                 "surface temperature, the thermal plume and "
                                 "total wall heat flow. Applied to the whole "
                                 "part surface.")))
        changed = true;
      if (thermalStudy || s.flowThermal) {
        static const char *const kModelLabels[2] = {"Fluid", "Conjugate (CHT)"};
        static const char *const kModelTips[2] = {
            "A thermal boundary condition on the part surface heats the fluid.",
            "The solid part conducts heat from an internal power dissipation "
            "and the fluid cools it: the right model for electronics / "
            "heat-sink cooling. Meshes a solid region and takes a few minutes."};
        int tt = (s.flowThermalType == 1) ? 1 : 0;
        if (kit::fieldEnum("model", "Thermal model", &tt, kModelLabels, 2,
                           FieldOpts().withItemTips(kModelTips).withTip(
                               "How heat moves between the part and the "
                               "fluid."))) {
          s.flowThermalType = tt;
          changed = true;
        }
        changed |= flowAdjust("adjust_inlet_t", "Adjust inlet",
                              &s.flowInletTempC, 0.1f,
                              FieldOpts().withUnit(Unit::DegC));
        if (kit::fieldNumber("inlet_t", "Inlet temperature", &s.flowInletTempC,
                             FieldOpts().withUnit(Unit::DegC).withTip(
                                 "Free-stream / ambient fluid temperature.")))
          changed = true;
        if (tt == 1) {
          changed |= flowAdjust("adjust_kappa", "Adjust conductivity",
                                &s.flowSolidKappa, 1.0f,
                                FieldOpts().withUnit(Unit::W_mK).withMin(0));
          if (kit::fieldNumber(
                  "kappa", "Solid conductivity", &s.flowSolidKappa,
                  FieldOpts()
                      .withUnit(Unit::W_mK)
                      .withMin(0.0)
                      .withRequired()
                      .withTip("Thermal conductivity of the part material. "
                               "~200 aluminium, ~50 steel, ~0.2 plastic.")
                      .withIssue(s.flowSolidKappa > 0.0f
                                     ? nullptr
                                     : "Conductivity must be greater than "
                                       "zero.")))
            changed = true;
          changed |= flowAdjust("adjust_power", "Adjust power",
                                &s.flowPowerW, 1.0f,
                                FieldOpts().withUnit(Unit::W).withMin(0));
          if (kit::fieldNumber(
                  "power", "Dissipated power", &s.flowPowerW,
                  FieldOpts()
                      .withUnit(Unit::W)
                      .withMin(0.0)
                      .withRequired()
                      .withTip("Total heat generated inside the solid part "
                               "(e.g. a component's power draw).")
                      .withIssue(s.flowPowerW > 0.0f
                                     ? nullptr
                                     : "Dissipated power must be greater "
                                       "than zero.")))
            changed = true;
        } else {
          static const char *const kWallLabels[3] = {"Temperature",
                                                     "Heat flux", "Power"};
          static const char *const kWallTips[3] = {
              "Hold the whole part surface at a fixed temperature.",
              "Drive a uniform heat flux through the part surface.",
              "Dissipate a fixed total power across the whole surface."};
          int wm =
              (s.flowWallMode < 0 || s.flowWallMode > 2) ? 2 : s.flowWallMode;
          if (kit::fieldEnum("wall_mode", "Wall condition", &wm, kWallLabels, 3,
                             FieldOpts().withItemTips(kWallTips).withTip(
                                 "How the part heats the fluid."))) {
            s.flowWallMode = wm;
            changed = true;
          }
          if (wm == 0) {
            changed |= flowAdjust("adjust_wall_t", "Adjust wall",
                                  &s.flowWallTempC, 0.1f,
                                  FieldOpts().withUnit(Unit::DegC));
            if (kit::fieldNumber("wall_t", "Wall temperature",
                                 &s.flowWallTempC,
                                 FieldOpts().withUnit(Unit::DegC).withTip(
                                     "Temperature held on the part surface.")))
              changed = true;
          } else if (wm == 1) {
            changed |= flowAdjust("adjust_wall_q", "Adjust heat flux",
                                  &s.flowWallFluxWm2, 10.0f,
                                  FieldOpts().withUnit(Unit::W_m2));
            if (kit::fieldNumber(
                    "wall_q", "Heat flux", &s.flowWallFluxWm2,
                    FieldOpts()
                        .withUnit(Unit::W_m2)
                        .withRequired()
                        .withTip("Uniform heat flux into the fluid (negative "
                                 "cools it).")
                        .withIssue(std::fabs(s.flowWallFluxWm2) >= 1e-12f
                                       ? nullptr
                                       : "Set a non-zero heat flux.")))
              changed = true;
          } else {
            changed |= flowAdjust("adjust_wall_p", "Adjust wall power",
                                  &s.flowWallPowerW, 1.0f,
                                  FieldOpts().withUnit(Unit::W).withMin(0));
            if (kit::fieldNumber(
                    "wall_p", "Total power", &s.flowWallPowerW,
                    FieldOpts()
                        .withUnit(Unit::W)
                        .withMin(0.0)
                        .withRequired()
                        .withTip("Total heat dissipated across the part "
                                 "surface.")
                        .withIssue(s.flowWallPowerW > 0.0f
                                       ? nullptr
                                       : "Total power must be greater than "
                                         "zero.")))
              changed = true;
          }
        }
        kit::fieldReadout("heat_scope", "Applied to",
                         tt == 1 ? "Solid and surrounding fluid"
                                 : "Whole part surface");
        const std::string inlet = kit::fmtQuantity(
            s.flowInletTempC, kit::unitText(Unit::DegC));
        kit::fieldReadout("heat_ambient", "Current inlet", inlet.c_str());
        std::string thermalInput;
        if (tt == 1) {
          thermalInput = "Internal power " +
                         kit::fmtQuantity(s.flowPowerW, kit::unitText(Unit::W));
        } else if (s.flowWallMode == 0) {
          thermalInput = "Wall " +
                         kit::fmtQuantity(s.flowWallTempC, kit::unitText(Unit::DegC)) +
                         " (difference " +
                         kit::fmtQuantity(s.flowWallTempC - s.flowInletTempC,
                                          kit::unitText(Unit::K)) + ")";
        } else if (s.flowWallMode == 1) {
          thermalInput = "Wall flux " +
                         kit::fmtQuantity(s.flowWallFluxWm2,
                                          kit::unitText(Unit::W_m2));
        } else {
          thermalInput = "Wall power " +
                         kit::fmtQuantity(s.flowWallPowerW, kit::unitText(Unit::W));
        }
        kit::fieldReadout("heat_input", "Current heat input", thermalInput.c_str(),
                         FieldOpts().withTip(thermalInput.c_str()));
      } else {
        kit::fieldReadout("heat_off", "Current heat setup", "Heat transfer disabled");
      }
    }
    kit::cardEnd();
  };

  // ===================================================== Resolution page
  auto resolutionPage = [&]() {
    // Fidelity writes the actual native parameters, not a GUI-only preset.
    takeFocus("resolution");
    if (kit::cardBegin("resolution", "Resolution", "mesh")) {
      kit::FieldGroup g("fields", "Grid resolution");
      static const char *const labels[] = {
          "Quick preview", "Standard", "Accurate"};
      static const char *const tips[] = {
          "Coarse preview for live design comparisons. Small details may be unresolved.",
          "Engine defaults: 96 cells along the longest domain axis, 2000 iterations.",
          "Finer grid and more iterations. Uses more time and memory; check convergence."};
      FlowResolution r = flowResolution(s);
      int fidelity = -1;
      for (int i = 0; i < 3; ++i)
        if (r.cells == kFlowFidelity[i].cells && r.steps == kFlowFidelity[i].steps)
          fidelity = i;
      if (kit::fieldEnum("fidelity", "Fidelity", &fidelity, labels, 3,
                         FieldOpts().withHint("Custom parameters")
                             .withEnumStyle(kit::EnumStyle::Combo)
                             .withItemTips(tips).withTip(
                                 "The same grid and iteration parameters used by agent tools. "
                                 "Choosing a fidelity replaces any custom resolution."))) {
        fidelityEdit = fidelity;
        r = kFlowFidelity[fidelity];
        changed = true;
      }
      kit::fieldReadout("cells", "Grid resolution", std::to_string(r.cells).c_str(),
                        FieldOpts().withTip("Cells along the longest wind-tunnel axis."));
      kit::fieldReadout("steps", "Iteration budget", std::to_string(r.steps).c_str());
      kit::dimText("Run shows live progress and an estimated remaining time. "
                   "Refine the grid for thin walls or small details.");
    }
    kit::cardEnd();

    // ---- Solver: LBM vs OpenFOAM RANS (Flow); the OpenFOAM engine (Thermal) --
    if (kit::cardBegin("solver", "Solver", "settings")) {
      kit::FieldGroup g("fields", "Engine");
      if (thermalStudy) {
        const std::string engine =
            !ofAvail || fc.version.empty() ? std::string("OpenFOAM")
                                           : "OpenFOAM " + fc.version;
        kit::fieldReadout("engine", "Engine", engine.c_str(),
                          FieldOpts().withTip(
                              "Thermal CFD always runs the OpenFOAM "
                              "heat-transfer pipeline (buoyantSimpleFoam / "
                              "conjugate heat transfer)."));
      } else {
        static const char *const kSolverLabels[2] = {"Fast (LBM)",
                                                     "Detailed (OpenFOAM RANS)"};
        static const char *const kSolverTips[2] = {
            "Lattice-Boltzmann method: a fast grid solve, for quick design "
            "comparisons and trends.",
            "Steady RANS (k-omega SST) with boundary-layer meshing: slower, "
            "but trustworthy Cd / Cl for engineering decisions."};
        int svi = s.flowSolver == 1 ? 1 : 0;
        if (kit::fieldEnum("engine", "Engine", &svi, kSolverLabels, 2,
                           FieldOpts()
                               .withItemTips(kSolverTips)
                               .withTip("How the flow is solved: Fast (LBM) for "
                                        "quick comparisons, Detailed (OpenFOAM "
                                        "RANS) for engineering-grade "
                                        "coefficients."))) {
          s.flowSolver = svi;
          changed = true;
        }
        if (ofAvail && !fc.version.empty())
          kit::fieldReadout("of_version", "OpenFOAM", fc.version.c_str());
      }
      if (!fc.known)
        kit::spinner("Checking OpenFOAM availability\xe2\x80\xa6", kit::Tone::Info);
      else if (!ofAvail)
        kit::inlineStatus(thermalStudy
                              ? "Install OpenFOAM to run Thermal CFD."
                              : "OpenFOAM runs are blocked; choose Fast (LBM) "
                                "explicitly to run on this backend.",
                          kit::Tone::Info);
    }
    kit::cardEnd();
  };

  switch (node_) {
  case SimNode::Boundaries:
    boundariesPage();
    break;
  case SimNode::Mesh:
    resolutionPage();
    break;
  default:
    fluidPage();
    break;
  }
  ImGui::PopID();
  ImGui::PopID();
  if (fidelityEdit >= 0) {
    auto definition = studyToJson(s);
    definition["flow"]["cells"] = kFlowFidelity[fidelityEdit].cells;
    definition["flow"]["n_steps"] = kFlowFidelity[fidelityEdit].steps;
    definition.erase("cells");
    definition.erase("n_steps");
    for (const char *key : {"results", "result", "result_summary", "summary",
                            "mesh_quality", "reactions", "provenance", "advisor",
                            "n_nodes", "n_elems", "n_dof", "freqs_hz"})
      definition.erase(key);
    s.definition = std::move(definition);
    clearTransientResult(s);
    clearResultOverlay(st);
  }
  if (changed)
    persist(st, s);
}

bool SimManager::flowHasResult() const {
  if (sel_ < 0 || sel_ >= (int)studies_.size())
    return false;
  const Study &s = studies_[sel_];
  return isFlowFamily(s.type) && s.hasResult;
}

} // namespace ui
