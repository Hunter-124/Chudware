// In-app-dialog transport for the interaction contract (contracts/interaction.md §8):
// renders a pending ask_user batch (AppState::pendingAskBatchJson) as a
// PropertyManager-style modal form and, on Submit, stores a §4 answers envelope
// onto AppState::askAnswersJson.
#pragma once

namespace app { struct AppState; }

namespace ui {

// Call once per frame from the draw path. Returns true on the frame answers were
// just submitted (AppState::askAnswersJson is freshly populated that frame).
bool drawAskDialog(app::AppState& s);

}  // namespace ui
