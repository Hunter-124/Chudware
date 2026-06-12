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
    Trim, Extend, Offset,
    // C42: remaining draw tools (3-pt arc, polygon, center rect, point, ellipse, spline):
    Arc3pt, Polygon, RectCenter, Point, Ellipse, Spline
};

// C37-D: One constraint record from a sketch_enter/solve/drag response (worker_rpc.md §"constraints").
// Drives the always-on dimension annotations + relation glyphs.
struct SketchConstraint {
    std::string id;
    std::string type;                 // distance|angle|radius|diameter|coincident|horizontal|...
    std::vector<std::string> refs;    // handles ("e1.p2") or entity ids ("e1")
    // distance only: per-ref arc condition ("center"|"min"|"max"), aligned with refs. Only
    // meaningful where the ref is a bare circle/arc id; empty = all center (legacy).
    std::vector<std::string> cond;
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
    float p1[2] = {0, 0}, p2[2] = {0, 0};   // line/slot endpoints; point reuses p1 for "p"
    float center[2] = {0, 0};               // circle/arc/ellipse/arc_slot
    float radius = 0;
    float start = 0, end = 0;               // arc angles (deg); arc_slot reuses these for a0/a1
    bool ccw = true;
    float width = 0;                        // slot/arc_slot
    float rx = 0, ry = 0, rot = 0;          // ellipse (rot in degrees)
    std::vector<ImVec2> pts;                // spline solved through-points
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

    // C40: ghost-render every UNCONSUMED, non-active sketch as flat curves in the viewport
    // (SolidWorks shows inactive sketches in grey). Cached per (doc id, revision); entity
    // snapshots are fetched via the async doc_sketch_overview RPC, so frames never block.
    // Called from the Viewport panel after the ImGui::Image in ALL modes (the active sketch,
    // if any, is skipped — drawOverlay owns it).
    void drawGhosts(AppState& st, const ImVec2& imgMin, const ImVec2& imgMax, int vw, int vh);

    // Short status line for the status bar ("Under Defined   DOF: 4"). Empty when inactive.
    std::string statusLine() const;

    // C40: ribbon cmd id of the armed tool ("" when none) — drives the ribbon's armed-tool
    // highlight (ui::setRibbonActiveCmd each frame from app.cpp).
    const char* activeToolCmd() const;

    // The plane-normal camera angles (so the caller can hard-lock the orbit while active).
    bool cameraLock(float& yaw, float& pitch) const;

    // Origin-plane index (0/1/2, -1 none) hovered during the plane-pick step. The viewport feeds
    // this to Renderer::setPlanes as the emphasis so the hovered datum plane brightens and the
    // other two dim (single source of plane visuals — the overlay no longer paints its own fills).
    int planeHover() const { return planePicking_ ? planeHover_ : -1; }

    // Smart Dimension may reference geometry that is not (yet) a sketch entity. Both paths project
    // the source into the sketch as a FIXED construction entity (worker sketch_project_ref) and add
    // it to the pick buffer, so the normal dimension combos apply.
    // True when the Smart Dimension tool is armed and collecting picks (feature_tree uses this to
    // route a datum-plane click here instead of toggling plane visibility).
    bool smartDimPicking() const;
    // Datum plane (0=front 1=top 2=right) clicked in the tree while Smart Dimension is armed:
    // project its intersection line with the sketch plane and add it as a dimension pick.
    void addPlaneDimRef(AppState& st, int planeIdx);

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
    // C50: in-sketch redo (Ctrl+Y / Ctrl+Shift+Z). doc redo + re-solve; closes gracefully if the
    // sketch vanished. Shares the post-undo/redo refresh path with inSketchUndo.
    void inSketchRedo(AppState& st);
    void afterSketchHistory(AppState& st);   // shared: re-solve or close after a doc undo/redo
    // C37-D: confirmation-corner ✗ — revert the whole sketch session via doc_revert(checkpoint),
    // then drop out of sketch mode cleanly (falls back to a plain keep-exit if no checkpoint).
    void cancelSketch(AppState& st);

    // C37-D: Smart Dimension state machine helpers.
    // A pick target: either an entity endpoint/center handle ("e3.p2") or a whole entity id ("e3").
    // C38 (S1): store the entity id STRING (entId) — ents_ is refilled while picks are held, so an
    // index into ents_ goes stale. entIdx is a per-frame convenience resolved from entId via
    // entIndexById(); -1 means the entity is currently absent (skip).
    struct DimPick { std::string handle; std::string entId; int entIdx = -1; bool isPoint = false; float u = 0, v = 0;
                     std::string cond; };   // circle/arc entity picks: "center"|"min"|"max" anchor
    int entIndexById(const std::string& id) const;   // C38 (S1): id -> current ents_ index, or -1
    bool dimHitTest(const ImVec2& s, DimPick& out) const;       // point-priority then entity
    // C50: (u,v) midpoint of the line under the cursor (Smart Dim hover marker), or false.
    bool hoveredLineMidpoint(const ImVec2& s, float& mu, float& mv) const;
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
    // C49: per-frame alignment-inference result filled by snapPoint (dashed lines to the source
    // points; positional only — no constraint emitted, matching SolidWorks inference lines).
    struct SnapInfer {
        bool alignU = false, alignV = false;   // cursor u / v pulled to a source point's u / v
        ImVec2 srcU{0, 0}, srcV{0, 0};         // (u,v) of the alignment source point(s)
    };
    // Snap a free (u,v) to nearby sketch geometry. *snapHandle encodes what fired:
    //   "e3.p2" / "e4.center" / "e5.p"  point snap -> coincident candidate (existing behavior)
    //   "mid:e3"                        line-midpoint snap -> midpoint-constraint candidate
    //   "rim:e5"                        circle/arc rim snap -> point_on (+ maybe tangent) candidate
    //   ""                              nothing within range
    // Midpoint/rim/alignment fire only while inferenceOn_; plain point snap is always on.
    void snapPoint(float& u, float& v, std::string& snapHandle, SnapInfer* infer = nullptr) const;
    int hitTestHandle(const ImVec2& s, std::string& handleOut, float& uOut, float& vOut) const;
    // Nearest WHOLE entity under the screen point (line segment / circle rim / arc rim distance);
    // returns the index into ents_ or -1. Used by Trim / Extend / Offset hover + pick (C29-B).
    int hitTestEntity(const ImVec2& s, float maxPx = 12.0f) const;

    // Trim preview: the exact span of ents_[idx] that a trim click at sketch point (mu,mv) would
    // remove, as a (u,v) polyline. Mirrors engine sketch_geom.split_entity (cut at every in-range
    // crossing with other lines/circles/arcs, drop the span nearest the point); when there are no
    // interior crossings the WHOLE entity is doomed (engine deletes it) and `wholeEntity` is set.
    // Returns false for unsupported entity types.
    bool trimDoomedSpan(int idx, float mu, float mv,
                        std::vector<ImVec2>& outUV, bool& wholeEntity) const;

    // ---- C40 ghost sketches (inactive-sketch rendering) ----
    struct GhostSketch {
        std::string fid;
        float frame[16] = {1,0,0,0, 0,1,0,0, 0,0,1,0, 0,0,0,1};
        std::vector<SketchEntity> ents;
        bool loaded = false;         // overview response applied
    };
    void refreshGhosts(AppState& st);   // re-fetch on (doc id, revision, exempt-fid) change
    std::vector<GhostSketch> ghosts_;
    std::string ghostsDocId_;
    std::string ghostsExemptFid_;    // C48: PM create-preview feature whose consumption is ignored
    int ghostsRevision_ = -999;
    int ghostsGen_ = 0;              // supersede guard for in-flight overview responses

    bool active_ = false;
    bool isNewEmpty_ = false;        // entered a freshly-created sketch (delete on cancel-exit)
    std::string fid_;
    int dof_ = 0;
    std::string status_;             // under|full|over
    std::vector<std::string> conflicting_;   // C50: constraint ids the solver flagged conflicting
    std::vector<std::string> redundant_;     // C50: constraint ids that remove no DOF (over-define)

    // C50: SolidWorks "Make Dimension Driven?" prompt. When a just-committed Smart Dimension
    // over-defines the sketch (its id lands in conflicting_), this modal offers Make Driven /
    // Keep Driving / Cancel(remove) instead of silently leaving an over-defined sketch.
    bool overDefOpen_ = false;
    std::string overDefCid_;         // the offending (just-added) constraint id
    ImVec2 overDefPos_{0, 0};
    void drawOverDefinePrompt(AppState& st);
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

    // C49: master inference toggle (SolidWorks "sketch snaps/relations"). Default on; no ribbon
    // UI here — the orchestrator wires a toggle later. When off, only the plain endpoint/center
    // coincident snap stays (pre-C49 behavior); h/v force, alignment lines, midpoint, rim and
    // tangent inference all stand down and no auto h/v relation is emitted.
    bool inferenceOn_ = true;

    // Sketch grid visibility — OFF by default (SolidWorks-style); toggled from the in-sketch
    // right-click context menu. The origin marker draws regardless.
    bool showGrid_ = false;

    // active tool state
    SketchTool tool_ = SketchTool::None;
    std::vector<ImVec2> toolUV_;     // in-progress click points in (u,v)
    std::vector<std::string> toolSnaps_;  // per-click snap record (snapPoint encoding, "" = none)
    // C37-D: id of the previous chained Line segment, so the next segment's p1 gets a coincident to
    // its p2 (the chain start handle is otherwise dropped). Cleared by resetTool via cancelTool.
    std::string chainPrevId_;
    float conicRho_ = 0.5f;          // Conic tool: live shoulder weight 0..1 during the rho pick

    // Emit the 4 lines of a quad (corners A,B,C,D in order) as one chained loop with the given
    // relation kinds (mirrors the corner-rect op pattern; relations[i] applies to edge i->i+1,
    // "" = none). Used by the 3pt/center/parallelogram rectangle variants.
    void addQuadLoop(AppState& st, const ImVec2 corners[4], const char* relations[4]);
    // C42: emit N lines as a closed coincident-chained loop (generalized addQuadLoop, no per-edge
    // h/v). equalEdges adds "equal" relations between consecutive edges (Polygon regularity).
    void addPolyLoop(AppState& st, const std::vector<ImVec2>& corners, bool equalEdges);
    // C42: Polygon tool side count (set with number keys 3-9 while the tool is armed).
    int polySides_ = 6;

    // C42: minimal "Add Relation" popup (ribbon sk_relations with 1-2 entities selected). The
    // selection is snapshotted into relIds_ when the popup opens (selIds_ may churn).
    bool relPopupOpen_ = false;
    ImVec2 relPopupPos_{0, 0};
    std::vector<std::string> relIds_;

    // drag state
    std::string dragHandle_;
    bool dragging_ = false;
    int dragFrame_ = -1;             // last frame a sketch_drag was issued (throttle: once/frame)
    // C50: click-vs-drag on a point handle. Mouse-down near an endpoint/center ARMS a potential
    // drag; moving past kDragThreshPx starts the drag, releasing without moving SELECTS that point
    // handle (so vertices can be picked for coincident / alignment relations, SolidWorks-style).
    bool dragArmed_ = false;
    std::string pendingHandle_;
    ImVec2 dragDownPos_{0, 0};

    // C41 (T2): in-sketch selection (tool_ == None). Left-click on/near an entity selects it
    // (Ctrl+click toggles multi-select); Delete/Backspace deletes the selection via
    // sketch_delete_entity ops. Cleared by resetTool (and thus every exit/cancel path) and
    // pruned of vanished ids on every applyEnterResult refill.
    std::vector<std::string> selIds_;
    bool isSelected(const std::string& id) const;

    // Trim power-sweep state: LMB held after a trim keeps trimming entities the cursor crosses.
    bool trimSweeping_ = false;
    ImVec2 trimSweepLast_{0, 0};

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
    // External references projected into this sketch (key = datum string / selector JSON dump,
    // value = the fixed construction entity id), so re-picking the same source reuses one entity.
    std::unordered_map<std::string, std::string> projRefs_;
    // Project an external ref (worker sketch_project_ref "ref" param) into the sketch as a fixed
    // construction entity and append it to dimPicks_. Returns the entity id ("" on failure).
    std::string addProjectedDimRef(AppState& st, const nlohmann::json& ref,
                                   const std::string& srcKey);
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
    std::vector<char> dimBuf_{std::vector<char>(256, 0)};   // C38 (S7): 256B — long exprs no longer truncate
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
