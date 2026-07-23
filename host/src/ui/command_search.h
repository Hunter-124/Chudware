// Chudware unified command + settings search widget (W2-Search).
// Opened by the top-bar "Search" menu item; searches both ribbon modeling commands and
// settings entries. Calls must be placed per-frame in app.cpp (see INTEGRATION NEEDED).
#pragma once

struct AppState;

namespace ui {

// Draw the command-search popup when st.showCommandSearch is true.
// Presents a centered glass window with an InputText filter and a scrollable results list
// covering (a) ribbon modeling commands and (b) settings entries. Activating a modeling
// tool dispatches via st.ribbonDispatch; activating a settings entry opens the Settings modal
// at the matched tab (st.openSettings + st.openSettingsTab). ESC or selecting an item closes.
void drawCommandSearch(AppState& st);

}  // namespace ui
