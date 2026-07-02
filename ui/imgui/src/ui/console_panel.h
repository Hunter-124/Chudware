// Runtime/agent log panel — color-coded by [tag].
#pragma once

#include <string>

namespace app { struct AppState; }

namespace ui {

void appendConsoleLog(const std::string& line);
void clearConsoleLog();

// Body only (no Begin/End) — renders inside the consolidated AI panel's Console tab.
void drawConsoleBody(app::AppState& state);

}  // namespace ui
