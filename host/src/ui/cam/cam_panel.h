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
// Handle a CAM ribbon command (cmd ids prefixed "cam_"). Opens the panel and performs the
// action (new job / extract / add operation / generate / simulate / post / ...).
void camHandleCommand(AppState& st, const std::string& cmd);

// Viewport overlay accessor: the active job's classified toolpath segments to draw over the
// part (rapid/cut/lead/plunge/arc), or nullptr when the panel is hidden / backplot off.
const CamBackplot* camActiveBackplot();

}  // namespace ui
