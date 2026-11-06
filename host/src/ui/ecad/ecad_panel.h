// KiCad project hub: read-only ECAD preview, project actions, checks and simulation.
// Each ECAD DocTab owns its own mirror, camera, selection and check result.
#pragma once

#include <string>

#include <nlohmann/json.hpp>

struct AppState;

namespace ui {

class EcadMirror;  // fwd (defined in ecad_mirror.h)
struct AdaptiveSurface;  // fwd (defined in ui_theme.h)

// Draw the read-only preview or the launchpad when no ECAD document is active.
void drawEcadEditor(AppState& st);

// Draw the project manager body in the Electronics workspace's pinned left column.
void drawEcadManagerBody(AppState& st, const ui::AdaptiveSurface& surf, unsigned int halo);

// Title for the Electronics manager header.
const char* ecadManagerTitle();

// Draw the latest KiCad DRC summary in the ribbon band for an open PCB.
void drawEcadRibbonStatus(AppState& st, float bandTopY, float bandH);

// Prime the editor's active-AppState pointer (the same one drawEcadEditor/drawEcadManagerBody set as
// their first line) WITHOUT drawing anything. Call once at the very top of the app frame, before any
// logic that might call ecadAdopt/ecadEditorLoadResponse on a frame where neither draw function has
// run yet this session — until one of them runs, the state() resolver falls back to a process-wide
// singleton instead of the active document tab's own state, so an RPC response adopted before that
// point silently lands in the wrong place.
void ecadPrimeActiveApp(AppState& st);

// Access the active ECAD document's read-only preview mirror.
EcadMirror& ecadEditorMirror();

// Adopt a tree or ops response for the active ECAD document.
void ecadEditorLoadResponse(const nlohmann::json& resp);

// Set the primary canvas selection to one frozen-contract "<kind>:<id>" token, or clear it.
// Interactive selection is ordered multi-select; this single-token hook is for scripted scenarios.
void ecadEditorSelect(const std::string& sel);
// Raw status and check RPC data, and file state, are addressed to the appropriate ECAD tab.
// A check result may include `_source_revision` to mark results from an older revision stale.
void ecadSetKicadStatus(const nlohmann::json& status);
void ecadSetCheckResult(const std::string& docId, const nlohmann::json& result);
void ecadSetFileState(const std::string& docId, const std::string& path,
                      bool dirty, bool fileChanged, bool kicadLocked);
// Switch the active project manager to its KiCad checks view.
void ecadEditorShowChecks();
void ecadEditorHighlightNet(const std::string& net);

// Pan + zoom the canvas to the first ERC/DRC violation that has a location and flash a marker there
// (what clicking a located violation row does). Returns false if no located violation exists. Lets a
// headless shot exercise "zoom to violation" without a mouse.
bool ecadEditorFocusFirstViolation();

}  // namespace ui
