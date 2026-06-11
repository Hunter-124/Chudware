// AI-CAD legacy Design panel: brand header, prompt / conversation takeover, iteration cards,
// plan card, and the always-visible build123d code section.
#pragma once

struct AppState;

namespace ui {

// Draw the consolidated AI panel (Design / Activity / Console tabs + workflow control strip).
// `busy` whether a job is running, `canRun` whether new jobs can be launched.
void drawAIPanel(AppState& state, bool busy, bool canRun);

}  // namespace ui
