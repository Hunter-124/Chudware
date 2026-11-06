// Chudware Settings modal (Appearance / General / AI / Providers) and the About modal.
// AI absorbs Prompts, Benchmarks, Tools, Roles, and the retired Experimental tab.
#pragma once
#include <string>


struct AppState;

// Search-table entry for the unified command search (command_search.cpp).
// One entry per notable settings field / section across the Settings tabs.
// Routes 0..3 select a page; 8..15 select an AI section as well. Legacy routes 4..7
// remain accepted by the modal for existing callers and screenshot scenarios.
enum SettingsRoute {
    SettingsAppearance = 0, SettingsGeneral = 1, SettingsAI = 2, SettingsProviders = 3,
    SettingsAiHarness = 8, SettingsAiRoles, SettingsAiPrompts, SettingsAiSkills,
    SettingsAiRules, SettingsAiBenchmarks, SettingsAiTools, SettingsAiDevPreview
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

// Shared external-browser OAuth seam used by Settings -> Providers and first-run onboarding.
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

// Settings -> AI -> Harness "Session knobs": the full run-behavior grid (plan/subagent/
// advisor preferences, proactive advisor/vision/offline/thoughts/redact flags, chat
// display, max subagent nesting, tool-call budget). Shared verbatim by the AI-tab command
// center's Options view (ai_workspace.cpp) so both surfaces read/write the exact same
// config.json fields — no duplicate widget code.
// Returns true if any Settings field changed this frame; EVERY caller must saveSettings() on
// true (both do, at the edit frame — there is no save-on-close).
bool drawHarnessSessionKnobs(AppState& state, float sc);

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
