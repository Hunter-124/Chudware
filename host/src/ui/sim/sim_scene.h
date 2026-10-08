// sim/sim_scene.h — the viewport <-> simulation-panel 3D scene seam.
//
// Panels that author geometry in 3D (Piping networks, Hydraulic circuits) publish shaded
// triangle batches and line batches here, and may own left-click input in the viewport while
// one of their tools is armed (draw a pipe run by clicking on a working plane, insert a fitting,
// place a component). viewport_panel.cpp is the only caller; it knows nothing about piping or
// hydraulics. The dispatcher (sim_scene.cpp) forwards to the open panel's producer:
//   ui::pipescene  (sim/pipe_scene.cpp)       — PipingPanel
//   ui::hydscene   (sim/hydraulic_scene.cpp)  — HydraulicPanel
//
// Contract
//   * active()/collect() run every viewport frame in the Simulate workspace, with or without a
//     document mesh (an empty part is a valid canvas). collect() must be cheap: producers return
//     cached buffers and bump `revision` only when the vertices change.
//   * Batch keys: piping 0x1000..0x1FFF, hydraulics 0x2000..0x2FFF (viewport's own simulation
//     batches use keys below 0x1000).
//   * handleViewport() runs once per frame after the 3D image is drawn, while the viewport image
//     is visible. It may read ImGui IO (mouse, keys) and draw screen-space HUD into `dl`
//     (rubber band, snap markers, tooltips). It returns true when it consumed this frame's
//     left click; the viewport then skips its own model selection for the frame. Left DRAG and
//     MMB/wheel navigation always stay with the viewport (a click is a press+release that did not
//     pass ImGui's drag threshold).
//   * toolArmed(): a tool owns plain left clicks and Esc/Enter/Backspace/Tab while the viewport
//     is hovered; the viewport suppresses selection, box-select and its Esc handling.
//   * onPlaneChip(): the viewport's X/Y, Y/Z, X/Z reference-plane chips route here first while
//     a tool is armed (pick the working plane) instead of selecting the datum.
#pragma once

#include <cstdint>
#include <vector>

#include "imgui.h"
#include "../../renderer.h"

struct AppState;

namespace ui::simscene {

enum class Plane : int { XY = 0, XZ = 1, YZ = 2 };

// Non-indexed world-space triangles (mm): xyz and nrm are flat [x,y,z]*3 per triangle, same
// length. rgb (optional, same vertex count, flat [r,g,b]) colours per vertex — e.g. pipes coloured
// by solved pressure/velocity; null = uniform rgba. rgba[3] < 1 draws translucent (ghost preview).
struct LitBatch {
    std::uint64_t key = 0;
    const std::vector<float>* xyz = nullptr;
    const std::vector<float>* nrm = nullptr;
    const std::vector<float>* rgb = nullptr;
    float rgba[4] = {0.7f, 0.7f, 0.7f, 1.0f};
    std::uint64_t revision = 0;  // non-zero; changes whenever xyz/nrm/rgb change
    // Persistent model geometry enlarges the framing radius; transient previews (rubber band,
    // ghost tube/glyph, cursor grid, hover) set this false so the camera never "breathes".
    bool bounds = true;
};

// GL_LINES world-space segments (flat [x0,y0,z0,x1,y1,z1]*n).
struct LineBatch {
    std::uint64_t key = 0;
    const std::vector<float>* xyz = nullptr;
    float rgba[4] = {1.0f, 1.0f, 1.0f, 1.0f};
    float width = 2.0f;          // on-screen px (viewport applies supersample/uiScale)
    bool overSurface = true;     // depth-biased over shaded surfaces
    std::uint64_t revision = 0;  // non-zero
    bool bounds = true;          // see LitBatch::bounds
};

struct Scene {
    std::vector<LitBatch> lit;
    std::vector<LineBatch> lines;
    void clear() { lit.clear(); lines.clear(); }
};

struct ViewportCtx {
    Renderer::CameraXform cam;   // camera of the frame just drawn
    ImVec2 imgMin{0, 0};         // viewport image rect on screen (logical px)
    ImVec2 imgMax{0, 0};
    int vw = 0, vh = 0;          // viewport image size (logical px)
    bool hovered = false;        // the image is hovered and no popup/modal owns input
    bool navigating = false;     // an orbit/pan/zoom drag is in progress this frame
};

// Screen <-> world helpers shared by every producer (implemented in sim_scene.cpp).
// Ray through a screen point (logical px): origin + unit direction, world mm.
void screenRay(const ViewportCtx& vp, ImVec2 mouse, float orig[3], float dir[3]);
// World point -> screen (logical px); false when behind the camera.
bool worldToScreen(const ViewportCtx& vp, const float p[3], ImVec2& out);

bool active(AppState& st);
void collect(AppState& st, Scene& out);
bool toolArmed(AppState& st);
bool handleViewport(AppState& st, const ViewportCtx& vp, ImDrawList* dl);
bool onPlaneChip(AppState& st, Plane plane);

}  // namespace ui::simscene

// Producer entry points — same meaning as above, each only called while its panel is open.
namespace ui::pipescene {
bool active(AppState& st);
void collect(AppState& st, ui::simscene::Scene& out);
bool toolArmed(AppState& st);
bool handleViewport(AppState& st, const ui::simscene::ViewportCtx& vp, ImDrawList* dl);
bool onPlaneChip(AppState& st, ui::simscene::Plane plane);
}  // namespace ui::pipescene

namespace ui::hydscene {
bool active(AppState& st);
void collect(AppState& st, ui::simscene::Scene& out);
bool toolArmed(AppState& st);
bool handleViewport(AppState& st, const ui::simscene::ViewportCtx& vp, ImDrawList* dl);
bool onPlaneChip(AppState& st, ui::simscene::Plane plane);
}  // namespace ui::hydscene
