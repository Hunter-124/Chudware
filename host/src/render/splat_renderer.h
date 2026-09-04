// RE-28: 3D Gaussian Splat (3DGS) renderer.
//
// Loads a Brush-style gaussian .ply (per contracts/reveng.md §6 subset — xyz, scale, rot
// quaternion, opacity, SH degree-0 color) and rasterizes each gaussian as a camera-facing
// billboard with a 2D-gaussian alpha falloff, alpha-blended back-to-front. The blend order
// is decided by a CPU depth sort recomputed whenever the camera moves; the sort runs off the
// GL thread and only the resulting index array is uploaded on the GL thread.
//
// The file is split so the loader + depth-sort are pure, GL-free free functions over plain
// structs (unit-testable without a GL context — see host/tests/test_splat_renderer.cpp), while
// the SplatRenderer class owns the GL program/buffers and does the actual draw. Null-safe: with
// no splat loaded, draw() is a no-op.
#pragma once

#include <cstdint>
#include <string>
#include <vector>

#include "../renderer.h"   // Renderer::CameraXform
#include "../vec_math.h"   // Vec3, Mat4 — header-relative, not via a target's -I host/src

namespace render {

// One gaussian, in the coordinate system of the .ply (right-handed mm after scale confirm).
struct Gaussian {
    Vec3 pos;                       // x, y, z
    Vec3 normal{0.0f, 0.0f, 1.0f};  // nx, ny, nz (default per contract §6)
    float color[3] = {0.5f, 0.5f, 0.5f};  // SH degree-0 -> base RGB (0..1)
    float opacity = 1.0f;           // 0..1 (sigmoid already applied at load)
    Vec3 scale{1.0f, 1.0f, 1.0f};   // exp(scale_i), world units
    float rot[4] = {1.0f, 0.0f, 0.0f, 0.0f};  // normalized quaternion (w, x, y, z)
};

// GL-free result of parsing a splat .ply.
struct SplatCloud {
    std::vector<Gaussian> gaussians;
    bool empty() const { return gaussians.empty(); }
    size_t size() const { return gaussians.size(); }
};

// ---- GL-free free functions (used by the ctest; no GL context required) ----

// Parse a Brush-style gaussian .ply (ascii or binary_little_endian) into `out`. Applies the
// standard activations: opacity = sigmoid(raw), scale_i = exp(raw), color = SH_C0 * f_dc + 0.5.
// Returns false + fills `err` on malformed input; never throws.
bool loadSplatPly(const std::string& path, SplatCloud& out, std::string& err);

// Same, but from an in-memory buffer (the loader entry point; the path overload wraps this).
bool parseSplatPly(const char* data, size_t len, SplatCloud& out, std::string& err);

// Depth-sort indices back-to-front (farthest gaussian first) for the given camera eye/forward.
// Returns an index permutation into `cloud.gaussians`. Pure; safe to call off the GL thread.
// `viewDir` is the camera forward (from eye toward the scene); depth is dot(pos - eye, viewDir).
std::vector<uint32_t> depthSortIndices(const SplatCloud& cloud, const Vec3& eye,
                                       const Vec3& viewDir);

// ---- GL-owning renderer (skip in the ctest) ----

class SplatRenderer {
public:
    SplatRenderer() = default;
    ~SplatRenderer();
    SplatRenderer(const SplatRenderer&) = delete;
    SplatRenderer& operator=(const SplatRenderer&) = delete;

    // True once a non-empty cloud has been loaded (draw() is a no-op otherwise).
    bool hasSplats() const { return !cloud_.empty(); }
    size_t count() const { return cloud_.size(); }

    // Load a splat cloud (CPU side; no GL needed). Returns false + fills `err` on failure.
    bool load(const std::string& path, std::string& err);
    // Adopt an already-parsed cloud (used by the shot fixture path / tests-adjacent callers).
    void setCloud(SplatCloud cloud);

    // Draw the splats as camera-facing billboards, alpha-blended back-to-front. Requires a
    // current GL context; lazily compiles the program + builds the instance buffer on first use.
    // Recomputes the depth sort when the camera eye moved past a small threshold since last draw.
    // Meant to run in the alpha pass, after opaque geometry, into the currently-bound FBO.
    void draw(const Renderer::CameraXform& cam);

private:
    bool ensureGL(std::string& err);   // compile program + create VAO/VBOs once
    void destroy();
    void uploadOrder(const std::vector<uint32_t>& order);  // GL-thread index upload

    SplatCloud cloud_;
    unsigned int prog_ = 0;
    unsigned int vao_ = 0;
    unsigned int quadVbo_ = 0;    // 4-vert unit quad (corner offsets)
    unsigned int instVbo_ = 0;    // per-gaussian instance attributes
    unsigned int orderVbo_ = 0;   // per-instance draw order (sorted index -> instance)
    int instCount_ = 0;
    bool glReady_ = false;
    bool glFailed_ = false;
    Vec3 lastEye_{1e30f, 1e30f, 1e30f};  // forces a sort on first draw
    std::vector<uint32_t> order_;

    int uMVP_ = -1, uCamRight_ = -1, uCamUp_ = -1;
};

}  // namespace render
