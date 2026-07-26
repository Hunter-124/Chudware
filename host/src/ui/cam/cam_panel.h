// CamPanel — the CAM (computer-aided machining) DETAIL panel. It is docked into a bottom split
// of the central node (ui::buildDefaultLayout), so the 3D viewport — which renders the stock box,
// the classified toolpaths, the material-removal sim frames and the transport HUD — stays visible
// directly above it. The panel therefore carries only what the 3D view and the Manufacture ribbon
// cannot: a flat toolpath backplot, the sim report, the operation parameter form and the posted
// G-code. It drives the C++ core's cam_* RPCs through the WorkerClient and mirrors the job tree.
//
// Entry points follow the host's ui::drawX(AppState&) convention. The panel owns its state in a
// file-static singleton (no AppState fields), like the ECAD scaffold; it reveals itself when the
// Manufacture workspace is entered and stays hidden (its dock node collapsed) everywhere else.
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

// Draw the CAM detail dock window. Call every frame from the app draw loop (after the other
// panels). Reveals itself on entry to the Manufacture workspace; no-ops while hidden, which lets
// its dock node collapse and gives every other workspace an edge-to-edge viewport.
void drawCamManager(AppState& st);

// Shot-only: float the panel so a headless CHUDWARE_SHOT=cam capture can show the populated tree.
void camFloatForShot();
// Adopt an EXISTING CAM job doc (e.g. one the copilot's CAM fast-path just built) into the
// panel: point the panel at docId, load its tree, and reveal it. Lets a core-side cam_* run
// surface in the GUI instead of leaving the Manufacture panel empty.
void camLoadJob(AppState& st, const std::string& docId);
// Handle a CAM ribbon command (cmd ids prefixed "cam_"). Performs the action (new job / extract
// / add operation / generate / simulate / post / show-in-3D / flavor / ...). Outside the
// Manufacture workspace it also reveals the panel; inside it, commands fire against the workspace
// tree's selection (the panel is already open under the 3D view as the detail editor).
void camHandleCommand(AppState& st, const std::string& cmd);

// The Manufacture workspace LEFT TREE body (no Begin/End) — the real job structure: Job / Job
// Setup / Stock Setup / Machining Setups (operations with status) / Tools / Models, styled with
// the exported FeatureManager row helpers. drawLeftPane (layout.cpp) routes the Manufacture
// workspace here, exactly like Simulate routes to SimManager::drawTreeBody. Selection sets the
// context the ribbon commands act on; double-click reveals the docked detail panel.
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
