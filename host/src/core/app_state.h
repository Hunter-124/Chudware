// Chudware shared application state + data model (extracted from gui_app.cpp).
//
// Holds the structs the worker/agent loop produces (Settings, JobState, ChatMsg, AgentEvent,
// PlanInfo, PartInfo, FitReport, ...) and the AppState aggregate that bundles every
// run_gui() local so the panel modules can share it. Behavior-preserving split — names and
// semantics are unchanged from the monolith.
#pragma once

#include <array>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <deque>
#include <functional>
#include <mutex>
#include <optional>
#include <set>
#include <string>
#include <thread>
#include <unordered_map>
#include <unordered_set>
#include <vector>

#include <nlohmann/json.hpp>

#include "imgui.h"

#include "../ui_theme.h"
#include "../win_chrome.h"
#include "../worker_client.h"

#include "selection.h"   // core::Selection (pulls render::PickHit; all GL-free headers)
#include "doc_mirror.h"  // core::DocMirror (C16)
#include "async_rpc.h"   // core::AsyncRpc (C39) — non-blocking worker calls
#include "switch_hold.h" // core::SwitchHold — async tab switch survives ensureActiveTab
#include "context_ref.h" // core::ContextRef — composer context-picker chips
#include "../ui/copilot/ask_answers.h"  // AskBatch — the agent's interactive question batches

#include "../ui/toast.h"    // W1-B: ToastLevel / Toast (toast queue lives on AppState)
#include "../ui/cursors.h"  // W1-B: CursorKind (per-frame cursor request)
#include "../ui/model_registry.h"  // ui::ModelInfo / ui::PROV_COUNT — ProviderModels::rows below

struct GLFWwindow;
class Renderer;
namespace render { class IdPass; class Overlays; struct DocMesh; }

// W1-B: typed subsystem forward declarations — AppState borrows these as proper typed pointers
// (replacing the old opaque void* + static_cast<...> sites scattered across host/src). The full
// class definitions stay in their own headers; AppState only needs the incomplete type for a
// pointer member, so the headers are NOT pulled in here (app_state.h stays GL-light).
namespace ui {
class PropertyManager;
class ConfigPanel;
class SketchMode;
class Sketch3DMode;   // 3D free-space sketcher (sketch3d features) — app-wide singleton
class SimManager;
class AsmPanels;
class EvaluateDialogs;  // Measure / Mass Properties / Section dialogs — owned per-document (DocSession)
// Wave 2 (GUI rework): the per-document ECAD editor state (mirror + canvas + selection + edit
// buffers) lives on each Schematic/PCB DocTab. Its full definition stays in ecad_panel.cpp (it
// owns EcadMirror/EcadCanvas); AppState only needs an opaque pointer so app_state.h stays light.
struct EcadEditorState;
class MfgMirror;
}  // namespace ui

#include <memory>  // std::shared_ptr<ui::EcadEditorState> on each ECAD DocTab
namespace core { class SimClient; }

extern const char* kStarterCode;

enum JobKind { JOB_BUILD, JOB_DESIGN, JOB_CHAT };

// Document display/input unit system. The engine is canonical mm/deg; this is a HOST-only
// concern that controls how lengths are FORMATTED for display and how a BARE number typed into
// a dim field is interpreted. Angles stay in degrees in both systems (SolidWorks behavior).
//   MMGS = millimeter / gram / second   IPS = inch / pound / second
enum class Units { MMGS = 0, IPS = 1 };

// ---------------- settings (AI provider) -> %APPDATA%/Chudware/config.json ----------------
// Copilot composer interaction mode (Agent | Plan | Ask) — a USER choice, never inferred from
// the request text. Every mode runs the same Chudcode general_agent turn; the mode only sets the
// user-chosen tool policy (Ask read-only, Plan no mutators). Integer values match
// Settings::copilotMode / AppState::aiInteraction / ChatMsg::copilotMode for persistence.
// Legacy Swarm(2)/Edit(3)/EngFirm(5) values are migrated to Agent on load; legacy Ask(4) to Ask.
enum class CopilotMode : int {
    Agent = 0,
    Plan  = 1,
    Ask   = 2,
};
constexpr int kCopilotModeCount = 3;
// Map a persisted mode integer from any build to the current enum (see the note above).
inline int normalizeCopilotMode(int v) {
    switch (v) {
        case 1: return (int)CopilotMode::Plan;
        case 2: return (int)CopilotMode::Ask;   // current Ask
        case 4: return (int)CopilotMode::Ask;   // legacy Ask
        default: return (int)CopilotMode::Agent;
    }
}

struct Settings {
    int provider = 9;  // 0=Anthropic 1=OpenAI 2=Local OpenAI-compat 3=managed llama.cpp 4=Gemini
                       // 5=Cloud OpenAI-compat 6=xAI 7=Cursor 8=Kimi Code 9=ZenMux (Chudcode)
                       // 10=Free AI (FreeLLMAPI, always cloud-classified).
                       // Default ZenMux x-ai/grok-4.5-free; online auth and execution are owned
                       // by Chudcode, while explicit offline mode retains only local providers.
    char model[512] = "x-ai/grok-4.5-free";  // frontier-free default; auto-pick still routes cheap
                                             // work down
    char apiKey[256] = "";        // active-provider key (local OpenAI-compat optional key)
    // Per-provider keys so the picker can hide providers with no key while keeping each key
    // around when another provider is active. Serialized under config.json "keys".
    char geminiKey[256] = "";
    char anthropicKey[256] = "";
    char openaiKey[256] = "";
    char cloudKey[256] = "";       // cloud OpenAI-compatible API key (DashScope / DeepSeek / etc.)
    char moonshotKey[256] = "";    // Moonshot (Kimi) API key — keys.moonshot / MOONSHOT_API_KEY
    char xaiKey[256] = "";         // xAI (Grok) API key — console.x.ai; empty when using xai-sub OAuth
    char freellmapiKey[256] = "";  // Free AI (FreeLLMAPI) unified key — keys.freellmapi
    char reasoningEffort[16] = "high";  // off|low|medium|high — only used by reasoning models
    char baseUrl[256] = "http://localhost:1234/v1";
    bool vision = true;
    bool offlineMode = false;    // C35: local-only mode — refuse any non-loopback provider (ITAR)
    // PreferenceMode 0=Off 1=Option 2=Encourage 3=Require. Serialized as
    // plan_preference / task_list_preference / subagent_preference / advisor_preference strings.
    // planMode stays as the binary gate (planPreference != 0) for legacy call sites
    // and plan_mode bool in config.json (Python still reads it).
    int planPreference = 0;      // default Off — direct tool use in main chat (no forced plan/intake)
    // Gates whether the agent publishes the live task list rendered by the copilot Task list panel.
    int taskListPreference = 2;  // default Encourage
    int subagentPreference = 1;  // default Option — model may spawn subagents
    int advisorPreference = 2;   // default Encourage — pull advisor + tone for guidance
    bool planMode = false;       // binary: planPreference != 0 (Supervised vs Autopilot)
    bool proactiveAdvisor = false; // opt-in mid-run proactive advisor (cheap gates; not every turn)
    // Opt-in company structure auto-route (agent.corporate_mode). Default off —
    // Agent never auto-invokes company; enable in Settings → AI → Behavior when ready.
    bool corporateMode = false;
    // Agent memory, kept per project folder by Chudcode: "sharpshooter" (curated decisions
    // injected every turn), "mnemo" (searchable recall of this project's conversations) or
    // "off". memoryEnabled mirrors backend != off for older Python readers.
    std::string memoryBackend = "sharpshooter";
    bool memoryEnabled = true;
    int memoryTokenBudget = 400; // legacy offline loop's .plm/memory.db recall budget
    // Copilot composer mode: Agent | Plan | Ask. Persisted as "copilot_mode" (name string).
    // Values match AppState::aiInteraction / CopilotMode.
    int copilotMode = 0;
    // Box.com project mirror. GUI-owned fields overlay config.json "box" / "box.auth";
    // OAuth access/refresh tokens remain Python-owned and are never modeled here.
    bool boxEnabled = false;
    std::string boxFolderId = "0";
    std::string boxAuthMethod = "developer_token";
    std::string boxDeveloperToken;
    std::string boxClientId;
    std::string boxClientSecret;
    int boxMaxFileMb = 512;
    int maxTokens = 0;           // max output tokens per model call; 0 = automatic (per-provider
                                 // default). Raise this if a reasoning model returns "no JSON ops"
                                 // / "budget exhausted" — it spent its output budget thinking.
    int contextWindow = 0;       // model's total token context window; 0 = automatic (per-provider
                                 // default: cloud large, local 8192). Input + output are fitted to
                                 // this so a small LOCAL model never overruns it and breaks the chain.
    std::string systemPrompt;    // system prompt override; empty = built-in
    std::string theme = "light";  // "dark" | "light" — persisted to config.json (default: light)
    Units units = Units::MMGS;   // document display/input unit system — persisted to config.json
    bool orthographic = true;    // viewport projection: true=orthographic (CAD default), false=perspective
                                 // — persisted to config.json as "orthographic"
    // Display tessellation tier for every open document: "draft" | "standard" | "fine".
    // Persisted to config.json as "display_quality"; a change pushes
    // doc_set_display_quality to every open part/assembly doc (contracts/doc_ops.md §7).
    std::string displayQuality = "fine";
    // Sketcher drag snap-and-relate: while dragging a sketch entity/point, snap to the origin,
    // keypoints, midpoints, rims, and alignment guides; auto-add the previewed relation on drop.
    // Settings → Modeling. Serialized "sketch_drag_snap_relations".
    bool sketchDragSnapRelations = true;
    bool partsGridView = false;  // Parts Library results as grid cards vs list — "parts_grid_view"
    // Parts Library GUI V2: expanded taxonomy categories + result sort mode
    // (0=Relevance 1=Price low→high 2=Price high→low 3=Name). Serialized as
    // "parts_expanded_categories" / "parts_sort_mode"; mirrored into the parts workspace
    // state on first frame like partsGridView, written back on change.
    std::vector<std::string> partsExpandedCats;
    int partsSortMode = 0;
    // v2.1 colored liquid glass: per-group tint/level/rounding/contrast knobs (+ dynamicColor,
    // ON by default) — persisted to config.json under "appearance.glass"; absent keys stay at
    // their GlassStyle sentinels (= inherit Global / built-in default). Mirrored into
    // ui::setGlassConfig() at startup and whenever a knob changes (safe to push every frame).
    ui::GlassConfig glassCfg;
    // File Explorer workspace root + recent files/projects, persisted to
    // config.json ("workspace_dir", "recent_files", "recent_projects").
    // Mirrored onto AppState at startup; AppState is the live UI copy.
    std::string workspaceDir;                  // "" = use the Documents/Chudware default
    std::vector<std::string> recentFiles;      // most-recent-first, capped at AppState::kRecentFilesMax
    std::vector<std::string> recentProjects;   // project folders, most-recent-first
    // Chat model-picker visibility: ids the user has explicitly hidden (VS-Code-Copilot-style
    // "Manage models" toggle in Settings → AI). Default-empty = every available model shows;
    // hiding is opt-out so the picker works out of the box. Serialized as "hidden_models".
    std::set<std::string> hiddenModels;
    // Bulk provider on/off: category keys the user has disabled wholesale ("gemini", "anthropic",
    // "openai", "openrouter", "alibaba", "deepseek", "local"). A disabled category's models are
    // hidden from the composer's chat picker and shown collapsed/greyed in Settings → Manage models.
    // Serialized as "disabled_providers".
    std::set<std::string> disabledProviders;

    // ITAR/IP outbound redaction (WS3 compliance_mode). "off" | "redact" | "local_only".
    // "redact" wraps cloud traffic: deterministic scrub (+ optional local-LLM residual pass) with
    // inbound rehydrate, images blocked. "local_only" refuses non-loopback (== offlineMode).
    // Enforced in the native C++ provider path (core/agent). Serialized top-level "compliance_mode".
    char complianceMode[16] = "off";

    // ---- Experimental AI features (Settings → Experimental) -> config.json "experimental" ----
    // All default-off and additive: when off, the resolved provider + agent loop are byte-identical
    // to the non-experimental path. See docs/EXPERIMENTAL_AI.md.
    //
    // Separate vision-review provider (the Seek-CAD split): a LOCAL OpenAI-compatible vision model
    // (e.g. self-hosted Qwen2.5-VL) judges the rendered viewport in the review/fix stages while the
    // main provider plans + emits ops. Lets you pair a strong/cloud planner with a local vision
    // judge, or keep the whole loop local for ITAR. Serialized under "experimental.vision_review".
    bool expVisionReview = false;
    char expVisionModel[512] = "qwen2.5-vl-7b-instruct";
    char expVisionBaseUrl[256] = "http://localhost:1234/v1";
    char expVisionKey[256] = "";   // API key for the vision endpoint (cloud vision model; blank=local)
    // Modeler prompt augmentation toward an editable parametric feature SEQUENCE (DeepCAD /
    // CAD-Coder-style design history) over a monolithic solid. Serialized "experimental.cad_sequence_mode".
    bool expCadSequenceMode = false;

    // Vertical-collapse state of the pinned FeatureManager / Copilot glass columns — a
    // collapsed column (rolled up to its header strip) stays collapsed across restarts. Serialized
    // "fm_collapsed" / "copilot_collapsed".
    bool fmCollapsed = false;
    bool copilotCollapsed = false;

    // Progressive disclosure for the Settings modal (the "Advanced" switch beside the tab
    // strip). OFF by default: every tab shows only the controls a normal user plausibly
    // changes, which is what keeps the AI surface readable instead of a wall of tuning
    // knobs. Turning it ON reveals every control in place.
    //
    // This NEVER removes a capability — the fork's mission statement is explicit that
    // "removing a setting must never remove a capability". Three escape hatches guarantee
    // that: (1) this switch, (2) config.json remains authoritative and is read whether or
    // not the control is currently painted, and (3) Settings search still indexes every
    // entry and auto-reveals advanced when a hit lands in a hidden section. So a hidden
    // knob is never an unreachable knob.
    //
    // Serialized top-level "show_advanced" (alongside "fm_collapsed"), omitted when false.
    bool showAdvanced = false;

    // Animation style of the copilot brand mark (the live tesseract logo): 0 = 8-cell
    // inside-out, 1 = double rotation, 2 = isoclinic, 3 = octave (2:1 locked planes). Plain
    // int, not ui::TesseractAnim, so core/ stays free of the UI header; the UI clamps it on
    // read via ui::tesseractAnimFromInt. Serialized "appearance.brand_anim" (omitted when 0).
    int brandAnim = 0;

    // User accent pair (Settings → Appearance). PRIMARY is the color a modelling operation's
    // RESULT is drawn in — including the geometry baked into the 3D ribbon icon tiles; SECONDARY
    // is the direction cue those tiles' arrows use. Mirrored into ui::setAccentColors, which
    // writes the palette AND retints the baked atlas so chrome and ribbon never disagree. Plain
    // float[3] (not an ImVec4) so core/ carries no ImGui type. Serialized
    // "appearance.accent.primary" / "appearance.accent.secondary" as 3-element arrays, omitted
    // when they equal the defaults below (== ui::kDefaultAccentPrimary/Secondary).
    float accentPrimary[3]   = {0.949f, 0.510f, 0.086f};   // #F28216 orange
    float accentSecondary[3] = {0.000f, 0.471f, 0.843f};   // #0078D7 blue
};

std::string appdataDir();
// The app data dir itself (base + "/Chudware" or "/Chudware" per the alias rule); logs once.
std::string appdataAppDir();
// Default Documents workspace dir (Documents/Chudware if it exists, else Documents/Chudware).
std::string defaultWorkspaceDir();
std::string configPath();
// Serialized, atomic read-modify-write of config.json. `patch` receives the parsed JSON
// (always an object) and mutates it in place; the result is written via tmp+rename.
//
// config.json is SHARED with the Python worker and the C++ engine, and every writer here
// used to do its own unsynchronized load-modify-write — so "GUI reads, Python writes, GUI
// writes" silently reverted Python's keys (the class of bug behind the 2026-07-14 account
// wipe). This takes the advisory lock on <config.json>.lock, the SAME file
// core/agent/auth.cpp and services/agent/providers/sub_auth.py use, so all three
// serialize. ALWAYS use this instead of hand-rolling ifstream/ofstream on configPath().
// Returns false if the file could not be written (the patch is then a no-op on disk).
bool updateConfigJson(const std::function<void(nlohmann::json&)>& patch);
// Consume the one-time warning emitted when updateConfigJson backs up a corrupt config.
// The writer may run on a background thread; the GUI displays this on its frame thread.
std::string takeConfigCorruptionWarning();
void loadSettings(Settings& s);
void saveSettings(const Settings& s);
// WASM IDBFS: mount /persist early, populate before first config/chat load, flush after writes.
// No-ops on desktop. appdataDir() returns "/persist" under EMSCRIPTEN once mounted.
void appstateInitPersist();
bool appstatePersistReady();
void appstatePersistSync();

// One provider's live availability, filled by the agent_list_models probe. Lets the model
// picker hide providers that are down / unkeyed and list the local endpoint's loaded models.
struct ProviderModels {
    bool checked = false;             // probe returned at least once
    bool ok = false;                  // reachable + (cloud) keyed
    std::vector<std::string> models;  // all discovered ids (local: every downloaded model)
    // The same probe's richer `model_rows` (id/label/context/max_output/vision/tools/
    // reasoning_efforts/auto_source), in the ORDER the endpoint returned — cloud APIs list
    // newest first, so a just-released model lands at the TOP of the picker. Empty when the
    // backend answered with flat ids only; the picker then falls back to the static catalog.
    std::vector<ui::ModelInfo> rows;
    // LM Studio's native endpoint reports which models are actually loaded (vs merely
    // downloaded). When loadedKnown is true, only ids in `loaded` can serve a request and the
    // picker hides the rest; when false (non-LM-Studio server) every listed model is assumed
    // available. See agent_list_models in libchudware.cpp.
    std::vector<std::string> loaded;
    bool loadedKnown = false;
    // Ids the endpoint prices at $0 (OpenRouter ":free" tiers etc.) — surfaced by agent_list_models
    // from each model's pricing. Lets the OpenRouter category list ALL currently-free models live,
    // so the set stays current instead of going stale in a hardcoded list.
    std::vector<std::string> free;
    std::string error;
};

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
    std::optional<double> value;  // exact overlap mm^3 or gap mm; absent is not zero
    std::optional<double> distanceLowerBoundMm;
};
struct FitReport {
    bool present = false;
    bool ok = true;  // true = no interference
    std::string summary;
    std::vector<FitPair> interferences, clearances;
};

// ---------------- per-document host session (multi-document mid-edit preservation) ----------------
// Each open CAD Part/Assembly tab owns one DocSession holding the editing/display state that must
// survive a tab switch: its OWN PropertyManager (the active feature-edit form + its live-preview
// batches), SketchMode (the active sketch), EvaluateDialogs (Measure / Mass / Section windows), plus
// a parked snapshot of the AppState per-doc VALUE fields (viewport selection, hidden-body mask, model
// readout, assembly readout, tree highlight). On a tab switch AppState swaps its live per-doc fields
// with the leaving/entering session (AppState::captureDocState / restoreDocState) and re-seats
// st.propertyManager / sketchMode / evaluateDialogs at the active session's instances — so switching
// neither cancels the PM form nor exits the sketch. The worker keeps every doc alive in its registry,
// so a backgrounded doc's parked edit state (PM checkpoint id / preview batches against its worker
// doc) stays valid and resumes exactly when its tab is reactivated.
//
// The DocMirror deliberately stays SINGLE on AppState (not per-session). Each DocSession parks an
// immutable mirror snapshot at its last loaded revision; switching restores it locally and fires the
// same listener, while a changed/cold tab asynchronously rebuilds from the worker. One live mirror
// therefore remains correct without putting a serialized worker round-trip on the UI thread.
struct DocSession {
    // Owned per-document UI subsystems. The full types live in their ui/*.h headers; this struct is
    // declared with only forward declarations, so its constructor + destructor are out-of-lined in
    // core/doc_tabs.cpp (which includes those headers) where the unique_ptrs see complete types.
    std::unique_ptr<ui::PropertyManager> pm;        // active feature-edit form (+ live preview)
    std::unique_ptr<ui::SketchMode>      sketch;    // active sketch (entities/constraints/tools)
    std::unique_ptr<ui::EvaluateDialogs> evaluate;  // Measure / Mass / Section result windows
    std::unique_ptr<ui::SimManager>      sim;       // simulation studies (FEA/CFD) linked to this part

    // Parked copy of AppState's per-doc VALUE fields, moved in/out on switch (empty for a fresh
    // session — equivalent to a cleared viewport). KEEP THIS LIST IN SYNC WITH
    // AppState::captureDocState / restoreDocState and AppState::clearViewportForNewDoc.
    std::vector<core::SelItem> selItems;            // viewport topology selection (st.selection items)
    render::PickHit            hover;
    std::string                pickStatus;
    std::vector<uint8_t>       bodyVisible;         // Solid Bodies hide/show mask (per renderer slot)
    std::vector<uint32_t>      docBodyIds;          // AICM body id per slot (keys the mask)
    std::unordered_set<std::string> hiddenFeatures; // FeatureManager eye-toggle: fids hidden (non-body)
    std::unordered_set<std::string> autoHiddenPlanes; // consumed default/user datum plane fids
    std::unordered_set<std::string> datumPlanesUserToggled; // sticky user-plane eye overrides
    // User-created points/csys whose FM eye took control (same sticky role as datumPlanesUserToggled).
    // Auto-hide with hasSolidContent never rewrites these again.
    std::unordered_set<std::string> refGeomUserToggled;
    // Datum visibility control state — parked per-doc so force-show/auto-show decisions never
    // leak across tabs (issue: origin force-shown in doc A stayed shown in doc B).
    bool showPlane[3] = {false, false, false};
    bool planesUserToggled = false;
    bool originUserToggled = false;
    bool axesUserToggled = false;   // sticky: FM eye on any axis takes control of axis auto-hide
    // Per-id sticky: axis ids whose FM eye the user explicitly toggled. Auto-hide never
    // rewrites these (esp. axis.auto:*); defaults also land here on eye click.
    std::unordered_set<std::string> axesUserToggledIds;
    std::string                modelInfo;            // (DocSession parked copy — see AppState field)
    std::vector<PartInfo>      asmParts;
    FitReport                  asmFit;
    std::string                asmStl;
    int                        selectedPart = -1;
    std::string                selectedFeatureId;   // FeatureManager primary selection
    bool                       treeScrollToSelection = false;
    std::vector<uint64_t>      featureHighlightFaces;
    bool                       sketchPlanePickActive = false;

    // Immutable content cache for instant already-open tab binds. The mirror snapshot, parsed AICM,
    // and Renderer GPU entry all share the same doc id + revision identity. aiPulse/invalidation
    // clears this path when a foreign edit changes a background document.
    std::string                       cachedAicmPath;
    int                               cachedRevision = -1;
    core::DocMirrorSnapshot           cachedMirror;
    std::shared_ptr<render::DocMesh>  cachedDocMesh;

    DocSession();
    ~DocSession();
    DocSession(const DocSession&) = delete;
    DocSession& operator=(const DocSession&) = delete;
};

// ---------------- worker job ----------------
struct AgentEvent;  // defined later (richer chat model)

// JobState is intrinsically non-copyable (std::mutex + std::atomic members) — it MUST always be
// held by std::shared_ptr<JobState> (never copied/moved). AppState::runs keys one JobState per
// conversation id, so a run is bound to its owning conversation for its entire lifetime.
struct JobState {
    std::atomic<bool> running{false};
    std::atomic<bool> ready{false};
    // The user hit Stop on THIS run specifically. UI-terminal: aiTurnActive() honors it
    // immediately for the VIEWED conversation; the drain uses it to discard this run's result
    // instead of applying it. Scoped per-run so stopping one conversation's turn never touches
    // another conversation's still-streaming run.
    std::atomic<bool> stopRequested{false};
    std::mutex mtx;
    int kind = JOB_BUILD;
    std::string stl, info, error;
    // The conversation + doc key this run is bound to (set at launch by launchAgentRunV2 /
    // launchMissionRun). Every streamed event and the final result are routed to THIS
    // conversation by id — never by scanning whichever conversation happens to be on screen —
    // so closing/switching a document tab or switching sessions never drops or misdelivers it.
    std::string convId;
    std::string docKey;
    // Active agent_run_v2 / mission run_id (from run_started event or result). Used by
    // requestStopRun → agent_cancel so Stop aborts the in-flight provider call.
    std::string runId;
    std::string provider;            // launched provider; browser controls never use the current picker
    bool errorIsCad = false;          // error came from CAD code that didn't build (expected,
                                      // the loop self-corrects) — NOT an app/runtime failure
    bool isAssembly = false;          // final result was a multi-part assembly
    std::vector<PartInfo> parts;      // per-part name/stl/dims (assemblies)
    FitReport fit;                    // final fit report (assemblies)
    bool converged = true;            // design loop ended by matching the request (not the cap)
    std::string stopReason;           // confirmed | stabilized | failed | provider_error | ...
    // Provider/loop failure detail (HTTP body excerpt, auth message, etc.). Prefer this over
    // a bare stopReason when composing the chat error card — empty on clean finishes.
    std::string errorDetail;
    // The short actionable sentence core/agent derives from the SAME failure
    // (humanize_provider_error) — e.g. "Provider not signed in. Open Settings > AI > Accounts."
    // Carried ALONGSIDE errorDetail, never as a fallback for it: errorDetail is the raw
    // supporting text, this is the headline the error card leads with.
    std::string errorHuman;
    // WS3: a true-assembly design run builds the parts in their own docs and saves a fresh
    // assembly .step+ rather than mutating the active doc. createdDocPath is that file (absolute);
    // the drain opens it as a NEW tab via the requestOpenPath pump (assembly opens route to Python).
    std::string createdDocPath;       // non-empty => open this file as a new tab on success
    // W17: the agent answered a "make a drawing of this part" request by building a 2D drawing
    // (a dwg-cpp registry id, NOT a doc_* doc). The drain opens it as a Drawing tab via
    // adoptDrawingTab — the active part doc was NOT mutated (so the doc_rebuild is skipped).
    std::string drawingDocId;         // non-empty => adopt this drawing as a new tab on success
    std::string drawingName;          // display name for the drawing tab
    std::string drawingSourceId;      // the source part doc the drawing projects from
    // CAM: the run built a machining job (its own cam doc id) from the current part. The drain
    // loads it into the Manufacture workspace panel; the active part doc was NOT mutated.
    std::string camDocId;             // non-empty => load this CAM job into the panel on success
    // Part-naming stage (ex-memory): AI-chosen display name for the built part. Set from the
    // streamed part_named event and/or the agent_run result's part_name field. The GL drain uses
    // it to refresh the tab title and auto-save <name>.step+ into the workspace.
    std::string partName;
    // File-first persistence: the core agent saved the part itself (at modeling start +
    // milestone re-saves + memory-stage rename) and reports the final path in the run
    // result's saved_path. Non-empty => the drain saves onto THIS path instead of
    // unique-ifying a duplicate <name>_2.step+ next to the agent's file.
    std::string savedPath;
    // Final doc revision from a successful agent_run_v2 result. Host autosave gates on
    // partName set AND finalRevision > 0 so a zero-progress / no_geometry run never writes.
    int finalRevision = 0;
    // Mission orchestrator: a multi-capability run (design -> CFD -> verdict). isMission marks the
    // job so the drain uses missionVerdict (the report stage's prose) as the final chat bubble
    // instead of the generic design summary; createdDocPath still carries any assembly tab to open.
    bool isMission = false;
    std::string missionVerdict;       // the report/verdict prose to show as the assistant reply
    // General-agent lane (core/agent/run.cpp pre-flight): the turn ran tools over
    // files/PLM/packs and never entered the geometry pipeline, so ITS handoff is the
    // only truthful reply. Non-empty => the drain prints this instead of the built-
    // geometry boilerplate, which otherwise claimed parametric features for a run
    // that created none (2026-07-27 zero-step mudbucket).
    bool generalLane = false;
    std::string agentSummary;
    // status=="partial": some tasks built, at least one failed. NOT an error — geometry is kept.
    // Drain shows a warning-accent "Partially complete" card; doc stays open.
    bool missionPartial = false;
    int missionTasksCompleted = 0;
    int missionTasksFailed = 0;
    int missionTasksTotal = 0;        // completed+failed+abandoned when known; else 0
    // Plan gate: ONLY when the user must decide — stop_reason awaiting_plan_approval
    // (manual Plan mode) or needs_user_decision (advisor escalate). Agent-decided plans
    // are advisor-approved in-core and do NOT set this (run continues normally).
    bool planReady = false;
    // Optional payload from the plan_ready result: {plan, tasks, summary, execution_style,
    // reason/verdict, ...} so the drain can seed ChatMsg plan section + card chrome.
    std::string planReadyJson;
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

// ---------------- multi-shape plan / objectives (copilot Plan section) ----------------
// Additive plan_state event surface. Host also normalizes legacy kind:"plan"/PlanInfo →
// shape=build_plan, and derives checklist/phased_todo from plan_steps / model_step / mission_*.
// kind on items: part|step|task|objective|node|subagent
// status: pending|running|pass|fail|done|abandoned
struct PlanItem {
    std::string id, title, desc, status, kind, phase, parent_id;
};
struct PlanEdge {
    std::string from, to;
};
struct PlanState {
    bool present = false;
    std::string shape;     // build_plan|checklist|phased_todo|objectives|dag
    std::string title, summary;
    int revision = 0;
    std::vector<PlanItem> items;
    std::vector<PlanEdge> edges;  // shape=dag only
    // Provenance: true when filled by an explicit kind:"plan_state" event (do not clobber
    // with weaker derived normalizers unless revision is higher).
    bool fromEvent = false;
};

// Parse a plan_state JSON object (SHARED CONTRACT). Returns present=false on bad input.
PlanState parsePlanState(const nlohmann::json& p);
// Map legacy PlanInfo → PlanState shape=build_plan (back-compat).
PlanState planStateFromPlanInfo(const PlanInfo& plan);

// ---------------- small ui helpers ----------------
// A caption button (minimize / maximize-restore / close) drawn with a crisp
// vector glyph and a full-height hover fill — close hovers red, the others a soft wash.
// For BTN_MAX the `maximized` arg is maximize-state.
enum WinBtn { BTN_MIN, BTN_MAX, BTN_CLOSE };
bool windowButton(const char* id, WinBtn kind, float h, bool maximized);

bool primaryButton(const char* label, const ImVec2& size);
void sectionHeader(ImFont* f, const char* text);
void dimText(const char* t);

void buildDefaultLayout(ImGuiID root, const ImVec2& size);

enum ErrKind { ERR_NONE = 0, ERR_CAD = 1, ERR_SYSTEM = 2, ERR_PARTIAL = 3 };

// Delivery of a user message sent while a run is live (ChatMsg::steerState). The agent reads
// a steer at its next tool boundary; a pure wait (wait_job) ends early for it.
enum SteerState {
    STEER_NONE = 0,       // not a steer (an ordinary prompt)
    STEER_SENDING = 1,    // handed to the control lane
    STEER_QUEUED = 2,     // the agent holds it; it reads it after the current step
    STEER_DELIVERED = 3,  // in the agent's context (or it answered an open question)
    STEER_CARRIED = 4,    // Stop withdrew it unread: it opens this chat's next message
    STEER_FAILED = 5,     // never reached the agent (run ended / transport error)
};

// ---------------- pre-flight clarify gate ----------------
// Before a Design run, agent_clarify_v1 returns the build-critical questions (or ready=true).
// One ClarifyQ per question; `answer` is the editable buffer shown in the inline clarify card,
// seeded with `suggested` so the user can accept a default with one click.
// Extended 2026-07-16 for interaction-contract kinds (choice multi/single, geometry_select).
struct ClarifyQ {
    std::string key, ask, unit, suggested;
    std::vector<char> answer{std::vector<char>(256, 0)};
    // interaction/1 kind: "text" | "dimension" | "choice" | "approval" | "geometry_select" | …
    std::string kind = "text";
    bool multi = false;                         // choice / geometry_select multi-select
    std::vector<std::pair<std::string, std::string>> options;  // choice: (value, label)
    std::vector<std::string> targets;           // geometry_select targets
    std::vector<std::string> selectedValues;    // multi-choice selected values OR geometry ids
    std::string selectedKind;                   // geometry: face|edge|feature|…
    bool pickArmed = false;                     // geometry_select: context-cube armed for this Q
};
struct ClarifyState {
    bool awaiting = false;   // clarify RPC in flight — the turn shows "working out what I need..."
    bool active = false;     // questions are shown; waiting for the user to fill them + Build
    std::string request;     // the original user request, launched once answered/skipped
    std::vector<ClarifyQ> questions;
    std::vector<std::string> assumptions;
    int geometryPickQ = -1;  // index of ClarifyQ currently armed for viewport geometry pick (-1=none)
};

// ---------------- transparent activity timeline ----------------
// One row in an assistant turn's activity timeline — a plain-language summary of one thing the
// model did (an orchestrator stage / tool action), expandable to reveal the raw thoughts/output
// behind it. Built live from orchestrator events (feedTurnActivity) so the user can watch what
// the AI is doing and thinking, coherently, inline in the conversation (this replaces the old
// separate "Agent Activity" panel).
struct ActivityItem {
    std::string stage;     // raw stage key (intake/plan/model/edit/review/fix/memory) — for matching
    std::string title;     // friendly heading ("Planning the build")
    std::string detail;    // the model's raw output / reasoning for this step (collapsible body)
    std::string reason;    // verdict reason (one short line shown under the title)
    std::string status;    // "running" | "pass" | "retry" | "fail" | "done" | ""
    int durationMs = 0, tokensIn = 0, tokensOut = 0, opsCount = 0;
    bool expanded = false; // user toggled this row open to read the detail
    bool missionHeader = false;  // a top-level mission TASK row ("Wind-tunnel it"); its sub-run
                                 // stages (intake/model/...) follow it, namespaced by task index
    // ---- typed transparency rows (M12-M14, aicad.agent_events/2 + exec-mode cards) ----
    // 0 = orchestrator stage row (everything above, unchanged default);
    // 1 = context injection chip (agent.context_injected): title=label, reason=source,
    //     ctxTokens=token_est (M12);
    // 2 = model thought box (agent.thought / agent.thought.delta): detail=text, streaming while
    //     deltas arrive, collapsed-by-default once complete (M13);
    // 3 = tool call row (agent.tool_call): title=tool_name, reason=target ("what it acts on"),
    //     durationMs=latency_ms, status=done|fail from `ok` (M14).
    // 4–8 = corporate org cards (see copilot/feed.cpp feedCorporateEvent).
    // 9 = provider_call log row (aggregated, not drawn inline).
    // 10 = routing / classification card.
    // 11 = swarm_wave header (wave N/M + task titles/status).
    // 12 = firm_bids card (contractor bids + budget).
    // 13 = firm_award card (winning firm + bid + reason).
    // 14 = firm_team roster card.
    // 15 = firm_comm thread (collapsed-by-default; detail appends lines).
    // 16 = advisor card (stage chip + question + advice; expanded).
    // 17 = sub_run compact card (purpose + status).
    // 18 = plan-section position marker; 19 = delegation card.
    // 20 = ask_user question-card position marker (stage "__ask__:<batch_id>").
    int rowKind = 0;
    int ctxTokens = 0;      // rowKind 1: best-effort injected-token estimate (kept out of the
                            // turn's usage footer — it is not model I/O)
    bool streaming = false; // rowKind 2: agent.thought.delta chunks still arriving; finalized
                            // (and auto-collapsed) by the matching completed agent.thought
    bool userToggled = false; // rowKind 2: user clicked the thought/output header; suppresses
                              // one-shot auto-collapse when the stream completes
    double startedTs = 0.0;   // GUI-only, not persisted: epoch s this row last entered
                              // "running" — the live elapsed readout on long sub-runs
                              // (context compaction, sub-agents)
};

// Wall-clock epoch seconds (UTC) — stamps ChatMsg send/receive times at construction.
inline double chatNowEpoch() {
    return std::chrono::duration<double>(
        std::chrono::system_clock::now().time_since_epoch()).count();
}

struct ChatMsg {
    bool fromUser = false;
    double ts = chatNowEpoch();    // send/receive time (epoch s); 0 = legacy/unknown (no age label)
    std::string text;              // user prompt, or the assistant's final summary
    bool running = false;          // assistant turn still streaming
    // Assistant turn closed early because the user steered the run: the SAME run continues in
    // the next assistant message, below the steer. Its empty text is not an empty turn.
    bool steerSplit = false;
    // User message sent mid-run (a steer): its delivery to the agent, drawn under the bubble.
    // steerId correlates the control-lane response and the worker's agent.steer events.
    std::string steerId;
    int steerState = STEER_NONE;
    std::string model;             // assistant turn: model id at run start (label); empty = plain "Assistant"
    int errKind = ERR_NONE;        // ERR_CAD = generated code didn't build (calm, amber);
                                   // ERR_SYSTEM = app/runtime/provider failure (red)
    // Error card fields (errKind != ERR_NONE). errorHuman is the short actionable sentence
    // core/agent emits as error_human — rendered as the card HEADLINE. errorFull is the
    // untruncated raw failure text (the drain keeps the whole thing; only the prose in `text`
    // is shortened), shown in a code box behind the "Error details" chip.
    std::string errorHuman;
    std::string errorFull;
    bool errFullOpen = false;      // user expanded the raw-detail box on this card
    std::string liveStatus;        // transient status line ("Building… rev 3") while running
    double lastEventTs = 0.0;      // GUI-only, not persisted: epoch s the run's latest feed event
                                   // landed — drives the "Waiting for model · 12m" quiet line
    PlanInfo plan;                 // the build plan drafted before the code (plan mode)
    PlanState planState;           // multi-shape Plan/Objectives section (plan_state + normalizers)
    std::vector<ActivityItem> activity;  // transparent "what the AI is doing" timeline (orchestrator)
    int curMissionTask = -1;       // index of the mission task currently running (-1 = not a mission);
                                   // namespaces sub-run stage keys so per-task stages don't collide
    ClarifyState clarify;          // pre-flight clarify gate (Agent/Design) — see ClarifyState
    // User-facing plan approve gate: ONLY manual Plan mode (plan_only) or advisor escalate
    // (stop_reason needs_user_decision). Agent/Edit/Swarm/EngFirm agent-decided plans are
    // advisor-approved in-core — no card. Cleared on execute / edit-plan / cancel.
    bool planApprovePending = false;
    std::string planApproveRequest;   // original user request to re-launch with approved_plan
    nlohmann::json planApproveExtras = nlohmann::json::object();  // attachments/context_refs
    // Optional escalate/advisor reason shown on the approve card (needs_user_decision).
    std::string planApproveReason;
    // The online agent's interactive ask_user batches (agent.question), drawn as question cards
    // in this turn. Resolution (agent.answer) may land after a steer opened a NEWER bubble, so
    // batches are located by batch_id across the conversation, never only in the live message.
    std::vector<AskBatch> asks;
    // Mission status=="partial": warning-accent result card (not error). Geometry kept.
    bool resultPartial = false;
    // Per-message copilot mode stamp (CopilotMode 0..2). -1 = unstamped / no mode border.
    // Set at submitTurn from st.aiInteraction; legacy persisted values pass normalizeCopilotMode.
    int copilotMode = -1;
    // Assistant turn: prompt-cache read tokens of the turn's LARGEST single completion
    // (agent.context events, feed.cpp). This is the figure shown in the bubble meta row,
    // because it is the only one comparable to the context meter beside it: with prompt
    // caching every completion re-reads the same prefix, so SUMMING the per-call reads
    // produced "150.8k cached" on a 23k context — arithmetically defensible, and read by
    // every human as a lie. 0 = no cache hits / provider reports no cache counts.
    int cacheReadTok = 0;
    // Sum of the same per-completion reads across the turn, plus how many completions
    // reported one. Cost-shaped rather than occupancy-shaped, so it lives in the tooltip.
    int cacheReadTotTok = 0;
    int cacheReadCalls = 0;
    // ---- M19 render-windowing cache (GUI-only, not persisted) ----
    // Height this message occupied last time it was actually drawn at uiCachedW width. Lets the
    // chat log emit a same-height Dummy for messages far outside the scroll viewport instead of
    // rendering them (endless scroll-back without unbounded frame cost). -1 = never measured.
    float uiCachedH = -1.0f;
    float uiCachedW = -1.0f;
    // User message: the model entities attached with the composer's context picker, drawn as
    // chips on the bubble (click = select in the viewport) and persisted with the session.
    std::vector<core::ContextRef> contextRefs;
    // User message: exact image paths sent to the model, never reconstructed from basenames.
    std::vector<std::string> imagePaths;
};

// Fold one streamed orchestrator event (kind = stage_started / agent_output / stage_result /
// run_finished; data = the event's "data" object) into a turn's activity timeline. Called on the
// GL thread from the event-drain loop. No-op for events it doesn't recognize.
void feedTurnActivity(ChatMsg& m, const std::string& kind, const nlohmann::json& data);

// Fold an agent.question / agent.answer event into the run's conversation. A question lands on
// `active` (the live assistant bubble; the last assistant message when none is live), deduped by
// batch_id; an answer resolves its batch wherever it sits in `msgs`. Returns true when `kind` was
// one of the two. GL thread (event drain, app.cpp).
bool feedAskEvent(std::vector<ChatMsg>& msgs, ChatMsg* active, const std::string& kind,
                  const nlohmann::json& data);
// The run that owned `msgs` ended or was stopped: every still-open batch becomes Cancelled.
// Idempotent with the worker's own agent.answer {source:"cancelled"}. Returns true if any changed.
bool closeOpenAsks(std::vector<ChatMsg>& msgs);
// True when any message in `msgs` holds an open (pending/submitting) batch.
bool hasOpenAsk(const std::vector<ChatMsg>& msgs);
// Steer delivery: agent.steer {steer_id, status: delivered|carried} moves the matching user
// bubble's steerState. Returns true when `kind` was a steer event. GL thread.
bool feedSteerEvent(std::vector<ChatMsg>& msgs, const std::string& kind,
                    const nlohmann::json& data);
// Set the state of the user bubble carrying `steerId`; confirmed DELIVERED never downgrades
// to a late control outcome (queued, carried, or failed). True if it changed.
bool setSteerState(std::vector<ChatMsg>& msgs, const std::string& steerId, int state);
// The run ended: steers it never confirmed (SENDING/QUEUED) are now FAILED. Returns true if
// any changed. A Stop's withdrawn steers are CARRIED by the cancel response instead.
bool settleOpenSteers(std::vector<ChatMsg>& msgs);

// Resolve the LIVE message vector a conversation's ChatMsgs currently live in: AppState::chat if
// convId is the currently VIEWED conversation, else that resident Conversation's parked msgs.
// nullptr if convId names no resident conversation (should not normally happen — every
// conversation stays resident for the app's lifetime once created). Used by the event drain /
// completion handling (app.cpp, engine_events.cpp) to route a run's output to ITS OWN bound
// conversation by id, never by scanning whichever conversation happens to be on screen.
std::vector<ChatMsg>* conversationMsgs(AppState& st, const std::string& convId);

// A conversation tail that a REVERT moved out of the way. Reverting is destructive by
// nature — it rewinds the chat to an earlier point — so the discarded messages are not
// deleted, they are parked here and persisted with the session. A mis-aimed revert is
// therefore always recoverable, which is the whole point of being able to revert "to any
// point": the operation must not itself be the thing that loses work.
struct ChatBranch {
    std::string id;                 // stable id ("b1", "b2", …) for the session file
    std::string label;              // human line, e.g. "reverted to message 12"
    double when = 0.0;              // epoch seconds the revert happened
    int fromMsgIdx = -1;            // index the chat was rewound to
    std::vector<ChatMsg> msgs;      // the discarded tail, in original order
};

// One copilot conversation = its own tab. RESIDENT: every conversation loaded this process stays
// in AppState::conversations for the app's lifetime — sessions are FIRST-CLASS, independent of
// documents; each optionally carries a docLink reference to the document it relates to.
// Switching/closing a document tab never destroys or reloads a conversation out from
// under an in-flight run. The ACTIVE (currently viewed) conversation's live messages stay in
// AppState::chat (so every existing st.chat user keeps working unchanged); every OTHER resident
// conversation parks its messages here, whether idle or still streaming in the background.
// Persisted to %APPDATA%/Chudware/chat_history.json as ONE flat session list (v2 schema) so
// conversations survive restarts independent of any document (see conversations.cpp).
struct Conversation {
    std::string id;
    std::string title;             // shown on the tab; defaults to "Chat N", renamed to 1st prompt
    std::string docLink;           // OPTIONAL document reference ("file:<path>" | "doc:<id>") —
                                   // metadata only (Sessions row badge); the session exists
                                   // independently of the document.
    // Workspace folder this session last ran under (initially its creation folder).
    // The composer sends the same top-left working directory as workspace_dir.
    // Persisted as "project"; Sessions filters this stored key, never document paths.
    // Empty = a legacy session shown only in Unassigned or All projects.
    std::string projectDir;
    std::vector<ChatMsg> msgs;      // populated only while INACTIVE (active lives in AppState::chat)
    int lastContextTokens = 0;     // per-session context meter (input tokens from last model call)
    std::vector<std::string> queuedPrompts;  // queued while this session's turn was active
    std::vector<nlohmann::json> queuedPromptDrafts; // attachment/context snapshots aligned with queuedPrompts
    // Archived sessions are hidden from the default Sessions list (Show archived toggle
    // reveals them) but keep their transcript/history — a soft "trash" before real deletion.
    bool archived = false;
    // Tails moved aside by reverts, newest last. Persisted with the session so a revert
    // stays undoable across restarts; never written to by anything except the revert path.
    std::vector<ChatBranch> branches;
};


// A progress event streamed from the worker (parsed off the worker thread, applied on the
// main/GL thread). kind = "orch_" + the run event's kind; text = the raw event JSON; notes = the
// compact status-line summary (jobs.cpp / engine_events.cpp).
struct AgentEvent {
    std::string kind;
    std::string text, notes;
};

// A small pill button that highlights when its section is expanded.
bool toggleChip(const char* label, bool active);

// Read-only, copyable monospace code view; height fits the content up to maxH.
void drawCodeBox(const char* id, const std::string& code, ImFont* mono, float maxH);

// Dim, wrapped prose (reasoning / notes) in a scrolling box, fixed at maxH.
// growToFit=true (a LIVE streaming card): grow to fit the content instead, capped at maxH —
// pass ui::metrics::copilotProseStreamCapH() as maxH in that case, not the static scroll cap.
void drawProseBox(const char* id, const std::string& text, float maxH, bool growToFit = false);

// Fit/interference readout: a green ("fits") or red ("clash") status line; when detailed,
// the per-pair overlap volumes (red) and clearance gaps (dim) below it.
void drawFitReadout(const FitReport& fit, bool detailed);

// Multi-shape Plan / Objectives section (tiles + todos tree + subagents).
void drawPlanSection(const PlanState& plan, const std::vector<ActivityItem>& activity,
                     const ui::Fonts& fonts);

// Manufacture operator surface owns only presentation/request state. Authoritative
// device/run/approval state lives in ui::MfgMirror and the hostd manufacturing lane.
struct MfgUi {
    enum class Mode { OfflineSimulation, LiveMachine };
    struct Request {
        std::string method;
        nlohmann::json params;
        std::string context;
    };

    Mode mode = Mode::OfflineSimulation;
    std::string selectedDeviceId;
    std::string selectedRunId;
    std::string liveDeviceId;
    std::string approvalRequestId;
    std::string lastError;
    std::deque<Request> requests;
    bool requestInFlight = false;
    bool bootstrapQueued = false;
    bool openApprovalDialog = false;
    int exercisableBy = 0;  // 0=human_only, 1=human_or_agent
};

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
    bool wantTree = false;   // CHUDWARE_SHOT=tree: scripted doc-smoke flow + screenshot
    bool treeShotDone = false; // guard so doc smoke runs exactly once
    bool wantEquations = false;   // CHUDWARE_SHOT=equations: open the Equations editor + seed two globals
    bool equationsShotDone = false; // guard so the equations seed runs exactly once
    bool wantPartsShot = false;   // CHUDWARE_SHOT=parts: Parts Library with a seeded no-model result (RE-32)
    // CHUDWARE_SHOT=*: the workspace the scenario selected, re-asserted every frame while
    // the capture runs. Startup doc restore calls activateTab, which pulls any workspace
    // that cannot host a 3D part back to Model (core/doc_tabs.cpp) — so a full-body
    // workspace scenario captured the modelling tab and "passed" while verifying nothing.
    // That race was hand-patched three times (parts, reveng, PLM) and still bit ECAD, the
    // AI workspace and Drawing; this pin replaces the special cases. -1 = unpinned (the
    // `workspaces` cycle scenario, which changes workspace deliberately).
    int shotWorkspacePin = -1;

    // ---- ECAD document (worker ecad_* RPCs; the editor mirror lives in ecad_panel.cpp) ----
    std::string ecadDocId;          // open ECAD doc id ("" = none)
    int         ecadRevision = -1;  // last known ECAD revision (optimistic-concurrency base)
    bool        wantEcad = false;   // CHUDWARE_SHOT=ecad scripted scenario
    bool        ecadShotDone = false;
    int         wantFocusViewport = 0;      // frames to keep forcing the 3D Viewport tab forward
                                            // (set by the ECAD board->3D bridge so the board shows)
    int         wantFocusEcad = 0;          // frames to keep forcing the ECAD tab forward (AI Design)
    std::string ecadAiRequest;              // NL circuit request from the ECAD launchpad
    bool        ecadAiGo = false;           // one-shot: run ecad_design(ecadAiRequest) this frame
    bool        ecadAiBoardGo = false;      // one-shot: AI-design a schematic, then create+populate+
                                            // outline+autoroute a board from it (reuses ecadAiRequest)
    int         ecadTool = 0;               // ECAD canvas tool: 0=Select 1=Place 2=Route
    nlohmann::json ecadPendingOps;          // ops the ECAD canvas wants applied (place/route clicks)
    bool        ecadOpsGo = false;          // one-shot: apply ecadPendingOps via ecad_apply_ops
    // Native simulator (ecad_simulate): the last result payload + a one-shot run trigger. The plot
    // panel in ecad_panel.cpp renders ecadSimResults when non-empty (transient waveforms / AC bode).
    nlohmann::json ecadSimResults;          // results object from the last ecad_simulate ({op,tran,ac,log})
    bool        ecadSimGo = false;          // one-shot: run ecadSimulate(st) this frame
    int         ecadSimAnalysis = 0;        // 0 = Transient, 1 = AC
    bool        ecadUndoGo = false;         // one-shot: run ecadUndo(st) this frame (ecad_undo RPC)
    bool        ecadRedoGo = false;         // one-shot: run ecadRedo(st) this frame (ecad_redo RPC)
    // One-shot ECAD command the 2D panel wants dispatched through the same handler the ribbon uses
    // (ecadDispatch in app.cpp): e.g. "sch_annotate", "pcb_update_from_sch". "" = nothing pending.
    // Lets panel-owned toolbar buttons fire the exact ribbon actions without duplicating logic.
    std::string ecadPanelCmd;

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
        // Wave 2 (GUI rework): Schematic/PCB appended at the END so existing numeric ordinals
        // (Part=0, Assembly=1, Drawing=2) don't shift. A Schematic/PCB tab is NOT a DocMirror doc:
        // like Drawing it leaves the mirror untouched and carries its OWN id (ecadDocId) into the
        // worker's ecad_* registry, with its per-document editor state on `ecadState` below.
        enum class TabKind { Part, Assembly, Drawing, Schematic, PCB };
        TabKind     kind = TabKind::Part;
        std::string docId;          // worker doc id (registry key) — empty for Drawing/ECAD tabs
        std::string title;          // display name (doc "name", "Part1"/"Assembly1")
        bool        isAssembly = false;
        bool        dirty = false;  // edits since last save (asterisk on the tab)
        // The blank "example" document the app offers on launch (the Welcome chooser's New Part /
        // New Assembly). It is disposable: opening a real file silently discards it (see
        // dropBlankStarterTabs) UNLESS the user touched it (then `dirty` is set and it is kept). A
        // doc from a DELIBERATE mid-session New Part/Assembly is NOT a starter, so it always survives.
        bool        isStarter = false;
        std::string path;           // last saved file path ("" = never saved)
        // Project folder that was active (convProjectDir) when this tab was first opened. Scopes
        // never-saved agent docs in the AI workspace; a saved doc is scoped by `path` instead.
        std::string projectDir;

        // Live MCP handoff mirror: this tab shows the part the external Chudware MCP engine is
        // building (pollAiCadHandoff opens it from %APPDATA%\Chudware\handoff\live.step+ and refreshes
        // it in place as the AI works). It is a READ-ONLY view of another process's doc — the user's
        // own tabs are never the handoff. Flagged so the refresh can find it without path-matching,
        // label it clearly, and avoid stealing focus / yanking an in-progress edit.
        bool        aiHandoff = false;
        // When aiHandoff is set, which MCP doc_id this mirror tab tracks. The poller keeps ONE tab
        // per doc_id, so an AI agent opening several parts leaves them all open side by side. Empty
        // for the legacy single-mirror path (older handoffs that only publish live.json).
        std::string handoffDocId;
        // The advertised part NAME for this mirror (from docs.json). Lets the poller collapse two
        // mirror tabs that are the SAME part re-created under different doc_ids (the duplicate-tab
        // bug) even when the doc_id key differs. Empty for legacy/unnamed handoffs.
        std::string handoffName;
        // An AI subagent has edited this part since the user last looked at it (it is NOT the active
        // tab). The tab strip renders an animated, pulsating glow around such tabs so a swarm working
        // in the background is visible without stealing focus or the viewport. Cleared the moment the
        // user switches to the tab (switchToTab) or the poller refreshes it while it IS active.
        bool        aiPulse = false;
        // Steady-clock time of the last agent edit event on this doc. The pulse above means
        // "actively being edited" ONLY: a per-frame tick clears it once the doc has seen no edit
        // event for kAgentEditPulseTimeoutS, so a finished/idle agent stops glowing on its own.
        // Timestamp-based (not a countdown timer) so nothing stacks or drifts across sleeps.
        std::chrono::steady_clock::time_point lastAgentEditAt{};
        // Agent edit lock (issue #55 interim): steady-clock time of the last revision of this doc
        // written by a NON-gui client (an in-app agent, its sub-agents, an external MCP agent),
        // stamped for active and background tabs alike. Epoch = never. See docLockedByAgent.
        std::chrono::steady_clock::time_point lastForeignWriteAt{};
        // A pushed foreign revision invalidated this tab's immutable mesh cache. The background
        // parser clears it after prewarming the matching revision; switchToTab never binds while set.
        bool        meshCacheInvalidated = false;
        // Ownership: the user ADOPTED this AI mirror tab by editing it (first edit flips it via
        // markActiveTabDirty). An adopted doc becomes user-owned — it is never auto-closed when
        // the agent finishes, IS included in the unsaved-work quit warning, and drops the "AI:"
        // label prefix. The agent keeps co-editing the same live doc (existing multi-writer CAS).
        // Set once, never cleared (survives save). Meaningless unless aiHandoff.
        bool        userAdopted = false;
        // The agent that was building this mirror has FINISHED (daemon sent doc.closed with
        // reason "last_writer_detached" — hostd emits it at the end of EVERY turn, so this is
        // "no writer attached right now", NOT "the agent abandoned the part"). Display only:
        // it drives the Active Work "agent done" chip. It MUST NOT gate closing a tab.
        bool        agentDone = false;
        // True after doc.history_cleared for this doc (human save wiped undo/redo). Greys the
        // undo/redo toolbar until a local edit re-enables history. Cleared on local mutation.
        bool        historyCleared = false;
        // Reconnect-brake (crash-loop guard, engine_events.cpp): this mirror tab's automatic
        // doc_activate-on-reveal was skipped because >=3 engine reconnects happened within 60s
        // (the doc's activation is crashing hostd every re-bootstrap). The tab stays open but
        // unfocused; cleared the moment any switchToTab targets it — a manual click still
        // activates normally, brake or not.
        bool        activationHeldByBrake = false;

        // Display label for the tab strip + viewport title. Prefers the on-disk file name (the
        // thing the user actually opened/saved) over the internal tree "name" — an opened .step+
        // keeps its tree name "Part1" but should read as "bracket" on the tab. Falls back to the
        // tree-derived `title` for never-saved docs (and for Drawing/ECAD tabs, whose `path` is
        // empty). The leading directory and a single trailing extension (incl. ".step+") are
        // stripped. Defined in core/doc_tabs.cpp.
        std::string displayTitle() const;

        // Saved camera so each tab keeps its own view across switches.
        float yaw = 35, pitch = 28, zoom = 1.0f, pan[3] = {0, 0, 0};
        bool  camValid = false;     // false until the first switch-away captures the live camera

        // ---- Part / Assembly tabs only — per-document editing/display session ----
        // Holds this document's PropertyManager + SketchMode + EvaluateDialogs and a parked snapshot
        // of the per-doc value state, so switching away preserves the in-progress edit instead of
        // cancelling it (see DocSession). Lazily created by ensureTabSession() on first activation;
        // null for Drawing / Schematic / PCB tabs (which don't drive the DocMirror). shared_ptr so a
        // DocTab stays copyable (closeDocTab takes a by-value copy), mirroring ecadState.
        std::shared_ptr<DocSession> session;

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

        // ---- W15: Section + Detail view tools (drawing tabs) ----
        // Section tool: arm it, then click two points across a parent view to drop a cutting line ->
        // drawing_add_section{cut_line}. Detail tool: arm, click a center on a parent view then drag
        // to set the radius -> drawing_add_detail. Both mirror the dimension tool's modal stages.
        bool        sectionToolActive = false;  // armed: clicks place a cutting line
        int         sectionStage = 0;           // 0=pick line start, 1=pick line end
        std::string sectionViewId;              // parent view the cutting line is anchored to
        float       sectionAx = 0, sectionAy = 0;  // cut-line start (sheet mm)
        bool        detailToolActive = false;   // armed: click center + drag radius
        int         detailStage = 0;            // 0=pick center, 1=drag radius
        std::string detailViewId;               // parent view the detail circle is anchored to
        float       detailCx = 0, detailCy = 0; // detail circle center (sheet mm)

        // ---- W17: detailing tools (drawing tabs) — all mutually exclusive with the above ----
        // One-shot / single-click tools the new Annotation ribbon arms. modelItems auto-dimensions
        // the clicked view; holeCallout/centerMark snap to a circle; balloon drops an item bubble;
        // note places a text note; centerLine is a 2-click construction line. drawingHandleCommand()
        // arms them; the 2D panel consumes the clicks (mirrors the dim/section/detail tool stages).
        bool        modelItemsActive = false;   // armed: click a view -> auto-dimension it
        bool        holeCalloutActive = false;  // armed: click a circle -> "n× ⌀d" callout
        bool        centerMarkActive = false;   // armed: click a circle -> center mark
        bool        noteActive = false;         // armed: click -> place a note
        bool        balloonActive = false;      // armed: click -> item-number balloon
        int         balloonNext = 1;            // next balloon item number (auto-increments)
        bool        centerLineActive = false;   // armed: 2 clicks -> center line
        int         centerLineStage = 0;        // 0=pick a, 1=pick b
        float       centerLineAx = 0, centerLineAy = 0;  // center-line start (sheet mm)
        // Title-block / sheet-properties editor popup (opened from the ribbon).
        bool        titleBlockOpen = false;     // request to open the title-block editor this frame

        // ---- Drawing overhaul: view drag/select + GD&T tools + custom sections ----
        // View move: click a view's geometry to select it, then drag to reposition. The drag
        // is OPTIMISTIC (the cached view position + polylines translate locally each frame) and
        // commits once on release via drawing_set_view {position} — a pure sheet offset that the
        // engine applies WITHOUT re-projecting (no HLR), so dragging stays smooth.
        std::string selViewId;                  // currently selected view (for move / Delete / scale)
        bool        viewDragActive = false;     // a view-move drag is in progress
        float       viewDragDX = 0, viewDragDY = 0;  // live drag offset (sheet mm) during the drag
        float       viewDragStartMx = 0, viewDragStartMy = 0;  // mouse-down point (sheet mm)
        // GD&T placement tool: arm a kind, click the feature to anchor (a), then click to drop the
        // frame/symbol (pos) -> drawing_add_gdt/datum/.... gdtKind is the annotation kind.
        bool        gdtToolActive = false;      // armed: place a GD&T element
        std::string gdtKind;                    // gdt_frame|datum_feature|datum_target|surface_finish|weld_symbol
        std::string gdtViewId;                  // view the GD&T element anchors to
        int         gdtStage = 0;               // 0=pick feature anchor, 1=pick symbol position
        float       gdtAx = 0, gdtAy = 0;       // picked feature anchor (sheet mm)
        nlohmann::json gdtSpec;                 // the spec being authored (symbol, tol, datums, ...)
        bool        gdtEditorOpen = false;      // request to open the GD&T editor popup
        // Custom-section controls the Section tool applies on commit.
        std::string sectionMode = "full";       // full|half
        bool        sectionReverse = false;     // flip which side is removed
        std::vector<std::array<float, 2>> sectionPts;  // multi-point (offset/jogged) cut-line points

        // ---- Schematic / PCB tabs only (kind == Schematic || PCB) ----
        // An ECAD tab is backed by a SEPARATE worker registry (ecad_* RPCs), not the DocMirror.
        // ecadDocId is the worker ecad doc id; ecadRevision is the optimistic-concurrency base.
        // ecadState holds this document's editor view (EcadMirror + EcadCanvas + selection + edit
        // buffers) so multiple ECAD docs can be open at once — the per-document fields that used to
        // live in ecad_panel.cpp's file-static singleton. It is a shared_ptr to an opaque type whose
        // full layout lives in ecad_panel.cpp; lazily created on first activation of the tab.
        std::string ecadDocId;                  // worker ecad doc id (NOT a doc_* id) — ECAD tabs
        int         ecadRevision = -1;          // last known ECAD revision for this doc
        std::shared_ptr<ui::EcadEditorState> ecadState;  // per-document ECAD editor state
    };
    std::vector<DocTab> docTabs;
    int activeTab = -1;             // index into docTabs; -1 = no document open (start state)
    // [workspace-tabs seam] Top-level workspace selected in the title bar. Drives which body fills
    // the central region (viewport / sketcher / sim / ecad / cam / plm / parts / ai) and which
    // ribbon set is visible. Title bar writes it; app.cpp routes on it; ribbon filters by it.
    // RE-ANCHOR(reveng): append ReverseEng after AI (RE-03 workspace tab shell).
    // Append Firmware after ReverseEng (append-only — status_bar indexes by value).
    enum class Workspace { Model, Drawing, Simulate, Electronics, Manufacture, PLM, PartsLibrary, AI, ReverseEng, Firmware };
    Workspace activeWorkspace = Workspace::Model;
    // Parts Library → Reverse Engineering handoff seam. The parts panel's
    // "Reverse-engineer this part" action creates a session (reveng_start_session
    // RPC), sets this to the new session_id, and switches activeWorkspace to
    // ReverseEng. The RE workspace (RE-30) consumes + clears it on its next draw to
    // auto-select that session. Additive + optional: even unread, the RE tab's
    // session list surfaces the new session, so the handoff still works.
    std::string pendingRevengSessionId;
    // Manufacturing UI seam: exactly one presentation/request bundle plus one
    // read-only mirror fed by RPC completions and the hostd event drain.
    MfgUi mfg;
    std::shared_ptr<ui::MfgMirror> mfgMirror;
    // Multi-document: sessions of CLOSED tabs that may still have an async RPC in flight. A
    // DocSession's PropertyManager / SketchMode / EvaluateDialogs capture `this` in their st.rpc
    // completion lambdas (live preview, AI assist, sketch overview/regions, evaluate). Destroying the
    // session while such a call is queued would make the later GL-thread drain deref a freed object,
    // so closeDocTab parks the closing session here instead of letting it die; sweepRetiredSessions()
    // releases them once the async queue is fully drained (rpc not busy). Bounded: the common
    // no-pending-call case clears it the very next frame.
    std::vector<std::shared_ptr<DocSession>> retiredSessions;
    void sweepRetiredSessions();
    // While true, the DocMirror listener does NOT mark the active tab dirty. Set around a tab
    // switch / resync / fresh-doc activation (those fire the mirror's listener via doc_rebuild but
    // are not user edits, so they must not light the unsaved-changes marker).
    bool suppressTabDirty = false;
    // While true, applyOpenedDoc stays SILENT (no "Opened …" / "Restored drawings" toast). Set only
    // around the live MCP-handoff refresh (pollAiCadHandoff reopens the mirror on every AI build
    // step) so watching the AI build doesn't spam a toast per step. Cleared right after the open.
    bool handoffQuietOpen = false;
    // Docs whose mirror tab the user CLOSED by hand this session, keyed by doc id
    // with the label the tab carried. The agent keeps building them (hostd degrades
    // a user close on an agent-owned doc to a detach), and every subsequent
    // doc.changed used to re-reveal the tab the user just dismissed — the close read
    // as a no-op. Membership suppresses the AUTOMATIC reveal only: the Artifacts
    // panel lists these under "Open Documents" with a Reopen action, and any
    // deliberate open clears the entry and restores live geometry updates.
    std::map<std::string, std::string> dismissedMirrorDocs;


    // ---- document-tab operations (implemented in core/doc_tabs.cpp via ui/doc_tabs) ----
    // Append a tab for an already-created/opened worker doc and make it active (does NOT touch the
    // worker — the caller already did doc_new/doc_open and the mirror points at it). Captures the
    // current doc id from docMirror.
    void addDocTab(bool isAssembly);
    // Switch the active document to docTabs[idx]: safely exits any in-progress sketch/PM form,
    // saves the leaving tab's camera, re-points DocMirror at idx's doc (doc_activate → tree+mesh),
    // resets per-doc caches/selection/tree transients, reloads the mesh, restores idx's camera.
    // Never closes a tab — see the "NO TAB IS EVER AUTO-CLOSED HERE" note in doc_tabs.cpp.
    void switchToTab(int idx);
    // Async engine activation for a tab switch (the cold-cache path of switchToTab): issues
    // doc_activate on st.rpc and adopts the ops-response on drain (mirror + mesh + selection +
    // camera). doc_activate serves the engine's warm read-cache compile without recompiling;
    // a stale daemon predating it is translated to doc_rebuild inside engine::call.
    void activateTabDocAsync(const std::string& docId, int tabIdx, uint64_t generation,
                             std::chrono::steady_clock::time_point queuedAt);
    // Close docTabs[idx] (with the worker doc_close). If it was active, activates a neighbor; if it
    // was the last tab, returns to the empty start state. Does NOT prompt — the caller decides.
    // detachOnly (aiHandoff tabs, daemon mode): drop only THIS client's view (detach_doc) instead of
    // discarding the doc, so an un-adopted AI doc the agent abandoned survives to the daemon's
    // GC/retire tier (recoverable) rather than being purged. A manual X-click leaves
    // detachOnly=false (explicit user intent to discard the mirror).
    void closeDocTab(int idx, bool detachOnly);
    void closeDocTab(int idx) { closeDocTab(idx, false); }
    // Discard any disposable blank "starter" tab(s) — the launch example doc the user never touched
    // (isStarter && !dirty && no saved path) — EXCEPT the tab at keepIdx. Called when a real file is
    // opened so the blank example doesn't linger beside it. A starter the user edited (dirty) or
    // saved (has a path) is kept. No-op when there is no such tab.
    void dropBlankStarterTabs(int keepIdx);
    // Discard the untouched auto-created "starter" document of the workspace being LEFT (an empty
    // PCB1 on leaving Electronics, an untouched starter Part on leaving Model, etc.) so a blank
    // initial doc the user never added to doesn't linger. No-op unless the tab is isStarter && clean
    // && never-saved; never closes the last remaining tab (won't strand the user with zero docs).
    void discardWorkspaceStarter(Workspace leftWs);
    // Mark the active tab dirty (on any mutation) / clear it (on save). Safe with no active tab.
    void markActiveTabDirty();
    void clearActiveTabDirty();
    // Mark the ECAD (Schematic/PCB) tab carrying `ecadDocId` dirty. ECAD tabs aren't DocMirror-backed
    // (their docId is empty), so they're matched by their ecad id, not the mirror. Honors
    // suppressTabDirty. No-op if no such tab.
    void markEcadTabDirty(const std::string& ecadDocId);
    // Mark a Drawing tab dirty. A drawing is embedded in its source part's .step+, so its unsaved
    // state is persisted by saving that part; the tab still carries its own marker so closing it
    // warns. Honors suppressTabDirty. No-op if idx isn't a Drawing tab.
    void markDrawingTabDirty(int idx);
    // Clear the dirty marker on every Drawing tab embedded in source part `sourceDocId` — call after
    // that part is saved (doc_save embeds all of its open drawings). No-op if none match.
    void clearDrawingDirtyForSource(const std::string& sourceDocId);
    // Refresh the active tab's cached title from the current doc tree (after a rename, etc.).
    void syncActiveTabTitle();
    // Index of the tab whose docId matches the mirror's current doc, or -1.
    int  tabIndexForActiveDoc() const;

    // ---- in-context component edit (SolidWorks "Edit Component"; app_state.cpp) ----
    // While editing a component's model in-context, the component's part-doc becomes the REAL ACTIVE
    // doc (so feature ops route to it for free — the "current doc" is process-wide) and the assembly
    // is drawn as a dimmed static GHOST in the same viewport. The active-doc switch reuses the exact
    // cached open/switch path openComponentPart uses (dedup→switchToTab, else requestOpenPath), so
    // the per-session mirror/parsed-tess/GPU cache is inherited — no fresh re-tessellate.
    struct ComponentEditCtx {
        bool        active = false;
        std::string componentId;      // the assembly component being edited (banner label)
        std::string sourcePath;       // component's resolved absolute source .step+ path
        std::string assemblyDocId;    // return target: the assembly doc to switch back to on exit
    } componentEdit;
    // Enter component-edit: remember the current (assembly) doc as the return target, capture its live
    // mesh into the renderer ghost, then switch the ACTIVE doc to sourceAbsPath via the cached open/
    // switch path. sourceAbsPath must already be resolved to an absolute on-disk .step+ (the caller
    // owns resolveComponentPath). No-op if already in a component edit or sourceAbsPath is empty.
    void enterComponentEdit(const std::string& componentId, const std::string& sourceAbsPath);
    // Exit component-edit: switch the active doc back to the assembly (its cached mesh/revision via the
    // same cached switch path — the assembly re-reads the edited component through the existing
    // mtime-keyed component cache), clear the renderer ghost, and reset the context. No-op if inactive.
    void exitComponentEdit();
    bool inComponentEdit() const { return componentEdit.active; }

    // ---- feature-tree → asm mate/placement panel request seam (asm_panels.cpp consumes) ----
    // The tree's component "Edit" item sets this to the clicked component id; AsmPanels::draw() opens
    // and focuses the Mate form on it once per frame, then clears the field. Kept tiny: the mate form
    // is an add-mate workflow (no "show existing mates for X" state today), so "Edit" surfaces the
    // placement/mate panel focused for that component rather than loading its existing mate list.
    std::string requestEditComponentMates;

    // ---- multi-document per-session state (core/doc_tabs.cpp + app_state.cpp) ----
    // The active CAD tab's DocSession (null at the empty start state or on a Drawing/ECAD tab).
    DocSession* activeSession();
    // Lazily allocate t.session for a Part/Assembly tab (no-op if already present or non-CAD tab).
    void ensureTabSession(DocTab& t);
    // Point st.propertyManager / sketchMode / evaluateDialogs at `s` (the active session), or at the
    // run_gui fallback singletons when s == nullptr (empty start / non-CAD tab). Called on every
    // switch + on session creation/teardown so every borrow site resolves the right document.
    void reseatActiveSubsystems(DocSession* s);
    // Move AppState's live per-doc VALUE fields (selection items, hover, hidden-body mask, model +
    // assembly readout, tree highlight, sketchPlanePickActive) INTO `s` — called for the LEAVING tab
    // before a switch. captureDocState moves out (leaves AppState's fields empty/default);
    // restoreDocState moves the ENTERING tab's parked fields back. Selection items are restored
    // AFTER loadDocMesh (which clears the selection), so restoreDocState handles only the model-side
    // fields and the caller restores selection/hover/pickStatus from `s` post-mesh. The owned PM /
    // sketch / evaluate are NOT touched here (they live on `s`; reseatActiveSubsystems repoints them).
    void captureDocState(DocSession& s);
    void restoreDocState(DocSession& s);
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
    // W12+ — adopt an ALREADY-registered drawing (restored by doc_open from a .step+) into a new
    // Drawing tab. Unlike makeDrawing (which creates a blank drawing + standard views), this just
    // fetches drawing_get for an existing drawing id and opens its tab. Does NOT steal the active
    // tab focus (the reopened part stays active). No-op + log on a failed fetch.
    void adoptDrawingTab(const std::string& drawingDocId, const std::string& name,
                         const std::string& sourceDocId);
    // Adopt a drawing opened from its own .draw+ container, remember that path for
    // direct standalone saves, and activate the Drawing tab.
    void adoptStandaloneDrawingTab(const std::string& drawingDocId,
                                   const std::string& name,
                                   const std::string& sourceDocId,
                                   const std::string& path);
    // True when the active tab is a Drawing (the central area shows the 2D sheet, not the 3D view).
    bool activeTabIsDrawing() const {
        return activeTab >= 0 && activeTab < (int)docTabs.size() &&
               docTabs[activeTab].kind == DocTab::TabKind::Drawing;
    }
    // Wave 2: true when the active tab is a Schematic or PCB (the ECAD editor owns the center, like
    // a Drawing tab the DocMirror is left untouched). Panels gated to a live CAD doc should also
    // exclude this so they don't act on the part hiding behind the ECAD canvas.
    bool activeTabIsEcad() const {
        return activeTab >= 0 && activeTab < (int)docTabs.size() &&
               (docTabs[activeTab].kind == DocTab::TabKind::Schematic ||
                docTabs[activeTab].kind == DocTab::TabKind::PCB);
    }
    // Wave 2 (GUI rework): open a freshly-created worker ECAD doc (ecad_new sch|pcb) as a new
    // Schematic/PCB tab + activate it. Implemented in core/doc_tabs.cpp; the worker call + mirror
    // load happen via the app.cpp ECAD helpers, this just appends the tab and points the editor at
    // it. kind is "sch" (Schematic) or "pcb" (PCB).
    void addEcadTab(const std::string& kind, const std::string& ecadDocId,
                    int ecadRevision, const std::string& title);

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
    // FeatureManager per-feature eye toggle (the hide/show eye on every tree row). Holds the fids of
    // features hidden via the eye that DON'T own bodies (sketches, datums) — body-owning features
    // route their eye straight to bodyVisible instead. Consulted by the sketch-ghost renderer; per-doc
    // (parked in DocSession, cleared by clearViewportForNewDoc).
    std::unordered_set<std::string> hiddenFeatures;
    // Auto-hide bookkeeping for datum planes. Every plane consumed (directly or transitively) by
    // a downstream 3D feature is recorded here. User-plane fids are also mirrored into
    // hiddenFeatures; the three reserved plane fids drive viewport_panel's origin-plane overlays.
    // Per-doc; cleared by clearViewportForNewDoc.
    std::unordered_set<std::string> autoHiddenPlanes;
    // User-created datum planes whose FeatureManager eye has taken control. Auto-hide never changes
    // these planes again, in either direction, so force-show and force-hide both survive rebuilds
    // and tab switches. The three reserved planes use planesUserToggled/showPlane instead.
    std::unordered_set<std::string> datumPlanesUserToggled;
    // User-created points / coordinate systems whose FM eye took control (parity with
    // datumPlanesUserToggled). Solid-content auto-hide skips these ids so an unhide sticks.
    std::unordered_set<std::string> refGeomUserToggled;
    // Push bodyVisible to the renderer (per-slot draw skip) and the id pass (pick skip). Call
    // after any toggle; loadDocMesh calls it after every setMeshes/setDoc.
    void applyBodyVisibility();
    // Re-derive document-driven visibility after a rebuild: assembly component masks plus datum
    // planes consumed by the active feature dependency graph (parts and assemblies).
    void applyDocDrivenVisibility(const nlohmann::json& tree);
    // Push hiddenFeatures to the renderer so hidden DATUM features (ref axis/plane/point/csys) drop
    // out of the reference-overlay draw immediately. Call after an eye toggle, a tab switch, and
    // every datum rebuild (the datum geometry is otherwise re-pushed only on a document mutation).
    void applyDatumVisibility();
    // W2-ref: rebuild axisDisplayNames for defaults + auto plane-intersection axes + user
    // axis features. Auto axes use "<UserPlane> <letter> Axis" (Front→X, Right→Y, Top→Z).
    // Call from FeatureManager (or after inject) so FM labels stay correct even when the
    // inject path still seeds the legacy "Axis A ∩ B" string into axisAutoCache.
    void rebuildAxisDisplayNames(const nlohmann::json& tree);
    // Pure helper: display name for a plane-pair auto axis (id form axis.auto:<a>|<b>).
    // planeNameById supplies feature/reserved display names; missing ids fall back to the id.
    static std::string formatAutoAxisDisplayName(
        const std::string& planeIdA, const std::string& planeIdB,
        const std::unordered_map<std::string, std::string>& planeNameById);


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
    // C39/stop-crash: guarded by logMtx — the worker errorSink calls addLog() from background
    // threads (jobs.cpp agent run thread, the AgentControlChannel cancel worker, AsyncRpc
    // dispatch) while drawConsoleBody iterates it on the GL thread. addLog() and the console
    // reader both take the lock; a bare push_back racing the render loop corrupted the heap
    // (intermittent SIGABRT on Stop).
    std::mutex logMtx;
    std::vector<std::string> log;

    // ---- runtime + worker ----
    WorkerClient worker;
    // C39: async dispatch over the same worker. Declared AFTER worker so it is destroyed
    // FIRST (joins its thread before the client goes away). run_gui also shuts it down
    // explicitly before worker.stop() so teardown never waits out an RPC timeout.
    core::AsyncRpc rpc{worker};
    std::string werr;
    bool workerStarted = false;
    std::atomic<bool> setupRunning{false}, setupDone{false}, setupOk{false};
    std::mutex setupMtx;
    std::string setupMsg;
    std::string status;
    int statusKind = 0;  // 0 = ok (teal), 1 = build issue (amber), 2 = system error (red)
    // H2: engine daemon socket dead / hello failed mid-session. Sticky status-bar
    // chip ("Engine daemon unreachable — retrying"); cleared on reconnect. Never
    // toast-spammed — one sticky notice only (see engine_events.cpp).
    bool daemonUnreachable = false;
    // Set while a draining hostd refuses this client (engine::daemonRefusal()): its
    // message and hint. The chip then names the drain instead of "unreachable".
    std::string daemonDrainNotice;
    bool autoSetupTried = false;    // guard so startup auto-heal runs once, not in a loop
    bool importRepairTried = false; // guard so per-job ModuleNotFoundError repair runs once
    bool starterTried = false;      // guard so the launch default-part creation runs once
    // ITEM 15: deferred engine bootstrap. When true, tryStartWorker has not yet run on the
    // interactive path — the first frames paint chrome, then the frame loop boots the kernel.
    // Shot/scenario mode still boots synchronously before the loop (deterministic captures).
    bool workerBootPending = false;
    // Human-readable stage for the left status-bar loading chip during startup/warmup
    // (e.g. "Starting engine…", "Warming up kernel…", "Opening starter part…"). Cleared
    // once the app is interactive (worker ready + starter settled / no boot pending).
    std::string startupPhase;

    // ---- job + chat model ----
    // Keyed run registry: one JobState per conversation id, so N conversations can each have
    // their own turn in flight simultaneously. A run is bound to its owning conversation at
    // launch (JobState::convId/docKey) and stays bound for its whole lifetime — the drain routes
    // every streamed event and the final result by that id, never by scanning whichever
    // conversation happens to be visible. JobState is non-copyable, so it is always held by
    // shared_ptr; see runFor/activeRun/runIfExists/anyRunActive/convRunning below (defined in
    // core/app_state.cpp).
    std::unordered_map<std::string, std::shared_ptr<JobState>> runs;
    // Get-or-create the run slot for a conversation id (creates an idle JobState on first use).
    std::shared_ptr<JobState> runFor(const std::string& convId);
    // runFor(activeConvId()) — the run the CURRENTLY VIEWED conversation acts on (Send/Stop).
    std::shared_ptr<JobState> activeRun();
    // nullptr if no run slot exists yet for convId (never creates one) — for read-only checks.
    JobState* runIfExists(const std::string& convId);
    // True if ANY conversation's run has running==true — for doc-mutation/global gates (undo,
    // file open/save, provider switch) that must never race ANY in-flight run, not just the
    // viewed one's.
    bool anyRunActive() const;
    // True if THAT SPECIFIC conversation's run is running — for Send/Stop/busy UI scoped to
    // whichever conversation is on screen, so an unrelated background run never blocks it.
    bool convRunning(const std::string& convId) const;
    // Agent edit lock (interim fix for issue #55: a cancelled human sketch/PM/drag doc_revert
    // discarded agent edits made during the session). While an agent is editing a document the
    // human's editing controls for THAT document are locked: it is locked once an agent wrote it
    // (DocTab::lastForeignWriteAt) and stays locked while any in-app run that was live at that
    // write is still running, or for kAgentLockGraceS after the last agent write (external
    // agents, writes landing as a run ends). Docs the agent never wrote stay editable. Every
    // human mutation path checks this; open human sessions on a newly locked doc exit keeping
    // their work (never doc_revert).
    static constexpr double kAgentLockGraceS = 8.0;
    bool docLockedByAgent(const std::string& docId) const;
    bool activeDocLockedByAgent() const;
    // "" when `docId` is not locked; otherwise one sentence for toasts / disabled tooltips.
    std::string agentLockReason(const std::string& docId) const;
    // Last frame on which no run was active (maintained per frame by engine_events); a foreign
    // write stamped at or after it happened during the current run(s).
    std::chrono::steady_clock::time_point runsIdleAt{};
    // The id of the currently viewed conversation, or "" if none (activeConv invalid).
    std::string activeConvId() const;
    std::vector<char> code{std::vector<char>(64 * 1024, 0)};
    std::vector<char> prompt{std::vector<char>(2048, 0)};
    std::vector<ChatMsg> chat;                 // the ACTIVE (viewed) conversation's live messages
    std::vector<Conversation> conversations;   // ALL resident sessions (document-independent);
                                               // active's msgs live in `chat`
    int activeConv = -1;                       // index into conversations; -1 = uninitialized
    std::string chatDocKey;                    // the CURRENT document's key — run launches +
                                               // prompt drafts bind to it (refreshed each frame
                                               // from convDocKey; NOT a session ownership key)
    int convCounter = 0;                       // monotonic id source (no Date/random needed)
    std::vector<char> helpQuestion{std::vector<char>(2048, 0)};
    std::string helpLastQuestion;
    std::string helpAnswer;
    std::string helpError;
    std::string helpStatus;
    bool helpBusy = false;

    // ---- current assembly (populated when a build/design returns multiple parts) ----
    std::vector<PartInfo> asmParts;
    FitReport asmFit;
    std::string asmStl;               // combined assembly STL (shown when selectedPart == -1)
    int selectedPart = -1;            // -1 = whole assembly; >=0 = index into asmParts

    // ---- model picker availability (filled by the agent_list_models probe) ----
    ProviderModels providerModels[ui::PROV_COUNT];  // indexed by ui::ProviderIndex
    std::atomic<bool> modelProbeInFlight{false};
    // Provider ids currently signed in through Chudcode, populated by the Settings poll.
    // The composer uses this to hide catalog rows for accounts the user has not connected.
    // Runtime-only (not persisted).
    std::set<std::string> signedInBridgeProviders;

    std::vector<char> chatInput{std::vector<char>(2048, 0)};
    std::string modelInfo;
    std::string livePhase;        // what the agent is doing right now (viewport overlay)
    // Async engine calls (doc_rebuild / eval_*) queued or undrained on st.rpc: while > 0 the
    // mesh on screen may lag the model. Status bar left loading chip shows detailed rebuild
    // progress (feature N/M / ops-in-flight); the old viewport "Rebuilding..." HUD is gone.
    // Bump at every st.rpc.call that mutates/evaluates the model; drop in its drain callback.
    int rebuildsInFlight = 0;
    // Activation coalescing (doc_tabs.cpp switchToTab/activateTabDocAsync): a monotonically
    // increasing generation bumped for every switch that needs an async doc_activate. While one
    // such RPC is already in flight, a newer switch does NOT queue a second one — it overwrites
    // the pending slot below; the in-flight completion dispatches the LATEST pending tab instead
    // of every intermediate one it was superseded by. Net effect: N rapid switches while one
    // activation is in flight produce at most 2 engine activations, never N. GL-thread-only
    // (touched only from switchToTab / the rpc.drain() completion callback).
    uint64_t activationGeneration = 0;
    bool activationInFlight = false;      // a doc_activate is currently in flight
    int pendingActivateTab = -1;          // -1 = no coalesced switch waiting; else docTabs index
    std::string pendingActivateDocId;     // pendingActivateTab's doc id (staleness check on drain)
    uint64_t pendingActivateGeneration = 0;
    // The async switch-in the strip is showing while its doc_activate is outstanding (see
    // switch_hold.h): ensureActiveTab must not snap the selection back to the mirror's doc.
    core::SwitchHold switchHold;
    // doc_ids with a lock-free doc_snapshot prime in flight (switchToTab). Dedup only:
    // a spectator can click the same wedged mirror tab a dozen times while the agent writes,
    // and each click re-enters switchToTab — without this every click would queue another
    // identical registry read.
    std::set<std::string> snapshotPrimeInFlight;
    // Mesh loader generation — bumped on every loadDocMeshAsync; drain drops
    // completions whose gen != meshGen_ (latest-wins; keep-last-good on screen meanwhile).
    std::atomic<uint64_t> meshGen_{0};
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
    // Reference planes (Front / Top / Right) shown faintly in the viewport. Before the user takes
    // control, viewport_panel shows each plane until the active dependency graph consumes it.
    // showPlane becomes authoritative after the FeatureManager eye or viewport menu is used;
    // planeEmphasis (0..2, -1=none) brightens one plane and dims the others.
    bool showPlane[3] = {false, false, false};
    // Per-frame EFFECTIVE origin-plane visibility after dependency auto-hide, the user's showPlane
    // override, and temporary sketch/PM pick force-show. Plane hit-tests and FeatureManager eyes use
    // this value so their state always matches what is actually drawn.
    bool planeVisEff[3] = {false, false, false};
    // Same "what the renderer actually draws" flag for the world-origin gizmo — written by the
    // viewport each frame right after it decides origin visibility (auto-hide / hiddenFeatures /
    // PM force-show). The origin PICK gates on THIS, not hiddenFeatures, so it is clickable exactly
    // when it is drawn (never through a body that auto-hid it, never in the default-hidden state).
    bool originVisEff = false;
    // True once the user explicitly takes control of the three default planes. Until then every
    // unconsumed plane is visible and every consumed plane is auto-hidden; afterward showPlane is
    // authoritative. Per-doc (parked in DocSession); reset on a fresh document.
    bool planesUserToggled = false;
    // Same story for the world-origin gizmo (issue #30): auto-show it while there's no solid body
    // (reference for the first sketch), auto-hide once a body exists — until the user explicitly
    // toggles origin visibility via the FeatureManager eye, after which hiddenFeatures is honored.
    bool originUserToggled = false;
    // Sticky escape hatch for reference-axis auto-hide (mirrors originUserToggled). Set when the
    // user toggles any axis eye in the FeatureManager. Per-id stickiness lives in
    // axesUserToggledIds — toggling one axis must NOT unlock auto-hide for other axis.auto:*.
    bool axesUserToggled = false;
    // Axis ids whose FeatureManager eye the user has explicitly toggled. Auto-hide
    // (hasSolidContent) never rewrites these; new axis.auto:* stay hidden on solid parts
    // until the user shows that specific id.
    std::unordered_set<std::string> axesUserToggledIds;
    // W2-ref: ids of every reference-axis datum currently injected (default axis.x/y/z +
    // GUI-derived plane-intersection axes). Seeded into axisDatumIds on a fresh doc and left
    // OUT of hiddenFeatures so empty/sketch-only docs show OPEN FM eyes + draw axes; auto-hide
    // (hiddenFeatures insert) kicks in with hasSolidContent unless axesUserToggled. Consulted
    // by the viewport to FORCE-SHOW every axis while a Revolve/axis PropertyManager well is
    // active, and by the FeatureManager to list them under References. Recomputed each rebuild.
    std::unordered_set<std::string> axisDatumIds;
    // Cached auto plane-intersection axes (USER planes only; reserved pairs map to axis.x/y/z).
    // Recomputed only when the user-plane set fingerprint changes — hitTest/route consume this
    // (or renderer refDatums) and never re-solve plane pairs.
    struct AutoAxisCache {
        std::string id;    // axis.auto:<a>|<b> sorted
        std::string name;
        float origin[3];
        float dir[3];
    };
    std::vector<AutoAxisCache> axisAutoCache;
    std::string axisInjectPlaneFp;  // fingerprint of user planes that built axisAutoCache
    // Tree display-name cache: id -> display name, rebuilt when axisDatumIds membership changes.
    std::unordered_map<std::string, std::string> axisDisplayNames;
    // W2-ref: one-shot Zoom-to-Fit after open/new. Legacy global flag still set by open paths;
    // prefer fitPendingDocIds (per-doc) so a part opened while another tab is focused still fits
    // the first time THAT doc is actually drawn with a known model radius. Consumed once; never
    // re-fits on rebuilds or later tab switches.
    bool fitViewOnNextMeshCommit = false;
    // Per-doc pending first-display Zoom-to-Fit. Keyed by worker doc id. Set on file open
    // (alongside fitViewOnNextMeshCommit); consumed by the viewport when that doc is the
    // active drawn doc AND has a mesh with modelRadius > 0.
    std::unordered_set<std::string> fitPendingDocIds;
    int  planeEmphasis = -1;
    // C25 W3-J: viewport display/visibility state.
    int  displayStyle  = 1;          // mirrors DisplayStyle enum; 0=Shaded 1=ShadedEdges(default) 2=HLR 3=HLV 4=Wire
    bool orthographic = true;        // live projection mode; mirrors Settings::orthographic (viewport/renderer apply each frame)
    // Appearance ribbon "Appearance..." button -> one-shot request the viewport's color/material
    // picker for the whole model (body "b1"). Consumed (and cleared) by viewport_panel each frame.
    bool requestAppearanceEditor = false;
    bool showTriad        = true;    // orientation triad, bottom-left of viewport
    bool showGroundShadow = true;    // soft contact-shadow blob
    bool showRealView     = true;    // C30-C: procedural environment reflections (RealView sheen)
    // Viewport left-drag mode: 0=rotate (default), 1=pan.
    int  vpDragMode = 0;
    // One-shot flag: set by mouse_gestures right-click-without-drag hook; consumed by viewport_panel.
    bool wantContextMenu = false;
    bool layoutInit = false, resetLayout = false, openSettings = false;
    bool showViewport = true;
    // C24: the right task pane (Design + Agent Activity tabs, SolidWorks-style). View > Task Pane.
    bool showTaskPane = true;
    bool focusHelpTab = false;    // one-shot: focus the AI pane's Help tab
    bool focusSettingsTab = false; // one-shot: focus the AI pane's Settings tab
    // Consolidated AI panel (Design / Activity / Console as internal tabs). One dock window "AI".
    bool showAIPanel = true;
    bool showSessionsPicker = false;  // Copilot: hide chat log, show the Sessions list/dropdown
    bool showAISettingsOverlay = false;  // Copilot: hide chat log, show compact AI settings (gear toggle; mutually exclusive with showSessionsPicker)
    bool showTaskList = false;   // Copilot: hide chat log, show the task/plan list (logo-left button)
    bool showArtifacts = false;  // Copilot: hide chat log, show Session Artifacts (logo-right button)
    // Design Table panel (SolidWorks Design Table). Dockable "Design Table" window in the right
    // task pane; View > Design Table toggles it. An editable grid (rows=configs, columns=driven
    // params + per-feature suppression) over the same config_* RPCs the Configurations panel reads.
    bool showDesignTablePanel = false;
    // Weldment Cut List panel (SolidWorks Weldment Cut List). Dockable "Cut List" window in the
    // right task pane; View > Cut List toggles it. Only meaningful for part docs with weldments.
    bool showCutListPanel = false;
    // Equations / Global Variables editor (SolidWorks "Equations" dialog). A modeless window over
    // doc.equations.globals; opened by double-clicking the FeatureManager "Equations" node or the
    // View > Equations menu toggle. Add/edit -> eq_set, delete -> eq_delete (drawn in equations_dialog.cpp).
    bool showEquations = false;
    // Copilot composer mode (dropdown). Mirrors Settings::copilotMode for live UI;
    // both are kept in sync on change + at first composer frame after loadSettings.
    // 0=Agent 1=Plan 2=Swarm 3=Edit 4=Ask  (see CopilotMode)
    int  aiInteraction = 0;
    bool focusActivityTab = false;  // one-shot: select the Activity tab (e.g. when a v2 run starts)
    bool showAbout = false, designStarted = false;
    int frame = 0;

    // ---- ribbon (C14) ----
    // Wave 1A: named ribbon-tab indices — MUST match the order of kRibbonTabs[] in
    // widgets/ribbon.cpp. Replaces the magic numbers that were scattered across the host (the PCB
    // tab was being set to 5 in one place, which landed on "Sheet Metal").
    enum RibbonTab {
        RT_Features   = 0, RT_Sketch     = 1, RT_Evaluate   = 2, RT_Simulation = 3,
        RT_Assembly   = 4, RT_SheetMetal = 5, RT_Weldments  = 6, RT_PCB        = 7,
        RT_Surfaces   = 8,
        // W17 drawing overhaul: contextual tabs shown ONLY while a Drawing doc is active
        // (they replace the mechanical tabs, SolidWorks-style). See ribbon_tabVisible().
        RT_ViewLayout = 9, RT_Annotation = 10,
        // Appearance workspace — per-body color / RealView material / display style (3D docs).
        RT_Appearance = 11,
        // Manufacture workspace — CAM toolpaths / post / material-removal sim (3D docs).
        RT_Manufacture = 12,
        // ECAD split: kRibbonTabs[] index 7 is "PCB Design" and index 20 is "Schematic"
        // (appended there to keep the sim per-type tab indices 13..19 stable). A Schematic
        // doc tab MUST select RT_Schematic — selecting RT_PCB for both kinds was why a new
        // schematic surfaced board commands.
        RT_Schematic = 20,
        // Topology optimization study tab (Simulate workspace only). Appended after Schematic so
        // every existing index stays stable; kRibbonTabs[21] in widgets/ribbon.cpp.
        RT_Topology = 21,
    };
    bool showRibbon = true;        // View > Ribbon visibility toggle
    bool profilerRunning = false;  // Evaluate > Profiler: sampling profiler active (libchudware prof_*)
    bool showProfilerWindow = false;  // in-app profiler results window (text report + flamegraph)
    bool showCommandSearch  = false;  // top-bar unified search popup (settings + modeling tools)
    bool showWeb            = false;  // "Web" title-bar chip: full-window in-app CEF browser overlay
    int  openSettingsTab    = -1;     // >=0: jump the Settings modal to this tab index (search result)
    int  ribbonTab  = RT_Features; // active ribbon tab (one of RibbonTab); contextual on the doc
    bool showSimulationTools = false; // View-gated advanced module; hidden from default ribbon
    bool showPcbTools = false;        // View-gated ECAD module; hidden until requested/used
    bool showFeatureTree = true;   // View > FeatureManager (C16)
    // Wave 1D: SW-faithful tabbed LEFT pane — Explorer / FeatureManager / PropertyManager /
    // Configurations as switchable tabs in one dock window. The PropertyManager auto-selects its
    // tab while a command form is active; closing the form returns to the FeatureManager tab.
    enum LeftPaneTab { LP_Explorer = 0, LP_FeatureManager = 1, LP_PropertyManager = 2,
                       LP_Configurations = 3, LP_Simulation = 4 };
    int leftPaneTab = LP_FeatureManager;
    // One-shot: the Simulate workspace's left rail switches to its Simulation Manager tab next
    // frame (it otherwise remembers the user's Explorer/Simulation choice). Set by commands that
    // open a study so the study is actually visible; layout.cpp consumes and clears it.
    bool revealSimManager = false;

    // ---- PropertyManager (C17) — occupies the tree dock slot while a command is active ----
    // W1-B: typed pointer (forward-declared above) so app_state.h stays free of the heavy
    // ui/property_manager.h include while every borrow site drops its static_cast.
    // Multi-document: these three point at the ACTIVE document's per-session instances (re-seated on
    // every tab switch by reseatActiveSubsystems). They fall back to the run_gui-owned singletons
    // (fallback* below) only at the empty start state / on a non-CAD tab. Every access site keeps
    // using st.propertyManager / sketchMode / evaluateDialogs byte-identically; only the pointee
    // changes per active document.
    ui::PropertyManager* propertyManager = nullptr;   // active doc's feature-edit form (per DocSession)
    // Fallback singletons owned by run_gui, used when no CAD document is active (empty start state /
    // Drawing / ECAD tab). Set once in run_gui; never null after startup.
    ui::PropertyManager* fallbackPropertyManager = nullptr;
    ui::SketchMode*      fallbackSketchMode      = nullptr;
    // 3D sketcher (sketch3d features). Single app-wide instance owned by run_gui (NOT per
    // DocSession — it guards its doc id and auto-exits on a doc switch). Never null after startup.
    ui::Sketch3DMode*    sketch3dMode            = nullptr;
    ui::EvaluateDialogs* fallbackEvaluate        = nullptr;
    // Configurations left-pane tab (SolidWorks ConfigurationManager). The live ConfigPanel is owned
    // by run_gui and borrowed here so ui::drawLeftPane's Configurations case can render it
    // (ConfigPanel::drawEmbedded). Null until run_gui wires it.
    ui::ConfigPanel* configPanel = nullptr;           // owned by run_gui

    // ---- C19: simulation + result-overlay UI state ----
    core::SimClient* simClient = nullptr;  // owned by run_gui (single solve worker, shared)
    // The active document's SimManager (per DocSession, re-seated on switch), or the run_gui fallback
    // singleton at the empty start state / a non-CAD tab. Studies live in the document and persist in
    // the .step+; the tree + setup forms render in the left pane's Simulation tab.
    ui::SimManager*  simManager = nullptr;
    ui::SimManager*  fallbackSimManager = nullptr;  // owned by run_gui; used when no CAD doc is active
    bool colormapOn = false;           // result colormap toggle (mirrors renderer state)
    bool docMeshHasScalar = false;     // the loaded overlay carries a per-vertex scalar
    bool docMeshHasDisp = false;       // the loaded overlay carries a displacement field
    float dispScaleHint = 1.0f;        // suggested deform multiplier from the last overlay
    std::string legendTitle, legendUnits;  // legend widget labels for the active scalar
    bool legendOn = false;             // draw the viewport legend overlay
    float legendMin = 0.0f, legendMax = 1.0f;
    // Sim result rework: which named scalar chunk the overlay colormaps (von_mises / p1 / p3 /
    // max_shear / displacement_mag / disp_x|y|z / fos). loadDocMesh prefers this field by name so the
    // user can switch result fields with NO re-solve (just a renderOnly re-load of the cached overlay).
    std::string activeScalarField = "von_mises";
    // Sim display-unit mode: 0 = follow the document units (st.ips()), 1 = force SI (MPa·N·mm),
    // 2 = force US customary (psi·lbf·in). Engine stays canonical SI; this only affects display/input.
    int simUnits = 0;
    bool wantFea = false;       // C19: CHUDWARE_SHOT=fea scripted static-study scenario
    bool feaShotDone = false;   // guard so the fea scenario runs exactly once
    // CHUDWARE_SHOT=simdisc_mech|simdisc_ecad — Simulate-tab disclosure-matrix shots.
    // mech: part only -> the "+ New Study" picker must show NO electronics options.
    // ecad: PCB tab + part -> electronics simulation + firmware emulation rows appear.
    bool wantSimDisc = false;
    int  simDiscKind = 0;          // 0 = mech-only, 1 = with a PCB open
    bool simDiscShotDone = false;
    bool wantCam = false;       // CHUDWARE_SHOT=cam: build a part, run the copilot CAM fast-path,
    bool camShotDone = false;   //   adopt the job into the Manufacture panel — runs exactly once
    bool wantFlow = false;      // CHUDWARE_SHOT=flow scripted external-flow (wind-tunnel) study scenario
    bool flowShotDone = false;  // guard so the flow scenario runs exactly once
    bool wantDrawing = false;   // W17: CHUDWARE_SHOT=drawing scripted engineering-drawing scenario
    bool drawingShotDone = false;  // guard so the drawing scenario runs exactly once
    bool wantEvalMass = false;  // C19: CHUDWARE_SHOT=evalmass mass-properties window
    bool evalMassShotDone = false;
    bool wantIconPipe = false;  // CHUDWARE_SHOT=iconpipe — recipe-driven clean-viewport icon render
    bool iconPipeShotDone = false;  // guard: keeps the frame loop alive until every recipe is captured

    // ---- SketchMode (C18) — in-sketch tools/overlay; typed like the PropertyManager (W1-B) ----
    ui::SketchMode* sketchMode = nullptr;  // active doc's sketch (per DocSession; re-seated on switch)
    bool wantSketch = false;           // CHUDWARE_SHOT=sketch scripted flow
    bool sketchShotDone = false;       // guard so the sketch scenario runs once per shot
    bool wantSketch3d = false;         // CHUDWARE_SHOT=sketch3d scripted 3D-sketch flow
    bool sketch3dShotDone = false;     // guard so the sketch3d scenario runs once per shot

    // ---- EvaluateDialogs (C19) — Measure / Mass / Section windows; per-document like the PM ----
    // Re-seated on tab switch so a measurement window/log on one part is independent of another's —
    // "measure one part while a feature-edit form is open on another" (the multi-document headline).
    ui::EvaluateDialogs* evaluateDialogs = nullptr;  // active doc's evaluate dialogs (per DocSession)

    // ---- AsmPanels (C20b) — assembly windows/forms; typed like the PropertyManager (W1-B) ----
    ui::AsmPanels* asmPanels = nullptr;  // owned by run_gui
    bool wantAsm = false;              // CHUDWARE_SHOT=asm scripted assembly flow
    bool asmShotDone = false;          // guard so the asm scenario runs once per shot

    // ---- C22b: prompt editor + orchestrator wiring ----
    bool agentV2 = true;                    // "Use orchestrator (v2)" checkbox in Design panel
    std::string pendingPromptRole;          // when non-empty, Settings > Agents tab opens on this role
    // Live status line for the v2 orchestrator run, shown in the chat area
    std::string agentV2Status;              // e.g. "PLAN > planner attempt 1..."
    bool agentV2Running = false;            // a v2 (job-based) run is in flight somewhere —
                                             // global/cosmetic (viewport badge, Activity panel,
                                             // doc-mutation guards); NOT a concurrency lock. See
                                             // AppState::anyRunActive()/convRunning() for the real
                                             // per-conversation/global run-state checks.
    // Live context-meter telemetry. `lastContextTokens` is the most recent agent call's INPUT
    // token count (how full the model's context is right now); `lastContextWindow` is the
    // window the SERVER reported for the model that actually ran, which beats any client-side
    // guess — the harness resolves it from the model catalog (services/agent/config.py sets
    // provider.context_window), while the GUI can only pattern-match the model id. 0 means the
    // server has not said yet, and the composer falls back to effectiveContextWindow().
    // Both are fed from `agent.context` events and reset at run start.
    int lastContextTokens = 0;
    int lastContextWindow = 0;
    // --- end C22b ---

    // ---- Stop + message queue (modern agentic composer) ----
    // Stop is per-run now (JobState::stopRequested) so stopping the VIEWED conversation's turn
    // never touches another conversation's still-streaming run. See AppState::activeRun().
    // queuedPrompts: messages the user sent WHILE a turn was active (Claude-Code-style queue).
    // Drained one-at-a-time by the composer once the assistant is free again.
    std::vector<std::string> queuedPrompts;
    std::vector<nlohmann::json> queuedPromptDrafts;
    // pendingAttachments (M23): absolute file paths attached via the composer paperclip. Shown
    // as chips above the input; they ride the NEXT send (submitTurn consumes + clears them —
    // small text files are inlined into the request, every path travels in the RPC payload's
    // "attachments" field).
    std::vector<std::string> pendingAttachments;
    // Stable numbered roster for composer image references. Empty slots are detached images;
    // never erase/reindex a slot while later draft markers still refer to it.
    std::vector<std::string> pendingInlineImagePaths;
    // pendingContextRefs (Wave4): model entities the user attached to the NEXT prompt via the
    // composer "context cube" button — parts/components, faces, edges, sketches, features picked
    // from the viewport or the feature tree (ui::addContextRef). Shown as chips above the input
    // beside file attachments; consumed + cleared by submitTurn, which serializes them into the
    // RPC payload's "context_refs" array (the worker resolves each against the engine) and copies
    // them onto the sent bubble.
    std::vector<core::ContextRef> pendingContextRefs;
    // While true the composer "context cube" is armed: viewport picks and feature-tree row clicks
    // append to pendingContextRefs instead of (or alongside) normal selection. Esc / re-click
    // disarms. Mirrors a PropertyManager selection well but routes into the chat context store.
    bool contextPickActive = false;

    // ---- C25: ribbon dispatch hook (set in app.cpp after the ribbon callback is wired) ----
    // Allows UI widgets (shortcut bar, gesture wheel) to fire ribbon commands without
    // knowing about the full dispatcher capture.
    std::function<void(const char*)> ribbonDispatch;

    // ---- screenshot-mode flags (set from CHUDWARE_SHOT in run_gui) ----
    bool wantSettings = false, wantChat = false, wantAssembly = false, wantPlan = false;
    bool wantCopilotSlash = false;  // CHUDWARE_SHOT=copilot_slash: slash-menu regression (menu + fill)
    bool wantPick   = false;  // C15: load box.aicm, simulate a center pick, print + screenshot
    bool wantRibbon = false;  // C14: ribbon screenshot mode
    bool pickPrinted = false;  // guard so the "pick:" stdout line prints exactly once
    bool wantM1 = false;       // C17: CHUDWARE_SHOT=m1 scripted integration scenario
    bool m1ShotDone = false;   // guard so the m1 scenario runs exactly once per shot
    bool wantMultidoc = false;     // CHUDWARE_SHOT=multidoc scripted multi-document mid-edit scenario
    bool multidocShotDone = false; // guard so the multidoc scenario runs exactly once per shot
    // CHUDWARE_SHOT=tabperf: open 2 parts and hammer A<->B tab switches for logTabPerf timings.
    bool wantTabPerf = false;
    bool tabPerfShotDone = false;
    // [workspace-tabs seam] CHUDWARE_SHOT=workspaces: cycle all 8 workspace tabs, one screenshot each.
    bool wantWorkspace = false;     // CHUDWARE_SHOT=workspaces: workspace cycle scenario
    bool workspaceShotDone = false; // guard so the workspaces scenario runs exactly once
    // CHUDWARE_SHOT=motion: kinematic motion study end-to-end (2-body assembly + angle mate + playback).
    bool wantMotion = false;        // CHUDWARE_SHOT=motion scripted motion study scenario
    bool motionShotDone = false;    // guard so the motion scenario runs exactly once
    // H2: CHUDWARE_SHOT=daemon_h2 — kill hostd mid-session, capture the sticky
    // "Engine daemon unreachable" chip, respawn hostd, capture repopulated tabs.
    bool wantDaemonH2 = false;
    int  daemonH2Phase = 0;     // 0=seed 1=killed 2=notice 3=respawned 4=done
    double daemonH2PhaseT = 0;  // ImGui time when the current phase began
    int  daemonH2CaptureFrame = 0;  // absolute frame to take the final screenshot (0=unset)

    // ---- W1-B: toast notification queue (rendered by ui::drawToasts in app.cpp) ----
    // C39: guarded by toastsMtx — the worker errorSink posts toasts from background threads
    // (jobs.cpp agent threads, the AsyncRpc dispatch thread) while drawToasts reads on the
    // GL thread. toast() and drawToasts() both take the lock.
    std::mutex toastsMtx;
    std::vector<ui::Toast> toasts;
    // CHUDWARE_SHOT=ribbon suppresses toasts (the Chudware on-load toast otherwise
    // occludes leftmost ribbon groups in per-tab icon-verification captures); real usage is
    // unaffected since toasts still auto-dismiss there. See app.cpp wantRibbon setup.
    bool suppressToasts = false;

    // ---- W1-B: per-frame cursor request (consumed by ui::applyCursorRequest at end of frame) ----
    ui::CursorKind cursorRequest = ui::CursorKind::Default;
    bool imguiCursorSuspended = false;  // tracks whether we toggled NoMouseCursorChange last frame

    // ---- Wave 4C: copy/paste clipboard (FeatureManager + sketch entities) ----
    // Single source of truth for Ctrl+C/X/V across the FeatureManager and the sketch editor; also
    // driven by the "edit_copy" / "edit_cut" / "edit_paste" dispatcher ids the menu bar fires. Holds
    // the last copied payload as parsed JSON + a tag for what kind it is, so the matching paste path
    // (paste_feature vs sketch_paste_entities) knows how to consume it. Cross-doc aware: srcDocId
    // records the source doc (the feature-bundle engine ops resolve external refs against the target
    // doc, so a paste into a DIFFERENT doc just works — the host only passes the bundle on).
    //   - Feature: `payloads` holds one `aicad.feature_bundle/1` per copied feature (copy_feature's
    //     id_map["$bundle"]); multi-select copy stores several, pasted in order.
    //   - SketchEntities: `payload` is an entities bundle {entities:[...], constraints:[...]} and
    //     copyOriginU/copyOriginV is the sketch-coord anchor used to compute the paste offset.
    struct Clipboard {
        enum class Kind { None, Feature, SketchEntities };
        Kind kind = Kind::None;
        std::string srcDocId;                 // doc the copy came from ("" = unknown / same doc)
        std::vector<nlohmann::json> payloads; // Feature: one bundle per copied feature
        nlohmann::json payload;               // SketchEntities: {entities, constraints}
        float copyOriginU = 0.0f, copyOriginV = 0.0f;  // SketchEntities: paste-offset anchor
        bool empty() const { return kind == Kind::None; }
        void clear() {
            kind = Kind::None; srcDocId.clear(); payloads.clear();
            payload = nlohmann::json(); copyOriginU = copyOriginV = 0.0f;
        }
    };
    Clipboard clipboard;

    // ---- File Explorer + project recents -------------------------------------
    // workspaceDir is the Explorer root. It defaults to Documents/Chudware,
    // is created on first use, and persists as "workspace_dir".
    std::string workspaceDir;
    // The top-left working-directory selector is the project authority for PLM,
    // Explorer and chat. An open document's source folder never changes it.
    std::string plmProjectPath() const {
        return workspaceDir;
    }
    // Recent files and project folders are persisted independently. Both are
    // most-recent-first, de-duplicated case-insensitively, and bounded.
    static constexpr size_t kRecentFilesMax = 10;
    std::vector<std::string> recentFiles;
    static constexpr size_t kRecentProjectsMax = 8;
    std::vector<std::string> recentProjects;
    // One-shot request flags fired by the File menu (title_bar.cpp) and consumed in app.cpp's key/
    // request pump on the GL thread — so the native file dialog opens with our window active and the
    // open/save reuses feature_tree's helpers (which own s_docPath + the mesh refresh).
    bool requestOpenDialog = false;   // File > Open... / Ctrl+O (and Import...)
    bool requestSave       = false;   // File > Save / Ctrl+S
    bool requestSaveAs     = false;   // File > Save As... / Ctrl+Shift+S
    std::string requestOpenPath;      // File > Recent <path> (open this exact path next pump)
    // Wave-5-fill: File > Export <fmt> sets requestExportFormat to one of the export_file format
    // strings ("step"|"iges"|"brep"|"stl"|"obj"|"ply"|"3mf"|"gltf"|"glb"|"dxf"|"dwg"); the
    // GL-thread pump drains it (native Save-As + DocMirror::exportFile) so the dialog never
    // re-enters the ImGui menu walk. File > Save All sets requestSaveAll; the pump iterates
    // docTabs and saves each saveable doc.
    std::string requestExportFormat;  // File > Export <fmt> (export the active doc next pump)
    bool requestSaveAll = false;      // File > Save All (save every saveable doc next pump)

    // Mesh-export tessellation dialog. File > Export of a TESSELLATED format (stl/obj/ply/3mf/
    // gltf/glb, and dxf/dwg whose free non-analytic curves are tessellated) opens this modal first
    // (instead of going straight to requestExportFormat) so the user picks a quality preset /
    // fine-tunes deflection before the Save-As. On confirm, the chosen options are stashed here and
    // requestExportFormat is set for the GL-thread pump to consume. B-rep formats (step/iges/brep)
    // bypass the dialog.
    struct ExportTessDialog {
        bool open = false;            // modal visible this frame
        std::string fmt;              // pending mesh format
        int preset = 1;              // 0 Coarse, 1 Standard, 2 Fine, 3 Custom
        bool advanced = false;        // reveal raw deflection / STL container controls
        float linDeflection = 0.05f;  // mm (or fraction when relative)
        float angDeflectionDeg = 20.0f;  // degrees (converted to radians on send)
        bool relative = false;        // relative-to-size linear deflection
        bool ascii = false;           // STL ASCII vs binary
        // Live estimate state.
        bool estimateDirty = true;    // recompute the triangle/vertex estimate next draw
        bool estimateValid = false;
        long long estTris = 0, estVerts = 0;
    } exportTess;
    bool wantNewDocChooser = false;   // doc-strip "+" / Ctrl+N → open the tiled "Create New" chooser
    // Unsaved-changes guard on app exit. The window-close (title-bar X / Alt+F4 / File > Quit) is
    // intercepted in run_gui: if any tab is dirty, the close is cancelled and wantQuitConfirm raises
    // the exit confirmation modal. quitConfirmed latches true once the user picks Save & Quit or
    // Quit-without-saving so the next close request falls straight through to the real exit.
    bool wantQuitConfirm = false;     // raise the "unsaved changes on exit" modal next frame
    bool quitConfirmed   = false;     // user approved the quit (after saving or discarding)
    // Agent-work quit guard: raised by the same close intercept when a copilot run is in flight.
    // The modal offers "Continue in background" (the run is daemon-side and survives the GUI) or
    // "Stop agent & quit" (agent_cancel). This prompt comes FIRST; the dirty-doc prompt above
    // follows if any user-owned docs are also unsaved.
    bool wantQuitAgentConfirm = false; // raise the "agent work in progress" modal next frame
    bool quitAgentContinues   = false; // user chose Continue-in-background (diagnostic only)
    // Orphan recovery (daemon mode): set at bootstrap when the daemon reports retired docs from a
    // previous session awaiting explicit recovery (recovery_list). >0 raises the "Recover unsaved
    // parts?" modal next frame; the modal consumes it. "Later" just dismisses — the daemon keeps
    // the entries, so the prompt returns on the next app start.
    int recoveryPromptCount = 0;      // raise the orphan-recovery modal next frame (0 = none)
    bool recoveryModalOpen = false;   // the orphan-recovery modal is currently showing (promoted
                                      // from a run_gui static so ensureStarterDoc can defer the
                                      // blank starter while the user is choosing recover/discard).
    // Web only: the shared daemon has connected AND its first bootstrap (doc-list mirror +
    // orphan-recovery check) has run. workerStarted is set synthetically at boot to unlock the UI,
    // so it is NOT a safe signal that daemon RPCs will succeed — gate boot-time daemon actions
    // (the starter part) on this instead to avoid transient "worker not started" failures.
    bool daemonEverConnected = false;
    // De-dup, cap, and persist recently opened files/project folders.
    void pushRecentFile(const std::string& path);
    void pushRecentProject(const std::string& path);

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

    // True while the Simulate WORKSPACE is active. Modeling tools (ribbon commands + Instant3D
    // push-pull/drag) lock out in this state so the model can't change shape out from under a study's
    // fixtures/loads/results — SolidWorks behaves the same in a study.
    // NB: gate on activeWorkspace, NOT leftPaneTab. leftPaneTab==LP_Simulation persists after the user
    // clicks away to another workspace tab (title_bar only flips activeWorkspace), so keying off it
    // left the model locked — and the "leave the Simulation tab" toast firing — on Model/Manufacture/
    // Drawing/etc. activeWorkspace is what the workspace tabs actually set, so the lock releases the
    // instant you leave Simulate.
    bool inSimulation() const { return activeWorkspace == Workspace::Simulate; }

    // Load an AICM tessellation file into the id pass + overlays (and clear the selection).
    // Requires a current GL context and st.idPass/st.overlays to be set. Returns false on
    // parse/GL failure (err set); on success sets hasDocMesh. (C15; the worker that produces
    // these per-rebuild files is wired in C16.)
    //
    // renderOnly: update ONLY the shaded renderer (setMeshes), leaving the id-pass / overlays /
    // pick caches untouched. Used by the blend (fillet/chamfer) live preview to DISPLAY the rounded
    // result while picking + highlighting still resolve against the un-rounded base body that the
    // session holds (suppressed). So the user sees the round form but every edge pick is exact.
    // cacheAsModel=false: a temporary full simulation overlay keeps scalar/displacement fields
    // without replacing the session's editable model cache; its GPU identity is not the part.
    bool loadDocMesh(const std::string& aicmPath, std::string& err, bool renderOnly = false,
                     bool cacheAsModel = true);
    // Restore a session's immutable mirror + parsed tessellation and bind its cached renderer
    // buffers. Rebuilds only the independent pick/overlay buffers; no worker or disk round-trip.
    bool bindCachedDocMesh(DocSession& session, std::string& err);

    // Non-blocking mesh load. Disk parse + pick-cache build run on a dedicated loader
    // thread; GL upload happens inside drainMeshLoads() (call once per frame after rpc.drain()).
    // meshGen drops stale completions when a newer load is queued. Keep-last-good is free: the
    // previous mesh stays on screen until a current completion commits. Safe to call from GL
    // thread; no-ops empty paths.
    void loadDocMeshAsync(const std::string& aicmPath, bool renderOnly = false,
                          bool cacheAsModel = true);
    // Parse and upload an inactive document's pushed tessellation into its per-tab CPU/GPU cache
    // without disturbing the visible document. Newer revisions supersede queued older ones.
    void cacheBackgroundDocMeshAsync(const std::string& docId, int revision,
                                     const std::string& aicmPath);
    void drainMeshLoads();   // GL thread, once per frame
    // Join the async mesh-loader thread (call once on run_gui teardown before exit).
    void stopMeshLoader();
    uint64_t meshGen() const { return meshGen_.load(); }

    // Drop shaded mesh + pick GPU buffers when the active doc has no tessellation (empty model /
    // sketch-only). Does NOT clear selection, body visibility, or other per-doc fields. Cold/changed
    // switches call this after parking the leaving document's GPU buffers so stale geometry is never
    // shown under the entering tab while its asynchronous rebuild is queued.
    void unloadDocMesh();

    // C34: reset the viewport to the fresh-document state — drops the legacy/demo mesh and any
    // previous doc's parts so the empty-doc datum-planes view shows. Called by every path that
    // creates a new document (tree toolbar New Part/Assembly, sketch auto-create).
    void clearViewportForNewDoc();

    // Clear ALL selection kinds in one call: viewport topology picks (+ hover/pickStatus), the
    // FeatureManager primary + multi-select + face highlight, and any in-sketch entity/relation
    // selection. Used by the global "click empty space to deselect" handler and ESC / Clear Selection.
    // Does NOT touch body visibility, model readouts, or other per-doc fields.
    void clearSelection();

    // Build the "Face 12 of Body1" status label for a pick (empty string for an invalid hit).
    std::string pickLabel(const render::PickHit& h) const;

    // W1-B (item 4): the single undo/redo entry point. Both the FeatureManager toolbar buttons and
    // the global Ctrl+Z / Ctrl+Y keys route through here, so behavior stays identical. Performs the
    // DocMirror mutation, refreshes the viewport mesh + owner map on success, logs, and posts a
    // brief confirmation toast ("Undo: ok" / "Undo: nothing to undo"). Returns the response so the
    // caller can branch further if it wants. No-op (returns a no_doc response) when no doc is open,
    // when the active tab is Drawing/ECAD (DocMirror still holds the previous part/asm), or when
    // the live mirror has not yet rebound to the active tab after a switch.
    core::OpsResponse docUndo();
    core::OpsResponse docRedo();
};

// Select one active project folder for PLM, Explorer, and the built-in agent.
// Persists the folder + recent-project list, lands on the Project Hub,
// invalidates its caches, and records outside edits best-effort. Empty is a
// cancelled-dialog no-op.
void setActiveProjectDir(AppState& st, const std::string& absDir);
