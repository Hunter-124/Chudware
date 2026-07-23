// ID-buffer picking pass (PLAN §3.1). Renders the DocMesh into an offscreen R32UI attachment
// where every fragment carries a packed topology id (body|kind|topo-id); a 1x1 glReadPixels under
// the cursor decodes to a PickHit. Draw order + a depth bias give vertices priority over edges
// over faces at equal depth. An RGBA8-encoded fallback (env CHUDWARE_IDBUF=rgba8) covers drivers /
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

// ---- id-pass frame caching (task 01: selection lag) ----
// The id buffer is an offscreen FBO that nothing else draws into, so its contents stay valid across
// frames as long as the inputs that produced them are unchanged. render() fingerprints those inputs
// into an IdPassFrameKey and skips the per-body draw work when the key matches the last rendered
// frame — turning a static-camera hover from O(bodies) draw calls every frame into a no-op. This is
// the dominant per-frame pick cost in large assemblies. Kept pure/inline so it unit-tests w/o GL.

// FNV-1a fold of the hidden-body ids. std::set iterates ascending, so the result is deterministic
// and independent of insertion order — toggling a body's visibility changes the digest, nothing
// else does.
inline uint64_t idPassHiddenHash(const std::set<uint32_t>& hidden) {
    uint64_t h = 1469598103934665603ull;  // FNV-1a offset basis
    for (uint32_t id : hidden) {
        h ^= (uint64_t)id;
        h *= 1099511628211ull;  // FNV-1a prime
    }
    return h;
}

// Fingerprint of everything render() consults to produce the id buffer. Exact float compare is
// intentional: the camera matrix is recomputed deterministically from the same yaw/pitch/zoom/pan,
// so a static camera yields bit-identical mvp (→ skip), and any motion or tween changes it (→
// redraw, so picks stay fresh during movement). Only the active clip planes participate.
struct IdPassFrameKey {
    float mvp[16] = {0};
    int w = 0, h = 0;
    int clipCount = 0;
    float clipN[9] = {0};
    float clipOff[3] = {0};
    uint64_t hiddenHash = 0;
    uint64_t docGen = 0;  // bumped on every setDoc() so a rebuilt doc always re-renders

    bool operator==(const IdPassFrameKey& o) const {
        if (w != o.w || h != o.h || clipCount != o.clipCount || hiddenHash != o.hiddenHash ||
            docGen != o.docGen)
            return false;
        for (int i = 0; i < 16; ++i)
            if (mvp[i] != o.mvp[i]) return false;
        for (int i = 0; i < clipCount * 3; ++i)
            if (clipN[i] != o.clipN[i]) return false;
        for (int i = 0; i < clipCount; ++i)
            if (clipOff[i] != o.clipOff[i]) return false;
        return true;
    }
    bool operator!=(const IdPassFrameKey& o) const { return !(*this == o); }
};

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

    // Like pickFinal, but if the exact pixel is a Face (or background) it searches outward up to
    // radiusPx (final-viewport pixels) for a thin target and returns that instead. Precedence is by
    // KIND first, distance second (SolidWorks/Fusion pick feel): ANY vertex in range beats EVERY
    // edge, and any edge/vertex beats the center face — points and edges are smaller targets than
    // faces, so they deserve a tolerance. For tools that target model edges (Convert Entities, blend
    // edge wells) and for general viewport hover: a 1-px-wide edge/vertex sitting on a large coplanar
    // face is otherwise nearly unclickable because the face wins the single center pixel. A center
    // hit that is already an edge/vertex is returned unchanged.
    PickHit pickFinalPreferEdge(int px, int py, int finalW, int finalH, int radiusPx);

    // Solid Bodies hide/show: bodies in this set are skipped by render(), so hidden geometry is
    // unpickable (a click goes through to whatever is behind it — never an invisible hit).
    // Persists across setDoc(); AppState::applyBodyVisibility() re-syncs it after every reload
    // and clearViewportForNewDoc() clears it.
    void setHiddenBodies(const std::set<uint32_t>& bodyIds) { hidden_ = bodyIds; }

    // Keep picking in lock-step with Section View. A fragment is pickable only inside every
    // active section half-space: dot(world, normal[i]) <= offset[i].
    void setSectionClip(int count, const float normals[3][3], const float offsets[3]);

private:
    // True when the encoded id belongs to a hidden body (body id lives in bits [31:24]).
    bool bodyHidden(uint32_t encoded) const {
        return !hidden_.empty() && hidden_.count((encoded >> 24) & 0xFFu) != 0;
    }
    // True when this AICM body id is hidden (batched draws carry the raw body id).
    bool bodyHiddenId(uint32_t body_id) const {
        return !hidden_.empty() && hidden_.count(body_id) != 0;
    }
    void destroy();
    void allocTargets(int w, int h);

    bool built_ = false;
    bool rgba8_ = false;  // fallback encoding (env CHUDWARE_IDBUF=rgba8)

    // offscreen target
    unsigned int fbo_ = 0, idTex_ = 0, depthRbo_ = 0;
    int w_ = 0, h_ = 0;

    // shaders: id shader reused for faces (tris), edges (lines), verts (points). The packed topo
    // id travels as a per-vertex INTEGER attribute (location 1) so a whole body draws in one call.
    unsigned int prog_ = 0;

    // One batched draw covering ALL of a body's faces (indexed tris), edges (line list), or verts
    // (points). Each vertex carries its own encoded id in vboId, so the entire body draws in a
    // single glDraw* — and a hidden body is skipped wholesale (its body_id is in bits [31:24] of
    // every id, but we keep it explicit for the skip test). faces use the EBO; edges/verts don't.
    struct BodyDraw {
        unsigned int vao = 0, vboPos = 0, vboId = 0, ebo = 0;
        int count = 0;         // index count (faces) or vertex count (edges/verts)
        uint32_t body_id = 0;  // for the hidden-body skip
    };
    std::vector<BodyDraw> faceDraws_;  // one per body (all face ranges batched, id per vertex)
    std::vector<BodyDraw> edgeDraws_;  // one per body (all edge polylines batched as a line list)
    std::vector<BodyDraw> vertDraws_;  // one per body (all B-rep verts batched as points)
    std::set<uint32_t> hidden_;  // AICM body ids hidden in the viewport (skip in render())
    int clipCount_ = 0;
    float clipNormals_[3][3] = {{1,0,0},{0,1,0},{0,0,1}};
    float clipOffsets_[3] = {0,0,0};

    // Frame cache (task 01): the last rendered id buffer is reused when its inputs are unchanged.
    IdPassFrameKey lastKey_;
    bool haveCachedFrame_ = false;  // false until the first render() at the current buffer state
    uint64_t docGen_ = 0;           // monotonically bumped by setDoc() to invalidate the cache
};

}  // namespace render
