// T-DP17 Agent DAG widget — interactive clickable node-graph of the running
// agent workflow.  Shows mission phases, the build DAG (aicad.buildgraph/1),
// and the delegation tree (aicad.delegation/1 — who spawned whom).
//
// Clicking a node opens a detail panel (role / model / current tool / ledger
// slice / token spend / status).  Two render modes:
//   Compact — graph only; intended to embed in the overlay copilot column.
//   Full    — graph + detail pane; intended for the AI-Agent workspace (T-DP16).
//
// Glass design language; dark-mode contrast; ui_style_check clean.
// Screenshot-verifiable via AI_CAD_SHOT=agent_dag (full, node selected)
// and AI_CAD_SHOT=agent_dag_compact (compact, no selection).
//
// Built on the T-U1 panel_scaffold; view-model lives in
// ui/abstract/agent_dag_view.h (no ImGui includes there).
#pragma once

#include <string>
#include "agent_dag_view.h"  // ui/abstract/

namespace app { struct AppState; }

namespace ui {

// Body only (no ImGui::Begin/End) — call inside an existing window.
// mode selects compact vs full.
void drawAgentDagPanel(app::AppState& s,
                       aicad::ui::DagWidgetMode mode = aicad::ui::DagWidgetMode::Full);

// Open the closable panel window (called by configureShot).
void openAgentDagPanel();

// The pane's open flag — application.cpp passes it to ImGui::Begin as p_open.
bool* agentDagPanelOpen();

// Seed the widget model from JSONL (aicad.agent_events/1 + delegation events).
// One event object per line.  Safe to call repeatedly (resets state).
void seedAgentDagPanelFromJsonl(const std::string& jsonl_text);

// Shot helper: select the node with the given id so the detail pane renders.
// Call after seedAgentDagPanelFromJsonl().
void agentDagShotSelectNode(const std::string& nodeId);

}  // namespace ui
