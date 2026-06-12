// ID-buffer picking pass (PLAN §3.1). Renders the DocMesh into an offscreen R32UI attachment
// where every fragment carries a packed topology id (body|kind|topo-id); a 1x1 glReadPixels under
// the cursor decodes to a PickHit. Draw order + a depth bias give vertices priority over edges
// over faces at equal depth. An RGBA8-encoded fallback (env AI_CAD_IDBUF=rgba8) covers drivers /
// contexts without integer color attachments.
//
// The pass owns its own GPU buffers built from the DocMesh (independent of Renderer's STL Subs)
// and renders with the Renderer's published CameraXform so the id buffer aligns pixel-for-pixel
// with the shaded viewport. GL-owning: build()/destroy() need a current context.
#pragma once

#include <cstdint>
#include <set>
#include <vector>

#include "aicm_loader.h"
#include "../renderer.h"

namespace render {

// What kind of topology a pixel belongs to. Order matters: higher value = higher pick priority.
enum class PickKind : uint8_t { None = 0, Face = 1, Edge = 2, Vertex = 3 };

// A decoded pick. body_id 0 / kind None == empty space (background).
struct PickHit {
    uint32_t body_id = 0;
    PickKind kind = PickKind::None;
    uint32_t topo_id = 0;  // face_id / edge_id / vertex_id depending on kind

    bool valid() const { return kind != PickKind::None && body_id != 0; }
    bool operator==(const PickHit& o) const {
        return body_id == o.body_id && kind == o.kind && topo_id == o.topo_id;
    }
    bool operator!=(const PickHit& o) const { return !(*this == o); }
};

// ---- packed id encoding: [31:24] body (8b) | [23:22] kind (2b) | [21:0] topo id (22b) ----
inline uint32_t encodeId(uint32_t body_id, PickKind kind, uint32_t topo_id) {
    return ((body_id & 0xFFu) << 24) | ((uint32_t(kind) & 0x3u) << 22) | (topo_id & 0x3FFFFFu);
}
inline PickHit decodeId(uint32_t v) {
    PickHit h;
    if (v == 0) return h;  // background
    h.body_id = (v >> 24) & 0xFFu;
    h.kind = PickKind((v >> 22) & 0x3u);
    h.topo_id = v & 0x3FFFFFu;
    return h;
}

class IdPass {
public:
    ~IdPass();

    // (Re)build GPU buffers from the document tessellation. Safe to call repeatedly. Requires a
    // current GL context. Returns false (err set) on shader/FBO failure.
    bool setDoc(const DocMesh& doc, std::string& err);

    // Render the id buffer for the given camera (matches a Renderer::camera() frame), sizing the
    // R32UI attachment to cam.ssWidth x cam.ssHeight.
    void render(const Renderer::CameraXform& cam);

    // Pick the pixel at (px, py) in FINAL (non-supersampled) viewport coordinates, top-left
    // origin (ImGui convention). Internally scales by the supersample factor and flips to GL's
    // bottom-left origin. Must be called after render() for the same frame. Returns an empty hit
    // for out-of-range / background pixels.
    PickHit pickFinal(int px, int py, int finalW, int finalH);

    bool usesRgba8() const { return rgba8_; }

    // Solid Bodies hide/show: bodies in this set are skipped by render(), so hidden geometry is
    // unpickable (a click goes through to whatever is behind it — never an invisible hit).
    // Persists across setDoc(); AppState::applyBodyVisibility() re-syncs it after every reload
    // and clearViewportForNewDoc() clears it.
    void setHiddenBodies(const std::set<uint32_t>& bodyIds) { hidden_ = bodyIds; }

private:
    // True when the encoded id belongs to a hidden body (body id lives in bits [31:24]).
    bool bodyHidden(uint32_t encoded) const {
        return !hidden_.empty() && hidden_.count((encoded >> 24) & 0xFFu) != 0;
    }
    void destroy();
    void allocTargets(int w, int h);

    bool built_ = false;
    bool rgba8_ = false;  // fallback encoding (env AI_CAD_IDBUF=rgba8)

    // offscreen target
    unsigned int fbo_ = 0, idTex_ = 0, depthRbo_ = 0;
    int w_ = 0, h_ = 0;

    // shaders: one uniform-color id shader reused for faces (tris), edges (lines), verts (points)
    unsigned int prog_ = 0;

    // per-body face geometry, one draw per FACE range with its encoded id as a uniform
    struct FaceDraw {
        unsigned int vao = 0, vboPos = 0, ebo = 0;
        int idxCount = 0;
        uint32_t id = 0;  // encoded
    };
    struct EdgeDraw {
        unsigned int vao = 0, vbo = 0;
        int vertCount = 0;  // line-list vertex count
        uint32_t id = 0;
    };
    struct VertDraw {
        unsigned int vao = 0, vbo = 0;
        int count = 0;  // points
        std::vector<uint32_t> ids;  // one per point (drawn individually for per-vertex id)
    };
    std::vector<FaceDraw> faces_;
    std::vector<EdgeDraw> edges_;
    VertDraw verts_;  // all bodies' brep verts in one buffer; ids drawn one point at a time
    std::set<uint32_t> hidden_;  // AICM body ids hidden in the viewport (skip in render())
};

}  // namespace render
