// AI workspace: full-viewport body for the AI title-bar tab (v2 layout, tracker M1-M8).
// Compact, transparency-first shell:
//   - UPPER main area: the DAG pane, switched by a five-chip tab strip (Agent Workflow |
//     Session Plan | Active Work | Documents | Leaderboard — ai_workspace.cpp, s_ai.dagView).
//     Left of it: the sessions/sub-agents rail.
//   - LOWER-LEFT: the conversation window (message log + composer) — ALWAYS visible, no tab
//     (SAME state/send path as the docked column, via the ui/copilot/copilot.h reuse hooks).
//   - LOWER-RIGHT: AI *workflow* settings only (Supervised/Autopilot, refine passes, max output
//     tokens, context window). The inference block (model/effort/tokens/API key/privacy/vision)
//     lives ONLY on the main Settings page now — persisted through saveSettings (config.json).
#pragma once

#include <string>

#include <nlohmann/json.hpp>

struct AppState;

namespace ui {

// Draws the full workspace body (no Begin/End of its own — app.cpp hosts it in one borderless
// window spanning the central band, same pattern as drawPlmWorkspace / drawPartsLibraryWorkspace).
// Safe to call every frame; degrades to informative empty-states when services are not running.
void drawAiWorkspace(AppState& st);

// Phase 2 (M9/M10) — live Agent Workflow DAG feed.
// Called ONCE per streamed orchestrator event from the app.cpp JobState.events GL-thread drain
// (the same drain that routes ActivityPanel + feedTurnActivity). `kind` is the raw event kind
// with the "orch_" prefix already stripped (e.g. "agent.tool_call", "run_started"); `data` is the
// parsed `aicad.agent_events/2` record. GL-thread only — the store is a plain static, no mutex.
//
// Builds the agent-execution tree by joining `parent_id`→`id` (§2.1). Consumes the LIVE kinds
// (agent.tool_call / agent.thought / agent.context_injected) and the emit-wired-but-currently-
// unsourced kinds (agent.spawn / agent.advisor) — the latter simply add no nodes until a real
// production sink emits them (honest empty-state, no fabrication). `run_started` resets the tree.
void feedAgentDagEvent(const std::string& kind, const nlohmann::json& data);

// Serialize / restore the live Agent Workflow DAG store (s_agdag) — every node field the
// canvas draws (id, parentId, kind, actor, title, detail, status, refId, latencyMs, seq) plus
// the run id and counters. Used to snapshot a run's execution tree as an artifact. GL-thread
// only (same store, no mutex). Tolerates partial/corrupt input (skips bad records).
nlohmann::json agentDagToJson();
void agentDagFromJson(const nlohmann::json& j);

}  // namespace ui
