// AI-CAD legacy Design panel: brand header, prompt / conversation takeover, iteration cards,
// plan card, and the always-visible build123d code section.
#pragma once

struct AppState;

namespace ui {

// Draw the consolidated AI panel (Design / Activity / Console tabs + workflow control strip).
// `busy` whether a job is running, `canRun` whether new jobs can be launched.
void drawAIPanel(AppState& state, bool busy, bool canRun);

// Shared provider + model picker (provider combo, availability-filtered model picker, per-
// provider API key, reasoning-effort knob, local base URL/vision, offline toggle). Rendered
// in BOTH the Settings modal's AI tab and the inline AI Settings tab so they never diverge.
// Returns true if a setting changed this frame (caller may persist).
bool drawProviderModelPicker(AppState& state, bool busy);

// Probe local-endpoint model availability (agent_list_models) into state.providerModels.
// Throttled internally; pass force=true to probe immediately (e.g. on a base-URL change).
void refreshModelAvailability(AppState& state, bool force);

// Persist the active document's conversations (the active one synced from state.chat) to
// %APPDATA%/AI-CAD/chat_history.json. Safe to call any time; no-op if nothing to save.
void persistChatHistory(AppState& state);

}  // namespace ui
