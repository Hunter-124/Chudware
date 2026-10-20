// Chudware ribbon widget — C14.
// SolidWorks-style tab strip + grouped big/small icon buttons, driven by ribbon_def.cpp.
// Pure ImGui drawing, no new dependencies, theme-role colors only.
#pragma once

#include "imgui.h"
#include <functional>
#include <string>

namespace ui {

// Register the command-dispatch callback (called once at startup).
// Default handler logs "[ribbon] <cmd_id>" to stdout.
void setRibbonCallback(std::function<void(const char* cmd_id)> cb);

// C40: mark one command as the ACTIVE tool (accent-filled button, SolidWorks style) — the
// armed sketch tool, e.g. "sk_line" while the Line tool is drawing. Pass ""/nullptr to clear.
// Set once per frame before drawRibbon (app.cpp mirrors SketchMode's active tool).
void setRibbonActiveCmd(const char* cmd_id);

// True while a sketch is being EDITED. When false, the Sketch tab swaps its "Exit Sketch" state
// button for the Features-tab "New Sketch" button so the user can start a sketch from this tab
// (SolidWorks shows the same entry point). Set once per frame before drawRibbon.
void setRibbonSketchActive(bool active);

// True while a 3D (free-space) sketch is being EDITED. When true, the Sketch tab's command
// source swaps from the 2D tool set to the 3D sketch tool set (Line/3-Pt Arc/Spline/Point,
// working-plane presets, Construction/Relations/Fix/Delete) and the same commit cluster
// (Finish/Cancel) shown for 2D sketches fires sketch3d_exit/sketch3d_cancel instead. Mutually
// exclusive with setRibbonSketchActive in practice (only one sketch mode is active at a time).
void setRibbonSketch3DActive(bool active);

// Per-workspace ribbon filter. Must be called once per frame before drawRibbon. The value is
// the integer cast of AppState::Workspace (order: Model=0, Drawing=1, Simulate=2, Electronics=3,
// Manufacture=4, PLM=5, PartsLibrary=6, AI=7). Default is 0 (Model = show all 3D tabs).
// PLM / PartsLibrary / AI collapse the ribbon to height 0 (no band). Workspace-shell owns the
// call-site in app.cpp; ribbon.cpp defaults to Model so the band builds without the wire.
void setRibbonWorkspace(int workspace);

// sim-gui: dynamically disable every command whose cmd_id starts with `prefix` — the buttons
// render in the disabled ghost state and their tooltip shows `reason` (instead of the static
// "Coming soon"). Pass ""/nullptr as prefix to clear. Used by SimManager to grey the EM tab's
// tools while the em_* electromagnetics solver backend is not installed. Sticky until changed.
void setRibbonDisabledPrefix(const char* prefix, const char* reason);

// Human mutation commands are disabled while an agent edits the active document.
// Refresh before drawRibbon each frame; pass "" to clear. Keep-style session exits remain enabled.
void setRibbonAgentLockReason(const char* reason);

// Simulate study context: the SELECTED study's type key (SimManager's studyTypeKey: "static",
// "modal", "buckling", "contact", "freq_response", "fatigue", "thermal", "thermal_fea", "flow",
// "motion", "topology"), or "" when no study is selected. Call every frame (SimManager::tick);
// the context expires when it is not refreshed (no CAD document active). The study tabs then
// grey the commands that do not apply to that study (tooltip says why) and show the setup rows
// of study types that share a tab (Buckling / Contact on Static, Vibration on Modal) only while
// such a study is selected. See ribbonStudyGate (ribbon_def.h).
void setRibbonStudyContext(const char* studyTypeKey);

// --- Ribbon layout helpers --------------------------------------------------

// Begin the ribbon bar: reserves a full-width child window of height ribbonH.
// Returns the outer height consumed (pass to dockspace as top offset).
// activeTab: which tab index is selected (in/out).
// titleBarH: the ACTUAL custom title-bar height (from drawTitleBar's barHOut) so the ribbon docks
// directly under it. Pass <0 to fall back to ImGui::GetFrameHeight() — but note that fallback uses
// the default frame padding, which underestimates the title bar (it is drawn with a taller padding)
// and would tuck the ribbon up under the menu bar, clipping the tab strip into the top edge.
// showDrawingTools (W17): true while the active doc is a Drawing — swaps the ribbon to the
// contextual "View Layout" + "Annotation" tabs and hides the mechanical/sim/pcb tabs.
float drawRibbon(int& activeTab, bool visible, bool showSimulationTools = true,
                 bool showPcbTools = true, float titleBarH = -1.0f, bool showDrawingTools = false);

}  // namespace ui
