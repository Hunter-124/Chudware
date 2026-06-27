// Panel draw entry points. Each takes AppState& and renders into the current ImGui frame.
#pragma once
namespace app { struct AppState; }

namespace ui {

void drawTitleBar(app::AppState& s, float height);     // app menu, doc tabs, search, window buttons
void drawWorkspaceBar(app::AppState& s, float height); // doc-type-aware mode switcher
void drawContextToolbar(app::AppState& s, float height);// verb-group tool buttons (from registry)
void drawActivityRail(app::AppState& s, float width);  // left vertical rail
void drawLeftDock(app::AppState& s);                   // tree / bodies / files / configs (rail-driven)
void drawInspector(app::AppState& s);                  // active-tool form (overlay card)
void drawViewport(app::AppState& s);                   // 3D canvas (FBO mesh or offline gradient)
void drawAgentPanel(app::AppState& s);                 // the transparent agent timeline
void drawStatusBar(app::AppState& s, float height);
void drawCommandPalette(app::AppState& s);             // ⌘K overlay

// shared widgets
bool toolButton(app::AppState& s, const char* cmdId, float square, bool* outArrow);

} // namespace ui
