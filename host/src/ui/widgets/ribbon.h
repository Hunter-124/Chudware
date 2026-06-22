// AI-CAD ribbon widget — C14.
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

// Returns the ribbon height in pixels for layout math.
float ribbonHeight();

}  // namespace ui
