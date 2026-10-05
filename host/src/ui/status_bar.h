// Chudware status bar: state + live agent phase (left), provider/model summary (right).
#pragma once

#include "imgui.h"

struct AppState;

namespace ui {

// Draw the bottom status bar. `vp` is the main viewport, `statusH` its height, `busy` whether a
// job is running (pulses the dot), `hostFlags` the shared borderless-window flags. Clicking the
// provider summary sets state.openSettings.
void drawStatusBar(AppState& state, ImGuiViewport* vp, float statusH, bool busy,
                   ImGuiWindowFlags hostFlags);

}  // namespace ui
