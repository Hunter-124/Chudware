// Chudware title bar: custom menu bar + window caption buttons + chrome::HitLayout publishing.
#pragma once

struct AppState;

namespace ui {

// Draw the custom (borderless) title bar: brand mark, Chudware/View/Help menus, and the
// minimize/maximize-restore/close caption buttons on desktop. Publishes the menu + controls
// spans into state.hitLayout for the native hit-test. Sets state.openSettings / resetLayout /
// showAbout / showViewport as the user picks menu items. Browser window controls stay outside
// the app bar.
void drawTitleBar(AppState& state, bool maximized, float& barH);

}  // namespace ui
