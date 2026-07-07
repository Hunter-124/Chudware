// Custom menu bar + window caption buttons (borderless chrome).
// Also hosts the centered workspace-mode chip cluster (Hunter 2026-07-06):
//   drawWorkspaceChips() renders the chip row; workspaceChipsWidth() measures it for
//   centering math — both live in workspace_bar.cpp but are declared here because
//   title_bar.cpp is the consumer (single caller site).
#pragma once

namespace app { struct AppState; }

namespace ui {

void drawTitleBar(app::AppState& state, float height);

// Workspace chip cluster — renders the mode-switch chips (Modeling|AI|ECAD|…) inline.
// width_hint: pre-measured total pixel width of the cluster (from workspaceChipsWidth).
// origin: screen-space X position where the cluster should start.
// height: title-bar pixel height (used for vertical centering).
void  drawWorkspaceChips(app::AppState& state, float originX, float height);
float workspaceChipsWidth(app::AppState& state);

}  // namespace ui
