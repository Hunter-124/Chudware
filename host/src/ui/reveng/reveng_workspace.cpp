// Reverse Engineering workspace — full-viewport shell for the "Reverse Eng" title-bar tab.
// Honest scan -> mesh -> AI -> parametric pipeline (contracts/reveng.md).
//
// RE-40 overhaul. Layout, top to bottom / left to right:
//   left   : session rail — rich session cards (thumbnail, status chip, honesty tag, stage
//            progress), a substring filter, and the New-session card.
//   center : source-lane SegmentedControl, then THE STAGE STEPPER (kit::StepTrack) pinned at
//            the TOP where a wizard's progress indicator belongs, then the active stage body.
//   right  : properties column with three tabs — Details / Capabilities / Provenance.
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
    std::string gaussianArtifactDirectory;
    struct SplatLoad {
        render::SplatCloud cloud;
        std::string error, path;
        uint64_t epoch = 0;
        uint64_t revision = 0;
        GaussianViewCamera viewCamera;
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
            return "Choose a source lane: scan photos/video/webcam, ingest a datasheet, look up a "
                   "part number on the web, or seed from the parts library.";
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
            return "Attach the result to the parts hub, export a bundle, or print a scale mat.";
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
                                          : "ingest a datasheet or research a part number in Source first";
        case Stage::Review:      return "build a model in the Model stage first";
        case Stage::Deliver:     return "build a model in the Model stage first";
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

// Returns the ordered stage list for the current source lane. Doc lanes (Datasheet, Web,
// FromLibrary) skip Scale + Reconstruct — there is nothing to scale or reconstruct.
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

// How far a session got, as a 0..1 fraction of the six canonical stages. Prefers the real
// per-stage badge mirror (only available for the selected session); falls back to the summary
// status, which is a real backend field rather than a guess.
float sessionProgress(const RevEngState& s, int idx) {
    if (idx == s.selected && s.detailSessionId == s.sessions[idx].session_id) {
        int done = 0;
        for (int i = 0; i < kStageCount; ++i)
            if (badgeLevel(s.stages[i].badge) == kit::Level::Ok) ++done;
        return (float)done / (float)kStageCount;
    }
    const std::string& st = s.sessions[idx].status;
    if (st == "done")           return 1.0f;
    if (st == "review")         return 5.0f / (float)kStageCount;
    if (st == "modeling")       return 4.0f / (float)kStageCount;
    if (st == "reconstructing") return 3.0f / (float)kStageCount;
    if (st == "capturing")      return s.sessions[idx].images > 0 ? 1.0f / (float)kStageCount : 0.0f;
    return 0.0f;
}

}  // namespace

RevEngState& S() {
    static RevEngState s;
    return s;
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

void loadSessions(AppState& st, bool force = false) {
    if (!st.workerStarted) return;
    RevEngState& s = S();
    if (s.listPending) return;
    const double now = ImGui::GetTime();
    if (!force && s.listedOnce && now - s.lastListT < 4.0) return;
    s.listPending = true;
    s.lastListT = now;
    st.rpc.call("list_reveng_sessions", nlohmann::json::object(), [](const core::RpcResult& r) {
        RevEngState& st2 = S();
        st2.listPending = false;
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

// Flatten a provenance record's non-structural fields into one "k=v · k=v" line.
std::string flattenProvenance(const nlohmann::json& j) {
    std::string out;
    for (auto it = j.begin(); it != j.end(); ++it) {
        const std::string& k = it.key();
        if (k == "step" || k == "ts") continue;
        std::string v;
        if (it->is_string())            v = it->get<std::string>();
        else if (it->is_boolean())      v = it->get<bool>() ? "yes" : "no";
        else if (it->is_null())         v = "\xe2\x80\x94";
        else                            v = it->dump();
        if (v.size() > 96) v = v.substr(0, 93) + "...";
        if (!out.empty()) out += "  \xc2\xb7  ";
        out += k;
        out += "=";
        out += v;
    }
    return out;
}

// Async: pull the full session manifest — artifact paths, scale, stage badges, honesty tags and
// the provenance timeline. Everything the properties column and Reconstruct stage mirror.
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
        for (int i = 0; i < kStageCount; ++i) st2.stages[i].badge = "idle";
        st2.provenance.clear();
        st2.honestyPipeline.clear();
        st2.honestyTier.clear();
        if (!r.result.contains("session") || !r.result["session"].is_object()) return;
        const auto& sess = r.result["session"];
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
            sc.mirrored    = true;
            sc.curMethod   = sj.value("method", std::string());
            sc.mmPerUnit   = sj.value("mm_per_unit", 0.0);
            sc.residualPct = sj.value("residual_pct", 0.0);
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

        // session.provenance[] (§2) — the audit trail, newest last in the manifest.
        if (sess.contains("provenance") && sess["provenance"].is_array()) {
            for (const auto& p : sess["provenance"]) {
                if (!p.is_object()) continue;
                ProvenanceRecord rec;
                rec.step   = p.value("step", std::string("(step)"));
                rec.ts     = p.value("ts", std::string());
                rec.detail = flattenProvenance(p);
                st2.provenance.push_back(std::move(rec));
            }
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
    if (key == "openmvs") return "install OpenMVS (AGPL-3.0, never bundled) and set OPENMVS_BIN \xe2\x80\x94 dense mesh";
    if (key == "brush")   return "provision the Brush binary and set BRUSH_BIN \xe2\x80\x94 3D Gaussian splatting";
    if (key == "triposr") return "provision TripoSR weights \xe2\x80\x94 single-photo tier";
    if (key == "vggt")    return "provision VGGT weights \xe2\x80\x94 feed-forward sparse alternative to COLMAP";
    if (key == "docling") return "install docling \xe2\x80\x94 datasheet ingest";
    return "not provisioned";
}

// Async: reveng_status() capability + provisioning probe. Throttled; Refresh forces it.
// This is what lets the Reconstruct stage disable Run with a real reason instead of letting the
// user click through into a backend_not_provisioned failure.
void loadCapabilities(AppState& st, bool force = false) {
    if (!st.workerStarted) return;
    CapabilityState& c = S().caps;
    if (c.pending) return;
    const double now = ImGui::GetTime();
    const double throttle = c.loaded ? 30.0 : 6.0;
    if (!force && now - c.lastT < throttle) return;
    c.pending = true;
    c.lastT = now;
    st.rpc.call("reveng_status", nlohmann::json::object(), [](const core::RpcResult& r) {
        CapabilityState& c2 = S().caps;
        c2.pending = false;
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
    });
}

// Publish only a successfully parsed cloud; a failed refresh never destroys the last preview.
void ensureSplatForSession(RevEngState& s) {
    auto& sh = SH();
    std::string want = s.gaussian.splatPath.empty() ? s.artifactSplatPath : s.gaussian.splatPath;
    if (want.empty() && s.recon.done && std::getenv("CHUDWARE_SHOT")) {
        const char* repo = std::getenv("CHUDWARE_REPO");
        want = std::string(repo ? repo : ".") + "/host/tests/fixtures/splat_tiny.ply";
    }
    if (sh.splatLoad.valid()) {
        if (sh.splatLoad.wait_for(std::chrono::seconds(0)) != std::future_status::ready) return;
        auto loaded = sh.splatLoad.get();
        if (loaded.epoch == sh.sessionEpoch && loaded.path == want &&
            loaded.revision == s.gaussian.artifactRevision) {
            if (!loaded.error.empty() || loaded.cloud.empty()) {
                sh.splatLoadError = "Cannot load Gaussian preview: " +
                    (loaded.error.empty() ? std::string("empty artifact") : loaded.error);
            } else {
                s.splat.setCloud(std::move(loaded.cloud));
                s.splatLoaded = true;
                sh.splatLoadError.clear();
                s.artifactSplatPath = loaded.path;
                const std::string directory = std::filesystem::path(loaded.path).parent_path().string();
                const bool newGauge = loaded.viewCamera.id != sh.loadedViewCamera.id;
                sh.loadedViewCamera = std::move(loaded.viewCamera);
                if (!sh.loadedViewCamera.id.empty()) {
                    if (newGauge || !sh.gaussianCameraFitted) restoreGaussianCaptureCamera();
                } else if (newGauge || !sh.gaussianCameraFitted ||
                           sh.gaussianArtifactDirectory != directory) {
                    sh.gaussianRight = {1, 0, 0};
                    sh.gaussianUp = {0, 1, 0};
                    sh.gaussianBack = {0, 0, 1};
                    sh.gaussianYaw = 0.6f;
                    sh.gaussianPitch = 0.25f;
                    fitGaussianCamera();
                }
                sh.gaussianArtifactDirectory = directory;
            }
        }
    }
    if (want.empty()) return;
    if (s.splatPathTried == want && sh.splatRevisionTried == s.gaussian.artifactRevision) return;
    s.splatPathTried = want;
    sh.splatRevisionTried = s.gaussian.artifactRevision;
    const auto epoch = sh.sessionEpoch;
    const auto revision = s.gaussian.artifactRevision;
    const auto viewCamera = want == s.gaussian.splatPath ? s.gaussian.viewCamera : GaussianViewCamera{};
    sh.splatLoad = std::async(std::launch::async, [want, epoch, revision, viewCamera] {
        ShellState::SplatLoad result;
        result.path = want;
        result.epoch = epoch;
        result.revision = revision;
        result.viewCamera = viewCamera;
        try {
            if (!render::loadSplatPly(want, result.cloud, result.error) && result.error.empty())
                result.error = "Invalid Gaussian PLY";
        } catch (const std::exception& error) {
            result.error = error.what();
        }
        return result;
    });
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

void startGaussian(AppState& st, const std::string& sid, bool live,
                   const std::string& videoPath) {
    if (!selectedSession(sid)) return;
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
    pollGaussian(st);
    ensureSplatForSession(s);
    const bool running = gaussianRunning(g);
    const bool busy = running || g.pending;
    const kit::Level level = !g.error.empty() || g.status == "failed" ? kit::Level::Error
                           : busy ? kit::Level::Busy
                           : g.status == "done" ? kit::Level::Ok
                           : g.status == "interrupted" || g.status == "cancelled" ? kit::Level::Warn
                           : kit::Level::Neutral;
    if (kit::CardBegin("##gaussian_job", "3D Gaussian splatting", "display_shaded")) {
        kit::StatusChip(g.pending ? "Sending request" : g.status.empty() ? "Ready" : g.status.c_str(), level);
        kit::InlineStatus("Local photo reconstruction in reconstruction units — not a scaled CAD model.",
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
        if (kit::PrimaryButton("Start Gaussian splat", !busy && st.workerStarted &&
                               !s.recon.active && !s.recon.kickPending,
                               "Select an idle session with accepted photos or use live camera capture."))
            startGaussian(st, sid, false);
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
    if (c == "reveng_datasheet")    { toStage(Stage::Source); s.sourceMode = SourceMode::Datasheet;
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
                   "Refresh on the Capabilities tab.";
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
        g.warn = "OpenMVS is not configured, so this run produces a SPARSE point cloud only \xe2\x80\x94 "
                 "no dense mesh. Install OpenMVS (AGPL-3.0, never bundled) and set OPENMVS_BIN for "
                 "a watertight mesh.";
    }
    if (!s.caps.loaded) {
        g.warn = "Backend availability has not been probed yet \xe2\x80\x94 open the Capabilities tab "
                 "to check before a long run.";
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

// Inline 3D preview of whatever the reconstruction produced. The GL pass that fills this region
// runs post-ImGui from app.cpp; it reads the rect back via kit::previewSlotRect(slot).
void drawReconPreview(RevEngState& s) {
    const kit::PreviewSlot slot = reconSlot(s);
    // The canned sparse fixture deliberately does NOT count as an artifact: it is demo scaffolding,
    // and treating it as content is exactly how "it worked, trust me" sneaks back in.
    const bool haveArtifact = (slot == kit::PreviewSlot::Mesh) || !s.artifactSparseDir.empty();

    const char* emptyLabel = haveArtifact ? nullptr
                                          : "No reconstruction yet \xe2\x80\x94 run one to see the "
                                            "point cloud and camera poses here";
    const ImVec2 box(ImGui::GetContentRegionAvail().x, M::reThumbLg());
    kit::Viewport3DPreview("##recon_preview", box, slot, emptyLabel);

    // Always say WHAT is on screen and whether it is this session's data.
    if (slot == kit::PreviewSlot::Mesh) {
        kit::ValueChip("mesh", "ready", kit::Level::Ok, s.artifactMeshPath.c_str());
    } else if (!s.artifactSparseDir.empty()) {
        kit::ValueChip("sparse model", "ready", kit::Level::Ok, s.artifactSparseDir.c_str());
    } else if (s.recon.done) {
        kit::InlineStatus("Reconstruction reported done but returned no artifact path \xe2\x80\x94 "
                          "nothing to preview.", kit::Level::Warn);
    }
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
    if (!s.cannedFixtureLoaded && std::getenv("CHUDWARE_SHOT")) {
        s.cannedFixtureLoaded = true;
        std::vector<render::SparsePoint>   pts;
        std::vector<render::CameraFrustum> frusta;
        render::buildCannedSparseFixture(pts, frusta);
        s.rePoints.setCloud(pts);
        s.rePoints.setFrusta(frusta);
    }
    // GL-safe: SplatRenderer::load() is CPU-only (GL resources are created on first draw()).
    ensureSplatForSession(s);

    const ReconGate gate = reconPreflight(st, s);

    if (gate.hardBlock) {
        if (kit::BlockedState("CAD reconstruction unavailable", gate.reason.c_str(), gate.hint.c_str(),
                              "Re-check backends")) {
            loadCapabilities(st, /*force=*/true);
            S().propsTab = 1;
            revealColumn(/*rail=*/false);
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
                char frameTxt[24];
                std::snprintf(frameTxt, sizeof(frameTxt), "%d", ss.gaussianFrames);
                kit::ValueChip("frames", frameTxt, kit::Level::Neutral,
                               "Frames accepted by the Gaussian job, including imported video.");
                if (!ss.gaussianMode.empty())
                    kit::KeyValueRow("Mode", ss.gaussianMode.c_str());
                char registeredTxt[48];
                std::snprintf(registeredTxt, sizeof(registeredTxt), "%d/%d",
                              ss.gaussianRegistered, ss.gaussianFrames);
                kit::KeyValueRow("Registered", registeredTxt);
                char progressTxt[96];
                std::snprintf(progressTxt, sizeof(progressTxt), "%s %d%%",
                              ss.gaussianStage.empty() ? "Gaussian" : ss.gaussianStage.c_str(),
                              std::clamp(ss.gaussianPct, 0, 100));
                kit::ProgressBar(std::clamp(ss.gaussianPct, 0, 100) / 100.0f, progressTxt, level);
            } else {
                kit::StatusChip(fitChip(ss.status.empty() ? "new" : ss.status).c_str(),
                                statusLevel(ss.status));
                char imgTxt[24];
                std::snprintf(imgTxt, sizeof(imgTxt), "%d", ss.images);
                flowSameLine(rightX, valueChipW("img", imgTxt), gap);
                kit::ValueChip("img", imgTxt, ss.images > 0 ? kit::Level::Ok : kit::Level::Neutral,
                               "Images captured or imported into this session.");
            }
            if (ss.approximate) {
                flowSameLine(rightX, statusChipW("approximate"), gap);
                kit::HonestyTag("approximate",
                                "This session's pipeline is flagged approximate \xe2\x80\x94 reference/preview "
                                "geometry only, not a measured model.");
            }

            if (!ss.hasGaussian) {
                const float frac = sessionProgress(s, i);
                char stageTxt[32];
                std::snprintf(stageTxt, sizeof(stageTxt), "%d/%d stages",
                              (int)(frac * kStageCount + 0.5f), kStageCount);
                kit::ProgressBar(frac, stageTxt,
                                 frac >= 1.0f ? kit::Level::Ok : kit::Level::Neutral);
            }
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
        const std::string hint = ui::kit::elide("Environment / object name (optional)", textW);
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
    if (kit::SecondaryButton(fitLabel(s.listPending ? "\xe2\x80\xa6" : "Refresh", "re_rail_refresh",
                                      refreshW).c_str(),
                             !s.listPending, "A refresh is already in flight.",
                             ImVec2(refreshW, 0.0f)))
        loadSessions(st, /*force=*/true);

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
            loadSessions(st, /*force=*/true);
        return;
    }
    if (s.sessions.empty()) {
        if (s.listPending && !s.listedOnce) {
            kit::InlineStatus("Loading sessions\xe2\x80\xa6", kit::Level::Busy);
            return;
        }
        if (kit::EmptyState("part", "No sessions yet",
                            "Capture an environment or object with a live camera, video or photos. "
                            "Gaussian references use reconstruction units; measured CAD needs scale and review.",
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
// Properties column
// ================================================================================

void drawPropsDetails(AppState& st, RevEngState& s) {
    if (s.selected < 0 || s.selected >= (int)s.sessions.size()) {
        kit::EmptyState("part", "No session selected",
                        "Pick a session in the rail to see its manifest, scale and artifacts.",
                        nullptr, false);
        return;
    }
    const SessionSummary& ss = s.sessions[s.selected];

    // Visual anchor: whichever contact-sheet image is selected (falls back to the first).
    if (kit::CardBegin("##props_hero", "Selected image", "sketch")) {
        const int idx = (s.selectedImage >= 0 && s.selectedImage < (int)s.galleryImages.size())
                            ? s.selectedImage
                            : (s.galleryImages.empty() ? -1 : 0);
        const ImVec2 box(ImGui::GetContentRegionAvail().x, M::reThumbLg());
        if (idx >= 0) {
            const GalleryImage& g = s.galleryImages[idx];
            char cap[128];
            std::snprintf(cap, sizeof(cap), "%d / %d  \xc2\xb7  %s", idx + 1,
                          (int)s.galleryImages.size(),
                          g.quality.empty() ? "ok" : g.quality.c_str());
            kit::HeroPreview("##props_hero_img", g.texId, g.thumbW, g.thumbH, box, cap, nullptr);
        } else {
            kit::HeroPreview("##props_hero_img", 0, 0, 0, box, nullptr,
                             "No images in this session yet");
        }
    }
    kit::CardEnd();
    kit::GapCards();

    if (kit::CardBegin("##props_session", "Session", "part")) {
        kit::KeyValueRow("Name", ss.name.c_str());
        kit::KeyValueRow("Session id", ss.session_id.c_str());
        kit::KeyValueRow("Status", ss.status.empty() ? "(unknown)" : ss.status.c_str(),
                         statusLevel(ss.status));
        char imgTxt[24];
        std::snprintf(imgTxt, sizeof(imgTxt), "%d", ss.images);
        kit::KeyValueRow("Images", imgTxt,
                         ss.images > 0 ? kit::Level::Ok : kit::Level::Warn);
        if (!ss.createdAt.empty()) kit::KeyValueRow("Created", ss.createdAt.c_str());
        if (!ss.updatedAt.empty()) kit::KeyValueRow("Updated", ss.updatedAt.c_str());
        if (!ss.partRef.empty())   kit::KeyValueRow("Part ref", ss.partRef.c_str());

        kit::GapControl();
        kit::KeyValueRow("Pipeline", s.honestyPipeline.empty() ? "scan -> mesh -> AI -> parametric"
                                                               : s.honestyPipeline.c_str());
        // Tier is DERIVED, never assumed. A multi-view scan only earns "high" once its scale is
        // confirmed; without a scale the geometry has shape but no size, which is "approximate".
        if (ss.approximate) {
            kit::HonestyTag("approximate",
                            "Reference/preview geometry only \xe2\x80\x94 do not machine or mate "
                            "from these dimensions without measuring the real part.");
        } else if (!s.honestyTier.empty()) {
            kit::HonestyTag(s.honestyTier.c_str(),
                            "Single-photo reconstruction tier reported by the backend.");
        } else if (!s.scale.mirrored) {
            kit::HonestyTag("approximate",
                            "No scale has been solved yet, so nothing here is a real-world "
                            "measurement.");
        } else if (!s.scale.confirmed) {
            kit::HonestyTag("low",
                            "A scale was solved but not confirmed. Confirm it in the Scale stage "
                            "before trusting any dimension.");
        } else if (s.scale.residualPct > 1.0) {
            kit::HonestyTag("medium",
                            "Scale is confirmed but its residual is above 1%, so expect roughly "
                            "\xc2\xb1""5%.");
        } else {
            kit::HonestyTag("high",
                            "Multi-view scan with a confirmed, low-residual scale \xe2\x80\x94 "
                            "about \xc2\xb1""2%.");
        }
        kit::InlineStatus(s.detailErr.c_str(), kit::Level::Error);
        if (s.detailPending) kit::InlineStatus("Refreshing manifest\xe2\x80\xa6", kit::Level::Busy);
    }
    kit::CardEnd();
    kit::GapCards();

    if (kit::CardBegin("##props_scale", "Scale", "measure")) {
        if (!s.scale.mirrored) {
            kit::InlineStatus("No scale set yet \xe2\x80\x94 the model will have shape but no size.",
                              kit::Level::Warn);
        } else {
            kit::KeyValueRow("Method", s.scale.curMethod.empty() ? "(pending)" : s.scale.curMethod.c_str());
            char mmTxt[48];
            std::snprintf(mmTxt, sizeof(mmTxt), "%.4g mm / unit", s.scale.mmPerUnit);
            kit::KeyValueRow("Scale factor", mmTxt);
            kit::KeyValueRow("Residual", kit::fmtPct(s.scale.residualPct / 100.0, 2).c_str(),
                             s.scale.residualPct <= 1.0 ? kit::Level::Ok : kit::Level::Warn);
            kit::KeyValueRow("Confirmed", s.scale.confirmed ? "yes" : "no",
                             s.scale.confirmed ? kit::Level::Ok : kit::Level::Warn);
        }
        if (kit::SecondaryButton("Open Scale stage", true, nullptr,
                                 ImVec2(ImGui::GetContentRegionAvail().x, 0.0f))) {
            s.stage = Stage::Scale;
            clampStageToPipeline(s);
        }
    }
    kit::CardEnd();
    kit::GapCards();

    if (kit::CardBegin("##props_artifacts", "Artifacts", "results")) {
        auto artifactRow = [](const char* label, const std::string& path) {
            kit::StatusChip(label, path.empty() ? kit::Level::Neutral : kit::Level::Ok,
                            path.empty() ? "Not produced yet." : path.c_str());
        };
        artifactRow("sparse", s.artifactSparseDir);
        ImGui::SameLine(0.0f, M::reCtlGap());
        artifactRow("mesh", s.artifactMeshPath);
        ImGui::SameLine(0.0f, M::reCtlGap());
        artifactRow("splat", s.artifactSplatPath);
        if (s.artifactSparseDir.empty() && s.artifactMeshPath.empty() && s.artifactSplatPath.empty())
            kit::InlineStatus("Nothing reconstructed yet.", kit::Level::Neutral);
    }
    kit::CardEnd();
    (void)st;
}

void drawPropsCapabilities(AppState& st, RevEngState& s) {
    CapabilityState& c = s.caps;

    if (kit::CardBegin("##caps_head", "What this machine can do", "health_check")) {
        const float btnW = M::reBtnW();
        kit::CardHeaderRight(btnW);
        if (kit::SecondaryButton(c.pending ? "\xe2\x80\xa6" : "Refresh", !c.pending,
                                 "A probe is already in flight.", ImVec2(btnW, 0.0f)))
            loadCapabilities(st, /*force=*/true);

        if (!st.workerStarted) {
            kit::BlockedState("Worker not running",
                              "reveng_status() cannot be reached, so backend availability is unknown.",
                              "Start the Chudware worker and press Refresh.");
            kit::CardEnd();
            return;
        }
        if (!c.err.empty()) {
            if (kit::BlockedState("Capability probe failed", c.err.c_str(),
                                  "The reveng tools may not be registered in this worker build.",
                                  "Retry"))
                loadCapabilities(st, /*force=*/true);
            kit::CardEnd();
            return;
        }
        if (!c.loaded) {
            kit::InlineStatus("Probing backends\xe2\x80\xa6", kit::Level::Busy);
            kit::CardEnd();
            return;
        }

        kit::CapabilityRow("Sparse reconstruction", c.sparse,
                           c.sparse ? "COLMAP or VGGT is available."
                                    : "Install COLMAP (COLMAP_BIN) or provision VGGT weights.");
        kit::CapabilityRow("Dense mesh", c.dense,
                           c.dense ? "OpenMVS is available."
                                   : "Needs COLMAP plus OpenMVS (OPENMVS_BIN). Without it a run "
                                     "yields a sparse cloud only.");
        kit::CapabilityRow("3D Gaussian splat", c.splat,
                           c.splat ? "Brush is available."
                                   : "Provision the Brush binary (BRUSH_BIN) for splat previews.");
        kit::CapabilityRow("Single photo", c.singlePhoto,
                           c.singlePhoto ? "TripoSR / TRELLIS weights present."
                                         : "Provision TripoSR or TRELLIS weights. Single-photo output "
                                           "is approximate by definition.");
        kit::CapabilityRow("Webcam capture", c.webcam,
                           c.webcam ? "A camera device was found."
                                    : "No camera device found \xe2\x80\x94 plug one in, or import photos "
                                      "from disk instead.");
        kit::CapabilityRow("GPU", c.gpu,
                           c.gpu ? "CUDA-capable GPU detected."
                                 : "CPU only: reconstruction still works but is far slower, and "
                                   "splat training is impractical.");
    }
    kit::CardEnd();
    if (!c.loaded) return;
    kit::GapCards();

    if (kit::CardBegin("##caps_artifacts", "Backends found", "settings")) {
        if (c.artifacts.empty()) {
            kit::InlineStatus("reveng_status() returned no provisioning block.", kit::Level::Warn);
        } else {
            for (const CapabilityState::Artifact& a : c.artifacts)
                kit::CapabilityRow(a.key.c_str(), a.present, a.hint.c_str());
        }
    }
    kit::CardEnd();

    if (!c.banners.empty()) {
        kit::GapCards();
        if (kit::CardBegin("##caps_banners", "Notices", "warning_badge")) {
            for (const std::string& b : c.banners)
                kit::InlineStatus(b.c_str(), kit::Level::Warn);
        }
        kit::CardEnd();
    }
}

void drawPropsProvenance(RevEngState& s) {
    if (s.selected < 0 || s.selected >= (int)s.sessions.size()) {
        kit::EmptyState("history", "No session selected",
                        "The provenance log records every pipeline step that touched a session.",
                        nullptr, false);
        return;
    }
    if (s.provenance.empty()) {
        kit::EmptyState("history", "Nothing recorded yet",
                        "Each capture, scale solve, reconstruction and model build appends a "
                        "record here. It is what makes a photo-derived dimension defensible.",
                        nullptr, false);
        return;
    }
    if (kit::CardBegin("##props_prov", "Provenance", "history")) {
        // Newest first: the last thing that happened is what you are asking about.
        for (int i = (int)s.provenance.size() - 1; i >= 0; --i) {
            const ProvenanceRecord& p = s.provenance[i];
            ImGui::PushID(i);
            kit::KeyValueRow(p.step.c_str(), p.ts.empty() ? "" : p.ts.c_str());
            if (!p.detail.empty()) {
                const float* d = ui::palette.textDisabled;
                ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(d[0], d[1], d[2], d[3]));
                ImGui::TextWrapped("%s", p.detail.c_str());
                ImGui::PopStyleColor();
            }
            if (i > 0) kit::Rule();
            ImGui::PopID();
        }
    }
    kit::CardEnd();
}

void drawPropertiesBody(AppState& st) {
    RevEngState& s = S();
    static const char* kTabs[] = { "Details", "Capabilities", "Provenance" };
    kit::SegmentedControl("##re_props_tabs", kTabs, 3, &s.propsTab,
                          ImGui::GetContentRegionAvail().x);
    kit::GapControl();

    ImGui::BeginChild("##re_props_body", ImVec2(0.0f, 0.0f), ImGuiChildFlags_None);
    switch (s.propsTab) {
        case 1:  drawPropsCapabilities(st, s); break;
        case 2:  drawPropsProvenance(s);       break;
        default: drawPropsDetails(st, s);      break;
    }
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

// RE-30: source lane. Doc lanes drop Scale + Reconstruct from the stepper entirely, which is why
// this sits ABOVE the stepper: changing the lane changes which steps exist.
bool drawSourceLane(RevEngState& s) {
    static const char* kLaneLabels[] = { "Scan", "Datasheet", "Web / Part#", "From Library" };
    static const char* kLaneTips[] = {
        "Photos / video / webcam -> Scale -> Reconstruct -> Model",
        "Local PDF or spec sheet -> Model  (no Scale / Reconstruct)",
        "Part number or URL -> Research -> Model",
        "Seeded from the Parts Library \xe2\x80\x94 auto-routed to the right lane",
    };
    int lane = (int)s.sourceMode;

    kit::FieldLabel("Source lane");
    ImGui::SameLine(0.0f, M::reCtlGap());
    kit::HelpMarker(kLaneTips[std::clamp(lane, 0, 3)]);
    const bool changed = kit::SegmentedControl("##re_lane", kLaneLabels, 4, &lane,
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
            reach[i] = prereqDone || states[i] != kit::Level::Neutral || pipeline[i] == s.stage;
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
        if (kit::EmptyState("part", "Capture an environment or object",
                            "Use live camera capture, imported video or photos to build a Gaussian "
                            "reference in reconstruction units — not a dimensionally-correct CAD model.\n\n"
                            "Measured, editable CAD is a separate workflow requiring scale references "
                            "and dimension review. Arbitrary photos do not guarantee accurate dimensions.\n\n"
                            "Select a session in the rail, or create one to begin.",
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
        return;
    }
    // Keep the intended SID until a refresh includes it; never consume it against the old list.
    s.lastListT = -100.0;
}

// Session changes release capture and invalidate in-flight callbacks before replacing mirrors.
void resetForSessionChange(RevEngState& s) {
    webcamCaptureShutdown();
    ++SH().sessionEpoch;
    SH().gaussianPollPending = false;
    SH().gaussianHydrated = false;
    SH().gaussianPollT = -100.0;
    SH().splatRevisionTried = 0;
    SH().splatLoadError.clear();
    SH().gaussianCameraFitted = false;
    SH().loadedViewCamera = {};
    SH().gaussianCaptureProjection = false;
    SH().gaussianArtifactDirectory.clear();
    SH().gaussianRight = {1, 0, 0};
    SH().gaussianUp = {0, 1, 0};
    SH().gaussianBack = {0, 0, 1};
    SH().gaussianYaw = 0.6f;
    SH().gaussianPitch = 0.25f;
    SH().cropDirty = false;
    s.splat.setCloud(render::SplatCloud{});
    s.rePoints.setCloud({});
    s.rePoints.setFrusta({});
    s.cannedFixtureLoaded = false;
    s.detailPending = false;
    galleryFreeTextures(s.galleryImages);
    s.galleryImages.clear();
    s.gallerySession = s.selected;
    s.selectedImage = -1;

    s.splatLoaded = false;
    s.splatPathTried.clear();
    s.artifactSplatPath.clear();
    s.artifactMeshPath.clear();
    s.artifactSparseDir.clear();
    s.detailSessionId.clear();
    s.lastDetailT = -100.0;
    s.detailErr.clear();
    s.provenance.clear();
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
    // MUST be first: every Viewport3DPreview this frame republishes into a cleared slot table.
    kit::resetPreviewSlots();

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
        ImGui::EndChild();
    }
    ImGui::EndChild();

    // ---- right: properties ---------------------------------------------------
    ImGui::SameLine(0.0f, gutter);
    if (propFold && drawPropW <= stripW + M::reCardGap()) {
        if (drawCollapsedStrip("##reveng_props_strip", "DETAILS", propAutoFolded, drawPropW))
            revealColumn(/*rail=*/false);
    } else {
        ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(M::reCardPadX(), M::reCardPadY()));
        ui::pushGlassChildStyle();
        ImGui::BeginChild("##reveng_props", ImVec2(drawPropW, 0.0f), ImGuiChildFlags_Border);
        ui::popGlassChildStyle();
        ImGui::PopStyleVar();
        if (drawColumnHeader("Properties")) s.propsIntent = ColumnIntent::Collapsed;
        drawPropertiesBody(st);
        ImGui::EndChild();
    }
}

}  // namespace reveng

// Thin forwarders in the ui:: namespace so app.cpp calls ui::drawRevEngWorkspace(st).
void drawRevEngWorkspace(AppState& st) { reveng::drawRevEngWorkspace(st); }
void revengWorkspaceTeardown()          { reveng::revengWorkspaceTeardown(); }

}  // namespace ui
