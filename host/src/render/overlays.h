// Pick overlays (C15): re-draws the hovered and selected topology over the studio-shaded viewport
// with a hover-highlight / selection-highlight color and a slight depth bias so the highlight sits
// on top of the shaded surface without z-fighting. Owns its own GPU buffers built from the same
// DocMesh as the id pass, so highlight geometry is pixel-identical to what was picked.
//
// Colors are wired from the theme roles (palette.hoverHighlight / selectionHighlight) via
// setColors() in app.cpp; the defaults below only cover the frames before that call. GL-owning.
#pragma once

#include <array>
#include <cstdint>
#include <unordered_map>
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

    // Highlight colors (RGBA, 0..1). hover = the single hovered item; selection = the picked set;
    // feature = the C37-C feature-highlight wash (nullptr keeps the current value — callers that
    // predate the third role compile unchanged).
    void setColors(const float hover[4], const float selection[4], const float feature[4] = nullptr);
    void setSectionClip(int count, const float normals[3][3], const float offsets[3]);

    // Screen-space width multiplier applied to every glLineWidth/glPointSize below. All overlay
    // batches draw into the renderer's supersampled FBO, so requested widths must be scaled by the
    // supersample factor (Renderer::supersample(), 3x) — and by ui::uiScale for HiDPI — to land at
    // the authored on-screen size after the downsample, matching the renderer's own feature edges.
    // Defaults to the supersample factor so widths are correct even before the caller wires uiScale.
    void setWidthScale(float s) { widthScale_ = s > 0.1f ? s : 1.0f; }

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
    // `lineWidth` is the final on-screen width; widthScale_ (supersample x uiScale) is applied inside.
    void drawSegments(const Renderer::CameraXform& cam, const std::vector<float>& xyz,
                      const float rgba[4], float lineWidth = 2.0f, bool overSurface = true);

    // Draw a flat batch of world-space triangles (flat [x,y,z] * 3 per triangle) through the same
    // clip-aware flat-color shader. Used for the solid arrowhead cones + fixture glyphs of the
    // simulation previews — the supersampled resolve anti-aliases the silhouettes for free.
    void drawTriangles(const Renderer::CameraXform& cam, const std::vector<float>& xyz,
                       const float rgba[4], bool overSurface = true);

private:
    void destroy();
    // Bind prog_ + upload the per-frame camera/clip uniforms shared by every draw path.
    void bindCommon(const Renderer::CameraXform& cam);

    // vbo here is NON-owning: it references the body's shared position VBO (bodyVbos_), uploaded
    // once per body instead of once per face. vao + ebo are owned by the FaceBuf.
    struct FaceBuf { unsigned int vao = 0, vbo = 0, ebo = 0; int idxCount = 0; };
    struct EdgeBuf { unsigned int vao = 0, vbo = 0; int vertCount = 0; };

    // lookups keyed by (body_id<<32)|topo_id — the same packing callers use for face keys.
    static uint64_t key(uint32_t body, uint32_t topo) { return ((uint64_t)body << 32) | topo; }
    const FaceBuf* findFace(uint32_t body, uint32_t face_id) const;
    const EdgeBuf* findEdge(uint32_t body, uint32_t edge_id) const;
    bool findVert(uint32_t body, uint32_t vert_id, float out[3]) const;

    void drawHit(const Renderer::CameraXform& cam, const PickHit& h, const float rgba[4]);

    unsigned int prog_ = 0;     // flat color (faces wash + edges)
    unsigned int vao_ = 0, vbo_ = 0;  // scratch buffer for single-vertex point draws
    unsigned int segVao_ = 0, segVbo_ = 0;  // dynamic buffer for drawSegments line batches
    unsigned int triVao_ = 0, triVbo_ = 0;  // dynamic buffer for drawTriangles batches
    // Uniform locations, cached once after link (queried by string per draw call before).
    int uMVP_ = -1, uColor_ = -1, uClipCount_ = -1, uClipN_ = -1, uClipOff_ = -1;

    std::unordered_map<uint64_t, FaceBuf> faces_;
    std::unordered_map<uint64_t, EdgeBuf> edges_;
    std::unordered_map<uint64_t, std::array<float, 3>> verts_;
    std::vector<unsigned int> bodyVbos_;  // owns the per-body shared position VBOs (FaceBuf.vbo refs)

    float hoverCol_[4] = {0.176f, 0.831f, 0.749f, 0.45f};      // teal wash (hover, not selected)
    // Amber's bright RGB channels (esp. R=1.0) composite lighter than teal against gray shaded
    // geometry even at higher alpha, so a plain alpha bump alone doesn't read as "more prominent
    // than hover" — the RGB is darkened/desaturated too so SELECTED unambiguously outweighs HOVER.
    float selCol_[4] = {0.850f, 0.480f, 0.100f, 0.70f};        // amber wash (selected), darkened + more opaque
    float selHoverCol_[4] = {0.550f, 0.341f, 0.099f, 0.75f};   // darker amber (selected AND hovered)
    // C37-C: feature-highlight color — slightly dimmer amber (same hue, lower alpha)
    float featHlCol_[4] = {1.000f, 0.620f, 0.180f, 0.30f};
    // Line/point width multiplier — see setWidthScale. Default = Renderer supersample factor (3x).
    float widthScale_ = 3.0f;
    int clipCount_ = 0;
    float clipNormals_[3][3] = {{1,0,0},{0,1,0},{0,0,1}};
    float clipOffsets_[3] = {0,0,0};
};

}  // namespace render
