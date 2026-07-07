// AI workspace: full-viewport body for the AI title-bar tab.
// Left rail: copilot sessions (+ sub-agents section). Center: expanded Copilot chat (SAME
// state/send path as the docked column, via the legacy_chat.h reuse hooks) or the project
// build-plan DAG view (persisted run sidecars + dag_status/dag_approve/dag_abort over st.rpc).
// Right: workflow + model-assignment settings persisted through saveSettings (config.json).
#pragma once

struct AppState;

namespace ui {

// Draws the full workspace body (no Begin/End of its own — app.cpp hosts it in one borderless
// window spanning the central band, same pattern as drawPlmWorkspace / drawPartsLibraryWorkspace).
// Safe to call every frame; degrades to informative empty-states when services are not running.
void drawAiWorkspace(AppState& st);

}  // namespace ui
