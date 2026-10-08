// Chudware Viewport panel: 3D orbit view + parts list + fit readout + live/dimension overlays.
#pragma once

#include <cstdint>

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

// Face-well chip hover hook (FEA fixtures/loads, topology wells, any face chip): call EVERY frame
// the chip is hovered with the face's AICM body id + 1-based face topo id. The viewport draws that
// face with the standard hover highlight while the claim is renewed (this or the previous frame),
// in preference to its own pick hover (which is empty while the mouse sits over a pane). The
// highlight drops the first frame nobody renews it — callers never write AppState::hover.
void claimViewportHoverFace(uint32_t bodyId, uint32_t faceId);

// The un-occluded region of the full-bleed 3D image last frame (screen coords): between the pinned
// manager column and the Copilot column, below the doc-tab band, gutter-inset. The camera frames
// the model in it; HUD/overlay owners clip or place viewport-anchored content inside it. Returns
// false before the viewport has drawn.
bool viewportClearRect(ImVec2& outMin, ImVec2& outMax);

}  // namespace ui
