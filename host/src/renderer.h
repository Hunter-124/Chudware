// OpenGL mesh renderer: renders one or more colored Meshes to an offscreen framebuffer
// (supersampled), supports the named CAD views, and reads back pixels for PNG screenshots.
// The same color texture is displayed live in the ImGui viewport. Multi-part assemblies
// render each body in its own color (matching the engine's contact-sheet palette).
#pragma once
#include <string>
#include <vector>

#include "math.h"
#include "mesh.h"

// Display style for the shaded pass (SolidWorks "Display Style" toolbar).
enum class DisplayStyle {
    Shaded,               // faces only, no feature edges
    ShadedEdges,          // faces + feature edges (default)
    HiddenLinesRemoved,   // faces in bg color (depth only) + visible edges -> classic HLR
    HiddenLinesVisible,   // HLR + occluded edges drawn dim
    Wireframe,            // edges only over the background
};

class Renderer {
public:
    // One body of the displayed model: geometry + its display color (RGB 0..1).
    //
    // C19 result overlays: a body may also carry a per-vertex scalar field (von Mises, |u|, FoS,
    // thickness, …) and a per-vertex displacement (FEA deformed shape). When the renderer's
    // colormap mode is on AND `scalar` is populated (one value per vertex), the body draws through
    // the LUT instead of its flat color; `disp` is applied on the CPU at the current deform scale.
    struct PartMesh {
        Mesh mesh;
        float color[3] = {0.62f, 0.70f, 0.80f};
        // C32-A RealView material (0 metallic / 0.5 roughness == dielectric plastic, the C30 look).
        float metallic = 0.0f;
        float roughness = 0.5f;
        std::vector<float> scalar;       // n_verts values (NaN = neutral); empty = no field
        float scalarMin = 0.0f, scalarMax = 1.0f;
        std::vector<float> disp;         // 3*n_verts displacement vectors (mm); empty = none
        float dispScaleHint = 1.0f;      // suggested multiplier for unit-visible deformation
    };

    // The camera transform for the most recently rendered orbit/view, exposed so the ID-buffer
    // pass (render/id_pass) and the hover/selection overlays (render/overlays) can draw the same
    // geometry in pixel-exact alignment with the studio shaded pass. (C15 picking integration.)
    struct CameraXform {
        Mat4 mvp;          // model->clip used by the last drawCurrent()
        Vec3 eye;          // world-space camera position
        Vec3 center;       // model bounding-sphere center
        float radius = 1.0f;
        int ssWidth = 0, ssHeight = 0;  // supersampled framebuffer size (matches colorTexture)
    };
    const CameraXform& camera() const { return cam_; }
    int supersample() const { return SS; }

    // Bind the studio color framebuffer + supersampled viewport so the C15 overlays can draw
    // highlights on top of the shaded model (call right after renderOrbit, then endOverlay()).
    void beginOverlay();
    void endOverlay();

    ~Renderer();
    bool init(int width, int height, std::string& err);  // requires a current GL context
    void resize(int width, int height);
    void setMesh(const Mesh& m);                          // single body, neutral color
    void setMeshes(const std::vector<PartMesh>& parts);   // multi-body, per-part colors
    void setBackground(float tr, float tg, float tb, float br, float bg, float bb);
    // 3-stop vertical background gradient (top / mid-band ~55% / bottom floor glow).
    void setBackground3(const float top[3], const float mid[3], const float bot[3]);
    // Feature-edge color + screen-space line width (px); width is scaled by the supersample factor.
    void setEdgeStyle(float r, float g, float b, float widthPx);
    // Fallback body color used when a body carries no explicit color (palette.partDefault).
    void setDefaultPartColor(float r, float g, float b);
    // Soft contact-shadow blob color + peak alpha (alpha falls to 0 at the blob rim).
    void setGroundShadow(float r, float g, float b, float maxAlpha);
    // C30-C: RealView-style procedural environment reflections on shaded faces (0=off, 1=on).
    void setRealView(bool on) { realView_ = on; }
    void setDisplayStyle(DisplayStyle s) { displayStyle_ = s; }
    DisplayStyle displayStyle() const { return displayStyle_; }
    void renderView(const std::string& view);            // "iso"|"front"|"right"|"top"
    void renderOrbit(float yawDeg, float pitchDeg, float zoom);
    // W2-D: orbit with a world-space pan offset added to the look-at target. The orbit center
    // becomes bbox-center + pan, so the published camera (mvp/eye/center) — and therefore the
    // id-buffer pick pass + overlays, which consume camera() — all stay pixel-exact in sync.
    void renderOrbit(float yawDeg, float pitchDeg, float zoom, const float pan[3]);

    // W2-D: bounding radius of the loaded model (1 if none). The viewport input code uses it to
    // convert a screen-space pan drag (px) into a world-space offset (matches drawCurrent's FOV/dist).
    float modelRadius() const { return radius_; }
    static constexpr float kFovDeg = 35.0f;  // shared with drawCurrent's projection

    // ---- reference planes (Front=XY, Top=XZ, Right=YZ) ----
    // Translucent datum planes drawn over the model. `show[3]` toggles each; `emphasized` (0..2,
    // or -1) renders that plane brighter and dims the others (focus-follows-selection).
    void setPlanes(const bool show[3], int emphasized);
    // W4-K: datum-plane fill + outline color (RGBA each), from palette.planeFill/planeOutline.
    // SolidWorks-style subtle light-blue; the alpha in each is the base, scaled per emphasis state.
    void setPlaneColors(const float fill[4], const float outline[4]);
    // World-space label anchor (a corner of the plane quad) for projecting the plane name to screen.
    // Returns false if the plane index is out of range. Valid after a render (uses center_/radius_).
    bool planeAnchor(int plane, Vec3& world) const;
    // The exact 4 world-space corners of a datum-plane quad as drawReferencePlanes draws it
    // (center_ + the same half-extent), so screen-space overlays (sketch plane-pick) hit-test the
    // SAME geometry the user sees. Returns false if the plane index is out of range.
    bool planeQuad(int plane, Vec3 out[4]) const;

    // ---- C28-C: SolidWorks-style Section View — up to 3 simultaneous clip planes ----
    // A fragment is visible only when it is inside ALL active half-spaces: dot(p,Ni) <= Oi.
    // When showCap is set, each active plane gets a stencil-masked filled cap, also clipped by
    // the other planes so intersecting cuts read as solid material (SolidWorks zonal mode).
    // count must be 1..3. The single-plane setSectionPlane() wrapper forwards here with count=1.
    void setSectionPlanes(const float n[][3], const float off[], int count,
                          const float capRgb[3], bool showCap);
    // Backward-compat single-plane setter (existing callers unchanged).
    void setSectionPlane(const float normal[3], float offset, bool enabled,
                         const float capRgb[3], bool showCap);
    bool sectionEnabled() const { return section_.count > 0; }

    // ---- C19 result colormap + deformation ----
    // When colormap mode is on, bodies that carry a per-vertex scalar render through the LUT.
    void setColormapMode(bool on) { colormap_ = on; }
    bool colormapMode() const { return colormap_; }
    // Deformation multiplier applied to the per-vertex DISP field (0 = undeformed). Setting it
    // re-uploads the displaced positions on the CPU, so call sparingly (on slider change).
    void setDeformScale(float s);
    float deformScale() const { return deformScale_; }
    // The legend range of the first scalar-bearing body (for the Legend widget). Returns false if
    // no body carries a scalar.
    bool scalarRange(float& vmin, float& vmax) const;

    // ---- C27-D: offscreen preview thumbnail (View Selector hover panel) ----
    // Render the CURRENT meshes from a candidate orientation (zoom=1 fit, no pan) into a small
    // dedicated FBO (px*px, no supersampling) and return its GL color-texture id. The texture is
    // owned by the renderer and valid only until the next renderThumbnail() call. Cheap to call:
    // a (yaw,pitch,meshes-revision) cache skips the re-render when nothing relevant changed, so
    // a steady hover doesn't redraw every frame. The thumb FBO is lazily allocated on first use.
    // GL note: the texture origin is bottom-left (GL) — flip V when blitting with ImGui::Image.
    // ALL state the main pass relies on (bound FBO, viewport, program) is restored before return.
    unsigned int renderThumbnail(float yawDeg, float pitchDeg, int px);

    std::vector<unsigned char> readRGBA() const;         // final w*h*4, top-left origin
    unsigned int colorTexture() const { return colorTex_; }
    int width() const { return w_; }
    int height() const { return h_; }
    bool hasMesh() const { return !subs_.empty(); }

    // ---- per-body visibility (Solid Bodies folder hide/show) ----
    // Index = the body's position in the setMeshes() vector (same order AppState builds it from
    // the AICM body list). setMeshes() resets every body to visible. Hidden bodies are skipped by
    // the shaded pass, both edge passes, the section-cap stencil pass, and the thumbnail; the
    // ground-shadow blob is skipped only when EVERY body is hidden (it is one aggregate blob, not
    // per-mesh). Camera bounds intentionally still include hidden bodies (SolidWorks keeps the
    // model extents stable while toggling visibility).
    size_t bodyCount() const { return subs_.size(); }
    void setBodyVisible(size_t index, bool visible);
    bool bodyVisible(size_t index) const {
        return index < subs_.size() && subs_[index].visible;
    }

private:
    // GPU buffers for one body.
    struct Sub {
        unsigned int vao = 0, vboPos = 0, vboNrm = 0, ebo = 0;
        unsigned int edgeVao = 0, vboEdge = 0;
        unsigned int vboScalar = 0;   // C19: per-vertex scalar attribute (location 2); 0 = none
        int idxCount = 0, edgeVerts = 0;
        float color[3] = {0.62f, 0.70f, 0.80f};
        float metallic = 0.0f, roughness = 0.5f;  // C32-A RealView material
        // CPU-side copies kept so the deform slider can re-upload displaced positions.
        std::vector<float> basePos;   // undeformed positions (3*n)
        std::vector<float> baseEdge;  // undeformed edge-line verts (3*k)
        std::vector<float> disp;      // 3*n displacement (empty = none)
        bool hasScalar = false;
        float scalarMin = 0.0f, scalarMax = 1.0f;
        bool visible = true;          // per-body hide/show (Solid Bodies folder)
    };

    void allocTargets();
    void drawCurrent(float yawDeg, float pitchDeg, float zoom);
    void drawGroundShadow(const Mat4& mvp, const Vec3& eye, const Vec3& up);  // soft contact blob
    void drawReferencePlanes(const Mat4& mvp);   // translucent datum planes (after the opaque pass)
    void drawSectionCap(const Mat4& mvp, int planeIdx);  // C28-C: cap for one plane, clipped by others
    void planeCorners(int plane, float e, Vec3 out[4]) const;  // 4 world corners of plane quad
    void freeSubs();
    void buildLut();              // C19: build the SW-style jet LUT texture (lazy)
    void uploadDeformed(Sub& s);  // C19: re-upload displaced positions for the current scale

    static constexpr int SS = 3;  // supersample factor for anti-aliasing (3x for crisp thin edges)
    int w_ = 0, h_ = 0;           // final size
    int sw_ = 0, sh_ = 0;         // supersampled size
    unsigned int fbo_ = 0, colorTex_ = 0, depthRbo_ = 0;
    // C27-D: dedicated small offscreen target for the View Selector preview thumbnail. Lazily
    // allocated on the first renderThumbnail() call; reallocated only when the requested px changes.
    unsigned int thumbFbo_ = 0, thumbTex_ = 0, thumbDepth_ = 0;
    int thumbPx_ = 0;                                  // current thumb target size (0 = unallocated)
    // Cache: skip the re-render while the hovered orientation and the model are unchanged.
    float thumbYaw_ = 1e30f, thumbPitch_ = 1e30f;     // last rendered orientation (deg)
    unsigned int thumbMeshRev_ = 0;                   // meshesRevision_ at last render (mismatch = dirty)
    bool thumbValid_ = false;                         // a thumbnail has been rendered at least once
    unsigned int meshesRevision_ = 0;                 // bumped by setMeshes(); thumbnail cache key
    unsigned int prog_ = 0;
    unsigned int edgeProg_ = 0;
    unsigned int bgProg_ = 0, bgVao_ = 0;
    unsigned int shadowProg_ = 0;  // ground contact-shadow quad
    unsigned int planeProg_ = 0, planeVao_ = 0, planeVbo_ = 0;
    unsigned int capProg_ = 0, capVao_ = 0, capVbo_ = 0;  // W3-I: section cap (flat-lit quad)
    bool  showPlane_[3] = {false, false, false};
    int   planeEmphasis_ = -1;
    // W4-K: datum-plane colors (light-blue, SolidWorks look). Defaults match palette.planeFill/
    // planeOutline so a render before the first setPlaneColors() still looks right.
    float planeFill_[4]    = {0.380f, 0.620f, 0.910f, 0.085f};
    float planeOutline_[4] = {0.290f, 0.540f, 0.870f, 0.700f};

    // C28-C: multi-plane section state. count=0 means disabled; 1..3 = that many active planes.
    // The visible region is the intersection of all half-spaces dot(p, normals[i]) <= offsets[i].
    static constexpr int kMaxSectionPlanes = 3;
    struct Section {
        int   count = 0;              // 0 = off; 1..3 = active
        bool  showCap = false;
        float normals[kMaxSectionPlanes][3] = {{1,0,0},{0,1,0},{0,0,1}};
        float offsets[kMaxSectionPlanes] = {0,0,0};
        float capRgb[3] = {0.62f, 0.66f, 0.74f};
    } section_;
    Vec3 capKeyDir_;  // W3-I: key-light dir captured for the cap's flat lighting
    float bgTop_[3] = {0.957f, 0.965f, 0.976f};
    float bgMid_[3] = {0.851f, 0.867f, 0.894f};
    float bgBot_[3] = {0.933f, 0.941f, 0.953f};
    float edgeColor_[3] = {0.090f, 0.100f, 0.120f};
    float edgeWidthPx_ = 1.6f;
    float defaultPart_[3] = {0.776f, 0.792f, 0.847f};
    float groundShadow_[4] = {0.20f, 0.22f, 0.26f, 0.35f};
    bool realView_ = true;  // C30-C: environment-reflection sheen (uRealView in kFrag)
    DisplayStyle displayStyle_ = DisplayStyle::ShadedEdges;
    std::vector<Sub> subs_;
    Vec3 center_;
    Vec3 pan_;  // W2-D: world-space orbit-target offset (set by renderOrbit; added to center_)
    float radius_ = 1.0f;
    CameraXform cam_;  // updated each drawCurrent()

    // C19 colormap / deformation state.
    bool colormap_ = false;
    float deformScale_ = 0.0f;
    unsigned int lutTex_ = 0;  // 256x1 RGB jet ramp
};
