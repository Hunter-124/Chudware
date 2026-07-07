// AI-CAD status bar: state + live agent phase (left), provider/model summary (right).
#pragma once

#include "imgui.h"

struct AppState;

namespace ui {

// [workspace-tabs seam] Parts-library download-queue count hook. The status bar shows
// "Downloads: N" in the Parts Library workspace when this is wired; it stays null until the
// parts subsystem assigns it (default-implemented as "no queue" — the token degrades to
// "Downloads: 0"). Lives here so the parts agent can wire it without touching the status bar.
extern int (*partsDownloadQueueCount)();

// Draw the bottom status bar. `vp` is the main viewport, `statusH` its height, `busy` whether a
// job is running (pulses the dot), `hostFlags` the shared borderless-window flags. Clicking the
// provider summary sets state.openSettings.
void drawStatusBar(AppState& state, ImGuiViewport* vp, float statusH, bool busy,
                   ImGuiWindowFlags hostFlags);

}  // namespace ui
