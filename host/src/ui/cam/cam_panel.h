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
#include <utility>
#include <vector>

#include "imgui.h"
#include <nlohmann/json.hpp>

struct AppState;
struct Mesh;  // fwd (host/src/mesh.h, global namespace)

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

// ---- viewport playhead (wave 3: animated material-removal sim in the 3D view) ----
// Adopt a cam_simulate response into the playhead state (async callback + headless scenarios).
void camAdoptSimResult(const nlohmann::json& result);
// Adopt a cam_generate response into the backplot (headless scenarios; normal path is async).
void camAdoptToolpaths(const nlohmann::json& result);
// True when a sim with frames is loaded and the backplot overlay is on.
bool camSimHudActive();
// Split backplot at the current playT (lazy re-split). first = before (done), second = after.
const std::pair<const CamBackplot*, const CamBackplot*>& camBackplotSplit();
// Frame index for the current playT (-1 = no frames).
int camCurrentFrameIndex();
// Sim generation — changes whenever a new sim result is adopted (aux-mesh cache key).
int camSimGen();
// Rebuild + return the remnant-stock mesh for the current frame index.
bool camFrameMeshFor(int idx);
const Mesh& camFrameMesh();
// Tool pose interpolated at playT; false when no toolpath geometry exists.
bool camToolPoseAt(double t, float outPos[3]);
double camPlayTotalTime();
double camPlayT();
// Bottom-center transport bar over the viewport rect (screen space).
void camDrawSimHud(AppState& st, const ImVec2& vpMin, const ImVec2& vpSize);
// Shot-only: park the playhead at a fraction of total time.
void camDebugSetPlayhead(float frac);
// Stock-box overlay: visible toggle + 12 edge segments of the selected setup's box stock.
bool camStockBoxVisible();
bool camStockBoxSegments(std::vector<float>& out);

}  // namespace ui
