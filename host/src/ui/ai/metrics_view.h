// AI workspace — Metrics view.
//
// Token spend and overall usage across every session in the current chat history: total
// observed spend + tokens, a per-model breakdown, and a per-session breakdown so the user can
// see where the tokens (and dollars) went. Pure client-side aggregation over st.chat /
// st.conversations[].msgs (the same completed-turn ActivityItem token counts the per-session
// cost strip uses) priced with the cached OMP bridge model catalog (ui::bridgeModelMeta) —
// no extra RPC, no fake numbers: sessions/models with no priced turns simply don't appear.
#pragma once

struct AppState;

namespace ui {

// The Metrics tab body (drawn inside the DAG pane child; scrolls automatically).
void drawMetricsView(AppState& st);

}  // namespace ui
