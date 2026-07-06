// ECAD workspace body — Schematic + PCB layout (FullViewport kind).
//
// Phase-2 hook: fill drawEcadWorkspace() with ECAD-specific chrome above/around
// the editor canvas. The ECAD editor backend is drawEcadEditor() (ecad/ecad_panel.h).
// In the global tab strip "ECAD" maps to Schematic workspace (see workspace_bar.cpp).
//
// Wiring: viewport.cpp calls drawEcadWorkspace() for both Schematic and Board
// workspaces instead of the inline drawEcadEditor() call that was there. Full-screen
// content-swap (no 3D scene beneath — FullViewport kind).
#pragma once

namespace app { struct AppState; }

namespace ui {

// ECAD workspace body. Called from viewport.cpp content-swap when workspace is
// Schematic or Board. Full-viewport takeover (no 3D scene beneath).
// Phase-2: add a workspace header bar above the canvas — "ECAD" title strip +
// Schematic / PCB tab toggle (switching between the two sub-workspaces), ERC/DRC
// buttons, and export shortcuts.
void drawEcadWorkspace(app::AppState& s);

} // namespace ui
