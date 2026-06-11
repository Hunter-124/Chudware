// AI-CAD Console panel: the runtime/agent log, color-coded by [tag].
#pragma once

struct AppState;

namespace ui {

// Draw the Console dockable window if state.showConsole. Renders state.log with per-tag colors.
void drawConsolePanel(AppState& state);

// Body only (no Begin/End) — for hosting the console inside the consolidated AI panel's tab.
void drawConsoleBody(AppState& state);

}  // namespace ui
