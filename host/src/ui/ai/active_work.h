// AI workspace — Active Work view.
//
// Live grid of the current project's documents agents are editing: one glass card per doc
// with a shaded preview of its CURRENT mesh (re-rendered after each agent revision), a status
// frame (pulsing while an agent writes, green when the agent finished, red on a failed op),
// the doc's revision activity, and a per-doc tool-call ticker. Docs touched within the last few
// minutes or during the project's running agent turn are "in flight", most recent first; other
// agent-touched docs collapse into an Idle list. Clicking a card or row opens the doc's tab.
//
// Data feeds (all GL-thread, plain statics — same pattern as the Agent Workflow DAG store):
//   - activeWorkFeedEvent(): agent.* orchestrator events (op attribution + tickers, run start
//     stamps), wired next to ui::feedAgentDagEvent in the app.cpp JobState.events drain.
//   - st.docTabs[]: live doc state (aiHandoff / aiPulse / agentDone / lastAgentEditAt) and each
//     DocSession's parked revision + parsed mesh (cachedRevision / cachedAicmPath /
//     cachedDocMesh), which engine_events.cpp prewarms for background mirrors per revision.
#pragma once

#include <string>

#include <nlohmann/json.hpp>

#include "imgui.h"

struct AppState;

namespace ui {
// Saved agent tabs belong by file location; never-saved tabs require their originating
// conversation's project directory. Missing provenance is not attributed to this project.
bool agentDocInProject(const std::string& path, const std::string& originProjectDir,
                       const std::string& activeProjectDir);

// Feed one drained orchestrator event (kind with "orch_" already stripped). GL-thread only.
void activeWorkFeedEvent(const std::string& kind, const nlohmann::json& data);

// Once per frame from the AI workspace: observes per-doc revision deltas, renders at most one
// pending preview texture, collects/starts at most one background AICM parse, and frees the
// previews of closed docs (LRU-capped otherwise).
void activeWorkPump(AppState& st);

// Drop all per-doc op state (project switch / full reset). Doc liveness re-derives from
// st.docTabs on the next frame.
void activeWorkReset();

// The Active Work tab body (drawn inside the DAG pane child; scrolls automatically).
void drawActiveWorkView(AppState& st);

// Live preview of open doc tab `tabIdx` painted into [p0, p1] of the current window: the same
// cache and states as the Active Work cards (loading shimmer, "Empty", failure reason). Only
// queues a render; the pump draws it on a later frame.
void drawAgentDocPreview(AppState& st, int tabIdx, ImVec2 p0, ImVec2 p1);

// Switch to doc tab `tabIdx` and land on the workspace that renders its kind.
void openAgentDocTab(AppState& st, int tabIdx);

}  // namespace ui
