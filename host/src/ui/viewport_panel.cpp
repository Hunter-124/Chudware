// AI-CAD Viewport panel — 3D orbit view + parts list + fit readout + overlays.
#include <cctype>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

#include "imgui.h"

#include "viewport_panel.h"

#include "legend.h"
#include "icons.h"
#include "mouse_gestures.h"
#include "shortcut_bar.h"
#include "camera_anim.h"     // C25 W2-D: eased camera tween (presets / Normal-To / zoom-to-fit)
#include "view_selector.h"   // C25 W2-E: SolidWorks-style glass-box view selector (Space)
#include "section_tool.h"    // C25 W3-J: section-view tool (another agent owns impl)

#include "../core/app_state.h"

#include "../renderer.h"
#include "../render/id_pass.h"
#include "../render/overlays.h"

#include "sketch_mode.h"   // C18
#include "property_manager.h"  // C27-A: in-viewport parameter callouts
#include "appearance_common.h"  // W1-B (item 9): shared MaterialPreset table + appearance picker
#include "drawing_panel.h"  // DRAWINGS: 2D sheet renderer for a Drawing tab

namespace ui {

// C25: file-local gesture/shortcut state — consistent with how SketchMode is held externally.
static MouseGestures s_gestures;
static ShortcutBar   s_shortcutBar;
static ViewSelector  s_viewSelector;   // C25 W2-E: glass-box view selector (Space)
static SectionTool   s_sectionTool;    // C25 W3-J: section-view (impl owned by section_tool agent)

// C25 W3-J: zoom-area mode state.
static bool  s_zoomAreaArmed = false;
static bool  s_zoomAreaDragging = false;
static ImVec2 s_zoomAreaP0 = {0, 0};  // drag start (screen)
static ImVec2 s_zoomAreaP1 = {0, 0};  // drag current (screen)

// C37-C: LMB drag-threshold state — suppress orbit micro-movement on click-selects.
static bool   s_lmbDragActive  = false;  // true once the 4px threshold has been crossed
static ImVec2 s_lmbDragOrigin  = {0, 0}; // screen position of the LMB press

// ---- Instant3D push-pull (drag-to-modify a face) -------------------------------------------
// When EXACTLY ONE face is selected we draw a normal-axis drag handle (a short ⟂ arrow + its
// mirror) at the face centroid. Pressing on the arrow starts a push-pull drag instead of an orbit:
// the cursor's screen motion is projected onto the face-normal's screen direction and converted
// px→mm at the face depth, then fed to doc_push_pull through the checkpoint/revert envelope.
struct PushPullDrag {
    bool      active     = false;     // a push-pull drag is in flight (owns the mouse, no orbit)
    uint32_t  bodyId     = 0;         // PickHit body (1-based) of the dragged face
    uint32_t  topoId     = 0;         // picked FACE topo_id (1-based)
    std::string checkpoint;           // doc_checkpoint id taken on press (for revert/cancel)
    ImVec2    pressPos    = {0, 0};    // screen pos at press (drag origin)
    float     nsx = 0, nsy = 0;       // unit screen-space direction of the face OUTWARD normal
    float     worldPerPx  = 1.0f;     // world(mm) per screen pixel at the face depth (this frame)
    double    lastApplied = 1e30f;    // last cumulative distance actually sent (skip dup samples)
    double    curDist     = 0.0;      // current cumulative signed distance (mm)
    // Live readout from the most recent draggable response (shown near the cursor).
    bool        haveReadout = false;
    std::string param;                // e.g. "depth"
    double      newValue  = 0.0;
    bool        clamped   = false;
    // Persists across the frame so the readout chip can draw even on a no-op sample.
    void reset() { *this = PushPullDrag{}; }
};
static PushPullDrag s_pp;

// ---- Instant3D dimension-handle drag (drag a FEATURE'S DIMENSION) --------------------------
// Complements the face push-pull (W8): when a FEATURE is selected (in the tree or via its
// geometry) — even with NO PropertyManager form open — we render its key linear/angular
// dimensions as draggable chips anchored near the feature's geometry. Dragging a dimension
// handle sets that ONE param to a new ABSOLUTE value live via edit_feature, mirroring the PM
// preview path: doc_checkpoint on press, then per sample doc_revert + applyOps(edit_feature with
// the full params, one field changed) + loadDocMesh, and on release one committed edit_feature
// (single undo entry). Esc cancels (revert). Throttled to one in-flight call.
//
// Which params are draggable is read from the PM-form field table (pm_forms): every enabled Dim
// field whose param exists on the feature is a candidate. Angle dims (unitLabel " [deg]") drag in
// degrees; length dims drag in mm using the same 1mm-world-probe px->scale as ppHandleGeometry.
//
// Precedence vs the W8 face arrow: the face arrow wins. The dimension handles are SUPPRESSED for
// the feature whenever a single FACE is selected (that face shows the push-pull arrow). They show
// when a feature is selected with no single drivable face under the cursor (tree selection, a
// body/edge pick, or a multi-face selection). This keeps the two gestures from ever overlapping.
struct DimDrag {
    bool        active   = false;     // a dimension drag is in flight (owns the mouse, no orbit)
    std::string featureId;            // the feature being edited
    std::string param;                // the param key being driven ("depth"/"radius"/"angle"/…)
    bool        angle    = false;     // true => value is degrees (drag maps px->deg)
    std::string label;                // PM label ("Depth"/"Radius"/"Angle"/…)
    std::string checkpoint;           // doc_checkpoint id taken on press (for revert/cancel)
    nlohmann::json baseParams;        // the feature's params at press (full; one field changes)
    nlohmann::json baseRefs;          // the feature's refs at press (emitted unchanged)
    double      oldValue = 0.0;       // value before the drag
    double      curValue = 0.0;       // current applied value
    double      lastApplied = 1e30;   // last value actually sent (skip dup samples)
    bool        clamped  = false;     // last sample hit the min/range clamp
    ImVec2      pressPos = {0, 0};    // screen pos at press (drag origin)
    float       dsx = 0, dsy = 0;     // unit screen-space drag direction (value grows along this)
    float       worldPerPx = 1.0f;    // mm per screen px along the measured world direction
    float       degPerPx   = 0.5f;    // deg per screen px (angle dims)
    bool        haveReadout = false;  // show the readout chip
    void reset() { *this = DimDrag{}; }
};
static DimDrag s_dim;

// ---- Move-Component drag (drag a COMPONENT in an ASSEMBLY) ----------------------------------
// When the open doc is an assembly and exactly one component body is selected, pressing on that
// body's geometry starts a constrained move-component drag instead of an orbit. The cursor's
// screen motion is projected onto the camera screen-plane at the component's world depth to form a
// world-space TARGET point, which is fed to doc_solve_mates {drag:{component, target}}. The engine
// soft-pulls that one component toward the target along its free DOF (mates re-seat the rest) and
// bumps the revision once on the committed solve = one undo. Mirrors the push-pull live-drag
// envelope: doc_checkpoint on press, transient solves during drag, revert + one final solve on
// release (so the drag is a SINGLE undo step), revert on Esc/cancel. Owns the mouse while active.
struct AsmDrag {
    bool        active   = false;     // a move-component drag is in flight (owns the mouse)
    std::string component;            // the component k-id being dragged
    std::string checkpoint;           // doc_checkpoint id taken on press (for the single-undo commit)
    ImVec2      pressPos = {0, 0};    // screen pos at press (drag origin)
    Vec3        startPos;             // component world origin at press (transform translation)
    Vec3        camR, camU;           // camera screen-right / screen-up (world) captured at press
    float       worldPerPx = 1.0f;    // world(mm) per screen pixel at the component depth (at press)
    ImVec2      lastSent = {1e30f, 1e30f};  // last cursor delta actually solved (throttle)
    void reset() { *this = AsmDrag{}; }
};
static AsmDrag s_asmDrag;

// One draggable dimension of the selected feature: the param key, its PM label, whether it is an
// angle, the world anchor point (where the handle sits), and the unit world DIRECTION the value is
// measured along (a length grows by +1 value per +1mm along dirW; an angle ignores dirW).
struct DimCandidate {
    std::string param;
    std::string label;
    bool        angle = false;
    double      value = 0.0;        // current stored value (mm or deg)
    float       anchor[3] = {0, 0, 0};
    Vec3        dirW;               // unit world direction the dim is measured along (length dims)
};

// Find the feature object (by id) in the mirror tree. Returns nullptr if absent / not a part doc.
static const nlohmann::json* findFeature(const nlohmann::json& tree, const std::string& fid) {
    if (fid.empty() || !tree.is_object()) return nullptr;
    auto feat = tree.find("features");
    if (feat == tree.end() || !feat->is_array()) return nullptr;
    for (const auto& f : *feat)
        if (f.is_object() && f.value("id", std::string()) == fid) return &f;
    return nullptr;
}

// Current stored value of a dim-or-number param (resolves the {value,expr} shape or a bare number).
// Returns false when absent, non-numeric, or expression-driven (a drag must never stomp an expr).
static bool dimParamValue(const nlohmann::json& params, const std::string& key, double& out,
                          bool& isExpr) {
    isExpr = false;
    auto it = params.find(key);
    if (it == params.end()) return false;
    if (it->is_object()) {
        auto ev = it->find("expr");
        if (ev != it->end() && ev->is_string() && !ev->get<std::string>().empty()) {
            isExpr = true; return false;   // equation-driven — not draggable
        }
        auto vv = it->find("value");
        if (vv == it->end() || !vv->is_number()) return false;
        out = vv->get<double>(); return true;
    }
    if (it->is_number()) { out = it->get<double>(); return true; }
    return false;
}

// World direction a given dim is measured along (length dims only; angle dims ignore it). The map
// mirrors the box/extrude conventions the engine push-pull uses (x->length, y->width, z->height)
// and falls back to world-X for "size" dims that have no principal axis (radius/thickness/diameter).
static Vec3 dimWorldDir(const std::string& param) {
    if (param == "width")                              return Vec3(0, 1, 0);
    if (param == "height" || param == "depth" ||
        param == "depth2" || param == "offset" ||
        param == "pitch"  || param == "thickness")     return Vec3(0, 0, 1);
    // length/radius/radius2/diameter/distance/spacing/dx/dy and any other size dim grow along +X.
    return Vec3(1, 0, 0);
}

// World anchor for the selected feature's handles: the centre of the union AABB of the bodies the
// feature owns (owner map), else any cached face/edge centroid the feature owns, else the scene
// centre. Also returns a representative extent (half-diagonal) used to space the handles out.
static bool featureAnchor(AppState& st, const std::string& fid, float out[3], float& extent) {
    float bmin[3] = { 1e30f,  1e30f,  1e30f};
    float bmax[3] = {-1e30f, -1e30f, -1e30f};
    bool got = false;
    auto grow = [&](const float* b /*6*/) {
        for (int i = 0; i < 3; ++i) {
            if (b[i]   < bmin[i]) bmin[i] = b[i];
            if (b[i+3] > bmax[i]) bmax[i] = b[i+3];
        }
        got = true;
    };
    const auto& om = st.docMirror.ownerMap();
    auto oit = om.find(fid);
    if (oit != om.end()) {
        for (uint32_t bodyId : oit->second.bodies) {
            auto bit = st.bodyBBoxCache.find(bodyId);
            if (bit != st.bodyBBoxCache.end()) grow(bit->second.data());
        }
        if (!got) {  // no body bbox (dress-up feature owns faces, not whole bodies): use its faces
            for (const auto& bf : oit->second.faces) {
                uint64_t key = ((uint64_t)bf.first << 32) | (uint64_t)bf.second;
                auto fbit = st.faceBBoxCache.find(key);
                if (fbit != st.faceBBoxCache.end()) grow(fbit->second.data());
            }
        }
    }
    if (got) {
        out[0] = (bmin[0] + bmax[0]) * 0.5f;
        out[1] = (bmin[1] + bmax[1]) * 0.5f;
        out[2] = (bmin[2] + bmax[2]) * 0.5f;
        float dx = bmax[0] - bmin[0], dy = bmax[1] - bmin[1], dz = bmax[2] - bmin[2];
        extent = 0.5f * std::sqrt(dx*dx + dy*dy + dz*dz);
        if (extent < 1.0f) extent = 1.0f;
        return true;
    }
    // Whole-scene fallback (e.g. owner map not shipped): centre on the model.
    if (st.renderer) {
        out[0] = 0; out[1] = 0; out[2] = 0;   // pan-relative model centre is the orbit target
        extent = st.renderer->modelRadius();
        if (extent < 1.0f) extent = 1.0f;
        return true;
    }
    return false;
}

// Build the draggable-dimension list for the selected feature, reusing the PM-form field table to
// learn which params are dims and their labels/units. Skips greyed/conditional-off and expr-bound
// dims, and any dim whose value the engine table drives (omitWhenDisabled while disabled). Returns
// at most ~4 candidates (the dims a user actually drives) so the overlay stays readable.
static std::vector<DimCandidate> dimCandidates(AppState& st) {
    std::vector<DimCandidate> out;
    const std::string& fid = st.selectedFeatureId;
    if (fid.empty() || !st.docMirror.hasDoc()) return out;
    const nlohmann::json& tree = st.docMirror.tree();
    const nlohmann::json* feat = findFeature(tree, fid);
    if (!feat) return out;
    std::string type = feat->value("type", std::string());
    const ui::PMFormDef* form = ui::pmFindForm(type);
    if (!form) return out;
    const nlohmann::json& params = feat->contains("params") && (*feat)["params"].is_object()
                                       ? (*feat)["params"] : nlohmann::json::object();

    float anchor[3]; float extent = 1.0f;
    bool hasAnchor = featureAnchor(st, fid, anchor, extent);

    // Resolve a Combo field's currently-selected value (to honour DimIf enable conditions).
    auto comboValueOf = [&](const char* key) -> std::string {
        if (!key) return std::string();
        for (const auto& fd : form->fields) {
            if (fd.kind != ui::PMFieldKind::Combo || std::strcmp(fd.key, key) != 0) continue;
            // The stored param value (engine echoes the chosen enum); fall back to the first option.
            auto it = params.find(key);
            if (it != params.end() && it->is_string()) return it->get<std::string>();
            if (fd.comboOptions) { std::string o(fd.comboOptions);
                                   return o.substr(0, o.find('|')); }
        }
        return std::string();
    };
    auto enabled = [&](const ui::PMFieldDef& fd) -> bool {
        if (fd.comingSoon) return false;
        if (!fd.enableKey)  return true;
        std::string cur = comboValueOf(fd.enableKey);
        if (cur.empty()) return false;
        std::string vals = fd.enableVals ? fd.enableVals : "";
        // pipe-separated membership test
        size_t p = 0;
        while (p <= vals.size()) {
            size_t e = vals.find('|', p);
            std::string tok = vals.substr(p, e == std::string::npos ? std::string::npos : e - p);
            if (tok == cur) return true;
            if (e == std::string::npos) break;
            p = e + 1;
        }
        return false;
    };

    int idx = 0;
    for (const auto& fd : form->fields) {
        if (fd.kind != ui::PMFieldKind::Dim) continue;   // only Dim/Angle rows (Int/Combo skipped)
        if (!enabled(fd)) continue;
        double val = 0.0; bool isExpr = false;
        if (!dimParamValue(params, fd.key, val, isExpr)) continue;  // absent or expr-driven
        bool isAngle = fd.unitLabel && std::strstr(fd.unitLabel, "deg");
        DimCandidate c;
        c.param = fd.key; c.label = fd.label; c.angle = isAngle; c.value = val;
        c.dirW = dimWorldDir(fd.key);
        if (hasAnchor) {
            // Spread the handles around the anchor so chips/arrows don't stack: nudge each one a
            // little further out along its own measured axis (or +Z for angles).
            Vec3 d = isAngle ? Vec3(0, 0, 1) : c.dirW;
            float off = extent * 0.6f + (float)idx * extent * 0.15f;
            c.anchor[0] = anchor[0] + d.x * off;
            c.anchor[1] = anchor[1] + d.y * off;
            c.anchor[2] = anchor[2] + d.z * off;
        }
        out.push_back(std::move(c));
        if (++idx >= 4) break;   // keep the overlay readable
    }
    return out;
}

// ---- C26-C / C37-C: Normal-To (Ctrl+8 / shortcut) -------------------------------------------
// "Look straight at the selected face." If a face is selected and the C26-C normal cache has data
// for it, animate the camera so the view direction equals the face's outward normal. Second press
// while already looking at that face flips to the anti-view (SolidWorks behavior). Pan resets to
// the face centroid (so the face centers on screen). Zoom is kept.
//
// C37-C additions:
//   - While in sketch: Ctrl+8 orients to the sketch plane (uses cameraLock yaw/pitch), second press
//     flips 180° (anti-plane view). Cooperates with cameraLock (sets same yaw/pitch so the lock
//     re-asserts them without fighting).
//   - Cylindrical/conical faces: reads axis from selector JSON if available, falls back to face
//     normal cache. Looking DOWN the axis (axis treated as face normal for yaw/pitch derivation).
//   - Adds to context menu and HUD view-orientation dropdown (done in the drawing section below).
//
// Yaw/pitch derivation (Z-up, target-to-eye convention):
//   dir = (cos(pitch)*sin(yaw), -cos(pitch)*cos(yaw), sin(pitch))
//   Given face normal N = (nx, ny, nz):
//     pitch = asin(nz)        (clamped ±89.5°)
//     yaw   = atan2(nx, -ny)
//
// Nearest-axis fallback: when no face is selected and not in sketch, snaps to nearest axis-aligned.

// Persistent state for second-press flip detection (file-static, one viewport):
static uint64_t s_normalToKey   = 0;    // last cached key (body<<32|face_id, or UINT64_MAX for sketch)
static float    s_normalToYaw   = 0;    // the yaw we animated to
static float    s_normalToPitch = 0;    // the pitch we animated to

// C37-C: internal helper — convert a unit direction vector to yaw/pitch degrees.
static void dirToYawPitch(float nx, float ny, float nz, float& yawDeg, float& pitchDeg) {
    const float kPiOver180 = 3.14159265f / 180.0f;
    float nzC = nz > 1.0f ? 1.0f : (nz < -1.0f ? -1.0f : nz);
    float pitch = std::asin(nzC);
    float yaw   = std::atan2(nx, -ny);
    yawDeg   = yaw   / kPiOver180;
    pitchDeg = pitch / kPiOver180;
}

// C37-C: shared AABB-framing helper — pan to bbox center, zoom to fit bbox radius (animated).
// Used by Zoom to Selection (Shift+F), st.zoomToBBoxRequested, and can be called directly.
// bboxMin/Max are world-space. Uses the same math as ZoomFit (zoom=1 frames scene radius r;
// for a sub-box we set zoom = sceneRadius / bboxRadius so the sub-box fills the view).
static void frameBBox(AppState& st, const float bboxMin[3], const float bboxMax[3]) {
    // Bbox center becomes the pan target.
    float cx = (bboxMin[0] + bboxMax[0]) * 0.5f;
    float cy = (bboxMin[1] + bboxMax[1]) * 0.5f;
    float cz = (bboxMin[2] + bboxMax[2]) * 0.5f;
    // Bbox radius (half-diagonal).
    float dx = bboxMax[0] - bboxMin[0];
    float dy = bboxMax[1] - bboxMin[1];
    float dz = bboxMax[2] - bboxMin[2];
    float bboxR = 0.5f * std::sqrt(dx * dx + dy * dy + dz * dz);
    // Scene radius — the Renderer's modelRadius drives ZoomFit (zoom=1 shows sceneR at full view).
    float sceneR = st.renderer ? st.renderer->modelRadius() : 1.0f;
    if (sceneR < 1e-5f) sceneR = 1.0f;
    float targetZoom;
    if (bboxR < 1e-5f) {
        // Degenerate (vertex/edge selection): zoom in close.
        targetZoom = 4.0f;
    } else {
        targetZoom = sceneR / bboxR;
        // Cap zoom: never more than 8× (avoids extreme zoom for tiny features) and never less than
        // the ZoomFit value (framing the full scene is always the minimum zoom).
        const float kMaxZoom = 8.0f;
        if (targetZoom > kMaxZoom) targetZoom = kMaxZoom;
        if (targetZoom < 1.0f) targetZoom = 1.0f;
    }
    Vec3 mc = st.renderer ? st.renderer->modelCenter() : Vec3{0, 0, 0};
    float pan[3] = {cx - mc.x, cy - mc.y, cz - mc.z};
    requestCameraAnimation(st, st.yaw, st.pitch, targetZoom, pan);
}

// C37-C: Zoom to Selection — union AABB over selection items, then frame it.
// Empty selection: falls back to ZoomFit (same as applyViewPreset ZoomFit).
static void zoomToSelection(AppState& st) {
    if (st.selection.empty()) {
        applyViewPreset(st, ViewPreset::ZoomFit);
        return;
    }
    float bmin[3] = { 1e30f,  1e30f,  1e30f};
    float bmax[3] = {-1e30f, -1e30f, -1e30f};
    bool gotAny = false;
    const float kEdgePad = 3.0f;  // mm padding for edge/vertex degenerate extent
    for (const auto& item : st.selection.items()) {
        const auto& h = item.hit;
        if (!h.valid()) continue;
        if (h.kind == render::PickKind::Face) {
            uint64_t key = ((uint64_t)h.body_id << 32) | (uint64_t)h.topo_id;
            auto it = st.faceBBoxCache.find(key);
            if (it != st.faceBBoxCache.end()) {
                const auto& b = it->second;
                for (int i = 0; i < 3; ++i) {
                    if (b[i]   < bmin[i]) bmin[i] = b[i];
                    if (b[i+3] > bmax[i]) bmax[i] = b[i+3];
                }
                gotAny = true;
            }
        } else if (h.kind == render::PickKind::Edge) {
            uint64_t key = ((uint64_t)h.body_id << 32) | (uint64_t)h.topo_id;
            auto it = st.edgeCentroidCache.find(key);
            if (it != st.edgeCentroidCache.end()) {
                const auto& c = it->second;
                for (int i = 0; i < 3; ++i) {
                    if (c[i] - kEdgePad < bmin[i]) bmin[i] = c[i] - kEdgePad;
                    if (c[i] + kEdgePad > bmax[i]) bmax[i] = c[i] + kEdgePad;
                }
                gotAny = true;
            }
        } else if (h.kind == render::PickKind::Vertex) {
            uint64_t key = ((uint64_t)h.body_id << 32) | (uint64_t)h.topo_id;
            auto it = st.edgeCentroidCache.find(key);  // vertex centroid falls in edgeCentroidCache
            if (it != st.edgeCentroidCache.end()) {
                const auto& c = it->second;
                for (int i = 0; i < 3; ++i) {
                    if (c[i] - kEdgePad < bmin[i]) bmin[i] = c[i] - kEdgePad;
                    if (c[i] + kEdgePad > bmax[i]) bmax[i] = c[i] + kEdgePad;
                }
                gotAny = true;
            } else {
                // Fall back to body bbox if no per-vertex data.
                auto bit = st.bodyBBoxCache.find(h.body_id);
                if (bit != st.bodyBBoxCache.end()) {
                    const auto& b = bit->second;
                    for (int i = 0; i < 3; ++i) {
                        if (b[i]   < bmin[i]) bmin[i] = b[i];
                        if (b[i+3] > bmax[i]) bmax[i] = b[i+3];
                    }
                    gotAny = true;
                }
            }
        }
    }
    if (!gotAny) {
        applyViewPreset(st, ViewPreset::ZoomFit);
        return;
    }
    frameBBox(st, bmin, bmax);
}

static void normalToSelectionOrNearestAxis(AppState& st, bool inSketch) {
    const float kPiOver180 = 3.14159265f / 180.0f;

    // C37-C: while in sketch, orient to the sketch plane via cameraLock; second press flips 180°.
    if (inSketch) {
        auto* sketch = st.sketchMode;
        if (sketch && sketch->active()) {
            float ly = 0, lp = 0;
            if (sketch->cameraLock(ly, lp)) {
                const uint64_t kSketchKey = 0xFFFFFFFFFFFFFFFFull;
                bool alreadyLocked = (s_normalToKey == kSketchKey);
                float targetYaw = ly, targetPitch = lp;
                if (alreadyLocked) {
                    // Flip: rotate 180° about the plane normal (add 180° yaw, negate pitch).
                    targetYaw = ly + 180.0f;
                    targetPitch = -lp;
                    s_normalToKey = 0;
                } else {
                    s_normalToKey = kSketchKey;
                }
                s_normalToYaw   = targetYaw;
                s_normalToPitch = targetPitch;
                float pan[3] = {st.pan[0], st.pan[1], st.pan[2]};
                requestCameraAnimation(st, targetYaw, targetPitch, -1.0f, pan);
            }
        }
        return;
    }

    // ---- try true face normal path ----
    if (!st.selection.empty()) {
        const core::SelItem& selItem = st.selection.items().back();
        const render::PickHit& h = selItem.hit;
        if (h.valid() && h.kind == render::PickKind::Face) {
            uint64_t key = ((uint64_t)h.body_id << 32) | (uint64_t)h.topo_id;

            // C37-C: try to read axis from the resolved selector JSON (cylinder/cone support).
            // The selector is in selItem.selector (filled by C16 resolve round-trip).
            float axNx = 0, axNy = 0, axNz = 0;
            float faceCx = 0, faceCy = 0, faceCz = 0;
            bool hasAxis = false;

            if (!selItem.selector.empty()) {
                try {
                    auto sj = nlohmann::json::parse(selItem.selector);
                    // Selector format per docs/contracts/selectors.md: for cylinder/cone the
                    // selector carries "axis": [x,y,z] (unit vector along the axis).
                    if (sj.contains("axis") && sj["axis"].is_array() && sj["axis"].size() == 3) {
                        axNx = sj["axis"][0].get<float>();
                        axNy = sj["axis"][1].get<float>();
                        axNz = sj["axis"][2].get<float>();
                        // Normalize (should already be, but guard against float drift).
                        float len = std::sqrt(axNx*axNx + axNy*axNy + axNz*axNz);
                        if (len > 1e-5f) { axNx /= len; axNy /= len; axNz /= len; hasAxis = true; }
                    }
                } catch (...) {}
            }

            // Fall back to faceNormalCache for centroid and (if no axis) normal.
            auto it = st.faceNormalCache.find(key);
            if (it != st.faceNormalCache.end()) {
                const auto& v = it->second;
                faceCx = v[3]; faceCy = v[4]; faceCz = v[5];
                if (!hasAxis) {
                    axNx = v[0]; axNy = v[1]; axNz = v[2];
                    hasAxis = true;
                }
            }

            if (hasAxis) {
                // Second-press flip: if we already animated to this face, flip to anti-view.
                bool alreadyNormal = (key == s_normalToKey);
                float targetNx = axNx, targetNy = axNy, targetNz = axNz;
                if (alreadyNormal) {
                    targetNx = -axNx; targetNy = -axNy; targetNz = -axNz;
                    s_normalToKey = 0;  // next press goes forward again
                } else {
                    s_normalToKey = key;
                }

                float yawDeg, pitchDeg;
                dirToYawPitch(targetNx, targetNy, targetNz, yawDeg, pitchDeg);
                s_normalToYaw   = yawDeg;
                s_normalToPitch = pitchDeg;

                // Pan to centroid so the face is centered on screen.
                Vec3 mc = st.renderer ? st.renderer->modelCenter() : Vec3{0, 0, 0};
                float pan[3] = {faceCx - mc.x, faceCy - mc.y, faceCz - mc.z};
                requestCameraAnimation(st, yawDeg, pitchDeg, -1.0f, pan);
                return;
            }
        }
    }
    s_normalToKey = 0;  // no face selected: reset flip state

    // ---- nearest-axis fallback ----
    // dir = view direction from target toward eye (target-to-eye convention):
    //   dir = (cos p sin y, -cos p cos y, sin p)
    float y = st.yaw * kPiOver180, p = st.pitch * kPiOver180;
    float dx = std::cos(p) * std::sin(y);
    float dy2 = -std::cos(p) * std::cos(y);
    float dz = std::sin(p);
    struct AxView { float x, y, z; ViewPreset preset; };
    static const AxView kAx[6] = {
        { 0, -1,  0, ViewPreset::Front  },
        { 0,  1,  0, ViewPreset::Back   },
        { 1,  0,  0, ViewPreset::Right  },
        {-1,  0,  0, ViewPreset::Left   },
        { 0,  0,  1, ViewPreset::Top    },
        { 0,  0, -1, ViewPreset::Bottom },
    };
    int best = 0; float bestDot = -2.0f;
    for (int i = 0; i < 6; ++i) {
        float d = dx * kAx[i].x + dy2 * kAx[i].y + dz * kAx[i].z;
        if (d > bestDot) { bestDot = d; best = i; }
    }
    applyViewPreset(st, kAx[best].preset);
}

// W1-B (item 9): the appearance picker + MaterialPreset table moved to ui/appearance_common.{h,cpp}
// (formerly duplicated verbatim here and in feature_tree.cpp). The call site below uses
// ui::appearanceColorPicker().

// ---- C26: apply a set_appearance op straight from the viewport, then reload the mesh. ----
// A DIRECT apply (not the feature tree's deferred queue) is SAFE here: the viewport context menu is
// NOT drawn while iterating the DocMirror tree json (the feature tree's use-after-free hazard) — it
// runs inside the Viewport window after rendering, touching no tree-iteration state. This mirrors
// feature_tree's applyTreeOpsNow: apply -> loadDocMesh(resp.aicm_path) repaints the body with its
// new color on the next frame.
static void applyAppearanceFromViewport(AppState& st, const std::string& bodyKey,
                                        const float* color /* nullptr = clear */,
                                        float metallic = 0.0f, float roughness = 0.5f) {
    if (!st.docMirror.hasDoc() || !st.workerStarted) return;
    nlohmann::json op = {{"op", "set_appearance"}, {"id", bodyKey}};
    if (color) {
        op["color"] = {color[0], color[1], color[2]};
        op["metallic"] = metallic;   // C32-A RealView material
        op["roughness"] = roughness;
    } else {
        op["color"] = nullptr;
    }
    auto resp = st.docMirror.applyOps(st.worker, nlohmann::json::array({op}));
    if (resp.ok && !resp.aicm_path.empty()) {
        std::string lerr;
        st.loadDocMesh(resp.aicm_path, lerr);
        if (!lerr.empty()) st.addLog("[err] loadDocMesh: " + lerr);
    } else if (!resp.ok) {
        st.addLog("[err] set_appearance: " + resp.error_message);
    }
}

// ---- Instant3D push-pull helpers -----------------------------------------------------------

// Project a world point through the renderer's published MVP to viewport-screen pixels.
// Returns false when the point is behind the camera. (Same column-major math the plane-label and
// PM-callout overlays use.)
static bool ppProject(const Renderer& renderer, const ImVec2& vr0, const ImVec2& vr1,
                      int vw, int vh, const float w[3], ImVec2& out) {
    const float* m = renderer.camera().mvp.m;  // column-major model->clip
    float x = m[0]*w[0] + m[4]*w[1] + m[8]*w[2]  + m[12];
    float y = m[1]*w[0] + m[5]*w[1] + m[9]*w[2]  + m[13];
    float wc= m[3]*w[0] + m[7]*w[1] + m[11]*w[2] + m[15];
    if (wc <= 1e-4f) return false;  // behind the camera
    out.x = vr0.x + (x / wc * 0.5f + 0.5f) * (vw);
    out.y = vr0.y + (1.0f - (y / wc * 0.5f + 0.5f)) * (vh);
    (void)vr1;
    return true;
}

// The push-pull handle for the single selected face: returns false when there is no single face
// selected or the face has no cached normal/centroid. On success fills the centroid screen pos,
// the two arrow tips (+normal and −normal) in screen space, the unit screen-space normal
// direction, and the world-per-pixel scale at the face depth.
static bool ppHandleGeometry(AppState& st, const Renderer& renderer, const ImVec2& vr0,
                             const ImVec2& vr1, int vw, int vh, ImVec2& centroidSp,
                             ImVec2& tipPos, ImVec2& tipNeg, float& nsx, float& nsy,
                             float& worldPerPx, uint32_t& bodyId, uint32_t& topoId) {
    if (st.selection.size() != 1) return false;
    const render::PickHit& h = st.selection.items().front().hit;
    if (!h.valid() || h.kind != render::PickKind::Face) return false;
    uint64_t key = ((uint64_t)h.body_id << 32) | (uint64_t)h.topo_id;
    auto it = st.faceNormalCache.find(key);
    if (it == st.faceNormalCache.end()) return false;
    const auto& v = it->second;  // {nx,ny,nz, cx,cy,cz}
    float nx = v[0], ny = v[1], nz = v[2];
    float nlen = std::sqrt(nx*nx + ny*ny + nz*nz);
    if (nlen < 1e-6f) return false;
    nx /= nlen; ny /= nlen; nz /= nlen;
    float cx = v[3], cy = v[4], cz = v[5];

    float C[3] = {cx, cy, cz};
    if (!ppProject(renderer, vr0, vr1, vw, vh, C, centroidSp)) return false;

    // world-per-pixel at the face depth: project a point one mm along the normal and measure the
    // screen displacement. (Robust to perspective: uses the centroid's actual depth, not the orbit
    // target plane.) Use a 1mm probe; if the normal is near edge-on the screen vector is tiny, so
    // fall back to the camera-plane scale used by pan/zoom.
    float Cp[3] = {cx + nx, cy + ny, cz + nz};
    ImVec2 cpSp;
    bool gotProbe = ppProject(renderer, vr0, vr1, vw, vh, Cp, cpSp);
    float sdx = gotProbe ? (cpSp.x - centroidSp.x) : 0.0f;
    float sdy = gotProbe ? (cpSp.y - centroidSp.y) : 0.0f;
    float slen = std::sqrt(sdx*sdx + sdy*sdy);

    if (slen > 1e-3f) {
        nsx = sdx / slen; nsy = sdy / slen;
        worldPerPx = 1.0f / slen;   // 1mm spans `slen` px => mm-per-px = 1/slen
    } else {
        // Face is nearly edge-on: derive a screen direction from the camera basis projection of the
        // normal and fall back to the orbit-plane world-per-px (matches pan/zoom conversion).
        const float deg = 3.14159265f / 180.0f;
        const float fov = Renderer::kFovDeg * deg;
        float zc = st.zoom < 0.05f ? 0.05f : st.zoom;
        float dist = renderer.modelRadius() / std::sin(fov * 0.5f) * 1.05f / zc;
        worldPerPx = 2.0f * dist * std::tan(fov * 0.5f) / (float)(vh > 1 ? vh : 1);
        // screen-space normal direction from the camera right/up basis.
        float yr = st.yaw * deg, pr = st.pitch * deg;
        Vec3 vdir(std::cos(pr) * std::sin(yr), -std::cos(pr) * std::cos(yr), std::sin(pr));
        Vec3 worldUp = (std::fabs(pr) > 89.0f * deg) ? Vec3(0, 1, 0) : Vec3(0, 0, 1);
        Vec3 V = normalize(vdir);
        Vec3 R = normalize(cross(worldUp, V));
        Vec3 U = normalize(cross(V, R));
        Vec3 N(nx, ny, nz);
        float rN = dot(R, N), uN = dot(U, N);
        float l2 = std::sqrt(rN*rN + uN*uN);
        if (l2 < 1e-4f) return false;  // truly edge-on AND parallel to view: no usable handle
        // screen x is +R, screen y is -U (y down).
        nsx = rN / l2; nsy = -uN / l2;
    }

    float armLenPx = 46.0f * ui::uiScale;  // arrow length in screen px
    tipPos = ImVec2(centroidSp.x + nsx * armLenPx, centroidSp.y + nsy * armLenPx);
    tipNeg = ImVec2(centroidSp.x - nsx * armLenPx, centroidSp.y - nsy * armLenPx);
    bodyId = h.body_id; topoId = h.topo_id;
    return true;
}

// Run ONE push-pull sample at the given cumulative distance (revert to checkpoint, then apply).
// Returns false (and leaves s_pp.active as the caller set it) on a hard transport failure. On a
// non-draggable first response the caller aborts the drag (handled at press time instead).
static void ppApplySample(AppState& st, double cumulative, bool commit) {
    if (!st.docMirror.hasDoc()) return;
    // Revert to the pre-drag checkpoint so previews never compound, then apply the cumulative.
    if (!s_pp.checkpoint.empty()) {
        auto rv = st.docMirror.revert(st.worker, s_pp.checkpoint);
        if (rv.ok && !rv.aicm_path.empty()) { std::string le; st.loadDocMesh(rv.aicm_path, le); }
    }
    auto r = st.docMirror.pushPull(st.worker, s_pp.bodyId, s_pp.topoId, cumulative, commit);
    if (!r.ok) {
        st.toast(ui::ToastLevel::Error, "Push-pull failed: " + r.error_message);
        return;
    }
    if (!r.draggable) {
        // Shouldn't happen mid-drag (we validated draggability on press), but guard anyway.
        st.toast(ui::ToastLevel::Warn, "This face can't be dragged — " + r.reason);
        return;
    }
    if (!r.ops.aicm_path.empty()) { std::string le; st.loadDocMesh(r.ops.aicm_path, le); }
    s_pp.haveReadout = true;
    s_pp.param    = r.param;
    s_pp.newValue = r.new_value;
    s_pp.clamped  = r.clamped;
    s_pp.lastApplied = cumulative;
}

// Cancel an in-flight push-pull drag: revert to the checkpoint and clear all drag state. Safe to
// call when no drag is active (no-op). Leaves NO zombie checkpoint/flag behind.
static void ppCancel(AppState& st) {
    if (!s_pp.active && s_pp.checkpoint.empty()) { s_pp.reset(); return; }
    if (!s_pp.checkpoint.empty() && st.docMirror.hasDoc()) {
        auto rv = st.docMirror.revert(st.worker, s_pp.checkpoint);
        if (rv.ok && !rv.aicm_path.empty()) { std::string le; st.loadDocMesh(rv.aicm_path, le); }
    }
    s_pp.reset();
}

// ---- Move-Component drag helpers ------------------------------------------------------------

// True when the mirror's open document is an assembly (aicad.asm/1). Mirrors feature_tree.cpp.
static bool docIsAssembly(AppState& st) {
    const auto& tree = st.docMirror.tree();
    return tree.is_object() && tree.value("schema", std::string()) == "aicad.asm/1";
}

// The component object at a 1-based body ordinal in an assembly tree (body_id == component
// ordinal; see engine assembly.py AsmBodyOut.body_id). Returns nullptr if out of range.
static const nlohmann::json* asmComponentByOrdinal(AppState& st, uint32_t bodyId) {
    const auto& tree = st.docMirror.tree();
    if (!tree.is_object() || !tree.contains("components") || !tree["components"].is_array())
        return nullptr;
    const auto& comps = tree["components"];
    if (bodyId < 1 || bodyId > comps.size()) return nullptr;
    const auto& c = comps[bodyId - 1];
    return c.is_object() ? &c : nullptr;
}

// Component world origin (the translation of its row-major 16-float transform). Falls back to the
// cached body bbox centre, then origin. transform[3],[7],[11] are tx,ty,tz (row-major).
static Vec3 asmComponentOrigin(AppState& st, const nlohmann::json* comp, uint32_t bodyId) {
    if (comp && comp->contains("transform") && (*comp)["transform"].is_array() &&
        (*comp)["transform"].size() == 16) {
        const auto& t = (*comp)["transform"];
        return Vec3(t[3].get<float>(), t[7].get<float>(), t[11].get<float>());
    }
    auto bit = st.bodyBBoxCache.find(bodyId);
    if (bit != st.bodyBBoxCache.end()) {
        const auto& b = bit->second;
        return Vec3((b[0] + b[3]) * 0.5f, (b[1] + b[4]) * 0.5f, (b[2] + b[5]) * 0.5f);
    }
    return Vec3(0, 0, 0);
}

// Run ONE move-component solve at the given cursor delta from press (revert to checkpoint so
// transient solves never compound, then solve with the drag target). On the committed sample the
// engine's revision bump makes this drag a single undo step. `commit` is informational here (the
// envelope is identical) — we always revert-then-solve so exactly one solve survives at the end.
static void asmDragSample(AppState& st, const ImVec2& cur, bool /*commit*/) {
    if (!st.docMirror.hasDoc() || s_asmDrag.component.empty()) return;
    // Revert to the pre-drag checkpoint so each transient solve starts from the original pose.
    if (!s_asmDrag.checkpoint.empty()) {
        auto rv = st.docMirror.revert(st.worker, s_asmDrag.checkpoint);
        if (rv.ok && !rv.aicm_path.empty()) { std::string le; st.loadDocMesh(rv.aicm_path, le); }
    }
    // Cursor delta -> world target: project onto the camera screen-plane at the component depth.
    // dx along screen-right (+camR), dy DOWN the screen maps to -camU (screen y is down).
    float dx = cur.x - s_asmDrag.pressPos.x;
    float dy = cur.y - s_asmDrag.pressPos.y;
    float k = s_asmDrag.worldPerPx;
    Vec3 target(
        s_asmDrag.startPos.x + (s_asmDrag.camR.x * dx - s_asmDrag.camU.x * dy) * k,
        s_asmDrag.startPos.y + (s_asmDrag.camR.y * dx - s_asmDrag.camU.y * dy) * k,
        s_asmDrag.startPos.z + (s_asmDrag.camR.z * dx - s_asmDrag.camU.z * dy) * k);
    nlohmann::json params = {
        {"doc_id", st.docMirror.docId()},
        {"drag", {{"component", s_asmDrag.component},
                  {"target", {target.x, target.y, target.z}}}}};
    nlohmann::json res; std::string err;
    if (!st.worker.call("doc_solve_mates", params, res, err)) {
        st.toast(ui::ToastLevel::Error, "Move component failed: " + err);
        return;
    }
    if (res.is_object() && res.contains("code")) {
        st.toast(ui::ToastLevel::Error,
                 "Move component: " + res.value("message", std::string()));
        return;
    }
    // doc_solve_mates bumped the worker revision; resync the mirror so the tree (and the next
    // checkpoint/undo bookkeeping) stays in step, then reload the re-tessellated mesh.
    st.docMirror.resync(st.worker);
    std::string aicm;
    if (res.contains("tess") && res["tess"].is_object())
        aicm = res["tess"].value("aicm_path", std::string());
    if (!aicm.empty()) { std::string le; st.loadDocMesh(aicm, le); }
    s_asmDrag.lastSent = cur;
}

// Cancel an in-flight move-component drag: revert to the checkpoint and clear state. No-op when no
// drag is active.
static void asmDragCancel(AppState& st) {
    if (!s_asmDrag.active && s_asmDrag.checkpoint.empty()) { s_asmDrag.reset(); return; }
    if (!s_asmDrag.checkpoint.empty() && st.docMirror.hasDoc()) {
        auto rv = st.docMirror.revert(st.worker, s_asmDrag.checkpoint);
        if (rv.ok && !rv.aicm_path.empty()) { std::string le; st.loadDocMesh(rv.aicm_path, le); }
    }
    s_asmDrag.reset();
}

// ---- Instant3D dimension-handle drag helpers ------------------------------------------------

// Screen geometry for one dimension handle: project the world anchor to screen, derive the unit
// screen-space drag direction (along which the value GROWS), the two arrow tips, and the world(mm)
// or deg per screen-pixel scale. For a LENGTH dim the px scale is the exact 1mm-world-probe used by
// ppHandleGeometry (probe the anchor +1mm along dirW, measure screen displacement). For an ANGLE
// dim the handle is laid horizontally on screen and a fixed deg/px maps drag to degrees.
static bool dimHandleGeometry(AppState& st, const Renderer& renderer, const ImVec2& vr0,
                              const ImVec2& vr1, int vw, int vh, const DimCandidate& c,
                              ImVec2& anchorSp, ImVec2& tipPos, ImVec2& tipNeg,
                              float& dsx, float& dsy, float& worldPerPx, float& degPerPx) {
    float A[3] = {c.anchor[0], c.anchor[1], c.anchor[2]};
    if (!ppProject(renderer, vr0, vr1, vw, vh, A, anchorSp)) return false;

    if (c.angle) {
        // Angle handle: horizontal on screen; a constant deg/px (SolidWorks-ish drag sensitivity).
        dsx = 1.0f; dsy = 0.0f;
        worldPerPx = 0.0f;
        degPerPx = 0.5f;   // 720px == full turn
    } else {
        // Probe 1mm along the measured world direction; the screen displacement gives both the
        // screen-space direction and the world-per-px scale at this depth (perspective-correct).
        float P[3] = {A[0] + c.dirW.x, A[1] + c.dirW.y, A[2] + c.dirW.z};
        ImVec2 pSp;
        bool gotProbe = ppProject(renderer, vr0, vr1, vw, vh, P, pSp);
        float sdx = gotProbe ? (pSp.x - anchorSp.x) : 0.0f;
        float sdy = gotProbe ? (pSp.y - anchorSp.y) : 0.0f;
        float slen = std::sqrt(sdx*sdx + sdy*sdy);
        if (slen > 1e-3f) {
            dsx = sdx / slen; dsy = sdy / slen;
            worldPerPx = 1.0f / slen;       // 1mm spans `slen` px
        } else {
            // Direction is nearly edge-on to the screen: fall back to the orbit-plane scale and a
            // screen-horizontal handle so the dim is still grabbable (rare; degenerate camera).
            const float deg = 3.14159265f / 180.0f;
            const float fov = Renderer::kFovDeg * deg;
            float zc = st.zoom < 0.05f ? 0.05f : st.zoom;
            float dist = renderer.modelRadius() / std::sin(fov * 0.5f) * 1.05f / zc;
            worldPerPx = 2.0f * dist * std::tan(fov * 0.5f) / (float)(vh > 1 ? vh : 1);
            dsx = 1.0f; dsy = 0.0f;
        }
        degPerPx = 0.0f;
    }
    float armLenPx = 42.0f * ui::uiScale;
    tipPos = ImVec2(anchorSp.x + dsx * armLenPx, anchorSp.y + dsy * armLenPx);
    tipNeg = ImVec2(anchorSp.x - dsx * armLenPx, anchorSp.y - dsy * armLenPx);
    return true;
}

// Apply ONE dimension sample, setting the dragged param to the given ABSOLUTE value via an
// edit_feature on the FULL params snapshot (one field changed) — the PM preview path exactly.
//
// Because the value is absolute (always derived from the press-time baseParams), a preview never
// compounds, so — unlike push-pull's delta envelope — previews need NO per-sample revert: each
// preview just overwrites the same field. The undo envelope still yields "one undo":
//   - PREVIEW (commit=false): applyOps(coalesce=true) — overwrite the field, push NO undo snapshot.
//   - COMMIT  (commit=true) : doc_revert(checkpoint) [restores pre-drag, pushes the last-preview
//                             state] then applyOps(coalesce=false) [pushes the pre-drag snapshot].
//     The TOP of the undo stack after release is the pre-drag state, so one Ctrl+Z restores it.
static void dimApplySample(AppState& st, double value, bool commit) {
    if (!st.docMirror.hasDoc()) return;
    if (commit && !s_dim.checkpoint.empty()) {
        // Revert to pre-drag first so the committed edit_feature's pushed undo snapshot is the
        // pre-drag doc (single-undo guarantee), matching the push-pull release envelope.
        auto rv = st.docMirror.revert(st.worker, s_dim.checkpoint);
        if (rv.ok && !rv.aicm_path.empty()) { std::string le; st.loadDocMesh(rv.aicm_path, le); }
    }
    // Build the edited params: copy the base params, set this one field to the new value. Preserve
    // the {value,expr} dict shape the engine uses (expr stays null — we only drive bare dims).
    nlohmann::json params = s_dim.baseParams;
    auto it = params.find(s_dim.param);
    if (it != params.end() && it->is_object()) {
        (*it)["value"] = value;
        (*it)["expr"]  = nullptr;
    } else {
        params[s_dim.param] = {{"value", value}, {"expr", nullptr}};
    }
    nlohmann::json ops = nlohmann::json::array();
    ops.push_back({{"op", "edit_feature"}, {"id", s_dim.featureId},
                   {"params", params}, {"refs", s_dim.baseRefs}});
    auto resp = st.docMirror.applyOps(st.worker, ops, /*coalesce=*/!commit);
    if (!resp.ok) {
        st.toast(ui::ToastLevel::Error, "Edit dimension failed: " + resp.error_message);
        return;
    }
    if (!resp.aicm_path.empty()) { std::string le; st.loadDocMesh(resp.aicm_path, le); }
    s_dim.curValue = value;
    s_dim.lastApplied = value;
    s_dim.haveReadout = true;
}

// Cancel an in-flight dimension drag: revert to the checkpoint and clear all state. Safe when no
// drag is active. Leaves NO zombie checkpoint/flag behind (mirrors ppCancel).
static void dimCancel(AppState& st) {
    if (!s_dim.active && s_dim.checkpoint.empty()) { s_dim.reset(); return; }
    if (!s_dim.checkpoint.empty() && st.docMirror.hasDoc()) {
        auto rv = st.docMirror.revert(st.worker, s_dim.checkpoint);
        if (rv.ok && !rv.aicm_path.empty()) { std::string le; st.loadDocMesh(rv.aicm_path, le); }
    }
    s_dim.reset();
}

void drawViewportPanel(AppState& st, ImGuiIO& io, bool busy) {
    Renderer& renderer = *st.renderer;

    // ---------- DRAWINGS module: a Drawing tab hosts a 2D sheet in the SAME central dock window
    // as the 3D viewport. Branch here BEFORE any 3D state is touched so part/asm behavior stays
    // byte-identical and the dock layout is unchanged (the "Viewport" window just paints a sheet).
    if (st.showViewport && st.activeTabIsDrawing()) {
        if (ImGui::Begin("Viewport")) {
            ui::drawDrawingSheet(st);
        }
        ImGui::End();
        return;
    }

    // C18: while a sketch is being edited the camera locks normal to the sketch plane and the
    // sketch tools own the mouse (no orbit, no topology picking).
    auto* sketch = st.sketchMode;
    const bool inSketch = sketch && sketch->active();

    // ---------- Viewport ----------
    if (st.showViewport) {
    if (ImGui::Begin("Viewport"))
    {
        // C24: the view-orientation presets moved into the centered heads-up toolbar drawn over the
        // viewport image (below), SolidWorks-style. Nothing is drawn here at the panel top so the
        // 3D view starts flush against the ribbon.

        // ---- assembly fit readout + parts list (multi-part builds only) ----
        if (!st.asmParts.empty()) {
            ImGui::Separator();
            drawFitReadout(st.asmFit, /*detailed=*/true);   // always visible (key readout)
            // parts selector — scrolls if there are many bodies
            float ph = ImGui::GetContentRegionAvail().y * 0.30f;
            ph = ph > 150 ? 150 : (ph < 78 ? 78 : ph);
            ImGui::BeginChild("partspanel", ImVec2(0, ph), ImGuiChildFlags_Border);
            const auto& palVp = ui::palette;
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(palVp.partsDim[0], palVp.partsDim[1], palVp.partsDim[2], 1));
            ImGui::TextUnformatted("Parts  (click to isolate in the viewport)");
            ImGui::PopStyleColor();
            if (ImGui::RadioButton("Assembly (all parts)", st.selectedPart == -1)) {
                st.selectedPart = -1;
                st.showPartMesh(-1);
            }
            for (int pi2 = 0; pi2 < (int)st.asmParts.size(); ++pi2) {
                ImGui::ColorButton(("##sw" + std::to_string(pi2)).c_str(), partColor(pi2),
                                   ImGuiColorEditFlags_NoTooltip | ImGuiColorEditFlags_NoDragDrop,
                                   ImVec2(12 * ui::uiScale, 12 * ui::uiScale));
                ImGui::SameLine(0, 6 * ui::uiScale);
                std::string lbl = st.asmParts[pi2].name + "##p" + std::to_string(pi2);
                if (ImGui::RadioButton(lbl.c_str(), st.selectedPart == pi2)) {
                    st.selectedPart = pi2;
                    st.showPartMesh(pi2);
                }
                if (!st.asmParts[pi2].dims.empty()) {
                    ImGui::SameLine();
                    ImGui::PushStyleColor(ImGuiCol_Text,
                        ImVec4(palVp.textDim[0], palVp.textDim[1], palVp.textDim[2], 1));
                    ImGui::Text("  %s", st.asmParts[pi2].dims.c_str());
                    ImGui::PopStyleColor();
                }
            }
            ImGui::EndChild();
        }

        ImVec2 avail = ImGui::GetContentRegionAvail();
        int vw = (int)avail.x > 64 ? (int)avail.x : 64;
        int vh = (int)avail.y > 64 ? (int)avail.y : 64;
        renderer.resize(vw, vh);
        // C18: hard-lock the camera normal to the sketch plane while editing a sketch.
        if (inSketch) { float ly, lp; if (sketch->cameraLock(ly, lp)) { st.yaw = ly; st.pitch = lp; } }
        // C37-C: reset zoom-area state on mode transitions (avoids cross-mode state leak).
        // Check at frame start: if sketch mode OR plane-pick activated, disarm zoom-area.
        if (inSketch || st.sketchPlanePickActive) {
            s_zoomAreaArmed    = false;
            s_zoomAreaDragging = false;
            s_lmbDragActive    = false;
        }
        // Also disarm when a PM form is open (item 3 requirement).
        if (st.propertyManager && st.propertyManager->active()) {
            s_zoomAreaArmed    = false;
            s_zoomAreaDragging = false;
        }

        // Instant3D push-pull safety net (runs every frame, regardless of hover): if a sketch /
        // plane-pick / PM took over the mouse mid-drag, cancel (revert) so no zombie checkpoint/flag
        // survives. If the LMB was released while the cursor was OUTSIDE the viewport image (the
        // hover-gated release handler never ran), commit the drag at its last distance.
        if (s_pp.active) {
            bool ppPmNow = st.propertyManager && st.propertyManager->active();
            if (inSketch || st.sketchPlanePickActive || ppPmNow) {
                ppCancel(st);
            } else if (!ImGui::IsMouseDown(ImGuiMouseButton_Left)) {
                ppApplySample(st, s_pp.curDist, /*commit=*/true);
                s_pp.active = false;
                s_pp.checkpoint.clear();
                s_pp.haveReadout = false;
            }
        }

        // Instant3D dimension-drag safety net (mirrors the push-pull one): if a sketch / plane-pick /
        // PM took over, or the selected feature changed/vanished mid-drag, cancel (revert). If the
        // LMB was released outside the viewport image, commit at the last value. No zombie state.
        if (s_dim.active) {
            bool dimPmNow = st.propertyManager && st.propertyManager->active();
            bool featGone = st.selectedFeatureId != s_dim.featureId ||
                            findFeature(st.docMirror.tree(), s_dim.featureId) == nullptr;
            if (inSketch || st.sketchPlanePickActive || dimPmNow || featGone) {
                dimCancel(st);
            } else if (!ImGui::IsMouseDown(ImGuiMouseButton_Left)) {
                dimApplySample(st, s_dim.curValue, /*commit=*/true);
                s_dim.active = false;
                s_dim.checkpoint.clear();
                s_dim.haveReadout = false;
            }
        }

        // Move-component drag safety net (mirrors the push-pull one): if a sketch / plane-pick / PM
        // took over the mouse, or the doc is no longer an assembly, cancel (revert). If the LMB was
        // released outside the viewport image, do one final solve (commit). No zombie state.
        if (s_asmDrag.active) {
            bool asmPmNow = st.propertyManager && st.propertyManager->active();
            if (inSketch || st.sketchPlanePickActive || asmPmNow || !docIsAssembly(st)) {
                asmDragCancel(st);
            } else if (!ImGui::IsMouseDown(ImGuiMouseButton_Left)) {
                asmDragSample(st, io.MousePos, /*commit=*/true);
                s_asmDrag.active = false;
                s_asmDrag.checkpoint.clear();
            }
        }

        // C37-C: consume st.zoomToBBoxRequested (one-shot from feature tree "Zoom to Feature").
        if (st.zoomToBBoxRequested) {
            st.zoomToBBoxRequested = false;
            frameBBox(st, st.zoomToBBoxMin, st.zoomToBBoxMax);
        }

        // Reference planes: hidden while sketching (the sketch grid owns the view), else per state.
        // C25: a fresh part with no geometry auto-shows its datum planes (SolidWorks "pick a
        // plane to start" state); once geometry exists, visibility is the user's toggle.
        bool effPlane[3] = {st.showPlane[0], st.showPlane[1], st.showPlane[2]};
        if (!st.hasDocMesh && st.docMirror.hasDoc())
            effPlane[0] = effPlane[1] = effPlane[2] = true;
        // C37-C item 5: while sketchPlanePickActive, force-show all three origin planes (the sketcher
        // needs them visible so the user can pick one). This uses the same effPlane mechanism as
        // the empty-doc state (viewport_panel.cpp:290-294 convention).
        if (st.sketchPlanePickActive)
            effPlane[0] = effPlane[1] = effPlane[2] = true;
        if (inSketch) { bool none[3] = {false,false,false}; renderer.setPlanes(none, -1); }
        else if (st.sketchPlanePickActive && sketch != nullptr)
            // Plane-pick: the hovered plane (from last frame's overlay hit-test) gets the
            // emphasis brightening; the other two dim. One-frame lag is invisible.
            renderer.setPlanes(effPlane, sketch->planeHover());
        else          renderer.setPlanes(effPlane, st.planeEmphasis);
        // C25 W2-D: advance any in-flight camera tween (presets / Normal-To / zoom-to-fit) before
        // rendering. Sketch mode hard-locks the camera, so cancel/skip the tween while sketching.
        if (inSketch) cancelCameraAnimation();
        else          tickCameraAnimation(st);
        renderer.renderOrbit(st.yaw, st.pitch, st.zoom, st.pan);

        // ---- topology picking (C15): id pass + overlay highlights drawn over the shaded view ----
        const bool picking = st.hasDocMesh && st.idPass && st.overlays;
        if (picking) {
            float clipN[3][3] = {{1,0,0},{0,1,0},{0,0,1}};
            float clipOff[3] = {0,0,0};
            int clipCount = renderer.sectionClip(clipN, clipOff);
            st.idPass->setSectionClip(clipCount, clipN, clipOff);
            st.overlays->setSectionClip(clipCount, clipN, clipOff);
            // Render the R32UI id buffer for this exact camera frame so picks are pixel-accurate.
            st.idPass->render(renderer.camera());
            st.pickReady = true;
            // Re-draw hover + selection highlights into the studio color FBO before it's sampled.
            renderer.beginOverlay();
            // C37-C item 4: draw feature-highlight faces first (underneath selection/hover) using
            // a slightly dimmer tint. Empty vector is zero-cost. Stale keys after rebuild are
            // silently skipped inside drawFeatureHighlight.
            if (!st.featureHighlightFaces.empty())
                st.overlays->drawFeatureHighlight(renderer.camera(), st.featureHighlightFaces);
            std::vector<render::PickHit> selHits;
            selHits.reserve(st.selection.size());
            for (const auto& it : st.selection.items()) selHits.push_back(it.hit);
            st.overlays->draw(renderer.camera(), st.hover, selHits);
            renderer.endOverlay();
        }

        ImGui::Image((ImTextureID)(intptr_t)renderer.colorTexture(), ImVec2((float)vw, (float)vh),
                     ImVec2(0, 1), ImVec2(1, 0));
        ImVec2 vr0 = ImGui::GetItemRectMin(), vr1 = ImGui::GetItemRectMax();
        const bool imgHovered = ImGui::IsItemHovered();
        // C25 W2-D: camera navigation. All of it is suppressed while sketching (camera is
        // plane-locked) except the wheel zoom, which stays usable. Any drag-navigation cancels an
        // in-flight tween so the user takes over instantly.
        if (imgHovered && !inSketch) {
            // Pixels -> world units at the orbit target plane (matches drawCurrent's FOV/dist):
            //   dist = R / sin(fov/2) * 1.05 / zoom;  worldPerPx = 2*dist*tan(fov/2) / viewportH.
            const float deg = 3.14159265f / 180.0f;
            const float fov = Renderer::kFovDeg * deg;
            float zc = st.zoom < 0.05f ? 0.05f : st.zoom;
            float dist = renderer.modelRadius() / std::sin(fov * 0.5f) * 1.05f / zc;
            float worldPerPx = 2.0f * dist * std::tan(fov * 0.5f) / (float)(vh > 1 ? vh : 1);

            // Camera basis for screen-space pan: right (R) and up (U) in world space. Mirrors
            // drawCurrent: dir = view direction (target->eye), up = +Z (or +Y near a pole).
            float yr = st.yaw * deg, pr = st.pitch * deg;
            Vec3 dir(std::cos(pr) * std::sin(yr), -std::cos(pr) * std::cos(yr), std::sin(pr));
            Vec3 worldUp = (std::fabs(pr) > 89.0f * deg) ? Vec3(0, 1, 0) : Vec3(0, 0, 1);
            Vec3 V = normalize(dir);
            Vec3 R = normalize(cross(worldUp, V));   // screen-right in world space
            Vec3 U = normalize(cross(V, R));         // screen-up in world space

            auto applyPan = [&](float ddx, float ddy) {
                // Drag right (ddx>0) moves the model right -> target shifts left (-R). Drag down
                // (ddy>0, screen y is down) moves the model down -> target shifts up (+U).
                st.pan[0] += (-R.x * ddx + U.x * ddy) * worldPerPx;
                st.pan[1] += (-R.y * ddx + U.y * ddy) * worldPerPx;
                st.pan[2] += (-R.z * ddx + U.z * ddy) * worldPerPx;
            };
            auto clampPitch = [&]() {
                st.pitch = st.pitch > 89 ? 89 : (st.pitch < -89 ? -89 : st.pitch);
            };

            const bool ctrl = io.KeyCtrl, shift = io.KeyShift;

            // C25 W3-J: apply display style from AppState each frame (cheap no-op when unchanged).
            renderer.setDisplayStyle(static_cast<DisplayStyle>(st.displayStyle));

            // C25 W3-J: zoom-area rubber-band drag.
            if (s_zoomAreaArmed) {
                if (ImGui::IsMouseDragging(ImGuiMouseButton_Left)) {
                    if (!s_zoomAreaDragging) {
                        s_zoomAreaDragging = true;
                        s_zoomAreaP0 = io.MouseClickedPos[ImGuiMouseButton_Left];
                    }
                    s_zoomAreaP1 = io.MousePos;
                } else if (s_zoomAreaDragging &&
                           !ImGui::IsMouseDown(ImGuiMouseButton_Left)) {
                    // Released: compute zoom+pan to frame the rect.
                    float rx0 = s_zoomAreaP0.x < s_zoomAreaP1.x ? s_zoomAreaP0.x : s_zoomAreaP1.x;
                    float ry0 = s_zoomAreaP0.y < s_zoomAreaP1.y ? s_zoomAreaP0.y : s_zoomAreaP1.y;
                    float rx1 = s_zoomAreaP0.x > s_zoomAreaP1.x ? s_zoomAreaP0.x : s_zoomAreaP1.x;
                    float ry1 = s_zoomAreaP0.y > s_zoomAreaP1.y ? s_zoomAreaP0.y : s_zoomAreaP1.y;
                    float rw = rx1 - rx0, rh = ry1 - ry0;
                    if (rw > 4 && rh > 4) {
                        // Center of the rect in viewport-relative px (origin = viewport center).
                        float vcx = (vr0.x + vr1.x) * 0.5f, vcy = (vr0.y + vr1.y) * 0.5f;
                        float rcx = (rx0 + rx1) * 0.5f - vcx;
                        float rcy = (ry0 + ry1) * 0.5f - vcy;
                        // Pan shift: move target so rect center lands at view center.
                        // rcx>0 = rect center is right of view center: shift target rightward (+R).
                        // rcy>0 = rect center is below view center (screen Y down): shift target downward (-U).
                        float k = worldPerPx;
                        st.pan[0] += (R.x * rcx - U.x * rcy) * k;
                        st.pan[1] += (R.y * rcx - U.y * rcy) * k;
                        st.pan[2] += (R.z * rcx - U.z * rcy) * k;
                        // Zoom: scale so rect extent maps to ~90% of viewport.
                        float vExt = (float)(vw < vh ? vw : vh) * 0.9f;
                        float rExt = rw < rh ? rw : rh;
                        if (rExt > 0.0f) {
                            st.zoom *= vExt / rExt;
                            st.zoom = st.zoom < 0.1f ? 0.1f : (st.zoom > 10.0f ? 10.0f : st.zoom);
                        }
                        float newPan[3] = {st.pan[0], st.pan[1], st.pan[2]};
                        requestCameraAnimation(st, st.yaw, st.pitch, st.zoom, newPan);
                    }
                    s_zoomAreaArmed = false;
                    s_zoomAreaDragging = false;
                }
                // Esc or second click disarms.
                if (ImGui::IsKeyPressed(ImGuiKey_Escape) ||
                    ImGui::IsMouseClicked(ImGuiMouseButton_Right)) {
                    s_zoomAreaArmed = false;
                    s_zoomAreaDragging = false;
                }
            }

            // C37-C item 3: LMB drag threshold — track press origin and only commit to drag
            // once 4px * uiScale has been exceeded, so click-selects don't micro-orbit.
            if (ImGui::IsMouseClicked(ImGuiMouseButton_Left)) {
                s_lmbDragOrigin = io.MousePos;
                s_lmbDragActive = false;
            }
            if (!s_lmbDragActive && ImGui::IsMouseDown(ImGuiMouseButton_Left)) {
                float ddx = io.MousePos.x - s_lmbDragOrigin.x;
                float ddy = io.MousePos.y - s_lmbDragOrigin.y;
                const float kThresh = 4.0f * ui::uiScale;
                if (std::sqrt(ddx * ddx + ddy * ddy) >= kThresh)
                    s_lmbDragActive = true;
            }
            if (!ImGui::IsMouseDown(ImGuiMouseButton_Left))
                s_lmbDragActive = false;

            // C37-C: crosshair cursor while zoom-area is armed.
            if (s_zoomAreaArmed)
                st.requestCursor(ui::CursorKind::Crosshair);

            bool ppPmOpen = st.propertyManager && st.propertyManager->active();

            // ---- Move-Component drag (assembly): press / drag / release -------------------------
            // Active only when the open doc is an ASSEMBLY, a mesh is loaded, nothing else owns the
            // mouse, and EXACTLY ONE component body is selected. Pressing on that component's
            // geometry begins a constrained drag (doc_solve_mates {drag:{component, target}}); the
            // engine soft-pulls it toward the cursor along its free DOF and re-seats the rest.
            // Owns the mouse while active (orbit/pan/pick all skip while s_asmDrag.active). Runs
            // BEFORE push-pull so it wins in assemblies; the push-pull/dim blocks below also gate on
            // !s_asmDrag.active so the two gestures never fight.
            if (st.hasDocMesh && docIsAssembly(st) && !st.sketchPlanePickActive && !ppPmOpen &&
                !s_zoomAreaArmed) {
                // Press: require a single-component selection and a press on its geometry.
                if (ImGui::IsMouseClicked(ImGuiMouseButton_Left) && !s_asmDrag.active &&
                    st.selection.size() == 1) {
                    const render::PickHit& h = st.selection.items().front().hit;
                    if (h.valid()) {
                        const nlohmann::json* comp = asmComponentByOrdinal(st, h.body_id);
                        if (comp) {
                            // Only the picked component starts a drag — and only if the user pressed
                            // ON that component (the cursor's pick body matches the selection). This
                            // keeps a press in empty space / on another body from hijacking an orbit.
                            bool pressOnSel = st.hover.valid() && st.hover.body_id == h.body_id;
                            std::string cid = comp->value("id", std::string());
                            bool fixed = comp->value("fixed", false);
                            if (pressOnSel && !cid.empty()) {
                                if (fixed) {
                                    st.toast(ui::ToastLevel::Warn,
                                             "This component is fixed — float it to move (right-click "
                                             "> Float).");
                                } else {
                                    // Camera basis + px->mm scale at the component depth, captured for
                                    // the whole drag (R/U/worldPerPx already computed for this frame).
                                    std::string cp = st.docMirror.hasDoc()
                                                         ? st.docMirror.checkpoint(st.worker)
                                                         : std::string();
                                    s_asmDrag.reset();
                                    s_asmDrag.active     = true;
                                    s_asmDrag.component  = cid;
                                    s_asmDrag.checkpoint = cp;
                                    s_asmDrag.pressPos   = io.MousePos;
                                    s_asmDrag.startPos    = asmComponentOrigin(st, comp, h.body_id);
                                    s_asmDrag.camR        = R;
                                    s_asmDrag.camU        = U;
                                    s_asmDrag.worldPerPx  = worldPerPx;
                                    s_asmDrag.lastSent    = ImVec2(1e30f, 1e30f);
                                }
                            }
                        }
                    }
                }

                // Drag: project cursor motion to a world target and solve (throttled ~3px).
                if (s_asmDrag.active && ImGui::IsMouseDown(ImGuiMouseButton_Left)) {
                    st.requestCursor(ui::CursorKind::Hand);
                    cancelCameraAnimation();
                    ImVec2 mp = io.MousePos;
                    float ddx = mp.x - s_asmDrag.lastSent.x;
                    float ddy = mp.y - s_asmDrag.lastSent.y;
                    if (s_asmDrag.lastSent.x > 1e29f ||
                        std::sqrt(ddx * ddx + ddy * ddy) >= 3.0f * ui::uiScale)
                        asmDragSample(st, mp, /*commit=*/false);
                }

                // Release: one final solve (engine bumps revision = single undo) and clear state.
                if (s_asmDrag.active && ImGui::IsMouseReleased(ImGuiMouseButton_Left)) {
                    asmDragSample(st, io.MousePos, /*commit=*/true);
                    s_asmDrag.active = false;
                    s_asmDrag.checkpoint.clear();
                }

                // Esc cancels the drag (revert to the pre-drag pose).
                if (s_asmDrag.active && ImGui::IsKeyPressed(ImGuiKey_Escape))
                    asmDragCancel(st);
            }

            // ---- Instant3D push-pull: handle press / drag / release ----------------------------
            // Only when a doc mesh is loaded, no PM/sketch/zoom-area is owning the mouse, and the
            // press lands on the normal-axis handle of the single selected face. Owns the mouse for
            // its whole lifetime (orbit/pan/pick all skip while s_pp.active).
            if (st.hasDocMesh && !st.sketchPlanePickActive && !ppPmOpen && !s_zoomAreaArmed &&
                !s_asmDrag.active) {
                // Press: hit-test the handle arrow. We use the handle geometry computed for THIS
                // frame's camera; the centroid screen pos + the two tips define a thick segment.
                if (ImGui::IsMouseClicked(ImGuiMouseButton_Left) && !s_pp.active) {
                    ImVec2 cSp, tipP, tipN; float nsx, nsy, wpp; uint32_t bId, tId;
                    if (ppHandleGeometry(st, renderer, vr0, vr1, vw, vh, cSp, tipP, tipN,
                                         nsx, nsy, wpp, bId, tId)) {
                        // Distance from the press point to the handle axis segment (centroid..tip,
                        // both directions). A generous hit radius makes the thin arrow grabbable.
                        ImVec2 mp = io.MousePos;
                        float hitR = 10.0f * ui::uiScale;
                        // Project press onto the infinite handle axis through the centroid.
                        float dx = mp.x - cSp.x, dy = mp.y - cSp.y;
                        float along = dx * nsx + dy * nsy;          // signed distance along the axis
                        float perp  = std::fabs(dx * (-nsy) + dy * nsx);  // perpendicular distance
                        float armLenPx = 46.0f * ui::uiScale;
                        bool onAxis = perp <= hitR && std::fabs(along) <= armLenPx + hitR;
                        // Also accept a direct hit near either arrowhead tip.
                        auto nearTip = [&](const ImVec2& t) {
                            float ddx = mp.x - t.x, ddy = mp.y - t.y;
                            return std::sqrt(ddx*ddx + ddy*ddy) <= hitR * 1.6f;
                        };
                        if (onAxis || nearTip(tipP) || nearTip(tipN)) {
                            // Validate draggability + take a checkpoint BEFORE mutating anything.
                            std::string cp = st.docMirror.hasDoc()
                                                 ? st.docMirror.checkpoint(st.worker) : std::string();
                            auto probe = st.docMirror.pushPull(st.worker, bId, tId, 0.0, false);
                            if (!probe.ok) {
                                st.toast(ui::ToastLevel::Error,
                                         "Push-pull failed: " + probe.error_message);
                                if (!cp.empty()) st.docMirror.revert(st.worker, cp);
                            } else if (!probe.draggable) {
                                st.toast(ui::ToastLevel::Warn,
                                         "This face can't be dragged — " + probe.reason);
                                if (!cp.empty()) st.docMirror.revert(st.worker, cp);
                            } else {
                                // Begin the drag. The zero-distance probe left geometry unchanged.
                                s_pp.reset();
                                s_pp.active     = true;
                                s_pp.bodyId     = bId;
                                s_pp.topoId     = tId;
                                s_pp.checkpoint = cp;
                                s_pp.pressPos   = mp;
                                s_pp.nsx = nsx; s_pp.nsy = nsy;
                                s_pp.worldPerPx = wpp;
                                s_pp.curDist    = 0.0;
                                s_pp.lastApplied = 1e30f;
                                s_pp.haveReadout = true;
                                s_pp.param    = probe.param;
                                s_pp.newValue = probe.old_value;  // value before the drag
                                s_pp.clamped  = false;
                            }
                        }
                    }
                }

                // Drag: convert cursor motion to a cumulative signed distance and apply a sample.
                if (s_pp.active && ImGui::IsMouseDown(ImGuiMouseButton_Left)) {
                    st.requestCursor(ui::CursorKind::Crosshair);
                    cancelCameraAnimation();
                    float dx = io.MousePos.x - s_pp.pressPos.x;
                    float dy = io.MousePos.y - s_pp.pressPos.y;
                    float px = dx * s_pp.nsx + dy * s_pp.nsy;   // signed px along the normal axis
                    double cumulative = (double)px * (double)s_pp.worldPerPx;
                    s_pp.curDist = cumulative;
                    // Throttle: only call the worker when the cumulative distance changed enough to
                    // matter (~0.05mm), so a steady hold doesn't queue a backlog of rebuilds. The
                    // call is synchronous, so at most one is ever in flight.
                    if (std::fabs(cumulative - s_pp.lastApplied) >= 0.05)
                        ppApplySample(st, cumulative, /*commit=*/false);
                }

                // Release: commit exactly one undo entry at the final distance.
                if (s_pp.active && ImGui::IsMouseReleased(ImGuiMouseButton_Left)) {
                    ppApplySample(st, s_pp.curDist, /*commit=*/true);
                    // Refresh the mirror tree (commit may have changed revision) and clear state.
                    s_pp.active = false;
                    s_pp.checkpoint.clear();
                    // Keep the readout for one frame? No — clear; selection highlight remains.
                    s_pp.haveReadout = false;
                }
            }

            // ---- Instant3D dimension-handle drag: press / drag / release ------------------------
            // Runs AFTER the push-pull block and is gated on !s_pp.active, so the FACE ARROW WINS
            // when a single drivable face is selected (its press grabs the mouse first). Dimension
            // handles are shown/hit only when a feature is selected with NO single face active (tree
            // selection, body/edge pick, or multi-face), so the two gestures never fight. Owns the
            // mouse for its whole lifetime (orbit/pan/pick skip while s_dim.active).
            if (st.hasDocMesh && !st.sketchPlanePickActive && !ppPmOpen && !s_zoomAreaArmed &&
                !s_pp.active && !s_asmDrag.active) {
                // Press: hit-test every dimension handle of the selected feature.
                if (ImGui::IsMouseClicked(ImGuiMouseButton_Left) && !s_dim.active) {
                    // Only offer dimension handles when NO single face is selected (that case is the
                    // push-pull arrow's domain). A multi-face / edge / body / tree selection — or no
                    // topology selection at all but a selectedFeatureId — shows dim handles.
                    bool singleFaceSel =
                        st.selection.size() == 1 &&
                        st.selection.items().front().hit.valid() &&
                        st.selection.items().front().hit.kind == render::PickKind::Face;
                    if (!singleFaceSel) {
                        auto cands = dimCandidates(st);
                        ImVec2 mp = io.MousePos;
                        float hitR = 9.0f * ui::uiScale;
                        float armLenPx = 42.0f * ui::uiScale;
                        for (const auto& c : cands) {
                            ImVec2 aSp, tipP, tipN; float dsx, dsy, wpp, dpp;
                            if (!dimHandleGeometry(st, renderer, vr0, vr1, vw, vh, c, aSp, tipP,
                                                   tipN, dsx, dsy, wpp, dpp))
                                continue;
                            float dx = mp.x - aSp.x, dy = mp.y - aSp.y;
                            float along = dx * dsx + dy * dsy;
                            float perp  = std::fabs(dx * (-dsy) + dy * dsx);
                            bool onAxis = perp <= hitR && std::fabs(along) <= armLenPx + hitR;
                            auto nearTip = [&](const ImVec2& t) {
                                float ddx = mp.x - t.x, ddy = mp.y - t.y;
                                return std::sqrt(ddx*ddx + ddy*ddy) <= hitR * 1.6f;
                            };
                            if (!(onAxis || nearTip(tipP) || nearTip(tipN))) continue;
                            // Grab this handle. Snapshot the feature's params/refs at press so every
                            // sample sets this one field to an ABSOLUTE value from the original
                            // params (no compounding); the checkpoint backs the commit/cancel revert.
                            const nlohmann::json* feat =
                                findFeature(st.docMirror.tree(), st.selectedFeatureId);
                            if (!feat) break;
                            std::string cp = st.docMirror.checkpoint(st.worker);
                            s_dim.reset();
                            s_dim.active     = true;
                            s_dim.featureId  = st.selectedFeatureId;
                            s_dim.param      = c.param;
                            s_dim.angle      = c.angle;
                            s_dim.label      = c.label;
                            s_dim.checkpoint = cp;
                            s_dim.baseParams = feat->contains("params") && (*feat)["params"].is_object()
                                                   ? (*feat)["params"] : nlohmann::json::object();
                            s_dim.baseRefs   = feat->contains("refs") && (*feat)["refs"].is_object()
                                                   ? (*feat)["refs"] : nlohmann::json::object();
                            s_dim.oldValue   = c.value;
                            s_dim.curValue   = c.value;
                            s_dim.lastApplied = 1e30;
                            s_dim.pressPos   = mp;
                            s_dim.dsx = dsx; s_dim.dsy = dsy;
                            s_dim.worldPerPx = wpp; s_dim.degPerPx = dpp;
                            s_dim.haveReadout = true;
                            s_dim.clamped = false;
                            break;   // one handle per press
                        }
                    }
                }

                // Drag: convert cursor motion to an absolute value and apply a throttled sample.
                if (s_dim.active && ImGui::IsMouseDown(ImGuiMouseButton_Left)) {
                    st.requestCursor(ui::CursorKind::Crosshair);
                    cancelCameraAnimation();
                    float dx = io.MousePos.x - s_dim.pressPos.x;
                    float dy = io.MousePos.y - s_dim.pressPos.y;
                    float px = dx * s_dim.dsx + dy * s_dim.dsy;   // signed px along the handle axis
                    double newVal;
                    if (s_dim.angle)
                        newVal = s_dim.oldValue + (double)px * (double)s_dim.degPerPx;
                    else
                        newVal = s_dim.oldValue + (double)px * (double)s_dim.worldPerPx;
                    // Clamp to a sane minimum (mirror the engine push-pull floor of 0.1mm; angles to
                    // 1..360 deg). Flag clamped so the readout shows the limit.
                    s_dim.clamped = false;
                    if (s_dim.angle) {
                        if (newVal < 1.0)   { newVal = 1.0;   s_dim.clamped = true; }
                        if (newVal > 360.0) { newVal = 360.0; s_dim.clamped = true; }
                    } else {
                        if (newVal < 0.1)   { newVal = 0.1;   s_dim.clamped = true; }
                    }
                    s_dim.curValue = newVal;
                    // Throttle: only call the worker when the value moved enough to matter (angles a
                    // touch coarser). Synchronous call => at most one ever in flight.
                    double thresh = s_dim.angle ? 0.25 : 0.05;
                    if (std::fabs(newVal - s_dim.lastApplied) >= thresh)
                        dimApplySample(st, newVal, /*commit=*/false);
                }

                // Release: commit exactly one undo entry at the final value.
                if (s_dim.active && ImGui::IsMouseReleased(ImGuiMouseButton_Left)) {
                    dimApplySample(st, s_dim.curValue, /*commit=*/true);
                    s_dim.active = false;
                    s_dim.checkpoint.clear();
                    s_dim.haveReadout = false;
                }
            }

            // Left-drag: orbit or pan depending on vpDragMode. Cancels any tween.
            // Zoom-area mode intercepts left-drag above; skip orbit/pan if armed.
            // C37-C: only starts dragging once the 4px threshold is crossed (s_lmbDragActive).
            // Instant3D: a push-pull OR dimension drag owns the mouse — never orbit/pan while active.
            if (!s_pp.active && !s_dim.active && !s_asmDrag.active && !s_zoomAreaArmed &&
                s_lmbDragActive && (io.MouseDelta.x != 0 || io.MouseDelta.y != 0)) {
                cancelCameraAnimation();
                if (st.vpDragMode == 1) {
                    applyPan(io.MouseDelta.x, io.MouseDelta.y);
                } else {
                    st.yaw += io.MouseDelta.x * 0.4f;
                    st.pitch += io.MouseDelta.y * 0.4f;
                    clampPitch();
                }
            }
            // W1-B (item 3): in pan-drag mode, show the Hand cursor over the viewport so the user
            // sees they can grab-pan (SolidWorks behavior). The cursor manager in app.cpp consumes
            // this per-frame request. (Wave-2 sketch tools / PM wells request Crosshair similarly.)
            if (st.vpDragMode == 1 && !s_zoomAreaArmed)
                st.requestCursor(ui::CursorKind::Hand);

            // Middle-button navigation (SolidWorks bindings):
            //   MMB        = orbit (same rate as left-drag)
            //   Ctrl+MMB   = pan
            //   Shift+MMB  = zoom (vertical, smooth exponential)
            if (ImGui::IsMouseDragging(ImGuiMouseButton_Middle) &&
                (io.MouseDelta.x != 0 || io.MouseDelta.y != 0)) {
                cancelCameraAnimation();
                if (ctrl) {
                    applyPan(io.MouseDelta.x, io.MouseDelta.y);
                } else if (shift) {
                    // Drag up = zoom in. exp keeps each pixel a constant multiplicative step.
                    st.zoom *= std::exp(-io.MouseDelta.y * 0.005f);
                    st.zoom = st.zoom < 0.1f ? 0.1f : (st.zoom > 10 ? 10 : st.zoom);
                } else {
                    st.yaw += io.MouseDelta.x * 0.4f;
                    st.pitch += io.MouseDelta.y * 0.4f;
                    clampPitch();
                }
            }

            // Double-click MMB = animated zoom-to-fit (zoom->1, pan->0, orientation kept).
            if (ImGui::IsMouseDoubleClicked(ImGuiMouseButton_Middle)) {
                applyViewPreset(st, ViewPreset::ZoomFit);
            }

            // Wheel zoom toward the cursor: scale zoom, then shift pan so the world point under the
            // cursor stays put. Cursor offset from viewport center, in pixels.
            if (io.MouseWheel != 0) {
                cancelCameraAnimation();
                float oldZoom = zc;
                st.zoom *= (1.0f + io.MouseWheel * 0.1f);
                st.zoom = st.zoom < 0.1f ? 0.1f : (st.zoom > 10 ? 10 : st.zoom);
                float newZoom = st.zoom;
                if (newZoom != oldZoom) {
                    float cxp = io.MousePos.x - (vr0.x + (vr1.x - vr0.x) * 0.5f);
                    float cyp = io.MousePos.y - (vr0.y + (vr1.y - vr0.y) * 0.5f);
                    // worldPerPx is inversely proportional to zoom; the target must move so the
                    // cursor ray hits the same world point. delta = cursorOffset * (1/old - 1/new)
                    // in world units, projected onto the camera plane (R right, U up; y is down).
                    float k = worldPerPx * (1.0f - oldZoom / newZoom);
                    st.pan[0] += ( R.x * cxp - U.x * cyp) * k;
                    st.pan[1] += ( R.y * cxp - U.y * cyp) * k;
                    st.pan[2] += ( R.z * cxp - U.z * cyp) * k;
                }
            }
        } else if (imgHovered && inSketch) {
            // Sketch mode: wheel still zooms (camera orientation stays plane-locked, no pan).
            if (io.MouseWheel != 0) {
                st.zoom *= (1.0f + io.MouseWheel * 0.1f);
                st.zoom = st.zoom < 0.1f ? 0.1f : (st.zoom > 10 ? 10 : st.zoom);
            }
        }

        // C37-C: keyboard view shortcuts — globalized: fire when the viewport is VISIBLE and
        // !io.WantTextInput and no modal open, regardless of hover. Keep !inSketch for the preset
        // rotations. Ctrl+8 (Normal To) is allowed while in sketch (item 1). F/Shift+F blocked in
        // sketch (camera is locked; ZoomFit would fight the lock). Space opens ViewSelector.
        // Condition: viewport is showing (we are inside the Begin/End block), and no text input.
        if (st.showViewport && !io.WantTextInput) {
            bool noModal = !ImGui::IsPopupOpen("", ImGuiPopupFlags_AnyPopupId);
            if (noModal) {
                if (io.KeyCtrl && !inSketch) {
                    // Ctrl+1..7 = view presets (not in sketch).
                    if      (ImGui::IsKeyPressed(ImGuiKey_1)) applyViewPreset(st, ViewPreset::Front);
                    else if (ImGui::IsKeyPressed(ImGuiKey_2)) applyViewPreset(st, ViewPreset::Back);
                    else if (ImGui::IsKeyPressed(ImGuiKey_3)) applyViewPreset(st, ViewPreset::Left);
                    else if (ImGui::IsKeyPressed(ImGuiKey_4)) applyViewPreset(st, ViewPreset::Right);
                    else if (ImGui::IsKeyPressed(ImGuiKey_5)) applyViewPreset(st, ViewPreset::Top);
                    else if (ImGui::IsKeyPressed(ImGuiKey_6)) applyViewPreset(st, ViewPreset::Bottom);
                    else if (ImGui::IsKeyPressed(ImGuiKey_7)) applyViewPreset(st, ViewPreset::Isometric);
                }
                // Ctrl+8 = Normal To — allowed in AND out of sketch (item 1).
                if (io.KeyCtrl && ImGui::IsKeyPressed(ImGuiKey_8))
                    normalToSelectionOrNearestAxis(st, inSketch);
                if (!inSketch) {
                    // F = Zoom to Fit (not in sketch).
                    if (!io.KeyShift && ImGui::IsKeyPressed(ImGuiKey_F))
                        applyViewPreset(st, ViewPreset::ZoomFit);
                    // C37-C item 2: Shift+F = Zoom to Selection (not in sketch).
                    if (io.KeyShift && ImGui::IsKeyPressed(ImGuiKey_F))
                        zoomToSelection(st);
                    // C37-C item 3: Space = open View Selector (when viewport visible, not sketching).
                    if (imgHovered && ImGui::IsKeyPressed(ImGuiKey_Space))
                        /* Space handled by ViewSelector::update() below — it reads imgHovered and
                           !inSketch; we just ensure no double-handling here. */ (void)0;
                }
                // Esc: cancel in-flight camera tween (selection-clear Esc handling lives in the
                // picking block below; canceling here is harmless when there is no animation).
                if (ImGui::IsKeyPressed(ImGuiKey_Escape) && cameraAnimating()) cancelCameraAnimation();
            }
        }

        // ---- C18: sketch overlay owns the mouse while a sketch is being edited ----
        // C37-C item 5: also call drawOverlay during sketchPlanePickActive (no active sketch yet,
        // but the sketcher agent needs to draw plane hover/click overlay). Guard with sketch != null.
        // C40: inactive sketches ghost-render in every mode (SolidWorks grey sketches); the
        // active sketch (if any) is skipped — drawOverlay below owns it.
        if (sketch != nullptr) sketch->drawGhosts(st, vr0, vr1, vw, vh);
        if (inSketch) {
            sketch->drawOverlay(st, io, vr0, vr1, vw, vh, imgHovered);
        } else if (st.sketchPlanePickActive && sketch != nullptr) {
            // Plane-pick mode: sketcher draws the plane quads/hover/click, but no sketch is active.
            sketch->drawOverlay(st, io, vr0, vr1, vw, vh, imgHovered);
        }

        // ---- C51: feature-PM region picker (hover-highlight + click-to-toggle profiles) ----
        // Runs when a feature PropertyManager with a profile/sections well is open and no sketch is
        // being edited. Returns true while the cursor is over a sketch region, so the model-pick
        // click below is suppressed (a region click must not also select a model face/edge).
        bool regionPickerHot = false;
        if (sketch != nullptr && !inSketch && !st.sketchPlanePickActive)
            regionPickerHot = sketch->drawRegionPicker(st, io, vr0, vr1, vw, vh, imgHovered);

        // ---- hover + click selection (once per frame; only while a doc mesh is loaded) ----
        // C25 W2-E: suppressed while the glass-box View Selector is up (it owns the click).
        // C37-C item 5: also suppressed while sketchPlanePickActive (the overlay owns the mouse).
        if (picking && !inSketch && !s_viewSelector.active() && !st.sketchPlanePickActive) {
            // Instant3D: while a push-pull OR dimension drag owns the mouse, freeze hover and swallow
            // the click so the press that grabbed the handle doesn't also re-select / clear.
            if (imgHovered && !s_lmbDragActive && !regionPickerHot && !s_pp.active && !s_dim.active) {
                int px = (int)(io.MousePos.x - vr0.x);
                int py = (int)(io.MousePos.y - vr0.y);
                st.hover = st.idPass->pickFinal(px, py, vw, vh);
            } else {
                st.hover = render::PickHit{};
            }

            // Click = replace, Ctrl-click = toggle. Empty-space click clears.
            // C37-C item 3: guard against micro-orbit clicks — only fire if drag was not active.
            // C51: a click consumed by the region picker never falls through to model selection.
            // Instant3D: a click that started a push-pull / dimension drag is NOT a selection.
            if (imgHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left) &&
                !s_lmbDragActive && !regionPickerHot && !s_pp.active && !s_dim.active) {
                if (io.KeyCtrl) {
                    if (st.hover.valid()) st.selection.toggle(st.hover);
                } else {
                    st.selection.selectOne(st.hover);  // clears if hover invalid
                }
            }
            // ESC: during a push-pull / dimension drag, cancel it (revert) and DON'T clear the
            // selection. Otherwise ESC clears the selection while the viewport is focused/hovered.
            // C37-C item 5: do NOT consume Esc while sketchPlanePickActive (guard above already
            // prevents this block from running in that case, so this Esc is safe).
            if (ImGui::IsKeyPressed(ImGuiKey_Escape)) {
                if (s_pp.active) ppCancel(st);
                else if (s_dim.active) dimCancel(st);
                else if (imgHovered) st.selection.clear();
            }

            // Status text: prefer the live hover, else the last selected item.
            if (st.hover.valid()) {
                st.pickStatus = st.pickLabel(st.hover);
            } else if (!st.selection.empty()) {
                st.pickStatus = st.pickLabel(st.selection.items().back().hit);
            } else {
                st.pickStatus.clear();
            }
        }

        // ---- C25: mouse gesture wheel (right-drag) + 'S' shortcut bar ----
        // Gestures are suppressed while in sketch mode (camera is plane-locked; no view presets).
        if (!inSketch) {
            s_gestures.update(st, imgHovered, vr0, vr1);
            s_gestures.draw(ImGui::GetWindowDrawList());
        }
        // Shortcut bar is context-aware: shows sketch tools when inSketch, model tools otherwise.
        s_shortcutBar.update(st, imgHovered, inSketch, vr0, vr1);
        s_shortcutBar.draw(st, inSketch);

        // ---- C25 W3-J: viewport right-click context menu ----
        // Opened by mouse_gestures when right button released without drag (kRmbContextMenuHook).
        if (st.wantContextMenu && !inSketch) {
            st.wantContextMenu = false;
            ImGui::OpenPopup("##vp_ctx");
        }
        // C26: appearance state shared between the vp_ctx menu item and its sibling picker popup.
        static float s_vpColor[3]   = {0.71f, 0.76f, 0.82f};
        static float s_vpMetallic = 0.0f, s_vpRoughness = 0.5f;  // C32-A material
        static std::string s_vpKey;          // "b<ordinal>" of the body to recolor
        static bool  s_openVpPicker = false;
        // The picked body's stable appearance key (part bodies: "b<ordinal>", matching the engine).
        // Appearance is offered for part docs only; assembly bodies key by component id, which the
        // viewport pick alone can't resolve (deferred — see report).
        // NOTE: tree() is null until the first ops response after doc_new — json::value()
        // on a null json THROWS (this crashed the m1 scenario); guard with is_object().
        bool vpIsPart = st.docMirror.hasDoc() && st.docMirror.tree().is_object() &&
                        st.docMirror.tree().value("schema", std::string()) != "aicad.asm/1";
        std::string vpBodyKey;
        if (vpIsPart && !st.selection.empty()) {
            uint32_t bid = st.selection.items().back().hit.body_id;
            if (bid != 0) vpBodyKey = "b" + std::to_string(bid);
        }

        if (ImGui::BeginPopup("##vp_ctx")) {
            // Selection section (shown only when something is selected / hovered).
            if (!st.pickStatus.empty()) {
                ImGui::PushStyleColor(ImGuiCol_Text,
                    ImVec4(ui::palette.textDim[0], ui::palette.textDim[1],
                           ui::palette.textDim[2], 1.0f));
                ImGui::TextUnformatted(st.pickStatus.c_str());
                ImGui::PopStyleColor();
                if (ImGui::MenuItem("Clear Selection")) st.selection.clear();
                // C26: recolor the selected body. Opens the shared picker (a sibling popup).
                if (!vpBodyKey.empty()) {
                    if (ImGui::MenuItem("Appearance...")) {
                        s_vpKey = vpBodyKey;
                        s_vpColor[0] = 0.71f; s_vpColor[1] = 0.76f; s_vpColor[2] = 0.82f;
                        s_vpMetallic = 0.0f; s_vpRoughness = 0.5f;
                        s_openVpPicker = true;
                    }
                    if (ImGui::MenuItem("Clear Appearance"))
                        applyAppearanceFromViewport(st, vpBodyKey, nullptr);
                }
                ImGui::Separator();
            }
            // View presets submenu.
            // C37-C: added Normal To (Ctrl+8) and Zoom to Selection (Shift+F).
            if (ImGui::BeginMenu("View Orientation")) {
                struct { const char* lbl; ViewPreset p; } prows[] = {
                    {"Front",    ViewPreset::Front},
                    {"Back",     ViewPreset::Back},
                    {"Left",     ViewPreset::Left},
                    {"Right",    ViewPreset::Right},
                    {"Top",      ViewPreset::Top},
                    {"Bottom",   ViewPreset::Bottom},
                    {"Isometric",ViewPreset::Isometric},
                };
                for (const auto& r : prows)
                    if (ImGui::MenuItem(r.lbl)) applyViewPreset(st, r.p);
                ImGui::Separator();
                // C37-C item 1: Normal To — available when a face is selected or always for axis fallback.
                if (ImGui::MenuItem("Normal To (Ctrl+8)"))
                    normalToSelectionOrNearestAxis(st, inSketch);
                ImGui::EndMenu();
            }
            if (ImGui::MenuItem("Zoom to Fit (F)"))   applyViewPreset(st, ViewPreset::ZoomFit);
            // C37-C item 2: Zoom to Selection — disabled when nothing is selected.
            {
                bool hasSelection = !st.selection.empty();
                if (!hasSelection) ImGui::BeginDisabled();
                if (ImGui::MenuItem("Zoom to Selection (Shift+F)")) zoomToSelection(st);
                if (!hasSelection) ImGui::EndDisabled();
            }
            if (ImGui::MenuItem("Zoom to Area")) {
                s_zoomAreaArmed    = true;
                s_zoomAreaDragging = false;
            }
            // Display style submenu.
            if (ImGui::BeginMenu("Display Style")) {
                struct { const char* lbl; int val; } drows[] = {
                    {"Shaded",                0},
                    {"Shaded with Edges",     1},
                    {"Hidden Lines Removed",  2},
                    {"Hidden Lines Visible",  3},
                    {"Wireframe",             4},
                };
                for (const auto& dr : drows) {
                    if (ImGui::MenuItem(dr.lbl, nullptr, st.displayStyle == dr.val)) {
                        st.displayStyle = dr.val;
                        renderer.setDisplayStyle(static_cast<DisplayStyle>(dr.val));
                    }
                }
                ImGui::EndMenu();
            }
            if (ImGui::MenuItem("Section View", nullptr, s_sectionTool.active()))
                s_sectionTool.toggle(st);
            // Hide/show submenu.
            if (ImGui::BeginMenu("Hide / Show")) {
                bool allPlanes = st.showPlane[0] && st.showPlane[1] && st.showPlane[2];
                if (ImGui::MenuItem("Reference Planes", nullptr, allPlanes)) {
                    bool nv = !allPlanes;
                    st.showPlane[0] = st.showPlane[1] = st.showPlane[2] = nv;
                }
                ImGui::MenuItem("Origin Triad",  nullptr, &st.showTriad);
                ImGui::MenuItem("Ground Shadow", nullptr, &st.showGroundShadow);
                ImGui::MenuItem("RealView reflections", nullptr, &st.showRealView);
                ImGui::EndMenu();
            }
            ImGui::Separator();
            // C37-C item 3: wire the dead "View Selector (Space)" menu item to actually open the selector.
            if (ImGui::MenuItem("View Selector (Space)")) {
                if (!s_viewSelector.active()) s_viewSelector.open();
            }
            ImGui::EndPopup();
        }

        // C26: appearance picker — a SIBLING popup of vp_ctx (opened from its menu item) to avoid
        // nested-popup ordering issues. Direct-applies on commit (safe outside tree iteration).
        if (s_openVpPicker) { ImGui::OpenPopup("##appearance_vp"); s_openVpPicker = false; }
        if (appearanceColorPicker("##appearance_vp", s_vpColor, &s_vpMetallic, &s_vpRoughness)
            && !s_vpKey.empty())
            applyAppearanceFromViewport(st, s_vpKey, s_vpColor, s_vpMetallic, s_vpRoughness);

        // ---- C25 W3-J: heads-up view toolbar (SolidWorks-style, fully functional) ----
        // Centered pill at the viewport top; hidden while editing a sketch.
        // Groups: [zoom-fit | zoom-area | pan | rotate] [view-orientation ▾] [display-style ▾]
        //         [hide/show ▾] [section]
        if (!inSketch) {
            const auto& hpal = ui::palette;
            ImDrawList* hdl = ImGui::GetWindowDrawList();
            const float s   = ui::uiScale;
            float btn = 26.0f * s, gap = 3.0f * s, padX = 8.0f * s, padY = 4.0f * s;

            // 9 buttons: zoom-fit, zoom-area, pan, rotate, view-orient▾, display-style▾, hide/show▾, section
            const int nHU = 8;
            float barW = nHU * btn + (nHU - 1) * gap + padX * 2;
            float barH = btn + padY * 2;
            float bx0  = vr0.x + ((vr1.x - vr0.x) - barW) * 0.5f;
            float by0  = vr0.y + 8.0f * s;
            float byi  = by0 + padY;

            // Chip background.
            hdl->AddRectFilled(ImVec2(bx0, by0), ImVec2(bx0 + barW, by0 + barH),
                               ui::Palette::toU32(hpal.overlayBg), barH * 0.30f);
            hdl->AddRect(ImVec2(bx0, by0), ImVec2(bx0 + barW, by0 + barH),
                         ui::Palette::toU32(hpal.overlayBorder), barH * 0.30f);

            // Helper: draw one toolbar icon button. Returns true on click.
            // active=true fills with accentSoft (toggle state).
            auto huBtn = [&](int idx, const char* iconName, const char* tip, bool active) -> bool {
                float bxi = bx0 + padX + (float)idx * (btn + gap);
                ImGui::SetCursorScreenPos(ImVec2(bxi, byi));
                ImGui::PushID(idx);
                bool clicked = ImGui::InvisibleButton("##hb", ImVec2(btn, btn));
                bool hov = ImGui::IsItemHovered();
                ImU32 fill = active  ? ui::Palette::toU32(hpal.accentSoft) :
                             hov     ? ui::Palette::toU32(hpal.buttonHovered) : 0;
                if (fill)
                    hdl->AddRectFilled(ImVec2(bxi, byi), ImVec2(bxi + btn, byi + btn), fill, 3.0f * s);
                IconUV iv = ui::getIcon(iconName, 20);
                float iz = 18.0f * s, off = (btn - iz) * 0.5f;
                hdl->AddImage(iv.texId, ImVec2(bxi + off, byi + off),
                              ImVec2(bxi + off + iz, byi + off + iz), iv.uv0, iv.uv1,
                              IM_COL32(255, 255, 255, 235));
                if (hov) ImGui::SetTooltip("%s", tip);
                ImGui::PopID();
                return clicked;
            };
            // Separator tick between groups.
            auto huSep = [&](int afterIdx) {
                float bxi = bx0 + padX + (float)afterIdx * (btn + gap);
                hdl->AddLine(ImVec2(bxi + btn + gap * 0.5f, byi + 3 * s),
                             ImVec2(bxi + btn + gap * 0.5f, byi + btn - 3 * s),
                             ui::Palette::toU32(hpal.border), 1.0f);
            };

            ImGui::PushID("##headsup");

            // [0] Zoom to Fit
            if (huBtn(0, "zoom_fit", "Zoom to Fit (F)", false))
                applyViewPreset(st, ViewPreset::ZoomFit);

            // [1] Zoom Area (toggle; left-drag = rubber-band, Esc disarms)
            if (huBtn(1, "zoom_area", "Zoom to Area", s_zoomAreaArmed)) {
                s_zoomAreaArmed   = !s_zoomAreaArmed;
                s_zoomAreaDragging = false;
            }

            // [2] Pan mode toggle
            if (huBtn(2, "pan_view", "Pan (left-drag while active)", st.vpDragMode == 1))
                st.vpDragMode = (st.vpDragMode == 1) ? 0 : 1;

            // [3] Rotate mode toggle (default)
            if (huBtn(3, "rotate_view", "Rotate (left-drag)", st.vpDragMode == 0))
                st.vpDragMode = 0;

            huSep(3);  // ---- separator: navigation | orientation

            // [4] View orientation dropdown ▾
            {
                float bxi = bx0 + padX + 4.0f * (btn + gap);
                ImGui::SetCursorScreenPos(ImVec2(bxi, byi));
                ImGui::PushID(4);
                bool clicked = ImGui::InvisibleButton("##hb", ImVec2(btn, btn));
                bool hov = ImGui::IsItemHovered();
                if (hov) hdl->AddRectFilled(ImVec2(bxi, byi), ImVec2(bxi + btn, byi + btn),
                                             ui::Palette::toU32(hpal.buttonHovered), 3.0f);
                IconUV iv = ui::getIcon("view_selector", 20);
                float iz = 18.0f * s, off = (btn - iz) * 0.5f;
                hdl->AddImage(iv.texId, ImVec2(bxi + off, byi + off),
                              ImVec2(bxi + off + iz, byi + off + iz), iv.uv0, iv.uv1,
                              IM_COL32(255, 255, 255, 235));
                if (hov) ImGui::SetTooltip("View Orientation");
                if (clicked) ImGui::OpenPopup("##hu_vieworient");
                ImGui::SetNextWindowPos(ImVec2(bxi, byi + btn + 4.0f * s), ImGuiCond_Always);
                if (ImGui::BeginPopup("##hu_vieworient")) {
                    struct { const char* label; ViewPreset p; } vrows[] = {
                        {"Front",    ViewPreset::Front},
                        {"Back",     ViewPreset::Back},
                        {"Left",     ViewPreset::Left},
                        {"Right",    ViewPreset::Right},
                        {"Top",      ViewPreset::Top},
                        {"Bottom",   ViewPreset::Bottom},
                        {"Isometric",ViewPreset::Isometric},
                    };
                    for (const auto& r : vrows) {
                        if (ImGui::MenuItem(r.label)) applyViewPreset(st, r.p);
                    }
                    ImGui::Separator();
                    // C37-C item 1: Normal To entry in HUD view-orientation dropdown.
                    if (ImGui::MenuItem("Normal To (Ctrl+8)"))
                        normalToSelectionOrNearestAxis(st, inSketch);
                    ImGui::Separator();
                    // C37-C item 2: Zoom to Selection entry in HUD view dropdown.
                    {
                        bool hasSelection = !st.selection.empty();
                        if (!hasSelection) ImGui::BeginDisabled();
                        if (ImGui::MenuItem("Zoom to Selection (Shift+F)")) zoomToSelection(st);
                        if (!hasSelection) ImGui::EndDisabled();
                    }
                    ImGui::Separator();
                    // C37-C item 3: wire View Selector to actually open the selector widget.
                    if (ImGui::MenuItem("View Selector (Space)")) {
                        if (!s_viewSelector.active()) s_viewSelector.open();
                    }
                    ImGui::EndPopup();
                }
                ImGui::PopID();
            }

            huSep(4);  // ---- separator: orientation | display

            // [5] Display style dropdown ▾
            {
                // Icon reflects current style.
                static const char* kDsIcons[] = {
                    "display_shaded", "display_shaded_edges", "display_hlr",
                    "display_hlv",    "display_wireframe"
                };
                const char* curIcon = kDsIcons[st.displayStyle < 5 ? st.displayStyle : 1];
                float bxi = bx0 + padX + 5.0f * (btn + gap);
                ImGui::SetCursorScreenPos(ImVec2(bxi, byi));
                ImGui::PushID(5);
                bool clicked = ImGui::InvisibleButton("##hb", ImVec2(btn, btn));
                bool hov = ImGui::IsItemHovered();
                if (hov) hdl->AddRectFilled(ImVec2(bxi, byi), ImVec2(bxi + btn, byi + btn),
                                             ui::Palette::toU32(hpal.buttonHovered), 3.0f);
                IconUV iv = ui::getIcon(curIcon, 20);
                float iz = 18.0f * s, off = (btn - iz) * 0.5f;
                hdl->AddImage(iv.texId, ImVec2(bxi + off, byi + off),
                              ImVec2(bxi + off + iz, byi + off + iz), iv.uv0, iv.uv1,
                              IM_COL32(255, 255, 255, 235));
                if (hov) ImGui::SetTooltip("Display Style");
                if (clicked) ImGui::OpenPopup("##hu_dispstyle");
                ImGui::SetNextWindowPos(ImVec2(bxi, byi + btn + 4.0f * s), ImGuiCond_Always);
                if (ImGui::BeginPopup("##hu_dispstyle")) {
                    struct { const char* icon; const char* label; int val; } drows[] = {
                        {"display_shaded",       "Shaded",                0},
                        {"display_shaded_edges", "Shaded with Edges",     1},
                        {"display_hlr",          "Hidden Lines Removed",  2},
                        {"display_hlv",          "Hidden Lines Visible",  3},
                        {"display_wireframe",    "Wireframe",             4},
                    };
                    for (const auto& dr : drows) {
                        IconUV div = ui::getIcon(dr.icon, 20);
                        ImGui::Image(div.texId, ImVec2(16.0f * s, 16.0f * s), div.uv0, div.uv1);
                        ImGui::SameLine(0, 6.0f * s);
                        bool sel = (st.displayStyle == dr.val);
                        if (sel) ImGui::PushStyleColor(ImGuiCol_Text,
                            ImVec4(hpal.accent[0], hpal.accent[1], hpal.accent[2], 1.0f));
                        if (ImGui::MenuItem(dr.label, nullptr, sel)) {
                            st.displayStyle = dr.val;
                            renderer.setDisplayStyle(static_cast<DisplayStyle>(dr.val));
                        }
                        if (sel) ImGui::PopStyleColor();
                    }
                    ImGui::EndPopup();
                }
                ImGui::PopID();
            }

            // [6] Hide/Show items dropdown ▾
            {
                float bxi = bx0 + padX + 6.0f * (btn + gap);
                ImGui::SetCursorScreenPos(ImVec2(bxi, byi));
                ImGui::PushID(6);
                bool clicked = ImGui::InvisibleButton("##hb", ImVec2(btn, btn));
                bool hov = ImGui::IsItemHovered();
                if (hov) hdl->AddRectFilled(ImVec2(bxi, byi), ImVec2(bxi + btn, byi + btn),
                                             ui::Palette::toU32(hpal.buttonHovered), 3.0f);
                IconUV iv = ui::getIcon("hide_show_eye", 20);   // C25b: proper visibility glyph
                float iz = 18.0f * s, off = (btn - iz) * 0.5f;
                hdl->AddImage(iv.texId, ImVec2(bxi + off, byi + off),
                              ImVec2(bxi + off + iz, byi + off + iz), iv.uv0, iv.uv1,
                              IM_COL32(255, 255, 255, 235));
                if (hov) ImGui::SetTooltip("Hide / Show Items");
                if (clicked) ImGui::OpenPopup("##hu_hideshow");
                ImGui::SetNextWindowPos(ImVec2(bxi, byi + btn + 4.0f * s), ImGuiCond_Always);
                if (ImGui::BeginPopup("##hu_hideshow")) {
                    bool allPlanes = st.showPlane[0] && st.showPlane[1] && st.showPlane[2];
                    if (ImGui::MenuItem("Reference Planes", nullptr, allPlanes)) {
                        bool nv = !allPlanes;
                        st.showPlane[0] = st.showPlane[1] = st.showPlane[2] = nv;
                    }
                    ImGui::MenuItem("Origin Triad",    nullptr, &st.showTriad);
                    ImGui::MenuItem("Ground Shadow",   nullptr, &st.showGroundShadow);
                    ImGui::MenuItem("RealView reflections", nullptr, &st.showRealView);
                    ImGui::EndPopup();
                }
                ImGui::PopID();
            }

            huSep(6);  // ---- separator: hide/show | section

            // [7] Section view toggle
            if (huBtn(7, "section_view", "Section View", s_sectionTool.active()))
                s_sectionTool.toggle(st);

            ImGui::PopID();

            // ---- C25 W3-J: zoom-area rubber-band rect overlay ----
            if (s_zoomAreaDragging) {
                float rx0 = s_zoomAreaP0.x < s_zoomAreaP1.x ? s_zoomAreaP0.x : s_zoomAreaP1.x;
                float ry0 = s_zoomAreaP0.y < s_zoomAreaP1.y ? s_zoomAreaP0.y : s_zoomAreaP1.y;
                float rx1 = s_zoomAreaP0.x > s_zoomAreaP1.x ? s_zoomAreaP0.x : s_zoomAreaP1.x;
                float ry1 = s_zoomAreaP0.y > s_zoomAreaP1.y ? s_zoomAreaP0.y : s_zoomAreaP1.y;
                hdl->AddRectFilled(ImVec2(rx0, ry0), ImVec2(rx1, ry1),
                                   ui::Palette::toU32(hpal.accentSoft));
                hdl->AddRect(ImVec2(rx0, ry0), ImVec2(rx1, ry1),
                             ui::Palette::toU32(hpal.accent), 1.5f);
            }
        }

        // ---- C25 W2-E: glass-box View Selector (Space). Drawn after the heads-up toolbar so its
        // orientation popup sits just below it; uses the same window draw list. While active it
        // suppresses normal click-picking (guard added on the picking click above).
        s_viewSelector.update(st, renderer, imgHovered, inSketch, vr0, vr1);
        s_viewSelector.draw(st, ImGui::GetWindowDrawList(), vr0, vr1);

        // ---- C25 W3-I: Section View tool. Pushes the clip plane to the renderer each frame and
        // draws the PM panel + in-viewport plane/arrow manipulator. Auto-suspends while sketching.
        s_sectionTool.updateAndDraw(st, renderer, imgHovered, inSketch, vr0, vr1);

        // ---- overlays on the 3D view ----
        ImDrawList* vdl = ImGui::GetWindowDrawList();
        {
            // Cache overlay colors as ImU32 from the palette for this frame.
            const auto& vpal = ui::palette;
            const ImU32 overlayBg   = ui::Palette::toU32(vpal.overlayBg);
            const ImU32 overlayBord = ui::Palette::toU32(vpal.overlayBorder);
            const ImU32 overlayTxt  = ui::Palette::toU32(vpal.overlayText);

        if (busy) {
            // live progress pill: pulsing dot + the agent's current phase
            std::string lt = st.livePhase.empty() ? std::string("Working...") : st.livePhase;
            ImVec2 ts = ImGui::CalcTextSize(lt.c_str());
            float maxW = (vr1.x - vr0.x) - 58.0f;
            ImVec2 p0(vr0.x + 12, vr0.y + 12);
            ImVec2 p1(p0.x + (ts.x > maxW ? maxW : ts.x) + 36.0f, p0.y + ts.y + 12.0f);
            vdl->AddRectFilled(p0, p1, overlayBg, (p1.y - p0.y) * 0.5f);
            vdl->AddRect(p0, p1, overlayBord, (p1.y - p0.y) * 0.5f);
            float pulse = 0.35f + 0.65f * (0.5f + 0.5f * std::sin((float)ImGui::GetTime() * 5.0f));
            vdl->AddCircleFilled(ImVec2(p0.x + 14 * ui::uiScale, (p0.y + p1.y) * 0.5f), 4.0f * ui::uiScale,
                                 ImGui::ColorConvertFloat4ToU32(
                                     ImVec4(vpal.accent[0], vpal.accent[1],
                                            vpal.accent[2], pulse)));
            vdl->PushClipRect(p0, ImVec2(p1.x - 8 * ui::uiScale, p1.y), true);
            vdl->AddText(ImVec2(p0.x + 24 * ui::uiScale, p0.y + 6 * ui::uiScale), overlayTxt, lt.c_str());
            vdl->PopClipRect();
        }
        if (st.hasDocMesh && !st.pickStatus.empty()) {
            // pick readout, top-right ("Face 12 of Body1")
            ImVec2 ts = ImGui::CalcTextSize(st.pickStatus.c_str());
            ImVec2 p1(vr1.x - 12, vr0.y + 12);
            ImVec2 p0(p1.x - ts.x - 20.0f, p1.y);
            ImVec2 p1b(p1.x, p1.y + ts.y + 10.0f);
            vdl->AddRectFilled(p0, p1b, overlayBg, 6.0f * ui::uiScale);
            vdl->AddRect(p0, p1b, overlayBord, 6.0f * ui::uiScale);
            vdl->AddText(ImVec2(p0.x + 10 * ui::uiScale, p0.y + 5 * ui::uiScale), overlayTxt,
                         st.pickStatus.c_str());
        }

        // ---- Instant3D push-pull handle + live readout ----
        // Drawn whenever exactly one face is selected (with a cached normal) and no sketch/PM/
        // plane-pick owns the view. The handle is a bidirectional accent arrow along the face
        // outward normal; press it to drag-modify the face. While dragging, a readout chip near the
        // cursor shows the driven param + new value (and a "clamped" hint at the limit).
        {
            bool ppPmOpen2 = st.propertyManager && st.propertyManager->active();
            bool canShowHandle = st.hasDocMesh && !inSketch && !st.sketchPlanePickActive &&
                                 !ppPmOpen2 && !s_viewSelector.active();
            if (canShowHandle) {
                ImVec2 cSp, tipP, tipN; float nsx, nsy, wpp; uint32_t bId, tId;
                if (ppHandleGeometry(st, renderer, vr0, vr1, vw, vh, cSp, tipP, tipN,
                                     nsx, nsy, wpp, bId, tId)) {
                    const auto& vpal2 = ui::palette;
                    const ImU32 acc  = ui::Palette::toU32(vpal2.accent);
                    const ImU32 accS = ui::Palette::toU32(vpal2.accentSoft);
                    float sc = ui::uiScale;
                    bool dragging = s_pp.active;
                    float lw = (dragging ? 3.0f : 2.2f) * sc;
                    // Shaft (centroid -> +normal tip, and centroid -> -normal tip).
                    vdl->AddLine(cSp, tipP, acc, lw);
                    vdl->AddLine(cSp, tipN, acc, lw);
                    // Arrowheads at both tips.
                    auto arrowHead = [&](const ImVec2& tip, float dx, float dy) {
                        float hl = 9.0f * sc, hw = 5.0f * sc;
                        // back along the shaft from the tip, then +/- perpendicular.
                        ImVec2 base(tip.x - dx * hl, tip.y - dy * hl);
                        ImVec2 l(base.x - (-dy) * hw, base.y - (dx) * hw);
                        ImVec2 r(base.x + (-dy) * hw, base.y + (dx) * hw);
                        vdl->AddTriangleFilled(tip, l, r, acc);
                    };
                    arrowHead(tipP,  nsx,  nsy);
                    arrowHead(tipN, -nsx, -nsy);
                    // Hub at the centroid.
                    vdl->AddCircleFilled(cSp, (dragging ? 4.0f : 3.0f) * sc, acc);
                    vdl->AddCircle(cSp, (dragging ? 7.0f : 5.0f) * sc, accS, 0, 1.5f * sc);

                    // Live readout chip near the cursor while dragging.
                    if (dragging && s_pp.haveReadout) {
                        // Title-case the param for display (depth -> "Depth").
                        std::string label = s_pp.param.empty() ? std::string("Value") : s_pp.param;
                        if (!label.empty()) label[0] = (char)std::toupper((unsigned char)label[0]);
                        // Format the (mm) drag distance in the active document units (mm / inch).
                        std::string txt = label + " " + ui::pmFormatDim(s_pp.newValue, st.ips());
                        if (s_pp.clamped) txt += "  (min)";
                        ImVec2 ts2 = ImGui::CalcTextSize(txt.c_str());
                        float padX = 8.0f * sc, padY = 4.0f * sc;
                        ImVec2 q0(io.MousePos.x + 16.0f * sc, io.MousePos.y + 16.0f * sc);
                        ImVec2 q1(q0.x + ts2.x + padX * 2, q0.y + ts2.y + padY * 2);
                        // keep inside the viewport rect
                        if (q1.x > vr1.x - 4) { float w = q1.x - q0.x; q1.x = vr1.x - 4; q0.x = q1.x - w; }
                        if (q1.y > vr1.y - 4) { float h = q1.y - q0.y; q1.y = vr1.y - 4; q0.y = q1.y - h; }
                        vdl->AddRectFilled(q0, q1, overlayBg, 4.0f * sc);
                        ImU32 chipBord = s_pp.clamped
                            ? ImGui::ColorConvertFloat4ToU32(ImVec4(0.90f, 0.55f, 0.25f, 1.0f))
                            : acc;
                        vdl->AddRect(q0, q1, chipBord, 4.0f * sc);
                        vdl->AddText(ImVec2(q0.x + padX, q0.y + padY), overlayTxt, txt.c_str());
                    } else if (!dragging) {
                        // Hint tooltip when hovering the handle (not dragging).
                        ImVec2 mp = io.MousePos;
                        float dxh = mp.x - cSp.x, dyh = mp.y - cSp.y;
                        float perp  = std::fabs(dxh * (-nsy) + dyh * nsx);
                        float along = std::fabs(dxh * nsx + dyh * nsy);
                        float armLenPx = 46.0f * ui::uiScale;
                        if (perp <= 10.0f * sc && along <= armLenPx + 10.0f * sc)
                            ImGui::SetTooltip("Drag to push/pull this face");
                    }
                }
            }
        }

        // ---- Instant3D dimension handles + live readout ----
        // Drawn whenever a FEATURE is selected and NO single face is (that face's domain is the
        // push-pull arrow above — clear precedence: the face arrow wins). Each draggable dim renders
        // a small accent handle (a value chip on a short bidirectional arrow) anchored near the
        // feature's geometry; press it to drag-edit that param. While dragging, the dragged chip
        // shows "Depth 25.0 -> 31.5 mm" in the active units, and a "(min)" hint at the clamp.
        {
            bool dimPmOpen = st.propertyManager && st.propertyManager->active();
            bool singleFaceSel =
                st.selection.size() == 1 &&
                st.selection.items().front().hit.valid() &&
                st.selection.items().front().hit.kind == render::PickKind::Face;
            bool canShowDim = st.hasDocMesh && !inSketch && !st.sketchPlanePickActive &&
                              !dimPmOpen && !s_viewSelector.active() && !s_pp.active &&
                              !singleFaceSel && !st.selectedFeatureId.empty();
            if (canShowDim) {
                auto cands = dimCandidates(st);
                const auto& vpal2 = ui::palette;
                const ImU32 acc  = ui::Palette::toU32(vpal2.accent);
                const ImU32 accS = ui::Palette::toU32(vpal2.accentSoft);
                float sc = ui::uiScale;
                ImVec2 mp = io.MousePos;
                for (const auto& c : cands) {
                    ImVec2 aSp, tipP, tipN; float dsx, dsy, wpp, dpp;
                    if (!dimHandleGeometry(st, renderer, vr0, vr1, vw, vh, c, aSp, tipP, tipN,
                                           dsx, dsy, wpp, dpp))
                        continue;
                    bool isDragged = s_dim.active && s_dim.featureId == st.selectedFeatureId &&
                                     s_dim.param == c.param;
                    // While THIS dim is being dragged, suppress the static chips for the OTHER dims so
                    // the view stays clean; the dragged dim keeps its handle + a live readout.
                    if (s_dim.active && !isDragged) continue;

                    float lw = (isDragged ? 3.0f : 2.0f) * sc;
                    vdl->AddLine(aSp, tipP, acc, lw);
                    vdl->AddLine(aSp, tipN, acc, lw);
                    auto arrowHead = [&](const ImVec2& tip, float dx, float dy) {
                        float hl = 8.0f * sc, hw = 4.5f * sc;
                        ImVec2 base(tip.x - dx * hl, tip.y - dy * hl);
                        ImVec2 l(base.x - (-dy) * hw, base.y - (dx) * hw);
                        ImVec2 r(base.x + (-dy) * hw, base.y + (dx) * hw);
                        vdl->AddTriangleFilled(tip, l, r, acc);
                    };
                    arrowHead(tipP,  dsx,  dsy);
                    arrowHead(tipN, -dsx, -dsy);
                    vdl->AddCircleFilled(aSp, (isDragged ? 4.0f : 3.0f) * sc, acc);
                    vdl->AddCircle(aSp, (isDragged ? 7.0f : 5.0f) * sc, accS, 0, 1.5f * sc);

                    // Label/value chip. Static = "Depth: 25mm"; dragged = "Depth 25.0 -> 31.5 mm".
                    std::string label = c.label.empty() ? c.param : c.label;
                    std::string txt;
                    if (isDragged && s_dim.haveReadout) {
                        std::string oldS = c.angle ? (std::to_string((int)std::lround(s_dim.oldValue)) + "°")
                                                   : ui::pmFormatDim(s_dim.oldValue, st.ips());
                        std::string newS = c.angle ? (std::to_string((int)std::lround(s_dim.curValue)) + "°")
                                                   : ui::pmFormatDim(s_dim.curValue, st.ips());
                        txt = label + " " + oldS + " -> " + newS;
                        if (s_dim.clamped) txt += (c.angle ? "  (limit)" : "  (min)");
                    } else {
                        std::string vS = c.angle ? (std::to_string((int)std::lround(c.value)) + "°")
                                                 : ui::pmFormatDim(c.value, st.ips());
                        txt = label + ": " + vS;
                    }
                    ImVec2 ts2 = ImGui::CalcTextSize(txt.c_str());
                    float padX = 7.0f * sc, padY = 3.5f * sc;
                    // Static chip floats just past the +tip; the dragged chip follows the cursor.
                    ImVec2 q0 = isDragged
                        ? ImVec2(mp.x + 16.0f * sc, mp.y + 16.0f * sc)
                        : ImVec2(tipP.x + 8.0f * sc, tipP.y - (ts2.y + padY * 2) * 0.5f);
                    ImVec2 q1(q0.x + ts2.x + padX * 2, q0.y + ts2.y + padY * 2);
                    // keep inside the viewport rect
                    if (q1.x > vr1.x - 4) { float w = q1.x - q0.x; q1.x = vr1.x - 4; q0.x = q1.x - w; }
                    if (q0.x < vr0.x + 4) { float w = q1.x - q0.x; q0.x = vr0.x + 4; q1.x = q0.x + w; }
                    if (q1.y > vr1.y - 4) { float h = q1.y - q0.y; q1.y = vr1.y - 4; q0.y = q1.y - h; }
                    if (q0.y < vr0.y + 4) { float h = q1.y - q0.y; q0.y = vr0.y + 4; q1.y = q0.y + h; }
                    vdl->AddRectFilled(q0, q1, overlayBg, 4.0f * sc);
                    ImU32 chipBord = (isDragged && s_dim.clamped)
                        ? ImGui::ColorConvertFloat4ToU32(ImVec4(0.90f, 0.55f, 0.25f, 1.0f))
                        : acc;
                    vdl->AddRect(q0, q1, chipBord, 4.0f * sc);
                    vdl->AddText(ImVec2(q0.x + padX, q0.y + padY), overlayTxt, txt.c_str());

                    // Hover hint when not dragging and the cursor is over the handle.
                    if (!s_dim.active) {
                        float dxh = mp.x - aSp.x, dyh = mp.y - aSp.y;
                        float perp  = std::fabs(dxh * (-dsy) + dyh * dsx);
                        float along = std::fabs(dxh * dsx + dyh * dsy);
                        float armLenPx = 42.0f * ui::uiScale;
                        if (perp <= 9.0f * sc && along <= armLenPx + 9.0f * sc)
                            ImGui::SetTooltip(c.angle ? "Drag to change this angle"
                                                      : "Drag to change this dimension");
                    }
                }
            }
        }
        // C25 W3-J: apply ground-shadow visibility (last call wins; app.cpp may set it per-frame
        // before viewport_panel runs — the ordering means this value wins for the rendered frame,
        // which is the desired result since viewport_panel calls renderOrbit immediately before).
        // NOTE for orchestrator: if app.cpp pushes groundShadow AFTER viewport_panel in the frame
        // loop, the visibility toggle here would be overridden. Verify render call order.
        {
            const auto& vpal = ui::palette;
            float alpha = st.showGroundShadow ? vpal.groundShadow[3] : 0.0f;
            renderer.setGroundShadow(vpal.groundShadow[0], vpal.groundShadow[1],
                                     vpal.groundShadow[2], alpha);
        }
        // ---- C24 / C26-C: orientation triad, bottom-left of the viewport (SolidWorks-style) ----
        // C25 W3-J: gated by st.showTriad.
        // C26-C: each axis tip is a small invisible ImGui button. Clicking an axis goes to the
        // corresponding preset view; clicking the axis you're already looking down flips opposite.
        //   +X tip → Right  (yaw=90,  pitch=0)
        //   +Y tip → Front  (yaw=0,   pitch=0)  — "look from the +Y side" = Front in this basis
        //   +Z tip → Top    (yaw=0,   pitch=89.5)
        // Hover: accentSoft circle behind the label. Tooltip: view name.
        if (st.showTriad) {
            const float ts = ui::uiScale;
            float cx = vr0.x + 34.0f * ts, cy = vr1.y - 34.0f * ts;
            float L = 20.0f * ts;
            float yawR = st.yaw * 3.14159265f / 180.0f, pitR = st.pitch * 3.14159265f / 180.0f;
            float cyaw = std::cos(yawR), syaw = std::sin(yawR);
            float cpit = std::cos(pitR), spit = std::sin(pitR);
            auto proj = [&](float wx, float wy, float wz, float& sx, float& sy) {
                float rx = wx * cyaw + wy * syaw;
                float ry = -wx * syaw + wy * cyaw;
                sx = rx;
                sy = -(wz * cpit - ry * spit);
            };
            struct Ax {
                float x, y, z;
                const char* l;
                const float* col;
                ViewPreset preset;
                const char* viewName;
                // The yaw/pitch that represents "already looking at this axis" (approx check).
                float chkYaw, chkPitch;
            };
            const Ax axes[] = {
                {1, 0, 0, "X", ui::palette.axisX, ViewPreset::Right,  "Right",  90.0f,  0.0f},
                {0, 1, 0, "Y", ui::palette.axisY, ViewPreset::Front,  "Front",   0.0f,  0.0f},
                {0, 0, 1, "Z", ui::palette.axisZ, ViewPreset::Top,    "Top",     0.0f, 89.5f},
            };
            // Draw lines and label chips first (under buttons).
            for (const auto& a : axes) {
                float sx, sy; proj(a.x, a.y, a.z, sx, sy);
                ImVec2 tip(cx + sx * L, cy + sy * L);
                ImU32 col = ImGui::ColorConvertFloat4ToU32(
                    ImVec4(a.col[0], a.col[1], a.col[2], 0.95f));
                vdl->AddLine(ImVec2(cx, cy), tip, col, 1.8f);
            }
            vdl->AddCircleFilled(ImVec2(cx, cy), 2.4f * ts, overlayTxt);

            // Draw buttons + labels on top.
            ImGui::PushID("##triad");
            const float hitR = 8.0f * ts;  // invisible button half-size
            for (int ai = 0; ai < 3; ++ai) {
                const auto& a = axes[ai];
                float sx, sy; proj(a.x, a.y, a.z, sx, sy);
                ImVec2 tip(cx + sx * L, cy + sy * L);

                // Invisible button centered on the axis tip.
                ImGui::SetCursorScreenPos(ImVec2(tip.x - hitR, tip.y - hitR));
                ImGui::PushID(ai);
                bool clicked = ImGui::InvisibleButton("##tb", ImVec2(hitR * 2, hitR * 2));
                bool hov = ImGui::IsItemHovered();
                ImGui::PopID();

                if (hov) {
                    // Hover: accentSoft circle highlight.
                    vdl->AddCircleFilled(tip, hitR,
                        ui::Palette::toU32(ui::palette.accentSoft));
                    ImGui::SetTooltip("%s view", a.viewName);
                }
                if (clicked) {
                    // Check if already looking from this axis (within 5° tolerance).
                    float dy2  = std::fabs(std::fmod(st.yaw - a.chkYaw + 540.0f, 360.0f) - 180.0f);
                    float dp2  = std::fabs(st.pitch - a.chkPitch);
                    bool already = (dy2 < 5.0f && dp2 < 5.0f);
                    if (already) {
                        // Flip to opposite view.
                        static const float kZeroPan[3] = {0,0,0};
                        requestCameraAnimation(st, a.chkYaw + 180.0f,
                                               -a.chkPitch, -1.0f, kZeroPan);
                    } else {
                        applyViewPreset(st, a.preset);
                    }
                }

                // Label chip.
                ImU32 col = ImGui::ColorConvertFloat4ToU32(
                    ImVec4(a.col[0], a.col[1], a.col[2], 0.95f));
                ImVec2 lp(tip.x - 3, tip.y - 7);
                ImVec2 lts = ImGui::CalcTextSize(a.l);
                vdl->AddRectFilled(ImVec2(lp.x - 2 * ts, lp.y - 1 * ts),
                                   ImVec2(lp.x + lts.x + 2 * ts, lp.y + lts.y + 1 * ts), overlayBg, 2.0f * ts);
                vdl->AddText(lp, col, a.l);
            }
            ImGui::PopID();
        }
        // C27-A: hide the model-info dims pill while a PM form is active (callouts own the
        // viewport's parameter readout; avoids visual clutter / overlap).
        bool pmActive = st.propertyManager && st.propertyManager->active();
        if (!st.modelInfo.empty() && renderer.hasMesh() && !pmActive) {
            // dimensions readout, bottom-left (sits above the orientation triad)
            ImFont* mf = st.fonts.mono ? st.fonts.mono : ImGui::GetFont();
            float fs = mf->FontSize * 0.92f;
            ImVec2 ts = mf->CalcTextSizeA(fs, 1e30f, 0.0f, st.modelInfo.c_str());
            ImVec2 p0(vr0.x + 12, vr1.y - ts.y - 70.0f);
            ImVec2 p1(p0.x + ts.x + 20.0f, p0.y + ts.y + 10.0f);
            vdl->AddRectFilled(p0, p1, overlayBg, 6.0f * ui::uiScale);
            vdl->AddText(mf, fs, ImVec2(p0.x + 10 * ui::uiScale, p0.y + 5 * ui::uiScale),
                         overlayTxt, st.modelInfo.c_str());
        }
        // ---- reference-plane name chips (projected to screen) ----
        // C25: gate on effPlane (same effective visibility as the quads) so labels never
        // float without their planes (the empty-doc bug).
        if (!inSketch && (effPlane[0] || effPlane[1] || effPlane[2])) {
            const float* m = renderer.camera().mvp.m;  // column-major model->clip
            auto project = [&](const Vec3& w, ImVec2& out) -> bool {
                float x = m[0]*w.x + m[4]*w.y + m[8]*w.z  + m[12];
                float y = m[1]*w.x + m[5]*w.y + m[9]*w.z  + m[13];
                float wc= m[3]*w.x + m[7]*w.y + m[11]*w.z + m[15];
                if (wc <= 1e-4f) return false;  // behind the camera
                out.x = vr0.x + (x / wc * 0.5f + 0.5f) * (vr1.x - vr0.x);
                out.y = vr0.y + (1.0f - (y / wc * 0.5f + 0.5f)) * (vr1.y - vr0.y);
                return true;
            };
            static const char* kPlaneName[3] = {"Front Plane", "Top Plane", "Right Plane"};
            // W4-K: chip border tracks the datum-plane outline color (light-blue, SolidWorks look)
            // so the labels read as part of the unified overlay family instead of per-axis RGB.
            const ImU32 planeChipBord = ui::Palette::toU32(vpal.planeOutline);
            for (int p = 0; p < 3; ++p) {
                if (!effPlane[p]) continue;
                Vec3 a; if (!renderer.planeAnchor(p, a)) continue;
                ImVec2 sp; if (!project(a, sp)) continue;
                const char* nm = kPlaneName[p];
                ImVec2 ts = ImGui::CalcTextSize(nm);
                // keep the chip inside the viewport rect
                float x0 = sp.x, y0 = sp.y;
                if (x0 + ts.x + 12 > vr1.x) x0 = vr1.x - ts.x - 12;
                if (x0 < vr0.x + 2) x0 = vr0.x + 2;
                if (y0 + ts.y + 6 > vr1.y) y0 = vr1.y - ts.y - 6;
                if (y0 < vr0.y + 2) y0 = vr0.y + 2;
                ImVec2 q0(x0, y0), q1(x0 + ts.x + 12, y0 + ts.y + 6);
                // C43: during plane-pick the label chips are CLICK TARGETS too (the quads
                // overlap heavily near the origin; the labels are unambiguous).
                bool chipHovered = st.sketchPlanePickActive &&
                                   ImGui::IsMouseHoveringRect(q0, q1);
                vdl->AddRectFilled(q0, q1, overlayBg, 4.0f * ui::uiScale);
                vdl->AddRect(q0, q1, chipHovered ? ui::Palette::toU32(vpal.accent)
                                                 : planeChipBord, 4.0f * ui::uiScale, 0,
                             chipHovered ? 2.0f : 1.0f);
                vdl->AddText(ImVec2(x0 + 6 * ui::uiScale, y0 + 3 * ui::uiScale), overlayTxt, nm);
                if (chipHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left) &&
                    st.sketchMode) {
                    static const char* kPlaneSel[3] = {"plane.front", "plane.top", "plane.right"};
                    st.sketchPlanePickActive = false;
                    st.sketchMode->createSketchOnPlane(st, kPlaneSel[p]);
                }
            }
        }

        // ---- C27-A: SolidWorks-style in-viewport parameter callouts ----
        // While the PropertyManager is editing a feature, show a small chip ("Radius: 4mm")
        // per visible+enabled Dim field, tied by a leader line to the affected geometry.
        if (st.propertyManager && st.hasDocMesh) {
            auto* pm = st.propertyManager;
            if (pm->active()) {
                std::vector<ui::PMCalloutItem> items = pm->calloutItems(st);
                if (!items.empty()) {
                    const float* m = renderer.camera().mvp.m;  // column-major model->clip
                    auto project = [&](const float w[3], ImVec2& out) -> bool {
                        float x = m[0]*w[0] + m[4]*w[1] + m[8]*w[2]  + m[12];
                        float y = m[1]*w[0] + m[5]*w[1] + m[9]*w[2]  + m[13];
                        float wc= m[3]*w[0] + m[7]*w[1] + m[11]*w[2] + m[15];
                        if (wc <= 1e-4f) return false;  // behind the camera
                        out.x = vr0.x + (x / wc * 0.5f + 0.5f) * (vr1.x - vr0.x);
                        out.y = vr0.y + (1.0f - (y / wc * 0.5f + 0.5f)) * (vr1.y - vr0.y);
                        return true;
                    };
                    const float sc = ui::uiScale;
                    const ImU32 leaderCol = ImGui::ColorConvertFloat4ToU32(
                        ImVec4(vpal.accent[0], vpal.accent[1], vpal.accent[2], 0.8f));
                    const ImU32 chipBord = ui::Palette::toU32(vpal.accent);
                    // Limit to ~3 chips; stagger vertically so anchored chips don't overlap.
                    int shown = 0, anchoredRow = 0, dockRow = 0;
                    for (const auto& ci : items) {
                        if (shown >= 3) break;
                        std::string txt = ci.label + ": " + ci.value;
                        ImVec2 ts = ImGui::CalcTextSize(txt.c_str());
                        float padX = 8.0f * sc, padY = 4.0f * sc;
                        float cw = ts.x + padX * 2, ch = ts.y + padY * 2;

                        ImVec2 chipTL;
                        bool drawLeader = false;
                        ImVec2 anchorSp;
                        if (ci.hasAnchor && project(ci.anchor, anchorSp)) {
                            // Offset the chip up-right from the anchor; stagger by row.
                            float off = 40.0f * sc;
                            chipTL = ImVec2(anchorSp.x + off,
                                            anchorSp.y - off - anchoredRow * (ch + 6.0f * sc));
                            drawLeader = true;
                            ++anchoredRow;
                        } else {
                            // No geometry: dock top-right, under the HUD pick readout.
                            chipTL = ImVec2(vr1.x - cw - 12.0f * sc,
                                            vr0.y + 44.0f * sc + dockRow * (ch + 6.0f * sc));
                            ++dockRow;
                        }
                        // Clamp the chip inside the viewport rect.
                        if (chipTL.x + cw > vr1.x - 4) chipTL.x = vr1.x - cw - 4;
                        if (chipTL.x < vr0.x + 4)       chipTL.x = vr0.x + 4;
                        if (chipTL.y + ch > vr1.y - 4) chipTL.y = vr1.y - ch - 4;
                        if (chipTL.y < vr0.y + 4)       chipTL.y = vr0.y + 4;
                        ImVec2 chipBR(chipTL.x + cw, chipTL.y + ch);

                        if (drawLeader) {
                            // Leader from the anchor to the nearest chip corner-ish point.
                            ImVec2 leaderEnd(chipTL.x, chipTL.y + ch * 0.5f);
                            vdl->AddLine(anchorSp, leaderEnd, leaderCol, 1.5f * sc);
                            vdl->AddCircleFilled(anchorSp, 2.6f * sc, leaderCol);
                        }
                        vdl->AddRectFilled(chipTL, chipBR, overlayBg, 4.0f * sc);
                        vdl->AddRect(chipTL, chipBR, chipBord, 4.0f * sc);
                        vdl->AddText(ImVec2(chipTL.x + padX, chipTL.y + padY), overlayTxt,
                                     txt.c_str());
                        ++shown;
                    }
                }
            }
        }

        // ---- C19: result colormap legend (bottom-right) ----
        ui::drawLegend(st, vdl, vr0, vr1);
        }  // end overlay colors scope
    }
    ImGui::End();
    }
}

}  // namespace ui
