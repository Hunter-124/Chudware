// Chudware Settings modal (Appearance / General / AI) and the About modal. The window is a table
// of categories, so a new one is a row plus its draw function. AI is a strip of tabs (Models,
// Behavior, Context, Memory, Tools, Coding, Agents, Providers, Developer) holding Chudware's own
// sections and OMP's settings, read and written through omp_config_get / omp_config_set.
#pragma once
#include <string>


struct AppState;

// Search-table entry for the unified command search (command_search.cpp).
// One entry per notable settings field / section across the Settings pages.
// A route opens a page (0..2), an AI tab, or a section inside an AI tab (scrolled into view).
// Opening a section that exists only in Advanced mode turns Advanced on.
enum SettingsRoute {
    SettingsAppearance = 0, SettingsGeneral = 1, SettingsAI = 2,
    // The AI page's Providers tab (it was a top-level page 3). Retired pages 4..7 (Benchmarks,
    // Prompts, Agent, Experimental) stay accepted: the modal folds each onto the AI tab that
    // absorbed it, for existing callers and screenshot scenarios.
    SettingsProviders = 3,
    // AI tabs.
    SettingsAiModels = 8, SettingsAiBehavior, SettingsAiContext, SettingsAiMemory,
    SettingsAiTools, SettingsAiCoding, SettingsAiAgents, SettingsAiDeveloper,
    // Sections inside an AI tab.
    SettingsAiRolePrompts, SettingsAiVisionRouting, SettingsAiRunPreferences,
    SettingsAiHarnessOrg, SettingsAiRules, SettingsAiAutoLearn, SettingsAiSkills,
    SettingsAiBenchmarks, SettingsAiDevPreview,
    SettingsAiMaintenance  // Developer tab; Behavior tab in Basic mode
};
struct SettingsEntry {
    const char* label;     // search-visible text (what the user types to find this)
    const char* tab;       // Settings tab name, shown dim on the right in the search row
    int         tabIndex;  // SettingsRoute
};
extern const SettingsEntry kSettingsEntries[];
extern const int           kSettingsEntryCount;

namespace ui {

// Draw the Settings popup (opened when state.openSettings is set) and the About popup (opened
// when state.showAbout is set). `busy` whether a job is running (disables worker-dependent
// actions like "Load built-in prompt").
void drawSettingsModal(AppState& state, bool busy);

// Shared external-browser OAuth seam used by Settings -> AI -> Providers and first-run onboarding.
// Chudcode owns the flow; polling returns its progress/error. No embedded webview is involved.
struct ExternalProviderSignInStatus {
    bool active = false;
    bool connected = false;
    bool browserOpened = false;
    std::string provider;
    std::string status;
    std::string progress;
    std::string instructions;
    std::string url;
    std::string error;
};
void beginExternalProviderSignIn(AppState& state, const std::string& provider);
ExternalProviderSignInStatus pollExternalProviderSignIn(AppState& state);

// Settings -> AI -> Behavior "Session knobs": the full run-behavior grid (plan/subagent/
// advisor preferences, proactive advisor/vision/offline/thoughts/redact flags, chat
// display). Shared verbatim by the AI-tab command center's Options view (ai_workspace.cpp) so
// both surfaces read/write the exact same config.json fields — no duplicate widget code.
// Returns true if any Settings field changed this frame; EVERY caller must saveSettings() on
// true (both do, at the edit frame — there is no save-on-close).
bool drawHarnessSessionKnobs(AppState& state, float sc);

// Settings -> AI -> Agents "Subagent limits": the subagent tool-call budget and max nesting
// depth, persisted by their own savers (never through Settings). Advanced-mode controls: the
// AI workspace Options view shows them beside the session knobs.
void drawSubagentLimitKnobs(float sc);

// Flush the Settings modal's debounced autosaves (system-prompt override, per-role prompt
// editors) immediately. Called from app teardown so an edit made <1 s before quit — or while
// the modal is still open — is not lost. Safe to call at any time.
void flushSettingsModalEdits(AppState& state);

// Exit-time companion for the RPC-backed edits (Skills/Rules editors, model-assignment draft):
// sends what is still pending and waits up to ~5 s for the worker's ack. Must run BEFORE
// AsyncRpc::shutdown() (it aborts the in-flight call); a no-op when the worker is not running.
void flushSettingsModalRpcEdits(AppState& state);

// Small / Medium / Large model-size assignment for Copilot Quick settings: rows of caption-left /
// model picker + thinking effort right (`captionColumn` px wide, capped to the row), plus the
// shared read-only / error / unsaved-draft status. Draws over the exact state, pickers and
// save/ack/retry path of Settings → AI → Models, so a change in either surface shows in the
// other. `captionInk` is an ImU32 for the captions (0 = current text color). Pickers are greyed
// until the agent service is ready. Call inside a card, once per frame.
void drawQuickModelSizes(AppState& state, float captionColumn, unsigned int captionInk);

}  // namespace ui
