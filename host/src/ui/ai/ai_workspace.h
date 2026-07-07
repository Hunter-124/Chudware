// AI workspace: full-viewport body for the AI title-bar tab (v2 layout, tracker M1-M8).
// Compact, transparency-first shell:
//   - UPPER main area: the DAG pane, switched by an Agent Workflow | Session Plan tab strip
//     (Phase 1 renders the existing buildgraph view in both; Phase 2 swaps in the live
//     agent-execution DAG for Agent Workflow). Left of it: the sessions/sub-agents rail.
//   - LOWER-LEFT: the conversation window (message log + composer) — ALWAYS visible, no tab
//     (SAME state/send path as the docked column, via the legacy_chat.h reuse hooks).
//   - LOWER-RIGHT: AI *workflow* settings only (Supervised/Autopilot, refine passes, max output
//     tokens, context window). The inference block (model/effort/tokens/API key/privacy/vision)
//     lives ONLY on the main Settings page now — persisted through saveSettings (config.json).
#pragma once

struct AppState;

namespace ui {

// Draws the full workspace body (no Begin/End of its own — app.cpp hosts it in one borderless
// window spanning the central band, same pattern as drawPlmWorkspace / drawPartsLibraryWorkspace).
// Safe to call every frame; degrades to informative empty-states when services are not running.
void drawAiWorkspace(AppState& st);

}  // namespace ui
