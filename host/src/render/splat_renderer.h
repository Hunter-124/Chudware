// Native anisotropic Gaussian splats in raw reconstruction coordinates.
#pragma once

#include <cstdint>
#ifndef CHUDWARE_GLES3
#include <future>
#endif
#include <string>
#include <vector>

#include "../renderer.h"
#include "../vec_math.h"

namespace render {

struct Gaussian {
    Vec3 pos;
    Vec3 normal{0.0f, 0.0f, 1.0f};
    // Unclamped degree-zero RGB: higher SH bands are added before clamping.
    float color[3] = {0.5f, 0.5f, 0.5f};
    float opacity = 1.0f;
    Vec3 scale{1.0f, 1.0f, 1.0f};
    float rot[4] = {1.0f, 0.0f, 0.0f, 0.0f}; // normalized w,x,y,z
    float sh[45] = {}; // channel-major, 15 coefficients per channel
    int shDegree = 0;
};

struct SplatCloud {
    std::vector<Gaussian> gaussians;
    bool empty() const { return gaussians.empty(); }
    size_t size() const { return gaussians.size(); }
};

// Strict scalar-vertex PLY, ASCII or little endian. Failure clears out and sets err.
// Counts, byte sizes and finite activated values are bounded before cloud allocation.
bool loadSplatPly(const std::string& path, SplatCloud& out, std::string& err);
bool parseSplatPly(const char* data, size_t len, SplatCloud& out, std::string& err);
std::vector<uint32_t> depthSortIndices(const SplatCloud& cloud, const Vec3& eye,
                                     const Vec3& viewDir);
// Packed symmetric world covariance: xx,xy,xz,yy,yz,zz. Used by GPU upload and bounds.
void gaussianCovariance(const Gaussian& gaussian, float covariance[6]);

class SplatRenderer {
public:
    SplatRenderer() = default;
    ~SplatRenderer();
    SplatRenderer(const SplatRenderer&) = delete;
    SplatRenderer& operator=(const SplatRenderer&) = delete;

    bool hasSplats() const { return !cloud_.empty(); }
    size_t count() const { return cloud_.size(); }
    bool bounds(Vec3& min, Vec3& max) const;
    bool load(const std::string& path, std::string& err);
    void setCloud(SplatCloud cloud);
    // Projects full covariance with the MVP Jacobian. Immutable attributes stay on GPU;
    // only sorted uint indices change with the eye or direction. Restores touched GL state.
    void draw(const Renderer::CameraXform& cam);

private:
    bool ensureGL(std::string& err);
    bool uploadCloud(std::string& err);
    void destroy();
    void uploadOrder(const std::vector<uint32_t>& order);

    SplatCloud cloud_;
    unsigned int prog_ = 0, vao_ = 0, quadVbo_ = 0, dataTex_ = 0, orderVbo_ = 0;
    int instCount_ = 0, textureWidth_ = 0;
    bool glReady_ = false, glFailed_ = false, dataDirty_ = true;
    Vec3 lastEye_{1e30f, 1e30f, 1e30f}, lastDir_{};
    Vec3 min_{}, max_{};
    std::vector<uint32_t> order_;
#ifndef CHUDWARE_GLES3
    std::future<std::vector<uint32_t>> sort_;
    Vec3 sortingEye_{}, sortingDir_{};
#endif
    int uMVP_ = -1, uEye_ = -1, uViewport_ = -1, uData_ = -1, uTextureWidth_ = -1;
};

} // namespace render
