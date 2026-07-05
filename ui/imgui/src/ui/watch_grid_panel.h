// T-W17 Multi-viewport Watch Grid panel — watch every agent's part at once.
// Built on ui/imgui/src/ui/panel_scaffold.h (T-U1 kit); view-model lives in
// ui/abstract/watch_grid_view.h (no ImGui includes there).
// Screenshot-verifiable via AI_CAD_SHOT=watch_grid (3 parts).
//
// A grid of live part previews: the latest render artifact per doc (loaded via
// theme/texture_cache.h loadPreviewTexture), a buildgraph node-status badge, and
// owner + model chips. Fed from workspace_status (T-W16 per-doc streams); renders
// from a committed fixture's workspace_status.json + render PNGs with no backend.
#pragma once

#include <string>

namespace app { struct AppState; }

namespace ui {

void drawWatchGridPanel(app::AppState& s);
void openWatchGridPanel();
bool* watchGridPanelOpen();

// Load a workspace_status.json (absolute path). Render paths inside are resolved
// relative to the file's directory. Populates the file-static model.
void loadWatchWorkspace(const std::string& absWorkspaceJsonPath);

}  // namespace ui
