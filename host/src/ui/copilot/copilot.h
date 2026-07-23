// Chudware Copilot: ONE chat implementation, two host layouts.
//
// The docked host is a PINNED, full-height, rounded liquid-glass column overlaid on the
// viewport's right edge (~12dp margins; the only docked layout — no docked fallback). Slim
// header (outline cube + status + Sessions), conversation log with a per-turn transparent
// activity timeline (what the AI is doing / thinking, expandable) whose cards are translucent
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

struct AppState;

namespace ui {

// Draw the AI Copilot pane (header + conversation + bottom composer). `busy` whether a job is
// running, `canRun` whether new jobs can be launched.
void drawAIPanel(AppState& state, bool busy, bool canRun);

// Shared AI settings fields (provider/model/auth, generation, privacy, manage-models).
// Rendered by the Settings modal's AI tab as glass-grouped sections, and composed by
// drawProviderModelPicker for any single-call site that still wants the whole form.
// Returns true if a setting changed this frame (caller may persist).
enum class AiSettingsSection {
    ProviderModel = 0,  // provider, model, sign-in / API key + status, vision
    Generation,         // reasoning effort, max response tokens, context window
    Privacy,            // offline mode, thoughts
    ManageModels,       // hide/show models in the chat picker
    All,                // full form (Provider + Generation + ManageModels + Privacy)
};
bool drawAiSettingsSection(AppState& state, bool busy, AiSettingsSection section);

// Full form: Provider + Generation + (Manage models unless slim) + Privacy.
// `slim` drops Manage models (legacy flag kept for any compact caller).
bool drawProviderModelPicker(AppState& state, bool busy, bool slim = false);

// Probe local-endpoint model availability (agent_list_models) into state.providerModels.
// Throttled internally; pass force=true to probe immediately (e.g. on a base-URL change).
void refreshModelAvailability(AppState& state, bool force);

// True when the provider has a key, signed-in subscription, host-shared account (web),
// or reachable local endpoint — shared by the composer model picker, Settings role/Eng.Firm
// combos, and Manage models.
bool providerUsable(AppState& state, int provider);

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
    int contextWindow = 0;   // tokens; 0 = unknown
    int maxTokens = 0;       // max output tokens; 0 = unknown
    bool hasCost = false;
    double costIn = 0, costOut = 0, costCacheRead = 0, costCacheWrite = 0;  // $/Mtok
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
// OMP model-browser.ts display formats: "ctx 200k \xc2\xb7 $3/$15 \xc2\xb7 tools vision" row
// sublabel, and the cache-pricing detail line for tooltips.
std::string bridgeModelSublabel(const BridgeModelMeta& m);
std::string bridgeModelCostDetail(const BridgeModelMeta& m);
// Per-session cost strip (agent_cost_summary, a pure $/Mtok calculator fed with the session's
// completed-turn token events): "Session $1.87 \xc2\xb7 142k tok" + click popover with the
// per-model breakdown. Hidden while the bridge method is unavailable. Emits into the CURRENT
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

// Persist the active document's conversations (the active one synced from state.chat) to
// %APPDATA%/Chudware/chat_history.json. Safe to call any time; no-op if nothing to save.
void persistChatHistory(AppState& state);

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
void chatLogStickToBottom();
void chatDrawComposer(AppState& st, bool busy, bool canRun);  // prelude + input + toolbar (real send path)
void chatDrawSessionsList(AppState& st, bool busy);           // session rows + "+ New session"

}  // namespace ui
