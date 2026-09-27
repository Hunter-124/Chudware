// AI workspace — token spend for the active project, with an opt-in All projects toggle.
// Aggregates completed assistant turns in resident conversations (active st.chat, parked
// Conversation::msgs), priced with cached Chudcode model metadata. Unknown-price turns
// contribute tokens, with their spend explicitly labelled as a floor.
#pragma once

struct AppState;

namespace ui {

// The Metrics tab body (drawn inside the DAG pane child; scrolls automatically).
void drawMetricsView(AppState& st);

}  // namespace ui
