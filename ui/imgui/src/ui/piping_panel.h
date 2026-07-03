#pragma once
// T-U7 piping network viewer panel (read-only v1).
// Renders an aicad.piping/1 doc as a schematic plan-view network + solve results.
// Built on net_canvas_panel (shared with hydraulic + structural, T-U7).
#include <string>
namespace app { struct AppState; }
namespace ui {
void drawPipingPanel(app::AppState& s);
void openPipingPanel();
bool* pipingPanelOpen();
// Seed the panel from pre-fetched JSON (scenario/shot use). doc_json = piping doc;
// solve_json = piping_solve result or "" if not available.
void seedPipingPanelFromJson(const std::string& doc_json, const std::string& solve_json);
}  // namespace ui
