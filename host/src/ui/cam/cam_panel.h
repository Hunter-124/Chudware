// CamPanel — the dockable CAM (computer-aided machining) workspace. Hosts the CAM
// manager tree (job -> setups -> operations + tool library), a toolbar to extract
// stock/geometry from the current part and add operations, a 2D toolpath backplot, a
// material-removal sim summary, and the post-processor (dialect -> G-code). It drives
// the C++ core's cam_* RPCs through the WorkerClient and mirrors the job tree.
//
// Entry points follow the host's ui::drawX(AppState&) convention. The panel owns its
// state in a file-static singleton (no AppState fields), like the ECAD scaffold; it is
// revealed by any cam_* ribbon command and draws nothing until then.
#pragma once

#include <string>

struct AppState;

namespace ui {

struct CamBackplot;  // fwd (cam_mirror.h)

// Draw the CAM workspace dock window. Call every frame from the app draw loop (after the
// other panels). No-ops until a CAM command opens it.
void drawCamManager(AppState& st);

// Reveal the CAM panel (called when the Manufacture ribbon workspace is activated).
void camEnsureVisible();
// Request focus for the CAM panel this frame (brings it in front of the Viewport in the shared
// central dock node). Call every frame when the Manufacture workspace is active so the CAM tab
// wins the docked-tab race against the Viewport window.
void camRequestFocus();
// Shot-only: float the panel so a headless AI_CAD_SHOT=cam capture can show the populated tree.
void camFloatForShot();
// Adopt an EXISTING CAM job doc (e.g. one the copilot's CAM fast-path just built) into the
// panel: point the panel at docId, load its tree, and reveal it. Lets a core-side cam_* run
// surface in the GUI instead of leaving the Manufacture panel empty.
void camLoadJob(AppState& st, const std::string& docId);
// Handle a CAM ribbon command (cmd ids prefixed "cam_"). Performs the action (new job / extract
// / add operation / generate / simulate / post / show-in-3D / flavor / ...). Outside the
// Manufacture workspace it also reveals the floating panel; inside it, commands fire silently
// against the workspace tree's selection (the panel stays a double-click detail editor).
void camHandleCommand(AppState& st, const std::string& cmd);

// The Manufacture workspace LEFT TREE body (no Begin/End) — the real job structure: Job / Job
// Setup / Stock Setup / Machining Setups (operations with status) / Tools / Models, styled with
// the exported FeatureManager row helpers. drawLeftPane (layout.cpp) routes the Manufacture
// workspace here, exactly like Simulate routes to SimManager::drawTreeBody. Selection sets the
// context ribbon commands act on; double-click opens the floating panel as the detail editor.
void drawCamTreeBody(AppState& st);

// Viewport overlay accessor: the active job's classified toolpath segments to draw over the
// part (rapid/cut/lead/plunge/arc), or nullptr when the panel is hidden / backplot off.
const CamBackplot* camActiveBackplot();

}  // namespace ui
