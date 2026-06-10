// AI-CAD Viewport panel: 3D orbit view + parts list + fit readout + live/dimension overlays.
#pragma once

#include "imgui.h"

struct AppState;

namespace ui {

// Draw the Viewport dockable window if state.showViewport. `io` drives orbit/zoom interaction,
// `busy` toggles the live-progress pill overlay.
void drawViewportPanel(AppState& state, ImGuiIO& io, bool busy);

}  // namespace ui
