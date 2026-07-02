// T-U1 scaffold demo panel — a minimal panel built entirely on panel_scaffold.h, used to prove the
// kit compiles/renders/screenshots and as the copy-paste starting point for T-U2..T-U8. See
// docs/UI_PATTERN_LIBRARY.md. Headless-verifiable via AI_CAD_SHOT=scaffold_demo.
#pragma once

namespace app { struct AppState; }

namespace ui {

// Body only (no Begin/End) — call inside a docked/closable window, same convention as
// drawPreviewPane().
void drawScaffoldDemoPanel(app::AppState& s);

// Open the (closable) demo pane. Costs nothing while closed.
void openScaffoldDemoPanel();

// The pane's open flag — application.cpp passes it to ImGui::Begin as p_open (close box).
bool* scaffoldDemoPanelOpen();

}  // namespace ui
