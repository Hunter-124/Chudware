// File Explorer panel — workspace browser for the left dock "Files" rail tab.
//
// Renders into the caller's container child (no Begin/End). G2 wires
// `drawFileExplorerBody` from `drawLeftDock` when `railTab == 2`.
#pragma once

#include <string>

namespace app { struct AppState; }

namespace ui {

constexpr int kFilesRailTab = 2;

void ensureWorkspaceDir(app::AppState& st);

// Draw the Explorer tab body (no Begin/End).
void drawFileExplorerBody(app::AppState& st);

// Ctrl+C / Ctrl+V routing when the Files rail tab is active.
bool fileExplorerActive(const app::AppState& st);
bool fileExplorerCopySelection(app::AppState& st);
bool fileExplorerPaste(app::AppState& st);

// Double-click / Open dispatches here; the app pump calls `takeExplorerOpenRequest()`.
std::string takeExplorerOpenRequest();

}  // namespace ui
