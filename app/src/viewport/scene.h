// Scene — a clean OpenGL 3.3 renderer for the new viewport. Loads the engine's AICM mesh
// (reusing host/src/render/aicm_loader, pure CPU) and renders shaded bodies + feature edges to
// a multisampled FBO, composited into the ImGui viewport window as a texture. Orbit camera,
// Z-up (matches the kernel). Owned by Application; AppState holds a forward-declared pointer.
#pragma once
#include <string>
#include <vector>
#include <cstdint>

namespace viewport {

class Scene {
public:
    bool init();
    void shutdown();
    void resize(int w, int h);
    bool loadAicm(const std::string& path);   // load + fit; true if a mesh was loaded
    void clearMesh();
    void render();                             // render current mesh to the resolve texture
    unsigned int texture() const { return resolveTex_; }
    int  width()  const { return w_; }
    int  height() const { return h_; }
    bool hasMesh() const { return !bodies_.empty(); }

    // camera (Z-up). distances are world mm.
    void fitView();
    void homeView();
    void orbit(float dAzRad, float dElRad);
    void pan(float dxPx, float dyPx);
    void zoom(float wheelSteps);
    void setStyle(int s) { style_ = s; }       // 0 shaded, 1 shaded+edges, 2 wireframe
    int  style() const { return style_; }

    // ---- selection / picking (faces) ----
    struct PickRef { uint32_t body_id=0; uint32_t topo_id=0; int kind=0;  // kind 0=face
        bool operator==(const PickRef& o) const { return body_id==o.body_id && topo_id==o.topo_id && kind==o.kind; } };
    bool pickAt(int px, int py, PickRef& out);   // px,py = pixels from the image TOP-LEFT; uses pickKind
    void setPickKind(int k) { pickKind_ = k; }   // 0 = faces, 1 = edges
    int  pickKind() const { return pickKind_; }
    void setHovered(const PickRef* r);           // null clears hover
    void toggleSelect(const PickRef& r);
    void clearSelection();
    const std::vector<PickRef>& selection() const { return selected_; }

    std::string lastError;

private:
    struct FacePart { uint32_t face_id=0, first_tri=0, tri_count=0; };
    struct EdgePart { uint32_t edge_id=0; int vtxStart=0; int vtxCount=0; };
    struct GBody {
        unsigned int vao=0, vbo=0, ebo=0; int nIdx=0;
        unsigned int evao=0, evbo=0; int nEdgeVerts=0;
        float color[3]={0.7f,0.75f,0.82f}; float metallic=0, roughness=0.5f;
        uint32_t body_id=0;
        std::vector<FacePart> faces;
        std::vector<EdgePart> edgeParts;
    };
    void buildFbo();
    void releaseFbo();
    void releaseBodies();
    void renderIdPassFaces();                     // faces colored by pick index into idFbo_
    void renderIdPassEdges();                     // edges colored by pick index into idFbo_

    int w_=16, h_=16, samples_=4;
    unsigned int msFbo_=0, msColor_=0, msDepth_=0;
    unsigned int resolveFbo_=0, resolveTex_=0;
    unsigned int idFbo_=0, idTex_=0, idDepth_=0;
    unsigned int prog_=0, bgProg_=0, edgeProg_=0, idProg_=0, bgVao_=0;
    std::vector<GBody> bodies_;

    std::vector<PickRef> pickMap_;                // face index (1-based id color) -> ref
    std::vector<PickRef> edgeMap_;                // edge index (1-based id color) -> ref
    std::vector<PickRef> selected_;
    PickRef hovered_; bool hasHover_=false;
    int pickKind_=0;                              // 0 faces, 1 edges

    // bounds + camera
    float center_[3]={0,0,0}, radius_=50.f;
    float az_=-0.9f, el_=0.5f, dist_=200.f, target_[3]={0,0,0}, fov_=38.f;
    int   style_=1;
};

} // namespace viewport
