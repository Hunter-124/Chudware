// AI-CAD Settings modal (AI provider + Agent tabs) and the About modal.
#pragma once

struct AppState;

namespace ui {

// Draw the Settings popup (opened when state.openSettings is set) and the About popup (opened
// when state.showAbout is set). `busy` whether a job is running (disables worker-dependent
// actions like "Load built-in prompt").
void drawSettingsModal(AppState& state, bool busy);

}  // namespace ui
