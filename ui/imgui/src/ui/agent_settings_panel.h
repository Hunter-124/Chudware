// T-W17 Agent Settings — model sources + per-role routing editor.
// Rendered INLINE inside the Settings modal's AI tab (settings_modal.cpp) —
// there is exactly one settings surface, the popup. View-model lives in
// ui/abstract/agent_settings_view.h (no ImGui includes there).
// Screenshot-verifiable via AI_CAD_SHOT=agent_settings (valid) and
// =agent_settings_error (validation-error state) — both open the Settings modal.
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

// Draw the routing editor body (sources, per-role assignment table, toggles,
// save-through-routing.py). Lazily loads from the routing CLI on first draw.
// Called from the Settings modal AI tab; no window/scaffold of its own.
void drawAgentSettingsInline(app::AppState& s);

// Seed the panel model from a routing `view` JSON (as emitted by
// `python3 -m services.agent.routing get`). Scenario/shot + offline use.
void seedAgentSettingsFromViewJson(const std::string& view_json);

}  // namespace ui
