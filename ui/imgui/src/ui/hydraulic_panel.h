#pragma once
// T-U7 hydraulic schematic viewer panel (read-only v1).
#include <string>
namespace app { struct AppState; }
namespace ui {
void drawHydraulicPanel(app::AppState& s);
void openHydraulicPanel();
bool* hydraulicPanelOpen();
void seedHydraulicPanelFromJson(const std::string& doc_json, const std::string& sizing_json);
}  // namespace ui
