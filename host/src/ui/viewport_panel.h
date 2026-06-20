// AI-CAD Viewport panel: 3D orbit view + parts list + fit readout + live/dimension overlays.
#pragma once

#include "imgui.h"

struct AppState;

namespace ui {

// Draw the Viewport dockable window if state.showViewport. `io` drives orbit/zoom interaction,
// `busy` toggles the live-progress pill overlay.
void drawViewportPanel(AppState& state, ImGuiIO& io, bool busy);

// Host the "Create New" document chooser modal (tiled previews: Part / Assembly / Drawing /
// Simulation / PCB / Open existing). Opens when state.wantNewDocChooser is set (the doc-strip "+"
// button or Ctrl+N). Drawn at app scope each frame so it works regardless of which central document
// is forward. No-op until requested.
void drawNewDocChooser(AppState& state);

}  // namespace ui
