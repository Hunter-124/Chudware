// AI-ECAD UI scaffold (Wave D) — NOT yet build-verified; see docs/ECAD_UI_INTEGRATION.md
//
// EcadPanel — the dockable ECAD editor panel. It hosts:
//   * LEFT sub-panel: for a PCB, a per-layer visibility list (copper + Edge.Cuts) with palette layer
//     swatches + checkboxes; for a schematic, the net list (name + pin count).
//   * CENTER: the EcadCanvas (2D schematic / board view).
//   * BOTTOM strip: the ERC (sch) or DRC + unrouted (pcb) results, error/warning tinted, each row
//     clickable (scaffold logs; the worker "zoom to violation" hook is documented in the guide).
//
// The panel reads everything from an ui::EcadMirror. In the scaffold the mirror + canvas live in a
// function-local state object (see ecad_panel.cpp::editorState) because the hard rules forbid adding
// fields to AppState. The integration guide (§(e)) shows the recommended permanent wiring: add an
// `EcadMirror`/`EcadCanvas`/`std::string ecadDocId` to AppState (or an owned EcadEditor*) and feed
// worker `ecad_tree` / `ecad_apply_ops` responses into the mirror — exactly like core::DocMirror.
//
// Entry point mirrors the other panels' `ui::drawX(AppState&)` convention (cf. drawFeatureTree,
// drawViewportPanel). It begins its own dock window "ECAD" and no-ops cleanly when there is no ECAD
// document loaded (shows a hint instead).
//
// Assumptions (could not build-verify):
//   * AppState exposes `worker` (WorkerClient) — confirmed in core/app_state.h — used only by the
//     OPTIONAL refresh helper. The pure-draw path needs no AppState fields beyond existing.
#pragma once

#include <string>

#include <nlohmann/json.hpp>

struct AppState;

namespace ui {

class EcadMirror;  // fwd (defined in ecad_mirror.h)

// Draw the ECAD editor dock window. Call every frame from the app draw loop, after the other panels
// (see ECAD_UI_INTEGRATION.md §(c)). Self-contained: opens/closes its own ImGui window.
void drawEcadEditor(AppState& st);

// Accessor for the scaffold's editor mirror, so external code (a ribbon command handler, a scripted
// shot) can feed it a fresh worker response without reaching through ImGui. Returns the same mirror
// drawEcadEditor() renders. (In the permanent wiring this becomes `st.ecadMirror`.)
EcadMirror& ecadEditorMirror();

// Feed a worker `ecad_tree` / `ecad_apply_ops` response object ({revision, kind, tree, compile, …})
// into the editor mirror and request a canvas fit when the document kind changes. Thin convenience
// over ecadEditorMirror().loadFromTree(); call after every successful ECAD RPC (guide §(e)).
void ecadEditorLoadResponse(const nlohmann::json& resp);

// Set the editor's selected entity to a "<kind>:<id>" token (kind in {s,fp,w,t}), or "" to clear.
// Lets a headless scenario drive selection without a mouse so a shot can show the highlight; the
// canvas haloes the matching symbol/footprint/wire/track on the next draw. (Select tool only.)
void ecadEditorSelect(const std::string& sel);

}  // namespace ui
