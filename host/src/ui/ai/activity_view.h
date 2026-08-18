#pragma once

// Composed "Activity" view for the AI workspace utility pane — Active Work on top,
// Documents below, one scroll surface. Implementation composes the existing per-view
// bodies (ui::drawActiveWorkView, ui::drawDocsPanel); no duplicated painters.

#include "core/app_state.h"

namespace ui {

// Drawn inside the DAG pane child; scrolls automatically.
void drawActivityView(AppState& st);

}  // namespace ui
