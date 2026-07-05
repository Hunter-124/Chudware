#pragma once
// T-W28 — "AI Effort" section of the settings modal (AI Models & Routing tab).
//
// Simple mode: one 4-level dial (Eco / Standard / Deliberate / Maximum), each a
// named preset from services/agent/effort_presets.yaml. Advanced mode: ALL
// harness knobs grouped (Deliberation / Parallelization / Safety nets /
// Context), per tier, each control showing the live resolved value and which
// layer supplies it (shipped / preset / override). Warnings never block.
//
// All reads/writes go through `python3 -m services.agent.effort get|set` — the
// same one-path resolution the mission pipeline and the design gym use. The
// GUI never re-implements resolution or warning logic.

#include <string>

namespace app { struct AppState; }

namespace ui {

// Draw the section body (called from the AI Models & Routing tab).
void drawAiEffortSection(app::AppState& st);

// Headless shot seam: populate the model from a committed `view` JSON fixture
// (no subprocess). Mirrors seedAgentSettingsFromViewJson.
void seedAiEffortFromViewJson(const std::string& view_json);

}  // namespace ui
