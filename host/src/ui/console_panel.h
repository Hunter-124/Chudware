// AI-CAD Console panel: the runtime/agent log, color-coded by [tag].
#pragma once

struct AppState;

namespace ui {

// Body only (no Begin/End) — renders state.log inside the consolidated AI panel's Console tab,
// color-coded per [tag].
void drawConsoleBody(AppState& state);

}  // namespace ui
