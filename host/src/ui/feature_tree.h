// FeatureManager tree panel (C16).
// Renders the left dock panel — fixed nodes (Equations, Material, planes, Origin) followed
// by the feature history with status badges, context menu (Rename/Suppress/Delete/Edit).
// Selecting a tree feature highlights its owned faces in the viewport via the selection set.
// The panel calls back into C17's edit dispatcher via the optional EditCallback slot.
#pragma once

#include <functional>
#include <string>

struct AppState;

namespace ui {

// Draw the FeatureManager tree panel. Call every frame from the main loop.
// editCallback is invoked when "Edit" is chosen from a feature's context menu;
// it receives the feature id. Pass {} until C17 wires it.
void drawFeatureTree(AppState& st,
                     const std::function<void(const std::string& featureId)>& editCallback = {});

}  // namespace ui
