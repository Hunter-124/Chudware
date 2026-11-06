// Reverse Engineering workspace — full-viewport shell for the "Reverse Eng" title-bar tab.
// Honest scan -> mesh -> AI -> parametric pipeline (contracts/reveng.md).
//
// Layout, top to bottom / left to right:
//   left   : session rail — the sessions of the CURRENT working folder only (top-left selector;
//            sessions carry project_dir), a substring filter, and the New-session card.
//   center : intake selector (Scan / From reference), then THE STAGE STEPPER (kit::StepTrack)
//            pinned at the TOP where a wizard's progress indicator belongs, then the stage body.
//   right  : Session column — what the session is, how far it got, how far its numbers can be
//            trusted, and (only when something is missing) the tools this lane still needs.
// Both side columns collapse to a header strip + chevron (see below).
//
// Why the stepper moved: it used to live at the BOTTOM of the workspace, below the stage body,
// so the one control that tells you where you are in a six-step pipeline was the last thing you
// read and could not be used for navigation. It is now the second thing you read, it is
// clickable, its per-step colour comes from the mirrored session.stages badges, and steps that
// are not reachable yet are dimmed and say what unblocks them.
//
// HARD RULES (shared with every other ui/reveng TU — see reveng_widgets.h):
//   • Sizes come from ui::metrics::re*(); never a fresh pixel literal.
//   • Colours come from ui::palette / the kit; never a hardcoded ImVec4/ImU32.
//   • Every async action shows Busy / Ok / Warn / Error; never a silent no-op.
//   • Every stage that produced an artifact SHOWS it (HeroPreview / ThumbTile / Viewport3DPreview).
//
// GL-thread only. Every service round-trip is ASYNC via st.rpc.call — a SYNC worker call in a
// draw path is a known freeze bug.
#include "reveng_workspace.h"

#include <algorithm>
#include <cctype>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <future>
#include <limits>
#include <memory>
#include <unordered_map>
#include <string>
#include <vector>

#include "imgui.h"
#include <nlohmann/json.hpp>

#include "core/app_state.h"
#include "core/engine_events.h"  // core::watchRevEngReconstruct — live Reconstruct progress feed
#include "ui_theme.h"
#include "../metrics.h"     // ui::metrics::re*() — the ONLY source of sizes in this TU
#include "ui/widgets/kit.h"  // ui::kit::elide — ellipsize rail labels to their frames

#include "reveng_internal.h"
#include "reveng_widgets.h"  // RE-40 shared visual kit (cards / chips / previews / steps)
#include "img_tex.h"         // rail card thumbnails (LRU-cached, cache-owned ids)
#include "dim_review.h"      // RE-22: drawReviewStage
// RE-ANCHOR(RE-19): capture suite — webcam + gallery wired into the Source stage
#include "webcam_capture.h"
#include "image_gallery.h"
#include "sources_panel.h"   // RE-30: drawSourcesPanel
#include "stage_panels.h"    // Scale / Model / Deliver stage panels
#include "../file_dialog.h"
#include "../file_explorer.h"   // ensureWorkspaceDir — sessions are scoped to the working folder
#include "reveng_import.h"

#ifdef __EMSCRIPTEN__
#include "core/web_fetch.h"
#include "../feature_tree.h"
#endif

namespace ui {
namespace reveng {

namespace M = ui::metrics;   // every size in this TU comes from here — no pixel literals

namespace {

// ---- shell-local state ---------------------------------------------------------
// Panel-local state lives in this TU, not in reveng_internal.h (which is the CROSS-panel
// contract). Everything here is derived/presentational: caches, animation, wall-clock.
struct ShellState {
    // Column roll animation. Negative = uninitialised, snaps on the first frame.
    float railW = -1.0f;
    float propW = -1.0f;
    // Width the two side columns may share while the stage body keeps its minimum — published by
    // the layout each frame so revealColumn() can tell whether a column is being auto-folded.
    // +inf until the first layout pass: nothing is auto-folded before anything has been measured.
    float sideBudget = std::numeric_limits<float>::infinity();

    // Rail card thumbnails. `path` is the session's first still, resolved once from the session
    // directory; the texture itself is owned by the img_tex LRU cache (never freed here).
    struct RailThumb {
        std::string sessionId;
        std::string path;          // "" once scanned and nothing was found
        int         forImages = -1;  // session.images at scan time — rescan when it changes
    };
    std::vector<RailThumb> thumbs;

    // Reconstruct wall-clock. reconStartT < 0 => no run in flight.
    double reconStartT   = -1.0;
    double reconElapsed  = 0.0;
    // Sparse/Dense/Mesh sub-step the run was last on, so a failure lands on the right step
    // after recon.active has already gone false.
    int    lastSubStep   = 0;

    // Webcam device lifetime. drawRevEngWorkspace stops being called the instant the user
    // leaves the RE tab, so we detect the gap on re-entry and tear the camera down then.
    int  lastDrawFrame = -1000;
    bool webcamLive    = false;

    uint64_t sessionEpoch = 0;
    bool gaussianPollPending = false, gaussianHydrated = false;
    double gaussianPollT = -100.0;
    uint64_t splatRevisionTried = 0;
    std::string splatTransformTried;
    std::string splatLoadError;
    Vec3 gaussianCenter;
    float gaussianRadius = 1.0f, gaussianYaw = 0.6f, gaussianPitch = 0.25f;
    float gaussianZoom = 1.0f;
    bool gaussianCameraFitted = false, cropDirty = false;
    Vec3 gaussianRight{1, 0, 0}, gaussianUp{0, 1, 0}, gaussianBack{0, 0, 1};
    GaussianViewCamera loadedViewCamera;
    bool gaussianCaptureProjection = false;
    uint64_t gaussianActionSerial = 0;
    std::unordered_map<std::string, std::shared_ptr<GaussianRunControl>> gaussianRuns;
    std::string gaussianArtifactDirectory, displayedSplatPath, displayedSplatTransformKey;
    std::string pendingSourceSession, pendingSourcePath, pendingSplatPath;
    SourceFile pendingSourceKind = SourceFile::Other;
    uint64_t sourceRequestSerial = 0;
    bool splatActionPending = false, splatActionError = false;
    std::string meshJobFinished;  // finished_at of the last session.mesh_job already reported
    int splatExportFormat = 0;
    float splatMinOpacity = 0.5f;
    bool splatAutoOpacity = true;
    bool splatRemoveFloor = true;
    std::string splatActionMsg;
    std::string previewTransformKey, previewRequestKey, previewTriedKey;
    std::string sparsePreviewWarnings;
    bool previewPending = false;
#ifdef __EMSCRIPTEN__
    std::string splatArtifactId, gaussianArtifactId, splatRefTried, splatRefDisplayed;
    std::string splatFetchKey, meshArtifactId, meshFetchKey;
    uint64_t splatFetchSerial = 0, meshFetchSerial = 0;
    uint64_t splatFetchEpoch = 0, meshFetchEpoch = 0;
    bool splatFetchPending = false, meshFetchPending = false;
    float splatFetchProgress = -1.0f, meshFetchProgress = -1.0f;
#endif
    struct MeshLoad {
        std::shared_ptr<render::DocMesh> doc;
        std::string path, key, error;
        uint64_t epoch = 0;
    };
    std::future<MeshLoad> meshLoad, parametricLoad;
    std::string meshLoadPath, meshSourcePath, meshSourceKey;
    std::string parametricLoadPath, parametricSourcePath, parametricSourceKey, parametricDocId;
    bool parametricPollPending = false;
    double parametricPollT = -100.0;
    struct SplatLoad {
        render::SplatCloud cloud;
        std::string error, path;
        uint64_t epoch = 0;
        uint64_t revision = 0;
        GaussianViewCamera viewCamera;
        SplatPreviewTransform transform;
#ifdef __EMSCRIPTEN__
        std::string artifactId;
#endif
        std::string transformKey;
    };
    std::future<SplatLoad> splatLoad;
};

ShellState& SH() {
    static ShellState s;
    return s;
}

// The responsive fold: when the body cannot hold three columns at their minimum widths
// (rail reRailW, properties rePropW, stage body rePreviewMinW), Auto columns roll to their strip —
// Properties first, then Sessions. A user-Collapsed column already counts as a strip; an
// Expanded one is never folded. Pure function of intent + budget, so it cannot oscillate.
void foldColumns(const RevEngState& s, float sideBudget, bool& railFold, bool& propFold) {
    const float stripW = M::reThumbSm();
    railFold = s.railIntent  == ColumnIntent::Collapsed;
    propFold = s.propsIntent == ColumnIntent::Collapsed;
    auto fits = [&] {
        return (railFold ? stripW : M::reRailW()) + (propFold ? stripW : M::rePropW()) <= sideBudget;
    };
    if (!fits() && s.propsIntent == ColumnIntent::Auto) propFold = true;
    if (!fits() && s.railIntent  == ColumnIntent::Auto) railFold = true;
}

// An explicit "show me this column" (strip chevron, ribbon command, empty-state button). If the
// layout would fold the column were it left on Auto, the request pins it Expanded so the next
// frame's auto-fold cannot undo it; otherwise it goes back to Auto, so a column the user merely
// re-opened on a wide body still folds if the window later narrows.
void revealColumn(bool rail) {
    RevEngState& s = S();
    ColumnIntent& intent = rail ? s.railIntent : s.propsIntent;
    intent = ColumnIntent::Auto;
    bool railFold = false, propFold = false;
    foldColumns(s, SH().sideBudget, railFold, propFold);
    if (rail ? railFold : propFold) intent = ColumnIntent::Expanded;
}

bool selectedSession(const std::string& sid) {
    const auto& s = S();
    return s.selected >= 0 && s.selected < (int)s.sessions.size() &&
           s.sessions[s.selected].session_id == sid;
}

// A label ellipsized to a fixed-width frame (button / input hint) instead of hard-clipped by it.
// The ### suffix keeps the ImGui id stable while the visible text changes with the width.
std::string fitLabel(const char* label, const char* id, float frameW) {
    const float room = frameW - ImGui::GetStyle().FramePadding.x * 2.0f;
    return ui::kit::elide(label, room) + "###" + id;
}

bool gaussianRunning(const GaussianState& g) {
    return g.status == "waiting" || g.status == "sparse" || g.status == "training" ||
           g.status == "watching" || g.status == "finalizing";
}

bool readBounds(const nlohmann::json& j, float (&out)[6]) {
    if (!j.is_array() || j.size() != 6) return false;
    float values[6];
    for (int i = 0; i < 6; ++i) {
        if (!j[i].is_number()) return false;
        values[i] = j[i].get<float>();
        if (!std::isfinite(values[i])) return false;
    }
    for (int i = 0; i < 3; ++i) if (values[i] > values[i + 3]) return false;
    std::copy(values, values + 6, out);
    return true;
}

GaussianViewCamera readViewCamera(const nlohmann::json& j) {
    GaussianViewCamera camera;
    if (!j.is_object() || !j.contains("id") || !j["id"].is_string() ||
        j["id"].get_ref<const std::string&>().empty()) return camera;
    const auto readVector = [&](const char* key, float* values, size_t size) {
        const auto it = j.find(key);
        if (it == j.end() || !it->is_array() || it->size() != size) return false;
        for (size_t i = 0; i < size; ++i) {
            if (!(*it)[i].is_number()) return false;
            values[i] = (*it)[i].get<float>();
            if (!std::isfinite(values[i])) return false;
        }
        return true;
    };
    float eye[3], forward[3], up[3];
    if (!readVector("eye", eye, 3) || !readVector("forward", forward, 3) ||
        !readVector("up", up, 3) || !readVector("intrinsics", camera.intrinsics, 6) ||
        !j.contains("focus_distance") || !j["focus_distance"].is_number()) return {};
    camera.focusDistance = j["focus_distance"].get<float>();
    if (!std::isfinite(camera.focusDistance) || camera.focusDistance <= 0.0f ||
        camera.intrinsics[0] <= 0.0f || camera.intrinsics[1] <= 0.0f ||
        camera.intrinsics[4] <= 0.0f || camera.intrinsics[5] <= 0.0f) return {};
    camera.eye = {eye[0], eye[1], eye[2]};
    camera.forward = {forward[0], forward[1], forward[2]};
    camera.up = {up[0], up[1], up[2]};
    const float forwardLength = length(camera.forward), upLength = length(camera.up);
    if (!std::isfinite(forwardLength) || !std::isfinite(upLength) ||
        forwardLength < 1e-6f || upLength < 1e-6f) return {};
    camera.forward = camera.forward * (1.0f / forwardLength);
    camera.up = camera.up * (1.0f / upLength);
    const Vec3 right = cross(camera.forward, camera.up);
    if (length(right) < 1e-6f) return {};
    camera.up = cross(normalize(right), camera.forward);
    const Vec3 target = camera.eye + camera.forward * camera.focusDistance;
    if (!std::isfinite(target.x) || !std::isfinite(target.y) || !std::isfinite(target.z)) return {};
    camera.id = j["id"].get<std::string>();
    return camera;
}

#ifdef __EMSCRIPTEN__
std::string artifactRefId(const nlohmann::json& response, const char* name) {
    const auto refs = response.find("artifact_refs");
    if (refs == response.end() || !refs->is_object()) return {};
    const auto ref = refs->find(name);
    if (ref == refs->end() || !ref->is_object()) return {};
    const auto id = ref->find("id");
    if (id == ref->end() || !id->is_string()) return {};
    const auto value = id->get<std::string>();
    if (value.size() != 64 || !std::all_of(value.begin(), value.end(), [](char c) {
            return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f');
        })) return {};
    return value;
}
#endif

void mirrorGaussian(const nlohmann::json& j, bool restoreOptions) {
    if (!j.is_object()) return;
    auto& g = S().gaussian;
    const auto text = [&](const char* key) {
        auto it = j.find(key);
        return it != j.end() && it->is_string() ? it->get<std::string>() : std::string();
    };
    const std::string runId = j.value("run_id", g.runId);
    restoreOptions = restoreOptions || (!runId.empty() && runId != g.runId);
    g.runId = runId;
    g.status = j.value("status", g.status);
    g.stage = j.value("stage", g.stage);
    g.msg = j.value("msg", std::string());
    g.live = j.value("live", g.live);
    g.pct = std::clamp(j.value("pct", g.pct), 0, 100);
    g.frames = j.value("frames", g.frames);
    g.registered = j.value("registered", g.registered);
    g.iteration = j.value("iteration", g.iteration);
    g.totalIterations = j.value("total_iterations", g.totalIterations);
#ifdef __EMSCRIPTEN__
    if (j.contains("artifact_refs"))
        SH().gaussianArtifactId = artifactRefId(j, "splat");
#endif
    if (j.contains("splat_path") && j["splat_path"].is_string() &&
        !j["splat_path"].get_ref<const std::string&>().empty()) {
        const auto path = j["splat_path"].get<std::string>();
        const bool changed = path != g.splatPath ||
            j.value("artifact_revision", g.artifactRevision) != g.artifactRevision;
        // The pose travels with the artifact, never with a sparse/training stage transition.
        if (j.contains("view_camera")) g.viewCamera = readViewCamera(j["view_camera"]);
        else if (changed) g.viewCamera = {};
        g.splatPath = path;
    }
    if (j.value("artifact_revision", g.artifactRevision) != g.artifactRevision &&
        j.contains("crop_bounds")) {
        float crop[6];
        g.cropEnabled = readBounds(j["crop_bounds"], crop);
        if (g.cropEnabled && !SH().cropDirty)
            std::copy(crop, crop + 6, g.cropBounds);
    }
    g.artifactRevision = j.value("artifact_revision", g.artifactRevision);
    if (j.contains("bounds")) g.hasBounds = readBounds(j["bounds"], g.bounds);
    if (restoreOptions) {
        g.mode = j.value("mode", std::string("environment")) == "object" ? 1 : 0;
        const auto quality = j.value("quality", std::string("normal"));
        g.quality = quality == "draft" ? 0 : quality == "high" ? 2 : 1;
        g.iterations = j.value("iterations", g.iterations);
        g.cropEnabled = j.contains("crop_bounds") && readBounds(j["crop_bounds"], g.cropBounds);
        SH().cropDirty = false;
    }
    g.error = text("error");
    const std::string reason = text("reason");
    const std::string hint = text("hint");
    if (!reason.empty() && reason != g.error) g.error += (g.error.empty() ? "" : ": ") + reason;
    if (!hint.empty()) g.error += (g.error.empty() ? "" : " — ") + hint;
}

void fitGaussianCamera() {
    auto& sh = SH();
    Vec3 lo, hi;
    if (!S().splat.bounds(lo, hi)) return;
    sh.gaussianCenter = (lo + hi) * 0.5f;
    sh.gaussianRadius = std::max(length(hi - lo) * 0.5f, 1e-6f);
    sh.gaussianZoom = 1.0f;
    sh.gaussianCaptureProjection = false;
    sh.gaussianCameraFitted = true;
}

void restoreGaussianCaptureCamera() {
    auto& sh = SH();
    const auto& capture = sh.loadedViewCamera;
    if (capture.id.empty()) return;
    sh.gaussianBack = capture.forward * -1.0f;
    sh.gaussianUp = capture.up;
    sh.gaussianRight = normalize(cross(capture.forward, capture.up));
    sh.gaussianCenter = capture.eye + capture.forward * capture.focusDistance;
    sh.gaussianRadius = capture.focusDistance;
    sh.gaussianYaw = sh.gaussianPitch = 0.0f;
    sh.gaussianZoom = 1.0f;
    sh.gaussianCaptureProjection = true;
    sh.gaussianCameraFitted = true;
}

// Exponential approach used for the column roll — same 120 ms feel as ui::PanelCollapseAnim.
float approachPx(float& cur, float target) {
    if (cur < 0.0f) { cur = target; return cur; }
    const float a = 1.0f - std::exp(-ImGui::GetIO().DeltaTime / 0.12f);
    cur += (target - cur) * a;
    if (std::fabs(cur - target) < 0.5f) cur = target;
    return cur;
}

// ---- stage vocabulary ----------------------------------------------------------

const char* stageName(Stage s) {
    switch (s) {
        case Stage::Source:      return "Source";
        case Stage::Scale:       return "Scale";
        case Stage::Reconstruct: return "Reconstruct";
        case Stage::Model:       return "Model";
        case Stage::Review:      return "Review";
        case Stage::Deliver:     return "Deliver";
    }
    return "?";
}

// The manifest key for a stage (contracts/reveng.md §2 session.stages). Source is "capture".
const char* stageManifestKey(Stage s) {
    switch (s) {
        case Stage::Source:      return "capture";
        case Stage::Scale:       return "scale";
        case Stage::Reconstruct: return "reconstruct";
        case Stage::Model:       return "model";
        case Stage::Review:      return "review";
        case Stage::Deliver:     return "deliver";
    }
    return "";
}

const char* stageBlurb(Stage stg) {
    switch (stg) {
        case Stage::Source:
            return "Scan the part (photos, video, webcam), or start from a reference: a part "
                   "number, product name, URL or datasheet — an existing CAD model is searched "
                   "for first.";
        case Stage::Scale:
            return "Set real-world scale from an ArUco mat, two picked points, or a known "
                   "dimension. Without scale the model has shape but no size.";
        case Stage::Reconstruct:
            return "Sparse -> dense -> mesh (or a 3D Gaussian splat) reconstruction of the "
                   "captured views into geometry you can measure.";
        case Stage::Model:
            return "Fit primitives (cylinders, holes, bosses, planes) and build an editable "
                   "parametric model grounded in those fits, with a mesh-body fallback.";
        case Stage::Review:
            return "Deviation heat-map (mesh vs. B-rep) plus the dimension-confirm table, to "
                   "prove the model matches the real part within tolerance.";
        case Stage::Deliver:
            return "Insert the scan as a reference in a part, attach a result to the parts hub, "
                   "export a bundle, or print a scale mat.";
    }
    return "";
}

// The stage whose COMPLETION unlocks `stg`. This is a dependency graph, not the visual order:
// Reconstruct depends on Source, NOT on Scale, because the ArUco scale solver needs a sparse
// model to triangulate against (reveng_set_scale returns reason:"scale_needs_sparse"). Gating
// Reconstruct behind Scale would deadlock the Scan lane.
Stage stagePrereq(Stage stg) {
    switch (stg) {
        case Stage::Source:      return Stage::Source;       // always reachable
        case Stage::Scale:       return Stage::Source;
        case Stage::Reconstruct: return Stage::Source;
        case Stage::Model:       return Stage::Reconstruct;  // doc lanes remap this to Source
        case Stage::Review:      return Stage::Model;
        case Stage::Deliver:     return Stage::Model;
    }
    return Stage::Source;
}

// What has to happen before a stage becomes reachable — shown on the locked step.
const char* stageGateReason(Stage stg, bool scanLane) {
    switch (stg) {
        case Stage::Source:      return "pick a source lane first";
        case Stage::Scale:       return "capture or import at least one image in Source first";
        case Stage::Reconstruct: return "Source needs captured images before a reconstruction can run";
        case Stage::Model:       return scanLane
                                          ? "run Reconstruct first so there is geometry to fit"
                                          : "pick an existing model, or add a datasheet or specs in Source first";
        case Stage::Review:      return "build a model in the Model stage first";
        case Stage::Deliver:     return scanLane
                                          ? "reconstruct a surface mesh or build a model first"
                                          : "build a model in the Model stage first";
    }
    return "an earlier stage is not finished";
}

// Contract badge string -> kit level. Unknown non-idle badges read as in-flight, which is the
// safe reading: the backend is doing something we do not have a name for.
kit::Level badgeLevel(const std::string& b) {
    if (b == "done" || b == "complete")            return kit::Level::Ok;
    if (b == "failed" || b == "error")             return kit::Level::Error;
    if (b.empty() || b == "idle" || b == "skipped") return kit::Level::Neutral;
    return kit::Level::Busy;
}

kit::Level statusLevel(const std::string& status) {
    if (status == "done")  return kit::Level::Ok;
    if (status == "error") return kit::Level::Error;
    if (status == "capturing" || status == "reconstructing" || status == "modeling")
        return kit::Level::Busy;
    return kit::Level::Neutral;
}

// Returns the ordered stage list for the current intake lane. The reference lane skips Scale +
// Reconstruct — there is nothing to scale or reconstruct.
std::vector<Stage> activeStagePipeline(SourceMode mode) {
    if (mode == SourceMode::Scan) {
        return { Stage::Source, Stage::Scale, Stage::Reconstruct,
                 Stage::Model, Stage::Review, Stage::Deliver };
    }
    return { Stage::Source, Stage::Model, Stage::Review, Stage::Deliver };
}

void clampStageToPipeline(RevEngState& s) {
    const std::vector<Stage> pipeline = activeStagePipeline(s.sourceMode);
    for (const Stage& stg : pipeline)
        if (stg == s.stage) return;
    s.stage = pipeline.front();
}

}  // namespace

RevEngState& S() {
    static RevEngState s;
    return s;
}

std::string revengProjectDir(AppState& st) {
#ifdef __EMSCRIPTEN__
    (void)st;
    return {};
#else
    ui::ensureWorkspaceDir(st);
    return st.workspaceDir;
#endif
}

static void openClassifiedSourceFile(AppState& st, const std::string& path, SourceFile kind) {
    if (kind == SourceFile::Other) return;
    if (!st.workerStarted) {
        st.toast(ui::ToastLevel::Error, "Open failed: worker not started");
        return;
    }
    st.activeWorkspace = AppState::Workspace::ReverseEng;
    auto& s = S();
    s.sourceMode = SourceMode::Scan;
    const uint64_t serial = ++SH().sourceRequestSerial;
    if (!SH().pendingSourceSession.empty() && st.pendingRevengSessionId == SH().pendingSourceSession)
        st.pendingRevengSessionId.clear();
    SH().pendingSourceSession.clear();
    SH().pendingSourcePath.clear();
    SH().pendingSplatPath.clear();
    SH().pendingSourceKind = SourceFile::Other;
    if (kind == SourceFile::Video && s.selected >= 0 && s.selected < (int)s.sessions.size()) {
        s.sourceVideoPath = path;
        s.stage = Stage::Source;
        s.sourceFocus = SourceFocus::Video;
        return;
    }
    const char* method = kind == SourceFile::Splat ? "reveng_import_splat" : "reveng_start_session";
    nlohmann::json params = kind == SourceFile::Splat
        ? nlohmann::json{{"path", path}}
        : nlohmann::json{{"name", std::filesystem::path(path).filename().string()}};
    if (const std::string project = revengProjectDir(st); !project.empty())
        params["project_dir"] = project;
    st.toast(ui::ToastLevel::Info, kind == SourceFile::Splat
        ? "Importing Gaussian splat..." : "Creating video capture session...");
    st.rpc.call(method, std::move(params), [&st, path, kind, serial](const core::RpcResult& r) {
        if (serial != SH().sourceRequestSerial) return;
        if (!r.ok || !r.result.is_object() || !r.result.value("ok", false)) {
            const std::string reason = r.ok && r.result.is_object()
                ? r.result.value("reason", std::string("Reverse Engineering import failed"))
                : (r.err.empty() ? std::string("Worker unavailable") : r.err);
            st.toast(ui::ToastLevel::Error, reason);
            return;
        }
        const std::string sid = r.result.value("session_id", std::string());
        const std::string splat = r.result.value("gaussian_ply", std::string());
        if (sid.empty() || (kind == SourceFile::Splat && splat.empty())) {
            st.toast(ui::ToastLevel::Error, "Import returned no session or Gaussian preview.");
            return;
        }
        SH().pendingSourceSession = sid;
        SH().pendingSourcePath = path;
        SH().pendingSplatPath = splat;
        SH().pendingSourceKind = kind;
        st.pendingRevengSessionId = sid;
        S().lastListT = -100.0;
    }, core::kLongEngineCallTimeoutMs);
}

void openSourceFile(AppState& st, const std::string& path) {
    const SourceFile kind = classifySourceFile(path);
#ifdef __EMSCRIPTEN__
    if (kind == SourceFile::Ply) {
        if (!st.workerStarted) {
            st.toast(ui::ToastLevel::Error, "Open failed: worker not started");
            return;
        }
        const auto serial = ++SH().sourceRequestSerial;
        st.toast(ui::ToastLevel::Info, "Inspecting PLY...");
        st.rpc.call("reveng_import_splat", {{"classify_ply", path}},
                    [&st, path, serial](const core::RpcResult& r) {
            if (serial != SH().sourceRequestSerial) return;
            if (!r.ok || !r.result.is_object() || !r.result.value("ok", false)) {
                const std::string reason = r.ok && r.result.is_object()
                    ? r.result.value("reason", std::string("Unable to inspect PLY"))
                    : (r.err.empty() ? std::string("Worker unavailable") : r.err);
                st.toast(ui::ToastLevel::Error, reason);
                return;
            }
            if (r.result.value("kind", std::string()) == "gaussian") {
                openClassifiedSourceFile(st, path, SourceFile::Splat);
            } else {
                const int choice = ui::promptImportMeshAsSolid();
                if (choice < 0) return;
                struct Bypass {
                    Bypass() { bypassPlyClassification() = true; }
                    ~Bypass() { bypassPlyClassification() = false; }
                } bypass;
                ui::featureTreeOpenPath(st, path, choice == 1);
            }
        }, core::kLongEngineCallTimeoutMs);
        return;
    }
#endif
    openClassifiedSourceFile(st, path, kind);
}

// Chip geometry mirrors ui::kit (chipGeom: padX 1.5U, dot 2x0.75U + 1U gap; valueChip: padX 1.5U,
// 1U label/value gap) so a flow layout can measure a chip before it is placed.
float statusChipW(const char* label) {
    return M::U(1.5f) * 2.0f + M::U(0.75f) * 2.0f + M::U(1.0f)
         + ImGui::CalcTextSize(label ? label : "").x;
}

float valueChipW(const char* label, const char* value) {
    return M::U(1.5f) * 2.0f + M::U(1.0f) + ImGui::CalcTextSize(label ? label : "").x
         + ImGui::CalcTextSize(value ? value : "").x;
}

void flowSameLine(float rightX, float w, float gap) {
    if (ImGui::GetItemRectMax().x + gap + w <= rightX) ImGui::SameLine(0.0f, gap);
}

// RE-21: Apply a reveng event to the singleton (GL-thread only, called from app.cpp drain).
// CONTRACT UNCHANGED — kinds, fields and semantics are exactly as before.
void feedRevEngEvent(const RevEngEvent& ev) {
    RevEngState& s = S();
    if (ev.kind == "reconstruct_progress") {
        // A progress bar that walks backwards reads as a fault. The stage map assigns each node
        // a nominal pct, so a node whose intra-node records already reported 62-64 can be
        // followed by the next node's nominal 55. Hold the high-water mark within a run; a
        // fresh run resets it via kickReconstruct, and done/error below set their own values.
        if (!s.recon.active) s.recon.pct = 0;   // first event of this run
        s.recon.active = true;
        s.recon.done   = false;
        s.recon.err.clear();
        if (ev.pct > s.recon.pct) s.recon.pct = ev.pct;
        s.recon.phase  = ev.msg;
    } else if (ev.kind == "reconstruct_done") {
        s.recon.active = false;
        s.recon.done   = true;
        s.recon.pct    = 100;
        s.recon.err.clear();
    } else if (ev.kind == "reconstruct_error") {
        s.recon.active = false;
        s.recon.err    = ev.msg.empty() ? "Unknown reconstruction error" : ev.msg;
    }
}

// RE-27: seed the canned splat fixture (for shot modes / headless verification).
void seedSplatFixture() {
    if (!std::getenv("CHUDWARE_SHOT")) return;
    RevEngState& s = S();
    if (s.splatLoaded) return;
    const char* repo = std::getenv("CHUDWARE_REPO");
    std::string base = repo ? std::string(repo) : std::string(".");
    std::string plyPath = base + "/host/tests/fixtures/splat_tiny.ply";
    s.splatLoaded = true;
    s.splatPathTried = plyPath;
    std::string err;
    if (!s.splat.load(plyPath, err))
        std::fprintf(stderr, "reveng seedSplatFixture: %s\n", err.c_str());
}

namespace {

// ---- session paths -------------------------------------------------------------

// Resolve a session-relative artifact path against $CHUDWARE_REVENG_DIR/<sid>/.
std::string resolveSessionPath(const std::string& session_id, const std::string& rel_or_abs) {
    if (rel_or_abs.empty()) return {};
    if (rel_or_abs[0] == '/') return rel_or_abs;
#if defined(_WIN32)
    if (rel_or_abs.size() > 2 && rel_or_abs[1] == ':') return rel_or_abs;
#endif
    const char* override_dir = std::getenv("CHUDWARE_REVENG_DIR");
    std::string root;
    if (override_dir && override_dir[0]) {
        root = override_dir;
    } else {
        const char* xdg = std::getenv("XDG_DATA_HOME");
        if (xdg && xdg[0]) root = std::string(xdg) + "/Chudware/reveng";
        else {
            const char* home = std::getenv("HOME");
            root = (home && home[0] ? std::string(home) : std::string(".")) +
                   "/.local/share/Chudware/reveng";
        }
    }
    if (!root.empty() && root.back() != '/') root += '/';
    if (!session_id.empty()) {
        root += session_id;
        root += '/';
    }
    root += rel_or_abs;
    return root;
}

bool looksLikeImage(const std::string& name) {
    const std::size_t dot = name.rfind('.');
    if (dot == std::string::npos) return false;
    std::string ext = name.substr(dot + 1);
    for (char& c : ext) c = (char)std::tolower((unsigned char)c);
    return ext == "jpg" || ext == "jpeg" || ext == "png";
}

// First still in <session>/images, else <session>/frames. Non-throwing; "" when there is none.
std::string firstSessionImage(const std::string& sid) {
    namespace fs = std::filesystem;
    const char* kDirs[] = { "images", "frames" };
    for (const char* sub : kDirs) {
        std::error_code ec;
        const fs::path dir(resolveSessionPath(sid, sub));
        if (!fs::is_directory(dir, ec)) continue;
        std::string best;
        for (fs::directory_iterator it(dir, ec), end; !ec && it != end; it.increment(ec)) {
            const std::string name = it->path().filename().string();
            if (!looksLikeImage(name)) continue;
            if (best.empty() || name < best) best = name;
        }
        if (!best.empty()) return (dir / best).string();
    }
    return {};
}

// Cached rail thumbnail for one session. The returned id belongs to the img_tex LRU cache —
// never delete it; it survives session switches for free.
unsigned int railThumb(const SessionSummary& ss, int* outW, int* outH) {
    ShellState& sh = SH();
    ShellState::RailThumb* slot = nullptr;
    for (ShellState::RailThumb& t : sh.thumbs)
        if (t.sessionId == ss.session_id) { slot = &t; break; }
    if (!slot) {
        sh.thumbs.push_back(ShellState::RailThumb{ ss.session_id, {}, -1 });
        slot = &sh.thumbs.back();
    }
    if (slot->forImages != ss.images) {
        slot->forImages = ss.images;
        slot->path = ss.images > 0 ? firstSessionImage(ss.session_id) : std::string();
    }
    if (slot->path.empty()) return 0;
    return ::reveng::cachedThumbTex(slot->path, (int)M::reThumbMd(), outW, outH);
}

// ---- async loaders -------------------------------------------------------------

void resetForSessionChange(RevEngState& s);

// `manual` marks a user-requested refresh: only that shows the Refresh button as busy, so the
// background 4 s poll never makes it flash.
void loadSessions(AppState& st, bool force = false, bool manual = false) {
    if (!st.workerStarted) return;
    RevEngState& s = S();
    const std::string project = revengProjectDir(st);
    if (s.listedProject.empty() && !project.empty()) {
        // First bind: whatever is listed already belongs to this folder (or to a screenshot
        // scenario that seeded the rail and blocked reloads); only an unlisted rail fetches now.
        s.listedProject = project;
        if (!s.listedOnce) force = true;
    } else if (project != s.listedProject) {
        // The working folder changed: drop the old folder's sessions now, not after the reply.
        s.listedProject = project;
        s.listedOnce = false;
        s.listErr.clear();
        if (!s.sessions.empty()) {
            s.sessions.clear();
            s.selected = -1;
            s.stage = Stage::Source;
            resetForSessionChange(s);
        }
        force = true;
    }
    if (s.listPending) {
        if (manual) s.listManual = true;   // the in-flight reply answers this click too
        return;
    }
    const double now = ImGui::GetTime();
    if (!force && s.listedOnce && now - s.lastListT < 4.0) return;
    s.listPending = true;
    s.listManual = manual;
    s.lastListT = now;
    nlohmann::json params = nlohmann::json::object();
    if (!project.empty()) params["project_dir"] = project;
    st.rpc.call("list_reveng_sessions", params, [project](const core::RpcResult& r) {
        RevEngState& st2 = S();
        st2.listPending = false;
        st2.listManual = false;
        if (project != st2.listedProject) return;   // reply for a folder we already left
        st2.listedOnce = true;
        if (!r.ok) {
            st2.listErr = r.err.empty() ? "worker unreachable" : r.err;
            return;
        }
        if (!r.result.is_object() || !r.result.value("ok", false)) {
            st2.listErr = r.result.is_object()
                              ? r.result.value("reason", std::string("Reverse Eng tools unavailable"))
                              : std::string("Reverse Eng tools unavailable");
            return;
        }
        st2.listErr.clear();
        const std::string selectedId = st2.selected >= 0 && st2.selected < (int)st2.sessions.size()
                                           ? st2.sessions[st2.selected].session_id : std::string();
        const std::string galleryId = st2.gallerySession >= 0 && st2.gallerySession < (int)st2.sessions.size()
                                          ? st2.sessions[st2.gallerySession].session_id : std::string();
        std::vector<SessionSummary> sessions;
        if (r.result.contains("sessions") && r.result["sessions"].is_array()) {
            for (const auto& j : r.result["sessions"]) {
                if (!j.is_object()) continue;
                SessionSummary ss;
                ss.session_id = j.value("session_id", std::string());
                ss.name = j.value("name", std::string("(unnamed)"));
                ss.status = j.value("status", std::string());
                ss.images = j.value("images", 0);
                ss.lane = j.value("lane", std::string());
                if (j.contains("gaussian") && j["gaussian"].is_object()) {
                    const auto& gaussian = j["gaussian"];
                    ss.hasGaussian = true;
                    ss.gaussianStatus = gaussian.value("status", std::string());
                    ss.gaussianStage = gaussian.value("stage", std::string());
                    ss.gaussianMode = gaussian.value("mode", std::string());
                    ss.gaussianFrames = gaussian.value("frames", 0);
                    ss.gaussianRegistered = gaussian.value("registered", 0);
                    ss.gaussianPct = gaussian.value("pct", 0);
                }
                ss.createdAt = j.value("created_at", std::string());
                ss.updatedAt = j.value("updated_at", std::string());
                if (j.contains("part_ref") && j["part_ref"].is_string())
                    ss.partRef = j["part_ref"].get<std::string>();
                if (j.contains("honesty") && j["honesty"].is_object())
                    ss.approximate = j["honesty"].value("approximate", false);
                ss.approximate = ss.approximate || j.value("approximate", false);
                sessions.push_back(std::move(ss));
            }
        }
        st2.sessions = std::move(sessions);
        st2.selected = -1;
        st2.gallerySession = -1;
        for (int i = 0; i < (int)st2.sessions.size(); ++i) {
            if (!selectedId.empty() && st2.sessions[i].session_id == selectedId) st2.selected = i;
            if (!galleryId.empty() && st2.sessions[i].session_id == galleryId) st2.gallerySession = i;
        }
        if ((!selectedId.empty() && st2.selected < 0) ||
            (!galleryId.empty() && st2.gallerySession < 0)) {
            st2.stage = Stage::Source;
            resetForSessionChange(st2);
        }
    });
}

// Async: pull the full session manifest — artifact paths, scale, stage badges, honesty tags and
// the reference-model search. Everything the Session column and the stages mirror.
void loadSessionDetail(AppState& st, bool force = false) {
    if (!st.workerStarted) return;
    RevEngState& s = S();
    if (s.selected < 0 || s.selected >= (int)s.sessions.size()) return;
    const std::string& sid = s.sessions[s.selected].session_id;
    if (sid.empty()) return;
    if (s.detailPending) return;
    const double now = ImGui::GetTime();
    if (!force && s.detailSessionId == sid && now - s.lastDetailT < 3.0) return;
    s.detailPending = true;
    s.lastDetailT = now;
    nlohmann::json params = nlohmann::json::object();
    params["session_id"] = sid;
    const uint64_t epoch = SH().sessionEpoch;
    st.rpc.call("get_reveng_session", params, [sid, epoch](const core::RpcResult& r) {
        RevEngState& st2 = S();
        if (!selectedSession(sid) || SH().sessionEpoch != epoch) return;
        st2.detailPending = false;
        if (!r.ok) {
            st2.detailErr = r.err.empty() ? "get_reveng_session failed" : r.err;
            return;
        }
        if (!r.result.is_object() || !r.result.value("ok", false)) {
            st2.detailErr = r.result.is_object()
                ? r.result.value("reason", std::string("get_reveng_session failed"))
                : std::string("get_reveng_session failed");
            return;
        }
        st2.detailErr.clear();
        st2.detailSessionId = sid;
        st2.artifactSplatPath.clear();
        st2.artifactMeshPath.clear();
        st2.artifactSparseDir.clear();
        st2.splatUntransformed = false;
        st2.splatArtifactTransform = {};
        for (int i = 0; i < kStageCount; ++i) st2.stages[i].badge = "idle";
        st2.honestyPipeline.clear();
        st2.honestyTier.clear();
        if (!r.result.contains("session") || !r.result["session"].is_object()) return;
        const auto& sess = r.result["session"];
#ifdef __EMSCRIPTEN__
        SH().splatArtifactId = artifactRefId(r.result, "splat");
        SH().gaussianArtifactId = artifactRefId(r.result, "gaussian_splat");
#endif
        SH().previewTransformKey = sess.contains("recon_transform")
            ? sess["recon_transform"].dump() : std::string();
        st2.artifactIsImportedSplat = sess.contains("imported_splat") &&
                                     sess["imported_splat"].is_object();
        st2.splatImportLoss.clear();
        if (st2.artifactIsImportedSplat && sess["imported_splat"].contains("lossy") &&
            sess["imported_splat"]["lossy"].is_array()) {
            for (const auto& loss : sess["imported_splat"]["lossy"])
                if (loss.is_string()) {
                    if (!st2.splatImportLoss.empty()) st2.splatImportLoss += "; ";
                    st2.splatImportLoss += loss.get<std::string>();
                }
        }
        if (sess.contains("gaussian") && sess["gaussian"].is_object() &&
            !SH().gaussianHydrated && !st2.gaussian.pending) {
            mirrorGaussian(sess["gaussian"], true);
            SH().gaussianHydrated = true;
        }
        if (r.result.contains("source_images") && r.result["source_images"].is_array()) {
            for (const auto& image : r.result["source_images"]) {
                if (!image.is_object()) continue;
                const std::string path = image.value("path", std::string());
                if (path.empty()) continue;
                auto it = std::find_if(st2.galleryImages.begin(), st2.galleryImages.end(),
                                      [&](const GalleryImage& old) { return old.path == path; });
                if (it == st2.galleryImages.end()) {
                    st2.galleryImages.emplace_back();
                    it = st2.galleryImages.end() - 1;
                    it->path = path;
                }
                it->quality = image.value("quality", it->quality);
                if (image.contains("blur_score") && image["blur_score"].is_number())
                    it->sharpness = image["blur_score"].get<double>();
                it->synthetic = image.value("synthetic", it->synthetic);
                it->srcW = image.value("width", it->srcW);
                it->srcH = image.value("height", it->srcH);
            }
        }

        // session.scale (§2) — the Scale stage + properties column read the mirror.
        if (sess.contains("scale") && sess["scale"].is_object()) {
            const auto& sj = sess["scale"];
            ScaleState& sc = st2.scale;
            sc.mirrored = sj.contains("mm_per_unit") && sj["mm_per_unit"].is_number();
            sc.curMethod = sj.contains("method") && sj["method"].is_string()
                ? sj["method"].get<std::string>() : std::string();
            sc.mmPerUnit = sc.mirrored ? sj["mm_per_unit"].get<double>() : 0.0;
            sc.hasResidual = sj.contains("residual_pct") && sj["residual_pct"].is_number();
            sc.residualPct = sc.hasResidual ? sj["residual_pct"].get<double>() : 0.0;
            sc.hasResidual = sc.hasResidual && std::isfinite(sc.residualPct) && sc.residualPct >= 0.0;
            sc.confirmed   = sj.value("confirmed", false);
        }

        // session.honesty (§2) — never render photo-derived geometry as exact.
        if (sess.contains("honesty") && sess["honesty"].is_object()) {
            const auto& hj = sess["honesty"];
            st2.honestyPipeline = hj.value("pipeline", std::string());
            if (hj.contains("single_photo_tier") && hj["single_photo_tier"].is_string())
                st2.honestyTier = hj["single_photo_tier"].get<std::string>();
        }

        if (sess.contains("artifacts") && sess["artifacts"].is_object()) {
            const auto& art = sess["artifacts"];
            auto take = [&](const char* key) -> std::string {
                if (!art.contains(key) || !art[key].is_string()) return {};
                return art[key].get<std::string>();
            };
            const std::string splat  = take("splat_path");
            const std::string mesh   = take("mesh_path");
            const std::string sparse = take("sparse_dir");
            if (!splat.empty())  st2.artifactSplatPath = resolveSessionPath(sid, splat);
            if (!mesh.empty())   st2.artifactMeshPath  = resolveSessionPath(sid, mesh);
            if (!sparse.empty()) st2.artifactSparseDir = resolveSessionPath(sid, sparse);
            st2.splatArtifactTransform = {};
            auto& transform = st2.splatArtifactTransform;
            transform.metric = art.contains("splat_transform_applied") &&
                art["splat_transform_applied"].is_boolean() &&
                art["splat_transform_applied"].get<bool>();
            if (transform.metric) {
                const auto t = sess.find("recon_transform");
                transform.valid = t != sess.end() && t->is_object() &&
                    t->contains("mm_per_unit") && (*t)["mm_per_unit"].is_number() &&
                    t->contains("rotation") && (*t)["rotation"].is_array() &&
                    (*t)["rotation"].size() == 3 && t->contains("translation_mm") &&
                    (*t)["translation_mm"].is_array() && (*t)["translation_mm"].size() == 3;
                if (transform.valid) {
                    transform.mmPerUnit = (*t)["mm_per_unit"].get<double>();
                    transform.valid = std::isfinite(transform.mmPerUnit) && transform.mmPerUnit > 0.0;
                    for (int row = 0; row < 3 && transform.valid; ++row) {
                        const auto& rot = (*t)["rotation"][row];
                        if (!rot.is_array() || rot.size() != 3 || !(*t)["translation_mm"][row].is_number()) {
                            transform.valid = false; break;
                        }
                        transform.translation[row] = (*t)["translation_mm"][row].get<double>();
                        transform.valid = std::isfinite(transform.translation[row]);
                        for (int col = 0; col < 3 && transform.valid; ++col) {
                            if (!rot[col].is_number()) { transform.valid = false; break; }
                            transform.rotation[row * 3 + col] = rot[col].get<double>();
                            transform.valid = std::isfinite(transform.rotation[row * 3 + col]);
                        }
                    }
                }
            }
            const bool metric = sess.contains("recon_transform") && sess["recon_transform"].is_object() &&
                sess["recon_transform"].value("source", std::string("identity")) != "identity";
            st2.splatUntransformed = metric && !splat.empty() &&
                take("splat_units") == "reconstruction" &&
                (!art.contains("splat_transform_applied") ||
                 !art["splat_transform_applied"].is_boolean() ||
                 !art["splat_transform_applied"].get<bool>());
        }
        const auto stamp = [](const std::string& path) {
            if (path.empty()) return std::string();
            std::error_code ec;
            const auto time = std::filesystem::last_write_time(path, ec);
            return ec ? std::string()
                      : std::to_string(static_cast<long long>(time.time_since_epoch().count()));
        };
        if (st2.artifactSparseDir.empty() && st2.artifactMeshPath.empty()) {
            SH().previewRequestKey.clear();
        } else {
            const auto sparse = std::filesystem::path(st2.artifactSparseDir);
            SH().previewRequestKey = st2.artifactSparseDir + "|" + st2.artifactMeshPath + "|" +
                SH().previewTransformKey + "|" + stamp((sparse / "points3D.bin").string()) +
                "|" + stamp((sparse / "points3D.txt").string()) + "|" + stamp(st2.artifactMeshPath);
#ifdef __EMSCRIPTEN__
            SH().previewRequestKey += "|" + artifactRefId(r.result, "mesh") +
                                      "|" + artifactRefId(r.result, "sparse_points");
#endif
        }

        // session.stages (§2) — the TOP stepper's per-step colour + reachability.
        if (sess.contains("stages") && sess["stages"].is_object()) {
            const auto& sj = sess["stages"];
            for (int i = 0; i < kStageCount; ++i) {
                const char* key = stageManifestKey((Stage)i);
                if (sj.contains(key) && sj[key].is_string())
                    st2.stages[i].badge = sj[key].get<std::string>();
            }
            const std::string reconBadge = sj.value("reconstruct", std::string());
            if (reconBadge == "done" || reconBadge == "complete") {
                st2.recon.done   = true;
                st2.recon.active = false;
                st2.recon.pct    = 100;
            }
        }

        // session.reference (§2) — the last existing-model search and the chosen model.
        if (sess.contains("reference") && sess["reference"].is_object())
            mirrorReference(sess["reference"]);

        // session.mesh_job — background reveng_splat_to_mesh (running → done | failed).
        if (sess.contains("mesh_job") && sess["mesh_job"].is_object()) {
            const auto& job = sess["mesh_job"];
            const std::string status = job.value("status", std::string());
            auto& sh = SH();
            if (status == "running") {
                sh.splatActionPending = true;
                sh.splatActionError = false;
                if (sh.splatActionMsg.empty())
                    sh.splatActionMsg = "Extracting an approximate mesh in the background...";
            } else if ((status == "done" || status == "failed") &&
                       job.value("finished_at", std::string()) != sh.meshJobFinished) {
                // Report only a job this view was waiting for, not one finished earlier.
                const bool waiting = sh.splatActionPending;
                sh.meshJobFinished = job.value("finished_at", std::string());
                if (waiting) {
                    sh.splatActionPending = false;
                    sh.splatActionError = status == "failed";
                    sh.splatActionMsg = status == "failed"
                        ? job.value("error", std::string("Mesh extraction failed."))
                        : "Pruned point cloud saved: " + job.value("points_path", std::string()) +
                          "  Approximate mesh: " + job.value("mesh_path", std::string());
                    S().lastListT = -100.0;
                }
            }
        }

        // Pick the intake lane ONCE per session from session.lane, so reopening a reference
        // session lands on its search, but the user's own lane switch is never overridden by
        // the next poll. Older sessions without a lane: a researched / datasheet-only session
        // is a reference session.
        if (st2.laneChosenFor != sid) {
            st2.laneChosenFor = sid;
            const std::string lane = sess.value("lane", std::string());
            const bool docOnly = sess.contains("stages") && sess["stages"].is_object() &&
                                 sess["stages"].value("datasheet", std::string()) == "done";
            const bool reference = lane == "reference" ||
                                   (lane.empty() && (docOnly || sess.contains("reference")));
            st2.sourceMode = reference ? SourceMode::Reference : SourceMode::Scan;
        }

        const std::string status = sess.value("status", std::string());
        if (status == "done" || status == "review" || status == "modeling") {
            // Artifacts may exist even if the reconstruct badge is still "idle" in old sessions.
            if (!st2.artifactSplatPath.empty() || !st2.artifactMeshPath.empty())
                st2.recon.done = true;
        }
    });
}

// Contract order for the §8.1 provisioning keys, so the capability list never reshuffles.
const char* const kProvKeys[] = { "colmap", "openmvs", "brush", "triposr", "vggt", "docling" };

// Human fix-it text for a backend the probe did not find. reveng_status() ships
// provisioning_detail{env_var,note}; this is the fallback when it does not.
const char* provFallbackHint(const std::string& key) {
    if (key == "colmap")  return "install COLMAP and set COLMAP_BIN \xe2\x80\x94 required for sparse reconstruction";
    if (key == "openmvs") return "installs from the app (Install missing tools) \xe2\x80\x94 dense mesh";
    if (key == "brush")   return "installs from the app (Install missing tools) \xe2\x80\x94 3D Gaussian splatting";
    if (key == "triposr") return "provision TripoSR weights \xe2\x80\x94 single-photo tier";
    if (key == "vggt")    return "provision VGGT weights \xe2\x80\x94 feed-forward sparse alternative to COLMAP";
    if (key == "docling") return "install docling \xe2\x80\x94 datasheet ingest";
    return "not provisioned";
}
void kickInstallTools(AppState& st);


// Async: reveng_status() capability + provisioning probe. Throttled; "Re-check" forces it.
// This is what lets the Reconstruct stage disable Run with a real reason instead of letting the
// user click through into a backend_not_provisioned failure. Only a `manual` re-check shows as
// busy in the UI; the 30 s background probe is silent.
void loadCapabilities(AppState& st, bool force = false, bool manual = false) {
    if (!st.workerStarted) return;
    CapabilityState& c = S().caps;
    if (c.pending) return;
    const double now = ImGui::GetTime();
    const double throttle = c.installRunning ? 1.0 : c.loaded ? 30.0 : 6.0;
    if (!force && now - c.lastT < throttle) return;
    c.pending = true;
    c.manual = manual;
    c.lastT = now;
    st.rpc.call("reveng_status", nlohmann::json::object(), [&st](const core::RpcResult& r) {
        CapabilityState& c2 = S().caps;
        c2.pending = false;
        c2.manual = false;
        if (!r.ok) {
            c2.err = r.err.empty() ? "reveng_status unreachable" : r.err;
            return;
        }
        if (!r.result.is_object() || !r.result.value("ok", false)) {
            c2.err = r.result.is_object()
                         ? r.result.value("reason", std::string("reveng_status failed"))
                         : std::string("reveng_status failed");
            return;
        }
        c2.err.clear();
        c2.loaded = true;
        if (r.result.contains("capabilities") && r.result["capabilities"].is_object()) {
            const auto& cj = r.result["capabilities"];
            c2.sparse      = cj.value("sparse", false);
            c2.dense       = cj.value("dense", false);
            c2.splat       = cj.value("splat", false);
            c2.singlePhoto = cj.value("single_photo", false);
            c2.webcam      = cj.value("webcam", false);
            c2.gpu         = cj.value("gpu", false);
        }
        c2.artifacts.clear();
        const nlohmann::json* prov = nullptr;
        if (r.result.contains("provisioning") && r.result["provisioning"].is_object())
            prov = &r.result["provisioning"];
        const nlohmann::json* detail = nullptr;
        if (r.result.contains("provisioning_detail") && r.result["provisioning_detail"].is_object())
            detail = &r.result["provisioning_detail"];
        auto pushArtifact = [&](const std::string& key, bool present) {
            CapabilityState::Artifact a;
            a.key = key;
            a.present = present;
            if (detail && detail->contains(key) && (*detail)[key].is_object()) {
                const auto& d = (*detail)[key];
                std::string env, note, lic;
                if (d.contains("env_var") && d["env_var"].is_string()) env = d["env_var"].get<std::string>();
                if (d.contains("note")    && d["note"].is_string())    note = d["note"].get<std::string>();
                if (d.contains("license") && d["license"].is_string()) lic  = d["license"].get<std::string>();
                if (!note.empty()) a.hint = note;
                if (!env.empty())  a.hint += (a.hint.empty() ? "" : "  ") + std::string("set ") + env;
                if (!lic.empty())  a.hint += (a.hint.empty() ? "" : "  \xc2\xb7  ") + lic;
            }
            if (a.hint.empty()) a.hint = present ? "provisioned" : provFallbackHint(key);
            c2.artifacts.push_back(std::move(a));
        };
        if (prov) {
            for (const char* k : kProvKeys)
                if (prov->contains(k)) pushArtifact(k, (*prov)[k].get<bool>());
            // Any key the backend grew that this build does not know about — still show it.
            for (auto it = prov->begin(); it != prov->end(); ++it) {
                bool known = false;
                for (const char* k : kProvKeys) known = known || it.key() == k;
                if (!known && it->is_boolean()) pushArtifact(it.key(), it->get<bool>());
            }
        }
        c2.banners.clear();
        if (r.result.contains("banners") && r.result["banners"].is_array())
            for (const auto& b : r.result["banners"])
                if (b.is_string()) c2.banners.push_back(b.get<std::string>());
        // Background install progress (reveng_install_tools).
        if (r.result.contains("install") && r.result["install"].is_object()) {
            const auto& ij = r.result["install"];
            c2.installRunning = ij.value("running", false);
            c2.installTool    = ij.value("tool", std::string());
            c2.installMsg     = ij.value("message", std::string());
            c2.installDone    = ij.value("done", 0.0);
            c2.installTotal   = ij.value("total", 0.0);
            if (ij.contains("errors") && ij["errors"].is_object() && !ij["errors"].empty()) {
                const auto first = ij["errors"].begin();
                c2.installErr = first.key() + ": " +
                                (first.value().is_string() ? first.value().get<std::string>()
                                                           : std::string("install failed"));
            } else if (!c2.installRunning && !c2.installPending) {
                c2.installErr.clear();
            }
        }
        // First time the app sees Brush / OpenMVS missing, fetch them: nobody should have to run
        // a provisioning script. One automatic attempt per run; the card offers Retry after it.
        bool toolMissing = false;
        for (const auto& a : c2.artifacts)
            toolMissing = toolMissing || ((a.key == "brush" || a.key == "openmvs") && !a.present);
        if (toolMissing && !c2.installAutoTried && !c2.installRunning && !c2.installPending) {
            c2.installAutoTried = true;
            kickInstallTools(st);
        }
    });
}

// Async: reveng_install_tools — the worker downloads Brush / OpenMVS on a background thread and
// reports progress through reveng_status().install, which loadCapabilities polls at 1 Hz.
void kickInstallTools(AppState& st) {
    if (!st.workerStarted) return;
    CapabilityState& c = S().caps;
    if (c.installPending || c.installRunning) return;
    c.installPending = true;
    c.installErr.clear();
    st.rpc.call("reveng_install_tools", nlohmann::json::object(), [&st](const core::RpcResult& r) {
        CapabilityState& c2 = S().caps;
        c2.installPending = false;
        if (!r.ok) {
            c2.installErr = r.err.empty() ? "reveng_install_tools unreachable" : r.err;
        } else if (r.result.is_object() && !r.result.value("ok", false)) {
            c2.installErr = r.result.value("hint", r.result.value("reason", std::string("install refused")));
        } else if (r.result.is_object() && r.result.contains("install") && r.result["install"].is_object()) {
            c2.installRunning = r.result["install"].value("running", false);
        }
        loadCapabilities(st, /*force=*/true);
    });
}

// Publish only a successfully parsed cloud; a failed refresh never destroys the last preview.
void ensureSplatForSession(RevEngState& s) {
    auto& sh = SH();
    s.splatPreviewCurrent = false;
    const std::string* wantPath = s.artifactSplatPath.empty() ? &s.gaussian.splatPath : &s.artifactSplatPath;
    std::string fixturePath;
    if (wantPath->empty() && s.recon.done && std::getenv("CHUDWARE_SHOT")) {
        const char* repo = std::getenv("CHUDWARE_REPO");
        fixturePath = std::string(repo ? repo : ".") + "/host/tests/fixtures/splat_tiny.ply";
        wantPath = &fixturePath;
    }
    const std::string& want = *wantPath;
    const auto transform = want == s.artifactSplatPath ? s.splatArtifactTransform : SplatPreviewTransform{};
    static const std::string rawKey = "raw";
    const std::string& transformKey = transform.metric ? sh.previewTransformKey : rawKey;
#ifdef __EMSCRIPTEN__
    const std::string ref = !s.artifactSplatPath.empty() ? sh.splatArtifactId : sh.gaussianArtifactId;
    const std::string fetchKey = want + "|" + ref + "|" + transformKey + "|" +
                                 std::to_string(s.gaussian.artifactRevision);
    if (sh.splatFetchPending &&
        (sh.splatFetchEpoch != sh.sessionEpoch || sh.splatFetchKey != fetchKey)) {
        sh.splatFetchPending = false;
        ++sh.splatFetchSerial;
    }
#endif
    if (sh.splatLoad.valid()) {
        if (sh.splatLoad.wait_for(std::chrono::seconds(0)) != std::future_status::ready) return;
        auto loaded = sh.splatLoad.get();
        bool sameArtifact = true;
#ifdef __EMSCRIPTEN__
        sameArtifact = loaded.artifactId == ref;
#endif
        if (sameArtifact && loaded.epoch == sh.sessionEpoch && loaded.path == want &&
            loaded.transformKey == transformKey && loaded.revision == s.gaussian.artifactRevision) {
            if (!loaded.error.empty() || loaded.cloud.empty()) {
                sh.splatLoadError = "Cannot load Gaussian preview: " +
                    (loaded.error.empty() ? std::string("empty artifact") : loaded.error);
            } else {
                s.splat.setCloud(std::move(loaded.cloud));
                s.splatLoaded = true;
                s.splatDisplayedTransform = loaded.transform;
                s.splatPickMarks.setCloud({});
                ++s.splatPreviewRevision;
                sh.splatLoadError.clear();
                const std::string directory = std::filesystem::path(loaded.path).parent_path().string();
                const bool newGauge = loaded.viewCamera.id != sh.loadedViewCamera.id;
                sh.loadedViewCamera = std::move(loaded.viewCamera);
                if (!sh.loadedViewCamera.id.empty()) {
                    if (newGauge || !sh.gaussianCameraFitted) restoreGaussianCaptureCamera();
                } else if (newGauge || !sh.gaussianCameraFitted ||
                           sh.gaussianArtifactDirectory != directory || sh.displayedSplatPath != loaded.path ||
                           sh.displayedSplatTransformKey != loaded.transformKey) {
                    sh.gaussianRight = {1, 0, 0};
                    sh.gaussianUp = {0, 1, 0};
                    sh.gaussianBack = {0, 0, 1};
                    sh.gaussianYaw = 0.6f;
                    sh.gaussianPitch = 0.25f;
                    fitGaussianCamera();
                }
                sh.gaussianArtifactDirectory = directory;
                sh.displayedSplatPath = loaded.path;
                sh.displayedSplatTransformKey = loaded.transformKey;
#ifdef __EMSCRIPTEN__
                sh.splatRefDisplayed = loaded.artifactId;
#endif
            }
        }
    }
    if (want.empty()) return;
    if (s.splatPathTried == want && sh.splatRevisionTried == s.gaussian.artifactRevision &&
        sh.splatTransformTried == transformKey
#ifdef __EMSCRIPTEN__
        && sh.splatRefTried == ref
#endif
        ) {
        s.splatPreviewCurrent = s.splatLoaded && sh.splatLoadError.empty() &&
            sh.displayedSplatPath == want && sh.displayedSplatTransformKey == transformKey
#ifdef __EMSCRIPTEN__
            && sh.splatRefDisplayed == ref && !ref.empty()
#endif
            ;
        return;
    }
    s.splatPathTried = want;
    sh.splatRevisionTried = s.gaussian.artifactRevision;
    sh.splatTransformTried = transformKey;
    const auto epoch = sh.sessionEpoch;
    const auto revision = s.gaussian.artifactRevision;
    const auto viewCamera = want == s.gaussian.splatPath && !s.artifactIsImportedSplat && !transform.metric
        ? s.gaussian.viewCamera : GaussianViewCamera{};
#ifdef __EMSCRIPTEN__
    sh.splatRefTried = ref;
    if (ref.empty()) {
        sh.splatLoadError = "The worker did not provide an authorized Gaussian artifact.";
        return;
    }
    sh.splatFetchPending = true;
    sh.splatFetchEpoch = epoch;
    sh.splatFetchKey = fetchKey;
    sh.splatFetchProgress = -1.0f;
    sh.splatLoadError.clear();
    const auto ticket = ++sh.splatFetchSerial;
    const bool started = chudware::webFetchAsync("/reveng/artifact/" + ref,
        [want, epoch, revision, viewCamera, transform, transformKey, ref, ticket]
        (std::vector<uint8_t> bytes, std::string error) mutable {
            auto& state = SH();
            if (state.splatFetchSerial != ticket || state.sessionEpoch != epoch) return;
            state.splatFetchPending = false;
            if (!error.empty()) {
                state.splatLoadError = "Cannot fetch Gaussian preview: " + error;
                return;
            }
            state.splatLoad = std::async(std::launch::async,
                [want, epoch, revision, viewCamera, transform, transformKey, ref,
                 bytes = std::move(bytes)]() mutable {
                    ShellState::SplatLoad result;
                    result.path = want; result.epoch = epoch; result.revision = revision;
                    result.viewCamera = viewCamera; result.transform = transform;
                    result.transformKey = transformKey; result.artifactId = ref;
                    try {
                        if (!render::parseSplatPly(reinterpret_cast<const char*>(bytes.data()),
                                                   bytes.size(), result.cloud, result.error) &&
                            result.error.empty()) result.error = "Invalid Gaussian PLY";
                    } catch (const std::exception& e) {
                        result.error = e.what();
                    }
                    return result;
                });
        },
        [ticket](float fraction) {
            if (SH().splatFetchSerial == ticket) SH().splatFetchProgress = fraction;
        });
    if (!started) {
        sh.splatFetchPending = false;
        sh.splatLoadError = "Cannot start Gaussian artifact download.";
    }
#else
    sh.splatLoad = std::async(std::launch::async, [want, epoch, revision, viewCamera, transform, transformKey] {
        ShellState::SplatLoad result;
        result.path = want;
        result.epoch = epoch;
        result.revision = revision;
        result.viewCamera = viewCamera;
        result.transform = transform;
        result.transformKey = transformKey;
        try {
            if (!render::loadSplatPly(want, result.cloud, result.error) && result.error.empty())
                result.error = "Invalid Gaussian PLY";
        } catch (const std::exception& error) {
            result.error = error.what();
        }
        return result;
    });
#endif
}

// Decode the shared COLMAP model in the worker; derive native calibration points
// from the display-space preview without requesting a second point array.
void ensureSparseAndMeshPreview(AppState& st) {
    auto& s = S();
    auto& sh = SH();
    if (!st.workerStarted || s.selected < 0 || s.selected >= (int)s.sessions.size()) return;
    const std::string& key = sh.previewRequestKey;
    if (key.empty() && !sh.previewTriedKey.empty()) {
        sh.previewTriedKey.clear();
        sh.meshSourcePath.clear();
        sh.sparsePreviewWarnings.clear();
        sh.meshSourceKey.clear();
        s.sparsePoints.clear();
        s.sparseNativePoints.clear();
        s.rePoints.setCloud({});
        s.rePoints.setFrusta({});
        s.meshPreview.reset();
        s.sparsePreviewError.clear();
        s.meshPreviewError.clear();
        ++s.sparsePreviewRevision;
        ++s.meshPreviewRevision;
    }
    if (!key.empty() && key != sh.previewTriedKey && !sh.previewPending) {
        sh.previewPending = true;
        sh.previewTriedKey = key;
        sh.meshSourcePath.clear();
        sh.meshSourceKey.clear();
        sh.meshLoadPath.clear();
        sh.sparsePreviewWarnings.clear();
        s.sparsePreviewError.clear();
        s.meshPreviewError.clear();
        s.sparsePoints.clear();
        s.sparseNativePoints.clear();
        s.rePoints.setCloud({});
        s.rePoints.setFrusta({});
        ++s.sparsePreviewRevision;
        s.meshPreview.reset();
        ++s.meshPreviewRevision;
        const auto sid = s.sessions[s.selected].session_id;
        const auto epoch = sh.sessionEpoch;
        st.rpc.call("reveng_sparse_preview", {{"session_id", sid}},
                    [sid, epoch, key](const core::RpcResult& r) {
            if (!selectedSession(sid) || SH().sessionEpoch != epoch) return;
            auto& sh2 = SH();
            auto& s2 = S();
            sh2.previewPending = false;
            if (key != sh2.previewRequestKey) return;
            if (!r.ok || !r.result.is_object() || !r.result.value("ok", false)) {
                s2.sparsePreviewError = r.ok && r.result.is_object()
                    ? r.result.value("reason", std::string("Sparse preview unavailable"))
                    : (r.err.empty() ? std::string("Sparse preview unavailable") : r.err);
                return;
            }
            s2.sparsePreviewError.clear();
            if (r.result.contains("warnings") && r.result["warnings"].is_array()) {
                for (const auto& warning : r.result["warnings"]) {
                    if (!warning.is_string()) continue;
                    if (!sh2.sparsePreviewWarnings.empty()) sh2.sparsePreviewWarnings += " ";
                    sh2.sparsePreviewWarnings += warning.get<std::string>();
                }
            }
            auto readRow = [](const nlohmann::json& row, size_t n, float* out) {
                if (!row.is_array() || row.size() < n) return false;
                for (size_t i = 0; i < n; ++i) {
                    if (!row[i].is_number()) return false;
                    const double v = row[i].get<double>();
                    if (!std::isfinite(v) || std::fabs(v) > std::numeric_limits<float>::max()) return false;
                    out[i] = float(v);
                }
                return true;
            };
            Vec3 lo{std::numeric_limits<float>::max(), std::numeric_limits<float>::max(),
                    std::numeric_limits<float>::max()};
            Vec3 hi{-lo.x, -lo.y, -lo.z};
            bool haveBounds = false;
            auto extend = [&](float x, float y, float z) {
                lo.x = std::min(lo.x, x); lo.y = std::min(lo.y, y); lo.z = std::min(lo.z, z);
                hi.x = std::max(hi.x, x); hi.y = std::max(hi.y, y); hi.z = std::max(hi.z, z);
                haveBounds = true;
            };
            const auto points = r.result.find("points");
            const auto units = r.result.find("units");
            const bool reconstruction = units != r.result.end() && units->is_string() &&
                                        *units == "reconstruction";
            const bool metric = units != r.result.end() && units->is_string() && *units == "mm";
            double scale = 1.0;
            std::array<double, 9> rotation{};
            std::array<double, 3> translation{};
            bool nativeAvailable = reconstruction;
            const auto transform = r.result.find("recon_transform");
            if (metric && transform != r.result.end() && transform->is_object()) {
                const auto s = transform->find("mm_per_unit");
                const auto r = transform->find("rotation");
                const auto t = transform->find("translation_mm");
                nativeAvailable = s != transform->end() && s->is_number() &&
                    r != transform->end() && r->is_array() && r->size() == 3 &&
                    t != transform->end() && t->is_array() && t->size() == 3;
                if (nativeAvailable) {
                    scale = s->get<double>();
                    nativeAvailable = std::isfinite(scale) && scale > 0.0;
                    for (size_t row = 0; row < 3 && nativeAvailable; ++row) {
                        if (!(*r)[row].is_array() || (*r)[row].size() != 3 ||
                            !(*t)[row].is_number()) {
                            nativeAvailable = false;
                            break;
                        }
                        translation[row] = (*t)[row].get<double>();
                        nativeAvailable = std::isfinite(translation[row]);
                        for (size_t col = 0; col < 3 && nativeAvailable; ++col) {
                            if (!(*r)[row][col].is_number()) {
                                nativeAvailable = false;
                                break;
                            }
                            rotation[row * 3 + col] = (*r)[row][col].get<double>();
                            nativeAvailable = std::isfinite(rotation[row * 3 + col]);
                        }
                    }
                    // The transpose is an inverse only for a proper rotation.
                    for (size_t a = 0; a < 3 && nativeAvailable; ++a) {
                        for (size_t b = 0; b < 3 && nativeAvailable; ++b) {
                            double dot = 0.0;
                            for (size_t k = 0; k < 3; ++k)
                                dot += rotation[k * 3 + a] * rotation[k * 3 + b];
                            nativeAvailable = std::isfinite(dot) &&
                                std::fabs(dot - (a == b ? 1.0 : 0.0)) <= 1e-6;
                        }
                    }
                    if (nativeAvailable) {
                        const double det = rotation[0] * (rotation[4] * rotation[8] - rotation[5] * rotation[7])
                            - rotation[1] * (rotation[3] * rotation[8] - rotation[5] * rotation[6])
                            + rotation[2] * (rotation[3] * rotation[7] - rotation[4] * rotation[6]);
                        nativeAvailable = std::isfinite(det) && std::fabs(det - 1.0) <= 1e-6;
                    }
                }
            }
            if (points != r.result.end() && points->is_array()) {
                for (const auto& row : *points) {
                    float p[6];
                    if (!readRow(row, 6, p)) continue;
                    s2.sparsePoints.push_back({p[0], p[1], p[2], uint8_t(std::clamp(p[3], 0.0f, 255.0f)),
                                               uint8_t(std::clamp(p[4], 0.0f, 255.0f)),
                                               uint8_t(std::clamp(p[5], 0.0f, 255.0f))});
                    if (nativeAvailable) {
                        std::array<double, 3> n{};
                        for (size_t col = 0; col < 3; ++col) {
                            if (metric) {
                                for (size_t k = 0; k < 3; ++k)
                                    n[col] += rotation[k * 3 + col] *
                                        (row[k].get<double>() - translation[k]);
                                n[col] /= scale;
                            } else {
                                n[col] = row[col].get<double>();
                            }
                            if (!std::isfinite(n[col])) nativeAvailable = false;
                        }
                        if (nativeAvailable) s2.sparseNativePoints.push_back(n);
                        else s2.sparseNativePoints.clear();
                    }
                    extend(p[0], p[1], p[2]);
                }
            }
            std::vector<render::CameraFrustum> frusta;
            const auto cameras = r.result.find("frusta");
            if (cameras != r.result.end() && cameras->is_array()) {
                for (const auto& row : *cameras) {
                    float p[15];
                    if (!readRow(row, 15, p)) continue;
                    frusta.push_back({p[0], p[1], p[2], p[3], p[4], p[5], p[6], p[7], p[8],
                                      p[9], p[10], p[11], p[12], p[13], p[14]});
                    extend(p[0], p[1], p[2]);
                }
            }
            if (haveBounds) {
                s2.sparseCenter = (lo + hi) * 0.5f;
                s2.sparseRadius = std::max(length(hi - lo) * 0.5f, 1e-6f);
            }
            s2.rePoints.setCloud(s2.sparsePoints);
            s2.rePoints.setFrusta(frusta);
            ++s2.sparsePreviewRevision;
            if (r.result.contains("mesh_preview_path") && r.result["mesh_preview_path"].is_string()) {
                sh2.meshSourcePath = r.result["mesh_preview_path"].get<std::string>();
                if (!sh2.meshSourcePath.empty()) {
                    sh2.meshSourceKey = key + "|" + sh2.meshSourcePath;
#ifdef __EMSCRIPTEN__
                    sh2.meshArtifactId = artifactRefId(r.result, "mesh_preview");
                    sh2.meshSourceKey += "|" + sh2.meshArtifactId;
#endif
                }
            } else if (!s2.artifactMeshPath.empty()) {
                s2.meshPreviewError = "The worker returned no mesh preview artifact.";
            }
        }, core::kLongEngineCallTimeoutMs);
    }
    if (sh.meshLoad.valid() && sh.meshLoad.wait_for(std::chrono::seconds(0)) == std::future_status::ready) {
        auto loaded = sh.meshLoad.get();
        if (loaded.epoch == sh.sessionEpoch && loaded.key == sh.meshSourceKey) {
            s.meshPreview = std::move(loaded.doc);
            s.meshPreviewError = loaded.error;
            ++s.meshPreviewRevision;
        }
    }
#ifdef __EMSCRIPTEN__
    if (sh.meshFetchPending &&
        (sh.meshFetchEpoch != sh.sessionEpoch || sh.meshFetchKey != sh.meshSourceKey)) {
        sh.meshFetchPending = false;
        ++sh.meshFetchSerial;
    }
    if (!sh.meshLoad.valid() && !sh.meshFetchPending && !sh.meshSourceKey.empty() &&
        sh.meshLoadPath != sh.meshSourceKey) {
        sh.meshLoadPath = sh.meshSourceKey;
        const auto path = sh.meshSourcePath, sourceKey = sh.meshSourceKey;
        const auto epoch = sh.sessionEpoch;
        if (sh.meshArtifactId.empty()) {
            s.meshPreviewError = "The worker did not provide an authorized mesh preview.";
        } else {
            sh.meshFetchPending = true;
            sh.meshFetchEpoch = epoch;
            sh.meshFetchKey = sourceKey;
            sh.meshFetchProgress = -1.0f;
            s.meshPreviewError.clear();
            const auto ticket = ++sh.meshFetchSerial;
            const bool started = chudware::webFetchAsync("/reveng/artifact/" + sh.meshArtifactId,
                [path, sourceKey, epoch, ticket](std::vector<uint8_t> bytes, std::string error) mutable {
                    auto& state = SH();
                    if (state.meshFetchSerial != ticket || state.sessionEpoch != epoch) return;
                    state.meshFetchPending = false;
                    if (!error.empty()) {
                        S().meshPreviewError = "Cannot fetch mesh preview: " + error;
                        return;
                    }
                    state.meshLoad = std::async(std::launch::async,
                        [path, sourceKey, epoch, bytes = std::move(bytes)]() mutable {
                            ShellState::MeshLoad out;
                            out.path = path; out.key = sourceKey; out.epoch = epoch;
                            Mesh mesh;
                            try {
                                if (loadBinarySTLBytes(bytes.data(), bytes.size(), mesh, out.error) &&
                                    !mesh.empty()) {
                                    out.doc = std::make_shared<render::DocMesh>();
                                    render::BodyMesh body;
                                    body.pos = std::move(mesh.positions);
                                    body.nrm = std::move(mesh.normals);
                                    body.idx = std::move(mesh.indices);
                                    body.n_verts = uint32_t(body.pos.size() / 3);
                                    body.n_tris = uint32_t(body.idx.size() / 3);
                                    out.doc->bodies.push_back(std::move(body));
                                } else if (out.error.empty()) {
                                    out.error = "Mesh preview contains no triangles.";
                                }
                            } catch (const std::exception& e) {
                                out.error = e.what();
                            }
                            return out;
                        });
                },
                [ticket](float fraction) {
                    if (SH().meshFetchSerial == ticket) SH().meshFetchProgress = fraction;
                });
            if (!started) {
                sh.meshFetchPending = false;
                s.meshPreviewError = "Cannot start mesh artifact download.";
            }
        }
    }
#else
    if (!sh.meshLoad.valid() && !sh.meshSourceKey.empty() && sh.meshLoadPath != sh.meshSourceKey) {
        sh.meshLoadPath = sh.meshSourceKey;
        const auto path = sh.meshSourcePath, sourceKey = sh.meshSourceKey;
        const auto epoch = sh.sessionEpoch;
        sh.meshLoad = std::async(std::launch::async, [path, sourceKey, epoch] {
            ShellState::MeshLoad out;
            out.path = path; out.key = sourceKey; out.epoch = epoch;
            Mesh mesh;
            if (loadBinarySTL(path, mesh, out.error) && !mesh.empty()) {
                out.doc = std::make_shared<render::DocMesh>();
                render::BodyMesh body;
                body.pos = std::move(mesh.positions);
                body.nrm = std::move(mesh.normals);
                body.idx = std::move(mesh.indices);
                body.n_verts = uint32_t(body.pos.size() / 3);
                body.n_tris = uint32_t(body.idx.size() / 3);
                out.doc->bodies.push_back(std::move(body));
            } else if (out.error.empty()) out.error = "Mesh preview contains no triangles.";
            return out;
        });
    }
#endif
}

// Snapshot is a lock-free, read-only view of an existing document. Never activate
// the built part or disturb the document the user is currently editing.
void ensureParametricPreview(AppState& st) {
    auto& s = S();
    auto& sh = SH();
    const std::string& docId = s.model.descDocId;
    if (docId != sh.parametricDocId) {
        sh.parametricDocId = docId;
        sh.parametricSourcePath.clear();
        sh.parametricSourceKey.clear();
        sh.parametricLoadPath.clear();
        sh.parametricPollPending = false;
        sh.parametricPollT = -100.0;
        s.parametricPreview.reset();
        s.parametricPreviewError.clear();
        ++s.parametricPreviewRevision;
    }
    if (!docId.empty() && st.workerStarted && !sh.parametricPollPending &&
        ImGui::GetTime() - sh.parametricPollT >= 5.0) {
        sh.parametricPollPending = true;
        sh.parametricPollT = ImGui::GetTime();
        const auto epoch = sh.sessionEpoch;
        st.rpc.call("doc_snapshot", {{"doc_id", docId}}, [docId, epoch](const core::RpcResult& r) {
            if (epoch != SH().sessionEpoch || docId != SH().parametricDocId) return;
            SH().parametricPollPending = false;
            if (!r.ok || !r.result.is_object() || !r.result.value("has_snapshot", false)) {
                S().parametricPreviewError = "Built document preview is not available from the document snapshot.";
                return;
            }
            const std::string path = r.result.value("aicm_path", std::string());
            if (path.empty()) {
                S().parametricPreviewError = "The built document has no tessellated preview yet.";
                return;
            }
            SH().parametricSourcePath = path;
            SH().parametricSourceKey = docId + "|" + path + "|" +
                std::to_string(r.result.value("revision", -1));
            S().parametricPreviewError.clear();
        }, 15000);
    }
    if (sh.parametricLoad.valid() &&
        sh.parametricLoad.wait_for(std::chrono::seconds(0)) == std::future_status::ready) {
        auto loaded = sh.parametricLoad.get();
        if (loaded.epoch == sh.sessionEpoch && loaded.key == sh.parametricSourceKey) {
            s.parametricPreview = std::move(loaded.doc);
            s.parametricPreviewError = loaded.error;
            ++s.parametricPreviewRevision;
        }
    }
    if (!sh.parametricLoad.valid() && !sh.parametricSourceKey.empty() &&
        sh.parametricLoadPath != sh.parametricSourceKey) {
        sh.parametricLoadPath = sh.parametricSourceKey;
        const auto path = sh.parametricSourcePath, key = sh.parametricSourceKey;
        const auto epoch = sh.sessionEpoch;
        sh.parametricLoad = std::async(std::launch::async, [path, key, epoch] {
            ShellState::MeshLoad out;
            out.path = path; out.key = key; out.epoch = epoch;
            auto doc = std::make_shared<render::DocMesh>();
            if (render::loadAicmFile(path, *doc, out.error) && !doc->empty()) out.doc = std::move(doc);
            else if (out.error.empty()) out.error = "Built document preview contains no geometry.";
            return out;
        });
    }
}

void pollGaussian(AppState& st) {
    // Retry only actions never dispatched while disconnected, never an unacknowledged RPC.
    for (const auto& entry : SH().gaussianRuns) drainGaussianActions(st, entry.second);
    auto& s = S();
    auto& sh = SH();
    if (!st.workerStarted || s.selected < 0 || s.selected >= (int)s.sessions.size() ||
        sh.gaussianPollPending || s.gaussian.pending) return;
    const double now = ImGui::GetTime();
    if (now - sh.gaussianPollT < (gaussianRunning(s.gaussian) ? 0.75 : 3.0)) return;
    sh.gaussianPollT = now;
    sh.gaussianPollPending = true;
    const auto sid = s.sessions[s.selected].session_id;
    const auto epoch = sh.sessionEpoch;
    const auto serial = sh.gaussianActionSerial;
    st.rpc.call("reveng_splat_status", {{"session_id", sid}}, [sid, epoch, serial](const core::RpcResult& r) {
        if (!selectedSession(sid) || SH().sessionEpoch != epoch) return;
        SH().gaussianPollPending = false;
        if (S().gaussian.pending || SH().gaussianActionSerial != serial) return;
        if (!r.ok || !r.result.is_object()) {
            S().gaussian.error = r.err.empty() ? "Gaussian status unavailable; check the local worker." : r.err;
            return;
        }
        if (r.result.value("reason", std::string()) == "no_splat_job" && S().gaussian.runId.empty()) {
            SH().gaussianHydrated = true;
            return;  // An untouched session is ready to start, not a failed training run.
        }
        mirrorGaussian(r.result, !SH().gaussianHydrated);
        SH().gaussianHydrated = true;
    });
}

void gaussianAction(AppState& st, const std::string& sid, const char* method,
                    nlohmann::json args,
                    const std::shared_ptr<GaussianRunControl>& explicitRun = nullptr) {
    if (!explicitRun && (!selectedSession(sid) || S().gaussian.pending)) return;
    const auto run = explicitRun ? explicitRun : S().gaussian.control;
    if (!st.workerStarted) {
        if (selectedSession(sid))
            S().gaussian.error = "Local worker unavailable. Start or reconnect the worker before training.";
        return;
    }
    if (run) run->pending = true;
    if (selectedSession(sid)) {
        S().gaussian.pending = true;
        S().gaussian.error.clear();
    }
    ++SH().gaussianActionSerial;
    args["session_id"] = sid;
    const auto epoch = SH().sessionEpoch;
    const bool starting = std::strcmp(method, "reveng_start_splat") == 0;
    const bool terminalAction = explicitRun && !starting;
    st.rpc.call(method, args, [&st, sid, epoch, run, starting, terminalAction](const core::RpcResult& r) {
        const bool ok = r.ok && r.result.is_object() && r.result.value("ok", false);
        if (run) {
            run->pending = false;
            if (starting) {
                run->starting = false;
                run->startFailed = !ok;
                if (!ok) run->requested = GaussianRunControl::Action::None;
            } else if (terminalAction) {
                run->acknowledged = ok;
            }
        }
        const bool current = selectedSession(sid) &&
            (run ? S().gaussian.control == run : SH().sessionEpoch == epoch);
        if (current) {
            S().gaussian.pending = false;
            SH().gaussianPollT = -100.0;
            if (!r.ok || !r.result.is_object()) {
                S().gaussian.error = r.err.empty() ? "Gaussian request failed; check the local worker." : r.err;
            } else {
                mirrorGaussian(r.result, ok);
                SH().gaussianHydrated = true;
                if (!ok && S().gaussian.error.empty())
                    S().gaussian.error = "Gaussian request was rejected; no new artifact was published.";
            }
            if (starting && !ok) S().gaussian.status = "failed";
        }
        // This is control-plane work, not a selected-session UI update. It must run even
        // after the camera, workspace or original session mirror has been torn down.
        if (run) drainGaussianActions(st, run);
    });
}

}  // namespace

void drainGaussianActions(AppState& st, const std::shared_ptr<GaussianRunControl>& run) {
    using Action = GaussianRunControl::Action;
    if (!run || run->pending || run->starting || run->startFailed || !st.workerStarted) return;
    const auto it = SH().gaussianRuns.find(run->sid);
    if (it == SH().gaussianRuns.end() || it->second != run) return;
    const char* method = nullptr;
    if (run->requested == Action::Cancel && !run->cancelSent) {
        run->cancelSent = true;
        method = "reveng_cancel_splat";
    } else if (run->requested == Action::Finish && !run->finishSent &&
               !run->cancelSent && run->pendingIngest == 0) {
        run->finishSent = true;
        method = "reveng_finish_splat";
    }
    if (!method) return;
    run->acknowledged = false;
    gaussianAction(st, run->sid, method, nlohmann::json::object(), run);
}

void finishGaussianRun(AppState& st, const std::shared_ptr<GaussianRunControl>& run) {
    if (!run || run->startFailed || run->requested == GaussianRunControl::Action::Cancel) return;
    run->requested = GaussianRunControl::Action::Finish;
    drainGaussianActions(st, run);
}

Renderer::CameraXform gaussianPreviewCamera(int width, int height) {
    auto& sh = SH();
    if (!sh.gaussianCameraFitted) fitGaussianCamera();
    Renderer::CameraXform cam;
    cam.vpWidth = cam.ssWidth = std::max(width, 1);
    cam.vpHeight = cam.ssHeight = std::max(height, 1);
    const float aspect = float(cam.vpWidth) / float(cam.vpHeight);
    const float halfFov = Renderer::kFovDeg * 0.00872664626f;
    const float limitingFov = std::atan(std::tan(halfFov) * std::min(aspect, 1.0f));
    cam.radius = sh.gaussianRadius;
    cam.zoom = sh.gaussianZoom;
    cam.dist = sh.gaussianCaptureProjection ? sh.loadedViewCamera.focusDistance / cam.zoom
        : cam.radius * 1.1f / (std::sin(limitingFov) * cam.zoom);
    cam.center = sh.gaussianCenter;
    cam.dir = sh.gaussianRight * (std::cos(sh.gaussianPitch) * std::sin(sh.gaussianYaw)) +
              sh.gaussianUp * std::sin(sh.gaussianPitch) +
              sh.gaussianBack * (std::cos(sh.gaussianPitch) * std::cos(sh.gaussianYaw));
    cam.eye = cam.center + cam.dir * cam.dist;
    cam.right = normalize(cross(sh.gaussianUp, cam.dir));
    cam.up = cross(cam.dir, cam.right);
    cam.orthographic = false;
    cam.view = lookAt(cam.eye, cam.center, cam.up);
    float farPlane = cam.dist + cam.radius * 100.0f;
    if (sh.gaussianCaptureProjection) {
        Vec3 lo, hi;
        if (S().splat.bounds(lo, hi))
            farPlane = std::max(farPlane, length((lo + hi) * 0.5f - cam.eye) + length(hi - lo));
    }
    cam.proj = perspective(halfFov * 2.0f, aspect, cam.radius * 0.0001f, farPlane);
    if (sh.gaussianCaptureProjection) {
        const auto& k = sh.loadedViewCamera.intrinsics;
        const float scale = std::min(float(cam.vpWidth) / k[4], float(cam.vpHeight) / k[5]);
        const float cx = k[2] * scale + (cam.vpWidth - k[4] * scale) * 0.5f;
        const float cy = k[3] * scale + (cam.vpHeight - k[5] * scale) * 0.5f;
        cam.proj.m[0] = 2.0f * k[0] * scale / cam.vpWidth;
        cam.proj.m[5] = 2.0f * k[1] * scale / cam.vpHeight;
        // Source pixels are top-left/down; OpenGL clip coordinates are bottom-left/up.
        cam.proj.m[8] = 1.0f - 2.0f * cx / cam.vpWidth;
        cam.proj.m[9] = 2.0f * cy / cam.vpHeight - 1.0f;
    }
    cam.halfHeight = cam.dist / cam.proj.m[5];
    cam.mvp = mul(cam.proj, cam.view);
    return cam;
}

Renderer::CameraXform sparsePreviewCamera(int width, int height) {
    const auto& s = S();
    Renderer::CameraXform cam;
    cam.vpWidth = cam.ssWidth = std::max(width, 1);
    cam.vpHeight = cam.ssHeight = std::max(height, 1);
    const float aspect = float(cam.vpWidth) / float(cam.vpHeight);
    const float halfFov = Renderer::kFovDeg * 0.00872664626f;
    const float limitingFov = std::atan(std::tan(halfFov) * std::min(aspect, 1.0f));
    cam.center = s.sparseCenter;
    cam.radius = std::max(s.sparseRadius, 1e-6f);
    cam.dist = cam.radius * 1.2f / std::sin(limitingFov);
    cam.dir = normalize(Vec3{22.0f, 18.0f, 30.5f});
    cam.eye = cam.center + cam.dir * cam.dist;
    cam.right = normalize(cross(Vec3{0.0f, 0.0f, 1.0f}, cam.dir));
    cam.up = cross(cam.dir, cam.right);
    cam.view = lookAt(cam.eye, cam.center, cam.up);
    cam.proj = perspective(2.0f * halfFov, aspect, cam.radius * 0.001f,
                           cam.dist + cam.radius * 3.0f);
    cam.mvp = mul(cam.proj, cam.view);
    cam.halfHeight = cam.dist * std::tan(halfFov);
    cam.orthographic = false;
    return cam;
}

void startGaussian(AppState& st, const std::string& sid, bool live,
                   const std::string& videoPath) {
    if (!selectedSession(sid)) return;
#ifdef __EMSCRIPTEN__
    S().gaussian.error = "Splat training requires a GPU, which the browser server sandbox does not expose.";
    return;
#endif
    auto& g = S().gaussian;
    if (g.pending || gaussianRunning(g) || S().recon.active || S().recon.kickPending ||
        (g.control && (g.control->pending || g.control->pendingIngest != 0 ||
                       (g.control->requested != GaussianRunControl::Action::None &&
                        !g.control->finishSent && !g.control->cancelSent)))) {
        g.error = "A reconstruction is already running. Finish or cancel it before starting another.";
        return;
    }
    if (!st.workerStarted) {
        g.error = "Local worker unavailable. Start or reconnect the worker before training.";
        return;
    }
    static const char* qualities[] = {"draft", "normal", "high"};
    nlohmann::json args = {{"mode", g.mode == 1 ? "object" : "environment"},
                          {"quality", qualities[std::clamp(g.quality, 0, 2)]},
                          {"iterations", std::clamp(g.iterations, 1, 1000000)},
                          {"live", live}, {"video_path", videoPath}, {"crop_bounds", nullptr}};
    // Every new SfM solve chooses a new coordinate gauge; an old crop is not transferable.
    g.cropEnabled = false;
    g.hasBounds = false;
    std::fill(g.cropBounds, g.cropBounds + 6, 0.0f);
    SH().cropDirty = false;
    auto run = std::make_shared<GaussianRunControl>();
    run->sid = sid;
    run->starting = true;
    SH().gaussianRuns[sid] = run;
    g.control = run;
    g.live = live;
    gaussianAction(st, sid, "reveng_start_splat", std::move(args), run);
}

void finishGaussian(AppState& st, const std::string& sid) {
    auto it = SH().gaussianRuns.find(sid);
    if (it == SH().gaussianRuns.end()) {
        if (!selectedSession(sid) || !gaussianRunning(S().gaussian)) return;
        auto run = std::make_shared<GaussianRunControl>();
        run->sid = sid;
        it = SH().gaussianRuns.emplace(sid, std::move(run)).first;
        S().gaussian.control = it->second;
    }
    finishGaussianRun(st, it->second);
}

void cancelGaussian(AppState& st, const std::string& sid) {
    auto it = SH().gaussianRuns.find(sid);
    if (it == SH().gaussianRuns.end()) {
        if (!selectedSession(sid) || !gaussianRunning(S().gaussian)) return;
        auto run = std::make_shared<GaussianRunControl>();
        run->sid = sid;
        it = SH().gaussianRuns.emplace(sid, std::move(run)).first;
        S().gaussian.control = it->second;
    }
    if (it->second->startFailed) return;
    it->second->requested = GaussianRunControl::Action::Cancel;
    drainGaussianActions(st, it->second);
}

void drawGaussianPanel(AppState& st) {
    auto& s = S();
    auto& g = s.gaussian;
    auto& sh = SH();
    if (s.selected < 0 || s.selected >= (int)s.sessions.size()) return;
    const auto sid = s.sessions[s.selected].session_id;
    const bool running = gaussianRunning(g);
    const bool busy = running || g.pending;
    const kit::Level level = !g.error.empty() || g.status == "failed" ? kit::Level::Error
                           : busy ? kit::Level::Busy
                           : g.status == "done" ? kit::Level::Ok
                           : g.status == "interrupted" || g.status == "cancelled" ? kit::Level::Warn
                           : kit::Level::Neutral;
    // A finished splat is something to look at, not to configure: shut the training card (once per
    // session / state change) so the preview below it is the first thing on screen.
    static bool trainOpen = true;
    static std::string trainSettled;
    const bool finished = !busy && g.status == "done";
    const std::string trainKey = sid + (finished ? "|done" : "|open");
    if (trainKey != trainSettled) { trainSettled = trainKey; trainOpen = !finished; }
    char trainTitle[96];
    std::snprintf(trainTitle, sizeof(trainTitle), "%s",
                  finished ? "3D Gaussian splat - trained (retrain settings)" : "3D Gaussian splatting");
    if (kit::CardBegin("##gaussian_job", trainTitle, "display_shaded", true, &trainOpen)) {
        kit::StatusChip(g.pending ? "Sending request" : g.status.empty() ? "Ready" : g.status.c_str(), level);
        kit::InlineStatus("Gaussian splats are visual references, not measured CAD surfaces.",
                          kit::Level::Neutral);
        static const char* modes[] = {"Environment", "Stationary object"};
        static const char* qualities[] = {"Draft", "Normal", "High"};
        ImGui::BeginDisabled(busy);
        kit::FieldLabel("Workflow");
        kit::SegmentedControl("##gaussian_mode", modes, 2, &g.mode, M::reFieldW());
        kit::FieldLabel("Quality");
        if (kit::SegmentedControl("##gaussian_quality", qualities, 3, &g.quality, M::reFieldW())) {
            static const int iterations[] = {500, 2000, 10000};
            g.iterations = iterations[std::clamp(g.quality, 0, 2)];
        }
        kit::FieldLabel("Final iterations", "Live updates use short snapshot passes; Finalize trains all accepted frames with this budget.");
        ImGui::InputInt("##gaussian_iterations", &g.iterations, 100, 1000);
        g.iterations = std::clamp(g.iterations, 1, 1000000);
        ImGui::EndDisabled();
        kit::KeyValueRow("Training / viewer budget", "2,000,000 splats maximum");
        if (g.mode == 1)
            kit::InlineStatus("Keep the object stationary and move the camera around it. No automatic masks or background removal.",
                              kit::Level::Warn);
        if (g.status == "interrupted")
            kit::InlineStatus("The previous worker stopped. Start again to retrain from saved frames; optimizer state is not resumed.",
                              kit::Level::Warn);
        if (g.live || running)
            kit::InlineStatus("Live preview uses bounded snapshots (6 frames minimum, 3 new frames per update). Finalize uses all accepted frames.",
                              kit::Level::Neutral);
        if (!g.stage.empty()) kit::KeyValueRow("Stage", g.stage.c_str(), level);
        char counts[160];
        std::snprintf(counts, sizeof(counts), "%d frames / %d registered / iteration %d of %d",
                      g.frames, g.registered, g.iteration, g.totalIterations);
        kit::ProgressBar(g.pct / 100.0f, counts, level);
        kit::InlineStatus(g.msg.c_str(), level);
        kit::InlineStatus(g.error.c_str(), kit::Level::Error);
        if (!st.workerStarted)
            kit::InlineStatus("Start or reconnect the local worker to train.", kit::Level::Warn);
        for (const auto& dependency : s.caps.artifacts) {
            if ((dependency.key == "brush" || dependency.key == "pycolmap") && !dependency.present)
                kit::CapabilityRow(dependency.key.c_str(), false, dependency.hint.c_str());
        }
        if (kit::SecondaryButton("Re-check local dependencies", !s.caps.pending))
            loadCapabilities(st, true);
        kit::GapControl();
#ifdef __EMSCRIPTEN__
        kit::InlineStatus("Splat training requires a GPU, which the browser server sandbox does not expose.",
                          kit::Level::Warn);
        kit::PrimaryButton("Start Gaussian splat", false,
                           "The browser server sandbox does not expose a GPU.");
#else
        if (kit::PrimaryButton(finished ? "Retrain Gaussian splat" : "Start Gaussian splat", !busy && st.workerStarted &&
                               !s.recon.active && !s.recon.kickPending,
                               "Select an idle session with accepted photos or use live camera capture."))
            startGaussian(st, sid, false);
#endif
        if (g.control && g.control->requested != GaussianRunControl::Action::None) {
            const bool cancelling = g.control->requested == GaussianRunControl::Action::Cancel;
            const bool sent = cancelling ? g.control->cancelSent : g.control->finishSent;
            if (!sent)
                kit::InlineStatus(cancelling ? "Cancel queued until startup completes."
                                             : "Finalize queued until startup and image ingest complete.",
                                  kit::Level::Busy);
            else if (!g.control->pending && !g.control->acknowledged)
                kit::InlineStatus("The terminal request was not acknowledged. Check the worker and job status before starting again.",
                                  kit::Level::Error);
        }
        if (kit::SecondaryButton("Finalize all frames", (running || g.pending) && g.live &&
                                 g.status != "finalizing", "Available during a live Gaussian job, including startup.")) {
            if (!webcamCaptureFinishGaussian(st, sid)) finishGaussian(st, sid);
        }
        if (kit::DangerButton("Cancel Gaussian job", running || g.pending))
            cancelGaussian(st, sid);
        if (!g.splatPath.empty()) kit::KeyValueRow("Saved PLY", g.splatPath.c_str());
    }
    kit::CardEnd();
    kit::GapCards();
    if (kit::CardBegin("##gaussian_preview_card", "Gaussian preview", "display_shaded")) {
        if (s.splatUntransformed)
            kit::InlineStatus("This splat is still in reconstruction coordinates; its placement "
                              "and scale do not match the calibrated mesh and cameras.", kit::Level::Warn);
        if (!s.splatImportLoss.empty())
            kit::InlineStatus(s.splatImportLoss.c_str(), kit::Level::Warn);
        const bool have = s.splatLoaded && s.splat.hasSplats();
        const ImVec2 box(ImGui::GetContentRegionAvail().x, M::reThumbLg());
        const ImRect rect = kit::Viewport3DPreview("##gaussian_preview", box,
                            have ? kit::PreviewSlot::Splat : kit::PreviewSlot::None,
                            have ? nullptr : "The first complete training export will appear here.");
        if (have) {
            // Place input on the same surface the native GL pass renders, respecting child clips.
            const ImVec2 after = ImGui::GetCursorScreenPos();
            ImGui::SetCursorScreenPos(rect.Min);
            ImGui::InvisibleButton("##gaussian_camera", rect.GetSize(),
                                   ImGuiButtonFlags_MouseButtonLeft | ImGuiButtonFlags_MouseButtonRight |
                                   ImGuiButtonFlags_MouseButtonMiddle);
            const auto& io = ImGui::GetIO();
            if (ImGui::IsItemHovered()) {
                ImGui::SetKeyOwner(ImGuiKey_MouseWheelY, ImGui::GetID("##gaussian_camera"));
                sh.gaussianZoom = std::clamp(sh.gaussianZoom * std::exp(io.MouseWheel * 0.15f), 0.02f, 100.0f);
                if (ImGui::IsMouseDoubleClicked(ImGuiMouseButton_Left)) fitGaussianCamera();
            }
            if (ImGui::IsItemActive()) {
                if (ImGui::IsMouseDown(ImGuiMouseButton_Left) && !io.KeyShift) {
                    sh.gaussianYaw -= io.MouseDelta.x / std::max(rect.GetWidth(), 1.0f) * 6.2831853f;
                    sh.gaussianPitch = std::clamp(sh.gaussianPitch + io.MouseDelta.y /
                                                  std::max(rect.GetHeight(), 1.0f) * 3.1415927f, -1.55f, 1.55f);
                } else {
                    const auto cam = gaussianPreviewCamera((int)rect.GetWidth(), (int)rect.GetHeight());
                    const float perPixelX = 2.0f * cam.dist / (cam.proj.m[0] * std::max(rect.GetWidth(), 1.0f));
                    const float perPixelY = 2.0f * cam.halfHeight / std::max(rect.GetHeight(), 1.0f);
                    sh.gaussianCenter = sh.gaussianCenter +
                        cam.right * (-io.MouseDelta.x * perPixelX) + cam.up * (io.MouseDelta.y * perPixelY);
                }
            }
            ImGui::SetCursorScreenPos(after);
            if (kit::SecondaryButton("Frame splat bounds")) fitGaussianCamera();
            if (kit::SecondaryButton("Capture view", !sh.loadedViewCamera.id.empty(),
                                     "This artifact has no calibrated capture camera."))
                restoreGaussianCaptureCamera();
            kit::InlineStatus("Drag: orbit | Right / middle / Shift-drag: pan | Wheel: zoom | Double-click: frame",
                              kit::Level::Neutral);
            if (std::getenv("CHUDWARE_SHOT") && s.splatPathTried.find("splat_tiny.ply") != std::string::npos)
                kit::InlineStatus("Screenshot fixture — not reconstructed session data.", kit::Level::Warn);
        }
#ifdef __EMSCRIPTEN__
        if (sh.splatFetchPending)
            kit::ProgressBar(sh.splatFetchProgress, "Downloading Gaussian artifact", kit::Level::Busy);
#endif
        if (sh.splatLoad.valid())
            kit::InlineStatus("Loading complete Gaussian export in the background…", kit::Level::Busy);
        kit::InlineStatus(sh.splatLoadError.c_str(), kit::Level::Error);
        if (!sh.splatLoadError.empty()) {
            kit::InlineStatus(have ? "Showing the last valid preview, not the failed refresh." :
                                    "No valid preview is loaded.", kit::Level::Warn);
            if (kit::SecondaryButton("Retry loading artifact")) s.splatPathTried.clear();
        }
        if (g.mode == 1) {
            kit::GapSection();
            kit::SectionHeader("Object crop — reconstruction units");
            kit::InlineStatus(g.cropEnabled ? "Explicit 3D center crop applied to saved Gaussian PLY." :
                                              "Uncropped object preview includes the surrounding scene.",
                              g.cropEnabled ? kit::Level::Neutral : kit::Level::Warn);
            ImGui::BeginDisabled(busy);
            if (!sh.cropDirty && !g.cropEnabled && g.hasBounds)
                std::copy(g.bounds, g.bounds + 6, g.cropBounds);
            kit::FieldLabel("Minimum X / Y / Z");
            sh.cropDirty |= ImGui::InputFloat3("##gaussian_crop_min", g.cropBounds, "%.6g");
            kit::FieldLabel("Maximum X / Y / Z");
            sh.cropDirty |= ImGui::InputFloat3("##gaussian_crop_max", g.cropBounds + 3, "%.6g");
            bool validCrop = true;
            for (int i = 0; i < 3; ++i)
                validCrop &= std::isfinite(g.cropBounds[i]) && std::isfinite(g.cropBounds[i + 3]) &&
                             g.cropBounds[i] < g.cropBounds[i + 3];
            if (kit::PrimaryButton("Apply object crop", have && validCrop,
                                   "Load a splat and specify finite minimum values below maximum values."))
                gaussianAction(st, sid, "reveng_crop_splat", {{"bounds", g.cropBounds}});
            if (kit::SecondaryButton("Reset crop from original", have && g.cropEnabled))
                gaussianAction(st, sid, "reveng_crop_splat", {{"bounds", nullptr}});
            ImGui::EndDisabled();
            kit::InlineStatus("Crop keeps Gaussian centers inside this box and preserves the original source. A new reconstruction clears the crop because its coordinates change. No semantic segmentation.",
                              kit::Level::Neutral);
        }
    }
    kit::CardEnd();
    kit::GapCards();
    if (kit::CardBegin("##splat_convert", "Splat conversion and approximate mesh", "mesh")) {
        const bool haveArtifact = !s.artifactSplatPath.empty() || !g.splatPath.empty();
        const bool canRun = st.workerStarted && haveArtifact && !busy &&
                            !sh.splatActionPending && !s.recon.active && !s.recon.kickPending;
        kit::InlineStatus("Pruning keeps the subject (haze, background, floor and floaters are "
                          "removed), then extracts a point cloud and an approximate mesh. Hidden or "
                          "poorly captured surfaces are not recovered; verify dimensions independently.",
                          kit::Level::Warn);
        ImGui::Checkbox("Automatic opacity cut (recommended)", &sh.splatAutoOpacity);
        ImGui::Checkbox("Remove floor / support surface", &sh.splatRemoveFloor);
        kit::FieldLabel("Minimum opacity");
        ImGui::BeginDisabled(sh.splatAutoOpacity);
        ImGui::SliderFloat("##splat_min_opacity", &sh.splatMinOpacity, 0.0f, 1.0f, "%.2f");
        ImGui::EndDisabled();
        if (kit::PrimaryButton("Prune and extract point cloud + mesh", canRun,
                               "Load a splat and wait for active jobs to finish.")) {
            sh.splatActionPending = true;
            sh.splatActionError = false;
            sh.splatActionMsg = "Extracting an approximate mesh...";
            const auto epoch = sh.sessionEpoch;
            nlohmann::json meshParams = {{"path_or_session", sid}, {"method", "auto"},
                                         {"remove_floor", sh.splatRemoveFloor}};
            if (!sh.splatAutoOpacity) meshParams["min_opacity"] = sh.splatMinOpacity;
            st.rpc.call("reveng_splat_to_mesh", meshParams,
                [sid, epoch](const core::RpcResult& r) {
                    if (!selectedSession(sid) || SH().sessionEpoch != epoch) return;
                    auto& sh2 = SH();
                    if (!r.ok || !r.result.is_object() || !r.result.value("ok", false)) {
                        sh2.splatActionPending = false;
                        sh2.splatActionError = true;
                        sh2.splatActionMsg = r.ok && r.result.is_object()
                            ? r.result.value("reason", std::string("Mesh extraction failed."))
                            : (r.err.empty() ? std::string("Mesh extraction failed.") : r.err);
                        return;
                    }
                    // The worker meshes in the background (wait=false is pinned for the GUI);
                    // session.mesh_job reports the outcome through the session detail poll.
                    sh2.splatActionMsg = "Extracting an approximate mesh in the background...";
                    S().lastDetailT = -100.0;
                }, core::kLongEngineCallTimeoutMs);
        }
        static const char* formats[] = {"Gaussian PLY", ".splat"};
        kit::FieldLabel("Convert to");
        kit::SegmentedControl("##splat_export_format", formats, 2, &sh.splatExportFormat, M::reFieldW());
        if (sh.splatExportFormat == 1)
            kit::InlineStatus(".splat quantizes color, opacity and rotation and drops higher-order spherical harmonics.",
                              kit::Level::Warn);
        if (kit::SecondaryButton("Convert and save...", canRun,
                                 "Load a splat and wait for active jobs to finish.")) {
            const bool ply = sh.splatExportFormat == 0;
            const std::string dest = ui::saveFileDialog(
                ply ? "Gaussian PLY (*.ply)\0*.ply\0" : "Gaussian splat (*.splat)\0*.splat\0",
                ply ? "ply" : "splat", ply ? "splat.ply" : "splat.splat");
            if (!dest.empty()) {
                sh.splatActionPending = true;
                sh.splatActionError = false;
                sh.splatActionMsg = "Converting splat...";
                const auto epoch = sh.sessionEpoch;
                nlohmann::json params = {{"out_format", ply ? "ply" : "splat"}, {"out_path", dest}};
#ifdef __EMSCRIPTEN__
                params["session_id"] = sid;
#else
                params["path"] = sid;
#endif
                st.rpc.call("reveng_convert_splat", std::move(params),
                    [sid, epoch, dest](const core::RpcResult& r) {
                        if (!selectedSession(sid) || SH().sessionEpoch != epoch) return;
                        auto& sh2 = SH();
                        sh2.splatActionPending = false;
                        if (!r.ok || !r.result.is_object() || !r.result.value("ok", false)) {
                            sh2.splatActionError = true;
                            sh2.splatActionMsg = r.ok && r.result.is_object()
                                ? r.result.value("reason", std::string("Conversion failed."))
                                : (r.err.empty() ? std::string("Conversion failed.") : r.err);
                            return;
                        }
                        sh2.splatActionMsg = "Saved " + dest;
                        if (r.result.contains("lossy") && r.result["lossy"].is_array()) {
                            for (const auto& loss : r.result["lossy"])
                                if (loss.is_string()) sh2.splatActionMsg += " \xe2\x80\x94 " + loss.get<std::string>();
                        }
                        ui::afterFileWritten(dest);
                    }, core::kLongEngineCallTimeoutMs);
            }
        }
        kit::InlineStatus(sh.splatActionMsg.c_str(),
                          sh.splatActionPending ? kit::Level::Busy :
                          sh.splatActionError ? kit::Level::Error : kit::Level::Neutral);
    }
    kit::CardEnd();
}

// ---- reconstruction kick -------------------------------------------------------

// Kick an async reveng_reconstruct for the selected session (ribbon + Run button share this).
void kickReconstruct(AppState& st) {
    RevEngState& s = S();
    if (s.selected < 0 || s.selected >= (int)s.sessions.size()) {
        st.toast(ui::ToastLevel::Info, "Reverse Eng: select a session first");
        return;
    }
    if (s.recon.kickPending) return;
    if (gaussianRunning(s.gaussian) || s.gaussian.pending) {
        s.recon.kickErr = "Finish or cancel the Gaussian job before starting a CAD reconstruction.";
        return;
    }
    static const char* kQuality[] = { "draft", "normal", "high" };
    s.recon.kickPending = true;
    s.recon.kickErr.clear();
    SH().reconStartT = ImGui::GetTime();
    nlohmann::json args = nlohmann::json::object();
    const auto sid = s.sessions[s.selected].session_id;
    const auto epoch = SH().sessionEpoch;
    args["session_id"] = sid;
    args["quality"]    = kQuality[std::clamp(s.recon.quality, 0, 2)];
    st.rpc.call("reveng_reconstruct", args, [sid, epoch](const core::RpcResult& r) {
        RevEngState& s2 = S();
        if (!selectedSession(sid) || SH().sessionEpoch != epoch) return;
        s2.recon.kickPending = false;
        if (!r.ok) {
            s2.recon.kickErr = r.err.empty() ? "reveng_reconstruct failed" : r.err;
            SH().reconStartT = -1.0;
            return;
        }
        if (r.result.is_object() && !r.result.value("ok", true)) {
            s2.recon.kickErr = r.result.value("reason", std::string("reveng_reconstruct failed"));
            const std::string hint = r.result.value("hint", std::string());
            if (!hint.empty()) s2.recon.kickErr += " \xe2\x80\x94 " + hint;
            SH().reconStartT = -1.0;
            return;
        }
        s2.recon.kickErr.clear();
        s2.recon.runId  = r.result.value("run_id", std::string());
        s2.recon.active = true;
        s2.recon.done   = false;
        s2.recon.pct    = 2;
        s2.recon.phase  = "queued";
        s2.recon.err.clear();
        // Arm the live progress feed on the run_id the launch just returned:
        // core::engineEventsTick tails that run's EventJournal, so the bar / sub-steps / clock
        // advance during the mission instead of waiting on the 3 s stage-badge poll. This runs
        // inside st.rpc.drain(), earlier in the frame than engineEventsTick, so the first
        // journal record replaces the "queued" seed above before anything draws.
        core::watchRevEngReconstruct(s2.recon.runId);
    });
}

// ---- ribbon command routing ----------------------------------------------------
// app.cpp dispatchCmd forwards every reveng_* id here. Each command switches to the workspace,
// lands on the right stage, and — for the three Source entry points — sets a one-shot
// SourceFocus so sources_panel.cpp opens the SPECIFIC widget the button promised. Before RE-40
// all three collapsed into one branch, so "Add Photos", "Add Video" and "Webcam" were the same
// button wearing three labels.
bool handleRevEngCommand(AppState& st, const char* cmd) {
    if (!cmd) return false;
    const std::string c(cmd);
    if (c.rfind("reveng_", 0) != 0) return false;

    RevEngState& s = S();
    auto toStage = [&](Stage stg) {
        st.activeWorkspace = AppState::Workspace::ReverseEng;
        s.stage = stg;
    };
    auto toSource = [&](SourceFocus focus) {
        toStage(Stage::Source);
        s.sourceMode  = SourceMode::Scan;
        s.sourceFocus = focus;
        revealColumn(/*rail=*/true);
    };

    if (c == "reveng_new")          { toStage(Stage::Source); s.focusNewName = true;
                                      revealColumn(/*rail=*/true); return true; }
    if (c == "reveng_open")         { st.activeWorkspace = AppState::Workspace::ReverseEng;
                                      revealColumn(/*rail=*/true);
                                      loadSessions(st, /*force=*/true); return true; }
    if (c == "reveng_from_library") { st.activeWorkspace = AppState::Workspace::PartsLibrary;
                                      return true; }
    if (c == "reveng_add_photos")   { toSource(SourceFocus::Photos); return true; }
    if (c == "reveng_add_video")    { toSource(SourceFocus::Video);  return true; }
    if (c == "reveng_webcam")       { toSource(SourceFocus::Webcam); return true; }
    if (c == "reveng_datasheet")    { toStage(Stage::Source); s.sourceMode = SourceMode::Reference;
                                      s.sourceFocus = SourceFocus::None; return true; }
    if (c == "reveng_scale_aruco")  { toStage(Stage::Scale); s.scale.method = 0; return true; }
    if (c == "reveng_scale_points") { toStage(Stage::Scale); s.scale.method = 1; return true; }
    if (c == "reveng_scale_known")  { toStage(Stage::Scale); s.scale.method = 2; return true; }
    if (c == "reveng_reconstruct")  { toStage(Stage::Reconstruct); kickReconstruct(st); return true; }
    if (c == "reveng_splat_view")   { toStage(Stage::Reconstruct); return true; }
    if (c == "reveng_fit")          { toStage(Stage::Model); kickFitPrimitives(st); return true; }
    if (c == "reveng_ai_model")     { toStage(Stage::Model); kickBuildParametric(st, 0); return true; }
    if (c == "reveng_mesh_body")    { toStage(Stage::Model); s.model.strategy = 1;
                                      kickBuildParametric(st, 1); return true; }
    if (c == "reveng_export")       { toStage(Stage::Deliver); return true; }
    if (c == "reveng_to_parts")     { toStage(Stage::Deliver); return true; }
    if (c == "reveng_print_mat")    { toStage(Stage::Deliver); kickPrintScaleMat(st); return true; }
    return false;
}

namespace {

// ================================================================================
// Reconstruct stage
// ================================================================================

// Result of the pre-flight capability check. Nothing here guesses: an unknown capability state
// never blocks, and a known-missing backend always names the env var that fixes it.
struct ReconGate {
    bool        canRun    = false;
    bool        hardBlock = false;   // no path to a reconstruction at all -> BlockedState
    std::string reason;              // disabledReason for the Run button / BlockedState body
    std::string hint;                // "what to do" line for the BlockedState
    std::string warn;                // non-blocking degradation notice
};

ReconGate reconPreflight(const AppState& st, const RevEngState& s) {
    ReconGate g;
    if (!st.workerStarted) {
        g.hardBlock = true;
        g.reason = "The Chudware worker is not running, so no reconstruction backend can be reached.";
        g.hint   = "Start the worker (Help > Diagnostics) and reopen this tab.";
        return g;
    }
    if (s.caps.loaded && !s.caps.sparse) {
        g.hardBlock = true;
        g.reason = "No sparse-reconstruction backend is provisioned. Structure-from-motion needs "
                   "either COLMAP or the VGGT feed-forward model, and neither was found.";
        g.hint   = "Install COLMAP and set COLMAP_BIN (or provision VGGT weights), then press "
                   "Re-check below.";
        return g;
    }
    if (s.selected < 0 || s.selected >= (int)s.sessions.size()) {
        g.reason = "Select a session in the rail first.";
        return g;
    }
    if (s.sessions[s.selected].images <= 0) {
        g.reason = "This session has no images yet. Capture or import photos in the Source stage.";
        return g;
    }
    if (s.recon.kickPending) { g.reason = "Starting the run\xe2\x80\xa6"; return g; }
    if (s.recon.active)      { g.reason = "A reconstruction is already running for this session."; return g; }

    g.canRun = true;
    if (s.caps.loaded && !s.caps.dense) {
        g.warn = "OpenMVS is not installed, so this run produces a SPARSE point cloud only \xe2\x80\x94 "
                 "no dense mesh. Use \"Install missing tools\" in the Session panel for "
                 "a watertight mesh.";
    }
    if (!s.caps.loaded) {
        g.warn = "Checking which reconstruction tools are installed\xe2\x80\xa6";
    } else if (s.scale.mirrored && !s.scale.confirmed) {
        g.warn = "Scale is not confirmed yet. That is fine here: ArUco scaling needs a sparse model, "
                 "so reconstructing first is the normal order \xe2\x80\x94 just confirm scale before "
                 "you trust any dimension.";
    }
    return g;
}

// Sparse -> Dense -> Mesh sub-steps, driven by the RevEngEvent drain (phase name first, pct as
// the fallback when the backend only reports a percentage).
void drawReconSubSteps(const RevEngState& s) {
    static const char* kSubLabels[] = { "Sparse", "Dense", "Mesh" };
    const int n = 3;
    ShellState& sh = SH();

    int active = -1;
    if (s.recon.active) {
        const std::string& p = s.recon.phase;
        if (p.find("mesh") != std::string::npos)        active = 2;
        else if (p.find("dense") != std::string::npos)  active = 1;
        else if (p.find("sparse") != std::string::npos) active = 0;
        // Stages AFTER reconstruction (the mission continues into fit/parametric/review/
        // deliver) are all "past" this three-step track. Without this they fell through to the
        // pct ladder below, and `fit` at pct 60-65 landed in the Dense bucket — a visible
        // Mesh -> Dense -> Mesh rewind.
        else if (p == "fit" || p == "parametric" || p == "review" || p == "deliver")
                                                        active = 2;
        else if (s.recon.pct >= 67)                     active = 2;
        else if (s.recon.pct >= 34)                     active = 1;
        else                                            active = 0;
        // The track never walks backwards inside one run: a stage map that reports a lower pct
        // for a later node must not un-complete a step the user already watched finish.
        if (sh.lastSubStep > active) active = sh.lastSubStep;
        sh.lastSubStep = active;
    }

    kit::Level states[3];
    for (int i = 0; i < n; ++i) {
        // A failure belongs on the step that was RUNNING when it happened, not on step 0.
        if (!s.recon.err.empty() && i == sh.lastSubStep) states[i] = kit::Level::Error;
        else if (!s.recon.err.empty() && i < sh.lastSubStep) states[i] = kit::Level::Ok;
        else if (!s.recon.err.empty())                  states[i] = kit::Level::Neutral;
        else if (s.recon.done)                          states[i] = kit::Level::Ok;
        else if (active > i)                            states[i] = kit::Level::Ok;
        else if (active == i)                           states[i] = kit::Level::Busy;
        else                                            states[i] = kit::Level::Neutral;
    }
    kit::StepTrack("##recon_substeps", kSubLabels, states, n, active,
                   /*clickable=*/false, M::reStepperH());
}

// The artifact this session currently has, and the slot that renders it.
kit::PreviewSlot reconSlot(const RevEngState& s) {
    if (!s.artifactMeshPath.empty())          return kit::PreviewSlot::Mesh;
    return kit::PreviewSlot::SparseCloud;
}

// Inline 3D preview of whatever the reconstruction produced. app.cpp's preview-slot painter fills
// this region from inside ImGui's draw pass (kit::Viewport3DPreview queues it).
void drawReconPreview(RevEngState& s) {
    const kit::PreviewSlot slot = reconSlot(s);
    // The canned sparse fixture deliberately does NOT count as an artifact: it is demo scaffolding,
    // and treating it as content is exactly how "it worked, trust me" sneaks back in.
    const bool haveArtifact = slot == kit::PreviewSlot::Mesh ? bool(s.meshPreview)
        : (!s.artifactSparseDir.empty() && (s.rePoints.hasPoints() || s.rePoints.hasFrusta()));
    const bool artifactPending = slot == kit::PreviewSlot::Mesh ? !s.artifactMeshPath.empty()
        : !s.artifactSparseDir.empty();
    const char* emptyLabel = haveArtifact ? nullptr : artifactPending
        ? "Loading preview, or see the error below."
        : "No reconstruction yet — run one to see the point cloud and camera poses here";
    const ImVec2 box(ImGui::GetContentRegionAvail().x, M::reThumbLg());
    kit::Viewport3DPreview("##recon_preview", box, slot, emptyLabel);

    // Always say WHAT is on screen and whether it is this session's data.
    if (slot == kit::PreviewSlot::Mesh) {
        kit::ValueChip("mesh", haveArtifact ? "ready" : s.meshPreviewError.empty() ? "loading" : "error",
                       haveArtifact ? kit::Level::Ok :
                       s.meshPreviewError.empty() ? kit::Level::Busy : kit::Level::Error,
                       s.artifactMeshPath.c_str());
    } else if (!s.artifactSparseDir.empty()) {
        kit::ValueChip("sparse model", haveArtifact ? "ready" :
                       s.sparsePreviewError.empty() ? "loading" : "error",
                       haveArtifact ? kit::Level::Ok :
                       s.sparsePreviewError.empty() ? kit::Level::Busy : kit::Level::Error,
                       s.artifactSparseDir.c_str());
    } else if (s.recon.done) {
        kit::InlineStatus("Reconstruction reported done but returned no artifact path \xe2\x80\x94 "
                          "nothing to preview.", kit::Level::Warn);
    }
#ifdef __EMSCRIPTEN__
    if (SH().meshFetchPending)
        kit::ProgressBar(SH().meshFetchProgress, "Downloading mesh preview", kit::Level::Busy);
#endif
    kit::InlineStatus(SH().sparsePreviewWarnings.c_str(), kit::Level::Warn);
    kit::InlineStatus(s.sparsePreviewError.c_str(), kit::Level::Error);
    kit::InlineStatus(s.meshPreviewError.c_str(), kit::Level::Error);
    if ((!s.sparsePreviewError.empty() || !s.meshPreviewError.empty()) &&
        !SH().previewPending && kit::SecondaryButton("Retry preview"))
        SH().previewTriedKey.clear();
}

void drawReconProgress(const RevEngState& s) {
    ShellState& sh = SH();
    const double now = ImGui::GetTime();
    // kickReconstruct() starts the clock at the click, so queue latency is counted too. Only
    // stop it once the run is genuinely over (not while the kick RPC is still in flight).
    if (s.recon.active && sh.reconStartT < 0.0) sh.reconStartT = now;
    if (!s.recon.active && !s.recon.kickPending && sh.reconStartT >= 0.0) {
        sh.reconElapsed = now - sh.reconStartT;
        sh.reconStartT = -1.0;
    }
    const double elapsed = sh.reconStartT >= 0.0 ? (now - sh.reconStartT) : sh.reconElapsed;

    if (!s.recon.active && !s.recon.done && s.recon.err.empty()) return;

    char cap[96];
    if (elapsed > 0.0)
        std::snprintf(cap, sizeof(cap), "%d%%  \xc2\xb7  %s", s.recon.pct,
                      kit::fmtDuration(elapsed).c_str());
    else
        std::snprintf(cap, sizeof(cap), "%d%%", s.recon.pct);

    const kit::Level lvl = !s.recon.err.empty() ? kit::Level::Error
                         : s.recon.done         ? kit::Level::Ok
                                                : kit::Level::Busy;
    kit::ProgressBar((float)s.recon.pct / 100.0f, cap, lvl);
    if (!s.recon.phase.empty() && s.recon.active)
        kit::InlineStatus(s.recon.phase.c_str(), kit::Level::Busy);
    kit::InlineStatus(s.recon.err.c_str(), kit::Level::Error);
}

void drawReconRunControls(AppState& st, RevEngState& s, const ReconGate& gate) {
    static const char* kQualityLabels[] = { "Draft", "Normal", "High" };

    kit::FieldLabel("Quality");
    kit::SegmentedControl("##recon_quality", kQualityLabels, 3, &s.recon.quality, M::reFieldW());
    ImGui::SameLine(0.0f, M::reCtlGap());
    kit::HelpMarker("Draft downsamples the images and skips refinement \xe2\x80\x94 minutes, good enough to "
                    "check coverage. Normal is the default. High keeps full resolution and runs "
                    "extra bundle-adjustment passes: several times slower, and the only setting "
                    "worth using for a part you intend to measure.");
    kit::GapControl();

    const char* runLabel = s.recon.kickPending ? "Starting\xe2\x80\xa6"
                         : s.recon.active      ? "Running\xe2\x80\xa6"
                                               : "Run full CAD reconstruction";
    const bool gaussianBusy = gaussianRunning(s.gaussian) || s.gaussian.pending;
    if (kit::PrimaryButton(runLabel, gate.canRun && !gaussianBusy,
                           gaussianBusy ? "Finish or cancel the Gaussian job first." : gate.reason.c_str(),
                           ImVec2(ImGui::GetContentRegionAvail().x, M::reBtnH()))) {
        kickReconstruct(st);
    }
    if (!gate.canRun && !gate.reason.empty())
        kit::InlineStatus(gate.reason.c_str(), kit::Level::Warn);
    kit::InlineStatus(s.recon.kickErr.c_str(), kit::Level::Error);

    if (!s.recon.runId.empty()) {
        kit::GapControl();
        kit::KeyValueRow("Run id", s.recon.runId.c_str());
    }
}

// RE-21/RE-40: Reconstruct stage — pre-flight gate, sub-steps, inline 3D preview, run controls.
// Decomposed from the old 183-line monolith into the focused helpers above.
void drawReconstructStage(AppState& st) {
    RevEngState& s = S();
    drawGaussianPanel(st);
    kit::GapCards();

    // Fixtures are strictly screenshot-only; an ordinary empty session stays empty.
    if (!s.cannedFixtureLoaded && s.artifactSparseDir.empty() && std::getenv("CHUDWARE_SHOT")) {
        s.cannedFixtureLoaded = true;
        std::vector<render::SparsePoint>   pts;
        std::vector<render::CameraFrustum> frusta;
        render::buildCannedSparseFixture(pts, frusta);
        s.rePoints.setCloud(pts);
        s.rePoints.setFrusta(frusta);
        s.sparseCenter = {0.0f, 0.0f, 7.5f};
        s.sparseRadius = 20.0f;
    }

    const ReconGate gate = reconPreflight(st, s);

    if (gate.hardBlock) {
        if (!s.artifactMeshPath.empty() || !s.artifactSparseDir.empty()) {
            if (kit::CardBegin("##recon_preview_blocked", "CAD reconstruction preview", "display_shaded"))
                drawReconPreview(s);
            kit::CardEnd();
            kit::GapCards();
        }
        if (kit::BlockedState("CAD reconstruction unavailable", gate.reason.c_str(), gate.hint.c_str(),
                              "Re-check")) {
            loadCapabilities(st, /*force=*/true, /*manual=*/true);
        }
        return;
    }

    if (kit::CardBegin("##recon_run", "Separate CAD / mesh reconstruction", "mesh")) {
        const kit::Level runLvl = !s.recon.err.empty() || !s.recon.kickErr.empty()
                                      ? kit::Level::Error
                                  : s.recon.active ? kit::Level::Busy
                                  : s.recon.done   ? kit::Level::Ok
                                                   : kit::Level::Neutral;
        const char* runTxt = !s.recon.err.empty() || !s.recon.kickErr.empty() ? "error"
                           : s.recon.active ? "running"
                           : s.recon.done   ? "complete"
                                            : "idle";
        kit::CardHeaderRight(M::reBtnW());
        kit::StatusChip(runTxt, runLvl);

        drawReconSubSteps(s);
        drawReconProgress(s);
        if (!gate.warn.empty()) {
            kit::GapControl();
            kit::InlineStatus(gate.warn.c_str(), kit::Level::Warn);
        }
        kit::GapSection();
        drawReconRunControls(st, s, gate);
    }
    kit::CardEnd();

    kit::GapCards();

    if (kit::CardBegin("##recon_preview_card", "CAD reconstruction preview", "display_shaded")) {
        drawReconPreview(s);
    }
    kit::CardEnd();
}

// ================================================================================
// Session rail
// ================================================================================

void drawSessionCard(AppState& st, RevEngState& s, int i) {
    const SessionSummary& ss = s.sessions[i];
    const bool selected = (s.selected == i);

    ImGui::PushID(i);
    char cardId[64];
    std::snprintf(cardId, sizeof(cardId), "##re_sess_%d", i);
    if (kit::CardBegin(cardId)) {
        int tw = 0, th = 0;
        const unsigned int tex = railThumb(ss, &tw, &th);

        bool pick = false;
        // No quality badge here: the documented ThumbTile badges describe an IMAGE
        // (ok/blurred/duplicate/synthetic), not a session. Session honesty is the tag below.
        if (kit::ThumbTile("##thumb", tex, M::reThumbSm(), selected, nullptr, nullptr)) {
            pick = true;
        }
        if (tex && ImGui::IsItemHovered()) {
            std::vector<std::string> meta;
            meta.push_back(ss.name);
            char imgLine[64];
            const int count = ss.hasGaussian ? ss.gaussianFrames : ss.images;
            std::snprintf(imgLine, sizeof(imgLine), "%d %s%s", count,
                          ss.hasGaussian ? "frame" : "image", count == 1 ? "" : "s");
            meta.push_back(imgLine);
            if (!ss.updatedAt.empty()) meta.push_back("updated " + ss.updatedAt);
            kit::ThumbHoverPreview(tex, tw, th, meta);
        }

        ImGui::SameLine(0.0f, M::reCardPadX());
        // Everything right of the thumbnail is ellipsized or flowed against the card's content
        // edge, so a narrow rail shortens the name / wraps a chip instead of hard-clipping it.
        const float rightX = ImGui::GetCursorScreenPos().x + ImGui::GetContentRegionAvail().x;
        const float gap    = M::reCtlGap();
        auto fitChip = [&](const std::string& label) {
            return ui::kit::elide(label.c_str(),
                                  rightX - ImGui::GetCursorScreenPos().x - statusChipW(""));
        };
        ImGui::BeginGroup();
        {
            const float       nameW = ImGui::GetContentRegionAvail().x;
            const std::string name  = ui::kit::elide(ss.name.c_str(), nameW);
            if (ImGui::Selectable((name + "###re_sess_name").c_str(), selected, 0,
                                  ImVec2(nameW, 0.0f)))
                pick = true;
            if (ImGui::IsItemHovered()) {
                if (name != ss.name)
                    ImGui::SetTooltip("%s\n%s", ss.name.c_str(), ss.session_id.c_str());
                else if (!ss.session_id.empty())
                    ImGui::SetTooltip("%s", ss.session_id.c_str());
            }

            if (ss.hasGaussian) {
                const kit::Level level = ss.gaussianStatus == "cancelled" ? kit::Level::Warn
                                        : badgeLevel(ss.gaussianStatus);
                const std::string label = "Gaussian " +
                    (ss.gaussianStatus.empty() ? std::string("idle") : ss.gaussianStatus);
                kit::StatusChip(fitChip(label).c_str(), level,
                                "Gaussian reference in reconstruction units, not completed CAD.");
                if (level == kit::Level::Busy) {
                    char progressTxt[96];
                    std::snprintf(progressTxt, sizeof(progressTxt), "%s %d%%",
                                  ss.gaussianStage.empty() ? "Gaussian" : ss.gaussianStage.c_str(),
                                  std::clamp(ss.gaussianPct, 0, 100));
                    kit::ProgressBar(std::clamp(ss.gaussianPct, 0, 100) / 100.0f, progressTxt, level);
                }
            } else {
                // "capturing" with nothing captured yet is just a new session.
                const std::string label = ss.status.empty() ||
                                                  (ss.status == "capturing" && ss.images == 0)
                                              ? std::string("new") : ss.status;
                kit::StatusChip(fitChip(label).c_str(),
                                label == "new" ? kit::Level::Neutral : statusLevel(ss.status));
            }
            if (ss.approximate) {
                flowSameLine(rightX, statusChipW("approximate"), gap);
                kit::HonestyTag("approximate",
                                "Reference/preview geometry only \xe2\x80\x94 not a measured model.");
            }
            char meta[48];
            if (ss.lane == "reference")
                std::snprintf(meta, sizeof(meta), "from reference");
            else
                std::snprintf(meta, sizeof(meta), "%d image%s", ss.images, ss.images == 1 ? "" : "s");
            ImGui::TextDisabled("%s", meta);
        }
        ImGui::EndGroup();

        if (pick && s.selected != i) {
            s.selected = i;
            s.stage = Stage::Source;
            resetForSessionChange(s);
        }
    }
    kit::CardEnd();
    ImGui::PopID();
    (void)st;
}

void drawNewSessionCard(AppState& st, RevEngState& s) {
    if (kit::CardBegin("##re_new_session", "New session", "folder_new")) {
        const bool canCreate = st.workerStarted && !s.newSessionPending;
        const float fieldW = ImGui::GetContentRegionAvail().x;
        const float textW  = fieldW - ImGui::GetStyle().FramePadding.x * 2.0f;
        ImGui::SetNextItemWidth(fieldW);
        if (s.focusNewName) {
            ImGui::SetKeyboardFocusHere();
            s.focusNewName = false;
        }
        const std::string hint = ui::kit::elide("Part or object name (optional)", textW);
        ImGui::InputTextWithHint("##reveng_new_name", hint.c_str(), s.newNameBuf,
                                 sizeof(s.newNameBuf));
        kit::GapControl();
        const char* createLab = s.newSessionPending ? "Creating\xe2\x80\xa6" : "Create session";
        const bool  create =
            kit::PrimaryButton(fitLabel(createLab, "re_create_session", fieldW).c_str(), canCreate,
                               st.workerStarted ? "A session is already being created."
                                                : "The Chudware worker is not running.",
                               ImVec2(fieldW, M::reBtnH()));
        if (canCreate && ImGui::CalcTextSize(createLab).x > textW && ImGui::IsItemHovered())
            ImGui::SetTooltip("%s", createLab);
        if (create) {
            s.newSessionPending = true;
            s.newSessionErr.clear();
            nlohmann::json params = nlohmann::json::object();
            if (s.newNameBuf[0] != '\0') params["name"] = std::string(s.newNameBuf);
            if (const std::string project = revengProjectDir(st); !project.empty())
                params["project_dir"] = project;
            st.rpc.call("reveng_start_session", params, [&st](const core::RpcResult& r) {
                RevEngState& s2 = S();
                s2.newSessionPending = false;
                if (!r.ok || !r.result.is_object() || !r.result.value("ok", false)) {
                    s2.newSessionErr = r.ok && r.result.is_object()
                                           ? r.result.value("reason", std::string("create failed"))
                                           : (r.err.empty() ? std::string("worker unreachable") : r.err);
                    return;
                }
                s2.newSessionErr.clear();
                s2.newNameBuf[0] = '\0';
                s2.stage = Stage::Source;   // land on Source so the entry widgets show
                s2.lastListT = -100.0;      // expire the throttle -> refresh now
                st.pendingRevengSessionId = r.result.value("session_id", std::string());
                // Keep the lane the user created it from; the first detail poll must not flip it.
                s2.laneChosenFor = st.pendingRevengSessionId;
            });
        }
        if (s.newSessionPending)
            kit::InlineStatus("Creating session\xe2\x80\xa6", kit::Level::Busy);
        kit::InlineStatus(s.newSessionErr.c_str(), kit::Level::Error);
    }
    kit::CardEnd();
}

bool sessionMatchesFilter(const SessionSummary& ss, const char* filter) {
    if (!filter || filter[0] == '\0') return true;
    std::string needle(filter);
    std::string hay = ss.name + " " + ss.session_id + " " +
        (ss.hasGaussian ? "Gaussian " + ss.gaussianStatus + " " + ss.gaussianStage + " " + ss.gaussianMode
                        : ss.status);
    for (char& c : needle) c = (char)std::tolower((unsigned char)c);
    for (char& c : hay)    c = (char)std::tolower((unsigned char)c);
    return hay.find(needle) != std::string::npos;
}

void drawSessionRailBody(AppState& st) {
    RevEngState& s = S();

    // The column header already names this column; the body opens with its controls.
    // Filter + Refresh share a line while the filter keeps a usable reFieldW; a narrower rail
    // stacks them rather than squeezing the field to nothing and clipping the button.
    const ImGuiStyle& style    = ImGui::GetStyle();
    const float       availW   = ImGui::GetContentRegionAvail().x;
    const bool        oneLine  = availW >= M::reFieldW() + style.ItemSpacing.x + M::reBtnW();
    const float       refreshW = oneLine ? M::reBtnW() : availW;
    const float       filterW  = oneLine ? availW - refreshW - style.ItemSpacing.x : availW;
    const std::string filterHint =
        ui::kit::elide("Filter by name or id", filterW - style.FramePadding.x * 2.0f);
    ImGui::SetNextItemWidth(filterW);
    ImGui::InputTextWithHint("##re_rail_filter", filterHint.c_str(), s.railFilterBuf,
                             sizeof(s.railFilterBuf));
    if (oneLine) ImGui::SameLine(0.0f, style.ItemSpacing.x);
    else         kit::GapControl();
    // Busy only for a refresh the user asked for — the background poll must not flash it.
    if (kit::SecondaryButton(fitLabel(s.listManual ? "\xe2\x80\xa6" : "Refresh", "re_rail_refresh",
                                      refreshW).c_str(),
                             !s.listManual, "A refresh is already in flight.",
                             ImVec2(refreshW, 0.0f)))
        loadSessions(st, /*force=*/true, /*manual=*/true);

    // Sessions are scoped to the working folder; say which one so an empty list is not a mystery.
    if (!s.listedProject.empty()) {
        const std::string folder = std::filesystem::path(s.listedProject).filename().string();
        const std::string label = ui::kit::elide(
            ("In " + (folder.empty() ? s.listedProject : folder)).c_str(),
            ImGui::GetContentRegionAvail().x);
        ImGui::TextDisabled("%s", label.c_str());
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("Sessions of the working folder\n%s", s.listedProject.c_str());
    }

    kit::GapControl();

    drawNewSessionCard(st, s);
    kit::GapCards();

    if (!st.workerStarted) {
        kit::BlockedState("Worker not running",
                          "The session list comes from the Chudware worker, which is not up.",
                          "Start the worker, then press Refresh.");
        return;
    }
    if (!s.listErr.empty()) {
        if (kit::BlockedState("Sessions unavailable", s.listErr.c_str(),
                              "The Reverse Engineering tools may not be registered in this build.",
                              "Retry"))
            loadSessions(st, /*force=*/true, /*manual=*/true);
        return;
    }
    if (s.sessions.empty()) {
        if (s.listPending && !s.listedOnce) {
            kit::InlineStatus("Loading sessions\xe2\x80\xa6", kit::Level::Busy);
            return;
        }
        if (kit::EmptyState("part", "No sessions in this folder",
                            "Sessions belong to the working folder chosen at the top left. Create "
                            "one to scan a part or to start from a part number, URL or datasheet.",
                            "New session")) {
            s.focusNewName = true;
        }
        return;
    }

    int shown = 0;
    ImGui::BeginChild("##re_sess_list", ImVec2(0.0f, 0.0f), ImGuiChildFlags_None);
    for (int i = 0; i < (int)s.sessions.size(); ++i) {
        if (!sessionMatchesFilter(s.sessions[i], s.railFilterBuf)) continue;
        ++shown;
        drawSessionCard(st, s, i);
        kit::GapCards();
    }
    if (shown == 0) {
        kit::EmptyState("filter_funnel", "No match",
                        "No session name, id or status contains that text.", nullptr, false);
    }
    ImGui::EndChild();
}

// ================================================================================
// Session column
// ================================================================================
//
// One scrolling column, no tabs. In order it answers: what is this session, how far can its
// numbers be trusted, and — only when something is actually missing — which tools this intake
// lane still needs. It replaced a Details / Capabilities / Provenance tab strip whose raw backend
// probe and step log read as noise.

// The accuracy the session's geometry has earned, with the reason in plain words. DERIVED, never
// assumed: a scan only earns "high" once its scale is confirmed with a low residual.
void drawAccuracyTag(const RevEngState& s, const SessionSummary& ss) {
    if (s.sourceMode == SourceMode::Reference) {
        if (!s.ref.modelPath.empty())
            kit::HonestyTag("high", "An existing CAD model \xe2\x80\x94 check it against the real "
                                    "part before fit-critical use.");
        else
            kit::HonestyTag("medium", "Datasheet / catalogue values are nominal, often without "
                                      "tolerances. Confirm fit-critical dimensions on the part.");
        return;
    }
    if (ss.approximate) {
        kit::HonestyTag("approximate", "Preview geometry only \xe2\x80\x94 do not machine or mate "
                                       "from these dimensions without measuring the real part.");
    } else if (!s.honestyTier.empty()) {
        kit::HonestyTag(s.honestyTier.c_str(), "Single-photo reconstruction tier.");
    } else if (!s.scale.mirrored) {
        kit::HonestyTag("approximate", "No scale yet: the shape is right, the size is not.");
    } else if (!s.scale.confirmed) {
        kit::HonestyTag("low", "Scale solved but not confirmed. Confirm it in the Scale stage "
                               "before trusting any dimension.");
    } else if (!s.scale.hasResidual || s.scale.residualPct > 1.0) {
        kit::HonestyTag("medium", "Scale confirmed, but its fit error is unknown or above 1% "
                                  "\xe2\x80\x94 expect roughly \xc2\xb1""5%.");
    } else {
        kit::HonestyTag("high", "Confirmed, low-error scale \xe2\x80\x94 about \xc2\xb1""2%.");
    }
}

void drawSessionSummary(AppState& st, RevEngState& s) {
    const SessionSummary& ss = s.sessions[s.selected];
    const bool reference = s.sourceMode == SourceMode::Reference;
    if (kit::CardBegin("##re_sess_summary", "Session", "part")) {
        kit::KeyValueRow("Name", ss.name.c_str());
        const std::string status = ss.status.empty() ||
                                           (ss.status == "capturing" && ss.images == 0)
                                       ? std::string("new") : ss.status;
        kit::KeyValueRow("Status", status.c_str(),
                         status == "new" ? kit::Level::Neutral : statusLevel(ss.status));
        kit::KeyValueRow("Intake", reference ? "From reference" : "Scan");
        if (!reference) {
            char imgTxt[24];
            std::snprintf(imgTxt, sizeof(imgTxt), "%d", ss.images);
            kit::KeyValueRow("Images", imgTxt);
        }
        if (!ss.partRef.empty()) kit::KeyValueRow("Library part", ss.partRef.c_str());
        if (!ss.updatedAt.empty()) kit::KeyValueRow("Updated", ss.updatedAt.c_str());
        kit::GapControl();
        kit::FieldLabel("Accuracy");
        ImGui::SameLine(0.0f, M::reCtlGap());
        drawAccuracyTag(s, ss);
        kit::InlineStatus(s.detailErr.c_str(), kit::Level::Error);
    }
    kit::CardEnd();

    if (reference && !s.ref.modelPath.empty()) {
        kit::GapCards();
        if (kit::CardBegin("##re_sess_model", "Model", "part")) {
            const std::string file = std::filesystem::path(s.ref.modelPath).filename().string();
            kit::KeyValueRow("File", file.c_str());
            if (!s.ref.modelSource.empty()) kit::KeyValueRow("From", s.ref.modelSource.c_str());
            if (kit::SecondaryButton("Open model", true, nullptr,
                                     ImVec2(ImGui::GetContentRegionAvail().x, 0.0f)))
                st.requestOpenPath = s.ref.modelPath;
        }
        kit::CardEnd();
    }

    if (!reference) {
        kit::GapCards();
        if (kit::CardBegin("##re_sess_scale", "Scale", "measure")) {
            if (!s.scale.mirrored) {
                kit::KeyValueRow("Scale", "not set", kit::Level::Warn);
            } else {
                char mmTxt[48];
                std::snprintf(mmTxt, sizeof(mmTxt), "%.4g mm / unit", s.scale.mmPerUnit);
                kit::KeyValueRow("Scale", mmTxt);
                kit::KeyValueRow("Confirmed", s.scale.confirmed ? "yes" : "no",
                                 s.scale.confirmed ? kit::Level::Ok : kit::Level::Warn);
            }
            if (!s.scale.confirmed &&
                kit::SecondaryButton("Open Scale stage", true, nullptr,
                                     ImVec2(ImGui::GetContentRegionAvail().x, 0.0f))) {
                s.stage = Stage::Scale;
                clampStageToPipeline(s);
            }
        }
        kit::CardEnd();
    }
}

// Tools the CURRENT lane needs that this machine does not have. Draws nothing when everything is
// installed (or before the first probe lands): a wall of green "available" rows is not information.
void drawMissingTools(AppState& st, RevEngState& s) {
    CapabilityState& c = s.caps;
    if (!st.workerStarted || (!c.loaded && c.err.empty())) return;

    struct Missing { const char* name; std::string hint; };
    std::vector<Missing> missing;
    if (c.loaded) {
        auto hintFor = [&](const char* key, const char* fallback) {
            for (const CapabilityState::Artifact& a : c.artifacts)
                if (a.key == key && !a.hint.empty()) return a.hint;
            return std::string(fallback);
        };
        if (s.sourceMode == SourceMode::Scan) {
            if (!c.sparse)
                missing.push_back({"Reconstruction (COLMAP)",
                                   hintFor("colmap", "Install COLMAP and set COLMAP_BIN.")});
            if (!c.dense)
                missing.push_back({"Dense mesh (OpenMVS)",
                                   hintFor("openmvs", "Without it a reconstruction yields a sparse "
                                                      "point cloud only.")});
            if (!c.splat)
                missing.push_back({"Gaussian splat (Brush)",
                                   hintFor("brush", "Installs from the app for splat previews.")});
        } else {
            for (const CapabilityState::Artifact& a : c.artifacts)
                if (a.key == "docling" && !a.present)
                    missing.push_back({"Datasheet reader (docling)", a.hint});
        }
    }
    bool needsInstall = false;
    for (const CapabilityState::Artifact& a : c.artifacts)
        needsInstall = needsInstall || ((a.key == "brush" || a.key == "openmvs") && !a.present);
    if (missing.empty() && c.err.empty() && !c.installRunning && c.installErr.empty()) return;

    kit::GapCards();
    if (kit::CardBegin("##re_sess_tools", c.installRunning ? "Installing tools" : "Missing tools",
                       "warning_badge")) {
        if (!c.err.empty())
            kit::InlineStatus(("Could not check installed tools: " + c.err).c_str(), kit::Level::Warn);
        for (const Missing& m : missing) kit::CapabilityRow(m.name, false, m.hint.c_str());
        if (c.installRunning || c.installPending) {
            char label[160];
            const char* what = c.installMsg.empty() ? "Starting" : c.installMsg.c_str();
            if (c.installTotal > 0.0)
                std::snprintf(label, sizeof(label), "%s  %.0f / %.0f MB", what,
                              c.installDone / 1048576.0, c.installTotal / 1048576.0);
            else
                std::snprintf(label, sizeof(label), "%s", what);
            kit::ProgressBar(c.installTotal > 0.0 ? (float)(c.installDone / c.installTotal) : -1.0f,
                             label, kit::Level::Busy);
        } else if (!c.installErr.empty()) {
            kit::InlineStatus(("Install failed - " + c.installErr).c_str(), kit::Level::Error);
        }
        kit::GapControl();
        const bool installing = c.installRunning || c.installPending;
        if (needsInstall && !installing) {
            if (kit::PrimaryButton(c.installErr.empty() ? "Install missing tools (~210 MB)###re_tools_install"
                                                        : "Retry install###re_tools_install",
                                   true, nullptr, ImVec2(ImGui::GetContentRegionAvail().x, 0.0f)))
                kickInstallTools(st);
            kit::GapControl();
        }
        if (kit::SecondaryButton(c.manual ? "Checking\xe2\x80\xa6###re_tools_recheck"
                                          : "Re-check###re_tools_recheck",
                                 !c.manual, "A check is already running.",
                                 ImVec2(ImGui::GetContentRegionAvail().x, 0.0f)))
            loadCapabilities(st, /*force=*/true, /*manual=*/true);
    }
    kit::CardEnd();
}

void drawSessionColumnBody(AppState& st) {
    RevEngState& s = S();
    ImGui::BeginChild("##re_props_body", ImVec2(0.0f, 0.0f), ImGuiChildFlags_None);
    if (s.selected < 0 || s.selected >= (int)s.sessions.size()) {
        kit::EmptyState("part", "No session selected",
                        "Pick a session on the left, or create one.", nullptr, false);
    } else {
        drawSessionSummary(st, s);
    }
    drawMissingTools(st, s);
    ImGui::EndChild();
}

// ================================================================================
// Columns (collapse)
// ================================================================================

// A collapsed side column rolls back to a thin full-height header strip carrying the chevron and
// a vertical caption, and the freed width goes to the stage body. This is the pinned-column affordance
// (roll to the header strip, NEVER shrink to a floating pill) rotated to the horizontal axis the
// three RE columns are laid out on — the pinned columns in layout.cpp roll UP because they stack
// vertically; these roll SIDEWAYS because they stack horizontally. Returns true when the expand
// chevron is clicked; the caller routes that through revealColumn() so an auto-fold is pinned open.
bool drawCollapsedStrip(const char* id, const char* caption, bool autoFolded, float w) {
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(M::reCardPadY(), M::reCardPadY()));
    ui::pushGlassChildStyle();
    ImGui::BeginChild(id, ImVec2(w, 0.0f), ImGuiChildFlags_Border,
                      ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoScrollWithMouse);
    ui::popGlassChildStyle();
    ImGui::PopStyleVar();

    const float innerW = ImGui::GetContentRegionAvail().x;
    const bool expand = ImGui::Button("\xc2\xbb", ImVec2(innerW, M::reBtnH()));
    if (ImGui::IsItemHovered()) {
        if (autoFolded) ImGui::SetTooltip("Expand %s (folded to fit this window width)", caption);
        else            ImGui::SetTooltip("Expand %s", caption);
    }

    ImGui::Dummy(ImVec2(0.0f, M::reCtlGap()));
    for (const char* p = caption; *p; ++p) {
        const char ch[2] = { *p, '\0' };
        const float tw = ImGui::CalcTextSize(ch).x;
        ImGui::SetCursorPosX(ImGui::GetCursorPosX() + (innerW - tw) * 0.5f);
        ImGui::TextDisabled("%s", ch);
    }
    ImGui::EndChild();
    return expand;
}

// Header row inside an expanded column: title on the left, collapse chevron on the right.
// Returns true when the collapse chevron is clicked.
bool drawColumnHeader(const char* title) {
    const float chevW  = ImGui::GetFrameHeight();
    const float startX = ImGui::GetCursorPosX();
    const float availW = ImGui::GetContentRegionAvail().x;
    ImGui::TextUnformatted(title);
    ImGui::SameLine(startX + availW - chevW);
    const bool collapse = ImGui::Button("\xc2\xab", ImVec2(chevW, chevW));
    if (ImGui::IsItemHovered()) ImGui::SetTooltip("Collapse %s", title);
    kit::Rule();
    return collapse;
}

// ================================================================================
// Lane selector + TOP stage stepper
// ================================================================================

// Intake lane. The reference lane drops Scale + Reconstruct from the stepper entirely, which is
// why this sits ABOVE the stepper: changing the lane changes which steps exist.
bool drawSourceLane(RevEngState& s) {
    static const char* kLaneLabels[] = { "Scan", "From reference" };
    static const char* kLaneTips[] = {
        "Capture the real part with a webcam, photos, video or your phone, then scale and "
        "reconstruct it.",
        "Start from a part number, product name, URL, datasheet or library part. Existing CAD "
        "models (GrabCAD, TraceParts, Printables, ...) are searched first; a model is only built "
        "from documents when none fits.",
    };
    int lane = (int)s.sourceMode;

    kit::FieldLabel("Intake");
    ImGui::SameLine(0.0f, M::reCtlGap());
    kit::HelpMarker(kLaneTips[std::clamp(lane, 0, 1)]);
    const bool changed = kit::SegmentedControl("##re_lane", kLaneLabels, 2, &lane,
                                               ImGui::GetContentRegionAvail().x);
    if (changed && lane != (int)s.sourceMode) {
        s.sourceMode = (SourceMode)lane;
        return true;
    }
    return false;
}

// THE stage stepper — top of the centre column, clickable, coloured from the real session.stages
// badge mirror, with unreachable steps dimmed and explained.
void drawStageStepper(AppState& st, RevEngState& s) {
    const std::vector<Stage> pipeline = activeStagePipeline(s.sourceMode);
    const int n = (int)pipeline.size();

    std::vector<const char*> labels(n);
    std::vector<kit::Level>  states(n);
    std::vector<bool>        reach(n, true);

    const bool haveSession = s.selected >= 0 && s.selected < (int)s.sessions.size();
    // Only gate navigation when we actually know the backend's stage badges for THIS session.
    // Unknown state must never lock the user out of their own wizard.
    const bool mirrored = haveSession && s.detailSessionId == s.sessions[s.selected].session_id;

    int active = 0;
    for (int i = 0; i < n; ++i) {
        labels[i] = stageName(pipeline[i]);
        const kit::Level lvl = badgeLevel(s.stages[(int)pipeline[i]].badge);
        states[i] = lvl;
        if (pipeline[i] == s.stage) active = i;
    }
    // Source counts as done the moment the session actually has images, even if the manifest
    // badge has not caught up.
    if (haveSession && s.sessions[s.selected].images > 0 && states[0] == kit::Level::Neutral)
        states[0] = kit::Level::Ok;

    // Reachability from the real dependency graph, not from list order. A stage is reachable
    // when its PREREQUISITE stage is done, when it already carries backend state of its own, or
    // when it is the stage you are standing on (never trap the user where they are).
    const bool scanLane = (s.sourceMode == SourceMode::Scan);
    if (mirrored) {
        for (int i = 0; i < n; ++i) {
            if (pipeline[i] == Stage::Source) continue;      // always reachable
            Stage prereq = stagePrereq(pipeline[i]);
            int prereqIdx = -1;
            for (int j = 0; j < n; ++j)
                if (pipeline[j] == prereq) { prereqIdx = j; break; }
            // Doc lanes have no Reconstruct step: fall back to the step before this one.
            if (prereqIdx < 0) prereqIdx = i - 1;
            const bool prereqDone = prereqIdx < 0 || states[prereqIdx] == kit::Level::Ok;
            reach[i] = prereqDone || states[i] != kit::Level::Neutral || pipeline[i] == s.stage ||
                       (pipeline[i] == Stage::Deliver && !s.artifactMeshPath.empty());
        }
    }

    const ImVec2 trackMin = ImGui::GetCursorScreenPos();
    const float  trackW   = ImGui::GetContentRegionAvail().x;
    const float  trackH   = M::reStepperH();
    const int clicked = kit::StepTrack("##re_stage_steps", labels.data(), states.data(), n,
                                       active, /*clickable=*/true, trackH);

    // The kit has no per-step tooltip hook, so explain a LOCKED step from the band it occupies
    // (StepTrack divides its width evenly). Reachable steps are left to the kit.
    {
        const float stepW = trackW / (float)n;
        for (int i = 0; i < n; ++i) {
            if (reach[i]) continue;
            const ImVec2 a(trackMin.x + stepW * (float)i, trackMin.y);
            const ImVec2 b(a.x + stepW, trackMin.y + trackH);
            if (ImGui::IsMouseHoveringRect(a, b)) {
                ImGui::SetTooltip("%s is locked \xe2\x80\x94 %s", labels[i],
                                  stageGateReason(pipeline[i], scanLane));
                break;
            }
        }
    }

    if (clicked >= 0 && clicked < n) {
        if (reach[clicked]) {
            s.stage = pipeline[clicked];
        } else {
            // Transient, not sticky: a permanent warning line under the stepper would go stale
            // the moment the blocking stage finishes.
            st.toast(ui::ToastLevel::Info,
                     std::string(labels[clicked]) + " is locked \xe2\x80\x94 " +
                         stageGateReason(pipeline[clicked], scanLane));
        }
    }
}

// ================================================================================
// Stage body
// ================================================================================

void drawStageBody(AppState& st, RevEngState& s) {
    if (s.selected < 0 || s.selected >= (int)s.sessions.size()) {
        const bool reference = s.sourceMode == SourceMode::Reference;
        if (kit::EmptyState("part", reference ? "Start from a reference" : "Scan a part",
                            reference
                                ? "Give a part number, product name, URL or datasheet. Existing CAD "
                                  "models are searched for first; one is only built from documents "
                                  "when nothing fits.\n\nSelect a session on the left, or create one."
                                : "Capture the real part with a webcam, photos, video or your phone. "
                                  "Scale it, reconstruct it, then fit an editable model.\n\n"
                                  "Select a session on the left, or create one.",
                            "New session")) {
            s.focusNewName = true;
            revealColumn(/*rail=*/true);
        }
        return;
    }

    switch (s.stage) {
        case Stage::Source:      drawSourcesPanel(st);       break;
        case Stage::Scale:       drawScalePanel(st);         break;
        case Stage::Reconstruct: drawReconstructStage(st);   break;
        case Stage::Model:       drawModelPanel(st);         break;
        case Stage::Review:      reveng::drawReviewStage(st); break;
        case Stage::Deliver:     drawDeliverPanel(st);       break;
    }
}

// ================================================================================
// Per-frame housekeeping
// ================================================================================

// Select a newly created or externally seeded session only once it appears in the list.
void consumePendingSession(AppState& st, RevEngState& s) {
    if (st.pendingRevengSessionId.empty() || !s.listedOnce) return;
    for (int i = 0; i < (int)s.sessions.size(); ++i) {
        if (s.sessions[i].session_id != st.pendingRevengSessionId) continue;
        st.pendingRevengSessionId.clear();
        if (s.selected != i) {
            s.selected = i;
            s.stage = Stage::Source;
            resetForSessionChange(s);
        }
        if (SH().pendingSourceSession == s.sessions[i].session_id) {
            if (SH().pendingSourceKind == SourceFile::Video) {
                s.sourceVideoPath = SH().pendingSourcePath;
                s.sourceFocus = SourceFocus::Video;
                s.stage = Stage::Source;
            } else if (SH().pendingSourceKind == SourceFile::Splat) {
                s.artifactSplatPath = SH().pendingSplatPath;
                s.artifactIsImportedSplat = true;
                s.stage = Stage::Reconstruct;
            }
            s.sourceMode = SourceMode::Scan;
            SH().pendingSourceSession.clear();
            SH().pendingSourcePath.clear();
            SH().pendingSplatPath.clear();
            SH().pendingSourceKind = SourceFile::Other;
        }
        return;
    }
    // Keep the intended SID until a refresh includes it; never consume it against the old list.
    s.lastListT = -100.0;
}

// Session changes release capture and invalidate in-flight callbacks before replacing mirrors.
void resetForSessionChange(RevEngState& s) {
    webcamCaptureShutdown();
    ++SH().sessionEpoch;
#ifdef __EMSCRIPTEN__
    SH().splatArtifactId.clear();
    SH().gaussianArtifactId.clear();
    SH().splatRefTried.clear();
    SH().splatRefDisplayed.clear();
    SH().meshArtifactId.clear();
    SH().splatFetchPending = false;
    SH().meshFetchPending = false;
    ++SH().splatFetchSerial;
    ++SH().meshFetchSerial;
#endif
    SH().gaussianPollPending = false;
    SH().gaussianHydrated = false;
    SH().gaussianPollT = -100.0;
    SH().splatRevisionTried = 0;
    SH().splatTransformTried.clear();
    SH().splatLoadError.clear();
    SH().gaussianCameraFitted = false;
    SH().loadedViewCamera = {};
    SH().gaussianCaptureProjection = false;
    SH().gaussianArtifactDirectory.clear();
    SH().displayedSplatPath.clear();
    SH().displayedSplatTransformKey.clear();
    SH().splatActionPending = false;
    SH().splatActionError = false;
    SH().splatActionMsg.clear();
    SH().meshJobFinished.clear();
    SH().gaussianRight = {1, 0, 0};
    SH().gaussianUp = {0, 1, 0};
    SH().gaussianBack = {0, 0, 1};
    SH().gaussianYaw = 0.6f;
    SH().gaussianPitch = 0.25f;
    SH().cropDirty = false;
    SH().previewTransformKey.clear();
    SH().previewRequestKey.clear();
    SH().previewTriedKey.clear();
    SH().sparsePreviewWarnings.clear();
    SH().previewPending = false;
    SH().meshSourcePath.clear();
    SH().meshSourceKey.clear();
    SH().meshLoadPath.clear();
    SH().parametricDocId.clear();
    SH().parametricSourcePath.clear();
    SH().parametricSourceKey.clear();
    SH().parametricLoadPath.clear();
    SH().parametricPollPending = false;
    SH().parametricPollT = -100.0;
    s.sparsePoints.clear();
    s.sparseNativePoints.clear();
    s.sparseCenter = {};
    s.sparseRadius = 1.0f;
    s.sparsePreviewError.clear();
    s.meshPreview.reset();
    s.parametricPreview.reset();
    s.meshPreviewError.clear();
    s.parametricPreviewError.clear();
    ++s.meshPreviewRevision;
    ++s.parametricPreviewRevision;
    ++s.sparsePreviewRevision;
    s.splat.setCloud(render::SplatCloud{});
    s.splatPickMarks.setCloud({});
    s.splatArtifactTransform = {};
    s.splatDisplayedTransform = {};
    ++s.splatPreviewRevision;
    s.rePoints.setCloud({});
    s.rePoints.setFrusta({});
    s.cannedFixtureLoaded = false;
    s.detailPending = false;
    galleryFreeTextures(s.galleryImages);
    s.galleryImages.clear();
    s.gallerySession = s.selected;
    s.selectedImage = -1;
    s.sourceVideoPath.clear();

    s.splatLoaded = false;
    s.splatPathTried.clear();
    s.artifactSplatPath.clear();
    s.artifactMeshPath.clear();
    s.artifactSparseDir.clear();
    s.artifactIsImportedSplat = false;
    s.splatImportLoss.clear();
    s.splatUntransformed = false;
    s.detailSessionId.clear();
    s.lastDetailT = -100.0;
    s.detailErr.clear();
    s.ref = ReferenceState{};
    s.researchResult.clear();
    s.researchErr.clear();
    s.researchPending = false;
    s.datasheetPath.clear();
    s.datasheetErr.clear();
    s.datasheetPending = false;
    s.honestyPipeline.clear();
    s.honestyTier.clear();
    for (int i = 0; i < kStageCount; ++i) s.stages[i].badge = "idle";

    s.recon = ReconstructState{};
    s.gaussian = GaussianState{};
    if (s.selected >= 0 && s.selected < (int)s.sessions.size()) {
        const auto it = SH().gaussianRuns.find(s.sessions[s.selected].session_id);
        if (it != SH().gaussianRuns.end()) {
            s.gaussian.control = it->second;
            s.gaussian.pending = it->second->pending;
        }
    }
    s.scale   = ScaleState{};
    s.model   = ModelState{};
    s.deliver = DeliverState{};

    SH().reconStartT  = -1.0;
    SH().reconElapsed = 0.0;


    webcamCaptureResetSession();
}

}  // namespace

// Release what the workspace holds OUTSIDE the GL frame loop: the camera device and its preview
// texture. The img_tex thumbnail LRU is deliberately NOT cleared — it is shared with the gallery
// and self-evicting, so dropping it here would dangle ids other panels still hold.
// See the seam note in reveng_workspace.h: app.cpp has no "workspace closed" callback yet, so
// drawRevEngWorkspace also calls this on re-entry after a frame gap.
void revengWorkspaceTeardown() {
    webcamCaptureShutdown();
    SH().webcamLive = false;
    SH().thumbs.clear();
}

// ---- entry point ---------------------------------------------------------------

void drawRevEngWorkspace(AppState& st) {
    ShellState& sh = SH();
    const int frame = ImGui::GetFrameCount();
    // We stopped being drawn at some point => the user left the RE tab. app.cpp has no teardown
    // hook, so reclaim the camera here, before the first redraw of the returning tab.
    if (frame - sh.lastDrawFrame > 1 && sh.webcamLive) revengWorkspaceTeardown();
    sh.lastDrawFrame = frame;

    RevEngState& s = S();

    loadSessions(st);
    loadCapabilities(st);
    consumePendingSession(st, s);

    if (s.selected != s.gallerySession) resetForSessionChange(s);
    if (s.selected >= 0 && s.selected < (int)s.sessions.size()) loadSessionDetail(st);
    ensureSplatForSession(s);
    ensureSparseAndMeshPreview(st);
    ensureParametricPreview(st);
    pollGaussian(st);
    clampStageToPipeline(s);

    // The webcam widget only exists on the Scan lane's Source stage; when it stops being drawn
    // the device must be released rather than left holding /dev/video.
    const bool webcamReachable = s.selected >= 0 && s.stage == Stage::Source &&
                                 s.sourceMode == SourceMode::Scan;
    if (webcamReachable) {
        sh.webcamLive = true;
    } else if (sh.webcamLive) {
        sh.webcamLive = false;
        webcamCaptureShutdown();
    }

    // Column widths. Each column has a real minimum — rail reRailW, properties rePropW, stage body
    // rePreviewMinW. A body too narrow for all three folds the Auto side columns to their strips
    // (Properties first, then Sessions) instead of squeezing everything until it clips. The fold
    // is recomputed every frame from the width alone and never written into the user's intent.
    const float gutter = M::reGutter();
    const float stripW = M::reThumbSm();
    const float totalW = ImGui::GetContentRegionAvail().x;
    sh.sideBudget = totalW - 2.0f * gutter - M::rePreviewMinW();
    bool railFold = false, propFold = false;
    foldColumns(s, sh.sideBudget, railFold, propFold);
    const bool railAutoFolded = railFold && s.railIntent  != ColumnIntent::Collapsed;
    const bool propAutoFolded = propFold && s.propsIntent != ColumnIntent::Collapsed;

    float drawRailW = approachPx(sh.railW, railFold ? stripW : M::reRailW());
    float drawPropW = approachPx(sh.propW, propFold ? stripW : M::rePropW());
    float midW = totalW - drawRailW - drawPropW - 2.0f * gutter;
    // Only a column pinned Expanded (or a body narrower than two strips + the stage minimum) can
    // still overflow. The stage body then gives up its preferred minimum, down to the hero-preview
    // width; past that the side columns shrink too — the work surface must never vanish.
    const float midFloor = M::reThumbLg();
    if (midW < midFloor) {
        const float sideTotal = drawRailW + drawPropW;
        const float k = sideTotal > 0.0f
                            ? std::clamp(1.0f - (midFloor - midW) / sideTotal, 0.0f, 1.0f)
                            : 0.0f;
        drawRailW *= k;
        drawPropW *= k;
        midW = std::max(0.0f, totalW - drawRailW - drawPropW - 2.0f * gutter);
    }

    // ---- left: session rail --------------------------------------------------
    if (railFold && drawRailW <= stripW + M::reCardGap()) {
        if (drawCollapsedStrip("##reveng_rail_strip", "SESSIONS", railAutoFolded, drawRailW))
            revealColumn(/*rail=*/true);
    } else {
        ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(M::reCardPadX(), M::reCardPadY()));
        ui::pushGlassChildStyle();
        ImGui::BeginChild("##reveng_rail", ImVec2(drawRailW, 0.0f), ImGuiChildFlags_Border);
        ui::popGlassChildStyle();
        ImGui::PopStyleVar();
        if (drawColumnHeader("Sessions")) s.railIntent = ColumnIntent::Collapsed;
        drawSessionRailBody(st);
        ImGui::EndChild();
    }

    // ---- centre: lane + stepper + stage body ---------------------------------
    ImGui::SameLine(0.0f, gutter);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(M::reCardPadX(), M::reCardPadY()));
    ui::pushGlassChildStyle();
    ImGui::BeginChild("##reveng_center", ImVec2(midW, 0.0f), ImGuiChildFlags_Border);
    ui::popGlassChildStyle();
    ImGui::PopStyleVar();
    {
        if (drawSourceLane(s)) clampStageToPipeline(s);
        kit::GapControl();
        drawStageStepper(st, s);
        kit::Rule();
        kit::GapControl();

        ImGui::BeginChild("##reveng_stage_body", ImVec2(0.0f, 0.0f), ImGuiChildFlags_None);
        drawStageBody(st, s);
        if (s.stage == Stage::Model || s.stage == Stage::Deliver) {
            kit::InlineStatus(s.meshPreviewError.c_str(), kit::Level::Error);
            kit::InlineStatus(s.parametricPreviewError.c_str(), kit::Level::Error);
        }
        ImGui::EndChild();
    }
    ImGui::EndChild();

    // ---- right: session summary ----------------------------------------------
    ImGui::SameLine(0.0f, gutter);
    if (propFold && drawPropW <= stripW + M::reCardGap()) {
        if (drawCollapsedStrip("##reveng_props_strip", "SUMMARY", propAutoFolded, drawPropW))
            revealColumn(/*rail=*/false);
    } else {
        ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(M::reCardPadX(), M::reCardPadY()));
        ui::pushGlassChildStyle();
        ImGui::BeginChild("##reveng_props", ImVec2(drawPropW, 0.0f), ImGuiChildFlags_Border);
        ui::popGlassChildStyle();
        ImGui::PopStyleVar();
        if (drawColumnHeader("Summary")) s.propsIntent = ColumnIntent::Collapsed;
        drawSessionColumnBody(st);
        ImGui::EndChild();
    }
}

}  // namespace reveng

// Thin forwarders in the ui:: namespace so app.cpp calls ui::drawRevEngWorkspace(st).
void drawRevEngWorkspace(AppState& st) { reveng::drawRevEngWorkspace(st); }
void revengWorkspaceTeardown()          { reveng::revengWorkspaceTeardown(); }

}  // namespace ui
