// Simulate workspace body — draws the simulation study column over the 3D viewport.
//
// Phase-2 hook: fill drawSimWorkspace() with additional Simulate-specific chrome
// (e.g. a workspace header bar, study selector, or results summary). The sim panel
// itself is owned by simManager().drawEmbedded() — do NOT duplicate it here;
// instead add chrome ABOVE/AROUND that call.
//
// Wiring: viewport.cpp calls drawSimWorkspace() when workspace == Simulate instead
// of the raw simManager().drawEmbedded() call that was inline there previously.
// The 3D scene + HUD still render beneath this (OverlayHosting kind).
#pragma once

#include "imgui.h"

namespace app { struct AppState; }

namespace ui {

// Simulate workspace body. Call AFTER the 3D scene has been rendered and the
// vpMin/vpMax viewport bounds are known (same contract as simManager().drawEmbedded).
// Phase-2: add workspace toolbar / study browser before the drawEmbedded call.
void drawSimWorkspace(app::AppState& s, const ImVec2& vpMin, const ImVec2& vpMax);

} // namespace ui
