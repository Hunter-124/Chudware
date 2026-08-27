// Chudware Copilot: ONE chat implementation, two host layouts.
//
// The docked host is a PINNED, full-height, rounded liquid-glass column overlaid on the
// viewport's right edge (~12dp margins; the only docked layout — no docked fallback). Slim
// header (the live animated tesseract brand mark, tinted by run status, its animation style
// chosen in Settings -> Appearance -> Brand mark, + status + Sessions), conversation log with
// a per-turn transparent activity timeline (what the AI is doing / thinking, expandable) whose
// cards are translucent
// adaptive glass bubbles over the live viewport backdrop (GlassGroup::Copilot, host contract
// H5), and a bottom composer (prelude: compact Agent/Plan/Swarm/Edit/Ask dropdown + context
// meter; transparent prompt with in-box send; graduated segmented toolbar: model biggest,
// thinking, New, paperclip+cube icons smallest — overlapping chips, drop-up model picker)
// painted over the same glass surface. The AI settings live in an inline view
// (Agents|Prompts|Skills|Context|Permissions|Providers).
//
// The full-page AI workspace (host/src/ui/ai/ai_workspace.cpp) embeds the SAME sessions /
// messages / composer state and send path through the reuse hooks at the bottom of this
// header — overlay pane + full-page workspace are two layouts over one implementation.
//
// Implementation is split by concern under host/src/ui/copilot/ (was the legacy_chat.cpp
// monolith): ui_common (glass/bubble toolkit), chat_log, activity_timeline, cards, feed,
// conversations, settings, composer, panel (the docked-pane host + reuse hooks).
#pragma once

#include <string>
#include <vector>

#include "../model_registry.h"  // ui::ModelInfo — providerPickerModels() rows

struct AppState;

namespace ui {

// Draw the AI Copilot pane (header + conversation + bottom composer). `busy` whether a job is
// running, `canRun` whether new jobs can be launched.
void drawAIPanel(AppState& state, bool busy, bool canRun);

// Shared AI settings fields (provider/model/auth, generation). Rendered by the Settings modal's
// Providers tab as glass-grouped sections — one call per section, each inside its own glass
// card, so the caller owns the headers. Privacy (offline mode) + assistant-thoughts live in the
// AI Harness "Session knobs" grid now, and per-model visibility moved to Settings -> Providers
// (per-provider model checkboxes) — so neither is a shared section anymore.
// Returns true if a setting changed this frame (caller may persist).
enum class AiSettingsSection {
    ProviderModel = 0,  // provider, model, sign-in / API key + status, vision
    Generation,         // reasoning effort, max response tokens, context window
};
bool drawAiSettingsSection(AppState& state, bool busy, AiSettingsSection section);

// Free AI (FreeLLMAPI) provider card — always reachable from Settings -> Providers regardless
// of OMP bridge availability (that tab is otherwise entirely bridge-catalog driven and has no
// row for a native, non-bridge provider). Renders active-editing or dormant-key-park UI
// depending on whether Free AI is the current Settings::provider. Returns true if a field
// changed (dormant-slot edits + the switch button persist immediately either way).
bool drawFreeAiProviderCard(AppState& state);

// Probe model availability (agent_list_models) into state.providerModels: always the LOCAL
// endpoint, plus the ACTIVE provider when it is a cloud one (so a newly released cloud model
// shows up without a catalog edit). Throttled PER PROVIDER; pass force=true to skip the
// cooldown (base-URL edit, provider switch). Never blocks — one async probe at a time.
void refreshModelAvailability(AppState& state, bool force);

// True when the provider has a key, signed-in subscription, host-shared account (web),
// or reachable local endpoint — shared by the composer model picker, Settings role/Eng.Firm
// combos, and Manage models.
bool providerUsable(AppState& state, int provider);

// The model rows the picker should show for ONE provider: that provider's live probe rows when
// a probe succeeded (order preserved — newest first), else the static builtinModels() catalog
// filtered to it. Cloud providers only; local/llama.cpp keep their own flat `models` path.
std::vector<ModelInfo> providerPickerModels(AppState& state, int provider);

// ---- OMP bridge-backed model catalog + session cost (S-UI-PROV) -------------------
// Static OMP catalog metadata (capabilities + cost) for bridge-routed providers, fetched via
// the libchudware `agent_models_list` C-ABI (bridge `models_list`). Distinct from the live
// agent_list_models probe above: that one asks a keyed endpoint what it can serve right now;
// this is the static catalog the model picker uses for ctx/cost/capability sublabels and the
// Providers-tab Models section. Degrades silently (empty catalog) when the bridge method is
// absent — every consumer must tolerate a nullptr/empty result.
struct BridgeModelMeta {
    std::string id;          // OMP catalog model id (e.g. "claude-opus-4-8")
    std::string name;        // display name
    std::string provider;    // OMP provider id ("anthropic", "kimi-code", "moonshot", ...)
    bool vision = false;
    bool tools = false;
    bool reasoning = false;
    std::vector<std::string> reasoningEfforts;  // OMP catalog effort ladder; empty = no dial
    int contextWindow = 0;   // tokens; 0 = unknown
    int maxTokens = 0;       // max output tokens; 0 = unknown
    // `priced` is the bridge's pricing PROVENANCE flag (models_list "priced"): true only when
    // the OMP catalog carries a real non-zero price. `hasCost` merely means the row had a cost
    // object at all — which is always, even when every field is zero. So !priced means "no
    // price data", NOT "free": never render a dollar figure derived from an unpriced row.
    bool priced = false;
    bool hasCost = false;
    double costIn = 0, costOut = 0, costCacheRead = 0, costCacheWrite = 0;  // $/Mtok
    // Release/lifecycle metadata (populated by refreshBridgeModelCatalog from the model id/name;
    // the OMP catalog itself carries no date). Drives per-provider newest-first sort + the
    // "older / deprecated" grouping in the Providers tab.
    std::string releaseDate;    // "YYYY-MM-DD" inferred from the id/name; "" when unknown
    long releaseSort = 0;       // YYYYMMDD int key for sorting; 0 = unknown (sorts oldest)
    bool deprecated = false;    // superseded/legacy family (inferred) -> grouped, hidden by default
};
// Fetch the full catalog (all providers) into the process cache; ~5-min TTL, async.
void refreshBridgeModelCatalog(AppState& state, bool force);
// The cached catalog (empty until the first successful fetch).
const std::vector<BridgeModelMeta>& bridgeModelCatalog();
// Exact (provider, id) lookup; providerId empty = first catalog row with that id.
const BridgeModelMeta* bridgeModelMeta(const std::string& providerId, const std::string& modelId);
// CAD picker lookup: map a CAD ProviderIndex (+ cloud base_url for the shared cloud slot) to
// the OMP provider id, then find the model; falls back to a global id match. nullptr = absent.
const BridgeModelMeta* bridgeModelMetaForCad(int cadProvider, const std::string& modelId,
                                             const char* cloudBaseUrl);
// True for OMP provider ids that are genuinely-local runtimes CAD talks to unauthenticated
// (the bridge's KEYLESS_PROVIDERS set): their catalog rows carry no price because inference on
// your own hardware really is $0, so "free" is the honest label there. Exact id match — the
// hosted "ollama-cloud" subscription is NOT in this set. Everywhere else an absent price means
// "no price data", and no dollar figure may be derived from it.
bool bridgeProviderIsLocalFree(const std::string& providerId);
// OMP model-browser.ts display formats: "ctx 200k \xc2\xb7 $3/$15 \xc2\xb7 tools vision" row
// sublabel, and the cache-pricing detail line for tooltips. Unpriced non-local rows render
// "no price data" rather than a fabricated "free".
std::string bridgeModelSublabel(const BridgeModelMeta& m);
std::string bridgeModelCostDetail(const BridgeModelMeta& m);
// Per-session cost strip (agent_cost_summary, a pure $/Mtok calculator fed with the session's
// completed-turn token events): "Session $1.87 \xc2\xb7 142k tok" + click popover with the
// per-model breakdown. When some model that spent tokens has no catalog price the total is
// shown as a floor ("Session \xe2\x89\xa5 $1.87 \xc2\xb7 3 of 4 models priced") instead of a
// complete figure. Hidden while the bridge method is unavailable. Emits into the CURRENT
// window; drawn by the composer model dropdown footer and the Settings->Providers footer.
void drawSessionCostStrip(AppState& state);

// Cloud OpenAI-compat / Moonshot key presence: local settings OR host-shared keys on web
// (GET /api/providers). Used by picker sections that gate rows on cloudKey/moonshotKey.
bool cloudCompatKeyPresent(AppState& state);
bool moonshotKeyPresent(AppState& state);

// Session-local "Show assistant thoughts" toggle (Copilot quick settings + Settings).
// Not persisted — lives for the process only.
bool& assistantThoughtsVisible();

// agent.max_subagent_depth in config.json (0–4). Shared by quick settings + Harness.
int  loadMaxSubagentDepth();
void saveMaxSubagentDepth(int depth);

// agent.subagent_tool_call_budget in config.json (0–64). 0 derives it from the token grant.
int  loadSubagentToolCallBudget();
void saveSubagentToolCallBudget(int budget);

// Persist the active document's conversations (the active one synced from state.chat) to
// %APPDATA%/Chudware/chat_history.json. Safe to call any time; no-op if nothing to save.
void persistChatHistory(AppState& state);

// Debounced mid-turn flush of the active session's chat history (resume-safe: streaming
// assistant text is written to disk ~1/s WHILE a turn runs, not only at completion).
// `force` bypasses the timer. Call once per frame from whichever chat host is on screen —
// the docked Copilot column OR the AI-tab workspace (which does not draw that column).
void maybePersistChatHistoryDebounced(AppState& state, bool force);

// Per-frame: if a chat-triggered accurate (OpenFOAM) CFD launched on the SimClient channel has
// finished, post its Cd/Cl result onto the running chat bubble. No-op when nothing is pending.
// Call once per frame from the main loop (alongside pollAiCadHandoff).
void pollChatOpenFoam(AppState& state);

// ---- AI workspace hub reuse (host/src/ui/ai/ai_workspace.cpp) --------------------------
// Thin exported wrappers over the pane's internals so the full-page AI workspace renders the
// SAME sessions/messages/composer state (st.chat / st.conversations / st.prompt / send path)
// as the docked Copilot — zero duplicated chat logic. All emit widgets into the CURRENT
// window/child; none Begin() a window of their own.
void chatWorkspaceSync(AppState& st);                         // bind conversations to the active doc
bool chatTurnActive(AppState& st);                            // an assistant turn is streaming
void chatDrawLog(AppState& st);                               // the active conversation's turns
// Stick-to-bottom autoscroll for the chat scroll region — call right after chatDrawLog inside
// the scrolling child. Pins the view to the tail while streaming unless the user scrolled up.
// regionId scopes the stick state to ONE surface: several chat surfaces can render the same
// conversation in the same frame, and they must not share (and fight over) one flag.
void chatLogStickToBottom(const char* regionId);
void chatLogResetStick();  // re-engage autoscroll everywhere (conversation switch)
// True while the region is at (or pinned to) the tail — false once the user scrolls up.
bool chatLogAtBottom(const char* regionId);
// Re-engage stick and snap to the tail on the region's next chatLogStickToBottom call (the
// scroll offset can only be set from INSIDE the scrolling child, so the jump is deferred).
void chatLogJumpToBottom(const char* regionId);
// Floating circular down-arrow "jump to latest" button, drawn OVER a chat viewport's
// bottom-right corner. Call AFTER the scrolling child's EndChild() with the child's screen
// rect (parent-window drawlist, so it never scrolls with the content). Hidden while
// chatLogAtBottom(regionId); clicking re-engages stick-to-bottom.
void chatDrawJumpToLatest(const char* regionId, const ImVec2& viewMin, const ImVec2& viewMax);
void chatDrawComposer(AppState& st, bool busy, bool canRun);  // prelude + input + toolbar (real send path)
void chatDrawSessionsList(AppState& st, bool busy);           // session rows + "+ New session"
// Same authoritative phased Todo/plan renderer used by the docked Copilot Tasks view.
// Exported for the Code-mode AI workspace so it never grows a second task UI.
void chatDrawTasksList(AppState& st, bool busy);

}  // namespace ui
