// Context toolbar (ribbon body) — verb-group tool buttons from command_registry.
#pragma once

namespace app { struct AppState; }

namespace ui {

bool toolButton(app::AppState& s, const char* cmdId, float cellH, bool* outArrow = nullptr);
void drawContextToolbar(app::AppState& s, float height);

} // namespace ui
