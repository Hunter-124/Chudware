// Chudware Settings modal (Appearance / General / AI / Providers) and the About modal.
// AI absorbs Prompts, Benchmarks, Tools, Roles, and the retired Experimental tab.
#pragma once

struct AppState;

// Search-table entry for the unified command search (command_search.cpp).
// One entry per notable settings field / section across the Settings tabs.
// Tab indices: 0=Appearance 1=General 2=AI 3=Providers
struct SettingsEntry {
    const char* label;     // search-visible text (what the user types to find this)
    const char* tab;       // Settings tab name, shown dim on the right in the search row
    int         tabIndex;  // 0-based index matching the Settings modal tab bar order
};
extern const SettingsEntry kSettingsEntries[];
extern const int           kSettingsEntryCount;

namespace ui {

// Draw the Settings popup (opened when state.openSettings is set) and the About popup (opened
// when state.showAbout is set). `busy` whether a job is running (disables worker-dependent
// actions like "Load built-in prompt").
void drawSettingsModal(AppState& state, bool busy);

// Settings -> AI -> Harness "Session knobs": the full run-behavior grid (plan/subagent/
// advisor preferences, role adoption, proactive advisor/company/vision/offline/thoughts/
// memory/redact flags, refine-pass cap, max subagent nesting, memory budget, Eng. Firm
// budget). Shared verbatim by the AI-tab command center's Options view (ai_workspace.cpp)
// so both surfaces read/write the exact same config.json fields — no duplicate widget code.
// Returns true if any field changed this frame (caller decides whether/when to saveSettings —
// the modal saves once on close; the Options view saves immediately).
bool drawHarnessSessionKnobs(AppState& state, float sc);

}  // namespace ui
