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
struct ChatMsg;

namespace ui {

// Draw the AI Copilot pane (header + conversation + bottom composer). `busy` whether a job is
// running, `canRun` whether new jobs can be launched.
void drawAIPanel(AppState& state, bool busy, bool canRun);

// Stop a named conversation without selecting it. Dispatches on the non-blocking control lane;
// the run remains live until the worker unwinds. Used by document-lock takeover chrome.
void requestStopRunFor(AppState& state, const std::string& convId);

// Recover legacy text-only steer delivery from an exact, unambiguous resolved ask reply
// in the same transcript turn. Never infer delivery for missing/ambiguous replies or images.
bool reconcileResolvedAskReplies(std::vector<ChatMsg>& msgs);

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

// Free AI (FreeLLMAPI) provider card — always reachable from Settings -> Providers even when
// the hosted catalog is unavailable. Renders active-editing or dormant-key-park UI depending
// on whether Free AI is the current Settings::provider. Returns true if a field changed
// (dormant-slot edits + the switch button persist immediately either way).
bool drawFreeAiProviderCard(AppState& state);

// Refresh model discovery without blocking: desktop probes the LOCAL endpoint and
// ACTIVE cloud provider via agent_list_models, throttled per provider; the browser
// uses the tenant's credential-free chudcode_models_list catalog instead. force=true
// skips the cooldown after a base-URL edit or provider switch.
void refreshModelAvailability(AppState& state, bool force);

// True when the provider has a key, signed-in subscription, host-shared account (web),
// or reachable local endpoint — shared by the composer model picker, Settings role/Eng.Firm
// combos, and Manage models.
bool providerUsable(AppState& state, int provider);

// The model rows the picker should show for ONE provider: that provider's live probe rows when
// a probe succeeded (order preserved — newest first), else the static builtinModels() catalog
// filtered to it. Cloud providers only; local/llama.cpp keep their own flat `models` path.
std::vector<ModelInfo> providerPickerModels(AppState& state, int provider);

// ---- Chudcode model catalog + session cost ------------------------------------------
// Cached catalog metadata (capabilities + cost) for online providers. Distinct from the
// native agent_list_models probe above, which asks a keyed endpoint what it can serve now.
// Consumers must tolerate an empty cache while Chudcode catalog data is unavailable.
struct BridgeModelMeta {
    std::string id;          // catalog model id (e.g. "claude-opus-4-8")
    std::string name;        // display name
    std::string provider;    // provider id ("anthropic", "kimi-code", "moonshot", ...)
    bool vision = false;
    bool tools = false;
    bool reasoning = false;
    std::vector<std::string> reasoningEfforts;  // provider effort ladder; empty = no dial
    int contextWindow = 0;   // tokens; 0 = unknown
    int maxTokens = 0;       // max output tokens; 0 = unknown
    // `priced` records pricing provenance: true only when the catalog carries a real non-zero
    // price. `hasCost` merely means the row had a cost object, which may contain only zeroes.
    // Thus !priced means "no price data", not "free".
    bool priced = false;
    bool hasCost = false;
    double costIn = 0, costOut = 0, costCacheRead = 0, costCacheWrite = 0;  // $/Mtok
    // Release/lifecycle metadata inferred from the model id/name. Drives per-provider
    // newest-first sort and the "older / deprecated" grouping in the Providers tab.
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
// the provider id, then find the model; falls back to a global id match. nullptr = absent.
const BridgeModelMeta* bridgeModelMetaForCad(int cadProvider, const std::string& modelId,
                                             const char* cloudBaseUrl);
// True for genuinely local provider ids whose inference runs on the user's hardware. Hosted
// variants are excluded; elsewhere an absent price means "no price data", not "free".
bool bridgeProviderIsLocalFree(const std::string& providerId);
// Catalog display formats: "ctx 200k · $3/$15 · tools vision" row sublabel and the
// cache-pricing detail line for tooltips. Unpriced non-local rows render "no price data".
std::string bridgeModelSublabel(const BridgeModelMeta& m);
std::string bridgeModelCostDetail(const BridgeModelMeta& m);
// Per-session cost strip computed from completed-turn token events and cached pricing:
// "Session $1.87 · 142k tok" plus a per-model breakdown. If some token-spending model has no
// catalog price, the total is shown as a floor rather than a complete figure. Hidden until
// catalog data is available.
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

// Durably persist resident conversations to the Sessions store. Explicit callers wait for
// pending writes; the per-frame debounce below snapshots to an ordered background writer.
void persistChatHistory(AppState& state);

// Save the latest transcript and composer draft, then drain pending writes before teardown.
void flushChatPersistence(AppState& state);

// Drain accepted answer/steer jobs while AppState and its worker are still alive.
void shutdownAgentControl(AppState& state);

// Debounced mid-turn flush of the active session's chat history (resume-safe: streaming
// assistant text is written to disk ~1/s WHILE a turn runs, not only at completion).
// `force` bypasses the timer. Call once per frame from whichever chat host is on screen —
// the docked Copilot column OR the AI-tab workspace (which does not draw that column).
void maybePersistChatHistoryDebounced(AppState& state, bool force);

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
// Reserved parent-layout gutter height, INCLUDING the item's trailing spacing. Reserve this
// before sizing the scrolling child, even while pinned, so showing the action never resizes it.
float chatJumpToLatestFooterHeight();
// Draw the layout-participating footer AFTER EndChild(), at the parent's current cursor.
// It consumes chatJumpToLatestFooterHeight(), never overlaps the transcript or its scrollbar,
// and hides the action while pinned. Clicking defers the region-scoped snap until the next log.
void chatDrawJumpToLatest(const char* regionId);
void chatDrawComposer(AppState& st, bool busy, bool canRun);  // prelude + input + toolbar (real send path)
void chatDrawSessionsList(AppState& st, bool busy);           // session rows + "+ New session"
// The ONE empty-state for a session with no messages yet — both chat hosts (docked Copilot and
// the AI workspace) draw it, so a blank session reads the same everywhere.
void chatDrawEmptyState();

// ---- session titles (conversations.cpp) -----------------------------------------------------
// A session starts as "Session N" (monotonic N) and is named from its first typed prompt
// (attachment/context markers stripped, ~40 chars). persistChatHistory applies this on every
// write; call it directly where a first prompt lands to retitle in the same frame.
void autoTitleConversation(AppState& st, int idx);
// User rename. Persists (marks history dirty) and pins the name against auto-titling; an
// empty/blank name hands the session back to its automatic title. Returns true on change.
bool renameConversation(AppState& st, int idx, const std::string& title);

}  // namespace ui
