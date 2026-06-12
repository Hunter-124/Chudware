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

    // smart-dimension popup state
    bool dimPopupOpen_ = false;
    std::vector<char> dimBuf_{std::vector<char>(64, 0)};
    std::string dimError_;
    nlohmann::json dimPending_;       // the constraint object awaiting a value/expr
    ImVec2 dimPopupPos_{0, 0};
};

}  // namespace ui
