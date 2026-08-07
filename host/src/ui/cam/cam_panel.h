// Manufacture/CAM integrated workspace surfaces and native viewport overlay state.
#pragma once

#include <string>
#include <vector>

#include "imgui.h"
#include <nlohmann/json.hpp>

struct AppState;
struct Mesh;  // fwd (host/src/mesh.h, global namespace)

namespace ui {

struct CamBackplot;  // fwd (cam_mirror.h)

// Adopt an existing CAM job doc (for example one built by the copilot CAM fast path).
void camLoadJob(AppState& st, const std::string& docId);
// Route Manufacture ribbon commands against the integrated workspace selection.
void camHandleCommand(AppState& st, const std::string& cmd);

// Integrated left-rail bodies. They draw inside their caller's surface (no Begin/End).
void drawCamTreeBody(AppState& st);
void drawCamOutputBody(AppState& st);

// Active classified toolpath geometry for the native viewport overlay.
const CamBackplot* camActiveBackplot();

// ---- viewport playhead (wave 3: animated material-removal sim in the 3D view) ----
// Adopt a cam_simulate response into the playhead state (async callback + headless scenarios).
void camAdoptSimResult(const nlohmann::json& result);
// Adopt a cam_generate response into the backplot (headless scenarios; normal path is async).
void camAdoptToolpaths(const nlohmann::json& result);
// True when the visible native path overlay has playback geometry.
bool camSimHudActive();
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
// Viewport-local controls and telemetry; all geometry remains in the native renderer.
void camDrawViewportHud(AppState& st, const ImVec2& vpMin, const ImVec2& vpSize);
// Shot-only: park the playhead at a fraction of total time.
void camDebugSetPlayhead(float frac);
// Cached stock-box overlay geometry for the selected setup.
bool camStockBoxVisible();
const std::vector<float>* camStockBoxSegments();

}  // namespace ui
