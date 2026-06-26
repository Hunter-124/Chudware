// Pick overlays (C15): re-draws the hovered and selected topology over the studio-shaded viewport
// with a hover-highlight / selection-highlight color and a slight depth bias so the highlight sits
// on top of the shaded surface without z-fighting. Owns its own GPU buffers built from the same
// DocMesh as the id pass, so highlight geometry is pixel-identical to what was picked.
//
// Colors default to sensible teal-family values; once C13's theme roles (hoverHighlight /
// selectionHighlight) land they can be wired in via setColors(). GL-owning.
#pragma once

#include <array>
#include <cstdint>
#include <utility>
#include <vector>

#include "aicm_loader.h"
#include "id_pass.h"
#include "../renderer.h"

namespace render {

class Overlays {
public:
    ~Overlays();

    // (Re)build GPU buffers from the document tessellation. Requires a current GL context.
    bool setDoc(const DocMesh& doc, std::string& err);

    // Highlight colors (RGBA, 0..1). hover = the single hovered item; selection = the picked set.
    void setColors(const float hover[4], const float selection[4]);
    void setSectionClip(int count, const float normals[3][3], const float offsets[3]);

    // Draw the hovered item (pass an invalid PickHit to draw nothing) and every item in `sel`,
    // using the camera from the matching shaded frame. Renders into the currently-bound FBO
    // (the Renderer's color target) — call right after Renderer::renderOrbit and before the
    // texture is presented. Faces draw as a translucent wash; edges/verts as bright lines/points.
    void draw(const Renderer::CameraXform& cam, const PickHit& hover,
              const std::vector<PickHit>& sel);

    // C37-C: draw a set of faces with the feature-highlight tint (slightly dimmer than selection).
    // Keys are (body_id<<32)|face_id matching st.featureHighlightFaces. Stale keys are silently
    // skipped. Zero-cost when the vector is empty.
    void drawFeatureHighlight(const Renderer::CameraXform& cam,
                              const std::vector<uint64_t>& faceKeys);
    // Same, but with an explicit RGBA wash colour — used by the simulation previews to paint fixture
    // faces (green) and load faces (red) at once. `rgba` is a 4-float pointer.
    void drawFaceWash(const Renderer::CameraXform& cam,
                      const std::vector<uint64_t>& faceKeys, const float rgba[4]);

    // Draw a flat batch of world-space line segments (flat [x0,y0,z0, x1,y1,z1, …] — every 2 verts is
    // one GL_LINES segment), biased slightly over the shaded surface. Used by the simulation module to
    // draw the mesh-preview wireframe and the fixture/load/pressure indicator arrows. No-op when empty.
    void drawSegments(const Renderer::CameraXform& cam, const std::vector<float>& xyz,
                      const float rgba[4], float lineWidth = 2.0f, bool overSurface = true);

private:
    void destroy();

    // vbo here is NON-owning: it references the body's shared position VBO (bodyVbos_), uploaded
    // once per body instead of once per face. vao + ebo are owned by the FaceBuf.
    struct FaceBuf { unsigned int vao = 0, vbo = 0, ebo = 0; int idxCount = 0; };
    struct EdgeBuf { unsigned int vao = 0, vbo = 0; int vertCount = 0; };

    // lookups keyed by (body_id, topo_id)
    struct Key {
        uint32_t body = 0, topo = 0;
        bool operator==(const Key& o) const { return body == o.body && topo == o.topo; }
    };
    const FaceBuf* findFace(uint32_t body, uint32_t face_id) const;
    const EdgeBuf* findEdge(uint32_t body, uint32_t edge_id) const;
    bool findVert(uint32_t body, uint32_t vert_id, float out[3]) const;

    void drawHit(const Renderer::CameraXform& cam, const PickHit& h, const float rgba[4]);

    unsigned int prog_ = 0;     // flat color (faces wash + edges)
    unsigned int vao_ = 0, vbo_ = 0;  // scratch buffer for single-vertex point draws
    unsigned int segVao_ = 0, segVbo_ = 0;  // dynamic buffer for drawSegments line batches

    std::vector<std::pair<Key, FaceBuf>> faces_;
    std::vector<std::pair<Key, EdgeBuf>> edges_;
    std::vector<std::pair<Key, std::array<float, 3>>> verts_;
    std::vector<unsigned int> bodyVbos_;  // owns the per-body shared position VBOs (FaceBuf.vbo refs)

    float hoverCol_[4] = {0.176f, 0.831f, 0.749f, 0.45f};      // teal wash
    float selCol_[4] = {1.000f, 0.620f, 0.180f, 0.55f};        // amber wash
    // C37-C: feature-highlight color — slightly dimmer amber (same hue, lower alpha)
    float featHlCol_[4] = {1.000f, 0.620f, 0.180f, 0.30f};
    int clipCount_ = 0;
    float clipNormals_[3][3] = {{1,0,0},{0,1,0},{0,0,1}};
    float clipOffsets_[3] = {0,0,0};
};

}  // namespace render
