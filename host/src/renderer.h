// OpenGL mesh renderer: renders one or more colored Meshes to an offscreen framebuffer
// (supersampled), supports the named CAD views, and reads back pixels for PNG screenshots.
// The same color texture is displayed live in the ImGui viewport. Multi-part assemblies
// render each body in its own color (matching the engine's contact-sheet palette).
#pragma once
#include <cstdint>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>

#include "vec_math.h"
#include "mesh.h"

// Display style for the shaded pass (SolidWorks "Display Style" toolbar).
enum class DisplayStyle {
    Shaded,               // faces only, no feature edges
    ShadedEdges,          // faces + feature edges (default)
    HiddenLinesRemoved,   // faces in bg color (depth only) + visible edges -> classic HLR
    HiddenLinesVisible,   // HLR + occluded edges drawn dim
    Wireframe,            // edges only over the background
};

// Kawase-blurred backdrop for liquid-glass UI (Liquid Glass v2, ported from
// ui/viewport/renderer.h). UVs map the full viewport image.
struct ViewportBackdrop {
    std::uintptr_t textureId = 0;
    float uvMin[2] = {0.f, 0.f};
    float uvMax[2] = {1.f, 1.f};
    bool valid() const { return textureId != 0; }
};

// GPU pass timings (ms), each backed by a double-buffered GL_TIME_ELAPSED query pair so a
// result is polled without ever blocking the GL pipeline (see Renderer::glassTimings()).
// valid=false until every phase (scene/blur/luma) has produced at least one polled result.
struct GlassTimings {
    float sceneMs = 0.f, blurMs = 0.f, lumaMs = 0.f;
    bool valid = false;
};

class Renderer {
public:
    // One body of the displayed model: geometry + its display color (RGB 0..1).
    //
    // C19 result overlays: a body may also carry a per-vertex scalar field (von Mises, |u|, FoS,
    // thickness, …) and a per-vertex displacement (FEA deformed shape). When the renderer's
    // colormap mode is on AND `scalar` is populated (one value per vertex), the body draws through
    // the LUT instead of its flat color; `disp` is applied on the CPU at the current deform scale.
    // One picking face = a contiguous run of triangles in the body's index buffer (mirrors
    // render::FaceRange). Carried through so the shaded pass can draw a single feature's faces
    // translucent (per-feature edit-preview isolation) while the rest of the body stays opaque.
    struct FaceSpan {
        uint32_t face_id = 0;
        uint32_t first_tri = 0;
        uint32_t tri_count = 0;
    };

    struct PartMesh {
        Mesh mesh;
        uint32_t body_id = 0;             // AICM body id (matches the owner-map (body<<32)|face keys)
        std::vector<FaceSpan> faces;      // per-face triangle runs (empty = whole body opaque only)
        float color[3] = {0.62f, 0.70f, 0.80f};
        // Per-body colour is shown VERBATIM: the AICM bakes a real colour for every body (the
        // set_appearance colour, or the engine default grey when none is set), so a set_appearance
        // change is reflected on screen. The literal above is just the inert init value — every
        // doc/assembly caller overwrites color[] from the loaded body. A body with NO baked colour
        // (the single-mesh setMesh() STL path) sets useDefaultColor=true to fall back to the theme
        // palette.partDefault (setDefaultPartColor) instead — an explicit flag, never a sentinel
        // colour value, so a legitimately-set colour is never mistaken for "unset".
        bool useDefaultColor = false;
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
    // Also the single camera/projection seam: every screen<->world helper (pan, push-pull scale,
    // sketch auto-pan, etc.) MUST go through these published fields instead of hardcoding FOV.
    struct CameraXform {
        Mat4 mvp;          // model->clip used by the last drawCurrent()
        Mat4 proj;         // projection alone (perspective or ortho, with lens shift baked in)
        Mat4 view;         // lookAt view matrix
        Vec3 eye;          // world-space camera position
        Vec3 center;       // orbit look-at target (bbox center + pan, or sketch override)
        Vec3 dir;          // unit target->eye (camera forward from the model)
        Vec3 right;        // unit screen-right in world space
        Vec3 up;           // unit screen-up in world space
        float radius = 1.0f;     // scene radius used for framing
        float dist = 1.0f;       // |eye - center| (orbit radius)
        float halfHeight = 1.0f; // world half-height of the view volume at the orbit target
                                 // (perspective: dist*tan(fov/2); ortho: sceneR*1.05/zoom)
        float zoom = 1.0f;
        bool  orthographic = true;  // projection mode of this frame
        int ssWidth = 0, ssHeight = 0;  // supersampled framebuffer size (matches colorTexture)
        // Viewport pixel size used when the frame was drawn (final, not supersampled).
        int vpWidth = 0, vpHeight = 0;
    };
    const CameraXform& camera() const { return cam_; }
    int supersample() const { return SS; }

    // World units per screen pixel at the orbit-target plane for the CURRENT camera framing.
    // Uses the published halfHeight / viewport height so pan, push-pull fallbacks, and sketch
    // auto-pan all share one formula across perspective and orthographic modes.
    // Pass the FINAL (non-supersampled) viewport height in pixels; falls back to cam_.vpHeight.
    float worldPerPixel(int viewportHeightPx = 0) const {
        int vh = viewportHeightPx > 0 ? viewportHeightPx
                                     : (cam_.vpHeight > 0 ? cam_.vpHeight : h_);
        if (vh < 1) vh = 1;
        return 2.0f * cam_.halfHeight / (float)vh;
    }
    // World half-height of the view volume at the orbit target (matches drawCurrent framing).
    float viewHalfHeight() const { return cam_.halfHeight; }

    // Bind the studio color framebuffer + supersampled viewport so the C15 overlays can draw
    // highlights on top of the shaded model (call right after renderOrbit, then endOverlay()).
    void beginOverlay();
    void endOverlay();

    ~Renderer();
    bool init(int width, int height, std::string& err);  // requires a current GL context
    void resize(int width, int height);
    void setMesh(const Mesh& m);                          // single body, neutral color
    void setMeshes(const std::vector<PartMesh>& parts);   // multi-body, per-part colors
    // Per-document GPU mesh cache. A content identity is the worker doc id + immutable revision;
    // bindCachedMeshes swaps already-uploaded VAO/VBO handles into the active slot without reading
    // AICM or issuing GL uploads. setActiveMeshIdentity marks the freshly uploaded active mesh.
    void setActiveMeshIdentity(const std::string& docId, int revision);
    bool bindCachedMeshes(const std::string& docId, int revision);
    void cacheMeshes(const std::string& docId, int revision,
                     const std::vector<PartMesh>& parts);
    void parkActiveMeshes();
    void discardCachedMeshes(const std::string& docId);
    // While framing is frozen, setMeshes() updates the geometry but PRESERVES center_/radius_,
    // so real-time rebuilds (e.g. a downstream extrude recomputing while a sketch is edited) do
    // not re-frame the camera. Used to stop the view "bouncing" during sketch editing — the view
    // stays normal-to-plane at a stable zoom while geometry updates live underneath.
    void setFramingFrozen(bool f) { framingFrozen_ = f; }
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
    // Zebra stripes: SolidWorks surface-continuity inspection mode. When on, shaded faces render
    // as if reflecting a black/white striped environment (uZebra/uZebraFreq in kFrag); the stripe
    // pattern takes precedence over the scalar LUT and RealView. `freq` is the band density along
    // the reflected-direction coordinate (typical 4..60). Default OFF.
    void setZebra(bool on, float freq) { zebra_ = on; if (freq > 0.0f) zebraFreq_ = freq; }
    void setDisplayStyle(DisplayStyle s) { displayStyle_ = s; }
    DisplayStyle displayStyle() const { return displayStyle_; }
    // SolidWorks-style per-feature edit preview: while a feature's PropertyManager is open, ONLY the
    // faces owned by that feature draw translucent (a real shaded pass at `alpha`), so the in-progress
    // feature reads as a ghost over the rest of the model — which stays fully opaque. `editFaceKeys`
    // are (body_id<<32)|face_id keys (the owner map's face set for the edited feature). Pass an empty
    // set (or alpha >= 1) to clear isolation and render everything opaque.
    void setPreviewIsolation(const std::vector<uint64_t>& editFaceKeys, float alpha);
    void clearPreviewIsolation() { previewFaces_.clear(); previewAlpha_ = 1.0f; }

    // In-context component edit (SolidWorks "Edit Component"): while a component's part-doc is the
    // active doc, the ASSEMBLY is drawn as a dimmed static ghost behind the live part so the user
    // keeps assembly context. captureCurrentAsGhost() STEALS the currently-uploaded active mesh
    // (`subs_`) into the ghost buffer — no re-tessellate, no file re-read (the assembly mesh is
    // already on the GPU). The next setMeshes()/freeSubs() only touches `subs_`, so the ghost
    // survives the switch to the component. clearGhost() frees the ghost buffers on exit. The ghost
    // is drawn translucent (shaded alpha pass) and is NON-pickable (never wired into id_pass).
    void captureCurrentAsGhost();
    void clearGhost();
    bool hasGhost() const { return showGhost_ && !ghostSubs_.empty(); }
    void setGhostAlpha(float a) { ghostAlpha_ = (a < 0.0f) ? 0.0f : (a > 1.0f ? 1.0f : a); }
    // In-context component edit: relocate the ghost pass by this column-major 4x4 (applied to ghost
    // vertices BEFORE the camera). Caller passes inverse(editedComponent.placement) so the ghost's
    // copy of the edited component lands on the live part at its own modelling origin, while the rest
    // of the assembly arranges around it. Identity (the default, and the fixed/first component's
    // inverse) leaves the ghost exactly where it was baked. Reset to identity by clearGhost().
    void setGhostTransform(const float m[16]) { for (int i = 0; i < 16; ++i) ghostXform_.m[i] = m[i]; }
    void renderView(const std::string& view);            // "iso"|"front"|"right"|"top"
    void renderOrbit(float yawDeg, float pitchDeg, float zoom);
    // W2-D: orbit with a world-space pan offset added to the look-at target. The orbit center
    // becomes bbox-center + pan, so the published camera (mvp/eye/center) — and therefore the
    // id-buffer pick pass + overlays, which consume camera() — all stay pixel-exact in sync.
    void renderOrbit(float yawDeg, float pitchDeg, float zoom, const float pan[3]);

    // W2-D: bounding radius of the loaded model (1 if none). The viewport input code uses it to
    // convert a screen-space pan drag (px) into a world-space offset (matches drawCurrent framing).
    float modelRadius() const { return radius_; }
    // Effective scene radius for camera math: the real bbox radius, or — when the part is empty
    // (no bodies, radius_==0) — the same fallback the camera uses to frame an empty scene. Pan/zoom
    // screen->world math MUST use this, not modelRadius(), or a drag on an empty part scales by 0
    // (worldPerPx == 0) and the view never moves until the first body exists.
    float effectiveSceneRadius() const { return radius_ > 0.0f ? radius_ : kEmptySceneRadius; }
    Vec3 modelCenter() const { return center_; }
    // Perspective vertical FOV (degrees). Only used when orthographic() is false; kept so
    // external math that still needs the historical framing constant can share one source.
    static constexpr float kFovDeg = 35.0f;
    static constexpr float kEmptySceneRadius = 35.0f;  // scene radius used to frame an empty part

    // Projection mode: orthographic is the default (CAD-standard parallel projection). Perspective
    // remains available as a toggle (View Selector / heads-up). Applied by drawCurrent + thumbnail.
    void setOrthographic(bool on) { orthographic_ = on; }
    bool orthographic() const { return orthographic_; }

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

    // ---- feature reference geometry (datum axes / planes / points / coordinate systems) ----
    // A datum feature builds NO body, so the engine ships its resolved geometry out-of-band
    // (doc_mirror::refDatums); the host draws these as reference overlays after the model, the way
    // SolidWorks shows a created Axis/Plane/Point. setRefDatums replaces the whole set each rebuild.
    struct RefDatum {
        enum class Kind { Axis, Plane, Point, Csys, Curve };
        Kind kind = Kind::Axis;
        Vec3 origin{0, 0, 0};   // axis: a point on the line; plane/csys: origin; point: the point
        Vec3 dir{0, 0, 1};      // axis: unit direction; plane/csys: unit normal; (point: unused)
        Vec3 xdir{1, 0, 0};     // plane/csys: in-plane +U  (unused for axis / point)
        Vec3 ydir{0, 1, 0};     // plane/csys: in-plane +V  (unused for axis / point)
        // curve (helix / spiral / spline path): the sampled world-space polyline.
        // A curve feature builds no body — this is what the host draws AND hit-tests
        // so the user can pick the helix as a sweep path.
        std::vector<Vec3> points;
        std::string id;
        std::string name;
    };
    void setRefDatums(std::vector<RefDatum> datums) { refDatums_ = std::move(datums); }
    // Read-only view of currently injected reference datums (axes/planes/points/…). Used by
    // hit-testing / PM routing so they consume the same geometry inject already solved — never
    // re-pair planes on the pick path.
    const std::vector<RefDatum>& refDatums() const { return refDatums_; }
    // True when a datum with this feature id is currently loaded (e.g. "point.origin", injected on
    // every doc rebuild). Lets screen-space overlays (the origin +X/+Y/+Z labels) gate on the same
    // set drawRefDatums draws from, so labels never float without their geometry.
    bool hasDatum(const std::string& id) const {
        for (const auto& d : refDatums_)
            if (d.id == id) return true;
        return false;
    }
    // Datum feature ids hidden via the FeatureManager eye toggle. drawRefDatums skips any datum whose
    // id is in this set, so a hide takes effect immediately (no document rebuild). Applied from
    // AppState::applyDatumVisibility() on toggle / tab-switch / rebuild. Harmless to pass a superset
    // (e.g. AppState::hiddenFeatures verbatim) — only datum ids are ever matched here.
    void setHiddenDatums(std::unordered_set<std::string> ids) { hiddenDatums_ = std::move(ids); }
    // The feature id of the currently SELECTED datum (drawn emphasized — brighter + thicker), or
    // "" for none. Set each frame from AppState::selectedFeatureId so clicking a datum highlights it.
    void setSelectedDatumId(const std::string& id) { selectedDatumId_ = id; }
    // The feature id of the datum plane currently OPEN in the PropertyManager (being created/edited).
    // Its normal arrow is drawn only while it's the active edit target — once committed the arrow is
    // dropped so created planes don't carry a persistent normal handle. "" = none.
    void setEditingDatumId(const std::string& id) { editingDatumId_ = id; }

    // Orbit-center override: while a sketch is open the model bbox (center_) doesn't include the
    // sketch geometry, so an offset sketch would orbit/frame around the origin. The sketch mode sets
    // this to the sketch's geometric center so the view orbits/centres there instead. Cleared (on
    // == false) outside sketch, where center_ (the model bbox centre) is already correct.
    void setOrbitCenterOverride(bool on, const Vec3& c) { orbitOverride_ = on; orbitCenter_ = c; }
    // W2 full-bleed viewport: a horizontal lens shift (in NDC units) baked into the projection's
    // unused m[8] slot by drawCurrent(), so the modeled part stays centered in the un-occluded
    // region between the side panes WITHOUT moving pan_ — picking/overlays read cam_.mvp and so
    // follow automatically. 0 = symmetric (no shift); set per frame from the viewport panel.
    void setLensShiftX(float x) { lensShiftX_ = x; }

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
    int sectionClip(float normals[3][3], float offsets[3]) const;

    // ---- C19 result colormap + deformation ----
    // When colormap mode is on, bodies that carry a per-vertex scalar render through the LUT.
    void setColormapMode(bool on) { colormap_ = on; }
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
    // transparentBg=true (T-G15 icon pipeline): clear the tile to RGBA(0,0,0,0) and SKIP the
    // studio-gradient fill, so the part is composited over transparency — the source for the
    // ribbon's photographic icon tiles. Part fragments stay opaque (uAlpha=1). Default false
    // preserves the View-Selector preview's solid studio background.
    unsigned int renderThumbnail(float yawDeg, float pitchDeg, int px, bool transparentBg = false);

    // Read back the last-rendered thumbnail FBO as px*px RGBA, flipped to top-left origin.
    // (readRGBA() reads the MAIN resolve FBO — not the thumbnail; use this for icon capture.)
    std::vector<unsigned char> readThumbnailRGBA(int px) const;

    std::vector<unsigned char> readRGBA() const;         // final w*h*4, top-left origin
    // Final-size box-resolved texture. This is the intended on-screen presentation target.
    unsigned int resolvedColorTexture() const;
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
    // Same query keyed by AICM body_id (as opposed to the setMeshes() vector index) — the id-buffer
    // pick pass reports body_id, and it draws every body regardless of hide/show, so callers that
    // want to reject a pick on a hidden body (e.g. the sketch-plane-pick face test) need this rather
    // than bodyVisible(index). Unknown body_id (stale key) reads as visible so it never masks a pick
    // that should otherwise resolve normally.
    bool bodyVisibleById(uint32_t body_id) const {
        for (const auto& s : subs_) if (s.body_id == body_id) return s.visible;
        return true;
    }

    // ---- F1: glass backdrop + adaptive contrast (Liquid Glass v2) ----
    // Kawase-blurred copy of the resolved color buffer (updated when resolveColorTexture runs).
    ViewportBackdrop backdrop() const;
    // Mean relative luminance (Rec.709) under the rect, top-left viewport-local px coords.
    float averageLuminance(float x0, float y0, float x1, float y1) const;
    // Mean backdrop color stats under the rect (same coord convention): mean RGB, mean Rec.709
    // luminance, and the luminance VARIANCE across the sampled grid cells (the dynamic-color
    // busyness term). Pure CPU read of the same async grid as averageLuminance (reduceLumaPbo
    // retains the mean-RGB cells alongside the luma ones). Returns false until the first
    // readback has published a grid — callers then behave as if dynamic color were off.
    bool averageBackdropStats(float x0, float y0, float x1, float y1,
                              float outRgb[3], float& outLum, float& outVariance) const;

    // GPU timings for the last polled scene-draw / blur-chain / luma-readback phases.
    GlassTimings glassTimings() const;
    // Master switch: when false, updateBackdropBlur() and the async luminance readback are
    // skipped entirely each frame (averageLuminance() keeps returning the last-known grid
    // value, or 0.5f if none was ever read back). Default true.
    void setBackdropEnabled(bool on);

private:
    // GPU buffers for one body.
    struct Sub {
        unsigned int vao = 0, vboPos = 0, vboNrm = 0, ebo = 0;
        unsigned int edgeVao = 0, vboEdge = 0;
        unsigned int vboScalar = 0;   // C19: per-vertex scalar attribute (location 2); 0 = none
        int idxCount = 0, edgeVerts = 0;
        uint32_t body_id = 0;                 // AICM body id (for per-feature preview isolation)
        std::vector<FaceSpan> faces;          // per-face triangle runs, sorted by first_tri
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
    // Feature datum overlays. axesOnly=false draws non-axis datums (planes/points/csys/curves);
    // axesOnly=true draws only Kind::Axis. Call axes LAST (after origin planes) so dashed axes
    // composite over translucent plane fills. selectedDatumId_ emphasizes the selected axis.
    void drawRefDatums(const Mat4& mvp, bool axesOnly);
    void drawSectionCap(const Mat4& mvp, int planeIdx);  // C28-C: cap for one plane, clipped by others
    void planeCorners(int plane, float e, Vec3 out[4]) const;  // 4 world corners of plane quad
    void freeSubs();
    void freeSubVector(std::vector<Sub>& subs);
    void buildLut();              // C19: build the SW-style jet LUT texture (lazy)
    void uploadDeformed(Sub& s);  // C19: re-upload displaced positions for the current scale
    void resolveColorTexture() const;  // SS colorTex_ -> final-size resolvedTex_ by box filter
    void updateBackdropBlur() const;   // resolvedTex_ -> quarter-res Kawase blur chain

    // ---- F1 async luminance pipeline (ported from ui/viewport/renderer.cpp) ----
    void issueLumaReadback() const;   // one glReadPixels/frame -> PBO ring + fence, or fallback
    void issueLumaPboRead(unsigned int srcFbo, int srcW, int srcH) const;  // handles resize
    void pollLumaPbos() const;           // non-blocking glClientWaitSync poll of the ring
    void reduceLumaPbo(int slot) const;  // map -> Rec.709 reduce -> staging -> published swap

    static constexpr int SS = 3;  // supersample factor for anti-aliasing (3x for crisp thin edges)
    static constexpr int kBlurPasses = 4;  // 4-tap Kawase passes (GLASS_DESIGN_SPEC)
    int w_ = 0, h_ = 0;           // final size
    int sw_ = 0, sh_ = 0;         // supersampled size
    unsigned int fbo_ = 0, colorTex_ = 0, depthRbo_ = 0;
    mutable unsigned int resolveFbo_ = 0, resolvedTex_ = 0;
    unsigned int resolveProg_ = 0;
    mutable bool resolvedDirty_ = true;
    // Kawase blur chain: downsample resolved color to ¼ res, then ping-pong blur passes.
    mutable unsigned int blurDownFbo_ = 0, blurDownTex_ = 0;
    mutable unsigned int blurPingFbo_[2] = {0, 0};
    mutable unsigned int blurPingTex_[2] = {0, 0};
    unsigned int downsampleProg_ = 0, kawaseProg_ = 0;
    mutable int blurW_ = 0, blurH_ = 0;
    mutable bool backdropDirty_ = true;

    // ---- F1 async luminance pipeline: 3-slot PBO ring + fence, GPU-async, never blocks ----
    static constexpr int kLumaPboCount = 3;
    static constexpr int kLumaGridMaxW = 64, kLumaGridMaxH = 40;
    mutable unsigned int lumaPbo_[kLumaPboCount] = {0, 0, 0};
    mutable void* lumaFence_[kLumaPboCount] = {nullptr, nullptr, nullptr};  // GLsync, opaque
    mutable bool lumaPboPending_[kLumaPboCount] = {false, false, false};
    mutable int lumaPboW_[kLumaPboCount] = {0, 0, 0};  // (w,h) generation each slot issued at
    mutable int lumaPboH_[kLumaPboCount] = {0, 0, 0};  // (detects a mid-flight blur resize)
    mutable int lumaPboNext_ = 0;              // next ring slot to issue into
    mutable int lumaSrcW_ = 0, lumaSrcH_ = 0;  // readback source size as of the last (re)spec
    mutable unsigned int lumaFallbackCounter_ = 0;  // every-8th-frame direct-read fallback cadence
    mutable std::vector<float> lumaGrid_;     // published grid, top-left row order, lumaW_*lumaH_
    mutable std::vector<float> lumaStaging_;  // built off to the side, then swapped in (no tearing)
    mutable std::vector<float> lumaRgbGrid_;     // mean-RGB grid, 3 floats/cell, same order/dims
    mutable std::vector<float> lumaRgbStaging_;  // staging twin, swapped in lockstep with lumaGrid_
    mutable int lumaW_ = 0, lumaH_ = 0;
    mutable bool lumaEverRead_ = false;  // averageLuminance() returns 0.5f until this is true
    bool backdropEnabled_ = true;

    // ---- F1 GPU pass timing: 2 queries per phase, ping-ponged so a result never blocks ----
    struct GpuTimerRing {
        unsigned int id[2] = {0, 0};
        bool pending[2] = {false, false};
        int cur = 0;
    };
    static void beginGpuTimer(GpuTimerRing& t);
    static void endGpuTimer(GpuTimerRing& t, float& outMs, bool& outValid);
    mutable GpuTimerRing sceneTimer_, blurTimer_, lumaTimer_;
    mutable float sceneMs_ = 0.f, blurMs_ = 0.f, lumaMs_ = 0.f;
    mutable bool sceneTimingValid_ = false, blurTimingValid_ = false, lumaTimingValid_ = false;
    // C27-D: dedicated small offscreen target for the View Selector preview thumbnail. Lazily
    // allocated on the first renderThumbnail() call; reallocated only when the requested px changes.
    unsigned int thumbFbo_ = 0, thumbTex_ = 0, thumbDepth_ = 0;
    int thumbPx_ = 0;                                  // current thumb target size (0 = unallocated)
    // Cache: skip the re-render while the hovered orientation and the model are unchanged.
    float thumbYaw_ = 1e30f, thumbPitch_ = 1e30f;     // last rendered orientation (deg)
    unsigned int thumbMeshRev_ = 0;                   // meshesRevision_ at last render (mismatch = dirty)
    bool thumbValid_ = false;                         // a thumbnail has been rendered at least once
    bool thumbTransparent_ = false;                   // last render's background mode (cache key)
    unsigned int meshesRevision_ = 0;                 // bumped by setMeshes(); thumbnail cache key
    unsigned int prog_ = 0;
    unsigned int edgeProg_ = 0;
    unsigned int bgProg_ = 0, bgVao_ = 0;
    unsigned int shadowProg_ = 0;  // ground contact-shadow quad
    unsigned int planeProg_ = 0, planeVao_ = 0, planeVbo_ = 0;
    std::vector<RefDatum> refDatums_;   // feature datum geometry to draw (reference overlays)
    std::unordered_set<std::string> hiddenDatums_;  // datum ids hidden via the FeatureManager eye
    std::string selectedDatumId_;       // id of the emphasized (selected) datum, "" = none
    std::string editingDatumId_;        // id of the datum plane open in the PM (draws normal arrow)
    bool  orbitOverride_ = false;       // use orbitCenter_ instead of center_ as the look-at target
    Vec3  orbitCenter_{0, 0, 0};        // sketch-geometry centre (valid when orbitOverride_)
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
    float previewAlpha_ = 1.0f; // < 1 ghosts the edited feature's faces (per-feature edit preview)
    std::unordered_set<uint64_t> previewFaces_;  // (body<<32)|face keys to ghost; empty = none
    bool zebra_ = false;        // Zebra surface-continuity stripe mode (uZebra in kFrag); default OFF
    float zebraFreq_ = 18.0f;   // stripe band density along the reflected-direction coord
    DisplayStyle displayStyle_ = DisplayStyle::ShadedEdges;
    std::vector<Sub> subs_;
    struct CachedMesh {
        std::vector<Sub> subs;
        Vec3 center;
        float radius = 0.0f;
    };
    std::unordered_map<std::string, CachedMesh> meshCache_;
    std::string activeMeshDocId_;
    int activeMeshRevision_ = -1;
    // In-context component-edit ghost: the parked assembly mesh (moved out of subs_ on enter),
    // drawn translucent behind the active component and excluded from picking. See captureCurrentAsGhost.
    std::vector<Sub> ghostSubs_;
    bool  showGhost_ = false;
    float ghostAlpha_ = 0.28f;
    Mat4  ghostXform_;   // extra model xform for the ghost pass only (identity = no shift); see setGhostTransform
    Vec3 center_;
    Vec3 pan_;  // W2-D: world-space orbit-target offset (set by renderOrbit; added to center_)
    float lensShiftX_ = 0.0f;  // W2: horizontal NDC lens shift (proj.m[8] / ortho offset)
    bool orthographic_ = true; // default: orthographic projection (CAD parallel view)
    float radius_ = 1.0f;
    CameraXform cam_;  // updated each drawCurrent()

    // C19 colormap / deformation state.
    bool colormap_ = false;
    float deformScale_ = 0.0f;
    bool framingFrozen_ = false;   // when true, setMeshes() keeps center_/radius_ (no re-frame)
    unsigned int lutTex_ = 0;  // 256x1 RGB jet ramp
};
