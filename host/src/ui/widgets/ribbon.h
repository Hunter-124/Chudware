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

// --- Ribbon layout helpers --------------------------------------------------

// Begin the ribbon bar: reserves a full-width child window of height ribbonH.
// Returns the outer height consumed (pass to dockspace as top offset).
// activeTab: which tab index is selected (in/out).
float drawRibbon(int& activeTab, bool visible);

// Returns the ribbon height in pixels for layout math.
float ribbonHeight();

}  // namespace ui
