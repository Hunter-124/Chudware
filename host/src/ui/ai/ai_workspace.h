// AI workspace: full-viewport body for the AI title-bar tab (v3 layout).
// Command-center shell:
//   - LEFT: the main conversation (message log + composer) — ALWAYS visible, no tab
//     (SAME state/send path as the docked column, via the ui/copilot/copilot.h reuse hooks).
//   - RIGHT: the utility panel — a vertical nav menu (Agent Hub | Session Plan | Active Work |
//     Documents | Sessions | Leaderboard | Options — ai_workspace.cpp, s_ai.view) hosting the
//     selected view. Sub-agent chats pop up as floating, role-themed windows (Agent Hub rows).
//   - Inference settings (model/effort/tokens/API key/privacy/vision) live ONLY on the main
//     Settings page; the Options view keeps the run-behavior + visibility knobs.
#pragma once

#include <string>

#include <nlohmann/json.hpp>

struct AppState;

namespace ui {

// Draws the full workspace body (no Begin/End of its own — app.cpp hosts it in one borderless
// window spanning the central band, same pattern as drawPlmWorkspace / drawPartsLibraryWorkspace).
// Safe to call every frame; degrades to informative empty-states when services are not running.
void drawAiWorkspace(AppState& st);

// Phase 2 (M9/M10) — live Agent Workflow DAG feed, ONE tree PER CONVERSATION.
// Called ONCE per streamed orchestrator event from the app.cpp JobState.events GL-thread drain
// (the same drain that routes ActivityPanel + feedTurnActivity). `convId` is the conversation
// whose run emitted the event (the drain iterates st.runs, keyed by conversation id); `kind` is
// the raw event kind with the "orch_" prefix already stripped (e.g. "agent.tool_call",
// "run_started"); `data` is the parsed `aicad.agent_events/2` record. GL-thread only — the
// stores are plain statics, no mutex.
//
// Builds the agent-execution tree by joining `parent_id`→`id` (§2.1). Consumes the LIVE kinds
// (agent.tool_call / agent.thought / agent.context_injected) and the emit-wired-but-currently-
// unsourced kinds (agent.spawn / agent.advisor) — the latter simply add no nodes until a real
// production sink emits them (honest empty-state, no fabrication). `run_started` resets only the
// emitting conversation's tree, so two concurrent chats never blank each other's trace.
void feedAgentDagEvent(const std::string& convId, const std::string& kind,
                       const nlohmann::json& data);

// Serialize / restore ONE conversation's Agent Workflow DAG — every node field the canvas draws
// (id, parentId, kind, actor, title, detail, status, refId, latencyMs, seq) plus the run id and
// counters. Used to snapshot a session's execution tree as a chat-history artifact. GL-thread
// only (same stores, no mutex). agentDagToJson returns a null json for a conversation that has
// never run; agentDagFromJson tolerates partial/corrupt input (skips bad records).
nlohmann::json agentDagToJson(const std::string& convId);
void agentDagFromJson(const std::string& convId, const nlohmann::json& j);

}  // namespace ui
