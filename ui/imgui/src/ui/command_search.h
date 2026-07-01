// Unified command + settings search popup (title-bar Search / future menu item).
#pragma once

#include "ui/settings_modal.h"

namespace app { struct AppState; }

namespace ui {

void setCommandSearchOpen(bool open);
bool commandSearchOpen();

// Consumed by settings modal (S6) after a search result selects a settings tab.
bool takeOpenSettingsRequest(int* tabIndexOut = nullptr);
void requestOpenSettings(int tabIndex = -1);

void drawCommandSearch(app::AppState& st);

} // namespace ui
