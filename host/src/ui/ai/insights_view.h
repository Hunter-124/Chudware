#pragma once

// Composed "Insights" view for the AI workspace utility pane — Leaderboard on top,
// Metrics below, one scroll surface. Implementation composes the existing per-view
// bodies (ui::leaderboard::drawFullPane, ui::drawMetricsView); no duplicated painters.

#include "core/app_state.h"

namespace ui {

// Drawn inside the DAG pane child; scrolls automatically.
void drawInsightsView(AppState& st);

}  // namespace ui
