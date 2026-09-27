// AI workspace: persistent main conversation on the left; project-scoped utility
// compartments on the right (Current Run, Plan, Activity, Sessions, Insights,
// Options). Current Run contains the execution trace, roster and one docked
// selected-agent inspector; no sub-agent popup windows.
// Inference settings live in the main Settings page; Options controls run behavior.
#pragma once

struct AppState;

namespace ui {

// Draws the full workspace body (no Begin/End of its own — app.cpp hosts it in one borderless
// window spanning the central band, same pattern as drawPlmWorkspace / drawPartsLibraryWorkspace).
// Safe to call every frame; degrades to informative empty-states when services are not running.
void drawAiWorkspace(AppState& st);

}  // namespace ui
