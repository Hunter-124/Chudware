// AI-CAD legacy Design panel: brand header, prompt / conversation takeover, iteration cards,
// plan card, and the always-visible build123d code section.
#pragma once

struct AppState;

namespace ui {

// Draw the Design dockable window if state.showDesign. `busy` whether a job is running,
// `canRun` whether new jobs can be launched (worker started and not busy).
void drawDesignPanel(AppState& state, bool busy, bool canRun);

}  // namespace ui
