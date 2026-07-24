// Reverse Engineering workspace: full-viewport body for the "Reverse Eng" title-bar tab.
// Honest scan -> mesh -> AI -> parametric pipeline shell (contracts/reveng.md).
//
// RE-40 layout: a session rail on the left (rich cards, filter, New-session card), the stage
// STEPPER pinned at the TOP of the centre column above the active stage body, and a
// Details / Capabilities / Provenance properties column on the right. Both side columns
// collapse to a header strip + chevron.
#pragma once

struct AppState;

namespace ui {

// Draws the full workspace body (no Begin/End of its own — app.cpp hosts it in one borderless
// window spanning the central band, same pattern as drawAiWorkspace / drawPartsLibraryWorkspace).
// Safe to call every frame; degrades to informative empty/blocked states when the reveng MCP
// tools are not registered. GL-thread only.
void drawRevEngWorkspace(AppState& st);

// Release the resources the workspace holds outside the frame loop — currently the webcam
// device + its preview texture (ui::reveng::webcamCaptureShutdown) and the session-rail
// thumbnail index.
//
// INTEGRATOR SEAM: app.cpp has no "workspace closed" callback, so nothing calls this on the edge
// where the user leaves the Reverse Eng tab; drawRevEngWorkspace self-heals by running it on
// re-entry after a frame gap, which reclaims the camera one frame late. Call it from app.cpp
// when activeWorkspace transitions AWAY from Workspace::ReverseEng (and once at shutdown, before
// the GL context is destroyed) to close the camera the instant the tab is left.
void revengWorkspaceTeardown();

}  // namespace ui
