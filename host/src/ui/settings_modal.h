// Chudware Settings modal (Appearance / General / AI / Providers / Experimental)
// and the About modal. AI absorbs Prompts, Benchmarks, Tools, Roles.
#pragma once

struct AppState;

// Search-table entry for the unified command search (command_search.cpp).
// One entry per notable settings field / section across the Settings tabs.
// Tab indices: 0=Appearance 1=General 2=AI 3=Providers 4=Experimental
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

}  // namespace ui
