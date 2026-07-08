// RE-21: Sparse point-cloud + camera-frusta overlay for the Reverse Engineering Reconstruct stage.
//
// Owns two GL VAO/VBO pairs — one for GL_POINTS (the sparse cloud) and one for GL_LINES (frusta
// outlines). Uses the same flat-colour shader as overlays.cpp (kVert/kFrag below) so it reads from
// the same CameraXform and composes cleanly after Renderer::renderOrbit / beginOverlay.
//
// All data is fed by the caller: either a canned fixture for the shot or a real COLMAP parse once
// RE-17 populates the session directory. draw() is a no-op when no data has been uploaded.
//
// GL-thread only. Never make sync RPC calls in a draw path (known freeze bug).
#pragma once

#include <cstdint>
#include <string>
#include <vector>

#include "../renderer.h"  // Renderer::CameraXform

namespace render {

// One sparse 3D point (XYZ + RGB u8).
struct SparsePoint {
    float x, y, z;
    uint8_t r, g, b;
};

// One camera frustum — defined by its centre + four corner rays in world space.
struct CameraFrustum {
    float cx, cy, cz;          // camera centre (world)
    float c0x, c0y, c0z;      // near-plane corner 0 (TL)
    float c1x, c1y, c1z;      // near-plane corner 1 (TR)
    float c2x, c2y, c2z;      // near-plane corner 2 (BR)
    float c3x, c3y, c3z;      // near-plane corner 3 (BL)
};

class RePoints {
public:
    RePoints() = default;
    ~RePoints();
    RePoints(const RePoints&) = delete;
    RePoints& operator=(const RePoints&) = delete;

    // Upload or replace the sparse cloud. safe to call before a GL context exists on first
    // frame; lazily initialises GL resources on first draw().
    void setCloud(const std::vector<SparsePoint>& pts);

    // Upload or replace the camera frusta.
    void setFrusta(const std::vector<CameraFrustum>& frusta);

    // Point radius in on-screen pixels (before widthScale). Default 3.
    void setPointSize(float px) { pointSize_ = px > 0.1f ? px : 1.0f; }

    // Line width for the frustum wireframe in on-screen pixels. Default 1.5.
    void setLineWidth(float px) { lineWidth_ = px > 0.1f ? px : 1.0f; }

    // Screen-space width multiplier (supersample × uiScale). Default 3 (supersample-only).
    void setWidthScale(float s) { widthScale_ = s > 0.1f ? s : 1.0f; }

    // Draw the point cloud and frusta. Requires a current GL context. No-op when both empty.
    // Renders into the currently-bound FBO (call inside beginOverlay / endOverlay).
    void draw(const Renderer::CameraXform& cam);

    bool hasPoints() const { return !pts_.empty(); }
    bool hasFrusta() const { return !frusta_.empty(); }

private:
    void ensureGL();
    void destroyGL();

    // Packed XYZ + RGBA per point for the GPU (color baked at upload time).
    std::vector<float> ptVerts_;     // stride 6: x y z r g b (floats 0-1)
    // Packed XYZ for frustum line segments (GL_LINES, every 2 verts = 1 segment).
    std::vector<float> frustaLines_;

    // Source data (kept for lazy GL-init).
    std::vector<SparsePoint>   pts_;
    std::vector<CameraFrustum> frusta_;

    bool dirty_ = false;  // true when pts_/frusta_ updated but GL not yet re-uploaded

    unsigned int prog_      = 0;
    // VAO/VBO for GL_POINTS with per-vertex colour.
    unsigned int ptVao_     = 0, ptVbo_     = 0;
    // VAO/VBO for GL_LINES frustum wireframe (single RGBA colour, no per-vertex colour).
    unsigned int lineVao_   = 0, lineVbo_   = 0;
    // VAO/VBO for shared per-vertex-color point path
    unsigned int ptColVao_  = 0, ptColVbo_  = 0;

    int uMVP_    = -1;
    int uColor_  = -1;
    int uUsePVC_ = -1;  // 1 = use per-vertex color attrib, 0 = use uColor uniform

    float pointSize_  = 3.0f;
    float lineWidth_  = 1.5f;
    float widthScale_ = 3.0f;  // supersample default
};

// Build a canned sparse fixture: a roughly spherical cloud of 200 points + 6 frusta arranged
// around the origin, suitable for the AI_CAD_SHOT=reveng_recon headless shot.
void buildCannedSparseFixture(std::vector<SparsePoint>& pts, std::vector<CameraFrustum>& frusta);

}  // namespace render
