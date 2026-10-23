// Reverse Engineering workspace — internal store + shared types (contracts/reveng.md).
// GL-thread only: the store is a plain file-static singleton (no mutex), mutated only from
// draw-path rpc callbacks that the worker drains on the GL thread. Never do a SYNC worker
// call in a draw path (known freeze bug); all session data arrives via ASYNC st.rpc.call.
#pragma once

#include <string>
#include <cstdint>
#include <memory>
#include <vector>

// RE-19: GalleryImage is defined in image_gallery.h; included below to avoid
// a circular dependency (image_gallery.h must NOT include reveng_internal.h).
#include "reveng_workspace.h"  // shared Gaussian actions
#include "image_gallery.h"
#include "render/re_points.h"       // RE-21: sparse point-cloud + frusta overlay
#include "render/splat_renderer.h"  // RE-28: 3DGS splat renderer (wired RE-27)

struct AppState;

namespace ui {
namespace reveng {

// RE-30: Source lane — determines which stages appear in the adaptive stepper.
//   Scan        : Source → Scale → Reconstruct → Model → Review → Deliver
//   Datasheet   : Source → Model → Review → Deliver  (skip Scale + Reconstruct)
//   Web         : Source → Model → Review → Deliver  (skip Scale + Reconstruct)
//   FromLibrary : seeded externally; auto-routed to Scan or Datasheet/Web
enum class SourceMode { Scan, Datasheet, Web, FromLibrary };

// RE-40: one-shot deep-link target for the three DISTINCT Sources entry points. The ribbon
// buttons reveng_add_photos / reveng_add_video / reveng_webcam used to collapse into one
// identical handler (switch to Source + Scan), so three differently-labelled buttons did the
// same thing. Each now sets this before switching, and sources_panel.cpp expands / scrolls to /
// keyboard-focuses the matching sub-widget.
//
// CONTRACT: one-shot. The consumer (sources_panel.cpp) MUST reset it to SourceFocus::None after
// acting on it — same consume-and-clear discipline as RevEngState::focusNewName.
enum class SourceFocus { None, Photos, Video, Webcam };

// The six pipeline stages in canonical order (contracts/reveng.md §2 session.stages).
// The adaptive stepper shows only the stages relevant to the active SourceMode.
enum class Stage { Source, Scale, Reconstruct, Model, Review, Deliver };
constexpr int kStageCount = 6;

// User intent for a collapsible side column (see RevEngState::railIntent). Auto = the shell's
// responsive layout decides; Collapsed / Expanded = the user said so explicitly this session.
enum class ColumnIntent { Auto, Collapsed, Expanded };

// Per-stage badge state mirrored from session.stages values ("idle"|"pending"|"done"|...).
struct StageState {
    std::string badge = "idle";  // raw contract badge string
};

// One row in the session rail — the summary subset of aicad.reveng_session/1 the list tool
// returns. Kept intentionally thin; the detail view fetches the full manifest on demand.
struct SessionSummary {
    std::string session_id;
    std::string name;
    std::string status;   // capturing|reconstructing|modeling|review|done|error
    bool approximate = false;
    int images = 0;
    // RE-40: the rest of what list_reveng_sessions already returns. The rail cards and the
    // properties Details tab show these instead of re-fetching the full manifest per row.
    std::string createdAt;   // ISO-8601
    std::string updatedAt;   // ISO-8601
    std::string partRef;     // parts-hub part_id when seeded from the library; empty otherwise
    // Optional Gaussian sidecar projection; never replaces CAD status or image counts.
    bool hasGaussian = false;
    std::string gaussianStatus, gaussianStage, gaussianMode;
    int gaussianFrames = 0, gaussianRegistered = 0, gaussianPct = 0;
};

// RE-21/RE-40: Reconstruct-stage progress event (mirrors aicad.reveng_events/1 subset).
// Fed by core::engineEventsTick() (host/src/core/engine_events.cpp), which tails the reveng
// mission's EventJournal (<cache>/runs/<run_id>/events.jsonl) and translates each record.
// app.cpp only feeds it on the CHUDWARE_SHOT=reveng_recon canned path. Events arrive async and
// are applied on the GL thread only (no mutex — GL-thread-only singleton).
struct RevEngEvent {
    std::string kind;     // "reconstruct_progress" | "reconstruct_done" | "reconstruct_error"
    int         pct  = 0; // 0..100 for progress events
    std::string msg;      // human-readable detail / error
};

// RE-21: Reconstruct stage live state — driven by RevEngEvent drain.
struct ReconstructState {
    bool     active   = false;  // true while a reconstruction job is running
    int      pct      = 0;      // 0..100
    std::string phase;          // e.g. "sparse", "dense", "mesh"
    std::string err;            // sticky error; empty when healthy
    bool     done     = false;

    // GUI kick bookkeeping (reveng_reconstruct launched from the stage panel or ribbon).
    int         quality    = 1;   // 0=draft, 1=normal, 2=high
    bool        kickPending = false;
    std::string kickErr;          // sticky; empty when healthy
    std::string runId;            // run_id returned by the kick (for status display)
};

// A run's control lifetime is independent of the selected session and camera device.
// RPC callbacks retain this ticket through start, image ingest and a single terminal action.
struct GaussianRunControl {
    enum class Action { None, Finish, Cancel };
    std::string sid;
    bool starting = false, pending = false, startFailed = false;
    int pendingIngest = 0;
    Action requested = Action::None;
    bool finishSent = false, cancelSent = false, acknowledged = false;
};

void finishGaussianRun(AppState& st, const std::shared_ptr<GaussianRunControl>& run);
void drainGaussianActions(AppState& st, const std::shared_ptr<GaussianRunControl>& run);

// Calibrated, undistorted capture pose in the published Gaussian artifact's SfM gauge.
// An empty id means a legacy artifact without capture calibration.
struct GaussianViewCamera {
    std::string id;
    Vec3 eye, forward, up;
    float focusDistance = 1.0f;
    float intrinsics[6] = {};  // fx, fy, cx, cy, source width, source height
};

// Local Gaussian jobs are independent of the scaled CAD/mesh reconstruction mission.
struct GaussianState {
    int mode = 0, quality = 1, iterations = 2000;
    bool pending = false, live = false;
    std::shared_ptr<GaussianRunControl> control;
    std::string runId, status, stage, msg, error, splatPath;
    int pct = 0, frames = 0, registered = 0, iteration = 0, totalIterations = 0;
    uint64_t artifactRevision = 0;
    GaussianViewCamera viewCamera;
    bool cropEnabled = false;
    float cropBounds[6] = {};
    float bounds[6] = {};
    bool hasBounds = false;
};

// Scale stage (contracts/reveng.md §2 session.scale + reveng_set_scale §8.2).
struct ScaleState {
    int    method = 0;          // 0=aruco_mat, 1=two_point, 2=known_dim
    // aruco_mat
    int    board = 0;           // 0=A4, 1=Letter
    // two_point
    int    imageIdx = 0;        // index into RevEngState::galleryImages
    float  p1[2] = {0.0f, 0.0f};
    float  p2[2] = {100.0f, 0.0f};
    double distMm = 50.0;
    // known_dim
    char   featureBuf[128] = {};
    double knownValueMm = 10.0;
    int    knownSource = 0;     // 0=caliper, 1=datasheet, 2=photo_fiducial, 3=guessed
    // async submit
    bool        pending = false;
    std::string err;            // sticky; empty when healthy
    std::string okMsg;
    // Mirror of session.scale from get_reveng_session (refreshed by the detail poll).
    bool        mirrored = false;
    std::string curMethod;
    double      mmPerUnit   = 0.0;
    double      residualPct = 0.0;
    bool        confirmed   = false;
};

// Model stage — one parsed row of aicad.reveng_fits/1 §3.
struct FitEntry {
    std::string kind;           // "cylinder" | "box" | ...
    double      confidence = 0.0;
    std::string source;         // "ransac" | ...
    std::string paramsSummary;  // preformatted "r=4.0 h=12.0" for the table
};

// Model stage (reveng_fit_primitives / reveng_build_parametric / describe_reveng_result).
struct ModelState {
    std::vector<FitEntry> fits;
    bool        fitPending = false;
    bool        fitsLoaded = false;
    std::string fitErr;

    int         strategy = 0;   // 0=auto, 1=mesh_fallback, 2=override_approximate
    bool        buildPending = false;
    std::string buildErr;
    std::string buildRunId;

    bool        descPending = false;
    bool        descLoaded  = false;
    std::string descErr;
    std::string descDocId;
    double      descMatchPct = 0.0;
    std::string descStrategy;
    bool        descMeshFallback = false;
};

// Deliver stage (reveng_attach_result / export_reveng_bundle / reveng_print_scale_mat).
struct DeliverState {
    char        partRefBuf[256] = {};
    bool        attachPending = false;
    std::string attachErr;
    std::string attachOk;

    char        exportDestBuf[512] = {};
    bool        exportPending = false;
    std::string exportErr;
    std::string exportOk;

    int         matSize = 0;    // 0=A4, 1=Letter
    bool        matPending = false;
    std::string matErr;
    std::string matPath;
};

// RE-40: mirror of reveng_status() — capability + provisioning probe (contracts/reveng.md §8.1).
// Fetched once per workspace entry (and on demand) so the UI can tell the user COLMAP is missing
// BEFORE they click Reconstruct, instead of surfacing backend_not_provisioned after the fact.
struct CapabilityState {
    bool loaded  = false;
    bool pending = false;
    double lastT = -100.0;
    std::string err;

    // capabilities{} — what the pipeline can actually do right now.
    bool sparse = false;
    bool dense = false;
    bool splat = false;
    bool singlePhoto = false;
    bool webcam = false;
    bool gpu = false;

    // provisioning{} — per-backend availability, name -> present.
    struct Artifact {
        std::string key;      // "colmap" | "openmvs" | "brush" | "triposr" | "vggt" | "docling"
        bool present = false;
        std::string hint;     // fix-it text ("set OPENMVS_BIN to …")
    };
    std::vector<Artifact> artifacts;
    std::vector<std::string> banners;   // license / degradation notices
};

// RE-40: one entry of session.provenance[] (contracts/reveng.md §2). The backend appends a free
// dict per pipeline step (`{"step": "n_reconstruct", "sparse_backend": …, "ts": …}`), so only
// `step` and `ts` are structural — everything else is flattened into `detail` for display.
// Rendered as the properties column's Provenance timeline: this is the audit trail that makes a
// photo-derived model defensible ("where did this number come from?").
struct ProvenanceRecord {
    std::string step;    // "set_scale" | "n_reconstruct" | "n_fit" | "attach_result" | …
    std::string ts;      // ISO-8601 when the record carries one; empty otherwise
    std::string detail;  // remaining fields flattened to "k=v · k=v"
};

// File-static store for the whole workspace. Async-load bookkeeping mirrors the parts /
// ai-workspace idiom (throttle by ImGui::GetTime, pending flag, listedOnce, sticky error).
struct RevEngState {
    std::vector<SessionSummary> sessions;
    int selected = -1;              // index into sessions, or -1
    Stage stage = Stage::Source;    // active center stage for the selected session

    // RE-30: active source lane (drives adaptive stepper + Sources panel).
    SourceMode sourceMode = SourceMode::Scan;

    // New-session creation (rail affordance — RE UI polish).
    char        newNameBuf[128] = {};
    bool        newSessionPending = false;
    bool        focusNewName  = false;  // one-shot: rail focuses the name box (reveng_new)
    std::string newSessionErr;      // sticky; empty when healthy

    // Async list bookkeeping.
    bool listPending = false;
    bool listedOnce = false;
    double lastListT = -100.0;
    std::string listErr;            // sticky; empty when healthy

    // RE-19: per-session image gallery (resets when selected session changes).
    std::vector<GalleryImage> galleryImages;
    int gallerySession = -1;        // selected index when gallery was last reset

    // RE-30: reveng_research_source (Web lane) async state.
    char researchQueryBuf[512] = {};
    std::string researchQuery;
    std::string researchResult;
    std::string researchErr;
    bool researchPending = false;

    // RE-30: reveng_ingest_datasheet (Datasheet lane) async state.
    std::string datasheetPath;
    std::string datasheetErr;
    bool datasheetPending = false;

    // Phone capture session (start_capture_session / stop_capture_session).
    bool        phonePending = false;
    bool        phoneActive  = false;
    int         phonePort    = 8742;
    int         phoneCount   = 0;          // photos received (last known)
    std::string phoneUrl;                  // upload_url for the phone
    std::string phoneQr;                   // data:image/png;base64,… or raw URL fallback
    std::string phoneErr;
    std::string phoneStatus;               // short status line
    unsigned int phoneQrTex = 0;           // GL texture for QR PNG (0 = none)
    int         phoneQrW = 0;
    int         phoneQrH = 0;
    double      phonePollT = -100.0;       // last stop/poll attempt

    // RE-21: Reconstruct stage.
    ReconstructState recon;
    GaussianState gaussian;

    // Per-stage panel state (reset when the selected session changes).
    ScaleState   scale;
    ModelState   model;
    DeliverState deliver;

    // RE-21: GL overlay — point cloud + camera frusta. Owned here so it persists across frames.
    render::RePoints rePoints;

    // RE-28 (wired RE-27): 3DGS splat renderer. Prefer session artifacts.splat_path when
    // present; fall back to the canned fixture only for demo/shot modes.
    // draw() is a no-op until a cloud is loaded; safe to call every frame.
    render::SplatRenderer splat;
    bool splatLoaded = false;   // true once load() has been attempted for this session
    std::string splatPathTried; // path last load attempt used (avoid reloading same path)

    // Session detail mirror (from get_reveng_session) — artifact paths for Reconstruct.
    std::string detailSessionId;     // session_id the detail was fetched for
    std::string artifactSplatPath;   // absolute or session-relative splat .ply
    std::string artifactMeshPath;    // absolute or session-relative mesh
    std::string artifactSparseDir;
    bool        detailPending = false;
    double      lastDetailT = -100.0;
    std::string detailErr;
    // RE-40: session.honesty mirror (contracts/reveng.md §2). `honestyPipeline` is the pipeline
    // label ("scan_mesh_ai_parametric" | "single_photo" | …); `honestyTier` is
    // honesty.single_photo_tier ("high"|"medium"|"low"|"approximate", empty when not single-photo).
    // Feeds the properties column's HonestyTag so photo-derived geometry is never shown as exact.
    std::string honestyPipeline;
    std::string honestyTier;

    // RE-21: Canned fixture loaded flag (true once buildCannedSparseFixture has been applied).
    bool cannedFixtureLoaded = false;

    // ---- RE-40 overhaul: shell + preview state -------------------------------------------------
    // Capability probe (reveng_status). Drives the properties-column capability panel, the
    // Reconstruct stage's pre-flight gate, and the Sources stage's webcam availability copy.
    CapabilityState caps;

    // Session rail filter (substring match over name + session_id).
    char railFilterBuf[128] = {};

    // Contact-sheet selection — the gallery drives the Scale stage's two-point picker and the
    // hero preview, so the selected index is shared workspace state rather than panel-local.
    int selectedImage = -1;

    // Right column view: 0 = Session details, 1 = Capabilities, 2 = Provenance log.
    int propsTab = 0;

    // User intent for the two side columns (rail / properties). The stage body always stays
    // visible; collapsing rolls a column to a header strip + chevron (same affordance as the pinned columns).
    // Auto follows the layout, which folds Properties then Sessions when the body is too narrow
    // for three columns at their minimum widths. That fold is a per-frame LAYOUT decision in the
    // shell and never written here, so an explicit Expanded survives any window/scale change.
    ColumnIntent railIntent  = ColumnIntent::Auto;
    ColumnIntent propsIntent = ColumnIntent::Auto;

    // RE-40: per-stage badge mirror of session.stages (contracts/reveng.md §2), indexed by
    // (int)Stage. Stage::Source maps to the manifest's "capture" key; the other five match their
    // enum name lowercased. Populated by the get_reveng_session detail poll and cleared on
    // session switch. Drives the TOP stage stepper's per-step colour + reachability, so the
    // wizard's progress indicator reflects real backend state instead of just where you clicked.
    StageState stages[kStageCount];

    // RE-40: session.provenance[] mirror — the Provenance tab's timeline. Same lifetime as the
    // stage mirror (filled by the detail poll, cleared on session switch).
    std::vector<ProvenanceRecord> provenance;

    // RE-40: one-shot ribbon deep-link (see SourceFocus above). Set by handleRevEngCommand for
    // reveng_add_photos / reveng_add_video / reveng_webcam; CONSUMED AND CLEARED by
    // sources_panel.cpp, which expands + focuses the matching sub-widget.
    SourceFocus sourceFocus = SourceFocus::None;
};

RevEngState& S();

// Flow layout for chip rows. statusChipW / valueChipW return the width kit::StatusChip /
// kit::ValueChip will occupy (HonestyTag is never wider than statusChipW of its tier), measured
// BEFORE placement. flowSameLine() keeps the next item on the current line when `w` still fits
// before screen-x `rightX` after `gap`; otherwise the item starts a new line at the indent.
float statusChipW(const char* label);
float valueChipW(const char* label, const char* value);
void  flowSameLine(float rightX, float w, float gap);

Renderer::CameraXform gaussianPreviewCamera(int width, int height);

// RE-21/RE-40: Apply one aicad.reveng_events/1 event to the singleton state.
// Called from core::engineEventsTick()'s EventJournal tail (and from app.cpp's canned
// CHUDWARE_SHOT=reveng_recon path) — GL thread only, no mutex.
void feedRevEngEvent(const RevEngEvent& ev);

// RE-27: Seed the splat renderer with the canned fixture (for shot modes that need it without
// waiting for a real reconstruction job). CPU-only; GL resources are lazily built on first draw.
void seedSplatFixture();

// Ribbon command routing (app.cpp dispatchCmd → reveng_* ids). Switches to the ReverseEng
// workspace, lands on the relevant stage and kicks the matching async action. Returns true
// when the command was consumed (always, for known reveng_* ids; false for unknown ones).
bool handleRevEngCommand(AppState& st, const char* cmd);

// Kick an async reveng_reconstruct for the selected session (shared by the ribbon command
// and the Reconstruct stage's Run button). No-op (with a toast) when nothing is selected.
void kickReconstruct(AppState& st);

// Kick an async reveng_fit_primitives / reveng_build_parametric for the selected session
// (shared by the ribbon commands and the Model stage buttons). strategyIdx mirrors
// ModelState::strategy (0=auto, 1=mesh_fallback, 2=override_approximate).
void kickFitPrimitives(AppState& st);
void kickBuildParametric(AppState& st, int strategyIdx);

// Kick reveng_print_scale_mat (ribbon + Scale/Deliver panels). sizeIdx: 0=A4, 1=Letter.
// On success writes the path into DeliverState::matPath and toasts it.
void kickPrintScaleMat(AppState& st);

}  // namespace reveng
}  // namespace ui
