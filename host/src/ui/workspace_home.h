// Workspace home: the Model workspace with no document of the user's own on screen — no tab at
// all, or only the untouched launch starter part (contracts: AppState::DocTab::isStarter). The
// empty starter scene (bare datum axes) says nothing, so this state instead
//   * widens the Explorer and Copilot columns (layout.cpp / copilot/panel.cpp read the blend),
//   * shows the Explorer as tiles (file_explorer.cpp),
//   * frosts the viewport and centres a card: what the agent is doing, any open in flight, and
//     New Part / New Assembly / Open… (drawWorkspaceHomeOverlay).
// The state ends the moment a real document becomes active, the starter gains its first
// feature or edit, or a command/sketch starts.
#pragma once

#include "imgui.h"

struct AppState;

namespace ui {

bool workspaceHomeActive(const AppState& st);

// 0 (normal layout) .. 1 (home layout), eased; one value per frame for every caller. Headless
// captures snap to the target.
float workspaceHomeBlend(const AppState& st);

// Viewport overlay (scrim + card). Call from the viewport window after its input surface;
// vr0/vr1 = the 3D image rect, cr0/cr1 = the visible content rect between the pinned columns.
void drawWorkspaceHomeOverlay(AppState& st, ImVec2 vr0, ImVec2 vr1, ImVec2 cr0, ImVec2 cr1);

}  // namespace ui
