// AI-CAD Copilot pane: slim header (logo + status + Sessions), conversation log with a per-turn
// transparent activity timeline (what the AI is doing / thinking, expandable) whose cards are now
// translucent Liquid-Glass bubbles over the live viewport backdrop (host contract H5), and a
// bottom composer (prelude: Design/Permissions + context meter; transparent prompt; toolbar:
// model picker + thinking + gear/send) painted over the same glass surface.
#pragma once

struct AppState;

namespace ui {

// Draw the AI Copilot pane (header + conversation + bottom composer). `busy` whether a job is
// running, `canRun` whether new jobs can be launched.
void drawAIPanel(AppState& state, bool busy, bool canRun);

// Draw the "Settings" dock tab that sits next to the "AI" window (same dock node). Call right
// after drawAIPanel each frame.
void drawAISettingsTab(AppState& state, bool busy);

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

// Per-frame: if a chat-triggered accurate (OpenFOAM) CFD launched on the SimClient channel has
// finished, post its Cd/Cl result onto the running chat bubble. No-op when nothing is pending.
// Call once per frame from the main loop (alongside pollAiCadHandoff).
void pollChatOpenFoam(AppState& state);

}  // namespace ui
