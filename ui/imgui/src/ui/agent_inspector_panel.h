// T-W17 Agent Inspector panel — advisor/agent observability & troubleshooting.
// Built on ui/imgui/src/ui/panel_scaffold.h (T-U1 kit); view-model lives in
// ui/abstract/agent_inspector_view.h (no ImGui includes there).
// Screenshot-verifiable via AI_CAD_SHOT=agent_inspector (populated) and
// =agent_inspector_empty (empty state).
//
// The panel renders ENTIRELY from a replayed run directory (ledger events.jsonl
// + advisory/preflight/audit/delegation/failure records) — no live backend. This
// is the troubleshooting window for the helper layer itself: it shows, per turn,
// the VERBATIM injected context (ledger render, [ADVISORY] block, playbook step),
// tool calls with preflight verdicts, the delegation tree, audit verdicts, and
// failure cards with attempt history. Filterable by agent / stage / severity.
#pragma once

#include <string>

namespace app { struct AppState; }

namespace ui {

// Body only (no ImGui::Begin/End) — call inside a docked/closable window.
void drawAgentInspectorPanel(app::AppState& s);

// Open the (closable) inspector pane. Costs nothing while closed.
void openAgentInspectorPanel();

// The pane's open flag — application.cpp passes it to ImGui::Begin as p_open.
bool* agentInspectorPanelOpen();

// Load + parse a run directory (absolute path). Populates the file-static model.
// Safe to call repeatedly (re-reads). Malformed/missing → Error/Empty state.
void loadInspectorRunDir(const std::string& absRunDir);

}  // namespace ui
