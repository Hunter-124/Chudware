#pragma once
// T-U7 structural frame viewer panel (read-only v1).
#include <string>
namespace app { struct AppState; }
namespace ui {
void drawStructuralPanel(app::AppState& s);
void openStructuralPanel();
bool* structuralPanelOpen();
void seedStructuralPanelFromJson(const std::string& doc_json, const std::string& results_json);
}  // namespace ui
