// AI-CAD title bar: custom menu bar + window caption buttons + chrome::HitLayout publishing.
#pragma once

struct AppState;

namespace ui {

// Draw the custom (borderless) title bar: brand mark, AI-CAD/View/Help menus, and the
// minimize/maximize/close caption buttons. Publishes the menu + controls spans into
// state.hitLayout for the native hit-test. Sets state.openSettings / resetLayout / showAbout /
// showViewport as the user picks menu items.
void drawTitleBar(AppState& state, bool maximized, float& barH);

}  // namespace ui
