// AI-CAD Copilot pane: a PINNED, full-height, rounded liquid-glass column overlaid on the
// viewport's right edge (~12dp margins; the only layout — no docked fallback). Slim header
// (logo + status + Sessions), conversation log with a per-turn transparent activity timeline
// (what the AI is doing / thinking, expandable) whose cards are translucent adaptive glass
// bubbles over the live viewport backdrop (GlassGroup::Copilot, host contract H5), and a bottom
// composer (prelude: Design|Edit|Ask segmented toggle + Permissions + context meter; transparent
// prompt; toolbar: model picker + thinking + send) painted over the same glass surface. The AI
// settings live in an inline view inside the column (opened by the composer gear).
#pragma once

struct AppState;

namespace ui {

// Draw the AI Copilot pane (header + conversation + bottom composer). `busy` whether a job is
// running, `canRun` whether new jobs can be launched.
void drawAIPanel(AppState& state, bool busy, bool canRun);

// Shared AI settings fields (provider/model/auth, generation, privacy, manage-models).
// Rendered by the Settings modal's AI tab as glass-grouped sections, and composed by
// drawProviderModelPicker for any single-call site that still wants the whole form.
// Returns true if a setting changed this frame (caller may persist).
enum class AiSettingsSection {
    ProviderModel = 0,  // provider, model, sign-in / API key + status, vision
    Generation,         // reasoning effort, max response tokens, context window
    Privacy,            // offline mode, thoughts
    ManageModels,       // hide/show models in the chat picker
    All,                // full form (Provider + Generation + ManageModels + Privacy)
};
bool drawAiSettingsSection(AppState& state, bool busy, AiSettingsSection section);

// Full form: Provider + Generation + (Manage models unless slim) + Privacy.
// `slim` drops Manage models (legacy flag kept for any compact caller).
bool drawProviderModelPicker(AppState& state, bool busy, bool slim = false);

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

// ---- AI workspace hub reuse (host/src/ui/ai/ai_workspace.cpp) --------------------------
// Thin exported wrappers over this pane's internals so the full-page AI workspace renders the
// SAME sessions/messages/composer state (st.chat / st.conversations / st.prompt / send path)
// as the docked Copilot — zero duplicated chat logic. All emit widgets into the CURRENT
// window/child; none Begin() a window of their own.
void chatWorkspaceSync(AppState& st);                         // bind conversations to the active doc
bool chatTurnActive(AppState& st);                            // an assistant turn is streaming
void chatDrawLog(AppState& st);                               // the active conversation's turns
void chatDrawComposer(AppState& st, bool busy, bool canRun);  // prelude + input + toolbar (real send path)
void chatDrawSessionsList(AppState& st, bool busy);           // session rows + "+ New session"

}  // namespace ui
