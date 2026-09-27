// AI workspace — Active Work view.
//
// Live grid of the documents agent(s) and subagents are creating/editing right now: one glass
// card per WIP doc with an iso preview slot, a status frame (pulsing while an agent writes,
// green when the agent finished, red on a failed op), and a per-doc operation ticker so the
// user can see exactly which operations each agent is making along the way. The grid
// auto-scales (column count tracks the pane width; cards grow/shrink to fit) and docs appear/
// disappear automatically as agents open, modify, and close them.
//
// Data feeds (all GL-thread, plain statics — same pattern as the Agent Workflow DAG store):
//   - activeWorkFeedEvent(): agent.* orchestrator events (op attribution + tickers), wired
//     next to ui::feedAgentDagEvent in the app.cpp JobState.events drain.
//   - st.docTabs[]: live doc state (aiHandoff / aiPulse / agentDone / dirty) each frame.
#pragma once

#include <string>

#include <nlohmann/json.hpp>

struct AppState;

namespace ui {
// Saved agent tabs belong by file location; never-saved tabs require their originating
// conversation's project directory. Missing provenance is not attributed to this project.
bool agentDocInProject(const std::string& path, const std::string& originProjectDir,
                       const std::string& activeProjectDir);

// Feed one drained orchestrator event (kind with "orch_" already stripped). GL-thread only.
void activeWorkFeedEvent(const std::string& kind, const nlohmann::json& data);

// Drive at most one pending thumbnail render per call; call once per frame from the AI
// workspace. No-op until the preview-texture slice lands.
void activeWorkPump(AppState& st);

// Drop all per-doc op state (project switch / full reset). Doc liveness re-derives from
// st.docTabs on the next frame.
void activeWorkReset();

// The Active Work tab body (drawn inside the DAG pane child; scrolls automatically).
void drawActiveWorkView(AppState& st);

}  // namespace ui
