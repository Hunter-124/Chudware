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
#include <set>
#include <string>
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
class ConfigPanel;
class SketchMode;
class Sketch3DMode;   // 3D free-space sketcher (sketch3d features) — app-wide singleton
class SimManager;
class AsmPanels;
class ActivityPanel;
class EvaluateDialogs;  // Measure / Mass Properties / Section dialogs — owned per-document (DocSession)
// Wave 2 (GUI rework): the per-document ECAD editor state (mirror + canvas + selection + edit
// buffers) lives on each Schematic/PCB DocTab. Its full definition stays in ecad_panel.cpp (it
// owns EcadMirror/EcadCanvas); AppState only needs an opaque pointer so app_state.h stays light.
struct EcadEditorState;
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

// ---------------- settings (AI provider) -> %APPDATA%/AI-CAD/config.json ----------------
struct Settings {
    int provider = 4;  // 0=Anthropic 1=OpenAI 2=Local OpenAI-compat 3=managed llama.cpp 4=Gemini.
                       // Default to Google Gemini (see kSeededGeminiKey) for out-of-box copilot.
    char model[512] = "gemini-2.5-flash";  // flash: reliable free-tier quota for multi-call runs
    char apiKey[256] = "";        // active-provider key (local OpenAI-compat optional key)
    // Per-provider keys so the picker can hide providers with no key while keeping each key
    // around when another provider is active. Serialized under config.json "keys".
    char geminiKey[256] = "";
    char anthropicKey[256] = "";
    char openaiKey[256] = "";
    char cloudKey[256] = "";       // cloud OpenAI-compatible API key (DashScope / DeepSeek / etc.)
    char reasoningEffort[16] = "high";  // off|low|medium|high — only used by reasoning models
    char baseUrl[256] = "http://localhost:1234/v1";
    bool vision = true;
    bool offlineMode = false;    // C35: local-only mode — refuse any non-loopback provider (ITAR)
    // Run mode (composer chip): planMode=true is "Supervised" (draft a build plan + ask the
    // build-critical clarify questions before modeling); planMode=false is "Autopilot" (model
    // straight from the prompt, no plan/clarify). Persisted as "plan_mode".
    bool planMode = true;
    int maxIters = 0;            // refine-pass cap per turn; 0 = automatic
    int maxTokens = 0;           // max output tokens per model call; 0 = automatic (per-provider
                                 // default). Raise this if a reasoning model returns "no JSON ops"
                                 // / "budget exhausted" — it spent its output budget thinking.
    int contextWindow = 0;       // model's total token context window; 0 = automatic (per-provider
                                 // default: cloud large, local 8192). Input + output are fitted to
                                 // this so a small LOCAL model never overruns it and breaks the chain.
    std::string systemPrompt;    // system prompt override; empty = built-in
    std::string theme = "dark";  // "dark" | "light" — persisted to config.json
    Units units = Units::MMGS;   // document display/input unit system — persisted to config.json
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
    // Wave 3 (GUI rework): File Explorer workspace root + Recent files, persisted to config.json
    // ("workspace_dir" / "recent_files"). Mirrored onto AppState at startup; AppState is the live
    // copy the UI reads, Settings is what saveSettings serializes.
    std::string workspaceDir;                  // "" = use the Documents/AI-CAD default
    std::vector<std::string> recentFiles;      // most-recent-first, capped at AppState::kRecentFilesMax
    // Chat model-picker visibility: ids the user has explicitly hidden (VS-Code-Copilot-style
    // "Manage models" toggle in Settings → AI). Default-empty = every available model shows;
    // hiding is opt-out so the picker works out of the box. Serialized as "hidden_models".
    std::set<std::string> hiddenModels;
    // Bulk provider on/off: category keys the user has disabled wholesale ("gemini", "anthropic",
    // "openai", "openrouter", "alibaba", "deepseek", "local"). A disabled category's models are
    // hidden from the composer's chat picker and shown collapsed/greyed in Settings → Manage models.
    // Serialized as "disabled_providers".
    std::set<std::string> disabledProviders;

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

    // T-G21: vertical-collapse state of the pinned FeatureManager / Copilot glass columns — a
    // collapsed column (rolled up to its header strip) stays collapsed across restarts. Serialized
    // "fm_collapsed" / "copilot_collapsed".
    bool fmCollapsed = false;
    bool copilotCollapsed = false;
};

std::string appdataDir();
// The app data dir itself (base + "/AI-CAD" or "/Billet" per the alias rule); logs once.
std::string appdataAppDir();
// Default Documents workspace dir (Documents/AI-CAD if it exists, else Documents/Billet).
std::string defaultWorkspaceDir();
std::string configPath();
void loadSettings(Settings& s);
void saveSettings(const Settings& s);

// The Gemini API key shipped as the out-of-box default (user-provided). Seeded into
// config.json on first run when no Gemini key is configured, so the copilot works
// immediately. Users can replace it in Settings → AI.
extern const char* kSeededGeminiKey;

// One provider's live availability, filled by the agent_list_models probe. Lets the model
// picker hide providers that are down / unkeyed and list the local endpoint's loaded models.
struct ProviderModels {
    bool checked = false;             // probe returned at least once
    bool ok = false;                  // reachable + (cloud) keyed
    std::vector<std::string> models;  // all discovered ids (local: every downloaded model)
    // LM Studio's native endpoint reports which models are actually loaded (vs merely
    // downloaded). When loadedKnown is true, only ids in `loaded` can serve a request and the
    // picker hides the rest; when false (non-LM-Studio server) every listed model is assumed
    // available. See agent_list_models in libaicad.cpp.
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
    double value = 0;  // overlap mm^3 (interference) or gap mm (clearance)
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
// The DocMirror deliberately stays SINGLE on AppState (not per-session): it is already re-pointed at
// the target worker doc via DocMirror::activate() on every switch, and is only ever mutated on behalf
// of the active document — so one re-pointed mirror is correct, and its (single) listener always
// drives the active doc's tree/mesh repaint. This mirrors the per-document precedent already set by
// ECAD tabs (DocTab::ecadState, a shared_ptr to per-document editor state).
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
    std::string                modelInfo;            // (DocSession parked copy — see AppState field)
    std::vector<PartInfo>      asmParts;
    FitReport                  asmFit;
    std::string                asmStl;
    int                        selectedPart = -1;
    std::string                selectedFeatureId;   // FeatureManager primary selection
    bool                       treeScrollToSelection = false;
    std::vector<uint64_t>      featureHighlightFaces;
    bool                       sketchPlanePickActive = false;

    // Fast-switch mesh cache (scales tab switching to big assemblies). Set on every switch-in to the
    // doc's last-loaded AICM path + the revision it was at; on the NEXT switch-in DocMirror::
    // activateCached reuses them (skipping the forced re-tessellating doc_rebuild) when the worker's
    // revision is unchanged — a backgrounded doc can't be edited, so it usually is. -1 = no cache yet.
    std::string                cachedAicmPath;
    int                        cachedRevision = -1;

    DocSession();
    ~DocSession();
    DocSession(const DocSession&) = delete;
    DocSession& operator=(const DocSession&) = delete;
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
    // Mission orchestrator: a multi-capability run (design -> CFD -> verdict). isMission marks the
    // job so the drain uses missionVerdict (the report stage's prose) as the final chat bubble
    // instead of the generic design summary; createdDocPath still carries any assembly tab to open.
    bool isMission = false;
    std::string missionVerdict;       // the report/verdict prose to show as the assistant reply
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

// ---------------- pre-flight clarify gate ----------------
// Before a Design run, agent_clarify_v1 returns the build-critical questions (or ready=true).
// One ClarifyQ per question; `answer` is the editable buffer shown in the inline clarify card,
// seeded with `suggested` so the user can accept a default with one click.
struct ClarifyQ {
    std::string key, ask, unit, suggested;
    std::vector<char> answer{std::vector<char>(256, 0)};
};
struct ClarifyState {
    bool awaiting = false;   // clarify RPC in flight — the turn shows "working out what I need..."
    bool active = false;     // questions are shown; waiting for the user to fill them + Build
    std::string request;     // the original user request, launched once answered/skipped
    std::vector<ClarifyQ> questions;
    std::vector<std::string> assumptions;
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
    // ---- typed transparency rows (M12-M14, aicad.agent_events/2) ----
    // 0 = orchestrator stage row (everything above, unchanged default);
    // 1 = context injection chip (agent.context_injected): title=label, reason=source,
    //     ctxTokens=token_est (M12);
    // 2 = model thought box (agent.thought / agent.thought.delta): detail=text, streaming while
    //     deltas arrive, collapsed-by-default once complete (M13);
    // 3 = tool call row (agent.tool_call): title=tool_name, reason=target ("what it acts on"),
    //     durationMs=latency_ms, status=done|fail from `ok` (M14).
    int rowKind = 0;
    int ctxTokens = 0;      // rowKind 1: best-effort injected-token estimate (kept out of the
                            // turn's usage footer — it is not model I/O)
    bool streaming = false; // rowKind 2: agent.thought.delta chunks still arriving; finalized
                            // (and auto-collapsed) by the matching completed agent.thought
};

struct ChatMsg {
    bool fromUser = false;
    std::string text;              // user prompt, or the assistant's final summary
    bool running = false;          // assistant turn still streaming
    int errKind = ERR_NONE;        // ERR_CAD = generated code didn't build (calm, amber);
                                   // ERR_SYSTEM = app/runtime/provider failure (red)
    std::string liveStatus;        // transient "Iteration N: building..." while running
    PlanInfo plan;                 // the build plan drafted before the code (plan mode)
    std::vector<ActivityItem> activity;  // transparent "what the AI is doing" timeline (orchestrator)
    int curMissionTask = -1;       // index of the mission task currently running (-1 = not a mission);
                                   // namespaces sub-run stage keys so per-task stages don't collide
    std::vector<AgentStep> steps;  // assistant turn: the iteration trace
    ClarifyState clarify;          // pre-flight clarify gate (Design mode) — see ClarifyState
    // ---- M19 render-windowing cache (GUI-only, not persisted) ----
    // Height this message occupied last time it was actually drawn at uiCachedW width. Lets the
    // chat log emit a same-height Dummy for messages far outside the scroll viewport instead of
    // rendering them (endless scroll-back without unbounded frame cost). -1 = never measured.
    float uiCachedH = -1.0f;
    float uiCachedW = -1.0f;
};

// Fold one streamed orchestrator event (kind = stage_started / agent_output / stage_result /
// run_finished; data = the event's "data" object) into a turn's activity timeline. Called on the
// GL thread from the event-drain loop. No-op for events it doesn't recognize.
void feedTurnActivity(ChatMsg& m, const std::string& kind, const nlohmann::json& data);

// One copilot conversation = its own tab. The ACTIVE conversation's live messages stay in
// AppState::chat (so every existing st.chat user keeps working unchanged); an INACTIVE
// conversation parks its messages here. Switching tabs swaps chat <-> msgs. Persisted per
// document to %APPDATA%/AI-CAD/chat_history.json so conversations survive restarts.
struct Conversation {
    std::string id;
    std::string title;             // shown on the tab; defaults to "Chat N", renamed to 1st prompt
    std::vector<ChatMsg> msgs;      // populated only while INACTIVE (active lives in AppState::chat)
    int lastContextTokens = 0;     // per-session context meter (input tokens from last model call)
    std::vector<std::string> queuedPrompts;  // queued while this session's turn was active
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
void drawPlanCard(const PlanInfo& plan, const std::vector<ActivityItem>& activity,
                  const ui::Fonts& fonts);

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
    bool treeShotDone = false; // guard so doc smoke runs exactly once
    bool wantEquations = false;   // AI_CAD_SHOT=equations: open the Equations editor + seed two globals
    bool equationsShotDone = false; // guard so the equations seed runs exactly once
    bool wantPartsShot = false;   // AI_CAD_SHOT=parts: Parts Library with a seeded no-model result (RE-32)

    // ---- Wave N: AI-ECAD document (worker ecad_* RPCs; the editor mirror lives in ecad_panel.cpp) ----
    std::string ecadDocId;          // open ECAD doc id ("" = none)
    int         ecadRevision = -1;  // last known ECAD revision (optimistic-concurrency base)
    bool        wantEcad = false;   // AI_CAD_SHOT=ecad scripted scenario
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

        // Live MCP handoff mirror: this tab shows the part the external AI-CAD MCP engine is
        // building (pollAiCadHandoff opens it from %APPDATA%\AI-CAD\handoff\live.step+ and refreshes
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
    enum class Workspace { Model, Drawing, Simulate, Electronics, Manufacture, PLM, PartsLibrary, AI, ReverseEng };
    Workspace activeWorkspace = Workspace::Model;
    // Parts Library → Reverse Engineering handoff seam. The parts panel's
    // "Reverse-engineer this part" action creates a session (reveng_start_session
    // RPC), sets this to the new session_id, and switches activeWorkspace to
    // ReverseEng. The RE workspace (RE-30) consumes + clears it on its next draw to
    // auto-select that session. Additive + optional: even unread, the RE tab's
    // session list surfaces the new session, so the handoff still works.
    std::string pendingRevengSessionId;
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
    // Push bodyVisible to the renderer (per-slot draw skip) and the id pass (pick skip). Call
    // after any toggle; loadDocMesh calls it after every setMeshes/setDoc.
    void applyBodyVisibility();
    // Push hiddenFeatures to the renderer so hidden DATUM features (ref axis/plane/point/csys) drop
    // out of the reference-overlay draw immediately. Call after an eye toggle, a tab switch, and
    // every datum rebuild (the datum geometry is otherwise re-pushed only on a document mutation).
    void applyDatumVisibility();

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
    bool starterTried = false;      // guard so the launch default-part creation runs once

    // ---- job + chat model ----
    JobState job;
    std::vector<char> code{std::vector<char>(64 * 1024, 0)};
    std::vector<char> prompt{std::vector<char>(2048, 0)};
    std::vector<ChatMsg> chat;                 // the ACTIVE conversation's live messages
    std::vector<Conversation> conversations;   // all conversation tabs (active's msgs live in `chat`)
    int activeConv = -1;                       // index into conversations; -1 = uninitialized
    std::string chatDocKey;                    // doc key that conversations/chat currently belong to
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
    ProviderModels providerModels[6];        // indexed by ui::ProviderIndex (PROV_COUNT)
    std::atomic<bool> modelProbeInFlight{false};

    std::vector<char> chatInput{std::vector<char>(2048, 0)};
    std::string modelInfo;
    std::string livePhase;        // what the agent is doing right now (viewport overlay)
    // Async engine calls (doc_rebuild / eval_*) queued or undrained on st.rpc: while > 0 the
    // mesh on screen may lag the model, so the viewport draws a small "Rebuilding..." chip.
    // Bump at every st.rpc.call that mutates/evaluates the model; drop in its drain callback.
    int rebuildsInFlight = 0;
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
    // True once the user has explicitly toggled origin-plane visibility (eye / FeatureManager row).
    // Until then, an empty part (no solid body) auto-shows all three planes (SolidWorks "pick a plane
    // to start"); after the user takes control, showPlane is authoritative so a hide actually sticks
    // even with no body yet. Per-doc (parked in DocSession); reset on a fresh doc.
    bool planesUserToggled = false;
    int  planeEmphasis = -1;
    // C25 W3-J: viewport display/visibility state.
    int  displayStyle  = 1;          // mirrors DisplayStyle enum; 0=Shaded 1=ShadedEdges(default) 2=HLR 3=HLV 4=Wire
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
    int  aiInteraction = 0;       // 0=Design (new part) 1=Edit (orchestrator v2) 2=Chat
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
    };
    bool showRibbon = true;        // View > Ribbon visibility toggle
    bool profilerRunning = false;  // Evaluate > Profiler: sampling profiler active (libaicad prof_*)
    bool showProfilerWindow = false;  // in-app profiler results window (text report + flamegraph)
    bool showCommandSearch  = false;  // top-bar unified search popup (settings + modeling tools)
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
    bool wantFea = false;       // C19: AI_CAD_SHOT=fea scripted static-study scenario
    bool feaShotDone = false;   // guard so the fea scenario runs exactly once
    bool wantCam = false;       // AI_CAD_SHOT=cam: build a part, run the copilot CAM fast-path,
    bool camShotDone = false;   //   adopt the job into the Manufacture panel — runs exactly once
    bool wantFlow = false;      // AI_CAD_SHOT=flow scripted external-flow (wind-tunnel) study scenario
    bool flowShotDone = false;  // guard so the flow scenario runs exactly once
    bool wantDrawing = false;   // W17: AI_CAD_SHOT=drawing scripted engineering-drawing scenario
    bool drawingShotDone = false;  // guard so the drawing scenario runs exactly once
    bool wantEvalMass = false;  // C19: AI_CAD_SHOT=evalmass mass-properties window
    bool evalMassShotDone = false;
    bool wantIconPipe = false;  // T-G15: AI_CAD_SHOT=iconpipe — recipe-driven clean-viewport icon render
    bool iconPipeShotDone = false;  // guard: keeps the frame loop alive until every recipe is captured

    // ---- SketchMode (C18) — in-sketch tools/overlay; typed like the PropertyManager (W1-B) ----
    ui::SketchMode* sketchMode = nullptr;  // active doc's sketch (per DocSession; re-seated on switch)
    bool wantSketch = false;           // AI_CAD_SHOT=sketch scripted flow
    bool sketchShotDone = false;       // guard so the sketch scenario runs once per shot
    bool wantSketch3d = false;         // AI_CAD_SHOT=sketch3d scripted 3D-sketch flow
    bool sketch3dShotDone = false;     // guard so the sketch3d scenario runs once per shot

    // ---- EvaluateDialogs (C19) — Measure / Mass / Section windows; per-document like the PM ----
    // Re-seated on tab switch so a measurement window/log on one part is independent of another's —
    // "measure one part while a feature-edit form is open on another" (the multi-document headline).
    ui::EvaluateDialogs* evaluateDialogs = nullptr;  // active doc's evaluate dialogs (per DocSession)

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
    // Chat-triggered accurate (OpenFOAM) CFD: launched on the SimClient channel (non-blocking) and
    // fulfilled by ui::pollChatOpenFoam each frame (it can't use the serialised st.rpc for a
    // minutes-long solve). chatCfdVel/Aoa carry the parsed condition for the result message.
    bool chatCfdPending = false;
    double chatCfdVel = 10.0;
    double chatCfdAoa = 0.0;
    // Live context-meter telemetry: the most recent agent call's INPUT token estimate (how full the
    // model's context is right now) — fed from agent_output events, reset at run start. The composer
    // shows it vs the active model's window like the Claude Code / Codex / VS Code context meters.
    int lastContextTokens = 0;
    // --- end C22b ---

    // ---- Stop + message queue (modern agentic composer) ----
    // stopRequested: the user hit Stop on an in-flight JOB run. The worker can't be interrupted
    // mid-RPC, so the background thread finishes, but the GL-thread drain (app.cpp) DISCARDS the
    // result instead of applying it (no chat/doc/tab updates). Cleared by the drain.
    bool stopRequested = false;
    // queuedPrompts: messages the user sent WHILE a turn was active (Claude-Code-style queue).
    // Drained one-at-a-time by the composer once the assistant is free again.
    std::vector<std::string> queuedPrompts;
    // pendingAttachments (M23): absolute file paths attached via the composer paperclip. Shown
    // as chips above the input; they ride the NEXT send (submitTurn consumes + clears them —
    // small text files are inlined into the request, every path travels in the RPC payload's
    // "attachments" field).
    std::vector<std::string> pendingAttachments;

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
    bool wantMultidoc = false;     // AI_CAD_SHOT=multidoc scripted multi-document mid-edit scenario
    bool multidocShotDone = false; // guard so the multidoc scenario runs exactly once per shot
    bool wantActivity = false;  // C22b: AI_CAD_SHOT=activity scripted orchestrator run + activity panel
    bool activityShotDone = false; // guard so the activity scenario runs exactly once
    // [workspace-tabs seam] AI_CAD_SHOT=workspaces: cycle all 8 workspace tabs, one screenshot each.
    bool wantWorkspace = false;     // AI_CAD_SHOT=workspaces: workspace cycle scenario
    bool workspaceShotDone = false; // guard so the workspaces scenario runs exactly once
    // AI_CAD_SHOT=motion: kinematic motion study end-to-end (2-body assembly + angle mate + playback).
    bool wantMotion = false;        // AI_CAD_SHOT=motion scripted motion study scenario
    bool motionShotDone = false;    // guard so the motion scenario runs exactly once

    // ---- W1-B: toast notification queue (rendered by ui::drawToasts in app.cpp) ----
    // C39: guarded by toastsMtx — the worker errorSink posts toasts from background threads
    // (jobs.cpp agent threads, the AsyncRpc dispatch thread) while drawToasts reads on the
    // GL thread. toast() and drawToasts() both take the lock.
    std::mutex toastsMtx;
    std::vector<ui::Toast> toasts;
    // T-BL31 NEW-3: AI_CAD_SHOT=ribbon suppresses toasts (the Billet on-load toast otherwise
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

    // ---- Wave 3 (GUI rework): File Explorer + Recent files (APPEND-ONLY) ----
    // workspaceDir: the root directory the Explorer tab browses. Defaults to Documents/AI-CAD
    // (created on first use); overridable and persisted to config.json ("workspace_dir").
    std::string workspaceDir;
    // plmProjectDir: the folder the PLM Project Hub (Workspace::PLM) treats as the current project
    // (a project = a folder + its hidden vault, contract plm.md §1). "" = derive it: the parent
    // folder of the active saved document, else workspaceDir. Set explicitly by "Open project
    // folder…" in the hub, and implicitly followed as documents are opened/saved. Read via
    // plmProjectPath() — every plm_* RPC passes {"path": st.plmProjectPath()}.
    std::string plmProjectDir;
    std::string plmProjectPath() const {
        if (!plmProjectDir.empty()) return plmProjectDir;
        // Prefer the active tab, but never the read-only MCP handoff mirror
        // (its folder is %APPDATA%\...\handoff — not a user project). Fall back
        // to the first real saved doc, then the workspace folder.
        auto folderOf = [](const std::string& p) -> std::string {
            size_t s = p.find_last_of("/\\");
            return s == std::string::npos ? std::string() : p.substr(0, s);
        };
        if (activeTab >= 0 && activeTab < (int)docTabs.size()) {
            const auto& t = docTabs[activeTab];
            if (!t.aiHandoff && !t.path.empty()) {
                std::string f = folderOf(t.path);
                if (!f.empty()) return f;
            }
        }
        for (const auto& t : docTabs) {
            if (t.aiHandoff || t.path.empty()) continue;
            std::string f = folderOf(t.path);
            if (!f.empty()) return f;
        }
        return workspaceDir;
    }
    // recentFiles: most-recent-first list of absolute paths opened/saved, capped at kRecentFilesMax.
    // Persisted to config.json ("recent_files"); shown under File > Recent Files and the Explorer
    // "Recent" section. pushRecentFile() de-dups + caps + persists.
    static constexpr size_t kRecentFilesMax = 10;
    std::vector<std::string> recentFiles;
    // One-shot request flags fired by the File menu (title_bar.cpp) and consumed in app.cpp's key/
    // request pump on the GL thread — so the native file dialog opens with our window active and the
    // open/save reuses feature_tree's helpers (which own s_docPath + the mesh refresh).
    bool requestOpenDialog = false;   // File > Open... / Ctrl+O (and Import...)
    bool requestSave       = false;   // File > Save / Ctrl+S
    bool requestSaveAs     = false;   // File > Save As... / Ctrl+Shift+S
    std::string requestOpenPath;      // File > Recent <path> (open this exact path next pump)
    // Wave-5-fill: File > Export <fmt> sets requestExportFormat to one of the export_file format
    // strings ("step"|"iges"|"stl"|"3mf"|"brep"); the GL-thread pump drains it (native Save-As +
    // DocMirror::exportFile) so the dialog never re-enters the ImGui menu walk. File > Save All sets
    // requestSaveAll; the pump iterates docTabs and saves each saveable doc.
    std::string requestExportFormat;  // File > Export <fmt> (export the active doc next pump)
    bool requestSaveAll = false;      // File > Save All (save every saveable doc next pump)

    // Mesh-export tessellation dialog. File > Export of a MESH format (stl/obj/ply/3mf/
    // gltf/glb) opens this modal first (instead of going straight to requestExportFormat)
    // so the user picks a quality preset / fine-tunes deflection before the Save-As. On
    // confirm, the chosen options are stashed here and requestExportFormat is set for the
    // GL-thread pump to consume. B-rep formats (step/iges/brep) bypass the dialog.
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
    // De-dup + cap (most-recent-first) + persist a path into recentFiles. Defined in app_state.cpp.
    void pushRecentFile(const std::string& path);

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

    // True while the Simulation left-pane tab is the active workspace. Modeling tools (ribbon
    // commands + Instant3D push-pull/drag) lock out in this state so the model can't change shape
    // out from under a study's fixtures/loads/results — SolidWorks behaves the same in a study.
    bool inSimulation() const { return leftPaneTab == LP_Simulation; }

    // Load an AICM tessellation file into the id pass + overlays (and clear the selection).
    // Requires a current GL context and st.idPass/st.overlays to be set. Returns false on
    // parse/GL failure (err set); on success sets hasDocMesh. (C15; the worker that produces
    // these per-rebuild files is wired in C16.)
    //
    // renderOnly: update ONLY the shaded renderer (setMeshes), leaving the id-pass / overlays /
    // pick caches untouched. Used by the blend (fillet/chamfer) live preview to DISPLAY the rounded
    // result while picking + highlighting still resolve against the un-rounded base body that the
    // session holds (suppressed). So the user sees the round form but every edge pick is exact.
    bool loadDocMesh(const std::string& aicmPath, std::string& err, bool renderOnly = false);

    // Drop shaded mesh + pick GPU buffers when the active doc has no tessellation (empty model /
    // sketch-only). Does NOT clear selection, body visibility, or other per-doc fields — used on
    // tab switch when activateCached returns an empty aicm_path so the leaving tab's mesh cannot
    // linger on screen.
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
    // caller can branch further if it wants. No-op (returns a no_doc response) when no doc is open.
    core::OpsResponse docUndo();
    core::OpsResponse docRedo();
};
