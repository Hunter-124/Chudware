// SolidWorks-style ribbon body — grouped big icon buttons driven by command_registry.
// Workspace tabs live in workspace_bar.cpp; this module draws the verb-group button rows.
#pragma once

namespace app { struct AppState; }

namespace ui::ribbon {

// Map host AI_CAD_TAB indices (0..12) onto AppState workspace / sketch mode for screenshot shots.
void applyShotEnv(app::AppState& s);

// Draw the ribbon tool area for the active workspace (groups, flyouts, overflow).
void drawBody(app::AppState& s, float height);

} // namespace ui::ribbon
