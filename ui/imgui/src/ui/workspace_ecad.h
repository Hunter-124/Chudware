// ECAD workspace body — Schematic + PCB layout (FullViewport kind).
//
// Wiring: viewport.cpp calls drawEcadWorkspace() for both Schematic and Board
// workspaces. workspace_bar.cpp must NOT auto-dispatch ecad_new_sch when no ECAD
// doc is open; instead it should set workspace=Schematic so the landing page shows.
// See shared-file-edits note in the task report.
//
// ecadHandleTo3d() is the dispatch-site delegate for "ecad_to_3d": converts the
// active PCB doc to a STEP, opens it as a new Part doc in the 3D scene, and sets
// workspace=Model. app_state.cpp dispatch() must add a one-line call (see report).
#pragma once

namespace app { struct AppState; }

namespace ui {

// ECAD workspace body. Called from viewport.cpp content-swap when workspace is
// Schematic or Board. Renders a landing page when no ECAD doc is open, otherwise
// a thin workspace header strip (Schematic/PCB sub-workspace toggle + key actions)
// above the existing ECAD canvas editor.
void drawEcadWorkspace(app::AppState& s);

// Board-to-3D dispatch delegate. Called from app_state.cpp dispatch("ecad_to_3d").
// Converts the active PCB doc to STEP via the worker RPC, opens it as a new Part
// doc (engine.open), pushes a DocTab, sets workspace=Model, and links the new Part
// tab back to the Board tab via DocTab::ecadDocId for the section switcher.
void ecadHandleTo3d(app::AppState& s);

} // namespace ui
