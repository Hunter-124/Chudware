// Reverse Engineering workspace: full-viewport body for the "Reverse Eng" title-bar tab.
// Honest scan -> mesh -> AI -> parametric pipeline shell (contracts/reveng.md). Left
// session rail (populated via ASYNC list_reveng_sessions rpc), a stage-driven center with
// per-stage empty-states, a bottom stage stepper, and a right properties column.
#pragma once

struct AppState;

namespace ui {

// Draws the full workspace body (no Begin/End of its own — app.cpp hosts it in one
// borderless window spanning the central band, same pattern as drawAiWorkspace /
// drawPartsLibraryWorkspace). Safe to call every frame; degrades to informative
// empty-states when the reveng MCP tools are not registered. GL-thread only.
void drawRevEngWorkspace(AppState& st);

}  // namespace ui
