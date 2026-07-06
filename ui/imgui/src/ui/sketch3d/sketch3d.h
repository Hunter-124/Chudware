// 3D free-space sketcher — FIRST SLICE.
//
// A lightweight, self-contained overlay: the normal 3D GL scene keeps rendering
// underneath (OverlayHosting workspace), and this module draws + edits 3D points
// and line segments placed on a selectable work plane (XY/YZ/XZ) through the
// origin. Entities are stored in world space, so they stay put as the user orbits.
//
// NO constraint solver — this is intentionally a placement-only slice.
// Snapping, dimensions, arcs, and splines are later slices.
#pragma once

#include <vector>

#include "imgui.h"
#include "renderer.h"          // Renderer::CameraXform

namespace app { struct AppState; }

namespace ui {

// --- data model -----------------------------------------------------------
struct S3Point { float x, y, z; };
struct S3Seg   { int a, b; };   // indices into Sketch3DModel::pts

struct Sketch3DModel {
    std::vector<S3Point> pts;
    std::vector<S3Seg>   segs;

    enum class Tool { Select, Point, Line };
    Tool tool = Tool::Point;

    int plane = 0;               // 0=XY, 1=YZ, 2=XZ
    int pendingLineStart = -1;   // first-click vertex index for the Line tool, or -1

    void clear() { pts.clear(); segs.clear(); pendingLineStart = -1; }
};

// Single shared instance (mirrors ui/imgui/src/ui/viewport.cpp s_sketch pattern).
Sketch3DModel& sketch3d();

// Per-frame overlay: render existing entities projected to screen, run the tools
// palette, and handle click-to-place on the active work plane. Called from the
// viewport when workspace == Sketch3D. Does NOT swap content — the GL scene stays.
void draw3DSketch(app::AppState& st, const Renderer::CameraXform& cam,
                  const ImVec2& imgMin, int vw, int vh, bool imgHovered);

// Shot/demo seed: fill the model with a small visible 3D polyline so a headless
// shot renders points + lines without interaction.
void sketch3dSeedDemo();

}  // namespace ui
