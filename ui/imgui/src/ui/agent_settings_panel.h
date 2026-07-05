// T-W17 Agent Settings panel — model sources + per-role routing editor.
// Built on ui/imgui/src/ui/panel_scaffold.h (T-U1 kit); view-model lives in
// ui/abstract/agent_settings_view.h (no ImGui includes there).
// Screenshot-verifiable via AI_CAD_SHOT=agent_settings (valid) and
// =agent_settings_error (validation-error state).
//
// Edits services/agent/routing.yaml THROUGH services/agent/routing.py validation
// (never free text): the panel reads a `view` JSON, edits a structured model, and
// writes back via the routing.py CLI (get/set). It shows every role in routing.yaml
// — including overseer — as an editable row with an ordered preference list and a
// live "resolves to: <model>@<source>" readout, and warns (never blocks) on risky
// picks. Also: source add/edit/remove, delegation approval policy, and the
// auto-coincident / preflight / advisory-verbosity toggles.
//
// Offline/shot mode seeds from a committed routing view fixture — no backend.
#pragma once

#include <string>

namespace app { struct AppState; }

namespace ui {

void drawAgentSettingsPanel(app::AppState& s);
void openAgentSettingsPanel();
bool* agentSettingsPanelOpen();

// Seed the panel model from a routing `view` JSON (as emitted by
// `python3 -m services.agent.routing get`). Scenario/shot + offline use.
void seedAgentSettingsFromViewJson(const std::string& view_json);

}  // namespace ui
