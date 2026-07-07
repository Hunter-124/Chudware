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
struct AdaptiveSurface;  // fwd (defined in ui_theme.h)

// Draw the ECAD editor dock window. Call every frame from the app draw loop, after the other panels
// (see ECAD_UI_INTEGRATION.md §(c)). Self-contained: opens/closes its own ImGui window.
void drawEcadEditor(AppState& st);

// Draw the "PCB Manager" / "Schematic Manager" body — the ECAD analogue of the Feature Manager tree,
// restyled to match its chrome (pane-header chip, glass icon toolbar, tree-row layers/nets/symbols).
// Hosted INSIDE the shared pinned left column (layout.cpp::drawLeftPane) when the Electronics
// workspace is active, so the ECAD tab's left column shares the FeatureManager column's exact
// geometry (position/width) — this is what keeps the doc-tab strip anchored at the same screen X as
// the Model tab (see doc_tabs.cpp's overlayColumnEdge(0) consumer). No Begin/End of its own — the
// caller owns the surrounding pinned window. `surf`/`halo` are the caller's already-resolved adaptive
// glass surface + icon halo (same ones used for the FeatureManager body) so icon/text tinting matches.
void drawEcadManagerBody(AppState& st, const ui::AdaptiveSurface& surf, unsigned int halo);

// Title for the manager's header chip ("PCB Manager" / "Schematic Manager"), reflecting the open
// doc kind. The Electronics rail draws this chip beside its tab buttons (round-5b), so the header is
// no longer owned by drawEcadManagerBody's body.
const char* ecadManagerTitle();

// Draw the PCB DRC / unrouted summary as a right-aligned glass pill in the ribbon band's trailing
// dead space. Call from the app draw loop right after drawRibbon when the Electronics workspace is
// active, passing the ribbon band's top Y (screen space) and height. No-op for schematic docs (they
// keep their own bottom ERC strip) and when no board is open. This is what lets the board canvas
// reclaim the full height (its old bottom results strip is gone).
void drawEcadRibbonStatus(AppState& st, float bandTopY, float bandH);

// Prime the editor's active-AppState pointer (the same one drawEcadEditor/drawEcadManagerBody set as
// their first line) WITHOUT drawing anything. Call once at the very top of the app frame, before any
// workspace-entry logic that might call ecadAdopt/ecadEditorLoadResponse (e.g. the Electronics
// workspace's auto-init-PCB1) on a frame where neither draw function has run yet this session — until
// one of them runs, the state() resolver falls back to a process-wide singleton instead of the active
// document tab's own state, so an RPC response adopted before that point silently lands in the wrong
// place (loads, but the manager column / canvas the user actually sees still reads empty).
void ecadPrimeActiveApp(AppState& st);

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

// Pan + zoom the canvas to the first ERC/DRC violation that has a location and flash a marker there
// (what clicking a located violation row does). Returns false if no located violation exists. Lets a
// headless shot exercise "zoom to violation" without a mouse.
bool ecadEditorFocusFirstViolation();

}  // namespace ui
