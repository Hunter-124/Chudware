// T-F17 Mission Control panel — public API.
// Built on ui/imgui/src/ui/panel_scaffold.h (T-U1 kit); view-model lives in
// ui/abstract/mission_control_view.h (no ImGui includes there).
// Screenshot-verifiable via AI_CAD_SHOT=mission_control (seeded one-shot).
//
// What it shows (contracts/agent_events.md, aicad.agent_events/1):
//   - Run tree: one row per DAG node with live status (running/done/failed/paused)
//   - Event trail: every dag.* event in order — no silent actions
//   - Steering: Approve / Reject buttons for a gate-paused run, wired to the
//     dag_approve / dag_abort worker methods (services/dag/worker_rpc.py, which
//     delegate to executor.resume() — the same single implementation the MCP
//     twins use). Refresh re-reads dag_status.
//
// Honestly deferred (noted in work/tasks/T-F17.md): the design-brief flow
// (brief proposed → edited → approved → verify nodes check it) and mid-run
// instruction injection — both need the T-BL09 requirements schema + an
// executor-side inject seam that do not exist yet.
#pragma once

#include <string>

namespace app { struct AppState; }

namespace ui {

// Body only (no ImGui::Begin/End) — call inside a docked/closable window, same
// convention as drawBomPanel() / drawTransparencyPanel().
void drawMissionControlPanel(app::AppState& s);

// Open the (closable) mission-control pane. Costs nothing while closed.
void openMissionControlPanel();

// The pane's open flag — application.cpp passes it to ImGui::Begin as p_open.
bool* missionControlPanelOpen();

// Seed the panel model from raw JSONL text (scenario/shot use only — one
// aicad.agent_events/1 event object per line, same shape EventJournal writes).
// Populates the file-static view so the headless screenshot shows a real run
// without a live DAG round-trip.
void seedMissionControlPanelFromJsonl(const std::string& jsonl_text);

// T-DP18: mirror whole-mission ETA from the panel view-model into AppState so
// the status-bar chip updates every frame even when Mission Control is closed.
// Call unconditionally each frame (cheap string/float copy).
void syncMissionEtaToAppState(app::AppState& s);

} // namespace ui
