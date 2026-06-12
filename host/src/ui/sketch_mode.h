// Sketch mode UI (C18) — the SolidWorks-style in-sketch experience.
//
// While a sketch is "entered" (tree "Edit Sketch" or ribbon Sketch tab "New Sketch"), SketchMode
// owns the interaction: it locks the camera normal to the sketch plane, draws a faint grid and the
// solved entities as a screen-space ImGui draw-list overlay (projected via Renderer::CameraXform —
// NO new GL paths), runs the drawing tools (line / corner rectangle / circle / center arc / slot)
// with auto-relation snapping, Smart Dimension, and constraint-exact drag (worker sketch_drag /
// sketch_drag_commit). Entity color follows the per-entity solver status via theme roles.
//
// All worker traffic goes through the generic WorkerClient::call using the DocMirror's doc id +
// revision; commits/exits return ops-responses that AppState::loadDocMesh consumes for the rebuilt
// mesh. No GL headers here (kept dock-light, like PropertyManager).
#pragma once

#include <string>
#include <unordered_map>
#include <vector>

#include <nlohmann/json.hpp>

#include "imgui.h"

struct AppState;
class Renderer;

namespace ui {

// The active drawing tool. None = selection/drag mode.
enum class SketchTool {
    None, Line, RectCorner, Circle, ArcCenter, Slot, SmartDim,
    // C28-B flyout variants (map clicks to existing entity types unless noted):
    Rect3ptCorner, Rect3ptCenter, Parallelogram,   // -> 4 lines
    CirclePerimeter,                                // -> 1 circle (circumcircle of 3 pts)
    SlotCtrStr,                                     // -> 1 slot (centerpoint input)
    SlotArcCtr, SlotArc3pt,                          // -> 1 arc_slot (C30-B; arc-spine capsule)
    // Conic family (engine contract C28-A: ellipse_arc / parabola / conic entities):
    EllipsePartial, Parabola, Conic,
    // C29-B sketch-editing tools (engine contract C29-A: sketch_trim/extend/offset ops):
    Trim, Extend, Offset
};

// C37-D: One constraint record from a sketch_enter/solve/drag response (worker_rpc.md §"constraints").
// Drives the always-on dimension annotations + relation glyphs.
struct SketchConstraint {
    std::string id;
    std::string type;                 // distance|angle|radius|diameter|coincident|horizontal|...
    std::vector<std::string> refs;    // handles ("e1.p2") or entity ids ("e1")
    bool hasValue = false;            // value present (stored dim)
    double value = 0.0;
    bool hasExpr = false;
    std::string expr;
    bool driven = false;
    bool hasPlacement = false;
    float placement[2] = {0, 0};      // [u,v] annotation anchor (sketch coords)
    bool hasMeasured = false;
    double measured = 0.0;            // solved measurement (mm/deg)
    std::string axis;                 // distance only: "h"|"v"|"" (aligned)
    bool isDimensional() const {
        return type == "distance" || type == "angle" || type == "radius" || type == "diameter";
    }
};

// One solved entity in the active sketch (mirrors a worker sketch_enter/solve entity record).
struct SketchEntity {
    std::string id;
    std::string type;            // line|circle|arc|slot|arc_slot|point|ellipse|spline
    std::string status;          // under|full|over (from the solver)
    bool construction = false;
    // Coordinate payload (sketch-plane u,v). Only the fields for `type` are populated.
    float p1[2] = {0, 0}, p2[2] = {0, 0};   // line/slot endpoints
    float center[2] = {0, 0};               // circle/arc/ellipse/arc_slot
    float radius = 0;
    float start = 0, end = 0;               // arc angles (deg); arc_slot reuses these for a0/a1
    bool ccw = true;
    float width = 0;                        // slot/arc_slot
};

class SketchMode {
public:
    bool active() const { return active_; }
    const std::string& sketchId() const { return fid_; }

    // Enter/exit. enterForFeature opens an existing sketch feature (tree "Edit Sketch");
    // enterNewOnSelection creates a new sketch on the selected planar face/datum (or plane.top)
    // and enters it (ribbon "New Sketch"). Both lock the camera to the plane.
    bool enterForFeature(AppState& st, const std::string& sketchFid);
    bool enterNewOnSelection(AppState& st);
    // C37-D: create + enter a new sketch on an EXPLICIT plane ref (datum string "plane.top" or a
    // resolved face/datum selector JSON). enterNewOnSelection() and the plane-pick flow both route
    // here; the scripted scenarios in app.cpp keep using enterForFeature/enterNewOnSelection
    // unchanged. Returns the new feature id (empty on failure) so callers can re-enter it.
    std::string createSketchOnPlane(AppState& st, const nlohmann::json& planeRef);
    void exitSketch(AppState& st, bool keep = true);   // keep=false also deletes an empty new sketch

    // Ribbon Sketch-tab command dispatch (sk_line / sk_rect_corner / sk_circle / sk_arc_center /
    // sk_slot / smart_dim / sketch_exit / sketch_new). Returns true if handled.
    bool onRibbonCommand(AppState& st, const std::string& cmdId);

    // Draw the in-viewport overlay (grid + entities + tool rubber-band + exit chip + status) and
    // process mouse/keyboard for the active tool / drag. Called from the Viewport panel AFTER the
    // ImGui::Image, with the image rect + size and whether the image is hovered.
    void drawOverlay(AppState& st, ImGuiIO& io, const ImVec2& imgMin, const ImVec2& imgMax,
                     int vw, int vh, bool imgHovered);

    // Short status line for the status bar ("Under Defined   DOF: 4"). Empty when inactive.
    std::string statusLine() const;

    // The plane-normal camera angles (so the caller can hard-lock the orbit while active).
    bool cameraLock(float& yaw, float& pitch) const;

private:
    // ---- worker round-trips ----
    bool callSketch(AppState& st, const char* method, const nlohmann::json& extra,
                    nlohmann::json& result);
    void applyEnterResult(const nlohmann::json& res);
    void resolve(AppState& st);                 // sketch_solve -> refresh entities/statuses
    void commitDrag(AppState& st);              // sketch_drag_commit -> rebuild mesh
    void addEntity(AppState& st, const nlohmann::json& entity, const nlohmann::json& constraints);
    void addConstraint(AppState& st, const nlohmann::json& constraint);
    // C37-D: store the constraints array carried on every sketch response.
    void applyConstraints(const nlohmann::json& res);

    // C37-D: in-sketch undo (mid-stroke point pop, else doc undo + resolve). Returns false when the
    // sketch vanished from the undo (caller exits gracefully).
    void inSketchUndo(AppState& st);
    // C37-D: confirmation-corner ✗ — revert the whole sketch session via doc_revert(checkpoint),
    // then drop out of sketch mode cleanly (falls back to a plain keep-exit if no checkpoint).
    void cancelSketch(AppState& st);

    // C37-D: Smart Dimension state machine helpers.
    // A pick target: either an entity endpoint/center handle ("e3.p2") or a whole entity id ("e3").
    struct DimPick { std::string handle; int entIdx = -1; bool isPoint = false; float u = 0, v = 0; };
    bool dimHitTest(const ImVec2& s, DimPick& out) const;       // point-priority then entity
    bool dimBuildPending(const ImVec2& cursor, float cu, float cv);  // picks -> dimPending_ (+axis)
    void dimOpenModify(const ImVec2& at);                       // open Modify box for dimPending_
    void dimCommit(AppState& st);                               // apply the Modify box value/expr
    // Render one dimension annotation (extension/dim lines, arrows, text). placement in (u,v).
    void drawDimension(ImDrawList* dl, const SketchConstraint& c, ImU32 lineCol, ImU32 textCol,
                       bool hovered, float pu, float pv) const;
    // Compute a sensible default placement (u,v) for a constraint that has none yet.
    bool defaultPlacement(const SketchConstraint& c, float& pu, float& pv) const;
    // Screen location of a dimension's text (for hover / drag / double-click hit-testing).
    bool dimTextAnchor(const SketchConstraint& c, ImVec2& out) const;
    const SketchEntity* entById(const std::string& id) const;

    // ---- projection (camera locked fronto-parallel to the plane) ----
    // Build the affine screen<->(u,v) basis for this frame from three projected plane points.
    void buildBasis(const Renderer& r, const ImVec2& imgMin, int vw, int vh);
    ImVec2 uvToScreen(float u, float v) const;
    void screenToUV(const ImVec2& s, float& u, float& v) const;

    // ---- tool helpers ----
    void resetTool();
    void cancelTool();
    // Snap a free (u,v) to nearby entity endpoints (coincident) + axis directions (h/v); fills the
    // inferred-constraint list for the next sketch_add_entity. *snapHandle set to the snapped-onto
    // handle (e.g. "e3.p2") when an endpoint snap fired.
    void snapPoint(float& u, float& v, std::string& snapHandle) const;
    int hitTestHandle(const ImVec2& s, std::string& handleOut, float& uOut, float& vOut) const;
    // Nearest WHOLE entity under the screen point (line segment / circle rim / arc rim distance);
    // returns the index into ents_ or -1. Used by Trim / Extend / Offset hover + pick (C29-B).
    int hitTestEntity(const ImVec2& s, float maxPx = 12.0f) const;

    bool active_ = false;
    bool isNewEmpty_ = false;        // entered a freshly-created sketch (delete on cancel-exit)
    std::string fid_;
    int dof_ = 0;
    std::string status_;             // under|full|over
    float frame_[16] = {1,0,0,0, 0,1,0,0, 0,0,1,0, 0,0,0,1};  // sketch(u,v,0)->world, row-major
    float camYaw_ = 0, camPitch_ = 89;
    // C34: pre-sketch camera, restored (animated) on exit.
    bool  havePrevCam_ = false;
    float prevYaw_ = 35, prevPitch_ = 30, prevZoom_ = 1;
    float prevPan_[3] = {0, 0, 0};

    std::vector<SketchEntity> ents_;
    std::vector<SketchConstraint> cons_;   // C37-D: every constraint (drives dim annotations)
    // C37-D: locally-remembered placements for dims whose engine placement is null, so a freshly
    // auto-placed dim keeps its spot frame-to-frame until the user drags it (which persists it).
    std::unordered_map<std::string, ImVec2> autoPlace_;

    // C37-D: cancel checkpoint taken at sketch entry; doc_revert on the confirmation-corner ✗.
    std::string cancelCheckpoint_;

    // C37-D: plane-pick (SolidWorks "select a plane to start") state. Driven by drawOverlay while
    // st.sketchPlanePickActive; -1 = no origin-plane hovered, else 0/1/2 = front/top/right.
    bool planePicking_ = false;
    int  planeHover_ = -1;

    // per-frame projection basis
    ImVec2 originS_{0, 0};
    ImVec2 uAxisS_{1, 0};            // screen delta for +1 unit of u
    ImVec2 vAxisS_{0, 1};            // screen delta for +1 unit of v
    float det_ = 1.0f;
    bool basisOk_ = false;

    // active tool state
    SketchTool tool_ = SketchTool::None;
    std::vector<ImVec2> toolUV_;     // in-progress click points in (u,v)
    std::vector<std::string> toolSnaps_;  // snapped-onto handle per click (for coincident)
    // C37-D: id of the previous chained Line segment, so the next segment's p1 gets a coincident to
    // its p2 (the chain start handle is otherwise dropped). Cleared by resetTool via cancelTool.
    std::string chainPrevId_;
    float conicRho_ = 0.5f;          // Conic tool: live shoulder weight 0..1 during the rho pick

    // Emit the 4 lines of a quad (corners A,B,C,D in order) as one chained loop with the given
    // relation kinds (mirrors the corner-rect op pattern; relations[i] applies to edge i->i+1,
    // "" = none). Used by the 3pt/center/parallelogram rectangle variants.
    void addQuadLoop(AppState& st, const ImVec2 corners[4], const char* relations[4]);

    // drag state
    std::string dragHandle_;
    bool dragging_ = false;
    int dragFrame_ = -1;             // last frame a sketch_drag was issued (throttle: once/frame)

    // C29-B Offset tool state. Stages: collect a chain of entity ids (selected ones tint accent),
    // then a distance overlay (InputFloat near cursor, Enter commits), then one click for the side
    // hint -> sketch_offset op. offsetStage_: 0=picking chain, 1=typing distance, 2=picking side.
    std::vector<std::string> offsetChain_;
    int offsetStage_ = 0;
    float offsetDist_ = 10.0f;
    ImVec2 offsetOverlayPos_{0, 0};
    bool offsetFocusInput_ = false;   // request keyboard focus on the distance field next frame

    // ---- C37-D Smart Dimension state machine ----
    // Pick buffer (separate from toolUV_): points/entities collected for the next dimension.
    std::vector<DimPick> dimPicks_;
    // The constraint object being built (refs/type/axis filled by dimBuildPending), awaiting a
    // placement click. dimHasPending_ gates the live preview.
    bool dimHasPending_ = false;
    nlohmann::json dimPending_;
    std::string dimType_;             // "distance"|"angle"|"radius"|"diameter"
    std::string dimAxis_;             // distance variant chosen at placement: "h"|"v"|""
    double dimMeasured_ = 0.0;        // current measured value (for preview text + box prefill)
    float dimPlaceU_ = 0, dimPlaceV_ = 0;   // committed placement (set on the placement click)

    // Modify box (replaces the old auto-popup): auto-focused, pre-selected, +/- step, Driven, units.
    bool dimPopupOpen_ = false;
    std::vector<char> dimBuf_{std::vector<char>(64, 0)};
    std::string dimError_;
    ImVec2 dimPopupPos_{0, 0};
    bool dimPopupFocus_ = false;      // request keyboard focus + select-all next frame
    bool dimPopupDriven_ = false;     // Driven checkbox state
    double dimPopupStep_ = 1.0;       // +/- step (1mm distance/radius/diameter, 1deg angle)
    bool dimIsAngle_ = false;         // suffix + step selection
    std::string dimEditCid_;          // non-empty: editing an EXISTING dim (double-click) via set_dim

    // ---- C37-D dimension hover / text-drag ----
    std::string dimHoverCid_;         // constraint id hovered this frame
    std::string dimDragCid_;          // constraint id whose text is being dragged
    bool dimDragging_ = false;
    ImVec2 dimDragPlace_{0, 0};       // transient placement (u,v) while dragging
};

}  // namespace ui
