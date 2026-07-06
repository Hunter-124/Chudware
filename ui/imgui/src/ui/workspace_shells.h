// T-DP15: full-viewport workspace shell panels.
// AiAgent and PlmManagement are full-viewport workspaces that own the entire screen
// (no glass overlay columns). Their bodies are deferred to T-DP16 and the R-stack;
// these shells provide the switch-framework placeholder rendered inside the viewport area.
#pragma once

namespace app { struct AppState; }

namespace ui {

// AI-Agent full-viewport shell (body: T-DP16). Renders a placeholder header bar +
// empty content area inside the docked "AI Agent" window.
void drawAiAgentShell(app::AppState& s);

// PLM project-hub full-viewport shell (body: R-stack). Renders a placeholder header bar +
// empty content area inside the docked "PLM" window.
void drawPlmShell(app::AppState& s);

} // namespace ui
