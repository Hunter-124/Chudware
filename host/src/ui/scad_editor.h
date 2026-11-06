#pragma once
#include <string>
struct AppState;
namespace ui {
void openScadEditor(AppState&, const std::string& docId, const std::string& featureId);
void openNewScadEditor(AppState&);
void openScadConversion(AppState&);
void drawScadEditor(AppState&);
}
