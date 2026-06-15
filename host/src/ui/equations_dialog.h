// Equations / Global Variables editor (SolidWorks "Equations" dialog).
//
// A small modeless window, gated by AppState::showEquations, that lists the document's global
// variables (doc.equations.globals = [{name, expr}, ...]) and lets the user add / edit / delete
// them. Mutations go through the engine's doc ops:
//   - eq_set    {name, expr}  — add or update (name must match ^[A-Za-z_]\w*$; same name updates)
//   - eq_delete {name}        — remove (engine errors with code "eq_in_use" if a feature uses it)
// applied via st.docMirror.applyOps(); on success the viewport mesh is reloaded from the result's
// aicm so dependent dimensions update (same flow as AsmPanels::addMate).
#pragma once

struct AppState;

namespace ui {

// Draw the Equations editor window when st.showEquations is true (no-op otherwise). Called once
// per frame from the main draw loop, alongside the other panels/dialogs (asmPanels.draw, etc.).
void drawEquationsDialog(AppState& st);

}  // namespace ui
