// OpenGL mesh renderer: renders one or more colored Meshes to an offscreen framebuffer
// (supersampled), supports the named CAD views, and reads back pixels for PNG screenshots.
// The same color texture is displayed live in the ImGui viewport. Multi-part assemblies
// render each body in its own color (matching the engine's contact-sheet palette).
#pragma once
#include <string>
#include <vector>

#include "mesh.h"

class Renderer {
public:
    // One body of the displayed model: geometry + its display color (RGB 0..1).
    struct PartMesh {
        Mesh mesh;
        float color[3] = {0.62f, 0.70f, 0.80f};
    };

    ~Renderer();
    bool init(int width, int height, std::string& err);  // requires a current GL context
    void resize(int width, int height);
    void setMesh(const Mesh& m);                          // single body, neutral color
    void setMeshes(const std::vector<PartMesh>& parts);   // multi-body, per-part colors
    void setBackground(float tr, float tg, float tb, float br, float bg, float bb);
    void renderView(const std::string& view);            // "iso"|"front"|"right"|"top"
    void renderOrbit(float yawDeg, float pitchDeg, float zoom);

    std::vector<unsigned char> readRGBA() const;         // final w*h*4, top-left origin
    unsigned int colorTexture() const { return colorTex_; }
    int width() const { return w_; }
    int height() const { return h_; }
    bool hasMesh() const { return !subs_.empty(); }

private:
    // GPU buffers for one body.
    struct Sub {
        unsigned int vao = 0, vboPos = 0, vboNrm = 0, ebo = 0;
        unsigned int edgeVao = 0, vboEdge = 0;
        int idxCount = 0, edgeVerts = 0;
        float color[3] = {0.62f, 0.70f, 0.80f};
    };

    void allocTargets();
    void drawCurrent(float yawDeg, float pitchDeg, float zoom);
    void freeSubs();

    static constexpr int SS = 2;  // supersample factor for anti-aliasing
    int w_ = 0, h_ = 0;           // final size
    int sw_ = 0, sh_ = 0;         // supersampled size
    unsigned int fbo_ = 0, colorTex_ = 0, depthRbo_ = 0;
    unsigned int prog_ = 0;
    unsigned int edgeProg_ = 0;
    unsigned int bgProg_ = 0, bgVao_ = 0;
    float bgTop_[3] = {0.95f, 0.96f, 0.97f};
    float bgBot_[3] = {0.88f, 0.90f, 0.93f};
    std::vector<Sub> subs_;
    Vec3 center_;
    float radius_ = 1.0f;
};
