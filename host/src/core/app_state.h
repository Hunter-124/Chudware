// AI-CAD shared application state + data model (extracted from gui_app.cpp).
//
// Holds the structs the worker/agent loop produces (Settings, JobState, ChatMsg, AgentStep,
// AgentEvent, PlanInfo, PartInfo, FitReport, ...) and the AppState aggregate that bundles every
// run_gui() local so the panel modules can share it. Behavior-preserving split — names and
// semantics are unchanged from the monolith.
#pragma once

#include <array>
#include <atomic>
#include <cstdint>
#include <functional>
#include <mutex>
#include <string>
#include <unordered_map>
#include <vector>

#include <nlohmann/json.hpp>

#include "imgui.h"

#include "../ui_theme.h"
#include "../win_chrome.h"
#include "../worker_client.h"

#include "selection.h"   // core::Selection (pulls render::PickHit; all GL-free headers)
#include "doc_mirror.h"  // core::DocMirror (C16)
#include "async_rpc.h"   // core::AsyncRpc (C39) — non-blocking worker calls

#include "../ui/toast.h"    // W1-B: ToastLevel / Toast (toast queue lives on AppState)
#include "../ui/cursors.h"  // W1-B: CursorKind (per-frame cursor request)

struct GLFWwindow;
class Renderer;
namespace render { class IdPass; class Overlays; }

// W1-B: typed subsystem forward declarations — AppState borrows these as proper typed pointers
// (replacing the old opaque void* + static_cast<...> sites scattered across host/src). The full
// class definitions stay in their own headers; AppState only needs the incomplete type for a
// pointer member, so the headers are NOT pulled in here (app_state.h stays GL-light).
namespace ui {
class PropertyManager;
class SketchMode;
class SimPanel;
class AsmPanels;
class ActivityPanel;
}  // namespace ui
namespace core { class SimClient; }

extern const char* kStarterCode;

enum JobKind { JOB_BUILD, JOB_DESIGN, JOB_CHAT };

// Document display/input unit system. The engine is canonical mm/deg; this is a HOST-only
// concern that controls how lengths are FORMATTED for display and how a BARE number typed into
// a dim field is interpreted. Angles stay in degrees in both systems (SolidWorks behavior).
//   MMGS = millimeter / gram / second   IPS = inch / pound / second
enum class Units { MMGS = 0, IPS = 1 };

// ---------------- settings (AI provider) -> %APPDATA%/AI-CAD/config.json ----------------
struct Settings {
    int provider = 2;  // 0 = Anthropic, 1 = OpenAI, 2 = Local (OpenAI-compatible).
                       // Default to a local LM Studio so the app is testable out of the box.
    char model[128] = "gemma-4-e4b";
    char apiKey[256] = "";
    char baseUrl[256] = "http://localhost:1234/v1";
    bool vision = true;
    bool offlineMode = false;    // C35: local-only mode — refuse any non-loopback provider (ITAR)
    bool planMode = true;        // draft a build plan (parts/dimensions/features) first
    int maxIters = 0;            // refine-pass cap per turn; 0 = automatic
    std::string systemPrompt;    // system prompt override; empty = built-in
    std::string theme = "dark";  // "dark" | "light" — persisted to config.json
    Units units = Units::MMGS;   // document display/input unit system — persisted to config.json
};

std::string appdataDir();
std::string configPath();
void loadSettings(Settings& s);
void saveSettings(const Settings& s);

// ---------------- textures ----------------
struct Tex {
    unsigned int id = 0;
    int w = 0, h = 0;
};
Tex loadTexture(const std::string& path);
void setWindowIcon(GLFWwindow* win, const std::string& path);

// ---------------- multi-part / assembly model ----------------
// Distinct per-part colors — MUST match engine/render._PART_COLORS so the swatch in the GUI
// matches the body's color in the rendered contact sheet.
inline ImVec4 partColor(int i) {
    static const ImVec4 kColors[] = {
        {0.298f, 0.447f, 0.690f, 1}, {0.867f, 0.518f, 0.322f, 1}, {0.333f, 0.659f, 0.408f, 1},
        {0.769f, 0.306f, 0.322f, 1}, {0.506f, 0.447f, 0.702f, 1}, {0.576f, 0.471f, 0.376f, 1},
        {0.855f, 0.545f, 0.765f, 1}, {0.498f, 0.498f, 0.498f, 1}, {0.800f, 0.725f, 0.455f, 1},
        {0.392f, 0.710f, 0.804f, 1},
    };
    return kColors[((i % 10) + 10) % 10];
}

struct PartInfo {
    std::string name, stlPath, dims;
    double volume = 0;
};
struct FitPair {
    std::string a, b;
    double value = 0;  // overlap mm^3 (interference) or gap mm (clearance)
};
struct FitReport {
    bool present = false;
    bool ok = true;  // true = no interference
    std::string summary;
    std::vector<FitPair> interferences, clearances;
};

// ---------------- worker job ----------------
struct AgentEvent;  // defined later (richer chat model)

struct JobState {
    std::atomic<bool> running{false};
    std::atomic<bool> ready{false};
    std::mutex mtx;
    int kind = JOB_BUILD;
    std::string stl, info, error;
    bool errorIsCad = false;          // error came from CAD code that didn't build (expected,
                                      // the loop self-corrects) — NOT an app/runtime failure
    bool isAssembly = false;          // final result was a multi-part assembly
    std::vector<PartInfo> parts;      // per-part name/stl/dims (assemblies)
    FitReport fit;                    // final fit report (assemblies)
    bool converged = true;            // design loop ended by matching the request (not the cap)
    std::string stopReason;           // confirmed | stabilized | failed | no_code | max_iters
    std::mutex evMtx;
    std::vector<AgentEvent> events;  // streamed agent progress, drained on the main thread
};

// safe JSON accessors: tolerate missing keys AND explicit null (Python None) values.
inline std::string jstr(const nlohmann::json& o, const char* k) {
    auto it = o.find(k);
    return (it != o.end() && it->is_string()) ? it->get<std::string>() : std::string();
}
inline bool jbool(const nlohmann::json& o, const char* k) {
    auto it = o.find(k);
    return it != o.end() && it->is_boolean() && it->get<bool>();
}
inline int jint(const nlohmann::json& o, const char* k) {
    auto it = o.find(k);
    return (it != o.end() && it->is_number_integer()) ? it->get<int>() : 0;
}
inline double jnum(const nlohmann::json& o, const char* k) {
    auto it = o.find(k);
    return (it != o.end() && it->is_number()) ? it->get<double>() : 0.0;
}

// The human-readable cause of a Python traceback: its last "SomethingError: ..." line
// (skipping the frames and any appended kernel HINT). Falls back to the first line.
std::string errSummary(const std::string& e);

// Parse the kernel's per-part list ([{name, stl_path, volume, bbox:{size:[x,y,z]}}, ...]).
std::vector<PartInfo> parseParts(const nlohmann::json& arr);

// Parse the kernel's fit report ({ok, summary, interferences:[{a,b,volume}], clearances:[{a,b,distance}]}).
FitReport parseFit(const nlohmann::json& f);

// ---------------- build plan (plan mode) ----------------
// The agent's pre-code plan: each part with concrete dimensions and a feature list.
// The Python side normalizes dimensions to a display string and features to strings.
struct PlanPart {
    std::string name, purpose, dims;
    std::vector<std::string> features;
};
struct PlanInfo {
    bool present = false;
    std::string name, summary, fit;
    std::vector<PlanPart> parts;
};

PlanInfo parsePlan(const nlohmann::json& p);

// ---------------- small ui helpers ----------------
// A caption button (minimize / maximize-restore / close) drawn with a crisp vector glyph
// and a full-height hover fill — close hovers red, the others a soft white wash.
enum WinBtn { BTN_MIN, BTN_MAX, BTN_CLOSE };
bool windowButton(const char* id, WinBtn kind, float h, bool maximized);

bool primaryButton(const char* label, const ImVec2& size);
void sectionHeader(ImFont* f, const char* text);
void dimText(const char* t);

void buildDefaultLayout(ImGuiID root, const ImVec2& size);

// One pass of the agent loop, shown as an expandable card in the conversation.
struct AgentStep {
    int index = 0;
    bool ok = false, done = false;
    std::string notes, thinking, code, diff, dims, error, contactSheet, stdoutText;
    bool isAssembly = false;       // this pass built a multi-part assembly
    std::vector<PartInfo> parts;   // per-part list (assemblies)
    FitReport fit;                 // interference/clearance report (assemblies)
    Tex thumb;                 // lazily loaded from contactSheet on the GL thread
    bool triedThumb = false;
    bool showThinking = false, showCode = false, showDiff = false, showError = false;
};

enum ErrKind { ERR_NONE = 0, ERR_CAD = 1, ERR_SYSTEM = 2 };

struct ChatMsg {
    bool fromUser = false;
    std::string text;              // user prompt, or the assistant's final summary
    bool running = false;          // assistant turn still streaming
    int errKind = ERR_NONE;        // ERR_CAD = generated code didn't build (calm, amber);
                                   // ERR_SYSTEM = app/runtime/provider failure (red)
    std::string liveStatus;        // transient "Iteration N: building..." while running
    PlanInfo plan;                 // the build plan drafted before the code (plan mode)
    std::vector<AgentStep> steps;  // assistant turn: the iteration trace
};

// A progress event streamed from the worker (parsed off the worker thread, applied on the
// main/GL thread so texture uploads stay on the GL context). kind: "status" | "iteration".
struct AgentEvent {
    std::string kind;
    int index = 0;
    bool ok = false, done = false;
    std::string phase, text, notes, thinking, code, diff, dims, error, contactSheet, stdoutText;
    std::string stlPath;  // this iteration's built mesh — drives the live viewport preview
    bool isAssembly = false;
    std::vector<PartInfo> parts;
    FitReport fit;
    PlanInfo plan;        // kind == "plan": the drafted build plan
};

void freeChatTextures(std::vector<ChatMsg>& chat);

// A small pill button that highlights when its section is expanded.
bool toggleChip(const char* label, bool active);

// Read-only, copyable monospace code view; height fits the content up to maxH.
void drawCodeBox(const char* id, const std::string& code, ImFont* mono, float maxH);

// Colored unified-diff view (+ green / - red / @@ blue) in a scrolling box.
void drawDiffBox(const char* id, const std::string& diff, ImFont* mono, float maxH);

// Dim, wrapped prose (reasoning / notes) in a scrolling box.
void drawProseBox(const char* id, const std::string& text, float maxH);

// Fit/interference readout: a green ("fits") or red ("clash") status line; when detailed,
// the per-pair overlap volumes (red) and clearance gaps (dim) below it.
void drawFitReadout(const FitReport& fit, bool detailed);

// The build-plan card: an accent-edged panel listing each planned part.
void drawPlanCard(const PlanInfo& plan, const ui::Fonts& fonts);

// One line "Parts: a, b, c" with each name tinted its assembly color.
void drawPartsLine(const std::vector<PartInfo>& parts);

// ---------------- app-state aggregate ----------------
// Every long-lived run_gui() local, bundled so the title-bar / panels / modals (each in its own
// translation unit) can share one mutable state object. Field declaration order and defaults match
// the monolith's local declarations exactly. The four run_gui lambdas (addLog / startSetup /
// tryStartWorker / showPartMesh) become methods carrying the same captured state.
struct AppState {
    // ---- window / render context (initialized in run_gui before the loop) ----
    GLFWwindow* win = nullptr;
    bool shot = false;
    float scale = 1.0f;
    std::string assets;
    ui::Fonts fonts;
    Tex wordmark, logoIcon;
    Tex wordmarkDark;   // dark-text "-CAD" variant for the light theme (white text washes out)
    Renderer* renderer = nullptr;     // owned by run_gui; AppState only borrows it
    chrome::HitLayout* hitLayout = nullptr;

    // ---- document mirror (C16) — worker-backed document state ----
    core::DocMirror docMirror;
    bool wantTree = false;   // AI_CAD_SHOT=tree: scripted doc-smoke flow + screenshot
    bool treeShotDone = false; // guard so doc smoke runs exactly once per shot

    // ---- document tabs (SolidWorks/browser-style multi-document) ----
    // The worker already keeps every open document alive in its registry (keyed by doc_id); the
    // host stays SINGLE-document at the DocMirror level (one "active" doc view: tree, mesh,
    // selection, caches). One DocTab per open doc records the minimum to re-point DocMirror on a
    // switch + render the strip. Switching activates the doc in the worker (doc_rebuild → tree +
    // mesh) and refreshes the viewport/tree/selection; per-tab camera is restored so each doc keeps
    // its view. docTabs is empty only at the empty/start state (no document open).
    // A tab is one of three kinds. Part/Assembly tabs are backed by a DocMirror part/asm session
    // (docId is a doc_* worker id, the central area shows the 3D viewport). A Drawing tab is NOT a
    // DocMirror doc — it is backed by a SEPARATE drawing registry id (drawingDocId) and the central
    // area shows a 2D sheet instead of the 3D viewport. switchToTab/ensureActiveTab keep their hands
    // off the mirror for Drawing tabs so the underlying part stays loaded behind the drawing.
    struct DocTab {
        enum class TabKind { Part, Assembly, Drawing };
        TabKind     kind = TabKind::Part;
        std::string docId;          // worker doc id (registry key) — empty for Drawing tabs
        std::string title;          // display name (doc "name", "Part1"/"Assembly1")
        bool        isAssembly = false;
        bool        dirty = false;  // edits since last save (asterisk on the tab)
        std::string path;           // last saved file path ("" = never saved)
        // Saved camera so each tab keeps its own view across switches.
        float yaw = 35, pitch = 28, zoom = 1.0f, pan[3] = {0, 0, 0};
        bool  camValid = false;     // false until the first switch-away captures the live camera

        // ---- Drawing tabs only (kind == Drawing) ----
        std::string drawingDocId;       // drawing registry id (from drawing_new) — NOT a doc_* id
        std::string sourceDocId;        // the part/asm doc this drawing projects from (for Rebuild)
        nlohmann::json drawingDoc;      // cached drawing_get result {sheets:[{...views...}]}
        // 2D sheet pan/zoom (persisted per tab). pxPerMm scales sheet-mm -> screen px; panPx is the
        // screen-space offset of the sheet origin. needFit=true requests a one-shot fit-to-window.
        float drawZoom = 0.0f;          // 0 = uninitialized -> triggers a fit on first show
        float drawPanX = 0.0f, drawPanY = 0.0f;
        bool  drawNeedFit = true;

        // ---- W13: dimension tool + BOM (drawing tabs) ----
        // The Dimension tool is a modal placement workflow owned by the 2D panel: arm it, then click
        // near a view's projected polyline VERTEX to snap point a, then b, then a 3rd click sets the
        // text position -> drawing_add_dim. Esc cancels. dimType is "linear_dim"|"aligned_dim".
        bool        dimToolActive = false;     // armed: clicks place a dimension
        std::string dimType = "linear_dim";    // which dimension the tool places
        int         dimStage = 0;              // 0=pick a, 1=pick b, 2=pick text pos
        std::string dimViewId;                 // view the in-progress dim is anchored to
        float       dimAx = 0, dimAy = 0;      // picked point a (sheet mm)
        float       dimBx = 0, dimBy = 0;      // picked point b (sheet mm)
        std::string selAnnViewId;              // selected stored annotation (for Delete)
        std::string selAnnId;
        bool        bomVisible = false;        // show the BOM overlay table (assembly drawings)
        nlohmann::json bomRows;                // cached drawing_bom result {rows, is_assembly, ...}
        bool        bomFetched = false;        // have we fetched the BOM for this drawing yet
    };
    std::vector<DocTab> docTabs;
    int activeTab = -1;             // index into docTabs; -1 = no document open (start state)
    // While true, the DocMirror listener does NOT mark the active tab dirty. Set around a tab
    // switch / resync / fresh-doc activation (those fire the mirror's listener via doc_rebuild but
    // are not user edits, so they must not light the unsaved-changes marker).
    bool suppressTabDirty = false;

    // ---- document-tab operations (implemented in core/doc_tabs.cpp via ui/doc_tabs) ----
    // Append a tab for an already-created/opened worker doc and make it active (does NOT touch the
    // worker — the caller already did doc_new/doc_open and the mirror points at it). Captures the
    // current doc id from docMirror.
    void addDocTab(bool isAssembly);
    // Switch the active document to docTabs[idx]: safely exits any in-progress sketch/PM form,
    // saves the leaving tab's camera, re-points DocMirror at idx's doc (doc_rebuild → tree+mesh),
    // resets per-doc caches/selection/tree transients, reloads the mesh, restores idx's camera.
    void switchToTab(int idx);
    // Close docTabs[idx] (with the worker doc_close). If it was active, activates a neighbor; if it
    // was the last tab, returns to the empty start state. Does NOT prompt — the caller decides.
    void closeDocTab(int idx);
    // Mark the active tab dirty (on any mutation) / clear it (on save). Safe with no active tab.
    void markActiveTabDirty();
    void clearActiveTabDirty();
    // Refresh the active tab's cached title from the current doc tree (after a rename, etc.).
    void syncActiveTabTitle();
    // Index of the tab whose docId matches the mirror's current doc, or -1.
    int  tabIndexForActiveDoc() const;
    // Reconcile the tab strip with the mirror once per frame: if the mirror holds a document that
    // has no tab yet (created by a path that doesn't open one itself — sketch auto-create, chat
    // "new part", an agent v2 run, the assembly panel), append a tab for it and make it active.
    // Also keeps the active title/kind fresh. Called from the per-frame draw before the strip.
    void ensureActiveTab();

    // ---- DRAWINGS module (host UI for the drawing engine RPCs) ----
    // "Make Drawing": from the active PART/ASM tab, create a blank drawing (drawing_new), add the
    // standard four views (front/top/right/iso) at a default layout, then open + activate a new
    // Drawing tab caching the drawing_get result. No-op + toast when no part is open. All worker
    // calls are guarded; a failure logs + toasts and leaves no half-open tab.
    void makeDrawing();
    // Re-fetch a Drawing tab's drawing_get (the engine reprojects from the live source part, so this
    // is how edits to the source reflect). Called on tab activation and by the manual Rebuild button.
    void refreshDrawingTab(int idx);
    // True when the active tab is a Drawing (the central area shows the 2D sheet, not the 3D view).
    bool activeTabIsDrawing() const {
        return activeTab >= 0 && activeTab < (int)docTabs.size() &&
               docTabs[activeTab].kind == DocTab::TabKind::Drawing;
    }

    // ---- topology picking (C15) — id pass + overlays owned by run_gui; AppState borrows ----
    render::IdPass* idPass = nullptr;
    render::Overlays* overlays = nullptr;
    bool hasDocMesh = false;            // an AICM doc is loaded into idPass/overlays
    core::Selection selection;          // current picked topology set
    render::PickHit hover;              // last hovered topology (transient)
    bool pickReady = false;             // id buffer rendered this frame (safe to pickFinal)
    // Status-bar hook: set to a human label like "Face 12 of Body1" on hover/selection change.
    std::string pickStatus;

    // ---- W1-B: feature-tree selection state (moved out of feature_tree.cpp's file statics) ----
    // The currently-selected feature id in the FeatureManager; the tree renders its row highlighted
    // and (Wave-2) scrolls to it. A viewport pick that resolves to a feature owner sets these so the
    // tree row highlights/scrolls to follow the 3D selection (item 7). Cleared on every doc change.
    std::string selectedFeatureId;
    bool treeScrollToSelection = false;  // one-shot: tree should scroll the selected row into view

    // C26-C: per-face normal + centroid cache, filled during loadDocMesh while the DocMesh is in
    // memory. Key = (body_id << 32) | face_id (matches PickHit body_id / topo_id exactly).
    // Value = {nx, ny, nz, cx, cy, cz} — area-weighted average face normal + face centroid (world).
    std::unordered_map<uint64_t, std::array<float, 6>> faceNormalCache;

    // C27-A: per-edge centroid cache, filled in the same loadDocMesh pass as the face cache.
    // Key = (body_id << 32) | edge_id (matches PickHit body_id / topo_id for PickKind::Edge).
    // Value = {cx, cy, cz} — mean of the edge polyline points (world). Used to anchor in-viewport
    // parameter callouts to a filleted/chamfered edge when the PM selector lacks a centroid.
    std::unordered_map<uint64_t, std::array<float, 3>> edgeCentroidCache;

    // ---- W1-B (item 8): axis-aligned bounding-box caches for Zoom-to-Selection (Wave-2). ----
    // Filled in loadDocMesh from the same DocMesh in memory. Plain float arrays keep memory sane.
    // bodyBBoxCache: key body_id -> {minX,minY,minZ, maxX,maxY,maxZ} (world).
    std::unordered_map<uint32_t, std::array<float, 6>> bodyBBoxCache;
    // faceBBoxCache: key (body_id<<32)|face_id -> {minX,minY,minZ, maxX,maxY,maxZ} (world), built
    // from each face's triangle range. Empty for faces with no triangles.
    std::unordered_map<uint64_t, std::array<float, 6>> faceBBoxCache;

    // ---- Solid Bodies visibility (FeatureManager "Solid Bodies" folder hide/show) ----
    // Parallel arrays indexed by the renderer body slot (the order loadDocMesh feeds setMeshes,
    // i.e. the AICM body order minus empty bodies). bodyVisible[i] != 0 -> shown. docBodyIds[i]
    // is that slot's AICM body_id (1-based ordinal) — it keys the id-pass pick skip and the
    // "BodyN" tree label. Reset semantics: loadDocMesh PRESERVES the hidden set across a rebuild
    // when the body-id list is unchanged (the common edit-a-feature case) and RESETS everything
    // to visible when it differs (a rebuild that adds/removes bodies realigns ordinals, so stale
    // by-ordinal hides would land on the wrong body). clearViewportForNewDoc clears both.
    std::vector<uint8_t>  bodyVisible;
    std::vector<uint32_t> docBodyIds;
    // Push bodyVisible to the renderer (per-slot draw skip) and the id pass (pick skip). Call
    // after any toggle; loadDocMesh calls it after every setMeshes/setDoc.
    void applyBodyVisibility();

    // ---- Wave-2 shared UI state (cross-panel contracts; each field has one writer + one reader) ----
    // True while SketchMode runs the SolidWorks-style "pick a plane to start the sketch" step.
    // Writer: sketch_mode. Readers: viewport_panel (force-show origin planes, suppress its own
    // click-pick/selection handling, keep calling drawOverlay even though no sketch is active yet).
    bool sketchPlanePickActive = false;

    // Faces to tint with the selection highlight because their owning feature is selected in the
    // FeatureManager tree. Keys = (body_id<<32)|face_id. Writer: feature_tree (on row click, from
    // DocMirror owner map). Reader: viewport_panel/overlays render pass. Cleared on doc change.
    std::vector<uint64_t> featureHighlightFaces;

    // One-shot camera request: animate to frame this world-space AABB ("Zoom to Selection" /
    // tree "Zoom to Feature"). Writers: feature_tree, viewport context menu. Reader: viewport_panel
    // (consumes the flag, computes zoom/pan from the box, calls requestCameraAnimation).
    bool  zoomToBBoxRequested = false;
    float zoomToBBoxMin[3] = {0, 0, 0};
    float zoomToBBoxMax[3] = {0, 0, 0};

    // ---- settings + system-prompt editor buffer ----
    Settings settings;
    std::vector<char> spBuf{std::vector<char>(16384, 0)};

    // ---- console log ----
    std::vector<std::string> log;

    // ---- runtime + worker ----
    WorkerClient worker;
    // C39: async dispatch over the same worker. Declared AFTER worker so it is destroyed
    // FIRST (joins its thread before the client goes away). run_gui also shuts it down
    // explicitly before worker.stop() so teardown never waits out an RPC timeout.
    core::AsyncRpc rpc{worker};
    std::string werr;
    bool workerStarted = false;
    bool rtReady = false;
    std::atomic<bool> setupRunning{false}, setupDone{false}, setupOk{false};
    std::mutex setupMtx;
    std::string setupMsg;
    std::string status;
    int statusKind = 0;  // 0 = ok (teal), 1 = build issue (amber), 2 = system error (red)
    bool autoSetupTried = false;    // guard so startup auto-heal runs once, not in a loop
    bool importRepairTried = false; // guard so per-job ModuleNotFoundError repair runs once

    // ---- job + chat model ----
    JobState job;
    std::vector<char> code{std::vector<char>(64 * 1024, 0)};
    std::vector<char> prompt{std::vector<char>(2048, 0)};
    std::vector<ChatMsg> chat;

    // ---- current assembly (populated when a build/design returns multiple parts) ----
    std::vector<PartInfo> asmParts;
    FitReport asmFit;
    std::string asmStl;               // combined assembly STL (shown when selectedPart == -1)
    int selectedPart = -1;            // -1 = whole assembly; >=0 = index into asmParts

    std::vector<char> chatInput{std::vector<char>(2048, 0)};
    std::string modelInfo;
    std::string livePhase;        // what the agent is doing right now (viewport overlay)
    std::string buildErrSummary;  // last "Build code" failure, shown under the editor
    std::string buildErrFull;     // its full traceback, behind a Details toggle
    bool showBuildErr = false;
    float yaw = 35, pitch = 28, zoom = 1.0f;
    // W2-D: world-space orbit-target offset (pan). 0 = look at the model bbox center.
    float pan[3] = {0, 0, 0};
    // W2-D: camera animation state (driven by ui::requestCameraAnimation/tickCameraAnimation in
    // ui/camera_anim.*). animActive gates the tween; the from/to snapshots + clock live here so the
    // animation module stays stateless across frames and rides AppState's lifetime.
    bool  animActive = false;
    float animT = 0.0f;          // elapsed seconds into the current tween
    float animDur = 0.35f;       // smoothstep ease duration
    float animYaw0 = 0, animPitch0 = 0, animZoom0 = 1.0f, animPan0[3] = {0, 0, 0};
    float animYaw1 = 0, animPitch1 = 0, animZoom1 = 1.0f, animPan1[3] = {0, 0, 0};
    // Reference planes (Front / Top / Right) shown faintly in the viewport; click a plane row in
    // the FeatureManager to toggle it. planeEmphasis (0..2, -1=none) brightens one, dims the others.
    // W4-K: hidden by default (SolidWorks behavior). Toggle a plane on via the FeatureManager row
    // or it is forced visible during sketch-plane picking. Sketch mode hides them (grid owns view).
    bool showPlane[3] = {false, false, false};
    int  planeEmphasis = -1;
    // C25 W3-J: viewport display/visibility state.
    int  displayStyle  = 1;          // mirrors DisplayStyle enum; 0=Shaded 1=ShadedEdges(default) 2=HLR 3=HLV 4=Wire
    bool showTriad        = true;    // orientation triad, bottom-left of viewport
    bool showGroundShadow = true;    // soft contact-shadow blob
    bool showRealView     = true;    // C30-C: procedural environment reflections (RealView sheen)
    // Viewport left-drag mode: 0=rotate (default), 1=pan.
    int  vpDragMode = 0;
    // One-shot flag: set by mouse_gestures right-click-without-drag hook; consumed by viewport_panel.
    bool wantContextMenu = false;
    bool layoutInit = false, resetLayout = false, openSettings = false;
    bool showDesign = true, showViewport = true, showConsole = true;
    // C24: the right task pane (Design + Agent Activity tabs, SolidWorks-style). View > Task Pane.
    bool showTaskPane = true;
    bool focusDesignTab = false;  // one-shot: focus the Design tab after a (re)layout
    // Consolidated AI panel (Design / Activity / Console as internal tabs). One dock window "AI".
    bool showAIPanel = true;
    // Configurations panel (SolidWorks ConfigurationManager). Dockable "Configurations" window in
    // the right task pane; View > Configurations toggles it. Only meaningful for part/asm docs.
    bool showConfigPanel = false;
    // Design Table panel (SolidWorks Design Table). Dockable "Design Table" window in the right
    // task pane; View > Design Table toggles it. An editable grid (rows=configs, columns=driven
    // params + per-feature suppression) over the same config_* RPCs the Configurations panel reads.
    bool showDesignTablePanel = false;
    // Weldment Cut List panel (SolidWorks Weldment Cut List). Dockable "Cut List" window in the
    // right task pane; View > Cut List toggles it. Only meaningful for part docs with weldments.
    bool showCutListPanel = false;
    int  aiInteraction = 0;       // 0=Design (new part) 1=Edit (orchestrator v2) 2=Chat
    bool focusActivityTab = false;  // one-shot: select the Activity tab (e.g. when a v2 run starts)
    bool showAbout = false, designStarted = false;
    int frame = 0;

    // ---- ribbon (C14) ----
    bool showRibbon = true;        // View > Ribbon visibility toggle
    int  ribbonTab  = 0;           // 0=Features 1=Sketch 2=Evaluate 3=Simulation 4=Assembly (C20b)
    bool showFeatureTree = true;   // View > FeatureManager (C16)

    // ---- PropertyManager (C17) — occupies the tree dock slot while a command is active ----
    // W1-B: typed pointer (forward-declared above) so app_state.h stays free of the heavy
    // ui/property_manager.h include while every borrow site drops its static_cast.
    ui::PropertyManager* propertyManager = nullptr;   // owned by run_gui

    // ---- C19: simulation + result-overlay UI state ----
    core::SimClient* simClient = nullptr;  // owned by run_gui
    ui::SimPanel*    simPanel   = nullptr;  // owned by run_gui
    bool showSimPanel = false;         // Simulation dock visibility
    bool colormapOn = false;           // result colormap toggle (mirrors renderer state)
    bool docMeshHasScalar = false;     // the loaded overlay carries a per-vertex scalar
    bool docMeshHasDisp = false;       // the loaded overlay carries a displacement field
    float dispScaleHint = 1.0f;        // suggested deform multiplier from the last overlay
    std::string legendTitle, legendUnits;  // legend widget labels for the active scalar
    bool legendOn = false;             // draw the viewport legend overlay
    float legendMin = 0.0f, legendMax = 1.0f;
    bool wantFea = false;       // C19: AI_CAD_SHOT=fea scripted static-study scenario
    bool feaShotDone = false;   // guard so the fea scenario runs exactly once
    bool wantEvalMass = false;  // C19: AI_CAD_SHOT=evalmass mass-properties window
    bool evalMassShotDone = false;

    // ---- SketchMode (C18) — in-sketch tools/overlay; typed like the PropertyManager (W1-B) ----
    ui::SketchMode* sketchMode = nullptr;  // owned by run_gui
    bool wantSketch = false;           // AI_CAD_SHOT=sketch scripted flow
    bool sketchShotDone = false;       // guard so the sketch scenario runs once per shot

    // ---- AsmPanels (C20b) — assembly windows/forms; typed like the PropertyManager (W1-B) ----
    ui::AsmPanels* asmPanels = nullptr;  // owned by run_gui
    bool wantAsm = false;              // AI_CAD_SHOT=asm scripted assembly flow
    bool asmShotDone = false;          // guard so the asm scenario runs once per shot

    // ---- C22b: Activity panel + prompt editor + orchestrator wiring ----
    bool showActivityPanel = false;         // View > Activity window (hidden until a run/View toggle opens it)
    bool agentV2 = true;                    // "Use orchestrator (v2)" checkbox in Design panel
    std::string pendingPromptRole;          // when non-empty, Settings > Agents tab opens on this role
    ui::ActivityPanel* activityPanel = nullptr;  // owned by run_gui (W1-B: typed)
    // Live status line for the v2 orchestrator run, shown in the chat area
    std::string agentV2Status;              // e.g. "PLAN > planner attempt 1..."
    bool agentV2Running = false;            // a v2 run is in flight (blocks concurrent runs)
    // --- end C22b ---

    // ---- C25: ribbon dispatch hook (set in app.cpp after the ribbon callback is wired) ----
    // Allows UI widgets (shortcut bar, gesture wheel) to fire ribbon commands without
    // knowing about the full dispatcher capture.
    std::function<void(const char*)> ribbonDispatch;

    // ---- screenshot-mode flags (set from AI_CAD_SHOT in run_gui) ----
    bool wantSettings = false, wantChat = false, wantAssembly = false, wantPlan = false;
    bool wantPick   = false;  // C15: load box.aicm, simulate a center pick, print + screenshot
    bool wantRibbon = false;  // C14: ribbon screenshot mode
    bool pickPrinted = false;  // guard so the "pick:" stdout line prints exactly once
    bool wantM1 = false;       // C17: AI_CAD_SHOT=m1 scripted integration scenario
    bool m1ShotDone = false;   // guard so the m1 scenario runs exactly once per shot
    bool wantActivity = false;  // C22b: AI_CAD_SHOT=activity scripted orchestrator run + activity panel
    bool activityShotDone = false; // guard so the activity scenario runs exactly once

    // ---- W1-B: toast notification queue (rendered by ui::drawToasts in app.cpp) ----
    // C39: guarded by toastsMtx — the worker errorSink posts toasts from background threads
    // (jobs.cpp agent threads, the AsyncRpc dispatch thread) while drawToasts reads on the
    // GL thread. toast() and drawToasts() both take the lock.
    std::mutex toastsMtx;
    std::vector<ui::Toast> toasts;

    // ---- W1-B: per-frame cursor request (consumed by ui::applyCursorRequest at end of frame) ----
    ui::CursorKind cursorRequest = ui::CursorKind::Default;
    bool imguiCursorSuspended = false;  // tracks whether we toggled NoMouseCursorChange last frame

    // ---- the run_gui lambdas, now methods ----
    void addLog(const std::string& s);
    void startSetup(bool force);
    void tryStartWorker();
    void showPartMesh(int sel);

    // W1-B: post a transient toast. Trivially callable from any TU that includes app_state.h.
    // Caps the queue so a storm of errors can't grow it unbounded (drawToasts also limits visible).
    void toast(ui::ToastLevel level, std::string msg);

    // W1-B: request a non-default cursor for THIS frame. The last request before end-of-frame wins.
    void requestCursor(ui::CursorKind k) { cursorRequest = k; }

    // Active document-unit system helpers (single source of truth = settings.units). Every dim
    // DISPLAY site routes ips() into pmFormatDim; every BARE-number dim INPUT site passes ips() as
    // the default-unit so "1.5" means 1.5 in in IPS and 1.5 mm in MMGS. Angles stay in degrees.
    bool ips() const { return settings.units == Units::IPS; }
    void toggleUnits() { settings.units = ips() ? Units::MMGS : Units::IPS; }

    // Load an AICM tessellation file into the id pass + overlays (and clear the selection).
    // Requires a current GL context and st.idPass/st.overlays to be set. Returns false on
    // parse/GL failure (err set); on success sets hasDocMesh. (C15; the worker that produces
    // these per-rebuild files is wired in C16.)
    bool loadDocMesh(const std::string& aicmPath, std::string& err);

    // C34: reset the viewport to the fresh-document state — drops the legacy/demo mesh and any
    // previous doc's parts so the empty-doc datum-planes view shows. Called by every path that
    // creates a new document (tree toolbar New Part/Assembly, sketch auto-create).
    void clearViewportForNewDoc();

    // Build the "Face 12 of Body1" status label for a pick (empty string for an invalid hit).
    std::string pickLabel(const render::PickHit& h) const;

    // W1-B (item 4): the single undo/redo entry point. Both the FeatureManager toolbar buttons and
    // the global Ctrl+Z / Ctrl+Y keys route through here, so behavior stays identical. Performs the
    // DocMirror mutation, refreshes the viewport mesh + owner map on success, logs, and posts a
    // brief confirmation toast ("Undo: ok" / "Undo: nothing to undo"). Returns the response so the
    // caller can branch further if it wants. No-op (returns a no_doc response) when no doc is open.
    core::OpsResponse docUndo();
    core::OpsResponse docRedo();
};
